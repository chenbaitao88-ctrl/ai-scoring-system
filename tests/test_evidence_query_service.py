"""
Phase 11B-1d：Evidence Sidecar 只读查询服务合成测试。

所有数据均为 tmp_path 内构造的脱敏合成 EvidencePackage，不读取真实学生材料。

覆盖：
- list_registrations：严格校验、稳定排序、各字段过滤、Index 缺失返回空、零写入。
- get_registration：entry/record/latest validation/issues 一致性；不存在返回 None。
- get_validation：目录边界（路径穿越/绝对/反斜杠/非法标识符拒绝）、历史 validation、
  Record 身份一致、缺失 SIDECAR_VALIDATION_NOT_FOUND。
- get_validation_issues：引用顺序一致、缺失/重复/身份不一致/损坏显式失败。
- 只读保证：查询前后 sidecar 路径与字节完全一致；无 lock/tmp/audit 新增；
  不修改输入 EvidencePackage；不读取未登记文件正文。
- 端到端合成验收：sufficient + limited 登记、历史 validation、查询列表/单记录/历史/
  issues、重复查询稳定、损坏显式失败、不存在不伪装成功。
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
    RegistrationIndexEntry,
    ValidationIssue,
    ValidationResult,
)
from services.evidence_package_validator import canonical_json_bytes
from services.evidence_query_service import (
    EvidenceQueryService,
    RegistrationQueryResult,
)
from services.evidence_registration_service import (
    ERR_HASH_MISMATCH,
    ERR_INDEX_CORRUPTED,
    ERR_RECORD_CORRUPTED,
    ERR_RELATIVE_REF_INVALID,
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
    level: str = "sufficient",
) -> Path:
    """构造合法 EvidencePackage 并正确计算 manifest 哈希。"""
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
    if level == "limited":
        evidence["evidence_level"] = "limited"
        evidence["manual_review_reasons"] = ["CORE_EVIDENCE_PARTIAL"]
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
def pair(env):
    """返回 (registration_service, query_service)。"""
    input_root, runtime_root = env
    reg = EvidenceRegistrationService(input_root, runtime_root)
    q = EvidenceQueryService(reg)
    return reg, q


def _snapshot(root: Path):
    """捕获目录内全部文件相对路径与字节。"""
    snap = {}
    for p in root.rglob("*"):
        if p.is_file():
            snap[str(p.relative_to(root)).replace("\\", "/")] = p.read_bytes()
    return snap


# ---------------------------------------------------------------- list_registrations


def test_list_registrations_empty_when_no_index(env, pair):
    reg, q = pair
    input_root, runtime_root = env
    pkg = build_package(input_root)
    # dry-run 不产生 sidecar；直接查询应为空
    reg.dry_run(pkg)
    assert list(runtime_root.rglob("*")) == []
    assert q.list_registrations() == []


def test_list_registrations_stable_sorted_and_filtered(pair):
    reg, q = pair
    input_root, _ = env_of(pair)
    build_package(input_root, package_id="pkg-aaa")
    build_package(input_root, package_id="pkg-bbb")
    build_package(input_root, package_id="pkg-ccc")
    reg.register(input_root / "pkg-aaa")
    reg.register(input_root / "pkg-bbb")
    reg.register(input_root / "pkg-ccc")

    all_entries = q.list_registrations()
    assert [e.package_id for e in all_entries] == ["pkg-aaa", "pkg-bbb", "pkg-ccc"]
    # 过滤
    assert [e.package_id for e in q.list_registrations(package_id="pkg-bbb")] == ["pkg-bbb"]
    assert [e.package_id for e in q.list_registrations(registration_status="registered")] == [
        "pkg-aaa", "pkg-bbb", "pkg-ccc",
    ]
    assert q.list_registrations(registration_status="superseded") == []
    assert [e.package_id for e in q.list_registrations(model_input_allowed=True)] == [
        "pkg-aaa", "pkg-bbb", "pkg-ccc",
    ]
    assert q.list_registrations(model_input_allowed=False) == []


def env_of(pair):
    return pair[0].input_root, pair[0].runtime_root


# ---------------------------------------------------------------- get_registration


def test_get_registration_returns_consistent_result(pair):
    reg, q = pair
    input_root, _ = env_of(pair)
    pkg = build_package(input_root)
    reg.register(pkg)

    result = q.get_registration("pkg-demo-001", 1)
    assert isinstance(result, RegistrationQueryResult)
    assert result.entry.package_id == "pkg-demo-001"
    assert result.entry.package_revision == 1
    assert result.record.record_id == result.entry.record_id
    assert result.validation.validation_id == result.record.latest_validation_id
    assert result.validation.package_id == result.record.package_id
    assert result.validation.manifest_sha256 == result.record.manifest_sha256
    assert result.issues == []


def test_get_registration_not_found_returns_none(pair):
    reg, q = pair
    input_root, _ = env_of(pair)
    pkg = build_package(input_root)
    reg.register(pkg)
    assert q.get_registration("pkg-does-not-exist", 1) is None
    assert q.get_registration("pkg-demo-001", 99) is None


def test_get_registration_invalid_input_rejected(pair):
    reg, q = pair
    input_root, _ = env_of(pair)
    pkg = build_package(input_root)
    reg.register(pkg)
    for bad_id in ["../evil", "a/b", "a\\b", "C:/x", "..", ""]:
        with pytest.raises(RegistrationError) as excinfo:
            q.get_registration(bad_id, 1)
        assert excinfo.value.code == ERR_RELATIVE_REF_INVALID
    with pytest.raises(RegistrationError) as excinfo:
        q.get_registration("pkg-demo-001", 0)
    assert excinfo.value.code == ERR_RELATIVE_REF_INVALID


# ---------------------------------------------------------------- get_validation


def test_get_validation_latest_and_history(pair):
    reg1, q = pair
    input_root, runtime_root = env_of(pair)
    pkg = build_package(input_root)
    reg1.register(pkg)

    # 不同 validator version 产生历史 validation
    reg2 = EvidenceRegistrationService(input_root, runtime_root, validator_version="2.0.0")
    q2 = EvidenceQueryService(reg2)
    out2 = reg2.register(pkg)
    assert out2.idempotent_hit is False  # validator 不同 -> 新 validation 并更新 record

    # latest
    result = q2.get_registration("pkg-demo-001", 1)
    latest_id = result.validation.validation_id
    assert result.record.latest_validation_id == latest_id
    assert result.record.revision >= 2

    # 历史 validation（第一个）可读取，不改 latest 指针
    val_files = sorted(
        (runtime_root / "phase11/evidence-sidecar/packages/pkg-demo-001/revisions/1/validations").glob("*.json")
    )
    assert len(val_files) >= 2
    history_id = result.record.latest_validation_id
    for vf in val_files:
        vid = vf.stem
        v = q2.get_validation("pkg-demo-001", 1, vid)
        assert v.validation_id == vid
        assert v.package_id == "pkg-demo-001"
        assert v.manifest_sha256 == result.record.manifest_sha256
    # latest 指针不变
    assert q2.get_registration("pkg-demo-001", 1).validation.validation_id == latest_id


def test_get_validation_missing_raises(pair):
    reg, q = pair
    input_root, _ = env_of(pair)
    pkg = build_package(input_root)
    reg.register(pkg)
    with pytest.raises(RegistrationError) as excinfo:
        q.get_validation("pkg-demo-001", 1, "validation-missing-0000")
    assert excinfo.value.code == ERR_VALIDATION_NOT_FOUND


def test_get_validation_path_traversal_rejected(pair):
    reg, q = pair
    input_root, _ = env_of(pair)
    pkg = build_package(input_root)
    reg.register(pkg)
    for bad in ["../evil", "a\\b", "C:/x", "", "a b"]:
        with pytest.raises(RegistrationError):
            q.get_validation("pkg-demo-001", 1, bad)


# ---------------------------------------------------------------- get_validation_issues


def _inject_warning_issue(reg_svc_module, service, pkg, code="SYNTHETIC_WARNING"):
    """构造含 warning issue 的验证结果（通过 monkeypatch 验证器注入）。"""
    from models.evidence_sidecar import ValidationIssueRef
    import services.evidence_registration_service as rmod

    real_validate = rmod.EvidencePackageValidator.validate

    def patched_validate(self, package_dir, mode="dry_run"):
        result, issues = real_validate(self, package_dir, mode=mode)
        if mode != "dry_run":
            issue = ValidationIssue(
                issue_id="issue-synthetic-warning-0001",
                code=code,
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
                created_at=datetime(2026, 8, 5, 7, 0, 0, tzinfo=UTC),
            )
            issues = issues + [issue]
            # 同步把引用加入 ValidationResult.issues（落盘引用数组），保证一致性
            result = result.model_copy(
                update={
                    "issues": [
                        *result.issues,
                        ValidationIssueRef(issue_id=issue.issue_id, code=issue.code, severity=issue.severity),
                    ]
                }
            )
        return result, issues

    rmod.EvidencePackageValidator.validate = patched_validate
    try:
        outcome = service.register(pkg)
    finally:
        rmod.EvidencePackageValidator.validate = real_validate
    return outcome


def test_get_validation_issues_order_and_content(pair):
    reg, q = pair
    input_root, _ = env_of(pair)
    pkg = build_package(input_root, package_id="pkg-warn", level="limited")
    outcome = _inject_warning_issue(None, reg, pkg)
    assert outcome.rejected is False

    result = q.get_registration("pkg-warn", 1)
    assert len(result.issues) == 1
    assert result.issues[0].issue_id == "issue-synthetic-warning-0001"
    assert result.issues[0].code == "SYNTHETIC_WARNING"
    assert result.issues[0].severity == "warning"

    issues = q.get_validation_issues("pkg-warn", 1, result.validation.validation_id)
    assert [i.issue_id for i in issues] == ["issue-synthetic-warning-0001"]
    # 与引用顺序一致
    refs = [r.issue_id for r in result.validation.issues]
    assert [i.issue_id for i in issues] == refs


def test_get_validation_issues_missing_ref_fails(pair):
    reg, q = pair
    input_root, _ = env_of(pair)
    pkg = build_package(input_root, package_id="pkg-warn2", level="limited")
    _inject_warning_issue(None, reg, pkg)

    result = q.get_registration("pkg-warn2", 1)
    vid = result.validation.validation_id
    # 删除 issue 文件 -> 引用缺失显式失败
    issue_file = (
        reg.runtime_root / "phase11/evidence-sidecar/packages/pkg-warn2/revisions/1/issues/issue-synthetic-warning-0001.json"
    )
    issue_file.unlink()
    with pytest.raises(RegistrationError) as excinfo:
        q.get_validation_issues("pkg-warn2", 1, vid)
    assert excinfo.value.code == ERR_INDEX_CORRUPTED


def test_get_validation_issues_corrupt_fails(pair):
    reg, q = pair
    input_root, _ = env_of(pair)
    pkg = build_package(input_root, package_id="pkg-warn3", level="limited")
    _inject_warning_issue(None, reg, pkg)
    result = q.get_registration("pkg-warn3", 1)
    vid = result.validation.validation_id
    issue_file = (
        reg.runtime_root / "phase11/evidence-sidecar/packages/pkg-warn3/revisions/1/issues/issue-synthetic-warning-0001.json"
    )
    issue_file.write_text("{corrupted", encoding="utf-8")
    with pytest.raises(RegistrationError) as excinfo:
        q.get_validation_issues("pkg-warn3", 1, vid)
    assert excinfo.value.code == ERR_RECORD_CORRUPTED


# ---------------------------------------------------------------- 损坏显式失败


def test_corrupted_index_fails(pair):
    reg, q = pair
    input_root, _ = env_of(pair)
    pkg = build_package(input_root)
    reg.register(pkg)
    idx = reg.index_path
    obj = json.loads(idx.read_text(encoding="utf-8"))
    obj["index_sha256"] = "f" * 64
    write_json(idx, obj)
    with pytest.raises(RegistrationError) as excinfo:
        q.list_registrations()
    assert excinfo.value.code == ERR_HASH_MISMATCH


def test_corrupted_record_fails(pair):
    reg, q = pair
    input_root, _ = env_of(pair)
    pkg = build_package(input_root)
    reg.register(pkg)
    rec = reg.runtime_root / "phase11/evidence-sidecar/packages/pkg-demo-001/revisions/1/package-record.json"
    rec.write_text("{corrupted", encoding="utf-8")
    with pytest.raises(RegistrationError) as excinfo:
        q.list_registrations()
    assert excinfo.value.code == ERR_RECORD_CORRUPTED


def test_corrupted_validation_fails(pair):
    reg, q = pair
    input_root, _ = env_of(pair)
    pkg = build_package(input_root)
    reg.register(pkg)
    val_dir = reg.runtime_root / "phase11/evidence-sidecar/packages/pkg-demo-001/revisions/1/validations"
    for f in val_dir.glob("*.json"):
        f.write_text("{corrupted", encoding="utf-8")
    with pytest.raises(RegistrationError):
        q.list_registrations()


# ---------------------------------------------------------------- 只读保证


def test_query_is_readonly_zero_write(pair):
    reg, q = pair
    input_root, _ = env_of(pair)
    pkg = build_package(input_root)
    reg.register(pkg)

    sidecar_before = _snapshot(reg.sidecar_root)
    input_before = _snapshot(pkg)

    # 执行全部查询
    q.list_registrations()
    q.list_registrations(package_id="pkg-demo-001")
    q.get_registration("pkg-demo-001", 1)
    result = q.get_registration("pkg-demo-001", 1)
    q.get_validation("pkg-demo-001", 1, result.validation.validation_id)
    q.get_validation_issues("pkg-demo-001", 1, result.validation.validation_id)

    sidecar_after = _snapshot(reg.sidecar_root)
    input_after = _snapshot(pkg)
    assert sidecar_after == sidecar_before
    assert input_after == input_before
    # 不创建 lock/tmp/audit 等运行文件（audit 已存在且字节不变）
    assert sidecar_after == sidecar_before


def test_query_does_not_read_unregistered_content(pair, monkeypatch):
    reg, q = pair
    input_root, _ = env_of(pair)
    pkg = build_package(input_root)
    # 制造一个未登记文件（验证器会拒绝登记，但用于证明查询不读它）
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
    # 合法包登记（pkg-demo-001 无未登记文件）
    pkg2 = build_package(input_root, package_id="pkg-ok")
    reg.register(pkg2)
    q.list_registrations()
    q.get_registration("pkg-ok", 1)
    assert opened == []


# ---------------------------------------------------------------- 端到端合成验收


def test_end_to_end_synthetic_acceptance(pair):
    """端到端：sufficient + limited 登记、历史 validation、全部查询、重复查询稳定、零写入。"""
    reg, q = pair
    input_root, runtime_root = env_of(pair)

    # 1) sufficient 包验证并登记
    pkg_s = build_package(input_root, package_id="pkg-suff")
    out_s = reg.register(pkg_s)
    assert out_s.rejected is False

    # 2) limited 包验证并登记
    pkg_l = build_package(input_root, package_id="pkg-lim", level="limited")
    out_l = reg.register(pkg_l)
    assert out_l.rejected is False

    # 3) 同一包不同 validator version 的历史验证结果
    reg2 = EvidenceRegistrationService(input_root, runtime_root, validator_version="9.9.9")
    q2 = EvidenceQueryService(reg2)
    out_s2 = reg2.register(pkg_s)
    assert out_s2.idempotent_hit is False
    assert out_s2.record.revision >= 2

    # 4) 查询登记列表（稳定排序）
    ids = [e.package_id for e in q2.list_registrations()]
    assert ids == ["pkg-lim", "pkg-suff"]

    # 5) 查询单个 Record 和 latest ValidationResult
    r = q2.get_registration("pkg-suff", 1)
    assert r is not None
    assert r.record.package_id == "pkg-suff"
    assert r.record.latest_validation_id == r.validation.validation_id
    assert r.record.revision >= 2

    # 6) 查询历史 ValidationResult（第一个 validator 的结果仍可读）
    val_dir = runtime_root / "phase11/evidence-sidecar/packages/pkg-suff/revisions/1/validations"
    vids = sorted(f.stem for f in val_dir.glob("*.json"))
    assert len(vids) >= 2
    for vid in vids:
        v = q2.get_validation("pkg-suff", 1, vid)
        assert v.validation_id == vid
        assert v.manifest_sha256 == r.record.manifest_sha256
    # latest 指针不变
    assert q2.get_registration("pkg-suff", 1).validation.validation_id == r.validation.validation_id

    # 7) 查询 Issue 明细（limited 包）
    rl = q2.get_registration("pkg-lim", 1)
    assert rl is not None
    issues = q2.get_validation_issues("pkg-lim", 1, rl.validation.validation_id)
    assert isinstance(issues, list)

    # 8) 重复查询结果稳定
    ids1 = [e.package_id for e in q2.list_registrations()]
    ids2 = [e.package_id for e in q2.list_registrations()]
    assert ids1 == ids2
    r_a = q2.get_registration("pkg-suff", 1)
    r_b = q2.get_registration("pkg-suff", 1)
    assert r_a.validation.validation_id == r_b.validation.validation_id

    # 9) 查询全过程零写入
    sidecar_before = _snapshot(reg.sidecar_root)
    input_before = _snapshot(pkg_s)
    q2.list_registrations()
    q2.get_registration("pkg-suff", 1)
    q2.get_validation("pkg-suff", 1, vids[0])
    q2.get_validation_issues("pkg-lim", 1, rl.validation.validation_id)
    assert _snapshot(reg.sidecar_root) == sidecar_before
    assert _snapshot(pkg_s) == input_before

    # 10) 不存在的 package/revision/validation 不得伪装为空成功（在损坏 index 前检查）
    assert q2.get_registration("pkg-ghost", 1) is None
    assert q2.get_registration("pkg-suff", 42) is None
    with pytest.raises(RegistrationError) as excinfo:
        q2.get_validation("pkg-suff", 1, "validation-ghost-0000")
    assert excinfo.value.code == ERR_VALIDATION_NOT_FOUND

    # 11) 损坏显式失败（损坏 index 后，任何读 index 的查询都显式失败）
    idx = reg.index_path
    obj = json.loads(idx.read_text(encoding="utf-8"))
    obj["revision"] = obj["revision"] + 1
    obj["index_sha256"] = "0" * 64
    write_json(idx, obj)
    with pytest.raises(RegistrationError):
        q2.list_registrations()


# ---------------------------------------------------------------- 11B-1d-fix-1：公开只读边界


def test_query_service_does_not_call_private_methods():
    """源码边界断言：查询服务不得调用登记服务私有方法，不得导入 _safe_json_load。"""
    from pathlib import Path as _P

    qsrc = _P("backend/services/evidence_query_service.py").read_text(encoding="utf-8")
    for forbidden in (
        "_reg._load_index",
        "_reg._load_record",
        "_reg._load_validation",
        "_reg._issue_path",
        "_safe_json_load",
    ):
        assert forbidden not in qsrc, "查询服务出现禁止引用: " + forbidden


def test_query_service_does_not_import_safe_json_load():
    from pathlib import Path as _P

    qsrc = _P("backend/services/evidence_query_service.py").read_text(encoding="utf-8")
    assert "_safe_json_load" not in qsrc
    allowed = {
        "ERR_INDEX_CORRUPTED",
        "ERR_RECORD_CORRUPTED",
        "ERR_RELATIVE_REF_INVALID",
        "ERR_VALIDATION_NOT_FOUND",
        "EvidenceRegistrationService",
        "RegistrationError",
    }
    for line in qsrc.splitlines():
        stripped = line.strip()
        if stripped.startswith("from services.evidence_registration_service import"):
            body = stripped.replace("from services.evidence_registration_service import", "")
            for token in body.replace(",", " ").split():
                token = token.strip().strip("()")
                if token and token != "(" and token != ")":
                    assert token in allowed, "禁止导入登记模块符号: " + token


def test_public_readonly_methods_zero_write(pair):
    """四个公开只读方法查询前后 sidecar 字节不变。"""
    reg, q = pair
    input_root, _ = env_of(pair)
    pkg = build_package(input_root)
    reg.register(pkg)
    sidecar_before = _snapshot(reg.sidecar_root)
    input_before = _snapshot(pkg)

    assert reg.read_index_strict() is not None
    rec = reg.read_record_strict("pkg-demo-001", 1)
    assert rec is not None
    val = reg.read_validation_strict("pkg-demo-001", 1, rec.latest_validation_id)
    assert val is not None

    assert _snapshot(reg.sidecar_root) == sidecar_before
    assert _snapshot(pkg) == input_before


def test_read_issue_strict_rejects_invalid_issue_id(pair):
    reg, q = pair
    input_root, _ = env_of(pair)
    pkg = build_package(input_root)
    reg.register(pkg)
    for bad in ["../evil", "a\\b", "C:/x", "", "..", "a b"]:
        with pytest.raises(RegistrationError) as excinfo:
            reg.read_issue_strict("pkg-demo-001", 1, bad)
        assert excinfo.value.code == ERR_RELATIVE_REF_INVALID


def test_read_issue_strict_missing_and_corrupt_fail(pair):
    reg, q = pair
    input_root, _ = env_of(pair)
    pkg = build_package(input_root, package_id="pkg-issue", level="limited")
    _inject_warning_issue(None, reg, pkg)
    with pytest.raises(RegistrationError) as excinfo:
        reg.read_issue_strict("pkg-issue", 1, "issue-ghost-0000")
    assert excinfo.value.code == ERR_INDEX_CORRUPTED
    issue_file = (
        reg.runtime_root / "phase11/evidence-sidecar/packages/pkg-issue/revisions/1/issues/issue-synthetic-warning-0001.json"
    )
    issue_file.write_text("{corrupted", encoding="utf-8")
    with pytest.raises(RegistrationError) as excinfo:
        reg.read_issue_strict("pkg-issue", 1, "issue-synthetic-warning-0001")
    assert excinfo.value.code == ERR_RECORD_CORRUPTED


def test_get_registration_none_means_not_registered(pair):
    """None = 未登记（API 层映射 404）；不是空成功对象；不误报 Index 损坏。"""
    reg, q = pair
    input_root, _ = env_of(pair)
    pkg = build_package(input_root)
    reg.register(pkg)
    r = q.get_registration("pkg-demo-001", 1)
    assert r is not None and isinstance(r, RegistrationQueryResult)
    assert q.get_registration("pkg-ghost-0001", 1) is None
    assert q.get_registration("pkg-demo-001", 42) is None


def test_get_validation_not_found_still_explicit(pair):
    """其他单对象查询不存在仍显式失败（get_validation 缺失 -> SIDECAR_VALIDATION_NOT_FOUND）。"""
    reg, q = pair
    input_root, _ = env_of(pair)
    pkg = build_package(input_root)
    reg.register(pkg)
    with pytest.raises(RegistrationError) as excinfo:
        q.get_validation("pkg-demo-001", 1, "validation-ghost-0000")
    assert excinfo.value.code == ERR_VALIDATION_NOT_FOUND
