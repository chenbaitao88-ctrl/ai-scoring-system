"""
ScoreAttempt / ScoreResultSnapshot / AttemptValidation 文件型事实存储（Phase 11E-2b-1）。

依据 Phase 11A-2d 契约第 18 节（文件型持久化要求）与 Phase 11A-3（路线 A：零 Schema sidecar）
实现单 item mock Provider 闭环的安全、不可覆盖、可恢复事实存储。本阶段只实现存储：
不调用 Provider / Gateway / CredentialResolver，不推进 PipelineTask，不写 MachineScore。

存储布局（根目录可由测试注入）：

```text
<root>/
  tasks/{task_id}/items/{item_id}/
    attempts/{attempt_id}.json       # 不可覆盖事实
    snapshots/{snapshot_id}.json     # 不可覆盖事实（成功结果保护）
    validations/{validation_id}.json # 不可覆盖事实
    events.ndjson                    # 追加式审计事件（append-only）
    locks/fact.lock                  # task/item 维度单机文件锁
```

冻结语义：
- 所有标识符必须是安全相对标识符（字符集 [A-Za-z0-9._-]，拒绝 / \\ .. 空字节 盘符 保留名），
  防止路径穿越；违反 -> UNSAFE_SCORE_FACT_PATH。
- 事实对象 create-only：同 ID 同内容 -> 幂等命中（不重复追加事件）；同 ID 不同内容 -> 稳定冲突，
  禁止覆盖。
- 写入原子化：同目录临时文件 + flush/fsync + os.replace；任一步失败不留下可误读半成品，
  并清理本次新建的空业务目录。
- 事件日志只追加；幂等命中不重复生成事件；事件损坏显式失败（SCORE_FACT_EVENT_CORRUPTED）。
- 并发保护：进程内锁 + task/item 维度单机文件锁；两个 writer 写同一 ID 只允许一个首次成功。
- 损坏显式失败：JSON 损坏、字段不合法、引用不一致不得伪装成 NOT_FOUND。
- 成功结果保护：已成功 snapshot 永不覆盖、删除或静默替换（create-only 天然保证）。
- 安全错误：错误信息只含稳定错误码，不含路径、正文、Key、URL、ID 或学生信息。
- 读路径零副作用：get/list/has/verify/load_events 不创建目录、不修改 revision/事件/时间戳。
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

from models.pipeline_task import canonical_json_bytes
from models.score_attempt import (
    TERMINAL_STATUSES,
    AttemptValidation,
    ScoreAttempt,
    ScoreResultSnapshot,
    can_transition,
)

# ---------------------------------------------------------------- 稳定错误码

ERR_ATTEMPT_NOT_FOUND = "SCORE_ATTEMPT_NOT_FOUND"
ERR_ATTEMPT_CONFLICT = "SCORE_ATTEMPT_CONFLICT"
ERR_ATTEMPT_CORRUPTED = "SCORE_ATTEMPT_CORRUPTED"
ERR_ATTEMPT_WRITE_FAILED = "SCORE_ATTEMPT_WRITE_FAILED"
ERR_ATTEMPT_LOCK_CONFLICT = "SCORE_ATTEMPT_LOCK_CONFLICT"
ERR_SNAPSHOT_NOT_FOUND = "SCORE_SNAPSHOT_NOT_FOUND"
ERR_SNAPSHOT_CONFLICT = "SCORE_SNAPSHOT_CONFLICT"
ERR_SNAPSHOT_CORRUPTED = "SCORE_SNAPSHOT_CORRUPTED"
ERR_VALIDATION_NOT_FOUND = "SCORE_VALIDATION_NOT_FOUND"
ERR_VALIDATION_CONFLICT = "SCORE_VALIDATION_CONFLICT"
ERR_VALIDATION_CORRUPTED = "SCORE_VALIDATION_CORRUPTED"
ERR_BINDING_MISMATCH = "SCORE_FACT_BINDING_MISMATCH"
ERR_UNSAFE_PATH = "UNSAFE_SCORE_FACT_PATH"
# 事件日志损坏码（用户冻结列表之外的补充：NDJSON 语法损坏/字段不合法）
ERR_EVENT_CORRUPTED = "SCORE_FACT_EVENT_CORRUPTED"
# 11E-2c：Provider 响应事实文件损坏码（恢复来源；损坏不得伪装为不存在）
ERR_RESPONSE_FACT_CORRUPTED = "SCORE_RESPONSE_FACT_CORRUPTED"


class ScoreFactStoreError(Exception):
    """存储层显式错误（非敏感：str 只返回稳定错误码，不包含路径/正文/ID）。"""

    def __init__(self, error_code: str, message_key: str = "", retryable: bool = False) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.message_key = message_key or error_code
        self.retryable = retryable

    def __str__(self) -> str:
        # 只暴露稳定错误码；绝不含绝对路径、请求正文或学生信息
        return self.error_code


# ---------------------------------------------------------------- 写入原语（模块级，便于故障注入）

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WINDOWS_RESERVED = frozenset({
    "con", "prn", "aux", "nul",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
})


def _validate_id(value: str) -> str:
    """安全相对标识符校验：拒绝路径穿越（/ \\ .. 空字节）、保留名与超长 ID。"""
    if not isinstance(value, str) or not value:
        raise ScoreFactStoreError(ERR_UNSAFE_PATH, retryable=False)
    if "\x00" in value or "/" in value or "\\" in value:
        raise ScoreFactStoreError(ERR_UNSAFE_PATH, retryable=False)
    if not _ID_RE.fullmatch(value):
        raise ScoreFactStoreError(ERR_UNSAFE_PATH, retryable=False)
    if value.lower() in _WINDOWS_RESERVED or value.lower().rstrip(".") in _WINDOWS_RESERVED:
        raise ScoreFactStoreError(ERR_UNSAFE_PATH, retryable=False)
    return value


def _strict_json_loads(text: str) -> Any:
    """禁止 NaN/Infinity 的严格 JSON 解析。"""
    def _reject(constant: str) -> Any:
        raise ValueError(f"non-finite constant: {constant}")

    return json.loads(text, parse_constant=_reject)


def _canonical_bytes(obj: Any) -> bytes:
    return canonical_json_bytes(obj)


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """同目录临时文件 + flush/fsync + os.replace 原子发布。失败不覆盖旧文件，并清理临时文件。"""
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


def _append_ndjson_bytes(path: Path, line: bytes) -> None:
    """NDJSON 追加 + flush/fsync。失败必须上抛（不静默）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


