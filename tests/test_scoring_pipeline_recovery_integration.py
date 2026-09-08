"""
Phase 11E-2c：失败恢复与调用交付保护集成测试。

在 11E-2b-2 mock 闭环基础上验证：调用前中断安全恢复 pending、明确失败仅待批准、
outcome_unknown 永久阻断、成功响应事实恢复补齐 snapshot/validation/item 状态、
已成功短路、stale 接管、指纹矛盾阻断、并发恢复单执行、幂等、多 item 隔离、
冻结配置不变、敏感零泄漏、恢复路径 Provider 调用恒 0。
全部合成脱敏 fixture + mock transport；不调用真实模型/凭据/学生材料。
"""
from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from models.score_attempt import ScoreAttempt
from services.pipeline_task_manager import PipelineTaskError
from services.score_attempt_store import ScoreAttemptStore
from services.scoring_pipeline_orchestrator import (
    RECOVERY_ALREADY_COMPLETED,
    RECOVERY_COMPLETED_FROM_FACTS,
    RECOVERY_CONCURRENT_REQUEST,
    RECOVERY_FACT_BINDING_MISMATCH,
    RECOVERY_OUTCOME_UNKNOWN_BLOCKED,
    RECOVERY_RETRY_APPROVAL_REQUIRED,
    RECOVERY_STALE_PRECALL_RESET,
    ERR_EXEC_ATTEMPT_WRITE_FAILED,
    ERR_EXEC_ITEM_COMPLETE_FAILED,
    ERR_EXEC_SNAPSHOT_WRITE_FAILED,
    ERR_EXEC_VALIDATION_WRITE_FAILED,
)
import test_scoring_pipeline_execute_integration as base


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def _stale_now(env, seconds=400):
    """把共享时钟推进到 stale 窗口（>300s）。"""
    env["clock"][0] += timedelta(seconds=seconds)


# ---------------- 调用前中断 ---------------- #


def test_precall_interrupt_stale_reset_to_pending(tmp_path, monkeypatch):
    """Provider 调用前中断（dispatch 标记失败）：item stale running -> 安全恢复 pending。"""
    env = base._seed_env(tmp_path)
    item = base._item(env)

    def boom(*a, **k):
        raise RuntimeError("process died before dispatch")

    monkeypatch.setattr(env["attempt_store"], "mark_attempt_dispatched", boom)
    r1 = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-pre"))
    assert r1.outcome == "internal_error"
    assert r1.error_code == ERR_EXEC_ATTEMPT_WRITE_FAILED
    assert len(env["transport"].calls) == 0  # Provider 未接收
    attempts = env["attempt_store"].list_attempts(env["task"].task_id, item.item_id)
    assert attempts and attempts[0].status == "running"  # 事实保留
    # 恢复：调用前 stale -> pending
    _stale_now(env)
    r2 = env["orch"].recover_score_item(env["task"].task_id, item.item_id, "rec-pre")
    assert r2.outcome == "stale_precall_reset"
    assert r2.error_code == RECOVERY_STALE_PRECALL_RESET
    item_after = base._item(env)
    assert item_after.status == "pending" and item_after.current_stage == "score"
    assert len(env["transport"].calls) == 0  # 恢复恒不调用 Provider


def test_precall_interrupt_valid_lease_not_reset(tmp_path, monkeypatch):
    """调用前中断但租约仍有效：不得抢占，返回并发请求。"""
    env = base._seed_env(tmp_path)
    item = base._item(env)

    def boom(*a, **k):
        raise RuntimeError("died before dispatch")

    monkeypatch.setattr(env["attempt_store"], "mark_attempt_dispatched", boom)
    _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-pre2"))
    # 心跳仍有效（未推进时钟）
    r = env["orch"].recover_score_item(env["task"].task_id, item.item_id, "rec-pre2")
    assert r.outcome == "concurrent_request"
    assert r.error_code == RECOVERY_CONCURRENT_REQUEST


# ---------------- 明确失败 / outcome_unknown ---------------- #


