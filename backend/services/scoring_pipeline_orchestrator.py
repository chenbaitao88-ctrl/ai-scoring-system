"""
ScoringPipelineOrchestrator（Phase 11E-2a / fix-1 / fix-2 / prerequisite-impl-4 / 11E-2b-2）。

本阶段（11E-2b-2）：新增单 item 正式执行入口 execute_score_item()，首次跑通
"dry-run 准入 -> score pending->running -> 创建 ScoreAttempt(running) -> 安全输入适配 ->
Gateway mock 调用 -> 结构化结果校验 -> 写入不可变 Snapshot/AttemptValidation ->
publish attempt 终态 -> score 阶段完成 -> item 推进 review/pending" 的 mock 闭环。

- 调用方只传 task_id / item_id / execution_request_id；Provider/model/profile/rubric/prompt/
  response_schema/evidence identity 一律来自任务冻结配置与权威证据记录，调用方不得覆盖。
- 幂等与成功保护：已有成功快照短路 already_completed；item 已 running 返回 running_conflict；
  相同 execution_request_id 重放不重复调用 mock Provider；已成功结果永不覆盖/删除/重评。
- outcome_unknown（timeout/network 结果不确定）形成明确 attempt 终态并阻止盲目重试；
  复杂恢复与人工批准重调留 11E-2c。
- 任何中途写入失败不制造"任务显示成功但快照不存在"的假完成。
- 返回 ScoreExecutionResult 只含安全标识与状态，不含 Prompt/证据正文/模型原文/Key/endpoint。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict

from models.identity_validation import sha256_canonical
from models.review_case import AdoptionScope, ResultAdoption
from models.scoring_configuration import ScoringTaskConfiguration
from models.score_attempt import (
    ActorRef,
    AttemptValidation,
    DryRunCheck,
    DryRunPlan,
    ProviderRequestPreview,
    ScoreAttempt,
    ScoreResultSnapshot,
    ScoringInputProfile,
    ValidationCheck,
)
from models.scoring_provider import ProviderError, ProviderRequest
from services.score_pipeline_protocols import (
    EvidenceInputAdapter,
    EvidenceRecordLookup,
    PipelineTaskItemLookup,
    ReviewCaseLookup,
    ScoreAttemptLookup,
    ScoreSnapshotFactory,
    ScoringInputProfileLookup,
    ScoringItemOperator,
    SuccessfulResultSnapshotLookup,
)
from services.pipeline_evidence_checks import check_evidence_identity, check_validation_binding
from services.scoring_provider_gateway import (
    ProviderCallPayload,
    ScoringProviderGateway,
    canonical_payload_sha256,
)
from services.scoring_provider_registry import ProviderRegistryError, ScoringProviderRegistry

# ---------------- 稳定错误码（dry-run 冻结，impl-4 扩展） ---------------- #

ERR_TASK_OR_ITEM_NOT_FOUND = "DRY_RUN_TASK_OR_ITEM_NOT_FOUND"
ERR_SCORE_STAGE_NOT_IN_SCOPE = "DRY_RUN_SCORE_STAGE_NOT_IN_SCOPE"
ERR_TASK_STATE_NOT_ALLOWED = "DRY_RUN_TASK_STATE_NOT_ALLOWED"  # 11E-2a-fix-2
ERR_TASK_STAGE_NOT_ALLOWED = "DRY_RUN_TASK_STAGE_NOT_ALLOWED"  # 11E-2a-fix-2
ERR_ITEM_STAGE_NOT_ALLOWED = "DRY_RUN_ITEM_STAGE_NOT_ALLOWED"  # 11E-2a-fix-2
ERR_ITEM_STATE_NOT_ALLOWED = "DRY_RUN_ITEM_STATE_NOT_ALLOWED"
ERR_ITEM_ALREADY_RUNNING = "DRY_RUN_ITEM_ALREADY_RUNNING"  # 11E-2a-fix-1
ERR_ITEM_RECOVERY_REQUIRED = "DRY_RUN_ITEM_RECOVERY_REQUIRED"  # 11E-2a-fix-1
ERR_EVIDENCE_RECORD_MISSING = "DRY_RUN_EVIDENCE_RECORD_MISSING"
ERR_EVIDENCE_MISMATCH = "DRY_RUN_EVIDENCE_MISMATCH"
ERR_VALIDATION_MISSING = "DRY_RUN_VALIDATION_MISSING"
ERR_VALIDATION_FAILED = "DRY_RUN_VALIDATION_FAILED"
ERR_MANUAL_ONLY = "DRY_RUN_MANUAL_ONLY"
ERR_SUCCESS_EXISTS = "DRY_RUN_SUCCESS_EXISTS"
ERR_OUTCOME_UNKNOWN_EXISTS = "DRY_RUN_OUTCOME_UNKNOWN_EXISTS"
ERR_OPEN_REVIEW_CASE = "DRY_RUN_OPEN_REVIEW_CASE"
ERR_PROVIDER_NOT_FOUND = "DRY_RUN_PROVIDER_NOT_FOUND"
ERR_PROVIDER_DISABLED = "DRY_RUN_PROVIDER_DISABLED"
ERR_CAPABILITY_MISMATCH = "DRY_RUN_CAPABILITY_MISMATCH"
ERR_PROFILE_MISSING = "DRY_RUN_PROFILE_MISSING"  # 11E-2a-fix-1
ERR_UNSAFE_REQUEST_SUMMARY = "DRY_RUN_UNSAFE_REQUEST_SUMMARY"
# 11E-2a-prerequisite-impl-4：绑定正式 scoring 任务
ERR_TASK_TYPE_NOT_SCORING = "DRY_RUN_TASK_TYPE_NOT_SCORING"
ERR_ITEM_TYPE_NOT_SCORING = "DRY_RUN_ITEM_TYPE_NOT_SCORING"
ERR_SOURCE_BINDING_MISMATCH = "DRY_RUN_SOURCE_BINDING_MISMATCH"
ERR_CONFIGURATION_INVALID = "DRY_RUN_CONFIGURATION_INVALID"
ERR_FROZEN_SELECTION_MISMATCH = "DRY_RUN_FROZEN_SELECTION_MISMATCH"

# 11E-2b-2：正式执行稳定错误码（结果对象 error_code）
ERR_EXEC_NOT_FOUND = "EXECUTION_TASK_OR_ITEM_NOT_FOUND"
ERR_EXEC_BLOCKED = "EXECUTION_BLOCKED"
ERR_EXEC_ALREADY_COMPLETED = "EXECUTION_ALREADY_COMPLETED"
ERR_EXEC_RUNNING_CONFLICT = "EXECUTION_ITEM_RUNNING_CONFLICT"
ERR_EXEC_ITEM_START_FAILED = "EXECUTION_ITEM_START_FAILED"
ERR_EXEC_ATTEMPT_WRITE_FAILED = "EXECUTION_ATTEMPT_WRITE_FAILED"
ERR_EXEC_SNAPSHOT_WRITE_FAILED = "EXECUTION_SNAPSHOT_WRITE_FAILED"
ERR_EXEC_VALIDATION_WRITE_FAILED = "EXECUTION_VALIDATION_WRITE_FAILED"
ERR_EXEC_ITEM_COMPLETE_FAILED = "EXECUTION_ITEM_COMPLETE_FAILED"
ERR_EXEC_PROVIDER_FAILED = "EXECUTION_PROVIDER_FAILED"
ERR_EXEC_OUTCOME_UNKNOWN = "EXECUTION_OUTCOME_UNKNOWN"
ERR_EXEC_INTERNAL = "EXECUTION_INTERNAL_ERROR"

# 结果不确定类错误：必须形成 outcome_unknown attempt 并阻止盲目重试
_OUTCOME_UNKNOWN_CODES = frozenset({"PROVIDER_TIMEOUT", "PROVIDER_NETWORK_ERROR"})
# 与 pipeline_task_manager._MAX_ATTEMPTS 对齐（scoring item 最大尝试次数）
_MAX_ATTEMPTS = 3

# 11E-2c：恢复稳定错误/决策码（以用户冻结命名为准）
RECOVERY_ALREADY_COMPLETED = "RECOVERY_ALREADY_COMPLETED"
RECOVERY_COMPLETED_FROM_FACTS = "RECOVERY_COMPLETED_FROM_FACTS"
RECOVERY_RETRY_APPROVAL_REQUIRED = "RECOVERY_RETRY_APPROVAL_REQUIRED"
RECOVERY_OUTCOME_UNKNOWN_BLOCKED = "RECOVERY_OUTCOME_UNKNOWN_BLOCKED"
RECOVERY_STALE_PRECALL_RESET = "RECOVERY_STALE_PRECALL_RESET"
RECOVERY_FACTS_INCOMPLETE = "RECOVERY_FACTS_INCOMPLETE"
RECOVERY_FACT_BINDING_MISMATCH = "RECOVERY_FACT_BINDING_MISMATCH"
RECOVERY_CONCURRENT_REQUEST = "RECOVERY_CONCURRENT_REQUEST"
RECOVERY_TASK_OR_ITEM_INVALID = "RECOVERY_TASK_OR_ITEM_INVALID"
RECOVERY_NOT_APPLICABLE = "RECOVERY_NOT_APPLICABLE"
# 11F-1b：active 人工最终锁阻断自动恢复（契约 §3.4；与 review_case 模型码一致）
RECOVERY_BLOCKED_BY_LOCK = "RECOVERY_BLOCKED_BY_LOCK"

# 11E-2c：执行成功路径新增稳定码（response fact 写入失败）
ERR_EXEC_RESPONSE_FACT_WRITE_FAILED = "EXECUTION_RESPONSE_FACT_WRITE_FAILED"

# 11E-3b-2：ReviewCase 采用流程接入（apply_review_decision）稳定码
ERR_APP_NOT_APPLICABLE = "REVIEW_APPLICATION_NOT_APPLICABLE"
ERR_APP_ADOPTION_FAILED = "REVIEW_APPLICATION_ADOPTION_FAILED"
ERR_APP_ITEM_ADVANCE_FAILED = "REVIEW_APPLICATION_ITEM_ADVANCE_FAILED"

ExecutionOutcome = Literal[
    "blocked", "already_completed", "running_conflict", "succeeded",
    "failed", "outcome_unknown", "internal_error",
]

RecoveryOutcome = Literal[
    "already_completed", "completed_from_facts", "retry_approval_required",
    "outcome_unknown_blocked", "stale_precall_reset", "facts_incomplete",
    "fact_binding_mismatch", "concurrent_request", "task_or_item_invalid",
    "not_applicable", "blocked_by_lock",
]


class ScoreExecutionResult(BaseModel):
    """单 item 正式执行结果（只含安全标识与状态；不含 Prompt/证据正文/模型原文/Key/endpoint）。"""

    model_config = ConfigDict(extra="forbid")

    execution_request_id: str
    task_id: str
    item_id: str
    outcome: ExecutionOutcome
    attempt_id: Optional[str] = None
    snapshot_id: Optional[str] = None
    validation_id: Optional[str] = None
    provider_called: bool = False
    error_code: Optional[str] = None


class ProviderResponseFact(BaseModel):
    """Provider 成功响应的安全持久化事实（11E-2c 恢复来源）。

    - 保存结构化评分结果与快照重建所需字段；不保存 Prompt/证据正文/Key。
    - rationale_summary 使用安全占位（不保存评语正文；fact 只承载结构化结果）。
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["provider-response-fact/v1"] = "provider-response-fact/v1"
    fact_id: str
    attempt_id: str
    task_id: str
    item_id: str
    output_sha256: str
    completed_at: datetime
    snapshot_payload: dict  # ScoreResultSnapshot 的安全数值载荷（含 scale；重建快照用）


