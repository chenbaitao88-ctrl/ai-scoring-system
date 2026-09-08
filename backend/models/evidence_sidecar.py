"""
Evidence Sidecar 契约数据模型（Phase 11B-1a）。

严格依据《Phase11A-fix-2-EvidenceSidecar契约.md》v1 实现，不重新设计字段。
本阶段只实现不依赖文件系统的确定性格式与取值约束：

- 四类对象均带 schema version。
- ID、revision、hash、相对引用和时间字段约束明确。
- ValidationResult / ValidationIssue 只保存脱敏摘要字段（actual_summary 长度上限）。
- 未知字段默认拒绝。
- issue counts 不允许负值。

本阶段不实现：文件持久化、原子写、锁、Index 更新、Audit Event 写入。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import List, Literal, Optional, Tuple

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# 复用 EvidencePackage 的 SHA-256 / 相对路径 / 标识符 / 时间校验与枚举
from models.evidence_package import (
    EvidenceLevel,
    PublicationStatus,
    validate_relative_path,
    validate_sha256,
)

# ---------------------------------------------------------------- 枚举

RegistrationStatus = Literal["registered", "rejected", "superseded", "corrupted"]
ValidationMode = Literal["dry_run", "registration", "revalidation"]
ValidationStatus = Literal["passed", "passed_with_warnings", "failed", "internal_error"]
IssueSeverity = Literal["info", "warning", "error", "fatal"]
CheckStatus = Literal["passed", "failed", "not_applicable"]

SidecarErrorCode = Literal[
    "SIDECAR_UNSUPPORTED_VERSION",
    "SIDECAR_RECORD_CONFLICT",
    "SIDECAR_REVISION_CONFLICT",
    "SIDECAR_RECORD_CORRUPTED",
    "SIDECAR_VALIDATION_NOT_FOUND",
    "SIDECAR_INDEX_CORRUPTED",
    "SIDECAR_INDEX_WRITE_FAILED",
    "SIDECAR_LOCK_CONFLICT",
    "SIDECAR_HASH_MISMATCH",
    "SIDECAR_RELATIVE_REF_INVALID",
    "REGISTRATION_NOT_ALLOWED",
    "REGISTRATION_IDEMPOTENCY_CONFLICT",
    "VALIDATION_INTERNAL_ERROR",
]

# 脱敏摘要长度上限（契约 6.2 要求 actual_summary 限制长度；采用保守上限）
SUMMARY_MAX_LENGTH = 2000

# ---------------------------------------------------------------- 子模型


class ValidationCheckSummary(BaseModel):
    """ValidationResult.checks 单项摘要（契约 5.2：check_id / status / message_key）。"""

    model_config = ConfigDict(extra="forbid")

    check_id: str = Field(min_length=1)
    status: CheckStatus
    message_key: str = Field(min_length=1)


class ValidationIssueRef(BaseModel):
    """ValidationResult.issues 引用项（契约 5.2：issue_id / code / severity）。"""

    model_config = ConfigDict(extra="forbid")

    issue_id: str = Field(min_length=1)
    code: str = Field(min_length=1)
    severity: IssueSeverity


class IssueCounts(BaseModel):
    """按 severity 计数（契约 5.2），不允许负值。"""

    model_config = ConfigDict(extra="forbid")

    info: int = Field(default=0, ge=0)
    warning: int = Field(default=0, ge=0)
    error: int = Field(default=0, ge=0)
    fatal: int = Field(default=0, ge=0)


class SidecarError(BaseModel):
    """sidecar 错误最小结构化模型（契约 9.2 错误码与处理策略）。"""

    model_config = ConfigDict(extra="forbid")

    error_code: SidecarErrorCode
    message_key: str = Field(min_length=1)
    retryable: bool
    blocks_registration: bool
    requires_manual_review: bool
    allows_index_rebuild: bool


def _validate_ref(value: str) -> str:
    """相对引用校验：复用相对路径规则，且不允许含路径穿越。"""
    return validate_relative_path(value)


def _validate_time(value: datetime) -> datetime:
    """时间必须严格为 RFC 3339 UTC（带时区且 offset 必须为 0）。

    拒绝 naive datetime 与非 UTC offset（如 +08:00）。
    """
    if value.tzinfo is None:
        raise ValueError("时间必须带时区（RFC 3339）")
    if value.utcoffset() != timedelta(0):
        raise ValueError("时间必须为 UTC（offset 必须为 0），不接受非 UTC 时区")
    return value


# ---------------------------------------------------------------- 四类对象


class EvidencePackageRecord(BaseModel):
    """登记事实（契约 4）。"""

    model_config = ConfigDict(extra="forbid")

    record_schema_version: Literal["evidence-sidecar/record/v1"] = "evidence-sidecar/record/v1"
    record_id: str = Field(min_length=1)
    package_id: str
    package_revision: int = Field(gt=0)
    batch_id: str
    submission_id: str
    evidence_version: str = Field(min_length=1)
    manifest_sha256: str
    privacy_policy_version: str = Field(min_length=1)
    contract_version: Literal["evidence-package/v1.1"] = "evidence-package/v1.1"
    publication_status: PublicationStatus
    registration_status: RegistrationStatus
    latest_validation_id: str = Field(min_length=1)
    latest_validation_status: ValidationStatus
    source_package_ref: str
    registered_at: AwareDatetime
    registered_by: str = Field(min_length=1)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    revision: int = Field(ge=1)
    record_sha256: str

    @field_validator("manifest_sha256", "record_sha256")
    @classmethod
    def _hash(cls, v: str) -> str:
        return validate_sha256(v)

    @field_validator("source_package_ref")
    @classmethod
    def _ref(cls, v: str) -> str:
        return _validate_ref(v)

    _validate_registered_at = field_validator("registered_at")(_validate_time)
    _validate_created_at = field_validator("created_at")(_validate_time)
    _validate_updated_at = field_validator("updated_at")(_validate_time)

    @model_validator(mode="after")
    def _check_times(self) -> "EvidencePackageRecord":
        if self.updated_at < self.created_at:
            raise ValueError("updated_at 不得早于 created_at")
        return self


class ValidationResult(BaseModel):
    """一次验证运行的不可变事实（契约 5）。"""

    model_config = ConfigDict(extra="forbid")

    validation_schema_version: Literal["evidence-sidecar/validation/v1"] = (
        "evidence-sidecar/validation/v1"
    )
    validation_id: str = Field(min_length=1)
    record_id: Optional[str] = None
    package_id: str
    package_revision: int = Field(gt=0)
    manifest_sha256: str
    validator_name: str = Field(min_length=1)
    validator_version: str = Field(min_length=1)
    evidence_contract_version: Literal["evidence-package/v1.1"] = "evidence-package/v1.1"
    privacy_policy_version: str = Field(min_length=1)
    mode: ValidationMode
    status: ValidationStatus
    started_at: AwareDatetime
    completed_at: AwareDatetime
    duration_ms: int = Field(ge=0)
    checks: list[ValidationCheckSummary] = Field(default_factory=list)
    issues: list[ValidationIssueRef] = Field(default_factory=list)
    issue_counts: IssueCounts = Field(default_factory=IssueCounts)
    model_input_allowed: bool
    registration_allowed: bool
    validated_file_count: int = Field(ge=0)
    declared_file_count: int = Field(ge=0)
    unregistered_file_count: int = Field(ge=0)
    result_sha256: str
    # Phase 11C-2c-prerequisite-fix-1：权威验证事实（EvidencePackage.evidence_level 写入）。
    # 向后兼容：旧版 sidecar 缺字段时反序列化为 None（None 表示不具备新任务准入条件）。
    evidence_level: Optional[EvidenceLevel] = None

    @field_validator("manifest_sha256", "result_sha256")
    @classmethod
    def _hash(cls, v: str) -> str:
        return validate_sha256(v)

    _validate_started_at = field_validator("started_at")(_validate_time)
    _validate_completed_at = field_validator("completed_at")(_validate_time)

    @model_validator(mode="after")
    def _check_times(self) -> "ValidationResult":
        if self.completed_at < self.started_at:
            raise ValueError("completed_at 不得早于 started_at")
        return self


class ValidationIssue(BaseModel):
    """结构化问题记录（契约 6.2 / 14.5），只保存脱敏摘要。"""

    model_config = ConfigDict(extra="forbid")

    issue_id: str = Field(min_length=1)
    code: str = Field(min_length=1)
    severity: IssueSeverity
    stage: str = Field(min_length=1)
    message_key: str = Field(min_length=1)
    location: str = Field(min_length=1)
    file_ref: Optional[str] = None
    field_path: Optional[str] = None
    expected_summary: Optional[str] = Field(default=None, max_length=SUMMARY_MAX_LENGTH)
    actual_summary: Optional[str] = Field(default=None, max_length=SUMMARY_MAX_LENGTH)
    retryable: bool
    blocks_registration: bool
    blocks_model_input: bool
    requires_manual_review: bool
    created_at: AwareDatetime

    @field_validator("file_ref")
    @classmethod
    def _file_ref(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return _validate_ref(v)
        return v

    _validate_created_at = field_validator("created_at")(_validate_time)


class RegistrationIndexEntry(BaseModel):
    """可重建查询投影（契约 7）。"""

    model_config = ConfigDict(extra="forbid")

    index_schema_version: Literal["evidence-sidecar/index/v1"] = "evidence-sidecar/index/v1"
    entry_id: str = Field(min_length=1)
    record_id: str = Field(min_length=1)
    package_id: str
    package_revision: int = Field(gt=0)
    batch_id: str
    submission_id: str
    evidence_version: str = Field(min_length=1)
    manifest_sha256: str
    registration_status: RegistrationStatus
    latest_validation_status: ValidationStatus
    model_input_allowed: bool
    record_ref: str
    latest_validation_ref: str
    registered_at: AwareDatetime
    updated_at: AwareDatetime
    revision: int = Field(ge=1)

    @field_validator("manifest_sha256")
    @classmethod
    def _hash(cls, v: str) -> str:
        return validate_sha256(v)

    @field_validator("record_ref", "latest_validation_ref")
    @classmethod
    def _ref(cls, v: str) -> str:
        return _validate_ref(v)

    _validate_registered_at = field_validator("registered_at")(_validate_time)
    _validate_updated_at = field_validator("updated_at")(_validate_time)


def utc_now() -> datetime:
    """返回带时区的当前 UTC 时间（测试与合成数据辅助）。"""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- RegistrationIndex 容器


INDEX_CONTAINER_SCHEMA_VERSION = "evidence-sidecar/index-container/v1"


def _entry_sort_key(entry: "RegistrationIndexEntry") -> Tuple[str, int]:
    """稳定组合键排序：package_id 升序，其次 package_revision 数值升序。"""
    return entry.package_id, entry.package_revision


def compute_index_sha256(index: "RegistrationIndex") -> str:
    """按 fix-4 契约计算容器 index_sha256：自身字段置 null 后稳定序列化。"""
    obj = index.model_dump(mode="json")
    obj["index_sha256"] = None
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_hex(raw)


def _sha256_hex(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


class RegistrationIndex(BaseModel):
    """RegistrationIndex 容器（Phase11A-fix-4 契约）。

    - 容器包含多个 RegistrationIndexEntry，按 (package_id, package_revision) 排序并存。
    - index_sha256 为容器自身规范化内容哈希（计算时自身字段置 null）。
    - 未知字段拒绝；UTC 时间；revision 正整数。
    """

    model_config = ConfigDict(extra="forbid")

    index_schema_version: Literal["evidence-sidecar/index-container/v1"] = INDEX_CONTAINER_SCHEMA_VERSION
    index_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    entries: List[RegistrationIndexEntry] = Field(default_factory=list)
    index_sha256: str

    _validate_created_at = field_validator("created_at")(_validate_time)
    _validate_updated_at = field_validator("updated_at")(_validate_time)

    @field_validator("index_sha256")
    @classmethod
    def _hash(cls, v: str) -> str:
        return validate_sha256(v)

    @model_validator(mode="after")
    def _check_entries(self) -> "RegistrationIndex":
        if self.updated_at < self.created_at:
            raise ValueError("updated_at 不得早于 created_at")
        # 稳定排序校验
        keys = [(e.package_id, e.package_revision) for e in self.entries]
        if keys != sorted(keys):
            raise ValueError("entries 必须按 (package_id, package_revision) 升序")
        # 同一组合键不得重复
        if len(keys) != len(set(keys)):
            raise ValueError("entries 组合键 (package_id, package_revision) 不得重复")
        return self