def test_retryable_failed_only_approval_required(tmp_path):
    """明确失败且 retryable：只返回待批准，不自动调用 Provider。"""
    transport = base.FakeTransport(failure_kind="rate_limited")  # retryable=conditional
    env = base._seed_env(tmp_path, transport=transport)
    item = base._item(env)
    r1 = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-rf"))
    assert r1.outcome == "failed" and r1.error_code == "PROVIDER_RATE_LIMITED"
    calls_before = len(env["transport"].calls)
    r2 = env["orch"].recover_score_item(env["task"].task_id, item.item_id, "rec-rf")
    assert r2.outcome == "retry_approval_required"
    assert r2.error_code == RECOVERY_RETRY_APPROVAL_REQUIRED
    assert len(env["transport"].calls) == calls_before  # 不自动调用


def test_outcome_unknown_permanently_blocked(tmp_path):
    """outcome_unknown：永久阻断自动重试，恢复返回 blocked。"""
    transport = base.FakeTransport(failure_kind="timeout")
    env = base._seed_env(tmp_path, transport=transport)
    item = base._item(env)
    r1 = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-ou"))
    assert r1.outcome == "outcome_unknown"
    calls_before = len(env["transport"].calls)
    r2 = env["orch"].recover_score_item(env["task"].task_id, item.item_id, "rec-ou")
    assert r2.outcome == "outcome_unknown_blocked"
    assert r2.error_code == RECOVERY_OUTCOME_UNKNOWN_BLOCKED
    assert len(env["transport"].calls) == calls_before


# ---------------- 调用后未知 -> outcome_unknown ---------------- #


def test_postcall_unknown_turns_outcome_unknown(tmp_path, monkeypatch):
    """dispatch 已标记但无响应事实：调用后状态不明 -> outcome_unknown，阻止盲目重试。"""
    env = base._seed_env(tmp_path)
    item = base._item(env)
    original_call = env["orch"]._gateway.call

    async def boom_call(request, payload):
        raise RuntimeError("process died during provider call")

    monkeypatch.setattr(env["orch"]._gateway, "call", boom_call)
    r1 = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-pc"))
    assert r1.outcome == "internal_error"
    # dispatch 已标记（Provider 已开始）
    attempts = env["attempt_store"].list_attempts(env["task"].task_id, item.item_id)
    assert attempts and attempts[0].status == "running"
    _stale_now(env)
    r2 = env["orch"].recover_score_item(env["task"].task_id, item.item_id, "rec-pc")
    assert r2.outcome == "outcome_unknown_blocked"
    assert r2.error_code == RECOVERY_OUTCOME_UNKNOWN_BLOCKED
    attempt_after = env["attempt_store"].get_attempt(env["task"].task_id, item.item_id, attempts[0].attempt_id)
    assert attempt_after is not None and attempt_after.status == "outcome_unknown"
    assert attempt_after.error is not None and attempt_after.error.error_code == "PROVIDER_OUTCOME_UNKNOWN"
    item_after = base._item(env)
    assert item_after.status == "failed"
    assert item_after.last_error.error_code == "PROVIDER_OUTCOME_UNKNOWN"


# ---------------- 响应事实恢复（不重复调用 Provider） ---------------- #


def test_snapshot_write_failure_recovered_from_fact(tmp_path, monkeypatch):
    """Provider 成功但 snapshot 写失败：恢复从响应事实补齐 snapshot/validation，不重复调用。"""
    base._fail_dir(monkeypatch, "snapshots")
    env = base._seed_env(tmp_path)
    item = base._item(env)
    r1 = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-sw"))
    assert r1.outcome == "internal_error"
    assert r1.error_code == ERR_EXEC_SNAPSHOT_WRITE_FAILED
    assert len(env["transport"].calls) == 1
    # 响应事实已保存
    fact = env["attempt_store"].get_response_fact(env["task"].task_id, item.item_id, r1.attempt_id)
    assert fact is not None and fact["attempt_id"] == r1.attempt_id
    monkeypatch.undo()  # 恢复写入
    calls_before = len(env["transport"].calls)
    r2 = env["orch"].recover_score_item(env["task"].task_id, item.item_id, "rec-sw")
    assert r2.outcome == "completed_from_facts"
    assert r2.error_code == RECOVERY_COMPLETED_FROM_FACTS
    # snapshot / validation 补齐且绑定正确
    snap = env["attempt_store"].get_snapshot(env["task"].task_id, item.item_id, r2.snapshot_id)
    assert snap is not None and snap.attempt_id == r1.attempt_id
    val = env["attempt_store"].get_validation(env["task"].task_id, item.item_id, r2.validation_id)
    assert val is not None and val.overall_status == "passed"
    env["attempt_store"].verify_fact_bindings(env["task"].task_id, item.item_id, r1.attempt_id)
    # attempt 发布成功终态；item 推进 review/pending
    attempt = env["attempt_store"].get_attempt(env["task"].task_id, item.item_id, r1.attempt_id)
    assert attempt is not None and attempt.status == "succeeded"
    item_after = base._item(env)
    assert item_after.current_stage == "review" and item_after.status == "pending"
    assert len(env["transport"].calls) == calls_before  # 恢复不重复调用


