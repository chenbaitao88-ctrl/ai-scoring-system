"""
Provider 切换决定文件型 Store（Phase 11D-3b）。

单 decision 目录：

```text
<root>/<task_id>/<item_id>/<switch_decision_id>/
    request.json          # 最新 decision 镜像（可重建）
    idempotency.json      # 幂等键 -> 首次创建记录（只写一次）
    current.json          # {"revision": N, "ref": "revisions/00000N.json"}
    revisions/000001.json # 不可覆盖历史 revision（权威事实）
    events.ndjson         # 审计事件（append-only）
    lock                  # 单机文件锁
```

冻结语义：
- revision 从 1 起递增；历史 revision 不覆盖不删除；current 指向最新。
- expected revision CAS（None=仅首次创建）；冲突零副作用。
- 进程锁 + 单机文件锁；同目录临时文件 + 原子替换。
- 多文件写入失败零半提交（写入顺序：revision -> current -> events；事件追加失败显式上抛）。
- JSON 损坏 / current 与 revision 不一致 / 事件损坏显式失败，不自动修复。
- 查询稳定排序与分页；不创建不存在任务的空目录（创建路径由受校验 ID 组合）。
- 不写正文、Key、URL、Prompt、evidence 内容、响应正文或异常原文。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from models.provider_switch import ProviderSwitchRecord, SwitchEvent

# ---------------- 稳定错误码 ---------------- #

ERR_SOURCE_REQUEST_NOT_FOUND = "SIDECAR_SWITCH_SOURCE_REQUEST_NOT_FOUND"
ERR_SOURCE_ERROR_NOT_FOUND = "SIDECAR_SWITCH_SOURCE_ERROR_NOT_FOUND"
ERR_SOURCE_ERROR_NOT_ALLOWED = "SIDECAR_SWITCH_SOURCE_ERROR_NOT_ALLOWED"
ERR_BINDING_MISMATCH = "SIDECAR_SWITCH_BINDING_MISMATCH"
ERR_SUCCESS_EXISTS = "SIDECAR_SWITCH_SUCCESS_EXISTS"
ERR_TARGET_NOT_FOUND = "SIDECAR_SWITCH_TARGET_NOT_FOUND"
ERR_TARGET_DISABLED = "SIDECAR_SWITCH_TARGET_DISABLED"
ERR_TARGET_CAPABILITY_MISMATCH = "SIDECAR_SWITCH_TARGET_CAPABILITY_MISMATCH"
ERR_SCORING_IDENTITY_MISMATCH = "SIDECAR_SWITCH_SCORING_IDENTITY_MISMATCH"
ERR_SAME_PROVIDER = "SIDECAR_SWITCH_SAME_PROVIDER"
ERR_IDEMPOTENCY_CONFLICT = "SIDECAR_SWITCH_IDEMPOTENCY_CONFLICT"
ERR_REVISION_CONFLICT = "SIDECAR_SWITCH_REVISION_CONFLICT"
ERR_INVALID_STATE = "SIDECAR_SWITCH_INVALID_STATE"
ERR_NOT_FOUND = "SIDECAR_SWITCH_NOT_FOUND"
ERR_CORRUPTED = "SIDECAR_SWITCH_CORRUPTED"
ERR_LOCK_CONFLICT = "SIDECAR_SWITCH_LOCK_CONFLICT"
ERR_TRANSACTION_ERROR = "SIDECAR_SWITCH_TRANSACTION_ERROR"


class SwitchStoreError(Exception):
    """切换 store 稳定错误。"""

    def __init__(self, error_code: str, message_key: str = "", retryable: bool = False):
        self.error_code = error_code
        self.message_key = message_key or error_code
        self.retryable = retryable
        super().__init__(error_code)


# ---------------- 进程锁 ---------------- #

_PROCESS_LOCKS: Dict[str, threading.Lock] = {}
_PROCESS_GUARD = threading.Lock()


def _process_lock_for(key: str) -> threading.Lock:
    with _PROCESS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.Lock())


class _SwitchFileLock:
    """单机文件锁（O_CREAT|O_EXCL）；owner token 为随机非敏感值。"""

    def __init__(self, lock_path: Path, timeout_ms: int = 5000):
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
                    raise SwitchStoreError(ERR_LOCK_CONFLICT, retryable=True)
                time.sleep(0.05)

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            current = self.lock_path.read_text(encoding="utf-8").strip()
        except OSError:
            # 锁文件/目录已被清理（如 create 失败回滚删除 decision 目录）：视为已释放，不覆盖原始错误
            self._acquired = False
            return
        if current != self._owner:
            raise SwitchStoreError(ERR_LOCK_CONFLICT, retryable=True)
        try:
            self.lock_path.unlink()
        except OSError:
            pass  # 目录已被清理等场景：视为已释放
        self._acquired = False


class _LockContext:
    def __init__(self, plock: threading.Lock, flock: _SwitchFileLock):
        self._plock = plock
        self._flock = flock

    def __enter__(self):
        self._plock.acquire()
        self._flock.acquire()
        return self

    def __exit__(self, *exc):
        self._flock.release()
        self._plock.release()
        return False


# ---------------- 原子写原语 ---------------- #


def _atomic_write_bytes(target: Path, data: bytes) -> None:
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


def _atomic_write_json(target: Path, obj: Any) -> None:
    _atomic_write_bytes(
        target,
        json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8"),
    )


def _append_ndjson(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        fh.write(b"\n")
        fh.flush()
        os.fsync(fh.fileno())


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        raise SwitchStoreError(ERR_CORRUPTED) from exc


# ---------------- Store ---------------- #


class ProviderSwitchStore:
    """切换决定文件型存储。"""

    def __init__(self, root: Path, lock_timeout_ms: int = 5000):
        self.root = Path(root)
        self.lock_timeout_ms = lock_timeout_ms

    # ---------- 路径 ---------- #

    def _validate_id(self, value: str) -> str:
        import re as _re
        if not isinstance(value, str) or not _re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
            raise SwitchStoreError(ERR_CORRUPTED, message_key="invalid id")
        return value

    def _decision_dir(self, task_id: str, item_id: str, decision_id: str) -> Path:
        return self.root / self._validate_id(task_id) / self._validate_id(item_id) / self._validate_id(decision_id)

    def _revisions_dir(self, ddir: Path) -> Path:
        return ddir / "revisions"

    def _revision_path(self, ddir: Path, revision: int) -> Path:
        return self._revisions_dir(ddir) / f"{revision:06d}.json"

    def _lock_path(self, ddir: Path) -> Path:
        return ddir / "lock"

    # ---------- 创建 ---------- #

    def create(self, record: ProviderSwitchRecord) -> str:
        """首次创建 revision 1 + created event（11D-3b-fix-1：任一步失败清理本次新建目录）。

        返回 "created" / "idempotent_hit" / "conflict"。
        任一写入点失败：删除本次新建 decision 目录及空父目录，不留下可读取/可幂等命中的半成品；
        不影响既有目录；返回稳定错误码 SIDECAR_SWITCH_TRANSACTION_ERROR。
        """
        ddir = self._decision_dir(record.decision.task_id, record.decision.item_id, record.decision.switch_decision_id)
        with self._lock_context(record.decision.task_id, record.decision.item_id, record.decision.switch_decision_id):
            # 存在性以 idempotency.json 为准（lock 可能已创建目录）
            idem = ddir / "idempotency.json"
            if idem.exists():
                data = _read_json(idem)
                if data.get("idempotency_key") == record.idempotency_key:
                    return "idempotent_hit"
                raise SwitchStoreError(ERR_IDEMPOTENCY_CONFLICT)
            ddir.mkdir(parents=True, exist_ok=True)
            steps = (
                ("idempotency", lambda: _atomic_write_json(idem, {
                    "idempotency_key": record.idempotency_key,
                    "decision_id": record.decision.switch_decision_id,
                    "created_at": record.created_at.isoformat(),
                })),
                ("revision", lambda: _atomic_write_json(
                    self._revision_path(ddir, 1), record.model_dump(mode="json"))),
                ("current", lambda: _atomic_write_json(
                    ddir / "current.json", {"revision": 1, "ref": "revisions/000001.json"})),
                ("request", lambda: _atomic_write_json(
                    ddir / "request.json", record.model_dump(mode="json"))),
                ("event", lambda: _append_ndjson(ddir / "events.ndjson", SwitchEvent(
                    sequence=1, decision_id=record.decision.switch_decision_id,
                    transition="created", from_status="", to_status="pending_approval",
                    revision_before=None, revision_after=1, occurred_at=record.created_at,
                ).model_dump(mode="json"))),
            )
            for name, step in steps:
                try:
                    step()
                except Exception:
                    try:
                        self._cleanup_created_dir(ddir)
                    except Exception:
                        pass  # 清理失败不吞原始失败语义
                    raise SwitchStoreError(ERR_TRANSACTION_ERROR)
            return "created"

    def _cleanup_created_dir(self, ddir: Path) -> None:
        """删除本次新建的 decision 目录全部内容 + 目录本身 + 空父目录（task/item）。

        - lock 文件由锁上下文 release 处理（release 容忍目录已被清理）。
        - 仅影响本次新建路径；既有目录不做任何改动。
        - 清理失败不吞原始失败语义（原始异常仍由调用方上抛为 TRANSACTION_ERROR）。
        """
        try:
            rev_dir = ddir / "revisions"
            for f in sorted(rev_dir.glob("*.json"), reverse=True):
                try:
                    f.unlink()
                except OSError:
                    pass
            for f in ("events.ndjson", "request.json", "current.json", "idempotency.json", "lock"):
                p = ddir / f
                try:
                    if p.is_file():
                        p.unlink()
                except OSError:
                    pass
            try:
                rev_dir.rmdir()
            except OSError:
                pass
            try:
                ddir.rmdir()
            except OSError:
                pass
            try:
                ddir.parent.rmdir()  # item 目录为空时清理
            except OSError:
                pass
            try:
                ddir.parent.parent.rmdir()  # task 目录为空时清理
            except OSError:
                pass
        except Exception:
            pass

    # ---------- 更新（CAS） ---------- #

    def update(
        self,
        decision_id: str,
        task_id: str,
        item_id: str,
        new_record: ProviderSwitchRecord,
        expected_revision: Optional[int],
        event: SwitchEvent,
    ) -> ProviderSwitchRecord:
        """写入新 revision + 更新 current/request + 追加事件（11D-3b-fix-1 事务安全）。

        expected revision CAS；任一写入点失败：current/request 回滚到旧 revision，
        旧记录仍可读取，事件序列不增加；孤立新 revision 保留为故障现场但 current 不指向（不可见不可采用）。
        """
        ddir = self._decision_dir(task_id, item_id, decision_id)
        with self._lock_context(task_id, item_id, decision_id):
            current = self._load_current(ddir, decision_id)
            if expected_revision is None:
                raise SwitchStoreError(ERR_REVISION_CONFLICT, message_key="update requires expected revision")
            if current != expected_revision:
                raise SwitchStoreError(ERR_REVISION_CONFLICT)
            new_rev = current + 1
            if new_record.revision != new_rev:
                raise SwitchStoreError(ERR_TRANSACTION_ERROR, message_key="revision mismatch in record")
            old_request = ddir / "request.json"
            old_request_bytes = old_request.read_bytes() if old_request.exists() else None
            steps = (
                ("revision", lambda: _atomic_write_json(
                    self._revision_path(ddir, new_rev), new_record.model_dump(mode="json"))),
                ("current", lambda: _atomic_write_json(
                    ddir / "current.json", {"revision": new_rev, "ref": f"revisions/{new_rev:06d}.json"})),
                ("request", lambda: _atomic_write_json(
                    ddir / "request.json", new_record.model_dump(mode="json"))),
                ("event", lambda: _append_ndjson(ddir / "events.ndjson", event.model_dump(mode="json"))),
            )
            for name, step in steps:
                try:
                    step()
                except Exception:
                    self._rollback_update(ddir, current, old_request_bytes)
                    raise SwitchStoreError(ERR_TRANSACTION_ERROR)
            return new_record

    def _rollback_update(self, ddir: Path, old_revision: int, old_request_bytes: Optional[bytes]) -> None:
        """update 失败回滚：current.json / request.json 恢复旧 revision；孤立新 revision 保留但不可见。

        回滚本身失败时保留现场（best effort），原始失败语义仍由调用方上抛。
        """
        try:
            _atomic_write_bytes(
                ddir / "current.json",
                json.dumps({"revision": old_revision, "ref": f"revisions/{old_revision:06d}.json"},
                           sort_keys=True).encode("utf-8"),
            )
            if old_request_bytes is not None:
                _atomic_write_bytes(ddir / "request.json", old_request_bytes)
        except Exception:
            pass

    # ---------- 读取 ---------- #

    def read(self, task_id: str, item_id: str, decision_id: str) -> Optional[ProviderSwitchRecord]:
        ddir = self._decision_dir(task_id, item_id, decision_id)
        if not ddir.exists():
            return None
        record = self._read_record(ddir, decision_id)
        self._verify_current(ddir, decision_id, record.revision)
        return record

    def find_by_idempotency(self, task_id: str, item_id: str, idempotency_key: str) -> Optional[str]:
        """按幂等键查找已存在的 decision_id（稳定语义：同 key 同 payload 幂等命中）。"""
        base = self.root / self._validate_id(task_id) / self._validate_id(item_id)
        if not base.exists():
            return None
        for d in sorted(p for p in base.iterdir() if p.is_dir()):
            idem = d / "idempotency.json"
            if idem.exists():
                try:
                    data = _read_json(idem)
                except SwitchStoreError:
                    raise  # 损坏显式失败，不跳过后继续
                if data.get("idempotency_key") == idempotency_key:
                    return d.name
        return None

    def list(self, task_id: str, item_id: Optional[str] = None, offset: int = 0, limit: int = 50) -> List[ProviderSwitchRecord]:
        """稳定排序：task_id -> item_id -> decision_id；分页。"""
        base = self.root / self._validate_id(task_id)
        if not base.exists():
            return []
        items_dir = base if item_id is None else base / self._validate_id(item_id)
        if not items_dir.exists():
            return []
        records: List[ProviderSwitchRecord] = []
        if item_id is None:
            for item_d in sorted(p for p in items_dir.iterdir() if p.is_dir()):
                records.extend(self._list_in_dir(item_d))
        else:
            records = self._list_in_dir(items_dir)
        records.sort(key=lambda r: (r.decision.task_id, r.decision.item_id, r.decision.switch_decision_id))
        return records[offset:offset + limit]

    def _list_in_dir(self, item_dir: Path) -> List[ProviderSwitchRecord]:
        out = []
        for d in sorted(p for p in item_dir.iterdir() if p.is_dir()):
            try:
                rec = self._read_record(d, d.name)
            except SwitchStoreError:
                raise  # 损坏显式失败，不跳过后继续
            out.append(rec)
        return out

    # ---------- 校验与恢复读取 ---------- #

    def _load_current(self, ddir: Path, decision_id: str) -> int:
        cur = ddir / "current.json"
        if not cur.exists():
            raise SwitchStoreError(ERR_CORRUPTED, message_key="current.json missing")
        data = _read_json(cur)
        rev = data.get("revision")
        if not isinstance(rev, int) or rev < 1:
            raise SwitchStoreError(ERR_CORRUPTED, message_key="invalid current revision")
        return rev

    def _read_record(self, ddir: Path, decision_id: str) -> ProviderSwitchRecord:
        cur = ddir / "current.json"
        if not cur.exists():
            raise SwitchStoreError(ERR_CORRUPTED, message_key="current.json missing")
        data = _read_json(cur)
        ref = data.get("ref")
        rev = data.get("revision")
        if not isinstance(rev, int) or rev < 1:
            raise SwitchStoreError(ERR_CORRUPTED, message_key="invalid current")
        target = ddir / ref if isinstance(ref, str) else None
        if target is None or not target.exists() or target.parent != self._revisions_dir(ddir):
            raise SwitchStoreError(ERR_CORRUPTED, message_key="current ref mismatch")
        try:
            record = ProviderSwitchRecord(**json.loads(target.read_text(encoding="utf-8")))
        except Exception as exc:
            raise SwitchStoreError(ERR_CORRUPTED) from exc
        if record.revision != rev:
            raise SwitchStoreError(ERR_CORRUPTED, message_key="revision mismatch")
        return record

    def _verify_current(self, ddir: Path, decision_id: str, revision: int) -> None:
        """read 时额外全量一致性校验：current 指向 revision 且 request.json 与 revision 一致。"""
        req = ddir / "request.json"
        if req.exists():
            data = _read_json(req)
            if data.get("revision") != revision:
                raise SwitchStoreError(ERR_CORRUPTED, message_key="request.json revision mismatch")

    # ---------- 锁 ---------- #

    def _lock_context(self, task_id: str, item_id: str, decision_id: str):
        ddir = self._decision_dir(task_id, item_id, decision_id)
        key = str(ddir)
        return _LockContext(_process_lock_for(key), _SwitchFileLock(self._lock_path(ddir), timeout_ms=self.lock_timeout_ms))
