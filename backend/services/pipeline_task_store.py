"""
PipelineTask 文件存储、原子写与崩溃恢复（Phase 11C-2b）。

冻结契约：Phase11A-2b（PipelineTask v1）+ Phase11C-1 最小实施设计
（fix-1 事务协议：tmp/transactions 四状态、崩溃恢复分支、tmp 清理规则）。

本模块只实现文件存储基础设施：独立运行目录、只读加载、原子写、NDJSON、
CAS revision、单机锁、事务目录、普通异常回滚、启动时崩溃恢复、事件保护。
不实现任务创建业务选择、EvidencePackage 纳入、状态转换、pause/resume 决策、
API、前端或真实评分。

安全边界：
- 运行目录 = DATA_DIR/evidence-runtime/pipeline，与旧 data/tasks 完全隔离。
- 所有路径由受校验 UUID 和固定目录组合生成；拒绝绝对路径、..、反斜杠、盘符、
  空字节和目录逃逸。
- 不返回材料正文、绝对路径或异常堆栈。
- 损坏文件不得按"不存在"处理；不自动修复、覆盖或删除损坏文件。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from models.pipeline_task import (
    PipelineEvent,
    PipelineItem,
    PipelineTask,
    PipelineTransaction,
    ResumeDecision,
    ResumeRequest,
    canonical_json_bytes,
    sha256_canonical,
)

# ---------------------------------------------------------------- 错误码

ERR_REVISION_CONFLICT = "SIDECAR_REVISION_CONFLICT"
ERR_TASK_CORRUPTED = "SIDECAR_TASK_CORRUPTED"
ERR_LOCK_CONFLICT = "SIDECAR_LOCK_CONFLICT"
ERR_EVENT_SEQUENCE_CONFLICT = "SIDECAR_EVENT_SEQUENCE_CONFLICT"
ERR_EVENT_IDEMPOTENCY_CONFLICT = "SIDECAR_EVENT_IDEMPOTENCY_CONFLICT"
ERR_TRANSACTION_ERROR = "SIDECAR_TRANSACTION_ERROR"
ERR_NOT_FOUND = "SIDECAR_TASK_NOT_FOUND"


class PipelineStoreError(Exception):
    """存储层显式错误（非敏感，message_key 固定）。"""

    def __init__(self, error_code: str, message_key: str, retryable: bool = False) -> None:
        super().__init__(message_key)
        self.error_code = error_code
        self.message_key = message_key
        self.retryable = retryable


# ---------------------------------------------------------------- 写入原语（模块级，便于故障注入）


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """同目录临时文件 + flush/fsync + os.replace 原子发布。失败不覆盖旧文件。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.tmp.{uuid.uuid4().hex[:8]}"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _atomic_write(target: Path, obj: Any) -> None:
    _atomic_write_bytes(target, canonical_json_bytes(obj))


