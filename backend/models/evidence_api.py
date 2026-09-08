"""
EvidencePackage 导入 API 请求/响应模型（Phase 11B-2a）。

冻结 Phase 11B-2 API 外观，本阶段不创建 router / 不修改 main.py。

设计纪律：
- Pydantic 使用项目现有版本与写法；extra="forbid"。
- 时间字段必须带时区（AwareDatetime）。
- 数字必须有限（allow_inf_nan=False），拒绝 NaN/Infinity。
- 枚举优先复用现有 Evidence 模型枚举。
- 不引入数据库 ORM 模型，不新增数据库字段。
- 不把内部 Pydantic 对象完整透传给 API；安全响应模型仅含必要字段。

路径安全（package_ref）：
- 必须是受控输入根下的 POSIX 相对目录引用。
- 不接受服务器绝对路径；拒绝 `..`、反斜杠、盘符、空字节和空值。
- API 不接受真实本地任意路径。

None 语义（契约冻结）：
- get_registration 未登记返回 None -> HTTP 404（本阶段模型层以 Optional 表达）。
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import List, Optional

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from models.evidence_package import EvidenceLevel
from models.evidence_sidecar import (
    IssueCounts,
    IssueSeverity,
    RegistrationStatus,
    ValidationMode,
    ValidationStatus,
)

# ------------------------------------------------------------------ 路径安全

_PACKAGE_REF_RE = re.compile(r"^[A-Za-z0-9_\-\./]+$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def validate_package_ref(value: str) -> str:
    """校验受控输入根下的 POSIX 相对目录引用（安全规则见模块 docstring）。"""
    if not isinstance(value, str) or not value:
        raise ValueError("package_ref 不能为空")
    if value.startswith("/") or _DRIVE_RE.match(value):
        raise ValueError("package_ref 不接受服务器绝对路径或盘符")
    if "\\" in value or "\x00" in value:
        raise ValueError("package_ref 含非法字符（反斜杠/空字节）")
    if not _PACKAGE_REF_RE.match(value):
        raise ValueError("package_ref 含非法字符")
    parts = value.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError("package_ref 含空段或 .. 路径穿越")
    return value


def _ensure_aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("时间必须带时区")
    return v


# ------------------------------------------------------------------ 请求模型


class EvidenceDryRunRequest(BaseModel):
    """单包 dry-run 请求。"""

    model_config = ConfigDict(extra="forbid")

    package_ref: str = Field(min_length=1)

    _validate_package_ref = field_validator("package_ref")(validate_package_ref)


class EvidenceBatchDryRunRequest(BaseModel):
    """批次 dry-run 请求（1—1000 项，不重复，保留顺序）。"""

    model_config = ConfigDict(extra="forbid")

    package_refs: List[str] = Field(min_length=1, max_length=1000)
    fail_fast: bool = False

    @field_validator("package_refs")
    @classmethod
    def _validate_package_refs(cls, v: List[str]) -> List[str]:
        for ref in v:
            validate_package_ref(ref)
        return v

    @model_validator(mode="after")
    def _check_unique(self) -> "EvidenceBatchDryRunRequest":
        seen: set[str] = set()
        for ref in self.package_refs:
            if ref in seen:
                raise ValueError("package_refs 不允许重复引用")
            seen.add(ref)
        return self


class EvidenceRegisterRequest(BaseModel):
    """正式登记请求。不接受任何评分、Provider、Prompt 或数据库参数。"""

    model_config = ConfigDict(extra="forbid")

    package_ref: str = Field(min_length=1)
    expected_index_revision: Optional[int] = None

    _validate_package_ref = field_validator("package_ref")(validate_package_ref)

    @field_validator("expected_index_revision", mode="before")
    @classmethod
    def _revision(cls, v: object) -> object:
        if v is None:
            return v
        # 精确类型：只接受 int（且非 bool）；拒绝 bool、float（含 1.0）、字符串
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError("expected_index_revision 只接受 null/0/正整数，拒绝布尔、浮点、字符串")
        if v < 0:
            raise ValueError("expected_index_revision 允许 None/0/正整数，禁止负数")
        return v

# ------------------------------------------------------------------ 安全响应模型


class EvidenceIssueResponse(BaseModel):
    """问题安全响应：仅 issue_id / code / severity / message_key。

    不得返回原始正文、绝对路径、学生个人信息、traceback 或内部异常字符串。
    """

    model_config = ConfigDict(extra="forbid")

    issue_id: str = Field(min_length=1)
    code: str = Field(min_length=1)
    severity: IssueSeverity
    message_key: str = Field(min_length=1)


class EvidenceValidationResponse(BaseModel):
    """验证结果安全响应：仅必要字段，不暴露 actual_summary/输入正文/服务器路径。"""

    model_config = ConfigDict(extra="forbid")

    validation_id: str = Field(min_length=1)
    package_id: str = Field(min_length=1)
    package_revision: int = Field(gt=0)
    mode: ValidationMode
    status: ValidationStatus
    evidence_level: Optional[EvidenceLevel] = None
    registration_allowed: bool
    model_input_allowed: bool
    issue_counts: IssueCounts = Field(default_factory=IssueCounts)
    # 安全 Issue 模型（含 message_key），非内部 ValidationIssueRef；
    # router 负责把 ValidationResult.issues 与 ValidationIssue 合并映射。
    issues: List["EvidenceIssueResponse"] = Field(default_factory=list)
    started_at: AwareDatetime
    completed_at: AwareDatetime

    _validate_started_at = field_validator("started_at")(_ensure_aware)
    _validate_completed_at = field_validator("completed_at")(_ensure_aware)

    @model_validator(mode="after")
    def _check_times(self) -> "EvidenceValidationResponse":
        if self.completed_at < self.started_at:
            raise ValueError("completed_at 不得早于 started_at")
        return self


class EvidenceBatchItemResponse(BaseModel):
    """批次单项响应：package_ref 只能回显调用方提交的安全相对引用。"""

    model_config = ConfigDict(extra="forbid")

    package_ref: str = Field(min_length=1)
    result: Optional[EvidenceValidationResponse] = None
    error: Optional["EvidenceApiError"] = None

    _validate_package_ref = field_validator("package_ref")(validate_package_ref)

    @model_validator(mode="after")
    def _check_exactly_one(self) -> "EvidenceBatchItemResponse":
        if (self.result is None) == (self.error is None):
            raise ValueError("result 与 error 必须且只能出现一个")
        return self


class EvidenceBatchDryRunResponse(BaseModel):
    """批次 dry-run 响应，含计数不变量：total == passed + passed_with_warnings + failed + internal_error。"""

    model_config = ConfigDict(extra="forbid")

    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    passed_with_warnings: int = Field(ge=0)
    failed: int = Field(ge=0)
    internal_error: int = Field(ge=0)
    items: List[EvidenceBatchItemResponse] = Field(default_factory=list)

    @model_validator(mode="after")
    def _counts_invariant(self) -> "EvidenceBatchDryRunResponse":
        if len(self.items) != self.total:
            raise ValueError("items 数量必须等于 total")
        if self.total != self.passed + self.passed_with_warnings + self.failed + self.internal_error:
            raise ValueError("total 必须等于 passed + passed_with_warnings + failed + internal_error")
        return self


class EvidenceRegistrationResponse(BaseModel):
    """登记响应（不变量冻结，Phase 11B-2a-fix-1）。

    rejected=true ：validation 必须存在（结构化验证依据）；record_id/entry_id/index_revision
                    必须为 null；idempotent_hit 必须为 false。
    rejected=false：validation/record_id/entry_id 必须存在；index_revision 必须存在且 >=1。
    idempotent_hit=true 要求 rejected=false；不允许"成功响应但所有 ID 均为空"。
    """

    model_config = ConfigDict(extra="forbid")

    idempotent_hit: bool
    rejected: bool
    record_id: Optional[str] = None
    entry_id: Optional[str] = None
    package_id: str = Field(min_length=1)
    package_revision: int = Field(gt=0)
    validation: Optional[EvidenceValidationResponse] = None
    index_revision: Optional[int] = None

    @model_validator(mode="after")
    def _invariants(self) -> "EvidenceRegistrationResponse":
        if self.rejected:
            if self.idempotent_hit:
                raise ValueError("rejected 登记不允许 idempotent_hit=true")
            if self.validation is None:
                raise ValueError("rejected 登记必须携带结构化 validation")
            if (
                self.record_id is not None
                or self.entry_id is not None
                or self.index_revision is not None
            ):
                raise ValueError("rejected 登记不得伪造 record_id/entry_id/index_revision")
        else:
            if self.validation is None:
                raise ValueError("成功登记必须携带 validation")
            if not self.record_id or not self.entry_id:
                raise ValueError("成功登记必须携带 record_id 与 entry_id")
            if self.index_revision is None or self.index_revision < 1:
                raise ValueError("成功登记必须携带 index_revision >= 1")
        return self


class EvidenceRegistrationSummary(BaseModel):
    """列表元素：仅 Index 中用于展示的安全元数据。"""

    model_config = ConfigDict(extra="forbid")

    package_id: str = Field(min_length=1)
    package_revision: int = Field(gt=0)
    batch_id: str = Field(min_length=1)
    submission_id: str = Field(min_length=1)
    registration_status: RegistrationStatus
    latest_validation_status: ValidationStatus
    model_input_allowed: bool
    manifest_sha256: str = Field(min_length=64, max_length=64)


class EvidenceRegistrationListResponse(BaseModel):
    """登记列表响应。"""

    model_config = ConfigDict(extra="forbid")

    total: int = Field(ge=0)
    items: List[EvidenceRegistrationSummary] = Field(default_factory=list)

    @model_validator(mode="after")
    def _total_matches(self) -> "EvidenceRegistrationListResponse":
        if len(self.items) != self.total:
            raise ValueError("items 数量必须等于 total")
        return self


class EvidenceRegistrationDetailResponse(BaseModel):
    """登记详情响应：使用安全响应子模型，不直接返回内部对象。"""

    model_config = ConfigDict(extra="forbid")

    package_id: str = Field(min_length=1)
    package_revision: int = Field(gt=0)
    record_id: str = Field(min_length=1)
    entry_id: str = Field(min_length=1)
    registration_status: RegistrationStatus
    record_revision: int = Field(ge=1)
    latest_validation: EvidenceValidationResponse
    issues: List[EvidenceIssueResponse] = Field(default_factory=list)


class EvidenceApiError(BaseModel):
    """稳定错误响应：不包含 exception / traceback / absolute_path / raw_response。"""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    message_key: str = Field(min_length=1)
    retryable: bool


# 前向引用解析
EvidenceBatchItemResponse.model_rebuild()


def utc_now() -> datetime:
    """当前 UTC 时间（带时区，供合成测试与文档示例辅助）。"""
    return datetime.now(timezone.utc)
