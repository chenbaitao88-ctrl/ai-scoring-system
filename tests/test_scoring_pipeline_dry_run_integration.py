"""
Phase 11E-2a-prerequisite-impl-4：dry-run 绑定正式落盘评分任务集成测试。

使用真实 PipelineTaskStore + ScoringTaskCreator 创建并落盘 scoring_pipeline 任务，
再经只读 adapter 交给 ScoringPipelineOrchestrator.dry_run() 评估。

覆盖：正式任务 ready、零副作用（文件哈希/revision 不变）、Provider/Gateway/
CredentialResolver 零调用、多 item 错峰独立 dry-run、冻结配置不可覆盖。

全部合成脱敏 fixture；不调用真实模型。
"""
from __future__ import annotations

import hashlib
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
from models.score_attempt import ScoringInputProfile
from models.scoring_configuration import ScoringTaskConfiguration, ScoringTaskCreateRequest
from services.pipeline_task_store import PipelineTaskStore
from services.scoring_pipeline_orchestrator import (
    ERR_FROZEN_SELECTION_MISMATCH,
    ERR_ITEM_ALREADY_RUNNING,
    ERR_TASK_TYPE_NOT_SCORING,
    ScoringPipelineOrchestrator,
)
from services.scoring_provider_registry import ScoringProviderRegistry
from services.scoring_task_creator import ScoringTaskCreator, ScoringTaskError

T0 = datetime(2026, 8, 11, 3, 0, 0, tzinfo=timezone.utc)
SHA = "a" * 64
PID = "provider_demo_001"
MID = "model_demo_v1"
PROFILE_V = "profile-mixed-v1"
POLICY = "policy-001"
RUBRIC = "rubric-001"
PROMPT = "prompt-001"
SCHEMA = "score-response-v1"
CONTRACT = "scoring-provider/v1"


# ---------------- fixture 构造 ---------------- #


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
    return ScoringInputProfile(**base)


