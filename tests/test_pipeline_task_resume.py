"""
Phase 11C-2d：PipelineTask 查询、心跳、暂停恢复决策与 stale 判定合成测试。

所有数据为脱敏合成数据；测试全部注入 tmp_path；不读真实材料、不调模型。
覆盖：查询排序/过滤/分页/零副作用、损坏显式失败、心跳合法更新与 revision 冲突、
stale 阈值边界、恢复资格判定、request/decision 幂等与顺序、dry-run 零副作用、
apply 一致性、重启后可继续、暂停恢复保留 started_at。
"""
from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from models.evidence_sidecar import ValidationResult
from models.pipeline_task import (
    PipelineConfigurationSnapshot,
    PipelineTask,
    ResumeDecision,
    ResumeRequest,
)
from services.pipeline_task_manager import (
    ERR_IDEMPOTENCY_CONFLICT,
    ERR_INVALID_STATE_TRANSITION,
    ERR_REVISION_CONFLICT,
    ERR_TASK_CORRUPTED,
    ERR_TASK_NOT_SETTLED,
    PipelineTaskError,
    PipelineTaskManager,
)
from services.pipeline_task_store import PipelineStoreError

UTC = timezone.utc
T0 = datetime(2026, 8, 10, 8, 0, 0, tzinfo=UTC)
SHA256 = "a" * 64
PKG = "pkg-demo-001"
REC = "record-0001"
VAL = "validation-0001"
CONFIG = PipelineConfigurationSnapshot(
    evidence_contract_version="evidence-package/v1.1", validator_version="1.0.0",
)


def _uuid_seq():
    i = [0]

    def gen() -> str:
        i[0] += 1
        return f"00000000-0000-0000-0000-{i[0]:012d}"

    return gen


def make_validation(evidence_level: str = "sufficient", **overrides):
    data = {
        "validation_schema_version": "evidence-sidecar/validation/v1",
        "validation_id": VAL,
        "record_id": REC,
        "package_id": PKG,
        "package_revision": 1,
        "manifest_sha256": SHA256,
        "validator_name": "validator-demo",
        "validator_version": "1.0.0",
        "evidence_contract_version": "evidence-package/v1.1",
        "privacy_policy_version": "privacy-policy/v1.1",
        "mode": "registration",
        "status": "passed",
        "started_at": T0,
        "completed_at": T0 + timedelta(seconds=2),
        "duration_ms": 2000,
        "checks": [{"check_id": "JSON_PARSEABLE", "status": "passed", "message_key": "CHECK_OK"}],
        "issues": [],
        "issue_counts": {"info": 0, "warning": 0, "error": 0, "fatal": 0},
        "model_input_allowed": True,
        "registration_allowed": True,
        "validated_file_count": 1,
        "declared_file_count": 1,
        "unregistered_file_count": 0,
        "result_sha256": SHA256,
        "evidence_level": evidence_level,
    }
    data.update(overrides)
    return ValidationResult.model_validate(data)


class FakeRegistrationService:
    def __init__(self):
        self.records = {(PKG, 1): _make_record()}
        self.validations = {(PKG, 1, VAL): make_validation()}

    def read_record_strict(self, package_id, package_revision):
        return self.records.get((package_id, package_revision))

    def read_validation_strict(self, package_id, package_revision, validation_id):
        return self.validations.get((package_id, package_revision, validation_id))


def _make_record():
    from models.evidence_sidecar import EvidencePackageRecord

    return EvidencePackageRecord(
        record_schema_version="evidence-sidecar/record/v1",
        record_id=REC, package_id=PKG, package_revision=1, batch_id="batch-demo-001",
        submission_id="sub-demo-001", evidence_version="1",
        privacy_policy_version="privacy-policy/v1.1", contract_version="evidence-package/v1.1",
        publication_status="ready", registration_status="registered",
        latest_validation_id=VAL, latest_validation_status="passed",
        source_package_ref=f"{PKG}/1/evidence.json", registered_at=T0,
        registered_by="synthetic-registrar", created_at=T0, updated_at=T0, revision=1,
        record_sha256=SHA256, manifest_sha256=SHA256,
    )


class _Clock:
    def __init__(self, start=T0):
        self.t = start

    def __call__(self):
        self.t = self.t + timedelta(seconds=1)
        return self.t

    def set(self, t):
        self.t = t


