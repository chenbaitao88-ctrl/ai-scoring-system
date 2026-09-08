"""
ScoringBatchOrchestrator（Phase 11E-3a）：PipelineTask 批量评分编排。

把 11E-2b-2 单 item 执行与 11E-2c 恢复扩展到整个 scoring_pipeline 任务：
多 item 有界并发、错峰推进、失败隔离、暂停/取消响应、CAS 有限重试与统计聚合。

- 只执行 task_type == "scoring_pipeline"；Provider/model/profile/rubric/prompt/schema
  一律来自任务冻结配置；调用方不得指定 item 分数/attempt/Provider/证据内容。
- 并发上限收敛到任务冻结 task.concurrency（传入更高并发被收敛，行为固定）。
- 调度分类：score/pending 执行；score/running 不重复启动（stale 走恢复）；
  score/failed 走恢复判断（retryable 仅待批准、outcome_unknown 阻断）；
  review/export/pending 视为 score 已完成跳过；completed 短路；manual_review/skipped 保留。
- 有界并发（asyncio.Semaphore）；同 item 单执行者（execute CAS 保护）；
  暂停/取消后不再启动新 item；在途 item 按事实完成或 outcome_unknown。
- execution_request_id 由 batch_execution_id + item_id 稳定派生，批次重放幂等。
- 已成功快照永不覆盖；单项失败不影响其他 item；不因单项失败回滚整批。
- 本阶段只编排 score 阶段：score 成功后 item 到 review/pending 即成功，
  不伪造 review/export 完成；task 停留在 review 是正确结果。
"""
from __future__ import annotations

import asyncio
import hashlib
from collections import Counter
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from services.score_pipeline_protocols import PipelineTaskItemLookup
from services.scoring_pipeline_orchestrator import (
    ERR_EXEC_ITEM_START_FAILED,
    ScoringPipelineOrchestrator,
)

# CAS/状态刷新有限重试次数（重新读取后重新判断，不产生重复 Provider 调用）
_MAX_REFRESH_RETRIES = 2

BatchOutcome = Literal[
    "completed", "completed_with_errors", "paused", "cancelled",
    "running", "invalid_task",
]


class BatchScoringExecutionResult(BaseModel):
    """批量评分执行安全摘要（不含 Prompt/证据正文/模型输出/Key/endpoint/绝对路径）。"""

    model_config = ConfigDict(extra="forbid")

    batch_execution_id: str
    task_id: str
    outcome: BatchOutcome
    task_status: str
    current_stage: Optional[str] = None
    task_revision: int = Field(ge=1)
    total_items: int = Field(ge=0)
    started: int = Field(ge=0)      # 进入执行（Provider 调用或尝试）
    succeeded: int = Field(ge=0)    # item 评分成功
    skipped: int = Field(ge=0)      # 调度跳过（已成功/review/manual/skipped/暂停取消）
    blocked: int = Field(ge=0)      # dry-run 阻断或执行阻断
    recovery_required: int = Field(ge=0)  # 需恢复判定（running/failed/outcome_unknown）
    pending_remaining: bool
    error_code_counts: Dict[str, int] = Field(default_factory=dict)


