"""
Evidence Sidecar 只读查询服务（Phase 11B-1d / 11B-1d-fix-1）。

供 Phase 11B-2 API 使用的正式只读查询边界。仅调用登记服务的公开只读接口
（read_index_strict / read_record_strict / read_validation_strict / read_issue_strict），
不调用任何私有方法、不导入登记模块私有函数、不自行拼接 sidecar 路径。

查询能力：
- list_registrations：从固定 RegistrationIndex 容器读取，完整严格校验，稳定排序，可过滤。
- get_registration：返回 RegistrationIndexEntry + EvidencePackageRecord + latest ValidationResult + Issues。
- get_validation：读取指定 package/revision 目录内的 ValidationResult（含历史），与 Record 身份一致。
- get_validation_issues：严格按 ValidationResult 引用顺序读取 issue，引用缺失/重复/身份不一致显式失败。

None 语义（正式、稳定）：
- get_registration 未登记时返回 None（不是成功空对象，不是伪造 Record）。
- None = 未登记，由 API 层映射为 HTTP 404。
- 不得把未登记误报为 Index 损坏。

只读保证：本服务不调用任何写入、登记、重建或恢复方法；不创建 lock/tmp/audit/运行文件；
不读取 EvidencePackage 正文；不读取真实学生材料。

错误处理（复用 fix-2 第 9.2 节冻结错误码，不新增契约码）：
- SIDECAR_INDEX_CORRUPTED / SIDECAR_RECORD_CORRUPTED / SIDECAR_VALIDATION_NOT_FOUND /
  SIDECAR_HASH_MISMATCH / SIDECAR_RELATIVE_REF_INVALID
"""
from __future__ import annotations

import re
from typing import List, Optional

from models.evidence_sidecar import (
    EvidencePackageRecord,
    RegistrationIndex,
    RegistrationIndexEntry,
    ValidationIssue,
    ValidationResult,
)
from services.evidence_registration_service import (
    ERR_INDEX_CORRUPTED,
    ERR_RECORD_CORRUPTED,
    ERR_RELATIVE_REF_INVALID,
    ERR_VALIDATION_NOT_FOUND,
    EvidenceRegistrationService,
    RegistrationError,
)

# 安全标识符：仅允许字母数字、下划线、连字符、点；拒绝路径分隔、空字节、`..`。
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_\-\.]+$")


class RegistrationQueryResult:
    """get_registration 的查询结果（只读内存对象）。"""

    def __init__(
        self,
        entry: RegistrationIndexEntry,
        record: EvidencePackageRecord,
        validation: ValidationResult,
        issues: List[ValidationIssue],
    ) -> None:
        self.entry = entry
        self.record = record
        self.validation = validation
        self.issues = issues


def _validate_query_identifier(value: str, field: str) -> None:
    """安全校验查询标识符：拒绝路径穿越、绝对路径、反斜杠、空字节、空字符串。"""
    if not isinstance(value, str) or not value:
        raise RegistrationError(ERR_RELATIVE_REF_INVALID, "SIDECAR_RELATIVE_REF_INVALID")
    if "\\" in value or "/" in value or "\x00" in value or value in (".", ".."):
        raise RegistrationError(ERR_RELATIVE_REF_INVALID, "SIDECAR_RELATIVE_REF_INVALID")
    if not _IDENTIFIER_RE.match(value):
        raise RegistrationError(ERR_RELATIVE_REF_INVALID, "SIDECAR_RELATIVE_REF_INVALID")


def _validate_query_revision(revision: int) -> None:
    if not isinstance(revision, int) or revision < 1:
        raise RegistrationError(ERR_RELATIVE_REF_INVALID, "SIDECAR_RELATIVE_REF_INVALID")


