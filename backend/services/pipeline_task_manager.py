"""
PipelineTaskManager：任务创建、幂等与 import/validate 状态机（Phase 11C-2c）。

冻结契约：Phase11A-2b + Phase11C-1 + Phase11C-2c-EvidenceLevel路由补充决策。
所有写入通过 PipelineTaskStore 的事务/锁/revision 保护，不旁路直接写 JSON。

本阶段不实现：真实解析/评分、Provider 调用、租约抢占与 stale 恢复调度器、
后台线程、ReviewCase、API/前端、数据库持久化、pause 之后的 resume。
"""
from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from models.evidence_package import validate_relative_path, validate_sha256
from models.evidence_sidecar import EvidencePackageRecord, ValidationResult
from models.pipeline_task import (
    ItemEvidenceLevel,
    PipelineConfigurationSnapshot,
    PipelineError,
    PipelineEvent,
    PipelineEventType,
    PipelineItem,
    PipelineItemIndexEntry,
    PipelineStage,
    PipelineStageStatus,
    PipelineStageSummary,
    PipelineTask,
    ResumeDecision,
    ResumeEligibleItem,
    ResumeProtectedItem,
    ResumeRejectedItem,
    ResumeRequest,
    _SCOPE,
    _validate_ascii_id,  # 11E-3b-2：review_decision_id 契约衔接（11A-2d 稳定 ID）
    _validate_uuid,
    canonical_json_bytes,
    compute_item_input_fingerprint,
    compute_task_idempotency,
    sha256_canonical,
)
from services.evidence_registration_service import RegistrationError
from services.pipeline_task_store import (
    ERR_REVISION_CONFLICT,
    ERR_TASK_CORRUPTED,
    PipelineStoreError,
    PipelineTaskStore,
    run_task_transaction,
)

# ---------------------------------------------------------------- 错误码（manager 内最小常量，锁定于测试）

ERR_TASK_LIMIT_EXCEEDED = "SIDECAR_TASK_LIMIT_EXCEEDED"
ERR_PACKAGE_NOT_REGISTERED = "SIDECAR_PACKAGE_NOT_REGISTERED"
ERR_VALIDATION_NOT_ALLOWED = "SIDECAR_VALIDATION_NOT_ALLOWED"
ERR_PACKAGE_IDENTITY_CONFLICT = "SIDECAR_PACKAGE_IDENTITY_CONFLICT"
ERR_IDEMPOTENCY_CONFLICT = "SIDECAR_IDEMPOTENCY_CONFLICT"
ERR_INVALID_STATE_TRANSITION = "SIDECAR_INVALID_STATE_TRANSITION"
ERR_ATTEMPT_LIMIT_EXCEEDED = "SIDECAR_ATTEMPT_LIMIT_EXCEEDED"
ERR_SUCCESS_RESULT_PROTECTED = "SIDECAR_SUCCESS_RESULT_PROTECTED"
ERR_TASK_NOT_SETTLED = "SIDECAR_TASK_NOT_SETTLED"
ERR_EVIDENCE_LEVEL_MISSING = "SIDECAR_EVIDENCE_LEVEL_MISSING"
ERR_EVIDENCE_LEVEL_NOT_ALLOWED = "SIDECAR_EVIDENCE_LEVEL_NOT_ALLOWED"
ERR_SCOPE_VIOLATION = "SIDECAR_SCOPE_VIOLATION"  # limited 进 score / manual_only 进自动阶段
# 11E-2a-prerequisite-impl-3：scoring 状态机扩展
ERR_OUTCOME_UNKNOWN_BLOCKED = "SIDECAR_OUTCOME_UNKNOWN_BLOCKED"  # outcome_unknown 不得自动重试/推进

# 冻结原因码
REASON_LIMITED_EVIDENCE = "LIMITED_EVIDENCE"
REASON_MANUAL_ONLY_EVIDENCE = "MANUAL_ONLY_EVIDENCE"

_MAX_ITEMS = 1000
_MAX_CONCURRENCY = 16
_MAX_ATTEMPTS = 3
_TASK_TYPE = "evidence_preparation_pipeline"
_CONTRACT_VERSION = "pipeline-task/v1"


class PipelineTaskError(Exception):
    """manager 业务错误：携带稳定错误码（可断言，不裸 ValueError）。"""

    def __init__(self, error_code: str, message_key: str, retryable: bool = False) -> None:
        super().__init__(message_key)
        self.error_code = error_code
        self.message_key = message_key
        self.retryable = retryable


# ---------------------------------------------------------------- 创建输入（非敏感引用）


class PackageRef(BaseModel):
    """单个 package revision 的非敏感引用（不含材料正文/姓名/手机号/绝对路径）。"""

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
            raise ValueError("标识符必须为 ASCII 安全标识符（仅字母数字 _ - .）")
        return v

    @field_validator("manifest_sha256")
    @classmethod
    def _hash(cls, v: str) -> str:
        return validate_sha256(v)


class PipelineTaskCreateRequest(BaseModel):
    """任务创建输入：调用方不得传 task_id/item_id/idempotency_key/指纹/状态。"""

    model_config = ConfigDict(extra="forbid")

    batch_id: str
    package_refs: List[PackageRef] = Field(min_length=1, max_length=_MAX_ITEMS)
    concurrency: int = Field(ge=1, le=_MAX_CONCURRENCY)
    configuration_snapshot: PipelineConfigurationSnapshot

    @field_validator("batch_id")
    @classmethod
    def _batch_id(cls, v: str) -> str:
        import re

        if not re.match(r"^[A-Za-z0-9_\-\.]+$", v):
            raise ValueError("batch_id 必须为 ASCII 安全标识符")
        return v

    @model_validator(mode="after")
    def _no_duplicate_package_revision(self) -> "PipelineTaskCreateRequest":
        keys = [(r.package_id, r.package_revision) for r in self.package_refs]
        if len(keys) != len(set(keys)):
            raise ValueError("package_id + package_revision 不得重复")
        return self


# ---------------------------------------------------------------- 聚合（纯函数）


def _stage_status_and_counts(items: List[PipelineItem], stage: PipelineStage) -> Tuple[PipelineStageStatus, Dict[str, int]]:
    """从 item 当前状态与阶段结果重建 stage summary（自动聚合，禁止调用方传入）。"""
    at = [i for i in items if i.current_stage == stage]
    counts: Dict[str, int] = {s: 0 for s in ("pending", "running", "completed", "failed", "skipped", "manual_review")}
    for i in at:
        counts[i.status] += 1
    automated = [i for i in items if i.evidence_level != "manual_only"]

    def has(status: str) -> bool:
        return any(i.status == status for i in at)

    if has("running"):
        return "running", counts
    if has("pending"):
        return "pending", counts
    if has("failed"):
        return "failed", counts
    if has("skipped"):
        return "skipped", counts
    # 无活动项在该 stage
    if stage == "import":
        if not automated:
            return "not_started", counts  # 全部 manual_only，无自动 import
        moved_on = any(i.current_stage == "validate" or i.status in ("completed", "manual_review", "skipped") for i in automated)
        return "completed" if moved_on else "not_started", counts
    if stage == "validate":
        if not automated:
            return "not_started", counts
        passed_validate = any(i.status in ("completed", "manual_review", "skipped") for i in automated)
        return "completed" if passed_validate else "not_started", counts
    return "not_started", counts


def _aggregate_task(task: PipelineTask, items: List[PipelineItem], now: Any) -> PipelineTask:
    """从 item 聚合刷新 task 计数、item_index、stage_summaries、终态判定（纯计算）。

    11E-2a-prerequisite-impl-3：按 task.task_type 分派——
    - item_index 保留 task_type/source_item_id（不把 scoring item 聚合回 evidence item）；
    - stage_summaries / current_stage 按 evidence（import->validate）或 scoring（score->review->export）规则。
    """
    counts = Counter(i.status for i in items)
    field_map = {
        "pending": "pending_items",
        "running": "running_items",
        "completed": "completed_items",
        "failed": "failed_items",
        "skipped": "skipped_items",
        "manual_review": "manual_review_items",
    }
    updates: Dict[str, Any] = {
        field_map[s]: counts.get(s, 0) for s in field_map
    }
    updates["total_items"] = len(items)
    updates["item_index"] = [
        PipelineItemIndexEntry(
            item_id=i.item_id,
            task_type=i.task_type,
            source_item_id=i.source_item_id,
            package_id=i.package_id,
            package_revision=i.package_revision,
            status=i.status,
            current_stage=i.current_stage,
            input_fingerprint=i.input_fingerprint,
            item_revision=i.item_revision,
            evidence_level=i.evidence_level,
        )
        for i in sorted(items, key=lambda x: x.item_id)
    ]

    if task.task_type == "scoring_pipeline":
        updates["stage_summaries"] = _scoring_stage_summaries(task, items, now)
        updates["current_stage"] = _derive_scoring_current_stage(task, items)
    else:
        updates["stage_summaries"] = _evidence_stage_summaries(task, items, now)
        updates["current_stage"] = _derive_evidence_current_stage(task, items)
    return task.model_copy(update=updates)