def test_validation_write_failure_recovered(tmp_path, monkeypatch):
    """snapshot 成功但 validation 写失败：恢复补齐 validation。"""
    base._fail_dir(monkeypatch, "validations")
    env = base._seed_env(tmp_path)
    item = base._item(env)
    r1 = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-vw"))
    assert r1.outcome == "internal_error"
    assert r1.error_code == ERR_EXEC_VALIDATION_WRITE_FAILED
    assert env["attempt_store"].has_successful_snapshot(env["task"].task_id, item.item_id) is True
    monkeypatch.undo()
    r2 = env["orch"].recover_score_item(env["task"].task_id, item.item_id, "rec-vw")
    assert r2.outcome == "completed_from_facts"
    val = env["attempt_store"].get_validation(env["task"].task_id, item.item_id, r2.validation_id)
    assert val is not None and val.attempt_id == r1.attempt_id
    attempt = env["attempt_store"].get_attempt(env["task"].task_id, item.item_id, r1.attempt_id)
    assert attempt is not None and attempt.status == "succeeded"
    assert len(env["transport"].calls) == 1


def test_item_completion_failure_recovered_state_only(tmp_path, monkeypatch):
    """snapshot/validation 成功、item 推进失败：恢复只补齐 item 状态。"""
    env = base._seed_env(tmp_path)

    def _boom(*a, **k):
        raise PipelineTaskError("SIDECAR_INVALID_STATE_TRANSITION", "SIDECAR_INVALID_STATE_TRANSITION")

    monkeypatch.setattr(env["mgr"], "complete_scoring_item_stage", _boom)
    item = base._item(env)
    r1 = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-tf"))
    assert r1.outcome == "internal_error"
    assert r1.error_code == ERR_EXEC_ITEM_COMPLETE_FAILED
    assert env["attempt_store"].has_successful_snapshot(env["task"].task_id, item.item_id) is True
    monkeypatch.undo()
    r2 = env["orch"].recover_score_item(env["task"].task_id, item.item_id, "rec-tf")
    assert r2.outcome == "completed_from_facts"
    item_after = base._item(env)
    assert item_after.current_stage == "review" and item_after.status == "pending"
    assert len(env["transport"].calls) == 1


# ---------------- 已成功 / 幂等 / 并发 ---------------- #


def test_already_completed_short_circuit(tmp_path):
    """已成功结果短路，不重复调用。"""
    env = base._seed_env(tmp_path)
    item = base._item(env)
    _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-ok"))
    calls_before = len(env["transport"].calls)
    r = env["orch"].recover_score_item(env["task"].task_id, item.item_id, "rec-ok")
    assert r.outcome == "already_completed"
    assert r.error_code == RECOVERY_ALREADY_COMPLETED
    assert len(env["transport"].calls) == calls_before


def test_recovery_idempotent_same_request(tmp_path, monkeypatch):
    """相同 recovery_request_id 幂等：完成后再次恢复短路。"""
    base._fail_dir(monkeypatch, "snapshots")
    env = base._seed_env(tmp_path)
    item = base._item(env)
    r1 = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-idem"))
    assert r1.outcome == "internal_error"
    monkeypatch.undo()
    rr1 = env["orch"].recover_score_item(env["task"].task_id, item.item_id, "rec-idem")
    assert rr1.outcome == "completed_from_facts"
    rr2 = env["orch"].recover_score_item(env["task"].task_id, item.item_id, "rec-idem")
    assert rr2.outcome == "already_completed"
    assert len(env["transport"].calls) == 1


