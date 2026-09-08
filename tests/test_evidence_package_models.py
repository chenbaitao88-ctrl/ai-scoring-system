"""
Phase 11B-1a：EvidencePackage / Evidence Sidecar 数据模型合成测试。

所有测试数据均为脱敏、虚构的合成数据，不包含任何真实学生材料。

覆盖：
- 正常：sufficient/limited EvidencePackage、ready manifest、
  registered PackageRecord、passed ValidationResult、ValidationIssue、RegistrationIndexEntry。
- 拒绝：缺必填字段、非法枚举、非正 package_revision、非法/大写 SHA-256、
  非法 publication status / evidence level / validation status / issue severity、
  负计数、绝对路径、盘符路径、`../` 路径穿越、未知字段、非有限数字、
  结束时间早于开始时间、completion marker 与 publication status 不一致、
  sufficient 但 evidence_items 为空。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from models.evidence_package import (
    EvidenceAvailability,
    EvidenceItem,
    EvidenceManifest,
    EvidencePackage,
    ManifestFileEntry,
    PrivacyCheck,
)
from models.evidence_sidecar import (
    EvidencePackageRecord,
    IssueCounts,
    RegistrationIndex,
    RegistrationIndexEntry,
    ValidationCheckSummary,
    ValidationIssue,
    ValidationIssueRef,
    ValidationResult,
    compute_index_sha256,
)

UTC = timezone.utc
T0 = datetime(2026, 8, 4, 7, 0, 0, tzinfo=UTC)
SHA256 = "a" * 64


# ---------------------------------------------------------------- fixtures


def privacy_check(**overrides):
    data = {
        "status": "passed",
        "privacy_policy_version": "privacy-policy/v1.1",
        "checked_at": T0,
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
        "manifest_sha256": SHA256,
        "generated_at": T0,
        "generator_name": "synthetic-generator",
        "generator_version": "1.0.0",
        "publication_status": "ready",
        "completion_marker": ".evidence-ready",
    }
    data.update(overrides)
    return data


def evidence_item(**overrides):
    data = {
        "evidence_id": "ev_code_001",
        "evidence_type": "code_summary",
        "summary": "脱敏合成摘要：含主循环与基本功能模块。",
        "availability": "available",
        "source_refs": ["file-demo-001"],
        "model_input_allowed": True,
        "limitations": [],
    }
    data.update(overrides)
    return data


def evidence_package(**overrides):
    data = {
        **package_common(),
        "evidence_level": "sufficient",
        "evidence_items": [evidence_item()],
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


def manifest_file_entry(**overrides):
    data = {
        "file_id": "file-demo-001",
        "relative_path": "summary/code.json",
        "file_role": "code_summary",
        "mime_type": "application/json",
        "media_type": "json",
        "size_bytes": 1024,
        "sha256": SHA256,
        "privacy_status": "passed",
        "model_input_allowed": True,
        "derived_from_raw": True,
        "redaction_applied": True,
    }
    data.update(overrides)
    return data


def evidence_manifest(**overrides):
    data = {
        **package_common(),
        "files": [manifest_file_entry()],
        "privacy_check": privacy_check(),
    }
    data.update(overrides)
    return data


def package_record(**overrides):
    data = {
        "record_schema_version": "evidence-sidecar/record/v1",
        "record_id": "rec-demo-001",
        "package_id": "pkg-demo-001",
        "package_revision": 1,
        "batch_id": "batch-demo-001",
        "submission_id": "sub-demo-001",
        "evidence_version": "1",
        "manifest_sha256": SHA256,
        "privacy_policy_version": "privacy-policy/v1.1",
        "contract_version": "evidence-package/v1.1",
        "publication_status": "ready",
        "registration_status": "registered",
        "latest_validation_id": "val-demo-001",
        "latest_validation_status": "passed",
        "source_package_ref": "packages/pkg-demo-001",
        "registered_at": T0,
        "registered_by": "registrar-demo-01",
        "created_at": T0,
        "updated_at": T0,
        "revision": 1,
        "record_sha256": SHA256,
    }
    data.update(overrides)
    return data


def validation_result(**overrides):
    data = {
        "validation_schema_version": "evidence-sidecar/validation/v1",
        "validation_id": "val-demo-001",
        "record_id": "rec-demo-001",
        "package_id": "pkg-demo-001",
        "package_revision": 1,
        "manifest_sha256": SHA256,
        "validator_name": "validator-demo",
        "validator_version": "1.0.0",
        "evidence_contract_version": "evidence-package/v1.1",
        "privacy_policy_version": "privacy-policy/v1.1",
        "mode": "registration",
        "status": "passed",
        "started_at": T0,
        "completed_at": T0 + timedelta(seconds=2),
        "duration_ms": 2000,
        "checks": [
            {"check_id": "JSON_PARSEABLE", "status": "passed", "message_key": "CHECK_OK"}
        ],
        "issues": [],
        "issue_counts": {"info": 0, "warning": 0, "error": 0, "fatal": 0},
        "model_input_allowed": True,
        "registration_allowed": True,
        "validated_file_count": 1,
        "declared_file_count": 1,
        "unregistered_file_count": 0,
        "result_sha256": SHA256,
    }
    data.update(overrides)
    return data


def validation_issue(**overrides):
    data = {
        "issue_id": "iss-demo-001",
        "code": "UNREGISTERED_PACKAGE_FILE",
        "severity": "error",
        "stage": "引用完整性",
        "message_key": "UNREGISTERED_FILE_FOUND",
        "location": "包根目录",
        "file_ref": "extra/unknown.txt",
        "field_path": "files",
        "expected_summary": "仅三类控制文件与 manifest 登记文件",
        "actual_summary": "发现 1 个未登记普通文件",
        "retryable": False,
        "blocks_registration": True,
        "blocks_model_input": True,
        "requires_manual_review": True,
        "created_at": T0,
    }
    data.update(overrides)
    return data


def index_entry(**overrides):
    data = {
        "index_schema_version": "evidence-sidecar/index/v1",
        "entry_id": "idx-demo-001",
        "record_id": "rec-demo-001",
        "package_id": "pkg-demo-001",
        "package_revision": 1,
        "batch_id": "batch-demo-001",
        "submission_id": "sub-demo-001",
        "evidence_version": "1",
        "manifest_sha256": SHA256,
        "registration_status": "registered",
        "latest_validation_status": "passed",
        "model_input_allowed": True,
        "record_ref": "records/rec-demo-001.json",
        "latest_validation_ref": "validations/val-demo-001.json",
        "registered_at": T0,
        "updated_at": T0,
        "revision": 1,
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------- 正常


def test_sufficient_evidence_package_parses():
    pkg = EvidencePackage(**evidence_package())
    assert pkg.package_revision == 1
    assert pkg.publication_status == "ready"
    assert pkg.completion_marker == ".evidence-ready"
    assert pkg.evidence_level == "sufficient"
    assert pkg.privacy_check.status == "passed"


def test_limited_evidence_package_parses():
    pkg = EvidencePackage(
        **evidence_package(
            evidence_level="limited",
            evidence_items=[evidence_item(availability="partial")],
        )
    )
    assert pkg.evidence_level == "limited"


def test_ready_manifest_parses():
    manifest = EvidenceManifest(**evidence_manifest())
    assert manifest.files[0].file_id == "file-demo-001"
    assert manifest.files[0].relative_path == "summary/code.json"
    assert manifest.manifest_sha256 == SHA256


def test_registered_package_record_parses():
    record = EvidencePackageRecord(**package_record())
    assert record.registration_status == "registered"
    assert record.revision >= 1


def test_passed_validation_result_parses():
    result = ValidationResult(**validation_result())
    assert result.status == "passed"
    assert result.model_input_allowed is True
    assert result.registration_allowed is True
    assert result.completed_at >= result.started_at


def test_validation_issue_parses():
    issue = ValidationIssue(**validation_issue())
    assert issue.severity == "error"
    assert issue.blocks_registration is True


def test_registration_index_entry_parses():
    entry = RegistrationIndexEntry(**index_entry())
    assert entry.registration_status == "registered"
    assert entry.record_ref.startswith("records/")


# ---------------------------------------------------------------- 拒绝


def test_missing_required_field_rejected():
    data = evidence_package()
    del data["package_id"]
    with pytest.raises(ValidationError):
        EvidencePackage(**data)


def test_unknown_contract_version_rejected():
    with pytest.raises(ValidationError):
        EvidencePackage(**evidence_package(contract_version="evidence-package/v9.9"))


def test_zero_or_negative_package_revision_rejected():
    with pytest.raises(ValidationError):
        EvidencePackage(**evidence_package(package_revision=0))
    with pytest.raises(ValidationError):
        EvidencePackage(**evidence_package(package_revision=-1))


def test_invalid_sha256_rejected():
    with pytest.raises(ValidationError):
        EvidencePackage(**evidence_package(manifest_sha256="z" * 64))


def test_uppercase_sha256_rejected():
    with pytest.raises(ValidationError):
        EvidencePackage(**evidence_package(manifest_sha256="A" * 64))


def test_invalid_publication_status_rejected():
    with pytest.raises(ValidationError):
        EvidencePackage(**evidence_package(publication_status="published"))


def test_invalid_evidence_level_rejected():
    with pytest.raises(ValidationError):
        EvidencePackage(**evidence_package(evidence_level="excellent"))


def test_invalid_validation_status_rejected():
    with pytest.raises(ValidationError):
        ValidationResult(**validation_result(status="completed"))


def test_invalid_issue_severity_rejected():
    with pytest.raises(ValidationError):
        ValidationIssue(**validation_issue(severity="critical"))


def test_negative_issue_counts_rejected():
    with pytest.raises(ValidationError):
        IssueCounts(info=-1, warning=0, error=0, fatal=0)
    with pytest.raises(ValidationError):
        ValidationResult(**validation_result(issue_counts={"info": 0, "warning": -2, "error": 0, "fatal": 0}))


def test_absolute_path_rejected():
    with pytest.raises(ValidationError):
        EvidenceManifest(**evidence_manifest(files=[manifest_file_entry(relative_path="/etc/passwd")]))


def test_windows_drive_path_rejected():
    with pytest.raises(ValidationError):
        EvidenceManifest(**evidence_manifest(files=[manifest_file_entry(relative_path="C:/windows/evil.txt")]))


def test_path_traversal_rejected():
    with pytest.raises(ValidationError):
        EvidenceManifest(**evidence_manifest(files=[manifest_file_entry(relative_path="../secret.json")]))


def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        EvidencePackage(**evidence_package(extra_field="nope"))


def test_non_finite_number_rejected():
    with pytest.raises(ValidationError):
        EvidenceManifest(**evidence_manifest(files=[manifest_file_entry(size_bytes=float("nan"))]))
    with pytest.raises(ValidationError):
        ValidationResult(**validation_result(duration_ms=float("inf")))


def test_completed_before_started_rejected():
    with pytest.raises(ValidationError):
        ValidationResult(
            **validation_result(
                started_at=T0 + timedelta(seconds=5),
                completed_at=T0,
            )
        )


def test_completion_marker_mismatch_rejected():
    # ready 但缺少 completion_marker
    with pytest.raises(ValidationError):
        EvidencePackage(**evidence_package(completion_marker=None))
    # staging 却声明 completion_marker
    with pytest.raises(ValidationError):
        EvidencePackage(
            **evidence_package(
                publication_status="staging",
                completion_marker=".evidence-ready",
            )
        )


def test_sufficient_with_empty_items_rejected():
    with pytest.raises(ValidationError):
        EvidencePackage(**evidence_package(evidence_level="sufficient", evidence_items=[]))


# ---------------------------------------------------------------- 时间严格 RFC 3339 UTC


def test_z_suffix_utc_time_parses():
    # RFC 3339 "Z" 后缀字符串可直接解析为 UTC aware datetime
    pkg = EvidencePackage(**evidence_package(generated_at="2026-08-04T07:00:00Z"))
    assert pkg.generated_at.utcoffset() == timedelta(0)
    issue = ValidationIssue(**validation_issue(created_at="2026-08-04T07:00:00Z"))
    assert issue.created_at.utcoffset() == timedelta(0)


def test_explicit_utc_offset_zero_time_parses():
    pkg = EvidencePackage(**evidence_package(generated_at=datetime(2026, 8, 4, 7, 0, 0, tzinfo=UTC)))
    assert pkg.generated_at.utcoffset() == timedelta(0)
    result = ValidationResult(**validation_result())
    assert result.started_at.utcoffset() == timedelta(0)
    assert result.completed_at.utcoffset() == timedelta(0)


def test_naive_datetime_rejected():
    naive = datetime(2026, 8, 4, 7, 0, 0)  # 无 tzinfo
    with pytest.raises(ValidationError):
        EvidencePackage(**evidence_package(generated_at=naive))
    with pytest.raises(ValidationError):
        EvidenceManifest(**evidence_manifest(generated_at=naive))
    with pytest.raises(ValidationError):
        EvidencePackage(**evidence_package(privacy_check=privacy_check(checked_at=naive)))
    with pytest.raises(ValidationError):
        EvidencePackageRecord(**package_record(registered_at=naive))
    with pytest.raises(ValidationError):
        ValidationResult(**validation_result(started_at=naive))
    with pytest.raises(ValidationError):
        ValidationIssue(**validation_issue(created_at=naive))
    with pytest.raises(ValidationError):
        RegistrationIndexEntry(**index_entry(registered_at=naive))


def test_non_utc_offset_rejected():
    # +08:00 等非 UTC offset 必须拒绝
    tz8 = timezone(timedelta(hours=8))
    local_time = datetime(2026, 8, 4, 15, 0, 0, tzinfo=tz8)
    with pytest.raises(ValidationError):
        EvidencePackage(**evidence_package(generated_at=local_time))
    with pytest.raises(ValidationError):
        EvidenceManifest(**evidence_manifest(generated_at=local_time))
    with pytest.raises(ValidationError):
        EvidencePackage(**evidence_package(privacy_check=privacy_check(checked_at=local_time)))
    with pytest.raises(ValidationError):
        EvidencePackageRecord(**package_record(registered_at=local_time))
    with pytest.raises(ValidationError):
        EvidencePackageRecord(**package_record(updated_at=local_time))
    with pytest.raises(ValidationError):
        ValidationResult(**validation_result(completed_at=local_time))
    with pytest.raises(ValidationError):
        ValidationIssue(**validation_issue(created_at=local_time))
    with pytest.raises(ValidationError):
        RegistrationIndexEntry(**index_entry(updated_at=local_time))


# ---------------------------------------------------------------- 契约示例兼容性


def test_contract_14_5_validation_issue_example_parses():
    """Sidecar 契约 14.5 的 ValidationIssue 示例必须可直接解析。"""
    example = {
        "issue_id": "issue-demo-0002",
        "code": "PRIVACY_CHECK_FAILED",
        "severity": "error",
        "stage": "privacy_gate",
        "message_key": "PRIVACY_CHECK_FAILED",
        "location": "evidence.json::privacy_check",
        "file_ref": "evidence.json",
        "field_path": "privacy_check.status",
        "expected_summary": "passed",
        "actual_summary": "failed",
        "retryable": False,
        "blocks_registration": True,
        "blocks_model_input": True,
        "requires_manual_review": True,
        "created_at": "2026-08-05T07:10:00Z",
    }
    issue = ValidationIssue(**example)
    assert issue.issue_id == "issue-demo-0002"
    assert issue.code == "PRIVACY_CHECK_FAILED"
    assert issue.created_at.utcoffset() == timedelta(0)


def test_contract_14_6_index_entry_example_parses():
    """Sidecar 契约 14.6 的 RegistrationIndexEntry 示例必须可直接解析。"""
    example = {
        "index_schema_version": "evidence-sidecar/index/v1",
        "entry_id": "entry-demo-0001",
        "record_id": "record-demo-0001",
        "package_id": "ep1_batch-demo-a-sub-demo-001",
        "package_revision": 1,
        "batch_id": "batch_demo-a",
        "submission_id": "sub_demo-001",
        "evidence_version": "1",
        "manifest_sha256": "e" * 64,
        "registration_status": "registered",
        "latest_validation_status": "passed",
        "model_input_allowed": True,
        "record_ref": "packages/ep1_batch-demo-a-sub-demo-001/revisions/1/package-record.json",
        "latest_validation_ref": "packages/ep1_batch-demo-a-sub-demo-001/revisions/1/validations/validation-demo-0001.json",
        "registered_at": "2026-08-05T07:00:00Z",
        "updated_at": "2026-08-05T07:00:00Z",
        "revision": 1,
    }
    entry = RegistrationIndexEntry(**example)
    assert entry.registration_status == "registered"
    assert entry.record_ref.startswith("packages/")


# ---------------------------------------------------------------- RegistrationIndex 容器


def _index_entry(pkg: str, rev: int, mh: str = "a" * 64) -> RegistrationIndexEntry:
    return RegistrationIndexEntry(
        index_schema_version="evidence-sidecar/index/v1",
        entry_id=f"entry-{pkg}-{rev}",
        record_id=f"record-{pkg}-{rev}",
        package_id=pkg,
        package_revision=rev,
        batch_id="batch_demo-a",
        submission_id="sub_demo-001",
        evidence_version="1",
        manifest_sha256=mh,
        registration_status="registered",
        latest_validation_status="passed",
        model_input_allowed=True,
        record_ref=f"packages/{pkg}/revisions/{rev}/package-record.json",
        latest_validation_ref=f"packages/{pkg}/revisions/{rev}/validations/val-{pkg}-{rev}.json",
        registered_at=T0,
        updated_at=T0,
        revision=1,
    )


def test_registration_index_container_parses():
    idx = RegistrationIndex(
        index_schema_version="evidence-sidecar/index-container/v1",
        index_id="index-demo-0001",
        revision=1,
        created_at=T0,
        updated_at=T0,
        entries=[_index_entry("pkg-a", 1), _index_entry("pkg-a", 2), _index_entry("pkg-b", 1)],
        index_sha256=SHA256,
    )
    assert idx.revision == 1
    assert len(idx.entries) == 3


def test_registration_index_sha256_computable():
    idx = RegistrationIndex(
        index_schema_version="evidence-sidecar/index-container/v1",
        index_id="index-demo-0002",
        revision=1,
        created_at=T0,
        updated_at=T0,
        entries=[_index_entry("pkg-a", 1)],
        index_sha256=SHA256,
    )
    h = compute_index_sha256(idx)
    # 64 位小写十六进制
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)
    # 相同内容产生相同 hash
    assert compute_index_sha256(idx) == h
    # 受保护字段变化产生不同 hash
    idx2 = idx.model_copy(update={"revision": idx.revision + 1})
    assert compute_index_sha256(idx2) != h
    idx3 = idx.model_copy(update={"index_id": "index-demo-modified"})
    assert compute_index_sha256(idx3) != h
    idx4 = idx.model_copy(update={"entries": [_index_entry("pkg-a", 1), _index_entry("pkg-a", 3)]})
    assert compute_index_sha256(idx4) != h


def test_registration_index_unknown_field_rejected():
    with pytest.raises(ValidationError):
        RegistrationIndex(
            index_schema_version="evidence-sidecar/index-container/v1",
            index_id="index-demo-0003",
            revision=1,
            created_at=T0,
            updated_at=T0,
            entries=[],
            index_sha256=SHA256,
            extra_field="x",
        )


def test_registration_index_duplicate_key_rejected():
    with pytest.raises(ValidationError):
        RegistrationIndex(
            index_schema_version="evidence-sidecar/index-container/v1",
            index_id="index-demo-0004",
            revision=1,
            created_at=T0,
            updated_at=T0,
            entries=[_index_entry("pkg-a", 1), _index_entry("pkg-a", 1, mh="b" * 64)],
            index_sha256=SHA256,
        )


def test_registration_index_unsorted_rejected():
    with pytest.raises(ValidationError):
        RegistrationIndex(
            index_schema_version="evidence-sidecar/index-container/v1",
            index_id="index-demo-0005",
            revision=1,
            created_at=T0,
            updated_at=T0,
            entries=[_index_entry("pkg-b", 1), _index_entry("pkg-a", 1)],
            index_sha256=SHA256,
        )


def test_registration_index_invalid_hash_revision_time_rejected():
    base = dict(
        index_schema_version="evidence-sidecar/index-container/v1",
        index_id="index-demo-0006",
        revision=1,
        created_at=T0,
        updated_at=T0,
        entries=[],
    )
    with pytest.raises(ValidationError):
        RegistrationIndex(**base, index_sha256="Z" * 64)
    bad_rev = dict(base)
    bad_rev["revision"] = 0
    bad_rev["index_sha256"] = SHA256
    with pytest.raises(ValidationError):
        RegistrationIndex(**bad_rev)
    bad_naive = dict(base)
    bad_naive["index_sha256"] = SHA256
    bad_naive["created_at"] = datetime(2026, 8, 4, 7, 0, 0)  # naive
    with pytest.raises(ValidationError):
        RegistrationIndex(**bad_naive)
    bad_order = dict(base)
    bad_order["index_sha256"] = SHA256
    bad_order["updated_at"] = T0 - timedelta(days=1)
    with pytest.raises(ValidationError):
        RegistrationIndex(**bad_order)


# ---------------------------------------------------------------- 11C-2c-prerequisite-fix-1：evidence_level 路由


def test_validation_result_evidence_level_persisted():
    """新验证结果保存 evidence_level，重新解析后值保留。"""
    for lv in ("sufficient", "limited", "manual_only"):
        raw = validation_result(evidence_level=lv)
        v = ValidationResult(**raw)
        assert v.evidence_level == lv
        # 序列化-反序列化往返（持久化再读取等价）
        again = ValidationResult(**v.model_dump(mode="json"))
        assert again.evidence_level == lv


def test_validation_result_evidence_level_old_sidecar_backward_compat():
    """旧版 sidecar 缺 evidence_level 字段仍可解析，值为 None。"""
    raw = validation_result()  # 无 evidence_level（向后兼容旧结构）
    assert "evidence_level" not in raw
    v = ValidationResult(**raw)
    assert v.evidence_level is None
    # None 显式传入等价
    v2 = ValidationResult(**validation_result(evidence_level=None))
    assert v2.evidence_level is None


def test_validation_result_evidence_level_invalid_rejected():
    """未知/非法 evidence_level 值拒绝。"""
    with pytest.raises(ValidationError):
        ValidationResult(**validation_result(evidence_level="not_a_level"))
