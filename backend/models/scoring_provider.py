"""
ScoringProvider 契约数据模型（Phase 11D-1b）。

严格依据《Phase11A-2c-ScoringProvider契约.md》v1 与《Phase11D-1a-ScoringProvider最小实施设计.md》实现，
不重新设计字段。本阶段只实现确定性数据模型，不发起网络请求、不解析凭据值。

冻结要点：
- 8 类对象全部 `extra="forbid"`，未知字段拒绝。
- `contract_version` 固定为 `scoring-provider/v1`。
- SHA-256 / fingerprint 一律 64 位小写十六进制。
- 时间字段全部带时区（AwareDatetime，naive 拒绝）。
- 能力三值：`True` / `False` / `"unknown"`（JSON 布尔；字符串 "true"/"false" 拒绝；unknown 不视为支持）。
- capability_ref 固定格式：`capability/<provider_id>/<model_id>/<capability_version>`，三段与 Provider 身份一致。
- ProviderRequest 不包含凭据值；ProviderError 不包含 traceback 或异常原文。
- ProviderSwitchDecision 表达人工批准状态与成功结果保护。

本阶段不实现：网关调用、健康探测、自动切换、凭据解析、文件持久化。
"""
from __future__ import annotations

import re
from typing import Any, List, Literal, Optional

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# ---------------- 常量与校验 ---------------- #

CONTRACT_VERSION = "scoring-provider/v1"
REGISTRY_VERSION = "provider-registry/v1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")  # 凭据引用只能是安全环境变量名
_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")  # endpoint profile 引用，拒绝 URL

# capability_ref 固定格式：capability/<provider_id>/<model_id>/<capability_version>
CAPABILITY_REF_PREFIX = "capability"
_CAPABILITY_REF_RE = re.compile(
    r"^capability/[A-Za-z0-9][A-Za-z0-9._-]{0,127}/"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}/[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
)

# 能力三值（契约 5.1）：True=已验证支持 / False=已验证不支持 / "unknown"=尚未验证。
# JSON true -> True、false -> False；字符串 "true"/"false" 一律拒绝；"unknown" 不满足能力匹配。
TriBool = Literal[True, False, "unknown"]

# 14+1 类标准错误码（契约 8.4 + Phase 11E 扩展）
ProviderErrorCode = Literal[
    "PROVIDER_CONFIG_INVALID",
    "PROVIDER_CREDENTIAL_INVALID",
    "MODEL_CAPABILITY_MISMATCH",
    "PROVIDER_REQUEST_INVALID",
    "PROVIDER_RATE_LIMITED",
    "PROVIDER_QUOTA_EXHAUSTED",
    "PROVIDER_TIMEOUT",
    "PROVIDER_NETWORK_ERROR",
    "PROVIDER_SERVER_ERROR",
    "PROVIDER_INVALID_JSON",
    "PROVIDER_RESPONSE_SCHEMA_INVALID",
    "PROVIDER_CONTENT_SAFETY_REJECTED",
    "PROVIDER_SCORE_OUT_OF_RANGE",
    "PROVIDER_OUTCOME_UNKNOWN",  # Phase 11E 扩展：结果不确定（超时/进程退出/成功未持久化等）
    "PROVIDER_UNKNOWN_ERROR",
]