def _cleanup_empty_chain(leaf: Path, stop: Path) -> None:
    """从 leaf 向上删除空目录直到 stop（不含 stop）。

    防御性实现：rmdir 前先显式确认目录存在且为空（listdir），非空立即停止；
    不存在的目录直接跳过（继续向上）。不依赖"rmdir 对非空目录抛错"的平台语义
    （部分 Windows 环境钩子会改变该行为）。
    """
    p = leaf
    while p != stop and p != p.parent:
        try:
            if not p.exists():
                p = p.parent
                continue
            if any(p.iterdir()):
                break
            p.rmdir()
        except OSError:
            break
        p = p.parent


# ---------------------------------------------------------------- 进程内锁（跨线程共享）

_PROCESS_LOCKS: Dict[str, threading.Lock] = {}
_PROCESS_GUARD = threading.Lock()


def _process_lock_for(key: str) -> threading.Lock:
    with _PROCESS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.Lock())


class _FactFileLock:
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
                    raise ScoreFactStoreError(ERR_ATTEMPT_LOCK_CONFLICT, retryable=True)
                time.sleep(0.05)

    def release(self) -> None:
        """释放前读取并确认 owner token；删除锁文件后尝试清理空的 locks 目录。"""
        if not self._acquired:
            return
        try:
            current = self.lock_path.read_text(encoding="utf-8").strip()
        except OSError:
            current = None
        if current != self._owner:
            raise ScoreFactStoreError(ERR_ATTEMPT_LOCK_CONFLICT, retryable=True)
        try:
            self.lock_path.unlink()
        except OSError:
            pass
        try:
            # 仅当 locks 目录确认为空时才删除（防御非空目录 rmdir 被环境钩子改写的风险）
            if not any(self.lock_path.parent.iterdir()):
                self.lock_path.parent.rmdir()
        except OSError:
            pass
        self._acquired = False


