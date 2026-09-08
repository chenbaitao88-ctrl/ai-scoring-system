"""
Phase 11E-3b-1：ReviewCaseStore 文件型事实存储合成测试。

覆盖：case/decision/adoption/adjustment 创建读取、稳定排序、幂等、冲突、路径穿越、
损坏显式失败、写入回滚零残留、并发同 ID、事件只追加、开放工单查询、must_review 阻断
自动采用、active adoption 唯一、supersede 链保留旧记录、人工锁定保护、调分不覆盖原
快照、工单重开保留旧决定、引用绑定不一致拒绝、敏感内容不泄露、PipelineTask/ScoreAttempt/
数据库隔离、零 Provider/Gateway 调用。
全部合成脱敏数据；不写正文/Key/URL/学生信息。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import test_score_attempt_store as sa
from models.review_case import (
    ActorRef,
    ManualAdjustment,
    ManualFinalLock,
    ResultAdoption,
    ReviewCase,
    ReviewDecision,
)
from services.review_case_store import (
    ERR_ADJUSTMENT_CONFLICT,
    ERR_ADJUSTMENT_CORRUPTED,
    ERR_ADOPTION_BLOCKED,
    ERR_ADOPTION_CONFLICT,
    ERR_ADOPTION_CORRUPTED,
    ERR_ADOPTION_NOT_FOUND,
    ERR_BINDING_MISMATCH,
    ERR_CASE_CONFLICT,
    ERR_CASE_CORRUPTED,
    ERR_CASE_NOT_FOUND,
    ERR_DECISION_CONFLICT,
    ERR_DECISION_CORRUPTED,
    ERR_DECISION_IDEMPOTENCY_CONFLICT,
    ERR_DECISION_NOT_FOUND,
    ERR_DECISION_STATE_NOT_DECISIONABLE,
    ERR_EVENT_CORRUPTED,
    ERR_UNSAFE_PATH,
    ERR_WRITE_FAILED,
    ReviewCaseStore,
    ReviewFactStoreError,
)
from models.review_case import (
    ActorRef,
    ERR_LOCK_ACTOR_MISMATCH,
    ERR_LOCK_CONFLICT,
    ERR_LOCK_CORRUPTED,
    ERR_LOCK_NOT_FOUND,
    ERR_LOCK_REVISION_CONFLICT,
)
from services.score_attempt_store import ScoreAttemptStore

T0 = datetime(2026, 8, 13, 2, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=1)
T2 = T0 + timedelta(minutes=2)
T3 = T0 + timedelta(minutes=3)
TASK = "task_demo_001"
ITEM = "item_demo_001"
REV = "rev_demo_001"
REV2 = "rev_demo_002"
DEC = "dec_demo_001"
DEC2 = "dec_demo_002"
ADP = "adp_demo_001"
ADP2 = "adp_demo_002"
ADJ = "adj_demo_001"
SHA = "ab" * 32
SHA2 = "cd" * 32
REVIEWER = {"actor_type": "reviewer", "actor_id": "reviewer-demo-01"}
SYSTEM = {"actor_type": "system", "actor_id": "review-case-store"}
# 11F-1b：release_final_lock 接受 ActorRef 模型对象（与既有 dict 常量并存）
REVIEWER_REF = ActorRef(**REVIEWER)
SYSTEM_REF = ActorRef(**SYSTEM)


# ---------------- fixtures ---------------- #


@pytest.fixture
def env(tmp_path):
    """注入根目录的 attempt_store + review_store（跨 store 绑定校验启用）。"""
    attempt_store = ScoreAttemptStore(tmp_path / "score-attempts")
    attempt = sa.make_attempt(status="succeeded")
    attempt_store.create_attempt(TASK, ITEM, attempt)
    attempt_store.write_snapshot(TASK, ITEM, sa.make_snapshot())
    attempt_store.write_validation(TASK, ITEM, sa.make_validation())
    store = ReviewCaseStore(tmp_path / "reviews", attempt_store=attempt_store)
    return dict(root=tmp_path, attempt_store=attempt_store, store=store)


def make_case(review_case_id=REV, status="open", **overrides):
    base = dict(
        contract_version="score-attempt-review/v1",
        schema_version="review-case/v1",
        review_case_id=review_case_id,
        case_number=f"RC-{review_case_id.upper()}",
        submission_id="sub_demo_001",
        task_id=TASK,
        item_id=ITEM,
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
            assigned_at=T1, review_started_at=T1, resolved_at=T2,
            resolution_decision_id=DEC, updated_at=T2,
        ))
    base.update(overrides)
    return ReviewCase(**base)


def make_decision(decision_id=DEC, case_id=REV, decision_type="request_additional_evidence", **overrides):
    base = dict(
        contract_version="score-attempt-review/v1",
        schema_version="review-decision/v1",
        decision_id=decision_id,
        review_case_id=case_id,
        case_revision=1,
        decision_type=decision_type,
        reason_codes=["EVIDENCE_INSUFFICIENT"],
        idempotency_key=f"idem-{decision_id}",  # 11F-1b
        requested_package_revision=2,
        decision_note_code="REQUEST_MISSING_REQUIRED_EVIDENCE",
        decided_by=REVIEWER,
        decided_at=T1,
        decision_hash=SHA,
    )
    if decision_type in ("adopt_existing_attempt", "reject_attempt"):
        base.update(dict(
            target_attempt_id=sa.ATTEMPT_ID, target_snapshot_id=sa.SNAPSHOT_ID,
            requested_package_revision=None,
        ))
    if decision_type == "apply_manual_adjustment":
        base.update(dict(
            manual_adjustment_id=ADJ, requested_package_revision=None,
            reason_codes=["LOW_CONFIDENCE"],  # 11F-1b 矩阵：apply_manual_adjustment 仅允许 G3
        ))
    base.update(overrides)
    return ReviewDecision(**base)


def make_adoption(adoption_id=ADP, status="adopted", **overrides):
    base = dict(
        contract_version="score-attempt-review/v1",
        schema_version="result-adoption/v1",
        adoption_id=adoption_id,
        adoption_scope={
            "competition_id": "competition_demo_001",
            "batch_id": "batch-demo-001",  # 11F-1b
            "stream_id": "default",
            "submission_id": "sub_demo_001",
            "scoring_policy_version": "policy-demo-v1",
            "purpose": "machine_result",
        },
        submission_id="sub_demo_001",
        attempt_id=sa.ATTEMPT_ID,
        snapshot_id=sa.SNAPSHOT_ID,
        validation_id=sa.VALIDATION_ID,
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


def make_adjustment(adjustment_id=ADJ, **overrides):
    base = dict(
        contract_version="score-attempt-review/v1",
        schema_version="manual-adjustment/v1",
        adjustment_id=adjustment_id,
        review_case_id=REV,
        decision_id=DEC,
        submission_id="sub_demo_001",
        base_attempt_id=sa.ATTEMPT_ID,
        base_snapshot_id=sa.SNAPSHOT_ID,
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


def _seed_case(env, status="open", review_case_id=REV):
    env["store"].create_review_case(TASK, ITEM, make_case(review_case_id=review_case_id, status=status))
    return env["store"].get_review_case(TASK, ITEM, review_case_id)


def _seed_decision(env, decision=None, case_id=REV):
    decision = decision or make_decision(case_id=case_id)
    env["store"].create_review_decision(TASK, ITEM, decision)
    return env["store"].get_review_decision(TASK, ITEM, decision.decision_id)


# ---------------- 创建与读取 ---------------- #


def test_case_decision_adoption_adjustment_crud(env):
    store = env["store"]
    created, case = store.create_review_case(TASK, ITEM, make_case())
    assert created == "created" and case.status == "open"
    assert store.get_review_case(TASK, ITEM, REV).review_case_id == REV

    store.create_review_decision(TASK, ITEM, make_decision(
        decision_type="apply_manual_adjustment", manual_adjustment_id=ADJ))
    assert store.get_review_decision(TASK, ITEM, DEC).decision_id == DEC

    store.create_adoption(TASK, ITEM, make_adoption())
    assert store.get_adoption(TASK, ITEM, ADP).status == "adopted"

    store.create_manual_adjustment(TASK, ITEM, make_adjustment())
    assert store.get_manual_adjustment(TASK, ITEM, ADJ).adjustment_id == ADJ

    # 事件链：CASE_OPENED + DECISION_RECORDED + ADOPTION_LINKED + MANUAL_ADJUSTMENT_LINKED
    events = store.load_review_events(TASK, ITEM)
    assert [e.event_type for e in events] == [
        "CASE_OPENED", "DECISION_RECORDED", "ADOPTION_LINKED", "MANUAL_ADJUSTMENT_LINKED",
    ]
    # previous_event_hash 链：case 维度事件连续
    case_events = store.load_review_events(TASK, ITEM, review_case_id=REV)
    for prev, cur in zip(case_events, case_events[1:]):
        assert cur.previous_event_hash == prev.event_hash


def test_list_order_stable(env):
    store = env["store"]
    for i, cid in enumerate(("rev_demo_003", "rev_demo_002", "rev_demo_001")):
        store.create_review_case(TASK, ITEM, make_case(review_case_id=cid, idempotency_key=f"k-{i}"))
    ids = [c.review_case_id for c in store.list_review_cases(TASK, ITEM)]
    assert ids == sorted(ids)
    # item 过滤 + 跨 item 查询
    store.create_review_case(TASK, "item_demo_002", make_case(review_case_id="rev_demo_010", item_id="item_demo_002"))
    assert len(store.list_review_cases(TASK)) == 4
    assert len(store.list_review_cases(TASK, ITEM)) == 3


# ---------------- 幂等与冲突 ---------------- #


def test_idempotent_same_content_no_dup_events(env):
    store = env["store"]
    created1, _ = store.create_review_case(TASK, ITEM, make_case())
    created2, _ = store.create_review_case(TASK, ITEM, make_case())
    assert (created1, created2) == ("created", "idempotent_hit")
    assert len(store.load_review_events(TASK, ITEM)) == 1  # 不重复追加事件

    # decision/adoption/adjustment 同内容幂等
    store.create_review_decision(TASK, ITEM, make_decision(
        decision_type="apply_manual_adjustment", manual_adjustment_id=ADJ))
    again, _ = store.create_review_decision(TASK, ITEM, make_decision(
        decision_type="apply_manual_adjustment", manual_adjustment_id=ADJ))
    assert again == "idempotent_hit"
    store.create_adoption(TASK, ITEM, make_adoption())
    again, _ = store.create_adoption(TASK, ITEM, make_adoption())
    assert again == "idempotent_hit"
    store.create_manual_adjustment(TASK, ITEM, make_adjustment())
    again, _ = store.create_manual_adjustment(TASK, ITEM, make_adjustment())
    assert again == "idempotent_hit"


def test_same_id_diff_content_conflict(env):
    store = env["store"]
    store.create_review_case(TASK, ITEM, make_case())
    with pytest.raises(ReviewFactStoreError) as ei:
        store.create_review_case(TASK, ITEM, make_case(priority="urgent"))
    assert ei.value.error_code == ERR_CASE_CONFLICT

    store.create_review_decision(TASK, ITEM, make_decision(
        decision_type="apply_manual_adjustment", manual_adjustment_id=ADJ))
    with pytest.raises(ReviewFactStoreError) as ei:
        store.create_review_decision(TASK, ITEM, make_decision(
            decision_type="apply_manual_adjustment", manual_adjustment_id=ADJ,
            decision_note_code="OTHER_CODE"))
    # 11F-1b：同 idempotency_key（idem-dec_demo_001）异内容 -> 幂等冲突码
    assert ei.value.error_code == ERR_DECISION_IDEMPOTENCY_CONFLICT

    store.create_adoption(TASK, ITEM, make_adoption())
    with pytest.raises(ReviewFactStoreError) as ei:
        store.create_adoption(TASK, ITEM, make_adoption(reason_code="OTHER"))
    assert ei.value.error_code == ERR_ADOPTION_CONFLICT

    store.create_manual_adjustment(TASK, ITEM, make_adjustment())
    with pytest.raises(ReviewFactStoreError) as ei:
        store.create_manual_adjustment(TASK, ITEM, make_adjustment(adjustment_note_code="OTHER"))
    assert ei.value.error_code == ERR_ADJUSTMENT_CONFLICT


# ---------------- 路径穿越 ---------------- #


@pytest.mark.parametrize("bad_id", [
    "", "..", "../evil", "a/b", "a\\b", "a\x00b", "x" * 200, "con", "NUL", ".hidden",
    "with space", "中文id",
])
def test_unsafe_paths_rejected(env, bad_id):
    store = env["store"]
    with pytest.raises(ReviewFactStoreError) as ei:
        store.get_review_case(bad_id, ITEM, REV)
    assert ei.value.error_code == ERR_UNSAFE_PATH
    with pytest.raises(ReviewFactStoreError) as ei:
        store.create_review_case(TASK, bad_id, make_case().model_copy(update={"task_id": bad_id, "item_id": bad_id}))
    assert ei.value.error_code == ERR_UNSAFE_PATH
    with pytest.raises(ReviewFactStoreError) as ei:
        store.create_review_case(TASK, ITEM, make_case().model_copy(update={"review_case_id": bad_id}))
    assert ei.value.error_code == ERR_UNSAFE_PATH
    with pytest.raises(ReviewFactStoreError) as ei:
        store.create_review_decision(TASK, ITEM, make_decision().model_copy(update={"decision_id": bad_id}))
    assert ei.value.error_code == ERR_UNSAFE_PATH
    with pytest.raises(ReviewFactStoreError) as ei:
        store.create_adoption(TASK, ITEM, make_adoption().model_copy(update={"adoption_id": bad_id}))
    assert ei.value.error_code == ERR_UNSAFE_PATH
    with pytest.raises(ReviewFactStoreError) as ei:
        store.create_manual_adjustment(TASK, ITEM, make_adjustment().model_copy(update={"adjustment_id": bad_id}))
    assert ei.value.error_code == ERR_UNSAFE_PATH


# ---------------- 损坏显式失败 ---------------- #


def _corrupt(env, rel_dir, object_id):
    path = env["root"] / "reviews" / "tasks" / TASK / "items" / ITEM / rel_dir / f"{object_id}.json"
    path.write_text("{not-json", encoding="utf-8")


def test_corrupted_json_explicit_failure(env):
    store = env["store"]
    # 先建齐四类对象
    store.create_review_case(TASK, ITEM, make_case())
    store.create_review_decision(TASK, ITEM, make_decision(
        decision_type="apply_manual_adjustment", manual_adjustment_id=ADJ))
    store.create_adoption(TASK, ITEM, make_adoption())
    store.create_manual_adjustment(TASK, ITEM, make_adjustment())
    # 逐个损坏并验证显式失败（不伪装 NOT_FOUND）
    _corrupt(env, "cases", REV)
    with pytest.raises(ReviewFactStoreError) as ei:
        store.get_review_case(TASK, ITEM, REV)
    assert ei.value.error_code == ERR_CASE_CORRUPTED
    with pytest.raises(ReviewFactStoreError) as ei:
        store.list_review_cases(TASK, ITEM)
    assert ei.value.error_code == ERR_CASE_CORRUPTED

    _corrupt(env, "decisions", DEC)
    with pytest.raises(ReviewFactStoreError) as ei:
        store.get_review_decision(TASK, ITEM, DEC)
    assert ei.value.error_code == ERR_DECISION_CORRUPTED

    _corrupt(env, "adoptions", ADP)
    with pytest.raises(ReviewFactStoreError) as ei:
        store.get_adoption(TASK, ITEM, ADP)
    assert ei.value.error_code == ERR_ADOPTION_CORRUPTED

    _corrupt(env, "adjustments", ADJ)
    with pytest.raises(ReviewFactStoreError) as ei:
        store.get_manual_adjustment(TASK, ITEM, ADJ)
    assert ei.value.error_code == ERR_ADJUSTMENT_CORRUPTED


def test_corrupted_event_log_explicit_failure(env):
    store = env["store"]
    store.create_review_case(TASK, ITEM, make_case())
    events = env["root"] / "reviews" / "tasks" / TASK / "items" / ITEM / "events.ndjson"
    with open(events, "a", encoding="utf-8") as fh:
        fh.write("{broken\n")
    with pytest.raises(ReviewFactStoreError) as ei:
        store.load_review_events(TASK, ITEM)
    assert ei.value.error_code == ERR_EVENT_CORRUPTED


def test_corrupted_event_sequence_discontinuity(env):
    store = env["store"]
    store.create_review_case(TASK, ITEM, make_case())
    events = env["root"] / "reviews" / "tasks" / TASK / "items" / ITEM / "events.ndjson"
    lines = events.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[0])
    row["sequence"] = 99
    events.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ReviewFactStoreError) as ei:
        store.load_review_events(TASK, ITEM)
    assert ei.value.error_code == ERR_EVENT_CORRUPTED


# ---------------- 写入失败回滚 ---------------- #


def test_write_failure_rollback_no_residue(env, monkeypatch):
    import services.review_case_store as mod
    calls = {"n": 0}

    def boom(target, data):
        calls["n"] += 1
        raise OSError("disk full")

    monkeypatch.setattr(mod, "_atomic_write_bytes", boom)
    store = env["store"]
    with pytest.raises(ReviewFactStoreError) as ei:
        store.create_review_case(TASK, ITEM, make_case())
    assert ei.value.error_code == ERR_WRITE_FAILED
    assert ei.value.retryable is True
    # 不残留业务文件；空目录链被清理
    reviews_root = env["root"] / "reviews"
    leftovers = [str(p) for p in reviews_root.rglob("*") if p.is_file()]
    assert leftovers == []


def test_event_write_failure_rolls_back_object(env, monkeypatch):
    import services.review_case_store as mod
    orig = mod._append_ndjson_bytes

    def boom(path, line):
        if str(path).endswith("events.ndjson"):
            raise OSError("event append failed")
        return orig(path, line)

    monkeypatch.setattr(mod, "_append_ndjson_bytes", boom)
    store = env["store"]
    with pytest.raises(ReviewFactStoreError) as ei:
        store.create_review_case(TASK, ITEM, make_case())
    assert ei.value.error_code == ERR_WRITE_FAILED
    assert store.get_review_case(TASK, ITEM, REV) is None  # 对象回滚删除


# ---------------- 并发同 ID ---------------- #


def test_concurrent_same_id_single_writer(env):
    store = env["store"]
    results = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        try:
            out, _ = store.create_review_case(TASK, ITEM, make_case())
            results.append(out)
        except ReviewFactStoreError as e:
            results.append(e.error_code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    outcomes = sorted(results)
    # 一个首次成功，另一个幂等命中或锁冲突
    assert outcomes.count("created") == 1
    assert outcomes[1] in ("idempotent_hit", ERR_LOCK_CONFLICT)
    assert len(store.load_review_events(TASK, ITEM)) == 1


# ---------------- 开放工单查询 ---------------- #


def test_open_cases_query(env):
    store = env["store"]
    store.create_review_case(TASK, ITEM, make_case())  # open
    _seed_case(env, status="open", review_case_id=REV2)
    store.create_review_case(TASK, ITEM, make_case(
        review_case_id="rev_demo_003", reason_codes=["EVIDENCE_INSUFFICIENT"],
    ))
    # 关闭一个
    store.transition_review_case(
        TASK, ITEM, REV2, expected_revision=1, to_status="cancelled",
        event_type="CASE_CANCELLED", actor=SYSTEM, occurred_at=T1,
    )
    open_ids = [c.review_case_id for c in store.list_open_cases(TASK, ITEM)]
    assert open_ids == [REV, "rev_demo_003"]
    assert store.has_open_review_case(TASK, ITEM) is True


# ---------------- must_review 阻断自动采用 ---------------- #


def test_must_review_blocks_auto_adoption(env):
    store = env["store"]
    store.create_review_case(TASK, ITEM, make_case())  # EVIDENCE_INSUFFICIENT -> blocks_auto_adoption
    with pytest.raises(ReviewFactStoreError) as ei:
        store.adopt_result(TASK, ITEM, make_adoption())
    assert ei.value.error_code == ERR_ADOPTION_BLOCKED
    assert store.get_active_adoption(TASK, ITEM) is None


def test_must_review_does_not_block_human_adoption(env):
    store = env["store"]
    store.create_review_case(TASK, ITEM, make_case())
    human = make_adoption(adoption_id=ADP2, decided_by=REVIEWER,
                          reason_code="HUMAN_REVIEW_APPROVED")
    adopted = store.adopt_result(TASK, ITEM, human)
    assert adopted.status == "adopted"
    assert store.get_active_adoption(TASK, ITEM).adoption_id == ADP2


# ---------------- active adoption 唯一 ---------------- #


def test_active_adoption_unique_detects_inconsistency(env):
    store = env["store"]
    store.create_adoption(TASK, ITEM, make_adoption(adoption_id=ADP))
    store.create_adoption(TASK, ITEM, make_adoption(adoption_id=ADP2, decided_by=REVIEWER,
                                                    reason_code="HUMAN_REVIEW_APPROVED"))
    with pytest.raises(ReviewFactStoreError) as ei:
        store.get_active_adoption(TASK, ITEM)
    assert ei.value.error_code == ERR_BINDING_MISMATCH


def test_adopt_without_declaring_supersede_conflict(env):
    store = env["store"]
    store.adopt_result(TASK, ITEM, make_adoption())  # 系统自动采用
    with pytest.raises(ReviewFactStoreError) as ei:
        store.adopt_result(TASK, ITEM, make_adoption(adoption_id=ADP2, reason_code="OTHER"))
    assert ei.value.error_code == ERR_ADOPTION_CONFLICT


# ---------------- supersede 链保留旧记录 ---------------- #


def test_supersede_chain_keeps_old_record(env):
    store = env["store"]
    store.adopt_result(TASK, ITEM, make_adoption())  # ADP adopted
    new = make_adoption(adoption_id=ADP2, reason_code="SUPERSEDED_BY_NEW_ADOPTION",
                        supersedes_adoption_id=ADP)
    adopted = store.adopt_result(TASK, ITEM, new)
    assert adopted.status == "adopted" and adopted.adoption_id == ADP2
    # 旧记录保留并转 superseded；active 唯一
    old = store.get_adoption(TASK, ITEM, ADP)
    assert old.status == "superseded"
    assert store.get_active_adoption(TASK, ITEM).adoption_id == ADP2
    assert len(store.list_adoptions(TASK, ITEM)) == 2


# ---------------- 人工锁定保护 ---------------- #


def test_human_locked_blocks_auto_replace(env):
    store = env["store"]
    human = make_adoption(adoption_id=ADP, decided_by=REVIEWER, reason_code="HUMAN_REVIEW_APPROVED")
    store.adopt_result(TASK, ITEM, human)
    # 人工（reviewer）采用被自动（system）采用替换 -> blocked
    with pytest.raises(ReviewFactStoreError) as ei:
        store.adopt_result(TASK, ITEM, make_adoption(
            adoption_id=ADP2, supersedes_adoption_id=ADP, reason_code="AUTO_ADOPTION_VALIDATION_PASSED",
        ))
    assert ei.value.error_code == ERR_ADOPTION_BLOCKED
    assert store.get_active_adoption(TASK, ITEM).adoption_id == ADP  # 未被替换
    # 人工替换人工 -> 允许
    store.adopt_result(TASK, ITEM, make_adoption(
        adoption_id=ADP2, supersedes_adoption_id=ADP, decided_by=REVIEWER,
        reason_code="HUMAN_REVIEW_APPROVED",
    ))
    assert store.get_active_adoption(TASK, ITEM).adoption_id == ADP2


def test_revoke_adoption_keeps_record(env):
    store = env["store"]
    store.adopt_result(TASK, ITEM, make_adoption())
    revoked = store.transition_adoption(TASK, ITEM, ADP, to_status="revoked",
                                        actor=SYSTEM, occurred_at=T2, reason_code="REVOKED_BY_ADMIN")
    assert revoked.status == "revoked"
    assert store.get_adoption(TASK, ITEM, ADP).status == "revoked"
    assert store.get_active_adoption(TASK, ITEM) is None


def test_adoption_terminal_no_rollback(env):
    store = env["store"]
    store.create_adoption(TASK, ITEM, make_adoption())
    store.transition_adoption(TASK, ITEM, ADP, to_status="revoked", actor=SYSTEM,
                              occurred_at=T2, reason_code="REVOKED_BY_ADMIN")
    with pytest.raises(ReviewFactStoreError) as ei:
        store.transition_adoption(TASK, ITEM, ADP, to_status="adopted", actor=SYSTEM,
                                  occurred_at=T3, reason_code="BACK")
    assert ei.value.error_code == ERR_ADOPTION_CONFLICT


# ---------------- 调分不覆盖原快照 ---------------- #


def test_adjustment_does_not_overwrite_snapshot(env):
    store = env["store"]
    # 决策链：case -> apply_manual_adjustment decision -> adjustment
    _seed_case(env)
    store.create_review_decision(TASK, ITEM, make_decision(
        decision_id=DEC, decision_type="apply_manual_adjustment", manual_adjustment_id=ADJ,
    ))
    store.create_manual_adjustment(TASK, ITEM, make_adjustment())
    adj = store.get_manual_adjustment(TASK, ITEM, ADJ)
    # 原快照未被修改
    snap = env["attempt_store"].get_snapshot(TASK, ITEM, sa.SNAPSHOT_ID)
    assert snap.total_score == 82.0
    assert snap.snapshot_id == sa.SNAPSHOT_ID
    assert adj.adjusted_snapshot_id != adj.base_snapshot_id
    assert adj.base_snapshot_id == snap.snapshot_id


# ---------------- 工单重开保留旧决定 ---------------- #


def test_reopen_keeps_old_resolution(env):
    store = env["store"]
    store.create_review_case(TASK, ITEM, make_case())
    # in_review 时记录 resolution decision（adopt_existing_attempt 引用既有 attempt/snapshot）
    store.create_review_decision(TASK, ITEM, make_decision(
        decision_type="adopt_existing_attempt", target_attempt_id=sa.ATTEMPT_ID,
        target_snapshot_id=sa.SNAPSHOT_ID,
    ))
    # in_review -> resolved（携带 resolution decision）
    store.transition_review_case(TASK, ITEM, REV, expected_revision=1, to_status="in_review",
                                 event_type="REVIEW_STARTED", actor=REVIEWER, occurred_at=T1)
    store.transition_review_case(TASK, ITEM, REV, expected_revision=2, to_status="resolved",
                                 event_type="CASE_RESOLVED", actor=REVIEWER, occurred_at=T2,
                                 resolution_decision_id=DEC)
    closed = store.get_review_case(TASK, ITEM, REV)
    assert closed.status == "resolved" and closed.resolution_decision_id == DEC
    # 重开（resolved -> open）：保留旧 resolution，reopen_count+1
    reopened = store.transition_review_case(TASK, ITEM, REV, expected_revision=3, to_status="open",
                                            event_type="CASE_REOPENED", actor=SYSTEM, occurred_at=T3)
    assert reopened.status == "open"
    assert reopened.reopen_count == 1
    assert reopened.previous_resolution_decision_id == DEC
    assert reopened.resolution_decision_id is None
    assert reopened.current_revision == 4
    # 旧决定事实仍可读
    assert store.get_review_decision(TASK, ITEM, DEC) is not None
    # 事件：OPENED/DECISION_RECORDED/STARTED/RESOLVED/REOPENED
    assert [e.event_type for e in store.load_review_events(TASK, ITEM)] == [
        "CASE_OPENED", "DECISION_RECORDED", "REVIEW_STARTED", "CASE_RESOLVED", "CASE_REOPENED",
    ]


def test_closed_case_cannot_record_new_decision(env):
    store = env["store"]
    store.create_review_case(TASK, ITEM, make_case(status="resolved"))
    with pytest.raises(ReviewFactStoreError) as ei:
        store.create_review_decision(TASK, ITEM, make_decision(
            decision_id=DEC2, decision_type="request_additional_evidence",
        ))
    # 11F-1b：resolved 工单不可记录新决定 -> REVIEW_CASE_STATE_NOT_DECISIONABLE
    assert ei.value.error_code == ERR_DECISION_STATE_NOT_DECISIONABLE


def test_cancelled_case_cannot_record_decision(env):
    store = env["store"]
    store.create_review_case(TASK, ITEM, make_case())
    store.transition_review_case(TASK, ITEM, REV, expected_revision=1, to_status="cancelled",
                                 event_type="CASE_CANCELLED", actor=SYSTEM, occurred_at=T1)
    with pytest.raises(ReviewFactStoreError) as ei:
        store.create_review_decision(TASK, ITEM, make_decision())
    # 11F-1b：cancelled 工单不可记录决定 -> REVIEW_CASE_STATE_NOT_DECISIONABLE
    assert ei.value.error_code == ERR_DECISION_STATE_NOT_DECISIONABLE


def test_illegal_case_transition_rejected(env):
    store = env["store"]
    store.create_review_case(TASK, ITEM, make_case())
    with pytest.raises(ReviewFactStoreError) as ei:
        store.transition_review_case(TASK, ITEM, REV, expected_revision=1, to_status="resolved",
                                     event_type="CASE_RESOLVED", actor=REVIEWER, occurred_at=T1,
                                     resolution_decision_id=DEC)
    assert ei.value.error_code == ERR_CASE_CONFLICT  # open -> resolved 非法


def test_case_cas_revision_conflict(env):
    store = env["store"]
    store.create_review_case(TASK, ITEM, make_case())
    with pytest.raises(ReviewFactStoreError) as ei:
        store.transition_review_case(TASK, ITEM, REV, expected_revision=9, to_status="assigned",
                                     event_type="CASE_ASSIGNED", actor=SYSTEM, occurred_at=T1)
    assert ei.value.error_code == ERR_CASE_CONFLICT


# ---------------- 引用绑定不一致 ---------------- #


def test_decision_case_not_found(env):
    store = env["store"]
    with pytest.raises(ReviewFactStoreError) as ei:
        store.create_review_decision(TASK, ITEM, make_decision())
    assert ei.value.error_code == ERR_CASE_NOT_FOUND


def test_adoption_snapshot_missing_binding_mismatch(env):
    store = env["store"]
    with pytest.raises(ReviewFactStoreError) as ei:
        store.create_adoption(TASK, ITEM, make_adoption(snapshot_id="snap_missing_001"))
    assert ei.value.error_code == ERR_BINDING_MISMATCH


def test_adoption_attempt_snapshot_mismatch(env):
    store = env["store"]
    with pytest.raises(ReviewFactStoreError) as ei:
        store.create_adoption(TASK, ITEM, make_adoption(attempt_id="atp_other_001"))
    assert ei.value.error_code == ERR_BINDING_MISMATCH


def test_adoption_validation_mismatch(env):
    store = env["store"]
    with pytest.raises(ReviewFactStoreError) as ei:
        store.create_adoption(TASK, ITEM, make_adoption(validation_id="val_other_001"))
    assert ei.value.error_code == ERR_BINDING_MISMATCH


def test_adoption_decision_not_found(env):
    store = env["store"]
    _seed_case(env)
    with pytest.raises(ReviewFactStoreError) as ei:
        store.create_adoption(TASK, ITEM, make_adoption(decision_id="dec_missing_001"))
    assert ei.value.error_code == ERR_DECISION_NOT_FOUND


def test_adjustment_decision_type_mismatch(env):
    store = env["store"]
    _seed_case(env)
    _seed_decision(env, make_decision(decision_type="request_additional_evidence"))
    with pytest.raises(ReviewFactStoreError) as ei:
        store.create_manual_adjustment(TASK, ITEM, make_adjustment())
    assert ei.value.error_code == ERR_BINDING_MISMATCH


def test_adjustment_base_snapshot_missing(env):
    store = env["store"]
    _seed_case(env)
    store.create_review_decision(TASK, ITEM, make_decision(
        decision_type="apply_manual_adjustment", manual_adjustment_id=ADJ,
    ))
    with pytest.raises(ReviewFactStoreError) as ei:
        store.create_manual_adjustment(TASK, ITEM, make_adjustment(base_snapshot_id="snap_missing_001"))
    assert ei.value.error_code == ERR_BINDING_MISMATCH


# ---------------- 敏感内容不泄露 ---------------- #


def test_sensitive_content_not_in_errors_or_events(env):
    store = env["store"]
    try:
        store.create_adoption(TASK, ITEM, make_adoption(snapshot_id="snap_missing_001"))
    except ReviewFactStoreError as e:
        msg = str(e)
        assert "snap_missing" not in msg
        assert "task_demo" not in msg and "reviews" not in msg and ":" not in msg
        assert e.error_code == ERR_BINDING_MISMATCH
    # 事件不包含路径/正文
    store.create_review_case(TASK, ITEM, make_case())
    for e in store.load_review_events(TASK, ITEM):
        dumped = json.dumps(e.model_dump(mode="json"))
        assert "C:\\" not in dumped and "sk-" not in dumped and "http" not in dumped


def test_error_message_only_stable_code():
    err = ReviewFactStoreError(ERR_CASE_CONFLICT)
    assert str(err) == ERR_CASE_CONFLICT


# ---------------- 隔离与零调用 ---------------- #


def test_no_pipeline_or_attempt_modification(env):
    """store 只写注入的 reviews 根；不改 ScoreAttempt/Snapshot/Validation 文件与 PipelineTask 文件。"""
    before_attempts = {
        p.name: p.read_bytes() for p in
        (env["root"] / "score-attempts" / "tasks" / TASK / "items" / ITEM / "attempts").glob("*.json")
    }
    store = env["store"]
    store.create_review_case(TASK, ITEM, make_case())
    store.create_review_decision(TASK, ITEM, make_decision(
        decision_type="apply_manual_adjustment", manual_adjustment_id=ADJ))
    store.create_adoption(TASK, ITEM, make_adoption())
    store.create_manual_adjustment(TASK, ITEM, make_adjustment())
    after_attempts = {
        p.name: p.read_bytes() for p in
        (env["root"] / "score-attempts" / "tasks" / TASK / "items" / ITEM / "attempts").glob("*.json")
    }
    assert before_attempts == after_attempts  # attempt 事实未被修改
    # 全部新增文件位于注入 reviews 根下
    reviews_root = env["root"] / "reviews"
    assert reviews_root.exists()
    assert not (env["root"] / "machine_scores").exists()
    # 数据库文件不存在（零 schema）
    assert not (env["root"] / "scoring.db").exists()


def test_store_source_no_gateway_provider_resolver():
    """store 源码不引用 Gateway/Provider 调用/CredentialResolver/httpx/环境变量。"""
    import services.review_case_store as mod
    src = open(mod.__file__, encoding="utf-8").read()
    for bad in ("ScoringProviderGateway", "CredentialResolver", "httpx", "os.getenv", "environ", "requests"):
        assert bad not in src, bad
    import models.review_case as mmod
    msrc = open(mmod.__file__, encoding="utf-8").read()
    for bad in ("httpx", "os.getenv", "environ", "requests", "http://", "https://"):
        assert bad not in msrc, bad


# ---------------------------------------------------------------- 11F-1b：ManualFinalLock / AdoptionScope 隔离


def make_lock(lock_id="flk_demo_001", adoption_id=ADP, **overrides):
    """构造人工最终锁（默认引用 ADP adoption 与 SNAPSHOT_ID 快照）。"""
    base = dict(
        contract_version="score-attempt-review/v1",
        schema_version="manual-final-lock/v1",
        lock_id=lock_id,
        task_id=TASK,
        item_id=ITEM,
        review_case_id=REV,
        decision_id=DEC,
        adoption_id=adoption_id,
        snapshot_id=sa.SNAPSHOT_ID,
        locked_by=REVIEWER,
        locked_at=T1,
        reason_code="HUMAN_FINAL_LOCKED",
        status="active",
        revision=1,
        idempotency_key=f"idem-{lock_id}",
        content_hash=SHA,
    )
    base.update(overrides)
    return ManualFinalLock(**base)


def _seed_for_lock(store):
    """最小前置：case + decision（供 lock 绑定校验引用）。"""
    store.create_review_case(TASK, ITEM, make_case())
    store.create_review_decision(TASK, ITEM, make_decision(
        decision_type="adopt_existing_attempt", target_attempt_id=sa.ATTEMPT_ID,
        target_snapshot_id=sa.SNAPSHOT_ID, idempotency_key="idem-lock-dec"))


def test_final_lock_create_and_active(env):
    store = env["store"]
    _seed_for_lock(store)
    created, lock = store.create_final_lock(TASK, ITEM, make_lock())
    assert created == "created"
    assert lock.status == "active" and lock.revision == 1
    active = store.get_active_final_lock(TASK, ITEM)
    assert active is not None and active.lock_id == lock.lock_id
    # 事件：FINAL_RESULT_LOCKED 追加
    events = [e.event_type for e in store.load_review_events(TASK, ITEM)]
    assert "FINAL_RESULT_LOCKED" in events


def test_final_lock_duplicate_same_content_idempotent(env):
    store = env["store"]
    _seed_for_lock(store)
    store.create_final_lock(TASK, ITEM, make_lock())
    again, _ = store.create_final_lock(TASK, ITEM, make_lock())
    assert again == "idempotent_hit"
    # 幂等命中不重复事件
    events = [e.event_type for e in store.load_review_events(TASK, ITEM)]
    assert events.count("FINAL_RESULT_LOCKED") == 1


def test_final_lock_duplicate_active_conflict(env):
    store = env["store"]
    _seed_for_lock(store)
    store.create_final_lock(TASK, ITEM, make_lock())
    with pytest.raises(ReviewFactStoreError) as ei:
        store.create_final_lock(TASK, ITEM, make_lock(
            lock_id="flk_demo_002", idempotency_key="idem-flk2"))
    assert ei.value.error_code == ERR_LOCK_CONFLICT


def test_final_lock_release_and_double_release(env):
    store = env["store"]
    _seed_for_lock(store)
    store.create_final_lock(TASK, ITEM, make_lock())
    _, released = store.release_final_lock(
        TASK, ITEM, "flk_demo_001", expected_revision=1, actor=REVIEWER_REF, occurred_at=T2)
    assert released.status == "released" and released.revision == 2
    assert store.get_active_final_lock(TASK, ITEM) is None
    # 同参数重复 release：已 released 且 revision/操作者一致 -> 幂等命中
    _, again = store.release_final_lock(
        TASK, ITEM, "flk_demo_001", expected_revision=2, actor=REVIEWER_REF, occurred_at=T2)
    assert again.status == "released"
    # 事件包含 UNLOCKED
    events = [e.event_type for e in store.load_review_events(TASK, ITEM)]
    assert "FINAL_RESULT_UNLOCKED" in events


def test_final_lock_wrong_actor_release(env):
    store = env["store"]
    _seed_for_lock(store)
    store.create_final_lock(TASK, ITEM, make_lock())
    with pytest.raises(ReviewFactStoreError) as ei:
        store.release_final_lock(
            TASK, ITEM, "flk_demo_001", expected_revision=1, actor=SYSTEM_REF, occurred_at=T2)
    assert ei.value.error_code == ERR_LOCK_ACTOR_MISMATCH


def test_final_lock_revision_conflict(env):
    store = env["store"]
    _seed_for_lock(store)
    store.create_final_lock(TASK, ITEM, make_lock())
    with pytest.raises(ReviewFactStoreError) as ei:
        store.release_final_lock(
            TASK, ITEM, "flk_demo_001", expected_revision=99, actor=REVIEWER_REF, occurred_at=T2)
    assert ei.value.error_code == ERR_LOCK_REVISION_CONFLICT


def test_final_lock_missing_not_found(env):
    store = env["store"]
    _seed_for_lock(store)
    with pytest.raises(ReviewFactStoreError) as ei:
        store.release_final_lock(
            TASK, ITEM, "flk_nope", expected_revision=1, actor=REVIEWER_REF, occurred_at=T2)
    assert ei.value.error_code == ERR_LOCK_NOT_FOUND


def test_final_lock_corrupted_explicit(env):
    store = env["store"]
    _seed_for_lock(store)
    store.create_final_lock(TASK, ITEM, make_lock())
    lock_path = env["root"] / "reviews" / "tasks" / TASK / "items" / ITEM / "final-locks" / "flk_demo_001.json"
    lock_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ReviewFactStoreError) as ei:
        store.get_active_final_lock(TASK, ITEM)
    assert ei.value.error_code == ERR_LOCK_CORRUPTED


def test_active_lock_blocks_auto_adoption(env):
    store = env["store"]
    _seed_for_lock(store)
    store.create_adoption(TASK, ITEM, make_adoption())  # ADP 已采用
    store.create_final_lock(TASK, ITEM, make_lock())  # 锁定 ADP（active）
    # 1) active 锁阻断新采用（自动/任何采用均被阻断）
    with pytest.raises(ReviewFactStoreError) as ei:
        store.adopt_result(TASK, ITEM, make_adoption(adoption_id=ADP2))
    assert ei.value.error_code == ERR_ADOPTION_BLOCKED
    # 2) 被锁定 adoption 禁止被 supersede/revoke
    with pytest.raises(ReviewFactStoreError) as ei:
        store.transition_adoption(TASK, ITEM, ADP, to_status="superseded",
                                  actor=SYSTEM_REF, occurred_at=T2)
    assert ei.value.error_code == ERR_ADOPTION_BLOCKED


def test_adoption_scope_batch_stream_isolation(env):
    store = env["store"]
    # 批次 A 采用
    store.adopt_result(TASK, ITEM, make_adoption(
        adoption_id=ADP,
        adoption_scope={
            "competition_id": "competition_demo_001",
            "batch_id": "batch-a-001",
            "stream_id": "default",
            "submission_id": "sub_demo_001",
            "scoring_policy_version": "policy-demo-v1",
            "purpose": "machine_result",
        }))
    # 批次 B 采用（不同 batch_id，不互相 supersede）
    b = make_adoption(
        adoption_id=ADP2,
        adoption_scope={
            "competition_id": "competition_demo_001",
            "batch_id": "batch-b-001",
            "stream_id": "default",
            "submission_id": "sub_demo_001",
            "scoring_policy_version": "policy-demo-v1",
            "purpose": "machine_result",
        })
    b = b.model_copy(update={"supersedes_adoption_id": None})
    store.adopt_result(TASK, ITEM, b)
    # 两个 active 共存（不同 scope）
    active_a = store.get_active_adoption(TASK, ITEM, scope={
        "competition_id": "competition_demo_001", "batch_id": "batch-a-001",
        "stream_id": "default", "submission_id": "sub_demo_001",
        "scoring_policy_version": "policy-demo-v1", "purpose": "machine_result"})
    active_b = store.get_active_adoption(TASK, ITEM, scope={
        "competition_id": "competition_demo_001", "batch_id": "batch-b-001",
        "stream_id": "default", "submission_id": "sub_demo_001",
        "scoring_policy_version": "policy-demo-v1", "purpose": "machine_result"})
    assert active_a is not None and active_a.adoption_id == ADP
    assert active_b is not None and active_b.adoption_id == ADP2
    # 同 scope 二次采用必须显式 supersede（scope 内唯一）
    with pytest.raises(ReviewFactStoreError) as ei:
        store.adopt_result(TASK, ITEM, make_adoption(
            adoption_id="adp_demo_003",
            adoption_scope={
                "competition_id": "competition_demo_001", "batch_id": "batch-a-001",
                "stream_id": "default", "submission_id": "sub_demo_001",
                "scoring_policy_version": "policy-demo-v1", "purpose": "machine_result",
            }))
    assert ei.value.error_code == ERR_ADOPTION_CONFLICT


def test_legacy_adoption_scope_read_compat(env):
    """旧 adoption sidecar 缺 batch_id/stream_id：只读兼容为 legacy/default。"""
    store = env["store"]
    legacy = make_adoption(
        adoption_id=ADP,
        adoption_scope={
            "competition_id": "competition_demo_001",
            "submission_id": "sub_demo_001",
            "scoring_policy_version": "policy-demo-v1",
            "purpose": "machine_result",
        })
    # 模型层默认补 batch_id=legacy / stream_id=default
    assert legacy.adoption_scope.batch_id == "legacy"
    assert legacy.adoption_scope.stream_id == "default"
    # 但新写入拒绝 legacy 标记（store 强制隔离字段）
    with pytest.raises(ReviewFactStoreError) as ei:
        store.adopt_result(TASK, ITEM, legacy)
    assert ei.value.error_code == ERR_BINDING_MISMATCH


def test_decision_reason_matrix_rejected(env):
    """11F-1b：decision_type × reason_code 矩阵——非法组合在模型层即拒绝。"""
    from models.review_case import validate_decision_reason_combo

    store = env["store"]
    store.create_review_case(TASK, ITEM, make_case())
    # helper 层：apply_manual_adjustment 不允许 EVIDENCE_INSUFFICIENT（G1）
    assert validate_decision_reason_combo("apply_manual_adjustment", ["EVIDENCE_INSUFFICIENT"]) == "EVIDENCE_INSUFFICIENT"
    # 模型构造即拒绝（矩阵在模型层强制）
    with pytest.raises(Exception):
        make_decision(
            decision_type="apply_manual_adjustment", manual_adjustment_id=ADJ,
            reason_codes=["EVIDENCE_INSUFFICIENT"], idempotency_key="idem-mx")
    # 合法组合可创建
    created, _ = store.create_review_decision(TASK, ITEM, make_decision(
        decision_type="apply_manual_adjustment", manual_adjustment_id=ADJ,
        reason_codes=["LOW_CONFIDENCE"], idempotency_key="idem-mx-ok"))
    assert created == "created"


def test_decision_target_required(env):
    """11F-1b：必需 target 字段——缺失在模型层即拒绝。"""
    store = env["store"]
    store.create_review_case(TASK, ITEM, make_case())
    with pytest.raises(Exception):
        make_decision(
            decision_type="adopt_existing_attempt",
            target_attempt_id=None, target_snapshot_id=None,
            requested_package_revision=None, idempotency_key="idem-tgt")
    # 合法组合可创建
    created, _ = store.create_review_decision(TASK, ITEM, make_decision(
        decision_type="adopt_existing_attempt", target_attempt_id=sa.ATTEMPT_ID,
        target_snapshot_id=sa.SNAPSHOT_ID, idempotency_key="idem-tgt-ok"))
    assert created == "created"
