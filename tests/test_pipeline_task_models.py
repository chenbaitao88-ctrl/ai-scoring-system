"""
Phase 11C-2a（fix-2）：PipelineTask Pydantic 模型合成测试。

所有测试数据均为脱敏、虚构的合成数据，不包含任何真实学生材料。

覆盖（fix-2 新增）：
- 计数与 item_index 聚合逐项对账（含"总数相等但分类错配"失败测试）。
- 终态（completed/completed_with_errors/failed/cancelled）时间规则正反例。
- 状态与计数关系（completed/completed_with_errors/pending/running）。
- PipelineItem current_stage 限定 import/validate、output 成对、completed 必须输出。
- ID 强化（UUID 校验；拒绝张三/URL/路径/空白/自由文本）。
- canonical helper 输入严格校验、configuration_fingerprint 持久化一致性。
- 契约字段补齐（StageSummary 依赖/时间/计数/阻断码/revision、ResumeRequest/Decision
  dry_run 与资格三分类、PipelineEvent 稳定事件类型）。
- PipelineTransaction 强化（负数 revision、非法 item_id、事件引用状态规则、old/new 配对）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from models.pipeline_task import (
    PipelineConfigurationSnapshot,
    PipelineError,
    PipelineErrorSummary,
    PipelineEvent,
    PipelineItem,
    PipelineItemIndexEntry,
    PipelineStageSummary,
    PipelineTask,
    PipelineTransaction,
    ResumeDecision,
    ResumeEligibleItem,
    ResumeProtectedItem,
    ResumeRejectedItem,
    ResumeRequest,
    canonical_json_bytes,
    compute_item_input_fingerprint,
    compute_task_idempotency,
    sha256_canonical,
)

UTC = timezone.utc
T0 = datetime(2026, 8, 6, 8, 0, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=1)
SHA256 = "a" * 64
FP = "f" * 64
IDEM = "b" * 64
CONFIG_FP = "c" * 64
TASK_ID = "00000000-0000-0000-0000-000000000001"
ITEM_ID = "00000000-0000-0000-0000-000000000101"
ITEM_ID2 = "00000000-0000-0000-0000-000000000102"
EVENT_ID = "00000000-0000-0000-0000-000000000201"
REQUEST_ID = "00000000-0000-0000-0000-000000000301"
DECISION_ID = "00000000-0000-0000-0000-000000000401"
TXN_ID = "00000000-0000-0000-0000-000000000501"


# ---------------------------------------------------------------- fixtures


def config_snapshot(**overrides):
    data = {
        "evidence_contract_version": "evidence-package/v1.1",
        "validator_version": "1.0.0",
    }
    data.update(overrides)
    return data


def _cfg_fp(snap: dict) -> str:
    return sha256_canonical(snap)


def item_index_entry(**overrides):
    data = {
        "item_id": ITEM_ID,
        "package_id": "pkg-demo-001",
        "package_revision": 1,
        "status": "pending",
        "current_stage": "import",
        "input_fingerprint": FP,
        "item_revision": 1,
        "evidence_level": "sufficient",
        "evidence_level": "sufficient",
    }
    data.update(overrides)
    return data


def stage_summaries(**overrides):
    data = [
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
    for i, upd in overrides.get("updates", {}).items():
        data[i].update(upd)
    return data


def task(**overrides):
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
        "configuration_fingerprint": _cfg_fp(snap),
        "item_index": [item_index_entry()],
        "stage_summaries": stage_summaries(),
        "error_summary": {"count": 0, "codes": []},
        "last_event_sequence": 0,
        "revision": 1,
        "idempotency_key": IDEM,
        "idempotency_payload_sha256": SHA256,
    }
    data.update(overrides)
    return data


def item(**overrides):
    data = {
        "contract_version": "pipeline-item/v1",
        "item_id": ITEM_ID,
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
        "item_revision": 1,
        "evidence_level": "sufficient",
        "evidence_level": "sufficient",
    }
    data.update(overrides)
    return data


def completed_item(**overrides):
    data = item(
        status="completed",
        output_ref="outputs/result.json",
        output_sha256=SHA256,
    )
    data.update(overrides)
    return data


def event(**overrides):
    data = {
        "contract_version": "pipeline-task/v1",
        "event_id": EVENT_ID,
        "task_id": TASK_ID,
        "sequence": 1,
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
    return data


def request(**overrides):
    data = {
        "contract_version": "pipeline-task/v1",
        "request_id": REQUEST_ID,
        "task_id": TASK_ID,
        "requested_at": T0,
        "mode": "resume_pending",
        "expected_revision": 1,
        "expected_item_revisions": {ITEM_ID: 1},
        "item_ids": [ITEM_ID],
        "reason_code": "stale_running_recovery",
        "dry_run": False,
    }
    data.update(overrides)
    return data


def decision(**overrides):
    data = {
        "contract_version": "pipeline-task/v1",
        "decision_id": DECISION_ID,
        "request_id": REQUEST_ID,
        "task_id": TASK_ID,
        "decided_at": T1,
        "approved": True,
        "task_revision_before": 1,
        "eligible_items": [{"item_id": ITEM_ID, "resume_from_stage": "validate", "next_attempt_number": 2, "reason_code": "retryable"}],
        "protected_items": [],
        "rejected_items": [],
        "decision_error_codes": [],
        "dry_run": False,
    }
    data.update(overrides)
    return data


def transaction(**overrides):
    data = {
        "transaction_schema_version": "pipeline-transaction/v1",
        "transaction_id": TXN_ID,
        "task_id": TASK_ID,
        "status": "prepared",
        "expected_task_revision": 1,
        "expected_item_revisions": {ITEM_ID: 1},
        "target_event_sequence": 1,
        "old_snapshot_refs": ["items/item-00000000-0000-0000-0000-000000000101.json"],
        "new_snapshot_refs": ["items/item-00000000-0000-0000-0000-000000000101.json"],
        "event_ref": None,
        "created_at": T0,
        "updated_at": T0,
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------- 合法示例


def test_all_model_valid_synthetic_examples():
    assert PipelineTask(**task()).task_type == "evidence_preparation_pipeline"
    assert PipelineItem(**item()).max_attempts == 3
    assert PipelineEvent(**event()).sequence == 1
    assert ResumeRequest(**request()).mode == "resume_pending"
    assert ResumeDecision(**decision()).approved is True
    assert PipelineTransaction(**transaction()).status == "prepared"
    assert PipelineConfigurationSnapshot(**config_snapshot()).validator_version == "1.0.0"
    assert PipelineErrorSummary(**{"count": 0, "codes": []}).count == 0
    assert PipelineItemIndexEntry(**item_index_entry()).item_id == ITEM_ID
    assert PipelineStageSummary(**stage_summaries()[0]).depends_on == []
    assert PipelineError(error_code="E", message_key="E", retryable=True, stage="import").retryable is True
    assert ResumeEligibleItem(**{"item_id": ITEM_ID, "resume_from_stage": "validate", "next_attempt_number": 1, "reason_code": "r"}).next_attempt_number == 1
    assert ResumeProtectedItem(**{"item_id": ITEM_ID, "reason_code": "completed"}).reason_code == "completed"
    assert ResumeRejectedItem(**{"item_id": ITEM_ID, "error_codes": ["X"]}).error_codes == ["X"]


# ---------------------------------------------------------------- 结构拒绝


def test_extra_fields_rejected():
    with pytest.raises(ValidationError):
        PipelineTask(**task(extra_field=1))
    with pytest.raises(ValidationError):
        PipelineItem(**item(extra_field=1))
    with pytest.raises(ValidationError):
        PipelineTransaction(**transaction(extra_field=1))
    with pytest.raises(ValidationError):
        ResumeDecision(**decision(extra_field=1))


def test_naive_and_non_utc_time_rejected():
    with pytest.raises(ValidationError):
        PipelineTask(**task(created_at=datetime(2026, 8, 6, 8, 0, 0)))  # naive
    with pytest.raises(ValidationError):
        PipelineTask(**task(created_at=datetime(2026, 8, 6, 16, 0, 0, tzinfo=timezone(timedelta(hours=8)))))
    with pytest.raises(ValidationError):
        PipelineItem(**item(created_at=datetime(2026, 8, 6, 8, 0, 0)))


def test_negative_counts_and_revision_rejected():
    with pytest.raises(ValidationError):
        PipelineTask(**task(total_items=-1))
    with pytest.raises(ValidationError):
        PipelineTask(**task(revision=0))
    with pytest.raises(ValidationError):
        PipelineItem(**item(attempt_count=-1))


# ---------------------------------------------------------------- 计数对账


def test_count_invariant_total_matches_sum():
    with pytest.raises(ValidationError):
        t = task()
        t["total_items"] = 1
        t["pending_items"] = 2  # 六者和=2 != total=1
        PipelineTask(**t)


def test_count_reconciliation_with_item_index():
    """六类计数必须与 item_index.status 聚合逐项一致（总数相等但分类错配也拒绝）。"""
    # 总数相等但分类错配：item_index 是 completed，计数却写 pending
    t = task()
    t["item_index"] = [item_index_entry(status="completed")]
    t["status"] = "completed"
    t["started_at"] = T0
    t["completed_at"] = T1
    t["total_items"] = 1
    t["pending_items"] = 1  # 错配：应为 0（item 实际是 completed）
    t["completed_items"] = 0
    t["stage_summaries"][0] = {**stage_summaries()[0], "status": "completed", "completed_items": 1, "pending_items": 0, "started_at": T0, "completed_at": T1, "revision": 2}
    with pytest.raises(ValidationError):
        PipelineTask(**t)


def test_count_reconciliation_correct_match():
    t = task()
    t["item_index"] = [item_index_entry(status="completed")]
    t["status"] = "completed"
    t["started_at"] = T0
    t["completed_at"] = T1
    t["pending_items"] = 0
    t["completed_items"] = 1
    t["stage_summaries"][0] = {**stage_summaries()[0], "status": "completed", "completed_items": 1, "pending_items": 0, "started_at": T0, "completed_at": T1, "revision": 2}
    assert PipelineTask(**t).completed_items == 1


# ---------------------------------------------------------------- 唯一性


def test_item_index_unique():
    t = task()
    t["item_index"] = [item_index_entry(), item_index_entry()]
    with pytest.raises(ValidationError):
        PipelineTask(**t)


def test_package_revision_unique():
    t = task()
    t["item_index"] = [item_index_entry(), item_index_entry(item_id=ITEM_ID2)]
    with pytest.raises(ValidationError):
        PipelineTask(**t)


# ---------------------------------------------------------------- 阶段


def test_stage_order_fixed():
    t = task()
    t["stage_summaries"] = [
        {**stage_summaries()[1]}, {**stage_summaries()[0]},
        {**stage_summaries()[2]}, {**stage_summaries()[3]}, {**stage_summaries()[4]},
    ]
    with pytest.raises(ValidationError):
        PipelineTask(**t)


def test_stage_depends_on_must_precede():
    t = task()
    t["stage_summaries"][1] = {**stage_summaries()[1], "depends_on": ["validate"]}  # validate 依赖自身
    with pytest.raises(ValidationError):
        PipelineTask(**t)


def test_stage_summary_count_invariant():
    with pytest.raises(ValidationError):
        PipelineStageSummary(**{**stage_summaries()[0], "total_items": 2})  # 计数和=1 != 2


def test_execution_scope_fixed():
    t = task()
    t["execution_scope"] = ["import"]
    with pytest.raises(ValidationError):
        PipelineTask(**t)


def test_later_stages_cannot_fake_completion():
    t = task()
    t["stage_summaries"][2] = {**stage_summaries()[2], "status": "completed"}  # 伪造评分完成
    with pytest.raises(ValidationError):
        PipelineTask(**t)


def test_current_stage_must_be_in_scope():
    t = task()
    t["current_stage"] = "score"
    with pytest.raises(ValidationError):
        PipelineTask(**t)


# ---------------------------------------------------------------- 终态与状态关系


def test_terminal_time_rules_all_four_statuses():
    for status in ("completed", "completed_with_errors", "failed", "cancelled"):
        base = task(status=status, started_at=T0, completed_at=T1)
        if status == "completed":
            base["item_index"] = [item_index_entry(status="completed")]
            base["pending_items"] = 0
            base["completed_items"] = 1
            base["stage_summaries"][0] = {**stage_summaries()[0], "status": "completed", "completed_items": 1, "pending_items": 0, "started_at": T0, "completed_at": T1, "revision": 2}
        elif status == "completed_with_errors":
            base["item_index"] = [item_index_entry(status="failed")]
            base["pending_items"] = 0
            base["failed_items"] = 1
            base["stage_summaries"][0] = {**stage_summaries()[0], "status": "failed", "failed_items": 1, "pending_items": 0, "started_at": T0, "completed_at": T1, "revision": 2}
        # 终态缺 completed_at -> 拒绝
        bad = dict(base)
        bad["completed_at"] = None
        with pytest.raises(ValidationError):
            PipelineTask(**bad)
        # 终态正确 completed_at -> 通过
        assert PipelineTask(**base).status == status


def test_non_terminal_completed_at_rejected():
    for status in ("pending", "running", "paused"):
        base = task(status=status)
        if status == "running":
            base["started_at"] = T0
            base["current_stage"] = "import"
            base["item_index"] = [item_index_entry(status="running")]
            base["pending_items"] = 0
            base["running_items"] = 1
            base["stage_summaries"][0] = {**stage_summaries()[0], "status": "running", "pending_items": 0, "running_items": 1}
        elif status == "paused":
            base["started_at"] = T0
            base["paused_at"] = T0
            base["current_stage"] = "import"
        base["completed_at"] = T1  # 非终态不得有
        with pytest.raises(ValidationError):
            PipelineTask(**base)


def test_paused_requires_paused_at():
    t = task(status="paused", started_at=T0, paused_at=None)
    with pytest.raises(ValidationError):
        PipelineTask(**t)


def test_pending_no_started_at():
    t = task(started_at=T0)  # pending + started_at
    with pytest.raises(ValidationError):
        PipelineTask(**t)


def test_running_and_terminal_require_started_at():
    for status in ("running", "completed", "failed", "cancelled"):
        base = task(status=status, started_at=None)
        if status == "running":
            base["item_index"] = [item_index_entry(status="running")]
            base["pending_items"] = 0
            base["running_items"] = 1
            base["stage_summaries"][0] = {**stage_summaries()[0], "status": "running", "pending_items": 0, "running_items": 1}
        with pytest.raises(ValidationError):
            PipelineTask(**base)  # 缺 started_at


def test_completed_counts_must_be_zero():
    t = task(status="completed", started_at=T0, completed_at=T1)
    t["item_index"] = [item_index_entry(status="completed")]
    t["pending_items"] = 0
    t["completed_items"] = 1
    t["stage_summaries"][0] = {**stage_summaries()[0], "status": "completed", "completed_items": 1, "pending_items": 0, "started_at": T0, "completed_at": T1, "revision": 2}
    PipelineTask(**t)  # 合法
    t["failed_items"] = 1  # completed 不允许 failed
    with pytest.raises(ValidationError):
        PipelineTask(**t)


def test_completed_with_errors_requires_retained_errors():
    # pending/running=0 且至少一项 failed/skipped/manual_review>0
    t = task(status="completed_with_errors", started_at=T0, completed_at=T1)
    t["item_index"] = [item_index_entry(status="failed")]
    t["pending_items"] = 0
    t["failed_items"] = 1
    t["stage_summaries"][0] = {**stage_summaries()[0], "status": "failed", "failed_items": 1, "pending_items": 0, "started_at": T0, "completed_at": T1, "revision": 2}
    assert PipelineTask(**t).status == "completed_with_errors"
    # 无保留项 -> 拒绝
    t2 = task(status="completed_with_errors", started_at=T0, completed_at=T1)
    t2["item_index"] = [item_index_entry(status="completed")]
    t2["pending_items"] = 0
    t2["completed_items"] = 1
    t2["stage_summaries"][0] = {**stage_summaries()[0], "status": "completed", "completed_items": 1, "pending_items": 0, "started_at": T0, "completed_at": T1, "revision": 2}
    with pytest.raises(ValidationError):
        PipelineTask(**t2)


# ---------------------------------------------------------------- item_index 与 stage 静态规则（fix-3）


def test_item_index_stage_limited_to_scope():
    """task.json 精简 item_index 的 current_stage 也只能是 import/validate。"""
    with pytest.raises(ValidationError):
        PipelineTask(**task(item_index=[item_index_entry(current_stage="score")]))
    with pytest.raises(ValidationError):
        PipelineTask(**task(item_index=[item_index_entry(current_stage="review")]))
    with pytest.raises(ValidationError):
        PipelineTask(**task(item_index=[item_index_entry(current_stage="export")]))
    with pytest.raises(ValidationError):
        PipelineItemIndexEntry(**item_index_entry(current_stage="score"))
    assert PipelineItemIndexEntry(**item_index_entry(current_stage="validate")).current_stage == "validate"


def test_stage_depends_on_frozen_exact():
    """depends_on 必须严格等于冻结依赖表。"""
    # 缺失依赖：validate 无依赖
    t = task()
    t["stage_summaries"][1] = {**stage_summaries()[1], "depends_on": []}
    with pytest.raises(ValidationError):
        PipelineTask(**t)
    # 多余依赖：score 额外依赖 import
    t2 = task()
    t2["stage_summaries"][2] = {**stage_summaries()[2], "depends_on": ["validate", "import"]}
    with pytest.raises(ValidationError):
        PipelineTask(**t2)
    # 顺序错误：review 的依赖顺序颠倒
    t3 = task()
    t3["stage_summaries"][3] = {**stage_summaries()[3], "depends_on": ["score", "validate"]}
    with pytest.raises(ValidationError):
        PipelineTask(**t3)
    # 错误的较早依赖：export 依赖 validate 而非 review
    t4 = task()
    t4["stage_summaries"][4] = {**stage_summaries()[4], "depends_on": ["validate"]}
    with pytest.raises(ValidationError):
        PipelineTask(**t4)
    # import 不应有依赖（helper 默认即冻结表）
    t5 = task()
    t5["stage_summaries"][0] = {**stage_summaries()[0], "depends_on": ["import"]}
    with pytest.raises(ValidationError):
        PipelineTask(**t5)


def test_stage_status_time_rules():
    """阶段状态基础时间规则：仅阻止静态矛盾快照。"""
    # not_started 不得有时间
    with pytest.raises(ValidationError):
        PipelineStageSummary(**{**stage_summaries()[1], "started_at": T0})
    with pytest.raises(ValidationError):
        PipelineStageSummary(**{**stage_summaries()[1], "completed_at": T0})
    # pending 不得有 completed_at（允许 started_at 存在）
    with pytest.raises(ValidationError):
        PipelineStageSummary(**{**stage_summaries()[0], "completed_at": T0})
    PipelineStageSummary(**{**stage_summaries()[0], "started_at": T0})  # pending + started_at 合法
    # running 必须有 started_at、不得有 completed_at
    with pytest.raises(ValidationError):
        PipelineStageSummary(**{**stage_summaries()[0], "status": "running", "running_items": 1, "pending_items": 0})
    with pytest.raises(ValidationError):
        PipelineStageSummary(**{**stage_summaries()[0], "status": "running", "started_at": T0, "completed_at": T1, "running_items": 1, "pending_items": 0})
    PipelineStageSummary(**{**stage_summaries()[0], "status": "running", "started_at": T0, "running_items": 1, "pending_items": 0})
    # completed/failed/skipped 必须有 completed_at
    for st in ("completed", "failed", "skipped"):
        with pytest.raises(ValidationError):
            PipelineStageSummary(**{**stage_summaries()[0], "status": st, "started_at": T0, "completed_items": 1, "pending_items": 0})
        PipelineStageSummary(**{**stage_summaries()[0], "status": st, "started_at": T0, "completed_at": T1, "completed_items": 1, "pending_items": 0})
    # completed_at 早于 started_at
    with pytest.raises(ValidationError):
        PipelineStageSummary(**{**stage_summaries()[0], "status": "completed", "started_at": T1, "completed_at": T0, "completed_items": 1, "pending_items": 0})


# ---------------------------------------------------------------- item 范围


def test_item_stage_limited_to_scope():
    with pytest.raises(ValidationError):
        PipelineItem(**item(current_stage="score"))
    with pytest.raises(ValidationError):
        PipelineItem(**item(current_stage="export"))
    assert PipelineItem(**item(current_stage="validate")).current_stage == "validate"


def test_output_pair_rule():
    with pytest.raises(ValidationError):
        PipelineItem(**item(output_ref="outputs/x.json", output_sha256=None))  # 只有一个
    with pytest.raises(ValidationError):
        PipelineItem(**item(output_ref=None, output_sha256=SHA256))
    # 非 completed 允许全空
    assert PipelineItem(**item()).output_ref is None


def test_completed_item_requires_output():
    with pytest.raises(ValidationError):
        PipelineItem(**item(status="completed"))  # 缺输出
    with pytest.raises(ValidationError):
        PipelineItem(**item(status="completed", output_ref="outputs/x.json", output_sha256=None))
    ok = PipelineItem(**completed_item())
    assert ok.output_ref == "outputs/result.json" and ok.output_sha256 == SHA256


def test_heartbeat_only_for_running():
    with pytest.raises(ValidationError):
        PipelineItem(**item(status="pending", heartbeat_updated_at=T0))
    with pytest.raises(ValidationError):
        PipelineItem(**item(status="running", heartbeat_updated_at=None))
    assert PipelineItem(**item(status="running", heartbeat_updated_at=T0)).status == "running"


def test_completed_item_not_retryable():
    with pytest.raises(ValidationError):
        PipelineItem(**completed_item(retryable=True))


def test_attempt_limit():
    with pytest.raises(ValidationError):
        PipelineItem(**item(attempt_count=4))
    assert PipelineItem(**item(attempt_count=3)).attempt_count == 3


# ---------------------------------------------------------------- ID 强化


def test_uuid_ids_enforced():
    with pytest.raises(ValidationError):
        PipelineTask(**task(task_id="task-0001"))  # 非 UUID
    with pytest.raises(ValidationError):
        PipelineItem(**item(item_id="item-0001"))
    with pytest.raises(ValidationError):
        ResumeRequest(**request(request_id="request-1"))
    with pytest.raises(ValidationError):
        ResumeDecision(**decision(decision_id="d-1"))
    with pytest.raises(ValidationError):
        PipelineTransaction(**transaction(transaction_id="txn-1"))
    with pytest.raises(ValidationError):
        PipelineEvent(**event(event_id="e-1"))


def test_chinese_name_rejected_as_id():
    with pytest.raises(ValidationError):
        PipelineTask(**task(task_id="张三"))  # 中文姓名不是 UUID
    with pytest.raises(ValidationError):
        PipelineTask(**task(batch_id="张三的批次"))
    with pytest.raises(ValidationError):
        PipelineItem(**item(package_id="张三"))
    with pytest.raises(ValidationError):
        PipelineItem(**item(registration_record_id="张三"))
    with pytest.raises(ValueError):
        compute_item_input_fingerprint(package_id="张三", package_revision=1, manifest_sha256=SHA256,
                                       registration_record_id="record-0001", validation_id="validation-0001")


def test_url_and_path_rejected_as_id():
    with pytest.raises(ValidationError):
        PipelineTask(**task(batch_id="https://example.com/1"))
    with pytest.raises(ValidationError):
        PipelineItem(**item(package_id="X:/Profiles/demo"))
    with pytest.raises(ValidationError):
        PipelineItem(**item(package_id="a/b"))
    with pytest.raises(ValidationError):
        PipelineItem(**item(package_id="a b"))  # 空白自由文本


def test_sha256_format():
    with pytest.raises(ValidationError):
        PipelineItem(**item(manifest_sha256="A" * 64))
    with pytest.raises(ValidationError):
        PipelineTask(**task(idempotency_key="not-a-hash"))


def test_output_ref_safe_relative():
    with pytest.raises(ValidationError):
        PipelineItem(**item(status="completed", output_ref="/abs/path.json", output_sha256=SHA256))
    with pytest.raises(ValidationError):
        PipelineItem(**item(status="completed", output_ref="../escape.json", output_sha256=SHA256))


# ---------------------------------------------------------------- transaction 强化


def test_transaction_expected_revisions_negative_rejected():
    with pytest.raises(ValidationError):
        PipelineTransaction(**transaction(expected_item_revisions={ITEM_ID: 0}))
    with pytest.raises(ValidationError):
        PipelineTransaction(**transaction(expected_item_revisions={ITEM_ID: -1}))


def test_transaction_expected_revisions_invalid_item_id():
    with pytest.raises(ValidationError):
        PipelineTransaction(**transaction(expected_item_revisions={"item-0001": 1}))  # 非 UUID
    with pytest.raises(ValidationError):
        PipelineTransaction(**transaction(expected_item_revisions={"张三": 1}))


def test_transaction_event_ref_state_rules():
    # event_appended/committed 必须 event_ref
    with pytest.raises(ValidationError):
        PipelineTransaction(**transaction(status="event_appended", event_ref=None))
    with pytest.raises(ValidationError):
        PipelineTransaction(**transaction(status="committed", event_ref=None))
    # prepared/snapshots_published 不得有 event_ref
    with pytest.raises(ValidationError):
        PipelineTransaction(**transaction(status="prepared", event_ref="events.ndjson"))
    ok = PipelineTransaction(**transaction(status="committed", event_ref="events.ndjson"))
    assert ok.status == "committed"


def test_transaction_snapshot_refs_pairing():
    with pytest.raises(ValidationError):
        PipelineTransaction(**transaction(
            old_snapshot_refs=["items/a.json"],
            new_snapshot_refs=["items/a.json", "items/b.json"],  # 数量不配对
        ))
    with pytest.raises(ValidationError):
        PipelineTransaction(**transaction(old_snapshot_refs=["/abs/a.json"]))
    with pytest.raises(ValidationError):
        PipelineTransaction(**transaction(new_snapshot_refs=["../x.json"]))


# ---------------------------------------------------------------- canonicalization


def test_canonical_json_stable():
    a = {"b": 1, "a": [1, 2], "中文": "值"}
    b = {"a": [1, 2], "b": 1, "中文": "值"}
    assert canonical_json_bytes(a) == canonical_json_bytes(b)
    assert len(sha256_canonical(a)) == 64


def test_fingerprint_changes_with_fields():
    kw = dict(package_id="pkg-demo-001", package_revision=1, manifest_sha256=SHA256,
              registration_record_id="record-0001", validation_id="validation-0001")
    fp1 = compute_item_input_fingerprint(**kw)
    fp2 = compute_item_input_fingerprint(**{**kw, "package_revision": 2})
    fp3 = compute_item_input_fingerprint(**{**kw, "validation_id": "validation-0002"})
    assert fp1 != fp2 and fp1 != fp3
    assert len(fp1) == 64


def test_fingerprint_strict_input_validation():
    kw = dict(package_id="pkg-demo-001", package_revision=1, manifest_sha256=SHA256,
              registration_record_id="record-0001", validation_id="validation-0001")
    with pytest.raises(ValueError):
        compute_item_input_fingerprint(**{**kw, "package_revision": 0})
    with pytest.raises(ValueError):
        compute_item_input_fingerprint(**{**kw, "package_revision": -1})
    with pytest.raises(ValueError):
        compute_item_input_fingerprint(**{**kw, "manifest_sha256": "A" * 64})
    with pytest.raises(ValueError):
        compute_item_input_fingerprint(**{**kw, "package_id": "张三"})


def test_task_idempotency_strict_input_validation():
    kw = dict(
        contract_version="pipeline-task/v1",
        task_type="evidence_preparation_pipeline",
        batch_id="batch-demo-001",
        execution_scope=["import", "validate"],
        items=[("pkg-a", 1, FP)],
        configuration_fingerprint=CONFIG_FP,
    )
    with pytest.raises(ValueError):
        compute_task_idempotency(**{**kw, "contract_version": "other/v1"})
    with pytest.raises(ValueError):
        compute_task_idempotency(**{**kw, "task_type": "evidence_scoring_pipeline"})
    with pytest.raises(ValueError):
        compute_task_idempotency(**{**kw, "execution_scope": ["import"]})
    with pytest.raises(ValueError):
        compute_task_idempotency(**{**kw, "configuration_fingerprint": "bad"})
    with pytest.raises(ValueError):
        compute_task_idempotency(**{**kw, "items": [("pkg-a", 0, FP)]})  # revision 非正
    with pytest.raises(ValueError):
        compute_task_idempotency(**{**kw, "items": [("pkg-a", 1, "not-hash")]})


def test_task_idempotency_order_independent():
    items_a = [("pkg-b", 1, FP), ("pkg-a", 1, FP), ("pkg-a", 2, FP)]
    items_b = [("pkg-a", 2, FP), ("pkg-a", 1, FP), ("pkg-b", 1, FP)]
    kw = dict(
        contract_version="pipeline-task/v1",
        task_type="evidence_preparation_pipeline",
        batch_id="batch-demo-001",
        execution_scope=["import", "validate"],
        configuration_fingerprint=CONFIG_FP,
    )
    assert compute_task_idempotency(items=items_a, **kw) == compute_task_idempotency(items=items_b, **kw)


def test_task_idempotency_duplicate_revision_rejected():
    with pytest.raises(ValueError):
        compute_task_idempotency(
            contract_version="pipeline-task/v1", task_type="evidence_preparation_pipeline",
            batch_id="batch-demo-001", execution_scope=["import", "validate"],
            items=[("pkg-a", 1, FP), ("pkg-a", 1, FP)], configuration_fingerprint=CONFIG_FP,
        )


def test_nan_infinity_rejected():
    with pytest.raises(ValueError):
        canonical_json_bytes({"x": float("nan")})
    with pytest.raises(ValueError):
        sha256_canonical({"x": float("inf")})


def test_configuration_fingerprint_persisted_and_consistent():
    t = PipelineTask(**task())
    assert t.configuration_fingerprint == sha256_canonical(t.configuration_snapshot.model_dump())
    # 指纹与快照不一致 -> 拒绝
    bad = task(configuration_fingerprint="d" * 64)
    with pytest.raises(ValidationError):
        PipelineTask(**bad)
    # 快照变化后指纹不更新 -> 拒绝
    t2 = task()
    t2["configuration_snapshot"] = config_snapshot(validator_version="2.0.0")
    with pytest.raises(ValidationError):
        PipelineTask(**t2)


# ---------------------------------------------------------------- 契约对象字段


def test_stage_summary_contract_fields():
    s = PipelineStageSummary(**stage_summaries()[1])
    assert s.depends_on == ["import"]
    assert s.started_at is None and s.completed_at is None
    assert s.total_items == 0 and s.revision == 1
    assert s.blocking_error_codes == []
    # 阶段计数不变量
    with pytest.raises(ValidationError):
        PipelineStageSummary(**{**stage_summaries()[1], "total_items": 5})


def test_resume_request_contract_fields():
    r = ResumeRequest(**request())
    assert r.contract_version == "pipeline-task/v1"
    assert r.mode == "resume_pending"
    assert r.expected_revision == 1
    assert r.expected_item_revisions == {ITEM_ID: 1}
    assert r.item_ids == [ITEM_ID]
    assert r.reason_code == "stale_running_recovery"
    assert r.dry_run is False
    # 无绕过字段
    with pytest.raises(ValidationError):
        ResumeRequest(**request(force=True))
    # 非法 item_ids
    with pytest.raises(ValidationError):
        ResumeRequest(**request(item_ids=["item-0001"]))


def test_resume_decision_contract_fields():
    d = ResumeDecision(**decision())
    assert d.decision_id == DECISION_ID and d.request_id == REQUEST_ID and d.task_id == TASK_ID
    assert d.approved is True and d.task_revision_before == 1 and d.dry_run is False
    assert d.eligible_items[0].item_id == ITEM_ID
    assert d.protected_items == [] and d.rejected_items == []
    # approved 但无 eligible -> 拒绝
    with pytest.raises(ValidationError):
        ResumeDecision(**decision(approved=True, eligible_items=[]))
    # denied 允许空 eligible
    assert ResumeDecision(**decision(approved=False, eligible_items=[])).approved is False


def test_event_contract_fields_and_stable_types():
    e = PipelineEvent(**event())
    assert e.contract_version == "pipeline-task/v1"
    assert e.revision_before == 0 and e.revision_after == 1
    assert e.metadata == {}
    # 非法事件类型
    with pytest.raises(ValidationError):
        PipelineEvent(**event(event_type="made_up_type"))
    # 稳定类型合法
    for et in ("task_created", "item_completed", "resume_decided", "successful_output_reused"):
        assert PipelineEvent(**event(event_type=et)).event_type == et
    # revision_after < before 拒绝
    with pytest.raises(ValidationError):
        PipelineEvent(**event(revision_before=3, revision_after=1))


# ---------------------------------------------------------------- 敏感扫描


def test_synthetic_data_no_sensitive_values():
    dumped = json.dumps({
        "task": task(),
        "item": item(),
        "completed_item": completed_item(),
        "event": event(),
        "request": request(),
        "decision": decision(),
        "transaction": transaction(),
    }, ensure_ascii=False, default=str)
    patterns = [
        r"1[3-9]\d{9}", r"https?://", r"school", r"姓名", r"网盘", r"D:/", r"C:\\Users",
    ]
    for pat in patterns:
        assert not re.search(pat, dumped), f"合成数据含敏感模式: {pat}"


# ---------------------------------------------------------------- 11C-2c-prerequisite-fix-1：item evidence_level 路由


def test_item_evidence_level_accepted_values():
    """PipelineItem / PipelineItemIndexEntry 接受 sufficient/limited/manual_only。"""
    for lv in ("sufficient", "limited", "manual_only"):
        assert PipelineItem(**item(evidence_level=lv)).evidence_level == lv
        assert PipelineItemIndexEntry(**item_index_entry(evidence_level=lv)).evidence_level == lv


def test_item_evidence_level_rejects_insufficient_and_unknown():
    """PipelineItem 拒绝 insufficient、未知值（Literal 校验）。"""
    with pytest.raises(ValidationError):
        PipelineItem(**item(evidence_level="insufficient"))
    with pytest.raises(ValidationError):
        PipelineItem(**item(evidence_level="not_a_level"))
    with pytest.raises(ValidationError):
        PipelineItemIndexEntry(**item_index_entry(evidence_level="insufficient"))


def test_item_evidence_level_required_field():
    """PipelineItem 缺失 evidence_level 字段拒绝（必填）。"""
    d = item()
    del d["evidence_level"]
    with pytest.raises(ValidationError):
        PipelineItem(**d)
    d2 = item_index_entry()
    del d2["evidence_level"]
    with pytest.raises(ValidationError):
        PipelineItemIndexEntry(**d2)


def test_task_evidence_level_reconciliation_models():
    """task.item_index 与 item 快照 evidence_level 模型层一致（task 级构造允许匹配组合）。"""
    t = task()
    t["item_index"] = [item_index_entry(evidence_level="limited")]
    t["configuration_fingerprint"] = _cfg_fp(config_snapshot())  # 保持不变
    # item_index 与计数一致时模型层通过（evidence_level 一致性由 store reconciliation 保证）
    assert PipelineTask(**t).item_index[0].evidence_level == "limited"
