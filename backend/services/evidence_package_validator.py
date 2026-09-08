"""
EvidencePackage v1.1 只读验证服务（Phase 11B-1b）。

严格依据《Phase11A-2a-EvidencePackage契约.md》v1.1 实现只读验证：
- 输入：受控根目录下的单个包目录（不修改输入，不写 Sidecar，不启动评分）。
- 输出：内存中的 ValidationResult + ValidationIssue 列表。

实现范围：
1. 受控根目录边界（拒绝绝对引用、路径穿越、符号链接、快捷方式、越界解析）。
2. 发布门（ready 必须 .evidence-ready；staging -> PACKAGE_NOT_READY）。
3. 双 JSON 安全读取与一致性（身份、版本、revision、manifest hash、发布状态、privacy_check）。
4. manifest 权威哈希（冻结规范化算法：UTF-8 无 BOM、键升序、稳定序列化、自引用置 null）。
5. 文件完整性（存在、大小、SHA-256、路径安全、未登记文件 fail-closed）。
6. 隐私门（model_input_allowed / registration_allowed 结论与错误码）。
7. 确定性结论（checks / issues / issue_counts / result_sha256，UTC 时间）。

本文件不实现：EvidencePackageRecord 写入、RegistrationIndex、原子写、锁、恢复、
Audit Event、API/router、评分、读取未登记文件正文。
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

from pydantic import ValidationError

from models.evidence_package import (
    COMPLETION_MARKER,
    CONTRACT_VERSION,
    EvidenceManifest,
    EvidencePackage,
)
from models.evidence_sidecar import (
    IssueCounts,
    ValidationCheckSummary,
    ValidationIssue,
    ValidationIssueRef,
    ValidationResult,
)

VALIDATOR_NAME = "evidence-package-validator"
VALIDATOR_VERSION = "1.0.0"
EVIDENCE_CONTRACT_VERSION = CONTRACT_VERSION
DEFAULT_PRIVACY_POLICY_VERSION = "privacy-policy/v1.1"

# 三类控制文件（契约 8.4）
CONTROL_FILES = {"evidence.json", "evidence_manifest.json", COMPLETION_MARKER}

# v1.1 标准错误码（契约 12.2，14 个）
ERROR_PACKAGE_NOT_READY = "PACKAGE_NOT_READY"
ERROR_UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
ERROR_INVALID_PACKAGE_ID = "INVALID_PACKAGE_ID"
ERROR_MANIFEST_MISSING = "MANIFEST_MISSING"
ERROR_EVIDENCE_MISSING = "EVIDENCE_MISSING"
ERROR_PATH_TRAVERSAL = "PATH_TRAVERSAL"
ERROR_FILE_MISSING = "FILE_MISSING"
ERROR_FILE_SIZE_MISMATCH = "FILE_SIZE_MISMATCH"
ERROR_HASH_MISMATCH = "HASH_MISMATCH"
ERROR_CROSS_FILE_MISMATCH = "CROSS_FILE_MISMATCH"
ERROR_PRIVACY_CHECK_FAILED = "PRIVACY_CHECK_FAILED"
ERROR_UNAPPROVED_MODEL_INPUT = "UNAPPROVED_MODEL_INPUT"
ERROR_UNSUPPORTED_FILE_ROLE = "UNSUPPORTED_FILE_ROLE"
ERROR_UNREGISTERED_PACKAGE_FILE = "UNREGISTERED_PACKAGE_FILE"

# 内部错误（契约 fix-2 5.4：internal_error 不得伪装成包验证失败）
ERROR_VALIDATION_INTERNAL = "VALIDATION_INTERNAL_ERROR"

# 额外结构化错误码（非 v1.1 标准 14 码，用于模型层解析失败归类）
ERROR_INVALID_JSON = "INVALID_JSON"
ERROR_SCHEMA_INVALID = "SCHEMA_INVALID"

_UTC = timezone.utc


# ---------------------------------------------------------------- 规范化


def canonical_json_bytes(obj: Any) -> bytes:
    """冻结规范化：UTF-8 无 BOM、键 Unicode 升序、无多余空白、稳定数字。

    契约 7.5：对象键按 Unicode code point 升序；不输出缩进/换行；
    数组顺序保持原样；禁止 NaN/Infinity/负零等非稳定表示。
    """
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_exception_summary(exc: Exception) -> str:
    """稳定、不含路径/原始值的异常摘要。

    只保存异常类型名与稳定属性（如 errno），不使用 str(exc)，
    避免绝对路径、受控根目录、账号目录或原始异常值进入 issue。
    """
    parts = [type(exc).__name__]
    errno = getattr(exc, "errno", None)
    if isinstance(errno, int):
        parts.append(f"errno={errno}")
    return " ".join(parts)


def _unregistered_summary(rel_path: str) -> str:
    """未登记文件的脱敏摘要：仅文件扩展名 + 相对路径 SHA-256 短前缀。

    不包含原始相对路径、文件名或任何可识别内容。
    """
    ext = Path(rel_path).suffix.lower() or "(no-extension)"
    digest = _sha256_bytes(rel_path.encode("utf-8"))[:12]
    return f"unregistered-file ext={ext} hash={digest}"


def _json_load_bytes(raw: bytes) -> Any:
    """解析 JSON 字节，拒绝 NaN/Infinity 等非有限常量。"""
    text = raw.decode("utf-8-sig")  # 容忍 BOM；规范化哈希以无 BOM 字节为准

    def _reject_constant(name: str) -> Any:
        raise ValueError(f"non-finite constant not allowed: {name}")

    return json.loads(text, parse_constant=_reject_constant)


def _safe_read_json(path: Path) -> Any:
    """安全读取 JSON 文件，返回解析对象；失败抛 ValueError。"""
    if not path.is_file():
        raise FileNotFoundError(str(path))
    with open(path, "rb") as fh:
        return _json_load_bytes(fh.read())


# ---------------------------------------------------------------- 路径安全


def _is_link(path: Path) -> bool:
    """检测符号链接、目录联接点、挂载点或重解析点（只读属性，不跟随）。

    - symlink / junction：path.is_symlink()（Windows 上 junction 亦返回 True）。
    - 挂载点 / reparse point 挂载：os.path.ismount()（只读，不修改）。
    """
    try:
        if path.is_symlink():
            return True
    except OSError:
        return True
    try:
        if os.path.ismount(path):
            return True
    except OSError:
        return True
    return False


def _find_link_ancestor(path: Path, stop_at: Path) -> Optional[Path]:
    """自 path 向上逐级检查每一级父目录是否链接/挂载点，直到 stop_at（不含）。

    契约 9.3：只检查最终文件不够，必须检查每一级父目录。
    返回第一个被判定为链接/挂载点的路径；无则返回 None。不跟随任何链接。
    """
    current = Path(path)
    stop = Path(stop_at)
    while True:
        if _is_link(current):
            return current
        if current == stop or current.parent == current:
            return None
        current = current.parent


def _is_shortcut_name(name: str) -> bool:
    """Windows 快捷方式 / 互联网快捷方式（.lnk / .url），不得解析目标。"""
    return name.lower().endswith((".lnk", ".url"))


def _hardlink_nlink(path: Path) -> Optional[int]:
    """以不跟随链接的方式读取 st_nlink。

    - 平台提供 st_nlink：返回实际链接数。
    - 平台无 st_nlink 属性或 stat 失败：返回 None，调用方必须 fail-closed，
      不得把无法判定的文件视为普通文件。
    """
    try:
        st = os.stat(str(path), follow_symlinks=False)
    except OSError:
        return None
    return getattr(st, "st_nlink", None)


def _resolve_inside(root: Path, candidate: Path) -> bool:
    """判断 candidate 解析后是否位于 root 内（不跟随链接）。"""
    root_resolved = os.path.realpath(str(root))
    cand_resolved = os.path.realpath(str(candidate))
    try:
        return Path(cand_resolved).is_relative_to(Path(root_resolved))
    except (ValueError, OSError):
        return False


def _assert_relative_path_format(relative_path: str) -> None:
    """路径格式检查（契约 9.1），不解析文件系统。越界由调用方在链接检查后处理。"""
    if not relative_path or relative_path.startswith("/") or relative_path.startswith("\\"):
        raise ValueError("absolute or empty path")
    if "\\" in relative_path:
        raise ValueError("backslash path")
    if relative_path.split("/")[0] == ".." or "/.." in f"/{relative_path}":
        raise ValueError("path traversal")
    if any(seg == "" for seg in relative_path.split("/")):
        raise ValueError("empty path segment")


def _assert_relative_path_safe(relative_path: str, package_dir: Path) -> None:
    """格式与越界检查（契约 9.1/9.2），越界抛 ValueError。"""
    _assert_relative_path_format(relative_path)
    candidate = (package_dir / relative_path).resolve()
    if not _resolve_inside(package_dir, candidate):
        raise ValueError("escapes package root")


# ---------------------------------------------------------------- issue 收集


class _IssueCollector:
    """确定性地收集 ValidationIssue 并维护引用数组。"""

    def __init__(self) -> None:
        self._issues: List[ValidationIssue] = []
        self._refs: List[ValidationIssueRef] = []
        self._counts = IssueCounts()
        self._used_ids: set[str] = set()

    def add(
        self,
        code: str,
        severity: str,
        stage: str,
        message_key: str,
        location: str,
        file_ref: Optional[str] = None,
        field_path: Optional[str] = None,
        expected_summary: Optional[str] = None,
        actual_summary: Optional[str] = None,
        retryable: bool = False,
        blocks_registration: bool = True,
        blocks_model_input: bool = True,
        requires_manual_review: bool = False,
    ) -> str:
        issue_id = f"issue-{uuid.uuid4().hex[:12]}"
        while issue_id in self._used_ids:
            issue_id = f"issue-{uuid.uuid4().hex[:12]}"
        self._used_ids.add(issue_id)
        issue = ValidationIssue(
            issue_id=issue_id,
            code=code,
            severity=severity,  # type: ignore[arg-type]
            stage=stage,
            message_key=message_key,
            location=location,
            file_ref=file_ref,
            field_path=field_path,
            expected_summary=expected_summary,
            actual_summary=actual_summary,
            retryable=retryable,
            blocks_registration=blocks_registration,
            blocks_model_input=blocks_model_input,
            requires_manual_review=requires_manual_review,
            created_at=datetime.now(_UTC),
        )
        self._issues.append(issue)
        self._refs.append(
            ValidationIssueRef(issue_id=issue_id, code=code, severity=severity)  # type: ignore[arg-type]
        )
        setattr(self._counts, severity, getattr(self._counts, severity) + 1)
        return issue_id

    @property
    def issues(self) -> List[ValidationIssue]:
        # 契约 6.4 / 12.3：issue 顺序必须稳定（按收集顺序）
        return list(self._issues)

    @property
    def refs(self) -> List[ValidationIssueRef]:
        return list(self._refs)

    @property
    def counts(self) -> IssueCounts:
        return self._counts

    @property
    def has_error(self) -> bool:
        return self._counts.error > 0 or self._counts.fatal > 0


# ---------------------------------------------------------------- 验证器


class EvidencePackageValidator:
    """EvidencePackage v1.1 只读验证器。

    用法：
        validator = EvidencePackageValidator(controlled_root)
        result, issues = validator.validate(package_dir, mode="dry_run")
    """

    def __init__(
        self,
        controlled_root: Path,
        validator_name: str = VALIDATOR_NAME,
        validator_version: str = VALIDATOR_VERSION,
    ) -> None:
        self.root = Path(controlled_root)
        self.validator_name = validator_name
        self.validator_version = validator_version

    # -- 对外入口 ------------------------------------------------------

    def validate(
        self, package_dir: Path, mode: str = "dry_run"
    ) -> Tuple[ValidationResult, List[ValidationIssue]]:
        started = datetime.now(_UTC)
        collector = _IssueCollector()
        checks: List[ValidationCheckSummary] = []
        package_id_meta: Optional[str] = None
        revision_meta: Optional[int] = None
        manifest_hash_meta: Optional[str] = None
        privacy_version_meta: Optional[str] = DEFAULT_PRIVACY_POLICY_VERSION
        evidence_level_meta: Optional[str] = None  # 权威验证事实（EvidencePackage.evidence_level）
        model_input_allowed = False
        registration_allowed = False
        validated_count = 0
        declared_count = 0
        unregistered_count = 0
        internal_error = False

        try:
            # ---- 0. 受控根目录边界 ----
            self._check_boundary(package_dir, collector)

            # ---- 1. 发布门 ----
            if not collector.has_error:
                self._publication_gate(package_dir, collector, checks)

            # ---- 2. 双 JSON 解析与一致性 ----
            pair: Optional[Tuple[EvidencePackage, EvidenceManifest]] = None
            if not collector.has_error:
                pair = self._dual_json_consistency(package_dir, collector, checks)
            if pair is not None:
                evidence, manifest = pair
                package_id_meta = evidence.package_id
                revision_meta = evidence.package_revision
                manifest_hash_meta = evidence.manifest_sha256
                privacy_version_meta = evidence.privacy_check.privacy_policy_version
                # 权威验证事实：只从已解析、已校验的 EvidencePackage.evidence_level 写入
                evidence_level_meta = evidence.evidence_level

                # ---- 3. manifest 权威哈希 ----
                self._manifest_hash_check(package_dir, evidence, manifest, collector, checks)

                # ---- 4. 文件完整性 ----
                validated_count, declared_count = self._file_integrity(
                    package_dir, manifest, collector, checks,
                )

                # ---- 5. 未登记文件（fail-closed） ----
                unregistered_count = self._unregistered_files_check(
                    package_dir, manifest, collector, checks,
                )

                # ---- 6. 隐私门 ----
                model_input_allowed, registration_allowed = self._privacy_gate(
                    evidence, manifest, collector, checks,
                )

                # ---- 7. 证据等级与结论 ----
                self._evidence_level_check(evidence, collector, checks)

            if collector.has_error:
                model_input_allowed = False
                registration_allowed = False
        except Exception as exc:  # noqa: BLE001 —— 内部错误不得伪装成输入失败
            internal_error = True
            collector.add(
                code=ERROR_VALIDATION_INTERNAL,
                severity="fatal",
                stage="internal",
                message_key="VALIDATION_INTERNAL_ERROR",
                location="validator",
                actual_summary=_safe_exception_summary(exc),
                retryable=True,
                blocks_registration=True,
                blocks_model_input=True,
                requires_manual_review=True,
            )

        result = self._build_result(
            collector, checks, package_id_meta, revision_meta,
            manifest_hash_meta, privacy_version_meta, started,
            model_input_allowed, registration_allowed,
            validated_count, declared_count, unregistered_count,
            internal_error=internal_error, mode=mode,
            evidence_level=evidence_level_meta,
        )
        return result, collector.issues

    # -- 分步检查 ------------------------------------------------------

    def _check_boundary(self, package_dir: Path, collector: _IssueCollector) -> None:
        """受控根目录边界：包目录必须在根目录内；拒绝链接/越界。"""
        try:
            pkg = Path(package_dir)
            if _is_link(pkg):
                raise ValueError("package dir is a symlink")
            if not _resolve_inside(self.root, pkg):
                raise ValueError("package dir outside controlled root")
        except Exception as exc:  # noqa: BLE001
            collector.add(
                code=ERROR_PATH_TRAVERSAL,
                severity="error",
                stage="boundary",
                message_key="PATH_TRAVERSAL",
                location="package_dir",
                actual_summary=_safe_exception_summary(exc),
            )

    def _publication_gate(
        self,
        package_dir: Path,
        collector: _IssueCollector,
        checks: List[ValidationCheckSummary],
    ) -> None:
        """发布门（契约 8.1）：只接受 ready / rejected；staging -> PACKAGE_NOT_READY。"""
        evidence_path = package_dir / "evidence.json"
        manifest_path = package_dir / "evidence_manifest.json"
        marker_path = package_dir / COMPLETION_MARKER

        if not evidence_path.is_file():
            collector.add(
                code=ERROR_EVIDENCE_MISSING,
                severity="error",
                stage="publication_gate",
                message_key="EVIDENCE_MISSING",
                location="evidence.json",
            )
        if not manifest_path.is_file():
            collector.add(
                code=ERROR_MANIFEST_MISSING,
                severity="error",
                stage="publication_gate",
                message_key="MANIFEST_MISSING",
                location="evidence_manifest.json",
            )

        # 先读取双 JSON 顶层 publication_status / completion_marker（不完整解析）
        statuses: List[str] = []
        for name, path in (("evidence.json", evidence_path), ("evidence_manifest.json", manifest_path)):
            if not path.is_file():
                continue
            try:
                obj = _safe_read_json(path)
                if not isinstance(obj, dict):
                    raise ValueError("not a JSON object")
                statuses.append(str(obj.get("publication_status", "")))
            except (ValueError, OSError, json.JSONDecodeError):
                collector.add(
                    code=ERROR_INVALID_JSON,
                    severity="error",
                    stage="publication_gate",
                    message_key="INVALID_JSON",
                    location=name,
                )

        # staging -> PACKAGE_NOT_READY（契约 5.1）
        if any(s == "staging" for s in statuses):
            collector.add(
                code=ERROR_PACKAGE_NOT_READY,
                severity="error",
                stage="publication_gate",
                message_key="PACKAGE_NOT_READY",
                location="publication_status",
                expected_summary="ready or rejected",
                actual_summary="staging",
            )

        # ready 必须存在 .evidence-ready（契约 5.1 / 8.1.5）
        if "ready" in statuses and not marker_path.is_file():
            collector.add(
                code=ERROR_PACKAGE_NOT_READY,
                severity="error",
                stage="publication_gate",
                message_key="PACKAGE_NOT_READY",
                location=".evidence-ready",
                expected_summary="ready 包必须存在 .evidence-ready",
                actual_summary="marker missing",
            )

        # marker 与双 JSON 状态不一致：marker 存在但无双 JSON 声明 ready
        if marker_path.is_file() and statuses and all(s != "ready" for s in statuses):
            collector.add(
                code=ERROR_PACKAGE_NOT_READY,
                severity="error",
                stage="publication_gate",
                message_key="PACKAGE_NOT_READY",
                location=".evidence-ready",
                expected_summary="marker 仅应存在于 ready 包",
                actual_summary="marker exists but publication_status != ready",
            )

        gate_ok = not any(i.stage == "publication_gate" for i in collector.issues)
        checks.append(
            ValidationCheckSummary(
                check_id="PUBLICATION_GATE",
                status="passed" if gate_ok else "failed",
                message_key="CHECK_OK" if gate_ok else "CHECK_FAILED",
            )
        )

    def _dual_json_consistency(
        self,
        package_dir: Path,
        collector: _IssueCollector,
        checks: List[ValidationCheckSummary],
    ) -> Optional[Tuple[EvidencePackage, EvidenceManifest]]:
        """双 JSON 解析与身份/版本/revision/hash/发布状态/privacy_check 一致性（8.2/8.5）。"""
        evidence_path = package_dir / "evidence.json"
        manifest_path = package_dir / "evidence_manifest.json"

        try:
            evidence_raw = _safe_read_json(evidence_path)
            manifest_raw = _safe_read_json(manifest_path)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            collector.add(
                code=ERROR_INVALID_JSON,
                severity="error",
                stage="dual_json",
                message_key="INVALID_JSON",
                location="evidence.json / evidence_manifest.json",
                actual_summary=_safe_exception_summary(exc),
            )
            checks.append(ValidationCheckSummary(check_id="DUAL_JSON_CONSISTENCY", status="failed", message_key="CHECK_FAILED"))
            return None

        # 模型层校验（未知字段/必填/枚举/格式）
        try:
            evidence = EvidencePackage(**evidence_raw)
        except ValidationError as exc:
            self._model_error_to_issue(exc, "evidence.json", collector, is_evidence=True)
            return None
        try:
            manifest = EvidenceManifest(**manifest_raw)
        except ValidationError as exc:
            self._model_error_to_issue(exc, "evidence_manifest.json", collector, is_evidence=False)
            return None

        # 身份/版本/revision/hash/发布状态一致性（8.2）
        identity_fields = [
            "contract_version", "package_id", "batch_id", "submission_id",
            "evidence_version", "package_revision", "manifest_sha256",
            "generated_at", "generator_name", "generator_version",
            "publication_status", "completion_marker",
        ]
        for field_name in identity_fields:
            ev = getattr(evidence, field_name)
            mv = getattr(manifest, field_name)
            if ev != mv:
                collector.add(
                    code=ERROR_CROSS_FILE_MISMATCH,
                    severity="error",
                    stage="dual_json",
                    message_key="CROSS_FILE_MISMATCH",
                    location=f"identity.{field_name}",
                    field_path=field_name,
                    expected_summary=str(ev)[:200],
                    actual_summary=str(mv)[:200],
                )

        # 完整 privacy_check 深度一致（8.5）
        ev_privacy = evidence.privacy_check.model_dump(mode="json")
        mv_privacy = manifest.privacy_check.model_dump(mode="json")
        if canonical_json_bytes(ev_privacy) != canonical_json_bytes(mv_privacy):
            collector.add(
                code=ERROR_CROSS_FILE_MISMATCH,
                severity="error",
                stage="dual_json",
                message_key="CROSS_FILE_MISMATCH",
                location="privacy_check",
                field_path="privacy_check",
                expected_summary="双 JSON privacy_check 规范化后深度相等",
                actual_summary="privacy_check 不一致",
            )

        ok = not any(i.stage == "dual_json" for i in collector.issues)
        checks.append(
            ValidationCheckSummary(
                check_id="DUAL_JSON_CONSISTENCY",
                status="passed" if ok else "failed",
                message_key="CHECK_OK" if ok else "CHECK_FAILED",
            )
        )
        return evidence, manifest

    def _model_error_to_issue(
        self, exc: ValidationError, source: str, collector: _IssueCollector, is_evidence: bool
    ) -> None:
        """Pydantic 校验错误归类为结构化 issue（不泄露正文）。"""
        for err in exc.errors():
            loc = err.get("loc") or ()
            loc_str = ".".join(str(x) for x in loc)
            etype = err.get("type", "")
            msg = err.get("msg", "")[:200]

            if "contract_version" in loc:
                code, key = ERROR_UNSUPPORTED_VERSION, "UNSUPPORTED_VERSION"
            elif "package_id" in loc:
                code, key = ERROR_INVALID_PACKAGE_ID, "INVALID_PACKAGE_ID"
            elif "relative_path" in loc:
                # 模型层路径格式校验（绝对/盘符/../反斜杠）对应契约 9.1/9.2 的路径安全失败
                code, key = ERROR_PATH_TRAVERSAL, "PATH_TRAVERSAL"
            else:
                code, key = ERROR_SCHEMA_INVALID, "SCHEMA_INVALID"
            collector.add(
                code=code,
                severity="error",
                stage="model_validation",
                message_key=key,
                location=source,
                field_path=loc_str or None,
                expected_summary="符合契约 schema",
                actual_summary=f"{etype}: {msg}",
            )

    def _manifest_hash_check(
        self,
        package_dir: Path,
        evidence: EvidencePackage,
        manifest: EvidenceManifest,
        collector: _IssueCollector,
        checks: List[ValidationCheckSummary],
    ) -> None:
        """manifest 权威哈希（7.5）：重算必须与双 JSON 声明一致。"""
        manifest_path = package_dir / "evidence_manifest.json"
        recomputed: Optional[str] = None
        try:
            obj = _safe_read_json(manifest_path)
            if not isinstance(obj, dict):
                raise ValueError("manifest not an object")
            # 自引用置 null 后规范化计算
            obj_copy = dict(obj)
            obj_copy["manifest_sha256"] = None
            recomputed = _sha256_bytes(canonical_json_bytes(obj_copy))
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            collector.add(
                code=ERROR_HASH_MISMATCH,
                severity="error",
                stage="manifest_hash",
                message_key="HASH_MISMATCH",
                location="evidence_manifest.json",
                actual_summary=_safe_exception_summary(exc),
            )

        if recomputed is not None:
            declared = evidence.manifest_sha256
            if recomputed != declared:
                collector.add(
                    code=ERROR_HASH_MISMATCH,
                    severity="error",
                    stage="manifest_hash",
                    message_key="HASH_MISMATCH",
                    location="manifest_sha256",
                    field_path="manifest_sha256",
                    expected_summary=f"按 7.5 节重算 {recomputed[:16]}…",
                    actual_summary=f"声明 {declared[:16]}…",
                )
        ok = recomputed is not None and recomputed == evidence.manifest_sha256
        checks.append(
            ValidationCheckSummary(
                check_id="MANIFEST_HASH",
                status="passed" if ok else "failed",
                message_key="CHECK_OK" if ok else "CHECK_FAILED",
            )
        )

    def _file_integrity(
        self,
        package_dir: Path,
        manifest: EvidenceManifest,
        collector: _IssueCollector,
        checks: List[ValidationCheckSummary],
    ) -> Tuple[int, int]:
        """文件完整性（8.3）：存在、大小、SHA-256、路径安全、角色支持。"""
        declared_count = len(manifest.files)
        validated_count = 0
        stage_codes = {
            ERROR_PATH_TRAVERSAL, ERROR_FILE_MISSING, ERROR_FILE_SIZE_MISMATCH,
            ERROR_HASH_MISMATCH, ERROR_UNSUPPORTED_FILE_ROLE,
        }
        for entry in manifest.files:
            candidate_raw = package_dir / entry.relative_path
            # 1) 路径格式检查（绝对/盘符/../反斜杠/空段）
            try:
                _assert_relative_path_format(entry.relative_path)
            except ValueError:
                collector.add(
                    code=ERROR_PATH_TRAVERSAL,
                    severity="error",
                    stage="file_integrity",
                    message_key="PATH_TRAVERSAL",
                    location=entry.file_id,
                    file_ref=entry.relative_path,
                    field_path=f"files[{entry.file_id}].relative_path",
                    expected_summary="安全相对路径",
                    actual_summary=entry.relative_path[:200],
                )
                continue
            # 2) 链接/挂载点/重解析点检查（逐级父目录，契约 9.3）
            link_ancestor = _find_link_ancestor(candidate_raw, package_dir)
            if link_ancestor is not None:
                collector.add(
                    code=ERROR_UNSUPPORTED_FILE_ROLE,
                    severity="error",
                    stage="file_integrity",
                    message_key="UNSUPPORTED_FILE_ROLE",
                    location=entry.file_id,
                    file_ref=entry.relative_path,
                    field_path=f"files[{entry.file_id}].relative_path",
                    expected_summary="普通文件（每级父目录均非链接/挂载点）",
                    actual_summary="symbolic link, junction, mount or reparse point",
                )
                continue
            # 2b) .lnk/.url 快捷方式：不解析目标，直接拒绝
            if _is_shortcut_name(entry.relative_path):
                collector.add(
                    code=ERROR_UNSUPPORTED_FILE_ROLE,
                    severity="error",
                    stage="file_integrity",
                    message_key="UNSUPPORTED_FILE_ROLE",
                    location=entry.file_id,
                    file_ref=entry.relative_path,
                    field_path=f"files[{entry.file_id}].relative_path",
                    expected_summary="普通数据文件",
                    actual_summary=".lnk/.url shortcut",
                )
                continue
            # 3) 越界检查（解析后必须仍在包目录内）
            try:
                _assert_relative_path_safe(entry.relative_path, package_dir)
            except ValueError:
                collector.add(
                    code=ERROR_PATH_TRAVERSAL,
                    severity="error",
                    stage="file_integrity",
                    message_key="PATH_TRAVERSAL",
                    location=entry.file_id,
                    file_ref=entry.relative_path,
                    field_path=f"files[{entry.file_id}].relative_path",
                    expected_summary="解析后必须位于包目录内",
                    actual_summary="escapes package root",
                )
                continue
            candidate = candidate_raw.resolve()
            if not candidate.is_file():
                collector.add(
                    code=ERROR_FILE_MISSING,
                    severity="error",
                    stage="file_integrity",
                    message_key="FILE_MISSING",
                    location=entry.file_id,
                    file_ref=entry.relative_path,
                    field_path=f"files[{entry.file_id}].relative_path",
                    expected_summary="文件存在",
                    actual_summary="missing",
                )
                continue
            # 3b) 硬链接别名检查：在读取大小/正文/哈希之前执行
            nlink = _hardlink_nlink(candidate)
            if nlink is None:
                # 平台无 st_nlink 或 stat 失败：fail-closed，不得视为普通文件
                collector.add(
                    code=ERROR_UNSUPPORTED_FILE_ROLE,
                    severity="error",
                    stage="file_integrity",
                    message_key="UNSUPPORTED_FILE_ROLE",
                    location=entry.file_id,
                    file_ref=entry.relative_path,
                    field_path=f"files[{entry.file_id}].relative_path",
                    expected_summary="可判定的普通文件（st_nlink 可用）",
                    actual_summary="st_nlink unavailable: fail-closed",
                )
                continue
            if nlink > 1:
                collector.add(
                    code=ERROR_UNSUPPORTED_FILE_ROLE,
                    severity="error",
                    stage="file_integrity",
                    message_key="UNSUPPORTED_FILE_ROLE",
                    location=entry.file_id,
                    file_ref=entry.relative_path,
                    field_path=f"files[{entry.file_id}].relative_path",
                    expected_summary="st_nlink == 1",
                    actual_summary=f"hard link detected (st_nlink={nlink})",
                )
                continue
            try:
                stat = candidate.stat()
                if stat.st_size != entry.size_bytes:
                    collector.add(
                        code=ERROR_FILE_SIZE_MISMATCH,
                        severity="error",
                        stage="file_integrity",
                        message_key="FILE_SIZE_MISMATCH",
                        location=entry.file_id,
                        file_ref=entry.relative_path,
                        field_path=f"files[{entry.file_id}].size_bytes",
                        expected_summary=str(entry.size_bytes),
                        actual_summary=str(stat.st_size),
                    )
                # 只读哈希计算，不解析内容
                actual_hash = _sha256_bytes(candidate.read_bytes())
                if actual_hash != entry.sha256:
                    collector.add(
                        code=ERROR_HASH_MISMATCH,
                        severity="error",
                        stage="file_integrity",
                        message_key="HASH_MISMATCH",
                        location=entry.file_id,
                        file_ref=entry.relative_path,
                        field_path=f"files[{entry.file_id}].sha256",
                        expected_summary=f"{entry.sha256[:16]}…",
                        actual_summary=f"{actual_hash[:16]}…",
                    )
            except OSError as exc:
                collector.add(
                    code=ERROR_FILE_MISSING,
                    severity="error",
                    stage="file_integrity",
                    message_key="FILE_MISSING",
                    location=entry.file_id,
                    file_ref=entry.relative_path,
                    actual_summary=_safe_exception_summary(exc),
                )
                continue
            validated_count += 1

        ok = not any(i.stage == "file_integrity" and i.code in stage_codes for i in collector.issues)
        checks.append(
            ValidationCheckSummary(
                check_id="FILE_INTEGRITY",
                status="passed" if ok else "failed",
                message_key="CHECK_OK" if ok else "CHECK_FAILED",
            )
        )
        return validated_count, declared_count

    def _unregistered_files_check(
        self,
        package_dir: Path,
        manifest: EvidenceManifest,
        collector: _IssueCollector,
        checks: List[ValidationCheckSummary],
    ) -> int:
        """未登记普通文件 fail-closed（8.4）：只读存在性/类型/脱敏摘要。

        未登记文件不得读取正文；issue 中只保存脱敏摘要（扩展名 + 路径 SHA-256 前缀）。
        未登记的链接、链接目录和 .lnk/.url 快捷方式同样产生错误，不得静默过滤。
        """
        registered = {entry.relative_path for entry in manifest.files}
        unregistered: List[str] = []      # 未登记普通文件（相对路径，仅用于脱敏摘要）
        unsupported: List[str] = []       # 未登记链接/快捷方式（相对路径，仅用于脱敏摘要）
        for root, dirs, files in os.walk(package_dir, followlinks=False):
            kept_dirs: List[str] = []
            for d in dirs:
                dp = Path(root) / d
                rel = os.path.relpath(str(dp), str(package_dir)).replace("\\", "/")
                if rel in registered:
                    kept_dirs.append(d)
                    continue
                # 未登记目录：若为链接/挂载点则记录错误，不进入
                if _is_link(dp):
                    unsupported.append(rel)
                    continue
                kept_dirs.append(d)
            dirs[:] = kept_dirs
            for fname in files:
                fpath = Path(root) / fname
                rel = os.path.relpath(str(fpath), str(package_dir)).replace("\\", "/")
                if rel in CONTROL_FILES:
                    continue
                if rel in registered:
                    continue
                if _is_link(fpath) or _is_shortcut_name(fname):
                    # 未登记链接/快捷方式：不得静默过滤
                    unsupported.append(rel)
                    continue
                # 未登记硬链接别名：fail-closed，不得静默跳过或仅记普通未登记
                nlink = _hardlink_nlink(fpath)
                if nlink is None or nlink > 1:
                    unsupported.append(rel)
                    continue
                # 只记录脱敏相对路径，不读取正文
                unregistered.append(rel)

        for rel in unregistered:
            collector.add(
                code=ERROR_UNREGISTERED_PACKAGE_FILE,
                severity="error",
                stage="unregistered_files",
                message_key="UNREGISTERED_PACKAGE_FILE",
                location="unregistered_file",
                expected_summary="仅三类控制文件与 manifest 登记文件",
                actual_summary=_unregistered_summary(rel),
            )
        for rel in unsupported:
            collector.add(
                code=ERROR_UNSUPPORTED_FILE_ROLE,
                severity="error",
                stage="unregistered_files",
                message_key="UNSUPPORTED_FILE_ROLE",
                location="unregistered_link",
                expected_summary="包内不允许未登记链接/快捷方式",
                actual_summary=_unregistered_summary(rel),
            )
        total = len(unregistered) + len(unsupported)
        ok = not total
        checks.append(
            ValidationCheckSummary(
                check_id="UNREGISTERED_FILES",
                status="passed" if ok else "failed",
                message_key="CHECK_OK" if ok else "CHECK_FAILED",
            )
        )
        return len(unregistered)

    def _privacy_gate(
        self,
        evidence: EvidencePackage,
        manifest: EvidenceManifest,
        collector: _IssueCollector,
        checks: List[ValidationCheckSummary],
    ) -> Tuple[bool, bool]:
        """隐私门（8.5/10.5）：结论 model_input_allowed / registration_allowed。"""
        model_input_allowed = True
        registration_allowed = True

        ev_pc = evidence.privacy_check
        mv_pc = manifest.privacy_check

        if ev_pc.status != "passed" or mv_pc.status != "passed":
            collector.add(
                code=ERROR_PRIVACY_CHECK_FAILED,
                severity="error",
                stage="privacy_gate",
                message_key="PRIVACY_CHECK_FAILED",
                location="privacy_check.status",
                field_path="privacy_check.status",
                expected_summary="passed",
                actual_summary=f"evidence={ev_pc.status}, manifest={mv_pc.status}",
            )
            model_input_allowed = registration_allowed = False
        if ev_pc.contains_direct_identity:
            collector.add(
                code=ERROR_PRIVACY_CHECK_FAILED,
                severity="error",
                stage="privacy_gate",
                message_key="PRIVACY_CHECK_FAILED",
                location="privacy_check.contains_direct_identity",
                field_path="privacy_check.contains_direct_identity",
                expected_summary="false",
                actual_summary="true",
            )
            model_input_allowed = registration_allowed = False
        if ev_pc.raw_audio_included or ev_pc.raw_video_included:
            collector.add(
                code=ERROR_PRIVACY_CHECK_FAILED,
                severity="error",
                stage="privacy_gate",
                message_key="PRIVACY_CHECK_FAILED",
                location="privacy_check.raw_media",
                field_path="privacy_check",
                expected_summary="raw_audio/raw_video 必须 false",
                actual_summary="raw media included",
            )
            model_input_allowed = registration_allowed = False

        for entry in manifest.files:
            if entry.model_input_allowed and entry.privacy_status != "passed":
                collector.add(
                    code=ERROR_PRIVACY_CHECK_FAILED,
                    severity="error",
                    stage="privacy_gate",
                    message_key="PRIVACY_CHECK_FAILED",
                    location=entry.file_id,
                    file_ref=entry.relative_path,
                    field_path=f"files[{entry.file_id}].privacy_status",
                    expected_summary="passed",
                    actual_summary=entry.privacy_status,
                )
                model_input_allowed = registration_allowed = False

        for item in evidence.evidence_items:
            if item.model_input_allowed:
                for src in item.source_refs:
                    entry = next((f for f in manifest.files if f.file_id == src), None)
                    if entry is None:
                        collector.add(
                            code=ERROR_UNAPPROVED_MODEL_INPUT,
                            severity="error",
                            stage="privacy_gate",
                            message_key="UNAPPROVED_MODEL_INPUT",
                            location=item.evidence_id,
                            file_ref=src,
                            field_path=f"evidence_items[{item.evidence_id}].source_refs",
                            expected_summary="source_ref 必须存在于 manifest",
                            actual_summary="引用不存在的 file_id",
                        )
                        model_input_allowed = registration_allowed = False
                    elif not entry.model_input_allowed:
                        collector.add(
                            code=ERROR_UNAPPROVED_MODEL_INPUT,
                            severity="error",
                            stage="privacy_gate",
                            message_key="UNAPPROVED_MODEL_INPUT",
                            location=item.evidence_id,
                            file_ref=entry.relative_path,
                            field_path=f"evidence_items[{item.evidence_id}].source_refs",
                            expected_summary="仅可引用 model_input_allowed=true 的文件",
                            actual_summary="引用未授权文件",
                        )
                        model_input_allowed = registration_allowed = False

        ok = not any(
            i.stage == "privacy_gate"
            and i.code in (ERROR_PRIVACY_CHECK_FAILED, ERROR_UNAPPROVED_MODEL_INPUT)
            for i in collector.issues
        )
        checks.append(
            ValidationCheckSummary(
                check_id="PRIVACY_GATE",
                status="passed" if ok else "failed",
                message_key="CHECK_OK" if ok else "CHECK_FAILED",
            )
        )
        return model_input_allowed, registration_allowed

    def _evidence_level_check(
        self,
        evidence: EvidencePackage,
        collector: _IssueCollector,
        checks: List[ValidationCheckSummary],
    ) -> None:
        """证据等级一致性（11.1）：仅做可确定性判定的约束。"""
        level = evidence.evidence_level
        if level == "sufficient":
            if not evidence.evidence_items:
                collector.add(
                    code=ERROR_SCHEMA_INVALID,
                    severity="error",
                    stage="evidence_level",
                    message_key="SCHEMA_INVALID",
                    location="evidence_level",
                    field_path="evidence_level",
                    expected_summary="sufficient 至少一个 evidence item",
                    actual_summary="evidence_items 为空",
                )
            if evidence.manual_review_reasons:
                collector.add(
                    code=ERROR_SCHEMA_INVALID,
                    severity="error",
                    stage="evidence_level",
                    message_key="SCHEMA_INVALID",
                    location="evidence_level",
                    field_path="manual_review_reasons",
                    expected_summary="sufficient 无人工复核原因",
                    actual_summary="manual_review_reasons 非空",
                )
        ok = not any(i.stage == "evidence_level" for i in collector.issues)
        checks.append(
            ValidationCheckSummary(
                check_id="EVIDENCE_LEVEL",
                status="passed" if ok else "failed",
                message_key="CHECK_OK" if ok else "CHECK_FAILED",
            )
        )

    # -- 结果构造 ------------------------------------------------------

    def _build_result(
        self,
        collector: _IssueCollector,
        checks: List[ValidationCheckSummary],
        package_id: Optional[str],
        package_revision: Optional[int],
        manifest_hash: Optional[str],
        privacy_version: Optional[str],
        started: datetime,
        model_input_allowed: bool,
        registration_allowed: bool,
        validated_file_count: int,
        declared_file_count: int,
        unregistered_file_count: int,
        internal_error: bool,
        mode: str,
        evidence_level: Optional[str] = None,
    ) -> ValidationResult:
        completed = datetime.now(_UTC)
        duration_ms = max(0, int((completed - started).total_seconds() * 1000))

        if internal_error:
            status = "internal_error"
        elif collector.has_error:
            status = "failed"
        else:
            status = "passed"

        validation_id = f"val-{uuid.uuid4().hex[:16]}"

        # result_sha256：基于稳定业务内容计算（排除运行时字段与自身字段，保证同输入稳定）
        stable_content = {
            "package_id": package_id or "unknown",
            "package_revision": package_revision if package_revision is not None else 1,
            "manifest_sha256": manifest_hash or "0" * 64,
            "status": status,
            "model_input_allowed": model_input_allowed,
            "registration_allowed": registration_allowed,
            "checks": [c.model_dump(mode="json") for c in checks],
            "issue_refs": [r.model_dump(mode="json") for r in collector.refs],
        }
        result_hash = _sha256_bytes(canonical_json_bytes(stable_content))

        result = ValidationResult(
            validation_schema_version="evidence-sidecar/validation/v1",
            validation_id=validation_id,
            record_id=None,  # dry-run 不持久化
            package_id=stable_content["package_id"],
            package_revision=stable_content["package_revision"],
            manifest_sha256=stable_content["manifest_sha256"],
            validator_name=self.validator_name,
            validator_version=self.validator_version,
            evidence_contract_version=EVIDENCE_CONTRACT_VERSION,
            privacy_policy_version=privacy_version or DEFAULT_PRIVACY_POLICY_VERSION,
            mode=mode,  # type: ignore[arg-type]
            status=status,  # type: ignore[arg-type]
            started_at=started,
            completed_at=completed,
            duration_ms=duration_ms,
            checks=checks,
            issues=collector.refs,
            issue_counts=collector.counts,
            model_input_allowed=model_input_allowed and not collector.has_error,
            registration_allowed=registration_allowed and not collector.has_error,
            validated_file_count=validated_file_count,
            declared_file_count=declared_file_count,
            unregistered_file_count=unregistered_file_count,
            result_sha256=result_hash,
            evidence_level=evidence_level,  # 权威验证事实：来自已校验 EvidencePackage.evidence_level
        )
        return result


def validate_package(
    package_dir: Path,
    controlled_root: Path,
    mode: str = "dry_run",
) -> Tuple[ValidationResult, List[ValidationIssue]]:
    """便捷函数入口：返回 (result, issues)。"""
    return EvidencePackageValidator(controlled_root).validate(package_dir, mode=mode)
