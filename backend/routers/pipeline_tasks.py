"""
PipelineTask API 路由（Phase 11C-3）。

全部写操作通过 PipelineTaskManager（依赖注入），API 不直接写 store。
错误映射走 pipeline_api_mapper（稳定结构，不泄露内部细节）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from models.pipeline_api import (
    ApplyBody,
    ExpectedRevisionBody,
    HeartbeatBody,
    PipelineApiError,
    PipelineEventListResponse,
    PipelineItemListResponse,
    PipelineItemResponse,
    PipelineTaskListResponse,
    PipelineTaskResponse,
    ResumeDecisionApplyResponse,
    ResumeDecisionResponse,
    ResumeRequestBody,
    ResumeRequestResponse,
)
from models.pipeline_task import ResumeRequest
from services import pipeline_api_mapper as mapper
from services.pipeline_api_mapper import error_status, to_api_error
from services.pipeline_task_manager import PipelineTaskCreateRequest, PipelineTaskManager
from services.pipeline_task_store import PipelineStoreError

router = APIRouter(prefix="/api/pipeline-tasks", tags=["PipelineTask 流程"])


def get_pipeline_task_manager() -> PipelineTaskManager:
    """默认依赖：运行目录 DATA_DIR/pipeline-runtime（独立于旧 data/tasks 与 evidence sidecar）。"""
    import os
    import uuid

    from config import DATA_DIR as CONFIG_DATA_DIR
    from services.evidence_registration_service import EvidenceRegistrationService
    from services.pipeline_task_store import PipelineTaskStore

    data_dir = Path(
        os.environ.get("SCORING_DATA_DIR")
        or os.environ.get("DATA_DIR")
        or CONFIG_DATA_DIR
    )
    runtime_dir = data_dir / "pipeline-runtime"
    store = PipelineTaskStore(runtime_dir)
    reg = EvidenceRegistrationService(
        controlled_input_root=data_dir / "evidence-inputs",
        runtime_data_root=data_dir / "evidence-runtime",
    )
    return PipelineTaskManager(
        store, reg,
        clock=lambda: datetime.now(timezone.utc),
        uuid_factory=lambda: str(uuid.uuid4()),
    )


def _handle(exc: Exception) -> None:
    """统一错误 -> HTTPException（稳定结构）。未知异常 -> INTERNAL_ERROR 500。"""
    api_error: PipelineApiError = to_api_error(exc)
    raise HTTPException(status_code=error_status(exc), detail=api_error.model_dump(mode="json"))


def _not_found(code: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail=PipelineApiError(code=code, message_key=code, retryable=False).model_dump(mode="json"),
    )


# ---------------------------------------------------------------- 创建与查询


@router.post("", response_model=PipelineTaskResponse)
def create_task(body: PipelineTaskCreateRequest, mgr: PipelineTaskManager = Depends(get_pipeline_task_manager)):
    """创建任务：首次 201；幂等命中 200（幂等命中不重复写事件）。"""
    try:
        task, hit = mgr.create_task(body)
    except Exception as exc:
        _handle(exc)
    status_code = 200 if hit else 201
    return JSONResponse(status_code=status_code, content=mapper.to_task_response(task, idempotent_hit=hit).model_dump(mode="json"))


@router.get("", response_model=PipelineTaskListResponse)
def list_tasks(
    status: Optional[str] = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    mgr: PipelineTaskManager = Depends(get_pipeline_task_manager),
):
    """列表：分页 + 状态过滤（稳定 task_id 排序）。"""
    try:
        tasks = mgr.list_tasks(status_filter=status, offset=offset, limit=limit)
    except Exception as exc:
        _handle(exc)
    return PipelineTaskListResponse(
        items=[mapper.to_task_response(t) for t in tasks],
        total=len(tasks),
        offset=offset,
        limit=limit,
    )


@router.get("/{task_id}", response_model=PipelineTaskResponse)
def get_task(task_id: str, mgr: PipelineTaskManager = Depends(get_pipeline_task_manager)):
    try:
        task = mgr.get_task(task_id)
    except Exception as exc:
        _handle(exc)
    if task is None:
        raise _not_found("SIDECAR_TASK_NOT_FOUND")
    return mapper.to_task_response(task)


# ---------------------------------------------------------------- start / pause


@router.post("/{task_id}/start", response_model=PipelineTaskResponse)
def start_task(task_id: str, body: ExpectedRevisionBody,
               mgr: PipelineTaskManager = Depends(get_pipeline_task_manager)):
    try:
        task = mgr.start_task(task_id, body.expected_revision)
    except Exception as exc:
        _handle(exc)
    return mapper.to_task_response(task)


@router.post("/{task_id}/pause", response_model=PipelineTaskResponse)
def pause_task(task_id: str, body: ExpectedRevisionBody,
               mgr: PipelineTaskManager = Depends(get_pipeline_task_manager)):
    try:
        task = mgr.pause_task(task_id, body.expected_revision)
    except Exception as exc:
        _handle(exc)
    return mapper.to_task_response(task)


# ---------------------------------------------------------------- items / heartbeat


@router.get("/{task_id}/items", response_model=PipelineItemListResponse)
def list_items(task_id: str, mgr: PipelineTaskManager = Depends(get_pipeline_task_manager)):
    try:
        items = mgr.list_items(task_id)
    except Exception as exc:
        _handle(exc)
    return PipelineItemListResponse(items=[mapper.to_item_response(i) for i in items], total=len(items))


@router.get("/{task_id}/items/{item_id}", response_model=PipelineItemResponse)
def get_item(task_id: str, item_id: str, mgr: PipelineTaskManager = Depends(get_pipeline_task_manager)):
    try:
        item = mgr.get_item(task_id, item_id)
    except Exception as exc:
        _handle(exc)
    if item is None:
        raise _not_found("SIDECAR_ITEM_NOT_FOUND")
    return mapper.to_item_response(item)


@router.post("/{task_id}/items/{item_id}/heartbeat", response_model=PipelineItemResponse)
def heartbeat(task_id: str, item_id: str, body: HeartbeatBody,
              mgr: PipelineTaskManager = Depends(get_pipeline_task_manager)):
    try:
        item = mgr.heartbeat_item(task_id, item_id, body.expected_revision, body.expected_item_revision)
    except Exception as exc:
        _handle(exc)
    return mapper.to_item_response(item)


# ---------------------------------------------------------------- events


@router.get("/{task_id}/events", response_model=PipelineEventListResponse)
def list_events(task_id: str, mgr: PipelineTaskManager = Depends(get_pipeline_task_manager)):
    try:
        events = mgr.get_event_log(task_id)
    except Exception as exc:
        _handle(exc)
    return PipelineEventListResponse(items=[mapper.to_event_response(e) for e in events], total=len(events))


# ---------------------------------------------------------------- resume


@router.post("/{task_id}/resume-requests", response_model=ResumeRequestResponse)
def create_resume_request(task_id: str, body: ResumeRequestBody,
                          mgr: PipelineTaskManager = Depends(get_pipeline_task_manager)):
    request = ResumeRequest(
        contract_version="pipeline-task/v1",
        request_id=body.request_id,
        task_id=task_id,
        requested_at=datetime.now(timezone.utc),
        mode=body.mode,
        expected_revision=body.expected_revision,
        expected_item_revisions=dict(body.expected_item_revisions),
        item_ids=list(body.item_ids),
        reason_code=body.reason_code,
        dry_run=body.dry_run,
    )
    try:
        created = mgr.create_resume_request(task_id, request)
    except Exception as exc:
        _handle(exc)
    status_code = 201 if created else 200
    return JSONResponse(status_code=status_code, content=mapper.to_request_response(request).model_dump(mode="json"))


@router.get("/{task_id}/resume-requests/{request_id}", response_model=ResumeRequestResponse)
def get_resume_request(task_id: str, request_id: str,
                       mgr: PipelineTaskManager = Depends(get_pipeline_task_manager)):
    try:
        r = mgr.get_resume_request(task_id, request_id)
    except Exception as exc:
        _handle(exc)
    if r is None:
        raise _not_found("SIDECAR_REQUEST_NOT_FOUND")
    return mapper.to_request_response(r)


@router.post("/{task_id}/resume-requests/{request_id}/evaluate", response_model=ResumeDecisionResponse)
def evaluate_resume_request(task_id: str, request_id: str,
                            mgr: PipelineTaskManager = Depends(get_pipeline_task_manager)):
    try:
        decision = mgr.evaluate_resume_request(task_id, request_id)
    except Exception as exc:
        _handle(exc)
    return mapper.to_decision_response(decision)


@router.get("/{task_id}/resume-decisions/{request_id}", response_model=ResumeDecisionResponse)
def get_resume_decision(task_id: str, request_id: str,
                        mgr: PipelineTaskManager = Depends(get_pipeline_task_manager)):
    try:
        d = mgr.get_resume_decision(task_id, request_id)
    except Exception as exc:
        _handle(exc)
    if d is None:
        raise _not_found("SIDECAR_REQUEST_NOT_FOUND")
    return mapper.to_decision_response(d)


@router.post("/{task_id}/resume-decisions/{request_id}/apply", response_model=ResumeDecisionApplyResponse)
def apply_resume_decision(task_id: str, request_id: str, body: ApplyBody,
                          mgr: PipelineTaskManager = Depends(get_pipeline_task_manager)):
    try:
        decision = mgr.get_resume_decision(task_id, request_id)
        if decision is None:
            raise _not_found("SIDECAR_REQUEST_NOT_FOUND")
        task = mgr.apply_resume_decision(task_id, decision, body.expected_revision)
    except HTTPException:
        raise
    except Exception as exc:
        _handle(exc)
    items = mgr.list_items(task_id)
    return ResumeDecisionApplyResponse(
        task=mapper.to_task_response(task),
        changed_items=[mapper.to_item_response(i) for i in items],
    )