_SCORING_ORDER = {"score": 0, "review": 1, "export": 2}
_ITEM_STATUS_KEYS = ("pending", "running", "completed", "failed", "skipped", "manual_review")


def _scoring_stage_status_and_counts(
    items: List[PipelineItem], stage: PipelineStage,
) -> Tuple[PipelineStageStatus, Dict[str, int]]:
    """scoring 阶段聚合：score/review/export 按该阶段 item 状态；无活动项时按是否已被越过判 completed。"""
    at = [i for i in items if i.current_stage == stage]
    counts: Dict[str, int] = {s: 0 for s in _ITEM_STATUS_KEYS}
    for i in at:
        counts[i.status] += 1

    def has(status: str) -> bool:
        return any(i.status == status for i in at)

    if has("running"):
        return "running", counts
    if has("pending"):
        return "pending", counts
    if has("failed"):
        return "failed", counts
    if has("skipped"):
        return "skipped", counts
    if has("manual_review"):
        # 人工复核项视为该阶段完成（与 evidence 语义一致；计数保留 manual_review_items）
        return "completed", counts
    # 无活动项：存在更后阶段的 item（该阶段已被越过）-> completed；否则 not_started
    idx = _SCORING_ORDER[stage]
    moved_on = any(_SCORING_ORDER.get(i.current_stage, 99) > idx for i in items)
    return ("completed" if moved_on else "not_started"), counts


def _scoring_stage_summaries(task: PipelineTask, items: List[PipelineItem], now: Any) -> List[PipelineStageSummary]:
    """scoring 五阶段摘要：import/validate 恒 not_started 计数零；score/review/export 聚合。"""
    old_by_stage = {s.stage: s for s in task.stage_summaries}
    out: List[PipelineStageSummary] = []
    for stage, deps in (("import", []), ("validate", ["import"])):
        old = old_by_stage.get(stage)
        out.append(PipelineStageSummary(
            stage=stage, status="not_started", depends_on=deps,
            started_at=None, completed_at=None, total_items=0,
            pending_items=0, running_items=0, completed_items=0, failed_items=0,
            skipped_items=0, manual_review_items=0,
            blocking_error_codes=old.blocking_error_codes if old else [], revision=1,
        ))
    deps = {"score": [], "review": ["score"], "export": ["review"]}
    for stage in ("score", "review", "export"):
        status, counts_stage = _scoring_stage_status_and_counts(items, stage)
        old = old_by_stage.get(stage)
        started_at = old.started_at if old else None
        completed_at = old.completed_at if old else None
        if status == "not_started":
            started_at = None
            completed_at = None
        elif status == "running":
            if started_at is None:
                started_at = now
            completed_at = None
        elif status == "pending" and completed_at is not None:
            completed_at = None
        elif status in ("completed", "failed", "skipped") and completed_at is None:
            completed_at = now
        rev = (old.revision + 1) if old and old.status != status else (old.revision if old else 1)
        out.append(PipelineStageSummary(
            stage=stage, status=status, depends_on=deps[stage],
            started_at=started_at, completed_at=completed_at,
            total_items=sum(counts_stage.values()),
            pending_items=counts_stage.get("pending", 0),
            running_items=counts_stage.get("running", 0),
            completed_items=counts_stage.get("completed", 0),
            failed_items=counts_stage.get("failed", 0),
            skipped_items=counts_stage.get("skipped", 0),
            manual_review_items=counts_stage.get("manual_review", 0),
            blocking_error_codes=old.blocking_error_codes if old else [],
            revision=rev,
        ))
    return out


def _derive_scoring_current_stage(task: PipelineTask, items: List[PipelineItem]) -> Optional[PipelineStage]:
    """scoring current_stage：终态 null；paused 保留；否则取尚有活动 item 的最早合法阶段。"""
    if task.status in ("completed", "completed_with_errors", "failed", "cancelled"):
        return None
    if task.status == "paused":
        return task.current_stage
    for stage in ("score", "review", "export"):
        if any(i.status in ("pending", "running", "failed") for i in items if i.current_stage == stage):
            return stage
    return task.current_stage


def _evidence_stage_summaries(task: PipelineTask, items: List[PipelineItem], now: Any) -> List[PipelineStageSummary]:
    """evidence 五阶段摘要（11C 原逻辑不变：import/validate 聚合；score/review/export 恒 not_started）。"""
    frozen_deps: Dict[PipelineStage, List[PipelineStage]] = {
        "import": [],
        "validate": ["import"],
        "score": ["validate"],
        "review": ["validate", "score"],
        "export": ["review"],
    }
    stage_summaries: List[PipelineStageSummary] = []
    old_by_stage = {s.stage: s for s in task.stage_summaries}
    for stage in ("import", "validate", "score", "review", "export"):
        if stage in ("score", "review", "export"):
            status: PipelineStageStatus = "not_started"
            counts_stage: Dict[str, int] = {s: 0 for s in _ITEM_STATUS_KEYS}
        else:
            status, counts_stage = _stage_status_and_counts(items, stage)
        old = old_by_stage.get(stage)
        started_at = old.started_at if old else None
        completed_at = old.completed_at if old else None
        if status == "not_started":
            started_at = None
            completed_at = None
        elif status == "running":
            if started_at is None:
                started_at = now
            completed_at = None  # running 阶段不得残留 completed_at
        elif status == "pending" and completed_at is not None:
            completed_at = None
        elif status in ("completed", "failed", "skipped") and completed_at is None:
            completed_at = now
        rev = (old.revision + 1) if old and old.status != status else (old.revision if old else 1)
        stage_summaries.append(PipelineStageSummary(
            stage=stage,
            status=status,
            depends_on=frozen_deps[stage],
            started_at=started_at,
            completed_at=completed_at,
            total_items=counts_stage.get("pending", 0) + counts_stage.get("running", 0)
            + counts_stage.get("completed", 0) + counts_stage.get("failed", 0)
            + counts_stage.get("skipped", 0) + counts_stage.get("manual_review", 0),
            pending_items=counts_stage.get("pending", 0),
            running_items=counts_stage.get("running", 0),
            completed_items=counts_stage.get("completed", 0),
            failed_items=counts_stage.get("failed", 0),
            skipped_items=counts_stage.get("skipped", 0),
            manual_review_items=counts_stage.get("manual_review", 0),
            blocking_error_codes=old.blocking_error_codes if old else [],
            revision=rev,
        ))
    return stage_summaries


def _derive_evidence_current_stage(task: PipelineTask, items: List[PipelineItem]) -> Optional[PipelineStage]:
    """evidence current_stage（11C 原逻辑不变）。"""
    if task.status in ("completed", "completed_with_errors", "failed", "cancelled"):
        return None
    if task.status == "paused":
        return task.current_stage  # paused 保留 current_stage（模型约束）
    if any(i.status in ("pending", "running", "failed") for i in items if i.current_stage == "import"):
        return "import"
    if any(i.status in ("pending", "running", "failed") for i in items if i.current_stage == "validate"):
        return "validate"
    if task.current_stage is not None:
        return task.current_stage
    return "import"


def _settled_status(counts: Counter) -> Optional[str]:
    """终态判定（仅任务级不可恢复结论；普通单项失败不得升级为任务 failed）。"""
    if counts["pending"] == 0 and counts["running"] == 0:
        if counts["failed"] == 0 and counts["manual_review"] == 0:
            return "completed"
        if counts["failed"] + counts["skipped"] + counts["manual_review"] > 0:
            return "completed_with_errors"
    return None


# ---------------------------------------------------------------- Manager


