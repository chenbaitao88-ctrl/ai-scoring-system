"""
PipelineTask API 契约数据模型（Phase 11C-3）。

严格依据《Phase11C-3-PipelineTaskAPI契约.md》实现。
字段白名单：不把内部对象无筛选 model_dump() 返回；不暴露绝对路径/原始 evidence/
学生信息/异常原文/traceback/Token/Provider 密钥/configuration_snapshot 自由文本。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------- 稳定错误

class PipelineApiError(BaseModel):
    """稳定错误响应：不包含 exception/traceback/absolute_path/raw_response。"""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    message_key: str = Field(min_length=1)
    retryable: bool


# ---------------------------------------------------------------- 创建输入

class PipelinePackageRefBody(BaseModel):
    """创建任务时单个 package revision 的非敏感引用。"""

    model_config = ConfigDict(extra="forbid")

    package_id: str
    package_revision: int = Field(gt=0)
    manifest_sha256: str
    registration_record_id: str
    validation_id: str

    @field_validator("package_id", "registration_record_id", "validation_id")
    @classmethod
    def _ascii_ids(cls, v: str) -> str:
        import re

        if not re.match(r"^[A-Za-z0-9_\-\.]+$", v):
            raise ValueError("标识符必须为 ASCII 安全标识符")
        return v

    @field_validator("manifest_sha256")
    @classmethod
    def _hash(cls, v: str) -> str:
        import re

        if not re.match(r"^[0-9a-f]{64}$", v):
            raise ValueError("manifest_sha256 必须为 64 位小写十六进制")
        return v


class PipelineTaskCreateBody(BaseModel):
    """创建请求体：调用方不得传 task_id/item_id/idempotency_key/指纹/状态。"""

    model_config = ConfigDict(extra="forbid")

    batch_id: str
    package_refs: List[PipelinePackageRefBody] = Field(min_length=1, max_length=1000)
    concurrency: int = Field(ge=1, le=16)
    configuration_snapshot: Dict[str, Any]

    @field_validator("batch_id")
    @classmethod
    def _batch_id(cls, v: str) -> str:
        import re

        if not re.match(r"^[A-Za-z0-9_\-\.]+$", v):
            raise ValueError("batch_id 必须为 ASCII 安全标识符")
        return v

    @model_validator(mode="after")
    def _no_duplicate(self) -> "PipelineTaskCreateBody":
        keys = [(r.package_id, r.package_revision) for r in self.package_refs]
        if len(keys) != len(set(keys)):
            raise ValueError("package_id + package_revision 不得重复")
        return self


# ---------------------------------------------------------------- 控制请求

class ExpectedRevisionBody(BaseModel):
    """start/pause/heartbeat/apply 的 expected revision 载体。"""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    expected_item_revision: Optional[int] = Field(default=None, ge=1)


class ResumeRequestBody(BaseModel):
    """恢复请求体：只接受契约字段，不接受调用方指定 decision。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    mode: Literal["resume_pending", "resume_retryable_failed", "recover_stale"]
    expected_revision: int = Field(ge=1)
    expected_item_revisions: Dict[str, int] = Field(default_factory=dict)
    item_ids: List[str] = Field(default_factory=list)
    reason_code: str = Field(min_length=1)
    dry_run: bool = False

    @field_validator("request_id")
    @classmethod
    def _request_id(cls, v: str) -> str:
        import re

        if not re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", v):
            raise ValueError("request_id 必须为 UUID")
        return v


class ApplyBody(BaseModel):
    """apply 决定：必须携带 expected task revision。"""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class HeartbeatBody(BaseModel):
    """心跳：expected task/item revision。"""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    expected_item_revision: int = Field(ge=1)


# ---------------------------------------------------------------- 响应白名单

class PipelineStageSummaryResponse(BaseModel):
    """stage summary 白名单（不含自由文本/阻断码原文以外的字段）。"""

    model_config = ConfigDict(extra="forbid")

    stage: str
    status: str
    depends_on: List[str] = Field(default_factory=list)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    total_items: int = Field(ge=0)
    pending_items: int = Field(ge=0)
    running_items: int = Field(ge=0)
    completed_items: int = Field(ge=0)
    failed_items: int = Field(ge=0)
    skipped_items: int = Field(ge=0)
    manual_review_items: int = Field(ge=0)
    blocking_error_codes: List[str] = Field(default_factory=list)
    revision: int = Field(ge=1)


