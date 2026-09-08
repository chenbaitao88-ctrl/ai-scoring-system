"""
评分流水线只读协议（Phase 11E-1b）。

- 只定义只读查询 Protocol：不创建、不修改任何对象。
- 不读取环境变量值；不暴露正文、响应原文、Key 或 endpoint。
- ReviewCase 创建写协议延后到 11E-3b，本阶段不定义。
"""
from __future__ import annotations

from typing import List, Optional, Protocol, runtime_checkable

from models.evidence_sidecar import EvidencePackageRecord, ValidationResult
from models.pipeline_task import PipelineItem, PipelineTask
from models.review_case import ReviewCase, ReviewDecision, ResultAdoption, ManualAdjustment
from models.score_attempt import (
    AttemptValidation,
    ScoreAttempt,
    ScoreResultSnapshot,
    ScoringInputProfile,
)
from models.scoring_provider import ProviderResponse
from services.scoring_provider_gateway import ProviderCallPayload


class ScoreAttemptLookup(Protocol):
    """attempt 只读查询。"""

    def get_attempt(self, attempt_id: str) -> Optional[ScoreAttempt]:
        ...

    def list_attempts_for_item(self, task_id: str, item_id: str) -> List[ScoreAttempt]:
        ...


class SuccessfulResultSnapshotLookup(Protocol):
    """成功结果快照只读查询（成功结果保护权威来源）。"""

    def has_successful_snapshot(
        self, task_id: str, item_id: str, attempt_id: Optional[str] = None,
    ) -> bool:
        ...

    def get_snapshot(self, snapshot_id: str) -> Optional[ScoreResultSnapshot]:
        ...


class AttemptValidationLookup(Protocol):
    """校验记录只读查询。"""

    def get_validation(self, validation_id: str) -> Optional[AttemptValidation]:
        ...

    def list_validations_for_attempt(self, attempt_id: str) -> List[AttemptValidation]:
        ...


class ProviderIdempotencyCapabilityLookup(Protocol):
    """Provider 幂等能力只读查询（本阶段只定义查询，不执行放行判断）。"""

    def is_idempotency_supported(self, provider_id: str, model_id: str, capability_version: str) -> bool:
        ...

    def get_idempotency_verification_source(
        self, provider_id: str, model_id: str, capability_version: str,
    ) -> Optional[str]:
        ...


class ReviewCaseLookup(Protocol):
    """ReviewCase 只读查询（ReviewCase 模型与写服务在 11E-3b 实现；本阶段返回 ID 列表）。"""

    def has_open_review_case(self, task_id: str, item_id: str) -> bool:
        ...

    def list_review_case_ids(self, task_id: str, item_id: Optional[str] = None) -> List[str]:
        ...


# ---------------- 11E-3b-1：ReviewCaseStore 只读查询协议 ---------------- #


@runtime_checkable
class ReviewCaseStoreLookup(Protocol):
    """ReviewCaseStore 只读查询协议（11E-3b-1）。

    文件型 Review 事实层按 task/item 分目录，查询必须携带 task_id/item_id 上下文；
    本协议描述该 store 的只读查询能力（写能力由 ReviewCaseStore 具体类提供）。
    """

    def get_review_case(self, task_id: str, item_id: str, review_case_id: str) -> Optional[ReviewCase]:
        ...

    def list_review_cases(self, task_id: str, item_id: Optional[str] = None) -> List[ReviewCase]:
        ...

    def list_open_cases(self, task_id: str, item_id: Optional[str] = None) -> List[ReviewCase]:
        ...

    def has_open_review_case(self, task_id: str, item_id: str) -> bool:
        ...

    def get_review_decision(self, task_id: str, item_id: str, decision_id: str) -> Optional[ReviewDecision]:
        ...

    def list_review_decisions(
        self, task_id: str, item_id: str, review_case_id: Optional[str] = None,
    ) -> List[ReviewDecision]:
        ...

    def get_adoption(self, task_id: str, item_id: str, adoption_id: str) -> Optional[ResultAdoption]:
        ...

    def list_adoptions(self, task_id: str, item_id: str) -> List[ResultAdoption]:
        ...

    def get_active_adoption(self, task_id: str, item_id: str) -> Optional[ResultAdoption]:
        ...

    def get_manual_adjustment(
        self, task_id: str, item_id: str, adjustment_id: str,
    ) -> Optional[ManualAdjustment]:
        ...

    def list_manual_adjustments(self, task_id: str, item_id: str) -> List[ManualAdjustment]:
        ...

    def load_review_events(
        self, task_id: str, item_id: str, review_case_id: Optional[str] = None,
    ) -> List[object]:
        ...


# ---------------- 11E-2a：dry-run 只读协议 ---------------- #


