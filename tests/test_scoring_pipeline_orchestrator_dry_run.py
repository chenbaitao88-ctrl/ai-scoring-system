"""
Phase 11E-2a-fix-1：ScoringPipelineOrchestrator dry-run 合成测试。

权威准入修正验证：
- 当前 11C evidence_preparation_pipeline（execution_scope=import/validate）必须被 score scope 阻断；
  ready 路径只能使用专门的合成只读评分任务 fixture（scope 含 score），不得伪装为当前 11C 正式任务。
- 证据权威使用 EvidencePackageRecord（sidecar 登记事实）与 ValidationResult 全绑定核对。
- 评分口径由 ScoringInputProfile 权威驱动，能力匹配覆盖 text/image/json/system_message/
  temperature/seed/max_output_tokens/max_images/image_formats。
- 零副作用：不调用 Gateway/Store/CredentialResolver/环境变量。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from models.evidence_sidecar import (
    EvidencePackageRecord,
    IssueCounts,
    ValidationResult,
)
from models.pipeline_task import compute_item_input_fingerprint, sha256_canonical
from models.scoring_configuration import ScoringTaskConfiguration
from models.score_attempt import (
    DryRunPlan,
    ProviderRequestPreview,
    ScoringInputProfile,
)
from services.scoring_pipeline_orchestrator import (
    ERR_CAPABILITY_MISMATCH,
    ERR_EVIDENCE_MISMATCH,
    ERR_EVIDENCE_RECORD_MISSING,
    ERR_ITEM_ALREADY_RUNNING,
    ERR_ITEM_RECOVERY_REQUIRED,
    ERR_ITEM_STATE_NOT_ALLOWED,
    ERR_MANUAL_ONLY,
    ERR_OPEN_REVIEW_CASE,
    ERR_OUTCOME_UNKNOWN_EXISTS,
    ERR_PROFILE_MISSING,
    ERR_PROVIDER_DISABLED,
    ERR_PROVIDER_NOT_FOUND,
    ERR_SCORE_STAGE_NOT_IN_SCOPE,
    ERR_TASK_STATE_NOT_ALLOWED,
    ERR_TASK_STAGE_NOT_ALLOWED,
    ERR_ITEM_STAGE_NOT_ALLOWED,
    ERR_SUCCESS_EXISTS,
    ERR_TASK_OR_ITEM_NOT_FOUND,
    ERR_TASK_TYPE_NOT_SCORING,
    ERR_ITEM_TYPE_NOT_SCORING,
    ERR_SOURCE_BINDING_MISMATCH,
    ERR_CONFIGURATION_INVALID,
    ERR_FROZEN_SELECTION_MISMATCH,
    ERR_UNSAFE_REQUEST_SUMMARY,
    ERR_VALIDATION_FAILED,
    ERR_VALIDATION_MISSING,
    ScoringPipelineOrchestrator,
)
from services.scoring_provider_registry import ScoringProviderRegistry

T0 = datetime(2026, 8, 11, 3, 0, 0, tzinfo=timezone.utc)
TASK_ID = str(uuid4())
ITEM_ID = str(uuid4())
PID = "provider_demo_001"
MID = "model_demo_v1"
PROFILE_V = "profile-mixed-v1"
POLICY = "policy-001"
RUBRIC = "rubric-001"
PROMPT = "prompt-001"
SCHEMA = "score-response-v1"
SHA = "a" * 64
CONTRACT = "scoring-provider/v1"
RECORD_ID = "record_001"
VALIDATION_ID = "validation_001"

_SYNTH_FP = compute_item_input_fingerprint(
    package_id="evidence_pkg_001", package_revision=1, manifest_sha256=SHA,
    registration_record_id=RECORD_ID, validation_id=VALIDATION_ID)

SOURCE_TASK_ID = str(uuid4())
SOURCE_ITEM_ID = str(uuid4())

_CONFIG_FIELDS = dict(
    profile_version=PROFILE_V,
    provider_id=PID,
    model_id=MID,
    scoring_policy_version=POLICY,
    rubric_version=RUBRIC,
    prompt_version=PROMPT,
    response_schema_version=SCHEMA,
)


def make_config(**overrides):
    """与 make_profile 口径一致的不可变评分配置快照（impl-4）。"""
    fields = dict(_CONFIG_FIELDS)
    fields.update(overrides)
    if "configuration_fingerprint" not in fields:
        fields["configuration_fingerprint"] = sha256_canonical(fields)
    return ScoringTaskConfiguration(**fields)

# 当前 11C 正式任务：scope 不含 score（真实任务在 scope 检查处阻断）
CURRENT_11C_TASK = SimpleNamespace(
    task_id=TASK_ID, task_type="evidence_preparation_pipeline",
    execution_scope=["import", "validate"],
    status="running", current_stage="validate", item_index=[])

# 合成只读评分任务 fixture（未来评分任务形态，scope 含 score、current_stage=score）。
# 明确标注：不是当前 11C 正式模型实例。
_SYNTH_CONFIG = make_config()

SYNTH_SCORE_TASK = SimpleNamespace(
    task_id=TASK_ID, task_type="scoring_pipeline",
    execution_scope=["score", "review", "export"],
    status="running", current_stage="score",
    source_task_id=SOURCE_TASK_ID,
    configuration_snapshot=_SYNTH_CONFIG,
    configuration_fingerprint=_SYNTH_CONFIG.configuration_fingerprint,
    item_index=[SimpleNamespace(
        item_id=ITEM_ID, task_type="scoring_pipeline", source_item_id=SOURCE_ITEM_ID,
        package_id="evidence_pkg_001", package_revision=1,
        status="pending", current_stage="score", input_fingerprint=_SYNTH_FP,
        item_revision=1, evidence_level="sufficient")])


# ---------------- 构造 helper ---------------- #


def make_record(**overrides):
    base = dict(
        record_schema_version="evidence-sidecar/record/v1",
        record_id=RECORD_ID,
        package_id="evidence_pkg_001",
        package_revision=1,
        batch_id="batch_001",
        submission_id="submission_demo_001",
        evidence_version="v1",
        manifest_sha256=SHA,
        privacy_policy_version="privacy-v1",
        contract_version="evidence-package/v1.1",
        publication_status="ready",
        registration_status="registered",
        latest_validation_id=VALIDATION_ID,
        latest_validation_status="passed",
        source_package_ref="inputs/evidence_pkg_001",
        registered_at=T0, registered_by="system", created_at=T0, updated_at=T0,
        revision=1, record_sha256=SHA,
    )
    base.update(overrides)
    return EvidencePackageRecord(**base)


def make_validation(evidence_level="sufficient", **overrides):
    base = dict(
        validation_schema_version="evidence-sidecar/validation/v1",
        validation_id=VALIDATION_ID,
        record_id=RECORD_ID,
        package_id="evidence_pkg_001",
        package_revision=1,
        manifest_sha256=SHA,
        validator_name="evidence-validator",
        validator_version="validator-001",
        evidence_contract_version="evidence-package/v1.1",
        privacy_policy_version="privacy-v1",
        mode="registration",
        status="passed",
        started_at=T0, completed_at=T0, duration_ms=100,
        checks=[], issues=[], issue_counts=IssueCounts(),
        model_input_allowed=True,
        registration_allowed=True,
        validated_file_count=1, declared_file_count=1, unregistered_file_count=0,
        result_sha256=SHA,
        evidence_level=evidence_level,
    )
    base.update(overrides)
    return ValidationResult(**base)


def make_source_pair():
    """与 scoring item 绑定一致的权威 source task/item（impl-4 source 绑定校验）。"""
    src_task = SimpleNamespace(
        task_id=SOURCE_TASK_ID, task_type="evidence_preparation_pipeline",
        execution_scope=["import", "validate"], status="completed",
        current_stage="validate",
        item_index=[SimpleNamespace(item_id=SOURCE_ITEM_ID)])
    src_item = SimpleNamespace(
        item_id=SOURCE_ITEM_ID, task_id=SOURCE_TASK_ID, task_type="evidence_preparation_pipeline",
        package_id="evidence_pkg_001", package_revision=1, manifest_sha256=SHA,
        registration_record_id=RECORD_ID, validation_id=VALIDATION_ID,
        input_fingerprint=_SYNTH_FP, status="completed",
        evidence_level="sufficient", current_stage="validate", item_revision=1)
    return src_task, src_item


def make_item(evidence_level="sufficient", status="pending", **overrides):
    base = dict(
        item_id=ITEM_ID, task_id=TASK_ID, task_type="scoring_pipeline",
        source_item_id=SOURCE_ITEM_ID, package_id="evidence_pkg_001",
        package_revision=1, manifest_sha256=SHA,
        registration_record_id=RECORD_ID, validation_id=VALIDATION_ID,
        input_fingerprint=compute_item_input_fingerprint(
            package_id="evidence_pkg_001", package_revision=1, manifest_sha256=SHA,
            registration_record_id=RECORD_ID, validation_id=VALIDATION_ID),
        status=status, evidence_level=evidence_level,
        current_stage="score", item_revision=1,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def make_profile(**overrides):
    base = dict(
        profile_version=PROFILE_V,
        scoring_mode="mixed",
        input_modalities=["text"],
        scoring_policy_version=POLICY,
        rubric_version=RUBRIC,
        prompt_version=PROMPT,
        response_schema_version=SCHEMA,
        temperature=0.3,
        seed=None,
        max_output_tokens=None,
        system_message_required=True,
        structured_json_required=True,
    )
    base.update(overrides)
    return ScoringInputProfile(**base)


def make_registry(tmp_path, capability_overrides=None, provider_overrides=None):
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
        "registry_version": "provider-registry/v1",
        "updated_at": T0.isoformat(),
        "revision": 1,
        "providers": [prov],
        "capabilities": [cap],
    }, ensure_ascii=False), encoding="utf-8")
    r = ScoringProviderRegistry(p)
    r.load()
    return r


class FakeLookups:
    def __init__(self, task=None, item=None, record=None, validation=None,
                 profile=None, attempts=None, success_has=False, open_review=False,
                 source_task=None, source_item=None):
        self.task = task
        self.item = item
        self.record = record
        self.validation = validation
        self.profile = profile
        self.attempts = attempts or []
        self.success_has = success_has
        self.open_review = open_review
        if source_task is None and task is not None and getattr(task, "task_type", None) == "scoring_pipeline":
            src_task, src_item = make_source_pair()
            self.source_task = src_task
            self.source_item = src_item
        else:
            self.source_task = source_task
            self.source_item = source_item

    def get_task(self, task_id):
        if self.task is not None and self.task.task_id == task_id:
            return self.task
        if self.source_task is not None and self.source_task.task_id == task_id:
            return self.source_task
        return None

    def get_item(self, task_id, item_id):
        if self.item is not None and self.item.task_id == task_id and self.item.item_id == item_id:
            return self.item
        if self.source_item is not None and self.source_item.task_id == task_id and self.source_item.item_id == item_id:
            return self.source_item
        return None

    def get_evidence_record(self, package_id):
        return self.record

    def get_validation_result(self, package_id):
        return self.validation

    def get_input_profile(self, profile_version):
        return self.profile

    def get_attempt(self, attempt_id):
        return None

    def list_attempts_for_item(self, task_id, item_id):
        return self.attempts

    def has_successful_snapshot(self, task_id, item_id, attempt_id=None):
        return self.success_has

    def get_snapshot(self, snapshot_id):
        return None

    def has_open_review_case(self, task_id, item_id):
        return self.open_review

    def list_review_case_ids(self, task_id, item_id=None):
        return ["review_001"] if self.open_review else []


def make_orchestrator(lookups, registry):
    return ScoringPipelineOrchestrator(
        task_item_lookup=lookups, evidence_lookup=lookups,
        attempt_lookup=lookups, success_lookup=lookups,
        review_case_lookup=lookups, profile_lookup=lookups,
        registry=registry, clock=lambda: T0,
    )


def synth_task_for(item):
    """构造与 item 权威快照一致的合成评分任务（entry 同步 item 的 status/stage/fingerprint）。"""
    return SimpleNamespace(
        task_id=TASK_ID, task_type="scoring_pipeline",
        execution_scope=["score", "review", "export"],
        status="running", current_stage="score",
        source_task_id=SOURCE_TASK_ID,
        configuration_snapshot=_SYNTH_CONFIG,
        configuration_fingerprint=_SYNTH_CONFIG.configuration_fingerprint,
        item_index=[SimpleNamespace(
            item_id=item.item_id, task_type="scoring_pipeline", source_item_id=SOURCE_ITEM_ID,
            package_id=item.package_id,
            package_revision=item.package_revision, status=item.status,
            current_stage=item.current_stage, input_fingerprint=item.input_fingerprint,
            item_revision=item.item_revision, evidence_level=item.evidence_level)])


def run_dry_run(o, **overrides):
    kwargs = dict(
        task_id=TASK_ID, item_id=ITEM_ID, provider_id=PID, model_id=MID,
        profile_version=PROFILE_V,
        dry_run_request_id="dry-run-001",
    )
    kwargs.update(overrides)
    return o.dry_run(**kwargs)


def happy_lookups():
    return FakeLookups(task=SYNTH_SCORE_TASK, item=make_item(),
                       record=make_record(), validation=make_validation(),
                       profile=make_profile())


# ---------------- Task / Item 准入 ---------------- #


def test_current_11c_task_blocked_by_scope(tmp_path):
    """当前 evidence_preparation_pipeline（scope=import/validate）被 score scope 阻断。"""
    r = make_registry(tmp_path)
    o = make_orchestrator(FakeLookups(task=CURRENT_11C_TASK, item=make_item(),
                                      record=make_record(), validation=make_validation(),
                                      profile=make_profile()), r)
    plan = run_dry_run(o)
    assert plan.decision == "blocked"
    # impl-4：evidence task 在 task_type 检查处拒绝
    assert plan.blocking_error_code == ERR_TASK_TYPE_NOT_SCORING
    assert plan.provider_call_planned is False


def test_ready_full_path_synth_score_task(tmp_path):
    """ready 完整路径（合成只读评分任务 fixture，scope 含 score）。"""
    r = make_registry(tmp_path)
    o = make_orchestrator(happy_lookups(), r)
    plan = run_dry_run(o)
    assert plan.decision == "ready"
    assert plan.provider_call_planned is True
    assert plan.provider_id == PID and plan.model_id == MID
    assert plan.capability_version == "cap-demo-001"
    assert plan.scoring_mode == "mixed"
    assert plan.input_modalities == ["text"]
    assert plan.blocking_error_code is None
    assert all(c.passed for c in plan.checks)


def test_item_status_gating(tmp_path):
    r = make_registry(tmp_path)
    # running -> 阻断（防并发重复）
    o1 = make_orchestrator(FakeLookups(task=synth_task_for(make_item(status="running")), item=make_item(status="running"),
                                       record=make_record(), validation=make_validation(),
                                       profile=make_profile()), r)
    assert run_dry_run(o1).blocking_error_code == ERR_ITEM_ALREADY_RUNNING
    # failed -> 阻断（需正式恢复决定）
    o2 = make_orchestrator(FakeLookups(task=synth_task_for(make_item(status="failed")), item=make_item(status="failed"),
                                       record=make_record(), validation=make_validation(),
                                       profile=make_profile()), r)
    assert run_dry_run(o2).blocking_error_code == ERR_ITEM_RECOVERY_REQUIRED
    # skipped -> 阻断
    o3 = make_orchestrator(FakeLookups(task=synth_task_for(make_item(status="skipped")), item=make_item(status="skipped"),
                                       record=make_record(), validation=make_validation(),
                                       profile=make_profile()), r)
    assert run_dry_run(o3).blocking_error_code == ERR_ITEM_STATE_NOT_ALLOWED
    # manual_review -> 人工复核
    o4 = make_orchestrator(FakeLookups(task=synth_task_for(make_item(status="manual_review")), item=make_item(status="manual_review"),
                                       record=make_record(), validation=make_validation(),
                                       profile=make_profile()), r)
    p4 = run_dry_run(o4)
    assert p4.decision == "manual_review" and p4.manual_review_required is True
    # completed 无成功快照 -> 阻断（不得直接计划）
    o5 = make_orchestrator(FakeLookups(task=synth_task_for(make_item(status="completed")), item=make_item(status="completed"),
                                       record=make_record(), validation=make_validation(),
                                       profile=make_profile()), r)
    assert run_dry_run(o5).blocking_error_code == ERR_ITEM_STATE_NOT_ALLOWED
    # completed + 成功快照 -> already_completed
    o6 = make_orchestrator(FakeLookups(task=synth_task_for(make_item(status="completed")), item=make_item(status="completed"),
                                       record=make_record(), validation=make_validation(),
                                       profile=make_profile(), success_has=True), r)
    assert run_dry_run(o6).decision == "already_completed"


def test_task_item_mismatches(tmp_path):
    r = make_registry(tmp_path)
    o1 = make_orchestrator(FakeLookups(task=None, item=make_item()), r)
    assert run_dry_run(o1).blocking_error_code == ERR_TASK_OR_ITEM_NOT_FOUND
    o2 = make_orchestrator(FakeLookups(task=SYNTH_SCORE_TASK, item=None), r)
    assert run_dry_run(o2).blocking_error_code == ERR_TASK_OR_ITEM_NOT_FOUND
    o3 = make_orchestrator(FakeLookups(task=SYNTH_SCORE_TASK, item=make_item(task_id=str(uuid4()))), r)
    assert run_dry_run(o3).blocking_error_code == ERR_TASK_OR_ITEM_NOT_FOUND


# ---------------- 权威 Evidence sidecar ---------------- #


def test_evidence_record_authoritative(tmp_path):
    r = make_registry(tmp_path)
    # record 缺失
    o1 = make_orchestrator(FakeLookups(task=SYNTH_SCORE_TASK, item=make_item(), record=None,
                                       validation=make_validation(), profile=make_profile()), r)
    assert run_dry_run(o1).blocking_error_code == ERR_EVIDENCE_RECORD_MISSING
    # 绑定不一致（record_id 不匹配 item.registration_record_id）
    o2 = make_orchestrator(FakeLookups(task=SYNTH_SCORE_TASK, item=make_item(),
                                       record=make_record(record_id="record_OTHER"),
                                       validation=make_validation(), profile=make_profile()), r)
    assert run_dry_run(o2).blocking_error_code == ERR_EVIDENCE_MISMATCH
    # revision 不一致
    o3 = make_orchestrator(FakeLookups(task=SYNTH_SCORE_TASK, item=make_item(),
                                       record=make_record(package_revision=2),
                                       validation=make_validation(), profile=make_profile()), r)
    assert run_dry_run(o3).blocking_error_code == ERR_EVIDENCE_MISMATCH
    # fingerprint 不一致
    o4 = make_orchestrator(FakeLookups(task=SYNTH_SCORE_TASK, item=make_item(input_fingerprint="d" * 64),
                                       record=make_record(), validation=make_validation(),
                                       profile=make_profile()), r)
    assert run_dry_run(o4).blocking_error_code == ERR_EVIDENCE_MISMATCH
    # registration_status 非 registered
    o5 = make_orchestrator(FakeLookups(task=SYNTH_SCORE_TASK, item=make_item(),
                                       record=make_record(registration_status="superseded"),
                                       validation=make_validation(), profile=make_profile()), r)
    assert run_dry_run(o5).blocking_error_code == ERR_EVIDENCE_MISMATCH
    # publication_status 非 ready
    o6 = make_orchestrator(FakeLookups(task=SYNTH_SCORE_TASK, item=make_item(),
                                       record=make_record(publication_status="staging"),
                                       validation=make_validation(), profile=make_profile()), r)
    assert run_dry_run(o6).blocking_error_code == ERR_EVIDENCE_MISMATCH


def test_validation_binding_authoritative(tmp_path):
    r = make_registry(tmp_path)
    # validation 缺失
    o1 = make_orchestrator(FakeLookups(task=SYNTH_SCORE_TASK, item=make_item(),
                                       record=make_record(), validation=None, profile=make_profile()), r)
    assert run_dry_run(o1).blocking_error_code == ERR_VALIDATION_MISSING
    # validation_id 不匹配 item.validation_id
    o2 = make_orchestrator(FakeLookups(task=SYNTH_SCORE_TASK, item=make_item(),
                                       record=make_record(),
                                       validation=make_validation(validation_id="validation_OTHER"),
                                       profile=make_profile()), r)
    assert run_dry_run(o2).blocking_error_code == ERR_VALIDATION_FAILED
    # record_id 不匹配
    o3 = make_orchestrator(FakeLookups(task=SYNTH_SCORE_TASK, item=make_item(),
                                       record=make_record(),
                                       validation=make_validation(record_id="record_OTHER"),
                                       profile=make_profile()), r)
    assert run_dry_run(o3).blocking_error_code == ERR_VALIDATION_FAILED
    # status failed
    o4 = make_orchestrator(FakeLookups(task=SYNTH_SCORE_TASK, item=make_item(),
                                       record=make_record(),
                                       validation=make_validation(status="failed"),
                                       profile=make_profile()), r)
    assert run_dry_run(o4).blocking_error_code == ERR_VALIDATION_FAILED
    # model_input_allowed False
    o5 = make_orchestrator(FakeLookups(task=SYNTH_SCORE_TASK, item=make_item(),
                                       record=make_record(),
                                       validation=make_validation(model_input_allowed=False),
                                       profile=make_profile()), r)
    assert run_dry_run(o5).blocking_error_code == ERR_VALIDATION_FAILED
    # evidence_level 与 item 不一致
    o6 = make_orchestrator(FakeLookups(task=SYNTH_SCORE_TASK, item=make_item(evidence_level="sufficient"),
                                       record=make_record(),
                                       validation=make_validation(evidence_level="limited"),
                                       profile=make_profile()), r)
    assert run_dry_run(o6).blocking_error_code == ERR_EVIDENCE_MISMATCH


# ---------------- 快照 / outcome / review ---------------- #


def test_success_outcome_review_gates(tmp_path):
    r = make_registry(tmp_path)
    # 成功快照短路
    o1 = make_orchestrator(FakeLookups(task=synth_task_for(make_item()), item=make_item(),
                                       record=make_record(), validation=make_validation(),
                                       profile=make_profile(), success_has=True), r)
    p1 = run_dry_run(o1)
    assert p1.decision == "already_completed" and p1.blocking_error_code == ERR_SUCCESS_EXISTS
    # outcome_unknown 阻断
    o2 = make_orchestrator(FakeLookups(task=synth_task_for(make_item()), item=make_item(),
                                       record=make_record(), validation=make_validation(),
                                       profile=make_profile(),
                                       attempts=[SimpleNamespace(status="outcome_unknown")]), r)
    assert run_dry_run(o2).blocking_error_code == ERR_OUTCOME_UNKNOWN_EXISTS
    # open review case 阻断
    o3 = make_orchestrator(FakeLookups(task=synth_task_for(make_item()), item=make_item(),
                                       record=make_record(), validation=make_validation(),
                                       profile=make_profile(), open_review=True), r)
    assert run_dry_run(o3).blocking_error_code == ERR_OPEN_REVIEW_CASE
    # manual_only evidence
    o4 = make_orchestrator(FakeLookups(task=synth_task_for(make_item(evidence_level="manual_only")), item=make_item(evidence_level="manual_only"),
                                       record=make_record(), validation=make_validation(evidence_level="manual_only"),
                                       profile=make_profile()), r)
    p4 = run_dry_run(o4)
    assert p4.decision == "manual_review" and p4.blocking_error_code == ERR_MANUAL_ONLY
    # limited evidence 可进入带标记
    o5 = make_orchestrator(FakeLookups(task=synth_task_for(make_item(evidence_level="limited")), item=make_item(evidence_level="limited"),
                                       record=make_record(), validation=make_validation(evidence_level="limited"),
                                       profile=make_profile()), r)
    p5 = run_dry_run(o5)
    assert p5.decision == "ready" and p5.limited_evidence is True


# ---------------- Profile / capability ---------------- #


def test_provider_and_profile_gates(tmp_path):
    r = make_registry(tmp_path)
    o = make_orchestrator(happy_lookups(), r)
    # provider 不存在（registry 不含冻结 provider）
    r_ghost = make_registry(tmp_path,
                           provider_overrides={
                               "provider_id": "provider_ghost", "model_id": "model_ghost",
                               "capability_ref": "capability/provider_ghost/model_ghost/cap-demo-001"},
                           capability_overrides={
                               "provider_id": "provider_ghost", "model_id": "model_ghost"})
    o_ghost = make_orchestrator(happy_lookups(), r_ghost)
    assert run_dry_run(o_ghost).blocking_error_code == ERR_PROVIDER_NOT_FOUND
    # provider 禁用
    r2 = make_registry(tmp_path, provider_overrides={"enabled": False})
    o2 = make_orchestrator(happy_lookups(), r2)
    assert run_dry_run(o2).blocking_error_code == ERR_PROVIDER_DISABLED
    # profile 缺失
    o3 = make_orchestrator(FakeLookups(task=SYNTH_SCORE_TASK, item=make_item(),
                                       record=make_record(), validation=make_validation(),
                                       profile=None), r)
    assert run_dry_run(o3).blocking_error_code == ERR_PROFILE_MISSING


def test_capability_matches_profile(tmp_path):
    # text/mixed profile：temperature 不支持 -> mismatch
    r = make_registry(tmp_path, capability_overrides={"temperature_supported": False})
    o = make_orchestrator(happy_lookups(), r)
    assert run_dry_run(o).blocking_error_code == ERR_CAPABILITY_MISMATCH
    # image profile：需要 image_input + max_images + formats
    r2 = make_registry(tmp_path, capability_overrides={
        "image_input": True, "max_images_per_request": 5,
        "supported_image_formats": ["png", "jpg"],
    })
    prof_img = make_profile(scoring_mode="mixed", input_modalities=["text", "image"],
                            max_images=3, image_formats=["png"])
    o2 = make_orchestrator(FakeLookups(task=synth_task_for(make_item()), item=make_item(),
                                       record=make_record(), validation=make_validation(),
                                       profile=prof_img), r2)
    p2 = run_dry_run(o2)
    assert p2.decision == "ready" and p2.input_modalities == ["text", "image"]
    # max_images 超限 -> mismatch
    r3 = make_registry(tmp_path, capability_overrides={
        "image_input": True, "max_images_per_request": 2,
        "supported_image_formats": ["png"],
    })
    o3 = make_orchestrator(FakeLookups(task=synth_task_for(make_item()), item=make_item(),
                                       record=make_record(), validation=make_validation(),
                                       profile=prof_img), r3)
    assert run_dry_run(o3).blocking_error_code == ERR_CAPABILITY_MISMATCH
    # 声明 image 但 capability image_input=False -> mismatch
    r4 = make_registry(tmp_path)
    o4 = make_orchestrator(FakeLookups(task=SYNTH_SCORE_TASK, item=make_item(),
                                       record=make_record(), validation=make_validation(),
                                       profile=make_profile(input_modalities=["image"], max_images=1, image_formats=["png"])), r4)
    assert run_dry_run(o4).blocking_error_code == ERR_CAPABILITY_MISMATCH


def test_profile_and_preview_models():
    """Profile/Preview 受控模型：extra forbid、安全校验、不含敏感。"""
    prof = make_profile()
    assert prof.scoring_mode == "mixed" and prof.input_modalities == ["text"]
    with pytest.raises(ValidationError):
        ScoringInputProfile(**make_profile().model_dump(), api_key="sk-x")
    with pytest.raises(ValidationError):
        make_profile(scoring_mode="both")  # 非法模式
    with pytest.raises(ValidationError):
        make_profile(input_modalities=["text"], max_images=3)  # 无 image 却带 max_images
    prev = ProviderRequestPreview(
        provider_id=PID, model_id=MID, capability_version="cap-demo-001",
        config_version="cfg-001", evidence_package_id="evidence_pkg_001",
        evidence_manifest_sha256=SHA, input_fingerprint=SHA,
        scoring_policy_version=POLICY, rubric_version=RUBRIC, scoring_mode="mixed",
        prompt_version=PROMPT, response_schema_version=SCHEMA,
        input_modalities=["text"], temperature=0.3,
    )
    assert prev.provider_id == PID
    with pytest.raises(ValidationError):
        ProviderRequestPreview(**prev.model_dump(), attempt_id="attempt_001")
    with pytest.raises(ValidationError):
        ProviderRequestPreview(**prev.model_dump(), api_key="sk-x")


def test_caller_cannot_forge_authoritative():
    """调用方不能伪造 profile 内容或能力结论。"""
    with pytest.raises(ValidationError):
        DryRunPlan(dry_run_request_id="dry-run-001", task_id=TASK_ID, item_id=ITEM_ID,
                   decision="ready", created_at=T0, validation_passed=True)
    with pytest.raises(ValidationError):
        DryRunPlan(dry_run_request_id="dry-run-001", task_id=TASK_ID, item_id=ITEM_ID,
                   decision="ready", created_at=T0, capability_ok=True)


def test_output_no_sensitive_content(tmp_path):
    r = make_registry(tmp_path)
    o = make_orchestrator(happy_lookups(), r)
    plan = run_dry_run(o)
    dump = json.dumps(plan.model_dump(mode="json"))
    for bad in ("prompt_text", "api_key", "http://", "https://", "total_score", "attempt_id"):
        assert bad.lower() not in dump.lower(), bad


def test_same_input_same_decision(tmp_path):
    r = make_registry(tmp_path)
    o = make_orchestrator(happy_lookups(), r)
    p1 = run_dry_run(o)
    p2 = run_dry_run(o)
    assert p1.decision == p2.decision
    assert [c.check_code for c in p1.checks] == [c.check_code for c in p2.checks]


def test_zero_side_effect_no_writes(tmp_path, monkeypatch):
    """所有写接口与 Gateway/CredentialResolver 被 monkeypatch 抛错仍不被调用。"""
    import services.scoring_provider_gateway as gw
    import services.provider_switch_store as pss
    import services.pipeline_task_store as pts
    import services.provider_switch_service as sws

    def _boom(*a, **k):
        raise AssertionError("write/gateway called during dry-run")

    monkeypatch.setattr(gw.ScoringProviderGateway, "call", _boom)
    monkeypatch.setattr(pss.ProviderSwitchStore, "create", _boom)
    monkeypatch.setattr(pss.ProviderSwitchStore, "update", _boom)
    monkeypatch.setattr(pts.PipelineTaskStore, "write_task_snapshot", _boom)
    monkeypatch.setattr(pts.PipelineTaskStore, "begin_transaction", _boom)
    monkeypatch.setattr(sws.ProviderSwitchService, "create_switch_request", _boom)
    r = make_registry(tmp_path)
    o = make_orchestrator(happy_lookups(), r)
    plan = run_dry_run(o)
    assert plan.decision == "ready"


# ---------------- 11E-2a-fix-2：Task/Item 阶段与 Profile 绑定 ---------------- #


def _task_with(task, item):
    """基于 item 构造指定 task 形态的合成任务。"""
    t = synth_task_for(item)
    for k, v in task.items():
        setattr(t, k, v)
    return t


def test_task_state_gating(tmp_path):
    r = make_registry(tmp_path)
    for st in ("paused", "completed", "completed_with_errors", "failed", "cancelled"):
        tk = _task_with({"status": st}, make_item())
        o = make_orchestrator(FakeLookups(task=tk, item=make_item(),
                                          record=make_record(), validation=make_validation(),
                                          profile=make_profile()), r)
        assert run_dry_run(o).blocking_error_code == ERR_TASK_STATE_NOT_ALLOWED, st


def test_task_stage_gating(tmp_path):
    r = make_registry(tmp_path)
    tk = _task_with({"current_stage": "validate"}, make_item())
    o = make_orchestrator(FakeLookups(task=tk, item=make_item(),
                                      record=make_record(), validation=make_validation(),
                                      profile=make_profile()), r)
    assert run_dry_run(o).blocking_error_code == ERR_TASK_STAGE_NOT_ALLOWED
    tk2 = _task_with({"current_stage": "export"}, make_item())
    o2 = make_orchestrator(FakeLookups(task=tk2, item=make_item(),
                                       record=make_record(), validation=make_validation(),
                                       profile=make_profile()), r)
    assert run_dry_run(o2).blocking_error_code == ERR_TASK_STAGE_NOT_ALLOWED


def test_item_stage_gating(tmp_path):
    r = make_registry(tmp_path)
    it = make_item(current_stage="validate")
    tk = _task_with({}, it)
    o = make_orchestrator(FakeLookups(task=tk, item=it,
                                      record=make_record(), validation=make_validation(),
                                      profile=make_profile()), r)
    assert run_dry_run(o).blocking_error_code == ERR_ITEM_STAGE_NOT_ALLOWED


def test_item_index_membership_and_identity(tmp_path):
    r = make_registry(tmp_path)
    tk = _task_with({"item_index": []}, make_item())
    o = make_orchestrator(FakeLookups(task=tk, item=make_item(),
                                      record=make_record(), validation=make_validation(),
                                      profile=make_profile()), r)
    assert run_dry_run(o).blocking_error_code == ERR_TASK_OR_ITEM_NOT_FOUND
    it = make_item()
    tk2 = synth_task_for(it)
    tk2.item_index[0].package_id = "evidence_pkg_OTHER"
    o2 = make_orchestrator(FakeLookups(task=tk2, item=it,
                                       record=make_record(), validation=make_validation(),
                                       profile=make_profile()), r)
    assert run_dry_run(o2).blocking_error_code == ERR_EVIDENCE_MISMATCH
    it3 = make_item()
    tk3 = synth_task_for(it3)
    tk3.item_index[0].item_revision = 99
    o3 = make_orchestrator(FakeLookups(task=tk3, item=it3,
                                       record=make_record(), validation=make_validation(),
                                       profile=make_profile()), r)
    assert run_dry_run(o3).blocking_error_code == ERR_EVIDENCE_MISMATCH
    it4 = make_item()
    tk4 = synth_task_for(it4)
    tk4.item_index[0].status = "completed"
    o4 = make_orchestrator(FakeLookups(task=tk4, item=it4,
                                       record=make_record(), validation=make_validation(),
                                       profile=make_profile()), r)
    assert run_dry_run(o4).blocking_error_code == ERR_EVIDENCE_MISMATCH


def test_caller_cannot_pass_versions_anymore(tmp_path):
    """调用方不得再单独传 scoring_policy/rubric/prompt/schema（dry_run 签名已移除）。"""
    r = make_registry(tmp_path)
    o = make_orchestrator(happy_lookups(), r)
    with pytest.raises(TypeError):
        run_dry_run(o, scoring_policy_version=POLICY)
    with pytest.raises(TypeError):
        run_dry_run(o, rubric_version=RUBRIC)
    with pytest.raises(TypeError):
        run_dry_run(o, prompt_version=PROMPT)
    with pytest.raises(TypeError):
        run_dry_run(o, response_schema_version=SCHEMA)


def test_versions_emitted_from_profile(tmp_path):
    """四版本均由权威 Profile 输出到 DryRunPlan 与 ProviderRequestPreview。"""
    r = make_registry(tmp_path)
    o = make_orchestrator(happy_lookups(), r)
    plan = run_dry_run(o)
    assert plan.scoring_policy_version == POLICY
    assert plan.rubric_version == RUBRIC
    assert plan.prompt_version == PROMPT
    assert plan.response_schema_version == SCHEMA
    prev = o._build_preview(make_item(), SimpleNamespace(provider_id=PID, model_id=MID,
                                                         config_version="cfg-001"),
                            SimpleNamespace(capability_version="cap-demo-001"),
                            make_profile())
    assert prev.rubric_version == RUBRIC
    assert prev.scoring_policy_version == POLICY


def test_temperature_capability_rules(tmp_path):
    r1 = make_registry(tmp_path, capability_overrides={"temperature_min": 0.0, "temperature_max": 0.2})
    o1 = make_orchestrator(happy_lookups(), r1)
    assert run_dry_run(o1).blocking_error_code == ERR_CAPABILITY_MISMATCH
    r2 = make_registry(tmp_path, capability_overrides={"temperature_allowed_values": [0.0, 0.5]})
    o2 = make_orchestrator(happy_lookups(), r2)
    assert run_dry_run(o2).blocking_error_code == ERR_CAPABILITY_MISMATCH
    r3 = make_registry(tmp_path, capability_overrides={"temperature_allowed_values": [0.3, 0.5]})
    o3 = make_orchestrator(happy_lookups(), r3)
    assert run_dry_run(o3).decision == "ready"


def test_image_profile_rules(tmp_path):
    r5 = make_registry(tmp_path, capability_overrides={"image_input": True, "max_images_per_request": 5})
    o5 = make_orchestrator(FakeLookups(task=synth_task_for(make_item()), item=make_item(),
                                       record=make_record(), validation=make_validation(),
                                       profile=make_profile(input_modalities=["image"])), r5)
    assert run_dry_run(o5).blocking_error_code == ERR_CAPABILITY_MISMATCH  # max_images None/0
    r6 = make_registry(tmp_path, capability_overrides={"image_input": True, "max_images_per_request": 2})
    o6 = make_orchestrator(FakeLookups(task=synth_task_for(make_item()), item=make_item(),
                                       record=make_record(), validation=make_validation(),
                                       profile=make_profile(input_modalities=["image"], max_images=3,
                                                            image_formats=["png"])), r6)
    assert run_dry_run(o6).blocking_error_code == ERR_CAPABILITY_MISMATCH
    r7 = make_registry(tmp_path, capability_overrides={"image_input": True, "max_images_per_request": 5,
                                                       "supported_image_formats": ["jpg"]})
    o7 = make_orchestrator(FakeLookups(task=synth_task_for(make_item()), item=make_item(),
                                       record=make_record(), validation=make_validation(),
                                       profile=make_profile(input_modalities=["image"], max_images=3,
                                                            image_formats=["png"])), r7)
    assert run_dry_run(o7).blocking_error_code == ERR_CAPABILITY_MISMATCH
    r8 = make_registry(tmp_path, capability_overrides={"image_input": True, "max_images_per_request": 5,
                                                       "supported_image_formats": ["png"]})
    o8 = make_orchestrator(FakeLookups(task=synth_task_for(make_item()), item=make_item(),
                                       record=make_record(), validation=make_validation(),
                                       profile=make_profile(input_modalities=["image"], max_images=3,
                                                            image_formats=["png"])), r8)
    assert run_dry_run(o8).decision == "ready"


def test_modalities_profile_rejects_invalid():
    """modalities 空或重复由 Profile 模型拒绝。"""
    with pytest.raises(ValidationError):
        make_profile(input_modalities=[])
    with pytest.raises(ValidationError):
        make_profile(input_modalities=["text", "text"])
    with pytest.raises(ValidationError):
        make_profile(input_modalities=["video"])
    with pytest.raises(ValidationError):
        make_profile(input_modalities=["text", "image"], image_formats=["png", "png"])


def test_current_11c_task_still_blocked(tmp_path):
    """当前 11C 正式任务仍被 scope 阻断（回归确认）。"""
    r = make_registry(tmp_path)
    o = make_orchestrator(FakeLookups(task=CURRENT_11C_TASK, item=make_item(),
                                      record=make_record(), validation=make_validation(),
                                      profile=make_profile()), r)
    plan = run_dry_run(o)
    assert plan.decision == "blocked"
    # impl-4：evidence task 类型拒绝（不再返回 ready）
    assert plan.blocking_error_code == ERR_TASK_TYPE_NOT_SCORING

# ---------------- 11E-2a-prerequisite-impl-4：绑定正式评分任务 ---------------- #


def test_frozen_selection_mismatch_rejected(tmp_path):
    """调用方试图替换 Provider/model/profile：不得静默覆盖，返回冻结选择冲突。"""
    r = make_registry(tmp_path)
    o = make_orchestrator(happy_lookups(), r)
    assert run_dry_run(o, provider_id="provider_other").blocking_error_code == ERR_FROZEN_SELECTION_MISMATCH
    assert run_dry_run(o, model_id="model_other").blocking_error_code == ERR_FROZEN_SELECTION_MISMATCH
    assert run_dry_run(o, profile_version="profile-other-v1").blocking_error_code == ERR_FROZEN_SELECTION_MISMATCH
    # 一致参数合法
    assert run_dry_run(o, provider_id=PID, model_id=MID, profile_version=PROFILE_V).decision == "ready"


def test_source_binding_mismatch_rejected(tmp_path):
    """source task/source item 绑定错误被拒绝。"""
    r = make_registry(tmp_path)
    # source item package 与 scoring item 不一致
    src_t, src_i = make_source_pair()
    src_i = SimpleNamespace(**{**src_i.__dict__, "package_id": "evidence_pkg_OTHER"})
    o = make_orchestrator(FakeLookups(task=SYNTH_SCORE_TASK, item=make_item(),
                                      record=make_record(), validation=make_validation(),
                                      profile=make_profile(),
                                      source_task=src_t, source_item=src_i), r)
    assert run_dry_run(o).blocking_error_code == ERR_SOURCE_BINDING_MISMATCH
    # source item 未结算（非 completed）
    src_t2, src_i2 = make_source_pair()
    src_i2 = SimpleNamespace(**{**src_i2.__dict__, "status": "pending"})
    o2 = make_orchestrator(FakeLookups(task=SYNTH_SCORE_TASK, item=make_item(),
                                       record=make_record(), validation=make_validation(),
                                       profile=make_profile(),
                                       source_task=src_t2, source_item=src_i2), r)
    assert run_dry_run(o2).blocking_error_code == ERR_SOURCE_BINDING_MISMATCH
    # source task 类型错误
    src_t3, src_i3 = make_source_pair()
    src_t3 = SimpleNamespace(**{**src_t3.__dict__, "task_type": "scoring_pipeline"})
    o3 = make_orchestrator(FakeLookups(task=SYNTH_SCORE_TASK, item=make_item(),
                                       record=make_record(), validation=make_validation(),
                                       profile=make_profile(),
                                       source_task=src_t3, source_item=src_i3), r)
    assert run_dry_run(o3).blocking_error_code == ERR_SOURCE_BINDING_MISMATCH


def test_configuration_invalid_rejected(tmp_path):
    """configuration snapshot 类型错误或 fingerprint 不一致被拒绝。"""
    r = make_registry(tmp_path)
    # snapshot 类型错误
    bad_task = SimpleNamespace(**{**SYNTH_SCORE_TASK.__dict__, "configuration_snapshot": {"not": "config"}})
    o = make_orchestrator(FakeLookups(task=bad_task, item=make_item(),
                                      record=make_record(), validation=make_validation(),
                                      profile=make_profile()), r)
    assert run_dry_run(o).blocking_error_code == ERR_CONFIGURATION_INVALID
    # fingerprint 与 snapshot 不一致
    bad_task2 = SimpleNamespace(**{**SYNTH_SCORE_TASK.__dict__,
                                   "configuration_fingerprint": "9" * 64})
    o2 = make_orchestrator(FakeLookups(task=bad_task2, item=make_item(),
                                       record=make_record(), validation=make_validation(),
                                       profile=make_profile()), r)
    assert run_dry_run(o2).blocking_error_code == ERR_CONFIGURATION_INVALID
    # 快照版本与权威 Profile 不一致
    cfg_bad = make_config(scoring_policy_version="policy-999")
    bad_task3 = SimpleNamespace(**{**SYNTH_SCORE_TASK.__dict__,
                                   "configuration_snapshot": cfg_bad,
                                   "configuration_fingerprint": cfg_bad.configuration_fingerprint})
    o3 = make_orchestrator(FakeLookups(task=bad_task3, item=make_item(),
                                       record=make_record(), validation=make_validation(),
                                       profile=make_profile()), r)
    assert run_dry_run(o3).blocking_error_code == ERR_FROZEN_SELECTION_MISMATCH


def test_item_type_mismatch_rejected(tmp_path):
    """item.task_type 非 scoring_pipeline 被拒绝。"""
    r = make_registry(tmp_path)
    bad_item = make_item(task_type="evidence_preparation_pipeline")
    o = make_orchestrator(FakeLookups(task=SYNTH_SCORE_TASK, item=bad_item,
                                      record=make_record(), validation=make_validation(),
                                      profile=make_profile()), r)
    assert run_dry_run(o).blocking_error_code == ERR_ITEM_TYPE_NOT_SCORING