class _LockContext:
    """进程内锁 + 文件锁；退出后释放锁并调用可选清理回调（空目录链清理在锁外执行）。"""

    def __init__(self, plock: threading.Lock, flock: _FactFileLock, on_release=None) -> None:
        self._plock = plock
        self._flock = flock
        self._on_release = on_release
        self._active = False

    def __enter__(self) -> "_LockContext":
        self._plock.acquire()
        try:
            self._flock.acquire()
        except ScoreFactStoreError:
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

_EVENT_OBJECT_TYPES = {
    "ATTEMPT_CREATED": "attempt",
    "ATTEMPT_DISPATCHED": "attempt",
    "ATTEMPT_TERMINAL": "attempt",
    "SNAPSHOT_CREATED": "snapshot",
    "VALIDATION_CREATED": "validation",
}
_ATTEMPTS_DIR = "attempts"
_SNAPSHOTS_DIR = "snapshots"
_VALIDATIONS_DIR = "validations"
_RESPONSES_DIR = "responses"  # 11E-2c：Provider 成功响应安全事实（本地写失败后的恢复来源）


class ScoreAttemptStore:
    """ScoreAttempt / ScoreResultSnapshot / AttemptValidation 文件型事实存储（单机、单实例 MVP）。"""

    def __init__(self, root: Path, lock_timeout_ms: int = 5000) -> None:
        self.root = Path(root)
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
        return self._item_dir(task_id, item_id) / "locks" / "fact.lock"

    # ---------------- 只读加载 ----------------

    def _parse_fact(self, path: Path, model_cls: Any, corrupted_code: str) -> Any:
        """解析事实文件：不存在 -> None；损坏/字段不合法 -> corrupted 显式失败（不伪装 NOT_FOUND）。"""
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8")
            return model_cls.model_validate(_strict_json_loads(text))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ScoreFactStoreError(corrupted_code, retryable=False) from exc

    # ---------------- attempt ----------------

    def create_attempt(self, task_id: str, item_id: str, attempt: ScoreAttempt) -> Tuple[str, ScoreAttempt]:
        """创建 ScoreAttempt 事实（create-only 幂等）。返回 ("created"|"idempotent_hit", attempt)。

        - attempt.task_id / item_id 必须与目录上下文一致，否则 SCORE_FACT_BINDING_MISMATCH。
        - 同 attempt_id 同内容 -> idempotent_hit（不重复生成事件）；同 ID 不同内容 -> SCORE_ATTEMPT_CONFLICT。
        """
        if attempt.task_id != task_id or attempt.item_id != item_id:
            raise ScoreFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
        return self._write_fact(
            task_id, item_id, _ATTEMPTS_DIR, attempt.attempt_id, attempt, ScoreAttempt,
            conflict_code=ERR_ATTEMPT_CONFLICT,
            corrupted_code=ERR_ATTEMPT_CORRUPTED,
            write_failed_code=ERR_ATTEMPT_WRITE_FAILED,
            event_type="ATTEMPT_CREATED",
            occurred_at=attempt.created_at,
        )

    def get_attempt(self, task_id: str, item_id: str, attempt_id: str) -> Optional[ScoreAttempt]:
        return self._parse_fact(
            self._fact_path(task_id, item_id, _ATTEMPTS_DIR, attempt_id), ScoreAttempt, ERR_ATTEMPT_CORRUPTED,
        )

    def list_attempts(self, task_id: str, item_id: str) -> List[ScoreAttempt]:
        """列出 task/item 下全部 attempts：attempt_id 稳定排序。只读零副作用；损坏显式失败不跳过。"""
        return self._list_facts(task_id, item_id, _ATTEMPTS_DIR, ScoreAttempt, ERR_ATTEMPT_CORRUPTED)

    def list_attempts_for_item(self, task_id: str, item_id: str) -> List[ScoreAttempt]:
        """与 11E-1b ScoreAttemptLookup 协议对齐的别名。"""
        return self.list_attempts(task_id, item_id)

    def publish_attempt_terminal(
        self, task_id: str, item_id: str, terminal_attempt: ScoreAttempt,
    ) -> Tuple[str, ScoreAttempt]:
        """发布 attempt 终态（running -> succeeded/failed/outcome_unknown/...，CAS 保护）。

        契约语义（11A-2d 6.2 不可变原则）：
        - attempt 不存在 -> SCORE_ATTEMPT_NOT_FOUND。
        - 已是终态：同内容 -> idempotent_hit（不重复事件）；不同内容 -> SCORE_ATTEMPT_CONFLICT，
          禁止覆盖已发布的终态事实。
        - 非终态（created/running）-> 终态：仅允许契约合法状态转换（can_transition），
          原子写替换 + 追加 ATTEMPT_TERMINAL 事件；身份绑定字段（task/item/provider/model/
          版本/证据指纹）不得改变，否则 SCORE_FACT_BINDING_MISMATCH。
        - 事件只追加：幂等命中不重复生成事件。
        """
        if terminal_attempt.task_id != task_id or terminal_attempt.item_id != item_id:
            raise ScoreFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
        self._validate_id(task_id)
        self._validate_id(item_id)
        attempt_id = self._validate_id(terminal_attempt.attempt_id)
        obj_dir = self._obj_dir(task_id, item_id, _ATTEMPTS_DIR)
        path = obj_dir / f"{attempt_id}.json"
        canonical = _canonical_bytes(terminal_attempt.model_dump(mode="json"))
        existed = obj_dir.exists()

        def _cleanup():
            _cleanup_empty_chain(obj_dir, self.root)

        with self._lock_context(task_id, item_id, on_release=_cleanup):
            if not path.exists():
                raise ScoreFactStoreError(ERR_ATTEMPT_NOT_FOUND, retryable=False)
            try:
                current = ScoreAttempt.model_validate(_strict_json_loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise ScoreFactStoreError(ERR_ATTEMPT_CORRUPTED, retryable=False) from exc
            if current.status in TERMINAL_STATUSES:
                if path.read_bytes() == canonical:
                    return ("idempotent_hit", current)
                raise ScoreFactStoreError(ERR_ATTEMPT_CONFLICT, retryable=False)
            if not can_transition(current.status, terminal_attempt.status):
                raise ScoreFactStoreError(ERR_ATTEMPT_CONFLICT, retryable=False)
            # 身份绑定（不可变契约）：task/item/provider/model/版本/证据指纹必须一致
            bind_fields = (
                "task_id", "item_id", "submission_id", "package_id", "package_revision",
                "evidence_manifest_sha256", "input_fingerprint", "provider_id",
                "provider_config_version", "model_id", "model_capability_version",
                "scoring_policy_version", "rubric_version", "prompt_version",
                "response_schema_version", "attempt_number",
            )
            for field in bind_fields:
                if getattr(current, field) != getattr(terminal_attempt, field):
                    raise ScoreFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
            # 校验终态对象合法（模型不变量），不合法视为非法状态冲突
            try:
                ScoreAttempt.model_validate(terminal_attempt.model_dump(mode="json"))
            except ValueError as exc:
                raise ScoreFactStoreError(ERR_ATTEMPT_CONFLICT, retryable=False) from exc
            try:
                _atomic_write_bytes(path, canonical)
            except OSError as exc:
                raise ScoreFactStoreError(ERR_ATTEMPT_WRITE_FAILED, retryable=True) from exc
            try:
                self._append_event(
                    task_id, item_id, "ATTEMPT_TERMINAL", attempt_id,
                    terminal_attempt.completed_at or terminal_attempt.created_at,
                )
            except OSError as exc:
                try:
                    # 事件追加失败：回滚到原非终态快照（不覆盖旧事实）
                    _atomic_write_bytes(path, _canonical_bytes(current.model_dump(mode="json")))
                except OSError:
                    pass
                raise ScoreFactStoreError(ERR_ATTEMPT_WRITE_FAILED, retryable=True) from exc
            return ("created", terminal_attempt)

    # ---------------- 11E-2c：调用调度标记（不可歧义） ----------------

    def mark_attempt_dispatched(
        self, task_id: str, item_id: str, attempt_id: str, occurred_at: datetime,
    ) -> bool:
        """Provider 调用前追加 ATTEMPT_DISPATCHED 事件（恢复时区分"调用前中断"与"调用后未知"）。

        - attempt 不存在 -> SCORE_ATTEMPT_NOT_FOUND；已终态 -> SCORE_ATTEMPT_CONFLICT。
        - 同 attempt 已存在 dispatch 标记 -> 幂等命中（不重复追加事件），返回 False。
        - 返回 True 表示本次首次标记。
        """
        self._validate_id(task_id)
        self._validate_id(item_id)
        attempt_id = self._validate_id(attempt_id)
        with self._lock_context(task_id, item_id):
            path = self._fact_path(task_id, item_id, _ATTEMPTS_DIR, attempt_id)
            if not path.exists():
                raise ScoreFactStoreError(ERR_ATTEMPT_NOT_FOUND, retryable=False)
            try:
                current = ScoreAttempt.model_validate(_strict_json_loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise ScoreFactStoreError(ERR_ATTEMPT_CORRUPTED, retryable=False) from exc
            if current.status in TERMINAL_STATUSES:
                raise ScoreFactStoreError(ERR_ATTEMPT_CONFLICT, retryable=False)
            if any(
                e.get("event_type") == "ATTEMPT_DISPATCHED" and e.get("object_id") == attempt_id
                for e in self.load_events(task_id, item_id)
            ):
                return False  # 幂等命中：已标记
            try:
                self._append_event(task_id, item_id, "ATTEMPT_DISPATCHED", attempt_id, occurred_at)
            except OSError as exc:
                raise ScoreFactStoreError(ERR_ATTEMPT_WRITE_FAILED, retryable=True) from exc
            return True

    # ---------------- 11E-2c：Provider 响应安全事实（恢复来源） ----------------

    def write_response_fact(
        self, task_id: str, item_id: str, fact: Any,
    ) -> Tuple[str, Any]:
        """写入 Provider 成功响应安全事实（create-only 幂等）。

        - 前置绑定：fact.attempt_id 对应 attempt 必须已存在（SCORE_ATTEMPT_NOT_FOUND）。
        - 同 attempt 同内容 -> idempotent_hit；不同内容 -> SCORE_ATTEMPT_CONFLICT（禁止覆盖）。
        - 不追加事件：响应事实文件本身是不可变恢复来源。
        """
        return self._write_fact(
            task_id, item_id, _RESPONSES_DIR, fact.attempt_id, fact, type(fact),
            conflict_code=ERR_ATTEMPT_CONFLICT,
            corrupted_code=ERR_RESPONSE_FACT_CORRUPTED,
            write_failed_code=ERR_ATTEMPT_WRITE_FAILED,
            event_type=None,
            occurred_at=fact.completed_at,
            binding_check=self._check_attempt_exists,
        )

    def get_response_fact(self, task_id: str, item_id: str, attempt_id: str) -> Optional[Dict[str, Any]]:
        """读取 Provider 响应安全事实（恢复来源）；损坏显式失败（SCORE_RESPONSE_FACT_CORRUPTED）。"""
        path = self._fact_path(task_id, item_id, _RESPONSES_DIR, attempt_id)
        if not path.exists():
            return None
        try:
            obj = _strict_json_loads(path.read_text(encoding="utf-8"))
            if not isinstance(obj, dict):
                raise ValueError("invalid response fact shape")
            return obj
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ScoreFactStoreError(ERR_RESPONSE_FACT_CORRUPTED, retryable=False) from exc

    # ---------------- snapshot ----------------

    def write_snapshot(
        self, task_id: str, item_id: str, snapshot: ScoreResultSnapshot,
    ) -> Tuple[str, ScoreResultSnapshot]:
        """写入 ScoreResultSnapshot 事实（create-only 幂等；成功结果保护）。

        前置绑定校验：snapshot.attempt_id 对应 attempt 必须已存在（SCORE_ATTEMPT_NOT_FOUND）；
        submission/package/manifest 与 attempt 一致（SCORE_FACT_BINDING_MISMATCH）。
        同 snapshot_id 同内容 -> idempotent_hit；不同内容 -> SCORE_SNAPSHOT_CONFLICT（永不覆盖）。
        """
        return self._write_fact(
            task_id, item_id, _SNAPSHOTS_DIR, snapshot.snapshot_id, snapshot, ScoreResultSnapshot,
            conflict_code=ERR_SNAPSHOT_CONFLICT,
            corrupted_code=ERR_SNAPSHOT_CORRUPTED,
            write_failed_code=ERR_SNAPSHOT_CONFLICT,
            event_type="SNAPSHOT_CREATED",
            occurred_at=snapshot.created_at,
            binding_check=self._check_snapshot_binding,
        )

    def get_snapshot(self, task_id: str, item_id: str, snapshot_id: str) -> Optional[ScoreResultSnapshot]:
        return self._parse_fact(
            self._fact_path(task_id, item_id, _SNAPSHOTS_DIR, snapshot_id),
            ScoreResultSnapshot, ERR_SNAPSHOT_CORRUPTED,
        )

    def delete_snapshot(self, task_id: str, item_id: str, snapshot_id: str) -> None:
        """11F-3b-fix-1：回滚清理（OSError 必须向上抛出，禁止吞错）。"""
        path = self._fact_path(task_id, item_id, _SNAPSHOTS_DIR, snapshot_id)
        if not path.exists():
            return  # 幂等：目标不存在
        path.unlink()  # OSError 向上抛出
        obj_dir = self._obj_dir(task_id, item_id, _SNAPSHOTS_DIR)
        _cleanup_empty_chain(obj_dir, self.root)

    def delete_validation(self, task_id: str, item_id: str, validation_id: str) -> None:
        """11F-3b-fix-1：清理 validation（OSError 必须向上抛出）。"""
        path = self._fact_path(task_id, item_id, _VALIDATIONS_DIR, validation_id)
        if not path.exists():
            return
        path.unlink()
        obj_dir = self._obj_dir(task_id, item_id, _VALIDATIONS_DIR)
        _cleanup_empty_chain(obj_dir, self.root)

    def create_validation(
        self, task_id: str, item_id: str, validation: AttemptValidation,
    ) -> Tuple[str, AttemptValidation]:
        """11F-3b：公开 validation 创建（复用现有 write_validation 逻辑）。"""
        return self.write_validation(task_id, item_id, validation)

    def list_snapshots(self, task_id: str, item_id: str) -> List[ScoreResultSnapshot]:
        """列出 task/item 下全部 snapshots：snapshot_id 稳定排序。只读零副作用；损坏显式失败。"""
        return self._list_facts(task_id, item_id, _SNAPSHOTS_DIR, ScoreResultSnapshot, ERR_SNAPSHOT_CORRUPTED)

    def has_successful_snapshot(self, task_id: str, item_id: str, attempt_id: Optional[str] = None) -> bool:
        """查询 item 是否已有成功快照（成功结果保护权威来源）。

        任一 snapshot 文件损坏 -> SCORE_SNAPSHOT_CORRUPTED（不伪装成"无成功快照"）。
        """
        snaps = self.list_snapshots(task_id, item_id)
        if attempt_id is None:
            return bool(snaps)
        return any(s.attempt_id == attempt_id for s in snaps)

    # ---------------- validation ----------------

    def write_validation(
        self, task_id: str, item_id: str, validation: AttemptValidation,
    ) -> Tuple[str, AttemptValidation]:
        """写入 AttemptValidation 事实（create-only 幂等）。

        前置绑定校验：validation.attempt_id 对应 attempt 必须已存在（SCORE_ATTEMPT_NOT_FOUND）；
        snapshot_id 非空时对应 snapshot 必须已存在（SCORE_SNAPSHOT_NOT_FOUND）且
        snapshot.attempt_id 与 validation.attempt_id 一致（SCORE_FACT_BINDING_MISMATCH）。
        """
        return self._write_fact(
            task_id, item_id, _VALIDATIONS_DIR, validation.validation_id, validation, AttemptValidation,
            conflict_code=ERR_VALIDATION_CONFLICT,
            corrupted_code=ERR_VALIDATION_CORRUPTED,
            write_failed_code=ERR_VALIDATION_CONFLICT,
            event_type="VALIDATION_CREATED",
            occurred_at=validation.validated_at,
            binding_check=self._check_validation_binding,
        )

    def get_validation(self, task_id: str, item_id: str, validation_id: str) -> Optional[AttemptValidation]:
        return self._parse_fact(
            self._fact_path(task_id, item_id, _VALIDATIONS_DIR, validation_id),
            AttemptValidation, ERR_VALIDATION_CORRUPTED,
        )

    def list_validations(
        self, task_id: str, item_id: str, attempt_id: Optional[str] = None,
    ) -> List[AttemptValidation]:
        """列出 task/item 下全部 validations：validation_id 稳定排序；可按 attempt 过滤。只读零副作用。"""
        found = self._list_facts(task_id, item_id, _VALIDATIONS_DIR, AttemptValidation, ERR_VALIDATION_CORRUPTED)
        if attempt_id is None:
            return found
        return [v for v in found if v.attempt_id == attempt_id]

    # ---------------- 引用一致性校验 ----------------

    def verify_fact_bindings(self, task_id: str, item_id: str, attempt_id: str) -> None:
        """将 attempt 与 snapshot/validation 的引用关系做一致性校验。

        - attempt 不存在 -> SCORE_ATTEMPT_NOT_FOUND。
        - attempt.result_snapshot_ref 非空：snapshot 必须存在（SCORE_SNAPSHOT_NOT_FOUND）、
          snapshot.attempt_id 一致且 submission/package/manifest 与 attempt 一致（SCORE_FACT_BINDING_MISMATCH）。
        - attempt.validation_ref 非空：validation 必须存在（SCORE_VALIDATION_NOT_FOUND）、
          validation.attempt_id 一致（SCORE_FACT_BINDING_MISMATCH）、
          validation.snapshot_id 与 result_snapshot_ref 同时非空时必须一致（SCORE_FACT_BINDING_MISMATCH）。
        - 通过则无返回值（None）。
        """
        attempt = self.get_attempt(task_id, item_id, attempt_id)
        if attempt is None:
            raise ScoreFactStoreError(ERR_ATTEMPT_NOT_FOUND, retryable=False)
        if attempt.result_snapshot_ref is not None:
            snap = self.get_snapshot(task_id, item_id, attempt.result_snapshot_ref)
            if snap is None:
                raise ScoreFactStoreError(ERR_SNAPSHOT_NOT_FOUND, retryable=False)
            if snap.attempt_id != attempt.attempt_id:
                raise ScoreFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
            if (
                snap.submission_id != attempt.submission_id
                or snap.package_id != attempt.package_id
                or snap.evidence_manifest_sha256 != attempt.evidence_manifest_sha256
            ):
                raise ScoreFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
        if attempt.validation_ref is not None:
            val = self.get_validation(task_id, item_id, attempt.validation_ref)
            if val is None:
                raise ScoreFactStoreError(ERR_VALIDATION_NOT_FOUND, retryable=False)
            if val.attempt_id != attempt.attempt_id:
                raise ScoreFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
            if val.snapshot_id is not None and attempt.result_snapshot_ref is not None:
                if val.snapshot_id != attempt.result_snapshot_ref:
                    raise ScoreFactStoreError(ERR_BINDING_MISMATCH, retryable=False)
            if val.snapshot_id is not None and attempt.result_snapshot_ref is None:
                raise ScoreFactStoreError(ERR_BINDING_MISMATCH, retryable=False)

    # ---------------- 事件（append-only 审计） ----------------

    def load_events(self, task_id: str, item_id: str) -> List[Dict[str, Any]]:
        """逐行解析 events.ndjson（只读）；任一损坏显式失败（SCORE_FACT_EVENT_CORRUPTED），不静默忽略。"""
        path = self._events_path(task_id, item_id)
        events: List[Dict[str, Any]] = []
        if not path.exists():
            return events
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                obj = _strict_json_loads(line)
                if not isinstance(obj, dict) or "event_id" not in obj or "sequence" not in obj:
                    raise ValueError("invalid event shape")
                events.append(obj)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ScoreFactStoreError(ERR_EVENT_CORRUPTED, retryable=False) from exc
        return events

    def _next_event_sequence(self, task_id: str, item_id: str) -> int:
        events = self.load_events(task_id, item_id)
        return (events[-1]["sequence"] + 1) if events else 1

    def _append_event(
        self, task_id: str, item_id: str, event_type: str, object_id: str, occurred_at: datetime,
    ) -> None:
        """追加审计事件（只在首次创建时调用；幂等命中不调用）。"""
        seq = self._next_event_sequence(task_id, item_id)
        event = {
            "event_id": uuid.uuid4().hex,
            "sequence": seq,
            "event_type": event_type,
            "task_id": task_id,
            "item_id": item_id,
            "object_type": _EVENT_OBJECT_TYPES[event_type],
            "object_id": object_id,
            "occurred_at": occurred_at.isoformat(),
        }
        _append_ndjson_bytes(
            self._events_path(task_id, item_id),
            json.dumps(event, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n",
        )

    # ---------------- 内部写入（create-only 幂等 + 绑定校验 + 原子写 + 事件） ----------------

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
        write_failed_code: str,
        event_type: str,
        occurred_at: datetime,
        binding_check=None,
    ) -> Tuple[str, Any]:
        self._validate_id(task_id)
        self._validate_id(item_id)
        self._validate_id(object_id)
        obj_dir = self._obj_dir(task_id, item_id, rel_dir)
        path = obj_dir / f"{object_id}.json"
        canonical = _canonical_bytes(obj.model_dump(mode="json"))
        existed = obj_dir.exists()

        def _cleanup():
            _cleanup_empty_chain(obj_dir, self.root)

        with self._lock_context(task_id, item_id, on_release=_cleanup):
            # 幂等/冲突判定（create-only）
            if path.exists():
                if path.read_bytes() == canonical:
                    return ("idempotent_hit", self._parse_fact(path, model_cls, corrupted_code))
                raise ScoreFactStoreError(conflict_code, retryable=False)
            # 绑定前置校验（锁内、写前）
            if binding_check is not None:
                binding_check(task_id, item_id, obj)
            # 阶段一：写事实对象（原子写，失败无半成品）
            try:
                _atomic_write_bytes(path, canonical)
            except OSError as exc:
                raise ScoreFactStoreError(write_failed_code, retryable=True) from exc
            # 阶段二：追加审计事件（event_type=None 表示无事件事实，如 response fact）
            if event_type is not None:
                try:
                    self._append_event(task_id, item_id, event_type, object_id, occurred_at)
                except OSError as exc:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise ScoreFactStoreError(write_failed_code, retryable=True) from exc
            return ("created", self._parse_fact(path, model_cls, corrupted_code))

    def _check_attempt_exists(self, task_id: str, item_id: str, obj: Any) -> None:
        """response fact 前置绑定：对应 attempt 必须已存在。"""
        if self.get_attempt(task_id, item_id, obj.attempt_id) is None:
            raise ScoreFactStoreError(ERR_ATTEMPT_NOT_FOUND, retryable=False)

    def _check_snapshot_binding(self, task_id: str, item_id: str, snapshot: ScoreResultSnapshot) -> None:
        attempt = self.get_attempt(task_id, item_id, snapshot.attempt_id)
        if attempt is None:
            raise ScoreFactStoreError(ERR_ATTEMPT_NOT_FOUND, retryable=False)
        if (
            attempt.submission_id != snapshot.submission_id
            or attempt.package_id != snapshot.package_id
            or attempt.evidence_manifest_sha256 != snapshot.evidence_manifest_sha256
        ):
            raise ScoreFactStoreError(ERR_BINDING_MISMATCH, retryable=False)

    def _check_validation_binding(self, task_id: str, item_id: str, validation: AttemptValidation) -> None:
        attempt = self.get_attempt(task_id, item_id, validation.attempt_id)
        if attempt is None:
            raise ScoreFactStoreError(ERR_ATTEMPT_NOT_FOUND, retryable=False)
        if validation.snapshot_id is not None:
            snapshot = self.get_snapshot(task_id, item_id, validation.snapshot_id)
            if snapshot is None:
                raise ScoreFactStoreError(ERR_SNAPSHOT_NOT_FOUND, retryable=False)
            if snapshot.attempt_id != validation.attempt_id:
                raise ScoreFactStoreError(ERR_BINDING_MISMATCH, retryable=False)

    # ---------------- 列表（稳定排序；损坏显式失败不跳过） ----------------

    def _list_facts(
        self, task_id: str, item_id: str, rel_dir: str, model_cls: Any, corrupted_code: str,
    ) -> List[Any]:
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

    # ---------------- 锁 ----------------

    def _lock_context(self, task_id: str, item_id: str, on_release=None) -> _LockContext:
        lock_path = self._lock_path(task_id, item_id)
        key = str(lock_path)
        return _LockContext(
            _process_lock_for(key),
            _FactFileLock(lock_path, timeout_ms=self.lock_timeout_ms),
            on_release=on_release,
        )
