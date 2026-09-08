"""
PipelineTask API 安全映射（Phase 11C-3）。

字段白名单映射：不直接对内部对象无筛选 model_dump()。
错误 -> HTTP 状态码映射：未知异常不返回 str(exc)。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from models.pipeline_api import (
    PipelineApiError,
    PipelineEventResponse,
    PipelineItemLastErrorResponse,
    PipelineItemOutputResponse,
    PipelineItemResponse,
    PipelineStageSummaryResponse,
    PipelineTaskResponse,
    ResumeDecisionResponse,
    ResumeRequestResponse,
)
from models.pipeline_task import PipelineEvent, PipelineItem, PipelineTask
from services.evidence_registration_service import RegistrationError
from services.pipeline_task_manager import PipelineTaskError
from services.pipeline_task_store import PipelineStoreError

# metadata 白名单键（事件响应只返回这些；未知键不返回）
EVENT_METADATA_ALLOWED_KEYS = frozenset({
    "attempt_id", "stage", "reason_code", "item_id", "next_attempt_number", "resume_from_stage",
})

# ---------------------------------------------------------------- 任务/事件白名单映射

def _map_stage_summary(s: Any) -> PipelineStageSummaryResponse:
    return PipelineStageSummaryResponse(
        stage=s.stage, status=s.status, depends_on=list(s.depends_on),
        started_at=s.started_at, completed_at=s.completed_at,
        total_items=s.total_items, pending_items=s.pending_items, running_items=s.running_items,
        completed_items=s.completed_items, failed_items=s.failed_items,
        skipped_items=s.skipped_items, manual_review_items=s.manual_review_items,
        blocking_error_codes=list(s.blocking_error_codes), revision=s.revision,
    )


def to_task_response(task: PipelineTask, idempotent_hit: bool = False) -> PipelineTaskResponse:
    """任务白名单（不含 configuration_snapshot/指纹/幂等键/内部引用）。"""
    return PipelineTaskResponse(
        task_id=task.task_id,
        batch_id=task.batch_id,
        status=task.status,
        current_stage=task.current_stage,
        execution_scope=list(task.execution_scope),
        total_items=task.total_items,
        pending_items=task.pending_items,
        running_items=task.running_items,
        completed_items=task.completed_items,
        failed_items=task.failed_items,
        skipped_items=task.skipped_items,
        manual_review_items=task.manual_review_items,
        concurrency=task.concurrency,
        stage_summaries=[_map_stage_summary(s) for s in task.stage_summaries],
        created_at=task.created_at,
        started_at=task.started_at,
        updated_at=task.updated_at,
        paused_at=task.paused_at,
        completed_at=task.completed_at,
        revision=task.revision,
        last_event_sequence=task.last_event_sequence,
        idempotent_hit=idempotent_hit,
    )


def to_item_response(item: PipelineItem) -> PipelineItemResponse:
    """item 白名单（不暴露 output_ref 绝对路径/原始材料/指纹）。"""
    output = PipelineItemOutputResponse(
        present=item.output_ref is not None,
        sha256=item.output_sha256,
    )
    last_error = None
    if item.last_error is not None:
        last_error = PipelineItemLastErrorResponse(
            code=item.last_error.error_code,
            message_key=item.last_error.message_key,
            retryable=item.last_error.retryable,
        )
    return PipelineItemResponse(
        item_id=item.item_id,
        task_id=item.task_id,
        package_id=item.package_id,
        package_revision=item.package_revision,
        evidence_level=item.evidence_level,
        status=item.status,
        current_stage=item.current_stage,
        attempt_count=item.attempt_count,
        max_attempts=item.max_attempts,
        retryable=item.retryable,
        heartbeat_updated_at=item.heartbeat_updated_at,
        output=output,
        last_error=last_error,
        item_revision=item.item_revision,
    )


def to_event_response(event: PipelineEvent) -> PipelineEventResponse:
    """事件白名单：metadata 仅白名单键，未知键不返回。"""
    meta: Dict[str, Any] = {}
    for k, v in (event.metadata or {}).items():
        if k in EVENT_METADATA_ALLOWED_KEYS:
            meta[k] = v
    return PipelineEventResponse(
        event_id=event.event_id,
        task_id=event.task_id,
        sequence=event.sequence,
        event_type=event.event_type,
        occurred_at=event.occurred_at,
        stage=event.stage,
        item_id=event.item_id,
        attempt_id=event.attempt_id,
        revision_before=event.revision_before,
        revision_after=event.revision_after,
        reason_code=event.reason_code,
        metadata=meta,
    )


def to_request_response(r: Any) -> ResumeRequestResponse:
    return ResumeRequestResponse(
        request_id=r.request_id, task_id=r.task_id, requested_at=r.requested_at, mode=r.mode,
        expected_revision=r.expected_revision, expected_item_revisions=dict(r.expected_item_revisions),
        item_ids=list(r.item_ids), reason_code=r.reason_code, dry_run=r.dry_run,
    )


def to_decision_response(d: Any) -> ResumeDecisionResponse:
    return ResumeDecisionResponse(
        decision_id=d.decision_id, request_id=d.request_id, task_id=d.task_id,
        decided_at=d.decided_at, approved=d.approved, task_revision_before=d.task_revision_before,
        eligible_items=[e.model_dump(mode="json") for e in d.eligible_items],
        protected_items=[p.model_dump(mode="json") for p in d.protected_items],
        rejected_items=[r.model_dump(mode="json") for r in d.rejected_items],
        decision_error_codes=list(d.decision_error_codes), dry_run=d.dry_run,
    )


# ---------------------------------------------------------------- 错误 -> HTTP

def _status_for_code(code: str) -> int:
    """稳定错误码 -> HTTP 状态映射（契约第五节）。"""
    if code == "REQUEST_VALIDATION_ERROR":
        return 422
    if code in ("SIDECAR_TASK_LIMIT_EXCEEDED", "SIDECAR_PACKAGE_NOT_REGISTERED",
                "SIDECAR_VALIDATION_NOT_ALLOWED", "SIDECAR_PACKAGE_IDENTITY_CONFLICT",
                "SIDECAR_EVIDENCE_LEVEL_MISSING", "SIDECAR_EVIDENCE_LEVEL_NOT_ALLOWED"):
        return 422  # evidence 准入失败
    if code in ("SIDECAR_REVISION_CONFLICT", "SIDECAR_LOCK_CONFLICT",
                "SIDECAR_IDEMPOTENCY_CONFLICT", "SIDECAR_EVENT_IDEMPOTENCY_CONFLICT",
                "SIDECAR_INVALID_STATE_TRANSITION", "SIDECAR_ATTEMPT_LIMIT_EXCEEDED",
                "SIDECAR_SUCCESS_RESULT_PROTECTED", "SIDECAR_TASK_NOT_SETTLED",
                "SIDECAR_SCOPE_VIOLATION", "SIDECAR_DECISION_NOT_APPROVED"):
        return 409
    if code in ("SIDECAR_TASK_NOT_FOUND", "SIDECAR_ITEM_NOT_FOUND",
                "SIDECAR_REQUEST_NOT_FOUND", "SIDECAR_VALIDATION_NOT_FOUND"):
        return 404
    if code in ("SIDECAR_TASK_CORRUPTED", "SIDECAR_RECORD_CORRUPTED", "SIDECAR_INDEX_CORRUPTED",
                "SIDECAR_TRANSACTION_ERROR", "SIDECAR_EVENT_SEQUENCE_CONFLICT"):
        return 500
    return 500  # 未知错误码 -> 通用 500 INTERNAL_ERROR


def to_api_error(exc: Exception) -> PipelineApiError:
    """异常 -> 稳定错误结构（不泄露 str(exc)/traceback/路径）。"""
    if isinstance(exc, PipelineTaskError):
        return PipelineApiError(code=exc.error_code, message_key=exc.message_key, retryable=exc.retryable)
    if isinstance(exc, PipelineStoreError):
        return PipelineApiError(code=exc.error_code, message_key=exc.message_key, retryable=exc.retryable)
    if isinstance(exc, RegistrationError):
        return PipelineApiError(code=exc.code, message_key=exc.message_key, retryable=exc.retryable)
    return PipelineApiError(code="INTERNAL_ERROR", message_key="INTERNAL_ERROR", retryable=False)


def error_status(exc: Exception) -> int:
    """异常 -> HTTP 状态码。"""
    if isinstance(exc, PipelineTaskError):
        return _status_for_code(exc.error_code)
    if isinstance(exc, PipelineStoreError):
        return _status_for_code(exc.error_code)
    if isinstance(exc, RegistrationError):
        return _status_for_code(exc.code)
    return 500