class ScoreRecoveryResult(BaseModel):
    """单 item 恢复结果（只含安全标识与状态；恢复路径恒不调用 Provider）。"""

    model_config = ConfigDict(extra="forbid")

    recovery_request_id: str
    task_id: str
    item_id: str
    outcome: RecoveryOutcome
    error_code: Optional[str] = None
    attempt_id: Optional[str] = None
    snapshot_id: Optional[str] = None
    validation_id: Optional[str] = None
    provider_called: bool = False


# ---------------- 11E-3b-2：人工复核采用结果（ReviewCase 接入） ----------------

ReviewApplicationOutcome = Literal[
    "adopted", "already_completed", "not_adopted", "blocked", "internal_error",
]


class ReviewApplicationResult(BaseModel):
    """人工复核决定落地结果（只含安全标识与状态；不含评语正文/证据/Key）。"""

    model_config = ConfigDict(extra="forbid")

    adoption_request_id: str
    task_id: str
    item_id: str
    decision_id: str
    outcome: ReviewApplicationOutcome
    adoption_id: Optional[str] = None
    review_decision_id: Optional[str] = None
    error_code: Optional[str] = None


class ScoringPipelineOrchestrator:
    """评分流水线编排器（dry-run 只读 + 11E-2b-2 正式执行入口）。"""

    def __init__(
        self,
        task_item_lookup: PipelineTaskItemLookup,
        evidence_lookup: EvidenceRecordLookup,
        attempt_lookup: ScoreAttemptLookup,
        success_lookup: SuccessfulResultSnapshotLookup,
        review_case_lookup: ReviewCaseLookup,
        profile_lookup: ScoringInputProfileLookup,
        registry: ScoringProviderRegistry,
        clock: Optional[Callable[[], datetime]] = None,
        *,
        attempt_store: Optional[Any] = None,
        item_operator: Optional[ScoringItemOperator] = None,
        gateway: Optional[ScoringProviderGateway] = None,
        evidence_adapter: Optional[EvidenceInputAdapter] = None,
        snapshot_factory: Optional[ScoreSnapshotFactory] = None,
        review_store: Optional[Any] = None,  # 11E-3b-2：ReviewCaseStore 写能力（采用流程）
        uuid_factory: Optional[Callable[[], str]] = None,
    ):
        self._task_item = task_item_lookup
        self._evidence = evidence_lookup
        self._attempts = attempt_lookup
        self._success = success_lookup
        self._reviews = review_case_lookup
        self._profiles = profile_lookup
        self._registry = registry
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._attempt_store = attempt_store
        self._operator = item_operator
        self._gateway = gateway
        self._evidence_adapter = evidence_adapter
        self._snapshots = snapshot_factory
        self._review_store = review_store
        self._uuid = uuid_factory or (lambda: f"exec-{datetime.now(timezone.utc).timestamp()}")

    # ---------------- 主入口 ---------------- #

    def dry_run(
        self,
        task_id: str,
        item_id: str,
        dry_run_request_id: str,
        provider_id: Optional[str] = None,
        model_id: Optional[str] = None,
        profile_version: Optional[str] = None,
    ) -> DryRunPlan:
        """单 item 只读评估；返回安全计划（零副作用）。

        impl-4：Provider/model/profile/版本从 task 不可变 configuration snapshot 读取；
        旧签名参数若传入必须与冻结配置一致，不一致返回稳定错误，禁止静默覆盖。
        """
        now = self._clock()
        checks: List[DryRunCheck] = []
        plan_kwargs = dict(
            dry_run_request_id=dry_run_request_id,
            task_id=task_id,
            item_id=item_id,
            created_at=now,
        )

        def fail(code: str, check_code: str) -> DryRunPlan:
            checks.append(DryRunCheck(check_code=check_code, passed=False, blocking=True))
            return DryRunPlan(**plan_kwargs, decision="blocked", checks=checks,
                              blocking_error_code=code)

        # 1. Task/Item 存在且 Item 属于 Task
        task = self._task_item.get_task(task_id)
        item = self._task_item.get_item(task_id, item_id) if task is not None else None
        if task is None or item is None or item.task_id != task_id:
            return fail(ERR_TASK_OR_ITEM_NOT_FOUND, "task_item_exists")
        checks.append(DryRunCheck(check_code="task_item_exists", passed=True))

        # 2. Task 类型必须是 scoring_pipeline（evidence 任务在此拒绝）
        if task.task_type != "scoring_pipeline":
            return fail(ERR_TASK_TYPE_NOT_SCORING, "task_type_scoring")
        checks.append(DryRunCheck(check_code="task_type_scoring", passed=True))

        # 3. Item 类型必须是 scoring_pipeline
        if item.task_type != "scoring_pipeline":
            return fail(ERR_ITEM_TYPE_NOT_SCORING, "item_type_scoring")
        checks.append(DryRunCheck(check_code="item_type_scoring", passed=True))

        # 4. Task execution_scope 必须包含 score
        if "score" not in getattr(task, "execution_scope", []):
            return fail(ERR_SCORE_STAGE_NOT_IN_SCOPE, "score_in_scope")
        checks.append(DryRunCheck(check_code="score_in_scope", passed=True))

        # 5. Task 状态：仅允许预检的非终态状态（pending/running）
        if task.status in ("paused", "completed", "completed_with_errors", "failed", "cancelled"):
            return fail(ERR_TASK_STATE_NOT_ALLOWED, "task_state")
        checks.append(DryRunCheck(check_code="task_state", passed=True))

        # 6. Task current_stage 必须为 score 且属于 execution_scope
        if task.current_stage != "score" or task.current_stage not in task.execution_scope:
            return fail(ERR_TASK_STAGE_NOT_ALLOWED, "task_stage")
        checks.append(DryRunCheck(check_code="task_stage", passed=True))

        # 7. Item 属于 Task item_index，且索引与 item 权威快照一致
        index_entries = {e.item_id: e for e in getattr(task, "item_index", [])}
        entry = index_entries.get(item_id)
        if entry is None:
            return fail(ERR_TASK_OR_ITEM_NOT_FOUND, "item_in_index")
        if (
            entry.package_id != item.package_id
            or entry.package_revision != item.package_revision
            or entry.input_fingerprint != item.input_fingerprint
            or entry.item_revision != item.item_revision
            or entry.status != item.status
            or entry.evidence_level != item.evidence_level
        ):
            return fail(ERR_EVIDENCE_MISMATCH, "index_item_identity")
        checks.append(DryRunCheck(check_code="item_in_index", passed=True))

        # 8. Item current_stage 必须为 score 且属于 Task execution_scope
        if item.current_stage != "score" or item.current_stage not in task.execution_scope:
            return fail(ERR_ITEM_STAGE_NOT_ALLOWED, "item_stage")
        checks.append(DryRunCheck(check_code="item_stage", passed=True))

        # 9. Item 状态（仅 pending 可计划；running/failed/skipped/manual_review/completed 分流）
        if item.status == "manual_review":
            return DryRunPlan(**plan_kwargs, checks=checks, decision="manual_review",
                              manual_review_required=True,
                              blocking_error_code=ERR_ITEM_STATE_NOT_ALLOWED)
        if item.status == "running":
            return fail(ERR_ITEM_ALREADY_RUNNING, "item_state")
        if item.status == "failed":
            return fail(ERR_ITEM_RECOVERY_REQUIRED, "item_state")
        if item.status == "skipped":
            return fail(ERR_ITEM_STATE_NOT_ALLOWED, "item_state")
        if item.status == "completed":
            if not self._success.has_successful_snapshot(task_id, item_id):
                return fail(ERR_ITEM_STATE_NOT_ALLOWED, "item_state")
            checks.append(DryRunCheck(check_code="item_state", passed=True))
            return DryRunPlan(**plan_kwargs, checks=checks, decision="already_completed",
                              blocking_error_code=ERR_SUCCESS_EXISTS)
        if item.status != "pending":
            return fail(ERR_ITEM_STATE_NOT_ALLOWED, "item_state")
        checks.append(DryRunCheck(check_code="item_state", passed=True))

        # 10. 上游绑定（impl-4）：source_task_id / source_item_id 存在且绑定一致
        source_task_id = getattr(task, "source_task_id", None)
        source_item_id = getattr(item, "source_item_id", None)
        if not source_task_id or not source_item_id:
            return fail(ERR_SOURCE_BINDING_MISMATCH, "source_refs_present")
        source_task = self._task_item.get_task(source_task_id)
        source_item = self._task_item.get_item(source_task_id, source_item_id) if source_task is not None else None
        if source_task is None or source_item is None or source_item.task_id != source_task_id:
            return fail(ERR_SOURCE_BINDING_MISMATCH, "source_refs_present")
        if source_task.task_type != "evidence_preparation_pipeline":
            return fail(ERR_SOURCE_BINDING_MISMATCH, "source_task_type")
        if source_item.status != "completed":
            return fail(ERR_SOURCE_BINDING_MISMATCH, "source_item_settled")
        if (
            source_item.package_id != item.package_id
            or source_item.package_revision != item.package_revision
            or source_item.manifest_sha256 != item.manifest_sha256
            or source_item.registration_record_id != item.registration_record_id
            or source_item.validation_id != item.validation_id
            or source_item.input_fingerprint != item.input_fingerprint
        ):
            return fail(ERR_SOURCE_BINDING_MISMATCH, "source_item_identity")
        checks.append(DryRunCheck(check_code="source_binding", passed=True))

        # 11. 冻结配置（impl-4）：snapshot 类型与 fingerprint 合法
        snapshot = getattr(task, "configuration_snapshot", None)
        if not isinstance(snapshot, ScoringTaskConfiguration):
            return fail(ERR_CONFIGURATION_INVALID, "configuration_snapshot_type")
        if snapshot.configuration_fingerprint != getattr(task, "configuration_fingerprint", None):
            return fail(ERR_CONFIGURATION_INVALID, "configuration_fingerprint")
        checks.append(DryRunCheck(check_code="configuration_snapshot", passed=True))

        # 12. 冻结选择：Provider/model/profile 从快照读取；调用方参数必须一致
        frozen_provider = snapshot.provider_id
        frozen_model = snapshot.model_id
        frozen_profile_version = snapshot.profile_version
        if provider_id is not None and provider_id != frozen_provider:
            return fail(ERR_FROZEN_SELECTION_MISMATCH, "frozen_provider")
        if model_id is not None and model_id != frozen_model:
            return fail(ERR_FROZEN_SELECTION_MISMATCH, "frozen_model")
        if profile_version is not None and profile_version != frozen_profile_version:
            return fail(ERR_FROZEN_SELECTION_MISMATCH, "frozen_profile")
        checks.append(DryRunCheck(check_code="frozen_selection", passed=True))

        # 13. 权威 EvidencePackageRecord + ValidationResult 全绑定核对
        record = self._evidence.get_evidence_record(item.package_id)
        err = check_evidence_identity(item, record)
        if err == ERR_EVIDENCE_RECORD_MISSING:
            return fail(ERR_EVIDENCE_RECORD_MISSING, "evidence_record_exists")
        if err is not None:
            return fail(ERR_EVIDENCE_MISMATCH, "evidence_identity")
        checks.append(DryRunCheck(check_code="evidence_identity", passed=True))
        validation = self._evidence.get_validation_result(item.package_id)
        err2 = check_validation_binding(item, record, validation)
        if err2 == ERR_VALIDATION_MISSING:
            return fail(ERR_VALIDATION_MISSING, "validation_exists")
        if err2 == ERR_EVIDENCE_MISMATCH:
            return fail(ERR_EVIDENCE_MISMATCH, "evidence_level_identity")
        if err2 is not None:
            return fail(ERR_VALIDATION_FAILED, "validation_binding")
        checks.append(DryRunCheck(check_code="validation_binding", passed=True))

        # 14. Evidence level
        limited_evidence = item.evidence_level == "limited"
        if item.evidence_level == "manual_only":
            return DryRunPlan(**plan_kwargs, checks=checks, decision="manual_review",
                              manual_review_required=True,
                              blocking_error_code=ERR_MANUAL_ONLY)
        checks.append(DryRunCheck(check_code="evidence_level", passed=True))

        # 15. 已有成功快照 -> 短路
        if self._success.has_successful_snapshot(task_id, item_id):
            return DryRunPlan(**plan_kwargs, checks=checks, decision="already_completed",
                              blocking_error_code=ERR_SUCCESS_EXISTS)
        checks.append(DryRunCheck(check_code="no_success_snapshot", passed=True))

        # 16. outcome_unknown attempt 阻断
        attempts = self._attempts.list_attempts_for_item(task_id, item_id)
        if any(a.status == "outcome_unknown" for a in attempts):
            return fail(ERR_OUTCOME_UNKNOWN_EXISTS, "no_outcome_unknown")
        checks.append(DryRunCheck(check_code="no_outcome_unknown", passed=True))

        # 17. 未关闭 ReviewCase 阻断
        if self._reviews.has_open_review_case(task_id, item_id):
            return fail(ERR_OPEN_REVIEW_CASE, "no_open_review_case")
        checks.append(DryRunCheck(check_code="no_open_review_case", passed=True))

        # 18. Provider/Model 存在且启用（冻结配置）
        try:
            provider, capability = self._registry.resolve_provider_capability(frozen_provider, frozen_model)
        except ProviderRegistryError:
            return fail(ERR_PROVIDER_NOT_FOUND, "provider_exists")
        if not provider.enabled or provider.deprecation_status == "retired":
            return fail(ERR_PROVIDER_DISABLED, "provider_enabled")
        checks.append(DryRunCheck(check_code="provider_enabled", passed=True))

        # 19. 权威 ScoringInputProfile（冻结 profile_version）
        profile = self._profiles.get_input_profile(frozen_profile_version)
        if profile is None:
            return fail(ERR_PROFILE_MISSING, "profile_exists")
        checks.append(DryRunCheck(check_code="profile_exists", passed=True))

        # 20. 冻结快照版本与权威 Profile 一致（creator 已保证，dry-run 复核）
        if (
            snapshot.scoring_policy_version != profile.scoring_policy_version
            or snapshot.rubric_version != profile.rubric_version
            or snapshot.prompt_version != profile.prompt_version
            or snapshot.response_schema_version != profile.response_schema_version
        ):
            return fail(ERR_FROZEN_SELECTION_MISMATCH, "frozen_versions_match_profile")
        checks.append(DryRunCheck(check_code="frozen_versions_match_profile", passed=True))

        # 21. Capability 匹配
        if not self._capability_matches(capability, profile):
            return fail(ERR_CAPABILITY_MISMATCH, "capability_match")
        checks.append(DryRunCheck(check_code="capability_match", passed=True))

        # 22. ProviderRequestPreview（版本全部来自快照/Profile 一致口径）
        try:
            preview = self._build_preview(item, provider, capability, profile)
        except Exception:
            return fail(ERR_UNSAFE_REQUEST_SUMMARY, "request_summary_safe")
        checks.append(DryRunCheck(check_code="request_summary_safe", passed=True))

        # 23. 返回 dry-run 计划
        return DryRunPlan(
            **plan_kwargs,
            checks=checks,
            decision="ready",
            provider_id=frozen_provider,
            model_id=frozen_model,
            capability_version=capability.capability_version,
            evidence_package_id=item.package_id,
            evidence_manifest_sha256=item.manifest_sha256,
            input_fingerprint=item.input_fingerprint,
            scoring_policy_version=profile.scoring_policy_version,
            rubric_version=profile.rubric_version,
            prompt_version=profile.prompt_version,
            response_schema_version=profile.response_schema_version,
            scoring_mode=profile.scoring_mode,
            input_modalities=list(profile.input_modalities),
            manual_review_required=False,
            limited_evidence=limited_evidence,
            provider_call_planned=True,
            blocking_error_code=None,
        )

    # ---------------- 内部 ---------------- #

    @staticmethod
    def _capability_matches(capability, profile: ScoringInputProfile) -> bool:
        """按权威 Profile 要求匹配能力（11E-2a-fix-2 完整规则；不读取 Prompt 或证据正文）。"""
        modalities = profile.input_modalities
        if "text" in modalities and capability.text_input is not True:
            return False
        if "image" in modalities:
            if capability.image_input is not True:
                return False
            if profile.max_images is None or profile.max_images <= 0:
                return False
            max_imgs = capability.max_images_per_request
            if max_imgs is None or profile.max_images > max_imgs:
                return False
            if not profile.image_formats:
                return False
            supported = capability.supported_image_formats or []
            norm = sorted(set(f.lower() for f in profile.image_formats))
            if not all(f in supported for f in norm):
                return False
        if profile.structured_json_required and capability.structured_json_output is not True:
            return False
        if profile.system_message_required and capability.system_message is not True:
            return False
        if profile.temperature is not None:
            if capability.temperature_supported is not True:
                return False
            if capability.temperature_min is not None and profile.temperature < capability.temperature_min:
                return False
            if capability.temperature_max is not None and profile.temperature > capability.temperature_max:
                return False
            if capability.temperature_allowed_values:
                if profile.temperature not in capability.temperature_allowed_values:
                    return False
        if profile.seed is not None and capability.seed_supported is not True:
            return False
        if profile.max_output_tokens is not None:
            cap_max = capability.max_output_tokens
            if cap_max is None or profile.max_output_tokens > cap_max:
                return False
        return True

    @staticmethod
    def _build_preview(item, provider, capability, profile: ScoringInputProfile) -> ProviderRequestPreview:
        """构造 ProviderRequestPreview（版本全部来自权威 Profile；不含 attempt_id/Key/endpoint/正文）。"""
        return ProviderRequestPreview(
            provider_id=provider.provider_id,
            model_id=provider.model_id,
            capability_version=capability.capability_version,
            config_version=provider.config_version,
            evidence_package_id=item.package_id,
            evidence_manifest_sha256=item.manifest_sha256,
            input_fingerprint=item.input_fingerprint,
            scoring_policy_version=profile.scoring_policy_version,
            rubric_version=profile.rubric_version,
            scoring_mode=profile.scoring_mode,
            prompt_version=profile.prompt_version,
            response_schema_version=profile.response_schema_version,
            input_modalities=list(profile.input_modalities),
            temperature=profile.temperature,
            seed=profile.seed,
            max_output_tokens=profile.max_output_tokens,
        )

    # ---------------- 11E-2b-2：单 item 正式执行 ---------------- #

    async def execute_score_item(
        self,
        task_id: str,
        item_id: str,
        execution_request_id: str,
    ) -> ScoreExecutionResult:
        """单 item mock Provider 评分闭环（不调用真实模型/凭据/学生材料）。

        调用方不得传入或覆盖 provider/model/profile/rubric/prompt/response_schema/
        evidence identity/attempt_id/分数——全部来自任务冻结配置与权威证据记录。
        """
        deps = (self._attempt_store, self._operator, self._gateway,
                self._evidence_adapter, self._snapshots)
        if any(d is None for d in deps):
            raise RuntimeError("execute_score_item dependencies not injected")

        # 0. 成功结果短路（无论 item 是否已推进到 review/pending）：已有成功快照不重评
        if self._success.has_successful_snapshot(task_id, item_id):
            return self._result(execution_request_id, task_id, item_id,
                                "already_completed", error_code=ERR_EXEC_ALREADY_COMPLETED)

        # 1. dry-run 准入（零副作用）
        plan = self.dry_run(task_id, item_id, dry_run_request_id=f"exec-{execution_request_id}")
        if plan.decision == "already_completed":
            return self._result(execution_request_id, task_id, item_id,
                                "already_completed", error_code=ERR_EXEC_ALREADY_COMPLETED)
        if plan.decision != "ready":
            return self._result(execution_request_id, task_id, item_id,
                                "blocked", error_code=plan.blocking_error_code or ERR_EXEC_BLOCKED)

        # 2. 新鲜读取 task/item（dry-run 后可能已变化）
        task = self._task_item.get_task(task_id)
        item = self._task_item.get_item(task_id, item_id)
        if task is None or item is None or item.task_id != task_id:
            return self._result(execution_request_id, task_id, item_id,
                                "internal_error", error_code=ERR_EXEC_NOT_FOUND)

        # 3. 并发保护：item 已 running 禁止第二次并发调用
        if item.status == "running":
            return self._result(execution_request_id, task_id, item_id,
                                "running_conflict", error_code=ERR_EXEC_RUNNING_CONFLICT)

        snapshot_cfg = task.configuration_snapshot
        if not isinstance(snapshot_cfg, ScoringTaskConfiguration):
            return self._result(execution_request_id, task_id, item_id,
                                "internal_error", error_code=ERR_EXEC_INTERNAL)
        record = self._evidence.get_evidence_record(item.package_id)
        if record is None:
            return self._result(execution_request_id, task_id, item_id,
                                "internal_error", error_code=ERR_EXEC_NOT_FOUND)

        now = self._clock()
        attempt_id = self._uuid()
        request_id = self._uuid()

        # 4. score pending -> running（CAS 事务；失败零假完成）
        try:
            running_item = self._operator.start_item_stage(
                task_id, item_id,
                expected_task_revision=task.revision,
                expected_item_revision=item.item_revision,
            )
        except Exception:
            return self._result(execution_request_id, task_id, item_id,
                                "internal_error", error_code=ERR_EXEC_ITEM_START_FAILED)
        task = self._task_item.get_task(task_id)
        if task is None:
            return self._result(execution_request_id, task_id, item_id,
                                "internal_error", error_code=ERR_EXEC_NOT_FOUND)

        # 5. 权威 profile / 已批准模型输入 / ProviderRequest
        profile = self._profiles.get_input_profile(snapshot_cfg.profile_version)
        if profile is None:
            return self._result(execution_request_id, task_id, item_id,
                                "internal_error", error_code=ERR_EXEC_INTERNAL)
        try:
            payload = self._evidence_adapter.build_payload(
                task_id=task_id, item_id=item_id, package_id=item.package_id,
                evidence_manifest_sha256=item.manifest_sha256, profile=profile,
            )
            provider, capability = self._registry.resolve_provider_capability(
                snapshot_cfg.provider_id, snapshot_cfg.model_id,
            )
            request = self._build_exec_request(
                task, running_item, snapshot_cfg, profile, capability, provider,
                attempt_id, request_id, payload, now,
            )
        except Exception:
            return self._result(execution_request_id, task_id, item_id,
                                "internal_error", error_code=ERR_EXEC_INTERNAL)

        # 6. 创建 running ScoreAttempt（不可变事实；终态在调用完成后发布）
        try:
            attempt = self._make_running_attempt(
                task, running_item, snapshot_cfg, provider, capability,
                record, attempt_id, request, now,
            )
            self._attempt_store.create_attempt(task_id, item_id, attempt)
        except Exception:
            return self._result(execution_request_id, task_id, item_id,
                                "internal_error", error_code=ERR_EXEC_ATTEMPT_WRITE_FAILED)

        # 7. 调用前标记（不可歧义：Provider 是否已开始）——标记失败则不得调用
        try:
            self._attempt_store.mark_attempt_dispatched(task_id, item_id, attempt_id, now)
        except Exception:
            return self._result(execution_request_id, task_id, item_id,
                                "internal_error", error_code=ERR_EXEC_ATTEMPT_WRITE_FAILED,
                                attempt_id=attempt_id)

        # 8. Gateway mock 调用（唯一一次 Provider 调用点）
        try:
            response, structured = await self._gateway.call(request, payload)
        except Exception:
            return self._result(execution_request_id, task_id, item_id,
                                "internal_error", error_code=ERR_EXEC_INTERNAL,
                                attempt_id=attempt_id, provider_called=True)
        if isinstance(response, ProviderError):
            return self._handle_provider_error(
                execution_request_id, task_id, item_id, running_item,
                task, attempt, response,
            )

        # 9. 成功路径：response fact -> snapshot -> validation -> publish 终态 -> 完成 score 阶段
        completed_at = self._clock()
        duration_ms = max(0, int((completed_at - now).total_seconds() * 1000))
        try:
            snapshot = self._snapshots.build(
                attempt=attempt, response=response, structured=structured,
                profile=profile, completed_at=completed_at,
            )
        except Exception:
            return self._result(execution_request_id, task_id, item_id,
                                "internal_error", error_code=ERR_EXEC_SNAPSHOT_WRITE_FAILED,
                                attempt_id=attempt_id, provider_called=True)
        # 先持久化安全响应事实（本地后续写入失败时的恢复来源；不保存评语正文/Key）
        try:
            fact = ProviderResponseFact(
                fact_id=response.response_id,
                attempt_id=attempt_id,
                task_id=task_id,
                item_id=item_id,
                output_sha256=response.output_sha256,
                completed_at=completed_at,
                snapshot_payload=snapshot.model_dump(mode="json"),
            )
            self._attempt_store.write_response_fact(task_id, item_id, fact)
        except Exception:
            return self._result(execution_request_id, task_id, item_id,
                                "internal_error", error_code=ERR_EXEC_RESPONSE_FACT_WRITE_FAILED,
                                attempt_id=attempt_id, provider_called=True)
        try:
            self._attempt_store.write_snapshot(task_id, item_id, snapshot)
        except Exception:
            return self._result(execution_request_id, task_id, item_id,
                                "internal_error", error_code=ERR_EXEC_SNAPSHOT_WRITE_FAILED,
                                attempt_id=attempt_id, provider_called=True)
        validation = AttemptValidation(
            schema_version="attempt-validation/v1",
            validation_id=self._uuid(),
            attempt_id=attempt_id,
            snapshot_id=snapshot.snapshot_id,
            validation_revision=1,
            validator_version=profile.response_schema_version,
            checks=[
                ValidationCheck(check_code="STRUCTURE_VALIDATION", passed=True),
                ValidationCheck(check_code="SCORING_RULE_VALIDATION", passed=True),
            ],
            overall_status="passed",
            adoption_candidate=True,
            manual_review_required=False,
            review_reason_codes=[],
            validated_at=completed_at,
            validated_by="orchestrator",
        )
        try:
            self._attempt_store.write_validation(task_id, item_id, validation)
        except Exception:
            return self._result(execution_request_id, task_id, item_id,
                                "internal_error", error_code=ERR_EXEC_VALIDATION_WRITE_FAILED,
                                attempt_id=attempt_id, snapshot_id=snapshot.snapshot_id,
                                provider_called=True)
        terminal = attempt.model_copy(update={
            "status": "succeeded",
            "completed_at": completed_at,
            "duration_ms": duration_ms,
            "response_hash": response.output_sha256,
            "result_snapshot_ref": snapshot.snapshot_id,
            "validation_ref": validation.validation_id,
        })
        try:
            self._attempt_store.publish_attempt_terminal(task_id, item_id, terminal)
        except Exception:
            return self._result(execution_request_id, task_id, item_id,
                                "internal_error", error_code=ERR_EXEC_ATTEMPT_WRITE_FAILED,
                                attempt_id=attempt_id, snapshot_id=snapshot.snapshot_id,
                                provider_called=True)

        # 9. 完成 score 阶段 -> review/pending（失败不得伪装成完成）
        task_latest = self._task_item.get_task(task_id)
        if task_latest is None:
            return self._result(execution_request_id, task_id, item_id,
                                "internal_error", error_code=ERR_EXEC_ITEM_COMPLETE_FAILED,
                                attempt_id=attempt_id, snapshot_id=snapshot.snapshot_id,
                                provider_called=True)
        try:
            self._operator.complete_scoring_item_stage(
                task_id, item_id,
                expected_task_revision=task_latest.revision,
                expected_item_revision=running_item.item_revision,
                attempt_id=attempt_id,
            )
        except Exception:
            return self._result(execution_request_id, task_id, item_id,
                                "internal_error", error_code=ERR_EXEC_ITEM_COMPLETE_FAILED,
                                attempt_id=attempt_id, snapshot_id=snapshot.snapshot_id,
                                provider_called=True)
        # 10. 一致性校验（引用绑定完整后才返回成功）
        try:
            self._attempt_store.verify_fact_bindings(task_id, item_id, attempt_id)
        except Exception:
            return self._result(execution_request_id, task_id, item_id,
                                "internal_error", error_code=ERR_EXEC_INTERNAL,
                                attempt_id=attempt_id, snapshot_id=snapshot.snapshot_id,
                                provider_called=True)
        return self._result(
            execution_request_id, task_id, item_id, "succeeded",
            attempt_id=attempt_id, snapshot_id=snapshot.snapshot_id,
            validation_id=validation.validation_id, provider_called=True,
        )

    # ---------------- 11E-2c：失败恢复与调用交付保护 ----------------

    def recover_score_item(
        self,
        task_id: str,
        item_id: str,
        recovery_request_id: str,
    ) -> ScoreRecoveryResult:
        """单 item 失败恢复（断线/超时/进程中断后安全恢复；恒不调用 Provider）。

        调用方不得传入 Provider/模型/Prompt/分数/attempt_id 或伪造恢复状态；
        恢复依据全部来自 task/item 状态、ScoreAttempt Store、Snapshot/Validation、
        ProviderResponseFact 与既有事件/error code。
        """
        if self._attempt_store is None or self._operator is None:
            raise RuntimeError("recover_score_item dependencies not injected")
        now = self._clock()

        # 1. 校验正式 scoring task/item 绑定
        task = self._task_item.get_task(task_id)
        item = self._task_item.get_item(task_id, item_id)
        if task is None or item is None or item.task_id != task_id:
            return self._recovery(recovery_request_id, task_id, item_id,
                                  "task_or_item_invalid", RECOVERY_TASK_OR_ITEM_INVALID)
        if task.task_type != "scoring_pipeline" or item.task_type != "scoring_pipeline":
            return self._recovery(recovery_request_id, task_id, item_id,
                                  "task_or_item_invalid", RECOVERY_TASK_OR_ITEM_INVALID)

        # 3. 最新 attempt（先加载：成功快照短路仅适用于已成功 attempt）
        attempts = self._attempt_store.list_attempts(task_id, item_id)

        # 2. 成功快照短路（succeeded attempt + 快照存在；中间态如 validation 写失败后 attempt 仍 running 不走短路）
        latest_succeeded = next((a for a in reversed(attempts) if a.status == "succeeded"), None)
        if latest_succeeded is not None and self._success.has_successful_snapshot(task_id, item_id):
            return self._recover_with_snapshot(task, item, recovery_request_id)

        if not attempts:
            return self._recover_no_attempt(task, item, recovery_request_id, now)
        latest = attempts[-1]

        # 4. 冻结口径核对：恢复不得改变冻结 Provider/模型/版本/证据指纹
        if (
            latest.input_fingerprint != item.input_fingerprint
            or latest.package_id != item.package_id
            or latest.evidence_manifest_sha256 != item.manifest_sha256
        ):
            return self._recovery(recovery_request_id, task_id, item_id,
                                  "fact_binding_mismatch", RECOVERY_FACT_BINDING_MISMATCH,
                                  attempt_id=latest.attempt_id)

        if latest.status == "outcome_unknown":
            return self._recovery(recovery_request_id, task_id, item_id,
                                  "outcome_unknown_blocked", RECOVERY_OUTCOME_UNKNOWN_BLOCKED,
                                  attempt_id=latest.attempt_id)
        # 11F-1b：active 人工最终锁阻断自动恢复推进/重试（契约 §3.4；不影响已成功短路）
        if self._review_store is not None and latest.status != "succeeded":
            try:
                lock = self._review_store.get_active_final_lock(task_id, item_id)
            except Exception:
                lock = None  # 锁事实损坏等由 store 显式失败路径处理；此处不伪装成功
            if lock is not None:
                return self._recovery(recovery_request_id, task_id, item_id,
                                      "blocked_by_lock", RECOVERY_BLOCKED_BY_LOCK,
                                      attempt_id=latest.attempt_id)
        if latest.status == "succeeded":
            return self._recover_succeeded(task, item, latest, recovery_request_id)
        if latest.status == "failed":
            return self._recover_failed(task, item, latest, recovery_request_id)
        if latest.status == "running":
            return self._recover_running(task, item, latest, recovery_request_id, now)
        return self._recovery(recovery_request_id, task_id, item_id,
                              "not_applicable", RECOVERY_NOT_APPLICABLE,
                              attempt_id=latest.attempt_id)

    def _recover_with_snapshot(self, task: Any, item: Any, recovery_request_id: str) -> ScoreRecoveryResult:
        """已有成功快照：短路；若 item 仍在 score/running 则补齐为 review/pending。"""
        snapshots = self._attempt_store.list_snapshots(task.task_id, item.item_id)
        snap = snapshots[-1]
        attempt_id: Optional[str] = None
        for a in reversed(self._attempt_store.list_attempts(task.task_id, item.item_id)):
            if a.status == "succeeded":
                attempt_id = a.attempt_id
                break
        if item.status == "running" and item.current_stage == "score":
            task_latest = self._task_item.get_task(task.task_id)
            try:
                self._operator.complete_scoring_item_stage(
                    task.task_id, item.item_id,
                    expected_task_revision=task_latest.revision,
                    expected_item_revision=item.item_revision,
                    attempt_id=attempt_id,
                )
            except Exception:
                return self._recovery(recovery_request_id, task.task_id, item.item_id,
                                      "concurrent_request", RECOVERY_CONCURRENT_REQUEST,
                                      attempt_id=attempt_id, snapshot_id=snap.snapshot_id)
            return self._recovery(recovery_request_id, task.task_id, item.item_id,
                                  "completed_from_facts", RECOVERY_COMPLETED_FROM_FACTS,
                                  attempt_id=attempt_id, snapshot_id=snap.snapshot_id)
        return self._recovery(recovery_request_id, task.task_id, item.item_id,
                              "already_completed", RECOVERY_ALREADY_COMPLETED,
                              attempt_id=attempt_id, snapshot_id=snap.snapshot_id)

    def _recover_no_attempt(self, task: Any, item: Any, recovery_request_id: str, now: datetime) -> ScoreRecoveryResult:
        """无 attempt 事实：调用前（Provider 未接收）。"""
        if item.status == "pending" and item.current_stage == "score":
            return self._recovery(recovery_request_id, task.task_id, item.item_id,
                                  "not_applicable", RECOVERY_NOT_APPLICABLE)
        if item.status == "running":
            if self._is_stale(item, now):
                return self._reset_to_pending(task, item, recovery_request_id)
            return self._recovery(recovery_request_id, task.task_id, item.item_id,
                                  "concurrent_request", RECOVERY_CONCURRENT_REQUEST)
        if item.status == "failed":
            if item.retryable and item.attempt_count < _MAX_ATTEMPTS and item.last_error is not None:
                if item.last_error.error_code == "PROVIDER_OUTCOME_UNKNOWN":
                    return self._recovery(recovery_request_id, task.task_id, item.item_id,
                                          "outcome_unknown_blocked", RECOVERY_OUTCOME_UNKNOWN_BLOCKED)
                return self._recovery(recovery_request_id, task.task_id, item.item_id,
                                      "retry_approval_required", RECOVERY_RETRY_APPROVAL_REQUIRED)
            return self._recovery(recovery_request_id, task.task_id, item.item_id,
                                  "not_applicable", RECOVERY_NOT_APPLICABLE)
        return self._recovery(recovery_request_id, task.task_id, item.item_id,
                              "not_applicable", RECOVERY_NOT_APPLICABLE)

    def _recover_succeeded(self, task: Any, item: Any, attempt: ScoreAttempt,
                           recovery_request_id: str) -> ScoreRecoveryResult:
        """attempt 已成功：校验事实完整性；仅补齐 item 状态，不重调 Provider。"""
        snap = self._attempt_store.get_snapshot(task.task_id, item.item_id, attempt.result_snapshot_ref)
        val = self._attempt_store.get_validation(task.task_id, item.item_id, attempt.validation_ref)
        if snap is None or val is None:
            return self._recovery(recovery_request_id, task.task_id, item.item_id,
                                  "facts_incomplete", RECOVERY_FACTS_INCOMPLETE,
                                  attempt_id=attempt.attempt_id)
        try:
            self._attempt_store.verify_fact_bindings(task.task_id, item.item_id, attempt.attempt_id)
        except Exception:
            return self._recovery(recovery_request_id, task.task_id, item.item_id,
                                  "fact_binding_mismatch", RECOVERY_FACT_BINDING_MISMATCH,
                                  attempt_id=attempt.attempt_id)
        if item.status == "running" and item.current_stage == "score":
            task_latest = self._task_item.get_task(task.task_id)
            try:
                self._operator.complete_scoring_item_stage(
                    task.task_id, item.item_id,
                    expected_task_revision=task_latest.revision,
                    expected_item_revision=item.item_revision,
                    attempt_id=attempt.attempt_id,
                )
            except Exception:
                return self._recovery(recovery_request_id, task.task_id, item.item_id,
                                      "concurrent_request", RECOVERY_CONCURRENT_REQUEST,
                                      attempt_id=attempt.attempt_id, snapshot_id=snap.snapshot_id)
            return self._recovery(recovery_request_id, task.task_id, item.item_id,
                                  "completed_from_facts", RECOVERY_COMPLETED_FROM_FACTS,
                                  attempt_id=attempt.attempt_id, snapshot_id=snap.snapshot_id,
                                  validation_id=val.validation_id)
        if item.current_stage == "review" and item.status == "pending":
            return self._recovery(recovery_request_id, task.task_id, item.item_id,
                                  "already_completed", RECOVERY_ALREADY_COMPLETED,
                                  attempt_id=attempt.attempt_id, snapshot_id=snap.snapshot_id,
                                  validation_id=val.validation_id)
        # 状态与事实矛盾：禁止猜测修复
        return self._recovery(recovery_request_id, task.task_id, item.item_id,
                              "fact_binding_mismatch", RECOVERY_FACT_BINDING_MISMATCH,
                              attempt_id=attempt.attempt_id, snapshot_id=snap.snapshot_id)

    def _recover_failed(self, task: Any, item: Any, attempt: ScoreAttempt,
                        recovery_request_id: str) -> ScoreRecoveryResult:
        """attempt 明确失败：outcome_unknown 阻断；retryable 仅返回待批准（不自动调用）。"""
        err = attempt.error
        if err is None:
            return self._recovery(recovery_request_id, task.task_id, item.item_id,
                                  "fact_binding_mismatch", RECOVERY_FACT_BINDING_MISMATCH,
                                  attempt_id=attempt.attempt_id)
        if err.error_code == "PROVIDER_OUTCOME_UNKNOWN":
            return self._recovery(recovery_request_id, task.task_id, item.item_id,
                                  "outcome_unknown_blocked", RECOVERY_OUTCOME_UNKNOWN_BLOCKED,
                                  attempt_id=attempt.attempt_id)
        if item.retryable and item.attempt_count < _MAX_ATTEMPTS and err.retryable in ("yes", "conditional"):
            return self._recovery(recovery_request_id, task.task_id, item.item_id,
                                  "retry_approval_required", RECOVERY_RETRY_APPROVAL_REQUIRED,
                                  attempt_id=attempt.attempt_id)
        return self._recovery(recovery_request_id, task.task_id, item.item_id,
                              "not_applicable", RECOVERY_NOT_APPLICABLE,
                              attempt_id=attempt.attempt_id)

    def _recover_running(self, task: Any, item: Any, attempt: ScoreAttempt,
                         recovery_request_id: str, now: datetime) -> ScoreRecoveryResult:
        """attempt 未终态：按 dispatch 标记区分调用前/调用后。"""
        events = self._attempt_store.load_events(task.task_id, item.item_id)
        dispatched = any(
            e.get("event_type") == "ATTEMPT_DISPATCHED" and e.get("object_id") == attempt.attempt_id
            for e in events
        )
        if not dispatched:
            # 调用前中断：Provider 未接收请求 -> 可安全恢复 pending（仅 stale）
            if item.status == "running":
                if self._is_stale(item, now):
                    return self._reset_to_pending(task, item, recovery_request_id)
                return self._recovery(recovery_request_id, task.task_id, item.item_id,
                                      "concurrent_request", RECOVERY_CONCURRENT_REQUEST,
                                      attempt_id=attempt.attempt_id)
            if item.status == "pending":
                return self._recovery(recovery_request_id, task.task_id, item.item_id,
                                      "not_applicable", RECOVERY_NOT_APPLICABLE,
                                      attempt_id=attempt.attempt_id)
            return self._recovery(recovery_request_id, task.task_id, item.item_id,
                                  "fact_binding_mismatch", RECOVERY_FACT_BINDING_MISMATCH,
                                  attempt_id=attempt.attempt_id)
        # 调用后：Provider 已开始
        fact = self._attempt_store.get_response_fact(task.task_id, item.item_id, attempt.attempt_id)
        if fact is not None:
            return self._recover_from_response_fact(task, item, attempt, fact, recovery_request_id, now)
        # 无响应事实：结果未知 -> outcome_unknown（阻止盲目重试）
        try:
            unknown_error = ProviderError(
                contract_version="scoring-provider/v1",
                error_id=self._uuid(),
                request_id=attempt.provider_request_id or f"req-{attempt.attempt_id}",
                task_id=task.task_id,
                item_id=item.item_id,
                attempt_id=attempt.attempt_id,
                provider_id=attempt.provider_id,
                model_id=attempt.model_id,
                error_code="PROVIDER_OUTCOME_UNKNOWN",
                error_category="unknown",
                message_safe="provider outcome cannot be confirmed",
                retryable="no",
                provider_switch_allowed="approval_required",
                manual_review_required="yes",
                consumes_provider_call_attempt=True,
                stop_entire_batch="no",
                retry_after_seconds=None,
                occurred_at=now,
                details_sha256=None,
            )
            terminal = attempt.model_copy(update={
                "status": "outcome_unknown", "completed_at": now, "error": unknown_error,
            })
            self._attempt_store.publish_attempt_terminal(task.task_id, item.item_id, terminal)
            self._safe_fail_item(task.task_id, item.item_id, task, item,
                                 error_code="PROVIDER_OUTCOME_UNKNOWN", retryable=False)
            return self._recovery(recovery_request_id, task.task_id, item.item_id,
                                  "outcome_unknown_blocked", RECOVERY_OUTCOME_UNKNOWN_BLOCKED,
                                  attempt_id=attempt.attempt_id)
        except Exception:
            return self._recovery(recovery_request_id, task.task_id, item.item_id,
                                  "fact_binding_mismatch", RECOVERY_FACT_BINDING_MISMATCH,
                                  attempt_id=attempt.attempt_id)

    def _recover_from_response_fact(self, task: Any, item: Any, attempt: ScoreAttempt,
                                    fact: dict, recovery_request_id: str, now: datetime) -> ScoreRecoveryResult:
        """调用后成功响应事实已保存：重建 snapshot/validation -> publish 终态 -> 补齐 item。
        全程不再次调用 Provider。"""
        try:
            fact_obj = ProviderResponseFact.model_validate(fact)
            snapshot = ScoreResultSnapshot.model_validate(dict(fact_obj.snapshot_payload))
            self._attempt_store.write_snapshot(task.task_id, item.item_id, snapshot)
        except ScoreFactStoreError as exc:
            if exc.error_code == "SCORE_SNAPSHOT_CONFLICT":
                return self._recovery(recovery_request_id, task.task_id, item.item_id,
                                      "fact_binding_mismatch", RECOVERY_FACT_BINDING_MISMATCH,
                                      attempt_id=attempt.attempt_id)
            return self._recovery(recovery_request_id, task.task_id, item.item_id,
                                  "facts_incomplete", RECOVERY_FACTS_INCOMPLETE,
                                  attempt_id=attempt.attempt_id)
        except Exception:
            return self._recovery(recovery_request_id, task.task_id, item.item_id,
                                  "facts_incomplete", RECOVERY_FACTS_INCOMPLETE,
                                  attempt_id=attempt.attempt_id)
        validation = AttemptValidation(
            schema_version="attempt-validation/v1",
            validation_id=self._uuid(),
            attempt_id=attempt.attempt_id,
            snapshot_id=snapshot.snapshot_id,
            validation_revision=1,
            validator_version=attempt.response_schema_version,
            checks=[
                ValidationCheck(check_code="STRUCTURE_VALIDATION", passed=True),
                ValidationCheck(check_code="SCORING_RULE_VALIDATION", passed=True),
            ],
            overall_status="passed",
            adoption_candidate=True,
            manual_review_required=False,
            review_reason_codes=[],
            validated_at=now,
            validated_by="recovery",
        )
        try:
            self._attempt_store.write_validation(task.task_id, item.item_id, validation)
        except Exception:
            return self._recovery(recovery_request_id, task.task_id, item.item_id,
                                  "concurrent_request", RECOVERY_CONCURRENT_REQUEST,
                                  attempt_id=attempt.attempt_id, snapshot_id=snapshot.snapshot_id)
        duration_ms = max(0, int((fact_obj.completed_at - attempt.started_at).total_seconds() * 1000))
        terminal = attempt.model_copy(update={
            "status": "succeeded",
            "completed_at": fact_obj.completed_at,
            "duration_ms": duration_ms,
            "response_hash": fact_obj.output_sha256,
            "result_snapshot_ref": snapshot.snapshot_id,
            "validation_ref": validation.validation_id,
        })
        try:
            self._attempt_store.publish_attempt_terminal(task.task_id, item.item_id, terminal)
        except Exception:
            return self._recovery(recovery_request_id, task.task_id, item.item_id,
                                  "concurrent_request", RECOVERY_CONCURRENT_REQUEST,
                                  attempt_id=attempt.attempt_id, snapshot_id=snapshot.snapshot_id)
        task_latest = self._task_item.get_task(task.task_id)
        if task_latest is None:
            return self._recovery(recovery_request_id, task.task_id, item.item_id,
                                  "concurrent_request", RECOVERY_CONCURRENT_REQUEST,
                                  attempt_id=attempt.attempt_id, snapshot_id=snapshot.snapshot_id)
        try:
            self._operator.complete_scoring_item_stage(
                task.task_id, item.item_id,
                expected_task_revision=task_latest.revision,
                expected_item_revision=item.item_revision,
                attempt_id=attempt.attempt_id,
            )
        except Exception:
            return self._recovery(recovery_request_id, task.task_id, item.item_id,
                                  "concurrent_request", RECOVERY_CONCURRENT_REQUEST,
                                  attempt_id=attempt.attempt_id, snapshot_id=snapshot.snapshot_id)
        return self._recovery(recovery_request_id, task.task_id, item.item_id,
                              "completed_from_facts", RECOVERY_COMPLETED_FROM_FACTS,
                              attempt_id=attempt.attempt_id, snapshot_id=snapshot.snapshot_id,
                              validation_id=validation.validation_id)

    def _reset_to_pending(self, task: Any, item: Any, recovery_request_id: str) -> ScoreRecoveryResult:
        """调用前 stale：安全重置 pending（不增加 attempt；不调用 Provider）。"""
        task_latest = self._task_item.get_task(task.task_id)
        try:
            self._operator.reset_scoring_item_to_pending(
                task.task_id, item.item_id,
                expected_task_revision=task_latest.revision,
                expected_item_revision=item.item_revision,
                reason_code="STALE_PRECALL_RECOVERY",
            )
        except Exception:
            return self._recovery(recovery_request_id, task.task_id, item.item_id,
                                  "concurrent_request", RECOVERY_CONCURRENT_REQUEST)
        return self._recovery(recovery_request_id, task.task_id, item.item_id,
                              "stale_precall_reset", RECOVERY_STALE_PRECALL_RESET)

    @staticmethod
    def _is_stale(item: Any, now: datetime) -> bool:
        """stale 判定（与 manager 冻结语义一致：running 且心跳严格超过 300s）。"""
        if item.status != "running" or item.heartbeat_updated_at is None:
            return False
        age = (now - item.heartbeat_updated_at).total_seconds()
        return age > 300

    def _recovery(
        self,
        recovery_request_id: str,
        task_id: str,
        item_id: str,
        outcome: RecoveryOutcome,
        error_code: Optional[str] = None,
        *,
        attempt_id: Optional[str] = None,
        snapshot_id: Optional[str] = None,
        validation_id: Optional[str] = None,
    ) -> ScoreRecoveryResult:
        return ScoreRecoveryResult(
            recovery_request_id=recovery_request_id,
            task_id=task_id,
            item_id=item_id,
            outcome=outcome,
            error_code=error_code,
            attempt_id=attempt_id,
            snapshot_id=snapshot_id,
            validation_id=validation_id,
            provider_called=False,
        )

    # ---------------- 执行内部 ---------------- #

    def _handle_provider_error(
        self,
        execution_request_id: str,
        task_id: str,
        item_id: str,
        running_item: Any,
        task: Any,
        attempt: ScoreAttempt,
        error: ProviderError,
    ) -> ScoreExecutionResult:
        """Provider 失败路径：outcome_unknown 明确阻断；明确失败按五维 retryable 归类。
        两者都必须先落 attempt 终态事实，再尝试推进 item（失败不掩盖事实）。"""
        completed_at = self._clock()
        provider_called = error.consumes_provider_call_attempt
        if error.error_code in _OUTCOME_UNKNOWN_CODES:
            unknown_error = ProviderError(
                contract_version="scoring-provider/v1",
                error_id=self._uuid(),
                request_id=error.request_id,
                task_id=error.task_id,
                item_id=error.item_id,
                attempt_id=error.attempt_id,
                provider_id=error.provider_id,
                model_id=error.model_id,
                error_code="PROVIDER_OUTCOME_UNKNOWN",
                error_category="unknown",
                message_safe="provider outcome cannot be confirmed",
                retryable="no",
                provider_switch_allowed="approval_required",
                manual_review_required="yes",
                consumes_provider_call_attempt=error.consumes_provider_call_attempt,
                stop_entire_batch="no",
                retry_after_seconds=None,
                occurred_at=completed_at,
                details_sha256=None,
            )
            terminal = attempt.model_copy(update={
                "status": "outcome_unknown",
                "completed_at": completed_at,
                "error": unknown_error,
            })
            try:
                self._attempt_store.publish_attempt_terminal(task_id, item_id, terminal)
            except Exception:
                return self._result(execution_request_id, task_id, item_id,
                                    "internal_error", error_code=ERR_EXEC_ATTEMPT_WRITE_FAILED,
                                    attempt_id=attempt.attempt_id, provider_called=provider_called)
            # item 置为非可重试失败（PROVIDER_OUTCOME_UNKNOWN 阻断盲目重试）
            self._safe_fail_item(task_id, item_id, task, running_item,
                                 error_code="PROVIDER_OUTCOME_UNKNOWN", retryable=False)
            return self._result(execution_request_id, task_id, item_id,
                                "outcome_unknown", error_code=ERR_EXEC_OUTCOME_UNKNOWN,
                                attempt_id=attempt.attempt_id, provider_called=provider_called)

        terminal = attempt.model_copy(update={
            "status": "failed",
            "completed_at": completed_at,
            "error": error,
        })
        try:
            self._attempt_store.publish_attempt_terminal(task_id, item_id, terminal)
        except Exception:
            return self._result(execution_request_id, task_id, item_id,
                                "internal_error", error_code=ERR_EXEC_ATTEMPT_WRITE_FAILED,
                                attempt_id=attempt.attempt_id, provider_called=provider_called)
        retryable = error.retryable in ("yes", "conditional")
        self._safe_fail_item(task_id, item_id, task, running_item,
                             error_code=error.error_code, retryable=retryable)
        return self._result(execution_request_id, task_id, item_id,
                            "failed", error_code=error.error_code,
                            attempt_id=attempt.attempt_id,
                            provider_called=provider_called)

    def _safe_fail_item(
        self, task_id: str, item_id: str, task: Any, running_item: Any,
        error_code: str, retryable: bool,
    ) -> None:
        """尽力推进 item 失败状态；失败不掩盖已落盘 attempt 事实（恢复留 11E-2c）。"""
        try:
            latest = self._task_item.get_task(task_id)
            rev = latest.revision if latest is not None else task.revision
            method = (self._operator.fail_item_retryable if retryable
                      else self._operator.fail_item_non_retryable)
            method(task_id, item_id, expected_task_revision=rev,
                   expected_item_revision=running_item.item_revision,
                   error_code=error_code)
        except Exception:
            pass

    def _build_exec_request(
        self,
        task: Any,
        running_item: Any,
        snapshot_cfg: ScoringTaskConfiguration,
        profile: ScoringInputProfile,
        capability: Any,
        provider: Any,
        attempt_id: str,
        request_id: str,
        payload: ProviderCallPayload,
        now: datetime,
    ) -> ProviderRequest:
        return ProviderRequest(
            contract_version="scoring-provider/v1",
            request_id=request_id,
            task_id=task.task_id,
            item_id=running_item.item_id,
            attempt_id=attempt_id,
            provider_id=snapshot_cfg.provider_id,
            model_id=snapshot_cfg.model_id,
            capability_version=capability.capability_version,
            config_version=provider.config_version,
            evidence_package_id=running_item.package_id,
            evidence_manifest_sha256=running_item.manifest_sha256,
            input_fingerprint=running_item.input_fingerprint,
            scoring_policy_version=snapshot_cfg.scoring_policy_version,
            scoring_mode=profile.scoring_mode,
            prompt_version=snapshot_cfg.prompt_version,
            response_schema_version=snapshot_cfg.response_schema_version,
            timeout_seconds=capability.request_timeout_seconds or 120,
            requested_at=now,
            input_modalities=list(profile.input_modalities),
            evidence_refs=[running_item.package_id],
            request_payload_sha256=canonical_payload_sha256(payload),
            selection_id=f"selection-{request_id}",
            temperature=profile.temperature,
            seed=profile.seed,
            max_output_tokens=profile.max_output_tokens,
        )

    def _make_running_attempt(
        self,
        task: Any,
        running_item: Any,
        snapshot_cfg: ScoringTaskConfiguration,
        provider: Any,
        capability: Any,
        record: Any,
        attempt_id: str,
        request: ProviderRequest,
        now: datetime,
    ) -> ScoreAttempt:
        return ScoreAttempt(
            contract_version="score-attempt-review/v1",
            schema_version="score-attempt/v1",
            attempt_id=attempt_id,
            attempt_number=running_item.attempt_count,
            task_id=task.task_id,
            item_id=running_item.item_id,
            submission_id=record.submission_id,
            package_id=running_item.package_id,
            package_revision=running_item.package_revision,
            evidence_manifest_sha256=running_item.manifest_sha256,
            input_fingerprint=running_item.input_fingerprint,
            provider_id=snapshot_cfg.provider_id,
            provider_config_version=provider.config_version,
            model_id=snapshot_cfg.model_id,
            model_capability_version=capability.capability_version,
            provider_request_id=request.request_id,
            previous_attempt_id=None,
            switch_decision_id=None,
            scoring_policy_version=snapshot_cfg.scoring_policy_version,
            rubric_version=snapshot_cfg.rubric_version,
            prompt_version=snapshot_cfg.prompt_version,
            response_schema_version=snapshot_cfg.response_schema_version,
            status="running",
            created_at=now,
            created_by=ActorRef(actor_type="orchestrator", actor_id="scoring-pipeline"),
            started_at=now,
            completed_at=None,
            duration_ms=None,
            error=None,
            result_snapshot_ref=None,
            validation_ref=None,
            request_hash=request.request_payload_sha256,
            response_hash=None,
        )

    def _result(
        self,
        execution_request_id: str,
        task_id: str,
        item_id: str,
        outcome: ExecutionOutcome,
        *,
        error_code: Optional[str] = None,
        attempt_id: Optional[str] = None,
        snapshot_id: Optional[str] = None,
        validation_id: Optional[str] = None,
        provider_called: bool = False,
    ) -> ScoreExecutionResult:
        return ScoreExecutionResult(
            execution_request_id=execution_request_id,
            task_id=task_id,
            item_id=item_id,
            outcome=outcome,
            attempt_id=attempt_id,
            snapshot_id=snapshot_id,
            validation_id=validation_id,
            provider_called=provider_called,
            error_code=error_code,
        )

    # ---------------- 11E-3b-2：人工复核决定落地（ReviewCase 采用流程接入） ----------------

    def apply_review_decision(
        self,
        task_id: str,
        item_id: str,
        decision_id: str,
        adoption_request_id: str,
        *,
        competition_id: str,
    ) -> ReviewApplicationResult:
        """落地一次人工复核决定（adopt_existing_attempt -> 人工采用 + item review 阶段完成）。

        调用方只传 task/item/decision/adoption_request_id 与赛项 ID；不传 Provider/model/分数/
        证据正文/Key。决策与快照事实全部来自权威 store。

        流程：
        1. task/item 存在且 scoring_pipeline（EXECUTION_TASK_OR_ITEM_NOT_FOUND 语义复用）。
        2. decision 与对应 case 存在且绑定到该 item（REVIEW_DECISION_NOT_FOUND /
           REVIEW_CASE_NOT_FOUND / REVIEW_FACT_BINDING_MISMATCH）。
        3. 幂等短路：item.review_decision_id == decision_id -> already_completed（不重复采用）。
        4. 仅 adopt_existing_attempt 推进 item；其余决策类型只返回 not_adopted（item 不动）。
        5. 从权威 Snapshot/Attempt/Validation 构造人工 ResultAdoption（decided_by=reviewer，
           scope 的 competition_id 由调用方提供、submission/policy 来自快照）并 adopt_result
           （must_review 阻断/人工锁定保护由 store 保证，RESULT_ADOPTION_* 码传播）。
        6. 采用成功后推进 item：review/pending -> review/running -> export/pending，
           review_decision_id 落 item（review->export 完成引用）。
        - 中途失败不制造假完成：采用成功但 item 推进失败时 adoption 事实已落盘，
          重试（同 decision）幂等命中后继续推进，不重复采用。
        """
        now = self._clock()
        base = dict(
            adoption_request_id=adoption_request_id,
            task_id=task_id,
            item_id=item_id,
            decision_id=decision_id,
        )
        if self._review_store is None or self._operator is None or self._attempt_store is None:
            return ReviewApplicationResult(**base, outcome="internal_error",
                                           error_code=ERR_EXEC_INTERNAL)
        task = self._task_item.get_task(task_id)
        item = self._task_item.get_item(task_id, item_id)
        if task is None or item is None or item.task_id != task_id:
            return ReviewApplicationResult(**base, outcome="blocked",
                                           error_code=ERR_TASK_OR_ITEM_NOT_FOUND)
        if task.task_type != "scoring_pipeline" or item.task_type != "scoring_pipeline":
            return ReviewApplicationResult(**base, outcome="blocked",
                                           error_code=ERR_TASK_TYPE_NOT_SCORING)

        # 2. 决策与工单绑定
        decision = self._review_store.get_review_decision(task_id, item_id, decision_id)
        if decision is None:
            return ReviewApplicationResult(**base, outcome="blocked",
                                           error_code="REVIEW_DECISION_NOT_FOUND")
        case = self._review_store.get_review_case(task_id, item_id, decision.review_case_id)
        if case is None:
            return ReviewApplicationResult(**base, outcome="blocked",
                                           error_code="REVIEW_CASE_NOT_FOUND")
        if case.task_id != task_id or case.item_id != item_id:
            return ReviewApplicationResult(**base, outcome="blocked",
                                           error_code="REVIEW_FACT_BINDING_MISMATCH")

        # 3. 幂等短路：该决策已落地（item 已携带 review_decision_id）
        if item.review_decision_id == decision_id:
            existing = self._review_store.get_active_adoption(task_id, item_id)
            return ReviewApplicationResult(
                **base, outcome="already_completed",
                adoption_id=existing.adoption_id if existing else None,
                review_decision_id=decision_id,
            )

        # 4. 仅 adopt_existing_attempt 推进 item；其他决策类型仅登记事实
        if decision.decision_type != "adopt_existing_attempt":
            return ReviewApplicationResult(**base, outcome="not_adopted")

        # item 必须处于 review/pending（score 完成后未开始复核）
        if item.current_stage != "review" or item.status != "pending":
            return ReviewApplicationResult(**base, outcome="blocked",
                                           error_code=ERR_APP_NOT_APPLICABLE)

        # 5. 构造人工 ResultAdoption（权威事实来源；store 稳定码原样映射）
        from services.review_case_store import ReviewFactStoreError
        try:
            adopted = self._adopt_from_decision(task, task_id, item_id, decision, competition_id, now)
        except ReviewFactStoreError as exc:
            return ReviewApplicationResult(**base, outcome="blocked", error_code=exc.error_code)
        if adopted is None:
            return ReviewApplicationResult(**base, outcome="blocked",
                                           error_code="REVIEW_FACT_BINDING_MISMATCH")

        # 6. 推进 item review 阶段完成（review_decision_id 落 item；review -> export/pending）
        try:
            running_item = self._operator.start_item_stage(task_id, item_id, task.revision, item.item_revision)
            self._operator.complete_scoring_item_stage(
                task_id, item_id, task.revision + 1, running_item.item_revision,
                review_decision_id=decision_id,
            )
        except Exception:
            # 采用事实已落盘；item 推进失败不伪装成功（重试幂等命中后继续推进）
            return ReviewApplicationResult(
                **base, outcome="internal_error", adoption_id=adopted.adoption_id,
                review_decision_id=decision_id, error_code=ERR_APP_ITEM_ADVANCE_FAILED,
            )
        return ReviewApplicationResult(
            **base, outcome="adopted", adoption_id=adopted.adoption_id,
            review_decision_id=decision_id,
        )

    def _adopt_from_decision(self, task, task_id: str, item_id: str, decision, competition_id: str, now):
        """从权威 Snapshot/Attempt/Validation 构造人工 ResultAdoption 并采用。

        绑定不一致返回 None（上层映射为 REVIEW_FACT_BINDING_MISMATCH）；
        adopt_result 的稳定错误（RESULT_ADOPTION_BLOCKED/CONFLICT）原样传播。
        """
        if decision.target_snapshot_id is None or decision.target_attempt_id is None:
            return None
        snapshot = self._attempt_store.get_snapshot(task_id, item_id, decision.target_snapshot_id)
        attempt = self._attempt_store.get_attempt(task_id, item_id, decision.target_attempt_id)
        if snapshot is None or attempt is None:
            return None
        if snapshot.attempt_id != attempt.attempt_id:
            return None
        if attempt.validation_ref is None:
            return None
        validation = self._attempt_store.get_validation(task_id, item_id, attempt.validation_ref)
        if validation is None or validation.snapshot_id != snapshot.snapshot_id:
            return None
        # adoption_id 由 decision_id 稳定派生：同 decision 重试幂等（不产生双 active）
        adoption_id = f"adp-{decision.decision_id}"
        payload = dict(
            contract_version="score-attempt-review/v1",
            schema_version="result-adoption/v1",
            adoption_id=adoption_id,
            adoption_scope=AdoptionScope(
                competition_id=competition_id,
                batch_id=task.batch_id,  # 11F-1b：采用 scope 绑定任务批次（隔离维度）
                stream_id="default",  # 11F-1b：非支线批次
                submission_id=snapshot.submission_id,
                scoring_policy_version=snapshot.scoring_policy_version,
                purpose="machine_result",
            ),
            submission_id=snapshot.submission_id,
            attempt_id=attempt.attempt_id,
            snapshot_id=snapshot.snapshot_id,
            validation_id=validation.validation_id,
            review_case_id=decision.review_case_id,
            decision_id=decision.decision_id,
            status="adopted",
            reason_code="HUMAN_REVIEW_APPROVED",
            effective_at=now,
            decided_at=decision.decided_at,
            decided_by=decision.decided_by,
        )
        payload = {k: (v.model_dump(mode="json") if hasattr(v, "model_dump") else v)
                   for k, v in payload.items()}
        # 先构造（占位 hash）再用 pydantic JSON 载荷计算确定性哈希，保证与存储 canonical 一致
        adoption = ResultAdoption(**payload, adoption_hash="0" * 64)
        json_payload = adoption.model_dump(mode="json", exclude={"adoption_hash"})
        return self._review_store.adopt_result(
            task_id, item_id,
            adoption.model_copy(update={"adoption_hash": sha256_canonical(json_payload)}),
        )