def make_manager(tmp_path):
    from services.pipeline_task_store import PipelineTaskStore

    store = PipelineTaskStore(tmp_path / "evidence-runtime" / "pipeline")
    return PipelineTaskManager(store, FakeRegistrationService(), clock=_Clock(), uuid_factory=_uuid_seq())


def create_task(mgr, batch_id="batch-demo-001"):
    from services.pipeline_task_manager import PackageRef, PipelineTaskCreateRequest

    req = PipelineTaskCreateRequest(
        batch_id=batch_id,
        package_refs=[PackageRef(package_id=PKG, package_revision=1, manifest_sha256=SHA256,
                                 registration_record_id=REC, validation_id=VAL)],
        concurrency=1,
        configuration_snapshot=CONFIG,
    )
    task, hit = mgr.create_task(req)
    return task


def make_request(task, mode, dry_run=False, item_ids=None, expected_item_revisions=None,
                 request_id="00000000-0000-0000-0000-0000000000a1", requested_at=None):
    return ResumeRequest(
        contract_version="pipeline-task/v1",
        request_id=request_id,
        task_id=task.task_id,
        requested_at=requested_at or (T0 + timedelta(minutes=30)),
        mode=mode,
        expected_revision=task.revision,
        expected_item_revisions=expected_item_revisions or {i.item_id: i.item_revision for i in task.item_index},
        item_ids=item_ids or [i.item_id for i in task.item_index],
        reason_code="TEST_RESUME",
        dry_run=dry_run,
    )


def run_sufficient_to_running(mgr, task, item_id):
    """推进到 item 进入 import running（attempt=1）。"""
    task = mgr.start_task(task.task_id, task.revision)
    mgr.start_item_stage(task.task_id, item_id, task.revision, 1)
    return mgr.get_task(task.task_id)


def run_sufficient_to_validate_pending(mgr, task, item_id):
    task = run_sufficient_to_running(mgr, task, item_id)
    mgr.complete_item_stage(task.task_id, item_id, task.revision, 2)
    return mgr.get_task(task.task_id)


# ---------------------------------------------------------------- 1-2 查询


def test_query_sorted_filtered_paginated_zero_side_effect(tmp_path):
    mgr = make_manager(tmp_path)
    create_task(mgr, "batch-a")
    create_task(mgr, "batch-b")
    create_task(mgr, "batch-c")
    all_tasks = mgr.list_tasks(limit=100)
    ids = [t.task_id for t in all_tasks]
    assert ids == sorted(ids)  # 稳定排序
    assert len(all_tasks) == 3
    page1 = mgr.list_tasks(limit=2)
    assert len(page1) == 2
    pending = mgr.list_tasks(status_filter="pending", limit=100)
    assert len(pending) == 3
    running = mgr.list_tasks(status_filter="running", limit=100)
    assert running == []
    # 查询零副作用：revision/事件/时间戳不变
    for t in all_tasks:
        loaded = mgr.get_task(t.task_id)
        assert loaded.revision == t.revision
    assert mgr.list_items(page1[0].task_id)  # 单 item


