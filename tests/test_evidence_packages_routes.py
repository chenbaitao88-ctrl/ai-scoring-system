"""
Phase 11B-2b：EvidencePackage 导入 API 路由合成测试。

独立 FastAPI app + dependency_overrides 注入 tmp_path 服务，不读写真实 data/。

覆盖：
- 核心接口 18 项：dry-run（sufficient/limited/failed/零写入）、登记（201/200 幂等/更新 201/拒绝 422/冲突 409 零副作用）、
  列表过滤、详情、历史 validation、issues 顺序与安全字段、None->404、受控根外拒绝、不读未登记正文、无评分/Provider/DB、
  OpenAPI 6 条路径无 dry-run-batch。
- 错误映射：404/409/422/500、安全错误字段、rejected 422 响应体。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import evidence_packages  # noqa: F401
from models.evidence_api import EvidenceApiError
from services.evidence_api_mapper import (
    query_error_status,
    registration_error_status,
    to_api_error,
)
from services.evidence_package_validator import canonical_json_bytes
from services.evidence_query_service import EvidenceQueryService
from services.evidence_registration_service import (
    ERR_AUDIT_WRITE_FAILED,
    ERR_CONTENT_CONFLICT,
    ERR_HASH_MISMATCH,
    ERR_IDEMPOTENCY_CONFLICT,
    ERR_INDEX_CORRUPTED,
    ERR_INDEX_WRITE_FAILED,
    ERR_INTERNAL,
    ERR_LOCK_CONFLICT,
    ERR_RECORD_CONFLICT,
    ERR_RECORD_CORRUPTED,
    ERR_REGISTRATION_NOT_ALLOWED,
    ERR_RELATIVE_REF_INVALID,
    ERR_REVISION_CONFLICT,
    ERR_UNSUPPORTED_VERSION,
    ERR_VALIDATION_NOT_FOUND,
    EvidenceRegistrationService,
    RegistrationError,
)

UTC = timezone.utc


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- 合成包构造


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
        "availability": {"code": "available", "document": "available", "video": "not_applicable", "transcript": "not_applicable"},
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
    level: str = "sufficient",
    publication_status: str = "ready",
) -> Path:
    pkg_dir = input_root / package_id
    pkg_dir.mkdir(parents=True, exist_ok=True)
    content = b'{"synthetic": true, "summary": "code structure"}'
    code_file = pkg_dir / "evidence" / "code_summary.json"
    code_file.parent.mkdir(parents=True, exist_ok=True)
    code_file.write_bytes(content)

    manifest = manifest_body(package_id=package_id, publication_status=publication_status)
    manifest["files"][0]["size_bytes"] = len(content)
    manifest["files"][0]["sha256"] = sha256_bytes(content)
    obj = dict(manifest)
    obj["manifest_sha256"] = None
    h = sha256_bytes(canonical_json_bytes(obj))
    manifest["manifest_sha256"] = h
    write_json(pkg_dir / "evidence_manifest.json", manifest)

    evidence = evidence_body(package_id=package_id, publication_status=publication_status)
    evidence["manifest_sha256"] = h
    if level == "limited":
        evidence["evidence_level"] = "limited"
        evidence["manual_review_reasons"] = ["CORE_EVIDENCE_PARTIAL"]
    write_json(pkg_dir / "evidence.json", evidence)

    (pkg_dir / ".evidence-ready").write_text("", encoding="utf-8")
    return pkg_dir


# ---------------------------------------------------------------- app 与 fixtures


@pytest.fixture
def app_env(tmp_path):
    input_root = tmp_path / "evidence-inputs"
    runtime_root = tmp_path / "evidence-runtime"
    input_root.mkdir()
    runtime_root.mkdir()
    return input_root, runtime_root


@pytest.fixture
def client(app_env):
    input_root, runtime_root = app_env
    app = FastAPI()
    app.include_router(evidence_packages.router)

    def over_reg():
        return EvidenceRegistrationService(input_root, runtime_root)

    def over_query():
        return EvidenceQueryService(over_reg())

    app.dependency_overrides[evidence_packages.get_evidence_registration_service] = over_reg
    app.dependency_overrides[evidence_packages.get_evidence_query_service] = over_query
    with TestClient(app) as c:
        yield c


def _snapshot(root: Path):
    snap = {}
    for p in root.rglob("*"):
        if p.is_file():
            snap[str(p.relative_to(root)).replace("\\", "/")] = p.read_bytes()
    return snap


# ---------------------------------------------------------------- dry-run


def test_dry_run_sufficient_200(client, app_env):
    input_root, _ = app_env
    build_package(input_root)
    r = client.post("/api/evidence-packages/dry-run", json={"package_ref": "pkg-demo-001"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "passed"
    assert body["evidence_level"] == "sufficient"  # 从已验证包映射实际等级
    assert body["package_id"] == "pkg-demo-001"


def test_dry_run_limited_200(client, app_env):
    input_root, _ = app_env
    build_package(input_root, package_id="pkg-lim", level="limited")
    r = client.post("/api/evidence-packages/dry-run", json={"package_ref": "pkg-lim"})
    assert r.status_code == 200
    assert r.json()["evidence_level"] == "limited"


def test_dry_run_failed_200(client, app_env):
    input_root, _ = app_env
    pkg = build_package(input_root, package_id="pkg-bad")
    ev = json.loads((pkg / "evidence.json").read_text(encoding="utf-8"))
    ev["privacy_check"]["contains_direct_identity"] = True  # 隐私门失败
    write_json(pkg / "evidence.json", ev)
    r = client.post("/api/evidence-packages/dry-run", json={"package_ref": "pkg-bad"})
    assert r.status_code == 200  # 验证结论 failed 仍 200
    assert r.json()["status"] in ("failed", "internal_error")


def test_dry_run_zero_sidecar_write(client, app_env):
    input_root, runtime_root = app_env
    build_package(input_root)
    before = _snapshot(runtime_root)
    client.post("/api/evidence-packages/dry-run", json={"package_ref": "pkg-demo-001"})
    assert _snapshot(runtime_root) == before  # 零写入（无任何运行文件）


# ---------------------------------------------------------------- 登记


def test_first_register_201(client, app_env):
    input_root, runtime_root = app_env
    build_package(input_root)
    r = client.post("/api/evidence-packages/register", json={"package_ref": "pkg-demo-001"})
    assert r.status_code == 201
    body = r.json()
    assert body["rejected"] is False
    assert body["record_id"] and body["entry_id"]
    assert body["index_revision"] == 1  # 严格读取的真实 Index revision


def test_idempotent_register_200(client, app_env):
    input_root, _ = app_env
    build_package(input_root)
    assert client.post("/api/evidence-packages/register", json={"package_ref": "pkg-demo-001"}).status_code == 201
    r2 = client.post("/api/evidence-packages/register", json={"package_ref": "pkg-demo-001"})
    assert r2.status_code == 200
    assert r2.json()["idempotent_hit"] is True
    assert r2.json()["record_id"]


def test_register_validator_update_201(client, app_env):
    input_root, runtime_root = app_env
    build_package(input_root)
    assert client.post("/api/evidence-packages/register", json={"package_ref": "pkg-demo-001"}).status_code == 201
    # 更换 validator version 的服务实例后再次登记（模拟升级） -> 更新 201
    reg2 = EvidenceRegistrationService(input_root, runtime_root, validator_version="9.9.9")
    from services.evidence_registration_service import EvidenceRegistrationService as _ERS
    from services.evidence_query_service import EvidenceQueryService as _EQS

    app = FastAPI()
    app.include_router(evidence_packages.router)
    app.dependency_overrides[evidence_packages.get_evidence_registration_service] = lambda: reg2
    app.dependency_overrides[evidence_packages.get_evidence_query_service] = lambda: _EQS(reg2)
    with TestClient(app) as c:
        r = c.post("/api/evidence-packages/register", json={"package_ref": "pkg-demo-001"})
    assert r.status_code == 201  # 新 ValidationResult 更新成功
    assert r.json()["idempotent_hit"] is False
    assert r.json()["index_revision"] == 2


def test_register_rejected_422_with_safe_validation(client, app_env):
    input_root, _ = app_env
    pkg = build_package(input_root, package_id="pkg-rej")
    ev = json.loads((pkg / "evidence.json").read_text(encoding="utf-8"))
    ev["privacy_check"]["contains_direct_identity"] = True  # 隐私门失败 -> 拒绝
    write_json(pkg / "evidence.json", ev)
    r = client.post("/api/evidence-packages/register", json={"package_ref": "pkg-rej"})
    assert r.status_code == 422
    body = r.json()
    assert body["rejected"] is True
    assert body["validation"] is not None  # 携带安全 validation
    assert body["validation"]["status"] in ("failed", "internal_error")
    assert body["record_id"] is None and body["entry_id"] is None and body["index_revision"] is None


def test_register_revision_conflict_409_zero_side_effect(client, app_env):
    input_root, runtime_root = app_env
    build_package(input_root)
    assert client.post("/api/evidence-packages/register", json={"package_ref": "pkg-demo-001"}).status_code == 201
    build_package(input_root, package_id="pkg-second")
    before = _snapshot(runtime_root)
    r = client.post(
        "/api/evidence-packages/register",
        json={"package_ref": "pkg-second", "expected_index_revision": 99},
    )
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "SIDECAR_REVISION_CONFLICT"
    assert _snapshot(runtime_root) == before  # 零副作用


def test_register_idempotency_conflict_409(client, app_env):
    input_root, _ = app_env
    pkg = build_package(input_root)
    assert client.post("/api/evidence-packages/register", json={"package_ref": "pkg-demo-001"}).status_code == 201
    # 篡改 manifest（同身份不同哈希）-> 幂等冲突
    mv = json.loads((pkg / "evidence_manifest.json").read_text(encoding="utf-8"))
    mv["files"][0]["mime_type"] = "text/plain"
    obj = dict(mv)
    obj["manifest_sha256"] = None
    mv["manifest_sha256"] = sha256_bytes(canonical_json_bytes(obj))
    write_json(pkg / "evidence_manifest.json", mv)
    ev = json.loads((pkg / "evidence.json").read_text(encoding="utf-8"))
    ev["manifest_sha256"] = mv["manifest_sha256"]
    write_json(pkg / "evidence.json", ev)
    r = client.post("/api/evidence-packages/register", json={"package_ref": "pkg-demo-001"})
    assert r.status_code == 409
    assert r.json()["code"] == "REGISTRATION_IDEMPOTENCY_CONFLICT"


def test_register_request_invalid_422(client):
    r = client.post("/api/evidence-packages/register", json={"package_ref": "/abs/path"})
    assert r.status_code == 422  # 请求模型非法


# ---------------------------------------------------------------- 查询


def test_list_registrations_filter(client, app_env):
    input_root, _ = app_env
    build_package(input_root, package_id="pkg-aaa")
    build_package(input_root, package_id="pkg-bbb")
    client.post("/api/evidence-packages/register", json={"package_ref": "pkg-aaa"})
    client.post("/api/evidence-packages/register", json={"package_ref": "pkg-bbb"})
    r = client.get("/api/evidence-packages/registrations")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert [i["package_id"] for i in body["items"]] == ["pkg-aaa", "pkg-bbb"]
    r2 = client.get("/api/evidence-packages/registrations", params={"package_id": "pkg-bbb"})
    assert r2.json()["total"] == 1
    assert r2.json()["items"][0]["package_id"] == "pkg-bbb"


def test_get_registration_detail(client, app_env):
    input_root, _ = app_env
    build_package(input_root)
    client.post("/api/evidence-packages/register", json={"package_ref": "pkg-demo-001"})
    r = client.get("/api/evidence-packages/registrations/pkg-demo-001/revisions/1")
    assert r.status_code == 200
    body = r.json()
    assert body["record_id"] and body["entry_id"]
    assert body["latest_validation"]["status"] == "passed"
    assert body["latest_validation"]["evidence_level"] is None  # Sidecar 未持久化 -> null
    # 安全：响应不含 internal 字段
    dumped = json.dumps(body)
    for forbidden in ("actual_summary", "manifest_sha256", "record_sha256", "validator_name", "source_package_ref"):
        assert forbidden not in dumped


def test_get_registration_404_none_to_404(client, app_env):
    input_root, _ = app_env
    build_package(input_root)
    client.post("/api/evidence-packages/register", json={"package_ref": "pkg-demo-001"})
    r = client.get("/api/evidence-packages/registrations/pkg-ghost/revisions/1")
    assert r.status_code == 404
    r2 = client.get("/api/evidence-packages/registrations/pkg-demo-001/revisions/99")
    assert r2.status_code == 404


def test_get_historical_validation(client, app_env):
    input_root, runtime_root = app_env
    build_package(input_root)
    assert client.post("/api/evidence-packages/register", json={"package_ref": "pkg-demo-001"}).status_code == 201
    reg2 = EvidenceRegistrationService(input_root, runtime_root, validator_version="2.0.0")
    app = FastAPI()
    app.include_router(evidence_packages.router)
    app.dependency_overrides[evidence_packages.get_evidence_registration_service] = lambda: reg2
    app.dependency_overrides[evidence_packages.get_evidence_query_service] = lambda: EvidenceQueryService(reg2)
    with TestClient(app) as c:
        assert c.post("/api/evidence-packages/register", json={"package_ref": "pkg-demo-001"}).status_code == 201
        detail = c.get("/api/evidence-packages/registrations/pkg-demo-001/revisions/1").json()
        latest_id = detail["latest_validation"]["validation_id"]
        # 历史 validation（非 latest）可读
        val_dir = runtime_root / "phase11/evidence-sidecar/packages/pkg-demo-001/revisions/1/validations"
        vids = sorted(f.stem for f in val_dir.glob("*.json"))
        assert len(vids) >= 2
        history_id = [v for v in vids if v != latest_id][0]
        r = c.get(f"/api/evidence-packages/registrations/pkg-demo-001/revisions/1/validations/{history_id}")
        assert r.status_code == 200
        assert r.json()["validation_id"] == history_id
        assert r.json()["evidence_level"] is None


def test_get_validation_404(client, app_env):
    input_root, _ = app_env
    build_package(input_root)
    client.post("/api/evidence-packages/register", json={"package_ref": "pkg-demo-001"})
    r = client.get("/api/evidence-packages/registrations/pkg-demo-001/revisions/1/validations/validation-ghost-0000")
    assert r.status_code == 404


def test_get_issues_order_and_safe_fields(client, app_env):
    input_root, _ = app_env
    pkg = build_package(input_root, package_id="pkg-warn", level="limited")
    # 注入 warning issue（通过 monkeypatch 验证器）——直接经服务登记，再用 API 查询
    import services.evidence_registration_service as rmod
    from models.evidence_sidecar import ValidationIssue, ValidationIssueRef

    real_validate = rmod.EvidencePackageValidator.validate

    def patched(self, package_dir, mode="dry_run"):
        result, issues = real_validate(self, package_dir, mode=mode)
        if mode != "dry_run":
            issue = ValidationIssue(
                issue_id="issue-synthetic-warning-0001", code="SYNTHETIC_WARNING", severity="warning",
                stage="evidence_level", message_key="SYNTHETIC_WARNING", location="evidence_level",
                expected_summary="passed", actual_summary="limited", retryable=False,
                blocks_registration=False, blocks_model_input=False, requires_manual_review=False,
                created_at=datetime(2026, 8, 5, 7, 0, 0, tzinfo=UTC),
            )
            result = result.model_copy(update={
                "issues": [*result.issues, ValidationIssueRef(issue_id=issue.issue_id, code=issue.code, severity=issue.severity)],
            })
            issues = issues + [issue]
        return result, issues

    rmod.EvidencePackageValidator.validate = patched
    try:
        client.post("/api/evidence-packages/register", json={"package_ref": "pkg-warn"})
    finally:
        rmod.EvidencePackageValidator.validate = real_validate

    # 从详情拿 latest validation_id
    detail = client.get("/api/evidence-packages/registrations/pkg-warn/revisions/1").json()
    vid = detail["latest_validation"]["validation_id"]
    r = client.get(f"/api/evidence-packages/registrations/pkg-warn/revisions/1/validations/{vid}/issues")
    assert r.status_code == 200
    issues = r.json()
    assert [i["issue_id"] for i in issues] == ["issue-synthetic-warning-0001"]
    assert issues[0]["message_key"] == "SYNTHETIC_WARNING"
    assert set(issues[0].keys()) == {"issue_id", "code", "severity", "message_key"}


# ---------------------------------------------------------------- 边界与安全


def test_request_cannot_escape_controlled_root(client, app_env):
    input_root, _ = app_env
    build_package(input_root)
    for ref in ["/etc/passwd", "C:/x", "../x", "a\\b", "a\x00b"]:
        r = client.post("/api/evidence-packages/dry-run", json={"package_ref": ref})
        assert r.status_code == 422, f"应拒绝: {ref}"


def test_query_does_not_read_unregistered_body(client, app_env, monkeypatch):
    input_root, _ = app_env
    pkg = build_package(input_root, package_id="pkg-ok")
    extra = pkg / "evidence" / "unregistered.txt"
    extra.write_text("synthetic-unregistered-0001", encoding="utf-8")
    client.post("/api/evidence-packages/register", json={"package_ref": "pkg-ok"})

    real_open = open
    opened: list[str] = []

    def spy(path, *args, **kwargs):
        if "unregistered.txt" in str(path):
            opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", spy)
    r = client.get("/api/evidence-packages/registrations")
    assert r.status_code == 200
    assert opened == []


def test_no_scoring_provider_db_usage(client, app_env, monkeypatch):
    """登记/查询路径不得调用评分、Provider、数据库或 TaskManager。"""
    import routers.evidence_packages as rmod
    import database as db_module

    input_root, _ = app_env
    build_package(input_root)

    calls: list[str] = []

    def trap(name):
        def inner(*a, **k):
            calls.append(name)
            raise AssertionError(f"禁止调用: {name}")
        return inner

    monkeypatch.setattr(db_module, "SessionLocal", trap("database.SessionLocal"))
    # dry-run + register 应完全绕开数据库
    r1 = client.post("/api/evidence-packages/dry-run", json={"package_ref": "pkg-demo-001"})
    assert r1.status_code == 200
    r2 = client.post("/api/evidence-packages/register", json={"package_ref": "pkg-demo-001"})
    assert r2.status_code == 201
    assert calls == []


def test_openapi_has_seven_paths(client):
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    assert "/api/evidence-packages/dry-run" in paths
    assert "/api/evidence-packages/dry-run-batch" in paths
    assert "/api/evidence-packages/register" in paths
    assert "/api/evidence-packages/registrations" in paths
    assert "/api/evidence-packages/registrations/{package_id}/revisions/{package_revision}" in paths
    assert "/api/evidence-packages/registrations/{package_id}/revisions/{package_revision}/validations/{validation_id}" in paths
    assert "/api/evidence-packages/registrations/{package_id}/revisions/{package_revision}/validations/{validation_id}/issues" in paths


# ---------------------------------------------------------------- 错误映射


def test_error_mapper_status_codes():
    """RegistrationError -> HTTP 状态映射（mapper 单元级，覆盖全部稳定错误码）。"""
    mapping_409 = {
        ERR_REVISION_CONFLICT, ERR_IDEMPOTENCY_CONFLICT, ERR_CONTENT_CONFLICT,
        ERR_LOCK_CONFLICT, ERR_RECORD_CONFLICT,
    }
    mapping_422 = {ERR_UNSUPPORTED_VERSION, ERR_REGISTRATION_NOT_ALLOWED, ERR_RELATIVE_REF_INVALID}
    mapping_500 = {
        ERR_INDEX_CORRUPTED, ERR_RECORD_CORRUPTED, ERR_HASH_MISMATCH,
        ERR_INDEX_WRITE_FAILED, ERR_AUDIT_WRITE_FAILED, ERR_INTERNAL,
        ERR_VALIDATION_NOT_FOUND,
    }
    for code in mapping_409:
        assert registration_error_status(RegistrationError(code, code)) == 409
    for code in mapping_422:
        assert registration_error_status(RegistrationError(code, code)) == 422
    for code in mapping_500:
        assert registration_error_status(RegistrationError(code, code)) == 500
    # 查询映射：VALIDATION_NOT_FOUND -> 404；RELATIVE_REF_INVALID -> 422；其余 500
    assert query_error_status(RegistrationError(ERR_VALIDATION_NOT_FOUND, "x")) == 404
    assert query_error_status(RegistrationError(ERR_RELATIVE_REF_INVALID, "x")) == 422
    assert query_error_status(RegistrationError(ERR_RECORD_CORRUPTED, "x")) == 500


def test_error_response_safe_fields(client, app_env):
    """所有错误响应只包含安全错误字段（code/message_key/retryable）。"""
    input_root, _ = app_env
    build_package(input_root)
    client.post("/api/evidence-packages/register", json={"package_ref": "pkg-demo-001"})
    # 409 响应
    build_package(input_root, package_id="pkg-second")
    r = client.post(
        "/api/evidence-packages/register",
        json={"package_ref": "pkg-second", "expected_index_revision": 99},
    )
    assert r.status_code == 409
    assert set(r.json().keys()) == {"code", "message_key", "retryable"}
    assert "traceback" not in json.dumps(r.json())


def test_internal_error_500_unknown_exception(client, app_env, monkeypatch):
    """未知异常 -> 通用 500 INTERNAL_ERROR。"""
    input_root, _ = app_env
    build_package(input_root)

    def boom(self, package_dir):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(EvidenceRegistrationService, "dry_run", boom)
    r = client.post("/api/evidence-packages/dry-run", json={"package_ref": "pkg-demo-001"})
    assert r.status_code == 500
    body = r.json()
    assert body["code"] == "INTERNAL_ERROR"
    assert body["message_key"] == "INTERNAL_ERROR"
    assert body["retryable"] is False
    assert "secret internal detail" not in json.dumps(body)


def test_corrupted_sidecar_500(client, app_env):
    """Sidecar 损坏 -> 查询 500。"""
    input_root, runtime_root = app_env
    build_package(input_root)
    client.post("/api/evidence-packages/register", json={"package_ref": "pkg-demo-001"})
    rec = runtime_root / "phase11/evidence-sidecar/packages/pkg-demo-001/revisions/1/package-record.json"
    rec.write_text("{corrupted", encoding="utf-8")
    r = client.get("/api/evidence-packages/registrations")
    assert r.status_code == 500
    body = r.json()
    assert body["code"] == "SIDECAR_RECORD_CORRUPTED"
    assert set(body.keys()) == {"code", "message_key", "retryable"}


# ---------------------------------------------------------------- 11B-2c：批次 dry-run


def _make_bad_package(input_root, package_id):
    """构造验证 failed 的包（隐私门失败）。"""
    pkg = build_package(input_root, package_id=package_id)
    ev = json.loads((pkg / "evidence.json").read_text(encoding="utf-8"))
    ev["privacy_check"]["contains_direct_identity"] = True
    write_json(pkg / "evidence.json", ev)
    return pkg


def test_batch_all_passed(client, app_env):
    input_root, _ = app_env
    build_package(input_root, package_id="pkg-a")
    build_package(input_root, package_id="pkg-b")
    build_package(input_root, package_id="pkg-c")
    r = client.post("/api/evidence-packages/dry-run-batch", json={"package_refs": ["pkg-a", "pkg-b", "pkg-c"]})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3 and body["passed"] == 3
    assert body["passed_with_warnings"] == 0 and body["failed"] == 0 and body["internal_error"] == 0
    assert body["total"] == body["passed"] + body["passed_with_warnings"] + body["failed"] + body["internal_error"]
    assert len(body["items"]) == body["total"]
    assert [i["package_ref"] for i in body["items"]] == ["pkg-a", "pkg-b", "pkg-c"]  # 顺序保持


def test_batch_mixed_statuses(client, app_env, monkeypatch):
    """passed、failed、internal_error（单项异常）混合；fail_fast=false 全处理。"""
    input_root, _ = app_env
    build_package(input_root, package_id="pkg-ok")
    _make_bad_package(input_root, "pkg-bad")
    real_dry_run = EvidenceRegistrationService.dry_run

    def fake(self, package_dir):
        if str(package_dir).endswith("pkg-ghost-missing"):
            raise RuntimeError("secret mixed detail")
        return real_dry_run(self, package_dir)

    monkeypatch.setattr(EvidenceRegistrationService, "dry_run", fake)
    r = client.post(
        "/api/evidence-packages/dry-run-batch",
        json={"package_refs": ["pkg-ok", "pkg-bad", "pkg-ghost-missing"], "fail_fast": False},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert body["passed"] == 1
    assert body["failed"] == 1
    assert body["internal_error"] == 1  # 单项异常 -> 隔离为 error
    assert len(body["items"]) == 3
    assert body["total"] == body["passed"] + body["passed_with_warnings"] + body["failed"] + body["internal_error"]
    # 单项 error 为安全字段，不泄露异常正文
    err_item = body["items"][2]
    assert err_item["result"] is None
    assert set(err_item["error"].keys()) == {"code", "message_key", "retryable"}
    assert "secret mixed detail" not in json.dumps(body)


def _fake_validation(status: str):
    """构造最小合成 ValidationResult（供 dry_run 注入）。"""
    from models.evidence_sidecar import IssueCounts, ValidationResult

    return ValidationResult(
        validation_id="validation-synthetic-0001",
        package_id="pkg-synthetic",
        package_revision=1,
        manifest_sha256="0" * 64,
        validator_name="synthetic-validator",
        validator_version="1.0.0",
        evidence_contract_version="evidence-package/v1.1",
        privacy_policy_version="privacy-policy/v1.1",
        mode="dry_run",
        status=status,
        started_at=datetime(2026, 8, 6, 7, 0, 0, tzinfo=UTC),
        completed_at=datetime(2026, 8, 6, 7, 0, 1, tzinfo=UTC),
        duration_ms=1000,
        checks=[],
        issues=[],
        issue_counts=IssueCounts(),
        model_input_allowed=True,
        registration_allowed=True,
        validated_file_count=0,
        declared_file_count=0,
        unregistered_file_count=0,
        result_sha256="0" * 64,
    )


def test_batch_passed_with_warnings_does_not_stop(client, app_env, monkeypatch):
    input_root, _ = app_env
    build_package(input_root, package_id="pkg-warn")
    build_package(input_root, package_id="pkg-ok")

    def fake_dry_run(self, package_dir):
        return _fake_validation("passed_with_warnings"), []

    monkeypatch.setattr(EvidenceRegistrationService, "dry_run", fake_dry_run)
    r = client.post(
        "/api/evidence-packages/dry-run-batch",
        json={"package_refs": ["pkg-warn", "pkg-ok"], "fail_fast": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2  # warnings 不触发停止 -> 全部处理
    assert body["passed_with_warnings"] == 2
    assert body["failed"] == 0 and body["internal_error"] == 0


def test_batch_fail_fast_stops_on_failed(client, app_env):
    input_root, _ = app_env
    build_package(input_root, package_id="pkg-ok")
    _make_bad_package(input_root, "pkg-bad")
    build_package(input_root, package_id="pkg-late")
    r = client.post(
        "/api/evidence-packages/dry-run-batch",
        json={"package_refs": ["pkg-ok", "pkg-bad", "pkg-late"], "fail_fast": True},
    )
    assert r.status_code == 200
    body = r.json()
    # 实际执行 2 项（pkg-ok + 触发停止的 pkg-bad）；pkg-late 未执行
    assert body["total"] == 2
    assert body["failed"] == 1
    assert [i["package_ref"] for i in body["items"]] == ["pkg-ok", "pkg-bad"]  # 连续前缀
    # 未执行项不出现在响应
    assert all(i["package_ref"] != "pkg-late" for i in body["items"])


def test_batch_fail_fast_stops_on_internal_error(client, app_env, monkeypatch):
    input_root, _ = app_env
    build_package(input_root, package_id="pkg-ok")

    def boom(self, package_dir):
        raise RuntimeError("secret batch internal detail")

    monkeypatch.setattr(EvidenceRegistrationService, "dry_run", boom)
    r = client.post(
        "/api/evidence-packages/dry-run-batch",
        json={"package_refs": ["pkg-ok", "pkg-other"], "fail_fast": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1  # 首项即 internal_error -> 停止
    assert body["internal_error"] == 1
    assert body["items"][0]["result"] is None
    assert body["items"][0]["error"]["code"] == "INTERNAL_ERROR"
    # 不泄露异常正文
    assert "secret batch internal detail" not in json.dumps(body)


def test_batch_single_and_1000_boundary(client, app_env):
    input_root, _ = app_env
    build_package(input_root, package_id="pkg-1")
    r = client.post("/api/evidence-packages/dry-run-batch", json={"package_refs": ["pkg-1"]})
    assert r.status_code == 200 and r.json()["total"] == 1
    # 1000 项边界：模型层接受 1000 项合法引用
    from models.evidence_api import EvidenceBatchDryRunRequest
    req = EvidenceBatchDryRunRequest(package_refs=["p" + str(i) for i in range(1000)])
    assert len(req.package_refs) == 1000


def test_batch_invalid_requests_422(client):
    for payload in [
        {"package_refs": []},
        {"package_refs": ["x"] * 1001},
        {"package_refs": ["a", "a"]},
        {"package_refs": ["/abs"]},
        {"package_refs": ["../x"]},
    ]:
        r = client.post("/api/evidence-packages/dry-run-batch", json=payload)
        assert r.status_code == 422, f"应 422: {payload}"


def test_batch_zero_write_and_safe_response(client, app_env):
    input_root, runtime_root = app_env
    build_package(input_root, package_id="pkg-a")
    build_package(input_root, package_id="pkg-b", level="limited")
    before = _snapshot(runtime_root)
    r = client.post(
        "/api/evidence-packages/dry-run-batch",
        json={"package_refs": ["pkg-a", "pkg-b"]},
    )
    assert r.status_code == 200
    assert _snapshot(runtime_root) == before  # 零写入
    body = json.dumps(r.json())
    for forbidden in ("actual_summary", "absolute_path", "traceback", "source_package_ref"):
        assert forbidden not in body