def make_record(package_id="pkg-001", record_id=None, validation_id=None):
    return EvidencePackageRecord(
        record_schema_version="evidence-sidecar/record/v1",
        record_id=record_id or f"record-{package_id}",
        package_id=package_id, package_revision=1,
        batch_id="b", submission_id="s", evidence_version="v1",
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
    """只读 adapter：从 PipelineTaskStore 读取 task/item（source 同库）。"""

    def __init__(self, store):
        self.store = store

    def get_task(self, task_id):
        return self.store.load_task(task_id)

    def get_item(self, task_id, item_id):
        return self.store.load_item(task_id, item_id)


class ZeroLookups:
    """零副作用协议实现：dry-run 不应触发这些写/调用。"""

    def __init__(self, reg, lookup, attempts=None):
        self.reg = reg
        self.lookup = lookup
        self.attempts = attempts or []
        self.snapshot_calls = 0
        self.review_calls = 0

    def get_task(self, task_id):
        return self.lookup.get_task(task_id)

    def get_item(self, task_id, item_id):
        return self.lookup.get_item(task_id, item_id)

    def get_evidence_record(self, package_id):
        return self.reg.get_evidence_record(package_id)

    def get_validation_result(self, package_id):
        return self.reg.get_validation_result(package_id)

    def get_input_profile(self, profile_version):
        return FakeProfileLookup().get_input_profile(profile_version)

    def list_attempts_for_item(self, task_id, item_id):
        return list(self.attempts)

    def get_attempt(self, attempt_id):
        return None

    def has_successful_snapshot(self, task_id, item_id, attempt_id=None):
        return False

    def get_snapshot(self, snapshot_id):
        return None

    def has_open_review_case(self, task_id, item_id):
        return False

    def list_review_case_ids(self, task_id, item_id=None):
        return []


def make_registry(tmp_path, provider_overrides=None, capability_overrides=None):
    import json
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


def _dir_hash(path: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(path.rglob("*")):
        if f.is_file():
            h.update(f.relative_to(path).as_posix().encode("utf-8"))
            h.update(b"\0")
            h.update(f.read_bytes())
    return h.hexdigest()


def _seed_env(tmp_path, item_count=1):
    """构造 source evidence task + 真实 scoring task，返回 (store, creator, scoring_task, scoring_item)。"""
    store = PipelineTaskStore(tmp_path / "pipeline-runtime" / "tasks")
    store.tasks_dir.mkdir(parents=True, exist_ok=True)
    # source evidence task（1~item_count 个 source item，不同 package）
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
        batch_id="batch-i4", source_task_id=source_task_id,
        source_item_ids=[i.item_id for i in src_items],
        configuration=make_config(), concurrency=2)
    scoring_task, created = creator.create_scoring_task(request)
    assert created is True
    scoring_item = store.load_item(scoring_task.task_id, scoring_task.item_index[0].item_id)
    return store, reg, lookup, scoring_task, scoring_item, source_task_id


# ---------------- 测试 ---------------- #


def _make_orchestrator(store, reg, lookup, registry, attempts=None):
    z = ZeroLookups(reg, StoreLookup(store), attempts=attempts or [])
    return ScoringPipelineOrchestrator(
        task_item_lookup=z, evidence_lookup=z, attempt_lookup=z, success_lookup=z,
        review_case_lookup=z, profile_lookup=z, registry=registry, clock=lambda: T0), z


def test_real_persisted_scoring_task_ready(tmp_path):
    """ScoringTaskCreator 创建并落盘的 scoring task -> dry_run ready（只传任务身份）。"""
    store, reg, lookup, task, item, _ = _seed_env(tmp_path)
    registry = make_registry(tmp_path)
    orch, z = _make_orchestrator(store, reg, lookup, registry)
    plan = orch.dry_run(task.task_id, item.item_id, dry_run_request_id="i4-001")
    assert plan.decision == "ready"
    assert plan.provider_id == PID
    assert plan.model_id == MID
    assert plan.scoring_policy_version == POLICY
    assert plan.rubric_version == RUBRIC
    assert plan.prompt_version == PROMPT
    assert plan.response_schema_version == SCHEMA
    assert plan.provider_call_planned is True


def test_evidence_task_rejected_by_type(tmp_path):
    """evidence preparation task 被类型检查拒绝（不返回 ready）。"""
    store, reg, lookup, task, item, source_task_id = _seed_env(tmp_path)
    registry = make_registry(tmp_path)
    orch, z = _make_orchestrator(store, reg, lookup, registry)
    plan = orch.dry_run(source_task_id, item.source_item_id, dry_run_request_id="i4-002")
    assert plan.decision == "blocked"
    assert plan.blocking_error_code == ERR_TASK_TYPE_NOT_SCORING


def test_frozen_selection_cannot_be_overridden(tmp_path):
    """请求参数替换 Provider/model/profile -> 冻结选择冲突。"""
    store, reg, lookup, task, item, _ = _seed_env(tmp_path)
    registry = make_registry(tmp_path)
    orch, z = _make_orchestrator(store, reg, lookup, registry)
    plan = orch.dry_run(task.task_id, item.item_id, dry_run_request_id="i4-003",
                        provider_id="provider_other")
    assert plan.blocking_error_code == ERR_FROZEN_SELECTION_MISMATCH
    plan2 = orch.dry_run(task.task_id, item.item_id, dry_run_request_id="i4-003b",
                         profile_version="profile-other-v1")
    assert plan2.blocking_error_code == ERR_FROZEN_SELECTION_MISMATCH


def test_dry_run_zero_side_effect(tmp_path):
    """dry-run 前后 task/item/event 文件哈希与 revision 均不变化。"""
    store, reg, lookup, task, item, _ = _seed_env(tmp_path)
    registry = make_registry(tmp_path)
    orch, z = _make_orchestrator(store, reg, lookup, registry)
    task_dir = store.tasks_dir / task.task_id
    before = _dir_hash(task_dir)
    before_rev = store.load_task(task.task_id).revision
    plan = orch.dry_run(task.task_id, item.item_id, dry_run_request_id="i4-004")
    assert plan.decision == "ready"
    after = _dir_hash(task_dir)
    assert after == before
    assert store.load_task(task.task_id).revision == before_rev


def test_zero_provider_gateway_calls(tmp_path):
    """dry-run 不调用 Provider/Gateway/CredentialResolver（无网络/凭据副作用）。"""
    store, reg, lookup, task, item, _ = _seed_env(tmp_path)
    registry = make_registry(tmp_path)
    orch, z = _make_orchestrator(store, reg, lookup, registry)
    plan = orch.dry_run(task.task_id, item.item_id, dry_run_request_id="i4-005")
    assert plan.decision == "ready"
    # 只读协议未产生任何写调用（attempt/snapshot/review 全为假值，无写入接口可触发）
    assert plan.provider_call_planned is True  # 仅计划，不执行
    # 无事件追加
    events_before = len(store.load_event_log(task.task_id))
    assert events_before == 1  # 仅 task_created
    orch.dry_run(task.task_id, item.item_id, dry_run_request_id="i4-005b")
    assert len(store.load_event_log(task.task_id)) == events_before


def test_staggered_item_independent_dry_run(tmp_path):
    """多 item 错峰：A 推进到 review，B 仍在 score/pending 可独立 dry-run。"""
    store, reg, lookup, task, item, _ = _seed_env(tmp_path, item_count=2)
    registry = make_registry(tmp_path)
    orch, z = _make_orchestrator(store, reg, lookup, registry)
    # 把 item A 推进到 review/pending（真实 Manager 状态推进）
    from services.pipeline_task_manager import PipelineTaskManager
    clock = [T0]

    def now():
        clock[0] = clock[0] + timedelta(seconds=1)
        return clock[0]

    mgr = PipelineTaskManager(store, reg, clock=now, uuid_factory=_uuid)
    t = mgr.start_task(task.task_id, expected_revision=task.revision)
    item_a = store.load_item(task.task_id, task.item_index[0].item_id)
    ia = mgr.start_item_stage(task.task_id, item_a.item_id,
                              expected_task_revision=t.revision, expected_item_revision=item_a.item_revision)
    t2 = store.load_task(task.task_id)
    mgr.complete_scoring_item_stage(
        task.task_id, item_a.item_id,
        expected_task_revision=t2.revision, expected_item_revision=ia.item_revision,
        attempt_id=_uuid())
    assert store.load_item(task.task_id, item_a.item_id).current_stage == "review"
    item_b = store.load_item(task.task_id, task.item_index[1].item_id)
    assert item_b.current_stage == "score" and item_b.status == "pending"
    # B 独立 dry-run -> ready
    plan = orch.dry_run(task.task_id, item_b.item_id, dry_run_request_id="i4-006")
    assert plan.decision == "ready"
    assert plan.item_id == item_b.item_id


def test_running_item_not_ready(tmp_path):
    """running item 阻断（防并发重复）。"""
    store, reg, lookup, task, item, _ = _seed_env(tmp_path)
    registry = make_registry(tmp_path)
    orch, z = _make_orchestrator(store, reg, lookup, registry)
    from services.pipeline_task_manager import PipelineTaskManager
    mgr = PipelineTaskManager(store, reg, clock=lambda: T0, uuid_factory=_uuid)
    t = mgr.start_task(task.task_id, expected_revision=task.revision)
    mgr.start_item_stage(task.task_id, item.item_id,
                         expected_task_revision=t.revision, expected_item_revision=item.item_revision)
    plan = orch.dry_run(task.task_id, item.item_id, dry_run_request_id="i4-007")
    assert plan.blocking_error_code == ERR_ITEM_ALREADY_RUNNING
