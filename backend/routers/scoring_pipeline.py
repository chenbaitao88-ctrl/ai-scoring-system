"""Minimal safe HTTP API for the production scoring pipeline (Phase 11E-4a)."""
from __future__ import annotations

import json as _json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from models.identity_validation import _validate_ascii_id, _validate_uuid
from routers.pipeline_tasks import get_pipeline_task_manager
from services.pipeline_task_manager import PipelineTaskError
from services.pipeline_task_store import PipelineStoreError
from services.scoring_batch_orchestrator import BatchScoringExecutionResult
from services.scoring_pipeline_api_container import (
    ScoringPipelineApiContainer,
    ScoringPipelineApiRuntimeError,
    assemble_non_provider_scoring_api_container,
)
from services.review_case_store import ReviewFactStoreError
from services.scoring_pipeline_orchestrator import (
    ReviewApplicationResult,
    ScoreRecoveryResult,
)
from services.scoring_task_creator import ScoringTaskError


ERR_REQUEST_INVALID = "SCORING_PIPELINE_REQUEST_INVALID"
ERR_TASK_NOT_FOUND = "SCORING_PIPELINE_TASK_NOT_FOUND"
ERR_ITEM_NOT_FOUND = "SCORING_PIPELINE_ITEM_NOT_FOUND"
ERR_INTERNAL = "SCORING_PIPELINE_INTERNAL_ERROR"
SCORING_PIPELINE_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


