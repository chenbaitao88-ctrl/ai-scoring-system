"""
Provider 切换工作流模型（Phase 11D-3b）。

- `ProviderSwitchTargetProposal`：调用方提交的目标口径输入（内部受控模型）。只含非敏感口径字段，
  不含 same_* 结论、Key、endpoint、Prompt 正文、evidence 正文或 target attempt ID。
- `ProviderSwitchRecord`：切换决定的持久化内部记录（revision + decision + proposal + 审计字段）。
- `SwitchEvent`：审计事件行（仅稳定安全字段）。

所有对象 `extra="forbid"`；时间字段带时区。
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from models.scoring_provider import (
    CONTRACT_VERSION,
    ProviderSwitchDecision,
    _validate_sha256,
    _validate_stable_id,
)


class ProviderSwitchTargetProposal(BaseModel):
    """目标口径提案（11D-3a 设计第四节）。服务与 source ProviderRequest 权威比较后计算九项 same_*。"""

    model_config = ConfigDict(extra="forbid")

    target_provider_id: str
    target_model_id: str
    scoring_policy_version: str
    prompt_version: str
    evidence_package_id: str
    evidence_manifest_sha256: str
    input_fingerprint: str
    response_schema_version: str
    scoring_mode: Literal["mixed", "ai_only"]
    temperature: Optional[float] = None
    seed: Optional[int] = None
    max_output_tokens: Optional[int] = Field(default=None, gt=0)

    _target_provider_id = field_validator("target_provider_id", mode="after")(_validate_stable_id)
    _target_model_id = field_validator("target_model_id", mode="after")(_validate_stable_id)
    _evidence_manifest_sha256 = field_validator("evidence_manifest_sha256", mode="after")(_validate_sha256)
    _input_fingerprint = field_validator("input_fingerprint", mode="after")(_validate_sha256)


class ProviderSwitchRecord(BaseModel):
    """切换决定持久化内部记录。"""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["scoring-provider/v1"] = CONTRACT_VERSION
    revision: int = Field(gt=0)
    decision: ProviderSwitchDecision
    proposal: ProviderSwitchTargetProposal
    idempotency_key: str = Field(min_length=64, max_length=64)
    transition_reason_code: Optional[str] = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    event_sequence: int = Field(ge=0)


class SwitchEvent(BaseModel):
    """审计事件行（仅安全字段）。"""

    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(gt=0)
    decision_id: str
    transition: Literal["created", "approved", "rejected", "cancelled", "executed"]
    from_status: str
    to_status: str
    revision_before: Optional[int] = None
    revision_after: int = Field(gt=0)
    occurred_at: AwareDatetime
    reason_code: Optional[str] = None
    approved_by: Optional[str] = None
