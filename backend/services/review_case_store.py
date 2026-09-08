"""
ReviewCase / ReviewDecision / ResultAdoption / ManualAdjustment 文件型事实存储
（Phase 11E-3b-1）。

依据 Phase 11A-2d 契约第 18 节（文件型持久化要求）与 Phase 11A-3（路线 A：零 Schema sidecar）
实现人工复核工单、复核决定、结果采用和人工调整的不可变事实层。本阶段只建立事实层：
不接 API/前端、不推进 PipelineTask、不写数据库最终分、不调用 Provider。

存储布局（根目录可由测试注入）：

```text
<root>/
  tasks/{task_id}/items/{item_id}/
    cases/{review_case_id}.json       # 当前投影（revision 递增；历史事件不覆盖）
    decisions/{decision_id}.json      # 不可变事实
    adoptions/{adoption_id}.json      # 采用记录（状态可推进：proposed/blocked/adopted/superseded/revoked）
    adjustments/{adjustment_id}.json  # 不可变事实
    events.ndjson                     # 追加式审计事件（append-only，previous_event_hash 链）
    locks/review.lock                 # task/item 维度单机文件锁
```

冻结语义（与 ScoreAttemptStore 同构 + Review 业务规则）：
- 安全相对 ID（字符集 [A-Za-z0-9._-]，拒绝 / \\ .. 空字节 盘符 保留名），违反 -> UNSAFE_REVIEW_FACT_PATH。
- 事实对象 create-only：同 ID 同内容 -> 幂等命中（不重复追加事件）；同 ID 不同内容 -> 稳定冲突。
- ReviewCase 是唯一可推进投影：状态转换必须 CAS（expected_revision）、合法转换表、追加状态事件、
  重开保留旧 resolution（previous_resolution_decision_id / reopened_from_case_id）。
- ResultAdoption：同一 item 同一时刻最多一个 active adopted；新采用保留旧记录（旧记录转 superseded）；
  must_review 开放工单阻断自动采用；人工采用（reviewer）不得被自动采用（system）替换。
- ManualAdjustment 不覆盖 Provider 快照（adjusted_snapshot_id != base_snapshot_id）。
- 原子写（同目录 temp + fsync + os.replace）、进程锁 + 文件锁（O_CREAT|O_EXCL）、
  损坏显式失败（不伪装 NOT_FOUND）、事件只追加、写失败回滚零残留。
- 安全错误：错误信息只含稳定错误码，不含路径、正文、Key、URL、ID 或学生信息。
- 只读查询零副作用：不创建目录、不修改 revision/事件/时间戳。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from models.review_case import (
    ADOPTION_TERMINAL,
    DECISION_REQUIRED_TARGETS,
    ERR_DECISION_IDEMPOTENCY_CONFLICT,
    ERR_DECISION_REASON_MISMATCH,
    ERR_DECISION_STATE_NOT_DECISIONABLE,
    ERR_DECISION_SUPERSEDES_NOT_FOUND,
    ERR_DECISION_SUPERSEDES_SCOPE_MISMATCH,
    ERR_DECISION_TARGET_REQUIRED,
    ERR_LOCK_ACTOR_MISMATCH as ERR_FINAL_LOCK_ACTOR_MISMATCH,
    ERR_LOCK_CONFLICT as ERR_FINAL_LOCK_CONFLICT,
    ERR_LOCK_CORRUPTED as ERR_FINAL_LOCK_CORRUPTED,
    ERR_LOCK_NOT_FOUND as ERR_FINAL_LOCK_NOT_FOUND,
    ERR_LOCK_REVISION_CONFLICT as ERR_FINAL_LOCK_REVISION_CONFLICT,
    ERR_LOCK_WRITE_FAILED as ERR_FINAL_LOCK_WRITE_FAILED,
    ERR_RECOVERY_BLOCKED_BY_LOCK,
    ManualAdjustment,
    ManualAdjustmentOperation,
    ManualFinalLock,
    ResultAdoption,
    ReviewCase,
    ReviewDecision,
    ReviewEvent,
    _CASE_REVISION_EVENTS,
    can_transition_adoption,
    can_transition_case,
    validate_decision_reason_combo,
)
from services.score_attempt_store import (
    _append_ndjson_bytes,
    _atomic_write_bytes,
    _canonical_bytes,
    _cleanup_empty_chain,
    _strict_json_loads,
)

# ---------------------------------------------------------------- 稳定错误码（11E-3b-1 冻结）

ERR_CASE_NOT_FOUND = "REVIEW_CASE_NOT_FOUND"
ERR_CASE_CONFLICT = "REVIEW_CASE_CONFLICT"
ERR_CASE_CORRUPTED = "REVIEW_CASE_CORRUPTED"
ERR_DECISION_NOT_FOUND = "REVIEW_DECISION_NOT_FOUND"
ERR_DECISION_CONFLICT = "REVIEW_DECISION_CONFLICT"
ERR_DECISION_CORRUPTED = "REVIEW_DECISION_CORRUPTED"
ERR_ADOPTION_NOT_FOUND = "RESULT_ADOPTION_NOT_FOUND"
ERR_ADOPTION_CONFLICT = "RESULT_ADOPTION_CONFLICT"
ERR_ADOPTION_BLOCKED = "RESULT_ADOPTION_BLOCKED"
ERR_ADOPTION_CORRUPTED = "RESULT_ADOPTION_CORRUPTED"
ERR_ADJUSTMENT_NOT_FOUND = "MANUAL_ADJUSTMENT_NOT_FOUND"
ERR_ADJUSTMENT_CONFLICT = "MANUAL_ADJUSTMENT_CONFLICT"
ERR_ADJUSTMENT_CORRUPTED = "MANUAL_ADJUSTMENT_CORRUPTED"
ERR_BINDING_MISMATCH = "REVIEW_FACT_BINDING_MISMATCH"
ERR_WRITE_FAILED = "REVIEW_FACT_WRITE_FAILED"
ERR_LOCK_CONFLICT = "REVIEW_FACT_LOCK_CONFLICT"
ERR_EVENT_CORRUPTED = "REVIEW_EVENT_CORRUPTED"
ERR_UNSAFE_PATH = "UNSAFE_REVIEW_FACT_PATH"
ERR_CASE_LOCKED = "CASE_LOCKED"


class ReviewFactStoreError(Exception):
    """Review 事实层显式错误（非敏感：str 只返回稳定错误码）。"""

    def __init__(self, error_code: str, message_key: str = "", retryable: bool = False) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.message_key = message_key or error_code
        self.retryable = retryable

    def __str__(self) -> str:
        return self.error_code


# ---------------------------------------------------------------- 安全标识符

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WINDOWS_RESERVED = frozenset({
    "con", "prn", "aux", "nul",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
})


def _validate_id(value: str) -> str:
    """安全相对标识符校验：拒绝路径穿越（/ \\ .. 空字节）、保留名与超长 ID。"""
    if not isinstance(value, str) or not value:
        raise ReviewFactStoreError(ERR_UNSAFE_PATH, retryable=False)
    if "\x00" in value or "/" in value or "\\" in value:
        raise ReviewFactStoreError(ERR_UNSAFE_PATH, retryable=False)
    if not _ID_RE.fullmatch(value):
        raise ReviewFactStoreError(ERR_UNSAFE_PATH, retryable=False)
    if value.lower() in _WINDOWS_RESERVED or value.lower().rstrip(".") in _WINDOWS_RESERVED:
        raise ReviewFactStoreError(ERR_UNSAFE_PATH, retryable=False)
    return value


# ---------------------------------------------------------------- 进程内锁（跨线程共享）

_PROCESS_LOCKS: Dict[str, threading.Lock] = {}
_PROCESS_GUARD = threading.Lock()


def _process_lock_for(key: str) -> threading.Lock:
    with _PROCESS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.Lock())


class _ReviewFileLock:
    """单机文件锁（O_CREAT|O_EXCL）；owner token 为随机非敏感值。"""

    def __init__(self, lock_path: Path, timeout_ms: int = 5000) -> None:
        self.lock_path = Path(lock_path)
        self.timeout_ms = timeout_ms
        self._acquired = False
        self._owner = uuid.uuid4().hex

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() * 1000 + self.timeout_ms
        while True:
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, self._owner.encode("utf-8"))
                os.close(fd)
                self._acquired = True
                return
            except FileExistsError:
                if time.monotonic() * 1000 >= deadline:
                    raise ReviewFactStoreError(ERR_LOCK_CONFLICT, retryable=True)
                time.sleep(0.05)

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            current = self.lock_path.read_text(encoding="utf-8").strip()
        except OSError:
            current = None
        if current != self._owner:
            raise ReviewFactStoreError(ERR_LOCK_CONFLICT, retryable=True)
        try:
            self.lock_path.unlink()
        except OSError:
            pass
        try:
            if not any(self.lock_path.parent.iterdir()):
                self.lock_path.parent.rmdir()
        except OSError:
            pass
        self._acquired = False


class _ReviewLockContext:
    """进程内锁 + 文件锁；退出后释放锁并调用可选清理回调（空目录链清理在锁外执行）。"""

    def __init__(self, plock: threading.Lock, flock: _ReviewFileLock, on_release=None) -> None:
        self._plock = plock
        self._flock = flock
        self._on_release = on_release
        self._active = False

    def __enter__(self) -> "_ReviewLockContext":
        self._plock.acquire()
        try:
            self._flock.acquire()
        except ReviewFactStoreError:
            self._plock.release()
            raise
        self._active = True
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if not self._active:
            return
        try:
            self._flock.release()
        finally:
            self._plock.release()
            self._active = False
        if self._on_release is not None:
            try:
                self._on_release()
            except OSError:
                pass
        return False


# ---------------------------------------------------------------- 存储服务

_CASES_DIR = "cases"
_DECISIONS_DIR = "decisions"
_ADOPTIONS_DIR = "adoptions"
_ADJUSTMENTS_DIR = "adjustments"
_FINAL_LOCKS_DIR = "final-locks"  # 11F-1b：ManualFinalLock 事实目录（与 locks/ 锁目录分离）


def _adoption_scope_key(scope: Any) -> tuple:
    """完整采用 scope 键（11F-1a 契约 §4.4）：隔离判定限定全部六个维度。

    兼容 AdoptionScope 模型对象与 dict（测试/序列化边界）。
    """
    if isinstance(scope, dict):
        return (
            scope.get("competition_id"),
            scope.get("batch_id"),
            scope.get("stream_id"),
            scope.get("submission_id"),
            scope.get("scoring_policy_version"),
            scope.get("purpose"),
        )
    return (
        scope.competition_id,
        scope.batch_id,
        scope.stream_id,
        scope.submission_id,
        scope.scoring_policy_version,
        scope.purpose,
    )

# 事件 -> (revision 是否推进, 链接对象类型)
_EVENT_META = {
    "CASE_OPENED": (False, "case"),
    "REASON_ADDED": (False, "case"),
    "PRIORITY_CHANGED": (False, "case"),
    "CASE_ASSIGNED": (True, "case"),
    "REVIEW_STARTED": (True, "case"),
    "EVIDENCE_REQUESTED": (True, "case"),
    "EVIDENCE_RECEIVED": (True, "case"),
    "ATTEMPT_LINKED": (False, "case"),
    "DECISION_RECORDED": (False, "decision"),
    "ADOPTION_LINKED": (False, "adoption"),
    "MANUAL_ADJUSTMENT_LINKED": (False, "adjustment"),
    "CASE_RESOLVED": (True, "case"),
    "CASE_DISMISSED": (True, "case"),
    "CASE_CANCELLED": (True, "case"),
    "CASE_REOPENED": (True, "case"),
    "FINAL_RESULT_LOCKED": (False, "case"),
    "FINAL_RESULT_UNLOCKED": (False, "case"),
}


class ReviewCaseStore:
    """ReviewCase / ReviewDecision / ResultAdoption / ManualAdjustment 文件型事实存储。

    attempt_store（可选）：只读访问 ScoreAttempt/Snapshot/Validation 事实，用于跨 store 绑定校验；
    未注入时跳过快照/校验存在性校验（本地引用关系仍校验）。
    """

    def __init__(
        self,
        root: Path,
        attempt_store=None,
        lock_timeout_ms: int = 5000,
    ) -> None:
        self.root = Path(root)
        self.attempt_store = attempt_store
        self.lock_timeout_ms = lock_timeout_ms

    # ---------------- 路径构建（全部由受校验 ID 组合，防目录逃逸） ----------------

    def _validate_id(self, value: str) -> str:
        return _validate_id(value)

    def _item_dir(self, task_id: str, item_id: str) -> Path:
        return self.root / "tasks" / self._validate_id(task_id) / "items" / self._validate_id(item_id)

    def _obj_dir(self, task_id: str, item_id: str, rel_dir: str) -> Path:
        return self._item_dir(task_id, item_id) / rel_dir

    def _fact_path(self, task_id: str, item_id: str, rel_dir: str, object_id: str) -> Path:
        return self._obj_dir(task_id, item_id, rel_dir) / f"{self._validate_id(object_id)}.json"

    def _events_path(self, task_id: str, item_id: str) -> Path:
        return self._item_dir(task_id, item_id) / "events.ndjson"

    def _lock_path(self, task_id: str, item_id: str) -> Path:
        return self._item_dir(task_id, item_id) / "locks" / "review.lock"

    def _lock_context(self, task_id: str, item_id: str, on_release=None) -> _ReviewLockContext:
        lock_path = self._lock_path(task_id, item_id)
        return _ReviewLockContext(
            _process_lock_for(str(lock_path)),
            _ReviewFileLock(lock_path, timeout_ms=self.lock_timeout_ms),
            on_release=on_release,
        )

    # ---------------- 只读加载 ----------------

    def _parse_fact(self, path: Path, model_cls: Any, corrupted_code: str) -> Any:
        """解析事实文件：不存在 -> None；损坏/字段不合法 -> corrupted 显式失败。"""
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8")
            return model_cls.model_validate(_strict_json_loads(text))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ReviewFactStoreError(corrupted_code, retryable=False) from exc

    def _list_facts(self, task_id: str, item_id: str, rel_dir: str, model_cls: Any, corrupted_code: str) -> List[Any]:
        """列出 task/item 下事实：ID 稳定排序；只读零副作用；损坏显式失败不跳过。"""
        d = self._obj_dir(task_id, item_id, rel_dir)
        found: List[Any] = []
        if not d.exists():
            return found
        for p in sorted(d.iterdir(), key=lambda p: p.name):
            if not p.is_file():
                continue
            obj = self._parse_fact(p, model_cls, corrupted_code)
            if obj is not None:
                found.append(obj)
        return found

    # ---------------- ReviewCase ----------------

    def create_review_case(
        self, task_id: str, item_id: str, case: ReviewCase, *, actor=None,
    ) -> Tuple[str, ReviewCase]:
        """创建 ReviewCase 当前投影（create-only 幂等 + CASE_OPENED 事件）。

        - case.task_id/item_id 必须与目录上下文一致（REVIEW_FACT_BINDING_MISMATCH）。
        - 同 review_case_id 同内容 -> idempotent_hit；异内容 -> REVIEW_CASE_CONFLICT。
        - 指定 idempotency_key 时：同 task/item + 同 key 的既有工单 -> 幂等命中（返回既有工单）。
        """
        self._validate_id(task_id)
        self._validate_id(item_id)
        self._validate_id(case.review_case_id)
        if case.task_id != task_id or case.item_id != item_id:
            raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
        obj_dir = self._obj_dir(task_id, item_id, _CASES_DIR)
        path = obj_dir / f"{case.review_case_id}.json"
        canonical = _canonical_bytes(case.model_dump(mode="json"))

        def _cleanup():
            _cleanup_empty_chain(obj_dir, self.root)

        with self._lock_context(task_id, item_id, on_release=_cleanup):
            # idempotency_key 幂等（同 task/item + 同 key 命中既有工单）
            if case.idempotency_key is not None:
                for existing in self._list_facts(task_id, item_id, _CASES_DIR, ReviewCase, ERR_CASE_CORRUPTED):
                    if existing.idempotency_key == case.idempotency_key:
                        return ("idempotent_hit", existing)
            if path.exists():
                if path.read_bytes() == canonical:
                    return ("idempotent_hit", self._parse_fact(path, ReviewCase, ERR_CASE_CORRUPTED))
                raise ReviewFactStoreError(ERR_CASE_CONFLICT, retryable=False)
            try:
                _atomic_write_bytes(path, canonical)
            except OSError as exc:
                raise ReviewFactStoreError(ERR_WRITE_FAILED, retryable=True) from exc
            try:
                self._append_event(task_id, item_id, ReviewEvent(
                    event_id=uuid.uuid4().hex,
                    review_case_id=case.review_case_id,
                    case_revision_before=1,
                    case_revision_after=1,
                    event_type="CASE_OPENED",
                    reason_codes=list(case.reason_codes),
                    object_refs=[case.review_case_id],
                    from_status=None,
                    to_status=case.status,
                    actor=actor or _system_actor(),
                    occurred_at=case.opened_at,
                    event_hash=uuid.uuid4().hex * 2,  # 占位：见 _append_event 重写
                    previous_event_hash=None,
                ))
            except OSError as exc:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise ReviewFactStoreError(ERR_WRITE_FAILED, retryable=True) from exc
            return ("created", self._parse_fact(path, ReviewCase, ERR_CASE_CORRUPTED))

    def get_review_case(self, task_id: str, item_id: str, review_case_id: str) -> Optional[ReviewCase]:
        return self._parse_fact(
            self._fact_path(task_id, item_id, _CASES_DIR, review_case_id), ReviewCase, ERR_CASE_CORRUPTED,
        )

    def list_review_cases(self, task_id: str, item_id: Optional[str] = None) -> List[ReviewCase]:
        """列出 task 下（item 过滤可选）全部工单：review_case_id 稳定排序。只读零副作用。"""
        if item_id is not None:
            return self._list_facts(task_id, item_id, _CASES_DIR, ReviewCase, ERR_CASE_CORRUPTED)
        item_dir = self.root / "tasks" / self._validate_id(task_id) / "items"
        found: List[ReviewCase] = []
        if not item_dir.exists():
            return found
        for sub in sorted(item_dir.iterdir(), key=lambda p: p.name):
            if not sub.is_dir():
                continue
            found.extend(self._list_facts(task_id, sub.name, _CASES_DIR, ReviewCase, ERR_CASE_CORRUPTED))
        return sorted(found, key=lambda c: c.review_case_id)

    def list_open_cases(self, task_id: str, item_id: Optional[str] = None) -> List[ReviewCase]:
        """查询开放工单（open/assigned/in_review/waiting_for_evidence）。只读零副作用。"""
        OPEN = frozenset({"open", "assigned", "in_review", "waiting_for_evidence"})
        return [c for c in self.list_review_cases(task_id, item_id) if c.status in OPEN]

    def has_open_review_case(self, task_id: str, item_id: str) -> bool:
        return bool(self.list_open_cases(task_id, item_id))

    def transition_review_case(
        self,
        task_id: str,
        item_id: str,
        review_case_id: str,
        *,
        expected_revision: int,
        to_status: str,
        event_type: str,
        actor,
        occurred_at: datetime,
        reason_codes: Optional[List[str]] = None,
        object_refs: Optional[List[str]] = None,
        resolution_decision_id: Optional[str] = None,
        assigned_to=None,
        assignment_group: Optional[str] = None,
        package_revision: Optional[int] = None,
    ) -> ReviewCase:
        """推进 ReviewCase 状态（CAS revision + 合法转换 + 状态事件；重开保留旧 resolution）。

        - 不存在 -> REVIEW_CASE_NOT_FOUND；损坏 -> REVIEW_CASE_CORRUPTED。
        - revision 不匹配 -> REVIEW_CASE_CONFLICT（并发 CAS 保护）。
        - 状态转换非法 -> REVIEW_CASE_CONFLICT。
        - resolved/dismissed -> open/assigned（重开规则）：reopen_count+1、
          previous_resolution_decision_id=旧 resolution、resolution/resolved_at 清空、
          追加 CASE_REOPENED（reason_codes 可追加，blocks 重算）。
        - 已关闭工单不可原地篡改决定：resolved/dismissed 工单不允许再记录 resolution 变化
          （只能通过重开流程）。
        """
        obj_dir = self._obj_dir(task_id, item_id, _CASES_DIR)
        path = obj_dir / f"{review_case_id}.json"

        def _cleanup():
            _cleanup_empty_chain(obj_dir, self.root)

        with self._lock_context(task_id, item_id, on_release=_cleanup):
            current = self._parse_fact(path, ReviewCase, ERR_CASE_CORRUPTED)
            if current is None:
                raise ReviewFactStoreError(ERR_CASE_NOT_FOUND, retryable=False)
            if current.current_revision != expected_revision:
                raise ReviewFactStoreError(ERR_CASE_CONFLICT, retryable=False)
            if not can_transition_case(current.status, to_status):
                raise ReviewFactStoreError(ERR_CASE_CONFLICT, retryable=False)
            if to_status in ("resolved", "dismissed") and resolution_decision_id is None:
                raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)

            updates: Dict[str, Any] = {"status": to_status, "updated_at": occurred_at}
            is_reopen = (
                current.status in ("resolved", "dismissed") and to_status in ("open", "assigned")
            ) or (
                current.status == "waiting_for_evidence"
                and to_status in ("assigned", "in_review")
                and event_type == "CASE_REOPENED"
            )
            if is_reopen:
                updates["reopen_count"] = current.reopen_count + 1
                updates["previous_resolution_decision_id"] = current.resolution_decision_id
                updates["resolution_decision_id"] = None
                updates["resolved_at"] = None
            if to_status == "assigned":
                if current.assigned_at is None:
                    updates["assigned_at"] = occurred_at
                if assigned_to is not None:
                    updates["assigned_to"] = assigned_to
            if to_status == "in_review":
                if current.review_started_at is None:
                    updates["review_started_at"] = occurred_at
            if to_status in ("resolved", "dismissed"):
                updates["resolved_at"] = occurred_at
                updates["resolution_decision_id"] = resolution_decision_id
            if to_status == "waiting_for_evidence":
                updates["review_started_at"] = current.review_started_at or current.assigned_at
            if assignment_group is not None:
                updates["assignment_group"] = assignment_group
            if package_revision is not None:
                updates["package_revision"] = package_revision
            # 原因码追加（去重）+ blocks 重算
            merged = list(current.reason_codes)
            if reason_codes:
                for c in reason_codes:
                    if c not in merged:
                        merged.append(c)
                updates["reason_codes"] = merged
                from models.review_case import reason_blocks_auto_adoption, reason_blocks_export
                updates["blocks_auto_adoption"] = current.blocks_auto_adoption or any(
                    reason_blocks_auto_adoption(c) for c in merged)
                updates["blocks_export"] = current.blocks_export or any(
                    reason_blocks_export(c) for c in merged)
            new_case = current.model_copy(update=dict(updates, current_revision=current.current_revision + 1))
            new_case = ReviewCase.model_validate(new_case.model_dump(mode="json"))
            new_canonical = _canonical_bytes(new_case.model_dump(mode="json"))
            try:
                _atomic_write_bytes(path, new_canonical)
            except OSError as exc:
                raise ReviewFactStoreError(ERR_WRITE_FAILED, retryable=True) from exc
            try:
                self._append_event(task_id, item_id, ReviewEvent(
                    event_id=uuid.uuid4().hex,
                    review_case_id=review_case_id,
                    case_revision_before=expected_revision,
                    case_revision_after=expected_revision + 1,
                    event_type=event_type,
                    reason_codes=list(new_case.reason_codes),
                    object_refs=object_refs or [review_case_id],
                    from_status=current.status,
                    to_status=to_status,
                    actor=actor,
                    occurred_at=occurred_at,
                    event_hash=uuid.uuid4().hex * 2,
                    previous_event_hash=None,
                ))
            except OSError as exc:
                try:
                    # 事件追加失败：回滚到原投影（不覆盖旧事实）
                    _atomic_write_bytes(path, _canonical_bytes(current.model_dump(mode="json")))
                except OSError:
                    pass
                raise ReviewFactStoreError(ERR_WRITE_FAILED, retryable=True) from exc
            return new_case

    def advance_case_for_evidence_request(
        self,
        task_id: str,
        item_id: str,
        decision: ReviewDecision,
        *,
        actor,
        occurred_at: datetime,
    ) -> Tuple[str, ReviewCase]:
        """Advance request_additional_evidence decisions to waiting_for_evidence.

        This is intentionally separate from create_review_decision so replay can
        complete a decision that was persisted before the case transition.
        """
        if decision.decision_type != "request_additional_evidence":
            raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
        if decision.requested_package_revision is None:
            raise ReviewFactStoreError(ERR_DECISION_TARGET_REQUIRED, retryable=False)

        case = self._require_case(task_id, item_id, decision.review_case_id)
        if case.task_id != task_id or case.item_id != item_id:
            raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
        if self._case_event_references(
            task_id, item_id, decision.review_case_id, "EVIDENCE_REQUESTED", decision.decision_id
        ):
            return ("idempotent_hit", case)
        if self.get_active_final_lock(task_id, item_id) is not None:
            raise ReviewFactStoreError(ERR_CASE_LOCKED, retryable=False)
        if case.status != "in_review":
            raise ReviewFactStoreError(ERR_CASE_CONFLICT, retryable=False)
        if case.current_revision != decision.case_revision:
            raise ReviewFactStoreError(ERR_CASE_CONFLICT, retryable=False)
        if case.package_revision is not None and decision.requested_package_revision <= case.package_revision:
            raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)

        advanced = self.transition_review_case(
            task_id,
            item_id,
            decision.review_case_id,
            expected_revision=decision.case_revision,
            to_status="waiting_for_evidence",
            event_type="EVIDENCE_REQUESTED",
            actor=actor,
            occurred_at=occurred_at,
            reason_codes=list(decision.reason_codes),
            object_refs=[
                decision.review_case_id,
                decision.decision_id,
                f"package-revision-{decision.requested_package_revision}",
            ],
        )
        return ("advanced", advanced)

    def reopen_review_case(
        self,
        task_id: str,
        item_id: str,
        review_case_id: str,
        *,
        expected_revision: int,
        package_revision: int,
        request_decision_id: str,
        actor,
        occurred_at: datetime,
    ) -> Tuple[str, ReviewCase]:
        """Reopen a waiting_for_evidence case after the requested package revision exists."""
        decision = self.get_review_decision(task_id, item_id, request_decision_id)
        if decision is None:
            raise ReviewFactStoreError(ERR_DECISION_NOT_FOUND, retryable=False)
        if decision.review_case_id != review_case_id or decision.decision_type != "request_additional_evidence":
            raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
        if decision.requested_package_revision != package_revision:
            raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)

        case = self._require_case(task_id, item_id, review_case_id)
        if case.task_id != task_id or case.item_id != item_id:
            raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
        if (
            case.status == "assigned"
            and case.package_revision == package_revision
            and self._case_event_references(
                task_id, item_id, review_case_id, "CASE_REOPENED", request_decision_id
            )
        ):
            return ("idempotent_hit", case)
        if case.current_revision != expected_revision:
            raise ReviewFactStoreError(ERR_CASE_CONFLICT, retryable=False)
        if case.status != "waiting_for_evidence":
            raise ReviewFactStoreError(ERR_CASE_CONFLICT, retryable=False)
        if case.package_revision is not None and package_revision <= case.package_revision:
            raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
        if self.get_active_final_lock(task_id, item_id) is not None:
            raise ReviewFactStoreError(ERR_CASE_LOCKED, retryable=False)

        reopened = self.transition_review_case(
            task_id,
            item_id,
            review_case_id,
            expected_revision=expected_revision,
            to_status="assigned",
            event_type="CASE_REOPENED",
            actor=actor,
            occurred_at=occurred_at,
            object_refs=[
                review_case_id,
                request_decision_id,
                f"package-revision-{package_revision}",
            ],
            package_revision=package_revision,
        )
        return ("reopened", reopened)

    def _case_event_references(
        self,
        task_id: str,
        item_id: str,
        review_case_id: str,
        event_type: str,
        object_ref: str,
    ) -> bool:
        for event in self.load_review_events(task_id, item_id, review_case_id=review_case_id):
            if event.event_type == event_type and object_ref in event.object_refs:
                return True
        return False

    # ---------------- ReviewDecision ----------------

    def create_review_decision(
        self, task_id: str, item_id: str, decision: ReviewDecision, *, actor=None,
    ) -> Tuple[str, ReviewDecision]:
        """创建 ReviewDecision 事实（create-only 幂等 + DECISION_RECORDED 事件；11F-1b 扩展）。

        绑定与规则校验（11F-1a 契约 §2）：
        - case 存在且属于该 item（REVIEW_CASE_NOT_FOUND / REVIEW_FACT_BINDING_MISMATCH）。
        - case 状态允许记录决定（REVIEW_CASE_STATE_NOT_DECISIONABLE）。
        - decision.case_revision 不得晚于 case 当前 revision（CAS；REVIEW_DECISION_CONFLICT）。
        - decision_type × reason_code 组合矩阵（REVIEW_DECISION_REASON_MISMATCH）。
        - 强制 target 字段齐全（REVIEW_DECISION_TARGET_REQUIRED）。
        - supersedes_decision_id 存在且属于同一 case（REVIEW_DECISION_SUPERSEDES_*）。
        - 同 idempotency_key 同内容幂等命中；异内容 REVIEW_DECISION_IDEMPOTENCY_CONFLICT。
        - 创建不自动 apply、不自动采用、不修改任何分数。
        """
        self._validate_id(decision.decision_id)
        self._check_decision_binding(task_id, item_id, decision)
        obj_dir = self._obj_dir(task_id, item_id, _DECISIONS_DIR)
        path = obj_dir / f"{decision.decision_id}.json"
        canonical = _canonical_bytes(decision.model_dump(mode="json"))

        def _cleanup():
            _cleanup_empty_chain(obj_dir, self.root)

        with self._lock_context(task_id, item_id, on_release=_cleanup):
            # 幂等：同 idempotency_key（case 作用域）
            existing = self._find_decision_by_idempotency_key(task_id, item_id, decision)
            if existing is not None:
                if existing.model_dump(mode="json") == decision.model_dump(mode="json"):
                    return ("idempotent_hit", existing)
                raise ReviewFactStoreError(ERR_DECISION_IDEMPOTENCY_CONFLICT, retryable=False)
            # decision_id 维度 create-only
            if path.exists():
                if path.read_bytes() == canonical:
                    return ("idempotent_hit", self._parse_fact(path, ReviewDecision, ERR_DECISION_CORRUPTED))
                raise ReviewFactStoreError(ERR_DECISION_CONFLICT, retryable=False)
            try:
                _atomic_write_bytes(path, canonical)
            except OSError as exc:
                raise ReviewFactStoreError(ERR_WRITE_FAILED, retryable=True) from exc
            try:
                self._append_event(task_id, item_id, ReviewEvent(
                    event_id=uuid.uuid4().hex,
                    review_case_id=decision.review_case_id,
                    case_revision_before=1,
                    case_revision_after=1,
                    event_type="DECISION_RECORDED",
                    reason_codes=list(decision.reason_codes),
                    object_refs=[decision.decision_id],
                    from_status=None,
                    to_status=None,
                    actor=actor or decision.decided_by,
                    occurred_at=decision.decided_at,
                    event_hash=uuid.uuid4().hex * 2,
                    previous_event_hash=None,
                ))
            except OSError as exc:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise ReviewFactStoreError(ERR_WRITE_FAILED, retryable=True) from exc
            return ("created", self._parse_fact(path, ReviewDecision, ERR_DECISION_CORRUPTED))

    def _find_decision_by_idempotency_key(
        self, task_id: str, item_id: str, decision: ReviewDecision,
    ) -> Optional[ReviewDecision]:
        """按 idempotency_key（case 作用域）查找既有 decision；无则 None。只读，调用方须持锁。"""
        if decision.idempotency_key is None:
            return None
        for existing in self.list_review_decisions(task_id, item_id, review_case_id=decision.review_case_id):
            if existing.idempotency_key == decision.idempotency_key:
                return existing
        return None

    def get_review_decision(self, task_id: str, item_id: str, decision_id: str) -> Optional[ReviewDecision]:
        return self._parse_fact(
            self._fact_path(task_id, item_id, _DECISIONS_DIR, decision_id), ReviewDecision, ERR_DECISION_CORRUPTED,
        )

    def list_review_decisions(self, task_id: str, item_id: str, review_case_id: Optional[str] = None) -> List[ReviewDecision]:
        found = self._list_facts(task_id, item_id, _DECISIONS_DIR, ReviewDecision, ERR_DECISION_CORRUPTED)
        if review_case_id is None:
            return found
        return [d for d in found if d.review_case_id == review_case_id]

    # ---------------- ResultAdoption ----------------

    def create_adoption(
        self, task_id: str, item_id: str, adoption: ResultAdoption, *, actor=None,
    ) -> Tuple[str, ResultAdoption]:
        """创建 ResultAdoption 记录（create-only 幂等 + ADOPTION_LINKED 事件）。

        绑定校验：snapshot/validation/decision/case 引用一致（跨 store 存在性校验在
        attempt_store 注入时执行）。
        """
        self._validate_id(adoption.adoption_id)
        self._validate_write_scope(adoption.adoption_scope)
        self._check_adoption_binding(task_id, item_id, adoption)
        return self._write_fact(
            task_id, item_id, _ADOPTIONS_DIR, adoption.adoption_id, adoption, ResultAdoption,
            conflict_code=ERR_ADOPTION_CONFLICT,
            corrupted_code=ERR_ADOPTION_CORRUPTED,
            event_type="ADOPTION_LINKED",
            occurred_at=adoption.decided_at,
            actor=actor or adoption.decided_by,
        )

    def get_adoption(self, task_id: str, item_id: str, adoption_id: str) -> Optional[ResultAdoption]:
        return self._parse_fact(
            self._fact_path(task_id, item_id, _ADOPTIONS_DIR, adoption_id), ResultAdoption, ERR_ADOPTION_CORRUPTED,
        )

    def list_adoptions(self, task_id: str, item_id: str) -> List[ResultAdoption]:
        return self._list_facts(task_id, item_id, _ADOPTIONS_DIR, ResultAdoption, ERR_ADOPTION_CORRUPTED)

    def get_active_adoption(self, task_id: str, item_id: str, scope: Optional[Any] = None) -> Optional[ResultAdoption]:
        """查询 item 当前 active（adopted）采用记录（11F-1b 支持 scope 隔离）。

        - scope 为 None：保持 11E-3b-1 冻结语义——同一 item 同一时刻最多一个 active adopted，
          发现多个 -> REVIEW_FACT_BINDING_MISMATCH（数据不一致显式失败）。
        - scope 提供时：仅在该完整 scope 内查找 active；不同 batch_id/stream_id 的 active
          互不干扰（11F-1a 契约 §4.4）。
        """
        all_active = [a for a in self.list_adoptions(task_id, item_id) if a.status == "adopted"]
        if scope is None:
            if len(all_active) > 1:
                raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
            return all_active[0] if all_active else None
        key = _adoption_scope_key(scope)
        scoped = [a for a in all_active if _adoption_scope_key(a.adoption_scope) == key]
        if len(scoped) > 1:
            raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
        return scoped[0] if scoped else None

    def transition_adoption(
        self,
        task_id: str,
        item_id: str,
        adoption_id: str,
        *,
        to_status: str,
        actor,
        occurred_at: datetime,
        reason_code: Optional[str] = None,
    ) -> ResultAdoption:
        """推进 ResultAdoption 状态（adopted -> superseded/revoked、blocked -> adopted/revoked ...）。

        - 不存在 -> RESULT_ADOPTION_NOT_FOUND；损坏 -> RESULT_ADOPTION_CORRUPTED。
        - 非法转换 -> RESULT_ADOPTION_CONFLICT；终态不可回退。
        - adopted -> superseded：仅允许由新的明确采用记录驱动（adopt_result 内部调用）。
        - 人工锁定保护：人工 adopted（decided_by.actor_type=reviewer）禁止被自动替换
          （在 adopt_result 判定）。
        - 11F-1b：被 ManualFinalLock 锁定的 adoption 禁止 supersede/revoke
          -> RESULT_ADOPTION_BLOCKED（契约 §3.4 第 2 条）。
        """
        obj_dir = self._obj_dir(task_id, item_id, _ADOPTIONS_DIR)
        path = obj_dir / f"{adoption_id}.json"

        def _cleanup():
            _cleanup_empty_chain(obj_dir, self.root)

        with self._lock_context(task_id, item_id, on_release=_cleanup):
            current = self._parse_fact(path, ResultAdoption, ERR_ADOPTION_CORRUPTED)
            if current is None:
                raise ReviewFactStoreError(ERR_ADOPTION_NOT_FOUND, retryable=False)
            if not can_transition_adoption(current.status, to_status):
                raise ReviewFactStoreError(ERR_ADOPTION_CONFLICT, retryable=False)
            if to_status in ("superseded", "revoked") and self._adoption_locked(task_id, item_id, current):
                raise ReviewFactStoreError(ERR_ADOPTION_BLOCKED, retryable=False)
            return self._replace_adoption_status(
                task_id, item_id, current, to_status, actor, occurred_at,
                reason_code or current.reason_code,
            )

    def _replace_adoption_status(
        self, task_id: str, item_id: str, current: ResultAdoption,
        to_status: str, actor, occurred_at: datetime, reason_code: str,
    ) -> ResultAdoption:
        """内部状态替换（调用方必须已持有 item 锁）：原子写 + ADOPTION_LINKED 事件。

        事件追加失败回滚到原状态；不取锁（避免同线程重入非重入锁死锁）。
        """
        path = self._fact_path(task_id, item_id, _ADOPTIONS_DIR, current.adoption_id)
        updates: Dict[str, Any] = {
            "status": to_status,
            "reason_code": reason_code,
            "decided_at": current.decided_at,
        }
        new_adoption = ResultAdoption.model_validate(
            current.model_copy(update=updates).model_dump(mode="json"))
        try:
            _atomic_write_bytes(path, _canonical_bytes(new_adoption.model_dump(mode="json")))
        except OSError as exc:
            raise ReviewFactStoreError(ERR_WRITE_FAILED, retryable=True) from exc
        try:
            self._append_event(task_id, item_id, ReviewEvent(
                event_id=uuid.uuid4().hex,
                review_case_id=new_adoption.review_case_id or current.adoption_id,
                case_revision_before=1,
                case_revision_after=1,
                event_type="ADOPTION_LINKED",
                reason_codes=[],
                object_refs=[current.adoption_id, new_adoption.snapshot_id],
                from_status=current.status,
                to_status=to_status,
                actor=actor,
                occurred_at=occurred_at,
                event_hash=uuid.uuid4().hex * 2,
                previous_event_hash=None,
            ))
        except OSError as exc:
            try:
                _atomic_write_bytes(path, _canonical_bytes(current.model_dump(mode="json")))
            except OSError:
                pass
            raise ReviewFactStoreError(ERR_WRITE_FAILED, retryable=True) from exc
        return new_adoption

    def adopt_result(
        self, task_id: str, item_id: str, adoption: ResultAdoption, *, actor=None,
    ) -> ResultAdoption:
        """采用结果（高层入口）：保证同 scope 同一时刻最多一个 active adopted。

        顺序（锁内）：
        1. 绑定校验 + 自动采用阻断检查（开放 must_review 阻断工单 -> RESULT_ADOPTION_BLOCKED）。
        2. 11F-1b：存在 active ManualFinalLock -> RESULT_ADOPTION_BLOCKED（契约 §3.4 第 1 条，
           锁定后任何新采用需先 release）。
        3. 若已有同 scope active adopted A（11F-1b 隔离：不同 batch_id/stream_id 不互相干扰）：
           - adoption.supersedes_adoption_id 必须等于 A.adoption_id（RESULT_ADOPTION_CONFLICT）。
           - A 为人工采用（reviewer）且新采用为自动（system）-> RESULT_ADOPTION_BLOCKED（人工锁定保护）。
           - 先推进 A -> superseded，再创建新 adoption（避免双 active）。
        4. 无 active：直接创建（status 必须为 adopted）。
        """
        if adoption.status != "adopted":
            raise ReviewFactStoreError(ERR_ADOPTION_CONFLICT, retryable=False)
        self._validate_id(adoption.adoption_id)
        self._validate_write_scope(adoption.adoption_scope)
        self._check_adoption_binding(task_id, item_id, adoption)
        self._check_auto_adoption_blocked(task_id, item_id, adoption)
        obj_dir = self._obj_dir(task_id, item_id, _ADOPTIONS_DIR)
        path = obj_dir / f"{adoption.adoption_id}.json"
        canonical = _canonical_bytes(adoption.model_dump(mode="json"))

        def _cleanup():
            _cleanup_empty_chain(obj_dir, self.root)

        with self._lock_context(task_id, item_id, on_release=_cleanup):
            # 幂等/冲突（create-only）
            if path.exists():
                if path.read_bytes() == canonical:
                    return self._parse_fact(path, ResultAdoption, ERR_ADOPTION_CORRUPTED)
                raise ReviewFactStoreError(ERR_ADOPTION_CONFLICT, retryable=False)
            # 11F-1b：active 人工最终锁阻断新采用
            if self.get_active_final_lock(task_id, item_id) is not None:
                raise ReviewFactStoreError(ERR_ADOPTION_BLOCKED, retryable=False)
            active = self.get_active_adoption(task_id, item_id, scope=adoption.adoption_scope)
            if active is not None:
                if active.adoption_id != adoption.supersedes_adoption_id:
                    raise ReviewFactStoreError(ERR_ADOPTION_CONFLICT, retryable=False)
                if active.decided_by.actor_type == "reviewer" and adoption.decided_by.actor_type != "reviewer":
                    raise ReviewFactStoreError(ERR_ADOPTION_BLOCKED, retryable=False)
                # 先 supersede 旧（避免双 active），再创建新（内部不取锁，避免重入死锁）
                self._replace_adoption_status(
                    task_id, item_id, active, "superseded",
                    adoption.decided_by, adoption.decided_at, "SUPERSEDED_BY_NEW_ADOPTION",
                )
            # 内联创建（_write_fact 会再次取锁，不可在锁内调用）
            try:
                _atomic_write_bytes(path, canonical)
            except OSError as exc:
                raise ReviewFactStoreError(ERR_WRITE_FAILED, retryable=True) from exc
            try:
                self._append_event(task_id, item_id, ReviewEvent(
                    event_id=uuid.uuid4().hex,
                    review_case_id=adoption.review_case_id or adoption.adoption_id,
                    case_revision_before=1,
                    case_revision_after=1,
                    event_type="ADOPTION_LINKED",
                    reason_codes=[],
                    object_refs=[adoption.adoption_id, adoption.snapshot_id],
                    from_status=None,
                    to_status=None,
                    actor=actor or adoption.decided_by,
                    occurred_at=adoption.decided_at,
                    event_hash=uuid.uuid4().hex * 2,
                    previous_event_hash=None,
                ))
            except OSError as exc:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise ReviewFactStoreError(ERR_WRITE_FAILED, retryable=True) from exc
            return self._parse_fact(path, ResultAdoption, ERR_ADOPTION_CORRUPTED)

    # ---------------- ManualFinalLock（11F-1b，关闭 N11） ----------------

    def create_final_lock(
        self, task_id: str, item_id: str, lock: ManualFinalLock, *, actor=None,
    ) -> Tuple[str, ManualFinalLock]:
        """创建 active ManualFinalLock（create-only 幂等 + FINAL_RESULT_LOCKED 事件）。

        - lock.task_id/item_id 必须与目录上下文一致（REVIEW_FACT_BINDING_MISMATCH）。
        - 绑定：case/decision 存在且属于该 item；锁目标（adoption/snapshot）绑定一致。
        - 同 lock_id 同内容幂等；异内容 REVIEW_LOCK_CONFLICT。
        - 同 item 已存在 active 锁 -> REVIEW_LOCK_CONFLICT（契约 §3.3 重复锁定）。
        """
        self._validate_id(lock.lock_id)
        if lock.task_id != task_id or lock.item_id != item_id:
            raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
        self._check_lock_binding(task_id, item_id, lock)
        obj_dir = self._obj_dir(task_id, item_id, _FINAL_LOCKS_DIR)
        path = obj_dir / f"{lock.lock_id}.json"
        canonical = _canonical_bytes(lock.model_dump(mode="json"))

        def _cleanup():
            _cleanup_empty_chain(obj_dir, self.root)

        with self._lock_context(task_id, item_id, on_release=_cleanup):
            if path.exists():
                if path.read_bytes() == canonical:
                    return ("idempotent_hit", self._parse_fact(path, ManualFinalLock, ERR_FINAL_LOCK_CORRUPTED))
                raise ReviewFactStoreError(ERR_FINAL_LOCK_CONFLICT, retryable=False)
            if self.get_active_final_lock(task_id, item_id) is not None:
                raise ReviewFactStoreError(ERR_FINAL_LOCK_CONFLICT, retryable=False)
            try:
                _atomic_write_bytes(path, canonical)
            except OSError as exc:
                raise ReviewFactStoreError(ERR_FINAL_LOCK_WRITE_FAILED, retryable=True) from exc
            try:
                self._append_lock_event(task_id, item_id, lock, "FINAL_RESULT_LOCKED", lock.locked_at, actor)
            except OSError as exc:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise ReviewFactStoreError(ERR_FINAL_LOCK_WRITE_FAILED, retryable=True) from exc
            return ("created", self._parse_fact(path, ManualFinalLock, ERR_FINAL_LOCK_CORRUPTED))

    def get_active_final_lock(self, task_id: str, item_id: str) -> Optional[ManualFinalLock]:
        """查询 item 当前 active（未释放）人工最终锁。

        同一 item 同时最多一个 active 锁；发现多个 -> REVIEW_LOCK_CONFLICT（数据不一致显式失败）。
        """
        locks = [l for l in self._list_facts(task_id, item_id, _FINAL_LOCKS_DIR, ManualFinalLock, ERR_FINAL_LOCK_CORRUPTED)
                 if l.status == "active"]
        if len(locks) > 1:
            raise ReviewFactStoreError(ERR_FINAL_LOCK_CONFLICT, retryable=False)
        return locks[0] if locks else None

    def release_final_lock(
        self,
        task_id: str,
        item_id: str,
        lock_id: str,
        *,
        expected_revision: int,
        actor,
        occurred_at: datetime,
    ) -> Tuple[str, ManualFinalLock]:
        """释放 ManualFinalLock（active -> released；CAS + 操作者校验 + FINAL_RESULT_UNLOCKED 事件）。

        - 不存在 -> REVIEW_LOCK_NOT_FOUND；损坏 -> REVIEW_LOCK_CORRUPTED。
        - 已 released：同参数（revision/操作者一致）幂等命中；否则 REVIEW_LOCK_CONFLICT。
        - revision 不匹配 -> REVIEW_LOCK_REVISION_CONFLICT。
        - 操作者与 locked_by 不一致 -> REVIEW_LOCK_ACTOR_MISMATCH。
        - 解锁不删除、不覆盖旧锁；仅 status/revision 变化，锁定字段不可变。
        """
        obj_dir = self._obj_dir(task_id, item_id, _FINAL_LOCKS_DIR)
        path = obj_dir / f"{lock_id}.json"

        def _cleanup():
            _cleanup_empty_chain(obj_dir, self.root)

        with self._lock_context(task_id, item_id, on_release=_cleanup):
            current = self._parse_fact(path, ManualFinalLock, ERR_FINAL_LOCK_CORRUPTED)
            if current is None:
                raise ReviewFactStoreError(ERR_FINAL_LOCK_NOT_FOUND, retryable=False)
            if current.status == "released":
                if (
                    expected_revision == current.revision
                    and actor.actor_type == current.locked_by.actor_type
                    and actor.actor_id == current.locked_by.actor_id
                ):
                    return ("idempotent_hit", current)
                raise ReviewFactStoreError(ERR_FINAL_LOCK_CONFLICT, retryable=False)
            if current.revision != expected_revision:
                raise ReviewFactStoreError(ERR_FINAL_LOCK_REVISION_CONFLICT, retryable=False)
            if not (
                actor.actor_type == current.locked_by.actor_type
                and actor.actor_id == current.locked_by.actor_id
            ):
                raise ReviewFactStoreError(ERR_FINAL_LOCK_ACTOR_MISMATCH, retryable=False)
            released = ManualFinalLock.model_validate(
                current.model_copy(update={"status": "released", "revision": current.revision + 1}).model_dump(mode="json"))
            try:
                _atomic_write_bytes(path, _canonical_bytes(released.model_dump(mode="json")))
            except OSError as exc:
                raise ReviewFactStoreError(ERR_FINAL_LOCK_WRITE_FAILED, retryable=True) from exc
            try:
                self._append_lock_event(task_id, item_id, released, "FINAL_RESULT_UNLOCKED", occurred_at, actor)
            except OSError as exc:
                try:
                    _atomic_write_bytes(path, _canonical_bytes(current.model_dump(mode="json")))
                except OSError:
                    pass
                raise ReviewFactStoreError(ERR_FINAL_LOCK_WRITE_FAILED, retryable=True) from exc
            return ("created", released)

    def _append_lock_event(
        self, task_id: str, item_id: str, lock: ManualFinalLock,
        event_type: str, occurred_at: datetime, actor,
    ) -> None:
        """追加锁事件（FINAL_RESULT_LOCKED / FINAL_RESULT_UNLOCKED）；不改变 case 投影。"""
        self._append_event(task_id, item_id, ReviewEvent(
            event_id=uuid.uuid4().hex,
            review_case_id=lock.review_case_id,
            case_revision_before=1,
            case_revision_after=1,
            event_type=event_type,
            reason_codes=[lock.reason_code],
            object_refs=[lock.lock_id, lock.adoption_id or lock.snapshot_id],
            from_status=None,
            to_status=None,
            actor=actor or lock.locked_by,
            occurred_at=occurred_at,
            event_hash=uuid.uuid4().hex * 2,
            previous_event_hash=None,
        ))

    def _check_lock_binding(self, task_id: str, item_id: str, lock: ManualFinalLock) -> None:
        case = self.get_review_case(task_id, item_id, lock.review_case_id)
        if case is None:
            raise ReviewFactStoreError(ERR_CASE_NOT_FOUND, retryable=False)
        if case.task_id != task_id or case.item_id != item_id:
            raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
        decision = self.get_review_decision(task_id, item_id, lock.decision_id)
        if decision is None:
            raise ReviewFactStoreError(ERR_DECISION_NOT_FOUND, retryable=False)
        if decision.review_case_id != lock.review_case_id:
            raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)

    def _adoption_locked(self, task_id: str, item_id: str, adoption: ResultAdoption) -> bool:
        """该 adoption 是否被当前 active 人工最终锁锁定（契约 §3.4 第 2 条）。"""
        lock = self.get_active_final_lock(task_id, item_id)
        if lock is None:
            return False
        if lock.adoption_id == adoption.adoption_id:
            return True
        if lock.snapshot_id is not None and lock.snapshot_id == adoption.snapshot_id:
            return True
        return False

    def _validate_write_scope(self, scope: Any) -> None:
        """新写入强制提供批次隔离字段（11F-1a 契约 §4.2/§4.3）：
        - batch_id 必须显式提供（不得使用旧数据兼容标记 "legacy"）。
        - stream_id 允许 "default"（非支线批次）。
        """
        if scope.batch_id == "legacy":
            raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)

    # ---------------- ManualAdjustment ----------------

    def create_manual_adjustment(
        self, task_id: str, item_id: str, adjustment: ManualAdjustment, *, actor=None,
    ) -> Tuple[str, ManualAdjustment]:
        """创建 ManualAdjustment 事实（create-only 幂等 + MANUAL_ADJUSTMENT_LINKED 事件）。

        绑定校验：授权决定必须存在且类型为 apply_manual_adjustment；决定与调分引用同一 case；
        base_snapshot 存在（attempt_store 注入时）；调分不覆盖原快照（模型层已校验）。
        """
        self._validate_id(adjustment.adjustment_id)
        self._check_adjustment_binding(task_id, item_id, adjustment)
        return self._write_fact(
            task_id, item_id, _ADJUSTMENTS_DIR, adjustment.adjustment_id, adjustment, ManualAdjustment,
            conflict_code=ERR_ADJUSTMENT_CONFLICT,
            corrupted_code=ERR_ADJUSTMENT_CORRUPTED,
            event_type="MANUAL_ADJUSTMENT_LINKED",
            occurred_at=adjustment.adjusted_at,
            actor=actor or adjustment.adjusted_by,
        )

    def get_manual_adjustment(self, task_id: str, item_id: str, adjustment_id: str) -> Optional[ManualAdjustment]:
        return self._parse_fact(
            self._fact_path(task_id, item_id, _ADJUSTMENTS_DIR, adjustment_id),
            ManualAdjustment, ERR_ADJUSTMENT_CORRUPTED,
        )

    def delete_manual_adjustment(self, task_id: str, item_id: str, adjustment_id: str) -> None:
        """11F-3b：回滚清理（best-effort，仅用于写失败后的 cleanup）。"""
        path = self._fact_path(task_id, item_id, _ADJUSTMENTS_DIR, adjustment_id)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        obj_dir = self._obj_dir(task_id, item_id, _ADJUSTMENTS_DIR)
        _cleanup_empty_chain(obj_dir, self.root)

    def list_manual_adjustments(self, task_id: str, item_id: str) -> List[ManualAdjustment]:
        return self._list_facts(task_id, item_id, _ADJUSTMENTS_DIR, ManualAdjustment, ERR_ADJUSTMENT_CORRUPTED)

    # ---------------- ManualAdjustmentOperation（11F-3b） ----------------

    _OPERATIONS_DIR = "operations"

    def create_operation(
        self, task_id: str, item_id: str, op: ManualAdjustmentOperation,
    ) -> Tuple[str, ManualAdjustmentOperation]:
        """创建 operation 事务日志（create-only 幂等；同 ID 同 payload_hash 幂等命中）。"""
        return self._write_fact(
            task_id, item_id, self._OPERATIONS_DIR, op.operation_id, op, ManualAdjustmentOperation,
            conflict_code=ERR_CASE_CONFLICT,
            corrupted_code=ERR_CASE_CORRUPTED,
            event_type="OPERATION_CREATED",
            occurred_at=op.created_at,
            actor={"actor_type": "system", "actor_id": "local-reviewer"},
        )

    def get_operation(
        self, task_id: str, item_id: str, operation_id: str,
    ) -> Optional[ManualAdjustmentOperation]:
        return self._parse_fact(
            self._fact_path(task_id, item_id, self._OPERATIONS_DIR, operation_id),
            ManualAdjustmentOperation, ERR_CASE_CORRUPTED,
        )

    def advance_operation(
        self, task_id: str, item_id: str, op: ManualAdjustmentOperation,
    ) -> ManualAdjustmentOperation:
        """推进 operation 到新 stage（CAS：终态只允许幂等命中，不允许覆盖）。"""
        path = self._fact_path(task_id, item_id, self._OPERATIONS_DIR, op.operation_id)
        obj_dir = self._obj_dir(task_id, item_id, self._OPERATIONS_DIR)

        def _cleanup():
            _cleanup_empty_chain(obj_dir, self.root)

        with self._lock_context(task_id, item_id, on_release=_cleanup):
            current = self._parse_fact(path, ManualAdjustmentOperation, ERR_CASE_CORRUPTED)
            if current is None:
                raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
            if current.stage == op.stage:
                return current  # 幂等
            # 终态不允许覆盖
            if current.stage in ("committed", "failed"):
                raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
            # 合法转换
            valid = {
                "prepared": {"committing"},
                "committing": {"committed", "failed"},
            }
            allowed = valid.get(current.stage, set())
            if op.stage not in allowed:
                raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
            canonical = _canonical_bytes(op.model_dump(mode="json"))
            try:
                _atomic_write_bytes(path, canonical)
            except OSError as exc:
                raise ReviewFactStoreError(ERR_WRITE_FAILED, retryable=True) from exc
            return self._parse_fact(path, ManualAdjustmentOperation, ERR_CASE_CORRUPTED)

    def mark_operation_failed(
        self, task_id: str, item_id: str, operation_id: str,
        error_code: str, failed_at_stage: str,
    ) -> ManualAdjustmentOperation:
        """标记 operation 为 failed 终态。"""
        op = self.get_operation(task_id, item_id, operation_id)
        if op is None:
            raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
        if op.stage == "failed" and op.error_code == error_code:
            return op  # 幂等
        if op.stage == "committed":
            raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
        from datetime import datetime, timezone
        failed_op = op.model_copy(update={
            "stage": "failed",
            "error_code": error_code,
            "failed_at_stage": failed_at_stage,
            "updated_at": datetime.now(timezone.utc),
        })
        return self.advance_operation(task_id, item_id, failed_op)

    def list_operations(self, task_id: str, item_id: str) -> List[ManualAdjustmentOperation]:
        return self._list_facts(
            task_id, item_id, self._OPERATIONS_DIR, ManualAdjustmentOperation, ERR_CASE_CORRUPTED)

    # ---------------- 旧 adoption 恢复（11F-3b） ----------------

    def restore_adoption_status(
        self, task_id: str, item_id: str, adoption_id: str,
        *, operation_id: str, new_adoption_id: str,
    ) -> Optional[str]:
        """事务回滚：将 superseded adoption 恢复为 adopted。

        仅允许 superseded -> adopted；已是 adopted 时幂等。
        同一 scope 已存在其他 adopted 时拒绝恢复。
        """
        from datetime import datetime, timezone
        path = self._fact_path(task_id, item_id, _ADOPTIONS_DIR, adoption_id)
        obj_dir = self._obj_dir(task_id, item_id, _ADOPTIONS_DIR)

        def _cleanup():
            _cleanup_empty_chain(obj_dir, self.root)

        with self._lock_context(task_id, item_id, on_release=_cleanup):
            current = self._parse_fact(path, ResultAdoption, ERR_ADOPTION_CORRUPTED)
            if current is None:
                return "RESTORE_ADOPTION_NOT_FOUND"
            if current.status == "adopted":
                return None  # 幂等
            if current.status != "superseded":
                return "RESTORE_ADOPTION_INVALID_STATUS"
            # 检查同一 scope 是否已有其他 adopted
            active = self.get_active_adoption(task_id, item_id, scope=current.adoption_scope)
            if active is not None and active.adoption_id != adoption_id:
                return "RESTORE_ADOPTION_CONFLICT"
            restored = current.model_copy(update={
                "status": "adopted",
                "supersedes_adoption_id": None,
            })
            try:
                _atomic_write_bytes(path, _canonical_bytes(restored.model_dump(mode="json")))
            except OSError as exc:
                raise ReviewFactStoreError(ERR_WRITE_FAILED, retryable=True) from exc
            self._append_event(task_id, item_id, ReviewEvent(
                event_id=uuid.uuid4().hex,
                review_case_id=current.review_case_id or adoption_id,
                case_revision_before=1,
                case_revision_after=1,
                event_type="ROLLBACK_SUPERSEDE",
                reason_codes=[],
                object_refs=[adoption_id, operation_id, new_adoption_id],
                from_status="superseded",
                to_status="adopted",
                actor={"actor_type": "system", "actor_id": "local-reviewer"},
                occurred_at=datetime.now(timezone.utc),
                event_hash=uuid.uuid4().hex * 2,
                previous_event_hash=None,
            ))
            return None

    # ---------------- 事件（append-only 审计） ----------------

    def load_review_events(self, task_id: str, item_id: str, review_case_id: Optional[str] = None) -> List[ReviewEvent]:
        """逐行解析 events.ndjson（只读）；任一损坏显式失败（REVIEW_EVENT_CORRUPTED），不静默忽略。

        行结构 = ReviewEvent dict + sequence；sequence 必须从 1 连续递增。
        """
        path = self._events_path(task_id, item_id)
        events: List[ReviewEvent] = []
        if not path.exists():
            return events
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            seq = 0
            for line in lines:
                if not line.strip():
                    continue
                seq += 1
                row = _strict_json_loads(line)
                if not isinstance(row, dict) or "sequence" not in row or "event" not in row:
                    raise ValueError("invalid review event row shape")
                if row["sequence"] != seq:
                    raise ValueError("event sequence discontinuity")
                ev = ReviewEvent.model_validate(row["event"])
                events.append(ev)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ReviewFactStoreError(ERR_EVENT_CORRUPTED, retryable=False) from exc
        if review_case_id is None:
            return events
        return [e for e in events if e.review_case_id == review_case_id]

    def _append_event(self, task_id: str, item_id: str, event: ReviewEvent) -> None:
        """追加事件行；校验 revision 连续性与 previous_event_hash 链（同 case 前序事件）。"""
        if event.event_type in _CASE_REVISION_EVENTS or event.event_type == "CASE_OPENED":
            case = self.get_review_case(task_id, item_id, event.review_case_id)
            if case is None and event.event_type != "CASE_OPENED":
                raise ReviewFactStoreError(ERR_CASE_NOT_FOUND, retryable=False)
            if event.event_type != "CASE_OPENED":
                if event.case_revision_after != case.current_revision:
                    raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
        events = self.load_review_events(task_id, item_id)
        previous = None
        for e in reversed(events):
            if e.review_case_id == event.review_case_id:
                previous = e
                break
        prev_hash = previous.event_hash if previous is not None else None
        # 用确定性内容重算事件哈希（占位替换；previous_event_hash 链真实维护）
        event = event.model_copy(update={
            "previous_event_hash": prev_hash,
            "event_hash": _event_hash_for(event, prev_hash),
        })
        sequence = len(events) + 1
        row = {"sequence": sequence, "event": event.model_dump(mode="json")}
        _append_ndjson_bytes(
            self._events_path(task_id, item_id),
            json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n",
        )

    # ---------------- 绑定校验 ----------------

    def _require_case(self, task_id: str, item_id: str, review_case_id: str) -> ReviewCase:
        case = self.get_review_case(task_id, item_id, review_case_id)
        if case is None:
            raise ReviewFactStoreError(ERR_CASE_NOT_FOUND, retryable=False)
        return case

    def _check_decision_binding(self, task_id: str, item_id: str, decision: ReviewDecision) -> None:
        case = self._require_case(task_id, item_id, decision.review_case_id)
        if case.task_id != task_id or case.item_id != item_id:
            raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
        # 已关闭工单不可原地篡改决定（重开才能新增决定）——11F-1a 契约 §2.3 第 3 条
        if case.status in ("cancelled", "resolved", "dismissed"):
            raise ReviewFactStoreError(ERR_DECISION_STATE_NOT_DECISIONABLE, retryable=False)
        if decision.case_revision > case.current_revision:
            raise ReviewFactStoreError(ERR_DECISION_CONFLICT, retryable=False)
        # 11F-1b：组合矩阵（模型层已拒绝，这里兜底映射稳定码）
        bad = validate_decision_reason_combo(decision.decision_type, decision.reason_codes)
        if bad is not None:
            raise ReviewFactStoreError(ERR_DECISION_REASON_MISMATCH, retryable=False)
        # 11F-1b：强制 target 字段（11F-1a 契约 §2.4 表）
        required = DECISION_REQUIRED_TARGETS.get(decision.decision_type, ())
        for field in required:
            if getattr(decision, field) is None:
                raise ReviewFactStoreError(ERR_DECISION_TARGET_REQUIRED, retryable=False)
        if decision.target_snapshot_id is not None and self.attempt_store is not None:
            snap = self.attempt_store.get_snapshot(task_id, item_id, decision.target_snapshot_id)
            if snap is None:
                raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
        # 11F-1b：supersedes 关系（11F-1a 契约 §2.3 第 5 条）
        if decision.supersedes_decision_id is not None:
            superseded = self.get_review_decision(task_id, item_id, decision.supersedes_decision_id)
            if superseded is None:
                raise ReviewFactStoreError(ERR_DECISION_SUPERSEDES_NOT_FOUND, retryable=False)
            if superseded.review_case_id != decision.review_case_id:
                raise ReviewFactStoreError(ERR_DECISION_SUPERSEDES_SCOPE_MISMATCH, retryable=False)

    def _check_adoption_binding(self, task_id: str, item_id: str, adoption: ResultAdoption) -> None:
        if adoption.decision_id is not None:
            decision = self.get_review_decision(task_id, item_id, adoption.decision_id)
            if decision is None:
                raise ReviewFactStoreError(ERR_DECISION_NOT_FOUND, retryable=False)
            if adoption.review_case_id is not None and decision.review_case_id != adoption.review_case_id:
                raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
        if adoption.review_case_id is not None:
            self._require_case(task_id, item_id, adoption.review_case_id)
        if self.attempt_store is not None:
            snap = self.attempt_store.get_snapshot(task_id, item_id, adoption.snapshot_id)
            if snap is None:
                raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
            if adoption.attempt_id is not None and snap.attempt_id != adoption.attempt_id:
                raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
            val = self.attempt_store.get_validation(task_id, item_id, adoption.validation_id)
            if val is None:
                raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
            if val.snapshot_id != adoption.snapshot_id:
                raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)

    def _check_auto_adoption_blocked(self, task_id: str, item_id: str, adoption: ResultAdoption) -> None:
        """must_review / 开放阻断工单 -> RESULT_ADOPTION_BLOCKED（禁止自动采用）。"""
        if adoption.decided_by.actor_type not in ("system", "controller", "orchestrator"):
            return  # 人工采用不受自动阻断检查约束（但受人工锁定保护约束）
        for case in self.list_open_cases(task_id, item_id):
            if case.blocks_auto_adoption and case.item_id == item_id:
                raise ReviewFactStoreError(ERR_ADOPTION_BLOCKED, retryable=False)

    def _check_adjustment_binding(self, task_id: str, item_id: str, adjustment: ManualAdjustment) -> None:
        decision = self.get_review_decision(task_id, item_id, adjustment.decision_id)
        if decision is None:
            raise ReviewFactStoreError(ERR_DECISION_NOT_FOUND, retryable=False)
        if decision.decision_type != "apply_manual_adjustment":
            raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
        if decision.review_case_id != adjustment.review_case_id:
            raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
        self._require_case(task_id, item_id, adjustment.review_case_id)
        if self.attempt_store is not None:
            base = self.attempt_store.get_snapshot(task_id, item_id, adjustment.base_snapshot_id)
            if base is None:
                raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
            if adjustment.base_attempt_id is not None and base.attempt_id != adjustment.base_attempt_id:
                raise ReviewFactStoreError(ERR_BINDING_MISMATCH, retryable=False)

    # ---------------- 内部写入（create-only 幂等 + 原子写 + 事件） ----------------

    def _write_fact(
        self,
        task_id: str,
        item_id: str,
        rel_dir: str,
        object_id: str,
        obj: Any,
        model_cls: Any,
        *,
        conflict_code: str,
        corrupted_code: str,
        event_type: str,
        occurred_at: datetime,
        actor,
    ) -> Tuple[str, Any]:
        self._validate_id(task_id)
        self._validate_id(item_id)
        self._validate_id(object_id)
        obj_dir = self._obj_dir(task_id, item_id, rel_dir)
        path = obj_dir / f"{object_id}.json"
        canonical = _canonical_bytes(obj.model_dump(mode="json"))

        def _cleanup():
            _cleanup_empty_chain(obj_dir, self.root)

        with self._lock_context(task_id, item_id, on_release=_cleanup):
            if path.exists():
                if path.read_bytes() == canonical:
                    return ("idempotent_hit", self._parse_fact(path, model_cls, corrupted_code))
                raise ReviewFactStoreError(conflict_code, retryable=False)
            try:
                _atomic_write_bytes(path, canonical)
            except OSError as exc:
                raise ReviewFactStoreError(ERR_WRITE_FAILED, retryable=True) from exc
            try:
                self._append_event(task_id, item_id, ReviewEvent(
                    event_id=uuid.uuid4().hex,
                    review_case_id=getattr(obj, "review_case_id", None) or object_id,
                    case_revision_before=1,
                    case_revision_after=1,
                    event_type=event_type,
                    reason_codes=[],
                    object_refs=[object_id],
                    from_status=None,
                    to_status=None,
                    actor=actor,
                    occurred_at=occurred_at,
                    event_hash=uuid.uuid4().hex * 2,
                    previous_event_hash=None,
                ))
            except OSError as exc:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise ReviewFactStoreError(ERR_WRITE_FAILED, retryable=True) from exc
            return ("created", self._parse_fact(path, model_cls, corrupted_code))


# ---------------------------------------------------------------- 工具


def _system_actor():
    """默认系统 actor（创建工单时的 system 操作者；稳定非敏感）。"""
    from models.score_attempt import ActorRef
    return ActorRef(actor_type="system", actor_id="review-case-store")


def _event_hash_for(event: ReviewEvent, previous_event_hash: Optional[str]) -> str:
    """确定性事件哈希：规范化事件载荷（不含哈希字段）的 sha256 十六进制。"""
    import hashlib
    payload = event.model_dump(mode="json", exclude={"event_hash"})
    payload["previous_event_hash"] = previous_event_hash
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
