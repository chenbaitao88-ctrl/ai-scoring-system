"""
ReviewCase / ReviewDecision / ResultAdoption / ManualAdjustment / ReviewEvent 契约模型
（Phase 11E-3b-1）。

严格依据《Phase11A-2d-ScoreAttempt与ReviewCase契约.md》v1 实现；本文件是对该契约的
Phase 11E 显式扩展，不静默改写历史文档：

- 五类对象：ReviewCase（可推进投影 + 审计事件）、ReviewDecision（不可变）、
  ResultAdoption（状态可推进的审计记录）、ManualAdjustment（不可变）、ReviewEvent（不可变）。
- ResultAdoption 状态以本阶段总控冻结为准：proposed / blocked / adopted / superseded / revoked
  （契约 9.4 的 withdrawn/rejected 语义由 blocked/revoked 表达，见模块内 REASON 注释）。
- 21 个稳定复核原因码及其策略矩阵（契约 14.2）：写入 ReviewCase 时校验
  blocks_auto_adoption / blocks_export 与原因码矩阵一致（任一阻断即整体阻断）。
- 人工锁定保护：人工 adoption（decided_by.actor_type=reviewer）不得被自动 adoption 替换；
  must_review 类原因码阻断自动采用。
- 工单重开：保留旧工单与旧 resolution（previous_resolution_decision_id），reopen_count+1，
  不覆盖旧事件。

所有对象 extra="forbid"；只保存脱敏 ID、版本、哈希与受控原因码；
不保存学生敏感信息、证据正文、Prompt、模型原始响应、Key、endpoint。
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from models.score_attempt import ActorRef
from models.scoring_provider import (
    _validate_sha256,
    _validate_sha256_optional,
    _validate_stable_id,
    _validate_stable_id_optional,
)

_REVIEW_CONTRACT_VERSION = "score-attempt-review/v1"  # 共享上游契约版本
_REVIEW_CASE_SCHEMA = "review-case/v1"
_REVIEW_DECISION_SCHEMA = "review-decision/v1"
_RESULT_ADOPTION_SCHEMA = "result-adoption/v1"
_MANUAL_ADJUSTMENT_SCHEMA = "manual-adjustment/v1"
_REVIEW_EVENT_SCHEMA = "review-event/v1"
_MANUAL_FINAL_LOCK_SCHEMA = "manual-final-lock/v1"  # 11F-1b：人工最终锁定（关闭 N11）

# ---------------------------------------------------------------- 11F-1b 增量稳定错误码
# 11F-1a 契约 §2.4 / §3.6 冻结；模型层定义域错误码，store 引用。

# ManualFinalLock（契约 §3.6）
ERR_LOCK_NOT_FOUND = "REVIEW_LOCK_NOT_FOUND"
ERR_LOCK_CONFLICT = "REVIEW_LOCK_CONFLICT"
ERR_LOCK_CORRUPTED = "REVIEW_LOCK_CORRUPTED"
ERR_LOCK_WRITE_FAILED = "REVIEW_LOCK_WRITE_FAILED"
ERR_LOCK_ACTOR_MISMATCH = "REVIEW_LOCK_ACTOR_MISMATCH"
ERR_LOCK_REVISION_CONFLICT = "REVIEW_LOCK_REVISION_CONFLICT"
# ReviewDecision 控制面（契约 §2.4）
ERR_DECISION_REASON_MISMATCH = "REVIEW_DECISION_REASON_MISMATCH"
ERR_DECISION_TARGET_REQUIRED = "REVIEW_DECISION_TARGET_REQUIRED"
ERR_DECISION_STATE_NOT_DECISIONABLE = "REVIEW_CASE_STATE_NOT_DECISIONABLE"
ERR_DECISION_IDEMPOTENCY_CONFLICT = "REVIEW_DECISION_IDEMPOTENCY_CONFLICT"
ERR_DECISION_SUPERSEDES_NOT_FOUND = "REVIEW_DECISION_SUPERSEDES_NOT_FOUND"
ERR_DECISION_SUPERSEDES_SCOPE_MISMATCH = "REVIEW_DECISION_SUPERSEDES_SCOPE_MISMATCH"
# 恢复保护（契约 §3.4）：锁定阻断 outcome_unknown 自动恢复
ERR_RECOVERY_BLOCKED_BY_LOCK = "RECOVERY_BLOCKED_BY_LOCK"

# ---------------------------------------------------------------- 稳定复核原因码（契约 14.2，共 21 个）

# 每个原因码五个策略维度：trigger_stage / blocks_auto_adoption / blocks_export /
# rescore_allowed / human_action_required。多原因并存时任一阻断即整体阻断（契约 14.3）。
REVIEW_REASON_CODES: dict[str, tuple[str, bool, bool, bool, bool]] = {
    "EVIDENCE_INSUFFICIENT": ("validate", True, True, True, True),
    "EVIDENCE_REJECTED": ("validate", True, True, True, True),
    "MATERIAL_MISSING": ("import_validate", True, True, True, True),
    "MATERIAL_CORRUPTED": ("import_validate", True, True, True, True),
    "LINK_INVALID": ("import_validate", True, True, True, True),
    "LARGE_VIDEO_MANUAL": ("validate", True, True, True, True),
    "PROVIDER_CAPABILITY_MISMATCH": ("score", True, True, True, True),
    "PROVIDER_UNAVAILABLE": ("score", False, False, True, True),
    "ALL_ATTEMPTS_FAILED": ("score", True, True, True, True),
    "INVALID_MODEL_RESPONSE": ("score_validate", True, True, True, True),
    "SCORE_OUT_OF_RANGE": ("validate", True, True, True, True),
    "SCORE_COMPONENT_MISMATCH": ("validate", True, True, True, True),
    "RATIONALE_MISSING": ("validate", True, True, True, True),
    "UNSUPPORTED_INFERENCE": ("validate", True, True, True, True),
    "LOW_CONFIDENCE": ("validate_review", True, True, True, True),
    "HIGH_SCORE_VARIANCE": ("review", True, True, True, True),
    "HARD_FLAG_TRIGGERED": ("validate_review", True, True, True, True),
    "PRIVACY_RISK": ("any", True, True, True, True),
    "RANDOM_AUDIT": ("review", True, True, True, True),
    "APPEAL_REQUESTED": ("review", True, True, True, True),
    "EXPORT_RESULT_MISSING": ("export", True, True, False, True),
}

# must_review / 自动采用阻断语义：任一原因码阻断即整体阻断。
# blocks_auto_adoption / blocks_export 不得与原因码矩阵矛盾。
_REASON_BLOCKS_AUTO = frozenset(code for code, (_, ba, _, _, _) in REVIEW_REASON_CODES.items() if ba)
_REASON_BLOCKS_EXPORT = frozenset(code for code, (_, _, be, _, _) in REVIEW_REASON_CODES.items() if be)


def reason_blocks_auto_adoption(reason_code: str) -> bool:
    """该原因码是否阻断自动采用（矩阵查询；未知原因码视为不阻断但由模型拒绝写入）。"""
    policy = REVIEW_REASON_CODES.get(reason_code)
    return bool(policy and policy[1])


def reason_blocks_export(reason_code: str) -> bool:
    policy = REVIEW_REASON_CODES.get(reason_code)
    return bool(policy and policy[2])


# ---------------------------------------------------------------- 11F-1b：decision_type × reason_code 组合矩阵
# 11F-1a 契约 §2.4 冻结：21 码归入 5 组；8 个 decision_type 只允许引用其许可组。

DECISION_REASON_GROUPS: dict[str, frozenset] = {
    "G1": frozenset({
        "EVIDENCE_INSUFFICIENT", "EVIDENCE_REJECTED", "MATERIAL_MISSING",
        "MATERIAL_CORRUPTED", "LINK_INVALID", "LARGE_VIDEO_MANUAL",
    }),
    "G2": frozenset({
        "PROVIDER_CAPABILITY_MISMATCH", "PROVIDER_UNAVAILABLE", "ALL_ATTEMPTS_FAILED",
        "INVALID_MODEL_RESPONSE", "SCORE_OUT_OF_RANGE", "SCORE_COMPONENT_MISMATCH",
        "RATIONALE_MISSING", "UNSUPPORTED_INFERENCE",
    }),
    "G3": frozenset({"LOW_CONFIDENCE", "HIGH_SCORE_VARIANCE", "HARD_FLAG_TRIGGERED", "PRIVACY_RISK"}),
    "G4": frozenset({"RANDOM_AUDIT", "APPEAL_REQUESTED"}),
    "G5": frozenset({"EXPORT_RESULT_MISSING"}),
}

DECISION_TYPE_ALLOWED_GROUPS: dict[str, frozenset] = {
    "adopt_existing_attempt": frozenset({"G1", "G3"}),
    "reject_attempt": frozenset({"G1", "G3"}),
    "request_additional_evidence": frozenset({"G1"}),
    "retry_same_provider": frozenset({"G2"}),
    "switch_provider": frozenset({"G2"}),
    "apply_manual_adjustment": frozenset({"G3"}),
    "dismiss_no_issue": frozenset({"G4", "G5"}),
    "cancel_case": frozenset({"G1", "G2", "G3", "G4", "G5"}),
}

# decision_type 强制目标字段（11F-1a 契约 §2.4 表；缺失 -> ERR_DECISION_TARGET_REQUIRED）
DECISION_REQUIRED_TARGETS: dict[str, tuple[str, ...]] = {
    "adopt_existing_attempt": ("target_attempt_id", "target_snapshot_id"),
    "reject_attempt": ("target_attempt_id", "target_snapshot_id"),
    "request_additional_evidence": ("requested_package_revision",),
    "apply_manual_adjustment": ("manual_adjustment_id",),
}


def decision_allows_reason(decision_type: str, reason_code: str) -> bool:
    """组合矩阵查询：该 decision_type 是否允许引用该 reason code。"""
    groups = DECISION_TYPE_ALLOWED_GROUPS.get(decision_type)
    if groups is None:
        return False
    for group in groups:
        if reason_code in DECISION_REASON_GROUPS[group]:
            return True
    return False


def validate_decision_reason_combo(decision_type: str, reason_codes) -> Optional[str]:
    """校验 decision_type 与 reason_codes 组合；合法返回 None，非法返回原因码。

    非法原因（来自 11F-1a 契约 §2.3 第 6/7 条）：
    - 引用了未许可组的原因码 -> 返回 reason code（调用方映射为 ERR_DECISION_REASON_MISMATCH）。
    """
    for code in reason_codes:
        if not decision_allows_reason(decision_type, code):
            return code
    return None


# ---------------------------------------------------------------- 枚举与状态

ReviewCaseStatus = Literal[
    "open", "assigned", "in_review", "waiting_for_evidence", "resolved", "dismissed", "cancelled",
]
ReviewPriority = Literal["low", "normal", "high", "urgent"]
ReviewCaseSourceType = Literal[
    "evidence_validation", "pipeline_stage", "provider_failure", "attempt_validation",
    "flag_engine", "attempt_comparison", "random_audit", "appeal", "export_reconciliation", "manual",
]
ReviewDecisionType = Literal[
    "adopt_existing_attempt", "reject_attempt", "request_additional_evidence",
    "retry_same_provider", "switch_provider", "apply_manual_adjustment",
    "dismiss_no_issue", "cancel_case",
]
AdoptionStatus = Literal["proposed", "blocked", "adopted", "superseded", "revoked"]
AdoptionPurpose = Literal["machine_result", "review_adjusted_result"]
ReviewEventType = Literal[
    "CASE_OPENED", "REASON_ADDED", "PRIORITY_CHANGED", "CASE_ASSIGNED", "REVIEW_STARTED",
    "EVIDENCE_REQUESTED", "EVIDENCE_RECEIVED", "ATTEMPT_LINKED", "DECISION_RECORDED",
    "ADOPTION_LINKED", "MANUAL_ADJUSTMENT_LINKED", "CASE_RESOLVED", "CASE_DISMISSED",
    "CASE_CANCELLED", "CASE_REOPENED", "FINAL_RESULT_LOCKED", "FINAL_RESULT_UNLOCKED",
    "OPERATION_CREATED", "ROLLBACK_SUPERSEDE",  # 11F-3b
]

# ReviewCase 合法状态转换（契约 10.7；resolved/dismissed -> open/assigned 仅按重开规则）
VALID_CASE_TRANSITIONS: frozenset = frozenset({
    ("open", "assigned"), ("open", "in_review"), ("open", "dismissed"), ("open", "cancelled"),
    ("assigned", "in_review"), ("assigned", "open"), ("assigned", "dismissed"), ("assigned", "cancelled"),
    ("in_review", "waiting_for_evidence"), ("in_review", "resolved"),
    ("in_review", "dismissed"), ("in_review", "cancelled"),
    ("waiting_for_evidence", "assigned"), ("waiting_for_evidence", "in_review"),
    ("waiting_for_evidence", "resolved"), ("waiting_for_evidence", "cancelled"),
    # 重开（仅按契约 10.9 重新打开规则，reopen_count+1 / 保留旧 resolution）
    ("resolved", "open"), ("resolved", "assigned"), ("dismissed", "open"),
})

# ResultAdoption 合法状态转换（11E-3b-1 冻结：proposed/blocked/adopted/superseded/revoked）
VALID_ADOPTION_TRANSITIONS: frozenset = frozenset({
    ("proposed", "adopted"), ("proposed", "blocked"), ("proposed", "revoked"),
    ("blocked", "adopted"), ("blocked", "revoked"),
    ("adopted", "superseded"), ("adopted", "revoked"),
})
ADOPTION_TERMINAL: frozenset = frozenset({"superseded", "revoked"})

# ReviewEvent 中"推进 case 投影 revision"的状态转换事件（其余事件 revision 不变）
_CASE_REVISION_EVENTS: frozenset = frozenset({
    "CASE_OPENED", "CASE_ASSIGNED", "REVIEW_STARTED", "EVIDENCE_REQUESTED", "EVIDENCE_RECEIVED",
    "CASE_RESOLVED", "CASE_DISMISSED", "CASE_CANCELLED", "CASE_REOPENED",
})


def can_transition_case(from_status: str, to_status: str) -> bool:
    return (from_status, to_status) in VALID_CASE_TRANSITIONS


def can_transition_adoption(from_status: str, to_status: str) -> bool:
    return (from_status, to_status) in VALID_ADOPTION_TRANSITIONS


# ---------------------------------------------------------------- 受控子对象


class AdoptionScope(BaseModel):
    """ResultAdoption 采用范围（契约 9.2；11F-1b 增批次/支线隔离，关闭 N10 残余）。

    - batch_id：报名批次/任务批次稳定 ID（11F-1a 契约 §4.2 必填；通用业务含义）。
    - stream_id：独立子批次/支线稳定 ID；非支线批次使用 "default"。
    - 旧 sidecar 兼容：缺失字段读取为 batch_id="legacy" / stream_id="default"（仅读取语义，
      不改写旧文件）；新写入由 store 强制提供 batch_id != "legacy"（见 review_case_store）。
    """

    model_config = ConfigDict(extra="forbid")

    competition_id: str
    batch_id: str = "legacy"  # 11F-1b：默认值仅用于旧数据兼容读取
    stream_id: str = "default"  # 11F-1b：默认值用于非支线批次与旧数据兼容
    submission_id: str
    scoring_policy_version: str
    purpose: AdoptionPurpose = "machine_result"

    _competition_id = field_validator("competition_id", mode="after")(_validate_stable_id)
    _batch_id = field_validator("batch_id", mode="after")(_validate_stable_id)
    _stream_id = field_validator("stream_id", mode="after")(_validate_stable_id)
    _submission_id = field_validator("submission_id", mode="after")(_validate_stable_id)
    _scoring_policy_version = field_validator("scoring_policy_version", mode="after")(_validate_stable_id)


class ManualAdjustmentChange(BaseModel):
    """单条人工调分变更（契约 12.4）：保留前后值与范围引用，不得只存总分。"""

    model_config = ConfigDict(extra="forbid")

    field_path: str
    before_value: float
    after_value: float
    allowed_range_ref: str
    evidence_refs: List[str] = Field(default_factory=list)
    change_reason_code: str

    @model_validator(mode="after")
    def _value_changed(self) -> "ManualAdjustmentChange":
        if self.before_value == self.after_value:
            raise ValueError("before_value must differ from after_value")
        return self


# ---------------------------------------------------------------- ReviewCase


class ReviewCase(BaseModel):
    """人工复核工单（当前状态投影；状态变化必须追加 ReviewEvent，历史事件不可覆盖）。

    11E-3b-1 显式扩展（用户冻结要求）：
    - reopened_from_case_id：重开审计关系（从旧工单重开时引用旧工单）。
    - previous_resolution_decision_id：契约 10.9 重新打开必须保存旧 resolution。
    - idempotency_key：创建幂等键（同 task/item + key 命中已有工单时幂等）。
    """

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["score-attempt-review/v1"] = _REVIEW_CONTRACT_VERSION
    schema_version: Literal["review-case/v1"] = _REVIEW_CASE_SCHEMA
    review_case_id: str
    case_number: str
    submission_id: str
    task_id: Optional[str] = None
    item_id: Optional[str] = None
    package_id: Optional[str] = None
    package_revision: Optional[int] = Field(default=None, ge=1)
    attempt_ids: List[str] = Field(default_factory=list)
    snapshot_ids: List[str] = Field(default_factory=list)
    validation_ids: List[str] = Field(default_factory=list)
    source_type: ReviewCaseSourceType
    reason_codes: List[str] = Field(default_factory=list)
    priority: ReviewPriority = "normal"
    status: ReviewCaseStatus = "open"
    assigned_to: Optional[ActorRef] = None
    assignment_group: Optional[str] = None
    blocks_auto_adoption: bool = False
    blocks_export: bool = False
    opened_at: AwareDatetime
    assigned_at: Optional[AwareDatetime] = None
    review_started_at: Optional[AwareDatetime] = None
    resolved_at: Optional[AwareDatetime] = None
    resolution_decision_id: Optional[str] = None
    previous_resolution_decision_id: Optional[str] = None  # 11E-3b-1：重开时保存旧 resolution
    reopened_from_case_id: Optional[str] = None  # 11E-3b-1：重开审计关系
    reopen_count: int = Field(default=0, ge=0)
    current_revision: int = Field(default=1, ge=1)
    updated_at: AwareDatetime
    idempotency_key: Optional[str] = None  # 11E-3b-1：创建幂等键

    _review_case_id = field_validator("review_case_id", mode="after")(_validate_stable_id)
    _case_number = field_validator("case_number", mode="after")(_validate_stable_id)
    _submission_id = field_validator("submission_id", mode="after")(_validate_stable_id)
    _task_id = field_validator("task_id", mode="after")(_validate_stable_id_optional)
    _item_id = field_validator("item_id", mode="after")(_validate_stable_id_optional)
    _package_id = field_validator("package_id", mode="after")(_validate_stable_id_optional)
    _resolution_decision_id = field_validator("resolution_decision_id", mode="after")(_validate_stable_id_optional)
    _previous_resolution_decision_id = field_validator(
        "previous_resolution_decision_id", mode="after")(_validate_stable_id_optional)
    _reopened_from_case_id = field_validator("reopened_from_case_id", mode="after")(_validate_stable_id_optional)
    _idempotency_key = field_validator("idempotency_key", mode="after")(_validate_stable_id_optional)

    @field_validator("reason_codes")
    @classmethod
    def _reason_codes_frozen(cls, value: List[str]) -> List[str]:
        if not value:
            raise ValueError("reason_codes must not be empty")
        unknown = [c for c in value if c not in REVIEW_REASON_CODES]
        if unknown:
            raise ValueError(f"unknown reason code: {unknown}")
        if len(set(value)) != len(value):
            raise ValueError("reason_codes must not contain duplicates")
        return value

    @model_validator(mode="after")
    def _case_invariants(self) -> "ReviewCase":
        """跨字段不变量：

        - 引用一致性：task_id/item_id 同时存在或同时为空；attempt/snapshot 引用去重。
        - blocks 与原因码矩阵一致：任一阻断原因码 => blocks_auto_adoption/blocks_export 必须为 True。
        - 状态时间不变量：assigned_at 仅 assigned/in_review/waiting_for_evidence/resolved/dismissed；
          resolved_at 与 resolution_decision_id 仅 resolved/dismissed；时间顺序合法。
        - 重开关系：reopened_from_case_id 与自身不同；previous_resolution_decision_id 仅在重开时保留。
        - opened_at <= updated_at。
        """
        if (self.task_id is None) != (self.item_id is None):
            raise ValueError("task_id and item_id must be provided together")
        for lst, name in ((self.attempt_ids, "attempt_ids"), (self.snapshot_ids, "snapshot_ids"),
                          (self.validation_ids, "validation_ids")):
            if len(set(lst)) != len(lst):
                raise ValueError(f"{name} must not contain duplicates")
        # blocks 与原因码矩阵一致（契约 14.3：任一阻断即整体阻断）
        if not self.blocks_auto_adoption and any(
            code in _REASON_BLOCKS_AUTO for code in self.reason_codes
        ):
            raise ValueError("blocks_auto_adoption must be true when any reason code blocks auto adoption")
        if not self.blocks_export and any(
            code in _REASON_BLOCKS_EXPORT for code in self.reason_codes
        ):
            raise ValueError("blocks_export must be true when any reason code blocks export")
        # 状态时间不变量
        if self.status in ("open", "cancelled") and self.assigned_at is not None:
            raise ValueError("assigned_at only allowed for assigned/in_review/waiting_for_evidence/resolved/dismissed")
        if self.status not in ("resolved", "dismissed"):
            if self.resolved_at is not None or self.resolution_decision_id is not None:
                raise ValueError("resolved_at/resolution_decision_id only allowed when resolved or dismissed")
        else:
            if self.resolved_at is None or self.resolution_decision_id is None:
                raise ValueError("resolved/dismissed case requires resolved_at and resolution_decision_id")
        # 时间顺序（存在时）
        times = [self.opened_at]
        for t in (self.assigned_at, self.review_started_at, self.resolved_at):
            if t is not None:
                times.append(t)
        for a, b in zip(times, times[1:]):
            if a > b:
                raise ValueError("case time order violated")
        if self.opened_at > self.updated_at:
            raise ValueError("opened_at must not be after updated_at")
        if self.reopened_from_case_id == self.review_case_id:
            raise ValueError("reopened_from_case_id must not equal review_case_id")
        if self.previous_resolution_decision_id is not None and (
            self.previous_resolution_decision_id == self.resolution_decision_id
        ):
            raise ValueError("previous_resolution_decision_id must differ from resolution_decision_id")
        return self


# ---------------------------------------------------------------- ReviewDecision


class ReviewDecision(BaseModel):
    """一次结构化人工复核决定（不可变事实；旧决定不删除，supersede 表达替换）。"""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["score-attempt-review/v1"] = _REVIEW_CONTRACT_VERSION
    schema_version: Literal["review-decision/v1"] = _REVIEW_DECISION_SCHEMA
    decision_id: str
    review_case_id: str
    case_revision: int = Field(ge=1)
    decision_type: ReviewDecisionType
    reason_codes: List[str] = Field(default_factory=list)
    target_attempt_id: Optional[str] = None
    target_snapshot_id: Optional[str] = None
    requested_package_revision: Optional[int] = Field(default=None, ge=1)
    manual_adjustment_id: Optional[str] = None
    result_adoption_id: Optional[str] = None
    supersedes_decision_id: Optional[str] = None  # 11E-3b-1：替换的旧决定
    decision_note_code: str
    evidence_refs: List[str] = Field(default_factory=list)
    idempotency_key: str  # 11F-1b：创建幂等键（同 case 下唯一）
    decided_by: ActorRef
    decided_at: AwareDatetime
    decision_hash: str

    _decision_id = field_validator("decision_id", mode="after")(_validate_stable_id)
    _review_case_id = field_validator("review_case_id", mode="after")(_validate_stable_id)
    _target_attempt_id = field_validator("target_attempt_id", mode="after")(_validate_stable_id_optional)
    _target_snapshot_id = field_validator("target_snapshot_id", mode="after")(_validate_stable_id_optional)
    _manual_adjustment_id = field_validator("manual_adjustment_id", mode="after")(_validate_stable_id_optional)
    _result_adoption_id = field_validator("result_adoption_id", mode="after")(_validate_stable_id_optional)
    _supersedes_decision_id = field_validator("supersedes_decision_id", mode="after")(_validate_stable_id_optional)
    _idempotency_key = field_validator("idempotency_key", mode="after")(_validate_stable_id)
    _decision_hash = field_validator("decision_hash", mode="after")(_validate_sha256)

    @field_validator("reason_codes")
    @classmethod
    def _reason_codes_frozen(cls, value: List[str]) -> List[str]:
        unknown = [c for c in value if c not in REVIEW_REASON_CODES]
        if unknown:
            raise ValueError(f"unknown reason code: {unknown}")
        return value

    @model_validator(mode="after")
    def _decision_invariants(self) -> "ReviewDecision":
        """跨字段校验（契约 11.5 + 11F-1b 组合矩阵）：

        - supersedes_decision_id != decision_id。
        - request_additional_evidence 必须指定 requested_package_revision（新 revision，禁止覆盖旧包）。
        - apply_manual_adjustment 必须引用 manual_adjustment_id（决定本身不携带分数）。
        - adopt_existing_attempt / reject_attempt 必须指定 target_attempt_id（拒绝不得用于关闭隐私风险）。
        - dismiss_no_issue 不得用于 PRIVACY_RISK / APPEAL_REQUESTED / HARD_FLAG_TRIGGERED。
        - adopt_existing_attempt 可引用 result_adoption_id；非该类型不得带 manual_adjustment_id。
        - 11F-1b：decision_type 与 reason_codes 必须满足 11F-1a §2.4 组合矩阵
          （否则 ValueError，store 映射为 REVIEW_DECISION_REASON_MISMATCH）。
        """
        if self.supersedes_decision_id == self.decision_id:
            raise ValueError("supersedes_decision_id must not equal decision_id")
        if self.decision_type == "request_additional_evidence" and self.requested_package_revision is None:
            raise ValueError("request_additional_evidence requires requested_package_revision")
        if self.decision_type == "apply_manual_adjustment" and self.manual_adjustment_id is None:
            raise ValueError("apply_manual_adjustment requires manual_adjustment_id")
        if self.decision_type in ("adopt_existing_attempt", "reject_attempt") and self.target_attempt_id is None:
            raise ValueError(f"{self.decision_type} requires target_attempt_id")
        if self.decision_type == "dismiss_no_issue" and any(
            c in ("PRIVACY_RISK", "APPEAL_REQUESTED", "HARD_FLAG_TRIGGERED") for c in self.reason_codes
        ):
            raise ValueError("dismiss_no_issue must not close privacy risk / appeal / hard flag")
        if self.decision_type != "apply_manual_adjustment" and self.manual_adjustment_id is not None:
            raise ValueError("manual_adjustment_id only allowed for apply_manual_adjustment")
        bad = validate_decision_reason_combo(self.decision_type, self.reason_codes)
        if bad is not None:
            raise ValueError(f"reason code {bad} not allowed for decision_type {self.decision_type}")
        return self


# ---------------------------------------------------------------- ResultAdoption


class ResultAdoption(BaseModel):
    """对不可变结果快照的采用记录（proposed/blocked/adopted/superseded/revoked）。

    11E-3b-1 冻结状态说明（相对契约 9.4 的显式扩展）：
    - blocked：候选因 must_review / 人工锁定 / 开放阻断工单被拒（契约 withdrawn/rejected 的中间态）。
    - revoked：采用被授权撤销（保留 actor、reason、time；不删除记录）。
    - superseded：被新的明确采用记录替代（旧记录保留）。
    """

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["score-attempt-review/v1"] = _REVIEW_CONTRACT_VERSION
    schema_version: Literal["result-adoption/v1"] = _RESULT_ADOPTION_SCHEMA
    adoption_id: str
    adoption_scope: AdoptionScope
    submission_id: str
    attempt_id: Optional[str] = None
    snapshot_id: str
    validation_id: str
    review_case_id: Optional[str] = None
    decision_id: Optional[str] = None
    status: AdoptionStatus = "proposed"
    reason_code: str
    supersedes_adoption_id: Optional[str] = None
    effective_at: Optional[AwareDatetime] = None
    decided_at: AwareDatetime
    decided_by: ActorRef
    adoption_hash: str

    _adoption_id = field_validator("adoption_id", mode="after")(_validate_stable_id)
    _submission_id = field_validator("submission_id", mode="after")(_validate_stable_id)
    _attempt_id = field_validator("attempt_id", mode="after")(_validate_stable_id_optional)
    _snapshot_id = field_validator("snapshot_id", mode="after")(_validate_stable_id)
    _validation_id = field_validator("validation_id", mode="after")(_validate_stable_id)
    _review_case_id = field_validator("review_case_id", mode="after")(_validate_stable_id_optional)
    _decision_id = field_validator("decision_id", mode="after")(_validate_stable_id_optional)
    _supersedes_adoption_id = field_validator("supersedes_adoption_id", mode="after")(_validate_stable_id_optional)
    _adoption_hash = field_validator("adoption_hash", mode="after")(_validate_sha256)

    @model_validator(mode="after")
    def _adoption_invariants(self) -> "ResultAdoption":
        """跨字段校验：

        - scope.submission_id 与 adoption.submission_id 一致。
        - supersedes_adoption_id != adoption_id。
        - status == adopted 时 effective_at 必须存在；非 adopted 时 effective_at 可以为 None。
        - adopted 时 attempt_id 必须存在（采用对象是不可变 Snapshot，绑定来源 attempt）。
        - 人工锁定保护：decided_by.actor_type == "reviewer" 且 reason_code 为人工采用码时，
          记录应视为人工锁定（本字段语义由 store 使用，见 ADOPTION_* 注释）。
        """
        if self.adoption_scope.submission_id != self.submission_id:
            raise ValueError("adoption_scope.submission_id must match submission_id")
        if self.supersedes_adoption_id == self.adoption_id:
            raise ValueError("supersedes_adoption_id must not equal adoption_id")
        if self.status == "adopted":
            if self.effective_at is None:
                raise ValueError("adopted adoption requires effective_at")
            if self.attempt_id is None:
                raise ValueError("adopted adoption requires attempt_id")
        if self.decided_by.actor_type not in ("system", "controller", "orchestrator", "reviewer"):
            raise ValueError("unsupported actor type")
        return self


# ---------------------------------------------------------------- ManualAdjustment


class ManualAdjustment(BaseModel):
    """人工调分记录（不可变；不覆盖 Provider 快照，生成新 derived snapshot 引用）。"""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["score-attempt-review/v1"] = _REVIEW_CONTRACT_VERSION
    schema_version: Literal["manual-adjustment/v1"] = _MANUAL_ADJUSTMENT_SCHEMA
    adjustment_id: str
    review_case_id: str
    decision_id: str
    submission_id: str
    base_attempt_id: Optional[str] = None
    base_snapshot_id: str
    adjusted_snapshot_id: str
    scoring_policy_version: str
    rubric_version: str
    changes: List[ManualAdjustmentChange] = Field(default_factory=list)
    reason_codes: List[str] = Field(default_factory=list)
    evidence_refs: List[str] = Field(default_factory=list)
    adjustment_note_code: str
    adjusted_by: ActorRef
    adjusted_at: AwareDatetime
    adjustment_hash: str

    _adjustment_id = field_validator("adjustment_id", mode="after")(_validate_stable_id)
    _review_case_id = field_validator("review_case_id", mode="after")(_validate_stable_id)
    _decision_id = field_validator("decision_id", mode="after")(_validate_stable_id)
    _submission_id = field_validator("submission_id", mode="after")(_validate_stable_id)
    _base_attempt_id = field_validator("base_attempt_id", mode="after")(_validate_stable_id_optional)
    _base_snapshot_id = field_validator("base_snapshot_id", mode="after")(_validate_stable_id)
    _adjusted_snapshot_id = field_validator("adjusted_snapshot_id", mode="after")(_validate_stable_id)
    _scoring_policy_version = field_validator("scoring_policy_version", mode="after")(_validate_stable_id)
    _rubric_version = field_validator("rubric_version", mode="after")(_validate_stable_id)
    _adjustment_hash = field_validator("adjustment_hash", mode="after")(_validate_sha256)

    @model_validator(mode="after")
    def _adjustment_invariants(self) -> "ManualAdjustment":
        """跨字段校验（契约 12）：

        - 调分不得覆盖原快照：adjusted_snapshot_id != base_snapshot_id。
        - 不允许只存总分丢失依据：changes 必须非空且包含受影响维度/字段。
        - 必须引用授权决定（decision_id 存在），本阶段不执行最终分计算。
        - 理由原因码必须来自冻结集合。
        """
        if self.adjusted_snapshot_id == self.base_snapshot_id:
            raise ValueError("adjusted_snapshot_id must differ from base_snapshot_id")
        if not self.changes:
            raise ValueError("changes must not be empty (no score-only adjustment without basis)")
        if self.base_attempt_id is not None and self.base_attempt_id == self.adjustment_id:
            raise ValueError("base_attempt_id must not equal adjustment_id")
        unknown = [c for c in self.reason_codes if c not in REVIEW_REASON_CODES]
        if unknown:
            raise ValueError(f"unknown reason code: {unknown}")
        return self


# ---------------------------------------------------------------- ManualAdjustmentOperation（11F-3b）


class ManualAdjustmentOperation(BaseModel):
    """人工调整事务日志（prepared / committing / committed / failed）。

    committed 和 failed 为终态，不允许倒退或相互转换。
    """

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["score-attempt-review/v1"] = _REVIEW_CONTRACT_VERSION
    schema_version: Literal["manual-adjustment-operation/v1"] = "manual-adjustment-operation/v1"
    operation_id: str
    request_id: str
    stage: Literal["prepared", "committing", "committed", "failed"]
    payload_hash: str
    created_at: AwareDatetime
    updated_at: AwareDatetime

    # prepared 阶段产出
    base_snapshot_id: Optional[str] = None
    adjusted_snapshot_id: Optional[str] = None
    adjusted_validation_id: Optional[str] = None
    changes: List[dict] = Field(default_factory=list)
    reason_codes: List[str] = Field(default_factory=list)

    # committing 阶段产出
    adjustment_id: Optional[str] = None
    new_adoption_id: Optional[str] = None
    old_adoption_id: Optional[str] = None
    old_adoption_status_before: Optional[str] = None

    # failed 信息
    error_code: Optional[str] = None
    failed_at_stage: Optional[str] = None

    _operation_id = field_validator("operation_id", mode="after")(_validate_stable_id)
    _base_snapshot_id = field_validator("base_snapshot_id", mode="after")(_validate_stable_id_optional)
    _adjusted_snapshot_id = field_validator("adjusted_snapshot_id", mode="after")(_validate_stable_id_optional)
    _adjusted_validation_id = field_validator("adjusted_validation_id", mode="after")(_validate_stable_id_optional)
    _adjustment_id = field_validator("adjustment_id", mode="after")(_validate_stable_id_optional)
    _new_adoption_id = field_validator("new_adoption_id", mode="after")(_validate_stable_id_optional)
    _old_adoption_id = field_validator("old_adoption_id", mode="after")(_validate_stable_id_optional)
    _payload_hash = field_validator("payload_hash", mode="after")(_validate_sha256)

    @model_validator(mode="after")
    def _op_invariants(self) -> "ManualAdjustmentOperation":
        if self.stage == "committed":
            if self.adjustment_id is None or self.new_adoption_id is None:
                raise ValueError("committed operation requires adjustment_id and new_adoption_id")
        if self.stage == "failed":
            if self.error_code is None:
                raise ValueError("failed operation requires error_code")
        return self


# ---------------------------------------------------------------- ReviewEvent


class ReviewEvent(BaseModel):
    """ReviewCase 生命周期追加式审计事件（不可变；previous_event_hash 表达同 case 审计链）。"""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["score-attempt-review/v1"] = _REVIEW_CONTRACT_VERSION
    schema_version: Literal["review-event/v1"] = _REVIEW_EVENT_SCHEMA
    event_id: str
    review_case_id: str
    case_revision_before: int = Field(ge=1)
    case_revision_after: int = Field(ge=1)
    event_type: ReviewEventType
    reason_codes: List[str] = Field(default_factory=list)
    object_refs: List[str] = Field(default_factory=list)
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    actor: ActorRef
    occurred_at: AwareDatetime
    event_hash: str
    previous_event_hash: Optional[str] = None

    _event_id = field_validator("event_id", mode="after")(_validate_stable_id)
    _review_case_id = field_validator("review_case_id", mode="after")(_validate_stable_id)
    _event_hash = field_validator("event_hash", mode="after")(_validate_sha256)
    _previous_event_hash = field_validator("previous_event_hash", mode="after")(_validate_sha256_optional)

    @model_validator(mode="after")
    def _event_invariants(self) -> "ReviewEvent":
        """- revision 连续性：after >= before 且差值 <= 1（状态转换事件 +1，其余不变）。
        - 状态转换事件必须携带 from/to_status；非状态事件不得携带（除 ADOPTION_LINKED 表达
          采用状态转换场景外，见 store 用法注释）。
        """
        if not (0 <= self.case_revision_after - self.case_revision_before <= 1):
            raise ValueError("case revision must be continuous (after in {before, before+1})")
        if self.event_type == "CASE_OPENED":
            # 创建事件：无前序状态，from_status 可为 None
            if self.to_status is None:
                raise ValueError("CASE_OPENED requires to_status")
        elif self.event_type in _CASE_REVISION_EVENTS:
            if self.from_status is None or self.to_status is None:
                raise ValueError(f"{self.event_type} requires from_status/to_status")
        else:
            # 非 case 状态事件：允许 ADOPTION_LINKED / ROLLBACK_SUPERSEDE 携带 from/to_status 表达采用状态转换审计
            if self.event_type not in ("ADOPTION_LINKED", "ROLLBACK_SUPERSEDE") and (self.from_status is not None or self.to_status is not None):
                raise ValueError(f"{self.event_type} must not carry from_status/to_status")
        return self


# ---------------------------------------------------------------- ManualFinalLock（11F-1b，关闭 N11）


class ManualFinalLock(BaseModel):
    """人工最终结果锁定（独立 sidecar 对象；11F-1a 契约 §3 冻结）。

    - 可推进投影：create 建 active；release 原子替换 status=released 且 revision+1。
    - 锁定字段（task/item/case/decision/adoption/snapshot/locked_by/locked_at/reason_code）
      全部不可变；release 只允许改变 status/revision（CAS + 绑定字段校验）。
    - 解锁不删除、不覆盖旧锁；追加 FINAL_RESULT_UNLOCKED 事件。
    - 同一 item 同时最多一个 active 锁；重复锁定 REVIEW_LOCK_CONFLICT。
    """

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["score-attempt-review/v1"] = _REVIEW_CONTRACT_VERSION
    schema_version: Literal["manual-final-lock/v1"] = _MANUAL_FINAL_LOCK_SCHEMA
    lock_id: str
    task_id: str
    item_id: str
    review_case_id: str
    decision_id: str
    adoption_id: Optional[str] = None
    snapshot_id: Optional[str] = None
    locked_by: ActorRef
    locked_at: AwareDatetime
    reason_code: str
    status: Literal["active", "released"] = "active"
    revision: int = Field(default=1, ge=1)
    idempotency_key: str
    content_hash: str

    _lock_id = field_validator("lock_id", mode="after")(_validate_stable_id)
    _task_id = field_validator("task_id", mode="after")(_validate_stable_id)
    _item_id = field_validator("item_id", mode="after")(_validate_stable_id)
    _review_case_id = field_validator("review_case_id", mode="after")(_validate_stable_id)
    _decision_id = field_validator("decision_id", mode="after")(_validate_stable_id)
    _adoption_id = field_validator("adoption_id", mode="after")(_validate_stable_id_optional)
    _snapshot_id = field_validator("snapshot_id", mode="after")(_validate_stable_id_optional)
    _idempotency_key = field_validator("idempotency_key", mode="after")(_validate_stable_id)
    _content_hash = field_validator("content_hash", mode="after")(_validate_sha256)

    @model_validator(mode="after")
    def _lock_invariants(self) -> "ManualFinalLock":
        """- adoption_id 与 snapshot_id 至少提供其一（锁定目标明确）。
        - 时间顺序：locked_at 即创建时间，无额外时间字段需排序。
        - reason_code 必须来自冻结集合。
        - 非 active 状态必须是 released（枚举已约束）。
        """
        if self.adoption_id is None and self.snapshot_id is None:
            raise ValueError("ManualFinalLock requires adoption_id or snapshot_id")
        if self.reason_code not in REVIEW_REASON_CODES and self.reason_code not in (
            "HUMAN_FINAL_LOCKED", "HUMAN_FINAL_UNLOCKED",
        ):
            raise ValueError("unknown lock reason code")
        return self
