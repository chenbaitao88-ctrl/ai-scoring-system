"""ScoringTaskConfiguration / ScoringTaskCreateRequest（Phase 11E-2a-prerequisite-impl-2 + fix-1）。

- ScoringTaskConfiguration：评分任务的独立不可变配置对象（profile/provider/scoring policy/rubric/
  prompt/response schema + configuration_fingerprint）。
- ScoringTaskCreateRequest：scoring_pipeline 任务创建请求，只含非敏感引用；
  调用方不得传入 task_id/新 item_id/状态/revision/幂等键/创建时间/输出结果/
  Key/Prompt 正文/学生正文/绝对路径。

fix-1 修正：
- 校验函数从 models.identity_validation 导入，消除与 pipeline_task 的运行期循环导入。
- 字段统一为契约名 `scoring_policy_version`（11A-2c ProviderRequest / ScoringInputProfile 一致），
  不再使用 impl-1 的 `provider_policy_version` 命名。
- configuration_fingerprint 内容一致性校验收敛到本模型（单一权威实现）。
- concurrency 上限与 pipeline_task_manager._MAX_CONCURRENCY 对齐（16）。
"""
from __future__ import annotations

from typing import List

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from models.identity_validation import (
    _validate_ascii_id,
    _validate_sha256,
    _validate_uuid,
    sha256_canonical,
)

# 与 pipeline_task_manager._MAX_CONCURRENCY = 16 对齐的安全上限
MAX_SCORING_CONCURRENCY = 16


def _validate_ascii_version(value: str) -> str:
    """安全版本/标识符校验（ASCII 安全字符，拒绝 URL/路径/空白/正文）。"""
    return _validate_ascii_id(value)


class ScoringTaskConfiguration(BaseModel):
    """评分任务不可变配置（11E-2a-prerequisite-impl-1/impl-2）。"""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_assignment=True)

    profile_version: str
    provider_id: str
    model_id: str
    scoring_policy_version: str
    rubric_version: str
    prompt_version: str
    response_schema_version: str
    configuration_fingerprint: str

    _profile_version = field_validator("profile_version", mode="after")(_validate_ascii_version)
    _provider_id = field_validator("provider_id", mode="after")(_validate_ascii_version)
    _model_id = field_validator("model_id", mode="after")(_validate_ascii_version)
    _scoring_policy_version = field_validator("scoring_policy_version", mode="after")(_validate_ascii_version)
    _rubric_version = field_validator("rubric_version", mode="after")(_validate_ascii_version)
    _prompt_version = field_validator("prompt_version", mode="after")(_validate_ascii_version)
    _response_schema_version = field_validator("response_schema_version", mode="after")(_validate_ascii_version)
    _configuration_fingerprint = field_validator("configuration_fingerprint", mode="after")(_validate_sha256)

    @model_validator(mode="after")
    def _fingerprint_matches_content(self) -> "ScoringTaskConfiguration":
        """指纹内容一致性（fix-1 单一权威实现）：伪造指纹在构造时拒绝。"""
        expected = sha256_canonical(self.model_dump(exclude={"configuration_fingerprint"}))
        if self.configuration_fingerprint != expected:
            raise ValueError("configuration_fingerprint 与配置内容不一致")
        return self


class ScoringTaskCreateRequest(BaseModel):
    """scoring_pipeline 任务创建请求（11E-2a-prerequisite-impl-2）。"""

    model_config = ConfigDict(extra="forbid")

    batch_id: str
    source_task_id: str
    source_item_ids: List[str]
    configuration: ScoringTaskConfiguration
    concurrency: int = Field(gt=0, le=MAX_SCORING_CONCURRENCY)

    _batch_id = field_validator("batch_id", mode="after")(_validate_ascii_version)
    _source_task_id = field_validator("source_task_id", mode="after")(_validate_uuid)

    @field_validator("source_item_ids")
    @classmethod
    def _source_item_ids(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError("source_item_ids 不得为空")
        return [_validate_uuid(x) for x in v]
