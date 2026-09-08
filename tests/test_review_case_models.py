"""
Phase 11E-3b-1：Review 五类契约模型合成测试。

覆盖：合法模型、非法状态/时间/引用组合拒绝、原因码矩阵校验、decision 约束、
adoption 约束（adopted 需 effective_at + attempt_id、scope 一致）、
调分不覆盖原快照、重开关系、事件 revision 连续性与哈希链校验。
全部合成脱敏数据；不保存正文/Key/URL/学生信息。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from models.review_case import (
    REVIEW_REASON_CODES,
    AdoptionScope,
    ManualAdjustment,
    ManualAdjustmentChange,
    ResultAdoption,
    ReviewCase,
    ReviewDecision,
    ReviewEvent,
    reason_blocks_auto_adoption,
    reason_blocks_export,
)

T0 = datetime(2026, 8, 13, 2, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=1)
T2 = T0 + timedelta(minutes=2)
T3 = T0 + timedelta(minutes=3)
SHA = "ab" * 32  # 64 hex
SHA2 = "cd" * 32
REV = "rev_demo_001"
DEC = "dec_demo_001"
ADP = "adp_demo_001"
ADJ = "adj_demo_001"
EVT = "evt_demo_001"
REVIEWER = {"actor_type": "reviewer", "actor_id": "reviewer-demo-01"}
SYSTEM = {"actor_type": "system", "actor_id": "review-case-store"}


def make_case(status="open", **overrides):
    base = dict(
        contract_version="score-attempt-review/v1",
        schema_version="review-case/v1",
        review_case_id=REV,
        case_number="RC-DEMO-0001",
        submission_id="sub_demo_001",
        task_id="task_demo_001",
        item_id="item_demo_001",
        package_id="pkg_demo_001",
        package_revision=1,
        attempt_ids=[],
        snapshot_ids=[],
        validation_ids=[],
        source_type="evidence_validation",
        reason_codes=["EVIDENCE_INSUFFICIENT"],
        priority="high",
        status=status,
        blocks_auto_adoption=True,
        blocks_export=True,
        opened_at=T0,
        updated_at=T0,
        reopen_count=0,
        current_revision=1,
    )
    if status in ("resolved", "dismissed"):
        base.update(dict(
            resolved_at=T2, resolution_decision_id=DEC,
            assigned_at=T1, review_started_at=T1,
            updated_at=T2,
        ))
    base.update(overrides)
    return ReviewCase(**base)


def make_decision(**overrides):
    base = dict(
        contract_version="score-attempt-review/v1",
        schema_version="review-decision/v1",
        decision_id=DEC,
        review_case_id=REV,
        case_revision=1,
        decision_type="request_additional_evidence",
        reason_codes=["EVIDENCE_INSUFFICIENT"],
        requested_package_revision=2,
        decision_note_code="REQUEST_MISSING_REQUIRED_EVIDENCE",
        idempotency_key="idem-demo-001",  # 11F-1b
        decided_by=REVIEWER,
        decided_at=T1,
        decision_hash=SHA,
    )
    base.update(overrides)
    return ReviewDecision(**base)


def make_adoption(status="adopted", **overrides):
    base = dict(
        contract_version="score-attempt-review/v1",
        schema_version="result-adoption/v1",
        adoption_id=ADP,
        adoption_scope={
            "competition_id": "competition_demo_001",
            "batch_id": "batch-demo-001",  # 11F-1b
            "stream_id": "default",
            "submission_id": "sub_demo_001",
            "scoring_policy_version": "policy-demo-v1",
            "purpose": "machine_result",
        },
        submission_id="sub_demo_001",
        attempt_id="atp_demo_001",
        snapshot_id="snap_demo_001",
        validation_id="val_demo_001",
        status=status,
        reason_code="AUTO_ADOPTION_VALIDATION_PASSED",
        decided_at=T1,
        decided_by=SYSTEM,
        adoption_hash=SHA,
    )
    if status == "adopted":
        base["effective_at"] = T1
    base.update(overrides)
    return ResultAdoption(**base)


def make_adjustment(**overrides):
    base = dict(
        contract_version="score-attempt-review/v1",
        schema_version="manual-adjustment/v1",
        adjustment_id=ADJ,
        review_case_id=REV,
        decision_id=DEC,
        submission_id="sub_demo_001",
        base_attempt_id="atp_demo_001",
        base_snapshot_id="snap_demo_001",
        adjusted_snapshot_id="snap_demo_001_adjusted",
        scoring_policy_version="policy-demo-v1",
        rubric_version="rubric-demo-v1",
        changes=[{
            "field_path": "total_score",
            "before_value": 88.0,
            "after_value": 85.0,
            "allowed_range_ref": "policy-demo-v1#total",
            "evidence_refs": [],
            "change_reason_code": "TOTAL_RECALCULATED",
        }],
        reason_codes=["HIGH_SCORE_VARIANCE"],
        adjustment_note_code="ADJUSTED_AFTER_CROSS_ATTEMPT_REVIEW",
        adjusted_by=REVIEWER,
        adjusted_at=T2,
        adjustment_hash=SHA,
    )
    base.update(overrides)
    return ManualAdjustment(**base)


def make_event(event_type="CASE_ASSIGNED", **overrides):
    base = dict(
        contract_version="score-attempt-review/v1",
        schema_version="review-event/v1",
        event_id=EVT,
        review_case_id=REV,
        case_revision_before=1,
        case_revision_after=2,
        event_type=event_type,
        from_status="open",
        to_status="assigned",
        actor=SYSTEM,
        occurred_at=T1,
        event_hash=SHA,
    )
    base.update(overrides)
    return ReviewEvent(**base)


# ---------------- 合法模型 ---------------- #


def test_all_five_models_valid():
    assert make_case().status == "open"
    assert make_case(status="resolved").resolution_decision_id == DEC
    assert make_decision().decision_type == "request_additional_evidence"
    assert make_adoption().status == "adopted"
    assert make_adjustment().adjusted_snapshot_id != make_adjustment().base_snapshot_id
    ev = make_event()
    assert ev.case_revision_after == ev.case_revision_before + 1
    assert ev.previous_event_hash is None


def test_reason_codes_matrix_21_codes_frozen():
    assert len(REVIEW_REASON_CODES) == 21
    # 每个原因码五个策略维度完整
    for code, (stage, blocks_auto, blocks_export, rescore, human) in REVIEW_REASON_CODES.items():
        assert code
        assert isinstance(stage, str) and isinstance(blocks_auto, bool)
        assert isinstance(blocks_export, bool) and isinstance(rescore, bool) and isinstance(human, bool)
    # 代表性矩阵断言
    assert reason_blocks_auto_adoption("EVIDENCE_INSUFFICIENT") is True
    assert reason_blocks_auto_adoption("PROVIDER_UNAVAILABLE") is False
    assert reason_blocks_export("PRIVACY_RISK") is True
    assert reason_blocks_export("PROVIDER_UNAVAILABLE") is False


# ---------------- 非法状态 / 时间 / 引用组合 ---------------- #


def test_case_unknown_reason_code_rejected():
    with pytest.raises(ValueError):
        make_case(reason_codes=["NOT_A_REAL_CODE"])


def test_case_empty_reason_codes_rejected():
    with pytest.raises(ValueError):
        make_case(reason_codes=[])


def test_case_duplicate_reason_codes_rejected():
    with pytest.raises(ValueError):
        make_case(reason_codes=["EVIDENCE_INSUFFICIENT", "EVIDENCE_INSUFFICIENT"])


def test_case_blocks_must_match_matrix():
    # 阻断原因码存在但 blocks_auto_adoption=False -> 拒绝
    with pytest.raises(ValueError):
        make_case(reason_codes=["EVIDENCE_INSUFFICIENT"], blocks_auto_adoption=False)
    with pytest.raises(ValueError):
        make_case(reason_codes=["PRIVACY_RISK"], blocks_export=False)


def test_case_task_item_together():
    with pytest.raises(ValueError):
        make_case(task_id="task_demo_001", item_id=None)


def test_case_resolved_requires_resolution():
    with pytest.raises(ValueError):
        make_case(status="resolved", resolution_decision_id=None)
    with pytest.raises(ValueError):
        make_case(status="resolved", resolved_at=None)


def test_case_open_with_assigned_at_rejected():
    with pytest.raises(ValueError):
        make_case(status="open", assigned_at=T1)


def test_case_time_order_violation():
    with pytest.raises(ValueError):
        make_case(status="resolved", assigned_at=T2, review_started_at=T1)


def test_case_reopen_self_reference_rejected():
    with pytest.raises(ValueError):
        make_case(reopened_from_case_id=REV)


def test_case_idempotency_key_validation():
    with pytest.raises(ValueError):
        make_case(idempotency_key="../evil")


# ---------------- ReviewDecision 约束 ---------------- #


def test_decision_request_evidence_requires_revision():
    with pytest.raises(ValueError):
        make_decision(requested_package_revision=None)


def test_decision_adjustment_requires_ref():
    with pytest.raises(ValueError):
        make_decision(decision_type="apply_manual_adjustment", manual_adjustment_id=None)


def test_decision_adopt_requires_target():
    with pytest.raises(ValueError):
        make_decision(decision_type="adopt_existing_attempt", target_attempt_id=None)


def test_decision_dismiss_not_for_privacy_risk():
    with pytest.raises(ValueError):
        make_decision(
            decision_type="dismiss_no_issue", reason_codes=["PRIVACY_RISK"],
            target_attempt_id=None, requested_package_revision=None,
        )


def test_decision_supersedes_not_self():
    with pytest.raises(ValueError):
        make_decision(supersedes_decision_id=DEC)


# ---------------- ResultAdoption 约束 ---------------- #


def test_adoption_scope_submission_match():
    scope = AdoptionScope(
        competition_id="competition_demo_001", submission_id="sub_demo_OTHER",
        scoring_policy_version="policy-demo-v1",
    )
    with pytest.raises(ValueError):
        make_adoption(adoption_scope=scope)


def test_adoption_adopted_requires_effective_and_attempt():
    with pytest.raises(ValueError):
        make_adoption(status="adopted", effective_at=None)
    with pytest.raises(ValueError):
        make_adoption(status="adopted", attempt_id=None)


def test_adoption_supersedes_not_self():
    with pytest.raises(ValueError):
        make_adoption(supersedes_adoption_id=ADP)


def test_adoption_non_terminal_statuses_valid():
    for st in ("proposed", "blocked", "revoked", "superseded"):
        make_adoption(status=st, effective_at=None)


# ---------------- ManualAdjustment 约束 ---------------- #


def test_adjustment_must_not_overwrite_base_snapshot():
    with pytest.raises(ValueError):
        make_adjustment(adjusted_snapshot_id="snap_demo_001")


def test_adjustment_changes_not_empty():
    with pytest.raises(ValueError):
        make_adjustment(changes=[])


def test_adjustment_change_same_value_rejected():
    with pytest.raises(ValueError):
        make_adjustment(changes=[{
            "field_path": "total_score",
            "before_value": 88.0,
            "after_value": 88.0,
            "allowed_range_ref": "policy-demo-v1#total",
            "evidence_refs": [],
            "change_reason_code": "TOTAL_RECALCULATED",
        }])


def test_adjustment_unknown_reason_rejected():
    with pytest.raises(ValueError):
        make_adjustment(reason_codes=["FAKE_CODE"])


# ---------------- ReviewEvent 约束 ---------------- #


def test_event_revision_continuous():
    with pytest.raises(ValueError):
        make_event(case_revision_before=1, case_revision_after=3)


def test_event_state_transition_requires_status():
    with pytest.raises(ValueError):
        make_event(event_type="CASE_ASSIGNED", from_status=None, to_status=None)


def test_event_case_opened_allows_no_from_status():
    ev = make_event(event_type="CASE_OPENED", from_status=None, to_status="open",
                    case_revision_before=1, case_revision_after=1)
    assert ev.event_type == "CASE_OPENED"


def test_event_non_state_must_not_carry_status():
    with pytest.raises(ValueError):
        make_event(event_type="DECISION_RECORDED", from_status="open", to_status="assigned")


def test_event_adoption_linked_can_carry_status():
    ev = make_event(event_type="ADOPTION_LINKED", from_status="adopted", to_status="superseded",
                    review_case_id="rev_demo_001")
    assert ev.from_status == "adopted"


def test_event_hash_must_be_sha256():
    with pytest.raises(ValueError):
        make_event(event_hash="not-a-hash")


# ---------------- 敏感字段禁止 ---------------- #


def test_models_reject_extra_fields():
    with pytest.raises(ValueError):
        make_case(student_name="张三")
    with pytest.raises(ValueError):
        make_decision(comment="自由文本正文")
    with pytest.raises(ValueError):
        make_adoption(api_key="")
    with pytest.raises(ValueError):
        make_adjustment(evidence_body="正文内容")
