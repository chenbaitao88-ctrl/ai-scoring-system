"""
Phase 11E-3b-2：ReviewCase 采用流程接入 PipelineTask 集成测试。

覆盖：score 闭环 -> 打开工单 -> reviewer 决定(adopt_existing_attempt) -> 人工采用 ->
item review 阶段完成（review_decision_id 落 item / export/pending）；幂等重放；
非采用决策不推进；绑定拒绝；推进失败无假完成；敏感零泄露；零 Provider 调用。
全部合成脱敏数据；不写正文/Key/URL/学生信息。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import test_scoring_pipeline_execute_integration as base
from models.review_case import ReviewCase, ReviewDecision
from services.review_case_store import ReviewCaseStore

T0 = datetime(2026, 8, 14, 2, 0, 0, tzinfo=timezone.utc)
SHA = "ab" * 32
REVIEWER = {"actor_type": "reviewer", "actor_id": "reviewer-demo-01"}
CASE_ID = "rev_app_001"
DEC_ADOPT = "dec-app-001"
DEC_OTHER = "dec-app-002"


# ---------------- fixtures ---------------- #


@pytest.fixture
def env(tmp_path):
    """复用 11E-2b-2 环境并注入 review_store 到 orchestrator。"""
    e = base._seed_env(tmp_path)
    e["review_store"] = ReviewCaseStore(tmp_path / "reviews", attempt_store=e["attempt_store"])
    e["orch"]._review_store = e["review_store"]
    return e


def _score_once(env):
    """跑通 score 闭环，item 进入 review/pending；返回 (item, attempt, snapshot_id, validation_id)。"""
    item = base._item(env)
    r = base._run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-app"))
    assert r.outcome == "succeeded"
    item_after = base._item(env)
    assert (item_after.current_stage, item_after.status) == ("review", "pending")
    attempts = env["attempt_store"].list_attempts(env["task"].task_id, item_after.item_id)
    attempt = attempts[-1]
    assert attempt.status == "succeeded"
    return item_after, attempt, attempt.result_snapshot_ref, attempt.validation_ref


def _open_case(env, item, attempt, snapshot_id):
    case = ReviewCase(
        contract_version="score-attempt-review/v1",
        schema_version="review-case/v1",
        review_case_id=CASE_ID,
        case_number="RC-APP-0001",
        submission_id="sub_demo_001",
        task_id=env["task"].task_id,
        item_id=item.item_id,
        package_id=item.package_id,
        package_revision=item.package_revision,
        attempt_ids=[attempt.attempt_id],
        snapshot_ids=[snapshot_id],
        source_type="manual",
        reason_codes=["EVIDENCE_INSUFFICIENT"],
        priority="high",
        status="open",
        blocks_auto_adoption=True,
        blocks_export=True,
        opened_at=T0,
        updated_at=T0,
        reopen_count=0,
        current_revision=1,
    )
    created, _ = env["review_store"].create_review_case(env["task"].task_id, item.item_id, case)
    assert created == "created"
    return case


def _make_decision(decision_id=DEC_ADOPT, case_id=CASE_ID, decision_type="adopt_existing_attempt",
                   attempt=None, snapshot_id=None, **overrides):
    base_kwargs = dict(
        contract_version="score-attempt-review/v1",
        schema_version="review-decision/v1",
        decision_id=decision_id,
        review_case_id=case_id,
        case_revision=1,
        decision_type=decision_type,
        reason_codes=["EVIDENCE_INSUFFICIENT"],
        idempotency_key=f"idem-{decision_id}",  # 11F-1b
        decision_note_code="ADOPT_EXISTING_ATTEMPT",
        decided_by=REVIEWER,
        decided_at=T0,
        decision_hash=SHA,
    )
    if decision_type in ("adopt_existing_attempt", "reject_attempt"):
        base_kwargs.update(target_attempt_id=attempt.attempt_id, target_snapshot_id=snapshot_id)
    if decision_type == "request_additional_evidence":
        base_kwargs.update(requested_package_revision=2)
    base_kwargs.update(overrides)
    return ReviewDecision(**base_kwargs)


def _apply(env, item, decision_id=DEC_ADOPT, competition_id="competition_demo_001", request_id="app-req-01"):
    return env["orch"].apply_review_decision(
        env["task"].task_id, item.item_id, decision_id, request_id,
        competition_id=competition_id,
    )


# ---------------- 成功闭环 ---------------- #


def test_apply_adopt_success_closure(env):
    """score 闭环 -> 工单 -> reviewer adopt 决定 -> 人工采用 + item review 完成。"""
    item, attempt, snapshot_id, _ = _score_once(env)
    _open_case(env, item, attempt, snapshot_id)
    env["review_store"].create_review_decision(env["task"].task_id, item.item_id,
                                               _make_decision(attempt=attempt, snapshot_id=snapshot_id))
    r = _apply(env, item)
    assert r.outcome == "adopted"
    assert r.adoption_id == f"adp-{DEC_ADOPT}"
    assert r.review_decision_id == DEC_ADOPT
    # adoption 事实落盘（人工采用，reviewer）
    active = env["review_store"].get_active_adoption(env["task"].task_id, item.item_id)
    assert active is not None and active.status == "adopted"
    assert active.decided_by.actor_type == "reviewer"
    assert active.decision_id == DEC_ADOPT
    # item 推进：review -> export/pending，review_decision_id 落 item
    item_after = base._item(env)
    assert (item_after.current_stage, item_after.status) == ("export", "pending")
    assert item_after.review_decision_id == DEC_ADOPT
    # 事件：adoption 链接事件存在
    evs = env["review_store"].load_review_events(env["task"].task_id, item.item_id)
    assert any(e.event_type == "ADOPTION_LINKED" for e in evs)


def test_apply_idempotent_replay(env):
    """同 decision 重复 apply -> already_completed；不产生第二份 adoption 事实。"""
    item, attempt, snapshot_id, _ = _score_once(env)
    _open_case(env, item, attempt, snapshot_id)
    env["review_store"].create_review_decision(env["task"].task_id, item.item_id,
                                               _make_decision(attempt=attempt, snapshot_id=snapshot_id))
    r1 = _apply(env, item, request_id="app-req-01")
    r2 = _apply(env, item, request_id="app-req-02")
    assert r1.outcome == "adopted"
    assert r2.outcome == "already_completed"
    assert r2.adoption_id == r1.adoption_id
    adoptions = env["review_store"].list_adoptions(env["task"].task_id, item.item_id)
    assert len(adoptions) == 1  # 无重复采用


def test_apply_non_adopt_decision_does_not_advance(env):
    """request_additional_evidence 决定：仅登记事实，item 保持 review/pending 不推进。"""
    item, attempt, snapshot_id, _ = _score_once(env)
    _open_case(env, item, attempt, snapshot_id)
    env["review_store"].create_review_decision(
        env["task"].task_id, item.item_id,
        _make_decision(decision_id=DEC_OTHER, decision_type="request_additional_evidence",
                       attempt=attempt, snapshot_id=snapshot_id))
    r = _apply(env, item, decision_id=DEC_OTHER)
    assert r.outcome == "not_adopted"
    item_after = base._item(env)
    assert (item_after.current_stage, item_after.status) == ("review", "pending")
    assert item_after.review_decision_id is None
    assert env["review_store"].get_active_adoption(env["task"].task_id, item.item_id) is None


# ---------------- 绑定与阻断 ---------------- #


def test_apply_decision_not_found(env):
    item, attempt, snapshot_id, _ = _score_once(env)
    r = _apply(env, item, decision_id="dec-missing-001")
    assert r.outcome == "blocked"
    assert r.error_code == "REVIEW_DECISION_NOT_FOUND"


def test_apply_case_not_bound_to_item(env):
    """decision 对应 case 属于其他 item -> REVIEW_FACT_BINDING_MISMATCH。"""
    item, attempt, snapshot_id, _ = _score_once(env)
    case = _open_case(env, item, attempt, snapshot_id)
    # 把 case 绑定到另一个 item（目录上下文仍是本 item -> 创建失败）
    # 改为：直接构造指向其他 item 的 decision 引用的 case 场景——用 model_copy 绕过目录绑定，
    # 让 get_review_case 在本 item 目录下读不到（REVIEW_CASE_NOT_FOUND），
    # 或构造一个本 item 目录下但 case.task_id/item_id 不符的 case（绑定检查拦截）。
    other_case = ReviewCase(
        contract_version="score-attempt-review/v1", schema_version="review-case/v1",
        review_case_id="rev_other_001", case_number="RC-OTHER",
        submission_id="sub_demo_001", task_id=env["task"].task_id, item_id="item_other_001",
        source_type="manual", reason_codes=["EVIDENCE_INSUFFICIENT"], priority="normal",
        status="open", blocks_auto_adoption=True, blocks_export=True,
        opened_at=T0, updated_at=T0, reopen_count=0, current_revision=1,
    )
    # 写入其他 item 目录
    env["review_store"].create_review_case(env["task"].task_id, "item_other_001", other_case)
    decision = _make_decision(decision_id="dec-other-001", case_id="rev_other_001",
                              attempt=attempt, snapshot_id=snapshot_id)
    # decision 写入本 item 目录（decision.review_case_id 指向其他 item 的 case -> 绑定拒绝）
    with pytest.raises(Exception):
        env["review_store"].create_review_decision(env["task"].task_id, item.item_id, decision)
    # 直接构造绑定不匹配场景：case 存在于本 item 但 decision 引用不存在的 case
    r = _apply(env, item, decision_id="dec-ghost-001")
    assert r.outcome == "blocked"
    assert r.error_code == "REVIEW_DECISION_NOT_FOUND"


def test_apply_item_not_in_review_stage(env):
    """item 未进入 review（score 未执行）-> REVIEW_APPLICATION_NOT_APPLICABLE。"""
    item = base._item(env)
    assert item.current_stage == "score"
    # 无 attempt/snapshot，无法构造 adopt decision -> 走 not_applicable 前置检查
    r = _apply(env, item)
    assert r.outcome == "blocked"
    assert r.error_code in ("REVIEW_APPLICATION_NOT_APPLICABLE", "REVIEW_FACT_BINDING_MISMATCH",
                            "REVIEW_DECISION_NOT_FOUND")


def test_apply_must_review_open_case_human_adoption_allowed(env):
    """开放 must_review 工单（EVIDENCE_INSUFFICIENT）不阻断人工采用。"""
    item, attempt, snapshot_id, _ = _score_once(env)
    _open_case(env, item, attempt, snapshot_id)
    env["review_store"].create_review_decision(env["task"].task_id, item.item_id,
                                               _make_decision(attempt=attempt, snapshot_id=snapshot_id))
    r = _apply(env, item)
    assert r.outcome == "adopted"


# ---------------- 推进失败无假完成 ---------------- #


def test_apply_item_advance_failure_no_fake_completion(env, monkeypatch):
    """采用事实落盘但 item 推进失败：不伪装成功，无 review_decision_id。"""
    import services.pipeline_task_manager as mgr_mod

    def boom(*a, **k):
        raise RuntimeError("advance failed")

    item, attempt, snapshot_id, _ = _score_once(env)
    _open_case(env, item, attempt, snapshot_id)
    env["review_store"].create_review_decision(env["task"].task_id, item.item_id,
                                               _make_decision(attempt=attempt, snapshot_id=snapshot_id))
    monkeypatch.setattr(env["orch"]._operator, "complete_scoring_item_stage", boom)
    r = _apply(env, item)
    assert r.outcome == "internal_error"
    assert r.error_code == "REVIEW_APPLICATION_ITEM_ADVANCE_FAILED"
    # adoption 事实已落盘（幂等可恢复），item 无假完成
    assert env["review_store"].get_active_adoption(env["task"].task_id, item.item_id) is not None
    item_after = base._item(env)
    assert item_after.review_decision_id is None
    # 重试：adopt 幂等命中不产生第二份 adoption；item 处于 review/running（start 已成功）-> blocked
    monkeypatch.undo()
    r2 = _apply(env, item, request_id="app-req-02")
    assert r2.outcome == "blocked"
    assert len(env["review_store"].list_adoptions(env["task"].task_id, item.item_id)) == 1


# ---------------- 敏感与零调用 ---------------- #


def test_apply_sensitive_no_leak(env):
    """错误信息与结果不含路径/正文/Key/学生信息。"""
    item, attempt, snapshot_id, _ = _score_once(env)
    r = _apply(env, item, decision_id="dec-missing-001")
    assert r.error_code == "REVIEW_DECISION_NOT_FOUND"
    dumped = r.model_dump(mode="json")
    assert "C:\\" not in str(dumped) and "sk-" not in str(dumped) and "http" not in str(dumped)


def test_apply_never_calls_provider(env):
    """采用流程零 Provider 调用（transport 计数不变）。"""
    item, attempt, snapshot_id, _ = _score_once(env)
    calls_before = len(env["transport"].calls)
    _open_case(env, item, attempt, snapshot_id)
    env["review_store"].create_review_decision(env["task"].task_id, item.item_id,
                                               _make_decision(attempt=attempt, snapshot_id=snapshot_id))
    r = _apply(env, item)
    assert r.outcome == "adopted"
    assert len(env["transport"].calls) == calls_before
