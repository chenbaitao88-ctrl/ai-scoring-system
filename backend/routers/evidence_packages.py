"""
EvidencePackage 导入 API（Phase 11B-2b + 11B-2c）：单包 dry-run、批量 dry-run、正式登记、只读查询。

当前实现（统一前缀 /api/evidence-packages）：
- POST /dry-run
- POST /dry-run-batch（Phase 11B-2c；fail_fast 冻结语义见契约 3.2）
- POST /register
- GET  /registrations
- GET  /registrations/{package_id}/revisions/{package_revision}
- GET  /registrations/{package_id}/revisions/{package_revision}/validations/{validation_id}
- GET  /registrations/{package_id}/revisions/{package_revision}/validations/{validation_id}/issues

依赖注入：get_evidence_registration_service / get_evidence_query_service 可被
FastAPI dependency_overrides 替换；默认根为 config.DATA_DIR 下相互隔离的兄弟目录
（evidence-inputs / evidence-runtime）。

安全：不返回 actual_summary、绝对路径、traceback、exception repr 或原始错误字符串；
未知异常只返回通用 500 INTERNAL_ERROR。错误响应体为顶层 EvidenceApiError
{code, message_key, retryable}；登记拒绝 422 响应体为 EvidenceRegistrationResponse。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from config import DATA_DIR
from models.evidence_api import (
    EvidenceApiError,
    EvidenceBatchDryRunRequest,
    EvidenceBatchDryRunResponse,
    EvidenceBatchItemResponse,
    EvidenceDryRunRequest,
    EvidenceIssueResponse,
    EvidenceRegisterRequest,
    EvidenceRegistrationDetailResponse,
    EvidenceRegistrationListResponse,
    EvidenceRegistrationResponse,
    EvidenceValidationResponse,
)
from models.evidence_package import EvidencePackage
from services.evidence_api_mapper import (
    INTERNAL_ERROR_RESPONSE,
    query_error_status,
    registration_error_status,
    to_api_error,
    to_issue_response,
    to_registration_detail,
    to_registration_response,
    to_registration_summary,
    to_validation_response,
)
from services.evidence_query_service import EvidenceQueryService
from services.evidence_registration_service import (
    EvidenceRegistrationService,
    RegistrationError,
)

router = APIRouter(prefix="/api/evidence-packages", tags=["EvidencePackage 导入"])


# ---------------------------------------------------------------- 服务依赖


def get_evidence_registration_service() -> EvidenceRegistrationService:
    """默认依赖：DATA_DIR 下相互隔离的兄弟目录（输入根 / 运行根）。"""
    return EvidenceRegistrationService(
        controlled_input_root=DATA_DIR / "evidence-inputs",
        runtime_data_root=DATA_DIR / "evidence-runtime",
    )


def get_evidence_query_service() -> EvidenceQueryService:
    """默认依赖：基于登记服务构造只读查询服务。"""
    return EvidenceQueryService(get_evidence_registration_service())


# ---------------------------------------------------------------- 内部辅助


def _error(status_code: int, error: EvidenceApiError) -> JSONResponse:
    """顶层稳定错误响应。"""
    return JSONResponse(status_code=status_code, content=error.model_dump(mode="json"))


def _resolve_package_dir(reg: EvidenceRegistrationService, package_ref: str) -> Path:
    """把安全 package_ref 解析到受控输入根（模型已校验 POSIX 相对路径）。"""
    return reg.input_root / package_ref


def _read_evidence_level(reg: EvidenceRegistrationService, package_ref: str) -> Optional[str]:
    """从已验证 EvidencePackage 映射真实 evidence_level（不得猜测）。

    解析失败时保守返回 None（验证器已通过，理论上不会发生）。
    """
    try:
        path = _resolve_package_dir(reg, package_ref) / "evidence.json"
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        return EvidencePackage(**raw).evidence_level
    except Exception:
        return None


def _current_index_revision(reg: EvidenceRegistrationService) -> Optional[int]:
    """严格读取后的真实 Index revision（不得自行计算或猜测）。"""
    index = reg.read_index_strict()
    return index.revision if index is not None else None


def _internal_error() -> JSONResponse:
    return _error(500, INTERNAL_ERROR_RESPONSE)


# ---------------------------------------------------------------- dry-run


@router.post("/dry-run", response_model=EvidenceValidationResponse, status_code=200)
def dry_run(
    body: EvidenceDryRunRequest,
    reg: EvidenceRegistrationService = Depends(get_evidence_registration_service),
) -> EvidenceValidationResponse:
    """单包 dry-run：验证结论为 failed 仍返回 HTTP 200；零写入。"""
    pkg_dir = _resolve_package_dir(reg, body.package_ref)
    try:
        result, issues = reg.dry_run(pkg_dir)
    except RegistrationError as exc:
        return _error(registration_error_status(exc), to_api_error(exc))
    except Exception:
        return _internal_error()
    return to_validation_response(
        result, issues, evidence_level=_read_evidence_level(reg, body.package_ref),
    )


# ---------------------------------------------------------------- 批次 dry-run


@router.post("/dry-run-batch", response_model=EvidenceBatchDryRunResponse, status_code=200)
def dry_run_batch(
    body: EvidenceBatchDryRunRequest,
    reg: EvidenceRegistrationService = Depends(get_evidence_registration_service),
) -> EvidenceBatchDryRunResponse:
    """批次 dry-run（fail_fast 冻结语义见契约 3.2）。

    - fail_fast=false：按 package_refs 原顺序处理全部项目。
    - fail_fast=true：串行处理，遇首个 failed/internal_error 立即停止（触发项保留）。
    - 未执行项目不进入 items；total 为实际执行数；items 为原顺序连续前缀。
    - 单项异常隔离为该项 error（计入 internal_error），不泄露异常正文/路径/traceback。
    - 批次整体（正常完成或提前结束）均返回 HTTP 200；零写入。
    """
    items: List[EvidenceBatchItemResponse] = []
    counts = {"passed": 0, "passed_with_warnings": 0, "failed": 0, "internal_error": 0}

    for package_ref in body.package_refs:
        pkg_dir = _resolve_package_dir(reg, package_ref)
        try:
            result, issues = reg.dry_run(pkg_dir)
        except RegistrationError as exc:
            # 单项无法进入验证流程：隔离为安全 error（internal_error），fail_fast 时停止
            items.append(EvidenceBatchItemResponse(
                package_ref=package_ref, result=None, error=to_api_error(exc),
            ))
            counts["internal_error"] += 1
            if body.fail_fast:
                break
            continue
        except Exception:
            items.append(EvidenceBatchItemResponse(
                package_ref=package_ref, result=None, error=INTERNAL_ERROR_RESPONSE,
            ))
            counts["internal_error"] += 1
            if body.fail_fast:
                break
            continue

        items.append(EvidenceBatchItemResponse(
            package_ref=package_ref,
            result=to_validation_response(
                result, issues, evidence_level=_read_evidence_level(reg, package_ref),
            ),
        ))
        status = result.status
        if status == "passed":
            counts["passed"] += 1
        elif status == "passed_with_warnings":
            counts["passed_with_warnings"] += 1
        elif status == "failed":
            counts["failed"] += 1
            if body.fail_fast:
                break
        else:  # internal_error（验证器返回）
            counts["internal_error"] += 1
            if body.fail_fast:
                break

    return EvidenceBatchDryRunResponse(
        total=len(items),
        passed=counts["passed"],
        passed_with_warnings=counts["passed_with_warnings"],
        failed=counts["failed"],
        internal_error=counts["internal_error"],
        items=items,
    )


# ---------------------------------------------------------------- 正式登记


@router.post("/register", response_model=EvidenceRegistrationResponse, status_code=201)
def register(
    body: EvidenceRegisterRequest,
    reg: EvidenceRegistrationService = Depends(get_evidence_registration_service),
) -> JSONResponse:
    """正式登记：首次/更新 201；幂等命中 200；验证门拒绝 422（rejected=true + 安全 validation）。"""
    pkg_dir = _resolve_package_dir(reg, body.package_ref)
    try:
        outcome = reg.register(
            pkg_dir, mode="registration", expected_index_revision=body.expected_index_revision,
        )
    except RegistrationError as exc:
        return _error(registration_error_status(exc), to_api_error(exc))
    except Exception:
        return _internal_error()

    validation = to_validation_response(outcome.result, outcome.issues, evidence_level=None)

    if outcome.rejected:
        # 验证门拒绝：422 + rejected=true + 安全 validation（不伪造登记事实）
        resp = to_registration_response(
            idempotent_hit=False, rejected=True, package_id=outcome.result.package_id,
            package_revision=outcome.result.package_revision, validation=validation,
            record_id=None, entry_id=None, index_revision=None,
        )
        return JSONResponse(status_code=422, content=resp.model_dump(mode="json"))

    record = outcome.record
    entry = outcome.entry
    if record is None or entry is None:
        return _internal_error()

    resp = to_registration_response(
        idempotent_hit=outcome.idempotent_hit, rejected=False,
        package_id=record.package_id, package_revision=record.package_revision,
        validation=validation, record_id=record.record_id, entry_id=entry.entry_id,
        index_revision=_current_index_revision(reg),
    )
    # 幂等命中 -> 200；首次/更新 -> 201
    status_code = 200 if outcome.idempotent_hit else 201
    return JSONResponse(status_code=status_code, content=resp.model_dump(mode="json"))


# ---------------------------------------------------------------- 查询


@router.get("/registrations", response_model=EvidenceRegistrationListResponse)
def list_registrations(
    package_id: Optional[str] = Query(default=None),
    batch_id: Optional[str] = Query(default=None),
    submission_id: Optional[str] = Query(default=None),
    registration_status: Optional[str] = Query(default=None),
    latest_validation_status: Optional[str] = Query(default=None),
    model_input_allowed: Optional[bool] = Query(default=None),
    q: EvidenceQueryService = Depends(get_evidence_query_service),
) -> EvidenceRegistrationListResponse:
    try:
        entries = q.list_registrations(
            package_id=package_id, batch_id=batch_id, submission_id=submission_id,
            registration_status=registration_status,
            latest_validation_status=latest_validation_status,
            model_input_allowed=model_input_allowed,
        )
    except RegistrationError as exc:
        return _error(query_error_status(exc), to_api_error(exc))
    except Exception:
        return _internal_error()
    items = [to_registration_summary(e) for e in entries]
    return EvidenceRegistrationListResponse(total=len(items), items=items)


@router.get(
    "/registrations/{package_id}/revisions/{package_revision}",
    response_model=EvidenceRegistrationDetailResponse,
)
def get_registration(
    package_id: str,
    package_revision: int,
    q: EvidenceQueryService = Depends(get_evidence_query_service),
) -> EvidenceRegistrationDetailResponse:
    try:
        result = q.get_registration(package_id, package_revision)
    except RegistrationError as exc:
        return _error(query_error_status(exc), to_api_error(exc))
    except Exception:
        return _internal_error()
    if result is None:
        # None -> HTTP 404（契约冻结）
        return _error(404, EvidenceApiError(
            code="SIDECAR_VALIDATION_NOT_FOUND", message_key="SIDECAR_VALIDATION_NOT_FOUND", retryable=False,
        ))
    return to_registration_detail(result)


@router.get(
    "/registrations/{package_id}/revisions/{package_revision}/validations/{validation_id}",
    response_model=EvidenceValidationResponse,
)
def get_validation(
    package_id: str,
    package_revision: int,
    validation_id: str,
    q: EvidenceQueryService = Depends(get_evidence_query_service),
) -> EvidenceValidationResponse:
    try:
        validation = q.get_validation(package_id, package_revision, validation_id)
        issues = q.get_validation_issues(package_id, package_revision, validation_id)
    except RegistrationError as exc:
        return _error(query_error_status(exc), to_api_error(exc))
    except Exception:
        return _internal_error()
    return to_validation_response(validation, issues, evidence_level=None)


@router.get(
    "/registrations/{package_id}/revisions/{package_revision}/validations/{validation_id}/issues",
    response_model=List[EvidenceIssueResponse],
)
def get_validation_issues(
    package_id: str,
    package_revision: int,
    validation_id: str,
    q: EvidenceQueryService = Depends(get_evidence_query_service),
) -> List[EvidenceIssueResponse]:
    try:
        issues = q.get_validation_issues(package_id, package_revision, validation_id)
    except RegistrationError as exc:
        return _error(query_error_status(exc), to_api_error(exc))
    except Exception:
        return _internal_error()
    return [to_issue_response(i) for i in issues]
