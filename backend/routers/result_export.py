"""Authoritative result preview and task-scoped Excel/Word/CSV export.

All files use the same derivation boundary. No Provider or legacy database
scores are used; only redacted identifiers, authority and scores leave it.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from routers.pipeline_tasks import get_pipeline_task_manager
from services.pipeline_task_manager import PipelineTaskManager
from services.result_derivation_service import (
    EXPORT_BLOCKED_BY_REVIEW,
    ResultDerivationError,
    ResultDerivationService,
)
from services.review_case_store import ReviewCaseStore
from services.score_attempt_store import ScoreAttemptStore
from services.authoritative_export_service import (
    AuthoritativeExportError, AuthoritativeExportService, ExportFormat, require_task_id,
)

RESULT_EXPORT_REQUEST_INVALID = "RESULT_EXPORT_REQUEST_INVALID"
RESULT_EXPORT_INTERNAL_ERROR = "RESULT_EXPORT_INTERNAL_ERROR"

router = APIRouter(prefix="/api/result-export", tags=["结果导出判定"])

# 与 review/score 事实层一致的安全相对 ID 规则（防路径穿越、保留名、超长）。
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WINDOWS_RESERVED = frozenset({
    "con", "prn", "aux", "nul",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
})


def _is_safe_id(value: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if "\x00" in value or "/" in value or "\\" in value:
        return False
    if not _ID_RE.fullmatch(value):
        return False
    lowered = value.lower()
    if lowered in _WINDOWS_RESERVED or lowered.rstrip(".") in _WINDOWS_RESERVED:
        return False
    return True


def get_result_derivation_service(
    task_manager: PipelineTaskManager = Depends(get_pipeline_task_manager),
) -> ResultDerivationService:
    """默认依赖：按流水线运行目录装配只读事实存储与派生服务。"""
    runtime_root = Path(task_manager.store.root)

    attempt_store = ScoreAttemptStore(
        runtime_root / "score-attempts"
    )

    review_store = ReviewCaseStore(
        runtime_root / "reviews",
        attempt_store=attempt_store,
    )

    service = ResultDerivationService(
        review_store,
        attempt_store,
        root=runtime_root,
    )
    return service


def get_authoritative_export_service(
    manager: PipelineTaskManager = Depends(get_pipeline_task_manager),
    derivation: ResultDerivationService = Depends(get_result_derivation_service),
) -> AuthoritativeExportService:
    return AuthoritativeExportService(manager, derivation)


def _export_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, AuthoritativeExportError):
        return JSONResponse(status_code=exc.status, content={
            "exportable": False,
            "error": {"code": exc.code, "message_key": exc.message},
            "blocked_items": exc.blocked_items,
        }, headers={"Cache-Control": "no-store"})
    return JSONResponse(status_code=409, content={
        "exportable": False,
        "error": {"code": "EXPORT_SOURCE_UNAVAILABLE", "message_key": "结果记录不可用，已阻止导出。请检查任务记录后重试。"},
    }, headers={"Cache-Control": "no-store"})


def export_file_response(service: AuthoritativeExportService, task_id: Optional[str],
                         fmt: ExportFormat, item_ids: Optional[List[str]] = None,
                         expected_derivations: Optional[Dict[str, str]] = None):
    try:
        task_id = require_task_id(task_id)
        payload = service.export(task_id, fmt, item_ids, expected_derivations)
    except Exception as exc:
        return _export_error(exc)
    media_types = {
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "csv": "text/csv; charset=utf-8",
    }
    return Response(payload, media_type=media_types[fmt], headers={
        "Content-Disposition": f'attachment; filename="authoritative-results-{task_id}.{fmt}"',
        "Cache-Control": "no-store",
    })


@router.get("/tasks")
def list_export_tasks(service: AuthoritativeExportService = Depends(get_authoritative_export_service)):
    try:
        return JSONResponse({"items": service.list_tasks()}, headers={"Cache-Control": "no-store"})
    except Exception as exc:
        return _export_error(exc)


@router.get("/tasks/{task_id}/results")
def preview_task_results(task_id: str, service: AuthoritativeExportService = Depends(get_authoritative_export_service)):
    try:
        return JSONResponse(service.preview(task_id), headers={"Cache-Control": "no-store"})
    except Exception as exc:
        return _export_error(exc)


@router.get("/tasks/{task_id}/export")
def export_task_results(
    task_id: str, format: ExportFormat = Query(default="xlsx"),
    item_id: Optional[List[str]] = Query(default=None),
    service: AuthoritativeExportService = Depends(get_authoritative_export_service),
):
    return export_file_response(service, task_id, format, item_id)


class ExportTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: ExportFormat = "xlsx"
    item_ids: Optional[List[str]] = Field(default=None, min_length=1)
    expected_derivations: Optional[Dict[str, str]] = None


@router.post("/tasks/{task_id}/export")
def export_selected_results(
    task_id: str, body: ExportTaskRequest,
    service: AuthoritativeExportService = Depends(get_authoritative_export_service),
):
    return export_file_response(service, task_id, body.format, body.item_ids, body.expected_derivations)


def _error_body(
    task_id: str,
    item_id: str,
    code: str,
    reasons: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return {
        "task_id": task_id,
        "item_id": item_id,
        "exportable": False,
        "error": {
            "code": code,
            "reasons": list(reasons or []),
        },
    }


@router.post("/tasks/{task_id}/items/{item_id}/derive")
def derive_authoritative_result(
    task_id: str,
    item_id: str,
    service: ResultDerivationService = Depends(get_result_derivation_service),
):
    """判定单个 item 是否具备权威可导出结果。

    - 200：存在权威结果，返回脱敏的最终分数字段。
    - 409：导出被阻断（无权威结果、未解决阻断工单、快照缺失/损坏/绑定冲突）。
    - 400：task_id / item_id 非法（RESULT_EXPORT_REQUEST_INVALID）。
    - 500：未知异常（RESULT_EXPORT_INTERNAL_ERROR，不返回堆栈或内部细节）。
    """
    if not _is_safe_id(task_id) or not _is_safe_id(item_id):
        return JSONResponse(
            status_code=400,
            content=_error_body(
                task_id,
                item_id,
                RESULT_EXPORT_REQUEST_INVALID,
                [{"code": "UNSAFE_TASK_OR_ITEM_ID"}],
            ),
        )
    try:
        result = service.derive(task_id, item_id)
    except ResultDerivationError as exc:
        if exc.error_code == EXPORT_BLOCKED_BY_REVIEW:
            return JSONResponse(
                status_code=409,
                content=_error_body(
                    task_id, item_id, EXPORT_BLOCKED_BY_REVIEW,
                    [dict(reason) for reason in exc.reasons],
                ),
            )
        return JSONResponse(
            status_code=409,
            content=_error_body(
                task_id, item_id, exc.error_code,
                [dict(reason) for reason in exc.reasons],
            ),
        )
    except Exception:
        return JSONResponse(
            status_code=500,
            content=_error_body(task_id, item_id, RESULT_EXPORT_INTERNAL_ERROR),
        )

    snapshot = result.final_snapshot
    return {
        "task_id": task_id,
        "item_id": item_id,
        "exportable": True,
        "authority_type": result.authority_type,
        "derivation_id": result.derivation_id,
        "result": {
            "snapshot_id": snapshot["snapshot_id"],
            "attempt_id": snapshot["attempt_id"],
            "submission_id": snapshot["submission_id"],
            "total_score": snapshot["total_score"],
            "objective_score": snapshot["objective_score"],
            "subjective_score": snapshot["subjective_score"],
            "dimension_scores": snapshot["dimension_scores"],
        },
    }