def test_concurrent_recovery_single_executor(tmp_path, monkeypatch):
    """并发双恢复：只允许一个执行者完成恢复，另一个短路/并发冲突。"""
    base._fail_dir(monkeypatch, "snapshots")
    env = base._seed_env(tmp_path)
    item = base._item(env)
    r1 = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-cc"))
    assert r1.outcome == "internal_error"
    monkeypatch.undo()
    results = []

    def rec():
        results.append(env["orch"].recover_score_item(env["task"].task_id, item.item_id, "rec-cc"))

    t1 = threading.Thread(target=rec)
    t2 = threading.Thread(target=rec)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    outcomes = sorted(r.outcome for r in results)
    assert outcomes.count("completed_from_facts") == 1
    other = [r for r in results if r.outcome != "completed_from_facts"][0]
    assert other.outcome in ("already_completed", "concurrent_request")
    assert len(env["transport"].calls) == 1
    item_after = base._item(env)
    assert item_after.current_stage == "review" and item_after.status == "pending"


# ---------------- 指纹矛盾 / 多 item / 冻结配置 / 敏感 ---------------- #


def _make_running_attempt(attempt_id, task, item, record):
    """构造合成 running attempt（与 orchestrator 结构一致；input_fingerprint 默认与 item 一致）。"""
    return ScoreAttempt(
        contract_version="score-attempt-review/v1",
        schema_version="score-attempt/v1",
        attempt_id=attempt_id,
        attempt_number=1,
        task_id=task.task_id,
        item_id=item.item_id,
        submission_id=record.submission_id,
        package_id=item.package_id,
        package_revision=item.package_revision,
        evidence_manifest_sha256=item.manifest_sha256,
        input_fingerprint=item.input_fingerprint,
        provider_id="provider_demo_001",
        provider_config_version="cfg-001",
        model_id="model_demo_v1",
        model_capability_version="cap-demo-001",
        provider_request_id=f"req-{attempt_id}",
        scoring_policy_version="policy-001",
        rubric_version="rubric-001",
        prompt_version="prompt-001",
        response_schema_version="score-response-v1",
        status="running",
        created_at=base.T0,
        created_by={"actor_type": "orchestrator", "actor_id": "scoring-pipeline"},
        started_at=base.T0,
        completed_at=None,
        duration_ms=None,
        error=None,
        result_snapshot_ref=None,
        validation_ref=None,
        request_hash="b" * 64,
        response_hash=None,
    )


def test_fingerprint_mismatch_blocked(tmp_path):
    """attempt 与 task/item/evidence 指纹不一致时阻断恢复。"""
    env = base._seed_env(tmp_path)
    item = base._item(env)
    task = env["store"].load_task(env["task"].task_id)
    mgr = env["mgr"]
    mgr.start_item_stage(env["task"].task_id, item.item_id,
                         expected_task_revision=task.revision,
                         expected_item_revision=item.item_revision)
    record = env["reg"].get_evidence_record(item.package_id)
    bad_attempt = _make_running_attempt("atp-mismatch-001", env["task"], item, record)
    bad_attempt = bad_attempt.model_copy(update={"input_fingerprint": "f" * 64})
    env["attempt_store"].create_attempt(env["task"].task_id, item.item_id, bad_attempt)
    r = env["orch"].recover_score_item(env["task"].task_id, item.item_id, "rec-fm")
    assert r.outcome == "fact_binding_mismatch"
    assert r.error_code == RECOVERY_FACT_BINDING_MISMATCH
    assert len(env["transport"].calls) == 0