class PipelineTaskManager:
    """文件型任务管理器（独立，不继承/修改旧 TaskManager）。"""

    def __init__(
        self,
        store: PipelineTaskStore,
        registration_service: Any,
        clock: Callable[[], Any],
        uuid_factory: Callable[[], str],
    ) -> None:
        self.store = store
        self.reg = registration_service
        self.clock = clock
        self.uuid = uuid_factory

    # ---------------- 只读 ----------------

    def get_task(self, task_id: str) -> Optional[PipelineTask]:
        return self.store.load_task(task_id)

    def get_item(self, task_id: str, item_id: str) -> Optional[PipelineItem]:
        return self.store.load_item(task_id, item_id)

    def get_event_log(self, task_id: str) -> List[PipelineEvent]:
        return self.store.load_event_log(task_id)

    # ---------------- 查询（11C-2d） ----------------

    def list_tasks(self, status_filter: Optional[str] = None, offset: int = 0, limit: int = 50) -> List[PipelineTask]:
        """稳定排序 + 最小状态过滤 + 分页；查询零副作用；损坏显式 corrupted。"""
        return self.store.list_tasks(status_filter=status_filter, offset=offset, limit=limit)

    def list_items(self, task_id: str) -> List[PipelineItem]:
        return self.store.list_items(task_id)

    def get_resume_request(self, task_id: str, request_id: str) -> Optional[ResumeRequest]:
        return self.store.load_resume_request(task_id, request_id)

    def get_resume_decision(self, task_id: str, request_id: str) -> Optional[ResumeDecision]:
        return self.store.load_resume_decision(task_id, request_id)

    # ---------------- 心跳与 stale（11C-2d） ----------------

    def heartbeat_item(
        self, task_id: str, item_id: str, expected_task_revision: int, expected_item_revision: int,
    ) -> PipelineItem:
        """running item 更新心跳（续租）。事务保持 task/item/index/事件一致；revision 冲突零副作用。"""

        def trans(task: PipelineTask, item: PipelineItem, now: Any):
            if task.status != "running":
                raise PipelineTaskError(ERR_INVALID_STATE_TRANSITION, "SIDECAR_INVALID_STATE_TRANSITION")  # 暂停后不再续租
            if item.status != "running":
                raise PipelineTaskError(ERR_INVALID_STATE_TRANSITION, "SIDECAR_INVALID_STATE_TRANSITION")
            new_item = item.model_copy(update={
                "heartbeat_updated_at": now,
                "updated_at": now,
                "item_revision": item.item_revision + 1,
            })
            event = PipelineEvent(
                contract_version=_CONTRACT_VERSION, event_id=self.uuid(), task_id=task_id,
                sequence=task.last_event_sequence + 1, event_type="lease_acquired", occurred_at=now,
                stage=item.current_stage, item_id=item_id, attempt_id=None,
                revision_before=task.revision, revision_after=task.revision + 1,
                reason_code=None, metadata={},
            )
            new_task = task.model_copy(update={
                "updated_at": now, "revision": task.revision + 1,
                "last_event_sequence": task.last_event_sequence + 1,
            })
            return new_task, new_item, event

        return self._apply_item_transition(task_id, item_id, expected_task_revision, expected_item_revision, trans)

    def _item_fingerprint_matches(self, item: PipelineItem) -> bool:
        """输入指纹未变化：用冻结五字段重算并与 item 快照对比。"""
        fp = compute_item_input_fingerprint(
            package_id=item.package_id,
            package_revision=item.package_revision,
            manifest_sha256=item.manifest_sha256,
            registration_record_id=item.registration_record_id,
            validation_id=item.validation_id,
        )
        return fp == item.input_fingerprint

    def _is_stale(self, item: PipelineItem, now: Any) -> bool:
        """stale 判定（冻结：running 且 heartbeat_updated_at 距今严格超过 stale_after_seconds=300）。"""
        if item.status != "running" or item.heartbeat_updated_at is None:
            return False
        age = (now - item.heartbeat_updated_at).total_seconds()
        if age <= 300:
            return False  # 恰好等于阈值不判 stale（契约语义"超过"= 严格大于）
        return True

    def stale_candidates(self, task_id: str, now: Optional[Any] = None) -> List[PipelineItem]:
        """只读 stale 候选（不自动重跑）：running + 心跳超时 + 指纹未变化。"""
        task = self._load_task_required(task_id)
        now = now if now is not None else self.clock()
        items = [self._load_item_required(task_id, e.item_id) for e in task.item_index]
        return [i for i in items if self._is_stale(i, now) and self._item_fingerprint_matches(i)]

    # ---------------- 暂停恢复（11C-2d） ----------------

    def create_resume_request(self, task_id: str, request: ResumeRequest) -> bool:
        """写入恢复请求（原子；幂等命中/冲突）。"""
        if request.task_id != task_id:
            raise PipelineTaskError(ERR_INVALID_STATE_TRANSITION, "SIDECAR_TASK_ID_MISMATCH")
        try:
            return self.store.write_resume_request(task_id, request)
        except PipelineStoreError as exc:
            if exc.error_code == "SIDECAR_EVENT_IDEMPOTENCY_CONFLICT":
                raise PipelineTaskError(ERR_IDEMPOTENCY_CONFLICT, exc.message_key) from exc
            raise

    def evaluate_resume_request(self, task_id: str, request_id: str) -> ResumeDecision:
        """生成并持久化 ResumeDecision（11A-2b 11.5 第 6 步：decision 统一持久化；
        dry_run 标志区分只读判定，apply 时 dry_run 不改状态）。"""
        task = self._load_task_required(task_id)
        request = self.get_resume_request(task_id, request_id)
        if request is None:
            raise PipelineTaskError(ERR_TASK_NOT_SETTLED, "SIDECAR_REQUEST_NOT_FOUND")
        items = [self._load_item_required(task_id, e.item_id) for e in task.item_index]
        by_id = {i.item_id: i for i in items}
        target_ids = request.item_ids or [i.item_id for i in items]

        eligible: List[ResumeEligibleItem] = []
        protected: List[ResumeProtectedItem] = []
        rejected: List[ResumeRejectedItem] = []
        decision_errors: List[str] = []
        if request.expected_revision != task.revision:
            decision_errors.append("SIDECAR_REVISION_CONFLICT")

        for item_id in target_ids:
            item = by_id.get(item_id)
            if item is None:
                rejected.append(ResumeRejectedItem(item_id=item_id, error_codes=["SIDECAR_ITEM_NOT_FOUND"]))
                continue
            if request.expected_item_revisions.get(item_id) not in (None, item.item_revision):
                rejected.append(ResumeRejectedItem(item_id=item_id, error_codes=["SIDECAR_REVISION_CONFLICT"]))
                continue
            if item.status == "completed":
                protected.append(ResumeProtectedItem(item_id=item_id, reason_code="SUCCESS_RESULT_PROTECTED"))
            elif item.status == "manual_review":
                protected.append(ResumeProtectedItem(item_id=item_id, reason_code="MANUAL_REVIEW_PROTECTED"))
            elif item.status == "skipped":
                protected.append(ResumeProtectedItem(item_id=item_id, reason_code="SKIPPED_PROTECTED"))
            elif item.status == "running":
                if self._is_stale(item, request.requested_at) and request.mode == "recover_stale":
                    eligible.append(ResumeEligibleItem(
                        item_id=item_id, resume_from_stage=item.current_stage,
                        next_attempt_number=item.attempt_count + 1, reason_code="STALE_RUNNING_RECOVERY"))
                else:
                    protected.append(ResumeProtectedItem(item_id=item_id, reason_code="RESUME_NOT_ALLOWED"))
            elif item.status == "failed":
                # 11E-2a-prerequisite-impl-3-fix-1：outcome_unknown 不得通过 resume 绕过，
                # 必须保留到核对/恢复决策路径（不盲目重试 Provider）
                if item.last_error is not None and item.last_error.error_code == "PROVIDER_OUTCOME_UNKNOWN":
                    rejected.append(ResumeRejectedItem(
                        item_id=item_id, error_codes=["SIDECAR_OUTCOME_UNKNOWN_BLOCKED"]))
                elif item.retryable and item.attempt_count < _MAX_ATTEMPTS \
                        and self._item_fingerprint_matches(item) and request.mode == "resume_retryable_failed":
                    eligible.append(ResumeEligibleItem(
                        item_id=item_id, resume_from_stage=item.current_stage,
                        next_attempt_number=item.attempt_count + 1, reason_code="RETRYABLE_FAILED_RECOVERY"))
                else:
                    codes: List[str] = []
                    if not item.retryable:
                        codes.append("SIDECAR_NON_RETRYABLE")
                    if item.attempt_count >= _MAX_ATTEMPTS:
                        codes.append("SIDECAR_ATTEMPT_LIMIT_EXCEEDED")
                    if not self._item_fingerprint_matches(item):
                        codes.append("SIDECAR_FINGERPRINT_CHANGED")
                    if request.mode != "resume_retryable_failed":
                        codes.append("SIDECAR_MODE_MISMATCH")
                    rejected.append(ResumeRejectedItem(item_id=item_id, error_codes=codes or ["SIDECAR_INVALID_STATE_TRANSITION"]))
            elif item.status == "pending":
                if request.mode == "resume_pending" and self._item_fingerprint_matches(item):
                    eligible.append(ResumeEligibleItem(
                        item_id=item_id, resume_from_stage=item.current_stage,
                        next_attempt_number=item.attempt_count + 1, reason_code="PENDING_RESUME"))
                else:
                    rejected.append(ResumeRejectedItem(item_id=item_id, error_codes=["SIDECAR_MODE_MISMATCH"]))
            else:
                rejected.append(ResumeRejectedItem(item_id=item_id, error_codes=["SIDECAR_INVALID_STATE_TRANSITION"]))

        decision = ResumeDecision(
            contract_version=_CONTRACT_VERSION,
            decision_id=self.uuid(),
            request_id=request_id,
            task_id=task_id,
            decided_at=request.requested_at,
            approved=len(eligible) > 0,
            task_revision_before=task.revision,
            eligible_items=eligible,
            protected_items=protected,
            rejected_items=rejected,
            decision_error_codes=decision_errors,
            dry_run=request.dry_run,
        )
        self.store.write_resume_decision(task_id, decision)  # 统一持久化（dry_run 也持久化）
        return decision

    def apply_resume_decision(
        self, task_id: str, decision: ResumeDecision, expected_task_revision: int,
    ) -> PipelineTask:
        """应用恢复决定：dry_run 不改状态；approved 且校验通过才原子更新。
        failed/stale running 安全回到 pending（不增加 attempt）；pending 保持 pending；
        不自动调用 start_item_stage；任务 paused -> running 保留首次 started_at。"""
        if decision.task_id != task_id:
            raise PipelineTaskError(ERR_INVALID_STATE_TRANSITION, "SIDECAR_TASK_ID_MISMATCH")
        if decision.dry_run:
            return self._load_task_required(task_id)  # dry-run：零状态副作用
        if not decision.approved:
            raise PipelineTaskError(ERR_TASK_NOT_SETTLED, "SIDECAR_DECISION_NOT_APPROVED")
        request = self.get_resume_request(task_id, decision.request_id)
        if request is None:
            raise PipelineTaskError(ERR_TASK_NOT_SETTLED, "SIDECAR_REQUEST_NOT_FOUND")
        task = self._load_task_required(task_id)
        if task.revision != decision.task_revision_before:
            raise PipelineTaskError(ERR_REVISION_CONFLICT, "SIDECAR_REVISION_CONFLICT")
        if expected_task_revision != task.revision:
            # apply 前任务 revision 再次变化：拒绝（调用方期望与当前不符）
            raise PipelineTaskError(ERR_REVISION_CONFLICT, "SIDECAR_REVISION_CONFLICT")
        items = [self._load_item_required(task_id, e.item_id) for e in task.item_index]
        by_id = {i.item_id: i for i in items}
        now = self.clock()

        changes: List[PipelineItem] = []
        for e in decision.eligible_items:
            item = by_id[e.item_id]
            # 再次校验：revision 与指纹
            if request.expected_item_revisions.get(e.item_id) not in (None, item.item_revision):
                raise PipelineTaskError(ERR_REVISION_CONFLICT, "SIDECAR_REVISION_CONFLICT")
            if not self._item_fingerprint_matches(item):
                raise PipelineTaskError(ERR_INVALID_STATE_TRANSITION, "SIDECAR_FINGERPRINT_CHANGED")
            if item.status == "pending":
                continue  # pending 保持 pending，不增加 attempt
            if item.status in ("failed", "running"):
                # failed(approved retryable) / stale running -> 安全回到 pending
                changes.append(item.model_copy(update={
                    "status": "pending",
                    "retryable": False,
                    "heartbeat_updated_at": None,
                    "updated_at": now,
                    "item_revision": item.item_revision + 1,
                }))
            else:
                raise PipelineTaskError(ERR_INVALID_STATE_TRANSITION, "SIDECAR_INVALID_STATE_TRANSITION")

        task_changes: Dict[str, Any] = {
            "updated_at": now,
            "revision": task.revision + 1,
            "last_event_sequence": task.last_event_sequence + 1,
        }
        event_type: PipelineEventType = "resume_decided"
        if task.status == "paused":
            task_changes["status"] = "running"
            task_changes["paused_at"] = None  # 保留首次 started_at
            event_type = "task_resumed"
        new_task = task.model_copy(update=task_changes)
        events: List[PipelineEvent] = []
        if changes or task.status == "paused":
            events.append(PipelineEvent(
                contract_version=_CONTRACT_VERSION, event_id=self.uuid(), task_id=task_id,
                sequence=new_task.last_event_sequence, event_type=event_type, occurred_at=now,
                stage=task.current_stage, item_id=None, attempt_id=None,
                revision_before=task.revision, revision_after=new_task.revision,
                reason_code=None, metadata={},
            ))
        if not events:
            return new_task  # 无状态变化（全 pending 且任务非 paused）：返回当前快照
        # 单事件事务：聚合后提交
        all_items = [by_id[e.item_id] for e in task.item_index]
        for c in changes:
            all_items = [c if i.item_id == c.item_id else i for i in all_items]
        new_task = _aggregate_task(new_task, all_items, now)
        run_task_transaction(
            self.store, task_id, events[0],
            new_task=new_task, new_items=changes,
            expected_task_revision=expected_task_revision,
            expected_item_revisions={c.item_id: by_id[c.item_id].item_revision for c in changes},
        )
        return new_task

    # ---------------- 创建 ----------------

    def _preflight(self, ref: PackageRef) -> ValidationResult:
        """单一 package 预检：登记/验证/身份/门/evidence_level 全部通过才返回 validation。"""
        record: Optional[EvidencePackageRecord]
        try:
            record = self.reg.read_record_strict(ref.package_id, ref.package_revision)
        except RegistrationError as exc:
            raise PipelineTaskError(ERR_PACKAGE_NOT_REGISTERED, exc.message_key or "SIDECAR_PACKAGE_NOT_REGISTERED") from exc
        if record is None:
            raise PipelineTaskError(ERR_PACKAGE_NOT_REGISTERED, "SIDECAR_PACKAGE_NOT_REGISTERED")
        try:
            validation = self.reg.read_validation_strict(ref.package_id, ref.package_revision, ref.validation_id)
        except RegistrationError as exc:
            raise PipelineTaskError(ERR_VALIDATION_NOT_ALLOWED, exc.message_key or "SIDECAR_VALIDATION_NOT_ALLOWED") from exc
        if validation is None:
            raise PipelineTaskError(ERR_VALIDATION_NOT_ALLOWED, "SIDECAR_VALIDATION_NOT_ALLOWED")

        # 身份一致性（record/validation/请求）
        identity = (
            (ref.package_id, record.package_id, validation.package_id),
            (ref.package_revision, record.package_revision, validation.package_revision),
            (ref.manifest_sha256, record.manifest_sha256, validation.manifest_sha256),
            (ref.registration_record_id, record.record_id, validation.record_id),
            (ref.validation_id, validation.validation_id, record.latest_validation_id),
        )
        for a, b, c in identity:
            if a != b or b != c:
                raise PipelineTaskError(ERR_PACKAGE_IDENTITY_CONFLICT, "SIDECAR_PACKAGE_IDENTITY_CONFLICT")

        if record.registration_status != "registered":
            raise PipelineTaskError(ERR_VALIDATION_NOT_ALLOWED, "SIDECAR_VALIDATION_NOT_ALLOWED")
        if not validation.registration_allowed or not validation.model_input_allowed:
            raise PipelineTaskError(ERR_VALIDATION_NOT_ALLOWED, "SIDECAR_VALIDATION_NOT_ALLOWED")
        if validation.evidence_level is None:
            raise PipelineTaskError(ERR_EVIDENCE_LEVEL_MISSING, "SIDECAR_EVIDENCE_LEVEL_MISSING")
        if validation.evidence_level not in ("sufficient", "limited", "manual_only"):
            raise PipelineTaskError(ERR_EVIDENCE_LEVEL_NOT_ALLOWED, "SIDECAR_EVIDENCE_LEVEL_NOT_ALLOWED")
        return validation

    def _find_existing_by_idempotency(self, idempotency_key: str, payload_sha256: str) -> Optional[PipelineTask]:
        """只读任务扫描：同 idempotency_key 且同 payload 返回原任务；同 key 不同 payload 显式冲突。
        损坏任务文件必须显式 corrupted，不得跳过后继续查找。"""
        tasks_dir = self.store.tasks_dir
        if not tasks_dir.exists():
            return None
        for entry in sorted(tasks_dir.iterdir()):
            if not entry.is_dir():
                continue
            task_path = entry / "task.json"
            if not task_path.exists():
                continue
            task = self.store.load_task(entry.name)
            if task is None:
                continue  # load_task 对损坏抛 corrupted；不存在返回 None（此处文件存在则应可解析）
            if task.idempotency_key != idempotency_key:
                continue
            if task.idempotency_payload_sha256 == payload_sha256:
                return task
            raise PipelineTaskError(ERR_IDEMPOTENCY_CONFLICT, "SIDECAR_IDEMPOTENCY_CONFLICT")
        return None

    def create_task(self, request: PipelineTaskCreateRequest) -> Tuple[PipelineTask, bool]:
        """创建任务：预检全部 item -> 幂等扫描 -> 一次事务提交 task+items+task_created。"""
        if len(request.package_refs) > _MAX_ITEMS:
            raise PipelineTaskError(ERR_TASK_LIMIT_EXCEEDED, "SIDECAR_TASK_LIMIT_EXCEEDED")
        now = self.clock()
        task_id = self.uuid()

        # 预检全部（任一失败整批不落盘）
        validations: List[Tuple[PackageRef, ValidationResult]] = [
            (ref, self._preflight(ref)) for ref in request.package_refs
        ]
        # 幂等预检：指纹/幂等键不依赖 evidence_level（冻结 5 字段 + 配置）
        configuration_fingerprint = sha256_canonical(request.configuration_snapshot.model_dump(mode="json"))
        item_fps = [
            compute_item_input_fingerprint(
                package_id=ref.package_id,
                package_revision=ref.package_revision,
                manifest_sha256=ref.manifest_sha256,
                registration_record_id=ref.registration_record_id,
                validation_id=ref.validation_id,
            )
            for ref in request.package_refs
        ]
        items_sorted = sorted(
            [(ref, fp, validation) for (ref, validation), fp in zip(validations, item_fps)],
            key=lambda t: (t[0].package_id, t[0].package_revision),
        )
        idempotency_key = compute_task_idempotency(
            contract_version=_CONTRACT_VERSION,
            task_type=_TASK_TYPE,
            batch_id=request.batch_id,
            execution_scope=list(_SCOPE),
            items=[(ref.package_id, ref.package_revision, fp) for ref, fp, _ in items_sorted],
            configuration_fingerprint=configuration_fingerprint,
        )
        payload = {
            "contract_version": _CONTRACT_VERSION,
            "task_type": _TASK_TYPE,
            "batch_id": request.batch_id,
            "execution_scope": list(_SCOPE),
            "ordered_input_fingerprints": [fp for _, fp, _ in items_sorted],
            "configuration_fingerprint": configuration_fingerprint,
        }
        payload_sha256 = sha256_canonical(payload)
        existing = self._find_existing_by_idempotency(idempotency_key, payload_sha256)
        if existing is not None:
            return existing, True  # 幂等命中：返回原任务，不创建新目录、不追加事件

        # 构造 items（manual_only 直接 manual_review 并保留标准原因码；其余 pending）
        items: List[PipelineItem] = []
        for ref, fp, validation in items_sorted:
            level: ItemEvidenceLevel = validation.evidence_level  # type: ignore[assignment]
            status = "manual_review" if level == "manual_only" else "pending"
            last_error = None
            if level == "manual_only":
                last_error = PipelineError(
                    error_code=REASON_MANUAL_ONLY_EVIDENCE,
                    message_key=REASON_MANUAL_ONLY_EVIDENCE,
                    retryable=False,
                    stage="import",
                )
            items.append(PipelineItem(
                contract_version="pipeline-item/v1",
                item_id=self.uuid(),
                task_id=task_id,
                package_id=ref.package_id,
                package_revision=ref.package_revision,
                manifest_sha256=ref.manifest_sha256,
                registration_record_id=ref.registration_record_id,
                validation_id=ref.validation_id,
                input_fingerprint=fp,
                idempotency_key=sha256_canonical({"task": idempotency_key, "package_id": ref.package_id,
                                                  "package_revision": ref.package_revision}),
                status=status,  # type: ignore[arg-type]
                current_stage="import",
                attempt_count=0,
                max_attempts=_MAX_ATTEMPTS,
                heartbeat_interval_seconds=60,
                stale_after_seconds=300,
                heartbeat_updated_at=None,
                created_at=now,
                updated_at=now,
                retryable=False,
                last_error=last_error,
                output_ref=None,
                output_sha256=None,
                item_revision=1,
                evidence_level=level,
            ))
        task = PipelineTask(
            contract_version=_CONTRACT_VERSION,
            task_type=_TASK_TYPE,
            execution_scope=list(_SCOPE),
            task_id=task_id,
            batch_id=request.batch_id,
            status="pending",
            current_stage="import",
            created_at=now,
            started_at=None,
            updated_at=now,
            paused_at=None,
            completed_at=None,
            total_items=len(items),
            pending_items=sum(1 for i in items if i.status == "pending"),
            running_items=0,
            completed_items=0,
            failed_items=0,
            skipped_items=0,
            manual_review_items=sum(1 for i in items if i.status == "manual_review"),
            concurrency=request.concurrency,
            configuration_snapshot=request.configuration_snapshot,
            configuration_fingerprint=configuration_fingerprint,
            item_index=[PipelineItemIndexEntry(
                item_id=i.item_id, package_id=i.package_id, package_revision=i.package_revision,
                status=i.status, current_stage=i.current_stage, input_fingerprint=i.input_fingerprint,
                item_revision=i.item_revision, evidence_level=i.evidence_level,
            ) for i in items],
            stage_summaries=self._initial_stage_summaries(items, now),
            error_summary={"count": 0, "codes": []},
            last_event_sequence=1,
            revision=1,
            idempotency_key=idempotency_key,
            idempotency_payload_sha256=payload_sha256,
        )
        event = PipelineEvent(
            contract_version=_CONTRACT_VERSION,
            event_id=self.uuid(),
            task_id=task_id,
            sequence=1,
            event_type="task_created",
            occurred_at=now,
            stage=None,
            item_id=None,
            attempt_id=None,
            revision_before=0,
            revision_after=1,
            reason_code=None,
            metadata={},
        )
        try:
            run_task_transaction(
                self.store, task_id, event,
                new_task=task, new_items=items,
                expected_task_revision=None, expected_item_revisions={},
            )
        except PipelineStoreError as exc:
            if exc.error_code in (ERR_REVISION_CONFLICT, ERR_TASK_CORRUPTED):
                raise PipelineTaskError(exc.error_code, exc.message_key) from exc
            raise
        return task, False

    def _initial_stage_summaries(self, items: List[PipelineItem], now: Any) -> List[PipelineStageSummary]:
        counts = Counter(i.status for i in items)
        automated = [i for i in items if i.evidence_level != "manual_only"]
        import_status: PipelineStageStatus = "pending" if any(i.status == "pending" for i in automated) else "not_started"
        manual = sum(1 for i in items if i.status == "manual_review")
        pending = sum(1 for i in items if i.status == "pending")
        return [
            PipelineStageSummary(stage="import", status=import_status, depends_on=[],
                                 started_at=now if import_status == "pending" else None, completed_at=None,
                                 total_items=pending, pending_items=pending, running_items=0,
                                 completed_items=0, failed_items=0, skipped_items=0,
                                 manual_review_items=0, blocking_error_codes=[], revision=1),
            PipelineStageSummary(stage="validate", status="not_started", depends_on=["import"],
                                 started_at=None, completed_at=None,
                                 total_items=0, pending_items=0, running_items=0, completed_items=0,
                                 failed_items=0, skipped_items=0, manual_review_items=0,
                                 blocking_error_codes=[], revision=1),
            PipelineStageSummary(stage="score", status="not_started", depends_on=["validate"],
                                 started_at=None, completed_at=None,
                                 total_items=0, pending_items=0, running_items=0, completed_items=0,
                                 failed_items=0, skipped_items=0, manual_review_items=0,
                                 blocking_error_codes=[], revision=1),
            PipelineStageSummary(stage="review", status="not_started", depends_on=["validate", "score"],
                                 started_at=None, completed_at=None,
                                 total_items=0, pending_items=0, running_items=0, completed_items=0,
                                 failed_items=0, skipped_items=0, manual_review_items=0,
                                 blocking_error_codes=[], revision=1),
            PipelineStageSummary(stage="export", status="not_started", depends_on=["review"],
                                 started_at=None, completed_at=None,
                                 total_items=0, pending_items=0, running_items=0, completed_items=0,
                                 failed_items=0, skipped_items=0, manual_review_items=0,
                                 blocking_error_codes=[], revision=1),
        ]

    # ---------------- 任务转换 ----------------

    def start_task(self, task_id: str, expected_revision: int) -> PipelineTask:
        task = self._load_task_required(task_id)
        if task.status != "pending":
            raise PipelineTaskError(ERR_INVALID_STATE_TRANSITION, "SIDECAR_INVALID_STATE_TRANSITION")
        now = self.clock()
        items = [self._load_item_required(task_id, e.item_id) for e in task.item_index]
        new_task = task.model_copy(update={
            "status": "running",
            "started_at": now,
            "updated_at": now,
            "revision": task.revision + 1,
            "last_event_sequence": task.last_event_sequence + 1,
        })
        new_task = _aggregate_task(new_task, items, now)
        # 11E-2a-prerequisite-impl-3：事件 stage 按 task 类型（scoring 初始阶段为 score）
        event_stage: Optional[PipelineStage] = "score" if task.task_type == "scoring_pipeline" else "import"
        event = PipelineEvent(
            contract_version=_CONTRACT_VERSION, event_id=self.uuid(), task_id=task_id,
            sequence=new_task.last_event_sequence, event_type="task_started", occurred_at=now,
            stage=event_stage, item_id=None, attempt_id=None,
            revision_before=task.revision, revision_after=new_task.revision,
            reason_code=None, metadata={},
        )
        run_task_transaction(self.store, task_id, event, new_task=new_task,
                             expected_task_revision=expected_revision, expected_item_revisions={})
        return new_task

    def pause_task(self, task_id: str, expected_revision: int) -> PipelineTask:
        task = self._load_task_required(task_id)
        if task.status != "running":
            raise PipelineTaskError(ERR_INVALID_STATE_TRANSITION, "SIDECAR_INVALID_STATE_TRANSITION")
        now = self.clock()
        items = [self._load_item_required(task_id, e.item_id) for e in task.item_index]
        new_task = task.model_copy(update={
            "status": "paused",
            "paused_at": now,
            "updated_at": now,
            "revision": task.revision + 1,
            "last_event_sequence": task.last_event_sequence + 1,
        })
        new_task = _aggregate_task(new_task, items, now)
        event = PipelineEvent(
            contract_version=_CONTRACT_VERSION, event_id=self.uuid(), task_id=task_id,
            sequence=new_task.last_event_sequence, event_type="task_paused", occurred_at=now,
            stage=task.current_stage, item_id=None, attempt_id=None,
            revision_before=task.revision, revision_after=new_task.revision,
            reason_code=None, metadata={},
        )
        run_task_transaction(self.store, task_id, event, new_task=new_task,
                             expected_task_revision=expected_revision, expected_item_revisions={})
        return new_task

    def finalize_task_if_settled(self, task_id: str, expected_revision: int) -> PipelineTask:
        task = self._load_task_required(task_id)
        items = [self._load_item_required(task_id, e.item_id) for e in task.item_index]
        counts = Counter(i.status for i in items)
        terminal = _settled_status(counts)
        if terminal is None:
            raise PipelineTaskError(ERR_TASK_NOT_SETTLED, "SIDECAR_TASK_NOT_SETTLED")
        now = self.clock()
        new_task = task.model_copy(update={
            "status": terminal,
            "completed_at": now,
            "updated_at": now,
            "revision": task.revision + 1,
            "last_event_sequence": task.last_event_sequence + 1,
            "current_stage": None,
        })
        new_task = _aggregate_task(new_task, items, now)
        event = PipelineEvent(
            contract_version=_CONTRACT_VERSION, event_id=self.uuid(), task_id=task_id,
            sequence=new_task.last_event_sequence,
            event_type="task_completed" if terminal == "completed" else "task_failed",
            occurred_at=now, stage=None, item_id=None, attempt_id=None,
            revision_before=task.revision, revision_after=new_task.revision,
            reason_code=None, metadata={},
        )
        run_task_transaction(self.store, task_id, event, new_task=new_task,
                             expected_task_revision=expected_revision, expected_item_revisions={})
        return new_task

    # ---------------- item 转换 ----------------

    def _load_task_required(self, task_id: str) -> PipelineTask:
        task = self.store.load_task(task_id)
        if task is None:
            raise PipelineTaskError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_NOT_FOUND")
        return task

    def _load_item_required(self, task_id: str, item_id: str) -> PipelineItem:
        item = self.store.load_item(task_id, item_id)
        if item is None:
            raise PipelineTaskError(ERR_TASK_CORRUPTED, "SIDECAR_ITEM_NOT_FOUND")
        return item

    def _apply_item_transition(
        self,
        task_id: str,
        item_id: str,
        expected_task_revision: int,
        expected_item_revision: int,
        transition: Callable[[PipelineTask, PipelineItem, Any], Tuple[PipelineTask, PipelineItem, PipelineEvent]],
    ) -> PipelineItem:
        """锁内执行单 item 转换：校验/构造新 task+item+事件 -> store 联合事务提交。"""
        task = self._load_task_required(task_id)
        if task.status not in ("pending", "running", "paused"):
            raise PipelineTaskError(ERR_INVALID_STATE_TRANSITION, "SIDECAR_INVALID_STATE_TRANSITION")
        item = self._load_item_required(task_id, item_id)
        if item.item_revision != expected_item_revision:
            raise PipelineTaskError(ERR_REVISION_CONFLICT, "SIDECAR_REVISION_CONFLICT")
        now = self.clock()
        new_task, new_item, event = transition(task, item, now)
        all_items = [self._load_item_required(task_id, e.item_id) for e in task.item_index]
        all_items = [new_item if i.item_id == item_id else i for i in all_items]
        new_task = _aggregate_task(new_task, all_items, now)
        run_task_transaction(
            self.store, task_id, event,
            new_task=new_task, new_items=[new_item],
            expected_task_revision=expected_task_revision,
            expected_item_revisions={item_id: expected_item_revision},
        )
        return new_item

    def start_item_stage(
        self, task_id: str, item_id: str, expected_task_revision: int, expected_item_revision: int,
    ) -> PipelineItem:
        def trans(task: PipelineTask, item: PipelineItem, now: Any):
            if item.evidence_level == "manual_only":
                raise PipelineTaskError(ERR_SCOPE_VIOLATION, "SIDECAR_SCOPE_VIOLATION")
            if task.task_type == "scoring_pipeline":
                # 11E-2a-prerequisite-impl-3：scoring item 启动要求 task running；
                # outcome_unknown 不得自动重试/新建覆盖性 attempt（走恢复/核对决策）
                if task.status != "running":
                    raise PipelineTaskError(ERR_INVALID_STATE_TRANSITION, "SIDECAR_INVALID_STATE_TRANSITION")
                if item.last_error is not None and item.last_error.error_code == "PROVIDER_OUTCOME_UNKNOWN":
                    raise PipelineTaskError(ERR_OUTCOME_UNKNOWN_BLOCKED, "SIDECAR_OUTCOME_UNKNOWN_BLOCKED")
            if item.status not in ("pending", "failed"):
                raise PipelineTaskError(ERR_INVALID_STATE_TRANSITION, "SIDECAR_INVALID_STATE_TRANSITION")
            if item.status == "failed" and not item.retryable:
                raise PipelineTaskError(ERR_INVALID_STATE_TRANSITION, "SIDECAR_INVALID_STATE_TRANSITION")
            if item.attempt_count + 1 > _MAX_ATTEMPTS:
                raise PipelineTaskError(ERR_ATTEMPT_LIMIT_EXCEEDED, "SIDECAR_ATTEMPT_LIMIT_EXCEEDED")
            new_item = item.model_copy(update={
                "status": "running",
                "attempt_count": item.attempt_count + 1,
                "heartbeat_updated_at": now,
                "retryable": False,
                "last_error": None,
                "updated_at": now,
                "item_revision": item.item_revision + 1,
            })
            event = PipelineEvent(
                contract_version=_CONTRACT_VERSION, event_id=self.uuid(), task_id=task_id,
                sequence=task.last_event_sequence + 1, event_type="item_started", occurred_at=now,
                stage=new_item.current_stage, item_id=item_id, attempt_id=None,
                revision_before=task.revision, revision_after=task.revision + 1,
                reason_code=None, metadata={},
            )
            new_task = task.model_copy(update={
                "updated_at": now, "revision": task.revision + 1,
                "last_event_sequence": task.last_event_sequence + 1,
            })
            return new_task, new_item, event

        return self._apply_item_transition(task_id, item_id, expected_task_revision, expected_item_revision, trans)

    def complete_item_stage(
        self, task_id: str, item_id: str, expected_task_revision: int, expected_item_revision: int,
        output_ref: Optional[str] = None, output_sha256: Optional[str] = None,
    ) -> PipelineItem:
        def trans(task: PipelineTask, item: PipelineItem, now: Any):
            if item.evidence_level == "manual_only":
                raise PipelineTaskError(ERR_SCOPE_VIOLATION, "SIDECAR_SCOPE_VIOLATION")
            if item.status == "completed":
                # 幂等完成：相同输出 -> 返回原状态；不同输出 -> 保护冲突
                if output_ref == item.output_ref and output_sha256 == item.output_sha256:
                    raise _IdempotentComplete()
                raise PipelineTaskError(ERR_SUCCESS_RESULT_PROTECTED, "SIDECAR_SUCCESS_RESULT_PROTECTED")
            if item.status != "running":
                raise PipelineTaskError(ERR_INVALID_STATE_TRANSITION, "SIDECAR_INVALID_STATE_TRANSITION")
            if output_ref is not None:
                if output_sha256 is None:
                    raise PipelineTaskError(ERR_INVALID_STATE_TRANSITION, "SIDECAR_INVALID_STATE_TRANSITION")
                validate_relative_path(output_ref)
                validate_sha256(output_sha256)
            updates: Dict[str, Any] = {
                "updated_at": now,
                "item_revision": item.item_revision + 1,
                "heartbeat_updated_at": None,  # 离开 running 清空
            }
            if item.current_stage == "import":
                updates["current_stage"] = "validate"
                updates["status"] = "pending"  # 显式进入 validate（等待 start_item_stage）
                event_type: PipelineEventType = "item_completed"
            elif item.current_stage == "validate":
                if item.evidence_level == "limited":
                    updates["status"] = "manual_review"
                    updates["last_error"] = PipelineError(
                        error_code="LIMITED_EVIDENCE", message_key="LIMITED_EVIDENCE",
                        retryable=False, stage="validate",
                    )
                    event_type = "item_sent_to_manual_review"
                else:
                    updates["status"] = "completed"
                    updates["output_ref"] = output_ref
                    updates["output_sha256"] = output_sha256
                    event_type = "item_completed"
            else:
                raise PipelineTaskError(ERR_SCOPE_VIOLATION, "SIDECAR_SCOPE_VIOLATION")
            new_item = item.model_copy(update=updates)
            event = PipelineEvent(
                contract_version=_CONTRACT_VERSION, event_id=self.uuid(), task_id=task_id,
                sequence=task.last_event_sequence + 1, event_type=event_type, occurred_at=now,
                stage=new_item.current_stage, item_id=item_id, attempt_id=None,
                revision_before=task.revision, revision_after=task.revision + 1,
                reason_code=None, metadata={},
            )
            new_task = task.model_copy(update={
                "updated_at": now, "revision": task.revision + 1,
                "last_event_sequence": task.last_event_sequence + 1,
            })
            return new_task, new_item, event

        try:
            return self._apply_item_transition(task_id, item_id, expected_task_revision, expected_item_revision, trans)
        except _IdempotentComplete:
            return self._load_item_required(task_id, item_id)  # 幂等命中：返回原状态

    def fail_item_retryable(
        self, task_id: str, item_id: str, expected_task_revision: int, expected_item_revision: int,
        error_code: str,
    ) -> PipelineItem:
        return self._fail_item(task_id, item_id, expected_task_revision, expected_item_revision,
                               error_code, retryable=True)

    def fail_item_non_retryable(
        self, task_id: str, item_id: str, expected_task_revision: int, expected_item_revision: int,
        error_code: str,
    ) -> PipelineItem:
        return self._fail_item(task_id, item_id, expected_task_revision, expected_item_revision,
                               error_code, retryable=False)

    # ---------------- scoring 阶段完成（11E-2a-prerequisite-impl-3） ----------------

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
        """scoring item 阶段完成入口（不伪造 Provider 结果，只记录非敏感引用）。

        - score -> review：必填 attempt_id（仅记录引用，不创建 ScoreAttempt）。
        - review -> export：必填 review_decision_id（11A-2d 非敏感引用，评语正文不入库）。
        - export -> completed：必填 output_ref/output_sha256（安全相对路径 + 合法哈希）。
        幂等：重复提交相同引用返回原状态；不同引用触发成功结果保护冲突。
        """

        def trans(task: PipelineTask, item: PipelineItem, now: Any):
            if item.task_type != "scoring_pipeline":
                raise PipelineTaskError(ERR_SCOPE_VIOLATION, "SIDECAR_SCOPE_VIOLATION")
            stage = item.current_stage
            if item.status == "completed":
                # export 完成后的成功结果保护：相同输出幂等，不同输出保护
                if output_ref == item.output_ref and output_sha256 == item.output_sha256:
                    raise _IdempotentComplete()
                raise PipelineTaskError(ERR_SUCCESS_RESULT_PROTECTED, "SIDECAR_SUCCESS_RESULT_PROTECTED")
            if item.status != "running":
                # 已推进后的重复提交：相同引用幂等，不同引用成功保护冲突
                if stage == "review" and attempt_id is not None:
                    last = self._last_scoring_complete_ref(task_id, item_id, "score")
                    if last == attempt_id:
                        raise _IdempotentComplete()
                    if last is not None:
                        raise PipelineTaskError(ERR_SUCCESS_RESULT_PROTECTED, "SIDECAR_SUCCESS_RESULT_PROTECTED")
                if stage == "export" and review_decision_id is not None:
                    if item.review_decision_id == review_decision_id:
                        raise _IdempotentComplete()
                    if item.review_decision_id is not None:
                        raise PipelineTaskError(ERR_SUCCESS_RESULT_PROTECTED, "SIDECAR_SUCCESS_RESULT_PROTECTED")
                raise PipelineTaskError(ERR_INVALID_STATE_TRANSITION, "SIDECAR_INVALID_STATE_TRANSITION")

            updates: Dict[str, Any] = {
                "updated_at": now,
                "item_revision": item.item_revision + 1,
                "heartbeat_updated_at": None,  # 离开 running 清空
            }
            event_attempt: Optional[str] = None
            event_type: PipelineEventType = "item_completed"
            if stage == "score":
                if attempt_id is None:
                    raise PipelineTaskError(ERR_INVALID_STATE_TRANSITION, "SIDECAR_ATTEMPT_REQUIRED")
                _validate_uuid(attempt_id)
                updates["current_stage"] = "review"
                updates["status"] = "pending"
                event_attempt = attempt_id
            elif stage == "review":
                if review_decision_id is None:
                    raise PipelineTaskError(ERR_INVALID_STATE_TRANSITION, "SIDECAR_REVIEW_DECISION_REQUIRED")
                # 11E-3b-2：契约衔接修复——11A-2d 建议 decision_id 为稳定 ID（如 dec-xxx），
                # 与 _validate_uuid 冲突；放宽为 ASCII 安全 ID（UUID 是其子集，拒绝中文/URL/路径/自由文本）
                _validate_ascii_id(review_decision_id)
                updates["current_stage"] = "export"
                updates["status"] = "pending"
                updates["review_decision_id"] = review_decision_id
            elif stage == "export":
                if output_ref is None or output_sha256 is None:
                    raise PipelineTaskError(ERR_INVALID_STATE_TRANSITION, "SIDECAR_OUTPUT_REQUIRED")
                validate_relative_path(output_ref)
                validate_sha256(output_sha256)
                updates["status"] = "completed"
                updates["output_ref"] = output_ref
                updates["output_sha256"] = output_sha256
            else:
                raise PipelineTaskError(ERR_SCOPE_VIOLATION, "SIDECAR_SCOPE_VIOLATION")
            new_item = item.model_copy(update=updates)
            event = PipelineEvent(
                contract_version=_CONTRACT_VERSION, event_id=self.uuid(), task_id=task_id,
                sequence=task.last_event_sequence + 1, event_type=event_type, occurred_at=now,
                # 事件 stage 记录"刚刚完成的阶段"（转换前），不是更新后的 current_stage（impl-3-fix-1）
                stage=stage, item_id=item_id, attempt_id=event_attempt,
                revision_before=task.revision, revision_after=task.revision + 1,
                reason_code=None, metadata={},
            )
            new_task = task.model_copy(update={
                "updated_at": now, "revision": task.revision + 1,
                "last_event_sequence": task.last_event_sequence + 1,
            })
            return new_task, new_item, event

        try:
            return self._apply_item_transition(task_id, item_id, expected_task_revision, expected_item_revision, trans)
        except _IdempotentComplete:
            return self._load_item_required(task_id, item_id)  # 幂等命中：返回原状态

    # ---------------- 11E-2c：scoring item 安全重置 pending ----------------

    def reset_scoring_item_to_pending(
        self, task_id: str, item_id: str, expected_task_revision: int, expected_item_revision: int,
        reason_code: str,
    ) -> PipelineItem:
        """scoring 专用：调用前 stale running / 可重试 failed 安全重置 pending（11E-2c 恢复）。

        - 只允许 scoring_pipeline item；不增加 attempt；保留 current_stage 与 last_error（审计）。
        - running 且心跳有效（非 stale）拒绝（不得抢占有效租约）。
        - failed 且 retryable=false 拒绝（不可盲目重试）。
        - 事件 resume_decided；幂等：pending 原样返回。
        """

        def trans(task: PipelineTask, item: PipelineItem, now: Any):
            if item.task_type != "scoring_pipeline":
                raise PipelineTaskError(ERR_SCOPE_VIOLATION, "SIDECAR_SCOPE_VIOLATION")
            if item.status == "pending":
                raise _IdempotentComplete()
            if item.status == "running":
                if not self._is_stale(item, now):
                    raise PipelineTaskError(ERR_INVALID_STATE_TRANSITION, "SIDECAR_LEASE_ACTIVE")
            elif item.status == "failed":
                if not item.retryable:
                    raise PipelineTaskError(ERR_INVALID_STATE_TRANSITION, "SIDECAR_INVALID_STATE_TRANSITION")
            else:
                raise PipelineTaskError(ERR_INVALID_STATE_TRANSITION, "SIDECAR_INVALID_STATE_TRANSITION")
            new_item = item.model_copy(update={
                "status": "pending",
                "retryable": False,
                "heartbeat_updated_at": None,
                "updated_at": now,
                "item_revision": item.item_revision + 1,
            })
            event = PipelineEvent(
                contract_version=_CONTRACT_VERSION, event_id=self.uuid(), task_id=task_id,
                sequence=task.last_event_sequence + 1, event_type="resume_decided", occurred_at=now,
                stage=item.current_stage, item_id=item_id, attempt_id=None,
                revision_before=task.revision, revision_after=task.revision + 1,
                reason_code=reason_code or None, metadata={},
            )
            new_task = task.model_copy(update={
                "updated_at": now, "revision": task.revision + 1,
                "last_event_sequence": task.last_event_sequence + 1,
            })
            return new_task, new_item, event

        try:
            return self._apply_item_transition(task_id, item_id, expected_task_revision, expected_item_revision, trans)
        except _IdempotentComplete:
            return self._load_item_required(task_id, item_id)


    def _last_scoring_complete_ref(self, task_id: str, item_id: str, completed_stage: PipelineStage) -> Optional[str]:
        """事件日志中该 item 最近一次完成指定阶段的 attempt_id（事件 stage=完成阶段，impl-3-fix-1）。"""
        for event in reversed(self.store.load_event_log(task_id)):
            if event.item_id == item_id and event.event_type == "item_completed" and event.stage == completed_stage:
                return event.attempt_id
        return None

    def _fail_item(
        self, task_id: str, item_id: str, expected_task_revision: int, expected_item_revision: int,
        error_code: str, retryable: bool,
    ) -> PipelineItem:
        def trans(task: PipelineTask, item: PipelineItem, now: Any):
            if item.status == "completed":
                raise PipelineTaskError(ERR_SUCCESS_RESULT_PROTECTED, "SIDECAR_SUCCESS_RESULT_PROTECTED")
            if item.status != "running":
                raise PipelineTaskError(ERR_INVALID_STATE_TRANSITION, "SIDECAR_INVALID_STATE_TRANSITION")
            new_item = item.model_copy(update={
                "status": "failed",
                "retryable": retryable,
                "last_error": PipelineError(error_code=error_code, message_key=error_code,
                                            retryable=retryable, stage=item.current_stage),
                "heartbeat_updated_at": None,
                "updated_at": now,
                "item_revision": item.item_revision + 1,
            })
            event = PipelineEvent(
                contract_version=_CONTRACT_VERSION, event_id=self.uuid(), task_id=task_id,
                sequence=task.last_event_sequence + 1, event_type="item_failed", occurred_at=now,
                stage=item.current_stage, item_id=item_id, attempt_id=None,
                revision_before=task.revision, revision_after=task.revision + 1,
                reason_code=error_code, metadata={},
            )
            new_task = task.model_copy(update={
                "updated_at": now, "revision": task.revision + 1,
                "last_event_sequence": task.last_event_sequence + 1,
            })
            return new_task, new_item, event

        return self._apply_item_transition(task_id, item_id, expected_task_revision, expected_item_revision, trans)

    def send_item_to_manual_review(
        self, task_id: str, item_id: str, expected_task_revision: int, expected_item_revision: int,
        reason_code: str,
    ) -> PipelineItem:
        def trans(task: PipelineTask, item: PipelineItem, now: Any):
            if item.status == "completed":
                raise PipelineTaskError(ERR_SUCCESS_RESULT_PROTECTED, "SIDECAR_SUCCESS_RESULT_PROTECTED")
            if item.status not in ("pending", "running", "failed"):
                raise PipelineTaskError(ERR_INVALID_STATE_TRANSITION, "SIDECAR_INVALID_STATE_TRANSITION")
            new_item = item.model_copy(update={
                "status": "manual_review",
                "heartbeat_updated_at": None,
                "last_error": PipelineError(error_code=reason_code, message_key=reason_code,
                                            retryable=False, stage=item.current_stage),
                "updated_at": now,
                "item_revision": item.item_revision + 1,
            })
            event = PipelineEvent(
                contract_version=_CONTRACT_VERSION, event_id=self.uuid(), task_id=task_id,
                sequence=task.last_event_sequence + 1, event_type="item_sent_to_manual_review",
                occurred_at=now, stage=item.current_stage, item_id=item_id, attempt_id=None,
                revision_before=task.revision, revision_after=task.revision + 1,
                reason_code=reason_code, metadata={},
            )
            new_task = task.model_copy(update={
                "updated_at": now, "revision": task.revision + 1,
                "last_event_sequence": task.last_event_sequence + 1,
            })
            return new_task, new_item, event

        return self._apply_item_transition(task_id, item_id, expected_task_revision, expected_item_revision, trans)

    def skip_item(
        self, task_id: str, item_id: str, expected_task_revision: int, expected_item_revision: int,
    ) -> PipelineItem:
        def trans(task: PipelineTask, item: PipelineItem, now: Any):
            if item.status == "completed":
                raise PipelineTaskError(ERR_SUCCESS_RESULT_PROTECTED, "SIDECAR_SUCCESS_RESULT_PROTECTED")
            if item.status not in ("pending", "failed"):
                raise PipelineTaskError(ERR_INVALID_STATE_TRANSITION, "SIDECAR_INVALID_STATE_TRANSITION")
            new_item = item.model_copy(update={
                "status": "skipped",
                "heartbeat_updated_at": None,
                "updated_at": now,
                "item_revision": item.item_revision + 1,
            })
            event = PipelineEvent(
                contract_version=_CONTRACT_VERSION, event_id=self.uuid(), task_id=task_id,
                sequence=task.last_event_sequence + 1, event_type="item_skipped", occurred_at=now,
                stage=item.current_stage, item_id=item_id, attempt_id=None,
                revision_before=task.revision, revision_after=task.revision + 1,
                reason_code=None, metadata={},
            )
            new_task = task.model_copy(update={
                "updated_at": now, "revision": task.revision + 1,
                "last_event_sequence": task.last_event_sequence + 1,
            })
            return new_task, new_item, event

        return self._apply_item_transition(task_id, item_id, expected_task_revision, expected_item_revision, trans)


class _IdempotentComplete(Exception):
    """内部哨兵：complete 幂等命中（相同输出）返回原状态。"""