class ScoringBatchOrchestrator:
    """scoring_pipeline 任务批量评分编排器。"""

    def __init__(
        self,
        orchestrator: ScoringPipelineOrchestrator,
        task_item_lookup: PipelineTaskItemLookup,
    ) -> None:
        self._orch = orchestrator
        self._task_item = task_item_lookup

    # ---------------- 主入口 ---------------- #

    async def execute_scoring_task(
        self,
        task_id: str,
        batch_execution_id: str,
        concurrency: Optional[int] = None,
    ) -> BatchScoringExecutionResult:
        """批量执行 scoring task 的全部可评分 item（有界并发）。

        - concurrency=None 用冻结 task.concurrency；传入更高值收敛到冻结上限。
        - 暂停/取消（paused/cancelled）后不再启动新 item。
        """
        task = self._task_item.get_task(task_id)
        if task is None or task.task_type != "scoring_pipeline":
            return self._result(batch_execution_id, task_id,
                                "invalid_task", task_status=task.status if task else "unknown",
                                current_stage=task.current_stage if task else None,
                                task_revision=task.revision if task else 1,
                                total_items=len(task.item_index) if task else 0,
                                pending_remaining=True)
        frozen = task.concurrency
        limit = concurrency if concurrency is not None else frozen
        if limit > frozen:
            limit = frozen  # 收敛到冻结上限（明确行为）
        if limit < 1:
            limit = 1
        items = [self._task_item.get_item(task_id, e.item_id) for e in task.item_index]
        items = [i for i in items if i is not None]

        sem = asyncio.Semaphore(limit)

        async def run_one(item: Any) -> "ItemBatchOutcome":
            # 启动前检查：暂停/取消不再启动新 item
            if self._is_paused_or_cancelled(task_id):
                return ItemBatchOutcome(kind="skipped", code="TASK_PAUSED_OR_CANCELLED")
            async with sem:
                if self._is_paused_or_cancelled(task_id):
                    return ItemBatchOutcome(kind="skipped", code="TASK_PAUSED_OR_CANCELLED")
                return await self._execute_one(task_id, item, batch_execution_id)

        outcomes = await asyncio.gather(*[run_one(i) for i in items])
        final_task = self._task_item.get_task(task_id)
        return self._aggregate(batch_execution_id, task_id, final_task, items, outcomes)

    # ---------------- 单 item 调度 ---------------- #

    async def _execute_one(self, task_id: str, item: Any, batch_execution_id: str) -> "ItemBatchOutcome":
        """按 item 状态分类调度（score/pending 执行；其余分流，不盲目调用 Provider）。"""
        # 已成功 / 已完成
        if item.status == "completed":
            return ItemBatchOutcome(kind="skipped", code="ALREADY_COMPLETED")
        # manual_review / skipped 保留
        if item.status in ("manual_review", "skipped"):
            return ItemBatchOutcome(kind="skipped", code="PRESERVED_STATE")
        # score 已完成：review/export 阶段跳过 Provider
        if item.current_stage in ("review", "export"):
            return ItemBatchOutcome(kind="skipped", code="SCORE_STAGE_DONE")
        # running：不重复启动；stale 走恢复判断
        if item.status == "running":
            rec = self._orch.recover_score_item(
                task_id, item.item_id, f"rec-{batch_execution_id}-{item.item_id}")
            return ItemBatchOutcome(kind="recovery", code=rec.error_code or rec.outcome)
        # failed：走恢复判断（retryable 仅待批准；outcome_unknown 阻断）
        if item.status == "failed":
            rec = self._orch.recover_score_item(
                task_id, item.item_id, f"rec-{batch_execution_id}-{item.item_id}")
            return ItemBatchOutcome(kind="recovery", code=rec.error_code or rec.outcome)
        # pending：dry-run + execute（CAS 有限重试）
        exec_id = self._exec_id(batch_execution_id, item.item_id)
        for attempt in range(_MAX_REFRESH_RETRIES):
            result = await self._orch.execute_score_item(task_id, item.item_id, exec_id)
            if result.outcome == "internal_error" and result.error_code == ERR_EXEC_ITEM_START_FAILED:
                # CAS/状态冲突：重新读取后重新判断（不产生重复 Provider 调用）
                fresh = self._task_item.get_item(task_id, item.item_id)
                if fresh is None or fresh.status == "running":
                    return ItemBatchOutcome(kind="recovery", code="ITEM_RUNNING_CONFLICT")
                if fresh.status != "pending":
                    return ItemBatchOutcome(kind="skipped", code="STATE_CHANGED")
                continue
            return self._classify_execution(result)
        return ItemBatchOutcome(kind="blocked", code=ERR_EXEC_ITEM_START_FAILED)

    @staticmethod
    def _classify_execution(result: Any) -> "ItemBatchOutcome":
        if result.outcome == "succeeded":
            return ItemBatchOutcome(kind="succeeded", code=None)
        if result.outcome == "already_completed":
            return ItemBatchOutcome(kind="skipped", code="ALREADY_COMPLETED")
        if result.outcome == "blocked":
            return ItemBatchOutcome(kind="blocked", code=result.error_code or "EXECUTION_BLOCKED")
        if result.outcome == "running_conflict":
            return ItemBatchOutcome(kind="recovery", code=result.error_code or "ITEM_RUNNING_CONFLICT")
        if result.outcome == "outcome_unknown":
            return ItemBatchOutcome(kind="recovery", code=result.error_code or "OUTCOME_UNKNOWN")
        if result.outcome == "failed":
            return ItemBatchOutcome(kind="recovery", code=result.error_code or "EXECUTION_FAILED")
        return ItemBatchOutcome(kind="blocked", code=result.error_code or "EXECUTION_INTERNAL")

    # ---------------- 辅助 ---------------- #

    def _is_paused_or_cancelled(self, task_id: str) -> bool:
        task = self._task_item.get_task(task_id)
        return task is not None and task.status in ("paused", "cancelled")

    @staticmethod
    def _exec_id(batch_execution_id: str, item_id: str) -> str:
        """execution_request_id 稳定可重建：batch_execution_id + item_id 派生（重放幂等）。"""
        digest = hashlib.sha256(f"{batch_execution_id}|{item_id}".encode("utf-8")).hexdigest()[:32]
        return f"b-{digest}"

    def _aggregate(
        self,
        batch_execution_id: str,
        task_id: str,
        final_task: Optional[Any],
        items: List[Any],
        outcomes: List["ItemBatchOutcome"],
    ) -> BatchScoringExecutionResult:
        counters = Counter(o.kind for o in outcomes)
        code_counts: Counter = Counter(o.code for o in outcomes if o.code)
        task_status = final_task.status if final_task is not None else "unknown"
        current_stage = final_task.current_stage if final_task is not None else None
        revision = final_task.revision if final_task is not None else 1
        pending_remaining = False
        if final_task is not None:
            pending_remaining = (
                final_task.pending_items > 0
                or final_task.running_items > 0
                or current_stage in ("score", "review")
            )
        if task_status == "completed":
            outcome: BatchOutcome = "completed"
        elif task_status == "completed_with_errors":
            outcome = "completed_with_errors"
        elif task_status == "paused":
            outcome = "paused"
        elif task_status == "cancelled":
            outcome = "cancelled"
        else:
            outcome = "running"
        return BatchScoringExecutionResult(
            batch_execution_id=batch_execution_id,
            task_id=task_id,
            outcome=outcome,
            task_status=task_status,
            current_stage=current_stage,
            task_revision=revision,
            total_items=len(items),
            started=counters["succeeded"] + counters["blocked"] + counters["recovery"],
            succeeded=counters["succeeded"],
            skipped=counters["skipped"],
            blocked=counters["blocked"],
            recovery_required=counters["recovery"],
            pending_remaining=pending_remaining,
            error_code_counts=dict(code_counts),
        )

    @staticmethod
    def _result(
        batch_execution_id: str,
        task_id: str,
        outcome: BatchOutcome,
        *,
        task_status: str,
        current_stage: Optional[str],
        task_revision: int,
        total_items: int,
        pending_remaining: bool,
    ) -> BatchScoringExecutionResult:
        return BatchScoringExecutionResult(
            batch_execution_id=batch_execution_id,
            task_id=task_id,
            outcome=outcome,
            task_status=task_status,
            current_stage=current_stage,
            task_revision=task_revision,
            total_items=total_items,
            started=0,
            succeeded=0,
            skipped=0,
            blocked=0,
            recovery_required=0,
            pending_remaining=pending_remaining,
            error_code_counts={},
        )


class ItemBatchOutcome:
    """批次内单 item 执行结果分类（内部）。"""

    __slots__ = ("kind", "code")

    def __init__(self, kind: str, code: Optional[str] = None) -> None:
        self.kind = kind  # succeeded / skipped / blocked / recovery
        self.code = code
