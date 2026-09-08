"""
PipelineTask 文件型任务管理器——Pydantic 模型（Phase 11C-2a，fix-2 契约对齐）。

冻结契约：Phase11A-2b（PipelineTask / PipelineItemStatus v1）+ Phase11C-1 最小实施设计
（11C-1-fix-1 已关闭：数值冻结、事务协议、execution_scope、canonical 算法、待决策修正）。

fix-2 对齐要点：
- 任务六类计数与 item_index.status 聚合逐项一致（不只校验总和）。
- 终态 = completed / completed_with_errors / failed / cancelled，均须 completed_at。
- 状态与计数关系锁定（completed 无 pending/running/failed/manual_review 等）。
- PipelineItem 的 current_stage 限定 import/validate；output_ref/output_sha256 成对且
  completed 必须具备可验证输出。
- ID 强化：task_id/item_id/request_id/decision_id/transaction_id/event_id 为脱敏 UUID；
  其他标识为 ASCII 安全标识符（拒绝中文姓名/URL/路径/空白/自由文本）。
- canonical helper 输入严格校验；configuration_fingerprint 在任务权威快照持久化并校验。
- 契约对象字段补齐（StageSummary 依赖/时间/计数/阻断码/revision、ResumeRequest/Decision
  dry_run 与资格三分类、PipelineEvent 稳定事件类型）。

本模块只定义模型与静态不变量；合法状态转换、CAS、锁、原子写、崩溃恢复留给后续 service。
模型构造不读取文件系统、不生成或猜测业务结果。

安全边界：任何模型字段都不得保存学生姓名、手机号、网盘地址、原始视频、未脱敏转写、
Prompt 正文、Provider 请求/响应正文、本机绝对路径、API Key / token / credential value。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from models.evidence_package import validate_relative_path

# ---------------------------------------------------------------- 冻结枚举

PipelineStage = Literal["import", "validate", "score", "review", "export"]
PipelineTaskStatus = Literal[
    "pending", "running", "paused",
    "completed", "completed_with_errors", "failed", "cancelled",
]
PipelineItemStatus = Literal[
    "pending", "running", "completed", "failed", "skipped", "manual_review",
]
PipelineStageStatus = Literal[
    "not_started", "pending", "running", "completed", "failed", "skipped",
]
ResumeMode = Literal["resume_pending", "resume_retryable_failed", "recover_stale"]
PipelineTransactionStatus = Literal[
    "prepared", "snapshots_published", "event_appended", "committed",
]

# 稳定事件类型（契约 15.2）
PipelineEventType = Literal[
    "task_created", "task_started", "task_paused", "task_resumed",
    "task_completed", "task_failed", "task_cancelled",
    "item_started", "item_completed", "item_failed", "item_skipped",
    "item_sent_to_manual_review",
    "lease_acquired", "lease_expired",
    "resume_requested", "resume_decided",
    "successful_output_reused",
]

_STAGES: List[PipelineStage] = ["import", "validate", "score", "review", "export"]
_SCOPE: List[PipelineStage] = ["import", "validate"]  # evidence_preparation_pipeline 固定 scope

# Phase 11E-2a-prerequisite-impl-1：任务类型与固定 scope 映射（不允许调用方自由组合）
PipelineTaskType = Literal["evidence_preparation_pipeline", "scoring_pipeline"]
_TASK_SCOPES: Dict[str, List[PipelineStage]] = {
    "evidence_preparation_pipeline": ["import", "validate"],
    "scoring_pipeline": ["score", "review", "export"],
}

_TERMINAL_STATUSES = {"completed", "completed_with_errors", "failed", "cancelled"}
# Phase 11C-2c-prerequisite-fix-1：item 路由等级（insufficient 不得进入 PipelineTask）
ItemEvidenceLevel = Literal["sufficient", "limited", "manual_only"]
# 11E-2a-prerequisite-impl-2-fix-1：标识符/UUID/SHA-256 校验提取到 identity_validation，
# 消除 pipeline_task <-> scoring_configuration 运行期循环导入
from models.identity_validation import (  # noqa: E402
    _validate_ascii_id,
    _validate_sha256,
    _validate_uuid,
    canonical_json_bytes,
    sha256_canonical,
)

# ---------------------------------------------------------------- 纯函数辅助


def _validate_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("时间必须带时区（RFC 3339）")
    if value.utcoffset() != timedelta(0):
        raise ValueError("时间必须为 UTC（offset 必须为 0）")
    return value


# 11E-2a-prerequisite-impl-2：延迟顶层 import ScoringTaskConfiguration
# （放在 helper 定义之后，避免与 scoring_configuration 顶层 import 循环）
from models.scoring_configuration import ScoringTaskConfiguration


def _validate_ref(value: str) -> str:
    """安全 POSIX 相对引用（复用 EvidencePackage 相对路径规则）。"""
    return validate_relative_path(value)


def compute_item_input_fingerprint(
    *,
    package_id: str,
    package_revision: int,
    manifest_sha256: str,
    registration_record_id: str,
    validation_id: str,
) -> str:
    """item input_fingerprint（冻结，11C-1-fix-1 5.1）。

    严格校验输入：package_revision>0、manifest_sha256 为 64 位小写十六进制、
    package_id / registration_record_id / validation_id 为 ASCII 安全标识符。
    """
    if package_revision <= 0:
        raise ValueError("package_revision 必须为正整数")
    _validate_sha256(manifest_sha256)
    _validate_ascii_id(package_id)
    _validate_ascii_id(registration_record_id)
    _validate_ascii_id(validation_id)
    payload: Dict[str, Any] = {
        "package_id": package_id,
        "package_revision": package_revision,
        "manifest_sha256": manifest_sha256,
        "registration_record_id": registration_record_id,
        "validation_id": validation_id,
    }
    return sha256_canonical(payload)


def compute_task_idempotency(
    *,
    contract_version: str,
    task_type: str,
    batch_id: str,
    execution_scope: List[str],
    items: List[Tuple[str, int, str]],
    configuration_fingerprint: str,
    source_task_id: Optional[str] = None,
    source_item_ids: Optional[List[str]] = None,
) -> str:
    """task idempotency_key（冻结，11C-1-fix-1 5.1；11E-2a-prerequisite-impl-1 按 task_type 分派）。

    严格校验：contract_version 固定 pipeline-task/v1、task_type 只能是
    evidence_preparation_pipeline / scoring_pipeline、execution_scope 必须匹配该类型的
    固定 scope（evidence -> ["import","validate"]；scoring -> ["score","review","export"]）、
    item 的 package_revision>0、item fingerprint 与 configuration_fingerprint 为
    合法 SHA-256、拒绝相同 package_id + package_revision 重复项。
    """
    if contract_version != "pipeline-task/v1":
        raise ValueError("contract_version 必须为 pipeline-task/v1")
    if task_type not in _TASK_SCOPES:
        raise ValueError("task_type 必须为 evidence_preparation_pipeline 或 scoring_pipeline")
    if execution_scope != _TASK_SCOPES[task_type]:
        raise ValueError(
            f"execution_scope 必须匹配 task_type 固定 scope: {_TASK_SCOPES[task_type]}"
        )
    _validate_sha256(configuration_fingerprint)
    _validate_ascii_id(batch_id)

    seen: Dict[Tuple[str, int], bool] = {}
    ordered: List[Tuple[str, int, str]] = []
    for pkg_id, rev, fp in items:
        _validate_ascii_id(pkg_id)
        if rev <= 0:
            raise ValueError("package_revision 必须为正整数")
        _validate_sha256(fp)
        key = (pkg_id, rev)
        if key in seen:
            raise ValueError(f"相同 package_id + package_revision 重复项被拒绝: {pkg_id} rev={rev}")
        seen[key] = True
        ordered.append((pkg_id, rev, fp))
    ordered.sort(key=lambda t: (t[0], t[1]))
    payload: Dict[str, Any] = {
        "contract_version": contract_version,
        "task_type": task_type,
        "batch_id": batch_id,
        "execution_scope": execution_scope,
        "ordered_input_fingerprints": [fp for _, _, fp in ordered],
        "configuration_fingerprint": configuration_fingerprint,
    }
    # 11E-2a-prerequisite-impl-2：scoring 幂等键纳入 source 维度（evidence 不传时行为不变）
    if source_task_id is not None:
        payload["source_task_id"] = source_task_id
        if source_item_ids is not None:
            payload["ordered_source_item_ids"] = sorted(source_item_ids)
    return sha256_canonical(payload)


# ---------------------------------------------------------------- 子模型


class PipelineConfigurationSnapshot(BaseModel):
    """创建任务时冻结的非敏感配置快照。"""

    model_config = ConfigDict(extra="forbid")

    evidence_contract_version: str = Field(min_length=1)
    validator_version: str = Field(min_length=1)


class PipelineError(BaseModel):
    """单项/任务级错误（非敏感聚合，不保存正文或堆栈）。"""

    model_config = ConfigDict(extra="forbid")

    error_code: str = Field(min_length=1)
    message_key: str = Field(min_length=1)
    retryable: bool
    stage: PipelineStage


class PipelineErrorSummary(BaseModel):
    """非敏感错误聚合。"""

    model_config = ConfigDict(extra="forbid")

    count: int = Field(ge=0)
    codes: List[str] = Field(default_factory=list)


class PipelineItemIndexEntry(BaseModel):
    """任务级 item 索引引用（完整状态在 items/<item_id>.json）。"""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    task_type: PipelineTaskType = "evidence_preparation_pipeline"  # 11E-2a-prerequisite-impl-2
    source_item_id: Optional[str] = None  # 11E-2a-prerequisite-impl-2
    package_id: str
    package_revision: int = Field(gt=0)
    status: PipelineItemStatus
    current_stage: PipelineStage
    input_fingerprint: str
    item_revision: int = Field(ge=1)
    evidence_level: ItemEvidenceLevel

    @field_validator("item_id")
    @classmethod
    def _item_id(cls, v: str) -> str:
        return _validate_uuid(v)

    @field_validator("source_item_id")
    @classmethod
    def _source_item_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _validate_uuid(v)

    @field_validator("package_id")
    @classmethod
    def _pkg_id(cls, v: str) -> str:
        return _validate_ascii_id(v)

    @field_validator("input_fingerprint")
    @classmethod
    def _fp(cls, v: str) -> str:
        return _validate_sha256(v)

    @model_validator(mode="after")
    def _stage_by_type(self) -> "PipelineItemIndexEntry":
        if self.task_type == "scoring_pipeline":
            if self.current_stage not in ("score", "review", "export"):
                raise ValueError("scoring item_index current_stage 只能是 score/review/export")
            if self.source_item_id is None:
                raise ValueError("scoring item_index 必须提供 source_item_id")
        else:
            if self.current_stage not in ("import", "validate"):
                raise ValueError("Phase 11C item_index current_stage 只能是 import 或 validate")
            if self.source_item_id is not None:
                raise ValueError("evidence item_index 不得有 source_item_id")
        return self


class PipelineStageSummary(BaseModel):
    """五阶段之一的状态摘要（契约字段完整版）。"""

    model_config = ConfigDict(extra="forbid")

    stage: PipelineStage
    status: PipelineStageStatus
    depends_on: List[PipelineStage] = Field(default_factory=list)
    started_at: Optional[AwareDatetime] = None
    completed_at: Optional[AwareDatetime] = None
    total_items: int = Field(ge=0)
    pending_items: int = Field(ge=0)
    running_items: int = Field(ge=0)
    completed_items: int = Field(ge=0)
    failed_items: int = Field(ge=0)
    skipped_items: int = Field(ge=0)
    manual_review_items: int = Field(ge=0)
    blocking_error_codes: List[str] = Field(default_factory=list)
    revision: int = Field(ge=1)

    @field_validator("started_at", "completed_at")
    @classmethod
    def _times(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is None:
            return v
        return _validate_time(v)

    @model_validator(mode="after")
    def _stage_counts(self) -> "PipelineStageSummary":
        total = (
            self.pending_items + self.running_items + self.completed_items
            + self.failed_items + self.skipped_items + self.manual_review_items
        )
        if self.total_items != total:
            raise ValueError("stage total_items 必须等于六类计数之和")
        if self.completed_at is not None and self.started_at is not None and self.completed_at < self.started_at:
            raise ValueError("stage completed_at 不得早于 started_at")
        # 基础状态时间规则：仅阻止静态矛盾快照，不扩展业务推进逻辑
        if self.status == "not_started" and (self.started_at is not None or self.completed_at is not None):
            raise ValueError("not_started 阶段不得有 started_at/completed_at")
        if self.status == "pending" and self.completed_at is not None:
            raise ValueError("pending 阶段不得有 completed_at")
        if self.status == "running":
            if self.started_at is None:
                raise ValueError("running 阶段必须有 started_at")
            if self.completed_at is not None:
                raise ValueError("running 阶段不得有 completed_at")
        if self.status in ("completed", "failed", "skipped") and self.completed_at is None:
            raise ValueError("completed/failed/skipped 阶段必须有 completed_at")
        return self


class PipelineEvent(BaseModel):
    """追加式审计事件（events.ndjson 项，非当前状态权威；契约 15.1/15.2）。"""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["pipeline-task/v1"] = "pipeline-task/v1"
    event_id: str
    task_id: str
    sequence: int = Field(ge=1)
    event_type: PipelineEventType
    occurred_at: AwareDatetime
    stage: Optional[PipelineStage] = None
    item_id: Optional[str] = None
    attempt_id: Optional[str] = None
    revision_before: int = Field(ge=0)
    revision_after: int = Field(ge=1)
    reason_code: Optional[str] = None
    metadata: Dict[str, str] = Field(default_factory=dict)

    @field_validator("event_id", "task_id")
    @classmethod
    def _ids(cls, v: str) -> str:
        return _validate_uuid(v)

    @field_validator("item_id", "attempt_id")
    @classmethod
    def _opt_ids(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _validate_uuid(v)

    @field_validator("occurred_at")
    @classmethod
    def _time(cls, v: datetime) -> datetime:
        return _validate_time(v)

    @model_validator(mode="after")
    def _sequence_order(self) -> "PipelineEvent":
        if self.revision_after < self.revision_before:
            raise ValueError("revision_after 不得小于 revision_before")
        return self


class ResumeRequest(BaseModel):
    """恢复请求（resume-requests/<request_id>.json；契约 11.3）。"""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["pipeline-task/v1"] = "pipeline-task/v1"
    request_id: str
    task_id: str
    requested_at: AwareDatetime
    mode: ResumeMode
    expected_revision: int = Field(ge=1)
    expected_item_revisions: Dict[str, int] = Field(default_factory=dict)
    item_ids: List[str] = Field(default_factory=list)
    reason_code: str = Field(min_length=1)
    dry_run: bool

    @field_validator("request_id", "task_id")
    @classmethod
    def _ids(cls, v: str) -> str:
        return _validate_uuid(v)

    @field_validator("item_ids")
    @classmethod
    def _item_ids(cls, v: List[str]) -> List[str]:
        for item_id in v:
            _validate_uuid(item_id)
        return v

    @field_validator("expected_item_revisions")
    @classmethod
    def _expected_revs(cls, v: Dict[str, int]) -> Dict[str, int]:
        for item_id, rev in v.items():
            _validate_uuid(item_id)
            if rev < 1:
                raise ValueError("expected item revision 必须 >= 1")
        return v

    @field_validator("requested_at")
    @classmethod
    def _time(cls, v: datetime) -> datetime:
        return _validate_time(v)


class ResumeEligibleItem(BaseModel):
    """可恢复 item 及起始阶段（契约 11.4 eligible_items 项）。"""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    resume_from_stage: PipelineStage
    next_attempt_number: int = Field(ge=1)
    reason_code: str = Field(min_length=1)

    @field_validator("item_id")
    @classmethod
    def _item_id(cls, v: str) -> str:
        return _validate_uuid(v)


class ResumeProtectedItem(BaseModel):
    """受保护 item（已成功/人工/跳过/租约有效；契约 11.4 protected_items 项）。"""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    reason_code: str = Field(min_length=1)

    @field_validator("item_id")
    @classmethod
    def _item_id(cls, v: str) -> str:
        return _validate_uuid(v)


class ResumeRejectedItem(BaseModel):
    """不满足恢复条件的 item 与错误码（契约 11.4 rejected_items 项）。"""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    error_codes: List[str] = Field(default_factory=list)

    @field_validator("item_id")
    @classmethod
    def _item_id(cls, v: str) -> str:
        return _validate_uuid(v)


class ResumeDecision(BaseModel):
    """恢复决定（resume-decisions/<request_id>.json；契约 11.4）。"""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["pipeline-task/v1"] = "pipeline-task/v1"
    decision_id: str
    request_id: str
    task_id: str
    decided_at: AwareDatetime
    approved: bool
    task_revision_before: int = Field(ge=1)
    eligible_items: List[ResumeEligibleItem] = Field(default_factory=list)
    protected_items: List[ResumeProtectedItem] = Field(default_factory=list)
    rejected_items: List[ResumeRejectedItem] = Field(default_factory=list)
    decision_error_codes: List[str] = Field(default_factory=list)
    dry_run: bool

    @field_validator("decision_id", "request_id", "task_id")
    @classmethod
    def _ids(cls, v: str) -> str:
        return _validate_uuid(v)

    @field_validator("decided_at")
    @classmethod
    def _time(cls, v: datetime) -> datetime:
        return _validate_time(v)

    @model_validator(mode="after")
    def _approved_consistency(self) -> "ResumeDecision":
        if self.approved and not self.eligible_items:
            raise ValueError("approved=true 必须至少有一个 eligible item")
        return self


class PipelineTransaction(BaseModel):
    """单机事务目录元数据（tmp/transactions/<transaction_id>/transaction.json）。"""

    model_config = ConfigDict(extra="forbid")

    transaction_schema_version: Literal["pipeline-transaction/v1"] = "pipeline-transaction/v1"
    transaction_id: str
    task_id: str
    status: PipelineTransactionStatus
    expected_task_revision: int = Field(ge=1)
    expected_item_revisions: Dict[str, int] = Field(default_factory=dict)
    target_event_sequence: int = Field(ge=1)
    old_snapshot_refs: List[str] = Field(default_factory=list)
    new_snapshot_refs: List[str] = Field(default_factory=list)
    event_ref: Optional[str] = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @field_validator("transaction_id", "task_id")
    @classmethod
    def _ids(cls, v: str) -> str:
        return _validate_uuid(v)

    @field_validator("expected_item_revisions")
    @classmethod
    def _expected_revs(cls, v: Dict[str, int]) -> Dict[str, int]:
        for item_id, rev in v.items():
            _validate_uuid(item_id)
            if rev < 1:
                raise ValueError("expected item revision 必须 >= 1")
        return v

    @field_validator("old_snapshot_refs", "new_snapshot_refs")
    @classmethod
    def _refs(cls, v: List[str]) -> List[str]:
        for ref in v:
            _validate_ref(ref)
        return v

    @field_validator("event_ref")
    @classmethod
    def _event_ref(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _validate_ref(v)

    @field_validator("created_at", "updated_at")
    @classmethod
    def _times(cls, v: datetime) -> datetime:
        return _validate_time(v)

    @model_validator(mode="after")
    def _invariants(self) -> "PipelineTransaction":
        if self.updated_at < self.created_at:
            raise ValueError("updated_at 不得早于 created_at")
        if len(self.old_snapshot_refs) != len(self.new_snapshot_refs):
            raise ValueError("old_snapshot_refs 与 new_snapshot_refs 必须配对（数量一致）")
        if self.status in ("event_appended", "committed") and self.event_ref is None:
            raise ValueError("event_appended/committed 状态必须存在 event_ref")
        if self.status in ("prepared", "snapshots_published") and self.event_ref is not None:
            raise ValueError("prepared/snapshots_published 状态不得填写 event_ref")
        return self


# ---------------------------------------------------------------- 核心模型


class PipelineItem(BaseModel):
    """单项权威快照（items/<item_id>.json）。"""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["pipeline-item/v1"] = "pipeline-item/v1"
    task_type: PipelineTaskType = "evidence_preparation_pipeline"  # 11E-2a-prerequisite-impl-2
    source_item_id: Optional[str] = None  # 11E-2a-prerequisite-impl-2（scoring item 必填上游 item）
    item_id: str
    task_id: str
    package_id: str
    package_revision: int = Field(gt=0)
    manifest_sha256: str
    registration_record_id: str
    validation_id: str
    input_fingerprint: str
    idempotency_key: str
    status: PipelineItemStatus
    current_stage: PipelineStage
    attempt_count: int = Field(ge=0)
    max_attempts: Literal[3] = 3
    heartbeat_interval_seconds: Literal[60] = 60
    stale_after_seconds: Literal[300] = 300
    heartbeat_updated_at: Optional[AwareDatetime] = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    retryable: bool = False
    last_error: Optional[PipelineError] = None
    output_ref: Optional[str] = None
    output_sha256: Optional[str] = None
    item_revision: int = Field(ge=1)
    # 11E-2a-prerequisite-impl-3：review 阶段完成引用（非敏感，仅记录决策 ID 不存评语正文）
    review_decision_id: Optional[str] = None
    # Phase 11C-2c-prerequisite-fix-1：创建任务时冻结的非敏感路由快照（不允许 insufficient）
    evidence_level: ItemEvidenceLevel

    @field_validator("item_id", "task_id")
    @classmethod
    def _uuids(cls, v: str) -> str:
        return _validate_uuid(v)

    @field_validator("review_decision_id")
    @classmethod
    def _review_decision_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        # 11E-3b-2：契约衔接修复——11A-2d 建议 decision_id 为稳定 ID（如 dec-xxx），
        # 与 UUID 校验冲突；放宽为 ASCII 安全 ID（UUID 是其子集，拒绝中文/URL/路径/自由文本）
        return _validate_ascii_id(v)

    @field_validator("package_id", "registration_record_id", "validation_id")
    @classmethod
    def _ids(cls, v: str) -> str:
        return _validate_ascii_id(v)

    @field_validator("manifest_sha256", "input_fingerprint", "idempotency_key")
    @classmethod
    def _hashes(cls, v: str) -> str:
        return _validate_sha256(v)

    @field_validator("heartbeat_updated_at", "created_at", "updated_at")
    @classmethod
    def _times(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is None:
            return v
        return _validate_time(v)

    @field_validator("output_ref")
    @classmethod
    def _output_ref(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _validate_ref(v)

    @field_validator("output_sha256")
    @classmethod
    def _output_sha(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _validate_sha256(v)

    @field_validator("source_item_id")
    @classmethod
    def _source_item_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _validate_uuid(v)

    @model_validator(mode="after")
    def _invariants(self) -> "PipelineItem":
        # 11E-2a-prerequisite-impl-2：按 task_type 限定 current_stage 与 source_item_id
        if self.task_type == "scoring_pipeline":
            if self.current_stage not in ("score", "review", "export"):
                raise ValueError("scoring item current_stage 只能是 score/review/export")
            if self.source_item_id is None:
                raise ValueError("scoring item 必须提供 source_item_id")
            if self.source_item_id == self.item_id:
                raise ValueError("scoring item 不得复用 source item_id")
        else:
            if self.current_stage not in ("import", "validate"):
                raise ValueError("Phase 11C item current_stage 只能是 import 或 validate")
            if self.source_item_id is not None:
                raise ValueError("evidence item 不得有 source_item_id")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at 不得早于 created_at")
        if self.attempt_count > self.max_attempts:
            raise ValueError("attempt_count 不得超过 max_attempts")
        if self.status == "completed" and self.retryable:
            raise ValueError("completed item 不得 retryable=true")
        if self.status != "running" and self.heartbeat_updated_at is not None:
            raise ValueError("heartbeat_updated_at 仅允许 running item 使用")
        if self.status == "running" and self.heartbeat_updated_at is None:
            raise ValueError("running item 必须携带 heartbeat_updated_at")
        if (self.output_ref is None) != (self.output_sha256 is None):
            raise ValueError("output_ref 与 output_sha256 必须同时为空或同时存在")
        if self.status == "completed" and (self.output_ref is None or self.output_sha256 is None):
            raise ValueError("completed item 必须具备可验证输出引用和哈希")
        return self


class PipelineTask(BaseModel):
    """任务级权威投影（task.json）。"""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["pipeline-task/v1"] = "pipeline-task/v1"
    task_type: PipelineTaskType = "evidence_preparation_pipeline"
    execution_scope: List[PipelineStage] = Field(default_factory=lambda: list(_SCOPE))
    source_task_id: Optional[str] = None  # 11E-2a-prerequisite-impl-2（scoring 必填上游 task）
    task_id: str
    batch_id: str
    status: PipelineTaskStatus
    current_stage: Optional[PipelineStage] = None
    created_at: AwareDatetime
    started_at: Optional[AwareDatetime] = None
    updated_at: AwareDatetime
    paused_at: Optional[AwareDatetime] = None
    completed_at: Optional[AwareDatetime] = None
    total_items: int = Field(ge=0)
    pending_items: int = Field(ge=0)
    running_items: int = Field(ge=0)
    completed_items: int = Field(ge=0)
    failed_items: int = Field(ge=0)
    skipped_items: int = Field(ge=0)
    manual_review_items: int = Field(ge=0)
    concurrency: int = Field(gt=0)
    configuration_snapshot: Union[PipelineConfigurationSnapshot, "ScoringTaskConfiguration"]  # 按 task_type 分派校验
    configuration_fingerprint: str
    item_index: List[PipelineItemIndexEntry] = Field(default_factory=list)
    stage_summaries: List[PipelineStageSummary]
    error_summary: PipelineErrorSummary = Field(default_factory=PipelineErrorSummary)
    last_event_sequence: int = Field(ge=0)
    revision: int = Field(ge=1)
    idempotency_key: str
    idempotency_payload_sha256: str

    @field_validator("task_id")
    @classmethod
    def _task_id(cls, v: str) -> str:
        return _validate_uuid(v)

    @field_validator("source_task_id")
    @classmethod
    def _source_task_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _validate_uuid(v)

    @field_validator("batch_id")
    @classmethod
    def _batch_id(cls, v: str) -> str:
        return _validate_ascii_id(v)

    @field_validator("created_at", "started_at", "updated_at", "paused_at", "completed_at")
    @classmethod
    def _times(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is None:
            return v
        return _validate_time(v)

    @field_validator("execution_scope")
    @classmethod
    def _scope(cls, v: List[str]) -> List[str]:
        # 类型-范围匹配在 _invariants 中按 task_type 分派校验（11E-2a-prerequisite-impl-1）
        return list(v)

    @field_validator("configuration_fingerprint", "idempotency_key", "idempotency_payload_sha256")
    @classmethod
    def _hashes(cls, v: str) -> str:
        return _validate_sha256(v)

    @model_validator(mode="after")
    def _invariants(self) -> "PipelineTask":
        # ---- 类型与固定 scope 匹配（11E-2a-prerequisite-impl-1）----
        expected_scope = _TASK_SCOPES[self.task_type]
        if list(self.execution_scope) != expected_scope:
            raise ValueError(
                f"task_type {self.task_type} 的 execution_scope 必须是 {expected_scope}"
            )
        # 11E-2a-prerequisite-impl-2：source_task_id 与配置类型按 task_type 分派
        from models.scoring_configuration import ScoringTaskConfiguration
        if self.task_type == "scoring_pipeline":
            if self.source_task_id is None:
                raise ValueError("scoring task 必须提供 source_task_id")
            if not isinstance(self.configuration_snapshot, ScoringTaskConfiguration):
                raise ValueError("scoring task configuration_snapshot 必须是 ScoringTaskConfiguration")
        else:
            if self.source_task_id is not None:
                raise ValueError("evidence task 不得有 source_task_id")
            if not isinstance(self.configuration_snapshot, PipelineConfigurationSnapshot):
                raise ValueError("evidence task configuration_snapshot 必须是 PipelineConfigurationSnapshot")
        # scoring task 初始形态：current_stage 必须属于 score/review/export
        if self.task_type == "scoring_pipeline" and self.current_stage is not None:
            if self.current_stage not in ("score", "review", "export"):
                raise ValueError("scoring_pipeline current_stage 只能是 score/review/export")

        # ---- 时间顺序 ----
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("completed_at 不得早于 created_at")
        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError("started_at 不得早于 created_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at 不得早于 created_at")
        if self.paused_at is not None and self.paused_at < self.created_at:
            raise ValueError("paused_at 不得早于 created_at")

        # ---- 六类计数与 item_index 聚合逐项对账 ----
        agg = Counter(e.status for e in self.item_index)
        count_fields = {
            "pending_items": "pending",
            "running_items": "running",
            "completed_items": "completed",
            "failed_items": "failed",
            "skipped_items": "skipped",
            "manual_review_items": "manual_review",
        }
        for field, status in count_fields.items():
            if getattr(self, field) != agg.get(status, 0):
                raise ValueError(f"{field} 与 item_index.status 聚合不一致（期望 {agg.get(status, 0)}，实际 {getattr(self, field)}）")
        if self.total_items != sum(getattr(self, f) for f in count_fields):
            raise ValueError("total_items 必须等于六类 item 计数之和")
        if len(self.item_index) != self.total_items:
            raise ValueError("item_index 数量必须等于 total_items")

        # ---- item 唯一性 ----
        item_ids = [e.item_id for e in self.item_index]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("item_id 不得重复")
        pkg_keys = [(e.package_id, e.package_revision) for e in self.item_index]
        if len(pkg_keys) != len(set(pkg_keys)):
            raise ValueError("package_id + package_revision 不得重复")

        # ---- 五阶段完整且顺序固定；depends_on 与阶段状态按 task_type 分派 ----
        stages = [s.stage for s in self.stage_summaries]
        if stages != _STAGES:
            raise ValueError("stage_summaries 必须包含五阶段各一次且顺序固定")
        summary_by_stage = {s.stage: s for s in self.stage_summaries}
        if self.task_type == "scoring_pipeline":
            self._check_scoring_stage_invariants(summary_by_stage)
        else:
            # evidence_preparation_pipeline：11C 原规则不变（score/review/export 必须 not_started）
            frozen_deps: Dict[PipelineStage, List[PipelineStage]] = {
                "import": [],
                "validate": ["import"],
                "score": ["validate"],
                "review": ["validate", "score"],
                "export": ["review"],
            }
            for s in self.stage_summaries:
                if s.depends_on != frozen_deps[s.stage]:
                    raise ValueError(f"stage {s.stage} 的 depends_on 必须严格等于 {frozen_deps[s.stage]}")
                if s.stage in ("score", "review", "export") and s.status != "not_started":
                    raise ValueError("score/review/export 阶段在 Phase 11C 必须为 not_started")

        # ---- configuration_fingerprint 与快照一致性 ----
        # 11E-2a-prerequisite-impl-2：ScoringTaskConfiguration 排除 fingerprint 自身再哈希
        # （PipelineConfigurationSnapshot 无该字段，pop 无效果，evidence 行为不变）
        cfg_dump = self.configuration_snapshot.model_dump()
        cfg_dump.pop("configuration_fingerprint", None)
        expected_cfg_fp = sha256_canonical(cfg_dump)
        if self.configuration_fingerprint != expected_cfg_fp:
            raise ValueError("configuration_fingerprint 与 configuration_snapshot 不一致")

        # ---- 终态时间规则 ----
        if self.status in _TERMINAL_STATUSES and self.completed_at is None:
            raise ValueError("completed / completed_with_errors / failed / cancelled 必须有 completed_at")
        if self.status not in _TERMINAL_STATUSES and self.completed_at is not None:
            raise ValueError("非终态不得填写 completed_at")
        if self.status == "paused" and self.paused_at is None:
            raise ValueError("paused 必须有 paused_at")

        # ---- 状态与计数关系 ----
        if self.status == "completed":
            for field in ("pending_items", "running_items", "failed_items", "manual_review_items"):
                if getattr(self, field) != 0:
                    raise ValueError(f"completed 任务 {field} 必须为 0")
        if self.status == "completed_with_errors":
            if self.pending_items != 0 or self.running_items != 0:
                raise ValueError("completed_with_errors 任务 pending/running 必须为 0")
            if self.failed_items + self.skipped_items + self.manual_review_items == 0:
                raise ValueError("completed_with_errors 任务必须保留 failed/skipped/manual_review 至少一项")

        # ---- started_at 规则 ----
        if self.status == "pending" and self.started_at is not None:
            raise ValueError("pending 任务不得有 started_at")
        if self.status in ("running", "paused") or self.status in _TERMINAL_STATUSES:
            if self.started_at is None:
                raise ValueError("running/paused/终态任务必须有 started_at")

        # ---- current_stage 规则 ----
        if self.current_stage is None:
            if self.status in ("running", "paused"):
                raise ValueError("running/paused 任务 current_stage 不能为 null")
        else:
            # 11E-2a-prerequisite-impl-1：按 task_type 固定 scope 校验（evidence 行为不变）
            if self.current_stage not in self.execution_scope:
                raise ValueError("current_stage 必须属于 execution_scope")

        return self

    def _check_scoring_stage_invariants(
        self, summary_by_stage: Dict[PipelineStage, "PipelineStageSummary"],
    ) -> None:
        """scoring_pipeline 阶段不变量（11E-2a-prerequisite-impl-1-fix-1 / impl-3-fix-1）。

        - import/validate 为范围外阶段：依赖保留 11C 冻结值（结构兼容），状态必须 not_started，
          计数必须为零；不得被标记为 pending/running/completed/failed。
        - score/review/export 使用评分范围内依赖：score->[]、review->[score]、export->[review]；
          score 的上游前置由 source_task_id 与上游验证事实负责，不伪装为内部 validate 依赖。
        - 初始不变量（status=pending）：current_stage=score、score=pending、review/export=not_started。
        - 启动后（非 pending）：scoring_pipeline 是 item 级流水线，不同 item 可同时处于
          score/review/export 的不同阶段（错位推进）；depends_on 表达单个 item 的阶段顺序，
          不是整批全局屏障。单 item 的阶段顺序由 Manager 转换入口保证，模型不做整批屏障校验。
        """
        frozen_out = {"import": [], "validate": ["import"]}
        for stage in ("import", "validate"):
            s = summary_by_stage[stage]
            if s.depends_on != frozen_out[stage]:
                raise ValueError(f"stage {stage} 的 depends_on 必须严格等于 {frozen_out[stage]}")
            if s.status != "not_started":
                raise ValueError(f"scoring_pipeline 范围外阶段 {stage} 必须为 not_started")
            counts = (s.total_items, s.pending_items, s.running_items, s.completed_items,
                      s.failed_items, s.skipped_items, s.manual_review_items)
            if any(c != 0 for c in counts):
                raise ValueError(f"scoring_pipeline 范围外阶段 {stage} 计数必须为零")
        scoring_deps = {"score": [], "review": ["score"], "export": ["review"]}
        for stage, deps in scoring_deps.items():
            if summary_by_stage[stage].depends_on != deps:
                raise ValueError(f"stage {stage} 的 depends_on 必须严格等于 {deps}")
        score_s = summary_by_stage["score"]
        review_s = summary_by_stage["review"]
        export_s = summary_by_stage["export"]
        if self.status == "pending":
            if self.current_stage != "score":
                raise ValueError("pending scoring task current_stage 必须为 score")
            if score_s.status != "pending":
                raise ValueError("pending scoring task score 阶段必须为 pending")
            if review_s.status != "not_started" or export_s.status != "not_started":
                raise ValueError("pending scoring task review/export 阶段必须为 not_started")
        if self.current_stage == "score" and score_s.status == "not_started":
            raise ValueError("current_stage=score 时 score 阶段不得为 not_started")

# ---- 解析字符串注解（configuration_snapshot Union 含 ScoringTaskConfiguration）----
PipelineTask.model_rebuild()
