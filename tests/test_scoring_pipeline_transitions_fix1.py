"""
Phase 11E-2a-prerequisite-impl-3-fix-1：scoring 多 item 错位推进专项测试。

覆盖：错位状态模型合法、并行推进、failed/manual_review 不阻塞、current_stage 取最早活动阶段、
阶段完成事件 stage 语义（记录完成阶段）、幂等按完成阶段查询、outcome_unknown resume 拒绝、
终态聚合。全部合成脱敏 fixture，不调用 Provider。
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from models.pipeline_task import (
    PipelineError,
    PipelineErrorSummary,
    PipelineEvent,
    PipelineItem,
    PipelineItemIndexEntry,
    PipelineStageSummary,
    PipelineTask,
    ResumeRequest,
    sha256_canonical,
)
from models.scoring_configuration import ScoringTaskConfiguration
from services.pipeline_task_manager import (
    ERR_OUTCOME_UNKNOWN_BLOCKED,
    ERR_SUCCESS_RESULT_PROTECTED,
    PipelineTaskError,
    PipelineTaskManager,
)
from services.pipeline_task_store import PipelineTaskStore, run_task_transaction

T0 = datetime(2026, 8, 11, 3, 0, 0, tzinfo=timezone.utc)
SHA = "a" * 64
SRC_TASK = "00000000-0000-0000-0000-000000000701"
SRC_ITEM_A = "00000000-0000-0000-0000-000000000a01"
SRC_ITEM_B = "00000000-0000-0000-0000-000000000a02"
SRC_ITEM_C = "00000000-0000-0000-0000-000000000a03"

_CONFIG_FIELDS = dict(
    profile_version="profile-mixed-v1",
    provider_id="provider_demo_001",
    model_id="model_demo_v1",
    scoring_policy_version="policy-001",
    rubric_version="rubric-001",
    prompt_version="prompt-001",
    response_schema_version="score-response-v1",
)


def _uuid_seq():
    i = [0]

    def gen() -> str:
        i[0] += 1
        return f"00000000-0000-0000-0000-{i[0]:012d}"

    return gen


def _config() -> ScoringTaskConfiguration:
    fields = dict(_CONFIG_FIELDS)
    return ScoringTaskConfiguration(**fields, configuration_fingerprint=sha256_canonical(fields))


def _item(item_id: str, task_id: str, status="pending", stage="score", source=SRC_ITEM_A,
         package_id="pkg-demo-001", **overrides):
    base = dict(
        contract_version="pipeline-item/v1",
        task_type="scoring_pipeline",
        source_item_id=source,
        item_id=item_id,
        task_id=task_id,
        package_id=package_id,
        package_revision=1,
        manifest_sha256=SHA,
        registration_record_id="record-0001",
        validation_id="validation-0001",
        input_fingerprint=sha256_canonical({"fp": item_id}),
        idempotency_key=sha256_canonical({"item": item_id}),
        status=status,
        current_stage=stage,
        attempt_count=0,
        max_attempts=3,
        heartbeat_interval_seconds=60,
        stale_after_seconds=300,
        heartbeat_updated_at=None,
        created_at=T0,
        updated_at=T0,
        retryable=False,
        last_error=None,
        output_ref=None,
        output_sha256=None,
        item_revision=1,
        evidence_level="sufficient",
    )
    base.update(overrides)
    return PipelineItem(**base)


def _summaries():
    def summary(stage, status, deps, counts):
        return PipelineStageSummary(
            stage=stage, status=status, depends_on=deps,
            started_at=None, completed_at=None,
            total_items=sum(counts.values()), pending_items=counts.get("pending", 0),
            running_items=counts.get("running", 0), completed_items=counts.get("completed", 0),
            failed_items=counts.get("failed", 0), skipped_items=counts.get("skipped", 0),
            manual_review_items=counts.get("manual_review", 0),
            blocking_error_codes=[], revision=1,
        )
    return [
        summary("import", "not_started", [], {}),
        summary("validate", "not_started", ["import"], {}),
        summary("score", "pending", [], {}),
        summary("review", "not_started", ["score"], {}),
        summary("export", "not_started", ["review"], {}),
    ]


def _scoring_task(task_id: str, items) -> PipelineTask:
    counts = Counter(i.status for i in items)
    idx = [PipelineItemIndexEntry(
        item_id=i.item_id, task_type=i.task_type, source_item_id=i.source_item_id,
        package_id=i.package_id, package_revision=i.package_revision,
        status=i.status, current_stage=i.current_stage, input_fingerprint=i.input_fingerprint,
        item_revision=i.item_revision, evidence_level=i.evidence_level,
    ) for i in items]
    return PipelineTask(
        contract_version="pipeline-task/v1",
        task_type="scoring_pipeline",
        execution_scope=["score", "review", "export"],
        source_task_id=SRC_TASK,
        task_id=task_id,
        batch_id="batch-001",
        status="pending",
        current_stage="score",
        created_at=T0,
        started_at=None,
        updated_at=T0,
        paused_at=None,
        completed_at=None,
        total_items=len(items),
        pending_items=counts.get("pending", 0),
        running_items=counts.get("running", 0),
        completed_items=counts.get("completed", 0),
        failed_items=counts.get("failed", 0),
        skipped_items=counts.get("skipped", 0),
        manual_review_items=counts.get("manual_review", 0),
        concurrency=2,
        configuration_snapshot=_config(),
        configuration_fingerprint=sha256_canonical(_CONFIG_FIELDS),
        item_index=idx,
        stage_summaries=_summaries(),
        error_summary=PipelineErrorSummary(count=0, codes=[]),
        last_event_sequence=1,
        revision=1,
        idempotency_key=sha256_canonical({"task": task_id}),
        idempotency_payload_sha256=SHA,
    )


def make_manager(tmp_path):
    store = PipelineTaskStore(tmp_path / "evidence-runtime" / "pipeline")
    store.tasks_dir.mkdir(parents=True, exist_ok=True)
    clock = [T0]

    def now():
        clock[0] = clock[0] + timedelta(seconds=1)
        return clock[0]

    return PipelineTaskManager(store, object(), clock=now, uuid_factory=_uuid_seq())


def seed(mgr, task, items):
    event = PipelineEvent(
        contract_version="pipeline-task/v1", event_id=mgr.uuid(), task_id=task.task_id,
        sequence=1, event_type="task_created", occurred_at=T0, stage=None, item_id=None,
        attempt_id=None, revision_before=0, revision_after=1, reason_code=None, metadata={})
    run_task_transaction(mgr.store, task.task_id, event, new_task=task, new_items=items,
                         expected_task_revision=None, expected_item_revisions={})


def _start_task(mgr, task):
    current = mgr.get_task(task.task_id)
    if current.status == "running":
        return current  # 已启动：返回当前快照，不重复启动
    return mgr.start_task(task.task_id, expected_revision=current.revision)


def _start_item(mgr, task, item, t=None):
    t = t or mgr.get_task(task.task_id)
    return mgr.start_item_stage(task.task_id, item.item_id,
                                expected_task_revision=t.revision,
                                expected_item_revision=item.item_revision)


def _complete_score(mgr, task, item, attempt, t=None):
    t = t or mgr.get_task(task.task_id)
    return mgr.complete_scoring_item_stage(task.task_id, item.item_id,
                                           expected_task_revision=t.revision,
                                           expected_item_revision=item.item_revision,
                                           attempt_id=attempt)


# ---------------- 1-2. 错位模型合法 ---------------- #


def test_two_item_staggered_model_legal(tmp_path):
    mgr = make_manager(tmp_path)
    task_id = "00000000-0000-0000-0000-000000000901"
    a = _item("00000000-0000-0000-0000-000000000b01", task_id, status="pending", stage="review", source=SRC_ITEM_A)
    b = _item("00000000-0000-0000-0000-000000000b02", task_id, status="pending", stage="score", source=SRC_ITEM_B, package_id="pkg-demo-002")
    task = _scoring_task(task_id, [a, b])
    seed(mgr, task, [a, b])
    t = _start_task(mgr, task)
    # A=review/pending、B=score/pending 聚合合法
    by = {s.stage: s for s in t.stage_summaries}
    assert by["score"].status == "pending"
    assert by["review"].status == "pending"
    assert by["export"].status == "not_started"
    assert t.current_stage == "score"  # 最早活动阶段


def test_three_item_all_stages_model_legal(tmp_path):
    mgr = make_manager(tmp_path)
    task_id = "00000000-0000-0000-0000-000000000902"
    a = _item("00000000-0000-0000-0000-000000000b11", task_id, status="pending", stage="export", source=SRC_ITEM_A)
    b = _item("00000000-0000-0000-0000-000000000b12", task_id, status="pending", stage="review", source=SRC_ITEM_B, package_id="pkg-demo-002")
    c = _item("00000000-0000-0000-0000-000000000b13", task_id, status="pending", stage="score", source=SRC_ITEM_C, package_id="pkg-demo-003")
    task = _scoring_task(task_id, [a, b, c])
    seed(mgr, task, [a, b, c])
    t = _start_task(mgr, task)
    by = {s.stage: s for s in t.stage_summaries}
    assert by["score"].status == "pending"
    assert by["review"].status == "pending"
    assert by["export"].status == "pending"
    assert t.current_stage == "score"


# ---------------- 3-5. 错位并行推进 ---------------- #


def _advance_item_to_export(mgr, task, item):
    """把单个 item 推进到 export/pending。"""
    t = _start_task(mgr, task)
    it = _start_item(mgr, task, item, t)
    t = mgr.get_task(task.task_id)
    it = _complete_score(mgr, task, it, mgr.uuid(), t)
    t = mgr.get_task(task.task_id)
    it = _start_item(mgr, task, it, t)
    t = mgr.get_task(task.task_id)
    return mgr.complete_scoring_item_stage(
        task.task_id, item.item_id,
        expected_task_revision=t.revision, expected_item_revision=it.item_revision,
        review_decision_id=mgr.uuid())


def test_a_completes_score_while_b_still_score(tmp_path):
    mgr = make_manager(tmp_path)
    task_id = "00000000-0000-0000-0000-000000000903"
    a = _item("00000000-0000-0000-0000-000000000b21", task_id, source=SRC_ITEM_A)
    b = _item("00000000-0000-0000-0000-000000000b22", task_id, source=SRC_ITEM_B, package_id="pkg-demo-002")
    task = _scoring_task(task_id, [a, b])
    seed(mgr, task, [a, b])
    # A 完成 score -> review/pending；B 仍在 score
    t = _start_task(mgr, task)
    ta = _start_item(mgr, task, a, t)
    t = mgr.get_task(task.task_id)
    ia = _complete_score(mgr, task, ta, mgr.uuid(), t)
    assert ia.current_stage == "review"
    ib = mgr.store.load_item(task.task_id, b.item_id)
    assert ib.current_stage == "score"
    assert ib.status == "pending"
    # 聚合合法（B score + A review）
    t2 = mgr.get_task(task.task_id)
    assert t2.current_stage == "score"


def test_a_export_while_b_c_in_earlier_stages(tmp_path):
    mgr = make_manager(tmp_path)
    task_id = "00000000-0000-0000-0000-000000000904"
    a = _item("00000000-0000-0000-0000-000000000b31", task_id, source=SRC_ITEM_A)
    b = _item("00000000-0000-0000-0000-000000000b32", task_id, source=SRC_ITEM_B, package_id="pkg-demo-002")
    c = _item("00000000-0000-0000-0000-000000000b33", task_id, source=SRC_ITEM_C, package_id="pkg-demo-003")
    task = _scoring_task(task_id, [a, b, c])
    seed(mgr, task, [a, b, c])
    ia = _advance_item_to_export(mgr, task, a)
    assert ia.current_stage == "export"
    # B/C 仍在 score/pending
    ib = mgr.store.load_item(task.task_id, b.item_id)
    ic = mgr.store.load_item(task.task_id, c.item_id)
    assert ib.current_stage == "score" and ic.current_stage == "score"
    t = mgr.get_task(task.task_id)
    by = {s.stage: s for s in t.stage_summaries}
    assert by["score"].status == "pending"
    assert by["review"].status == "completed"  # A 已完成 review 阶段（错位聚合）
    assert by["export"].status == "pending"
    assert t.current_stage == "score"


# ---------------- 6-7. failed/manual_review 不阻塞 ---------------- #


def test_failed_item_does_not_block_others(tmp_path):
    mgr = make_manager(tmp_path)
    task_id = "00000000-0000-0000-0000-000000000905"
    a = _item("00000000-0000-0000-0000-000000000b41", task_id, source=SRC_ITEM_A)
    b = _item("00000000-0000-0000-0000-000000000b42", task_id, source=SRC_ITEM_B, package_id="pkg-demo-002")
    task = _scoring_task(task_id, [a, b])
    seed(mgr, task, [a, b])
    t = _start_task(mgr, task)
    ta = _start_item(mgr, task, a, t)
    t = mgr.get_task(task.task_id)
    mgr.fail_item_non_retryable(task.task_id, a.item_id,
                                expected_task_revision=t.revision, expected_item_revision=ta.item_revision,
                                error_code="PROVIDER_SERVER_ERROR")
    # A failed 在 score，B 仍可推进 score->review->export
    ia = _advance_item_to_export(mgr, task, b)
    assert ia.current_stage == "export"
    t2 = mgr.get_task(task.task_id)
    assert t2.failed_items == 1
    # A 保留原阶段不推进
    assert mgr.store.load_item(task.task_id, a.item_id).current_stage == "score"


def test_manual_review_item_does_not_block_others(tmp_path):
    mgr = make_manager(tmp_path)
    task_id = "00000000-0000-0000-0000-000000000906"
    a = _item("00000000-0000-0000-0000-000000000b51", task_id, status="manual_review", stage="review", source=SRC_ITEM_A)
    b = _item("00000000-0000-0000-0000-000000000b52", task_id, source=SRC_ITEM_B, package_id="pkg-demo-002")
    task = _scoring_task(task_id, [a, b])
    seed(mgr, task, [a, b])
    _start_task(mgr, task)
    ib = _advance_item_to_export(mgr, task, b)
    assert ib.current_stage == "export"
    t = mgr.get_task(task.task_id)
    assert t.manual_review_items == 1
    assert mgr.store.load_item(task.task_id, a.item_id).current_stage == "review"


# ---------------- 8. current_stage 取最早活动阶段 ---------------- #


def test_current_stage_earliest_active_stage(tmp_path):
    mgr = make_manager(tmp_path)
    task_id = "00000000-0000-0000-0000-000000000907"
    a = _item("00000000-0000-0000-0000-000000000b61", task_id, source=SRC_ITEM_A)
    b = _item("00000000-0000-0000-0000-000000000b62", task_id, source=SRC_ITEM_B, package_id="pkg-demo-002")
    task = _scoring_task(task_id, [a, b])
    seed(mgr, task, [a, b])
    _start_task(mgr, task)
    # B 推到 export，A 留在 score
    _advance_item_to_export(mgr, task, b)
    t = mgr.get_task(task.task_id)
    assert t.current_stage == "score"  # A 在 score -> 最早活动阶段
    # A 也推进到 review -> 最早活动为 review
    ia = mgr.store.load_item(task.task_id, a.item_id)
    ta = _start_item(mgr, task, ia, t)
    t = mgr.get_task(task.task_id)
    ia2 = _complete_score(mgr, task, ta, mgr.uuid(), t)
    assert ia2.current_stage == "review"
    t2 = mgr.get_task(task.task_id)
    assert t2.current_stage == "review"


# ---------------- 9-13. 事件 stage 语义 ---------------- #


def test_score_complete_event_stage_is_score(tmp_path):
    mgr = make_manager(tmp_path)
    task_id = "00000000-0000-0000-0000-000000000908"
    a = _item("00000000-0000-0000-0000-000000000b71", task_id, source=SRC_ITEM_A)
    task = _scoring_task(task_id, [a])
    seed(mgr, task, [a])
    t = _start_task(mgr, task)
    ia = _start_item(mgr, task, a, t)
    t = mgr.get_task(task.task_id)
    attempt = mgr.uuid()
    _complete_score(mgr, task, ia, attempt, t)
    events = [e for e in mgr.store.load_event_log(task.task_id) if e.event_type == "item_completed"]
    assert len(events) == 1
    assert events[0].stage == "score"
    assert events[0].attempt_id == attempt


def test_review_complete_event_stage_is_review(tmp_path):
    mgr = make_manager(tmp_path)
    task_id = "00000000-0000-0000-0000-000000000909"
    a = _item("00000000-0000-0000-0000-000000000b81", task_id, source=SRC_ITEM_A)
    task = _scoring_task(task_id, [a])
    seed(mgr, task, [a])
    t = _start_task(mgr, task)
    ia = _start_item(mgr, task, a, t)
    t = mgr.get_task(task.task_id)
    ia = _complete_score(mgr, task, ia, mgr.uuid(), t)
    t = mgr.get_task(task.task_id)
    ia = _start_item(mgr, task, ia, t)
    t = mgr.get_task(task.task_id)
    mgr.complete_scoring_item_stage(
        task.task_id, a.item_id,
        expected_task_revision=t.revision, expected_item_revision=ia.item_revision,
        review_decision_id=mgr.uuid())
    events = [e for e in mgr.store.load_event_log(task.task_id) if e.event_type == "item_completed"]
    assert len(events) == 2
    assert events[0].stage == "score"
    assert events[1].stage == "review"


def test_export_complete_event_stage_is_export(tmp_path):
    mgr = make_manager(tmp_path)
    task_id = "00000000-0000-0000-0000-00000000090a"
    a = _item("00000000-0000-0000-0000-000000000b91", task_id, source=SRC_ITEM_A)
    task = _scoring_task(task_id, [a])
    seed(mgr, task, [a])
    ia = _advance_item_to_export(mgr, task, a)
    t = mgr.get_task(task.task_id)
    ia = _start_item(mgr, task, ia, t)
    t = mgr.get_task(task.task_id)
    mgr.complete_scoring_item_stage(
        task.task_id, a.item_id,
        expected_task_revision=t.revision, expected_item_revision=ia.item_revision,
        output_ref="outputs/out.json", output_sha256=SHA)
    events = [e for e in mgr.store.load_event_log(task.task_id) if e.event_type == "item_completed"]
    assert len(events) == 3
    assert [e.stage for e in events] == ["score", "review", "export"]


def test_score_idempotent_query_uses_stage_score(tmp_path):
    mgr = make_manager(tmp_path)
    task_id = "00000000-0000-0000-0000-00000000090b"
    a = _item("00000000-0000-0000-0000-000000000c01", task_id, source=SRC_ITEM_A)
    task = _scoring_task(task_id, [a])
    seed(mgr, task, [a])
    t = _start_task(mgr, task)
    ia = _start_item(mgr, task, a, t)
    t = mgr.get_task(task.task_id)
    attempt = mgr.uuid()
    _complete_score(mgr, task, ia, attempt, t)
    # 相同 attempt 幂等（按 stage=score 查询）
    t2 = mgr.get_task(task.task_id)
    ia2 = mgr.store.load_item(task.task_id, a.item_id)
    again = mgr.complete_scoring_item_stage(
        task.task_id, a.item_id,
        expected_task_revision=t2.revision, expected_item_revision=ia2.item_revision,
        attempt_id=attempt)
    assert again.current_stage == "review"


def test_score_different_attempt_conflict(tmp_path):
    mgr = make_manager(tmp_path)
    task_id = "00000000-0000-0000-0000-00000000090c"
    a = _item("00000000-0000-0000-0000-000000000c11", task_id, source=SRC_ITEM_A)
    task = _scoring_task(task_id, [a])
    seed(mgr, task, [a])
    t = _start_task(mgr, task)
    ia = _start_item(mgr, task, a, t)
    t = mgr.get_task(task.task_id)
    _complete_score(mgr, task, ia, mgr.uuid(), t)
    t2 = mgr.get_task(task.task_id)
    ia2 = mgr.store.load_item(task.task_id, a.item_id)
    with pytest.raises(PipelineTaskError) as ei:
        mgr.complete_scoring_item_stage(
            task.task_id, a.item_id,
            expected_task_revision=t2.revision, expected_item_revision=ia2.item_revision,
            attempt_id=mgr.uuid())
    assert ei.value.error_code == ERR_SUCCESS_RESULT_PROTECTED


# ---------------- 14. outcome_unknown resume 拒绝 ---------------- #


def test_outcome_unknown_resume_rejected(tmp_path):
    mgr = make_manager(tmp_path)
    task_id = "00000000-0000-0000-0000-00000000090d"
    a = _item(
        "00000000-0000-0000-0000-000000000c21", task_id,
        status="failed", stage="score", source=SRC_ITEM_A,
        retryable=True, attempt_count=1,
        last_error=PipelineError(
            error_code="PROVIDER_OUTCOME_UNKNOWN", message_key="PROVIDER_OUTCOME_UNKNOWN",
            retryable=True, stage="score"),
    )
    task = _scoring_task(task_id, [a])
    seed(mgr, task, [a])
    _start_task(mgr, task)
    # 构造 resume 请求（retryable-failed 模式）
    req = ResumeRequest(
        contract_version="pipeline-task/v1",
        request_id="00000000-0000-0000-0000-000000000d01",
        task_id=task_id,
        requested_at=T0,
        mode="resume_retryable_failed",
        expected_revision=mgr.get_task(task_id).revision,
        expected_item_revisions={a.item_id: a.item_revision},
        item_ids=[a.item_id],
        reason_code="RESUME_RETRYABLE_FAILED",
        dry_run=True,
    )
    mgr.create_resume_request(task_id, req)
    decision = mgr.evaluate_resume_request(task_id, req.request_id)
    # outcome_unknown 不得被恢复（拒绝，保留到核对/恢复决策）
    assert decision.approved is False
    assert a.item_id in {r.item_id for r in decision.rejected_items}
    # item 仍为 failed + score，未回 pending、未重试
    it = mgr.store.load_item(task.task_id, a.item_id)
    assert it.status == "failed"
    assert it.current_stage == "score"
    # 即使 apply 被误调也无法绕过（decision 未批准）
    assert not decision.eligible_items


# ---------------- 15-16. 终态聚合 ---------------- #


def _export_complete_item(mgr, task, item):
    ia = _advance_item_to_export(mgr, task, item)
    t = mgr.get_task(task.task_id)
    ia = _start_item(mgr, task, ia, t)
    t = mgr.get_task(task.task_id)
    return mgr.complete_scoring_item_stage(
        task.task_id, item.item_id,
        expected_task_revision=t.revision, expected_item_revision=ia.item_revision,
        output_ref="outputs/out.json", output_sha256=SHA)


def test_all_normal_export_completed_task_completed(tmp_path):
    mgr = make_manager(tmp_path)
    task_id = "00000000-0000-0000-0000-00000000090e"
    a = _item("00000000-0000-0000-0000-000000000c31", task_id, source=SRC_ITEM_A)
    b = _item("00000000-0000-0000-0000-000000000c32", task_id, source=SRC_ITEM_B, package_id="pkg-demo-002")
    task = _scoring_task(task_id, [a, b])
    seed(mgr, task, [a, b])
    _start_task(mgr, task)
    _export_complete_item(mgr, task, a)
    _export_complete_item(mgr, task, b)
    t = mgr.get_task(task.task_id)
    final = mgr.finalize_task_if_settled(task.task_id, expected_revision=t.revision)
    assert final.status == "completed"
    assert final.completed_at is not None
    assert final.current_stage is None


def test_completed_with_errors_when_failed_and_manual(tmp_path):
    mgr = make_manager(tmp_path)
    task_id = "00000000-0000-0000-0000-00000000090f"
    a = _item("00000000-0000-0000-0000-000000000c41", task_id, source=SRC_ITEM_A)
    b = _item("00000000-0000-0000-0000-000000000c42", task_id, status="manual_review", stage="review", source=SRC_ITEM_B, package_id="pkg-demo-002")
    task = _scoring_task(task_id, [a, b])
    seed(mgr, task, [a, b])
    _start_task(mgr, task)
    _export_complete_item(mgr, task, a)
    t = mgr.get_task(task.task_id)
    final = mgr.finalize_task_if_settled(task.task_id, expected_revision=t.revision)
    assert final.status == "completed_with_errors"
    assert final.completed_at is not None
