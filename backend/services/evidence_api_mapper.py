"""
EvidencePackage API 统一安全映射（Phase 11B-2b）。

单一映射口径，避免各 endpoint 分别手写：
- ValidationResult + ValidationIssue[] -> EvidenceValidationResponse
- RegistrationIndexEntry -> EvidenceRegistrationSummary
- RegistrationQueryResult -> EvidenceRegistrationDetailResponse
- RegistrationError -> HTTP status + EvidenceApiError

安全要求：不返回 actual_summary、绝对路径、traceback、exception repr 或原始错误字符串；
未知异常由调用方映射为通用 500（INTERNAL_ERROR）。
"""
from __future__ import annotations

from typing import List, Optional

from models.evidence_api import (
    EvidenceApiError,
    EvidenceIssueResponse,
    EvidenceRegistrationDetailResponse,
    EvidenceRegistrationResponse,
    EvidenceRegistrationSummary,
    EvidenceValidationResponse,
)
from models.evidence_sidecar import (
    RegistrationIndexEntry,
    ValidationIssue,
    ValidationResult,
)
from services.evidence_query_service import RegistrationQueryResult
from services.evidence_registration_service import (
    ERR_AUDIT_WRITE_FAILED,
    ERR_CONTENT_CONFLICT,
    ERR_HASH_MISMATCH,
    ERR_IDEMPOTENCY_CONFLICT,
    ERR_INDEX_CORRUPTED,
    ERR_INDEX_WRITE_FAILED,
    ERR_INTERNAL,
    ERR_LOCK_CONFLICT,
    ERR_RECORD_CONFLICT,
    ERR_RECORD_CORRUPTED,
    ERR_REGISTRATION_NOT_ALLOWED,
    ERR_RELATIVE_REF_INVALID,
    ERR_REVISION_CONFLICT,
    ERR_UNSUPPORTED_VERSION,
    ERR_VALIDATION_NOT_FOUND,
    RegistrationError,
)

# 通用未知异常响应（冻结）：code/message_key=INTERNAL_ERROR、retryable=false
INTERNAL_ERROR_RESPONSE = EvidenceApiError(
    code="INTERNAL_ERROR", message_key="INTERNAL_ERROR", retryable=False,
)


def to_validation_response(
    result: ValidationResult,
    issues: List[ValidationIssue],
    evidence_level: Optional[str] = None,
) -> EvidenceValidationResponse:
    """把 ValidationResult 与实际 ValidationIssue 合并为安全 ValidationResponse。

    issues 逐引用合并为 EvidenceIssueResponse（含 message_key）；
    evidence_level 由调用方按语义提供（dry-run 映射实际等级；查询返回 null）。
    """
    issue_map = {i.issue_id: i for i in issues}
    safe_issues: List[EvidenceIssueResponse] = []
    for ref in result.issues:
        issue = issue_map.get(ref.issue_id)
        message_key = issue.message_key if issue is not None else ref.code
        safe_issues.append(
            EvidenceIssueResponse(
                issue_id=ref.issue_id,
                code=ref.code,
                severity=ref.severity,
                message_key=message_key,
            )
        )
    return EvidenceValidationResponse(
        validation_id=result.validation_id,
        package_id=result.package_id,
        package_revision=result.package_revision,
        mode=result.mode,
        status=result.status,
        evidence_level=evidence_level,
        registration_allowed=result.registration_allowed,
        model_input_allowed=result.model_input_allowed,
        issue_counts=result.issue_counts,
        issues=safe_issues,
        started_at=result.started_at,
        completed_at=result.completed_at,
    )


def to_issue_response(issue: ValidationIssue) -> EvidenceIssueResponse:
    """ValidationIssue -> 安全 Issue 响应（仅 4 字段）。"""
    return EvidenceIssueResponse(
        issue_id=issue.issue_id,
        code=issue.code,
        severity=issue.severity,
        message_key=issue.message_key,
    )


def to_registration_summary(entry: RegistrationIndexEntry) -> EvidenceRegistrationSummary:
    """Index 条目 -> 列表安全元数据。"""
    return EvidenceRegistrationSummary(
        package_id=entry.package_id,
        package_revision=entry.package_revision,
        batch_id=entry.batch_id,
        submission_id=entry.submission_id,
        registration_status=entry.registration_status,
        latest_validation_status=entry.latest_validation_status,
        model_input_allowed=entry.model_input_allowed,
        manifest_sha256=entry.manifest_sha256,
    )


def to_registration_detail(
    query_result: RegistrationQueryResult,
) -> EvidenceRegistrationDetailResponse:
    """查询结果 -> 安全详情（latest validation 的 evidence_level 为 null：Sidecar 未持久化）。"""
    return EvidenceRegistrationDetailResponse(
        package_id=query_result.record.package_id,
        package_revision=query_result.record.package_revision,
        record_id=query_result.record.record_id,
        entry_id=query_result.entry.entry_id,
        registration_status=query_result.record.registration_status,
        record_revision=query_result.record.revision,
        latest_validation=to_validation_response(
            query_result.validation, query_result.issues, evidence_level=None,
        ),
        issues=[to_issue_response(i) for i in query_result.issues],
    )


def to_registration_response(
    *,
    idempotent_hit: bool,
    rejected: bool,
    package_id: str,
    package_revision: int,
    validation: Optional[EvidenceValidationResponse],
    record_id: Optional[str],
    entry_id: Optional[str],
    index_revision: Optional[int],
) -> EvidenceRegistrationResponse:
    """构造登记响应并让模型层冻结不变量（rejected/成功/幂等）。"""
    return EvidenceRegistrationResponse(
        idempotent_hit=idempotent_hit,
        rejected=rejected,
        record_id=record_id,
        entry_id=entry_id,
        package_id=package_id,
        package_revision=package_revision,
        validation=validation,
        index_revision=index_revision,
    )


# ---------------------------------------------------------------- 错误映射


def registration_error_status(exc: RegistrationError) -> int:
    """登记流程错误 -> HTTP 状态（409 冲突类 / 422 请求或门类 / 500 其余）。"""
    code = exc.code
    if code in (
        ERR_REVISION_CONFLICT,
        ERR_IDEMPOTENCY_CONFLICT,
        ERR_CONTENT_CONFLICT,
        ERR_LOCK_CONFLICT,
        ERR_RECORD_CONFLICT,
    ):
        return 409
    if code in (
        ERR_UNSUPPORTED_VERSION,
        ERR_REGISTRATION_NOT_ALLOWED,
        ERR_RELATIVE_REF_INVALID,
    ):
        return 422
    return 500


def query_error_status(exc: RegistrationError) -> int:
    """查询流程错误 -> HTTP 状态（404 不存在 / 422 请求非法 / 500 损坏等）。"""
    code = exc.code
    if code == ERR_VALIDATION_NOT_FOUND:
        return 404
    if code == ERR_RELATIVE_REF_INVALID:
        return 422
    return 500


def to_api_error(exc: RegistrationError) -> EvidenceApiError:
    """稳定错误响应：仅 code/message_key/retryable，不含内部细节。"""
    return EvidenceApiError(code=exc.code, message_key=exc.message_key, retryable=exc.retryable)
