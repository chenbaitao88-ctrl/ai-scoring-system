"""
ScoreAttempt / ScoreResultSnapshot / AttemptValidation 契约模型（Phase 11E-1b）。

严格依据《Phase11A-2d-ScoreAttempt与ReviewCase契约.md》v1 实现；本文件是对该契约的 Phase 11E 显式扩展，
不静默改写历史文档：

- 九状态契约（created/running/succeeded/failed/timed_out/rate_limited/quota_exhausted/
  invalid_response/cancelled）+ Phase 11E 新增 `outcome_unknown`（第 10 状态）。
- `outcome_unknown`：Provider 是否成功无法确认；终态，不得改回 running/succeeded/failed；
  合法转换仅允许 `running -> outcome_unknown`；必须 completed_at；必须绑定稳定错误码
  `PROVIDER_OUTCOME_UNKNOWN`；不得有 result_snapshot_ref；必须进入人工复核/Provider 查询或
  受控恢复决策；后续重调必须创建新 attempt 并通过 `previous_attempt_id` 关联。
- 分值范围不硬编码为 100 或 60+40：`ScoreScale` 只保存维度与总分范围引用。

所有对象 `extra="forbid"`；不保存正文、Key、URL、学生个人信息。
"""
from __future__ import annotations

import re
from typing import List, Literal, Optional

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from models.scoring_provider import (
    ProviderError,
    _validate_sha256,
    _validate_sha256_optional,
    _validate_stable_id,
    _validate_stable_id_optional,
)

_SCORE_ATTEMPT_CONTRACT = "score-attempt-review/v1"
_SCORE_ATTEMPT_SCHEMA = "score-attempt/v1"
_SNAPSHOT_SCHEMA = "score-result-snapshot/v1"
_VALIDATION_SCHEMA = "attempt-validation/v1"

# 10 状态：11A-2d 九状态 + Phase 11E 显式扩展 outcome_unknown
ScoreAttemptStatus = Literal[
    "created", "running", "succeeded", "failed", "timed_out", "rate_limited",
    "quota_exhausted", "invalid_response", "cancelled", "outcome_unknown",
]

TERMINAL_STATUSES = frozenset({
    "succeeded", "failed", "timed_out", "rate_limited", "quota_exhausted",
    "invalid_response", "cancelled", "outcome_unknown",
})

# 合法状态转换（11A-2d 6.7 + outcome_unknown 扩展：仅 running -> outcome_unknown）
VALID_TRANSITIONS = frozenset({
    ("created", "running"),
    ("created", "cancelled"),
    ("running", "succeeded"),
    ("running", "failed"),
    ("running", "timed_out"),
    ("running", "rate_limited"),
    ("running", "quota_exhausted"),
    ("running", "invalid_response"),
    ("running", "cancelled"),
    ("running", "outcome_unknown"),
})


def can_transition(from_status: str, to_status: str) -> bool:
    """是否允许 from_status -> to_status（outcome_unknown 为终态，不得回退）。"""
    return (from_status, to_status) in VALID_TRANSITIONS


# ---------------- ActorRef（11E-1b-fix-1） ---------------- #

ActorType = Literal["system", "controller", "orchestrator", "reviewer"]


class ActorRef(BaseModel):
    """受控操作人引用（安全身份；不允许姓名、手机号、URL 或自由文本身份）。"""

    model_config = ConfigDict(extra="forbid")

    actor_type: ActorType
    actor_id: str

    _actor_id = field_validator("actor_id", mode="after")(_validate_stable_id)


# ---------------- 受控子对象 ---------------- #


class ScoreDimensionRef(BaseModel):
    """维度与分值范围引用（不硬编码分值；只保存引用与数值范围）。"""

    model_config = ConfigDict(extra="forbid")

    dimension_code: str
    score_range_ref: str  # 分值范围配置引用（如 range:objective-40）
    min_score: float = Field(ge=0)
    max_score: float = Field(ge=0)

    @model_validator(mode="after")
    def _range_consistent(self) -> "ScoreDimensionRef":
        if self.min_score > self.max_score:
            raise ValueError("min_score must not exceed max_score")
        return self


