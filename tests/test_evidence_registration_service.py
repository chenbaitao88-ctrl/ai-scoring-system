"""
Phase 11B-1c（修正版）：EvidencePackage 单机文件型登记服务合成测试。

权威契约：fix-2 + fix-4（RegistrationIndex 容器）。
所有测试使用 tmp_path 与合成脱敏 EvidencePackage，不读取真实学生材料。

覆盖：
- dry-run 零写入。
- 首次登记成功；新目录布局准确，不出现旧目录（records/validations/issues/index/current.json/.locks）。
- ValidationResult issues 对象数组往返。
- 两个不同 package 并存于 index。
- 同 package 两个 revision 并存。
- validator_version / privacy_policy_version / mode 不同不错误命中幂等。
- 相同七字段命中幂等。
- expected index revision 冲突。
- index hash 损坏。
- index 原子写失败保留旧文件。
- rebuild 包含全部 records。
- rebuild 遇损坏 record / 缺 validation 显式失败。
- rejected audit 在锁内。
- Audit 写失败返回 SIDECAR_AUDIT_WRITE_FAILED。
- Audit 恢复产生 recovery_performed。
- Sidecar 无绝对路径、PII、凭据。
- 输入包保持不变。
- 不读取未登记文件正文。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from models.evidence_sidecar import (
    RegistrationIndex,
    RegistrationIndexEntry,
    ValidationResult,
)
from services.evidence_package_validator import canonical_json_bytes
from services.evidence_registration_service import (
    ERR_AUDIT_WRITE_FAILED,
    ERR_HASH_MISMATCH,
    ERR_IDEMPOTENCY_CONFLICT,
    ERR_INDEX_CORRUPTED,
    ERR_INDEX_WRITE_FAILED,
    ERR_LOCK_CONFLICT,
    ERR_REVISION_CONFLICT,
    ERR_RECORD_CORRUPTED,
    ERR_VALIDATION_NOT_FOUND,
    EvidenceRegistrationService,
    RegistrationError,
)

UTC = timezone.utc


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- fixture 构造


def privacy_check(**overrides):
    data = {
        "status": "passed",
        "privacy_policy_version": "privacy-policy/v1.1",
        "checked_at": "2026-08-05T07:00:00Z",
        "checker": "synthetic-privacy-checker",
        "findings": [],
        "contains_face": False,
        "contains_direct_identity": False,
        "transcript_redacted": True,
        "raw_audio_included": False,
        "raw_video_included": False,
    }
    data.update(overrides)
    return data


def package_common(**overrides):
    data = {
        "contract_version": "evidence-package/v1.1",
        "package_id": "pkg-demo-001",
        "batch_id": "batch-demo-001",
        "submission_id": "sub-demo-001",
        "evidence_version": "1",
        "package_revision": 1,
        "manifest_sha256": "0" * 64,
        "generated_at": "2026-08-05T07:00:00Z",
        "generator_name": "synthetic-generator",
        "generator_version": "1.0.0",
        "publication_status": "ready",
        "completion_marker": ".evidence-ready",
    }
    data.update(overrides)
    return data


def evidence_body(**overrides):
    data = {
        **package_common(),
        "evidence_level": "sufficient",
        "evidence_items": [
            {
                "evidence_id": "ev_code_001",
                "evidence_type": "code_summary",
                "summary": "合成摘要：含主循环与输入校验。",
                "availability": "available",
                "source_refs": ["file-code-001"],
                "model_input_allowed": True,
                "limitations": [],
            }
        ],
        "availability": {
            "code": "available",
            "document": "available",
            "video": "not_applicable",
            "transcript": "not_applicable",
        },
        "missing_evidence": [],
        "manual_review_reasons": [],
        "privacy_check": privacy_check(),
        "rejection_reasons": [],
    }
    data.update(overrides)
    return data


def manifest_body(**overrides):
    data = {
        **package_common(),
        "files": [
            {
                "file_id": "file-code-001",
                "relative_path": "evidence/code_summary.json",
                "file_role": "code_summary",
                "mime_type": "application/json",
                "media_type": "json",
                "size_bytes": 0,
                "sha256": "0" * 64,
                "privacy_status": "passed",
                "model_input_allowed": True,
                "derived_from_raw": True,
                "redaction_applied": True,
            }
        ],
        "privacy_check": privacy_check(),
    }
    data.update(overrides)
    return data


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def build_package(
    input_root: Path,
    package_id: str = "pkg-demo-001",
    revision: int = 1,
    validator_version: str = "1.0.0",
    privacy_policy_version: str = "privacy-policy/v1.1",
) -> Path:
    """构造合法 sufficient 包并正确计算 manifest 哈希。"""
    pkg_dir = input_root / package_id
    pkg_dir.mkdir(parents=True, exist_ok=True)

    content = b'{"synthetic": true, "summary": "code structure"}'
    code_file = pkg_dir / "evidence" / "code_summary.json"
    code_file.parent.mkdir(parents=True, exist_ok=True)
    code_file.write_bytes(content)

    manifest = manifest_body(package_id=package_id, package_revision=revision)
    manifest["files"][0]["size_bytes"] = len(content)
    manifest["files"][0]["sha256"] = sha256_bytes(content)

    obj = dict(manifest)
    obj["manifest_sha256"] = None
    h = sha256_bytes(canonical_json_bytes(obj))
    manifest["manifest_sha256"] = h
    write_json(pkg_dir / "evidence_manifest.json", manifest)

    evidence = evidence_body(package_id=package_id, package_revision=revision)
    evidence["manifest_sha256"] = h
    write_json(pkg_dir / "evidence.json", evidence)

    (pkg_dir / ".evidence-ready").write_text("", encoding="utf-8")
    return pkg_dir


@pytest.fixture
def env(tmp_path):
    input_root = tmp_path / "controlled-input-root"
    runtime_root = tmp_path / "runtime-data-root"
    input_root.mkdir()
    runtime_root.mkdir()
    return input_root, runtime_root


@pytest.fixture
def service(env):
    input_root, runtime_root = env
    return EvidenceRegistrationService(input_root, runtime_root)


def _collect_files(root: Path):
    return sorted(
        str(p.relative_to(root)).replace("\\", "/")
        for p in root.rglob("*")
        if p.is_file()
    )


# ---------------------------------------------------------------- 模型：容器


def test_registration_index_model_parses():
    T = datetime(2026, 8, 5, 7, 0, 0, tzinfo=UTC)
    entry = RegistrationIndexEntry(
        index_schema_version="evidence-sidecar/index/v1",
        entry_id="entry-1",
        record_id="record-1",
        package_id="pkg-a",
        package_revision=1,
        batch_id="batch-a",
        submission_id="sub-a",
        evidence_version="1",
        manifest_sha256="a" * 64,
        registration_status="registered",
        latest_validation_status="passed",
        model_input_allowed=True,
        record_ref="packages/pkg-a/revisions/1/package-record.json",
        latest_validation_ref="packages/pkg-a/revisions/1/validations/val-1.json",
        registered_at=T,
        updated_at=T,
        revision=1,
    )
    idx = RegistrationIndex(
        index_schema_version="evidence-sidecar/index-container/v1",
        index_id="index-1",
        revision=1,
        created_at=T,
        updated_at=T,
        entries=[entry],
        index_sha256="0" * 64,
    )
    assert idx.revision == 1
    assert len(idx.entries) == 1


def test_registration_index_unknown_field_rejected():
    T = datetime(2026, 8, 5, 7, 0, 0, tzinfo=UTC)
    with pytest.raises(ValidationError):
        RegistrationIndex(
            index_schema_version="evidence-sidecar/index-container/v1",
            index_id="index-1",
            revision=1,
            created_at=T,
            updated_at=T,
            entries=[],
            index_sha256="0" * 64,
            extra_field="x",
        )


def test_registration_index_duplicate_key_rejected():
    T = datetime(2026, 8, 5, 7, 0, 0, tzinfo=UTC)

    def entry(pkg, rev, mh):
        return RegistrationIndexEntry(
            index_schema_version="evidence-sidecar/index/v1",
            entry_id=f"e-{pkg}-{rev}",
            record_id=f"r-{pkg}-{rev}",
            package_id=pkg,
            package_revision=rev,
            batch_id="batch-a",
            submission_id="sub-a",
            evidence_version="1",
            manifest_sha256=mh,
            registration_status="registered",
            latest_validation_status="passed",
            model_input_allowed=True,
            record_ref=f"packages/{pkg}/revisions/{rev}/package-record.json",
            latest_validation_ref=f"packages/{pkg}/revisions/{rev}/validations/v.json",
            registered_at=T,
            updated_at=T,
            revision=1,
        )

    with pytest.raises(ValidationError):
        RegistrationIndex(
            index_schema_version="evidence-sidecar/index-container/v1",
            index_id="index-1",
            revision=1,
            created_at=T,
            updated_at=T,
            entries=[entry("pkg-a", 1, "a" * 64), entry("pkg-a", 1, "b" * 64)],
            index_sha256="0" * 64,
        )


def test_registration_index_wrong_sort_rejected():
    T = datetime(2026, 8, 5, 7, 0, 0, tzinfo=UTC)

    def entry(pkg, rev, mh):
        return RegistrationIndexEntry(
            index_schema_version="evidence-sidecar/index/v1",
            entry_id=f"e-{pkg}-{rev}",
            record_id=f"r-{pkg}-{rev}",
            package_id=pkg,
            package_revision=rev,
            batch_id="batch-a",
            submission_id="sub-a",
            evidence_version="1",
            manifest_sha256=mh,
            registration_status="registered",
            latest_validation_status="passed",
            model_input_allowed=True,
            record_ref=f"packages/{pkg}/revisions/{rev}/package-record.json",
            latest_validation_ref=f"packages/{pkg}/revisions/{rev}/validations/v.json",
            registered_at=T,
            updated_at=T,
            revision=1,
        )

    with pytest.raises(ValidationError):
        RegistrationIndex(
            index_schema_version="evidence-sidecar/index-container/v1",
            index_id="index-1",
            revision=1,
            created_at=T,
            updated_at=T,
            entries=[entry("pkg-b", 1, "b" * 64), entry("pkg-a", 1, "a" * 64)],
            index_sha256="0" * 64,
        )


def test_registration_index_invalid_hash_revision_utc_rejected():
    import datetime as _dt

    T = _dt.datetime(2026, 8, 5, 7, 0, 0, tzinfo=UTC)
    base = dict(
        index_schema_version="evidence-sidecar/index-container/v1",
        index_id="index-1",
        revision=1,
        created_at=T,
        updated_at=T,
        entries=[],
        index_sha256="a" * 64,
    )
    with pytest.raises(ValidationError):
        bad_hash = dict(base)
        bad_hash["index_sha256"] = "Z" * 64
        RegistrationIndex(**bad_hash)
    bad_rev = dict(base)
    bad_rev["revision"] = 0
    with pytest.raises(ValidationError):
        RegistrationIndex(**bad_rev)
    bad_naive = dict(base)
    bad_naive["created_at"] = _dt.datetime(2026, 8, 5, 7, 0, 0)  # naive
    with pytest.raises(ValidationError):
        RegistrationIndex(**bad_naive)
    bad_order = dict(base)
    bad_order["updated_at"] = T - _dt.timedelta(days=1)
    with pytest.raises(ValidationError):
        RegistrationIndex(**bad_order)


# ---------------------------------------------------------------- 布局


def test_new_directory_layout_no_old_dirs(env, service):
    input_root, _ = env
    pkg = build_package(input_root)
    service.register(pkg)
    files = _collect_files(service.sidecar_root)
    # 新布局
    assert any(f.startswith("packages/pkg-demo-001/revisions/1/package-record.json") for f in files)
    assert any(f.startswith("packages/pkg-demo-001/revisions/1/validations/") for f in files)
    assert any(f.startswith("indexes/registration-index.json") for f in files)
    assert any(f.startswith("events/evidence-registration.ndjson") for f in files)
    # 旧布局不得出现
    for old in ("records/", "validations/", "issues/", "index/current.json", ".locks/"):
        assert not any(f.startswith(old) for f in files), f"旧目录出现: {old}"


def test_validation_issues_object_array_roundtrip(env, service):
    """ValidationResult.issues 落盘为 {issue_id, code, severity} 对象数组，可重新解析。"""
    import services.evidence_registration_service as reg_svc

    input_root, _ = env
    # 构造含 warning issue 的合法包：改 evidence 加 manual_review_reasons 且 level=limited
    pkg = build_package(input_root, package_id="pkg-warn")
    ev_path = pkg / "evidence.json"
    ev = json.loads(ev_path.read_text(encoding="utf-8"))
    ev["evidence_level"] = "limited"
    ev["manual_review_reasons"] = ["CORE_EVIDENCE_PARTIAL"]
    write_json(ev_path, ev)

    # 打补丁：让验证器产生一个 warning issue（注入到验证结果后再写入）
    real_validate = reg_svc.EvidencePackageValidator.validate

    def patched_validate(self, package_dir, mode="dry_run"):
        result, issues = real_validate(self, package_dir, mode=mode)
        if mode != "dry_run":
            from models.evidence_sidecar import ValidationIssue

            issues = issues + [
                ValidationIssue(
                    issue_id="issue-synthetic-warning-0001",
                    code="SYNTHETIC_WARNING",
                    severity="warning",
                    stage="evidence_level",
                    message_key="SYNTHETIC_WARNING",
                    location="evidence_level",
                    expected_summary="passed",
                    actual_summary="limited",
                    retryable=False,
                    blocks_registration=False,
                    blocks_model_input=False,
                    requires_manual_review=False,
                    created_at=__import__("datetime").datetime(2026, 8, 5, 7, 0, 0, tzinfo=UTC),
                )
            ]
        return result, issues

    reg_svc.EvidencePackageValidator.validate = patched_validate
    try:
        outcome = service.register(pkg)
    finally:
        reg_svc.EvidencePackageValidator.validate = real_validate

    # 读取落盘的 validation 并重新解析
    val_files = list((service.sidecar_root / "packages" / "pkg-warn" / "revisions" / "1" / "validations").glob("*.json"))
    assert val_files
    obj = json.loads(val_files[0].read_text(encoding="utf-8"))
    parsed = ValidationResult(**obj)
    # 必须是对象数组，含 code/severity
    for ref in parsed.issues:
        assert isinstance(ref, dict) or hasattr(ref, "code")
        assert ref.code and ref.severity
    # 从落盘 JSON 直接断言字段结构
    assert all(isinstance(i, dict) and "issue_id" in i and "code" in i and "severity" in i for i in obj["issues"])


# ---------------------------------------------------------------- 幂等


def test_same_seven_fields_hits_idempotency(env, service):
    input_root, _ = env
    pkg = build_package(input_root)
    out1 = service.register(pkg)
    files1 = _collect_files(service.sidecar_root)
    out2 = service.register(pkg)
    assert out2.idempotent_hit is True
    assert _collect_files(service.sidecar_root) == files1


def test_different_validator_version_no_false_idempotency(env):
    input_root, runtime_root = env
    svc1 = EvidenceRegistrationService(input_root, runtime_root, validator_version="1.0.0")
    svc2 = EvidenceRegistrationService(input_root, runtime_root, validator_version="2.0.0")
    pkg = build_package(input_root)
    out1 = svc1.register(pkg)
    assert out1.idempotent_hit is False
    # 同包、同 manifest，但 validator_version 不同：不得错误命中旧结果
    out2 = svc2.register(pkg)
    assert out2.idempotent_hit is False
    assert out2.rejected is False
    # 产生新的 validation 并更新 record（revision+1），不覆盖
    record = out2.record
    assert record is not None and record.revision >= 2
    # 两条 validation 并存
    vals = list((runtime_root / "phase11/evidence-sidecar/packages/pkg-demo-001/revisions/1/validations").glob("*.json"))
    assert len(vals) >= 2


def test_different_privacy_policy_no_false_idempotency(env):
    input_root, runtime_root = env
    svc1 = EvidenceRegistrationService(input_root, runtime_root, validator_version="1.0.0")
    pkg1 = build_package(input_root, package_id="pkg-priv")
    out1 = svc1.register(pkg1)
    assert out1.idempotent_hit is False
    # 修改 privacy policy 后重新验证（同 manifest）
    svc2 = EvidenceRegistrationService(input_root, runtime_root, validator_version="1.0.0")
    # 直接调用 register 但让验证器产生不同 privacy_policy_version
    import services.evidence_registration_service as reg_svc

    real_validate = reg_svc.EvidencePackageValidator.validate

    def patched_validate(self, package_dir, mode="dry_run"):
        result, issues = real_validate(self, package_dir, mode=mode)
        result = result.model_copy(
            update={"privacy_policy_version": "privacy-policy/v2.0"}
        )
        return result, issues

    reg_svc.EvidencePackageValidator.validate = patched_validate
    try:
        out2 = svc2.register(pkg1)
    finally:
        reg_svc.EvidencePackageValidator.validate = real_validate
    assert out2.idempotent_hit is False
    assert out2.rejected is False


def test_different_mode_no_false_idempotency(env, service):
    input_root, _ = env
    pkg = build_package(input_root)
    out1 = service.register(pkg, mode="registration")
    assert out1.idempotent_hit is False
    out2 = service.register(pkg, mode="revalidation")
    assert out2.idempotent_hit is False
    assert out2.rejected is False


def test_same_identity_different_manifest_conflict(env, service):
    input_root, _ = env
    pkg = build_package(input_root)
    service.register(pkg)
    mv_path = pkg / "evidence_manifest.json"
    mv = json.loads(mv_path.read_text(encoding="utf-8"))
    mv["files"][0]["mime_type"] = "text/plain"
    obj = dict(mv)
    obj["manifest_sha256"] = None
    mv["manifest_sha256"] = sha256_bytes(canonical_json_bytes(obj))
    write_json(mv_path, mv)
    ev_path = pkg / "evidence.json"
    ev = json.loads(ev_path.read_text(encoding="utf-8"))
    ev["manifest_sha256"] = mv["manifest_sha256"]
    write_json(ev_path, ev)
    with pytest.raises(RegistrationError) as excinfo:
        service.register(pkg)
    assert excinfo.value.code == ERR_IDEMPOTENCY_CONFLICT


# ---------------------------------------------------------------- 失败路径


def test_failed_validation_no_record_no_index(env, service):
    input_root, _ = env
    pkg = build_package(input_root)
    ev_path = pkg / "evidence.json"
    ev = json.loads(ev_path.read_text(encoding="utf-8"))
    ev["privacy_check"]["contains_direct_identity"] = True
    write_json(ev_path, ev)
    outcome = service.register(pkg)
    assert outcome.rejected is True
    files = _collect_files(service.sidecar_root)
    assert not any(f.startswith("packages/") for f in files)
    assert not any(f.startswith("indexes/") for f in files)
    audit = service.audit_path.read_text(encoding="utf-8") if service.audit_path.is_file() else ""
    assert "registration_rejected" in audit


# ---------------------------------------------------------------- index 容器


def test_two_packages_coexist_in_index(env, service):
    input_root, _ = env
    p1 = build_package(input_root, package_id="pkg-aaa")
    p2 = build_package(input_root, package_id="pkg-bbb")
    service.register(p1)
    obj1 = json.loads(service.index_path.read_text(encoding="utf-8"))
    assert obj1["revision"] == 1  # 第一次登记精确 revision=1
    service.register(p2)
    obj2 = json.loads(service.index_path.read_text(encoding="utf-8"))
    assert obj2["revision"] == 2  # 第二次新 entry 精确 revision=2
    assert len(obj2["entries"]) == 2
    keys = [(e["package_id"], e["package_revision"]) for e in obj2["entries"]]
    assert keys == sorted(keys)


def test_same_package_two_revisions_coexist(env, service):
    input_root, _ = env
    # 同一 package_id 的 revision 1 与 revision 2 并存（不同目录避免覆盖）
    p1 = build_package(input_root, package_id="pkg-multi", revision=1)
    service.register(p1)
    p2 = build_package(input_root, package_id="pkg-multi", revision=2)
    service.register(p2)
    obj = json.loads(service.index_path.read_text(encoding="utf-8"))
    revs = sorted(e["package_revision"] for e in obj["entries"] if e["package_id"] == "pkg-multi")
    assert revs == [1, 2]
    assert obj["revision"] == 2


def test_expected_index_revision_zero_ok_first_create(env, service):
    """不存在 Index 且传 0：期望不存在且确实不存在 -> 首次创建 revision=1。"""
    input_root, _ = env
    pkg = build_package(input_root)
    service.register(pkg, expected_index_revision=0)
    obj = json.loads(service.index_path.read_text(encoding="utf-8"))
    assert obj["revision"] == 1


def test_expected_index_revision_zero_conflict_when_exists(env, service):
    """已存在 Index 且传 0：期望不存在但实际存在 -> SIDECAR_REVISION_CONFLICT。"""
    input_root, _ = env
    pkg = build_package(input_root)
    service.register(pkg)
    p2 = build_package(input_root, package_id="pkg-second")
    with pytest.raises(RegistrationError) as excinfo:
        service.register(p2, expected_index_revision=0)
    assert excinfo.value.code == ERR_REVISION_CONFLICT


def test_expected_index_revision_positive_conflict_when_missing(env, service):
    """不存在 Index 且传正整数：期望存在但实际不存在 -> 冲突。"""
    input_root, _ = env
    pkg = build_package(input_root)
    with pytest.raises(RegistrationError) as excinfo:
        service.register(pkg, expected_index_revision=1)
    assert excinfo.value.code == ERR_REVISION_CONFLICT


def test_expected_index_revision_wrong_positive_conflict(env, service):
    """已存在 Index 且传错误正整数 -> 冲突。"""
    input_root, _ = env
    pkg = build_package(input_root)
    service.register(pkg)  # revision=1
    p2 = build_package(input_root, package_id="pkg-second")
    with pytest.raises(RegistrationError) as excinfo:
        service.register(p2, expected_index_revision=99)
    assert excinfo.value.code == ERR_REVISION_CONFLICT


def test_expected_index_revision_none_uses_current(env, service):
    """None：锁内读取当前 revision 作为内部 expected，正常继续。"""
    input_root, _ = env
    pkg = build_package(input_root)
    service.register(pkg)  # revision=1
    p2 = build_package(input_root, package_id="pkg-second")
    service.register(p2, expected_index_revision=None)  # 内部 expected=1 -> revision=2
    obj = json.loads(service.index_path.read_text(encoding="utf-8"))
    assert obj["revision"] == 2


def test_rebuild_first_creation_revision_1(env, service):
    """rebuild 首次创建：index 不存在时重建为 revision=1。"""
    input_root, _ = env
    pkg = build_package(input_root)
    service.register(pkg)
    service.index_path.unlink()
    idx = service.rebuild_index()
    assert idx.revision == 1
    disk = json.loads(service.index_path.read_text(encoding="utf-8"))
    assert disk["revision"] == 1
    # 返回对象 index_sha256 == 落盘值且重算一致
    assert idx.index_sha256 == disk["index_sha256"]
    from models.evidence_sidecar import compute_index_sha256

    assert compute_index_sha256(idx) == idx.index_sha256
    assert idx.index_sha256 != "0" * 64


def test_index_hash_corruption_detected(env, service):
    input_root, _ = env
    pkg = build_package(input_root)
    service.register(pkg)
    obj = json.loads(service.index_path.read_text(encoding="utf-8"))
    obj["index_sha256"] = "f" * 64
    write_json(service.index_path, obj)
    # 再次登记会先校验旧容器 hash -> 显式失败
    with pytest.raises(RegistrationError) as excinfo:
        service.register(pkg)
    assert excinfo.value.code == ERR_HASH_MISMATCH


def test_index_atomic_write_failure_keeps_old(env, service, monkeypatch):
    import services.evidence_registration_service as reg_svc

    input_root, _ = env
    pkg = build_package(input_root)
    service.register(pkg)
    old_bytes = service.index_path.read_bytes()

    real_atomic = reg_svc._atomic_write

    def flaky(target, obj):
        if "registration-index.json" in str(target):
            raise OSError("simulated index write failure")
        return real_atomic(target, obj)

    monkeypatch.setattr(reg_svc, "_atomic_write", flaky)
    p2 = build_package(input_root, package_id="pkg-second")
    with pytest.raises(RegistrationError) as excinfo:
        service.register(p2)
    assert excinfo.value.code == ERR_INDEX_WRITE_FAILED
    monkeypatch.undo()
    # 旧索引字节保留
    assert service.index_path.read_bytes() == old_bytes


# ---------------------------------------------------------------- rebuild


def test_rebuild_includes_all_records(env, service):
    input_root, _ = env
    p1 = build_package(input_root, package_id="pkg-aaa")
    p2 = build_package(input_root, package_id="pkg-bbb")
    service.register(p1)
    service.register(p2)
    # 删除 index 后重建
    service.index_path.unlink()
    index = service.rebuild_index()
    keys = [(e.package_id, e.package_revision) for e in index.entries]
    assert len(keys) == 2
    assert keys == sorted(keys)
    audit = service.audit_path.read_text(encoding="utf-8")
    assert "index_rebuilt" in audit


def test_rebuild_corrupted_record_fails(env, service):
    input_root, _ = env
    pkg = build_package(input_root)
    service.register(pkg)
    rec = service.sidecar_root / "packages/pkg-demo-001/revisions/1/package-record.json"
    rec.write_text("{corrupted", encoding="utf-8")
    with pytest.raises(RegistrationError) as excinfo:
        service.rebuild_index()
    assert excinfo.value.code == ERR_RECORD_CORRUPTED


def test_rebuild_missing_validation_fails(env, service):
    input_root, _ = env
    pkg = build_package(input_root)
    service.register(pkg)
    for f in (service.sidecar_root / "packages/pkg-demo-001/revisions/1/validations").glob("*.json"):
        f.unlink()
    with pytest.raises(RegistrationError) as excinfo:
        service.rebuild_index()
    assert excinfo.value.code == ERR_VALIDATION_NOT_FOUND


# ---------------------------------------------------------------- 锁


def test_lock_conflict(env, service):
    input_root, _ = env
    pkg = build_package(input_root)
    from services.evidence_registration_service import _SingleWriterLock

    lock = _SingleWriterLock(service.lock_path, timeout_ms=200)
    lock.acquire()
    try:
        with pytest.raises(RegistrationError) as excinfo:
            service.register(pkg)
        assert excinfo.value.code == ERR_LOCK_CONFLICT
    finally:
        lock.release()


# ---------------------------------------------------------------- audit


def test_rejected_audit_within_lock(env, service):
    """rejected audit 在锁内：锁被占用时 rejected 路径也返回锁冲突。"""
    input_root, _ = env
    pkg = build_package(input_root)
    from services.evidence_registration_service import _SingleWriterLock

    lock = _SingleWriterLock(service.lock_path, timeout_ms=200)
    lock.acquire()
    try:
        with pytest.raises(RegistrationError) as excinfo:
            service.register(pkg)  # 合法包也会先拿锁
        assert excinfo.value.code == ERR_LOCK_CONFLICT
    finally:
        lock.release()


def test_audit_write_failure_returns_error(env, service, monkeypatch):
    import services.evidence_registration_service as reg_svc

    input_root, _ = env
    pkg = build_package(input_root)

    real_append = reg_svc._append_ndjson

    def flaky(path, obj):
        if "ndjson" in str(path):
            raise OSError("simulated audit write failure")
        return real_append(path, obj)

    monkeypatch.setattr(reg_svc, "_append_ndjson", flaky)
    with pytest.raises(RegistrationError) as excinfo:
        service.register(pkg)
    assert excinfo.value.code == ERR_AUDIT_WRITE_FAILED
    monkeypatch.undo()
    # record 已写成功但 audit 缺失 -> 再次登记应恢复
    assert (service.sidecar_root / "packages/pkg-demo-001/revisions/1/package-record.json").is_file()
    out = service.register(pkg)
    assert out.idempotent_hit is True
    audit = service.audit_path.read_text(encoding="utf-8")
    assert "recovery_performed" in audit
    assert "idempotent_hit" in audit


def _assert_full_event_set_and_sequence(audit_text: str) -> None:
    """断言完整事件集合存在、sequence 严格递增且不重复。"""
    events = []
    for line in audit_text.splitlines():
        if not line.strip():
            continue
        events.append(json.loads(line))
    seqs = [e["sequence"] for e in events]
    assert seqs == sorted(seqs), "sequence 必须严格递增"
    assert len(set(seqs)) == len(seqs), "sequence 不得重复"
    types = {e["event_type"] for e in events}
    # 正常登记所需事件集合
    assert "validation_created" in types
    assert "record_created" in types
    assert "index_updated" in types
    assert "idempotent_hit" in types


@pytest.mark.parametrize("fail_at", [1, 2, 3])
def test_audit_failure_at_each_step_recovers(env, service, monkeypatch, fail_at):
    """第一条/第二条/第三条 Audit 失败；重试后完整事件集合存在、sequence 严格递增不重复。"""
    import services.evidence_registration_service as reg_svc

    input_root, _ = env
    pkg = build_package(input_root)

    real_append = reg_svc._append_ndjson
    state = {"count": 0}

    def flaky(path, obj):
        if "ndjson" in str(path):
            state["count"] += 1
            if state["count"] == fail_at:
                raise OSError("simulated audit write failure")
        return real_append(path, obj)

    monkeypatch.setattr(reg_svc, "_append_ndjson", flaky)
    with pytest.raises(RegistrationError) as excinfo:
        service.register(pkg)
    assert excinfo.value.code == ERR_AUDIT_WRITE_FAILED
    monkeypatch.undo()

    # 重试：恢复缺失审计 + 幂等命中
    out = service.register(pkg)
    assert out.idempotent_hit is True
    audit_text = service.audit_path.read_text(encoding="utf-8")
    assert "recovery_performed" in audit_text
    _assert_full_event_set_and_sequence(audit_text)


# ---------------------------------------------------------------- 敏感与不可变


def test_no_sensitive_data_in_sidecar(env, service):
    input_root, _ = env
    pkg = build_package(input_root)
    fake_name = "\u5f20\u4e09_\u674e\u56db_" + "138" + "0013" + "8000" + "_notes.txt"
    (pkg / "evidence" / fake_name).write_text("x", encoding="utf-8")
    service.register(pkg)  # 该包验证失败，不写 sidecar
    pkg2 = build_package(input_root, package_id="pkg-demo-002")
    service.register(pkg2)

    blob = ""
    for f in service.sidecar_root.rglob("*"):
        if f.is_file() and f.suffix in (".json", ".ndjson"):
            blob += f.read_text(encoding="utf-8", errors="ignore")
    assert "\u5f20\u4e09" not in blob
    assert "138" + "0013" + "8000" not in blob
    assert str(input_root) not in blob


def test_input_package_unchanged(env, service):
    input_root, _ = env
    pkg = build_package(input_root)
    before = {
        "evidence.json": (pkg / "evidence.json").read_bytes(),
        "evidence_manifest.json": (pkg / "evidence_manifest.json").read_bytes(),
        "code": (pkg / "evidence" / "code_summary.json").read_bytes(),
        "marker": (pkg / ".evidence-ready").read_bytes(),
    }
    service.register(pkg)
    after = {
        "evidence.json": (pkg / "evidence.json").read_bytes(),
        "evidence_manifest.json": (pkg / "evidence_manifest.json").read_bytes(),
        "code": (pkg / "evidence" / "code_summary.json").read_bytes(),
        "marker": (pkg / ".evidence-ready").read_bytes(),
    }
    assert before == after


def test_unregistered_content_not_read(env, service, monkeypatch):
    input_root, _ = env
    pkg = build_package(input_root)
    extra = pkg / "evidence" / "unregistered.txt"
    extra.write_text("synthetic-unregistered-0001", encoding="utf-8")

    real_open = open
    opened: list[str] = []

    def spy_open(path, *args, **kwargs):
        p = str(path)
        if "unregistered.txt" in p:
            opened.append(p)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", spy_open)
    service.register(pkg)
    assert opened == []


def test_dry_run_zero_write(env, service):
    input_root, _ = env
    pkg = build_package(input_root)
    result, issues = service.dry_run(pkg)
    assert result.status == "passed"
    assert result.registration_allowed is True
    assert list(service.runtime_root.rglob("*")) == []


# ---------------------------------------------------------------- 事务一致性（fix-3）


def _sidecar_file_snapshot(root: Path):
    """捕获 sidecar 全部文件字节与相对路径，用于零副作用断言。"""
    snap = {}
    for p in root.rglob("*"):
        if p.is_file():
            snap[str(p.relative_to(root)).replace("\\", "/")] = p.read_bytes()
    return snap


def _dir_snapshot(root: Path):
    """捕获目录内全部文件的相对路径与字节（用于输入包前后快照比对）。"""
    snap = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            snap[str(p.relative_to(root)).replace("\\", "/")] = p.read_bytes()
    return snap


def test_expected_revision_wrong_zero_side_effect_free(env, service):
    """已存在 Index 且传 0：预检冲突，Index/Record/Validation/Issue/Audit 全部不变，输入包不变。"""
    input_root, _ = env
    pkg = build_package(input_root)
    service.register(pkg)
    before = _sidecar_file_snapshot(service.sidecar_root)
    p2 = build_package(input_root, package_id="pkg-second")
    input_before = _dir_snapshot(pkg)

    with pytest.raises(RegistrationError) as excinfo:
        service.register(p2, expected_index_revision=0)
    assert excinfo.value.code == ERR_REVISION_CONFLICT

    after = _sidecar_file_snapshot(service.sidecar_root)
    assert after == before  # 零副作用：无任何 sidecar 文件变化或新增
    # 输入包不变：调用前后所有文件相对路径与字节完全相等
    assert _dir_snapshot(pkg) == input_before


def test_expected_revision_wrong_positive_side_effect_free(env, service):
    """已存在 Index 且传错误正整数：预检冲突，零副作用。"""
    input_root, _ = env
    pkg = build_package(input_root)
    service.register(pkg)
    before = _sidecar_file_snapshot(service.sidecar_root)
    p2 = build_package(input_root, package_id="pkg-second")

    with pytest.raises(RegistrationError) as excinfo:
        service.register(p2, expected_index_revision=99)
    assert excinfo.value.code == ERR_REVISION_CONFLICT
    assert _sidecar_file_snapshot(service.sidecar_root) == before


def test_expected_revision_positive_when_missing_side_effect_free(env, service):
    """Index 不存在且传正整数：预检冲突，零副作用（不产生任何记录/index/audit 文件）。"""
    input_root, _ = env
    pkg = build_package(input_root)
    with pytest.raises(RegistrationError) as excinfo:
        service.register(pkg, expected_index_revision=1)
    assert excinfo.value.code == ERR_REVISION_CONFLICT
    # 零副作用：不得产生 record/validation/index/audit（锁目录允许存在，非登记事实）
    files = [str(p.relative_to(service.sidecar_root)).replace("\\", "/")
             for p in service.sidecar_root.rglob("*") if p.is_file()]
    assert files == []


def test_expected_revision_none_captures_real_revision(env, service):
    """None：捕获当前真实 revision，正常完成一次更新，revision+1，不出现恒等比较。"""
    input_root, _ = env
    pkg = build_package(input_root)
    service.register(pkg)  # revision=1
    before_rev = json.loads(service.index_path.read_text(encoding="utf-8"))["revision"]
    assert before_rev == 1
    p2 = build_package(input_root, package_id="pkg-second")
    service.register(p2, expected_index_revision=None)  # 锁内捕获当前 revision=1
    after = json.loads(service.index_path.read_text(encoding="utf-8"))
    assert after["revision"] == 2
    assert len(after["entries"]) == 2


def test_index_referenced_record_hash_corrupt_fails_zero_side_effect(env, service):
    """已有 Index 引用的 Record 哈希损坏：登记立即失败，零副作用。"""
    input_root, _ = env
    pkg = build_package(input_root)
    service.register(pkg)
    before = _sidecar_file_snapshot(service.sidecar_root)
    rec = service.sidecar_root / "packages/pkg-demo-001/revisions/1/package-record.json"
    rec.write_text("{corrupted", encoding="utf-8")

    p2 = build_package(input_root, package_id="pkg-second")
    with pytest.raises(RegistrationError) as excinfo:
        service.register(p2)
    assert excinfo.value.code == ERR_RECORD_CORRUPTED
    after = _sidecar_file_snapshot(service.sidecar_root)
    # 仅损坏的 record 文件被我们写入（视为现场保留）；不得新增任何其他文件
    keys_before = set(before)
    keys_after = set(after)
    assert keys_after == keys_before  # 零新增文件（record 损坏是外部改动，非登记副作用）


def test_index_referenced_validation_corrupt_fails_zero_side_effect(env, service):
    """已有 Index 引用的 ValidationResult 损坏：登记立即失败，零副作用。"""
    input_root, _ = env
    pkg = build_package(input_root)
    service.register(pkg)
    before = _sidecar_file_snapshot(service.sidecar_root)
    val = next((service.sidecar_root / "packages/pkg-demo-001/revisions/1/validations").glob("*.json"))
    val.write_text("{corrupted", encoding="utf-8")

    p2 = build_package(input_root, package_id="pkg-second")
    with pytest.raises(RegistrationError) as excinfo:
        service.register(p2)
    assert excinfo.value.code == ERR_RECORD_CORRUPTED
    assert set(_sidecar_file_snapshot(service.sidecar_root)) == set(before)


def test_record_update_writes_record_updated_not_created(env, service):
    """Record 更新流程：写入 record_updated，不补 record_created。"""
    input_root, runtime_root = env
    svc1 = EvidenceRegistrationService(input_root, runtime_root, validator_version="1.0.0")
    pkg = build_package(input_root)
    svc1.register(pkg)

    svc2 = EvidenceRegistrationService(input_root, runtime_root, validator_version="2.0.0")
    out2 = svc2.register(pkg)  # validator 不同 -> record 更新
    assert out2.idempotent_hit is False
    assert out2.record is not None and out2.record.revision >= 2

    types = []
    for line in service_audit_lines(svc2):
        types.append((line.get("record_id"), line.get("event_type")))
    record_id = out2.record.record_id
    events = [t for (rid, t) in types if rid == record_id]
    # 首次创建恰一次 record_created；更新恰一次 record_updated；更新流程不重复写 record_created
    assert events.count("record_created") == 1
    assert events.count("record_updated") == 1
    assert events.count("validation_created") == 2  # 两次验证各一次
    assert events.count("index_updated") == 2


def service_audit_lines(svc):
    lines = []
    if svc.audit_path.is_file():
        for line in svc.audit_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                lines.append(json.loads(line))
    return lines


def test_audit_invalid_json_line_fails_explicitly(env, service):
    """审计文件存在非法 JSON 行：显式失败，不生成新 sequence，不修改 sidecar。"""
    input_root, _ = env
    pkg = build_package(input_root)
    # 预写入一个合法事件 + 一个非法行
    service.audit_path.parent.mkdir(parents=True, exist_ok=True)
    service.audit_path.write_text(
        json.dumps({"event_type": "x", "event_id": "e0", "sequence": 1, "occurred_at": "2026-08-05T00:00:00Z"}) + "\n"
        + "{not valid json\n",
        encoding="utf-8",
    )
    before = _sidecar_file_snapshot(service.sidecar_root)
    with pytest.raises(RegistrationError) as excinfo:
        service.register(pkg)
    assert excinfo.value.code == ERR_AUDIT_WRITE_FAILED
    # 零副作用：不新增文件、audit 未追加
    assert set(_sidecar_file_snapshot(service.sidecar_root)) == set(before)


def test_audit_read_failure_fails_explicitly(env, service, monkeypatch):
    """审计文件读取异常：显式失败，不继续登记。"""
    input_root, _ = env
    pkg = build_package(input_root)

    real_open = open

    def flaky_open(path, *args, **kwargs):
        p = str(path)
        if "evidence-registration.ndjson" in p:
            raise OSError("simulated audit read failure")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", flaky_open)
    with pytest.raises(RegistrationError) as excinfo:
        service.register(pkg)
    assert excinfo.value.code == ERR_AUDIT_WRITE_FAILED


def test_index_changed_between_preflight_and_replace_conflict(env, service, monkeypatch):
    """索引在预检后、原子替换前发生 revision/hash 变化：返回冲突，不覆盖变化后的索引，且零登记副作用。"""
    import services.evidence_registration_service as reg_svc
    from models.evidence_sidecar import RegistrationIndex

    input_root, _ = env
    pkg = build_package(input_root)
    service.register(pkg)
    # 捕获既有 package 的全部 sidecar 字节（冲突后必须不变）
    original_sidecar = _sidecar_file_snapshot(service.sidecar_root)

    real_write_index = reg_svc.EvidenceRegistrationService._write_index

    def sabotage(self, index, snapshot):
        # 模拟外部并发写入：把索引 revision 改为 101 并重算合法 hash
        cur_obj = json.loads(self.index_path.read_text(encoding="utf-8"))
        cur_obj["revision"] = 101
        cur_obj["index_sha256"] = "0" * 64
        cur = RegistrationIndex(**cur_obj)
        obj = cur.model_dump(mode="json")
        obj["index_sha256"] = None
        import hashlib as _h

        cur_obj["index_sha256"] = _h.sha256(
            json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.index_path.write_text(json.dumps(cur_obj), encoding="utf-8")
        return real_write_index(self, index, snapshot)

    monkeypatch.setattr(reg_svc.EvidenceRegistrationService, "_write_index", sabotage)
    p2 = build_package(input_root, package_id="pkg-second")
    # 实际失败登记的是 p2：调用前保存其输入快照
    p2_input_snapshot = _dir_snapshot(p2)
    with pytest.raises(RegistrationError) as excinfo:
        service.register(p2)
    assert excinfo.value.code == ERR_REVISION_CONFLICT
    monkeypatch.undo()

    # 变化后的索引未被覆盖（保留被篡改后的 revision=101）
    current = json.loads(service.index_path.read_text(encoding="utf-8"))
    assert current["revision"] == 101

    # 零登记副作用：新 package 的 Record/Validation/Issue 均不存在
    p2_sidecar = service.sidecar_root / "packages/pkg-second/revisions/1"
    assert not (p2_sidecar / "package-record.json").exists()
    assert not p2_sidecar.exists()

    # 既有 package 的全部 sidecar 字节不变（索引被外部篡改，属外部变化，单独校验）
    after = _sidecar_file_snapshot(service.sidecar_root)
    for rel, data in original_sidecar.items():
        if rel == "indexes/registration-index.json":
            continue  # 索引被外部篡改，字节必然不同（revision=101）
        assert after[rel] == data, f"既有文件被改动: {rel}"
    # 未新增任何文件（除被外部篡改的索引外，无本次登记产生的文件）
    new_keys = set(after) - set(original_sidecar)
    assert new_keys == set(), f"意外新增: {new_keys}"

    # Audit 未新增
    assert after.get("events/evidence-registration.ndjson") == original_sidecar.get("events/evidence-registration.ndjson")

    # 实际失败登记的输入包 p2 字节不变
    assert _dir_snapshot(p2) == p2_input_snapshot

    # 无遗留临时事务文件
    tmp_files = [r for r in service.sidecar_root.rglob("*.tmp.*")]
    assert tmp_files == []


def test_record_update_rollback_on_index_conflict(env, monkeypatch):
    """Record 更新时索引替换前外部变化：原 Record 字节恢复、原 latest validation 引用不变、
    新 Validation/Issue 不残留、Audit 不新增、外部 Index 保留。"""
    import services.evidence_registration_service as reg_svc
    from models.evidence_sidecar import RegistrationIndex

    input_root, runtime_root = env
    svc1 = EvidenceRegistrationService(input_root, runtime_root, validator_version="1.0.0")
    pkg = build_package(input_root)
    out1 = svc1.register(pkg)
    record_path = runtime_root / "phase11/evidence-sidecar/packages/pkg-demo-001/revisions/1/package-record.json"
    original_record_bytes = record_path.read_bytes()
    original_validation_ref = out1.record.latest_validation_id
    # 首次登记成功后立即保存 Audit 基准（冲突后必须与此完全一致）
    original_audit_bytes = svc1.audit_path.read_bytes()

    # 用不同 validator 触发 record 更新，并在最终 index 替换前外部篡改索引
    svc2 = EvidenceRegistrationService(input_root, runtime_root, validator_version="2.0.0")
    real_write_index = reg_svc.EvidenceRegistrationService._write_index

    def sabotage(self, index, snapshot):
        cur_obj = json.loads(self.index_path.read_text(encoding="utf-8"))
        cur_obj["revision"] = 999
        cur_obj["index_sha256"] = "0" * 64
        cur = RegistrationIndex(**cur_obj)
        obj = cur.model_dump(mode="json")
        obj["index_sha256"] = None
        import hashlib as _h

        cur_obj["index_sha256"] = _h.sha256(
            json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.index_path.write_text(json.dumps(cur_obj), encoding="utf-8")
        return real_write_index(self, index, snapshot)

    monkeypatch.setattr(reg_svc.EvidenceRegistrationService, "_write_index", sabotage)
    with pytest.raises(RegistrationError) as excinfo:
        svc2.register(pkg)
    assert excinfo.value.code == ERR_REVISION_CONFLICT
    monkeypatch.undo()

    # 原 Record 字节恢复（回滚后 latest validation 引用不变）
    restored = json.loads(record_path.read_text(encoding="utf-8"))
    assert record_path.read_bytes() == original_record_bytes
    assert restored["latest_validation_id"] == original_validation_ref
    assert restored["revision"] == 1  # 未被更新为 2

    # 新 ValidationResult / Issue 不残留
    val_dir = runtime_root / "phase11/evidence-sidecar/packages/pkg-demo-001/revisions/1/validations"
    assert sorted(p.name for p in val_dir.glob("*.json")) == [f"{original_validation_ref}.json"]
    issue_dir = runtime_root / "phase11/evidence-sidecar/packages/pkg-demo-001/revisions/1/issues"
    assert list(issue_dir.glob("*.json")) == []

    # Audit 不新增（与首次登记成功后保存的基准字节完全一致）
    assert svc2.audit_path.read_bytes() == original_audit_bytes

    # 外部变化后的 Index 被保留（revision=999）
    current_index = json.loads(svc2.index_path.read_text(encoding="utf-8"))
    assert current_index["revision"] == 999

    # 无遗留临时文件
    assert list(runtime_root.rglob("*.tmp.*")) == []


def test_index_write_failure_rolls_back_sidecar(env, service, monkeypatch):
    """模拟最终 _atomic_write(index) 抛异常：返回 SIDECAR_INDEX_WRITE_FAILED，
    本次新建 sidecar 全部回滚、原历史文件和 Audit 不变、无临时文件残留。"""
    import services.evidence_registration_service as reg_svc

    input_root, _ = env
    pkg = build_package(input_root)
    service.register(pkg)
    before = _sidecar_file_snapshot(service.sidecar_root)

    real_write = reg_svc._atomic_write_bytes

    def flaky(target, data):
        if "registration-index.json" in str(target):
            raise OSError("simulated index write failure")
        return real_write(target, data)

    monkeypatch.setattr(reg_svc, "_atomic_write_bytes", flaky)
    p2 = build_package(input_root, package_id="pkg-second")
    # 实际失败登记的是 p2：调用前保存其输入快照
    p2_input_snapshot = _dir_snapshot(p2)
    with pytest.raises(RegistrationError) as excinfo:
        service.register(p2)
    assert excinfo.value.code == ERR_INDEX_WRITE_FAILED
    monkeypatch.undo()

    # 本次新建 sidecar 全部回滚
    after = _sidecar_file_snapshot(service.sidecar_root)
    assert after == before  # 与首次登记后完全一致（index 未被写入，无新增文件）
    # 新 package 无任何残留
    assert not (service.sidecar_root / "packages/pkg-second").exists()
    # 实际失败登记的输入包 p2 字节不变
    assert _dir_snapshot(p2) == p2_input_snapshot
    # 无临时文件残留
    assert list(service.sidecar_root.rglob("*.tmp.*")) == []
