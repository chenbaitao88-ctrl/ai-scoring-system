"""
Phase 11E-3a：PipelineTask 批量评分编排集成测试。

多 item 有界并发、错峰推进、失败隔离、暂停/取消响应、CAS 有限重试、
批次幂等重放、并发竞争单调用、统计聚合、敏感零泄漏、evidence task 拒绝。
全部合成脱敏 fixture + mock transport；不调用真实模型/凭据/学生材料。
"""
from __future__ import annotations

import asyncio
import json
import threading

import pytest

from services.scoring_batch_orchestrator import (
    BatchScoringExecutionResult,
    ScoringBatchOrchestrator,
)
import test_scoring_pipeline_execute_integration as base


def _run(coro):
    return asyncio.run(coro)


class CountingTransport(base.FakeTransport):
    """mock transport + 并发计数（有界并发实测）。"""

    def __init__(self, scenario="direct", *a, **k):
        super().__init__(scenario=scenario, *a, **k)
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()
        self.on_first_call = None  # 回调：第一次调用后触发（暂停等）

    async def call(self, *a, **k):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)  # 让出：真实并发交错
            result = await super().call(*a, **k)
            if self.on_first_call is not None and len(self.calls) == 1:
                cb, args = self.on_first_call
                cb(*args)
            return result
        finally:
            with self._lock:
                self.active -= 1


class SequentialFailTransport(CountingTransport):
    """前 fail_at 次调用失败（failure_kind），其余成功。"""

    def __init__(self, fail_at=1, failure_kind="rate_limited", **k):
        super().__init__(**k)
        self.fail_at = fail_at
        self.failure_kind = failure_kind
        self._index = 0

    async def call(self, *a, **k):
        with self._lock:
            self._index += 1
            idx = self._index
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            if idx <= self.fail_at:
                # 失败调用同样计入调用事实（Provider 已收到）
                self.calls.append({"model_id": a[2]})
                return base.TransportResult(status_code=429, failure_kind=self.failure_kind)
            return await super().call(*a, **k)
        finally:
            with self._lock:
                self.active -= 1


def _make_batch(env):
    return ScoringBatchOrchestrator(env["orch"], env["orch"]._task_item)


def _item_ids(env, count=None):
    task = env["task"]
    ids = [e.item_id for e in task.item_index]
    return ids if count is None else ids[:count]


# ---------------- 全成功 / 并发 ---------------- #


def test_batch_all_success(tmp_path):
    """4 item 全部成功：Provider 调用次数 = 实际待评分数。"""
    transport = CountingTransport(scenario="direct")
    env = base._seed_env(tmp_path, item_count=4, transport=transport)
    batch = _make_batch(env)
    r = _run(batch.execute_scoring_task(env["task"].task_id, "batch-ok", concurrency=2))
    assert r.outcome == "running"  # score 完成后 task 仍在 review（不伪造 export/completed）
    assert r.succeeded == 4
    assert r.total_items == 4
    assert r.started == 4
    assert r.blocked == 0 and r.recovery_required == 0
    assert len(transport.calls) == 4
    # 每个 item 到 review/pending
    for item_id in _item_ids(env):
        item = env["store"].load_item(env["task"].task_id, item_id)
        assert item.current_stage == "review" and item.status == "pending"
    assert r.task_status in ("running", "completed_with_errors")
    assert r.pending_remaining is True  # review 阶段仍有待处理


def test_batch_bounded_concurrency(tmp_path):
    """多 item 有界并发：实测同时调用数不超过冻结上限（concurrency=2）。"""
    transport = CountingTransport(scenario="direct")
    env = base._seed_env(tmp_path, item_count=5, transport=transport)
    assert env["task"].concurrency == 2
    batch = _make_batch(env)
    r = _run(batch.execute_scoring_task(env["task"].task_id, "batch-cc", concurrency=99))
    assert r.succeeded == 5
    assert transport.max_active <= 2  # 收敛到冻结上限
    # 显式并发=1 时串行
    transport2 = CountingTransport(scenario="direct")
    env2 = base._seed_env(tmp_path / "env2", item_count=3, transport=transport2)
    batch2 = _make_batch(env2)
    _run(batch2.execute_scoring_task(env2["task"].task_id, "batch-cc2", concurrency=1))
    assert transport2.max_active == 1


# ---------------- 失败隔离 ---------------- #