def _validate_sha256(value: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError("must be 64-char lowercase hex sha256")
    return value


def _validate_sha256_optional(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return _validate_sha256(value)


def _validate_stable_id(value: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValueError("invalid stable identifier")
    return value


def _validate_stable_id_optional(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return _validate_stable_id(value)


def _validate_env_name(value: str) -> str:
    """credential_ref 只能是安全环境变量名（不是值、不是 URL、不是路径）。"""
    if not isinstance(value, str) or not _ENV_NAME_RE.fullmatch(value):
        raise ValueError("credential_ref must be a safe environment variable name")
    return value


def _validate_profile(value: str) -> str:
    """endpoint_profile 只能是配置引用，禁止 URL / 路径 / 空白。"""
    if not isinstance(value, str) or not _PROFILE_RE.fullmatch(value):
        raise ValueError("endpoint_profile must be a safe profile reference, not a URL")
    if "://" in value or "/" in value or "\\" in value:
        raise ValueError("endpoint_profile must not contain URL or path")
    return value


def _contract_version(value: str) -> str:
    if value != CONTRACT_VERSION:
        raise ValueError(f"contract_version must be {CONTRACT_VERSION}")
    return value


# ---------------- capability_ref 纯函数 ---------------- #


def build_capability_ref(provider_id: str, model_id: str, capability_version: str) -> str:
    """构造 canonical capability_ref：capability/<provider_id>/<model_id>/<capability_version>。"""
    for part, name in ((provider_id, "provider_id"), (model_id, "model_id"), (capability_version, "capability_version")):
        if not isinstance(part, str) or not _ID_RE.fullmatch(part):
            raise ValueError(f"invalid {name} for capability ref")
    return f"{CAPABILITY_REF_PREFIX}/{provider_id}/{model_id}/{capability_version}"


def parse_capability_ref(value: str) -> tuple:
    """解析 capability_ref 为 (provider_id, model_id, capability_version)。

    拒绝：非固定前缀、空段、`..`、反斜杠、URL、查询参数、多余层级。
    """
    if not isinstance(value, str) or not _CAPABILITY_REF_RE.fullmatch(value):
        raise ValueError("invalid capability_ref: must be capability/<provider_id>/<model_id>/<capability_version>")
    _prefix, provider_id, model_id, capability_version = value.split("/")
    if ".." in (provider_id, model_id, capability_version) or "\\" in value or "://" in value or "?" in value:
        raise ValueError("invalid capability_ref segment")
    return provider_id, model_id, capability_version


def _validate_capability_ref(value: str) -> str:
    parse_capability_ref(value)  # 校验即返回；不修改值
    return value


# ---------------- ScoringProvider ---------------- #


class ScoringProvider(BaseModel):
    """一个可调用模型通道的版本化注册记录（契约 4）。"""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["scoring-provider/v1"] = CONTRACT_VERSION
    provider_id: str
    provider_type: str
    display_name: str
    endpoint_profile: str  # 配置引用，不是完整地址
    model_id: str
    capability_version: str
    config_version: str
    enabled: bool
    credential_ref: str  # 外部凭据引用 = 环境变量名
    capability_ref: str  # ModelCapability 记录引用
    created_at: AwareDatetime
    updated_at: AwareDatetime
    tags: List[str] = Field(default_factory=list)
    region_profile: Optional[str] = None
    owner_scope: Optional[str] = None
    deprecation_status: Literal["active", "deprecated", "retired"] = "active"
    notes: Optional[str] = None

    _provider_id = field_validator("provider_id", mode="after")(_validate_stable_id)
    _model_id = field_validator("model_id", mode="after")(_validate_stable_id)
    _endpoint_profile = field_validator("endpoint_profile", mode="after")(_validate_profile)
    _credential_ref = field_validator("credential_ref", mode="after")(_validate_env_name)
    _capability_ref = field_validator("capability_ref", mode="after")(_validate_capability_ref)

    @model_validator(mode="after")
    def _capability_ref_identity(self) -> "ScoringProvider":
        """capability_ref 三段必须与 Provider 的 provider_id/model_id/capability_version 一致（11D-1b-fix-1）。"""
        ref_pid, ref_mid, ref_cap = parse_capability_ref(self.capability_ref)
        if (ref_pid, ref_mid, ref_cap) != (self.provider_id, self.model_id, self.capability_version):
            raise ValueError("capability_ref identity mismatch with provider fields")
        return self


# ---------------- ModelCapability ---------------- #


class RateLimit(BaseModel):
    """已知限流信息（契约 5.3）；未知字段为 null。"""

    model_config = ConfigDict(extra="forbid")

    requests_per_minute: Optional[int] = Field(default=None, ge=0)
    tokens_per_minute: Optional[int] = Field(default=None, ge=0)
    images_per_minute: Optional[int] = Field(default=None, ge=0)
    burst_limit: Optional[int] = Field(default=None, ge=0)
    quota_window: Optional[str] = None


class RetryCapability(BaseModel):
    """Provider 层可重试能力声明（契约 5.4）。"""

    model_config = ConfigDict(extra="forbid")

    supports_retry_after: TriBool = "unknown"
    idempotent_request_supported: TriBool = "unknown"
    max_safe_retries: Optional[int] = Field(default=None, ge=0)
    retryable_error_codes: Optional[List[str]] = None


class ModelCapability(BaseModel):
    """模型能力声明（契约 5）。三值能力 unknown 不得用于放行。"""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["scoring-provider/v1"] = CONTRACT_VERSION
    capability_version: str
    provider_id: str
    model_id: str
    text_input: TriBool = "unknown"
    image_input: TriBool = "unknown"
    structured_json_output: TriBool = "unknown"
    max_context_tokens: Optional[int] = Field(default=None, ge=0)
    max_output_tokens: Optional[int] = Field(default=None, ge=0)
    max_images_per_request: Optional[int] = Field(default=None, ge=0)
    supported_image_formats: Optional[List[str]] = None
    system_message: TriBool = "unknown"
    temperature_supported: TriBool = "unknown"
    temperature_min: Optional[float] = None
    temperature_max: Optional[float] = None
    temperature_allowed_values: Optional[List[float]] = None
    request_timeout_seconds: Optional[int] = Field(default=None, ge=0)
    recommended_concurrency: Optional[int] = Field(default=None, ge=0)
    rate_limit: Optional[RateLimit] = None
    retry_capability: RetryCapability = Field(default_factory=RetryCapability)
    seed_supported: TriBool = "unknown"
    verified_at: Optional[AwareDatetime] = None
    verification_source: Literal["static_config", "runtime_probe", "provider_doc", "unknown"] = "static_config"
    # Phase 11E-1b：Provider 幂等能力声明（True 时才允许自动重放，且必须验证来源与时间）
    request_idempotency_supported: TriBool = "unknown"
    idempotency_verification_source: Optional[
        Literal["static_config", "provider_doc", "runtime_probe", "unknown"]
    ] = None
    idempotency_verified_at: Optional[AwareDatetime] = None

    _provider_id = field_validator("provider_id", mode="after")(_validate_stable_id)
    _model_id = field_validator("model_id", mode="after")(_validate_stable_id)

    @model_validator(mode="after")
    def _idempotency_claim_verified(self) -> "ModelCapability":
        """True 时 verification source 不得为 unknown、verified_at 必须存在；
        False/unknown 不得用于放行自动重放（本阶段只声明，不执行判断）。"""
        if self.request_idempotency_supported is True:
            if self.idempotency_verification_source in (None, "unknown"):
                raise ValueError("idempotency True requires non-unknown verification source")
            if self.idempotency_verified_at is None:
                raise ValueError("idempotency True requires verified_at")
        return self

    @model_validator(mode="after")
    def _temperature_range_consistent(self) -> "ModelCapability":
        if (
            self.temperature_min is not None
            and self.temperature_max is not None
            and self.temperature_min > self.temperature_max
        ):
            raise ValueError("temperature_min must not exceed temperature_max")
        return self


# ---------------- ProviderRequest ---------------- #


class ProviderRequest(BaseModel):
    """一次标准化模型调用的不可变请求摘要（契约 6）。不含任何正文与凭据值。"""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["scoring-provider/v1"] = CONTRACT_VERSION
    request_id: str
    task_id: str
    item_id: str
    attempt_id: str
    provider_id: str
    model_id: str
    capability_version: str
    config_version: str
    evidence_package_id: str
    evidence_manifest_sha256: str
    input_fingerprint: str
    scoring_policy_version: str
    scoring_mode: Literal["mixed", "ai_only"]  # 评分口径（11D-3a-prerequisite-fix-1 必填）
    prompt_version: str
    response_schema_version: str
    timeout_seconds: int = Field(gt=0)
    requested_at: AwareDatetime
    input_modalities: List[Literal["text", "image"]]
    evidence_refs: List[str]
    request_payload_sha256: str
    selection_id: str
    temperature: Optional[float] = None
    seed: Optional[int] = None
    max_output_tokens: Optional[int] = Field(default=None, gt=0)
    provider_idempotency_key_hash: Optional[str] = None
    # 原始幂等键属于调用期内存数据（ProviderCallPayload/transport 上下文），
    # ProviderRequest 只保存其 SHA-256；不写 sidecar、不写日志（11E-1b-fix-1）
    trace_id: Optional[str] = None

    _request_id = field_validator("request_id", mode="after")(_validate_stable_id)
    _task_id = field_validator("task_id", mode="after")(_validate_stable_id)
    _item_id = field_validator("item_id", mode="after")(_validate_stable_id)
    _attempt_id = field_validator("attempt_id", mode="after")(_validate_stable_id)
    _provider_id = field_validator("provider_id", mode="after")(_validate_stable_id)
    _model_id = field_validator("model_id", mode="after")(_validate_stable_id)
    _evidence_manifest_sha256 = field_validator("evidence_manifest_sha256", mode="after")(_validate_sha256)
    _input_fingerprint = field_validator("input_fingerprint", mode="after")(_validate_sha256)
    _request_payload_sha256 = field_validator("request_payload_sha256", mode="after")(_validate_sha256)
    _idem_hash = field_validator("provider_idempotency_key_hash", mode="after")(_validate_sha256_optional)


# ---------------- ProviderResponse ---------------- #


class Usage(BaseModel):
    """token / 计量摘要（契约 7.4）；无可靠数据必须为 null，不得估算。"""

    model_config = ConfigDict(extra="forbid")

    input_tokens: Optional[int] = Field(default=None, ge=0)
    output_tokens: Optional[int] = Field(default=None, ge=0)
    total_tokens: Optional[int] = Field(default=None, ge=0)
    image_units: Optional[int] = Field(default=None, ge=0)
    billing_units: Optional[int] = Field(default=None, ge=0)
    cost_amount: Optional[float] = Field(default=None, ge=0)
    cost_currency: Optional[str] = None


class ProviderResponse(BaseModel):
    """一次 Provider 调用结果的不可变摘要（契约 7）。成功不等于最终采用。"""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["scoring-provider/v1"] = CONTRACT_VERSION
    response_id: str
    request_id: str
    task_id: str
    item_id: str
    attempt_id: str
    provider_id: str
    model_id: str
    provider_request_id: Optional[str] = None
    http_status_summary: Optional[str] = None
    result_status: Literal["success", "invalid", "rejected", "error"]
    structured_output_ref: Optional[str] = None
    output_sha256: Optional[str] = None
    usage: Optional[Usage] = None
    latency_ms: Optional[int] = Field(default=None, ge=0)
    completed_at: AwareDatetime
    schema_validation_passed: bool
    scoring_rule_validation_passed: bool
    error_ref: Optional[str] = None
    input_fingerprint: str
    prompt_version: str
    evidence_manifest_sha256: str

    _response_id = field_validator("response_id", mode="after")(_validate_stable_id)
    _request_id = field_validator("request_id", mode="after")(_validate_stable_id)
    _provider_id = field_validator("provider_id", mode="after")(_validate_stable_id)
    _model_id = field_validator("model_id", mode="after")(_validate_stable_id)
    _output_sha256 = field_validator("output_sha256", mode="after")(_validate_sha256_optional)
    _input_fingerprint = field_validator("input_fingerprint", mode="after")(_validate_sha256)
    _evidence_manifest_sha256 = field_validator("evidence_manifest_sha256", mode="after")(_validate_sha256)

    @model_validator(mode="after")
    def _success_requires_validations(self) -> "ProviderResponse":
        if self.result_status == "success" and not (
            self.schema_validation_passed and self.scoring_rule_validation_passed
        ):
            raise ValueError("success response requires both schema and rule validation passed")
        return self


# ---------------- ProviderError ---------------- #


class ProviderError(BaseModel):
    """一次失败或无效调用的结构化错误（契约 8）。message_safe 只允许脱敏摘要。"""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["scoring-provider/v1"] = CONTRACT_VERSION
    error_id: str
    request_id: str
    task_id: str
    item_id: str
    attempt_id: str
    provider_id: str
    model_id: str
    error_code: ProviderErrorCode
    error_category: Literal[
        "config", "credential", "capability", "request", "rate_limit", "quota",
        "timeout", "network", "server", "parse", "schema", "content_safety",
        "range", "unknown",
    ]
    message_safe: str
    retryable: Literal["yes", "no", "conditional"]
    provider_switch_allowed: Literal["yes", "no", "approval_required"]
    manual_review_required: Literal["yes", "no", "after_exhaustion"]
    consumes_provider_call_attempt: bool
    stop_entire_batch: Literal["yes", "no", "if_no_approved_fallback"]
    retry_after_seconds: Optional[int] = Field(default=None, ge=0)
    occurred_at: AwareDatetime
    details_sha256: Optional[str] = None

    _error_id = field_validator("error_id", mode="after")(_validate_stable_id)
    _request_id = field_validator("request_id", mode="after")(_validate_stable_id)
    _provider_id = field_validator("provider_id", mode="after")(_validate_stable_id)
    _model_id = field_validator("model_id", mode="after")(_validate_stable_id)
    _details_sha256 = field_validator("details_sha256", mode="after")(_validate_sha256_optional)


# ---------------- ProviderHealth ---------------- #


class ProviderHealth(BaseModel):
    """Provider 在某一时间窗口的可用性摘要（契约 9）。"""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["scoring-provider/v1"] = CONTRACT_VERSION
    health_id: str
    provider_id: str
    model_id: str
    status: Literal["unknown", "healthy", "degraded", "unavailable", "disabled"]
    observed_at: AwareDatetime
    window_seconds: int = Field(gt=0)
    sample_size: int = Field(ge=0)
    success_rate: Optional[float] = Field(default=None, ge=0, le=1)
    rate_limit_state: Literal["unknown", "normal", "throttled"] = "unknown"
    quota_state: Literal["unknown", "available", "low", "exhausted"] = "unknown"
    p95_latency_ms: Optional[int] = Field(default=None, ge=0)
    consecutive_failures: int = Field(default=0, ge=0)
    last_error_code: Optional[str] = None
    source: Literal["passive", "active_probe", "manual"]
    expires_at: AwareDatetime

    _health_id = field_validator("health_id", mode="after")(_validate_stable_id)
    _provider_id = field_validator("provider_id", mode="after")(_validate_stable_id)
    _model_id = field_validator("model_id", mode="after")(_validate_stable_id)


# ---------------- ProviderSelection ---------------- #


class RejectedCandidate(BaseModel):
    """被拒绝候选及原因（契约 10.2）。"""

    model_config = ConfigDict(extra="forbid")

    provider_id: str
    reason_code: str

    _provider_id = field_validator("provider_id", mode="after")(_validate_stable_id)


class ProviderSelection(BaseModel):
    """一次调用前的候选集、约束检查、选择结果和理由（契约 10）。"""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["scoring-provider/v1"] = CONTRACT_VERSION
    selection_id: str
    task_id: str
    item_id: str
    attempt_id: str
    candidate_provider_ids: List[str]
    required_modalities: List[Literal["text", "image"]]
    required_capabilities: List[str]
    evidence_manifest_sha256: str
    input_fingerprint: str
    scoring_policy_version: str
    prompt_version: str
    response_schema_version: str
    selected_provider_id: Optional[str] = None
    selected_model_id: Optional[str] = None
    selection_status: Literal["selected", "no_candidate", "rejected", "pending_approval", "cancelled"]
    selection_strategy: str
    rejected_candidates: List[RejectedCandidate] = Field(default_factory=list)
    health_snapshot_refs: List[str] = Field(default_factory=list)
    requires_human_approval: bool = False
    approved_by: Optional[str] = None
    approved_at: Optional[AwareDatetime] = None
    created_at: AwareDatetime

    _selection_id = field_validator("selection_id", mode="after")(_validate_stable_id)
    _evidence_manifest_sha256 = field_validator("evidence_manifest_sha256", mode="after")(_validate_sha256)
    _input_fingerprint = field_validator("input_fingerprint", mode="after")(_validate_sha256)


# ---------------- ProviderSwitchDecision ---------------- #


class ProviderSwitchDecision(BaseModel):
    """通道切换的申请、校验和批准结果（契约 11）。"""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["scoring-provider/v1"] = CONTRACT_VERSION
    switch_decision_id: str
    task_id: str
    item_id: str
    source_attempt_id: str
    target_attempt_id: Optional[str] = None
    source_provider_id: str
    source_model_id: str
    target_provider_id: str
    target_model_id: str
    reason_error_id: Optional[str] = None
    reason_error_code: Optional[str] = None
    decision_status: Literal["pending_approval", "approved", "rejected", "executed", "cancelled"]
    same_scoring_policy: bool
    same_prompt_version: bool
    same_evidence_manifest_sha256: bool
    same_input_fingerprint: bool
    same_response_schema_version: bool
    same_scoring_mode: bool  # 11D-3a-prerequisite-fix-1：服务比较后写入，调用方不得声明
    same_temperature: bool
    same_seed: bool
    same_max_output_tokens: bool
    successful_result_exists: bool
    approval_required: bool
    approved_by: Optional[str] = None
    approved_at: Optional[AwareDatetime] = None
    created_at: AwareDatetime
    executed_at: Optional[AwareDatetime] = None
    event_ref: Optional[str] = None

    _switch_decision_id = field_validator("switch_decision_id", mode="after")(_validate_stable_id)
    _source_attempt_id = field_validator("source_attempt_id", mode="after")(_validate_stable_id)
    _target_attempt_id = field_validator("target_attempt_id", mode="after")(_validate_stable_id_optional)

    @model_validator(mode="after")
    def _approval_identity_gate(self) -> "ProviderSwitchDecision":
        """同口径门与批准完整性（11D-3a-prerequisite-fix-1）：
        approved/executed 必须九个 same_* 全 true、successful_result_exists=false、
        approved_by/approved_at 完整、target_attempt_id 存在；executed 还需 executed_at。
        rejected/cancelled 不得伪装为 approved（状态由 decision_status 表达）。"""
        if self.decision_status in ("approved", "executed"):
            all_same = (
                self.same_scoring_policy and self.same_prompt_version
                and self.same_evidence_manifest_sha256 and self.same_input_fingerprint
                and self.same_response_schema_version and self.same_scoring_mode
                and self.same_temperature and self.same_seed and self.same_max_output_tokens
            )
            if not all_same:
                raise ValueError("approved/executed switch requires all nine same-* fields true")
            if self.successful_result_exists:
                raise ValueError("switch forbidden: successful result exists")
            if self.approved_by is None or self.approved_at is None:
                raise ValueError("approved switch requires approved_by and approved_at")
            if self.target_attempt_id is None:
                raise ValueError("approved switch requires target_attempt_id")
            if self.decision_status == "executed" and self.executed_at is None:
                raise ValueError("executed switch requires executed_at")
        return self
