"""
Phase 11E-1b-fix-1：ScoreAttempt / 快照 / 校验 / 恢复决策 / Provider 幂等能力 合成测试。

覆盖 11A-2d 第 6.3 节必需字段精确断言（逐项），避免"测试通过但字段遗漏"。
全部合成脱敏数据；不包含正文、Key、URL、学生个人信息。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from models.score_attempt import (
    ActorRef,
    AttemptValidation,
    DimensionScore,
    Flag,
    RecoveryAssessment,
    ScoreAttempt,
    ScoreDimensionRef,
    ScoreResultSnapshot,
    ScoreScale,
    TERMINAL_STATUSES,
    ValidationCheck,
    can_transition,
)
from models.scoring_provider import (
    ModelCapability,
    ProviderError,
    ProviderRequest,
)

T0 = datetime(2026, 8, 11, 3, 0, 0, tzinfo=timezone.utc)
SHA = "a" * 64
FP = "b" * 64
CONTRACT = "score-attempt-review/v1"
ACTOR = ActorRef(actor_type="orchestrator", actor_id="orchestrator_001")


def make_error(code="PROVIDER_OUTCOME_UNKNOWN"):
    return ProviderError(
        contract_version="scoring-provider/v1",
        error_id="provider_error_001",
        request_id="provider_request_001",
        task_id="task_001", item_id="item_001", attempt_id="attempt_001",
        provider_id="provider_001", model_id="model_v1",
        error_code=code, error_category="unknown",
        message_safe="结果不确定，未记录正文",
        retryable="no", provider_switch_allowed="approval_required",
        manual_review_required="yes", consumes_provider_call_attempt=True,
        stop_entire_batch="no", retry_after_seconds=None,
        occurred_at=T0, details_sha256=None,
    )


def make_scale():
    return ScoreScale(
        scoring_policy_version="policy-001",
        total_score_range_ref="range:total",
        total_min=0.0, total_max=40.0,
        dimensions=[
            ScoreDimensionRef(dimension_code="objective", score_range_ref="range:objective-40",
                              min_score=0.0, max_score=40.0),
        ],
    )


def make_snapshot(**overrides):
    base = dict(
        schema_version="score-result-snapshot/v1",
        snapshot_id="snapshot_001",
        attempt_id="attempt_001",
        submission_id="submission_demo_001",
        package_id="evidence_pkg_001",
        evidence_manifest_sha256=SHA,
        scoring_policy_version="policy-001",
        rubric_version="rubric-001",
        response_schema_version="score-response-v1",
        score_scale=make_scale(),
        objective_score=36.0,
        subjective_score=None,
        dimension_scores=[
            DimensionScore(dimension_code="objective", score=36.0, min_score=0.0,
                           max_score=40.0, score_range_ref="range:objective-40"),
        ],
        total_score=36.0,
        rationale_summary="脱敏依据摘要",
        evidence_level="sufficient",
        evidence_refs=["evidence_demo_001"],
        confidence="high",
        flags=[Flag(flag_code="LOW_RISK", severity="info", message_key="flag.low_risk")],
        manual_review_recommended=False,
        structure_validation_status="passed",
        score_range_validation_status="passed",
        result_hash=SHA,
        created_at=T0,
    )
    base.update(overrides)
    return ScoreResultSnapshot(**base)


def make_attempt(**overrides):
    base = dict(
        contract_version=CONTRACT,
        schema_version="score-attempt/v1",
        attempt_id="attempt_001",
        attempt_number=1,
        task_id="task_001",
        item_id="item_001",
        submission_id="submission_demo_001",
        package_id="evidence_pkg_001",
        package_revision=1,
        evidence_manifest_sha256=SHA,
        input_fingerprint=FP,
        provider_id="provider_001",
        provider_config_version="cfg-001",
        model_id="model_v1",
        model_capability_version="cap-001",
        provider_request_id="provider_request_001",
        previous_attempt_id=None,
        switch_decision_id=None,
        scoring_policy_version="policy-001",
        rubric_version="rubric-001",
        prompt_version="prompt-001",
        response_schema_version="score-response-v1",
        status="created",
        created_at=T0,
        created_by=ACTOR,
        started_at=None,
        completed_at=None,
        duration_ms=None,
        error=None,
        result_snapshot_ref=None,
        validation_ref=None,
        request_hash=None,
        response_hash=None,
    )
    base.update(overrides)
    return ScoreAttempt(**base)


# ---------------- 11A-2d 6.3 必需字段精确断言 ---------------- #

# 11A-2d 第 6.3 节 ScoreAttempt 必需字段全集（22 个契约字段）
CONTRACT_REQUIRED_FIELDS_6_3 = [
    "contract_version", "schema_version", "attempt_id", "attempt_number",
    "task_id", "item_id", "submission_id", "package_id", "package_revision",
    "evidence_manifest_sha256", "input_fingerprint", "provider_id",
    "provider_config_version", "model_id", "model_capability_version",
    "provider_request_id", "previous_attempt_id", "switch_decision_id",
    "scoring_policy_version", "rubric_version", "prompt_version",
    "response_schema_version", "status", "started_at", "completed_at",
    "duration_ms", "error", "result_snapshot_ref", "validation_ref", "request_hash",
]


def test_contract_6_3_required_fields_exact():
    """11A-2d 第 6.3 节必需字段逐项存在（精确集合断言）。"""
    missing = [f for f in CONTRACT_REQUIRED_FIELDS_6_3 if f not in ScoreAttempt.model_fields]
    assert missing == [], f"缺失 6.3 字段: {missing}"


def test_new_fixed_fields_present_and_validated():
    """三个新字段（response_hash/created_at/created_by）正反例。"""
    # 正例
    a = make_attempt(status="created")
    assert a.response_hash is None
    assert a.created_at == T0
    assert a.created_by.actor_type == "orchestrator"
    assert a.created_by.actor_id == "orchestrator_001"
    # 反例：缺 created_at / created_by 拒绝
    d = make_attempt().model_dump()
    del d["created_at"]
    with pytest.raises(ValidationError):
        ScoreAttempt(**d)
    d2 = make_attempt().model_dump()
    del d2["created_by"]
    with pytest.raises(ValidationError):
        ScoreAttempt(**d2)
    # response_hash 非法拒绝
    with pytest.raises(ValidationError):
        make_attempt(response_hash="bad-hash")


def test_actor_ref_controlled():
    """ActorRef 受控：枚举 actor_type + 安全 actor_id + extra forbid。"""
    assert ActorRef(actor_type="system", actor_id="sys_001").actor_type == "system"
    assert ActorRef(actor_type="controller", actor_id="ctrl_001").actor_id == "ctrl_001"
    with pytest.raises(ValidationError):
        ActorRef(actor_type="student", actor_id="x")  # 非受控枚举
    with pytest.raises(ValidationError):
        ActorRef(actor_type="system", actor_id="张三")  # 非安全 ID
    with pytest.raises(ValidationError):
        ActorRef(actor_type="system", actor_id="x", display_name="张三")  # extra forbid
    with pytest.raises(ValidationError):
        ActorRef(actor_type="system", actor_id="x", phone="00000000000")


# ---------------- 状态与转换 ---------------- #


def test_ten_attempt_statuses_and_transitions():
    """10 个状态 + 合法转换表（11A-2d 6.7 + outcome_unknown 扩展）。"""
    ten = ["created", "running", "succeeded", "failed", "timed_out",
           "rate_limited", "quota_exhausted", "invalid_response", "cancelled", "outcome_unknown"]
    for s in ten:
        assert s in ScoreAttempt.model_fields["status"].annotation.__args__
    legal = [
        ("created", "running"), ("created", "cancelled"),
        ("running", "succeeded"), ("running", "failed"), ("running", "timed_out"),
        ("running", "rate_limited"), ("running", "quota_exhausted"),
        ("running", "invalid_response"), ("running", "cancelled"),
        ("running", "outcome_unknown"),
    ]
    for f, t in legal:
        assert can_transition(f, t), (f, t)
    assert not can_transition("outcome_unknown", "running")
    assert not can_transition("succeeded", "running")
    assert not can_transition("failed", "running")
    assert not can_transition("cancelled", "running")
    assert not can_transition("created", "succeeded")
    assert not can_transition("running", "created")


def test_created_invariants():
    """created：无 started/completed、无响应哈希/快照/错误。"""
    a = make_attempt(status="created")
    assert a.started_at is None and a.completed_at is None
    assert a.response_hash is None and a.result_snapshot_ref is None and a.error is None
    with pytest.raises(ValidationError):
        make_attempt(status="created", started_at=T0)
    with pytest.raises(ValidationError):
        make_attempt(status="created", response_hash=SHA)
    with pytest.raises(ValidationError):
        make_attempt(status="created", error=make_error(code="PROVIDER_UNKNOWN_ERROR"))


def test_running_invariants():
    """running：有 started、无 completed、无快照、无终态错误。"""
    a = make_attempt(status="running", started_at=T0)
    assert a.started_at == T0 and a.completed_at is None
    with pytest.raises(ValidationError):
        make_attempt(status="running")  # 缺 started_at
    with pytest.raises(ValidationError):
        make_attempt(status="running", started_at=T0, completed_at=T0)
    with pytest.raises(ValidationError):
        make_attempt(status="running", started_at=T0, result_snapshot_ref="snapshot_001")
    with pytest.raises(ValidationError):
        make_attempt(status="running", started_at=T0, error=make_error())


def test_succeeded_invariants():
    """succeeded：started/completed/duration_ms/response_hash/result_snapshot_ref 全部存在，error 为 None。"""
    base_ok = dict(status="succeeded", started_at=T0, completed_at=T0 + timedelta(seconds=2),
                   duration_ms=2000, response_hash=SHA, result_snapshot_ref="snapshot_001")
    a = make_attempt(**base_ok)
    assert a.status == "succeeded"
    for kw in (dict(started_at=None), dict(completed_at=None), dict(duration_ms=None),
               dict(response_hash=None), dict(result_snapshot_ref=None)):
        merged = dict(base_ok)
        merged.update(kw)
        with pytest.raises(ValidationError):
            make_attempt(**merged)
    with pytest.raises(ValidationError):
        make_attempt(**base_ok, error=make_error(code="PROVIDER_UNKNOWN_ERROR"))


def test_failure_terminal_invariants():
    """七类失败终态：error + completed_at 必须；无快照；outcome_unknown 必须 PROVIDER_OUTCOME_UNKNOWN。"""
    fail_statuses = ["failed", "timed_out", "rate_limited", "quota_exhausted",
                     "invalid_response", "cancelled", "outcome_unknown"]
    for s in fail_statuses:
        code = "PROVIDER_OUTCOME_UNKNOWN" if s == "outcome_unknown" else "PROVIDER_UNKNOWN_ERROR"
        a = make_attempt(status=s, started_at=T0, completed_at=T0 + timedelta(seconds=1),
                         error=make_error(code=code))
        assert a.status in TERMINAL_STATUSES
        assert a.error is not None and a.completed_at is not None
        assert a.result_snapshot_ref is None
        # 缺 error / 缺 completed_at / 有快照 拒绝
        with pytest.raises(ValidationError):
            make_attempt(status=s, started_at=T0, completed_at=T0 + timedelta(seconds=1))
        with pytest.raises(ValidationError):
            make_attempt(status=s, started_at=T0, error=make_error(code=code))
        with pytest.raises(ValidationError):
            make_attempt(status=s, started_at=T0, completed_at=T0 + timedelta(seconds=1),
                         error=make_error(code=code), result_snapshot_ref="snapshot_001")
    # invalid_response 可以有 response_hash（不强制）
    ir = make_attempt(status="invalid_response", started_at=T0, completed_at=T0 + timedelta(seconds=1),
                      error=make_error(code="PROVIDER_INVALID_JSON"), response_hash=SHA)
    assert ir.response_hash == SHA
    # outcome_unknown 错误码必须一致
    with pytest.raises(ValidationError):
        make_attempt(status="outcome_unknown", started_at=T0, completed_at=T0 + timedelta(seconds=1),
                     error=make_error(code="PROVIDER_TIMEOUT"))
    # cancelled 允许 created 时取消（started_at None 合法）
    c = make_attempt(status="cancelled", completed_at=T0, error=make_error(code="PROVIDER_UNKNOWN_ERROR"))
    assert c.started_at is None


def test_time_order_enforced():
    """时间顺序 created_at <= started_at <= completed_at。"""
    with pytest.raises(ValidationError):
        make_attempt(status="running", started_at=T0 - timedelta(seconds=5))  # started < created
    with pytest.raises(ValidationError):
        make_attempt(status="succeeded", started_at=T0, completed_at=T0 - timedelta(seconds=1),
                     duration_ms=100, response_hash=SHA, result_snapshot_ref="snapshot_001")  # completed < started


def test_previous_attempt_self_reference_rejected():
    """previous_attempt_id == attempt_id 拒绝。"""
    with pytest.raises(ValidationError):
        make_attempt(status="created", previous_attempt_id="attempt_001")


def test_terminal_not_back_to_non_terminal():
    """终态回到非终态由转换表禁止（can_transition 覆盖全部终态）。"""
    for t in TERMINAL_STATUSES:
        assert not can_transition(t, "created")
        assert not can_transition(t, "running")
        assert not can_transition(t, "succeeded")


def test_outcome_unknown_previous_attempt_link():
    """outcome_unknown 后续重调：新 attempt 通过 previous_attempt_id 关联（新 attempt 不重复 attempt_id）。"""
    old = make_attempt(status="outcome_unknown", started_at=T0, completed_at=T0 + timedelta(seconds=1),
                       error=make_error())
    new = make_attempt(attempt_id="attempt_002", previous_attempt_id=old.attempt_id)
    assert new.previous_attempt_id == "attempt_001"
    assert new.attempt_id != new.previous_attempt_id


# ---------------- 快照/校验/恢复 ---------------- #


def test_snapshot_hash_evidence_and_validation_status():
    """快照哈希、证据引用和校验状态。"""
    snap = make_snapshot()
    assert snap.result_hash == SHA
    assert snap.evidence_refs == ["evidence_demo_001"]
    assert snap.structure_validation_status == "passed"
    assert snap.score_range_validation_status == "passed"
    with pytest.raises(ValidationError):
        make_snapshot(result_hash="not-a-hash")
    with pytest.raises(ValidationError):
        make_snapshot(total_score=99.0)


def test_forbid_sensitive_fields():
    """禁止正文、URL、Key、敏感字段（extra=forbid）。"""
    for builder, kwargs in (
        (make_attempt, {"user_prompt": "正文"}),
        (make_attempt, {"api_key": "sk-x"}),
        (make_attempt, {"endpoint": "https://x"}),
        (make_snapshot, {"raw_response": "..."}),
        (make_snapshot, {"student_phone": "00000000000"}),
    ):
        with pytest.raises(ValidationError):
            builder(**kwargs)


def test_recovery_assessment_controlled():
    """恢复决策对象：四路径枚举 + extra forbid + 幂等一致性。"""
    for path in ("provider_query", "manual_approved_new_attempt", "manual_review"):
        ra = RecoveryAssessment(attempt_id="attempt_001", path=path, reason_code=f"RECOVER_{path.upper()}")
        assert ra.path == path
    with pytest.raises(ValidationError):
        RecoveryAssessment(attempt_id="attempt_001", path="idempotent_replay", reason_code="R1")
    ok = RecoveryAssessment(attempt_id="attempt_001", path="idempotent_replay",
                            reason_code="R1", idempotency_verified=True,
                            idempotency_verification_ref="cap-001")
    assert ok.idempotency_verification_ref == "cap-001"
    with pytest.raises(ValidationError):
        RecoveryAssessment(attempt_id="attempt_001", path="manual_review",
                           reason_code="R2", idempotency_verified=True)
    with pytest.raises(ValidationError):
        RecoveryAssessment(attempt_id="attempt_001", path="manual_review",
                           reason_code="R3", provider_succeeded=True)


def test_validation_model():
    """AttemptValidation 受控字段。"""
    v = AttemptValidation(
        schema_version="attempt-validation/v1",
        validation_id="validation_001",
        attempt_id="attempt_001",
        snapshot_id="snapshot_001",
        validation_revision=1,
        validator_version="validator-001",
        checks=[ValidationCheck(check_code="SCHEMA", passed=True, message_key="check.schema")],
        overall_status="passed",
        adoption_candidate=True,
        manual_review_required=False,
        review_reason_codes=[],
        validated_at=T0,
        validated_by="system",
    )
    assert v.adoption_candidate is True
    with pytest.raises(ValidationError):
        AttemptValidation(
            schema_version="attempt-validation/v1", validation_id="validation_001",
            attempt_id="attempt_001", validation_revision=0, validator_version="v1",
            overall_status="passed", validated_at=T0, validated_by="system",
        )


# ---------------- Provider 幂等能力（Phase 11E-1b-fix-1） ---------------- #


def make_capability(**overrides):
    base = dict(
        contract_version="scoring-provider/v1",
        capability_version="cap-001",
        provider_id="provider_001",
        model_id="model_v1",
        verification_source="static_config",
    )
    base.update(overrides)
    return ModelCapability(**base)


def test_idempotency_tribool_values():
    """Provider 幂等能力 True/False/unknown。"""
    assert make_capability(request_idempotency_supported=True,
                           idempotency_verification_source="provider_doc",
                           idempotency_verified_at=T0).request_idempotency_supported is True
    assert make_capability(request_idempotency_supported=False).request_idempotency_supported is False
    assert make_capability().request_idempotency_supported == "unknown"


def test_idempotency_true_requires_verification():
    """True 但无验证来源或无验证时间时拒绝。"""
    with pytest.raises(ValidationError):
        make_capability(request_idempotency_supported=True)
    with pytest.raises(ValidationError):
        make_capability(request_idempotency_supported=True, idempotency_verification_source="provider_doc")
    with pytest.raises(ValidationError):
        make_capability(request_idempotency_supported=True, idempotency_verification_source="unknown",
                        idempotency_verified_at=T0)
    ok = make_capability(request_idempotency_supported=True,
                         idempotency_verification_source="runtime_probe",
                         idempotency_verified_at=T0)
    assert ok.idempotency_verified_at == T0


def _provider_request_base():
    return dict(
        contract_version="scoring-provider/v1",
        request_id="request_001", task_id="task_001", item_id="item_001",
        attempt_id="attempt_001", provider_id="provider_001", model_id="model_v1",
        capability_version="cap-001", config_version="cfg-001",
        evidence_package_id="pkg_001", evidence_manifest_sha256=SHA,
        input_fingerprint=FP, scoring_policy_version="policy-001", scoring_mode="mixed",
        prompt_version="prompt-001", response_schema_version="rsv1",
        timeout_seconds=120, requested_at=T0, input_modalities=["text"],
        evidence_refs=["e1"], request_payload_sha256=SHA, selection_id="sel_001",
    )


def test_provider_request_rejects_raw_idempotency_key():
    """原始 provider_idempotency_key 作为 extra 字段拒绝（只保存 hash）。"""
    with pytest.raises(ValidationError):
        ProviderRequest(**_provider_request_base(), provider_idempotency_key="idem-key-0001")


def test_provider_request_idempotency_key_hash_validation():
    """provider_idempotency_key_hash 合法/非法校验。"""
    ok = ProviderRequest(**_provider_request_base(), provider_idempotency_key_hash=SHA)
    assert ok.provider_idempotency_key_hash == SHA
    # 未设置时默认 None
    assert ProviderRequest(**_provider_request_base()).provider_idempotency_key_hash is None
    for bad in ("short", "x" * 63, "x" * 65):
        with pytest.raises(ValidationError):
            ProviderRequest(**_provider_request_base(), provider_idempotency_key_hash=bad)
