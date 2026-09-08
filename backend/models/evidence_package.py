"""
EvidencePackage v1.1 契约数据模型（Phase 11B-1a）。

严格依据《Phase11A-2a-EvidencePackage契约.md》v1.1 实现，不重新设计字段。
本阶段只实现不依赖文件系统的确定性格式与取值约束：

- 未知字段默认拒绝（extra="forbid"）。
- 必填字段缺失即失败。
- package_revision 为正整数。
- SHA-256 为 64 位小写十六进制。
- 时间字段为带时区 datetime（RFC 3339）。
- 路径字段仅保存 POSIX 风格相对路径文本，只做格式级约束，不访问文件系统。
- 不允许 NaN/Infinity 等非有限数字。

本阶段不实现：哈希计算、ready marker 文件读取、双 JSON 一致性服务、文件扫描。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

CONTRACT_VERSION = "evidence-package/v1.1"
COMPLETION_MARKER = ".evidence-ready"

# ---------------------------------------------------------------- 枚举

PublicationStatus = Literal["staging", "ready", "rejected"]
EvidenceLevel = Literal["sufficient", "limited", "insufficient", "manual_only"]
EvidenceType = Literal[
    "code_summary",
    "document_summary",
    "video_summary",
    "frame_ocr_summary",
    "transcript_summary",
    "archive_summary",
    "runtime_observation",
    "other_summary",
]
AvailabilityState = Literal["available", "partial", "missing", "blocked", "not_applicable"]
ItemAvailability = Literal["available", "partial", "missing", "blocked"]
PrivacyStatus = Literal["passed", "failed", "needs_review"]
FileRole = Literal[
    "code_summary",
    "document_summary",
    "video_summary",
    "safe_frame",
    "frame_ocr_summary",
    "transcript_redacted",
    "archive_summary",
    "supporting_metadata",
]
MediaType = Literal["json", "text", "image", "archive_metadata", "other"]
FilePrivacyStatus = Literal["passed", "failed", "needs_review", "not_applicable"]

# ---------------------------------------------------------------- 格式校验

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# 标识符禁止字符（契约 4.2：禁止空格、斜杠、反斜杠、冒号、查询参数等）
_IDENTIFIER_FORBIDDEN = re.compile(r'[ \t/\\:?"<>|]')


def validate_sha256(value: str) -> str:
    """SHA-256 必须是 64 位小写十六进制。"""
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError("sha256 必须是 64 位小写十六进制")
    return value


def validate_relative_path(value: str) -> str:
    """POSIX 风格相对路径格式级校验（不访问文件系统）。

    拒绝：空串、绝对路径、Windows 盘符路径、反斜杠、`..` 路径穿越、
    含空段（如 `a//b` 或尾部斜杠）。
    """
    if not isinstance(value, str) or not value:
        raise ValueError("相对路径不能为空")
    if value.startswith("/"):
        raise ValueError("绝对路径不允许（不得以 / 开头）")
    if re.match(r"^[A-Za-z]:", value):
        raise ValueError("盘符路径不允许")
    if "\\" in value:
        raise ValueError("反斜杠不允许（须使用 POSIX 风格）")
    segments = value.split("/")
    if any(seg == ".." for seg in segments):
        raise ValueError("`..` 路径穿越不允许")
    if any(seg == "" for seg in segments):
        raise ValueError("路径段不能为空（不允许 a//b 或尾部斜杠）")
    return value


def _validate_identifier(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("标识符不能为空")
    if len(value) > 128:
        raise ValueError("标识符长度不能超过 128")
    if _IDENTIFIER_FORBIDDEN.search(value):
        raise ValueError("标识符包含禁止字符（空格/斜杠/反斜杠/冒号/问号等）")
    return value


def _validate_time(value: datetime) -> datetime:
    """时间必须严格为 RFC 3339 UTC（带时区且 offset 必须为 0）。

    拒绝 naive datetime 与非 UTC offset（如 +08:00）。
    """
    if value.tzinfo is None:
        raise ValueError("时间必须带时区（RFC 3339）")
    if value.utcoffset() != timedelta(0):
        raise ValueError("时间必须为 UTC（offset 必须为 0），不接受非 UTC 时区")
    return value


# ---------------------------------------------------------------- 子模型


class PrivacyCheck(BaseModel):
    """包级隐私检查结论（契约 6.6）。"""

    model_config = ConfigDict(extra="forbid")

    status: PrivacyStatus
    privacy_policy_version: str = Field(min_length=1)
    checked_at: AwareDatetime
    checker: str = Field(min_length=1)
    findings: list[str] = Field(default_factory=list)
    contains_face: bool
    contains_direct_identity: bool
    transcript_redacted: bool
    raw_audio_included: bool
    raw_video_included: bool

    _validate_checked_at = field_validator("checked_at")(_validate_time)


class EvidenceItem(BaseModel):
    """证据条目（契约 6.3）。"""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1)
    evidence_type: EvidenceType
    summary: str = Field(min_length=1)
    availability: ItemAvailability
    source_refs: list[str] = Field(default_factory=list)
    model_input_allowed: bool
    limitations: list[str] = Field(default_factory=list)


class EvidenceAvailability(BaseModel):
    """顶层 availability 固定字段（契约 6.5）。"""

    model_config = ConfigDict(extra="forbid")

    code: AvailabilityState
    document: AvailabilityState
    video: AvailabilityState
    transcript: AvailabilityState


class ManifestFileEntry(BaseModel):
    """manifest 文件条目（契约 7.3）。"""

    model_config = ConfigDict(extra="forbid")

    file_id: str = Field(min_length=1)
    relative_path: str
    file_role: FileRole
    mime_type: str = Field(min_length=1)
    media_type: MediaType
    size_bytes: int = Field(ge=0)
    sha256: str
    privacy_status: FilePrivacyStatus
    model_input_allowed: bool
    derived_from_raw: bool
    redaction_applied: bool

    @field_validator("relative_path")
    @classmethod
    def _path(cls, v: str) -> str:
        return validate_relative_path(v)

    @field_validator("sha256")
    @classmethod
    def _hash(cls, v: str) -> str:
        return validate_sha256(v)


# ---------------------------------------------------------------- 文档模型


class EvidencePackageCommon(BaseModel):
    """双 JSON 共同必填字段（契约 4.1）。"""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["evidence-package/v1.1"] = CONTRACT_VERSION
    package_id: str
    batch_id: str
    submission_id: str
    evidence_version: str = Field(min_length=1)
    package_revision: int = Field(gt=0)
    manifest_sha256: str
    generated_at: AwareDatetime
    generator_name: str = Field(min_length=1)
    generator_version: str = Field(min_length=1)
    publication_status: PublicationStatus
    completion_marker: Optional[str] = None

    @field_validator("package_id", "batch_id", "submission_id")
    @classmethod
    def _id(cls, v: str) -> str:
        return _validate_identifier(v)

    @field_validator("manifest_sha256")
    @classmethod
    def _hash(cls, v: str) -> str:
        return validate_sha256(v)

    _validate_generated_at = field_validator("generated_at")(_validate_time)

    @model_validator(mode="after")
    def _check_completion_marker(self) -> "EvidencePackageCommon":
        # 契约 5.1：ready 必须存在 .evidence-ready；staging/rejected 不得存在
        if self.publication_status == "ready":
            if self.completion_marker != COMPLETION_MARKER:
                raise ValueError("ready 状态必须声明 completion_marker=.evidence-ready")
        else:
            if self.completion_marker is not None:
                raise ValueError("非 ready 状态不得声明 completion_marker")
        return self


class EvidencePackage(EvidencePackageCommon):
    """evidence.json 文档模型（契约 6.x）。"""

    evidence_level: EvidenceLevel
    evidence_items: list[EvidenceItem] = Field(default_factory=list)
    availability: EvidenceAvailability
    missing_evidence: list[str] = Field(default_factory=list)
    manual_review_reasons: list[str] = Field(default_factory=list)
    privacy_check: PrivacyCheck
    rejection_reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_level_and_rejection(self) -> "EvidencePackage":
        # 契约 6.2：evidence_items 允许空数组，但等级不得为 sufficient
        if self.evidence_level == "sufficient" and not self.evidence_items:
            raise ValueError("evidence_level=sufficient 时 evidence_items 不能为空")
        # 契约 6.2：rejected 时 rejection_reasons 至少一项；其他状态为空数组
        if self.publication_status == "rejected" and not self.rejection_reasons:
            raise ValueError("publication_status=rejected 时 rejection_reasons 至少一项")
        if self.publication_status != "rejected" and self.rejection_reasons:
            raise ValueError("非 rejected 状态 rejection_reasons 必须为空数组")
        return self


class EvidenceManifest(EvidencePackageCommon):
    """evidence_manifest.json 文档模型（契约 7.x）。"""

    files: list[ManifestFileEntry] = Field(default_factory=list)
    privacy_check: PrivacyCheck


def utc_now() -> datetime:
    """返回带时区的当前 UTC 时间（测试与合成数据辅助）。"""
    return datetime.now(timezone.utc)
