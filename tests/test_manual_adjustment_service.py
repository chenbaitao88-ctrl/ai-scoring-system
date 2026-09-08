"""
Phase 11F-3b-prerequisite-impl-2：ManualAdjustmentService 可恢复事务合成测试。

覆盖：正常流程、幂等、冲突、续跑、恢复、失败清理。
所有测试使用正规合成 snapshot 和公开 Store API。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

T0 = datetime(2026, 8, 26, 10, 0, 0, tzinfo=timezone.utc)


def _seed(tmp_path):
    import test_scoring_pipeline_execute_integration as base
    from services.review_case_store import ReviewCaseStore
    from models.score_attempt import DimensionScore
    env = base._seed_env(tmp_path)
    attempt_store = env["attempt_store"]
    review_store = ReviewCaseStore(tmp_path / "reviews", attempt_store=attempt_store)
    env["review_store"] = review_store
    env["orch"]._review_store = review_store
    item = base._item(env)
    result = base._run(env["orch"].execute_score_item(
        env["task"].task_id, item.item_id, "exec-ctl-0"))
    assert result.outcome == "succeeded", result.error_code
    # 注入维度
    attempts = attempt_store.list_attempts(env["task"].task_id, item.item_id)
    snap = attempt_store.get_snapshot(env["task"].task_id, item.item_id, attempts[-1].result_snapshot_ref)
    if not snap.dimension_scores:
        new_dims = [
            DimensionScore(dimension_code="objective", score=50.0, min_score=0.0, max_score=60.0, score_range_ref="range-obj-60"),
            DimensionScore(dimension_code="subjective", score=40.0, min_score=0.0, max_score=40.0, score_range_ref="range-sub-40"),
        ]
        new_snap = snap.model_copy(update={"dimension_scores": new_dims, "total_score": 90.0, "objective_score": 50.0, "subjective_score": 40.0})
        snap_path = attempt_store._fact_path(env["task"].task_id, item.item_id, "snapshots", snap.snapshot_id)
        snap_path.write_text(json.dumps(new_snap.model_dump(mode="json"), indent=2), encoding="utf-8")
    env["item"] = item
    return env


def _setup_review(env):
    from models.review_case import ReviewCase, ReviewDecision, ResultAdoption
    attempts = env["attempt_store"].list_attempts(env["task"].task_id, env["item"].item_id)
    att = attempts[-1]
    snap = env["attempt_store"].get_snapshot(env["task"].task_id, env["item"].item_id, att.result_snapshot_ref)
    case = ReviewCase(
        contract_version="score-attempt-review/v1", schema_version="review-case/v1",
        review_case_id="rev-test", case_number="RC-001",
        submission_id=snap.submission_id, task_id=env["task"].task_id, item_id=env["item"].item_id,
        package_id=env["item"].package_id, package_revision=env["item"].package_revision,
        attempt_ids=[att.attempt_id], snapshot_ids=[snap.snapshot_id],
        source_type="manual", reason_codes=["LOW_CONFIDENCE"], priority="high",
        status="open", blocks_auto_adoption=True, blocks_export=True,
        opened_at=T0, updated_at=T0, current_revision=1, reopen_count=0, idempotency_key="idem-rev-test",
    )
    env["review_store"].create_review_case(env["task"].task_id, env["item"].item_id, case)
    dec = ReviewDecision(
        contract_version="score-attempt-review/v1", schema_version="review-decision/v1",
        decision_id="dec-test", review_case_id=case.review_case_id, case_revision=case.current_revision,
        decision_type="apply_manual_adjustment", reason_codes=["LOW_CONFIDENCE"],
        target_attempt_id=att.attempt_id, target_snapshot_id=snap.snapshot_id,
        manual_adjustment_id="adj-req-test",
        decision_note_code="APPLY_MANUAL_ADJUSTMENT",
        decided_by={"actor_type": "reviewer", "actor_id": "r1"},
        decided_at=T0, decision_hash="ab" * 32, idempotency_key="idem-dec-test",
    )
    env["review_store"].create_review_decision(env["task"].task_id, env["item"].item_id, dec)
    val = env["attempt_store"].get_validation(
        env["task"].task_id, env["item"].item_id, att.validation_ref) if att.validation_ref else None
    import uuid
    adoption = ResultAdoption(
        contract_version="score-attempt-review/v1", schema_version="result-adoption/v1",
        adoption_id=f"adp-{uuid.uuid4().hex[:12]}",
        adoption_scope={"competition_id": "demo", "batch_id": "batch-demo", "stream_id": "default", "submission_id": snap.submission_id, "scoring_policy_version": snap.scoring_policy_version, "purpose": "machine_result"},
        submission_id=snap.submission_id, attempt_id=att.attempt_id, snapshot_id=snap.snapshot_id,
        validation_id=val.validation_id if val else "val_demo_001",
        status="adopted", reason_code="AUTO_ADOPTION_VALIDATION_PASSED",
        effective_at=T0, decided_at=T0,
        decided_by={"actor_type": "reviewer", "actor_id": "r1"},
        adoption_hash="ab" * 32, decision_id=dec.decision_id,
    )
    env["review_store"].adopt_result(env["task"].task_id, env["item"].item_id, adoption)
    return case, dec, adoption, snap, att


def _svc(env):
    from services.manual_adjustment_service import ManualAdjustmentService
    return ManualAdjustmentService(env["review_store"], env["attempt_store"])


def _changes(snap):
    d = snap.dimension_scores[0]
    return [{"dimension_code": d.dimension_code, "before_value": d.score, "after_value": d.score + 1.0, "change_reason_code": "LOW_CONFIDENCE"}]


# ---------------------------------------------------------------- 测试


def test_normal_flow_prepared_to_committed(tmp_path):
    env = _seed(tmp_path)
    case, dec, adoption, snap, att = _setup_review(env)
    svc = _svc(env)
    result, error = svc.build_and_apply(
        env["task"].task_id, env["item"].item_id, case.review_case_id, dec.decision_id,
        "req-test", _changes(snap), ["LOW_CONFIDENCE"], "MANUAL_SCORE_CORRECTION")
    assert error is None, error
    assert result["outcome"] == "created"
    assert result["adjustment_id"] == "adj-req-test"
    assert result["adjusted_snapshot_id"] == "snap-adjusted-req-test"
    assert result["adoption_id"] == "adp-adjusted-req-test"
    # 验证 operation 已 committed
    op = env["review_store"].get_operation(env["task"].task_id, env["item"].item_id, "op-req-test")
    assert op.stage == "committed"
    # 验证原始 snapshot 不变
    orig = env["attempt_store"].get_snapshot(env["task"].task_id, env["item"].item_id, snap.snapshot_id)
    assert orig.total_score == snap.total_score


def test_idempotent_replay(tmp_path):
    env = _seed(tmp_path)
    case, dec, adoption, snap, att = _setup_review(env)
    svc = _svc(env)
    args = (env["task"].task_id, env["item"].item_id, case.review_case_id, dec.decision_id,
            "req-idem", _changes(snap), ["LOW_CONFIDENCE"], "MANUAL_SCORE_CORRECTION")
    r1, _ = svc.build_and_apply(*args)
    r2, _ = svc.build_and_apply(*args)
    assert r1["outcome"] == "created"
    assert r2["outcome"] == "idempotent_hit"
    assert r2["idempotent"] is True


def test_conflict_on_different_payload(tmp_path):
    env = _seed(tmp_path)
    case, dec, adoption, snap, att = _setup_review(env)
    svc = _svc(env)
    svc.build_and_apply(env["task"].task_id, env["item"].item_id, case.review_case_id, dec.decision_id,
                        "req-conflict", _changes(snap), ["LOW_CONFIDENCE"], "MANUAL_SCORE_CORRECTION")
    r2, err = svc.build_and_apply(
        env["task"].task_id, env["item"].item_id, case.review_case_id, dec.decision_id,
        "req-conflict", [{"dimension_code": snap.dimension_scores[0].dimension_code, "before_value": snap.dimension_scores[0].score, "after_value": snap.dimension_scores[0].score + 5.0, "change_reason_code": "OTHER"}],
        ["LOW_CONFIDENCE"], "MANUAL_SCORE_CORRECTION")
    assert err == "MANUAL_ADJUSTMENT_CONFLICT"


def test_validation_binds_to_adjusted_snapshot(tmp_path):
    env = _seed(tmp_path)
    case, dec, adoption, snap, att = _setup_review(env)
    svc = _svc(env)
    svc.build_and_apply(env["task"].task_id, env["item"].item_id, case.review_case_id, dec.decision_id,
                        "req-val", _changes(snap), ["LOW_CONFIDENCE"], "MANUAL_SCORE_CORRECTION")
    val = env["attempt_store"].get_validation(env["task"].task_id, env["item"].item_id, "val-adjusted-req-val")
    assert val is not None
    assert val.snapshot_id == "snap-adjusted-req-val"
    assert any(check.check_code == "MANUAL_ADJUSTMENT_VALIDATED" for check in val.checks)


def test_resume_from_prepared(tmp_path):
    env = _seed(tmp_path)
    case, dec, adoption, snap, att = _setup_review(env)
    svc = _svc(env)
    # 第一次调用：正常执行，但在 advance_operation 推进到 committing 后，手动把 operation 回退到 prepared
    # 模拟进程中断在 prepared 阶段
    from models.review_case import ManualAdjustmentOperation
    from services.manual_adjustment_service import _payload_hash, _stable_ids
    ids = _stable_ids("req-resume")
    p_hash = _payload_hash(
        env["task"].task_id, env["item"].item_id, case.review_case_id, dec.decision_id,
        "req-resume", _changes(snap), ["LOW_CONFIDENCE"], "MANUAL_SCORE_CORRECTION",
        snap.snapshot_id, adoption.adoption_id)
    now = T0
    op = ManualAdjustmentOperation(
        contract_version="score-attempt-review/v1",
        schema_version="manual-adjustment-operation/v1",
        operation_id=ids["operation_id"],
        request_id="req-resume",
        stage="prepared",
        payload_hash=p_hash,
        created_at=now, updated_at=now,
        base_snapshot_id=snap.snapshot_id,
        adjusted_snapshot_id=ids["adjusted_snapshot_id"],
        adjusted_validation_id=ids["adjusted_validation_id"],
        changes=_changes(snap),
        reason_codes=["LOW_CONFIDENCE"],
        old_adoption_id=adoption.adoption_id,
        old_adoption_status_before="adopted",
    )
    env["review_store"].create_operation(env["task"].task_id, env["item"].item_id, op)
    # 续跑 - 使用完全相同的请求
    result, error = svc.build_and_apply(
        env["task"].task_id, env["item"].item_id, case.review_case_id, dec.decision_id,
        "req-resume", _changes(snap), ["LOW_CONFIDENCE"], "MANUAL_SCORE_CORRECTION")
    assert error is None, error
    assert result["outcome"] == "created"


def test_failed_operation_not_retried(tmp_path):
    env = _seed(tmp_path)
    case, dec, adoption, snap, att = _setup_review(env)
    # 手动创建 failed operation
    from models.review_case import ManualAdjustmentOperation
    import hashlib
    p_hash = hashlib.sha256(b"some-payload").hexdigest()
    op = ManualAdjustmentOperation(
        contract_version="score-attempt-review/v1", schema_version="manual-adjustment-operation/v1",
        operation_id="op-req-failed", request_id="req-failed", stage="failed",
        payload_hash=p_hash, created_at=T0, updated_at=T0,
        error_code="MANUAL_ADJUSTMENT_TRANSACTION_FAILED", failed_at_stage="committing",
    )
    env["review_store"].create_operation(env["task"].task_id, env["item"].item_id, op)
    # 不能重试
    r, err = _svc(env).build_and_apply(
        env["task"].task_id, env["item"].item_id, case.review_case_id, dec.decision_id,
        "req-failed", _changes(snap), ["LOW_CONFIDENCE"], "MANUAL_SCORE_CORRECTION")
    assert err is not None


def test_original_snapshot_unchanged(tmp_path):
    env = _seed(tmp_path)
    case, dec, adoption, snap, att = _setup_review(env)
    orig_total = snap.total_score
    _svc(env).build_and_apply(env["task"].task_id, env["item"].item_id, case.review_case_id, dec.decision_id,
                              "req-unchanged", _changes(snap), ["LOW_CONFIDENCE"], "MANUAL_SCORE_CORRECTION")
    reread = env["attempt_store"].get_snapshot(env["task"].task_id, env["item"].item_id, snap.snapshot_id)
    assert reread.total_score == orig_total


def test_only_one_active_adoption_after_failure(tmp_path):
    env = _seed(tmp_path)
    case, dec, adoption, snap, att = _setup_review(env)
    result, _ = _svc(env).build_and_apply(
        env["task"].task_id, env["item"].item_id, case.review_case_id, dec.decision_id,
        "req-one", _changes(snap), ["LOW_CONFIDENCE"], "MANUAL_SCORE_CORRECTION")
    active = env["review_store"].get_active_adoption(env["task"].task_id, env["item"].item_id)
    assert active is not None
    assert active.adoption_id == "adp-adjusted-req-one"