def test_multi_item_only_target_recovered(tmp_path, monkeypatch):
    """多 item 任务：恢复只作用于目标 item，其他 item 不受影响。"""
    base._fail_dir(monkeypatch, "snapshots")
    env = base._seed_env(tmp_path, item_count=2)
    task = env["task"]
    item_a = env["store"].load_item(task.task_id, task.item_index[0].item_id)
    item_b = env["store"].load_item(task.task_id, task.item_index[1].item_id)
    r1 = _run(env["orch"].execute_score_item(task.task_id, item_a.item_id, "exec-mi"))
    assert r1.outcome == "internal_error"
    monkeypatch.undo()
    r2 = env["orch"].recover_score_item(task.task_id, item_a.item_id, "rec-mi")
    assert r2.outcome == "completed_from_facts"
    a_after = env["store"].load_item(task.task_id, item_a.item_id)
    b_after = env["store"].load_item(task.task_id, item_b.item_id)
    assert a_after.current_stage == "review" and a_after.status == "pending"
    assert b_after.current_stage == "score" and b_after.status == "pending"  # 未受影响
    assert len(env["transport"].calls) == 1


def test_frozen_configuration_unchanged_after_recovery(tmp_path, monkeypatch):
    """恢复前后冻结配置（configuration_snapshot/fingerprint）不变。"""
    base._fail_dir(monkeypatch, "snapshots")
    env = base._seed_env(tmp_path)
    item = base._item(env)
    r1 = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-fc"))
    assert r1.outcome == "internal_error"
    monkeypatch.undo()
    before = env["store"].load_task(env["task"].task_id)
    snap_before = before.configuration_snapshot.model_dump(mode="json")
    fp_before = before.configuration_fingerprint
    env["orch"].recover_score_item(env["task"].task_id, item.item_id, "rec-fc")
    after = env["store"].load_task(env["task"].task_id)
    assert after.configuration_snapshot.model_dump(mode="json") == snap_before
    assert after.configuration_fingerprint == fp_before


def test_recovery_sensitive_no_leak(tmp_path, monkeypatch):
    """敏感正文不进入恢复错误、事件与返回值。"""
    base._fail_dir(monkeypatch, "snapshots")
    env = base._seed_env(tmp_path)
    item = base._item(env)
    r1 = _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-sec"))
    assert r1.outcome == "internal_error"
    monkeypatch.undo()
    r = env["orch"].recover_score_item(env["task"].task_id, item.item_id, "rec-sec")
    assert r.outcome == "completed_from_facts"
    dump = r.model_dump(mode="json")
    import json as _json
    text = _json.dumps(dump)
    for bad in ("system_prompt", "user_prompt", "MOCK_CRED", "http://", "bearer", "sk-", "api_key"):
        assert bad not in text, bad
    # 事件与响应事实文件不含 prompt/凭据正文
    for f in env["attempt_store"].root.rglob("*.ndjson"):
        low = f.read_text(encoding="utf-8").lower()
        for bad in ("synthetic scoring standard", "mock_cred", "bearer", "http://"):
            assert bad not in low, (f, bad)
    for f in (env["attempt_store"].root / "tasks").rglob("*.json"):
        low = f.read_text(encoding="utf-8").lower()
        assert "synthetic scoring standard" not in low
        assert "mock_cred" not in low


def test_recovery_never_calls_provider(tmp_path):
    """恢复路径任意分支 Provider 调用计数保持为 0（新增调用）。"""
    env = base._seed_env(tmp_path)
    item = base._item(env)
    # 无 attempt 场景
    calls0 = len(env["transport"].calls)
    r = env["orch"].recover_score_item(env["task"].task_id, item.item_id, "rec-z1")
    assert r.outcome == "not_applicable"
    assert len(env["transport"].calls) == calls0
    # 成功短路场景
    _run(env["orch"].execute_score_item(env["task"].task_id, item.item_id, "exec-z1"))
    calls1 = len(env["transport"].calls)
    env["orch"].recover_score_item(env["task"].task_id, item.item_id, "rec-z2")
    assert len(env["transport"].calls) == calls1
    # 失败待批准场景
    transport = base.FakeTransport(failure_kind="quota_exhausted")
    env2 = base._seed_env(tmp_path / "env2", transport=transport)
    item2 = base._item(env2)
    _run(env2["orch"].execute_score_item(env2["task"].task_id, item2.item_id, "exec-z2"))
    calls2 = len(env2["transport"].calls)
    env2["orch"].recover_score_item(env2["task"].task_id, item2.item_id, "rec-z3")
    assert len(env2["transport"].calls) == calls2
