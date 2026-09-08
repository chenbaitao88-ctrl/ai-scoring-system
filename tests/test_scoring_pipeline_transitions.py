"""
Phase 11E-2a-prerequisite-impl-3：scoring_pipeline 状态机推进测试。

覆盖：聚合保留 scoring 字段、score->review->export 推进、幂等与成功保护、
终态聚合、outcome_unknown 阻断、CAS/事务零副作用、limited 准入、旧 evidence 兼容。

全部合成脱敏 fixture；不调用 Provider、不创建真实 ScoreAttempt、不接 API。
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from models.evidence_sidecar import IssueCounts, ValidationResult
from models.pipeline_task import (
    PipelineError,
    PipelineErrorSummary,
    PipelineEvent,
    PipelineItem,
    PipelineItemIndexEntry,
    PipelineStageSummary,
    PipelineTask,
    sha256_canonical,
)
from models.scoring_configuration import ScoringTaskConfiguration
from services.pipeline_task_manager import (
    ERR_ATTEMPT_LIMIT_EXCEEDED,
    ERR_INVALID_STATE_TRANSITION,
    ERR_OUTCOME_UNKNOWN_BLOCKED,
    ERR_REVISION_CONFLICT,
    ERR_SCOPE_VIOLATION,
    ERR_SUCCESS_RESULT_PROTECTED,
    PipelineTaskError,
    PipelineTaskManager,
)
from services.pipeline_task_store import (
    ERR_TRANSACTION_ERROR,
    PipelineStoreError,
    PipelineTaskStore,
    run_task_transaction,
)

T0 = datetime(2026, 8, 11, 3, 0, 0, tzinfo=timezone.utc)
SHA = "a" * 64
SRC_TASK = "00000000-0000-0000-0000-000000000701"
SRC_ITEM = "00000000-0000-0000-0000-000000000801"

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


def _scoring_item(item_id: str, task_id: str, status="pending", stage="score", **overrides):
    base = dict(
        contract_version="pipeline-item/v1",
        task_type="scoring_pipeline",
        source_item_id=SRC_ITEM,
        item_id=item_id,
        task_id=task_id,
        package_id="pkg-demo-001",
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


def _stage_summaries(item_count: int):
    def summary(stage: str, status: str, depends_on, total: int) -> PipelineStageSummary:
        return PipelineStageSummary(
            stage=stage, status=status, depends_on=depends_on,
            started_at=None, completed_at=None,
            total_items=total, pending_items=total if status == "pending" else 0,
            running_items=0, completed_items=0, failed_items=0, skipped_items=0,
            manual_review_items=0, blocking_error_codes=[], revision=1,
        )
    return [
        summary("import", "not_started", [], 0),
        summary("validate", "not_started", ["import"], 0),
        summary("score", "pending", [], item_count),
        summary("review", "not_started", ["score"], 0),
        summary("export", "not_started", ["review"], 0),
    ]


def _scoring_task(task_id: str, items=None) -> PipelineTask:
    items = items or []
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
        concurrency=1,
        configuration_snapshot=_config(),
        configuration_fingerprint=sha256_canonical(_CONFIG_FIELDS),
        item_index=idx,
        stage_summaries=_stage_summaries(len(items)),
        error_summary=PipelineErrorSummary(count=0, codes=[]),
        last_event_sequence=1,
        revision=1,
        idempotency_key=sha256_canonical({"task": task_id}),
        idempotency_payload_sha256=SHA,
    )


class FakeRegistration:
    def __init__(self):
        pass

    def read_record_strict(self, package_id, package_revision):
        return None

    def read_validation_strict(self, package_id, package_revision, validation_id):
        return None


def make_manager(tmp_path):
    store = PipelineTaskStore(tmp_path / "evidence-runtime" / "pipeline")
    store.tasks_dir.mkdir(parents=True, exist_ok=True)
    clock = [T0]

    def now():
        clock[0] = clock[0] + timedelta(seconds=1)
        return clock[0]

    return PipelineTaskManager(store, FakeRegistration(), clock=now, uuid_factory=_uuid_seq())


def seed_scoring(mgr: PipelineTaskManager, task: PipelineTask, items) -> None:
    """通过真实事务创建任务（task_created 事件 sequence=1 与快照一致）。"""
    event = PipelineEvent(
        contract_version="pipeline-task/v1",
        event_id=mgr.uuid(),
        task_id=task.task_id,
        sequence=1,
        event_type="task_created",
        occurred_at=T0,
        stage=None,
        item_id=None,
        attempt_id=None,
        revision_before=0,
        revision_after=1,
        reason_code=None,
        metadata={},
    )
    run_task_transaction(
        mgr.store, task.task_id, event,
        new_task=task, new_items=items,
        expected_task_revision=None, expected_item_revisions={},
    )


def _load(mgr, task_id, item_id):
    return mgr.store.load_task(task_id), mgr.store.load_item(task_id, item_id)


# ---------------- 1-2. 聚合 ---------------- #


def test_aggregate_preserves_scoring_item_fields(tmp_path):
    mgr = make_manager(tmp_path)
    item = _scoring_item("00000000-0000-0000-0000-000000000a01", "00000000-0000-0000-0000-000000000901")
    task = _scoring_task("00000000-0000-0000-0000-000000000901", [item])
    seed_scoring(mgr, task, [item])
    task2 = mgr.start_task(task.task_id, expected_revision=1)
    entry = task2.item_index[0]
    assert entry.task_type == "scoring_pipeline"
    assert entry.source_item_id == SRC_ITEM
    assert entry.current_stage == "score"
    assert task2.total_items == 1


def test_scoring_aggregate_never_activates_import_validate(tmp_path):
    mgr = make_manager(tmp_path)
    item = _scoring_item("00000000-0000-0000-0000-000000000a02", "00000000-0000-0000-0000-000000000902")
    task = _scoring_task("00000000-0000-0000-0000-000000000902", [item])
    seed_scoring(mgr, task, [item])
    task2 = mgr.start_task(task.task_id, expected_revision=1)
    by_stage = {s.stage: s for s in task2.stage_summaries}
    assert by_stage["import"].status == "not_started"
    assert by_stage["validate"].status == "not_started"
    assert by_stage["import"].total_items == 0
    assert by_stage["validate"].total_items == 0
    assert by_stage["score"].status == "pending"
    assert by_stage["score"].pending_items == 1


# ---------------- 3-4. 启动 ---------------- #


def test_scoring_task_pending_to_running(tmp_path):
    mgr = make_manager(tmp_path)
    item = _scoring_item("00000000-0000-0000-0000-000000000a03", "00000000-0000-0000-0000-000000000903")
    task = _scoring_task("00000000-0000-0000-0000-000000000903", [item])
    seed_scoring(mgr, task, [item])
    t = mgr.start_task(task.task_id, expected_revision=1)
    assert t.status == "running"
    assert t.started_at is not None
    assert t.current_stage == "score"
    # 重复启动拒绝（不重复写事件）
    with pytest.raises(PipelineTaskError) as ei:
        mgr.start_task(task.task_id, expected_revision=2)
    assert ei.value.error_code == ERR_INVALID_STATE_TRANSITION


def test_scoring_item_pending_to_running(tmp_path):
    mgr = make_manager(tmp_path)
    item = _scoring_item("00000000-0000-0000-0000-000000000a04", "00000000-0000-0000-0000-000000000904")
    task = _scoring_task("00000000-0000-0000-0000-000000000904", [item])
    seed_scoring(mgr, task, [item])
    t = mgr.start_task(task.task_id, expected_revision=1)
    it = mgr.start_item_stage(task.task_id, item.item_id,
                              expected_task_revision=t.revision, expected_item_revision=item.item_revision)
    assert it.status == "running"
    assert it.current_stage == "score"
    assert it.attempt_count == 1
    assert it.heartbeat_updated_at is not None


# ---------------- 5-8. score 完成 ---------------- #


def _to_running(mgr, task, item):
    t = mgr.start_task(task.task_id, expected_revision=task.revision)
    it = mgr.start_item_stage(task.task_id, item.item_id,
                              expected_task_revision=t.revision, expected_item_revision=item.item_revision)
    t2 = mgr.get_task(task.task_id)  # item start 后 task revision 已递增
    return t2, it


def test_score_complete_to_review_with_attempt(tmp_path):
    mgr = make_manager(tmp_path)
    item = _scoring_item("00000000-0000-0000-0000-000000000a05", "00000000-0000-0000-0000-000000000905")
    task = _scoring_task("00000000-0000-0000-0000-000000000905", [item])
    seed_scoring(mgr, task, [item])
    t, it = _to_running(mgr, task, item)
    attempt = "00000000-0000-0000-0000-000000000b05"
    it2 = mgr.complete_scoring_item_stage(
        task.task_id, item.item_id,
        expected_task_revision=t.revision, expected_item_revision=it.item_revision,
        attempt_id=attempt)
    assert it2.current_stage == "review"
    assert it2.status == "pending"
    t2 = mgr.get_task(task.task_id)
    assert t2.current_stage == "review"
    # 事件保留 attempt_id
    events = mgr.store.load_event_log(task.task_id)
    completed = [e for e in events if e.event_type == "item_completed"]
    assert len(completed) == 1
    assert completed[0].attempt_id == attempt


def test_score_complete_idempotent_same_attempt(tmp_path):
    mgr = make_manager(tmp_path)
    item = _scoring_item("00000000-0000-0000-0000-000000000a06", "00000000-0000-0000-0000-000000000906")
    task = _scoring_task("00000000-0000-0000-0000-000000000906", [item])
    seed_scoring(mgr, task, [item])
    t, it = _to_running(mgr, task, item)
    attempt = "00000000-0000-0000-0000-000000000b06"
    mgr.complete_scoring_item_stage(task.task_id, item.item_id,
                                    expected_task_revision=t.revision, expected_item_revision=it.item_revision,
                                    attempt_id=attempt)
    t2 = mgr.get_task(task.task_id)
    it2 = mgr.store.load_item(task.task_id, item.item_id)
    # 相同 attempt 重复提交 -> 幂等返回原状态
    it3 = mgr.complete_scoring_item_stage(task.task_id, item.item_id,
                                          expected_task_revision=t2.revision, expected_item_revision=it2.item_revision,
                                          attempt_id=attempt)
    assert it3.current_stage == "review"
    events = [e for e in mgr.store.load_event_log(task.task_id) if e.event_type == "item_completed"]
    assert len(events) == 1  # 不追加事件


def test_score_complete_different_attempt_conflict(tmp_path):
    mgr = make_manager(tmp_path)
    item = _scoring_item("00000000-0000-0000-0000-000000000a07", "00000000-0000-0000-0000-000000000907")
    task = _scoring_task("00000000-0000-0000-0000-000000000907", [item])
    seed_scoring(mgr, task, [item])
    t, it = _to_running(mgr, task, item)
    mgr.complete_scoring_item_stage(task.task_id, item.item_id,
                                    expected_task_revision=t.revision, expected_item_revision=it.item_revision,
                                    attempt_id="00000000-0000-0000-0000-000000000b07")
    t2 = mgr.get_task(task.task_id)
    it2 = mgr.store.load_item(task.task_id, item.item_id)
    with pytest.raises(PipelineTaskError) as ei:
        mgr.complete_scoring_item_stage(task.task_id, item.item_id,
                                        expected_task_revision=t2.revision, expected_item_revision=it2.item_revision,
                                        attempt_id="00000000-0000-0000-0000-000000000c07")
    assert ei.value.error_code == ERR_SUCCESS_RESULT_PROTECTED


def test_score_complete_requires_attempt(tmp_path):
    mgr = make_manager(tmp_path)
    item = _scoring_item("00000000-0000-0000-0000-000000000a08", "00000000-0000-0000-0000-000000000908")
    task = _scoring_task("00000000-0000-0000-0000-000000000908", [item])
    seed_scoring(mgr, task, [item])
    t, it = _to_running(mgr, task, item)
    with pytest.raises(PipelineTaskError) as ei:
        mgr.complete_scoring_item_stage(task.task_id, item.item_id,
                                        expected_task_revision=t.revision, expected_item_revision=it.item_revision)
    assert ei.value.error_code == ERR_INVALID_STATE_TRANSITION


# ---------------- 9-11. review / export / 终态 ---------------- #


def _to_review_pending(mgr, task, item):
    t, it = _to_running(mgr, task, item)
    it2 = mgr.complete_scoring_item_stage(
        task.task_id, item.item_id,
        expected_task_revision=t.revision, expected_item_revision=it.item_revision,
        attempt_id="00000000-0000-0000-0000-000000000b09")
    t2 = mgr.get_task(task.task_id)
    return t2, it2


def test_review_complete_to_export_pending(tmp_path):
    mgr = make_manager(tmp_path)
    item = _scoring_item("00000000-0000-0000-0000-000000000a09", "00000000-0000-0000-0000-000000000909")
    task = _scoring_task("00000000-0000-0000-0000-000000000909", [item])
    seed_scoring(mgr, task, [item])
    t, it = _to_review_pending(mgr, task, item)
    it2 = mgr.start_item_stage(task.task_id, item.item_id,
                               expected_task_revision=t.revision, expected_item_revision=it.item_revision)
    it3 = mgr.complete_scoring_item_stage(
        task.task_id, item.item_id,
        expected_task_revision=t.revision + 1, expected_item_revision=it2.item_revision,
        review_decision_id="00000000-0000-0000-0000-000000000c09")
    assert it3.current_stage == "export"
    assert it3.status == "pending"
    assert it3.review_decision_id == "00000000-0000-0000-0000-000000000c09"
    assert mgr.get_task(task.task_id).current_stage == "export"


def test_export_complete_to_item_completed(tmp_path):
    mgr = make_manager(tmp_path)
    item = _scoring_item("00000000-0000-0000-0000-000000000a0a", "00000000-0000-0000-0000-00000000090a")
    task = _scoring_task("00000000-0000-0000-0000-00000000090a", [item])
    seed_scoring(mgr, task, [item])
    t, it = _to_review_pending(mgr, task, item)
    it2 = mgr.start_item_stage(task.task_id, item.item_id,
                               expected_task_revision=t.revision, expected_item_revision=it.item_revision)
    t2 = mgr.get_task(task.task_id)
    it3 = mgr.complete_scoring_item_stage(
        task.task_id, item.item_id,
        expected_task_revision=t2.revision, expected_item_revision=it2.item_revision,
        review_decision_id="00000000-0000-0000-0000-000000000c0a")
    t3 = mgr.get_task(task.task_id)
    it4 = mgr.start_item_stage(task.task_id, item.item_id,
                               expected_task_revision=t3.revision, expected_item_revision=it3.item_revision)
    t4 = mgr.get_task(task.task_id)
    it5 = mgr.complete_scoring_item_stage(
        task.task_id, item.item_id,
        expected_task_revision=t4.revision, expected_item_revision=it4.item_revision,
        output_ref="outputs/out.json", output_sha256=SHA)
    assert it5.status == "completed"
    assert it5.output_ref == "outputs/out.json"
    assert it5.output_sha256 == SHA


def test_all_export_completed_task_finalizes(tmp_path):
    mgr = make_manager(tmp_path)
    item = _scoring_item("00000000-0000-0000-0000-000000000a0b", "00000000-0000-0000-0000-00000000090b")
    task = _scoring_task("00000000-0000-0000-0000-00000000090b", [item])
    seed_scoring(mgr, task, [item])
    # 推进到 completed
    t, it = _to_review_pending(mgr, task, item)
    it2 = mgr.start_item_stage(task.task_id, item.item_id,
                               expected_task_revision=t.revision, expected_item_revision=it.item_revision)
    t2 = mgr.get_task(task.task_id)
    it3 = mgr.complete_scoring_item_stage(
        task.task_id, item.item_id,
        expected_task_revision=t2.revision, expected_item_revision=it2.item_revision,
        review_decision_id="00000000-0000-0000-0000-000000000c0b")
    t3 = mgr.get_task(task.task_id)
    it4 = mgr.start_item_stage(task.task_id, item.item_id,
                               expected_task_revision=t3.revision, expected_item_revision=it3.item_revision)
    t4 = mgr.get_task(task.task_id)
    mgr.complete_scoring_item_stage(
        task.task_id, item.item_id,
        expected_task_revision=t4.revision, expected_item_revision=it4.item_revision,
        output_ref="outputs/out.json", output_sha256=SHA)
    t5 = mgr.get_task(task.task_id)
    final = mgr.finalize_task_if_settled(task.task_id, expected_revision=t5.revision)
    assert final.status == "completed"
    assert final.completed_at is not None
    assert final.current_stage is None


def test_task_completed_with_errors_when_failed_item(tmp_path):
    mgr = make_manager(tmp_path)
    item = _scoring_item("00000000-0000-0000-0000-000000000a0c", "00000000-0000-0000-0000-00000000090c")
    task = _scoring_task("00000000-0000-0000-0000-00000000090c", [item])
    seed_scoring(mgr, task, [item])
    t, it = _to_running(mgr, task, item)
    mgr.fail_item_non_retryable(task.task_id, item.item_id,
                                expected_task_revision=t.revision, expected_item_revision=it.item_revision,
                                error_code="PROVIDER_SERVER_ERROR")
    t2 = mgr.get_task(task.task_id)
    final = mgr.finalize_task_if_settled(task.task_id, expected_revision=t2.revision)
    assert final.status == "completed_with_errors"
    assert final.current_stage is None


# ---------------- 12-13. current_stage 聚合 ---------------- #


def test_scoring_current_stage_derivation(tmp_path):
    mgr = make_manager(tmp_path)
    item = _scoring_item("00000000-0000-0000-0000-000000000a0d", "00000000-0000-0000-0000-00000000090d")
    task = _scoring_task("00000000-0000-0000-0000-00000000090d", [item])
    seed_scoring(mgr, task, [item])
    t = mgr.start_task(task.task_id, expected_revision=1)
    assert t.current_stage == "score"
    # score 完成 -> review
    it = mgr.start_item_stage(task.task_id, item.item_id,
                              expected_task_revision=t.revision, expected_item_revision=item.item_revision)
    t2 = mgr.get_task(task.task_id)
    mgr.complete_scoring_item_stage(
        task.task_id, item.item_id,
        expected_task_revision=t2.revision, expected_item_revision=it.item_revision,
        attempt_id="00000000-0000-0000-0000-000000000b0d")
    t3 = mgr.get_task(task.task_id)
    assert t3.current_stage == "review"
    assert t3.pending_items == 1


# ---------------- 14. scoring item 不得进入 import/validate ---------------- #


def test_scoring_item_stage_restricted_to_score_review_export():
    with pytest.raises(Exception):
        _scoring_item("00000000-0000-0000-0000-000000000a0e", "00000000-0000-0000-0000-00000000090e",
                      stage="import")
    with pytest.raises(Exception):
        _scoring_item("00000000-0000-0000-0000-000000000a0f", "00000000-0000-0000-0000-00000000090f",
                      stage="validate")


# ---------------- 16. limited 准入 ---------------- #


def test_limited_scoring_item_can_start_score(tmp_path):
    mgr = make_manager(tmp_path)
    item = _scoring_item("00000000-0000-0000-0000-000000000a10", "00000000-0000-0000-0000-000000000910",
                         evidence_level="limited")
    task = _scoring_task("00000000-0000-0000-0000-000000000910", [item])
    seed_scoring(mgr, task, [item])
    t = mgr.start_task(task.task_id, expected_revision=1)
    it = mgr.start_item_stage(task.task_id, item.item_id,
                              expected_task_revision=t.revision, expected_item_revision=item.item_revision)
    assert it.status == "running"
    assert it.current_stage == "score"


# ---------------- 17-18. 人工复核 / 失败 ---------------- #


def test_manual_review_not_auto_advanced(tmp_path):
    mgr = make_manager(tmp_path)
    item = _scoring_item("00000000-0000-0000-0000-000000000a11", "00000000-0000-0000-0000-000000000911",
                         status="manual_review", stage="review")
    task = _scoring_task("00000000-0000-0000-0000-000000000911", [item])
    seed_scoring(mgr, task, [item])
    t = mgr.start_task(task.task_id, expected_revision=1)
    with pytest.raises(PipelineTaskError) as ei:
        mgr.start_item_stage(task.task_id, item.item_id,
                             expected_task_revision=t.revision, expected_item_revision=item.item_revision)
    assert ei.value.error_code == ERR_INVALID_STATE_TRANSITION
    # 不自动推进 export
    assert mgr.store.load_item(task.task_id, item.item_id).current_stage == "review"


def test_failure_does_not_auto_advance(tmp_path):
    mgr = make_manager(tmp_path)
    item = _scoring_item("00000000-0000-0000-0000-000000000a12", "00000000-0000-0000-0000-000000000912")
    task = _scoring_task("00000000-0000-0000-0000-000000000912", [item])
    seed_scoring(mgr, task, [item])
    t, it = _to_running(mgr, task, item)
    mgr.fail_item_retryable(task.task_id, item.item_id,
                            expected_task_revision=t.revision, expected_item_revision=it.item_revision,
                            error_code="PROVIDER_TIMEOUT")
    t2 = mgr.get_task(task.task_id)
    it2 = mgr.store.load_item(task.task_id, item.item_id)
    assert it2.status == "failed"
    assert it2.current_stage == "score"  # 不推进
    assert t2.current_stage == "score"
    # retryable failed 可显式重试
    it3 = mgr.start_item_stage(task.task_id, item.item_id,
                               expected_task_revision=t2.revision, expected_item_revision=it2.item_revision)
    assert it3.status == "running"
    assert it3.attempt_count == 2
    # non-retryable failed 拒绝重试
    mgr.fail_item_non_retryable(task.task_id, item.item_id,
                                expected_task_revision=t2.revision + 1, expected_item_revision=it3.item_revision,
                                error_code="PROVIDER_SCORE_OUT_OF_RANGE")
    t3 = mgr.get_task(task.task_id)
    it4 = mgr.store.load_item(task.task_id, item.item_id)
    with pytest.raises(PipelineTaskError) as ei:
        mgr.start_item_stage(task.task_id, item.item_id,
                             expected_task_revision=t3.revision, expected_item_revision=it4.item_revision)
    assert ei.value.error_code == ERR_INVALID_STATE_TRANSITION


# ---------------- 19. heartbeat / pause ---------------- #


def test_heartbeat_and_pause_resume_for_scoring(tmp_path):
    mgr = make_manager(tmp_path)
    item = _scoring_item("00000000-0000-0000-0000-000000000a13", "00000000-0000-0000-0000-000000000913")
    task = _scoring_task("00000000-0000-0000-0000-000000000913", [item])
    seed_scoring(mgr, task, [item])
    t = mgr.start_task(task.task_id, expected_revision=1)
    it = mgr.start_item_stage(task.task_id, item.item_id,
                              expected_task_revision=t.revision, expected_item_revision=item.item_revision)
    assert it.heartbeat_updated_at is not None
    # pause 保留 current_stage
    tp = mgr.pause_task(task.task_id, expected_revision=t.revision + 1)
    assert tp.status == "paused"
    assert tp.current_stage == "score"
    assert tp.paused_at is not None
    # paused 不得普通 start（resume 走既有恢复路径）
    with pytest.raises(PipelineTaskError):
        mgr.start_task(task.task_id, expected_revision=tp.revision)


# ---------------- 20. outcome_unknown ---------------- #


def test_outcome_unknown_not_auto_advanced(tmp_path):
    mgr = make_manager(tmp_path)
    item = _scoring_item(
        "00000000-0000-0000-0000-000000000a14", "00000000-0000-0000-0000-000000000914",
        status="failed", stage="score",
        retryable=True,
        last_error=PipelineError(
            error_code="PROVIDER_OUTCOME_UNKNOWN", message_key="PROVIDER_OUTCOME_UNKNOWN",
            retryable=True, stage="score"),
    )
    task = _scoring_task("00000000-0000-0000-0000-000000000914", [item])
    seed_scoring(mgr, task, [item])
    t = mgr.start_task(task.task_id, expected_revision=1)
    # outcome_unknown 不得自动重试（即使 retryable=True）
    with pytest.raises(PipelineTaskError) as ei:
        mgr.start_item_stage(task.task_id, item.item_id,
                             expected_task_revision=t.revision, expected_item_revision=item.item_revision)
    assert ei.value.error_code == ERR_OUTCOME_UNKNOWN_BLOCKED
    # 不得自动推进 review/export
    assert mgr.store.load_item(task.task_id, item.item_id).current_stage == "score"


# ---------------- 21-23. 保护 / CAS / 回滚 ---------------- #


def test_completed_item_result_protected(tmp_path):
    mgr = make_manager(tmp_path)
    item = _scoring_item("00000000-0000-0000-0000-000000000a15", "00000000-0000-0000-0000-000000000915")
    task = _scoring_task("00000000-0000-0000-0000-000000000915", [item])
    seed_scoring(mgr, task, [item])
    t, it = _to_review_pending(mgr, task, item)
    it2 = mgr.start_item_stage(task.task_id, item.item_id,
                               expected_task_revision=t.revision, expected_item_revision=it.item_revision)
    t2 = mgr.get_task(task.task_id)
    it3 = mgr.complete_scoring_item_stage(
        task.task_id, item.item_id,
        expected_task_revision=t2.revision, expected_item_revision=it2.item_revision,
        review_decision_id="00000000-0000-0000-0000-000000000c15")
    t3 = mgr.get_task(task.task_id)
    it4 = mgr.start_item_stage(task.task_id, item.item_id,
                               expected_task_revision=t3.revision, expected_item_revision=it3.item_revision)
    t4 = mgr.get_task(task.task_id)
    mgr.complete_scoring_item_stage(
        task.task_id, item.item_id,
        expected_task_revision=t4.revision, expected_item_revision=it4.item_revision,
        output_ref="outputs/out.json", output_sha256=SHA)
    t5 = mgr.get_task(task.task_id)
    it5 = mgr.store.load_item(task.task_id, item.item_id)
    # 不同输出 -> 成功结果保护
    with pytest.raises(PipelineTaskError) as ei:
        mgr.complete_scoring_item_stage(
            task.task_id, item.item_id,
            expected_task_revision=t5.revision, expected_item_revision=it5.item_revision,
            output_ref="outputs/other.json", output_sha256=SHA)
    assert ei.value.error_code == ERR_SUCCESS_RESULT_PROTECTED
    # 相同输出 -> 幂等
    again = mgr.complete_scoring_item_stage(
        task.task_id, item.item_id,
        expected_task_revision=t5.revision, expected_item_revision=it5.item_revision,
        output_ref="outputs/out.json", output_sha256=SHA)
    assert again.status == "completed"


def test_cas_conflict_zero_side_effect(tmp_path):
    from services.pipeline_task_store import ERR_REVISION_CONFLICT as STORE_ERR_REV

    mgr = make_manager(tmp_path)
    item = _scoring_item("00000000-0000-0000-0000-000000000a16", "00000000-0000-0000-0000-000000000916")
    task = _scoring_task("00000000-0000-0000-0000-000000000916", [item])
    seed_scoring(mgr, task, [item])
    t = mgr.start_task(task.task_id, expected_revision=1)
    # item revision 过期 -> manager 层冲突
    with pytest.raises(PipelineTaskError) as ei:
        mgr.start_item_stage(task.task_id, item.item_id,
                             expected_task_revision=t.revision, expected_item_revision=99)
    assert ei.value.error_code == ERR_REVISION_CONFLICT
    # task revision 过期 -> store 层冲突（零副作用）
    with pytest.raises(PipelineStoreError) as ei2:
        mgr.start_item_stage(task.task_id, item.item_id,
                             expected_task_revision=99, expected_item_revision=item.item_revision)
    assert ei2.value.error_code == STORE_ERR_REV
    # 零副作用：task/item 未变化
    t2 = mgr.get_task(task.task_id)
    it = mgr.store.load_item(task.task_id, item.item_id)
    assert t2.revision == t.revision
    assert it.status == "pending"


def test_transaction_write_failure_full_rollback(tmp_path, monkeypatch):
    mgr = make_manager(tmp_path)
    item = _scoring_item("00000000-0000-0000-0000-000000000a17", "00000000-0000-0000-0000-000000000917")
    task = _scoring_task("00000000-0000-0000-0000-000000000917", [item])
    seed_scoring(mgr, task, [item])
    import services.pipeline_task_manager as mod

    def _boom(*a, **k):
        raise PipelineStoreError(ERR_TRANSACTION_ERROR, "SIDECAR_TRANSACTION_ERROR", retryable=True)

    monkeypatch.setattr(mod, "run_task_transaction", _boom)
    with pytest.raises(PipelineStoreError):
        mgr.start_task(task.task_id, expected_revision=1)
    # 完整回滚：仍为 pending
    assert mgr.get_task(task.task_id).status == "pending"


# ---------------- 24. 旧 evidence JSON 兼容 ---------------- #


def test_old_evidence_json_compatible():
    """旧 evidence item JSON 无 review_decision_id 字段仍可读取。"""
    item = _scoring_item("00000000-0000-0000-0000-000000000a18", "00000000-0000-0000-0000-000000000918",
                         task_type="evidence_preparation_pipeline", stage="validate",
                         source_item_id=None)
    dumped = json.loads(item.model_dump_json())
    del dumped["review_decision_id"]
    del dumped["task_type"]
    del dumped["source_item_id"]
    again = PipelineItem(**dumped)
    assert again.review_decision_id is None
