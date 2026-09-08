"""
Phase 11B-1b：EvidencePackage v1.1 只读验证器合成测试。

所有测试在临时目录内构造合成脱敏 fixture，不读取真实学生材料。

覆盖：
- 合法 sufficient 包通过；合法 limited 包按契约得出结论。
- staging 包拒绝；marker 缺失/不一致。
- 双 JSON 缺失、非法 JSON、未知字段。
- 身份、revision、privacy_check 不一致。
- manifest hash 正确/错误。
- 文件缺失、大小错误、SHA-256 错误。
- 未登记普通文件 fail-closed。
- ../、绝对路径、盘符路径、反斜杠路径。
- 符号链接越界拒绝（Windows 不支持时 skip）。
- naive / 非 UTC 时间拒绝。
- issue 顺序与 result hash 同输入稳定。
- 验证不修改输入目录。
- 不读取未登记文件正文（monkeypatch 证明）。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.evidence_package_validator import (
    ERROR_CROSS_FILE_MISMATCH,
    ERROR_EVIDENCE_MISSING,
    ERROR_FILE_MISSING,
    ERROR_FILE_SIZE_MISMATCH,
    ERROR_HASH_MISMATCH,
    ERROR_INVALID_PACKAGE_ID,
    ERROR_MANIFEST_MISSING,
    ERROR_PACKAGE_NOT_READY,
    ERROR_PATH_TRAVERSAL,
    ERROR_PRIVACY_CHECK_FAILED,
    ERROR_UNREGISTERED_PACKAGE_FILE,
    ERROR_UNSUPPORTED_VERSION,
    EvidencePackageValidator,
    canonical_json_bytes,
)

UTC = timezone.utc
T0 = datetime(2026, 8, 5, 7, 0, 0, tzinfo=UTC)


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


def build_package(root: Path, package_id: str = "pkg-demo-001") -> Path:
    """构造合法 sufficient 包并正确计算 manifest 哈希。"""
    pkg_dir = root / package_id
    pkg_dir.mkdir(parents=True, exist_ok=True)

    content = b'{"synthetic": true, "summary": "code structure"}'
    code_file = pkg_dir / "evidence" / "code_summary.json"
    code_file.parent.mkdir(parents=True, exist_ok=True)
    code_file.write_bytes(content)

    manifest = manifest_body()
    manifest["package_id"] = package_id
    manifest["files"][0]["size_bytes"] = len(content)
    manifest["files"][0]["sha256"] = sha256_bytes(content)

    # 自引用置 null 计算权威哈希
    obj = dict(manifest)
    obj["manifest_sha256"] = None
    h = sha256_bytes(canonical_json_bytes(obj))
    manifest["manifest_sha256"] = h
    write_json(pkg_dir / "evidence_manifest.json", manifest)

    evidence = evidence_body()
    evidence["package_id"] = package_id
    evidence["manifest_sha256"] = h
    write_json(pkg_dir / "evidence.json", evidence)

    (pkg_dir / ".evidence-ready").write_text("", encoding="utf-8")
    return pkg_dir


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "controlled-root"
    root.mkdir()
    return root


# ---------------------------------------------------------------- 正常


def test_sufficient_package_passes(env):
    pkg = build_package(env)
    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "passed"
    assert result.model_input_allowed is True
    assert result.registration_allowed is True
    assert result.record_id is None
    assert result.validated_file_count == 1
    assert result.declared_file_count == 1
    assert result.unregistered_file_count == 0
    assert issues == []


def test_limited_package_conclusion(env):
    pkg = build_package(env)
    # 改成 limited：有可用证据但存在缺失
    ev_path = pkg / "evidence.json"
    ev = json.loads(ev_path.read_text(encoding="utf-8"))
    ev["evidence_level"] = "limited"
    ev["manual_review_reasons"] = ["CORE_EVIDENCE_PARTIAL"]
    write_json(ev_path, ev)
    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "passed"
    assert result.model_input_allowed is True
    assert result.registration_allowed is True


# ---------------------------------------------------------------- 发布门


def test_staging_package_rejected(env):
    pkg = build_package(env)
    ev_path = pkg / "evidence.json"
    ev = json.loads(ev_path.read_text(encoding="utf-8"))
    ev["publication_status"] = "staging"
    ev["completion_marker"] = None
    write_json(ev_path, ev)
    mv_path = pkg / "evidence_manifest.json"
    mv = json.loads(mv_path.read_text(encoding="utf-8"))
    mv["publication_status"] = "staging"
    mv["completion_marker"] = None
    write_json(mv_path, mv)

    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"
    codes = [i.code for i in issues]
    assert ERROR_PACKAGE_NOT_READY in codes
    assert result.model_input_allowed is False


def test_marker_missing_rejected(env):
    pkg = build_package(env)
    (pkg / ".evidence-ready").unlink()
    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"
    assert any(i.code == ERROR_PACKAGE_NOT_READY for i in issues)


def test_marker_inconsistent_rejected(env):
    pkg = build_package(env)
    ev_path = pkg / "evidence.json"
    ev = json.loads(ev_path.read_text(encoding="utf-8"))
    ev["publication_status"] = "rejected"
    ev["completion_marker"] = None
    ev["rejection_reasons"] = ["SYNTHETIC_REJECTION"]
    write_json(ev_path, ev)
    mv_path = pkg / "evidence_manifest.json"
    mv = json.loads(mv_path.read_text(encoding="utf-8"))
    mv["publication_status"] = "rejected"
    mv["completion_marker"] = None
    write_json(mv_path, mv)

    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    # marker 存在但双 JSON 声明 rejected -> 不一致
    assert result.status == "failed"
    assert any(i.code == ERROR_PACKAGE_NOT_READY for i in issues)


# ---------------------------------------------------------------- 双 JSON


def test_evidence_missing(env):
    pkg = build_package(env)
    (pkg / "evidence.json").unlink()
    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"
    assert any(i.code == ERROR_EVIDENCE_MISSING for i in issues)


def test_manifest_missing(env):
    pkg = build_package(env)
    (pkg / "evidence_manifest.json").unlink()
    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"
    assert any(i.code == ERROR_MANIFEST_MISSING for i in issues)


def test_invalid_json(env):
    pkg = build_package(env)
    (pkg / "evidence.json").write_text("{not valid json", encoding="utf-8")
    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"


def test_unknown_field_in_evidence(env):
    pkg = build_package(env)
    ev_path = pkg / "evidence.json"
    ev = json.loads(ev_path.read_text(encoding="utf-8"))
    ev["extra_field"] = "nope"
    write_json(ev_path, ev)
    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"
    assert any(i.code == "SCHEMA_INVALID" for i in issues)


def test_unsupported_contract_version(env):
    pkg = build_package(env)
    ev_path = pkg / "evidence.json"
    ev = json.loads(ev_path.read_text(encoding="utf-8"))
    ev["contract_version"] = "evidence-package/v9.9"
    write_json(ev_path, ev)
    mv_path = pkg / "evidence_manifest.json"
    mv = json.loads(mv_path.read_text(encoding="utf-8"))
    mv["contract_version"] = "evidence-package/v9.9"
    write_json(mv_path, mv)
    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"
    assert any(i.code == ERROR_UNSUPPORTED_VERSION for i in issues)


def test_identity_mismatch(env):
    pkg = build_package(env)
    ev_path = pkg / "evidence.json"
    ev = json.loads(ev_path.read_text(encoding="utf-8"))
    ev["batch_id"] = "batch-demo-002"
    write_json(ev_path, ev)
    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"
    assert any(i.code == ERROR_CROSS_FILE_MISMATCH for i in issues)


def test_revision_mismatch(env):
    pkg = build_package(env)
    ev_path = pkg / "evidence.json"
    ev = json.loads(ev_path.read_text(encoding="utf-8"))
    ev["package_revision"] = 2
    write_json(ev_path, ev)
    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"
    assert any(i.code == ERROR_CROSS_FILE_MISMATCH for i in issues)


def test_privacy_check_mismatch(env):
    pkg = build_package(env)
    ev_path = pkg / "evidence.json"
    ev = json.loads(ev_path.read_text(encoding="utf-8"))
    ev["privacy_check"]["contains_face"] = True
    write_json(ev_path, ev)
    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"
    assert any(i.code == ERROR_CROSS_FILE_MISMATCH for i in issues)


# ---------------------------------------------------------------- manifest 哈希


def test_manifest_hash_mismatch(env):
    pkg = build_package(env)
    ev_path = pkg / "evidence.json"
    ev = json.loads(ev_path.read_text(encoding="utf-8"))
    ev["manifest_sha256"] = "1" * 64
    write_json(ev_path, ev)
    mv_path = pkg / "evidence_manifest.json"
    mv = json.loads(mv_path.read_text(encoding="utf-8"))
    mv["manifest_sha256"] = "1" * 64
    write_json(mv_path, mv)
    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"
    assert any(i.code == ERROR_HASH_MISMATCH for i in issues)


def test_manifest_hash_correct(env):
    pkg = build_package(env)
    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "passed"
    assert not any(i.code == ERROR_HASH_MISMATCH for i in issues)


# ---------------------------------------------------------------- 文件完整性


def test_file_missing(env):
    pkg = build_package(env)
    (pkg / "evidence" / "code_summary.json").unlink()
    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"
    assert any(i.code == ERROR_FILE_MISSING for i in issues)


def test_file_size_mismatch(env):
    pkg = build_package(env)
    (pkg / "evidence" / "code_summary.json").write_bytes(b'{"longer": true, "extra": 1}')
    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"
    assert any(i.code == ERROR_FILE_SIZE_MISMATCH for i in issues)


def test_file_sha256_mismatch(env):
    pkg = build_package(env)
    mv_path = pkg / "evidence_manifest.json"
    mv = json.loads(mv_path.read_text(encoding="utf-8"))
    mv["files"][0]["sha256"] = "f" * 64
    # 注意：修改 manifest 后需重算 manifest hash
    obj = dict(mv)
    obj["manifest_sha256"] = None
    mv["manifest_sha256"] = sha256_bytes(canonical_json_bytes(obj))
    write_json(mv_path, mv)
    ev_path = pkg / "evidence.json"
    ev = json.loads(ev_path.read_text(encoding="utf-8"))
    ev["manifest_sha256"] = mv["manifest_sha256"]
    write_json(ev_path, ev)
    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"
    assert any(i.code == ERROR_HASH_MISMATCH for i in issues)


# ---------------------------------------------------------------- 未登记文件


def test_unregistered_file_fail_closed(env):
    pkg = build_package(env)
    extra = pkg / "evidence" / "unregistered.txt"
    extra.write_text("synthetic extra", encoding="utf-8")
    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"
    assert any(i.code == ERROR_UNREGISTERED_PACKAGE_FILE for i in issues)
    assert result.model_input_allowed is False
    assert result.registration_allowed is False


def test_unregistered_file_content_not_read(env, monkeypatch):
    """未登记文件只检查存在性，不读取正文。"""
    pkg = build_package(env)
    extra = pkg / "evidence" / "unregistered.txt"
    extra.write_text("synthetic-unregistered-content-0001", encoding="utf-8")

    real_open = open
    opened_paths: list[str] = []

    def spy_open(path, *args, **kwargs):
        p = str(path)
        if "unregistered.txt" in p:
            opened_paths.append(p)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", spy_open)
    validator = EvidencePackageValidator(env)
    validator.validate(pkg)
    assert opened_paths == [], "未登记文件正文不应被读取"


# ---------------------------------------------------------------- 路径安全


def test_path_traversal_in_manifest(env):
    pkg = build_package(env)
    mv_path = pkg / "evidence_manifest.json"
    mv = json.loads(mv_path.read_text(encoding="utf-8"))
    mv["files"][0]["relative_path"] = "../outside.txt"
    obj = dict(mv)
    obj["manifest_sha256"] = None
    mv["manifest_sha256"] = sha256_bytes(canonical_json_bytes(obj))
    write_json(mv_path, mv)
    ev_path = pkg / "evidence.json"
    ev = json.loads(ev_path.read_text(encoding="utf-8"))
    ev["manifest_sha256"] = mv["manifest_sha256"]
    write_json(ev_path, ev)
    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"
    assert any(i.code == ERROR_PATH_TRAVERSAL for i in issues)


def test_absolute_path_in_manifest(env):
    pkg = build_package(env)
    mv_path = pkg / "evidence_manifest.json"
    mv = json.loads(mv_path.read_text(encoding="utf-8"))
    mv["files"][0]["relative_path"] = "/etc/passwd"
    obj = dict(mv)
    obj["manifest_sha256"] = None
    mv["manifest_sha256"] = sha256_bytes(canonical_json_bytes(obj))
    write_json(mv_path, mv)
    ev_path = pkg / "evidence.json"
    ev = json.loads(ev_path.read_text(encoding="utf-8"))
    ev["manifest_sha256"] = mv["manifest_sha256"]
    write_json(ev_path, ev)
    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"
    assert any(i.code == ERROR_PATH_TRAVERSAL for i in issues)


def test_windows_drive_path_in_manifest(env):
    pkg = build_package(env)
    mv_path = pkg / "evidence_manifest.json"
    mv = json.loads(mv_path.read_text(encoding="utf-8"))
    mv["files"][0]["relative_path"] = "C:/windows/evil.txt"
    obj = dict(mv)
    obj["manifest_sha256"] = None
    mv["manifest_sha256"] = sha256_bytes(canonical_json_bytes(obj))
    write_json(mv_path, mv)
    ev_path = pkg / "evidence.json"
    ev = json.loads(ev_path.read_text(encoding="utf-8"))
    ev["manifest_sha256"] = mv["manifest_sha256"]
    write_json(ev_path, ev)
    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"
    assert any(i.code == ERROR_PATH_TRAVERSAL for i in issues)


def test_backslash_path_in_manifest(env):
    pkg = build_package(env)
    mv_path = pkg / "evidence_manifest.json"
    mv = json.loads(mv_path.read_text(encoding="utf-8"))
    mv["files"][0]["relative_path"] = "evidence\\evil.txt"
    obj = dict(mv)
    obj["manifest_sha256"] = None
    mv["manifest_sha256"] = sha256_bytes(canonical_json_bytes(obj))
    write_json(mv_path, mv)
    ev_path = pkg / "evidence.json"
    ev = json.loads(ev_path.read_text(encoding="utf-8"))
    ev["manifest_sha256"] = mv["manifest_sha256"]
    write_json(ev_path, ev)
    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"
    assert any(i.code == ERROR_PATH_TRAVERSAL for i in issues)


def test_package_dir_outside_root_rejected(env):
    outside = env.parent / "outside-pkg"
    build_package(outside)  # 建在受控根目录外
    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(outside)
    assert result.status == "failed"
    assert any(i.code == ERROR_PATH_TRAVERSAL for i in issues)


@pytest.mark.skipif(os.name == "nt" and not hasattr(os, "symlink"), reason="Windows 无 symlink 支持")
def test_symlink_file_rejected(env):
    pkg = build_package(env)
    target = env.parent / "symlink-target.txt"
    target.write_text("synthetic", encoding="utf-8")
    link = pkg / "evidence" / "code_summary.json"
    link.unlink()
    try:
        os.symlink(str(target), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")
    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"
    assert any(i.code == "UNSUPPORTED_FILE_ROLE" for i in issues)


# ---------------------------------------------------------------- 时间


def test_naive_time_rejected(env):
    pkg = build_package(env)
    ev_path = pkg / "evidence.json"
    ev = json.loads(ev_path.read_text(encoding="utf-8"))
    ev["generated_at"] = "2026-08-05T07:00:00"  # 无时区
    write_json(ev_path, ev)
    mv_path = pkg / "evidence_manifest.json"
    mv = json.loads(mv_path.read_text(encoding="utf-8"))
    mv["generated_at"] = "2026-08-05T07:00:00"
    write_json(mv_path, mv)
    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"


def test_non_utc_time_rejected(env):
    pkg = build_package(env)
    ev_path = pkg / "evidence.json"
    ev = json.loads(ev_path.read_text(encoding="utf-8"))
    ev["generated_at"] = "2026-08-05T15:00:00+08:00"
    write_json(ev_path, ev)
    mv_path = pkg / "evidence_manifest.json"
    mv = json.loads(mv_path.read_text(encoding="utf-8"))
    mv["generated_at"] = "2026-08-05T15:00:00+08:00"
    write_json(mv_path, mv)
    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"


# ---------------------------------------------------------------- 稳定性


def test_issue_order_stable_same_input(env):
    pkg = build_package(env)
    # 制造多个错误
    ev_path = pkg / "evidence.json"
    ev = json.loads(ev_path.read_text(encoding="utf-8"))
    ev["batch_id"] = "batch-demo-002"
    ev["privacy_check"]["contains_face"] = True
    write_json(ev_path, ev)

    validator = EvidencePackageValidator(env)
    _, issues1 = validator.validate(pkg)
    _, issues2 = validator.validate(pkg)
    codes1 = [(i.code, i.field_path) for i in issues1]
    codes2 = [(i.code, i.field_path) for i in issues2]
    assert codes1 == codes2


def test_result_hash_stable_same_input(env):
    pkg = build_package(env)
    validator = EvidencePackageValidator(env)
    r1, _ = validator.validate(pkg)
    r2, _ = validator.validate(pkg)
    assert r1.result_sha256 == r2.result_sha256


def test_validation_does_not_modify_input(env):
    pkg = build_package(env)
    before = {
        "evidence.json": (pkg / "evidence.json").read_bytes(),
        "evidence_manifest.json": (pkg / "evidence_manifest.json").read_bytes(),
        "code_summary.json": (pkg / "evidence" / "code_summary.json").read_bytes(),
        "marker": (pkg / ".evidence-ready").read_bytes(),
    }
    validator = EvidencePackageValidator(env)
    validator.validate(pkg)
    after = {
        "evidence.json": (pkg / "evidence.json").read_bytes(),
        "evidence_manifest.json": (pkg / "evidence_manifest.json").read_bytes(),
        "code_summary.json": (pkg / "evidence" / "code_summary.json").read_bytes(),
        "marker": (pkg / ".evidence-ready").read_bytes(),
    }
    assert before == after


# ---------------------------------------------------------------- 未登记路径脱敏


def test_unregistered_path_not_leaked(env):
    """未登记文件路径脱敏：虚构敏感文件名不得出现在 issue 中。"""
    pkg = build_package(env)
    # 虚构脱敏姓名与数字式文件名片段（拆分为拼接，避免在源码中形成字面敏感串）
    fake_name = "\u5f20\u4e09_\u674e\u56db"           # 虚构姓名（unicode 转义）
    fake_phone = "138" + "0013" + "8000"
    secret_name = f"{fake_name}_{fake_phone}_internal.txt"
    extra = pkg / "evidence" / secret_name
    extra.write_text("synthetic content", encoding="utf-8")

    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"

    issue = next(i for i in issues if i.code == ERROR_UNREGISTERED_PACKAGE_FILE)
    # location 固定为占位符，不出现原始路径
    assert issue.location == "unregistered_file"
    assert issue.file_ref is None
    # 不出现原始文件名、姓名或数字串
    blob = json.dumps(issue.model_dump(mode="json"), ensure_ascii=False)
    assert "\u5f20\u4e09" not in blob
    assert "\u674e\u56db" not in blob
    assert fake_phone not in blob
    assert "internal" not in blob
    assert ".txt" in (issue.actual_summary or "")
    # 全部 issue 均不得泄露该名称
    all_blob = json.dumps([i.model_dump(mode="json") for i in issues], ensure_ascii=False)
    assert fake_phone not in all_blob
    assert "\u5f20\u4e09" not in all_blob


def test_unregistered_link_directory_not_silently_filtered(env):
    """未登记的链接目录必须产生错误，不得从扫描中静默过滤。"""
    pkg = build_package(env)
    target = env.parent / "link-target-dir"
    target.mkdir(exist_ok=True)
    (target / "payload.txt").write_text("synthetic", encoding="utf-8")
    link_dir = pkg / "evidence" / "linked_dir"
    link_dir.mkdir(exist_ok=True)  # 先建普通目录再替换
    try:
        os.symlink(str(target), str(link_dir), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建目录符号链接")
    # 移除原普通目录：symlink 已覆盖
    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"
    assert any(i.code == "UNSUPPORTED_FILE_ROLE" for i in issues)


# ---------------------------------------------------------------- 异常脱敏


def test_oserror_path_not_leaked(env, monkeypatch):
    """异常信息脱敏：含绝对路径的 OSError 不得进入 issue。"""
    from services import evidence_package_validator as ev

    secret_path = "X:/Profiles/demo/secret-dir/secret-file.json"
    secret_root = "D:/controlled-root-secret"

    def boom(path):
        raise OSError(f"permission denied: {secret_path}")

    # monkeypatch 模块级读取函数，使其抛出含绝对路径的 OSError
    monkeypatch.setattr(ev, "_safe_read_json", boom)

    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(env / "pkg-demo-001")
    # 边界或读取失败：any status，但绝不能泄露路径
    all_blob = json.dumps(
        {"result": result.model_dump(mode="json"), "issues": [i.model_dump(mode="json") for i in issues]},
        ensure_ascii=False,
    )
    assert "secret-admin" not in all_blob
    assert "secret-dir" not in all_blob
    assert "secret-file" not in all_blob
    assert "controlled-root-secret" not in all_blob
    assert "C:/Users" not in all_blob
    # issue 只含稳定异常类型摘要
    for issue in issues:
        assert issue.actual_summary is None or "OSError" in issue.actual_summary


def test_internal_error_path_not_leaked(env, monkeypatch):
    """内部错误脱敏：validator 内部异常只保留类型名，不含 str(exc)。"""
    from services import evidence_package_validator as ev

    secret = "/etc/secret-internal/path.json"

    def boom(*args, **kwargs):
        raise RuntimeError(f"internal boom at {secret}")

    monkeypatch.setattr(ev.EvidencePackageValidator, "_check_boundary", boom)
    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(env / "pkg-demo-001")
    all_blob = json.dumps([i.model_dump(mode="json") for i in issues], ensure_ascii=False)
    assert "secret-internal" not in all_blob
    assert "/etc/" not in all_blob
    internal = [i for i in issues if i.code == "VALIDATION_INTERNAL_ERROR"]
    assert internal and "RuntimeError" in (internal[0].actual_summary or "")


# ---------------------------------------------------------------- 链接与快捷方式


@pytest.mark.skipif(os.name == "nt" and not hasattr(os, "symlink"), reason="Windows 无 symlink 支持")
def test_symlink_parent_directory_rejected(env):
    """manifest 文件路径的每一级父目录链接必须被拒绝。"""
    pkg = build_package(env)
    # 将 evidence 目录替换为指向包外目录的符号链接
    target = env.parent / "parent-link-target"
    target.mkdir(exist_ok=True)
    (target / "code_summary.json").write_bytes(b'{"synthetic": true}')
    import shutil

    shutil.rmtree(pkg / "evidence")
    try:
        os.symlink(str(target), str(pkg / "evidence"), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建目录符号链接")
    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"
    assert any(i.code == "UNSUPPORTED_FILE_ROLE" for i in issues)


def test_lnk_manifest_entry_rejected(env):
    """manifest 指向 .lnk 快捷方式：不得解析目标，返回 UNSUPPORTED_FILE_ROLE（不可 skip）。"""
    pkg = build_package(env)
    lnk = pkg / "evidence" / "evil.lnk"
    lnk.write_text("fake shortcut payload", encoding="utf-8")  # 只是文本，不解析

    mv_path = pkg / "evidence_manifest.json"
    mv = json.loads(mv_path.read_text(encoding="utf-8"))
    mv["files"][0]["relative_path"] = "evidence/evil.lnk"
    mv["files"][0]["size_bytes"] = len("fake shortcut payload")
    mv["files"][0]["sha256"] = sha256_bytes(b"fake shortcut payload")
    obj = dict(mv)
    obj["manifest_sha256"] = None
    mv["manifest_sha256"] = sha256_bytes(canonical_json_bytes(obj))
    write_json(mv_path, mv)
    ev_path = pkg / "evidence.json"
    ev = json.loads(ev_path.read_text(encoding="utf-8"))
    ev["manifest_sha256"] = mv["manifest_sha256"]
    write_json(ev_path, ev)

    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"
    assert any(i.code == "UNSUPPORTED_FILE_ROLE" for i in issues)


def test_url_manifest_entry_rejected(env):
    """manifest 指向 .url 快捷方式：不得解析目标，返回 UNSUPPORTED_FILE_ROLE（不可 skip）。"""
    pkg = build_package(env)
    url = pkg / "evidence" / "link.url"
    url.write_text("[InternetShortcut]\nURL=about:blank", encoding="utf-8")

    mv_path = pkg / "evidence_manifest.json"
    mv = json.loads(mv_path.read_text(encoding="utf-8"))
    mv["files"][0]["relative_path"] = "evidence/link.url"
    mv["files"][0]["size_bytes"] = len("[InternetShortcut]\nURL=about:blank")
    mv["files"][0]["sha256"] = sha256_bytes(b"[InternetShortcut]\nURL=about:blank")
    obj = dict(mv)
    obj["manifest_sha256"] = None
    mv["manifest_sha256"] = sha256_bytes(canonical_json_bytes(obj))
    write_json(mv_path, mv)
    ev_path = pkg / "evidence.json"
    ev = json.loads(ev_path.read_text(encoding="utf-8"))
    ev["manifest_sha256"] = mv["manifest_sha256"]
    write_json(ev_path, ev)

    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"
    assert any(i.code == "UNSUPPORTED_FILE_ROLE" for i in issues)


def test_unregistered_lnk_not_silently_filtered(env):
    """未登记的 .lnk 文件必须产生错误，不得静默过滤（不可 skip）。"""
    pkg = build_package(env)
    lnk = pkg / "evidence" / "unregistered.lnk"
    lnk.write_text("fake", encoding="utf-8")
    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"
    assert any(i.code == "UNSUPPORTED_FILE_ROLE" for i in issues)


# ---------------------------------------------------------------- 硬链接别名 fail-closed


@pytest.mark.skipif(
    not hasattr(os, "link"),
    reason="平台不支持 os.link 硬链接",
)
def test_registered_hardlink_rejected(env):
    """manifest 已登记文件若为硬链接别名（st_nlink>1），必须 UNSUPPORTED_FILE_ROLE。"""
    pkg = build_package(env)
    target = pkg / "evidence" / "code_summary.json"
    alias_outside = env.parent / "alias-outside.bin"  # 包外别名，避免包内多一个未登记文件
    try:
        os.link(str(target), str(alias_outside))
    except OSError as exc:
        pytest.skip(f"文件系统不支持 os.link: {type(exc).__name__}")

    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"
    hard = [i for i in issues if i.code == "UNSUPPORTED_FILE_ROLE"]
    assert hard, "应产生 UNSUPPORTED_FILE_ROLE"
    # 不泄露绝对路径
    blob = json.dumps([i.model_dump(mode="json") for i in issues], ensure_ascii=False)
    assert str(env.parent) not in blob
    assert "alias-outside" not in blob


def test_registered_hardlink_content_not_read(env, monkeypatch):
    """发现已登记硬链接后不得读取正文（用 monkeypatch 证明）。"""
    pkg = build_package(env)
    target = pkg / "evidence" / "code_summary.json"
    alias_outside = env.parent / "alias-outside.bin"
    try:
        os.link(str(target), str(alias_outside))
    except OSError as exc:
        pytest.skip(f"文件系统不支持 os.link: {type(exc).__name__}")

    real_open = open
    opened: list[str] = []

    def spy_open(path, *args, **kwargs):
        p = str(path)
        if "code_summary" in p:
            opened.append(p)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", spy_open)
    validator = EvidencePackageValidator(env)
    validator.validate(pkg)
    assert opened == [], "硬链接文件的正文不应被读取"


@pytest.mark.skipif(
    not hasattr(os, "link"),
    reason="平台不支持 os.link 硬链接",
)
def test_unregistered_hardlink_rejected(env):
    """未登记硬链接同样 fail-closed，返回 UNSUPPORTED_FILE_ROLE，且不泄露敏感名。"""
    pkg = build_package(env)
    fake_name = "\u5f20\u4e09_\u674e\u56db_" + "138" + "0013" + "8000" + "_notes.txt"
    hard = pkg / "evidence" / fake_name
    hard.write_text("synthetic", encoding="utf-8")
    alias_outside = env.parent / "unreg-alias-outside.bin"
    try:
        os.link(str(hard), str(alias_outside))
    except OSError as exc:
        pytest.skip(f"文件系统不支持 os.link: {type(exc).__name__}")

    validator = EvidencePackageValidator(env)
    result, issues = validator.validate(pkg)
    assert result.status == "failed"
    # 不得仅返回普通 UNREGISTERED_PACKAGE_FILE
    assert not any(i.code == "UNREGISTERED_PACKAGE_FILE" for i in issues)
    hard_issues = [i for i in issues if i.code == "UNSUPPORTED_FILE_ROLE"]
    assert hard_issues, "应产生 UNSUPPORTED_FILE_ROLE"
    blob = json.dumps([i.model_dump(mode="json") for i in issues], ensure_ascii=False)
    assert "\u5f20\u4e09" not in blob
    assert "138" + "0013" + "8000" not in blob
    assert str(env.parent) not in blob


# ---------------------------------------------------------------- 11C-2c-prerequisite-fix-1：evidence_level 路由


def test_result_persists_evidence_level_for_all_levels(env):
    """新验证结果必须保存从 EvidencePackage.evidence_level 写入的权威 level。"""
    for lv in ("sufficient", "limited", "manual_only"):
        pkg = build_package(env, package_id=f"pkg-level-{lv}")
        # 改写 evidence.json 的 evidence_level 后再验证（保持其余结构合法）
        evidence_path = pkg / "evidence.json"
        ev = json.loads(evidence_path.read_text(encoding="utf-8"))
        ev["evidence_level"] = lv
        if lv == "manual_only":
            ev["publication_status"] = "ready"
        write_json(evidence_path, ev)
        validator = EvidencePackageValidator(env)
        result, _issues = validator.validate(pkg)
        assert result.evidence_level == lv  # 权威 level 写入结果


def test_result_evidence_level_comes_from_evidence_not_inferred(env):
    """level 只来自 evidence.json，不从文件名/摘要/availability 推测。"""
    pkg = build_package(env)
    evidence_path = pkg / "evidence.json"
    ev = json.loads(evidence_path.read_text(encoding="utf-8"))
    ev["evidence_level"] = "limited"  # 文件/结构均表明 sufficient 形态，但权威声明是 limited
    write_json(evidence_path, ev)
    validator = EvidencePackageValidator(env)
    result, _issues = validator.validate(pkg)
    assert result.evidence_level == "limited"  # 以 evidence.json 声明为准