def test_single_failure_others_succeed(tmp_path):
    """一个明确失败（retryable）：其他 item 继续成功，失败不覆盖成功快照。"""
    transport = SequentialFailTransport(fail_at=1, failure_kind="rate_limited")
    env = base._seed_env(tmp_path, item_count=4, transport=transport)
    batch = _make_batch(env)
    r = _run(batch.execute_scoring_task(env["task"].task_id, "batch-f1"))
    assert r.succeeded == 3
    assert r.recovery_required == 1  # rate_limited -> retry 待批准
    assert r.total_items == 4
    assert len(transport.calls) == 4
    assert "PROVIDER_RATE_LIMITED" in r.error_code_counts
    # 按状态断言：1 个 failed（待批准），3 个 review/pending 且成功快照存在
    failed = [env["store"].load_item(env["task"].task_id, e.item_id)
              for e in env["task"].item_index if env["store"].load_item(env["task"].task_id, e.item_id).status == "failed"]
    assert len(failed) == 1 and failed[0].current_stage == "score"
    assert env["attempt_store"].has_successful_snapshot(env["task"].task_id, failed[0].item_id) is False
    for item_id in _item_ids(env):
        item = env["store"].load_item(env["task"].task_id, item_id)
        if item.status != "failed":
            assert item.current_stage == "review" and item.status == "pending"
            assert env["attempt_store"].has_successful_snapshot(env["task"].task_id, item_id) is True


def test_single_outcome_unknown_others_succeed(tmp_path):
    """一个 outcome_unknown：其他 item 继续，任务不伪装全成功。"""
    transport = SequentialFailTransport(fail_at=1, failure_kind="timeout")
    env = base._seed_env(tmp_path, item_count=4, transport=transport)
    batch = _make_batch(env)
    r = _run(batch.execute_scoring_task(env["task"].task_id, "batch-ou"))
    assert r.succeeded == 3
    assert r.recovery_required == 1
    assert "EXECUTION_OUTCOME_UNKNOWN" in r.error_code_counts or "PROVIDER_OUTCOME_UNKNOWN" in r.error_code_counts
    # 恰好一个 item outcome_unknown 失败，其余成功
    failed = [env["store"].load_item(env["task"].task_id, e.item_id)
              for e in env["task"].item_index if env["store"].load_item(env["task"].task_id, e.item_id).status == "failed"]
    assert len(failed) == 1
    assert failed[0].last_error.error_code == "PROVIDER_OUTCOME_UNKNOWN"
    assert env["attempt_store"].has_successful_snapshot(env["task"].task_id, failed[0].item_id) is False
    assert r.succeeded < r.total_items  # 不伪装全成功
    assert len(transport.calls) == 4


# ---------------- 分流跳过 ---------------- #


def test_existing_success_skipped(tmp_path):
    """已有成功快照 item 跳过（不重复 Provider 调用）。"""
    env = base._seed_env(tmp_path, item_count=3)
    batch = _make_batch(env)
    item_a = env["store"].load_item(env["task"].task_id, _item_ids(env)[0])
    _run(env["orch"].execute_score_item(env["task"].task_id, item_a.item_id, "pre-a"))
    calls_before = len(env["transport"].calls)
    r = _run(batch.execute_scoring_task(env["task"].task_id, "batch-sk"))
    assert r.succeeded == 2  # 剩余两个
    assert r.skipped == 1  # item A 已成功
    assert len(env["transport"].calls) == calls_before + 2  # A 不重复


def test_review_export_skipped(tmp_path):
    """已进入 review/pending 的 item 跳过（score 已完成）。"""
    env = base._seed_env(tmp_path, item_count=3)
    batch = _make_batch(env)
    item_a = env["store"].load_item(env["task"].task_id, _item_ids(env)[0])
    _run(env["orch"].execute_score_item(env["task"].task_id, item_a.item_id, "pre-r"))
    assert env["store"].load_item(env["task"].task_id, item_a.item_id).current_stage == "review"
    calls_before = len(env["transport"].calls)
    r = _run(batch.execute_scoring_task(env["task"].task_id, "batch-rv"))
    assert r.skipped >= 1  # review item 跳过
    assert len(env["transport"].calls) == calls_before + 2


def test_manual_review_skipped_not_called(tmp_path, monkeypatch):
    """manual_review / skipped / completed item 不调用 Provider。"""
    env = base._seed_env(tmp_path, item_count=3)
    batch = _make_batch(env)
    task = env["task"]
    ids = _item_ids(env)
    # 把 item A 显示为 manual_review（只读投影）
    item_a = env["store"].load_item(task.task_id, ids[0])
    fake = item_a.model_copy(update={"status": "manual_review"})

    def fake_get_item(tid, iid):
        real = env["store"].load_item(tid, iid)
        if iid == ids[0]:
            return fake
        return real

    monkeypatch.setattr(batch._task_item, "get_item", fake_get_item)
    calls_before = len(env["transport"].calls)
    r = _run(batch.execute_scoring_task(task.task_id, "batch-mr"))
    assert r.skipped == 1  # manual_review 保留
    assert r.succeeded == 2
    assert len(env["transport"].calls) == calls_before + 2  # manual_review 不调用