class SafeValidationRoute(APIRoute):
    """Keep request validation errors inside the stable safe error envelope."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                return _error_response(
                    400,
                    ERR_REQUEST_INVALID,
                    request_id=_request_id(request),
                )

        return handler


router = APIRouter(
    prefix="/api/scoring-pipeline",
    tags=["Scoring Pipeline"],
    route_class=SafeValidationRoute,
)


def get_scoring_pipeline_api_container(
    task_manager=Depends(get_pipeline_task_manager),
) -> ScoringPipelineApiContainer:
    """Assemble the formal control plane; credential-backed execution stays closed."""
    return assemble_non_provider_scoring_api_container(
        task_manager=task_manager,
        config_dir=SCORING_PIPELINE_CONFIG_DIR,
    )


def _safe_id(value: str) -> str:
    return _validate_ascii_id(value)


class CreateScoringTaskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_task_id: str
    source_item_ids: list[str]
    batch_id: str
    profile_version: str
    provider_model_ref: str
    concurrency: int = Field(gt=0, le=16)

    _source_task_id = field_validator("source_task_id", mode="after")(_validate_uuid)
    _source_item_ids = field_validator("source_item_ids", mode="after")(
        lambda values: [_validate_uuid(value) for value in values]
    )
    _safe_fields = field_validator(
        "batch_id", "profile_version", "provider_model_ref", mode="after"
    )(_safe_id)


class DryRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str
    _request_id = field_validator("request_id", mode="after")(_safe_id)


class ExecuteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    batch_execution_id: str
    concurrency: Optional[int] = Field(default=None, gt=0, le=16)
    _batch_execution_id = field_validator("batch_execution_id", mode="after")(_safe_id)


class RecoverBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recovery_request_id: str
    _recovery_request_id = field_validator("recovery_request_id", mode="after")(_safe_id)


class ApplyReviewDecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    adoption_request_id: str
    competition_id: str
    _safe_fields = field_validator(
        "adoption_request_id", "competition_id", mode="after"
    )(_safe_id)


class TaskCountsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total: int
    pending: int
    running: int
    completed: int
    failed: int
    skipped: int
    manual_review: int


class ScoringTaskSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    source_task_id: str
    batch_id: str
    status: str
    current_stage: Optional[str]
    revision: int
    concurrency: int
    counts: TaskCountsResponse
    idempotent_hit: Optional[bool] = None


class ScoringItemSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str
    source_item_id: str
    status: str
    current_stage: str
    item_revision: int
    attempt_count: int
    retryable: bool
    evidence_level: str
    review_decision_id: Optional[str]
    latest_error_code: Optional[str] = None


class ScoringItemListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    total: int
    items: list[ScoringItemSummaryResponse]


class DryRunCheckResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    check_code: str
    passed: bool
    blocking: bool


class DryRunSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str
    task_id: str
    item_id: str
    decision: str
    checks: list[DryRunCheckResponse]
    blocking_error_code: Optional[str]


def _request_id(request: Request, fallback: Optional[str] = None) -> str:
    raw = fallback or request.headers.get("X-Request-ID")
    if raw:
        try:
            return _safe_id(raw)
        except Exception:
            pass
    return f"req-{uuid4()}"


def _error_response(
    status_code: int,
    code: str,
    *,
    request_id: str,
    message_key: Optional[str] = None,
    retryable: bool = False,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message_key": message_key or code,
                "retryable": retryable,
                "request_id": request_id,
            }
        },
    )


def _exception_response(exc: Exception, request_id: str) -> JSONResponse:
    if isinstance(exc, ScoringPipelineApiRuntimeError):
        return _error_response(
            exc.status_code,
            exc.error_code,
            message_key=exc.message_key,
            retryable=exc.retryable,
            request_id=request_id,
        )

    if isinstance(exc, ValidationError):
        return _error_response(400, ERR_REQUEST_INVALID, request_id=request_id)

    code = getattr(exc, "error_code", ERR_INTERNAL)
    message_key = getattr(exc, "message_key", code) or code
    retryable = bool(getattr(exc, "retryable", False))
    if isinstance(exc, (ScoringTaskError, PipelineTaskError, PipelineStoreError)):
        if "NOT_FOUND" in code:
            status = 404
        elif any(token in code for token in ("CONFLICT", "STATE", "PROTECTED", "SETTLED")):
            status = 409
        elif any(
            token in code
            for token in (
                "SOURCE_", "EVIDENCE", "VALIDATION", "PROFILE", "CONFIGURATION",
                "PRIVACY", "MODEL_INPUT", "SCOPE", "PACKAGE",
            )
        ):
            status = 422
        else:
            status = 500
        return _error_response(
            status,
            code,
            message_key=message_key,
            retryable=retryable,
            request_id=request_id,
        )
    return _error_response(500, ERR_INTERNAL, request_id=request_id)


def _task_summary(task: Any, *, idempotent_hit: Optional[bool] = None) -> dict:
    payload = {
        "task_id": task.task_id,
        "source_task_id": task.source_task_id,
        "batch_id": task.batch_id,
        "status": task.status,
        "current_stage": task.current_stage,
        "revision": task.revision,
        "concurrency": task.concurrency,
        "counts": {
            "total": task.total_items,
            "pending": task.pending_items,
            "running": task.running_items,
            "completed": task.completed_items,
            "failed": task.failed_items,
            "skipped": task.skipped_items,
            "manual_review": task.manual_review_items,
        },
    }
    if idempotent_hit is not None:
        payload["idempotent_hit"] = idempotent_hit
    return payload


def _item_summary(item: Any) -> dict:
    last_error = getattr(item, "last_error", None)
    return {
        "item_id": item.item_id,
        "source_item_id": item.source_item_id,
        "status": item.status,
        "current_stage": item.current_stage,
        "item_revision": item.item_revision,
        "attempt_count": item.attempt_count,
        "retryable": item.retryable,
        "evidence_level": item.evidence_level,
        "review_decision_id": item.review_decision_id,
        "latest_error_code": getattr(last_error, "error_code", None),
    }


def _require_task(container: ScoringPipelineApiContainer, task_id: str):
    task = container.task_manager.get_task(task_id)
    if task is None or task.task_type != "scoring_pipeline":
        raise ScoringPipelineApiRuntimeError(ERR_TASK_NOT_FOUND, status_code=404)
    return task


def _require_item(container: ScoringPipelineApiContainer, task_id: str, item_id: str):
    _require_task(container, task_id)
    item = container.task_manager.get_item(task_id, item_id)
    if item is None or item.task_id != task_id or item.task_type != "scoring_pipeline":
        raise ScoringPipelineApiRuntimeError(ERR_ITEM_NOT_FOUND, status_code=404)
    return item


# 11F-1b：服务端注入的本地 reviewer ActorRef（稳定、非 PII；不来自请求体）
_LOCAL_REVIEWER_REF = {"actor_type": "reviewer", "actor_id": "local-reviewer-workbench"}


def _hash_canonical(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_review_decision(task_id: str, item_id: str, body: ReviewDecisionCreateBody, case):
    """由请求体构造不可变 ReviewDecision 事实（服务端注入 actor/时间/hash）。"""
    from datetime import datetime, timezone

    import json as _json

    from models.review_case import ReviewDecision

    decision_id = f"dec-{body.idempotency_key}"
    payload = dict(
        contract_version="score-attempt-review/v1",
        schema_version="review-decision/v1",
        decision_id=decision_id,
        review_case_id=body.review_case_id,
        case_revision=case.current_revision,
        decision_type=body.decision_type,
        reason_codes=list(body.reason_codes),
        target_attempt_id=body.target_attempt_id,
        target_snapshot_id=body.target_snapshot_id,
        requested_package_revision=body.requested_package_revision,
        manual_adjustment_id=body.manual_adjustment_id,
        supersedes_decision_id=body.supersedes_decision_id,
        decision_note_code=body.decision_note_code,
        evidence_refs=[],
        idempotency_key=body.idempotency_key,
        decided_by=_LOCAL_REVIEWER_REF,
        decided_at=datetime.now(timezone.utc),
    )
    payload["decision_hash"] = _hash_canonical(
        _json.dumps(payload, sort_keys=True, default=str))
    return ReviewDecision(**payload)


def _find_decision_by_key(review_store, task_id: str, item_id: str, body: ReviewDecisionCreateBody):
    """同 idempotency_key 既有 decision（幂等重放命中；客户端可控字段一致才视为同内容）。"""
    decisions = review_store.list_review_decisions(task_id, item_id)
    for d in decisions:
        if d.idempotency_key != body.idempotency_key:
            continue
        if (
            d.review_case_id == body.review_case_id
            and d.decision_type == body.decision_type
            and list(d.reason_codes) == list(body.reason_codes)
            and d.target_attempt_id == body.target_attempt_id
            and d.target_snapshot_id == body.target_snapshot_id
            and d.requested_package_revision == body.requested_package_revision
            and d.manual_adjustment_id == body.manual_adjustment_id
            and d.supersedes_decision_id == body.supersedes_decision_id
            and d.decision_note_code == body.decision_note_code
        ):
            return d
        raise ScoringPipelineApiRuntimeError(
            "REVIEW_DECISION_IDEMPOTENCY_CONFLICT", status_code=409)
    return None


@router.post("/tasks", response_model=ScoringTaskSummaryResponse)
def create_task(
    body: CreateScoringTaskBody,
    request: Request,
    container: ScoringPipelineApiContainer = Depends(get_scoring_pipeline_api_container),
):
    request_id = _request_id(request)
    try:
        create_request = container.build_create_request(**body.model_dump())
        task, created = container.task_creator.create_scoring_task(create_request)
        return JSONResponse(
            status_code=201 if created else 200,
            content=_task_summary(task, idempotent_hit=not created),
        )
    except Exception as exc:
        return _exception_response(exc, request_id)


@router.get(
    "/tasks/{task_id}",
    response_model=ScoringTaskSummaryResponse,
    response_model_exclude_none=True,
)
def get_task(
    task_id: str,
    request: Request,
    container: ScoringPipelineApiContainer = Depends(get_scoring_pipeline_api_container),
):
    request_id = _request_id(request)
    try:
        return _task_summary(_require_task(container, task_id))
    except Exception as exc:
        return _exception_response(exc, request_id)


@router.get("/tasks/{task_id}/items", response_model=ScoringItemListResponse)
def list_items(
    task_id: str,
    request: Request,
    container: ScoringPipelineApiContainer = Depends(get_scoring_pipeline_api_container),
):
    request_id = _request_id(request)
    try:
        _require_task(container, task_id)
        items = container.task_manager.list_items(task_id)
        return {"task_id": task_id, "total": len(items), "items": [_item_summary(i) for i in items]}
    except Exception as exc:
        return _exception_response(exc, request_id)


@router.post(
    "/tasks/{task_id}/items/{item_id}/dry-run",
    response_model=DryRunSummaryResponse,
)
def dry_run(
    task_id: str,
    item_id: str,
    body: DryRunBody,
    request: Request,
    container: ScoringPipelineApiContainer = Depends(get_scoring_pipeline_api_container),
):
    request_id = _request_id(request, body.request_id)
    try:
        _require_item(container, task_id, item_id)
        plan = container.require_dry_run_orchestrator().dry_run(
            task_id, item_id, body.request_id
        )
        return {
            "request_id": plan.dry_run_request_id,
            "task_id": plan.task_id,
            "item_id": plan.item_id,
            "decision": plan.decision,
            "checks": [check.model_dump(mode="json") for check in plan.checks],
            "blocking_error_code": plan.blocking_error_code,
        }
    except Exception as exc:
        return _exception_response(exc, request_id)


@router.post("/tasks/{task_id}/execute", response_model=BatchScoringExecutionResult)
async def execute_task(
    task_id: str,
    body: ExecuteBody,
    request: Request,
    container: ScoringPipelineApiContainer = Depends(get_scoring_pipeline_api_container),
):
    request_id = _request_id(request, body.batch_execution_id)
    try:
        task = _require_task(container, task_id)
        if body.concurrency is not None and body.concurrency > task.concurrency:
            raise ScoringPipelineApiRuntimeError(
                "SCORING_CONCURRENCY_EXCEEDS_FROZEN_LIMIT",
                status_code=409,
            )
        batch = container.require_execute_runtime()
        result = await batch.execute_scoring_task(
            task_id,
            body.batch_execution_id,
            concurrency=body.concurrency,
        )
        return result.model_dump(mode="json")
    except Exception as exc:
        return _exception_response(exc, request_id)


@router.post(
    "/tasks/{task_id}/items/{item_id}/recover",
    response_model=ScoreRecoveryResult,
)
def recover_item(
    task_id: str,
    item_id: str,
    body: RecoverBody,
    request: Request,
    container: ScoringPipelineApiContainer = Depends(get_scoring_pipeline_api_container),
):
    request_id = _request_id(request, body.recovery_request_id)
    try:
        _require_item(container, task_id, item_id)
        result = container.require_orchestrator().recover_score_item(
            task_id, item_id, body.recovery_request_id
        )
        return result.model_dump(mode="json")
    except Exception as exc:
        return _exception_response(exc, request_id)


@router.post(
    "/tasks/{task_id}/items/{item_id}/review-decisions/{decision_id}/apply",
    response_model=ReviewApplicationResult,
)
def apply_review_decision(
    task_id: str,
    item_id: str,
    decision_id: str,
    body: ApplyReviewDecisionBody,
    request: Request,
    container: ScoringPipelineApiContainer = Depends(get_scoring_pipeline_api_container),
):
    request_id = _request_id(request, body.adoption_request_id)
    try:
        _require_item(container, task_id, item_id)
        result = container.require_orchestrator().apply_review_decision(
            task_id,
            item_id,
            decision_id,
            body.adoption_request_id,
            competition_id=body.competition_id,
        )
        if result.error_code == "REVIEW_CASE_NOT_FOUND":
            raise ScoringPipelineApiRuntimeError(result.error_code, status_code=404)
        if result.outcome == "blocked":
            status = 422 if result.error_code == "REVIEW_FACT_BINDING_MISMATCH" else 409
            raise ScoringPipelineApiRuntimeError(result.error_code or "REVIEW_APPLICATION_BLOCKED", status_code=status)
        if result.outcome == "internal_error":
            raise RuntimeError("review application failed")
        return result.model_dump(mode="json")
    except Exception as exc:
        return _exception_response(exc, request_id)


# ---------------------------------------------------------------- 11F-1b：复核控制面 API


class ReviewCaseSummaryResponse(BaseModel):
    """单任务复核队列脱敏摘要（不返回证据正文/Prompt/模型响应/个人信息）。"""

    model_config = ConfigDict(extra="forbid")

    review_case_id: str
    item_id: str
    priority: str
    status: str
    reason_codes: list[str]
    opened_at: str
    blocks_auto_adoption: bool
    blocks_export: bool


class ReviewCaseListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    total: int
    items: list[ReviewCaseSummaryResponse]


class ReviewDecisionCreateBody(BaseModel):
    """创建 ReviewDecision 请求体（11F-1a 契约 §5）。

    decided_by 由服务端注入；请求体不得携带操作者身份或任何敏感材料正文。
    """

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str
    review_case_id: str
    decision_type: str
    reason_codes: list[str]
    target_attempt_id: Optional[str] = None
    target_snapshot_id: Optional[str] = None
    requested_package_revision: Optional[int] = Field(default=None, gt=0)
    manual_adjustment_id: Optional[str] = None
    supersedes_decision_id: Optional[str] = None
    decision_note_code: str = "REVIEWER_DECISION_RECORDED"
    comment_summary: str = ""

    _safe_ids = field_validator(
        "idempotency_key", "review_case_id", "target_attempt_id",
        "target_snapshot_id", "manual_adjustment_id", "supersedes_decision_id",
        mode="after",
    )(_safe_id)


class ReviewDecisionCreateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: str
    review_case_id: str
    case_revision: int
    decision_type: str
    outcome: str  # created | idempotent_hit
    idempotent: bool = False
    created: bool = False


class ReviewCaseReopenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(gt=0)
    package_revision: int = Field(gt=0)
    request_decision_id: str

    _request_decision_id = field_validator("request_decision_id", mode="after")(_safe_id)


class ReviewCaseReopenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_case_id: str
    status: str
    package_revision: int | None
    current_revision: int
    reopen_count: int
    outcome: str
    idempotent: bool


@router.get(
    "/tasks/{task_id}/review-cases",
    response_model=ReviewCaseListResponse,
)
def list_review_cases(
    task_id: str,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    reason_code: Optional[str] = None,
    request: Request = None,
    container: ScoringPipelineApiContainer = Depends(get_scoring_pipeline_api_container),
):
    """单任务复核队列查询（11F-1a 契约 §4）。

    支持 status/priority/reason_code 过滤与稳定排序（priority 降序、opened_at 升序）。
    只返回脱敏摘要；不返回证据正文、Prompt、模型响应或个人信息。
    """
    request_id = _request_id(request)
    try:
        task = _require_task(container, task_id)
        review_store = container.require_review_store()
        cases = review_store.list_review_cases(task_id)
        if status is not None:
            cases = [c for c in cases if c.status == status]
        if priority is not None:
            cases = [c for c in cases if c.priority == priority]
        if reason_code is not None:
            cases = [c for c in cases if reason_code in c.reason_codes]
        _PRIORITY_ORDER = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
        cases.sort(key=lambda c: (_PRIORITY_ORDER.get(c.priority, 9), c.opened_at.isoformat()))
        items = [
            ReviewCaseSummaryResponse(
                review_case_id=c.review_case_id,
                item_id=c.item_id,
                priority=c.priority,
                status=c.status,
                reason_codes=list(c.reason_codes),
                opened_at=c.opened_at.isoformat(),
                blocks_auto_adoption=c.blocks_auto_adoption,
                blocks_export=c.blocks_export,
            )
            for c in cases
        ]
        return ReviewCaseListResponse(task_id=task_id, total=len(items), items=items).model_dump(mode="json")
    except Exception as exc:
        return _exception_response(exc, request_id)


@router.post(
    "/tasks/{task_id}/items/{item_id}/review-cases/{case_id}/reopen",
    response_model=ReviewCaseReopenResponse,
)
def reopen_review_case(
    task_id: str,
    item_id: str,
    case_id: str,
    body: ReviewCaseReopenBody,
    request: Request = None,
    container: ScoringPipelineApiContainer = Depends(get_scoring_pipeline_api_container),
):
    """11F-3d: waiting_for_evidence -> assigned after the requested package revision returns."""
    request_id = _request_id(request)
    try:
        _require_item(container, task_id, item_id)
        review_store = container.require_review_store()
        outcome, case = review_store.reopen_review_case(
            task_id,
            item_id,
            case_id,
            expected_revision=body.expected_revision,
            package_revision=body.package_revision,
            request_decision_id=body.request_decision_id,
            actor=_LOCAL_REVIEWER_REF,
            occurred_at=datetime.now(timezone.utc),
        )
        return JSONResponse(
            status_code=200 if outcome == "idempotent_hit" else 201,
            content=ReviewCaseReopenResponse(
                review_case_id=case.review_case_id,
                status=case.status,
                package_revision=case.package_revision,
                current_revision=case.current_revision,
                reopen_count=case.reopen_count,
                outcome=outcome,
                idempotent=outcome == "idempotent_hit",
            ).model_dump(mode="json"),
        )
    except ReviewFactStoreError as exc:
        if "NOT_FOUND" in (exc.error_code or ""):
            return _exception_response(
                ScoringPipelineApiRuntimeError(exc.error_code, status_code=404), request_id)
        if "CORRUPTED" in (exc.error_code or ""):
            return _exception_response(
                ScoringPipelineApiRuntimeError(exc.error_code, status_code=500), request_id)
        return _exception_response(
            ScoringPipelineApiRuntimeError(exc.error_code, status_code=409), request_id)
    except Exception as exc:
        return _exception_response(exc, request_id)


# ---------------------------------------------------------------- 11F-2c：复核详情

@router.get(
    "/tasks/{task_id}/review-cases/{case_id}/detail",
)
def get_review_case_detail(
    task_id: str,
    case_id: str,
    request: Request = None,
    container: ScoringPipelineApiContainer = Depends(get_scoring_pipeline_api_container),
):
    """安全复核详情（11F-2c；只读，不返回个人信息/证据/Prompt/模型原文/分项得分）。"""
    request_id = _request_id(request)
    try:
        _require_task(container, task_id)
        review_store = container.require_review_store()
        attempt_store = container.require_attempt_store()
        # case_id 可能属于任意 item；先通过队列查找确定 item_id
        all_cases = review_store.list_review_cases(task_id)
        case = None
        for c in all_cases:
            if c.review_case_id == case_id:
                case = c
                break
        if case is None:
            return _exception_response(
                ScoringPipelineApiRuntimeError("REVIEW_CASE_NOT_FOUND", status_code=404), request_id)
        if case.task_id != task_id:
            return _exception_response(
                ScoringPipelineApiRuntimeError("REVIEW_FACT_BINDING_MISMATCH", status_code=409), request_id)
        from services.review_case_detail_service import ReviewCaseDetailService
        service = ReviewCaseDetailService(review_store, attempt_store)
        detail, error_code = service.build_detail(task_id, case.item_id, case_id)
        if error_code is not None:
            status = 404 if error_code == "REVIEW_CASE_NOT_FOUND" else 409
            return _exception_response(
                ScoringPipelineApiRuntimeError(error_code, status_code=status), request_id)
        return detail
    except ReviewFactStoreError as exc:
        if "NOT_FOUND" in (exc.error_code or ""):
            return _exception_response(
                ScoringPipelineApiRuntimeError(exc.error_code, status_code=404), request_id)
        if "CORRUPTED" in (exc.error_code or ""):
            return _exception_response(
                ScoringPipelineApiRuntimeError(exc.error_code, status_code=500), request_id)
        return _exception_response(
            ScoringPipelineApiRuntimeError(exc.error_code, status_code=409), request_id)
    except Exception as exc:
        return _exception_response(exc, request_id)


# ---------------------------------------------------------------- 11F-2d：最终结果锁定

class LockResultRequest(BaseModel):
    """11F-2d：人工最终锁定请求（客户端不得传 actor/分数/snapshot）。"""
    model_config = ConfigDict(extra="forbid")

    lock_request_id: str
    review_case_id: str
    adoption_id: str
    reason_code: str = "HUMAN_FINAL_LOCKED"


class LockResultResponse(BaseModel):
    lock_id: str
    status: str
    revision: int
    outcome: str
    idempotent: bool


@router.post(
    "/tasks/{task_id}/items/{item_id}/review-decisions/{decision_id}/lock",
)
def lock_final_result(
    task_id: str,
    item_id: str,
    decision_id: str,
    body: LockResultRequest,
    request: Request = None,
    container: ScoringPipelineApiContainer = Depends(get_scoring_pipeline_api_container),
):
    """11F-2d：人工最终锁定已采用结果（create-only 幂等）。"""
    request_id = _request_id(request)
    try:
        _require_item(container, task_id, item_id)
        review_store = container.require_review_store()
        attempt_store = container.require_attempt_store()
        case = review_store.get_review_case(task_id, item_id, body.review_case_id)
        if case is None:
            return _exception_response(
                ScoringPipelineApiRuntimeError("REVIEW_CASE_NOT_FOUND", status_code=404), request_id)
        if case.task_id != task_id or case.item_id != item_id:
            return _exception_response(
                ScoringPipelineApiRuntimeError("REVIEW_FACT_BINDING_MISMATCH", status_code=409), request_id)
        # 校验 decision
        decisions = review_store.list_review_decisions(task_id, item_id, body.review_case_id)
        decision = None
        for d in decisions:
            if d.decision_id == decision_id:
                decision = d
                break
        if decision is None:
            return _exception_response(
                ScoringPipelineApiRuntimeError("REVIEW_DECISION_NOT_FOUND", status_code=404), request_id)
        if decision.review_case_id != body.review_case_id:
            return _exception_response(
                ScoringPipelineApiRuntimeError("REVIEW_FACT_BINDING_MISMATCH", status_code=409), request_id)
        # 校验 adoption
        adoption = review_store.get_active_adoption(task_id, item_id)
        if adoption is None or adoption.adoption_id != body.adoption_id:
            return _exception_response(
                ScoringPipelineApiRuntimeError("RESULT_ADOPTION_NOT_FOUND", status_code=404), request_id)
        if adoption.status != "adopted":
            return _exception_response(
                ScoringPipelineApiRuntimeError("RESULT_ADOPTION_NOT_ADOPTED", status_code=409), request_id)
        if adoption.decision_id != decision_id:
            return _exception_response(
                ScoringPipelineApiRuntimeError("REVIEW_FACT_BINDING_MISMATCH", status_code=409), request_id)
        # 获取 snapshot（从 adoption 推断，不信任客户端）
        snapshot_id = adoption.snapshot_id
        if not snapshot_id:
            return _exception_response(
                ScoringPipelineApiRuntimeError("REVIEW_FACT_BINDING_MISMATCH", status_code=409), request_id)
        snap = attempt_store.get_snapshot(task_id, item_id, snapshot_id)
        if snap is None:
            return _exception_response(
                ScoringPipelineApiRuntimeError("REVIEW_FACT_BINDING_MISMATCH", status_code=409), request_id)
        # 构造 lock（幂等优先：先查同 lock_id 已存在）
        lock_id = f"flk-{body.lock_request_id}"
        existing = review_store.get_active_final_lock(task_id, item_id)
        if existing is not None and existing.lock_id == lock_id:
            return JSONResponse(
                status_code=200,
                content=LockResultResponse(
                    lock_id=existing.lock_id,
                    status=existing.status,
                    revision=existing.revision,
                    outcome="idempotent_hit",
                    idempotent=True,
                ).model_dump(mode="json"),
            )
        from models.review_case import ManualFinalLock
        lock = ManualFinalLock(
            contract_version="score-attempt-review/v1",
            schema_version="manual-final-lock/v1",
            lock_id=lock_id,
            task_id=task_id,
            item_id=item_id,
            review_case_id=body.review_case_id,
            decision_id=decision_id,
            adoption_id=body.adoption_id,
            snapshot_id=snapshot_id,
            locked_by=_LOCAL_REVIEWER_REF,
            locked_at=datetime.now(timezone.utc),
            reason_code=body.reason_code,
            status="active",
            revision=1,
            idempotency_key=body.lock_request_id,
            content_hash=_hash_canonical(
                _json.dumps({"lock_id": lock_id, "lock_request_id": body.lock_request_id},
                           sort_keys=True, default=str)),
        )
        outcome, saved = review_store.create_final_lock(task_id, item_id, lock)
        status_code = 200 if outcome == "idempotent_hit" else 201
        return JSONResponse(
            status_code=status_code,
            content=LockResultResponse(
                lock_id=saved.lock_id,
                status=saved.status,
                revision=saved.revision,
                outcome=outcome,
                idempotent=outcome == "idempotent_hit",
            ).model_dump(mode="json"),
        )
    except ReviewFactStoreError as exc:
        if "NOT_FOUND" in (exc.error_code or ""):
            return _exception_response(
                ScoringPipelineApiRuntimeError(exc.error_code, status_code=404), request_id)
        if "CORRUPTED" in (exc.error_code or ""):
            return _exception_response(
                ScoringPipelineApiRuntimeError(exc.error_code, status_code=500), request_id)
        return _exception_response(
            ScoringPipelineApiRuntimeError(exc.error_code, status_code=409), request_id)
    except Exception as exc:
        return _exception_response(exc, request_id)


# ---------------------------------------------------------------- 11F-3b：人工调整

class AdjustmentChangeBody(BaseModel):
    """11F-3b-fix-1：单条调整变更（extra=forbid）。"""
    model_config = ConfigDict(extra="forbid")

    dimension_code: str
    before_value: float
    after_value: float
    change_reason_code: str = "OTHER"


class AdjustResultRequest(BaseModel):
    """11F-3b：人工调整请求（extra=forbid，客户端不得传 snapshot/actor/分数）。"""
    model_config = ConfigDict(extra="forbid")

    adjustment_request_id: str
    review_case_id: str
    changes: list[AdjustmentChangeBody] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    adjustment_note_code: str = "MANUAL_SCORE_CORRECTION"


class AdjustResultResponse(BaseModel):
    adjustment_id: str
    adjusted_snapshot_id: str
    adoption_id: str | None = None
    outcome: str
    idempotent: bool


@router.post(
    "/tasks/{task_id}/items/{item_id}/review-decisions/{decision_id}/adjust",
)
def adjust_result(
    task_id: str,
    item_id: str,
    decision_id: str,
    body: AdjustResultRequest,
    request: Request = None,
    container: ScoringPipelineApiContainer = Depends(get_scoring_pipeline_api_container),
):
    """11F-3b：人工调整某个已采用结果的分项得分。"""
    request_id = _request_id(request)
    try:
        _require_item(container, task_id, item_id)
        review_store = container.require_review_store()
        attempt_store = container.require_attempt_store()
        from services.manual_adjustment_service import ManualAdjustmentService
        service = container.adjustment_service or ManualAdjustmentService(review_store, attempt_store)
        result, error_code = service.build_and_apply(
            task_id=task_id,
            item_id=item_id,
            case_id=body.review_case_id,
            decision_id=decision_id,
            request_id=body.adjustment_request_id,
            changes=[ch.model_dump() for ch in body.changes],
            reason_codes=body.reason_codes,
            adjustment_note_code=body.adjustment_note_code,
        )
        if error_code is not None:
            status = 404 if error_code == "REVIEW_CASE_NOT_FOUND" else 409
            if "LOCK" in error_code:
                status = 409
            if "CONFLICT" in error_code:
                status = 409
            if "TRANSACTION" in error_code:
                status = 500
            return _exception_response(
                ScoringPipelineApiRuntimeError(error_code, status_code=status), request_id)
        return JSONResponse(
            status_code=201 if not result["idempotent"] else 200,
            content=AdjustResultResponse(
                adjustment_id=result["adjustment_id"],
                adjusted_snapshot_id=result["adjusted_snapshot_id"],
                adoption_id=result.get("adoption_id"),
                outcome=result["outcome"],
                idempotent=result["idempotent"],
            ).model_dump(mode="json"),
        )
    except ReviewFactStoreError as exc:
        if "NOT_FOUND" in (exc.error_code or ""):
            return _exception_response(
                ScoringPipelineApiRuntimeError(exc.error_code, status_code=404), request_id)
        return _exception_response(
            ScoringPipelineApiRuntimeError(exc.error_code, status_code=409), request_id)
    except Exception as exc:
        return _exception_response(exc, request_id)


@router.post(
    "/tasks/{task_id}/items/{item_id}/review-decisions",
    response_model=ReviewDecisionCreateResponse,
)
def create_review_decision(
    task_id: str,
    item_id: str,
    body: ReviewDecisionCreateBody,
    request: Request = None,
    container: ScoringPipelineApiContainer = Depends(get_scoring_pipeline_api_container),
):
    """创建 ReviewDecision（11F-1a 契约 §5；不可变事实，无原地覆盖接口）。

    - decided_by 由服务端注入（稳定、非 PII 的本地 reviewer ActorRef）。
    - 新建返回 201；幂等命中返回 200。
    - 不调用 apply 接口、不写最终分；request_additional_evidence 会推进 case 到 waiting_for_evidence。
    """
    request_id = _request_id(request)
    try:
        _require_item(container, task_id, item_id)
        review_store = container.require_review_store()
        case = review_store.get_review_case(task_id, item_id, body.review_case_id)
        if case is None:
            raise ScoringPipelineApiRuntimeError("REVIEW_CASE_NOT_FOUND", status_code=404)
        if case.task_id != task_id or case.item_id != item_id:
            raise ScoringPipelineApiRuntimeError("REVIEW_FACT_BINDING_MISMATCH", status_code=409)
        # 幂等优先：同 idempotency_key 既有 decision（客户端可控字段一致 -> 幂等命中）
        existing = _find_decision_by_key(review_store, task_id, item_id, body)
        if existing is not None:
            if existing.decision_type == "request_additional_evidence":
                review_store.advance_case_for_evidence_request(
                    task_id,
                    item_id,
                    existing,
                    actor=_LOCAL_REVIEWER_REF,
                    occurred_at=datetime.now(timezone.utc),
                )
            return JSONResponse(
                status_code=200,
                content=ReviewDecisionCreateResponse(
                    decision_id=existing.decision_id,
                    review_case_id=existing.review_case_id,
                    case_revision=existing.case_revision,
                    decision_type=existing.decision_type,
                    outcome="idempotent_hit",
                    idempotent=True,
                    created=False,
                ).model_dump(mode="json"),
            )
        if body.decision_type == "request_additional_evidence":
            if case.status != "in_review":
                raise ReviewFactStoreError("REVIEW_CASE_CONFLICT", retryable=False)
            if (
                case.package_revision is not None
                and body.requested_package_revision is not None
                and body.requested_package_revision <= case.package_revision
            ):
                raise ReviewFactStoreError("REVIEW_FACT_BINDING_MISMATCH", retryable=False)
        decision = _build_review_decision(task_id, item_id, body, case)
        outcome, saved = review_store.create_review_decision(
            task_id, item_id, decision,
            actor=_LOCAL_REVIEWER_REF,
        )
        if saved.decision_type == "request_additional_evidence":
            review_store.advance_case_for_evidence_request(
                task_id,
                item_id,
                saved,
                actor=_LOCAL_REVIEWER_REF,
                occurred_at=datetime.now(timezone.utc),
            )
        status_code = 200 if outcome == "idempotent_hit" else 201
        return JSONResponse(
            status_code=status_code,
            content=ReviewDecisionCreateResponse(
                decision_id=saved.decision_id,
                review_case_id=saved.review_case_id,
                case_revision=saved.case_revision,
                decision_type=saved.decision_type,
                outcome=outcome,
                idempotent=outcome == "idempotent_hit",
                created=outcome == "created",
            ).model_dump(mode="json"),
        )
    except ReviewFactStoreError as exc:
        # 11F-1b：store 稳定错误码原样映射（幂等冲突 409 / 状态/绑定 422/409）
        if exc.error_code == "REVIEW_DECISION_IDEMPOTENCY_CONFLICT":
            return _exception_response(
                ScoringPipelineApiRuntimeError(exc.error_code, status_code=409), request_id)
        if exc.error_code == "REVIEW_DECISION_REASON_MISMATCH":
            return _exception_response(
                ScoringPipelineApiRuntimeError(exc.error_code, status_code=422), request_id)
        if exc.error_code == "REVIEW_DECISION_TARGET_REQUIRED":
            return _exception_response(
                ScoringPipelineApiRuntimeError(exc.error_code, status_code=422), request_id)
        if exc.error_code == "REVIEW_CASE_STATE_NOT_DECISIONABLE":
            return _exception_response(
                ScoringPipelineApiRuntimeError(exc.error_code, status_code=409), request_id)
        if exc.error_code == "REVIEW_DECISION_CONFLICT":
            return _exception_response(
                ScoringPipelineApiRuntimeError(exc.error_code, status_code=409), request_id)
        if exc.error_code == "REVIEW_CASE_CONFLICT":
            return _exception_response(
                ScoringPipelineApiRuntimeError(exc.error_code, status_code=409), request_id)
        if exc.error_code == "REVIEW_FACT_BINDING_MISMATCH":
            return _exception_response(
                ScoringPipelineApiRuntimeError(exc.error_code, status_code=409), request_id)
        if exc.error_code == "CASE_LOCKED":
            return _exception_response(
                ScoringPipelineApiRuntimeError(exc.error_code, status_code=409), request_id)
        raise
    except Exception as exc:
        return _exception_response(exc, request_id)