def _append_ndjson_bytes(path: Path, line: bytes) -> None:
    """NDJSON 追加 + flush/fsync。失败必须上抛（不静默）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


def _strict_json_loads(text: str) -> Any:
    """禁止 NaN/Infinity 的严格 JSON 解析。"""
    def _reject(constant: str) -> Any:
        raise ValueError(f"non-finite constant: {constant}")

    return json.loads(text, parse_constant=_reject)


# ---------------------------------------------------------------- 进程内锁（跨实例共享）

_PROCESS_LOCKS: Dict[str, threading.Lock] = {}
_PROCESS_GUARD = threading.Lock()


def _process_lock_for(key: str) -> threading.Lock:
    with _PROCESS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.Lock())


# ---------------------------------------------------------------- 文件锁

class _TaskFileLock:
    """单机文件锁（O_CREAT|O_EXCL）。锁元数据写入随机非敏感 owner token（不写账号/主机名/路径/token）。"""

    def __init__(self, lock_path: Path, timeout_ms: int = 5000) -> None:
        self.lock_path = Path(lock_path)
        self.timeout_ms = timeout_ms
        self._acquired = False
        self._owner_token = uuid.uuid4().hex

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() * 1000 + self.timeout_ms
        while True:
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, self._owner_token.encode("utf-8"))
                os.close(fd)
                self._acquired = True
                return
            except FileExistsError:
                if time.monotonic() * 1000 >= deadline:
                    raise PipelineStoreError(ERR_LOCK_CONFLICT, "SIDECAR_LOCK_CONFLICT", retryable=True)
                time.sleep(0.05)

    def release(self) -> None:
        """释放前读取并确认 owner token 与本实例一致；不一致不得删除其他调用持有的锁。"""
        if not self._acquired:
            return
        try:
            current = self.lock_path.read_text(encoding="utf-8").strip()
        except OSError:
            current = None
        if current != self._owner_token:
            # 不是本实例持有的锁：不得删除，保留现场并明确冲突
            raise PipelineStoreError(ERR_LOCK_CONFLICT, "SIDECAR_LOCK_CONFLICT", retryable=True)
        self.lock_path.unlink()
        self._acquired = False


# ---------------------------------------------------------------- 存储服务

_REL_DIRS = ("items", "resume-requests", "resume-decisions", "locks", "tmp")


class PipelineTaskStore:
    """PipelineTask 文件存储基础设施（单机、单实例 MVP）。"""

    def __init__(self, pipeline_root: Path, lock_timeout_ms: int = 5000) -> None:
        self.root = Path(pipeline_root)
        self.tasks_dir = self.root / "tasks"
        self.backups_dir = self.root / "backups"
        self.lock_timeout_ms = lock_timeout_ms
        for d in (self.tasks_dir, self.backups_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ---------------- 路径构建（全部由受校验 UUID 组合，防目录逃逸） ----------------

    def _validate_id(self, value: str) -> str:
        import re as _re

        if not _re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", value):
            raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_INVALID_ID", retryable=False)
        return value

    def _task_dir(self, task_id: str, create: bool = False) -> Path:
        """路径计算与目录创建分离：只读查询不 mkdir；仅明确写入/事务初始化时 create=True。"""
        self._validate_id(task_id)
        d = self.tasks_dir / task_id
        if create:
            d.mkdir(parents=True, exist_ok=True)
        return d

    def _item_path(self, task_id: str, item_id: str) -> Path:
        self._validate_id(item_id)
        return self._task_dir(task_id) / "items" / f"{item_id}.json"

    def _events_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "events.ndjson"

    def _resume_request_path(self, task_id: str, request_id: str) -> Path:
        self._validate_id(request_id)
        return self._task_dir(task_id) / "resume-requests" / f"{request_id}.json"

    def _resume_decision_path(self, task_id: str, request_id: str) -> Path:
        self._validate_id(request_id)
        return self._task_dir(task_id) / "resume-decisions" / f"{request_id}.json"

    def _lock_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "locks" / "task.lock"

    def _transactions_dir(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "tmp" / "transactions"

    def _txn_dir(self, task_id: str, txn_id: str) -> Path:
        self._validate_id(txn_id)
        return self._transactions_dir(task_id) / txn_id

    # ---------------- 只读加载 ----------------

    def _read_snapshot(self, path: Path, model_cls: Any) -> Any:
        """读取权威快照：不存在 -> None；损坏 -> TASK_CORRUPTED（不按不存在处理）。"""
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8")
            obj = _strict_json_loads(text)
            return model_cls.model_validate(obj)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False) from exc

    def load_task(self, task_id: str) -> Optional[PipelineTask]:
        return self._read_snapshot(self._task_dir(task_id) / "task.json", PipelineTask)

    def load_item(self, task_id: str, item_id: str) -> Optional[PipelineItem]:
        return self._read_snapshot(self._item_path(task_id, item_id), PipelineItem)

    def load_event_log(self, task_id: str) -> List[PipelineEvent]:
        """逐行解析 events.ndjson；任一损坏显式失败（尾部损坏不得静默忽略）。"""
        path = self._events_path(task_id)
        events: List[PipelineEvent] = []
        if not path.exists():
            return events
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                obj = _strict_json_loads(line)
                events.append(PipelineEvent.model_validate(obj))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False) from exc
        return events

    def load_resume_request(self, task_id: str, request_id: str) -> Optional[ResumeRequest]:
        return self._read_snapshot(self._resume_request_path(task_id, request_id), ResumeRequest)

    def load_resume_decision(self, task_id: str, request_id: str) -> Optional[ResumeDecision]:
        return self._read_snapshot(self._resume_decision_path(task_id, request_id), ResumeDecision)

    # ---------------- 查询（只读、稳定排序、分页、零副作用） ----------------

    def list_tasks(
        self,
        status_filter: Optional[str] = None,
        offset: int = 0,
        limit: int = 50,
    ) -> List[PipelineTask]:
        """列出任务：task_id 稳定排序、最小状态过滤、分页；损坏显式 corrupted，不自动修复。
        查询不创建目录、不修改 revision/事件/时间戳。"""
        tasks_dir = self.tasks_dir
        if not tasks_dir.exists():
            return []
        found: List[PipelineTask] = []
        for entry in sorted(tasks_dir.iterdir(), key=lambda p: p.name):
            if not entry.is_dir():
                continue
            task = self.load_task(entry.name)
            if task is None:
                continue
            if status_filter is not None and task.status != status_filter:
                continue
            found.append(task)
        return found[offset: offset + limit]

    def list_items(self, task_id: str) -> List[PipelineItem]:
        """列出任务全部 item：item_id 稳定排序。只读零副作用。"""
        items_dir = self._task_dir(task_id) / "items"
        found: List[PipelineItem] = []
        if not items_dir.exists():
            return found
        for p in sorted(items_dir.iterdir(), key=lambda p: p.name):
            if not p.is_file():
                continue
            item = self.load_item(task_id, p.name[: -len(".json")])
            if item is not None:
                found.append(item)
        return found

    def list_resume_requests(self, task_id: str) -> List[ResumeRequest]:
        """列出全部恢复请求：request_id 稳定排序。只读零副作用。"""
        d = self._task_dir(task_id) / "resume-requests"
        found: List[ResumeRequest] = []
        if not d.exists():
            return found
        for p in sorted(d.iterdir(), key=lambda p: p.name):
            if not p.is_file():
                continue
            obj = self.load_resume_request(task_id, p.name[: -len(".json")])
            if obj is not None:
                found.append(obj)
        return found

    def list_resume_decisions(self, task_id: str) -> List[ResumeDecision]:
        """列出全部恢复决定：request_id 稳定排序。只读零副作用。"""
        d = self._task_dir(task_id) / "resume-decisions"
        found: List[ResumeDecision] = []
        if not d.exists():
            return found
        for p in sorted(d.iterdir(), key=lambda p: p.name):
            if not p.is_file():
                continue
            obj = self.load_resume_decision(task_id, p.name[: -len(".json")])
            if obj is not None:
                found.append(obj)
        return found

    # ---------------- Resume 原子写（幂等/冲突，不覆盖） ----------------

    def _write_resume_object(
        self, task_id: str, rel_dir: str, request_id: str, obj: Any,
    ) -> bool:
        """原子写 resume 对象：同 request_id 同内容幂等命中（True 表示新建/更新，False 幂等命中）；
        同 id 不同内容显式冲突；不覆盖既有对象。"""
        self._validate_id(request_id)
        d = self._task_dir(task_id, create=True) / rel_dir
        path = d / f"{request_id}.json"
        canonical = canonical_json_bytes(obj.model_dump(mode="json"))
        existing_bytes = path.read_bytes() if path.exists() else None
        if existing_bytes is not None:
            if existing_bytes == canonical:
                return False  # 幂等命中：内容一致，不重复写
            raise PipelineStoreError(
                ERR_EVENT_IDEMPOTENCY_CONFLICT, "SIDECAR_EVENT_IDEMPOTENCY_CONFLICT", retryable=False
            )
        try:
            d.mkdir(parents=True, exist_ok=True)
            _atomic_write_bytes(path, canonical)
        except OSError as exc:
            raise PipelineStoreError(ERR_TRANSACTION_ERROR, "SIDECAR_TRANSACTION_ERROR", retryable=True) from exc
        return True

    def write_resume_request(self, task_id: str, request: ResumeRequest) -> bool:
        """写入恢复请求（原子）。请求 id 冲突/幂等语义见 _write_resume_object。"""
        return self._write_resume_object(task_id, "resume-requests", request.request_id, request)

    def write_resume_decision(self, task_id: str, decision: ResumeDecision) -> bool:
        """写入恢复决定（原子）。决定必须晚于请求（请求先存在）；否则显式失败。"""
        if self.load_resume_request(task_id, decision.request_id) is None:
            raise PipelineStoreError(ERR_TRANSACTION_ERROR, "SIDECAR_REQUEST_NOT_FOUND", retryable=False)
        return self._write_resume_object(task_id, "resume-decisions", decision.request_id, decision)

    # ---------------- CAS 快照发布 ----------------

    def _current_revision(self, task_id: str, snapshot_path: Path, revision_key: str = "revision") -> Optional[int]:
        if not snapshot_path.exists():
            return None
        try:
            obj = _strict_json_loads(snapshot_path.read_text(encoding="utf-8"))
            rev = obj.get(revision_key) if isinstance(obj, dict) else None
            if isinstance(rev, int) and rev >= 1:
                return rev
            raise ValueError("missing revision")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False) from exc

    def _cas_check(self, expected_revision: Optional[int], current: Optional[int]) -> None:
        """CAS 不存在语义（冻结）：
        - expected_revision=None：只允许首次创建，目标已存在则冲突。
        - expected_revision=N：目标必须存在且当前 revision==N。
        - 目标不存在或 revision 不一致均返回 SIDECAR_REVISION_CONFLICT。
        """
        if expected_revision is None:
            if current is not None:
                raise PipelineStoreError(ERR_REVISION_CONFLICT, "SIDECAR_REVISION_CONFLICT", retryable=False)
        else:
            if current != expected_revision:
                raise PipelineStoreError(ERR_REVISION_CONFLICT, "SIDECAR_REVISION_CONFLICT", retryable=False)

    def write_task_snapshot(
        self, task_id: str, task: PipelineTask, expected_revision: Optional[int] = None
    ) -> None:
        """CAS 发布 task.json。冲突零副作用；损坏显式失败。"""
        path = self._task_dir(task_id) / "task.json"
        current = self._current_revision(task_id, path)
        self._cas_check(expected_revision, current)
        _atomic_write(path, task.model_dump(mode="json"))

    def write_item_snapshot(
        self, task_id: str, item: PipelineItem, expected_revision: Optional[int] = None
    ) -> None:
        path = self._item_path(task_id, item.item_id)
        current = self._current_revision(task_id, path, revision_key="item_revision")
        self._cas_check(expected_revision, current)
        _atomic_write(path, item.model_dump(mode="json"))

    # ---------------- 事件保护 ----------------

    def _event_sequence_check(self, task_id: str, event: PipelineEvent) -> None:
        """sequence 必须等于事件日志尾部 sequence + 1（冻结：基于 old 状态，不基于已发布新 task）。"""
        events = self.load_event_log(task_id)
        last_seq = events[-1].sequence if events else 0
        if event.sequence != last_seq + 1:
            raise PipelineStoreError(
                ERR_EVENT_SEQUENCE_CONFLICT, "SIDECAR_EVENT_SEQUENCE_CONFLICT", retryable=False
            )

    def _event_idempotency_check(self, task_id: str, event: PipelineEvent) -> bool:
        """同 event_id 已存在且内容一致 -> 幂等命中（True）；内容不同 -> 显式冲突（raise）。"""
        for existing in self.load_event_log(task_id):
            if existing.event_id == event.event_id:
                if canonical_json_bytes(existing.model_dump(mode="json")) == canonical_json_bytes(event.model_dump(mode="json")):
                    return True  # 幂等命中
                raise PipelineStoreError(
                    ERR_EVENT_IDEMPOTENCY_CONFLICT, "SIDECAR_EVENT_IDEMPOTENCY_CONFLICT", retryable=False
                )
        return False

    def _append_event_bytes(self, task_id: str, event: PipelineEvent) -> None:
        """追加事件字节并 fsync；OSError 包装为显式存储错误（不静默）。
        冻结：仅事务内部使用，不存在公开路径可脱离 task 快照单独追加正式事件。"""
        try:
            _append_ndjson_bytes(self._events_path(task_id), canonical_json_bytes(event.model_dump(mode="json")) + b"\n")
        except OSError as exc:
            raise PipelineStoreError(ERR_TRANSACTION_ERROR, "SIDECAR_TRANSACTION_ERROR", retryable=True) from exc

    # 注意：无公开 append_event。所有正式事件必须通过联合事务（含 new_task）追加，
    # 保证 task.json.last_event_sequence 与事件日志始终同步。

    # ---------------- 锁 ----------------

    def _lock_context(self, task_id: str):
        lock_key = str(self._lock_path(task_id))
        plock = _process_lock_for(lock_key)
        flock = _TaskFileLock(self._lock_path(task_id), timeout_ms=self.lock_timeout_ms)
        return _LockContext(plock, flock)

    def acquire_creation_lock(
        self, idempotency_key: str, timeout_ms: Optional[int] = None,
    ) -> _LockContext:
        """按 idempotency_key 获取创建锁（11E-2a-prerequisite-impl-2-fix-1）。

        锁文件名只含合法 SHA-256 摘要（不含敏感信息）；同进程线程与独立进程共用
        同一稳定路径（进程内锁 + 文件锁）。冲突/超时抛 SIDECAR_LOCK_CONFLICT
        （retryable=True），由调用方映射为稳定可重试错误。
        """
        digest = sha256_canonical({"creation_lock": idempotency_key})
        lock_path = self.root / "locks" / f"create-{digest}.lock"
        lock_key = str(lock_path)
        plock = _process_lock_for(lock_key)
        flock = _TaskFileLock(lock_path, timeout_ms or self.lock_timeout_ms)
        return _LockContext(plock, flock)

    # ---------------- 事务 ----------------

    def begin_transaction(
        self,
        task_id: str,
        event: PipelineEvent,
        expected_task_revision: Optional[int],
        expected_item_revisions: Optional[Dict[str, int]] = None,
    ) -> str:
        """创建事务目录（prepared）：transaction.json（refs 空）+ event.json + old/ + new/。"""
        txn_id = str(uuid.uuid4())
        txn_dir = self._txn_dir(task_id, txn_id)
        (txn_dir / "old").mkdir(parents=True, exist_ok=True)
        (txn_dir / "new").mkdir(parents=True, exist_ok=True)
        transaction = PipelineTransaction(
            transaction_id=txn_id,
            task_id=task_id,
            status="prepared",
            expected_task_revision=expected_task_revision if expected_task_revision is not None else 1,
            expected_item_revisions=expected_item_revisions or {},
            target_event_sequence=event.sequence,
            old_snapshot_refs=[],
            new_snapshot_refs=[],
            event_ref=None,
            created_at=event.occurred_at,
            updated_at=event.occurred_at,
        )
        try:
            _atomic_write(txn_dir / "transaction.json", transaction.model_dump(mode="json"))
            _atomic_write(txn_dir / "event.json", event.model_dump(mode="json"))
        except OSError as exc:
            raise PipelineStoreError(ERR_TRANSACTION_ERROR, "SIDECAR_TRANSACTION_ERROR", retryable=True) from exc
        return txn_id

    def _load_transaction(self, task_id: str, txn_id: str) -> PipelineTransaction:
        txn_path = self._txn_dir(task_id, txn_id) / "transaction.json"
        if not txn_path.exists():
            raise PipelineStoreError(ERR_TRANSACTION_ERROR, "SIDECAR_TRANSACTION_ERROR", retryable=False)
        try:
            obj = _strict_json_loads(txn_path.read_text(encoding="utf-8"))
            return PipelineTransaction.model_validate(obj)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False) from exc

    def _save_transaction(self, task_id: str, txn_id: str, transaction: PipelineTransaction) -> None:
        try:
            _atomic_write(self._txn_dir(task_id, txn_id) / "transaction.json", transaction.model_dump(mode="json"))
        except OSError as exc:
            raise PipelineStoreError(ERR_TRANSACTION_ERROR, "SIDECAR_TRANSACTION_ERROR", retryable=True) from exc

    def _safe_rel_ref(self, rel_ref: str) -> Path:
        """事务引用路径收紧（冻结）：仅允许 task.json 或 items/<合法 item UUID>.json。"""
        if not isinstance(rel_ref, str) or not rel_ref or "\x00" in rel_ref or "\\" in rel_ref:
            raise PipelineStoreError(ERR_TRANSACTION_ERROR, "SIDECAR_TRANSACTION_ERROR", retryable=False)
        if rel_ref == "task.json":
            return Path(rel_ref)
        if rel_ref.startswith("items/") and rel_ref.count("/") == 1 and rel_ref.endswith(".json"):
            item_id = rel_ref[len("items/"):-len(".json")]
            self._validate_id(item_id)
            return Path(rel_ref)
        raise PipelineStoreError(ERR_TRANSACTION_ERROR, "SIDECAR_TRANSACTION_ERROR", retryable=False)

    def _txn_file(self, task_id: str, txn_id: str, subdir: str, rel_ref: str) -> Path:
        """事务目录内 old/ 或 new/ 下与 rel_ref 对应的文件路径。"""
        return self._txn_dir(task_id, txn_id) / subdir / rel_ref

    def prepare_snapshots(
        self,
        task_id: str,
        txn_id: str,
        task_snapshot: Optional[PipelineTask] = None,
        item_snapshots: Optional[List[PipelineItem]] = None,
    ) -> None:
        """完整准备写集合（锁内）：全部 old/ 备份与 new/ 字节先安全落盘，
        transaction.json 记录完整 refs（状态仍为 prepared），不发布任何正式快照。"""
        txn = self._load_transaction(task_id, txn_id)
        if txn.status != "prepared":
            raise PipelineStoreError(ERR_TRANSACTION_ERROR, "SIDECAR_TRANSACTION_ERROR", retryable=False)
        refs: List[str] = []
        try:
            if task_snapshot is not None:
                rel_ref = "task.json"
                self._stage_snapshot(task_id, txn_id, rel_ref, canonical_json_bytes(task_snapshot.model_dump(mode="json")))
                refs.append(rel_ref)
            if item_snapshots:
                for item in item_snapshots:
                    rel_ref = f"items/{item.item_id}.json"
                    self._stage_snapshot(task_id, txn_id, rel_ref, canonical_json_bytes(item.model_dump(mode="json")))
                    refs.append(rel_ref)
        except OSError as exc:
            raise PipelineStoreError(ERR_TRANSACTION_ERROR, "SIDECAR_TRANSACTION_ERROR", retryable=True) from exc
        if not refs:
            raise PipelineStoreError(ERR_TRANSACTION_ERROR, "SIDECAR_TRANSACTION_ERROR", retryable=False)
        self._save_transaction(
            task_id, txn_id,
            txn.model_copy(update={"old_snapshot_refs": list(refs), "new_snapshot_refs": list(refs)}),
        )

    def _stage_snapshot(self, task_id: str, txn_id: str, rel_ref: str, data: bytes) -> None:
        """事务目录内准备 old/ 与 new/ 字节（不发布正式快照）。"""
        safe_rel = self._safe_rel_ref(rel_ref)
        txn_dir = self._txn_dir(task_id, txn_id)
        target = self._task_dir(task_id) / rel_ref
        old_bytes = target.read_bytes() if target.exists() else None
        if old_bytes is not None:
            (txn_dir / "old" / safe_rel).parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_bytes(txn_dir / "old" / safe_rel, old_bytes)
        (txn_dir / "new" / safe_rel).parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(txn_dir / "new" / safe_rel, data)

    def _expected_revision_for_ref(self, txn: PipelineTransaction, rel_ref: str) -> Optional[int]:
        if rel_ref == "task.json":
            return txn.expected_task_revision
        item_id = rel_ref[len("items/"):-len(".json")]
        return txn.expected_item_revisions.get(item_id)

    def _revision_key_for_ref(self, rel_ref: str) -> str:
        return "revision" if rel_ref == "task.json" else "item_revision"

    def publish_prepared_snapshots(self, task_id: str, txn_id: str) -> None:
        """一次性发布全部 prepared 快照（锁内）：CAS 全部校验通过后逐个 os.replace；
        全部成功后才进入 snapshots_published。发布中途崩溃时状态仍为 prepared，恢复负责回滚。"""
        txn = self._load_transaction(task_id, txn_id)
        if txn.status != "prepared":
            raise PipelineStoreError(ERR_TRANSACTION_ERROR, "SIDECAR_TRANSACTION_ERROR", retryable=False)
        txn_dir = self._txn_dir(task_id, txn_id)
        # 1. CAS 一次性校验（发布前）：old 有备份 -> 期望 revision 匹配；old 无备份（首次创建）-> 目标必须仍不存在
        for rel_ref in txn.new_snapshot_refs:
            safe_rel = self._safe_rel_ref(rel_ref)
            target = self._task_dir(task_id) / rel_ref
            current = self._current_revision(task_id, target, revision_key=self._revision_key_for_ref(rel_ref))
            if (txn_dir / "old" / safe_rel).exists():
                self._cas_check(self._expected_revision_for_ref(txn, rel_ref), current)
            else:
                if current is not None:
                    raise PipelineStoreError(ERR_REVISION_CONFLICT, "SIDECAR_REVISION_CONFLICT", retryable=False)
        # 2. 全部发布
        try:
            for rel_ref in txn.new_snapshot_refs:
                safe_rel = self._safe_rel_ref(rel_ref)
                target = self._task_dir(task_id) / rel_ref
                _atomic_write_bytes(target, (txn_dir / "new" / safe_rel).read_bytes())
        except OSError as exc:
            raise PipelineStoreError(ERR_TRANSACTION_ERROR, "SIDECAR_TRANSACTION_ERROR", retryable=True) from exc
        # 3. 全部成功后标记
        self._save_transaction(
            task_id, txn_id,
            txn.model_copy(update={"status": "snapshots_published", "updated_at": txn.updated_at}),
        )

    def append_transaction_event(self, task_id: str, txn_id: str) -> None:
        """读取 event.json -> 事件保护校验（事务必须含 task 快照、sequence/幂等/
        last_event_sequence 同步）-> 追加 events.ndjson -> event_appended。"""
        txn = self._load_transaction(task_id, txn_id)
        txn_dir = self._txn_dir(task_id, txn_id)
        # 冻结：所有正式事件必须通过含 new_task 的联合事务追加；不允许"只有 item+event"的事务
        if "task.json" not in txn.new_snapshot_refs:
            raise PipelineStoreError(ERR_TRANSACTION_ERROR, "SIDECAR_TRANSACTION_ERROR", retryable=False)
        try:
            event_bytes = (txn_dir / "event.json").read_bytes()
            obj = _strict_json_loads(event_bytes.decode("utf-8"))
            event = PipelineEvent.model_validate(obj)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False) from exc
        # last_event_sequence 同步：new task 的 last_event_sequence 必须等于事件 sequence
        try:
            new_task_bytes = (txn_dir / "new" / "task.json").read_bytes()
            new_task = PipelineTask.model_validate(_strict_json_loads(new_task_bytes.decode("utf-8")))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False) from exc
        if new_task.last_event_sequence != event.sequence:
            raise PipelineStoreError(ERR_EVENT_SEQUENCE_CONFLICT, "SIDECAR_EVENT_SEQUENCE_CONFLICT", retryable=False)
        # 幂等命中优先：同 event_id 已存在且内容一致 -> 不重复追加（sequence 已消费场景）
        if self._event_idempotency_check(task_id, event):
            self._save_transaction(
                task_id, txn_id,
                txn.model_copy(update={"status": "event_appended", "event_ref": "events.ndjson",
                                       "updated_at": event.occurred_at}),
            )
            return
        self._event_sequence_check(task_id, event)
        self._append_event_bytes(task_id, event)
        self._save_transaction(
            task_id, txn_id,
            txn.model_copy(update={"status": "event_appended", "event_ref": "events.ndjson",
                                   "updated_at": event.occurred_at}),
        )

    def _event_already_appended(self, task_id: str, txn_id: str) -> bool:
        """events.ndjson 尾部事件与 event.json 完全一致（canonical bytes）即视为已追加。"""
        txn_dir = self._txn_dir(task_id, txn_id)
        event_path = txn_dir / "event.json"
        if not event_path.exists():
            return False
        try:
            event_bytes = event_path.read_bytes()
            events = self.load_event_log(task_id)
            if not events:
                return False
            return canonical_json_bytes(events[-1].model_dump(mode="json")) == event_bytes
        except (OSError, ValueError, json.JSONDecodeError):
            return False

    def _verify_committable(self, task_id: str, txn: PipelineTransaction) -> PipelineEvent:
        """提交前完整核验（冻结）：事件内容完全一致 + 快照与 new 一致 + last_event_sequence 同步。
        任一无法证明一致 -> SIDECAR_TASK_CORRUPTED。"""
        txn_dir = self._txn_dir(task_id, txn.transaction_id)
        event_path = txn_dir / "event.json"
        if not event_path.exists():
            raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False)
        try:
            event_bytes = event_path.read_bytes()
            event = PipelineEvent.model_validate(_strict_json_loads(event_bytes.decode("utf-8")))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False) from exc
        events = self.load_event_log(task_id)
        if not events:
            raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False)
        tail = events[-1]
        if tail.sequence != txn.target_event_sequence:
            raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False)
        if canonical_json_bytes(tail.model_dump(mode="json")) != event_bytes:
            raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False)
        # 正式快照字节与 new/ 一致
        for rel_ref in txn.new_snapshot_refs:
            safe_rel = self._safe_rel_ref(rel_ref)
            new_path = txn_dir / "new" / safe_rel
            target = self._task_dir(task_id) / rel_ref
            if not new_path.exists() or not target.exists():
                raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False)
            if target.read_bytes() != new_path.read_bytes():
                raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False)
        # last_event_sequence 同步
        if "task.json" in txn.new_snapshot_refs:
            task = self.load_task(task_id)
            if task is None or task.last_event_sequence != tail.sequence:
                raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False)
            # task_id 一致性：事务 task_id == task.json/event.json/所有 item.task_id
            if task.task_id != task_id:
                raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False)
            if tail.task_id != task_id:
                raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False)
            # task.item_index 与全部 item 正式快照逐项对账 + items 目录无未登记快照
            self._verify_item_index_reconciliation(task, task_id)
        return event

    def _verify_item_index_reconciliation(self, task: PipelineTask, task_id: str) -> None:
        """task.item_index 与 items/*.json 逐项对账（冻结）：
        item_id/package_id/package_revision/status/current_stage/input_fingerprint/
        item_revision/evidence_level 一致；
        item 文件必须存在可解析；items 目录不得存在未登记 item 快照。
        任一不一致 -> SIDECAR_TASK_CORRUPTED，不得提交或清理事务。"""
        items_dir = self._task_dir(task_id) / "items"
        expected_refs = set()
        for entry in task.item_index:
            item = self.load_item(task_id, entry.item_id)
            if item is None:
                raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False)
            if item.task_id != task_id:
                raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False)
            pairs = (
                ("package_id", item.package_id, entry.package_id),
                ("package_revision", item.package_revision, entry.package_revision),
                ("status", item.status, entry.status),
                ("current_stage", item.current_stage, entry.current_stage),
                ("input_fingerprint", item.input_fingerprint, entry.input_fingerprint),
                ("item_revision", item.item_revision, entry.item_revision),
                ("evidence_level", item.evidence_level, entry.evidence_level),
            )
            for field, item_val, index_val in pairs:
                if item_val != index_val:
                    raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False)
            expected_refs.add(f"{entry.item_id}.json")
        # items 目录不得存在不属于 item_index 的当前任务 item 快照
        if items_dir.exists():
            for p in items_dir.iterdir():
                if p.is_file() and p.name not in expected_refs:
                    raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False)

    def commit_transaction(self, task_id: str, txn_id: str) -> None:
        """提交：先完整核验，再写 committed，最后清理本次事务目录。
        状态写入或清理失败 -> 可恢复错误（保留现场，下次 recover 完成）。"""
        txn = self._load_transaction(task_id, txn_id)
        self._verify_committable(task_id, txn)
        self._save_transaction(
            task_id, txn_id,
            txn.model_copy(update={"status": "committed", "event_ref": "events.ndjson", "updated_at": txn.updated_at}),
        )
        try:
            shutil.rmtree(self._txn_dir(task_id, txn_id), ignore_errors=False)
        except OSError as exc:
            raise PipelineStoreError(ERR_TRANSACTION_ERROR, "SIDECAR_TRANSACTION_ERROR", retryable=True) from exc

    def rollback_transaction(self, task_id: str, txn_id: str) -> None:
        """用 old/ 回滚已发布快照。事件已追加时拒绝回滚并保留现场。
        回滚失败 -> SIDECAR_TASK_CORRUPTED 并保留事务目录。"""
        txn = self._load_transaction(task_id, txn_id)
        if txn.status in ("event_appended", "committed"):
            raise PipelineStoreError(ERR_TRANSACTION_ERROR, "SIDECAR_TRANSACTION_ERROR", retryable=False)
        if self._event_already_appended(task_id, txn_id):
            # 事件已追加（状态未及时更新）：禁止回滚
            raise PipelineStoreError(ERR_TRANSACTION_ERROR, "SIDECAR_TRANSACTION_ERROR", retryable=False)
        txn_dir = self._txn_dir(task_id, txn_id)
        for rel_ref in txn.new_snapshot_refs:
            safe_rel = self._safe_rel_ref(rel_ref)
            old_bytes_path = txn_dir / "old" / safe_rel
            target = self._task_dir(task_id) / rel_ref
            try:
                if old_bytes_path.exists():
                    _atomic_write_bytes(target, old_bytes_path.read_bytes())
                else:
                    # 无 old 备份（发布前不存在）：删除已发布的 new
                    target.unlink(missing_ok=True)
            except OSError as exc:
                raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False) from exc
        self._save_transaction(
            task_id, txn_id,
            txn.model_copy(update={"status": "prepared", "new_snapshot_refs": [], "old_snapshot_refs": [],
                                   "updated_at": txn.updated_at}),
        )

    # ---------------- 崩溃恢复 ----------------

    def recover_incomplete_transactions(self, task_id: str) -> None:
        """启动时显式恢复（幂等）。必须获取完整进程锁 + 文件锁，避免与正常事务并发修改
        快照和事件；锁冲突返回 SIDECAR_LOCK_CONFLICT 零副作用。"""
        with self._lock_context(task_id):
            self._recover_locked(task_id)

    def _recover_locked(self, task_id: str) -> None:
        """锁内扫描并恢复该 task 的全部事务（内部方法不再重复获取非重入锁）。"""
        txns_dir = self._transactions_dir(task_id)
        if not txns_dir.exists():
            return
        for txn_id in sorted(p.name for p in txns_dir.iterdir() if p.is_dir()):
            txn_path = txns_dir / txn_id / "transaction.json"
            if not txn_path.exists():
                raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False)
            try:
                obj = _strict_json_loads(txn_path.read_text(encoding="utf-8"))
                txn = PipelineTransaction.model_validate(obj)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False) from exc
            self._recover_one(task_id, txn)

    def _recover_one(self, task_id: str, txn: PipelineTransaction) -> None:
        txn_dir = self._txn_dir(task_id, txn.transaction_id)
        if txn.status == "prepared":
            # 未发布（正式与 old 一致）-> 无副作用清理；
            # 发布中断（某正式快照已与 new 一致）-> 用 old 回滚后清理。
            try:
                published = self._any_snapshot_matches_new(task_id, txn)
            except OSError as exc:
                raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False) from exc
            if published:
                try:
                    self.rollback_transaction(task_id, txn.transaction_id)
                except PipelineStoreError:
                    raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False)
            shutil.rmtree(txn_dir, ignore_errors=False)
            return
        if txn.status == "snapshots_published":
            # 事件尚未追加：优先补写 event.json -> 失败回滚 old/
            try:
                self.append_transaction_event(task_id, txn.transaction_id)
                self.commit_transaction(task_id, txn.transaction_id)
                return
            except PipelineStoreError as exc:
                if exc.error_code == ERR_EVENT_SEQUENCE_CONFLICT:
                    # sequence 已被消费：核验是否恰好是本事务事件（不允许仅凭 sequence 判定）
                    if self._event_already_appended(task_id, txn.transaction_id):
                        try:
                            self._finalize_as_committed(task_id, txn)
                            return
                        except PipelineStoreError:
                            raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False)
                    try:
                        self.rollback_transaction(task_id, txn.transaction_id)
                        shutil.rmtree(txn_dir, ignore_errors=False)
                        return
                    except PipelineStoreError:
                        raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False)
                try:
                    self.rollback_transaction(task_id, txn.transaction_id)
                    shutil.rmtree(txn_dir, ignore_errors=False)
                    return
                except PipelineStoreError:
                    raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False)
        if txn.status in ("event_appended", "committed"):
            # 完整核验（事件内容 + 快照与 new 一致）后 committed + 清理
            self._finalize_as_committed(task_id, txn)
            return
        raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False)

    def _any_snapshot_matches_new(self, task_id: str, txn: PipelineTransaction) -> bool:
        """prepared 恢复判定：任一正式快照字节已与 new/ 一致（发布中断）。"""
        txn_dir = self._txn_dir(task_id, txn.transaction_id)
        for rel_ref in txn.new_snapshot_refs:
            safe_rel = self._safe_rel_ref(rel_ref)
            new_path = txn_dir / "new" / safe_rel
            target = self._task_dir(task_id) / rel_ref
            if new_path.exists() and target.exists() and target.read_bytes() == new_path.read_bytes():
                return True
        return False

    def _finalize_as_committed(self, task_id: str, txn: PipelineTransaction) -> None:
        """完整核验（事件内容 + 快照一致 + last_event_sequence 同步）后 committed + 清理。"""
        self._verify_committable(task_id, txn)
        txn_dir = self._txn_dir(task_id, txn.transaction_id)
        self._save_transaction(
            task_id, txn.transaction_id,
            txn.model_copy(update={"status": "committed", "event_ref": "events.ndjson", "updated_at": txn.updated_at}),
        )
        shutil.rmtree(txn_dir, ignore_errors=False)


class _LockContext:
    """进程内锁 + 文件锁；finally 中释放本次持有的锁。"""

    def __init__(self, plock: threading.Lock, flock: _TaskFileLock) -> None:
        self._plock = plock
        self._flock = flock
        self._active = False

    def __enter__(self) -> "_LockContext":
        self._plock.acquire()
        try:
            self._flock.acquire()
        except PipelineStoreError:
            self._plock.release()
            raise
        self._active = True
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """无论文件锁释放成功或失败，进程内锁都必须在 finally 中释放，避免永久占住。"""
        if not self._active:
            return
        try:
            self._flock.release()
        finally:
            self._plock.release()
            self._active = False


# ---------------------------------------------------------------- 便捷联合事务

def run_task_transaction(
    store: PipelineTaskStore,
    task_id: str,
    event: PipelineEvent,
    new_task: Optional[PipelineTask] = None,
    new_items: Optional[List[PipelineItem]] = None,
    expected_task_revision: Optional[int] = None,
    expected_item_revisions: Optional[Dict[str, int]] = None,
) -> None:
    """锁内执行完整事务：begin -> prepare（写集合落盘）-> publish（一次性发布）
    -> append 事件 -> commit。事件追加后禁止回滚；commit 失败保留现场可由 recover 完成。

    冻结：每个会追加事件的事务必须包含 new_task（new_task.last_event_sequence == event.sequence），
    不允许"只有 item + event、没有 task 快照"的事务。
    """
    if new_task is None:
        raise PipelineStoreError(ERR_TRANSACTION_ERROR, "SIDECAR_TRANSACTION_ERROR", retryable=False)
    with store._lock_context(task_id):
        # CAS None 语义前置：expected_task_revision=None 表示首次创建，目标已存在则冲突
        if expected_task_revision is None and store.load_task(task_id) is not None:
            raise PipelineStoreError(ERR_REVISION_CONFLICT, "SIDECAR_REVISION_CONFLICT", retryable=False)
        txn_id = store.begin_transaction(task_id, event, expected_task_revision, expected_item_revisions)
        # 阶段一：prepare + publish（事件尚未追加，失败可回滚）
        try:
            store.prepare_snapshots(task_id, txn_id, task_snapshot=new_task, item_snapshots=new_items)
            store.publish_prepared_snapshots(task_id, txn_id)
        except PipelineStoreError:
            try:
                store.rollback_transaction(task_id, txn_id)
                shutil.rmtree(store._txn_dir(task_id, txn_id), ignore_errors=True)
            except PipelineStoreError:
                raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False)
            raise
        # 阶段二：追加事件（追加成功即进入 event_appended，禁回滚）
        try:
            store.append_transaction_event(task_id, txn_id)
        except PipelineStoreError:
            if not store._event_already_appended(task_id, txn_id):
                try:
                    store.rollback_transaction(task_id, txn_id)
                    shutil.rmtree(store._txn_dir(task_id, txn_id), ignore_errors=True)
                except PipelineStoreError:
                    raise PipelineStoreError(ERR_TASK_CORRUPTED, "SIDECAR_TASK_CORRUPTED", retryable=False)
            raise
        # 阶段三：提交（事件已追加，禁止回滚；失败保留现场，可恢复错误）
        try:
            store.commit_transaction(task_id, txn_id)
        except PipelineStoreError as exc:
            raise PipelineStoreError(exc.error_code, exc.message_key, retryable=True) from exc