def test_stale_running_goes_recovery(tmp_path, monkeypatch):
    """stale running：走恢复判断，不盲目调用 Provider。"""
    env = base._seed_env(tmp_path, item_count=2)

    def boom(*a, **k):
        raise RuntimeError("died before dispatch")

    monkeypatch.setattr(env["attempt_store"], "mark_attempt_dispatched", boom)
    item_a = env["store"].load_item(env["task"].task_id, _item_ids(env)[0])
    _run(env["orch"].execute_score_item(env["task"].task_id, item_a.item_id, "pre-st"))
    # item A running（调用前中断）；撤销注入避免影响 item B
    monkeypatch.undo()
    calls_before = len(env["transport"].calls)
    env["clock"][0] += __import__("datetime").timedelta(seconds=400)
    batch = _make_batch(env)
    r = _run(batch.execute_scoring_task(env["task"].task_id, "batch-st"))
    # item A 恢复（stale_precall_reset -> pending），item B 正常执行
    assert r.recovery_required >= 1
    assert r.succeeded == 1
    assert len(env["transport"].calls) == calls_before + 1  # 仅 item B 调用


def test_retryable_failed_approval_only(tmp_path):
    """retryable failed item：只进入待批准，不自动调用 Provider。"""
    transport = SequentialFailTransport(fail_at=1, failure_kind="rate_limited")
    env = base._seed_env(tmp_path, item_count=2, transport=transport)
    batch = _make_batch(env)
    r1 = _run(batch.execute_scoring_task(env["task"].task_id, "batch-rf1"))
    assert r1.succeeded == 1 and r1.recovery_required == 1
    calls_after = len(env["transport"].calls)
    r2 = _run(batch.execute_scoring_task(env["task"].task_id, "batch-rf2"))
    assert r2.recovery_required == 1  # 仍待批准
    assert len(env["transport"].calls) == calls_after  # 不自动重调


# ---------------- 暂停 / 取消 / CAS / 幂等 / 并发竞争 ---------------- #


def test_pause_stops_new_items(tmp_path):
    """批次执行中暂停：不再启动新 item；已进入 Provider 的 item 按事实完成。"""
    env_holder = {}

    def do_pause(*_):
        env = env_holder["env"]
        tid = env["task"].task_id
        task = env["store"].load_task(tid)
        env["mgr"].pause_task(tid, expected_revision=task.revision)

    transport = CountingTransport(scenario="direct")
    transport.on_first_call = (do_pause, ())
    env = base._seed_env(tmp_path, item_count=3, transport=transport)
    env_holder["env"] = env
    batch = _make_batch(env)
    r = _run(batch.execute_scoring_task(env["task"].task_id, "batch-pz", concurrency=1))
    assert r.outcome == "paused"
    # 仅第一个 item 完成调用；其余因暂停不再启动
    assert len(transport.calls) == 1
    assert r.succeeded == 1
    assert r.skipped == 2  # 暂停后未启动
    final = env["store"].load_task(env["task"].task_id)
    assert final.status == "paused"


def test_cancelled_no_new_calls(tmp_path, monkeypatch):
    """task 取消后不再启动新调用（取消状态由只读投影模拟）。"""
    env = base._seed_env(tmp_path, item_count=3)
    batch = _make_batch(env)

    def cancelled_get_task(tid):
        real = env["store"].load_task(tid)
        return real.model_copy(update={"status": "cancelled"}) if real else None

    monkeypatch.setattr(batch._task_item, "get_task", cancelled_get_task)
    calls_before = len(env["transport"].calls)
    r = _run(batch.execute_scoring_task(env["task"].task_id, "batch-cx"))
    assert r.skipped == 3  # 取消后全部不启动
    assert len(env["transport"].calls) == calls_before  # 零新调用


def test_batch_replay_idempotent(tmp_path):
    """同一 batch_execution_id 重放：已成功 item 短路，不产生重复调用。"""
    env = base._seed_env(tmp_path, item_count=3)
    batch = _make_batch(env)
    r1 = _run(batch.execute_scoring_task(env["task"].task_id, "batch-rp"))
    assert r1.succeeded == 3
    calls_after_first = len(env["transport"].calls)
    r2 = _run(batch.execute_scoring_task(env["task"].task_id, "batch-rp"))
    assert r2.skipped == 3  # 全部短路
    assert len(env["transport"].calls) == calls_after_first  # 零重复


