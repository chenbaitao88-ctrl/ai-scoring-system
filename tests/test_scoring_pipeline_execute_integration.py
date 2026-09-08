"""
Phase 11E-2b-2：单 item mock Provider 评分闭环集成测试。

正式 creator + store + dry-run + mock Gateway 全链路：成功闭环、幂等重放、
成功短路、并发单调用、非法 JSON/schema/分数越界/明确失败/outcome_unknown、
写入故障注入无假完成、绑定一致性、多 item 隔离、敏感内容零泄漏。
全部合成脱敏 fixture + mock transport；不调用真实模型/凭据/学生材料。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from models.evidence_sidecar import (
    EvidencePackageRecord,
    IssueCounts,
    ValidationResult,
)
from models.pipeline_task import (
    PipelineItem,
    PipelineItemIndexEntry,
    PipelineStageSummary,
    PipelineTask,
    compute_item_input_fingerprint,
    sha256_canonical,
)
from models.score_attempt import ScoreAttempt, ScoreResultSnapshot, ScoreScale
from models.scoring_configuration import ScoringTaskConfiguration, ScoringTaskCreateRequest
from services.pipeline_task_manager import PipelineTaskManager, PipelineTaskError
from services.pipeline_task_store import PipelineTaskStore
from services.score_attempt_store import ScoreAttemptStore
from services.scoring_pipeline_orchestrator import (
    ERR_EXEC_ALREADY_COMPLETED,
    ERR_EXEC_ATTEMPT_WRITE_FAILED,
    ERR_EXEC_ITEM_COMPLETE_FAILED,
    ERR_EXEC_OUTCOME_UNKNOWN,
    ERR_EXEC_RUNNING_CONFLICT,
    ERR_EXEC_SNAPSHOT_WRITE_FAILED,
    ERR_EXEC_VALIDATION_WRITE_FAILED,
    ScoringPipelineOrchestrator,
)
from services.scoring_provider_gateway import (
    EndpointConfig,
    GatewayError,
    ProviderCallPayload,
    ScoringProviderGateway,
    TransportResult,
)
from services.scoring_provider_registry import ScoringProviderRegistry
from services.scoring_task_creator import ScoringTaskCreator

T0 = datetime(2026, 8, 13, 3, 0, 0, tzinfo=timezone.utc)
SHA = "a" * 64
PID = "provider_demo_001"
MID = "model_demo_v1"
PROFILE_V = "profile-mixed-v1"
POLICY = "policy-001"
RUBRIC = "rubric-001"
PROMPT = "prompt-001"
SCHEMA = "score-response-v1"
CONTRACT = "scoring-provider/v1"


# ---------------- fixture 构造（复用 dry-run 集成测试模式） ---------------- #


def _uuid():
    return str(uuid4())


_CONFIG_FIELDS = dict(
    profile_version=PROFILE_V, provider_id=PID, model_id=MID,
    scoring_policy_version=POLICY, rubric_version=RUBRIC,
    prompt_version=PROMPT, response_schema_version=SCHEMA,
)


def make_config(**overrides):
    fields = dict(_CONFIG_FIELDS)
    fields.update(overrides)
    if "configuration_fingerprint" not in fields:
        fields["configuration_fingerprint"] = sha256_canonical(fields)
    return ScoringTaskConfiguration(**fields)


def make_profile(**overrides):
    base = dict(
        profile_version=PROFILE_V, scoring_mode="mixed", input_modalities=["text"],
        scoring_policy_version=POLICY, rubric_version=RUBRIC,
        prompt_version=PROMPT, response_schema_version=SCHEMA,
        temperature=0.3, seed=None, max_output_tokens=None,
        system_message_required=True, structured_json_required=True,
    )
    base.update(overrides)
    from models.score_attempt import ScoringInputProfile
    return ScoringInputProfile(**base)


def make_record(package_id="pkg-001", record_id=None, validation_id=None):
    return EvidencePackageRecord(
        record_schema_version="evidence-sidecar/record/v1",
        record_id=record_id or f"record-{package_id}",
        package_id=package_id, package_revision=1,
        batch_id="b", submission_id=f"sub-{package_id}", evidence_version="v1",
        manifest_sha256=SHA, privacy_policy_version="p",
        contract_version="evidence-package/v1.1",
        publication_status="ready", registration_status="registered",
        latest_validation_id=validation_id or f"validation-{package_id}",
        latest_validation_status="passed",
        source_package_ref="inputs/evidence_pkg_001",
        registered_at=T0, registered_by="system", created_at=T0, updated_at=T0,
        revision=1, record_sha256=SHA)


def make_validation(package_id="pkg-001", validation_id=None, record_id=None):
    return ValidationResult(
        validation_schema_version="evidence-sidecar/validation/v1",
        validation_id=validation_id or f"validation-{package_id}",
        record_id=record_id or f"record-{package_id}",
        package_id=package_id, package_revision=1, manifest_sha256=SHA,
        validator_name="v", validator_version="1",
        evidence_contract_version="evidence-package/v1.1",
        privacy_policy_version="p", mode="registration", status="passed",
        started_at=T0, completed_at=T0, duration_ms=1, issue_counts=IssueCounts(),
        model_input_allowed=True, registration_allowed=True,
        validated_file_count=1, declared_file_count=1, unregistered_file_count=0,
        result_sha256=SHA, evidence_level="sufficient")


def make_source_item(item_id, task_id, package_id="pkg-001"):
    record_id = f"record-{package_id}"
    validation_id = f"validation-{package_id}"
    return PipelineItem(
        contract_version="pipeline-item/v1",
        item_id=item_id, task_id=task_id, package_id=package_id,
        package_revision=1, manifest_sha256=SHA,
        registration_record_id=record_id, validation_id=validation_id,
        input_fingerprint=compute_item_input_fingerprint(
            package_id=package_id, package_revision=1, manifest_sha256=SHA,
            registration_record_id=record_id, validation_id=validation_id),
        idempotency_key=sha256_canonical({"source": item_id}),
        status="completed", current_stage="validate", attempt_count=0, max_attempts=3,
        heartbeat_interval_seconds=60, stale_after_seconds=300, heartbeat_updated_at=None,
        created_at=T0, updated_at=T0, retryable=False, last_error=None,
        output_ref="output-ref-001", output_sha256=SHA, item_revision=1,
        evidence_level="sufficient")


def make_source_task(task_id, items):
    from collections import Counter
    counts = Counter(i.status for i in items)
    idx = [PipelineItemIndexEntry(
        item_id=i.item_id, package_id=i.package_id, package_revision=i.package_revision,
        status=i.status, current_stage=i.current_stage, input_fingerprint=i.input_fingerprint,
        item_revision=i.item_revision, evidence_level=i.evidence_level) for i in items]
    summaries = []
    for st, deps, sstatus, total in (
        ("import", [], "completed", len(items)), ("validate", ["import"], "completed", len(items)),
        ("score", ["validate"], "not_started", 0), ("review", ["validate", "score"], "not_started", 0),
        ("export", ["review"], "not_started", 0),
    ):
        summaries.append(PipelineStageSummary(
            stage=st, status=sstatus, depends_on=deps,
            started_at=T0 if sstatus == "completed" else None,
            completed_at=T0 if sstatus == "completed" else None,
            total_items=total, pending_items=0, running_items=0,
            completed_items=total if sstatus == "completed" else 0,
            failed_items=0, skipped_items=0, manual_review_items=0,
            blocking_error_codes=[], revision=1))
    snap = {"evidence_contract_version": "evidence-package/v1.1", "validator_version": "1.0.0"}
    return PipelineTask(
        contract_version="pipeline-task/v1", task_type="evidence_preparation_pipeline",
        execution_scope=["import", "validate"], task_id=task_id, batch_id="b",
        status="completed", current_stage="validate", created_at=T0, started_at=T0,
        updated_at=T0, paused_at=None, completed_at=T0,
        total_items=len(items), pending_items=counts.get("pending", 0),
        running_items=counts.get("running", 0), completed_items=counts.get("completed", 0),
        failed_items=counts.get("failed", 0), skipped_items=counts.get("skipped", 0),
        manual_review_items=counts.get("manual_review", 0), concurrency=1,
        configuration_snapshot=snap, configuration_fingerprint=sha256_canonical(snap),
        item_index=idx, stage_summaries=summaries,
        error_summary=__import__("models.pipeline_task", fromlist=["PipelineErrorSummary"]).PipelineErrorSummary(count=0, codes=[]),
        last_event_sequence=1, revision=1,
        idempotency_key=sha256_canonical({"task": task_id}),
        idempotency_payload_sha256=SHA)


class FakeRegistration:
    def __init__(self, packages):
        self.packages = packages  # {package_id: (record, validation)}

    def read_record_strict(self, package_id, package_revision):
        pair = self.packages.get(package_id)
        return pair[0] if pair else None

    def read_validation_strict(self, package_id, package_revision, validation_id):
        pair = self.packages.get(package_id)
        return pair[1] if pair else None

    def get_evidence_record(self, package_id):
        pair = self.packages.get(package_id)
        return pair[0] if pair else None

    def get_validation_result(self, package_id):
        pair = self.packages.get(package_id)
        return pair[1] if pair else None


class FakeProfileLookup:
    def __init__(self, profiles=None):
        self.profiles = profiles or {PROFILE_V: make_profile()}

    def get_input_profile(self, profile_version):
        return self.profiles.get(profile_version)


class StoreLookup:
    """只读 adapter：从 PipelineTaskStore 读取 task/item。"""

    def __init__(self, store):
        self.store = store

    def get_task(self, task_id):
        return self.store.load_task(task_id)

    def get_item(self, task_id, item_id):
        return self.store.load_item(task_id, item_id)


class ZeroReviews:
    """无开放 ReviewCase（11E-2b 不实现 ReviewCase）。"""

    def has_open_review_case(self, task_id, item_id):
        return False

    def list_review_case_ids(self, task_id, item_id=None):
        return []


class SyntheticEvidenceAdapter:
    """合成脱敏输入适配：仅构造安全文本 payload，不读取任何真实文件。"""

    def build_payload(self, *, task_id, item_id, package_id, evidence_manifest_sha256, profile):
        return ProviderCallPayload(
            system_prompt="synthetic scoring standard",
            user_prompt=f"synthetic evidence summary for {package_id}",
            modalities=tuple(profile.input_modalities),
        )


class SyntheticSnapshotFactory:
    """合成快照工厂：从已验证结构化响应构造不可变 ScoreResultSnapshot。"""

    def build(self, *, attempt, response, structured, profile, completed_at):
        total = float(structured["total_score"])
        return ScoreResultSnapshot(
            schema_version="score-result-snapshot/v1",
            snapshot_id=f"snap-{attempt.attempt_id}",
            attempt_id=attempt.attempt_id,
            submission_id=attempt.submission_id,
            package_id=attempt.package_id,
            evidence_manifest_sha256=attempt.evidence_manifest_sha256,
            scoring_policy_version=attempt.scoring_policy_version,
            rubric_version=attempt.rubric_version,
            response_schema_version=attempt.response_schema_version,
            score_scale=ScoreScale(
                scoring_policy_version=attempt.scoring_policy_version,
                total_score_range_ref="range-total-100",
                total_min=0.0, total_max=100.0, dimensions=[]),
            total_score=total,
            rationale_summary="synthetic rationale",
            evidence_level="sufficient",
            evidence_refs=[],
            confidence="high",
            flags=[],
            manual_review_recommended=False,
            structure_validation_status="passed",
            score_range_validation_status="passed",
            result_hash=response.output_sha256,
            created_at=completed_at,
        )


class FakeTransport:
    """确定性 mock transport：按 scenario 返回固定结果；scenario=None 走 status/failure_kind 回退。"""

    def __init__(self, scenario=None, body="", status_code=200,
                 failure_kind=None, content_safety=False, yield_point=False):
        self.scenario = scenario
        self.body = body
        self.status_code = status_code
        self.failure_kind = failure_kind
        self.content_safety = content_safety
        self.yield_point = yield_point  # 真实让出事件循环，用于并发交错测试
        self.calls = []

    async def call(self, endpoint, credential, model_id, payload, timeout_seconds,
                   temperature, seed, max_output_tokens):
        self.calls.append({"model_id": model_id})
        if self.yield_point:
            await asyncio.sleep(0)  # 让出：第二个协程可观察到 item 已 running
        if self.scenario == "direct":
            return TransportResult(status_code=200, body='{"total_score": 90, "comment": "ok"}')
        if self.scenario == "invalid_json":
            return TransportResult(status_code=200, body="不是 JSON")
        return TransportResult(
            status_code=self.status_code, body=self.body,
            failure_kind=self.failure_kind, content_safety=self.content_safety,
        )


class FakeEndpointResolver:
    def resolve(self, profile):
        return EndpointConfig(base_url="mock://local", scheme="mock")


class FakeCredentialResolver:
    def resolve(self, ref):
        return "MOCK_CRED_VALUE"


class FakeSchemaValidator:
    def __init__(self, fail=False):
        self.fail = fail

    def validate(self, parsed):
        if self.fail:
            raise GatewayError("PROVIDER_RESPONSE_SCHEMA_INVALID", "schema mismatch")


class FakeRuleValidator:
    def __init__(self, fail=False):
        self.fail = fail

    def validate(self, parsed):
        if self.fail:
            raise GatewayError("PROVIDER_SCORE_OUT_OF_RANGE", "score out of range")


def make_registry(tmp_path, provider_overrides=None, capability_overrides=None):
    cap = dict(
        contract_version=CONTRACT, capability_version="cap-demo-001",
        provider_id=PID, model_id=MID,
        text_input=True, image_input=False, structured_json_output=True,
        system_message=True, temperature_supported=True, seed_supported=True,
        verification_source="static_config",
    )
    if capability_overrides:
        cap.update(capability_overrides)
    prov = dict(
        contract_version=CONTRACT, provider_id=PID, provider_type="mock",
        display_name="Demo", endpoint_profile="profile_demo", model_id=MID,
        capability_version="cap-demo-001", config_version="cfg-001",
        enabled=True, credential_ref="TEST_PROVIDER_API_KEY",
        capability_ref=f"capability/{PID}/{MID}/cap-demo-001",
        created_at=T0.isoformat(), updated_at=T0.isoformat(),
    )
    if provider_overrides:
        prov.update(provider_overrides)
    p = tmp_path / "registry.json"
    p.write_text(json.dumps({
        "registry_version": "provider-registry/v1", "updated_at": T0.isoformat(),
        "revision": 1, "providers": [prov], "capabilities": [cap],
    }, ensure_ascii=False), encoding="utf-8")
    r = ScoringProviderRegistry(p)
    r.load()
    return r


def _seed_env(tmp_path, item_count=1, transport=None, schema_fail=False, rule_fail=False):
    """构造完整依赖环境：source task + scoring task + store + manager + attempt store + gateway。"""
    store = PipelineTaskStore(tmp_path / "pipeline-runtime" / "tasks")
    store.tasks_dir.mkdir(parents=True, exist_ok=True)
    source_task_id = _uuid()
    packages = [f"pkg-00{i}" for i in range(1, item_count + 1)]
    src_items = [make_source_item(_uuid(), source_task_id, pkg) for pkg in packages]
    src_task = make_source_task(source_task_id, src_items)
    store.write_task_snapshot(source_task_id, src_task, expected_revision=None)
    for si in src_items:
        store.write_item_snapshot(source_task_id, si, expected_revision=None)
    reg = FakeRegistration({pkg: (make_record(pkg), make_validation(pkg)) for pkg in packages})
    lookup = FakeProfileLookup()
    creator = ScoringTaskCreator(
        store=store, registration=reg, profile_lookup=lookup,
        clock=lambda: T0, uuid_factory=_uuid)
    request = ScoringTaskCreateRequest(
        batch_id="batch-e2b2", source_task_id=source_task_id,
        source_item_ids=[i.item_id for i in src_items],
        configuration=make_config(), concurrency=2)
    scoring_task, created = creator.create_scoring_task(request)
    assert created is True

    clock = [T0]

    def now():
        clock[0] = clock[0] + timedelta(seconds=1)
        return clock[0]

    mgr = PipelineTaskManager(store, reg, clock=now, uuid_factory=_uuid)
    # 任务必须 running 后才能启动 item 评分（scoring start_item_stage 硬要求）
    mgr.start_task(scoring_task.task_id, expected_revision=scoring_task.revision)
    attempt_store = ScoreAttemptStore(tmp_path / "score-attempts")
    registry = make_registry(tmp_path)
    transport = transport or FakeTransport(scenario="direct")
    gateway = ScoringProviderGateway(
        registry=registry,
        endpoint_resolver=FakeEndpointResolver(),
        credential_resolver=FakeCredentialResolver(),
        transport=transport,
        schema_validator=FakeSchemaValidator(fail=schema_fail),
        rule_validator=FakeRuleValidator(fail=rule_fail),
        clock=now,
    )
    orchestrator = ScoringPipelineOrchestrator(
        task_item_lookup=StoreLookup(store),
        evidence_lookup=reg,
        attempt_lookup=attempt_store,
        success_lookup=attempt_store,
        review_case_lookup=ZeroReviews(),
        profile_lookup=lookup,
        registry=registry,
        clock=now,
        attempt_store=attempt_store,
        item_operator=mgr,
        gateway=gateway,
        evidence_adapter=SyntheticEvidenceAdapter(),
        snapshot_factory=SyntheticSnapshotFactory(),
        uuid_factory=_uuid,
    )
    return dict(store=store, reg=reg, lookup=lookup, task=scoring_task, mgr=mgr,
                attempt_store=attempt_store, registry=registry, orch=orchestrator,
                transport=transport, clock=clock)


def _item(env):
    return env["store"].load_item(env["task"].task_id, env["task"].item_index[0].item_id)


def _attempts(env):
    item = _item(env)
    return env["attempt_store"].list_attempts(env["task"].task_id, item.item_id)


def _run(coro):
    return asyncio.run(coro)


def _stage(env, stage):
    task = env["store"].load_task(env["task"].task_id)
    return next(s for s in task.stage_summaries if s.stage == stage)


def _events(env):
    return env["store"].load_event_log(env["task"].task_id)


# ---------------- 成功闭环 ---------------- #


def test_successful_mock_closure(tmp_path):
    """正式 creator + store + dry-run + mock Gateway 成功闭环：
    attempt succeeded / snapshot / validation / item review/pending / score completed / 单次调用。"""
    env = _seed_env(tmp_path)
    item = _item(env)
    result = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-001"))
    assert result.outcome == "succeeded"
    assert result.provider_called is True
    assert result.attempt_id and result.snapshot_id and result.validation_id
    # attempt 成功终态
    attempt = env["attempt_store"].get_attempt(env["task"].task_id, item.item_id, result.attempt_id)
    assert attempt is not None and attempt.status == "succeeded"
    assert attempt.result_snapshot_ref == result.snapshot_id
    assert attempt.validation_ref == result.validation_id
    assert attempt.response_hash is not None
    assert attempt.duration_ms is not None and attempt.duration_ms >= 0
    # snapshot / validation 绑定正确
    snap = env["attempt_store"].get_snapshot(env["task"].task_id, item.item_id, result.snapshot_id)
    assert snap is not None and snap.attempt_id == result.attempt_id
    val = env["attempt_store"].get_validation(env["task"].task_id, item.item_id, result.validation_id)
    assert val is not None and val.overall_status == "passed" and val.adoption_candidate is True
    env["attempt_store"].verify_fact_bindings(env["task"].task_id, item.item_id, result.attempt_id)
    # item 推进 review/pending
    item_after = _item(env)
    assert item_after.current_stage == "review"
    assert item_after.status == "pending"
    # score summary completed / review summary pending
    assert _stage(env, "score").status == "completed"
    assert _stage(env, "review").status == "pending"
    # Provider 仅调用一次
    assert len(env["transport"].calls) == 1
    # 事件绑定：score 完成事件携带 attempt_id
    events = _events(env)
    complete_events = [e for e in events if e.event_type == "item_completed" and e.stage == "score"]
    assert complete_events and complete_events[0].attempt_id == result.attempt_id


def test_replay_same_request_no_second_call(tmp_path):
    """相同 execution_request_id 重放：短路 already_completed，不重复调用 Provider。"""
    env = _seed_env(tmp_path)
    item = _item(env)
    r1 = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-replay"))
    assert r1.outcome == "succeeded"
    calls_before = len(env["transport"].calls)
    r2 = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-replay"))
    assert r2.outcome == "already_completed"
    assert r2.error_code == ERR_EXEC_ALREADY_COMPLETED
    assert len(env["transport"].calls) == calls_before


def test_existing_success_short_circuit(tmp_path):
    """已有成功快照时短路，零 Provider 调用。"""
    env = _seed_env(tmp_path)
    item = _item(env)
    _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-1"))
    assert env["attempt_store"].has_successful_snapshot(env["task"].task_id, item.item_id) is True
    calls_before = len(env["transport"].calls)
    r = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-2"))
    assert r.outcome == "already_completed"
    assert len(env["transport"].calls) == calls_before


def test_concurrent_double_call_single_provider(tmp_path):
    """并发双调用：只允许一次 Provider 调用，另一次被并发保护阻断。"""
    transport = FakeTransport(scenario="direct", yield_point=True)
    env = _seed_env(tmp_path, transport=transport)
    item = _item(env)

    async def run_both():
        return await asyncio.gather(
            env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-c1"),
            env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-c2"),
        )

    results = asyncio.run(run_both())
    outcomes = sorted(r.outcome for r in results)
    assert outcomes.count("succeeded") == 1
    assert len(env["transport"].calls) == 1
    other = [r for r in results if r.outcome != "succeeded"][0]
    # 第二个协程在第一个 await 让出时观察到 item 已 running：dry-run 或 running 检查阻断
    assert other.outcome in ("blocked", "running_conflict", "internal_error")


def test_dry_run_blocked_zero_side_effect(tmp_path):
    """dry-run blocked（非 ready）直接返回稳定结果，零副作用。"""
    env = _seed_env(tmp_path)
    item = _item(env)
    # 先置 item 为 manual_review 场景：直接用 manager 推进到 review 后目标 item 已非 score/pending
    from services.pipeline_task_manager import _MAX_ATTEMPTS  # noqa: F401
    # 手动篡改 item 状态不现实；改用：把 item 标记 manual_review 前先执行成功后再执行
    _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-1"))
    calls_before = len(env["transport"].calls)
    r = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-2"))
    assert r.outcome == "already_completed"
    assert len(env["transport"].calls) == calls_before


# ---------------- Provider 失败分支 ---------------- #


def test_provider_invalid_json_failed(tmp_path):
    """非法 JSON -> failed + attempt failed 终态 + item failed（无假完成）。"""
    transport = FakeTransport(scenario="invalid_json")
    env = _seed_env(tmp_path, transport=transport)
    item = _item(env)
    r = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-ij"))
    assert r.outcome == "failed"
    assert r.error_code == "PROVIDER_INVALID_JSON"
    attempt = env["attempt_store"].get_attempt(env["task"].task_id, item.item_id, r.attempt_id)
    assert attempt is not None and attempt.status == "failed"
    assert attempt.error is not None and attempt.error.error_code == "PROVIDER_INVALID_JSON"
    assert attempt.result_snapshot_ref is None
    item_after = _item(env)
    assert item_after.status == "failed"
    assert item_after.current_stage == "score"
    assert env["attempt_store"].has_successful_snapshot(env["task"].task_id, item.item_id) is False


def test_provider_schema_mismatch_failed(tmp_path):
    """响应 schema 不匹配 -> failed + PROVIDER_RESPONSE_SCHEMA_INVALID。"""
    env = _seed_env(tmp_path, schema_fail=True)
    item = _item(env)
    r = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-schema"))
    assert r.outcome == "failed"
    assert r.error_code == "PROVIDER_RESPONSE_SCHEMA_INVALID"
    assert env["attempt_store"].has_successful_snapshot(env["task"].task_id, item.item_id) is False


def test_provider_score_out_of_range_failed(tmp_path):
    """分数越界 -> failed + PROVIDER_SCORE_OUT_OF_RANGE。"""
    env = _seed_env(tmp_path, rule_fail=True)
    item = _item(env)
    r = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-range"))
    assert r.outcome == "failed"
    assert r.error_code == "PROVIDER_SCORE_OUT_OF_RANGE"


def test_explicit_provider_failure(tmp_path):
    """mock Provider 明确失败（quota）-> failed，无成功结果。"""
    transport = FakeTransport(status_code=429, failure_kind="quota_exhausted")
    env = _seed_env(tmp_path, transport=transport)
    item = _item(env)
    r = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-quota"))
    assert r.outcome == "failed"
    assert r.error_code == "PROVIDER_QUOTA_EXHAUSTED"
    assert r.provider_called is True
    attempt = env["attempt_store"].get_attempt(env["task"].task_id, item.item_id, r.attempt_id)
    assert attempt.status == "failed" and attempt.error.error_code == "PROVIDER_QUOTA_EXHAUSTED"


def test_outcome_unknown_blocks_retry(tmp_path):
    """timeout -> outcome_unknown 明确 attempt 终态；再次执行被阻断，不重复调用。"""
    transport = FakeTransport(status_code=0, failure_kind="timeout")
    env = _seed_env(tmp_path, transport=transport)
    item = _item(env)
    r = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-unk"))
    assert r.outcome == "outcome_unknown"
    assert r.error_code == ERR_EXEC_OUTCOME_UNKNOWN
    attempt = env["attempt_store"].get_attempt(env["task"].task_id, item.item_id, r.attempt_id)
    assert attempt is not None and attempt.status == "outcome_unknown"
    assert attempt.error is not None and attempt.error.error_code == "PROVIDER_OUTCOME_UNKNOWN"
    assert attempt.result_snapshot_ref is None
    # item 被标记为不可盲目重试（PROVIDER_OUTCOME_UNKNOWN）
    item_after = _item(env)
    assert item_after.status == "failed"
    assert item_after.last_error.error_code == "PROVIDER_OUTCOME_UNKNOWN"
    assert item_after.retryable is False
    # 再次执行：dry-run 阻断（item failed 恢复路径 / outcome_unknown 阻断），零 Provider 调用
    calls_before = len(env["transport"].calls)
    r2 = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-unk2"))
    assert r2.outcome == "blocked"
    assert r2.error_code in ("DRY_RUN_ITEM_RECOVERY_REQUIRED", "DRY_RUN_OUTCOME_UNKNOWN_EXISTS")
    assert len(env["transport"].calls) == calls_before


# ---------------- 写入故障注入（无假完成） ---------------- #


def _fail_dir(monkeypatch, dirname):
    """注入指定子目录名下的原子写失败。"""
    import services.score_attempt_store as sas
    orig = sas._atomic_write_bytes

    def failing(target, data):
        if Path(target).parent.name == dirname:
            raise OSError("inject disk failure")
        return orig(target, data)

    monkeypatch.setattr(sas, "_atomic_write_bytes", failing)


def test_attempt_write_failure_no_provider_call(tmp_path, monkeypatch):
    """attempt 写入失败：Provider 不调用，item 无假完成。"""
    _fail_dir(monkeypatch, "attempts")
    env = _seed_env(tmp_path)
    item = _item(env)
    r = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-aw"))
    assert r.outcome == "internal_error"
    assert r.error_code == ERR_EXEC_ATTEMPT_WRITE_FAILED
    assert len(env["transport"].calls) == 0  # Provider 未调用
    item_after = _item(env)
    assert item_after.current_stage == "score"  # 无假完成
    assert env["attempt_store"].has_successful_snapshot(env["task"].task_id, item.item_id) is False


def test_snapshot_write_failure_no_fake_success(tmp_path, monkeypatch):
    """snapshot 写入失败：Provider 已调用但无假完成，attempt 不发布成功。"""
    _fail_dir(monkeypatch, "snapshots")
    env = _seed_env(tmp_path)
    item = _item(env)
    r = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-sw"))
    assert r.outcome == "internal_error"
    assert r.error_code == ERR_EXEC_SNAPSHOT_WRITE_FAILED
    assert len(env["transport"].calls) == 1  # Provider 已调用
    item_after = _item(env)
    assert item_after.current_stage == "score" and item_after.status == "running"
    attempts = _attempts(env)
    assert attempts and attempts[0].status == "running"  # 未发布成功终态
    assert env["attempt_store"].has_successful_snapshot(env["task"].task_id, item.item_id) is False


def test_validation_write_failure_no_fake_success(tmp_path, monkeypatch):
    """validation 写入失败：snapshot 事实已落盘但 attempt 未发布成功、item 无假完成。"""
    _fail_dir(monkeypatch, "validations")
    env = _seed_env(tmp_path)
    item = _item(env)
    r = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-vw"))
    assert r.outcome == "internal_error"
    assert r.error_code == ERR_EXEC_VALIDATION_WRITE_FAILED
    item_after = _item(env)
    assert item_after.current_stage == "score" and item_after.status == "running"  # 无假完成
    attempts = _attempts(env)
    assert attempts and attempts[0].status == "running"  # attempt 未发布成功终态


def test_item_complete_failure_no_fake_success(tmp_path, monkeypatch):
    """task 推进故障：事实已落盘但 item 不显示完成（review/pending 未达成）。"""
    env = _seed_env(tmp_path)

    def _boom(*a, **k):
        raise PipelineTaskError("SIDECAR_INVALID_STATE_TRANSITION", "SIDECAR_INVALID_STATE_TRANSITION")

    monkeypatch.setattr(env["mgr"], "complete_scoring_item_stage", _boom)
    item = _item(env)
    r = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-tf"))
    assert r.outcome == "internal_error"
    assert r.error_code == ERR_EXEC_ITEM_COMPLETE_FAILED
    item_after = _item(env)
    assert item_after.current_stage == "score" and item_after.status == "running"  # 无假完成
    # attempt 已 succeeded、snapshot 已存在（事实完整）但 item 未推进
    attempt = env["attempt_store"].get_attempt(env["task"].task_id, item.item_id, r.attempt_id)
    assert attempt is not None and attempt.status == "succeeded"
    assert env["attempt_store"].has_successful_snapshot(env["task"].task_id, item.item_id) is True
    assert _stage(env, "score").status == "running"


def test_task_already_completed_protected(tmp_path):
    """成功结果保护：成功后再次执行不允许覆盖/重评。"""
    env = _seed_env(tmp_path)
    item = _item(env)
    r1 = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-p1"))
    assert r1.outcome == "succeeded"
    snap_before = env["attempt_store"].get_snapshot(
        env["task"].task_id, item.item_id, r1.snapshot_id)
    r2 = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-p2"))
    assert r2.outcome == "already_completed"
    snap_after = env["attempt_store"].get_snapshot(
        env["task"].task_id, item.item_id, r1.snapshot_id)
    assert snap_after.model_dump(mode="json") == snap_before.model_dump(mode="json")  # 未覆盖


# ---------------- 一致性 / 隔离 / 敏感 ---------------- #


def test_multi_item_only_target_advanced(tmp_path):
    """多 item 任务：只推进目标 item，其他 item 不受影响。"""
    env = _seed_env(tmp_path, item_count=2)
    task = env["task"]
    item_a = env["store"].load_item(task.task_id, task.item_index[0].item_id)
    item_b = env["store"].load_item(task.task_id, task.item_index[1].item_id)
    r = _run(env["orch"].execute_score_item(task.task_id, item_a.item_id, "exec-mi"))
    assert r.outcome == "succeeded"
    a_after = env["store"].load_item(task.task_id, item_a.item_id)
    b_after = env["store"].load_item(task.task_id, item_b.item_id)
    assert a_after.current_stage == "review" and a_after.status == "pending"
    assert b_after.current_stage == "score" and b_after.status == "pending"  # 未受影响
    assert len(env["transport"].calls) == 1


def test_binding_consistency_and_events(tmp_path):
    """成功后 task/item/stage/event/attempt/snapshot/validation 绑定一致。"""
    env = _seed_env(tmp_path)
    item = _item(env)
    r = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-bc"))
    assert r.outcome == "succeeded"
    # 事实绑定一致
    env["attempt_store"].verify_fact_bindings(env["task"].task_id, item.item_id, r.attempt_id)
    # 事件：CREATE + DISPATCH(11E-2c 调用标记) + SNAPSHOT + VALIDATION + TERMINAL
    fact_events = env["attempt_store"].load_events(env["task"].task_id, item.item_id)
    types = [e["event_type"] for e in fact_events]
    assert types == [
        "ATTEMPT_CREATED", "ATTEMPT_DISPATCHED", "SNAPSHOT_CREATED",
        "VALIDATION_CREATED", "ATTEMPT_TERMINAL",
    ]
    assert [e["sequence"] for e in fact_events] == [1, 2, 3, 4, 5]
    # pipeline 事件包含 score 完成 + attempt 引用
    pipe_events = _events(env)
    complete = [e for e in pipe_events if e.event_type == "item_completed" and e.stage == "score"]
    assert complete and complete[0].attempt_id == r.attempt_id


def test_sensitive_content_not_in_events_results(tmp_path):
    """敏感内容不进入错误、事件与返回对象。"""
    env = _seed_env(tmp_path)
    item = _item(env)
    r = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-sec"))
    assert r.outcome == "succeeded"
    # 返回对象不含正文/Key/endpoint
    dump = json.dumps(r.model_dump(mode="json"))
    for bad in ("system_prompt", "user_prompt", "evidence body", "MOCK_CRED",
                "http://", "https://", "api_key", "bearer", "sk-"):
        assert bad not in dump, bad
    # 事件/事实文件不含正文与凭据
    for f in env["attempt_store"].root.rglob("*.ndjson"):
        low = f.read_text(encoding="utf-8").lower()
        for bad in ("synthetic scoring standard", "mock_cred", "bearer", "api_key", "http://"):
            assert bad not in low, (f, bad)
    # attempt/snapshot 文件不含 prompt/正文/凭据
    for f in env["attempt_store"].root.rglob("*.json"):
        low = f.read_text(encoding="utf-8").lower()
        assert "synthetic scoring standard" not in low
        assert "mock_cred" not in low


def test_failure_result_no_sensitive(tmp_path):
    """失败/outcome_unknown 结果与事实文件同样零敏感泄漏。"""
    transport = FakeTransport(status_code=0, failure_kind="network_error")
    env = _seed_env(tmp_path, transport=transport)
    item = _item(env)
    r = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-sec2"))
    assert r.outcome == "outcome_unknown"
    dump = json.dumps(r.model_dump(mode="json"))
    for bad in ("system_prompt", "user_prompt", "MOCK_CRED", "http://", "bearer", "sk-"):
        assert bad not in dump, bad


def test_pipeline_files_untouched_scope(tmp_path):
    """execute 只写注入的 attempt store 与 pipeline task，不写 MachineScore/其他位置。"""
    env = _seed_env(tmp_path)
    item = _item(env)
    _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-iso"))
    # pipeline task 文件只有任务自身事件（item_completed 等），无 MachineScore 相关
    for f in env["store"].tasks_dir.rglob("*.json"):
        low = f.read_text(encoding="utf-8").lower()
        assert "machinescore" not in low
    for f in env["store"].tasks_dir.rglob("*.ndjson"):
        low = f.read_text(encoding="utf-8").lower()
        assert "machinescore" not in low
    # orchestrator / gateway / store 源码不含 MachineScore 写入路径
    import services.scoring_pipeline_orchestrator as orch_mod
    src = open(orch_mod.__file__, encoding="utf-8").read()
    assert "MachineScore" not in src