class ScoreScale(BaseModel):
    """本快照使用的维度与总分范围引用（非硬编码 100 / 60+40）。"""

    model_config = ConfigDict(extra="forbid")

    scoring_policy_version: str
    total_score_range_ref: str  # 总分范围配置引用
    total_min: float = Field(ge=0)
    total_max: float = Field(ge=0)
    dimensions: List[ScoreDimensionRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _total_consistent(self) -> "ScoreScale":
        if self.total_min > self.total_max:
            raise ValueError("total_min must not exceed total_max")
        return self


class DimensionScore(BaseModel):
    """维度得分（分数、范围与证据引用）。"""

    model_config = ConfigDict(extra="forbid")

    dimension_code: str
    score: float
    min_score: float = Field(ge=0)
    max_score: float = Field(ge=0)
    score_range_ref: str
    evidence_refs: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _score_in_range(self) -> "DimensionScore":
        if not (self.min_score <= self.score <= self.max_score):
            raise ValueError("score out of dimension range")
        return self


class Flag(BaseModel):
    """标准 Flag（脱敏）。"""

    model_config = ConfigDict(extra="forbid")

    flag_code: str
    severity: Literal["info", "warning", "critical"]
    message_key: str  # 稳定 message key，不保存自由文本


# ---------------- ScoreResultSnapshot ---------------- #


class ScoreResultSnapshot(BaseModel):
    """成功响应解析后的结构化评分结果快照（不可变；不可覆盖）。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["score-result-snapshot/v1"] = _SNAPSHOT_SCHEMA
    snapshot_id: str
    attempt_id: str
    submission_id: str
    package_id: str
    evidence_manifest_sha256: str
    scoring_policy_version: str
    rubric_version: str
    response_schema_version: str
    score_scale: ScoreScale
    objective_score: Optional[float] = None
    subjective_score: Optional[float] = None
    dimension_scores: List[DimensionScore] = Field(default_factory=list)
    total_score: float
    rationale_summary: str
    evidence_level: Literal["sufficient", "limited", "insufficient", "manual_only"]
    evidence_refs: List[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low", "unknown"] = "unknown"
    flags: List[Flag] = Field(default_factory=list)
    manual_review_recommended: bool = False
    structure_validation_status: Literal["pending", "passed", "failed"] = "pending"
    score_range_validation_status: Literal["pending", "passed", "failed"] = "pending"
    result_hash: str
    created_at: AwareDatetime

    _snapshot_id = field_validator("snapshot_id", mode="after")(_validate_stable_id)
    _attempt_id = field_validator("attempt_id", mode="after")(_validate_stable_id)
    _submission_id = field_validator("submission_id", mode="after")(_validate_stable_id)
    _package_id = field_validator("package_id", mode="after")(_validate_stable_id)
    _evidence_manifest_sha256 = field_validator("evidence_manifest_sha256", mode="after")(_validate_sha256)
    _result_hash = field_validator("result_hash", mode="after")(_validate_sha256)

    @model_validator(mode="after")
    def _total_within_scale(self) -> "ScoreResultSnapshot":
        if not (self.score_scale.total_min <= self.total_score <= self.score_scale.total_max):
            raise ValueError("total_score out of scale range")
        return self


# ---------------- AttemptValidation ---------------- #


class ValidationCheck(BaseModel):
    """单项校验结果。"""

    model_config = ConfigDict(extra="forbid")

    check_code: str
    passed: bool
    message_key: Optional[str] = None


class AttemptValidation(BaseModel):
    """对 attempt 和结果快照执行的校验结论。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["attempt-validation/v1"] = _VALIDATION_SCHEMA
    validation_id: str
    attempt_id: str
    snapshot_id: Optional[str] = None
    validation_revision: int = Field(ge=1)
    validator_version: str
    checks: List[ValidationCheck] = Field(default_factory=list)
    overall_status: Literal["passed", "failed", "manual_review_required"]
    adoption_candidate: bool = False
    manual_review_required: bool = False
    review_reason_codes: List[str] = Field(default_factory=list)
    validated_at: AwareDatetime
    validated_by: str  # system actor 引用（角色/非敏感 code）

    _validation_id = field_validator("validation_id", mode="after")(_validate_stable_id)
    _attempt_id = field_validator("attempt_id", mode="after")(_validate_stable_id)
    _snapshot_id = field_validator("snapshot_id", mode="after")(_validate_stable_id)


# ---------------- ScoreAttempt ---------------- #


class ScoreAttempt(BaseModel):
    """一次实际 Provider 调用（或发起前取消）的不可变事实。"""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["score-attempt-review/v1"] = _SCORE_ATTEMPT_CONTRACT
    schema_version: Literal["score-attempt/v1"] = _SCORE_ATTEMPT_SCHEMA
    attempt_id: str
    attempt_number: int = Field(ge=1)
    task_id: str
    item_id: str
    submission_id: str
    package_id: str
    package_revision: int = Field(ge=1)
    evidence_manifest_sha256: str
    input_fingerprint: str
    provider_id: str
    provider_config_version: str
    model_id: str
    model_capability_version: str
    provider_request_id: Optional[str] = None
    previous_attempt_id: Optional[str] = None
    switch_decision_id: Optional[str] = None
    scoring_policy_version: str
    rubric_version: str
    prompt_version: str
    response_schema_version: str
    status: ScoreAttemptStatus = "created"
    created_at: AwareDatetime  # 11E-1b-fix-1
    created_by: ActorRef  # 11E-1b-fix-1
    started_at: Optional[AwareDatetime] = None
    completed_at: Optional[AwareDatetime] = None
    duration_ms: Optional[int] = Field(default=None, ge=0)
    error: Optional[ProviderError] = None
    result_snapshot_ref: Optional[str] = None
    validation_ref: Optional[str] = None
    request_hash: Optional[str] = None
    response_hash: Optional[str] = None  # 11E-1b-fix-1

    _attempt_id = field_validator("attempt_id", mode="after")(_validate_stable_id)
    _task_id = field_validator("task_id", mode="after")(_validate_stable_id)
    _item_id = field_validator("item_id", mode="after")(_validate_stable_id)
    _submission_id = field_validator("submission_id", mode="after")(_validate_stable_id)
    _package_id = field_validator("package_id", mode="after")(_validate_stable_id)
    _provider_id = field_validator("provider_id", mode="after")(_validate_stable_id)
    _model_id = field_validator("model_id", mode="after")(_validate_stable_id)
    _previous_attempt_id = field_validator("previous_attempt_id", mode="after")(_validate_stable_id_optional)
    _switch_decision_id = field_validator("switch_decision_id", mode="after")(_validate_stable_id_optional)
    _result_snapshot_ref = field_validator("result_snapshot_ref", mode="after")(_validate_stable_id_optional)
    _validation_ref = field_validator("validation_ref", mode="after")(_validate_stable_id_optional)
    _evidence_manifest_sha256 = field_validator("evidence_manifest_sha256", mode="after")(_validate_sha256)
    _input_fingerprint = field_validator("input_fingerprint", mode="after")(_validate_sha256)
    _request_hash = field_validator("request_hash", mode="after")(_validate_sha256_optional)
    _response_hash = field_validator("response_hash", mode="after")(_validate_sha256_optional)

    @model_validator(mode="after")
    def _state_invariants(self) -> "ScoreAttempt":
        """状态不变量（11E-1b-fix-1）：

        - created：无 started/completed、无响应哈希/快照/错误。
        - running：有 started、无 completed、无快照、无终态错误。
        - succeeded：started/completed/duration_ms/response_hash/result_snapshot_ref 全部存在，error 为 None。
        - 失败类终态（failed/timed_out/rate_limited/quota_exhausted/invalid_response/cancelled/outcome_unknown）：
          error 必须存在、不得有 result_snapshot_ref；started_at 除 cancelled（允许 created 时取消）外必须存在；
          invalid_response 可以有 response_hash；outcome_unknown 必须 PROVIDER_OUTCOME_UNKNOWN。
        - 时间顺序：created_at <= started_at <= completed_at（存在时）。
        - previous_attempt_id != attempt_id。
        """
        if self.previous_attempt_id == self.attempt_id:
            raise ValueError("previous_attempt_id must not equal attempt_id")
        times = [self.created_at]
        if self.started_at is not None:
            times.append(self.started_at)
        if self.completed_at is not None:
            times.append(self.completed_at)
        for a, b in zip(times, times[1:]):
            if a > b:
                raise ValueError("time order violated: created_at <= started_at <= completed_at")

        if self.status == "created":
            if self.started_at is not None or self.completed_at is not None:
                raise ValueError("created attempt must not have started_at/completed_at")
            if self.response_hash is not None or self.result_snapshot_ref is not None or self.error is not None:
                raise ValueError("created attempt must not have response_hash/result_snapshot/error")
        elif self.status == "running":
            if self.started_at is None:
                raise ValueError("running attempt requires started_at")
            if self.completed_at is not None:
                raise ValueError("running attempt must not have completed_at")
            if self.result_snapshot_ref is not None or self.error is not None:
                raise ValueError("running attempt must not have result_snapshot/terminal error")
        elif self.status == "succeeded":
            if None in (self.started_at, self.completed_at, self.duration_ms,
                        self.response_hash, self.result_snapshot_ref):
                raise ValueError("succeeded attempt requires started_at/completed_at/duration_ms/response_hash/result_snapshot_ref")
            if self.error is not None:
                raise ValueError("succeeded attempt must not have error")
        else:  # 失败类终态
            if self.completed_at is None or self.error is None:
                raise ValueError("terminal failure requires completed_at and error")
            if self.status != "cancelled" and self.started_at is None:
                raise ValueError(f"{self.status} attempt requires started_at")
            if self.result_snapshot_ref is not None:
                raise ValueError(f"{self.status} attempt must not have result_snapshot_ref")
            if self.status == "outcome_unknown":
                if self.error.error_code != "PROVIDER_OUTCOME_UNKNOWN":
                    raise ValueError("outcome_unknown requires error with PROVIDER_OUTCOME_UNKNOWN")
        return self


# ---------------- 恢复决策模型（Phase 11E-1b 决策项 ③） ---------------- #

RecoveryPath = Literal[
    "provider_query",          # 向 Provider 查询结果
    "idempotent_replay",       # Provider 幂等能力已验证，同 attempt/request key 重放
    "manual_approved_new_attempt",  # 人工批准的新 attempt 重调（保留重复计费/结果审计）
    "manual_review",           # 进入人工复核
]


class RecoveryAssessment(BaseModel):
    """outcome_unknown 等结果不确定状态的受控恢复评估对象。

    只含受控字段；调用方不得直接传入"Provider 已成功"等权威结论（extra=forbid 且无该字段）。
    """

    model_config = ConfigDict(extra="forbid")

    attempt_id: str
    path: RecoveryPath
    reason_code: str  # 稳定恢复原因码
    previous_attempt_id: Optional[str] = None  # 重调时关联前序 attempt
    idempotency_verified: bool = False  # 仅 idempotent_replay 时可 true，且必须有验证来源引用
    idempotency_verification_ref: Optional[str] = None

    _attempt_id = field_validator("attempt_id", mode="after")(_validate_stable_id)
    _previous_attempt_id = field_validator("previous_attempt_id", mode="after")(_validate_stable_id_optional)

    @model_validator(mode="after")
    def _idempotency_consistency(self) -> "RecoveryAssessment":
        if self.path == "idempotent_replay":
            if not self.idempotency_verified:
                raise ValueError("idempotent_replay requires idempotency_verified")
            if not self.idempotency_verification_ref:
                raise ValueError("idempotent_replay requires verification ref")
        if self.path != "idempotent_replay" and self.idempotency_verified:
            raise ValueError("idempotency_verified only allowed for idempotent_replay")
        return self


# ---------------- Dry-run 输入/输出模型（Phase 11E-2a） ---------------- #

DryRunDecision = Literal["ready", "already_completed", "manual_review", "blocked"]


class DryRunCheck(BaseModel):
    """单项 dry-run 检查结论（受控）。"""

    model_config = ConfigDict(extra="forbid")

    check_code: str  # 稳定检查码
    passed: bool
    blocking: bool = False  # 该检查失败是否阻断调用计划


class DryRunPlan(BaseModel):
    """单 item dry-run 输出（只读安全计划；不含 Prompt/证据/Key/endpoint/分数）。"""

    model_config = ConfigDict(extra="forbid")

    dry_run_request_id: str
    task_id: str
    item_id: str
    decision: DryRunDecision
    checks: List[DryRunCheck] = Field(default_factory=list)
    provider_id: Optional[str] = None
    model_id: Optional[str] = None
    capability_version: Optional[str] = None
    evidence_package_id: Optional[str] = None
    evidence_manifest_sha256: Optional[str] = None
    input_fingerprint: Optional[str] = None
    scoring_policy_version: Optional[str] = None
    rubric_version: Optional[str] = None
    prompt_version: Optional[str] = None
    response_schema_version: Optional[str] = None
    scoring_mode: Optional[str] = None
    input_modalities: List[str] = Field(default_factory=list)
    manual_review_required: bool = False
    limited_evidence: bool = False
    provider_call_planned: bool = False
    blocking_error_code: Optional[str] = None
    created_at: AwareDatetime

    _dry_run_request_id = field_validator("dry_run_request_id", mode="after")(_validate_stable_id)
    _task_id = field_validator("task_id", mode="after")(_validate_stable_id)
    _item_id = field_validator("item_id", mode="after")(_validate_stable_id)
    _evidence_manifest_sha256 = field_validator("evidence_manifest_sha256", mode="after")(_validate_sha256_optional)
    _input_fingerprint = field_validator("input_fingerprint", mode="after")(_validate_sha256_optional)


# ---------------- 评分口径 Profile / 请求预检（11E-2a-fix-1） ---------------- #


class ScoringInputProfile(BaseModel):
    """权威评分输入口径（11E-2a-fix-1/fix-2）：由 Lookup 从权威来源读取，调用方不得传入内容。

    11E-2a-fix-2：绑定全部评分版本（scoring_policy/rubric/prompt/response_schema）；
    调用方只传 profile_version；modalities 非空、去重、仅 text/image。
    """

    model_config = ConfigDict(extra="forbid")

    profile_version: str
    scoring_mode: Literal["mixed", "ai_only"]
    input_modalities: List[Literal["text", "image"]]
    scoring_policy_version: str  # 11E-2a-fix-2
    rubric_version: str  # 11E-2a-fix-2
    prompt_version: str  # 11E-2a-fix-2
    response_schema_version: str  # 11E-2a-fix-2
    temperature: Optional[float] = None
    seed: Optional[int] = None
    max_output_tokens: Optional[int] = Field(default=None, gt=0)
    system_message_required: bool = True
    structured_json_required: bool = True
    max_images: Optional[int] = Field(default=None, ge=0)
    image_formats: Optional[List[str]] = None

    _profile_version = field_validator("profile_version", mode="after")(_validate_stable_id)
    _scoring_policy_version = field_validator("scoring_policy_version", mode="after")(_validate_stable_id)
    _rubric_version = field_validator("rubric_version", mode="after")(_validate_stable_id)
    _prompt_version = field_validator("prompt_version", mode="after")(_validate_stable_id)
    _response_schema_version = field_validator("response_schema_version", mode="after")(_validate_stable_id)

    @model_validator(mode="after")
    def _modalities_consistent(self) -> "ScoringInputProfile":
        if not self.input_modalities:
            raise ValueError("input_modalities must not be empty")
        if len(set(self.input_modalities)) != len(self.input_modalities):
            raise ValueError("input_modalities must not contain duplicates")
        if "image" not in self.input_modalities:
            if self.max_images is not None:
                raise ValueError("max_images only allowed when image modality declared")
            if self.image_formats is not None:
                raise ValueError("image_formats only allowed when image modality declared")
        else:
            if self.image_formats is not None:
                if not self.image_formats:
                    raise ValueError("image_formats must not be empty when declared")
                if len(set(self.image_formats)) != len(self.image_formats):
                    raise ValueError("image_formats must not contain duplicates")
        return self


class ProviderRequestPreview(BaseModel):
    """dry-run 安全预检摘要（11E-2a-fix-1/fix-2）。

    使用与 ProviderRequest 相同的安全字段校验规则；不含 attempt_id、Key、endpoint、正文。
    11E-2a-fix-2：版本字段全部来自权威 Profile；补充 rubric_version。
    """

    model_config = ConfigDict(extra="forbid")

    provider_id: str
    model_id: str
    capability_version: str
    config_version: str
    evidence_package_id: str
    evidence_manifest_sha256: str
    input_fingerprint: str
    scoring_policy_version: str
    rubric_version: str  # 11E-2a-fix-2
    scoring_mode: Literal["mixed", "ai_only"]
    prompt_version: str
    response_schema_version: str
    input_modalities: List[Literal["text", "image"]]
    temperature: Optional[float] = None
    seed: Optional[int] = None
    max_output_tokens: Optional[int] = Field(default=None, gt=0)

    _provider_id = field_validator("provider_id", mode="after")(_validate_stable_id)
    _model_id = field_validator("model_id", mode="after")(_validate_stable_id)
    _scoring_policy_version = field_validator("scoring_policy_version", mode="after")(_validate_stable_id)
    _rubric_version = field_validator("rubric_version", mode="after")(_validate_stable_id)
    _prompt_version = field_validator("prompt_version", mode="after")(_validate_stable_id)
    _response_schema_version = field_validator("response_schema_version", mode="after")(_validate_stable_id)
    _evidence_manifest_sha256 = field_validator("evidence_manifest_sha256", mode="after")(_validate_sha256)
    _input_fingerprint = field_validator("input_fingerprint", mode="after")(_validate_sha256)
