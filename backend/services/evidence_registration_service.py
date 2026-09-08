"""
EvidencePackage 单机文件型登记服务（Phase 11B-1c 修正版）。

权威契约：《Phase11A-fix-2-EvidenceSidecar契约.md》+《Phase11A-fix-4-RegistrationIndexContainer契约.md》。

实现要点（fix-4 对齐）：
- 双根隔离：controlled_input_root（只读输入）与 runtime_data_root（运行数据根）。
- Sidecar 根固定：runtime_data_root/phase11/evidence-sidecar/。
- 布局：
    packages/<package_id>/revisions/<revision>/package-record.json
    packages/<package_id>/revisions/<revision>/validations/<validation_id>.json
    packages/<package_id>/revisions/<revision>/issues/<issue_id>.json
    indexes/registration-index.json            （RegistrationIndex 容器）
    events/evidence-registration.ndjson        （Audit Event）
    locks/registration.lock                    （单写者锁）
  不再使用 records/、validations/、issues/、index/current.json、.locks/。
- dry-run 零写入。
- 正式登记门：仅 registration_allowed=true 可创建 registered record。
- 写入顺序：ValidationIssue -> ValidationResult -> PackageRecord -> RegistrationIndex -> Audit Event。
- ValidationResult.issues 落盘保持 {issue_id, code, severity} 对象数组。
- 七字段幂等：从 PackageRecord + latest ValidationResult 恢复完整判断。
- RegistrationIndex 容器化：首次 revision=1，每次更新 +1，entries 稳定排序并保留全部。
- 原子写：同目录临时文件 + flush/fsync + os.replace；失败保留旧文件。
- 单机锁：所有写入（含 rejected audit、rebuild）持同一锁；release 失败明确报错。
- Audit：锁内分配 sequence；失败返回 SIDECAR_AUDIT_WRITE_FAILED；缺失审计经 recovery_performed 恢复。

不实现：API/router/前端、TaskManager、数据库、评分、读取真实材料。
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from models.evidence_package import EvidenceManifest as EvidenceManifestModel
from models.evidence_sidecar import (
    EvidencePackageRecord,
    RegistrationIndex,
    RegistrationIndexEntry,
    RegistrationStatus,
    ValidationIssue,
    ValidationResult,
    compute_index_sha256,
)
from services.evidence_package_validator import EvidencePackageValidator

_UTC = timezone.utc

# ------------------------------------------------------------------ 契约常量

SIDECAR_SUBDIR = "phase11/evidence-sidecar"
AUDIT_RELATIVE = "events/evidence-registration.ndjson"
INDEX_RELATIVE = "indexes/registration-index.json"
LOCK_RELATIVE = "locks/registration.lock"

# 错误码（契约 9.2 + fix-4）
ERR_LOCK_CONFLICT = "SIDECAR_LOCK_CONFLICT"
ERR_LOCK_RELEASE_FAILED = "SIDECAR_LOCK_RELEASE_FAILED"
ERR_INDEX_CORRUPTED = "SIDECAR_INDEX_CORRUPTED"
ERR_INDEX_WRITE_FAILED = "SIDECAR_INDEX_WRITE_FAILED"
ERR_REVISION_CONFLICT = "SIDECAR_REVISION_CONFLICT"
ERR_RECORD_CONFLICT = "SIDECAR_RECORD_CONFLICT"
ERR_RECORD_CORRUPTED = "SIDECAR_RECORD_CORRUPTED"
ERR_VALIDATION_NOT_FOUND = "SIDECAR_VALIDATION_NOT_FOUND"
ERR_HASH_MISMATCH = "SIDECAR_HASH_MISMATCH"
ERR_RELATIVE_REF_INVALID = "SIDECAR_RELATIVE_REF_INVALID"
ERR_REGISTRATION_NOT_ALLOWED = "REGISTRATION_NOT_ALLOWED"
ERR_IDEMPOTENCY_CONFLICT = "REGISTRATION_IDEMPOTENCY_CONFLICT"
ERR_UNSUPPORTED_VERSION = "SIDECAR_UNSUPPORTED_VERSION"
ERR_AUDIT_WRITE_FAILED = "SIDECAR_AUDIT_WRITE_FAILED"
ERR_CONTENT_CONFLICT = "SIDECAR_CONTENT_CONFLICT"
ERR_INTERNAL = "VALIDATION_INTERNAL_ERROR"

# Audit event types（fix-2 第 12 节）
EVENT_VALIDATION_CREATED = "validation_created"
EVENT_RECORD_CREATED = "record_created"
EVENT_RECORD_UPDATED = "record_updated"
EVENT_INDEX_UPDATED = "index_updated"
EVENT_IDEMPOTENT_HIT = "idempotent_hit"
EVENT_REGISTRATION_REJECTED = "registration_rejected"
EVENT_INDEX_REBUILT = "index_rebuilt"
EVENT_RECOVERY_PERFORMED = "recovery_performed"


class RegistrationError(Exception):
    """登记服务错误：携带稳定错误码。"""

    def __init__(self, code: str, message_key: str, retryable: bool = False) -> None:
        super().__init__(message_key)
        self.code = code
        self.message_key = message_key
        self.retryable = retryable


# ------------------------------------------------------------------ 文件系统工具


def _sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


_READ_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_\-\.]+$")


def _validate_read_identifier(value: Any, field: str) -> None:
    """公开只读接口的安全标识符校验：拒绝路径穿越 / 绝对路径 / 反斜杠 / 空字节 / 空字符串。"""
    if not isinstance(value, str) or not value:
        raise RegistrationError(ERR_RELATIVE_REF_INVALID, "SIDECAR_RELATIVE_REF_INVALID")
    if "\\" in value or "/" in value or "\x00" in value or value in (".", ".."):
        raise RegistrationError(ERR_RELATIVE_REF_INVALID, "SIDECAR_RELATIVE_REF_INVALID")
    if not _READ_IDENTIFIER_RE.match(value):
        raise RegistrationError(ERR_RELATIVE_REF_INVALID, "SIDECAR_RELATIVE_REF_INVALID")


def _validate_read_revision(revision: Any) -> None:
    if not isinstance(revision, int) or revision < 1:
        raise RegistrationError(ERR_RELATIVE_REF_INVALID, "SIDECAR_RELATIVE_REF_INVALID")


def _stable_json_bytes(obj: Any) -> bytes:
    """UTF-8 无 BOM、键升序、无多余空白的稳定 JSON 序列化。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _safe_json_load(path: Path) -> Any:
    if not path.is_file():
        raise RegistrationError(ERR_VALIDATION_NOT_FOUND, "FILE_MISSING")
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig")

        def _reject(name: str) -> Any:
            raise ValueError(f"non-finite constant: {name}")

        return json.loads(text, parse_constant=_reject)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        raise RegistrationError(ERR_RECORD_CORRUPTED, "CORRUPTED_FILE", retryable=False) from exc


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """同目录临时文件 + flush/fsync + 原子替换（字节级）。失败不覆盖旧文件。"""
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
    """同目录临时文件 + flush/fsync + 原子替换。失败不覆盖旧文件。"""
    _atomic_write_bytes(target, _stable_json_bytes(obj))


