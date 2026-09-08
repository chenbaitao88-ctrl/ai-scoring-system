"""
ScoringTaskCreator（Phase 11E-2a-prerequisite-impl-2 + fix-1）。

scoring_pipeline 后继任务安全创建服务：

- 上游事实核对全部来自权威存储（store / registration service / profile lookup），不信任调用方声明。
- 新 task/item 构造、不可变评分配置、幂等创建、原子持久化（复用 PipelineTaskStore 事务）。
- fix-1：基于 idempotency_key 的创建锁（真实并发幂等）；source item 首次核对后使用
  内存权威快照不再二次读取；store/sidecar 损坏显式传播（不吞成 NOT_FOUND）；
  从权威 ScoringInputProfileLookup 绑定评分口径版本。
- 不执行评分、不调用 Provider、不接 API；不得修改 source task/item。
- 错误信息不含学生正文、绝对路径、网盘链接或敏感值（稳定错误码）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from models.pipeline_task import (
    PipelineError,
    PipelineErrorSummary,
    PipelineEvent,
    PipelineItem,
    PipelineItemIndexEntry,
    PipelineStageSummary,
    PipelineTask,
    compute_item_input_fingerprint,
    compute_task_idempotency,
    sha256_canonical,
)
from models.scoring_configuration import ScoringTaskConfiguration, ScoringTaskCreateRequest
from services.evidence_registration_service import RegistrationError
from services.pipeline_evidence_checks import (
    ERR_EVIDENCE_MISMATCH,
    ERR_EVIDENCE_RECORD_MISSING,
    ERR_VALIDATION_MISSING,
    check_evidence_identity,
    check_validation_binding,
)
from services.pipeline_task_store import (
    ERR_LOCK_CONFLICT,
    ERR_NOT_FOUND,
    ERR_TASK_CORRUPTED,
    PipelineStoreError,
    PipelineTaskStore,
    run_task_transaction,
)

# ---------------- 稳定错误码 ----------------

ERR_SOURCE_TASK_NOT_FOUND = "SCORING_SOURCE_TASK_NOT_FOUND"
ERR_SOURCE_TASK_TYPE_INVALID = "SCORING_SOURCE_TASK_TYPE_INVALID"
ERR_SOURCE_TASK_NOT_SETTLED = "SCORING_SOURCE_TASK_NOT_SETTLED"
ERR_SOURCE_ITEM_NOT_FOUND = "SCORING_SOURCE_ITEM_NOT_FOUND"
ERR_SOURCE_ITEM_NOT_ELIGIBLE = "SCORING_SOURCE_ITEM_NOT_ELIGIBLE"
ERR_SOURCE_BINDING_MISMATCH = "SCORING_SOURCE_BINDING_MISMATCH"
ERR_VALIDATION_NOT_CURRENT = "SCORING_VALIDATION_NOT_CURRENT"
ERR_PRIVACY_CHECK_FAILED = "SCORING_PRIVACY_CHECK_FAILED"
ERR_MODEL_INPUT_NOT_ALLOWED = "SCORING_MODEL_INPUT_NOT_ALLOWED"
ERR_EVIDENCE_LEVEL_NOT_ALLOWED = "SCORING_EVIDENCE_LEVEL_NOT_ALLOWED"
ERR_DUPLICATE_SOURCE_ITEM = "SCORING_DUPLICATE_SOURCE_ITEM"
ERR_EMPTY_SOURCE_ITEMS = "SCORING_EMPTY_SOURCE_ITEMS"
ERR_IDEMPOTENCY_CONFLICT = "SCORING_IDEMPOTENCY_CONFLICT"
ERR_TASK_WRITE_FAILED = "SCORING_TASK_WRITE_FAILED"
# 11E-2a-prerequisite-impl-2-fix-1：损坏 / 锁 / Profile 绑定
ERR_SOURCE_TASK_CORRUPTED = "SCORING_SOURCE_TASK_CORRUPTED"
ERR_SOURCE_ITEM_CORRUPTED = "SCORING_SOURCE_ITEM_CORRUPTED"
ERR_EVIDENCE_RECORD_CORRUPTED = "SCORING_EVIDENCE_RECORD_CORRUPTED"
ERR_VALIDATION_CORRUPTED = "SCORING_VALIDATION_CORRUPTED"
ERR_CREATION_LOCK_CONFLICT = "SCORING_CREATION_LOCK_CONFLICT"
ERR_PROFILE_NOT_FOUND = "SCORING_PROFILE_NOT_FOUND"
ERR_CONFIGURATION_PROFILE_MISMATCH = "SCORING_CONFIGURATION_PROFILE_MISMATCH"

_ALLOWED_EVIDENCE_LEVELS = ("sufficient", "limited")


class ScoringTaskError(Exception):
    """scoring 任务创建稳定错误。"""

    def __init__(self, error_code: str, message_key: str = "", retryable: bool = False):
        self.error_code = error_code
        self.message_key = message_key or error_code
        self.retryable = retryable
        super().__init__(error_code)


def _map_store_error(exc: PipelineStoreError, corrupted_code: str, not_found_code: str) -> ScoringTaskError:
    """把 PipelineStoreError 按实际错误码分类（fix-2：不把存储错误降级成 NOT_FOUND）。

    - ERR_TASK_CORRUPTED -> 对应 CORRUPTED 错误。
    - ERR_NOT_FOUND / 权威加载返回 None -> 对应 NOT_FOUND。
    - ERR_LOCK_CONFLICT -> SCORING_CREATION_LOCK_CONFLICT（可重试）。
    - 事务错误、revision 冲突、事件冲突等未定义映射 -> 原样传播，不得伪装成 NOT_FOUND。
    """
    if exc.error_code == ERR_TASK_CORRUPTED:
        return ScoringTaskError(corrupted_code)
    if exc.error_code == ERR_NOT_FOUND:
        return ScoringTaskError(not_found_code)
    if exc.error_code == ERR_LOCK_CONFLICT:
        return ScoringTaskError(ERR_CREATION_LOCK_CONFLICT, retryable=True)
    raise exc


def _map_registration_error(exc: RegistrationError, corrupted_code: str) -> ScoringTaskError:
    """把注册服务错误映射为稳定 scoring 错误：损坏类显式映射；其余原样传播（不吞）。"""
    if "CORRUPTED" in (exc.code or ""):
        return ScoringTaskError(corrupted_code)
    raise exc


class ScoringTaskCreator:
    """scoring_pipeline 后继任务创建器。"""

    def __init__(
        self,
        store: PipelineTaskStore,
        registration: object,  # 只读接口：read_record_strict / read_validation_strict
        profile_lookup: object,  # 只读接口：get_input_profile(profile_version) -> Optional[ScoringInputProfile]
        clock: Optional[Callable[[], datetime]] = None,
        uuid_factory: Optional[Callable[[], str]] = None,
    ):
        self._store = store
        self._reg = registration
        self._profile_lookup = profile_lookup
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._uuid = uuid_factory or (lambda: str(__import__("uuid").uuid4()))

    # ---------------- 主入口 ----------------

    def create_scoring_task(
        self, request: ScoringTaskCreateRequest,
    ) -> Tuple[PipelineTask, bool]:
        """创建 scoring task（幂等：同输入返回既有任务；并发安全基于 idempotency_key 创建锁）。

        返回 (task, created)；created=False 表示幂等命中。
        """
        if not request.source_item_ids:
            raise ScoringTaskError(ERR_EMPTY_SOURCE_ITEMS)
        if len(request.source_item_ids) != len(set(request.source_item_ids)):
            raise ScoringTaskError(ERR_DUPLICATE_SOURCE_ITEM)

        # 0. 权威 Profile 绑定（fix-1）：调用方只传 profile_version，口径来自权威 Lookup
        profile = self._profile_lookup.get_input_profile(request.configuration.profile_version)
        if profile is None:
            raise ScoringTaskError(ERR_PROFILE_NOT_FOUND)
        cfg = request.configuration
        if (
            cfg.scoring_policy_version != profile.scoring_policy_version
            or cfg.rubric_version != profile.rubric_version
            or cfg.prompt_version != profile.prompt_version
            or cfg.response_schema_version != profile.response_schema_version
        ):
            raise ScoringTaskError(ERR_CONFIGURATION_PROFILE_MISMATCH)

        # 1-3. 上游 task 事实（损坏显式传播，不吞成 NOT_FOUND）
        source_task = self._load_task(request.source_task_id)
        if source_task is None:
            raise ScoringTaskError(ERR_SOURCE_TASK_NOT_FOUND)
        if source_task.task_type != "evidence_preparation_pipeline":
            raise ScoringTaskError(ERR_SOURCE_TASK_TYPE_INVALID)
        if source_task.status not in ("completed", "completed_with_errors"):
            raise ScoringTaskError(ERR_SOURCE_TASK_NOT_SETTLED)

        # 4-11. 逐 source item 核对；核对通过后保存权威快照（fix-1：后续不再二次读取）
        snapshots: Dict[str, PipelineItem] = {}
        for source_item_id in request.source_item_ids:
            item = self._load_item(source_task.task_id, source_item_id)
            if item is None or item.task_id != source_task.task_id:
                raise ScoringTaskError(ERR_SOURCE_ITEM_NOT_FOUND)
            if item.task_type != "evidence_preparation_pipeline":
                raise ScoringTaskError(ERR_SOURCE_ITEM_NOT_ELIGIBLE)
            if item.status != "completed":
                raise ScoringTaskError(ERR_SOURCE_ITEM_NOT_ELIGIBLE)
            if item.evidence_level not in _ALLOWED_EVIDENCE_LEVELS:
                raise ScoringTaskError(ERR_EVIDENCE_LEVEL_NOT_ALLOWED)
            self._check_authoritative(item)
            snapshots[source_item_id] = item

        # 12. 幂等键（source 维度 + 排序 source_item_ids；顺序无关；只用已核对快照）
        sorted_source_ids = sorted(request.source_item_ids)
        idem_inputs = [snapshots[sid] for sid in sorted_source_ids]
        idempotency_key = compute_task_idempotency(
            contract_version="pipeline-task/v1",
            task_type="scoring_pipeline",
            batch_id=request.batch_id,
            execution_scope=["score", "review", "export"],
            items=[(it.package_id, it.package_revision, it.input_fingerprint) for it in idem_inputs],
            configuration_fingerprint=cfg.configuration_fingerprint,
            source_task_id=request.source_task_id,
            source_item_ids=sorted_source_ids,
        )
        payload = {
            "contract_version": "pipeline-task/v1",
            "task_type": "scoring_pipeline",
            "batch_id": request.batch_id,
            "execution_scope": ["score", "review", "export"],
            "source_task_id": request.source_task_id,
            "ordered_source_item_ids": sorted_source_ids,
            "ordered_input_fingerprints": [it.input_fingerprint for it in idem_inputs],
            "configuration_fingerprint": cfg.configuration_fingerprint,
        }
        payload_sha256 = sha256_canonical(payload)

        # 13. 创建锁：锁内再次扫描 + 构造 + 写入（fix-1 真实并发幂等）
        #     锁获取/持有/释放或事务写路径的 LOCK_CONFLICT 均映射为可重试稳定错误
        try:
            with self._store.acquire_creation_lock(idempotency_key):
                existing = self._find_existing_by_idempotency(idempotency_key, payload_sha256)
                if existing is not None:
                    return existing, False

                now = self._clock()
                task_id = self._uuid()
                items: List[PipelineItem] = []
                for sid in sorted_source_ids:
                    src = snapshots[sid]
                    items.append(PipelineItem(
                        contract_version="pipeline-item/v1",
                        task_type="scoring_pipeline",
                        source_item_id=src.item_id,
                        item_id=self._uuid(),
                        task_id=task_id,
                        package_id=src.package_id,
                        package_revision=src.package_revision,
                        manifest_sha256=src.manifest_sha256,
                        registration_record_id=src.registration_record_id,
                        validation_id=src.validation_id,
                        input_fingerprint=src.input_fingerprint,
                        idempotency_key=sha256_canonical({"task": idempotency_key, "source_item_id": src.item_id}),
                        status="pending",
                        current_stage="score",
                        attempt_count=0,
                        max_attempts=3,
                        heartbeat_interval_seconds=60,
                        stale_after_seconds=300,
                        heartbeat_updated_at=None,
                        created_at=now,
                        updated_at=now,
                        retryable=False,
                        last_error=None,
                        output_ref=None,
                        output_sha256=None,
                        item_revision=1,
                        evidence_level=src.evidence_level,
                    ))
                task = PipelineTask(
                    contract_version="pipeline-task/v1",
                    task_type="scoring_pipeline",
                    execution_scope=["score", "review", "export"],
                    source_task_id=request.source_task_id,
                    task_id=task_id,
                    batch_id=request.batch_id,
                    status="pending",
                    current_stage="score",
                    created_at=now,
                    started_at=None,
                    updated_at=now,
                    paused_at=None,
                    completed_at=None,
                    total_items=len(items),
                    pending_items=len(items),
                    running_items=0,
                    completed_items=0,
                    failed_items=0,
                    skipped_items=0,
                    manual_review_items=0,
                    concurrency=request.concurrency,
                    configuration_snapshot=cfg,
                    configuration_fingerprint=cfg.configuration_fingerprint,
                    item_index=[PipelineItemIndexEntry(
                        item_id=i.item_id, task_type="scoring_pipeline", source_item_id=i.source_item_id,
                        package_id=i.package_id, package_revision=i.package_revision,
                        status=i.status, current_stage=i.current_stage, input_fingerprint=i.input_fingerprint,
                        item_revision=i.item_revision, evidence_level=i.evidence_level,
                    ) for i in items],
                    stage_summaries=self._scoring_stage_summaries(len(items), now),
                    error_summary=PipelineErrorSummary(count=0, codes=[]),
                    last_event_sequence=1,
                    revision=1,
                    idempotency_key=idempotency_key,
                    idempotency_payload_sha256=payload_sha256,
                )
                event = PipelineEvent(
                    contract_version="pipeline-task/v1",
                    event_id=self._uuid(),
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
                run_task_transaction(
                    self._store, task_id, event,
                    new_task=task, new_items=items,
                    expected_task_revision=None, expected_item_revisions={},
                )
                return task, True
        except ScoringTaskError:
            # 服务内部已明确分类的错误（IDEMPOTENCY_CONFLICT/CORRUPTED/PROFILE_MISMATCH 等）
            # 必须原样传播，不得改写成普通写入失败（fix-2）
            raise
        except PipelineStoreError as exc:
            # 锁获取/持有/事务写路径：LOCK_CONFLICT -> 可重试稳定错误；
            # 其余权威存储层明确报告的事务/写入错误 -> 写失败
            if exc.error_code == ERR_LOCK_CONFLICT:
                raise ScoringTaskError(ERR_CREATION_LOCK_CONFLICT, retryable=True) from exc
            raise ScoringTaskError(ERR_TASK_WRITE_FAILED) from exc
        # 未知程序错误（RuntimeError 等）原样传播，暴露真实缺陷，不做宽泛包装（fix-2）

    # ---------------- 权威核对 ----------------

    def _check_authoritative(self, item: PipelineItem) -> None:
        """从权威存储读取 record/validation 并逐项核对（不信任调用方声明）。

        损坏显式映射为 CORRUPTED；未知异常原样传播（不吞成 binding mismatch）。
        """
        try:
            record = self._reg.read_record_strict(item.package_id, item.package_revision)
        except RegistrationError as exc:
            raise _map_registration_error(exc, ERR_EVIDENCE_RECORD_CORRUPTED) from exc
        err = check_evidence_identity(item, record)
        if err is not None:
            raise ScoringTaskError(ERR_SOURCE_BINDING_MISMATCH)
        try:
            validation = self._reg.read_validation_strict(
                item.package_id, item.package_revision, item.validation_id,
            )
        except RegistrationError as exc:
            raise _map_registration_error(exc, ERR_VALIDATION_CORRUPTED) from exc
        if validation is None or validation.validation_id != record.latest_validation_id:
            raise ScoringTaskError(ERR_VALIDATION_NOT_CURRENT)
        if not validation.registration_allowed:
            raise ScoringTaskError(ERR_PRIVACY_CHECK_FAILED)
        if validation.model_input_allowed is not True:
            raise ScoringTaskError(ERR_MODEL_INPUT_NOT_ALLOWED)
        err2 = check_validation_binding(item, record, validation)
        if err2 == ERR_VALIDATION_MISSING:
            raise ScoringTaskError(ERR_VALIDATION_NOT_CURRENT)
        if err2 == ERR_EVIDENCE_MISMATCH:
            raise ScoringTaskError(ERR_SOURCE_BINDING_MISMATCH)
        if err2 is not None:
            raise ScoringTaskError(ERR_MODEL_INPUT_NOT_ALLOWED)

    # ---------------- 只读加载与幂等 ----------------

    def _load_task(self, task_id: str) -> Optional[PipelineTask]:
        """权威读取：不存在 -> None；损坏 -> ERR_SOURCE_TASK_CORRUPTED；未知异常传播。"""
        try:
            return self._store.load_task(task_id)
        except PipelineStoreError as exc:
            raise _map_store_error(exc, ERR_SOURCE_TASK_CORRUPTED, ERR_SOURCE_TASK_NOT_FOUND) from exc

    def _load_item(self, task_id: str, item_id: str) -> Optional[PipelineItem]:
        """权威读取：不存在 -> None；损坏 -> ERR_SOURCE_ITEM_CORRUPTED；未知异常传播。"""
        try:
            return self._store.load_item(task_id, item_id)
        except PipelineStoreError as exc:
            raise _map_store_error(exc, ERR_SOURCE_ITEM_CORRUPTED, ERR_SOURCE_ITEM_NOT_FOUND) from exc

    def _find_existing_by_idempotency(
        self, idempotency_key: str, payload_sha256: str,
    ) -> Optional[PipelineTask]:
        """只读任务扫描（调用方必须已持有创建锁）：同 key 同 payload 返回原任务；
        同 key 不同 payload 显式冲突；扫描中的损坏任务显式传播。"""
        tasks_dir = self._store.tasks_dir
        if not tasks_dir.exists():
            return None
        for entry in sorted(tasks_dir.iterdir()):
            if not entry.is_dir():
                continue
            task_path = entry / "task.json"
            if not task_path.exists():
                continue
            try:
                task = self._store.load_task(entry.name)
            except PipelineStoreError as exc:
                raise _map_store_error(exc, ERR_SOURCE_TASK_CORRUPTED, ERR_SOURCE_TASK_NOT_FOUND) from exc
            if task is None:
                continue
            if task.idempotency_key != idempotency_key:
                continue
            if task.idempotency_payload_sha256 == payload_sha256:
                return task
            raise ScoringTaskError(ERR_IDEMPOTENCY_CONFLICT)
        return None

    # ---------------- 阶段摘要 ----------------

    @staticmethod
    def _scoring_stage_summaries(item_count: int, now: datetime) -> List[PipelineStageSummary]:
        """scoring 五阶段摘要：import/validate 范围外 not_started 计数零；
        score pending 计数与 item 数一致；review/export not_started。"""
        def summary(stage: str, status: str, depends_on: List[str], total: int) -> PipelineStageSummary:
            return PipelineStageSummary(
                stage=stage, status=status, depends_on=depends_on,
                started_at=None, completed_at=None,
                total_items=total, pending_items=total if status == "pending" else 0,
                running_items=0, completed_items=0, failed_items=0, skipped_items=0,
                manual_review_items=0, blocking_error_codes=[], revision=1,
            )
        return [
            summary("import", "not_started", [], 0),
            summary("validate", "not_started", ["import"], 0),
            summary("score", "pending", [], item_count),
            summary("review", "not_started", ["score"], 0),
            summary("export", "not_started", ["review"], 0),
        ]