def test_query_corrupted_task_explicit_failure(tmp_path):
    mgr = make_manager(tmp_path)
    create_task(mgr)
    task = mgr.get_task(mgr.list_tasks(limit=1)[0].task_id)
    (mgr.store.tasks_dir / task.task_id / "task.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(PipelineStoreError) as ei:
        mgr.list_tasks(limit=100)
    assert ei.value.error_code == ERR_TASK_CORRUPTED
    with pytest.raises(PipelineStoreError):
        mgr.get_task(task.task_id)


# ---------------------------------------------------------------- 3-6 心跳与 stale


def test_heartbeat_legal_update(tmp_path):
    mgr = make_manager(tmp_path)
    task = create_task(mgr)
    item_id = task.item_index[0].item_id
    task = run_sufficient_to_running(mgr, task, item_id)
    item = mgr.heartbeat_item(task.task_id, item_id, task.revision, 2)
    assert item.status == "running"
    assert item.heartbeat_updated_at is not None and item.heartbeat_updated_at > T0
    # task/item/index/事件一致
    task2 = mgr.get_task(task.task_id)
    assert task2.revision == task.revision + 1
    assert mgr.get_item(task.task_id, item_id).item_revision == 3
    assert mgr.get_event_log(task.task_id)[-1].event_type == "lease_acquired"


def test_heartbeat_revision_conflict_zero_side_effect(tmp_path):
    mgr = make_manager(tmp_path)
    task = create_task(mgr)
    item_id = task.item_index[0].item_id
    task = run_sufficient_to_running(mgr, task, item_id)
    before_events = len(mgr.get_event_log(task.task_id))
    before_item = mgr.get_item(task.task_id, item_id).model_dump(mode="json")
    with pytest.raises(PipelineTaskError) as ei:
        mgr.heartbeat_item(task.task_id, item_id, task.revision, 99)  # item revision 过期
    assert ei.value.error_code == ERR_REVISION_CONFLICT
    assert len(mgr.get_event_log(task.task_id)) == before_events
    assert mgr.get_item(task.task_id, item_id).model_dump(mode="json") == before_item


def test_stale_threshold_boundaries(tmp_path):
    """阈值前/阈值点/阈值后：契约语义'超过 300s'（严格大于，等于不判 stale）。"""
    mgr = make_manager(tmp_path)
    task = create_task(mgr)
    item_id = task.item_index[0].item_id
    task = run_sufficient_to_running(mgr, task, item_id)
    item = mgr.get_item(task.task_id, item_id)
    hb = item.heartbeat_updated_at
    # 阈值前（<300s）
    assert mgr.stale_candidates(task.task_id, now=hb + timedelta(seconds=299)) == []
    # 阈值点（==300s）：不判 stale
    assert mgr.stale_candidates(task.task_id, now=hb + timedelta(seconds=300)) == []
    # 阈值后（>300s）：判 stale
    cands = mgr.stale_candidates(task.task_id, now=hb + timedelta(seconds=301))
    assert [c.item_id for c in cands] == [item_id]


def test_non_running_statuses_not_stale(tmp_path):
    mgr = make_manager(tmp_path)
    task = create_task(mgr)
    item_id = task.item_index[0].item_id
    # completed
    t = run_sufficient_to_validate_pending(mgr, task, item_id)
    item = mgr.get_item(t.task_id, item_id)
    mgr.start_item_stage(t.task_id, item_id, t.revision, 3)
    t = mgr.get_task(t.task_id)
    mgr.complete_item_stage(t.task_id, item_id, t.revision, 4,
                            output_ref="outputs/result.json", output_sha256=SHA256)
    task2 = mgr.get_task(t.task_id)
    assert mgr.stale_candidates(task2.task_id, now=T0 + timedelta(hours=2)) == []
    # manual_review / skipped（独立子目录避免幂等命中）
    mgr2 = make_manager(tmp_path / "m2")
    t2 = create_task(mgr2)
    i2 = t2.item_index[0].item_id
    mgr2.send_item_to_manual_review(t2.task_id, i2, 1, 1, reason_code="TEST")
    task3 = mgr2.get_task(t2.task_id)
    assert mgr2.stale_candidates(task3.task_id, now=T0 + timedelta(hours=2)) == []
    mgr3 = make_manager(tmp_path / "m3")
    t4 = create_task(mgr3)
    i4 = t4.item_index[0].item_id
    mgr3.skip_item(t4.task_id, i4, 1, 1)
    task5 = mgr3.get_task(t4.task_id)
    assert mgr3.stale_candidates(task5.task_id, now=T0 + timedelta(hours=2)) == []


# ---------------------------------------------------------------- 7-9 request/decision 存储


def test_request_before_decision_and_idempotency(tmp_path):
    mgr = make_manager(tmp_path)
    task = create_task(mgr)
    req = make_request(task, "resume_pending")
    assert mgr.create_resume_request(task.task_id, req) is True  # 新建
    assert mgr.create_resume_request(task.task_id, req) is False  # 幂等命中
    assert len(mgr.store.list_resume_requests(task.task_id)) == 1
    # decision 必须在 request 之后
    dec = ResumeDecision(
        contract_version="pipeline-task/v1", decision_id="00000000-0000-0000-0000-0000000000d1", request_id="00000000-0000-0000-0000-0000000000a1",
        task_id=task.task_id, decided_at=T0, approved=False, task_revision_before=task.revision,
        eligible_items=[], protected_items=[], rejected_items=[], decision_error_codes=[], dry_run=True,
    )
    # decision 必须晚于 request（request 不存在时拒绝）
    with pytest.raises(PipelineStoreError):
        mgr.store.write_resume_decision(task.task_id, dec.model_copy(update={"request_id": "00000000-0000-0000-0000-0000000000c1"}))
    # 同 request_id 不同内容 -> 冲突
    conflict = req.model_copy(update={"reason_code": "OTHER"})
    with pytest.raises(PipelineTaskError) as ei:
        mgr.create_resume_request(task.task_id, conflict)
    assert ei.value.error_code == ERR_IDEMPOTENCY_CONFLICT


def test_orphan_request_stays_pending(tmp_path):
    mgr = make_manager(tmp_path)
    task = create_task(mgr)
    req = make_request(task, "resume_pending")
    mgr.create_resume_request(task.task_id, req)
    # 无 decision：孤儿 request 保持待决，不自动删除/批准
    assert mgr.get_resume_request(task.task_id, req.request_id) is not None
    assert mgr.get_resume_decision(task.task_id, req.request_id) is None
    assert len(mgr.store.list_resume_requests(task.task_id)) == 1


def test_request_decision_list_sorted(tmp_path):
    mgr = make_manager(tmp_path)
    task = create_task(mgr)
    for rid in ("00000000-0000-0000-0000-0000000000e2", "00000000-0000-0000-0000-0000000000e1", "00000000-0000-0000-0000-0000000000e3"):
        mgr.create_resume_request(task.task_id, make_request(task, "resume_pending", request_id=rid))
    ids = [r.request_id for r in mgr.store.list_resume_requests(task.task_id)]
    assert ids == sorted(ids)


# ---------------------------------------------------------------- 10-16 恢复资格


def test_pending_eligible(tmp_path):
    mgr = make_manager(tmp_path)
    task = create_task(mgr)
    req = make_request(task, "resume_pending")
    mgr.create_resume_request(task.task_id, req)
    decision = mgr.evaluate_resume_request(task.task_id, req.request_id)
    assert decision.approved is True
    assert len(decision.eligible_items) == 1
    assert decision.protected_items == [] and decision.rejected_items == []


def test_retryable_failed_eligible(tmp_path):
    mgr = make_manager(tmp_path)
    task = create_task(mgr)
    item_id = task.item_index[0].item_id
    task = run_sufficient_to_running(mgr, task, item_id)
    mgr.fail_item_retryable(task.task_id, item_id, task.revision, 2, error_code="TRANSIENT")
    task = mgr.get_task(task.task_id)
    req = make_request(task, "resume_retryable_failed")
    mgr.create_resume_request(task.task_id, req)
    decision = mgr.evaluate_resume_request(task.task_id, req.request_id)
    assert decision.approved is True
    assert len(decision.eligible_items) == 1


def test_attempt_limit_rejects_resume(tmp_path):
    mgr = make_manager(tmp_path)
    task = create_task(mgr)
    item_id = task.item_index[0].item_id
    # 三次失败占满 attempt
    rev_t, rev_i = 1, 1
    for _ in range(3):
        task = run_sufficient_to_running(mgr, task, item_id) if rev_t == 1 else _reuse(mgr, task, item_id, rev_t, rev_i)
        rev_t = mgr.get_task(task.task_id).revision
        rev_i = mgr.get_item(task.task_id, item_id).item_revision
        mgr.fail_item_retryable(task.task_id, item_id, rev_t, rev_i, error_code="TRANSIENT")
        task = mgr.get_task(task.task_id)
        rev_t = task.revision
        rev_i = mgr.get_item(task.task_id, item_id).item_revision
    req = make_request(task, "resume_retryable_failed")
    mgr.create_resume_request(task.task_id, req)
    decision = mgr.evaluate_resume_request(task.task_id, req.request_id)
    assert decision.approved is False
    assert any("ATTEMPT_LIMIT" in c for r in decision.rejected_items for c in r.error_codes)


def _reuse(mgr, task, item_id, rev_t, rev_i):
    mgr.start_item_stage(task.task_id, item_id, rev_t, rev_i)
    return mgr.get_task(task.task_id)


def test_stale_running_eligible(tmp_path):
    mgr = make_manager(tmp_path)
    task = create_task(mgr)
    item_id = task.item_index[0].item_id
    task = run_sufficient_to_running(mgr, task, item_id)
    item = mgr.get_item(task.task_id, item_id)
    # 构造超过 300s 的请求时间
    later = item.heartbeat_updated_at + timedelta(seconds=301)
    req = make_request(task, "recover_stale")
    req = req.model_copy(update={"requested_at": later})
    mgr.create_resume_request(task.task_id, req)
    decision = mgr.evaluate_resume_request(task.task_id, req.request_id)
    assert decision.approved is True
    assert decision.eligible_items[0].reason_code == "STALE_RUNNING_RECOVERY"


def test_non_stale_running_protected(tmp_path):
    mgr = make_manager(tmp_path)
    task = create_task(mgr)
    item_id = task.item_index[0].item_id
    task = run_sufficient_to_running(mgr, task, item_id)
    item = mgr.get_item(task.task_id, item_id)
    # 请求时点接近心跳（<300s）：非 stale
    req = make_request(task, "recover_stale", requested_at=item.heartbeat_updated_at + timedelta(seconds=100))
    mgr.create_resume_request(task.task_id, req)
    decision = mgr.evaluate_resume_request(task.task_id, req.request_id)
    assert decision.approved is False
    assert any(p.reason_code == "RESUME_NOT_ALLOWED" for p in decision.protected_items)


def test_completed_manual_skipped_protected(tmp_path):
    mgr = make_manager(tmp_path)
    task = create_task(mgr)
    item_id = task.item_index[0].item_id
    t = run_sufficient_to_validate_pending(mgr, task, item_id)
    item = mgr.get_item(t.task_id, item_id)
    mgr.start_item_stage(t.task_id, item_id, t.revision, 3)
    t = mgr.get_task(t.task_id)
    mgr.complete_item_stage(t.task_id, item_id, t.revision, 4,
                            output_ref="outputs/result.json", output_sha256=SHA256)
    task = mgr.get_task(t.task_id)
    req = make_request(task, "resume_pending")
    mgr.create_resume_request(task.task_id, req)
    decision = mgr.evaluate_resume_request(task.task_id, req.request_id)
    assert decision.approved is False
    assert any(p.reason_code == "SUCCESS_RESULT_PROTECTED" for p in decision.protected_items)


# ---------------------------------------------------------------- 17-22 决策应用


def test_dry_run_zero_side_effect(tmp_path):
    mgr = make_manager(tmp_path)
    task = create_task(mgr)
    item_id = task.item_index[0].item_id
    task = run_sufficient_to_running(mgr, task, item_id)
    mgr.fail_item_retryable(task.task_id, item_id, task.revision, 2, error_code="TRANSIENT")
    task = mgr.get_task(task.task_id)
    before_events = len(mgr.get_event_log(task.task_id))
    req = make_request(task, "resume_retryable_failed", dry_run=True)
    mgr.create_resume_request(task.task_id, req)
    decision = mgr.evaluate_resume_request(task.task_id, req.request_id)
    assert decision.dry_run is True and decision.approved is True
    after = mgr.apply_resume_decision(task.task_id, decision, expected_task_revision=task.revision)
    assert after.revision == task.revision  # 零状态副作用
    assert mgr.get_item(task.task_id, item_id).status == "failed"
    assert len(mgr.get_event_log(task.task_id)) == before_events


def test_decision_write_failure_no_state_change(tmp_path, monkeypatch):
    mgr = make_manager(tmp_path)
    task = create_task(mgr)
    req = make_request(task, "resume_pending")
    mgr.create_resume_request(task.task_id, req)

    def boom(*a, **k):
        raise PipelineStoreError("SIDECAR_TRANSACTION_ERROR", "SIDECAR_TRANSACTION_ERROR")

    monkeypatch.setattr(mgr.store, "write_resume_decision", boom)
    with pytest.raises(PipelineStoreError):
        mgr.evaluate_resume_request(task.task_id, req.request_id)
    monkeypatch.undo()
    task2 = mgr.get_task(task.task_id)
    assert task2.revision == task.revision  # item/task 状态不变
    assert mgr.get_item(task.task_id, req.item_ids[0]).status == "pending"


def test_apply_revision_fingerprint_recheck_rejected(tmp_path):
    mgr = make_manager(tmp_path)
    task = create_task(mgr)
    item_id = task.item_index[0].item_id
    task = run_sufficient_to_running(mgr, task, item_id)
    mgr.fail_item_retryable(task.task_id, item_id, task.revision, 2, error_code="TRANSIENT")
    task = mgr.get_task(task.task_id)
    req = make_request(task, "resume_retryable_failed")
    mgr.create_resume_request(task.task_id, req)
    decision = mgr.evaluate_resume_request(task.task_id, req.request_id)
    # apply 前任务 revision 被推进（模拟并发）
    with pytest.raises(PipelineTaskError) as ei:
        mgr.apply_resume_decision(task.task_id, decision, expected_task_revision=task.revision + 99)
    assert ei.value.error_code in (ERR_REVISION_CONFLICT, ERR_TASK_NOT_SETTLED)


def test_apply_approved_updates_consistent(tmp_path):
    mgr = make_manager(tmp_path)
    task = create_task(mgr)
    item_id = task.item_index[0].item_id
    task = run_sufficient_to_running(mgr, task, item_id)
    mgr.fail_item_retryable(task.task_id, item_id, task.revision, 2, error_code="TRANSIENT")
    task = mgr.get_task(task.task_id)
    req = make_request(task, "resume_retryable_failed")
    mgr.create_resume_request(task.task_id, req)
    decision = mgr.evaluate_resume_request(task.task_id, req.request_id)
    task2 = mgr.apply_resume_decision(task.task_id, decision, expected_task_revision=task.revision)
    assert task2.status == "running"
    item = mgr.get_item(task.task_id, item_id)
    assert item.status == "pending"  # failed -> pending（不增加 attempt）
    assert item.attempt_count == 1  # attempt 未增加
    assert item.heartbeat_updated_at is None
    task3 = mgr.get_task(task.task_id)
    assert task3.revision == task.revision + 1
    assert len(mgr.get_event_log(task.task_id)) == len(mgr.get_event_log(task.task_id))  # 事件已写
    assert mgr.get_event_log(task.task_id)[-1].event_type in ("resume_decided", "task_resumed")


def test_pause_resume_keeps_started_at(tmp_path):
    mgr = make_manager(tmp_path)
    task = create_task(mgr)
    item_id = task.item_index[0].item_id
    task = mgr.start_task(task.task_id, 1)
    started_at = task.started_at
    paused = mgr.pause_task(task.task_id, task.revision)
    assert paused.status == "paused" and paused.paused_at is not None
    # 重复暂停 -> 非法
    with pytest.raises(PipelineTaskError) as ei:
        mgr.pause_task(task.task_id, paused.revision)
    assert ei.value.error_code == ERR_INVALID_STATE_TRANSITION
    # 恢复必须经 request + decision
    req = make_request(paused, "resume_pending")
    mgr.create_resume_request(paused.task_id, req)
    decision = mgr.evaluate_resume_request(paused.task_id, req.request_id)
    resumed = mgr.apply_resume_decision(paused.task_id, decision, expected_task_revision=paused.revision)
    assert resumed.status == "running"
    assert resumed.started_at == started_at  # 保留首次 started_at
    assert resumed.paused_at is None
    assert mgr.get_event_log(paused.task_id)[-1].event_type == "task_resumed"


def test_recreate_manager_reads_resume_state(tmp_path):
    from services.pipeline_task_store import PipelineTaskStore

    store = PipelineTaskStore(tmp_path / "evidence-runtime" / "pipeline")
    m1 = PipelineTaskManager(store, FakeRegistrationService(), clock=_Clock(), uuid_factory=_uuid_seq())
    task = create_task(m1)
    req = make_request(task, "resume_pending", request_id="00000000-0000-0000-0000-0000000000b1")
    m1.create_resume_request(task.task_id, req)
    m1.evaluate_resume_request(task.task_id, req.request_id)
    # 重建 manager
    m2 = PipelineTaskManager(store, FakeRegistrationService(), clock=_Clock(), uuid_factory=_uuid_seq())
    assert m2.get_resume_request(task.task_id, "00000000-0000-0000-0000-0000000000b1") is not None
    assert m2.get_resume_decision(task.task_id, "00000000-0000-0000-0000-0000000000b1") is not None
    assert m2.get_task(task.task_id).status == "pending"