def _append_ndjson(path: Path, obj: Any) -> None:
    """NDJSON 追加写入（Audit Event）。失败必须上抛（不静默）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = _stable_json_bytes(obj) + b"\n"
    try:
        with open(path, "ab") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        raise RegistrationError(ERR_AUDIT_WRITE_FAILED, "SIDECAR_AUDIT_WRITE_FAILED", retryable=True) from exc


# ------------------------------------------------------------------ 锁


class _SingleWriterLock:
    """runtime 下标准库单写者锁（O_CREAT|O_EXCL）。

    - 冲突返回 SIDECAR_LOCK_CONFLICT。
    - 不静默抢锁、不自动删除无法确认归属的锁。
    - release 失败必须明确报错，不静默伪装成功。
    """

    def __init__(self, lock_path: Path, timeout_ms: int = 5000) -> None:
        self.lock_path = Path(lock_path)
        self.timeout_ms = timeout_ms
        self._acquired = False

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() * 1000 + self.timeout_ms
        while True:
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode("utf-8"))
                os.close(fd)
                self._acquired = True
                return
            except FileExistsError:
                if time.monotonic() * 1000 >= deadline:
                    raise RegistrationError(ERR_LOCK_CONFLICT, "SIDECAR_LOCK_CONFLICT", retryable=True)
                time.sleep(0.05)

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            self.lock_path.unlink()
            self._acquired = False
        except OSError as exc:
            raise RegistrationError(ERR_LOCK_RELEASE_FAILED, "SIDECAR_LOCK_RELEASE_FAILED", retryable=True) from exc


# ------------------------------------------------------------------ 登记服务


class EvidenceRegistrationService:
    """单机文件型登记服务（fix-4 容器化）。"""

    def __init__(
        self,
        controlled_input_root: Path,
        runtime_data_root: Path,
        validator_name: str = "evidence-package-validator",
        validator_version: str = "1.0.0",
        lock_timeout_ms: int = 5000,
    ) -> None:
        self.input_root = Path(controlled_input_root)
        self.runtime_root = Path(runtime_data_root)
        self.validator_name = validator_name
        self.validator_version = validator_version
        self.lock_timeout_ms = lock_timeout_ms
        self._validate_root_isolation()

    # -- 根目录隔离 ------------------------------------------------

    def _validate_root_isolation(self) -> None:
        in_r = os.path.realpath(str(self.input_root))
        out_r = os.path.realpath(str(self.runtime_root))
        if in_r == out_r:
            raise RegistrationError(ERR_RELATIVE_REF_INVALID, "ROOTS_IDENTICAL")
        try:
            if Path(out_r).is_relative_to(Path(in_r)) or Path(in_r).is_relative_to(Path(out_r)):
                raise RegistrationError(ERR_RELATIVE_REF_INVALID, "ROOTS_NESTED")
        except (ValueError, OSError):
            raise RegistrationError(ERR_RELATIVE_REF_INVALID, "ROOTS_NESTED")

    # -- 布局 ------------------------------------------------------

    @property
    def sidecar_root(self) -> Path:
        return self.runtime_root / SIDECAR_SUBDIR

    @property
    def packages_dir(self) -> Path:
        return self.sidecar_root / "packages"

    @property
    def index_path(self) -> Path:
        return self.sidecar_root / INDEX_RELATIVE

    @property
    def audit_path(self) -> Path:
        return self.sidecar_root / AUDIT_RELATIVE

    @property
    def lock_path(self) -> Path:
        return self.sidecar_root / LOCK_RELATIVE

    def _package_dir(self, package_id: str, revision: int) -> Path:
        return self.packages_dir / package_id / "revisions" / str(revision)

    def _record_path(self, package_id: str, revision: int) -> Path:
        return self._package_dir(package_id, revision) / "package-record.json"

    def _validation_path(self, package_id: str, revision: int, validation_id: str) -> Path:
        return self._package_dir(package_id, revision) / "validations" / f"{validation_id}.json"

    def _issue_path(self, package_id: str, revision: int, issue_id: str) -> Path:
        return self._package_dir(package_id, revision) / "issues" / f"{issue_id}.json"

    def _ref_record(self, package_id: str, revision: int) -> str:
        return f"packages/{package_id}/revisions/{revision}/package-record.json"

    def _ref_validation(self, package_id: str, revision: int, validation_id: str) -> str:
        return f"packages/{package_id}/revisions/{revision}/validations/{validation_id}.json"

    # -- 幂等键 ----------------------------------------------------

    def _idempotency_key(self, result: ValidationResult) -> Dict[str, Any]:
        return {
            "package_id": result.package_id,
            "package_revision": result.package_revision,
            "manifest_sha256": result.manifest_sha256,
            "validator_version": result.validator_version,
            "evidence_contract_version": result.evidence_contract_version,
            "privacy_policy_version": result.privacy_policy_version,
            "mode": result.mode,
        }

    # -- 入口 ------------------------------------------------------

    def dry_run(self, package_dir: Path) -> Tuple[ValidationResult, List[ValidationIssue]]:
        """dry-run：只调用验证器，返回内存结果；零写入。"""
        validator = EvidencePackageValidator(
            self.input_root,
            validator_name=self.validator_name,
            validator_version=self.validator_version,
        )
        return validator.validate(package_dir, mode="dry_run")

    def register(
        self,
        package_dir: Path,
        mode: str = "registration",
        expected_index_revision: Optional[int] = None,
    ) -> "RegistrationOutcome":
        """正式登记（fix-4 容器化）。

        expected_index_revision 语义：
        - None 或 0：期望容器不存在（首次创建 revision=1）。
        - 正整数：期望容器当前 revision 等于该值；不一致返回 SIDECAR_REVISION_CONFLICT。
        """
        if mode not in ("registration", "revalidation"):
            raise RegistrationError(ERR_UNSUPPORTED_VERSION, "UNSUPPORTED_MODE")

        validator = EvidencePackageValidator(
            self.input_root,
            validator_name=self.validator_name,
            validator_version=self.validator_version,
        )
        result, issues = validator.validate(package_dir, mode=mode)

        lock = _SingleWriterLock(self.lock_path, timeout_ms=self.lock_timeout_ms)
        lock.acquire()
        lock_released = False
        try:
            # 读取已验证双 JSON 的身份字段（只读，控制文件）
            manifest_raw = _safe_json_load(package_dir / "evidence_manifest.json")
            manifest = EvidenceManifestModel(**manifest_raw)

            outcome = self._register_locked(
                result, issues, manifest, expected_index_revision=expected_index_revision,
            )
            lock.release()
            lock_released = True
            return outcome
        finally:
            if not lock_released and lock._acquired:
                # release 失败不得静默；保留现场并明确报错
                try:
                    lock.release()
                except RegistrationError:
                    raise

    def _register_locked(
        self,
        result: ValidationResult,
        issues: List[ValidationIssue],
        manifest: EvidenceManifestModel,
        expected_index_revision: Optional[int],
    ) -> "RegistrationOutcome":
        package_id = result.package_id
        revision = result.package_revision

        # ---- 事务预检：在任何写入（issue/result/record/index/audit）之前 ----
        # 严格读取现有索引、校验 Audit 可读、捕获 revision/hash、校验 expected_index_revision。
        # 预检冲突立即返回稳定错误，不产生任何 sidecar/审计/索引副作用。
        snapshot, strict_index = self._preflight_index(expected_index_revision)

        # 验证失败 / 不允许登记：只追加 rejected audit（锁内），不写 record/index
        if result.status in ("failed", "internal_error") or not result.registration_allowed:
            self._append_audit_locked(
                event_type=EVENT_REGISTRATION_REJECTED,
                package_id=package_id,
                package_revision=revision,
                validation_id=result.validation_id,
                reason_code=result.status if result.status in ("failed", "internal_error") else "REGISTRATION_NOT_ALLOWED",
            )
            return RegistrationOutcome(
                result=result, issues=issues, record=None, entry=None,
                idempotent_hit=False, rejected=True,
            )

        # 查找既有 record（同 package_id + revision）
        existing_record = self._load_record(package_id, revision)
        existing_validation: Optional[ValidationResult] = None
        if existing_record is not None:
            existing_validation = self._load_validation(
                package_id, revision, existing_record.latest_validation_id,
            )

        # ---- 完整七字段幂等判断 ----
        if existing_record is not None and existing_validation is not None:
            same = self._idempotency_key(result) == self._idempotency_key(existing_validation)
            if same:
                # 幂等命中：逐项检查缺失审计，缺失则 recovery_performed + 补写，再 idempotent_hit
                self._recover_audit_gap(existing_record, result, package_id, revision)
                self._append_audit_locked(
                    event_type=EVENT_IDEMPOTENT_HIT,
                    package_id=package_id,
                    package_revision=revision,
                    validation_id=result.validation_id,
                    record_id=existing_record.record_id,
                    reason_code="IDEMPOTENT_REGISTRATION_REUSED",
                )
                entry = self._load_index_entry_for(package_id, revision)
                return RegistrationOutcome(
                    result=result, issues=issues, record=existing_record, entry=entry,
                    idempotent_hit=True, rejected=False,
                )

            # 同身份但 manifest 不同 -> 冲突（不覆盖）
            if existing_record.manifest_sha256 != result.manifest_sha256:
                raise RegistrationError(ERR_IDEMPOTENCY_CONFLICT, "REGISTRATION_IDEMPOTENCY_CONFLICT")

            # 同 manifest 但 validator/privacy/mode 不同：创建新 ValidationResult 并更新 record
            txn = _WriteSet(self.sidecar_root)
            try:
                record = self._update_record_with_new_validation(
                    package_id, revision, existing_record, result, manifest, issues, txn,
                )
                record_event = EVENT_RECORD_UPDATED
                entry = self._commit_index(
                    package_id, revision, record, result,
                    snapshot=snapshot, strict_index=strict_index, txn=txn,
                )
            except RegistrationError:
                txn.rollback()
                raise
        else:
            # 无既有 record：全新登记
            txn = _WriteSet(self.sidecar_root)
            try:
                record = self._create_record(package_id, revision, result, manifest, issues, txn)
                record_event = EVENT_RECORD_CREATED
                entry = self._commit_index(
                    package_id, revision, record, result,
                    snapshot=snapshot, strict_index=strict_index, txn=txn,
                )
            except RegistrationError:
                txn.rollback()
                raise

        # Audit 追加（锁内）。Audit 失败不触发回滚：事实已提交，保留并返回明确错误（fix-4 语义）。
        self._append_audit_locked(
            event_type=EVENT_VALIDATION_CREATED,
            package_id=package_id,
            package_revision=revision,
            validation_id=result.validation_id,
            record_id=record.record_id,
        )
        self._append_audit_locked(
            event_type=record_event,
            package_id=package_id,
            package_revision=revision,
            validation_id=result.validation_id,
            record_id=record.record_id,
        )
        self._append_audit_locked(
            event_type=EVENT_INDEX_UPDATED,
            package_id=package_id,
            package_revision=revision,
            validation_id=result.validation_id,
            record_id=record.record_id,
            entry_id=entry.entry_id,
        )

        return RegistrationOutcome(
            result=result, issues=issues, record=record, entry=entry,
            idempotent_hit=False, rejected=False,
        )

    def _commit_index(
        self,
        package_id: str,
        revision: int,
        record: EvidencePackageRecord,
        result: ValidationResult,
        snapshot: "_IndexSnapshot",
        strict_index: Optional[RegistrationIndex],
        txn: "_WriteSet",
    ) -> RegistrationIndexEntry:
        """Index 更新（锁内）。Index 比较/写入失败由调用方回滚 txn；本方法不追加 Audit。"""
        entry = self._build_index_entry(record, result)
        new_index = self._upsert_entry(strict_index, entry)
        self._write_index(new_index, snapshot=snapshot)
        return entry

    # -- 对象读写 ---------------------------------------------------

    def _write_issue(
        self, package_id: str, revision: int, issue: ValidationIssue, txn: "_WriteSet"
    ) -> None:
        target = self._issue_path(package_id, revision, issue.issue_id)
        if target.is_file():
            existing = _safe_json_load(target)
            if existing == issue.model_dump(mode="json"):
                return  # 内容相同可复用
            raise RegistrationError(ERR_CONTENT_CONFLICT, "SIDECAR_CONTENT_CONFLICT")
        _atomic_write(target, issue.model_dump(mode="json"))
        txn.record_new(target)

    def _write_validation(
        self, package_id: str, revision: int, result: ValidationResult, txn: "_WriteSet"
    ) -> None:
        target = self._validation_path(package_id, revision, result.validation_id)
        if target.is_file():
            existing = _safe_json_load(target)
            if existing == result.model_dump(mode="json"):
                return  # 内容相同可复用
            raise RegistrationError(ERR_CONTENT_CONFLICT, "SIDECAR_CONTENT_CONFLICT")
        # issues 保持 {issue_id, code, severity} 对象数组（fix-4 第 8 节）
        _atomic_write(target, result.model_dump(mode="json"))
        txn.record_new(target)

    def _load_record(self, package_id: str, revision: int) -> Optional[EvidencePackageRecord]:
        path = self._record_path(package_id, revision)
        if not path.is_file():
            return None
        obj = _safe_json_load(path)
        record = EvidencePackageRecord(**obj)
        # 校验 record_sha256
        obj2 = record.model_dump(mode="json")
        obj2["record_sha256"] = None
        if _sha256_bytes(_stable_json_bytes(obj2)) != record.record_sha256:
            raise RegistrationError(ERR_RECORD_CORRUPTED, "SIDECAR_RECORD_CORRUPTED")
        return record

    def _load_validation(self, package_id: str, revision: int, validation_id: str) -> ValidationResult:
        path = self._validation_path(package_id, revision, validation_id)
        if not path.is_file():
            raise RegistrationError(ERR_VALIDATION_NOT_FOUND, "SIDECAR_VALIDATION_NOT_FOUND")
        obj = _safe_json_load(path)
        try:
            return ValidationResult(**obj)
        except Exception as exc:
            raise RegistrationError(ERR_RECORD_CORRUPTED, "SIDECAR_RECORD_CORRUPTED") from exc

    # -- 公开只读接口（Phase 11B-1d-fix-1）-------------------------
    # 供查询服务 / 后续 API 层使用的正式只读边界：
    # - 只读，不获取写锁，不创建目录 / Audit / tmp 或其他文件；
    # - 复用内部严格校验逻辑（_load_index / _load_record / _load_validation），不复制两套语义；
    # - 安全校验 package_id / revision / validation_id / issue_id，拒绝路径穿越；
    # - read_issue_strict 内部完成安全路径构造、JSON 解析与模型校验，不向调用方暴露文件路径。

    def read_index_strict(self) -> Optional[RegistrationIndex]:
        """公开只读：严格读取 RegistrationIndex 容器（含引用的 Record/Validation 全量一致性校验）。

        缺失返回 None（未创建容器，不视为错误）；损坏 / 引用不一致抛稳定错误码。
        """
        return self._load_index()

    def read_record_strict(
        self, package_id: str, package_revision: int
    ) -> Optional[EvidencePackageRecord]:
        """公开只读：读取 PackageRecord 并校验 record_sha256。

        安全校验 package_id / package_revision；未登记返回 None；
        文件损坏或 record_sha256 不符抛 SIDECAR_RECORD_CORRUPTED。
        """
        _validate_read_identifier(package_id, "package_id")
        _validate_read_revision(package_revision)
        return self._load_record(package_id, package_revision)

    def read_validation_strict(
        self, package_id: str, package_revision: int, validation_id: str
    ) -> ValidationResult:
        """公开只读：读取指定 ValidationResult（含历史）。

        安全校验标识符；不存在抛 SIDECAR_VALIDATION_NOT_FOUND；损坏抛 SIDECAR_RECORD_CORRUPTED。
        只读，不改变 latest 指针。
        """
        _validate_read_identifier(package_id, "package_id")
        _validate_read_revision(package_revision)
        _validate_read_identifier(validation_id, "validation_id")
        return self._load_validation(package_id, package_revision, validation_id)

    def read_issue_strict(
        self, package_id: str, package_revision: int, issue_id: str
    ) -> ValidationIssue:
        """公开只读：读取指定 ValidationIssue 并完成模型校验。

        安全路径构造、JSON 解析与模型校验均在内部完成，不向调用方暴露文件路径。
        引用缺失（文件不存在）抛 SIDECAR_INDEX_CORRUPTED；损坏抛 SIDECAR_RECORD_CORRUPTED。
        """
        _validate_read_identifier(package_id, "package_id")
        _validate_read_revision(package_revision)
        _validate_read_identifier(issue_id, "issue_id")
        path = self._issue_path(package_id, package_revision, issue_id)
        if not path.is_file():
            raise RegistrationError(ERR_INDEX_CORRUPTED, "SIDECAR_INDEX_CORRUPTED")
        obj = _safe_json_load(path)
        try:
            return ValidationIssue(**obj)
        except Exception as exc:
            raise RegistrationError(ERR_RECORD_CORRUPTED, "SIDECAR_RECORD_CORRUPTED") from exc

    def _create_record(
        self,
        package_id: str,
        revision: int,
        result: ValidationResult,
        manifest: EvidenceManifestModel,
        issues: List[ValidationIssue],
        txn: "_WriteSet",
    ) -> EvidencePackageRecord:
        # 1) 写不可变 issue（存在且相同则复用，不同则冲突）
        for issue in issues:
            self._write_issue(package_id, revision, issue, txn)
        # 2) 写不可变 ValidationResult
        self._write_validation(package_id, revision, result, txn)
        # 3) 写 PackageRecord（revision=1）
        now = datetime.now(_UTC)
        record_id = f"record-{uuid.uuid4().hex[:16]}"
        record = EvidencePackageRecord(
            record_schema_version="evidence-sidecar/record/v1",
            record_id=record_id,
            package_id=package_id,
            package_revision=revision,
            batch_id=manifest.batch_id,
            submission_id=manifest.submission_id,
            evidence_version=manifest.evidence_version,
            manifest_sha256=result.manifest_sha256,
            privacy_policy_version=result.privacy_policy_version,
            contract_version=result.evidence_contract_version,
            publication_status="ready",
            registration_status="registered",
            latest_validation_id=result.validation_id,
            latest_validation_status=result.status,
            source_package_ref=f"controlled-inputs/{package_id}",
            registered_at=now,
            registered_by=self.validator_name,
            created_at=now,
            updated_at=now,
            revision=1,
            record_sha256="0" * 64,
        )
        record = self._sign_record(record)
        record_path = self._record_path(package_id, revision)
        _atomic_write(record_path, record.model_dump(mode="json"))
        txn.record_new(record_path)
        return record

    def _update_record_with_new_validation(
        self,
        package_id: str,
        revision: int,
        existing: EvidencePackageRecord,
        result: ValidationResult,
        manifest: EvidenceManifestModel,
        issues: List[ValidationIssue],
        txn: "_WriteSet",
    ) -> EvidencePackageRecord:
        """同 manifest、不同 validator/privacy/mode：写新 ValidationResult 并更新 record（revision+1）。"""
        # 1) 写不可变 issue
        for issue in issues:
            self._write_issue(package_id, revision, issue, txn)
        # 2) 写不可变 ValidationResult（新 validation_id，不与旧冲突）
        self._write_validation(package_id, revision, result, txn)
        # 3) 更新 record：latest_validation 指向新结果，revision+1（不覆盖历史）
        now = datetime.now(_UTC)
        updated = existing.model_copy(
            update={
                "latest_validation_id": result.validation_id,
                "latest_validation_status": result.status,
                "privacy_policy_version": result.privacy_policy_version,
                "updated_at": now,
                "revision": existing.revision + 1,
                "record_sha256": "0" * 64,
            }
        )
        updated = self._sign_record(updated)
        # expected revision 检查：目标必须是 existing.revision（乐观锁）
        current = self._load_record(package_id, revision)
        if current is None or current.revision != existing.revision:
            raise RegistrationError(ERR_REVISION_CONFLICT, "SIDECAR_REVISION_CONFLICT", retryable=True)
        record_path = self._record_path(package_id, revision)
        txn.record_update(record_path, record_path.read_bytes())
        _atomic_write(record_path, updated.model_dump(mode="json"))
        return updated

    def _sign_record(self, record: EvidencePackageRecord) -> EvidencePackageRecord:
        obj = record.model_dump(mode="json")
        obj["record_sha256"] = None
        return record.model_copy(
            update={"record_sha256": _sha256_bytes(_stable_json_bytes(obj))}
        )

    def _build_index_entry(
        self, record: EvidencePackageRecord, result: ValidationResult
    ) -> RegistrationIndexEntry:
        now = datetime.now(_UTC)
        return RegistrationIndexEntry(
            index_schema_version="evidence-sidecar/index/v1",
            entry_id=f"entry-{uuid.uuid4().hex[:16]}",
            record_id=record.record_id,
            package_id=record.package_id,
            package_revision=record.package_revision,
            batch_id=record.batch_id,
            submission_id=record.submission_id,
            evidence_version=record.evidence_version,
            manifest_sha256=record.manifest_sha256,
            registration_status=record.registration_status,
            latest_validation_status=record.latest_validation_status,
            model_input_allowed=result.model_input_allowed,
            record_ref=self._ref_record(record.package_id, record.package_revision),
            latest_validation_ref=self._ref_validation(
                record.package_id, record.package_revision, record.latest_validation_id,
            ),
            registered_at=now,
            updated_at=now,
            revision=record.revision,
        )

    # -- Index 容器 -------------------------------------------------

    def _load_index(self) -> Optional[RegistrationIndex]:
        """严格读取并完整校验 Index 容器（供查询/幂等判断）。缺失返回 None。

        对每个 entry 验证：
        - record 文件存在并能通过 EvidencePackageRecord 解析；
        - record_sha256 重算一致；
        - entry 的 record_id/package_id/package_revision/manifest_sha256/registration_status/
          latest_validation_status 与 record 一致；
        - latest ValidationResult 存在并能解析；
        - ValidationResult 的 package_id/package_revision/manifest_sha256 与 record 一致；
        - entry.latest_validation_ref 指向该 ValidationResult。
        任一不一致返回 SIDECAR_INDEX_CORRUPTED / SIDECAR_RECORD_CORRUPTED /
        SIDECAR_VALIDATION_NOT_FOUND / SIDECAR_HASH_MISMATCH。
        """
        index = self._load_index_lax()
        if index is None:
            return None
        # 逐 entry 全量一致性校验
        for entry in index.entries:
            rec_path = self._record_path(entry.package_id, entry.package_revision)
            if not rec_path.is_file():
                raise RegistrationError(ERR_INDEX_CORRUPTED, "SIDECAR_INDEX_CORRUPTED")
            try:
                record = self._load_record(entry.package_id, entry.package_revision)
            except RegistrationError:
                raise  # record 损坏 / 哈希不符 -> SIDECAR_RECORD_CORRUPTED
            if record is None:
                raise RegistrationError(ERR_INDEX_CORRUPTED, "SIDECAR_INDEX_CORRUPTED")
            # entry 与 record 一致性
            entry_fields = (
                entry.record_id,
                entry.package_id,
                entry.package_revision,
                entry.manifest_sha256,
                entry.registration_status,
                entry.latest_validation_status,
            )
            record_fields = (
                record.record_id,
                record.package_id,
                record.package_revision,
                record.manifest_sha256,
                record.registration_status,
                record.latest_validation_status,
            )
            if entry_fields != record_fields:
                raise RegistrationError(ERR_INDEX_CORRUPTED, "SIDECAR_INDEX_CORRUPTED")
            # latest ValidationResult 存在并可解析
            try:
                validation = self._load_validation(
                    record.package_id, record.package_revision, record.latest_validation_id,
                )
            except RegistrationError:
                raise  # SIDECAR_VALIDATION_NOT_FOUND / SIDECAR_RECORD_CORRUPTED
            # ValidationResult 与 record 一致
            if (
                validation.package_id != record.package_id
                or validation.package_revision != record.package_revision
                or validation.manifest_sha256 != record.manifest_sha256
            ):
                raise RegistrationError(ERR_INDEX_CORRUPTED, "SIDECAR_INDEX_CORRUPTED")
            # entry.latest_validation_ref 指向该 ValidationResult
            expected_ref = self._ref_validation(
                record.package_id, record.package_revision, record.latest_validation_id,
            )
            if entry.latest_validation_ref != expected_ref:
                raise RegistrationError(ERR_INDEX_CORRUPTED, "SIDECAR_INDEX_CORRUPTED")
        return index

    def _load_index_lax(self) -> Optional[RegistrationIndex]:
        """宽松读取 Index 容器（写入前置校验）：schema、hash、record 文件存在。

        不校验 entry 与 record 的全量一致性（record 可能刚被更新而 index 尚未同步，
        写入流程会在同一次调用内完成同步）。
        """
        if not self.index_path.is_file():
            return None
        obj = _safe_json_load(self.index_path)
        try:
            index = RegistrationIndex(**obj)
        except Exception as exc:
            raise RegistrationError(ERR_INDEX_CORRUPTED, "SIDECAR_INDEX_CORRUPTED") from exc
        # hash 重算验证
        if compute_index_sha256(index) != index.index_sha256:
            raise RegistrationError(ERR_HASH_MISMATCH, "SIDECAR_HASH_MISMATCH")
        # record 文件存在性校验
        for entry in index.entries:
            rec_path = self._record_path(entry.package_id, entry.package_revision)
            if not rec_path.is_file():
                raise RegistrationError(ERR_INDEX_CORRUPTED, "SIDECAR_INDEX_CORRUPTED")
        return index

    def _upsert_entry(
        self, index: Optional[RegistrationIndex], entry: RegistrationIndexEntry
    ) -> RegistrationIndex:
        """追加/替换 entry，保留全部既有 entries，稳定排序。

        index 为 None 时构造新容器草案（revision 占位 1，最终落盘 revision 由 _write_index 决定）。
        """
        now = datetime.now(_UTC)
        if index is None:
            return RegistrationIndex(
                index_schema_version="evidence-sidecar/index-container/v1",
                index_id=f"index-{uuid.uuid4().hex[:16]}",
                revision=1,  # 占位；_write_index 首次落盘即为 1
                created_at=now,
                updated_at=now,
                entries=[entry],
                index_sha256="0" * 64,
            )
        entries = [e for e in index.entries if not (
            e.package_id == entry.package_id and e.package_revision == entry.package_revision
        )]
        entries.append(entry)
        entries.sort(key=lambda e: (e.package_id, e.package_revision))
        return index.model_copy(
            update={
                "entries": entries,
                "revision": index.revision + 1,
                "updated_at": now,
                "index_sha256": "0" * 64,
            }
        )

    def _write_index(
        self, index: RegistrationIndex, snapshot: "_IndexSnapshot"
    ) -> RegistrationIndex:
        """原子写 Index 容器；返回最终已签名的 RegistrationIndex。

        expected_index_revision 已在预检（_preflight_index）中校验；
        本方法在最终替换前再次读取当前索引并与预检捕获的 snapshot 比较：
        存在性 / revision / 文件 SHA-256 任一变化 -> SIDECAR_REVISION_CONFLICT，
        不得覆盖外部并发写入。
        落盘 revision：snapshot 不存在 -> 1；存在 -> snapshot.revision + 1。
        """
        # 最终替换前再次比较（防预检后被外部并发修改）
        current_snapshot = self._capture_index_snapshot()
        if (
            current_snapshot.exists != snapshot.exists
            or current_snapshot.revision != snapshot.revision
            or current_snapshot.file_sha256 != snapshot.file_sha256
        ):
            raise RegistrationError(ERR_REVISION_CONFLICT, "SIDECAR_REVISION_CONFLICT", retryable=True)

        final_revision = snapshot.revision + 1 if snapshot.exists else 1
        signed = index.model_copy(
            update={
                "revision": final_revision,
                "index_sha256": "0" * 64,
            }
        )
        obj = signed.model_dump(mode="json")
        obj["index_sha256"] = None
        final_hash = _sha256_bytes(_stable_json_bytes(obj))
        final_index = signed.model_copy(update={"index_sha256": final_hash})
        try:
            _atomic_write(self.index_path, final_index.model_dump(mode="json"))
        except OSError as exc:
            raise RegistrationError(ERR_INDEX_WRITE_FAILED, "SIDECAR_INDEX_WRITE_FAILED", retryable=True) from exc
        return final_index

    def _capture_index_snapshot(self) -> "_IndexSnapshot":
        """捕获当前索引状态：存在性 / revision / 文件 SHA-256。

        解析或哈希校验失败视为并发修改（revision conflict），不得继续写入。
        """
        if not self.index_path.is_file():
            return _IndexSnapshot(exists=False, revision=0, file_sha256=None)
        try:
            file_sha = _sha256_bytes(self.index_path.read_bytes())
            index = self._load_index_lax()  # schema/hash/record 存在校验
        except (RegistrationError, OSError) as exc:
            raise RegistrationError(ERR_REVISION_CONFLICT, "SIDECAR_REVISION_CONFLICT", retryable=True) from exc
        return _IndexSnapshot(exists=True, revision=index.revision, file_sha256=file_sha)

    def _preflight_index(
        self, expected_index_revision: Optional[int]
    ) -> Tuple["_IndexSnapshot", Optional[RegistrationIndex]]:
        """锁内预检：在任何写入（issue/result/record/index/audit）之前执行。

        1. 严格读取现有 RegistrationIndex（校验其自身哈希、引用的 Record/ValidationResult、
           哈希与身份关系）；损坏/引用缺失立即失败。
        2. 校验 Audit 文件可读（解析全部行，损坏立即失败）。
        3. 捕获当前 index revision 与文件 SHA-256。
        4. 校验 expected_index_revision：
           - 0      ：要求索引不存在；已存在 -> SIDECAR_REVISION_CONFLICT。
           - 正整数 ：要求与当前 revision 精确一致；不存在或不等 -> 冲突。
           - None   ：捕获当前真实 revision（不存在则 0）作为内部 expected（真实比较，非恒等）。
        返回 (snapshot, strict_index)；strict_index 为预检时严格读取的索引（None 表示不存在）。
        """
        # Audit 可读性预检（损坏则显式失败，不生成新 sequence、不修改 sidecar）
        self._next_sequence()

        strict_index = self._load_index()  # 严格读取（含引用的 Record/Validation 全量校验）
        if strict_index is None:
            if expected_index_revision is not None and expected_index_revision != 0:
                raise RegistrationError(ERR_REVISION_CONFLICT, "SIDECAR_REVISION_CONFLICT", retryable=True)
            snapshot = _IndexSnapshot(exists=False, revision=0, file_sha256=None)
            return snapshot, None

        snapshot = self._capture_index_snapshot()
        if expected_index_revision == 0:
            # 已存在但调用方期望不存在 -> 冲突
            raise RegistrationError(ERR_REVISION_CONFLICT, "SIDECAR_REVISION_CONFLICT", retryable=True)
        if expected_index_revision is None:
            # 捕获当前真实 revision 作为内部 expected（与磁盘一致则通过，非恒等比较）
            if strict_index.revision != snapshot.revision:
                raise RegistrationError(ERR_REVISION_CONFLICT, "SIDECAR_REVISION_CONFLICT", retryable=True)
        else:
            # 正整数：要求与当前 revision 精确一致
            if strict_index.revision != expected_index_revision:
                raise RegistrationError(ERR_REVISION_CONFLICT, "SIDECAR_REVISION_CONFLICT", retryable=True)
        return snapshot, strict_index

    def _load_index_entry_for(
        self, package_id: str, revision: int
    ) -> Optional[RegistrationIndexEntry]:
        index = self._load_index()
        if index is None:
            return None
        for entry in index.entries:
            if entry.package_id == package_id and entry.package_revision == revision:
                return entry
        return None

    def rebuild_index(self, expected_index_revision: Optional[int] = None) -> RegistrationIndex:
        """从全部 PackageRecord 重建 Index（fix-4 第 6 节）。锁内执行。

        返回最终已签名的 RegistrationIndex（index_sha256 为落盘值，非占位）。
        """
        lock = _SingleWriterLock(self.lock_path, timeout_ms=self.lock_timeout_ms)
        lock.acquire()
        lock_released = False
        try:
            # 事务预检：写任何文件前严格校验现有索引 + 校验 expected_index_revision
            snapshot, _ = self._preflight_index(expected_index_revision)

            entries: List[RegistrationIndexEntry] = []
            packages_dir = self.packages_dir
            if not packages_dir.is_dir():
                raise RegistrationError(ERR_INDEX_CORRUPTED, "SIDECAR_INDEX_CORRUPTED")
            # 遍历 packages/<id>/revisions/<n>/package-record.json
            for record_path in sorted(packages_dir.rglob("package-record.json")):
                try:
                    obj = _safe_json_load(record_path)
                    record = EvidencePackageRecord(**obj)
                    obj2 = record.model_dump(mode="json")
                    obj2["record_sha256"] = None
                    if _sha256_bytes(_stable_json_bytes(obj2)) != record.record_sha256:
                        raise RegistrationError(ERR_RECORD_CORRUPTED, "SIDECAR_RECORD_CORRUPTED")
                    validation = self._load_validation(
                        record.package_id, record.package_revision, record.latest_validation_id,
                    )
                except RegistrationError:
                    raise
                except Exception as exc:
                    raise RegistrationError(ERR_RECORD_CORRUPTED, "SIDECAR_RECORD_CORRUPTED") from exc
                entries.append(self._build_index_entry(record, validation))
            entries.sort(key=lambda e: (e.package_id, e.package_revision))

            index = self._load_index_lax()  # 重建基数（预检已严格校验通过）
            new_index = self._upsert_rebuild(index, entries)
            signed = self._write_index(new_index, snapshot=snapshot)
            self._append_audit_locked(
                event_type=EVENT_INDEX_REBUILT,
                package_id=None,
                package_revision=None,
                validation_id=None,
                record_id=None,
                entry_id=None,
                reason_code="INDEX_REBUILT",
            )
            lock.release()
            lock_released = True
            return signed
        finally:
            if not lock_released and lock._acquired:
                try:
                    lock.release()
                except RegistrationError:
                    raise

    def _upsert_rebuild(
        self, index: Optional[RegistrationIndex], entries: List[RegistrationIndexEntry]
    ) -> RegistrationIndex:
        """重建：以全部合法 entries 替换容器内容。index 为 None 时构造新容器草案。"""
        now = datetime.now(_UTC)
        if index is None:
            return RegistrationIndex(
                index_schema_version="evidence-sidecar/index-container/v1",
                index_id=f"index-{uuid.uuid4().hex[:16]}",
                revision=1,  # 占位；_write_index 首次落盘即为 1
                created_at=now,
                updated_at=now,
                entries=entries,
                index_sha256="0" * 64,
            )
        return index.model_copy(
            update={
                "entries": entries,
                "revision": index.revision + 1,
                "updated_at": now,
                "index_sha256": "0" * 64,
            }
        )

    # -- Audit ------------------------------------------------------

    def _append_audit_locked(
        self,
        event_type: str,
        package_id: Optional[str] = None,
        package_revision: Optional[int] = None,
        validation_id: Optional[str] = None,
        record_id: Optional[str] = None,
        entry_id: Optional[str] = None,
        reason_code: Optional[str] = None,
    ) -> None:
        """在单写者锁内分配 sequence 并追加（fix-4 第 9 节）。失败上抛。"""
        sequence = self._next_sequence()
        event: Dict[str, Any] = {
            "event_type": event_type,
            "event_id": f"event-{uuid.uuid4().hex[:16]}",
            "sequence": sequence,
            "occurred_at": datetime.now(_UTC).isoformat().replace("+00:00", "Z"),
        }
        if package_id is not None:
            event["package_id"] = package_id
        if package_revision is not None:
            event["package_revision"] = package_revision
        if validation_id is not None:
            event["validation_id"] = validation_id
        if record_id is not None:
            event["record_id"] = record_id
        if entry_id is not None:
            event["entry_id"] = entry_id
        if reason_code is not None:
            event["reason_code"] = reason_code
        try:
            _append_ndjson(self.audit_path, event)
        except RegistrationError:
            raise
        except OSError as exc:
            raise RegistrationError(ERR_AUDIT_WRITE_FAILED, "SIDECAR_AUDIT_WRITE_FAILED", retryable=True) from exc

    def _next_sequence(self) -> int:
        """读取审计文件最大 sequence 并返回下一个值。

        显式失败（不得静默忽略损坏）：
        - NDJSON 行无法解析、必需字段缺失、sequence 非法或非递增 -> SIDECAR_AUDIT_WRITE_FAILED；
        - 文件读取失败 -> 同码。
        """
        if not self.audit_path.is_file():
            return 1
        last = 0
        try:
            with open(self.audit_path, "rb") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line.decode("utf-8"))
                    except (ValueError, json.JSONDecodeError) as exc:
                        raise RegistrationError(ERR_AUDIT_WRITE_FAILED, "SIDECAR_AUDIT_WRITE_FAILED", retryable=True) from exc
                    if not isinstance(obj, dict) or not isinstance(obj.get("sequence"), int):
                        raise RegistrationError(ERR_AUDIT_WRITE_FAILED, "SIDECAR_AUDIT_WRITE_FAILED", retryable=True)
                    seq = obj["sequence"]
                    if seq < 1 or seq <= last:
                        # sequence 非法或重复/非递增
                        raise RegistrationError(ERR_AUDIT_WRITE_FAILED, "SIDECAR_AUDIT_WRITE_FAILED", retryable=True)
                    last = seq
        except RegistrationError:
            raise
        except OSError as exc:
            raise RegistrationError(ERR_AUDIT_WRITE_FAILED, "SIDECAR_AUDIT_WRITE_FAILED", retryable=True) from exc
        return last + 1

    def _audit_has(self, record_id: str, event_type: str) -> bool:
        """检查审计中是否已存在指定 record_id 的指定事件。"""
        return event_type in self._audit_event_types_for(record_id)

    def _audit_event_types_for(self, record_id: str) -> set[str]:
        """返回该 record 已存在的全部 event_type 集合。

        显式失败（不得静默忽略损坏）：行无法解析、必需字段缺失、读取失败 -> SIDECAR_AUDIT_WRITE_FAILED。
        """
        found: set[str] = set()
        if not self.audit_path.is_file():
            return found
        try:
            with open(self.audit_path, "rb") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line.decode("utf-8"))
                    except (ValueError, json.JSONDecodeError) as exc:
                        raise RegistrationError(ERR_AUDIT_WRITE_FAILED, "SIDECAR_AUDIT_WRITE_FAILED", retryable=True) from exc
                    if not isinstance(obj, dict) or not isinstance(obj.get("event_type"), str):
                        raise RegistrationError(ERR_AUDIT_WRITE_FAILED, "SIDECAR_AUDIT_WRITE_FAILED", retryable=True)
                    if obj.get("record_id") == record_id:
                        found.add(obj["event_type"])
        except RegistrationError:
            raise
        except OSError as exc:
            raise RegistrationError(ERR_AUDIT_WRITE_FAILED, "SIDECAR_AUDIT_WRITE_FAILED", retryable=True) from exc
        return found

    def _recover_audit_gap(
        self,
        record: EvidencePackageRecord,
        result: ValidationResult,
        package_id: str,
        revision: int,
    ) -> None:
        """正常登记所需事件集合：validation_created、record_created/record_updated、index_updated。

        根据登记事实区分创建与更新：
        - record.revision == 1：首次创建，期望 record_created；
        - record.revision > 1 ：已有 Record 更新，期望 record_updated。

        幂等重试时逐项检查缺失事件；发现任一缺失：
        1) 追加一条 recovery_performed（reason_code 白名单说明缺失事件类型）；
        2) 补写缺失事件（不重复已有）；
        3) 随后由调用方追加 idempotent_hit。
        """
        existing_types = self._audit_event_types_for(record.record_id)
        record_event = EVENT_RECORD_UPDATED if record.revision > 1 else EVENT_RECORD_CREATED
        required = {
            EVENT_VALIDATION_CREATED,
            record_event,
            EVENT_INDEX_UPDATED,
        }
        missing = sorted(required - existing_types)
        if not missing:
            return

        # 1) recovery_performed：稳定白名单 reason_code
        reason = "MISSING_AUDIT_" + "_".join(missing)
        self._append_audit_locked(
            event_type=EVENT_RECOVERY_PERFORMED,
            package_id=package_id,
            package_revision=revision,
            validation_id=result.validation_id,
            record_id=record.record_id,
            reason_code=reason,
        )
        # 2) 补写缺失事件（按固定顺序，不重复已有）
        if EVENT_VALIDATION_CREATED in missing:
            self._append_audit_locked(
                event_type=EVENT_VALIDATION_CREATED,
                package_id=package_id,
                package_revision=revision,
                validation_id=result.validation_id,
                record_id=record.record_id,
            )
        if record_event in missing:
            self._append_audit_locked(
                event_type=record_event,
                package_id=package_id,
                package_revision=revision,
                validation_id=result.validation_id,
                record_id=record.record_id,
            )
        if EVENT_INDEX_UPDATED in missing:
            entry = self._load_index_entry_for(package_id, revision)
            self._append_audit_locked(
                event_type=EVENT_INDEX_UPDATED,
                package_id=package_id,
                package_revision=revision,
                validation_id=result.validation_id,
                record_id=record.record_id,
                entry_id=entry.entry_id if entry else None,
            )


class _IndexSnapshot:
    """预检时捕获的索引状态快照，用于原子替换前的并发一致性比较。"""

    __slots__ = ("exists", "revision", "file_sha256")

    def __init__(self, exists: bool, revision: int, file_sha256: Optional[str]) -> None:
        self.exists = exists
        self.revision = revision  # 不存在时为 0
        self.file_sha256 = file_sha256  # 不存在时为 None


class _WriteSet:
    """登记事务写集：记录本次新建/更新的 sidecar 文件，支持冲突回滚。

    - new_files：本次新建的不可变对象（Issue / ValidationResult / Record）。
    - updated_bytes：更新已有 Record 前保存的原始字节。
    回滚：恢复更新文件的原始字节、删除本次新建文件（并逐级清理空父目录）；
    不修改 Audit、不触碰有内容的既有目录。回滚本身失败抛出 VALIDATION_INTERNAL_ERROR。
    """

    __slots__ = ("new_files", "updated_bytes", "_sidecar_root")

    def __init__(self, sidecar_root: Path) -> None:
        self.new_files: List[Path] = []
        self.updated_bytes: Dict[Path, bytes] = {}
        self._sidecar_root = Path(sidecar_root)

    def record_new(self, path: Path) -> None:
        self.new_files.append(Path(path))

    def record_update(self, path: Path, original_bytes: bytes) -> None:
        self.updated_bytes[Path(path)] = original_bytes

    def rollback(self) -> None:
        # 1) 恢复更新文件的原始字节
        for path, original in self.updated_bytes.items():
            try:
                _atomic_write_bytes(path, original)
            except OSError as exc:
                raise RegistrationError(ERR_INTERNAL, "VALIDATION_INTERNAL_ERROR", retryable=True) from exc
        # 2) 删除本次新建文件，并逐级清理因此产生的空父目录（直到 sidecar 根或遇非空目录；
        #    不触碰有内容的既有目录）
        for path in self.new_files:
            try:
                if path.exists():
                    path.unlink()
                parent = path.parent
                while parent != parent.anchor and parent != self._sidecar_root:
                    if parent.exists() and not any(parent.iterdir()):
                        parent.rmdir()
                        parent = parent.parent
                    else:
                        break
            except OSError as exc:
                raise RegistrationError(ERR_INTERNAL, "VALIDATION_INTERNAL_ERROR", retryable=True) from exc


class RegistrationOutcome:
    """登记结果（内存对象）。"""

    def __init__(
        self,
        result: ValidationResult,
        issues: List[ValidationIssue],
        record: Optional[EvidencePackageRecord],
        entry: Optional[RegistrationIndexEntry],
        idempotent_hit: bool,
        rejected: bool,
    ) -> None:
        self.result = result
        self.issues = issues
        self.record = record
        self.entry = entry
        self.idempotent_hit = idempotent_hit
        self.rejected = rejected