class EvidenceRecordLookup(Protocol):
    """已登记证据包记录只读查询（11B registration/query 服务实现）。

    返回类型为权威登记事实 `EvidencePackageRecord`（sidecar），不是 EvidencePackage 输入对象。
    """

    def get_evidence_record(self, package_id: str) -> Optional[EvidencePackageRecord]:
        ...

    def get_validation_result(self, package_id: str) -> Optional[ValidationResult]:
        ...


class ScoringInputProfileLookup(Protocol):
    """权威评分输入口径只读查询（11E-2a-fix-1）：调用方只传 profile_version 引用。"""

    def get_input_profile(self, profile_version: str) -> Optional[ScoringInputProfile]:
        ...


class PipelineTaskItemLookup(Protocol):
    """PipelineTask / PipelineItem 只读查询（11C store/manager 实现）。"""

    def get_task(self, task_id: str) -> Optional[PipelineTask]:
        ...

    def get_item(self, task_id: str, item_id: str) -> Optional[PipelineItem]:
        ...


# ---------------- 11E-2b-1：ScoreAttemptStore 只读查询协议 ---------------- #


@runtime_checkable
class ScoreFactStoreLookup(Protocol):
    """ScoreAttemptStore 只读查询协议（11E-2b-1）。

    文件型事实存储按 task/item 分目录，查询必须携带 task_id/item_id 上下文；
    本协议描述该 store 的只读查询能力（写能力由 ScoreAttemptStore 具体类提供）。
    """

    def get_attempt(self, task_id: str, item_id: str, attempt_id: str) -> Optional[ScoreAttempt]:
        ...

    def list_attempts(self, task_id: str, item_id: str) -> List[ScoreAttempt]:
        ...

    def get_snapshot(self, task_id: str, item_id: str, snapshot_id: str) -> Optional[ScoreResultSnapshot]:
        ...

    def has_successful_snapshot(
        self, task_id: str, item_id: str, attempt_id: Optional[str] = None,
    ) -> bool:
        ...

    def get_validation(self, task_id: str, item_id: str, validation_id: str) -> Optional[AttemptValidation]:
        ...

    def list_validations(
        self, task_id: str, item_id: str, attempt_id: Optional[str] = None,
    ) -> List[AttemptValidation]:
        ...


# ---------------- 11E-2b-2：正式执行最小协议 ---------------- #


class EvidenceInputAdapter(Protocol):
    """读取"已批准模型输入"并构造仅内存 ProviderCallPayload。

    - 测试只使用合成脱敏文本/图片引用；生产实现不得让 Orchestrator 读取任意文件路径。
    - 返回 payload 只进 Gateway transport；不进入日志、事件或结果对象。
    """

    def build_payload(
        self,
        *,
        task_id: str,
        item_id: str,
        package_id: str,
        evidence_manifest_sha256: str,
        profile: ScoringInputProfile,
    ) -> ProviderCallPayload:
        ...


class ScoreSnapshotFactory(Protocol):
    """从已验证结构化响应构造不可变 ScoreResultSnapshot（评分范围等来自权威配置实现）。"""

    def build(
        self,
        *,
        attempt: ScoreAttempt,
        response: ProviderResponse,
        structured: dict,
        profile: ScoringInputProfile,
        completed_at: object,
    ) -> ScoreResultSnapshot:
        ...


class ScoringItemOperator(Protocol):
    """scoring item 状态推进最小接口（PipelineTaskManager 满足）。"""

    def get_task(self, task_id: str) -> Optional[PipelineTask]:
        ...

    def get_item(self, task_id: str, item_id: str) -> Optional[PipelineItem]:
        ...

    def start_item_stage(
        self, task_id: str, item_id: str, expected_task_revision: int, expected_item_revision: int,
    ) -> PipelineItem:
        ...

    def complete_scoring_item_stage(
        self,
        task_id: str,
        item_id: str,
        expected_task_revision: int,
        expected_item_revision: int,
        attempt_id: Optional[str] = None,
        review_decision_id: Optional[str] = None,
        output_ref: Optional[str] = None,
        output_sha256: Optional[str] = None,
    ) -> PipelineItem:
        ...

    def fail_item_retryable(
        self, task_id: str, item_id: str, expected_task_revision: int, expected_item_revision: int,
        error_code: str,
    ) -> PipelineItem:
        ...

    def fail_item_non_retryable(
        self, task_id: str, item_id: str, expected_task_revision: int, expected_item_revision: int,
        error_code: str,
    ) -> PipelineItem:
        ...

    def reset_scoring_item_to_pending(
        self, task_id: str, item_id: str, expected_task_revision: int, expected_item_revision: int,
        reason_code: str,
    ) -> PipelineItem:
        """scoring item 安全重置 pending（11E-2c 恢复：调用前 stale / 可重试失败，不增加 attempt）。"""
        ...