class EvidenceQueryService:
    """Evidence Sidecar 只读查询服务。"""

    def __init__(self, registration: EvidenceRegistrationService) -> None:
        self._reg = registration

    # -- 1. 查询登记索引 -------------------------------------------

    def list_registrations(
        self,
        package_id: Optional[str] = None,
        batch_id: Optional[str] = None,
        submission_id: Optional[str] = None,
        registration_status: Optional[str] = None,
        latest_validation_status: Optional[str] = None,
        model_input_allowed: Optional[bool] = None,
    ) -> List[RegistrationIndexEntry]:
        """从固定 RegistrationIndex 容器读取全部 entry（严格校验）。

        - Index 不存在时返回空结果，不创建文件。
        - 稳定排序：RegistrationIndex 模型强制 entries 按 (package_id, package_revision) 排序。
        - 按现有字段过滤，不增加契约外字段；不读取 EvidencePackage 正文。
        """
        index = self._reg.read_index_strict()
        if index is None:
            return []
        entries: List[RegistrationIndexEntry] = []
        for entry in index.entries:
            if package_id is not None and entry.package_id != package_id:
                continue
            if batch_id is not None and entry.batch_id != batch_id:
                continue
            if submission_id is not None and entry.submission_id != submission_id:
                continue
            if registration_status is not None and entry.registration_status != registration_status:
                continue
            if latest_validation_status is not None and entry.latest_validation_status != latest_validation_status:
                continue
            if model_input_allowed is not None and entry.model_input_allowed != model_input_allowed:
                continue
            entries.append(entry)
        return entries

    # -- 2. 查询单个登记记录 ---------------------------------------

    def get_registration(
        self, package_id: str, package_revision: int
    ) -> Optional[RegistrationQueryResult]:
        """查询单个登记记录。

        返回 entry + record + latest ValidationResult + 其引用的 ValidationIssue 列表。

        None 语义：未登记时返回 None。None 是正式、稳定的内部 not-found 信号，
        不是成功空对象，不是伪造 Record；由 Phase 11B-2 API 层映射为 HTTP 404。
        不得把未登记误报为 Index 损坏。

        - package_id / package_revision 必须经过安全校验。
        - Record / ValidationResult / Issue / Index 的身份和引用必须一致；哈希全部重新验证。
        - 不得静默忽略缺失或损坏文件。
        """
        _validate_query_identifier(package_id, "package_id")
        _validate_query_revision(package_revision)

        entry = self._find_entry(package_id, package_revision)
        if entry is None:
            return None  # 未登记 -> None（API 层映射 404）
        record = self._reg.read_record_strict(package_id, package_revision)
        if record is None:
            raise RegistrationError(ERR_INDEX_CORRUPTED, "SIDECAR_INDEX_CORRUPTED")
        validation = self._reg.read_validation_strict(
            package_id, package_revision, record.latest_validation_id,
        )
        # latest ValidationResult 与 Record 身份一致（严格校验）
        self._assert_validation_matches_record(record, validation)
        issues = self._load_issues_by_refs(package_id, package_revision, validation)
        return RegistrationQueryResult(entry=entry, record=record, validation=validation, issues=issues)

    # -- 3. 查询指定验证结果 ---------------------------------------

    def get_validation(
        self, package_id: str, package_revision: int, validation_id: str
    ) -> ValidationResult:
        """读取指定 package/revision 目录内的 ValidationResult（含历史）。

        - 只能访问该 package/revision 目录内的文件（公开只读接口内部完成安全路径构造）。
        - validation 必须与 Record 的 package 身份和 manifest SHA-256 一致。
        - 历史 ValidationResult 可读取，但不得改变 latest 指针（只读不写）。
        - 不存在返回 SIDECAR_VALIDATION_NOT_FOUND。
        """
        _validate_query_identifier(package_id, "package_id")
        _validate_query_revision(package_revision)
        _validate_query_identifier(validation_id, "validation_id")

        record = self._reg.read_record_strict(package_id, package_revision)
        if record is None:
            # 无该 package/revision 的登记事实 -> 单对象查询不存在，显式失败
            raise RegistrationError(ERR_VALIDATION_NOT_FOUND, "SIDECAR_VALIDATION_NOT_FOUND")
        validation = self._reg.read_validation_strict(package_id, package_revision, validation_id)
        self._assert_validation_matches_record(record, validation)
        return validation

    # -- 4. 查询问题明细 -------------------------------------------

    def get_validation_issues(
        self, package_id: str, package_revision: int, validation_id: str
    ) -> List[ValidationIssue]:
        """严格按 ValidationResult 中的 issue 引用读取 issue。

        - 返回顺序与 ValidationResult 引用顺序一致。
        - 引用缺失、重复、身份不一致或内容损坏必须显式失败。
        - 不扫描目录猜测问题文件。
        """
        validation = self.get_validation(package_id, package_revision, validation_id)
        return self._load_issues_by_refs(package_id, package_revision, validation)

    # -- 内部只读辅助（仅调用公开接口） ----------------------------

    def _find_entry(
        self, package_id: str, package_revision: int
    ) -> Optional[RegistrationIndexEntry]:
        index = self._reg.read_index_strict()
        if index is None:
            return None
        for entry in index.entries:
            if entry.package_id == package_id and entry.package_revision == package_revision:
                return entry
        return None

    def _assert_validation_matches_record(
        self, record: EvidencePackageRecord, validation: ValidationResult
    ) -> None:
        """ValidationResult 必须与 Record 的 package 身份和 manifest SHA-256 一致。"""
        if (
            validation.package_id != record.package_id
            or validation.package_revision != record.package_revision
            or validation.manifest_sha256 != record.manifest_sha256
        ):
            raise RegistrationError(ERR_RECORD_CORRUPTED, "SIDECAR_RECORD_CORRUPTED")

    def _load_issues_by_refs(
        self, package_id: str, package_revision: int, validation: ValidationResult
    ) -> List[ValidationIssue]:
        """严格按引用读取 issue；顺序一致；缺失/重复/身份不一致/损坏显式失败。"""
        issues: List[ValidationIssue] = []
        seen: set[str] = set()
        for ref in validation.issues:
            issue_id = ref.issue_id
            if issue_id in seen:
                raise RegistrationError(ERR_INDEX_CORRUPTED, "SIDECAR_INDEX_CORRUPTED")
            seen.add(issue_id)
            issue = self._reg.read_issue_strict(package_id, package_revision, issue_id)
            # 引用身份一致：issue_id / code / severity 必须与引用一致
            if (
                issue.issue_id != ref.issue_id
                or issue.code != ref.code
                or issue.severity != ref.severity
            ):
                raise RegistrationError(ERR_INDEX_CORRUPTED, "SIDECAR_INDEX_CORRUPTED")
            issues.append(issue)
        return issues