class PipelineTaskResponse(BaseModel):
    """任务响应白名单。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    batch_id: str
    status: str
    current_stage: Optional[str] = None
    execution_scope: List[str]
    total_items: int = Field(ge=0)
    pending_items: int = Field(ge=0)
    running_items: int = Field(ge=0)
    completed_items: int = Field(ge=0)
    failed_items: int = Field(ge=0)
    skipped_items: int = Field(ge=0)
    manual_review_items: int = Field(ge=0)
    concurrency: int = Field(ge=1)
    stage_summaries: List[PipelineStageSummaryResponse] = Field(default_factory=list)
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    paused_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    revision: int = Field(ge=1)
    last_event_sequence: int = Field(ge=0)
    idempotent_hit: bool = False


class PipelineItemOutputResponse(BaseModel):
    """item 输出摘要：只返回是否存在与哈希，不暴露本地绝对路径。"""

    model_config = ConfigDict(extra="forbid")

    present: bool
    sha256: Optional[str] = None


class PipelineItemLastErrorResponse(BaseModel):
    """last_error 白名单：仅稳定 code/message_key/retryable。"""

    model_config = ConfigDict(extra="forbid")

    code: str
    message_key: str
    retryable: bool


class PipelineItemResponse(BaseModel):
    """item 响应白名单。"""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    task_id: str
    package_id: str
    package_revision: int = Field(gt=0)
    evidence_level: str
    status: str
    current_stage: str
    attempt_count: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    retryable: bool
    heartbeat_updated_at: Optional[datetime] = None
    output: PipelineItemOutputResponse
    last_error: Optional[PipelineItemLastErrorResponse] = None
    item_revision: int = Field(ge=1)


class PipelineEventResponse(BaseModel):
    """事件响应白名单：metadata 仅返回白名单键。"""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    task_id: str
    sequence: int = Field(ge=1)
    event_type: str
    occurred_at: Optional[datetime] = None
    stage: Optional[str] = None
    item_id: Optional[str] = None
    attempt_id: Optional[str] = None
    revision_before: int = Field(ge=0)
    revision_after: int = Field(ge=1)
    reason_code: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class PipelineTaskListResponse(BaseModel):
    """列表响应：分页。"""

    model_config = ConfigDict(extra="forbid")

    items: List[PipelineTaskResponse] = Field(default_factory=list)
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1)


class PipelineItemListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[PipelineItemResponse] = Field(default_factory=list)
    total: int = Field(ge=0)


class PipelineEventListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[PipelineEventResponse] = Field(default_factory=list)
    total: int = Field(ge=0)


class ResumeRequestResponse(BaseModel):
    """恢复请求响应白名单。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    task_id: str
    requested_at: Optional[datetime] = None
    mode: str
    expected_revision: int = Field(ge=1)
    expected_item_revisions: Dict[str, int] = Field(default_factory=dict)
    item_ids: List[str] = Field(default_factory=list)
    reason_code: str
    dry_run: bool


class ResumeDecisionResponse(BaseModel):
    """恢复决定响应白名单。"""

    model_config = ConfigDict(extra="forbid")

    decision_id: str
    request_id: str
    task_id: str
    decided_at: Optional[datetime] = None
    approved: bool
    task_revision_before: int = Field(ge=1)
    eligible_items: List[Dict[str, Any]] = Field(default_factory=list)
    protected_items: List[Dict[str, Any]] = Field(default_factory=list)
    rejected_items: List[Dict[str, Any]] = Field(default_factory=list)
    decision_error_codes: List[str] = Field(default_factory=list)
    dry_run: bool


class ResumeDecisionApplyResponse(BaseModel):
    """apply 后响应：更新后的 task + 受影响 item 摘要。"""

    model_config = ConfigDict(extra="forbid")

    task: PipelineTaskResponse
    changed_items: List[PipelineItemResponse] = Field(default_factory=list)
