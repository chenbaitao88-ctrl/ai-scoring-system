"""
Phase 11C-2b（fix-1）：PipelineTask 文件存储基础设施合成测试（事务一致性加固）。

所有测试数据均为脱敏、虚构的合成数据，不包含任何真实学生材料。
测试全部注入 tmp_path，禁止写真实 data 目录。

覆盖（fix-1）：
- 多快照事务崩溃窗口（prepare 后发布前/发布中途/元数据失败 -> 无部分新快照）。
- last_event_sequence 与事件追加同步（成功事务/连续事务/未同步拒绝/跳号回退拒绝）。
- 事件追加后禁止回滚（commit 状态写入失败/清理失败 -> 快照不回滚、recover 幂等完成）。
- 恢复必须核验事件内容（同 sequence 不同 event_id/同 id 不同内容/快照不一致/new 缺失 -> corrupted）。
- CAS 不存在语义四组正反例。
- 只读方法不创建任务目录。
- append_event 受锁保护（并发同 sequence 仅一个成功）。
- 事务引用路径收紧。
- 文件锁所有权 token 保护。
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from models.pipeline_task import (
    PipelineEvent,
    PipelineItem,
    PipelineTask,
    ResumeDecision,
    ResumeRequest,
    canonical_json_bytes,
    sha256_canonical,
)
from services.pipeline_task_store import (
    ERR_EVENT_IDEMPOTENCY_CONFLICT,
    ERR_EVENT_SEQUENCE_CONFLICT,
    ERR_LOCK_CONFLICT,
    ERR_REVISION_CONFLICT,
    ERR_TASK_CORRUPTED,
    ERR_TRANSACTION_ERROR,
    PipelineStoreError,
    PipelineTaskStore,
    _append_ndjson_bytes,
    _atomic_write_bytes,
    run_task_transaction,
)

UTC = timezone.utc
T0 = datetime(2026, 8, 7, 8, 0, 0, tzinfo=UTC)
SHA256 = "a" * 64
FP = "f" * 64
IDEM = "b" * 64
CONFIG_FP = "c" * 64
TASK_ID = str(uuid.uuid4())
ITEM_ID = str(uuid.uuid4())
ITEM_ID2 = str(uuid.uuid4())
EVENT_ID = str(uuid.uuid4())


# ---------------------------------------------------------------- fixtures


def config_snapshot():
    return {"evidence_contract_version": "evidence-package/v1.1", "validator_version": "1.0.0"}


def stage_summaries():
    return [
        {"stage": "import", "status": "pending", "depends_on": [], "started_at": None, "completed_at": None,
         "total_items": 1, "pending_items": 1, "running_items": 0, "completed_items": 0,
         "failed_items": 0, "skipped_items": 0, "manual_review_items": 0, "blocking_error_codes": [], "revision": 1},
        {"stage": "validate", "status": "not_started", "depends_on": ["import"], "started_at": None, "completed_at": None,
         "total_items": 0, "pending_items": 0, "running_items": 0, "completed_items": 0,
         "failed_items": 0, "skipped_items": 0, "manual_review_items": 0, "blocking_error_codes": [], "revision": 1},
        {"stage": "score", "status": "not_started", "depends_on": ["validate"], "started_at": None, "completed_at": None,
         "total_items": 0, "pending_items": 0, "running_items": 0, "completed_items": 0,
         "failed_items": 0, "skipped_items": 0, "manual_review_items": 0, "blocking_error_codes": [], "revision": 1},
        {"stage": "review", "status": "not_started", "depends_on": ["validate", "score"], "started_at": None, "completed_at": None,
         "total_items": 0, "pending_items": 0, "running_items": 0, "completed_items": 0,
         "failed_items": 0, "skipped_items": 0, "manual_review_items": 0, "blocking_error_codes": [], "revision": 1},
        {"stage": "export", "status": "not_started", "depends_on": ["review"], "started_at": None, "completed_at": None,
         "total_items": 0, "pending_items": 0, "running_items": 0, "completed_items": 0,
         "failed_items": 0, "skipped_items": 0, "manual_review_items": 0, "blocking_error_codes": [], "revision": 1},
    ]


def make_task(revision: int = 1, last_event_sequence: int = 0, **overrides):
    snap = config_snapshot()
    data = {
        "contract_version": "pipeline-task/v1",
        "task_type": "evidence_preparation_pipeline",
        "execution_scope": ["import", "validate"],
        "task_id": TASK_ID,
        "batch_id": "batch-demo-001",
        "status": "pending",
        "current_stage": "import",
        "created_at": T0,
        "started_at": None,
        "updated_at": T0,
        "paused_at": None,
        "completed_at": None,
        "total_items": 1,
        "pending_items": 1,
        "running_items": 0,
        "completed_items": 0,
        "failed_items": 0,
        "skipped_items": 0,
        "manual_review_items": 0,
        "concurrency": 1,
        "configuration_snapshot": snap,
        "configuration_fingerprint": sha256_canonical(snap),
        "item_index": [
            {"item_id": ITEM_ID, "package_id": "pkg-demo-001", "package_revision": 1,
             "status": "pending", "current_stage": "import", "input_fingerprint": FP, "item_revision": 1, "evidence_level": "sufficient"}
        ],
        "stage_summaries": stage_summaries(),
        "error_summary": {"count": 0, "codes": []},
        "last_event_sequence": last_event_sequence,
        "revision": revision,
        "idempotency_key": IDEM,
        "idempotency_payload_sha256": SHA256,
    }
    data.update(overrides)
    return PipelineTask.model_validate(data)


def make_item(revision: int = 1, item_id: str = ITEM_ID, **overrides):
    data = {
        "contract_version": "pipeline-item/v1",
        "item_id": item_id,
        "task_id": TASK_ID,
        "package_id": "pkg-demo-001",
        "package_revision": 1,
        "manifest_sha256": SHA256,
        "registration_record_id": "record-0001",
        "validation_id": "validation-0001",
        "input_fingerprint": FP,
        "idempotency_key": IDEM,
        "status": "pending",
        "current_stage": "import",
        "attempt_count": 0,
        "max_attempts": 3,
        "heartbeat_interval_seconds": 60,
        "stale_after_seconds": 300,
        "heartbeat_updated_at": None,
        "created_at": T0,
        "updated_at": T0,
        "retryable": False,
        "last_error": None,
        "output_ref": None,
        "output_sha256": None,
        "item_revision": revision,
        "evidence_level": "sufficient",
    }
    data.update(overrides)
    return PipelineItem.model_validate(data)


def make_event(sequence: int = 1, event_id: str = EVENT_ID, **overrides):
    data = {
        "contract_version": "pipeline-task/v1",
        "event_id": event_id,
        "task_id": TASK_ID,
        "sequence": sequence,
        "event_type": "task_created",
        "occurred_at": T0,
        "stage": None,
        "item_id": None,
        "attempt_id": None,
        "revision_before": 0,
        "revision_after": 1,
        "reason_code": None,
        "metadata": {},
    }
    data.update(overrides)
    return PipelineEvent.model_validate(data)


@pytest.fixture
def store(tmp_path):
    s = PipelineTaskStore(tmp_path / "evidence-runtime" / "pipeline")
    return s


def T1():
    return T0 + timedelta(minutes=1)


def new_task_v2(sequence: int = 1, revision: int = 2, include_item: bool = True):
    """事务更新用新 task：last_event_sequence 与事件 sequence 同步；item_index 默认含 ITEM_ID。"""
    item_index = [
        {"item_id": ITEM_ID, "package_id": "pkg-demo-001", "package_revision": 1,
         "status": "pending", "current_stage": "import", "input_fingerprint": FP, "item_revision": 1, "evidence_level": "sufficient"}
    ] if include_item else []
    return make_task(revision=revision, last_event_sequence=sequence,
                     updated_at=T1(), item_index=item_index,
                     total_items=len(item_index), pending_items=len(item_index))


# ---------------------------------------------------------------- 基础与 CAS 语义


def test_init_independent_dirs(store):
    assert store.tasks_dir.is_dir() and store.backups_dir.is_dir()


def test_task_item_atomic_write_and_read(store):
    store.write_task_snapshot(TASK_ID, make_task(), expected_revision=None)
    assert store.load_task(TASK_ID).revision == 1
    store.write_item_snapshot(TASK_ID, make_item(), expected_revision=None)
    assert store.load_item(TASK_ID, ITEM_ID).item_revision == 1


def test_missing_vs_corrupted_distinguished(store):
    assert store.load_task(TASK_ID) is None
    store.write_task_snapshot(TASK_ID, make_task(), expected_revision=None)
    (store.tasks_dir / TASK_ID / "task.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(PipelineStoreError) as ei:
        store.load_task(TASK_ID)
    assert ei.value.error_code == ERR_TASK_CORRUPTED


def test_path_escape_rejected(store):
    for bad in ("../escape", "张三", "X:/Profiles/demo", "task/0001"):
        with pytest.raises(PipelineStoreError):
            store.load_task(bad)


def test_cas_four_semantics_task(store):
    # None + 不存在：通过
    store.write_task_snapshot(TASK_ID, make_task(revision=1), expected_revision=None)
    # None + 已存在：冲突
    with pytest.raises(PipelineStoreError) as ei:
        store.write_task_snapshot(TASK_ID, make_task(revision=2), expected_revision=None)
    assert ei.value.error_code == ERR_REVISION_CONFLICT
    # N + 存在且相等：通过
    store.write_task_snapshot(TASK_ID, make_task(revision=2), expected_revision=1)
    assert store.load_task(TASK_ID).revision == 2
    # N + 不存在或不相等：冲突
    with pytest.raises(PipelineStoreError) as ei:
        store.write_task_snapshot(TASK_ID, make_task(revision=3), expected_revision=5)
    assert ei.value.error_code == ERR_REVISION_CONFLICT
    other = str(uuid.uuid4())
    with pytest.raises(PipelineStoreError) as ei:
        store.write_task_snapshot(other, make_task(revision=1, task_id=other), expected_revision=1)
    assert ei.value.error_code == ERR_REVISION_CONFLICT


def test_cas_four_semantics_item(store):
    store.write_item_snapshot(TASK_ID, make_item(revision=1), expected_revision=None)
    with pytest.raises(PipelineStoreError) as ei:
        store.write_item_snapshot(TASK_ID, make_item(revision=2), expected_revision=None)
    assert ei.value.error_code == ERR_REVISION_CONFLICT
    store.write_item_snapshot(TASK_ID, make_item(revision=2), expected_revision=1)
    with pytest.raises(PipelineStoreError) as ei:
        store.write_item_snapshot(TASK_ID, make_item(revision=3), expected_revision=9)
    assert ei.value.error_code == ERR_REVISION_CONFLICT
    with pytest.raises(PipelineStoreError) as ei:
        store.write_item_snapshot(TASK_ID, make_item(revision=3, item_id=ITEM_ID2), expected_revision=1)
    assert ei.value.error_code == ERR_REVISION_CONFLICT


def test_file_lock_conflict(store):
    lock_path = store._lock_path(TASK_ID)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("other-owner-token", encoding="utf-8")
    with pytest.raises(PipelineStoreError) as ei:
        with store._lock_context(TASK_ID):
            pass
    assert ei.value.error_code == ERR_LOCK_CONFLICT


def test_lock_ownership_token_protection(store):
    """release 时 token 与本实例不一致不得删除其他调用持有的锁。"""
    lock_path = store._lock_path(TASK_ID)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("someone-elses-token", encoding="utf-8")
    from services.pipeline_task_store import _TaskFileLock

    fl = _TaskFileLock(lock_path)
    fl._acquired = True  # 模拟本实例声称持有但锁被他人改写
    with pytest.raises(PipelineStoreError):
        fl.release()
    assert lock_path.exists()  # 不删除他人锁


# ---------------------------------------------------------------- 只读不创建目录


def test_readonly_does_not_create_task_dir(store):
    store.load_task(TASK_ID)
    store.load_item(TASK_ID, ITEM_ID)
    store.load_event_log(TASK_ID)
    store.load_resume_request(TASK_ID, str(uuid.uuid4()))
    store.load_resume_decision(TASK_ID, str(uuid.uuid4()))
    assert not (store.tasks_dir / TASK_ID).exists()  # 查询不创建目录


# ---------------------------------------------------------------- 成功事务与 last_event_sequence


def test_successful_joint_transaction_syncs_last_event_sequence(store):
    ev = make_event(sequence=1)
    run_task_transaction(store, TASK_ID, ev, new_task=new_task_v2(sequence=1),
                         new_items=[make_item()], expected_task_revision=None, expected_item_revisions={})
    task = store.load_task(TASK_ID)
    assert task.revision == 2 and task.last_event_sequence == 1  # 与事件同步
    assert store.load_item(TASK_ID, ITEM_ID) is not None
    events = store.load_event_log(TASK_ID)
    assert len(events) == 1 and events[0].sequence == 1
    assert not list(store._transactions_dir(TASK_ID).iterdir())


def test_two_consecutive_transactions_sequence_2(store):
    run_task_transaction(store, TASK_ID, make_event(sequence=1), new_task=new_task_v2(sequence=1),
                         new_items=[make_item()], expected_task_revision=None, expected_item_revisions={})
    ev2 = make_event(sequence=2, event_id=str(uuid.uuid4()))
    run_task_transaction(store, TASK_ID, ev2, new_task=new_task_v2(sequence=2, revision=3),
                         expected_task_revision=2, expected_item_revisions={})
    task = store.load_task(TASK_ID)
    assert task.last_event_sequence == 2 and task.revision == 3
    assert len(store.load_event_log(TASK_ID)) == 2


def test_new_task_sequence_unsynced_rejected_zero_side_effect(store):
    """新 task 快照 last_event_sequence 未与事件 sequence 同步 -> 拒绝且零副作用。"""
    before = (store.tasks_dir / TASK_ID / "task.json").read_bytes() if (store.tasks_dir / TASK_ID / "task.json").exists() else None
    ev = make_event(sequence=1)
    with pytest.raises(PipelineStoreError):
        run_task_transaction(store, TASK_ID, ev, new_task=make_task(revision=1, last_event_sequence=0),  # 未同步
                             expected_task_revision=None, expected_item_revisions={})
    if before is None:
        assert not (store.tasks_dir / TASK_ID / "task.json").exists()
    else:
        assert (store.tasks_dir / TASK_ID / "task.json").read_bytes() == before
    assert store.load_event_log(TASK_ID) == []


def test_event_sequence_skip_and_regress_rejected(store):
    """跳号/回退号拒绝（事务路径；事件只能通过含 new_task 的联合事务追加）。"""
    run_task_transaction(store, TASK_ID, make_event(sequence=1), new_task=new_task_v2(sequence=1),
                         new_items=[make_item()], expected_task_revision=None, expected_item_revisions={})
    # 跳号（3 != 1+1）
    with pytest.raises(PipelineStoreError) as ei:
        run_task_transaction(store, TASK_ID, make_event(sequence=3, event_id=str(uuid.uuid4())),
                             new_task=new_task_v2(sequence=3, revision=3), expected_task_revision=2,
                             expected_item_revisions={})
    assert ei.value.error_code == ERR_EVENT_SEQUENCE_CONFLICT
    # 回退号（1 已被消费）
    with pytest.raises(PipelineStoreError) as ei:
        run_task_transaction(store, TASK_ID, make_event(sequence=1, event_id=str(uuid.uuid4())),
                             new_task=new_task_v2(sequence=1, revision=3), expected_task_revision=2,
                             expected_item_revisions={})
    assert ei.value.error_code == ERR_EVENT_SEQUENCE_CONFLICT
    assert len(store.load_event_log(TASK_ID)) == 1


# ---------------------------------------------------------------- 多快照事务崩溃窗口


def test_crash_before_publish_no_partial_snapshots(store):
    """old/new 已准备完成、正式发布前崩溃 -> recover 后无部分新快照。"""
    store.write_task_snapshot(TASK_ID, make_task(revision=1), expected_revision=None)
    before = (store.tasks_dir / TASK_ID / "task.json").read_bytes()
    with store._lock_context(TASK_ID):
        txn_id = store.begin_transaction(TASK_ID, make_event(sequence=1), None, {})
        store.prepare_snapshots(TASK_ID, txn_id, task_snapshot=new_task_v2(sequence=1),
                                item_snapshots=[make_item(revision=2)])
        # 未 publish 即"崩溃"
    store.recover_incomplete_transactions(TASK_ID)
    assert (store.tasks_dir / TASK_ID / "task.json").read_bytes() == before
    assert not (store.tasks_dir / TASK_ID / "items" / f"{ITEM_ID}.json").exists()  # item 未发布
    assert not list(store._transactions_dir(TASK_ID).iterdir())


def test_crash_mid_publish_rolls_back_partial(store, monkeypatch):
    """发布 task 后、第一个 item 前崩溃 -> recover 回滚 old，无部分新快照。"""
    store.write_task_snapshot(TASK_ID, make_task(revision=1), expected_revision=None)
    before = (store.tasks_dir / TASK_ID / "task.json").read_bytes()
    txn_id = None
    with store._lock_context(TASK_ID):
        txn_id = store.begin_transaction(TASK_ID, make_event(sequence=1), None, {})
        store.prepare_snapshots(TASK_ID, txn_id, task_snapshot=new_task_v2(sequence=1),
                                item_snapshots=[make_item(revision=2)])
    # 模拟发布中断：只发布 task.json（不发布 item）
    txn_dir = store._txn_dir(TASK_ID, txn_id)
    (store.tasks_dir / TASK_ID / "task.json").write_bytes((txn_dir / "new" / "task.json").read_bytes())
    store.recover_incomplete_transactions(TASK_ID)
    assert (store.tasks_dir / TASK_ID / "task.json").read_bytes() == before  # 回滚 old
    assert not (store.tasks_dir / TASK_ID / "items" / f"{ITEM_ID}.json").exists()
    assert not list(store._transactions_dir(TASK_ID).iterdir())


def test_crash_mid_multi_item_publish_rolls_back(store):
    """多 item 中间崩溃 -> 全部回滚。"""
    store.write_task_snapshot(TASK_ID, make_task(revision=1), expected_revision=None)
    before = (store.tasks_dir / TASK_ID / "task.json").read_bytes()
    with store._lock_context(TASK_ID):
        txn_id = store.begin_transaction(TASK_ID, make_event(sequence=1), None, {})
        store.prepare_snapshots(TASK_ID, txn_id, task_snapshot=new_task_v2(sequence=1),
                                item_snapshots=[make_item(revision=2, item_id=ITEM_ID),
                                                make_item(revision=2, item_id=ITEM_ID2)])
    txn_dir = store._txn_dir(TASK_ID, txn_id)
    # 发布 task + item1（item2 未发布）
    (store.tasks_dir / TASK_ID / "items").mkdir(parents=True, exist_ok=True)
    (store.tasks_dir / TASK_ID / "task.json").write_bytes((txn_dir / "new" / "task.json").read_bytes())
    (store.tasks_dir / TASK_ID / "items" / f"{ITEM_ID}.json").write_bytes((txn_dir / "new" / f"items/{ITEM_ID}.json").read_bytes())
    store.recover_incomplete_transactions(TASK_ID)
    assert (store.tasks_dir / TASK_ID / "task.json").read_bytes() == before
    assert not (store.tasks_dir / TASK_ID / "items" / f"{ITEM_ID}.json").exists()
    assert not (store.tasks_dir / TASK_ID / "items" / f"{ITEM_ID2}.json").exists()


def test_transaction_metadata_save_failure(store, monkeypatch):
    """transaction metadata 保存失败 -> 显式失败，无部分新快照。"""
    store.write_task_snapshot(TASK_ID, make_task(revision=1), expected_revision=None)
    before = (store.tasks_dir / TASK_ID / "task.json").read_bytes()
    real = _atomic_write_bytes
    calls = {"n": 0}

    def fail_on_transaction_json(target, data):
        calls["n"] += 1
        if "transaction.json" in str(target) and calls["n"] >= 2:
            raise OSError("injected metadata failure")
        return real(target, data)

    monkeypatch.setattr("services.pipeline_task_store._atomic_write_bytes", fail_on_transaction_json)
    with pytest.raises(PipelineStoreError):
        run_task_transaction(store, TASK_ID, make_event(sequence=1), new_task=new_task_v2(sequence=1),
                             expected_task_revision=1, expected_item_revisions={})
    monkeypatch.undo()
    assert (store.tasks_dir / TASK_ID / "task.json").read_bytes() == before


# ---------------------------------------------------------------- 事件后禁回滚


def test_commit_state_write_failure_keeps_snapshot(store, monkeypatch):
    """事件追加成功、commit 状态写入失败 -> 快照不回滚、事件保留、可恢复错误、recover 幂等完成。"""
    run_task_transaction(store, TASK_ID, make_event(sequence=1), new_task=new_task_v2(sequence=1),
                         new_items=[make_item()], expected_task_revision=None, expected_item_revisions={})
    # 第二次事务：commit 的 _save_transaction 注入失败
    before_task = (store.tasks_dir / TASK_ID / "task.json").read_bytes()
    real = _atomic_write_bytes
    txn_meta_calls = {"n": 0}

    def fail_commit_meta(target, data):
        if "transaction.json" in str(target):
            txn_meta_calls["n"] += 1
            if txn_meta_calls["n"] >= 5:  # begin/prepare/publish/append 后 commit 写状态失败
                raise OSError("injected commit failure")
        return real(target, data)

    monkeypatch.setattr("services.pipeline_task_store._atomic_write_bytes", fail_commit_meta)
    ev2 = make_event(sequence=2, event_id=str(uuid.uuid4()))
    with pytest.raises(PipelineStoreError) as ei:
        run_task_transaction(store, TASK_ID, ev2, new_task=new_task_v2(sequence=2, revision=3),
                             expected_task_revision=2, expected_item_revisions={})
    monkeypatch.undo()
    assert ei.value.retryable is True  # 可恢复错误
    assert (store.tasks_dir / TASK_ID / "task.json").read_bytes() != before_task  # 新快照保留（不回滚）
    assert store.load_task(TASK_ID).revision == 3
    events = store.load_event_log(TASK_ID)
    assert len(events) == 2  # 事件保留不删除不重复
    # recover 幂等完成
    store.recover_incomplete_transactions(TASK_ID)
    assert store.load_task(TASK_ID).revision == 3
    assert len(store.load_event_log(TASK_ID)) == 2
    assert not list(store._transactions_dir(TASK_ID).iterdir())


def test_commit_cleanup_failure_keeps_snapshot(store, monkeypatch):
    """事件追加成功、事务目录清理失败 -> 快照不回滚、recover 幂等完成。"""
    run_task_transaction(store, TASK_ID, make_event(sequence=1), new_task=new_task_v2(sequence=1),
                         new_items=[make_item()], expected_task_revision=None, expected_item_revisions={})
    real_rmtree = shutil.rmtree

    def fail_rmtree(path, ignore_errors=False):
        raise OSError("injected cleanup failure")

    monkeypatch.setattr("services.pipeline_task_store.shutil.rmtree", fail_rmtree)
    ev2 = make_event(sequence=2, event_id=str(uuid.uuid4()))
    with pytest.raises(PipelineStoreError) as ei:
        run_task_transaction(store, TASK_ID, ev2, new_task=new_task_v2(sequence=2, revision=3),
                             expected_task_revision=2, expected_item_revisions={})
    monkeypatch.undo()
    assert ei.value.retryable is True
    assert store.load_task(TASK_ID).revision == 3  # 新快照保留
    assert len(store.load_event_log(TASK_ID)) == 2
    store.recover_incomplete_transactions(TASK_ID)
    assert len(store.load_event_log(TASK_ID)) == 2
    assert not list(store._transactions_dir(TASK_ID).iterdir())


def test_rollback_rejected_after_event_appended(store):
    """rollback_transaction 检测到事件已追加必须拒绝并保留现场。"""
    run_task_transaction(store, TASK_ID, make_event(sequence=1), new_task=new_task_v2(sequence=1),
                         new_items=[make_item()], expected_task_revision=None, expected_item_revisions={})
    # 构造一个事件已追加但事务未清理的场景
    with store._lock_context(TASK_ID):
        txn_id = store.begin_transaction(TASK_ID, make_event(sequence=2, event_id=str(uuid.uuid4())), 2, {})
        store.prepare_snapshots(TASK_ID, txn_id, task_snapshot=new_task_v2(sequence=2, revision=3))
        store.publish_prepared_snapshots(TASK_ID, txn_id)
        store.append_transaction_event(TASK_ID, txn_id)
        with pytest.raises(PipelineStoreError):
            store.rollback_transaction(TASK_ID, txn_id)  # 事件已追加 -> 拒绝
    assert store.load_task(TASK_ID).revision == 3  # 现场保留
    assert len(store.load_event_log(TASK_ID)) == 2


# ---------------------------------------------------------------- 恢复核验事件内容


def _make_event_appended_txn(store, event=None, new_task=None):
    """构造 event_appended 状态事务（未 commit）。仅发布 task（item_index 空）以通过 item 对账。"""
    with store._lock_context(TASK_ID):
        txn_id = store.begin_transaction(TASK_ID, event or make_event(sequence=1), None, {})
        store.prepare_snapshots(TASK_ID, txn_id,
                                task_snapshot=new_task or new_task_v2(sequence=1, include_item=False))
        store.publish_prepared_snapshots(TASK_ID, txn_id)
        store.append_transaction_event(TASK_ID, txn_id)
        return txn_id


def test_recover_verifies_event_content_same_sequence_different_id(store):
    """同 sequence 不同 event_id -> corrupted，不得自动提交。"""
    store.write_task_snapshot(TASK_ID, make_task(revision=1), expected_revision=None)
    txn_id = _make_event_appended_txn(store)
    # 篡改 events.ndjson 尾部为同 sequence 不同 event_id
    events_path = store._events_path(TASK_ID)
    lines = events_path.read_text(encoding="utf-8").splitlines()
    other = make_event(sequence=1, event_id=str(uuid.uuid4()))
    lines[-1] = canonical_json_bytes(other.model_dump(mode="json")).decode("utf-8")
    events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(PipelineStoreError) as ei:
        store.recover_incomplete_transactions(TASK_ID)
    assert ei.value.error_code == ERR_TASK_CORRUPTED


def test_recover_verifies_event_content_same_id_diff_content(store):
    store.write_task_snapshot(TASK_ID, make_task(revision=1), expected_revision=None)
    txn_id = _make_event_appended_txn(store)
    events_path = store._events_path(TASK_ID)
    lines = events_path.read_text(encoding="utf-8").splitlines()
    tampered = make_event(sequence=1, event_id=EVENT_ID, event_type="task_started")
    lines[-1] = canonical_json_bytes(tampered.model_dump(mode="json")).decode("utf-8")
    events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(PipelineStoreError) as ei:
        store.recover_incomplete_transactions(TASK_ID)
    assert ei.value.error_code == ERR_TASK_CORRUPTED


def test_recover_verifies_snapshot_matches_new(store):
    store.write_task_snapshot(TASK_ID, make_task(revision=1), expected_revision=None)
    txn_id = _make_event_appended_txn(store)
    # 篡改正式 task.json 使其与 new/ 不一致
    (store.tasks_dir / TASK_ID / "task.json").write_text(
        canonical_json_bytes(new_task_v2(sequence=1, revision=99).model_dump(mode="json")).decode("utf-8"),
        encoding="utf-8",
    )
    with pytest.raises(PipelineStoreError) as ei:
        store.recover_incomplete_transactions(TASK_ID)
    assert ei.value.error_code == ERR_TASK_CORRUPTED


def test_recover_new_file_missing_corrupted(store):
    store.write_task_snapshot(TASK_ID, make_task(revision=1), expected_revision=None)
    txn_id = _make_event_appended_txn(store)
    # 删除 new/task.json
    (store._txn_dir(TASK_ID, txn_id) / "new" / "task.json").unlink()
    with pytest.raises(PipelineStoreError) as ei:
        store.recover_incomplete_transactions(TASK_ID)
    assert ei.value.error_code == ERR_TASK_CORRUPTED


def test_recover_snapshots_published_completes_event(store):
    """snapshots_published（事件未追加）恢复：补写事件并提交。"""
    store.write_task_snapshot(TASK_ID, make_task(revision=1), expected_revision=None)
    with store._lock_context(TASK_ID):
        txn_id = store.begin_transaction(TASK_ID, make_event(sequence=1), None, {})
        store.prepare_snapshots(TASK_ID, txn_id, task_snapshot=new_task_v2(sequence=1, include_item=False))
        store.publish_prepared_snapshots(TASK_ID, txn_id)
    store.recover_incomplete_transactions(TASK_ID)
    events = store.load_event_log(TASK_ID)
    assert len(events) == 1 and events[0].sequence == 1
    assert store.load_task(TASK_ID).last_event_sequence == 1
    assert not list(store._transactions_dir(TASK_ID).iterdir())


def test_recover_snapshots_published_append_fail_rolls_back(store, monkeypatch):
    store.write_task_snapshot(TASK_ID, make_task(revision=1), expected_revision=None)
    before = (store.tasks_dir / TASK_ID / "task.json").read_bytes()
    with store._lock_context(TASK_ID):
        txn_id = store.begin_transaction(TASK_ID, make_event(sequence=1), None, {})
        store.prepare_snapshots(TASK_ID, txn_id, task_snapshot=new_task_v2(sequence=1, include_item=False))
        store.publish_prepared_snapshots(TASK_ID, txn_id)

    def failing(path, line):
        raise OSError("injected append failure")

    monkeypatch.setattr("services.pipeline_task_store._append_ndjson_bytes", failing)
    store.recover_incomplete_transactions(TASK_ID)
    monkeypatch.undo()
    assert (store.tasks_dir / TASK_ID / "task.json").read_bytes() == before  # 回滚 old
    assert store.load_event_log(TASK_ID) == []


def test_recover_event_appended_finalizes(store):
    store.write_task_snapshot(TASK_ID, make_task(revision=1), expected_revision=None)
    txn_id = _make_event_appended_txn(store)
    store.recover_incomplete_transactions(TASK_ID)
    assert len(store.load_event_log(TASK_ID)) == 1
    assert store.load_task(TASK_ID).last_event_sequence == 1
    assert not list(store._transactions_dir(TASK_ID).iterdir())


def test_recover_committed_leftover_cleaned(store):
    store.write_task_snapshot(TASK_ID, make_task(revision=1), expected_revision=None)
    txn_id = _make_event_appended_txn(store)
    txn = store._load_transaction(TASK_ID, txn_id)
    store._save_transaction(TASK_ID, txn_id,
                            txn.model_copy(update={"status": "committed", "event_ref": "events.ndjson"}))
    store.recover_incomplete_transactions(TASK_ID)
    assert not list(store._transactions_dir(TASK_ID).iterdir())
    assert len(store.load_event_log(TASK_ID)) == 1


def test_recover_idempotent_no_duplicate_events(store):
    store.write_task_snapshot(TASK_ID, make_task(revision=1), expected_revision=None)
    with store._lock_context(TASK_ID):
        txn_id = store.begin_transaction(TASK_ID, make_event(sequence=1), None, {})
        store.prepare_snapshots(TASK_ID, txn_id, task_snapshot=new_task_v2(sequence=1, include_item=False))
        store.publish_prepared_snapshots(TASK_ID, txn_id)
    store.recover_incomplete_transactions(TASK_ID)
    assert len(store.load_event_log(TASK_ID)) == 1
    store.recover_incomplete_transactions(TASK_ID)  # 第二次
    assert len(store.load_event_log(TASK_ID)) == 1


# ---------------------------------------------------------------- 事件保护


def test_event_idempotency_and_conflict(store):
    """同 event_id 已存在且内容一致 -> 幂等命中不重复追加；内容不同 -> 显式冲突。"""
    with store._lock_context(TASK_ID):
        txn_id = store.begin_transaction(TASK_ID, make_event(sequence=1), None, {})
        store.prepare_snapshots(TASK_ID, txn_id, task_snapshot=new_task_v2(sequence=1, include_item=False))
        store.publish_prepared_snapshots(TASK_ID, txn_id)
        store.append_transaction_event(TASK_ID, txn_id)
        store.append_transaction_event(TASK_ID, txn_id)  # 幂等命中不重复
    assert len(store.load_event_log(TASK_ID)) == 1
    with store._lock_context(TASK_ID):
        txn_dir = store._txn_dir(TASK_ID, txn_id)
        tampered = make_event(sequence=1, event_id=EVENT_ID, event_type="task_started")
        (txn_dir / "event.json").write_bytes(canonical_json_bytes(tampered.model_dump(mode="json")))
        with pytest.raises(PipelineStoreError) as ei:
            store.append_transaction_event(TASK_ID, txn_id)
        assert ei.value.error_code == ERR_EVENT_IDEMPOTENCY_CONFLICT


def test_event_log_tail_corruption_explicit_failure(store):
    run_task_transaction(store, TASK_ID, make_event(sequence=1), new_task=new_task_v2(sequence=1),
                         new_items=[make_item()], expected_task_revision=None, expected_item_revisions={})
    with open(store._events_path(TASK_ID), "ab") as fh:
        fh.write(b"{corrupt tail\n")
    with pytest.raises(PipelineStoreError) as ei:
        store.load_event_log(TASK_ID)
    assert ei.value.error_code == ERR_TASK_CORRUPTED


def test_append_event_concurrent_same_sequence(store):
    """两个并发事务使用相同 sequence：仅一个成功，另一个明确冲突，日志只有一条。"""
    results = []
    barrier = threading.Barrier(2)

    def worker(n):
        try:
            barrier.wait()
            run_task_transaction(store, TASK_ID, make_event(sequence=1, event_id=str(uuid.uuid4())),
                                 new_task=new_task_v2(sequence=1), new_items=[make_item()],
                                 expected_task_revision=None, expected_item_revisions={})
            results.append("ok")
        except PipelineStoreError as exc:
            results.append(exc.error_code)
        except Exception as exc:  # pragma: no cover
            results.append(type(exc).__name__)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert results.count("ok") == 1
    assert len(store.load_event_log(TASK_ID)) == 1


# ---------------------------------------------------------------- 引用路径与备份


def test_transaction_ref_paths_tightened(store):
    for bad in ("task.json/child", "items/", "items/not-a-uuid.json",
                "items/00000000-0000-0000-0000-000000000001.json/child",
                "other.json", "items/a.json"):
        with pytest.raises(PipelineStoreError):
            store._safe_rel_ref(bad)
    assert str(store._safe_rel_ref("task.json")).replace("\\", "/") == "task.json"
    assert str(store._safe_rel_ref(f"items/{ITEM_ID}.json")).replace("\\", "/") == f"items/{ITEM_ID}.json"


def test_backups_not_touched_by_ordinary_ops(store):
    store.write_task_snapshot(TASK_ID, make_task(revision=1), expected_revision=None)
    marker = store.backups_dir / "marker"
    marker.write_text("keep", encoding="utf-8")
    run_task_transaction(store, TASK_ID, make_event(sequence=1), new_task=new_task_v2(sequence=1),
                         new_items=[make_item()], expected_task_revision=1, expected_item_revisions={})
    store.recover_incomplete_transactions(TASK_ID)
    assert marker.read_text(encoding="utf-8") == "keep"


def test_old_data_tasks_untouched(store, tmp_path):
    old_dir = tmp_path / "data" / "tasks"
    old_dir.mkdir(parents=True, exist_ok=True)
    sentinel = old_dir / "sentinel.json"
    sentinel.write_text('{"old": true}', encoding="utf-8")
    run_task_transaction(store, TASK_ID, make_event(sequence=1), new_task=new_task_v2(sequence=1),
                         new_items=[make_item()], expected_task_revision=None, expected_item_revisions={})
    assert sentinel.read_text(encoding="utf-8") == '{"old": true}'


def test_synthetic_data_no_sensitive_values(store):
    dumped = json.dumps({
        "task": make_task().model_dump(mode="json"),
        "item": make_item().model_dump(mode="json"),
        "event": make_event().model_dump(mode="json"),
        "request": ResumeRequest(
            contract_version="pipeline-task/v1", request_id=str(uuid.uuid4()),
            task_id=TASK_ID, requested_at=T0, mode="resume_pending", expected_revision=1,
            expected_item_revisions={ITEM_ID: 1}, item_ids=[ITEM_ID],
            reason_code="stale_running_recovery", dry_run=False,
        ).model_dump(mode="json"),
        "decision": ResumeDecision(
            contract_version="pipeline-task/v1", decision_id=str(uuid.uuid4()),
            request_id=str(uuid.uuid4()), task_id=TASK_ID, decided_at=T0, approved=True,
            task_revision_before=1, eligible_items=[{"item_id": ITEM_ID, "resume_from_stage": "validate",
                                                     "next_attempt_number": 1, "reason_code": "retryable"}],
            protected_items=[], rejected_items=[], decision_error_codes=[], dry_run=False,
        ).model_dump(mode="json"),
    }, ensure_ascii=False, default=str)
    for pat in (r"1[3-9]\d{9}", r"https?://", r"school", r"姓名", r"网盘", r"D:/", r"C:\\Users"):
        assert not re.search(pat, dumped), f"合成数据含敏感模式: {pat}"


def test_no_half_written_authoritative_snapshot_after_fault(store, monkeypatch):
    store.write_task_snapshot(TASK_ID, make_task(revision=1), expected_revision=None)
    before = (store.tasks_dir / TASK_ID / "task.json").read_bytes()
    real = _atomic_write_bytes
    calls = {"n": 0}

    def failing(target, data):
        calls["n"] += 1
        if calls["n"] == 3:  # prepare 后 publish 阶段失败
            raise OSError("injected")
        return real(target, data)

    monkeypatch.setattr("services.pipeline_task_store._atomic_write_bytes", failing)
    with pytest.raises(PipelineStoreError):
        run_task_transaction(store, TASK_ID, make_event(sequence=1), new_task=new_task_v2(sequence=1),
                             new_items=[make_item()], expected_task_revision=None, expected_item_revisions={})
    monkeypatch.undo()
    loaded = store.load_task(TASK_ID)
    assert loaded is not None and loaded.revision == 1
    assert (store.tasks_dir / TASK_ID / "task.json").read_bytes() == before
    assert store.load_event_log(TASK_ID) == []


# ---------------------------------------------------------------- fix-2：事件必须通过含 new_task 的事务


def test_transaction_without_new_task_rejected_zero_side_effect(store):
    """没有 new_task 的事务被拒绝，零副作用。"""
    with pytest.raises(PipelineStoreError) as ei:
        run_task_transaction(store, TASK_ID, make_event(sequence=1), new_task=None,
                             new_items=[make_item()], expected_task_revision=None, expected_item_revisions={})
    assert ei.value.error_code == ERR_TRANSACTION_ERROR
    assert not (store.tasks_dir / TASK_ID).exists()  # 零副作用


def test_item_only_transaction_rejected(store):
    """只有 item + event（无 task 快照）被拒绝。"""
    with store._lock_context(TASK_ID):
        txn_id = store.begin_transaction(TASK_ID, make_event(sequence=1), None, {})
        store.prepare_snapshots(TASK_ID, txn_id, task_snapshot=None, item_snapshots=[make_item()])
        store.publish_prepared_snapshots(TASK_ID, txn_id)
        with pytest.raises(PipelineStoreError) as ei:
            store.append_transaction_event(TASK_ID, txn_id)
        assert ei.value.error_code == ERR_TRANSACTION_ERROR
    assert store.load_event_log(TASK_ID) == []


def test_no_public_append_event_entrypoint():
    """不存在公开 append_event 路径（事件只能通过事务追加）。"""
    from services import pipeline_task_store as mod

    assert not hasattr(mod.PipelineTaskStore, "append_event")
    assert not hasattr(mod.PipelineTaskStore, "_append_event_locked")


# ---------------------------------------------------------------- fix-2：item_index 逐项对账


def _successful_txn_with_item(store, sequence: int = 1, revision: int = 2, item_revision: int = 1):
    run_task_transaction(store, TASK_ID, make_event(sequence=sequence, event_id=str(uuid.uuid4())),
                         new_task=new_task_v2(sequence=sequence, revision=revision),
                         new_items=[make_item(revision=item_revision)],
                         expected_task_revision=1 if revision > 2 else None,
                         expected_item_revisions={})


def test_reconciliation_missing_item_file(store):
    """item_index 声明 ITEM_ID 但 item 文件缺失 -> corrupted，不得提交。"""
    with store._lock_context(TASK_ID):
        txn_id = store.begin_transaction(TASK_ID, make_event(sequence=1), None, {})
        store.prepare_snapshots(TASK_ID, txn_id, task_snapshot=new_task_v2(sequence=1))  # index 含 ITEM_ID
        store.publish_prepared_snapshots(TASK_ID, txn_id)
        store.append_transaction_event(TASK_ID, txn_id)
        with pytest.raises(PipelineStoreError) as ei:
            store.commit_transaction(TASK_ID, txn_id)
        assert ei.value.error_code == ERR_TASK_CORRUPTED


def test_reconciliation_status_mismatch(store):
    """task.item_index status 与 item 快照不一致 -> corrupted。"""
    with store._lock_context(TASK_ID):
        txn_id = store.begin_transaction(TASK_ID, make_event(sequence=1), None, {})
        # task 的 index 写 completed，item 文件是 pending
        t = new_task_v2(sequence=1)
        t.item_index[0] = t.item_index[0].model_copy(update={"status": "completed"})
        t.completed_items = 1
        t.pending_items = 0
        store.prepare_snapshots(TASK_ID, txn_id, task_snapshot=t, item_snapshots=[make_item(revision=1)])
        store.publish_prepared_snapshots(TASK_ID, txn_id)
        store.append_transaction_event(TASK_ID, txn_id)
        with pytest.raises(PipelineStoreError) as ei:
            store.commit_transaction(TASK_ID, txn_id)
        assert ei.value.error_code == ERR_TASK_CORRUPTED


def test_reconciliation_current_stage_mismatch(store):
    with store._lock_context(TASK_ID):
        txn_id = store.begin_transaction(TASK_ID, make_event(sequence=1), None, {})
        t = new_task_v2(sequence=1)
        t.item_index[0] = t.item_index[0].model_copy(update={"current_stage": "validate"})
        store.prepare_snapshots(TASK_ID, txn_id, task_snapshot=t, item_snapshots=[make_item()])
        store.publish_prepared_snapshots(TASK_ID, txn_id)
        store.append_transaction_event(TASK_ID, txn_id)
        with pytest.raises(PipelineStoreError) as ei:
            store.commit_transaction(TASK_ID, txn_id)
        assert ei.value.error_code == ERR_TASK_CORRUPTED


def test_reconciliation_item_revision_mismatch(store):
    with store._lock_context(TASK_ID):
        txn_id = store.begin_transaction(TASK_ID, make_event(sequence=1), None, {})
        t = new_task_v2(sequence=1)
        t.item_index[0] = t.item_index[0].model_copy(update={"item_revision": 5})
        store.prepare_snapshots(TASK_ID, txn_id, task_snapshot=t, item_snapshots=[make_item(revision=1)])
        store.publish_prepared_snapshots(TASK_ID, txn_id)
        store.append_transaction_event(TASK_ID, txn_id)
        with pytest.raises(PipelineStoreError) as ei:
            store.commit_transaction(TASK_ID, txn_id)
        assert ei.value.error_code == ERR_TASK_CORRUPTED


def test_reconciliation_fingerprint_mismatch(store):
    with store._lock_context(TASK_ID):
        txn_id = store.begin_transaction(TASK_ID, make_event(sequence=1), None, {})
        t = new_task_v2(sequence=1)
        t.item_index[0] = t.item_index[0].model_copy(update={"input_fingerprint": "d" * 64})
        store.prepare_snapshots(TASK_ID, txn_id, task_snapshot=t, item_snapshots=[make_item()])
        store.publish_prepared_snapshots(TASK_ID, txn_id)
        store.append_transaction_event(TASK_ID, txn_id)
        with pytest.raises(PipelineStoreError) as ei:
            store.commit_transaction(TASK_ID, txn_id)
        assert ei.value.error_code == ERR_TASK_CORRUPTED


def test_reconciliation_evidence_level_mismatch(store):
    """task.item_index evidence_level 与 item 快照不一致 -> corrupted（11C-2c-prerequisite-fix-1）。"""
    with store._lock_context(TASK_ID):
        txn_id = store.begin_transaction(TASK_ID, make_event(sequence=1), None, {})
        t = new_task_v2(sequence=1)
        t.item_index[0] = t.item_index[0].model_copy(update={"evidence_level": "limited"})
        store.prepare_snapshots(TASK_ID, txn_id, task_snapshot=t, item_snapshots=[make_item()])  # item 是 sufficient
        store.publish_prepared_snapshots(TASK_ID, txn_id)
        store.append_transaction_event(TASK_ID, txn_id)
        with pytest.raises(PipelineStoreError) as ei:
            store.commit_transaction(TASK_ID, txn_id)
        assert ei.value.error_code == ERR_TASK_CORRUPTED


def test_reconciliation_evidence_level_matched_ok(store):
    """index 与 item 的 evidence_level 一致 -> 对账通过。"""
    run_task_transaction(store, TASK_ID, make_event(sequence=1, event_id=str(uuid.uuid4())),
                         new_task=new_task_v2(sequence=1), new_items=[make_item()],
                         expected_task_revision=None, expected_item_revisions={})
    assert store.load_task(TASK_ID).item_index[0].evidence_level == "sufficient"


def test_reconciliation_extra_unregistered_item(store):
    """items 目录存在不属于 item_index 的当前任务 item 快照 -> corrupted。"""
    run_task_transaction(store, TASK_ID, make_event(sequence=1, event_id=str(uuid.uuid4())),
                         new_task=new_task_v2(sequence=1), new_items=[make_item()],
                         expected_task_revision=None, expected_item_revisions={})
    # 第二个事务只更新 task（index 仍只含 ITEM_ID），items 目录多出一个未登记 item
    (store.tasks_dir / TASK_ID / "items").mkdir(parents=True, exist_ok=True)
    (store.tasks_dir / TASK_ID / "items" / f"{ITEM_ID2}.json").write_bytes(
        canonical_json_bytes(make_item(revision=1, item_id=ITEM_ID2).model_dump(mode="json")))
    with pytest.raises(PipelineStoreError) as ei:
        run_task_transaction(store, TASK_ID, make_event(sequence=2, event_id=str(uuid.uuid4())),
                             new_task=new_task_v2(sequence=2, revision=3), expected_task_revision=2,
                             expected_item_revisions={})
    assert ei.value.error_code == ERR_TASK_CORRUPTED


def test_reconciliation_partial_item_update_ok(store):
    """更新部分 item 时，未更新 item 仍能从正式快照完成对账。"""
    run_task_transaction(store, TASK_ID, make_event(sequence=1, event_id=str(uuid.uuid4())),
                         new_task=new_task_v2(sequence=1), new_items=[make_item()],
                         expected_task_revision=None, expected_item_revisions={})
    # 第二次只更新 task（item_index 保持 ITEM_ID pending，item 文件未变）-> 对账通过
    run_task_transaction(store, TASK_ID, make_event(sequence=2, event_id=str(uuid.uuid4())),
                         new_task=new_task_v2(sequence=2, revision=3), expected_task_revision=2,
                         expected_item_revisions={})
    assert store.load_task(TASK_ID).revision == 3
    assert len(store.load_event_log(TASK_ID)) == 2


# ---------------------------------------------------------------- fix-2：recover 持锁与锁释放


def test_recover_lock_conflict_zero_side_effect(store):
    """正常事务持锁期间调用恢复 -> 明确锁冲突；事务目录/快照/事件均不变。"""
    run_task_transaction(store, TASK_ID, make_event(sequence=1, event_id=str(uuid.uuid4())),
                         new_task=new_task_v2(sequence=1), new_items=[make_item()],
                         expected_task_revision=None, expected_item_revisions={})
    before_task = (store.tasks_dir / TASK_ID / "task.json").read_bytes()
    before_events = store.load_event_log(TASK_ID)
    lock_path = store._lock_path(TASK_ID)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("other-holder-token", encoding="utf-8")  # 模拟他人持锁
    with pytest.raises(PipelineStoreError) as ei:
        store.recover_incomplete_transactions(TASK_ID)
    assert ei.value.error_code == ERR_LOCK_CONFLICT
    assert (store.tasks_dir / TASK_ID / "task.json").read_bytes() == before_task
    assert store.load_event_log(TASK_ID) == before_events


def test_recover_concurrent_only_one_executes(store):
    """两个并发恢复调用：只有一个执行，另一个不得重复追加事件。"""
    run_task_transaction(store, TASK_ID, make_event(sequence=1, event_id=str(uuid.uuid4())),
                         new_task=new_task_v2(sequence=1), new_items=[make_item()],
                         expected_task_revision=None, expected_item_revisions={})
    # 制造一个未完成的 snapshots_published 事务（task item_index 保留 ITEM_ID 与既有 item 文件对账）
    with store._lock_context(TASK_ID):
        txn_id = store.begin_transaction(TASK_ID, make_event(sequence=2, event_id=str(uuid.uuid4())), 2, {})
        store.prepare_snapshots(TASK_ID, txn_id, task_snapshot=new_task_v2(sequence=2, revision=3))
        store.publish_prepared_snapshots(TASK_ID, txn_id)
    errors = []

    def do_recover():
        try:
            store.recover_incomplete_transactions(TASK_ID)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=do_recover) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    assert len(store.load_event_log(TASK_ID)) == 2  # 无重复追加
    assert not list(store._transactions_dir(TASK_ID).iterdir())


def test_lock_release_failure_does_not_block_process_lock(store):
    """人为替换 owner token：第一次退出返回 LOCK_CONFLICT 且锁文件保留；
    清理锁后后续操作仍能获取进程锁，不发生死锁。"""
    run_task_transaction(store, TASK_ID, make_event(sequence=1, event_id=str(uuid.uuid4())),
                         new_task=new_task_v2(sequence=1), new_items=[make_item()],
                         expected_task_revision=None, expected_item_revisions={})
    lock_path = store._lock_path(TASK_ID)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # 人为替换 token（模拟锁被他人改写）
    lock_path.write_text("tampered-token", encoding="utf-8")
    with pytest.raises(PipelineStoreError) as ei:
        with store._lock_context(TASK_ID):
            pass
    assert ei.value.error_code == ERR_LOCK_CONFLICT
    assert lock_path.exists()  # 锁文件保留（不删除他人锁）
    # 清理合成测试锁后，后续操作可再次获取进程锁（不卡死）
    lock_path.unlink()
    store.write_task_snapshot(TASK_ID, make_task(revision=9), expected_revision=2)  # 快速无卡死验证
    assert store.load_task(TASK_ID).revision == 9