def test_two_batch_executors_single_call_per_item(tmp_path):
    """两个批量执行者并发竞争：同一 item 最多调用一次 Provider。"""
    transport = CountingTransport(scenario="direct", yield_point=True)
    env = base._seed_env(tmp_path, item_count=3, transport=transport)
    batch = _make_batch(env)

    async def run_both():
        return await asyncio.gather(
            batch.execute_scoring_task(env["task"].task_id, "batch-c1"),
            batch.execute_scoring_task(env["task"].task_id, "batch-c2"),
        )

    results = asyncio.run(run_both())
    succeeded_total = sum(r.succeeded for r in results)
    assert succeeded_total == 3  # 每个 item 恰好成功一次
    assert len(transport.calls) == 3  # 每 item 一次调用
    for item_id in _item_ids(env):
        item = env["store"].load_item(env["task"].task_id, item_id)
        assert item.current_stage == "review" and item.status == "pending"


# ---------------- 聚合 / 错峰 / 敏感 ---------------- #


def test_staggered_stages_and_aggregation(tmp_path):
    """多 item 错峰合法 + task/item/stage summary 聚合与 item 事实一致。"""
    env = base._seed_env(tmp_path, item_count=3)
    batch = _make_batch(env)
    r = _run(batch.execute_scoring_task(env["task"].task_id, "batch-ag"))
    assert r.succeeded == 3
    task = env["store"].load_task(env["task"].task_id)
    # 所有 item 到 review/pending（错峰合法：score 完成、review 待处理）
    for item_id in _item_ids(env):
        item = env["store"].load_item(task.task_id, item_id)
        assert item.current_stage == "review" and item.status == "pending"
    score_summary = next(s for s in task.stage_summaries if s.stage == "score")
    review_summary = next(s for s in task.stage_summaries if s.stage == "review")
    # 冻结聚合语义：score 阶段被全部越过 -> status completed（completed_items 统计仍在 score 阶段的项，恒 0）
    assert score_summary.status == "completed"
    assert review_summary.pending_items == 3
    # 计数公式一致（六类互斥穷尽）
    assert task.total_items == (task.pending_items + task.running_items + task.completed_items
                                + task.failed_items + task.skipped_items + task.manual_review_items)


def test_failure_does_not_overwrite_other_success(tmp_path):
    """单项失败不覆盖其他成功快照（成功结果保护）。"""
    transport = SequentialFailTransport(fail_at=2, failure_kind="invalid_json")
    env = base._seed_env(tmp_path, item_count=3, transport=transport)
    batch = _make_batch(env)
    r = _run(batch.execute_scoring_task(env["task"].task_id, "batch-pr"))
    assert r.succeeded == 1  # 前两次调用失败，剩 1 个成功
    task = env["task"]
    failed = [env["store"].load_item(task.task_id, e.item_id)
              for e in task.item_index if env["store"].load_item(task.task_id, e.item_id).status == "failed"]
    assert len(failed) == 2
    for item in failed:
        assert env["attempt_store"].has_successful_snapshot(task.task_id, item.item_id) is False
    for item_id in _item_ids(env):
        item = env["store"].load_item(task.task_id, item_id)
        if item.status != "failed":
            assert env["attempt_store"].has_successful_snapshot(task.task_id, item_id) is True


def test_batch_sensitive_no_leak(tmp_path):
    """敏感内容不进入批量返回、事件与错误。"""
    env = base._seed_env(tmp_path, item_count=2)
    batch = _make_batch(env)
    r = _run(batch.execute_scoring_task(env["task"].task_id, "batch-sec"))
    text = json.dumps(r.model_dump(mode="json"))
    for bad in ("system_prompt", "user_prompt", "MOCK_CRED", "http://", "bearer", "sk-", "api_key"):
        assert bad not in text, bad
    for f in env["attempt_store"].root.rglob("*.ndjson"):
        low = f.read_text(encoding="utf-8").lower()
        assert "synthetic scoring standard" not in low
        assert "mock_cred" not in low


def test_batch_no_machinescore(tmp_path):
    """批量路径不写 MachineScore。"""
    env = base._seed_env(tmp_path, item_count=2)
    batch = _make_batch(env)
    r = _run(batch.execute_scoring_task(env["task"].task_id, "batch-ms"))
    assert r.succeeded == 2
    import services.scoring_batch_orchestrator as mod
    src = open(mod.__file__, encoding="utf-8").read()
    assert "MachineScore" not in src
    assert "HumanScore" not in src and "FinalScore" not in src


def test_evidence_task_rejected(tmp_path):
    """evidence preparation task 不可进入评分批量执行。"""
    env = base._seed_env(tmp_path, item_count=1)
    batch = _make_batch(env)
    # source evidence task（evidence_preparation_pipeline）
    source_task_id = None
    # 从 store 找 evidence task
    for task_dir in env["store"].tasks_dir.iterdir():
        t = env["store"].load_task(task_dir.name)
        if t is not None and t.task_type == "evidence_preparation_pipeline":
            source_task_id = t.task_id
            break
    assert source_task_id is not None
    r = _run(batch.execute_scoring_task(source_task_id, "batch-ev"))
    assert r.outcome == "invalid_task"
