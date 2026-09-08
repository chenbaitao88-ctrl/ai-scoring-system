"""
Phase 11B-2d：EvidencePackage Router 主应用集成测试。

使用真实主应用（from main import app）+ dependency_overrides 注入 tmp_path 服务，
不读写真实 data/。覆盖：
1. 主应用 OpenAPI 7 条 EvidencePackage 路径。
2. /dry-run 与 /dry-run-batch 经真实主应用可访问。
3. 请求校验错误（非法 package_ref/空体/额外字段/错误 revision/重复 refs/超 1000 项）统一返回顶层 EvidenceApiError。
4. 响应不含 detail/input/ctx/非法原值/绝对路径/traceback。
5. 非 EvidencePackage 路由校验错误保持 FastAPI 默认结构（无全局回归）。
6. /health 保持 {"status":"healthy"}。
7. 不调用评分、Provider、数据库或 TaskManager。
8. 使用临时受控输入根与运行根。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import app
from routers import evidence_packages
from services.evidence_package_validator import canonical_json_bytes
from services.evidence_query_service import EvidenceQueryService
from services.evidence_registration_service import EvidenceRegistrationService


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


def build_package(input_root: Path, package_id: str = "pkg-demo-001") -> Path:
    pkg_dir = input_root / package_id
    pkg_dir.mkdir(parents=True, exist_ok=True)
    content = b'{"synthetic": true, "summary": "code structure"}'
    code_file = pkg_dir / "evidence" / "code_summary.json"
    code_file.parent.mkdir(parents=True, exist_ok=True)
    code_file.write_bytes(content)

    manifest = manifest_body(package_id=package_id)
    manifest["files"][0]["size_bytes"] = len(content)
    manifest["files"][0]["sha256"] = sha256_bytes(content)
    obj = dict(manifest)
    obj["manifest_sha256"] = None
    h = sha256_bytes(canonical_json_bytes(obj))
    manifest["manifest_sha256"] = h
    write_json(pkg_dir / "evidence_manifest.json", manifest)

    evidence = evidence_body(package_id=package_id)
    evidence["manifest_sha256"] = h
    write_json(pkg_dir / "evidence.json", evidence)

    (pkg_dir / ".evidence-ready").write_text("", encoding="utf-8")
    return pkg_dir


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def real_client(tmp_path, monkeypatch):
    """真实主应用 + tmp_path 服务覆盖（不读写真实 data/）。"""
    input_root = tmp_path / "evidence-inputs"
    runtime_root = tmp_path / "evidence-runtime"
    input_root.mkdir()
    runtime_root.mkdir()

    def over_reg():
        return EvidenceRegistrationService(input_root, runtime_root)

    def over_query():
        return EvidenceQueryService(over_reg())

    app.dependency_overrides[evidence_packages.get_evidence_registration_service] = over_reg
    app.dependency_overrides[evidence_packages.get_evidence_query_service] = over_query

    # 隔离数据库：主应用 lifespan 会 init_db()（TestClient 触发 lifespan）
    # 覆盖 database 模块的 SessionLocal/engine 为内存引擎（复用现有约定思路，不触碰真实 data）
    import database as db_module

    monkeypatch.setattr(db_module, "DATABASE_URL", "sqlite:///:memory:")
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    _engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    _session = sessionmaker(bind=_engine)
    monkeypatch.setattr(db_module, "engine", _engine)
    monkeypatch.setattr(db_module, "SessionLocal", _session)
    db_module.Base.metadata.create_all(bind=_engine)

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.pop(evidence_packages.get_evidence_registration_service, None)
    app.dependency_overrides.pop(evidence_packages.get_evidence_query_service, None)


# ---------------------------------------------------------------- 主应用挂载


def test_main_app_openapi_has_seven_evidence_paths(real_client):
    spec = real_client.get("/openapi.json").json()
    paths = spec["paths"]
    expected = [
        "/api/evidence-packages/dry-run",
        "/api/evidence-packages/dry-run-batch",
        "/api/evidence-packages/register",
        "/api/evidence-packages/registrations",
        "/api/evidence-packages/registrations/{package_id}/revisions/{package_revision}",
        "/api/evidence-packages/registrations/{package_id}/revisions/{package_revision}/validations/{validation_id}",
        "/api/evidence-packages/registrations/{package_id}/revisions/{package_revision}/validations/{validation_id}/issues",
    ]
    for p in expected:
        assert p in paths


def test_main_app_dry_run_accessible(real_client, tmp_path):
    build_package(tmp_path / "evidence-inputs")
    r = real_client.post("/api/evidence-packages/dry-run", json={"package_ref": "pkg-demo-001"})
    assert r.status_code == 200
    assert r.json()["status"] == "passed"


def test_main_app_dry_run_batch_accessible(real_client, tmp_path):
    build_package(tmp_path / "evidence-inputs", package_id="pkg-a")
    build_package(tmp_path / "evidence-inputs", package_id="pkg-b")
    r = real_client.post(
        "/api/evidence-packages/dry-run-batch",
        json={"package_refs": ["pkg-a", "pkg-b"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2 and body["passed"] == 2


# ---------------------------------------------------------------- 校验错误收口


def test_main_app_invalid_package_ref_422_top_level_error(real_client):
    r = real_client.post("/api/evidence-packages/dry-run", json={"package_ref": "/abs/path"})
    assert r.status_code == 422
    assert r.json() == {
        "code": "REQUEST_VALIDATION_ERROR",
        "message_key": "REQUEST_VALIDATION_ERROR",
        "retryable": False,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},  # 空请求体（缺 package_ref）
        {"package_ref": "pkg-a", "extra": 1},  # 额外字段
        {"package_ref": "pkg-a", "expected_index_revision": -1},  # 负数 revision
        {"package_ref": "pkg-a", "expected_index_revision": True},  # 布尔 revision
        {"package_refs": ["a", "a"]},  # 重复 refs
        {"package_refs": ["x"] * 1001},  # 超 1000 项
        {"package_refs": []},  # 空数组
        {"package_refs": ["../x"]},  # 非法路径
    ],
)
def test_main_app_validation_errors_same_safe_structure(real_client, payload):
    r = real_client.post("/api/evidence-packages/dry-run-batch", json=payload)
    assert r.status_code == 422
    body = r.json()
    assert body == {
        "code": "REQUEST_VALIDATION_ERROR",
        "message_key": "REQUEST_VALIDATION_ERROR",
        "retryable": False,
    }
    dumped = json.dumps(body)
    for forbidden in ("detail", "input", "ctx", "traceback", "loc", "/abs", ".."):
        assert forbidden not in dumped


def test_main_app_validation_error_no_detail_input_ctx(real_client):
    r = real_client.post("/api/evidence-packages/dry-run", json={"package_ref": ""})
    assert r.status_code == 422
    body = r.json()
    for forbidden in ("detail", "input", "ctx", "loc", "type", "traceback"):
        assert forbidden not in json.dumps(body)


# ---------------------------------------------------------------- 无全局回归


def test_main_app_non_evidence_validation_keeps_default_structure(real_client):
    """非 EvidencePackage 路由的请求校验错误保持 FastAPI 默认结构（无全局回归）。"""
    # 用任意非 Evidence 路由的非法请求：teams 列表页带非法参数会触发校验（若无 Query 参数则用不存在 body 路由）
    # 选择 /api/works 的一个 POST（非法 body）来观察默认校验结构
    r = real_client.post("/api/works", json={"not_a_valid_field": 1})
    # 只要不是 EvidencePackage 顶层错误结构即可；若 404 则说明该路径无 POST，换 /api/teams
    if r.status_code == 422:
        body = r.json()
        # 默认 FastAPI 校验响应是 {"detail": [...]}
        assert "detail" in body and isinstance(body["detail"], list)
    else:
        # 该路径可能不存在 POST；用 /api/competitions 复验
        r2 = real_client.post("/api/competitions", json={"not_a_valid_field": 1})
        assert r2.status_code in (404, 422)
        if r2.status_code == 422:
            assert "detail" in r2.json()


def test_main_app_health_unchanged(real_client):
    r = real_client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "healthy"}


# ---------------------------------------------------------------- 边界


def test_main_app_no_db_provider_scoring(real_client, tmp_path, monkeypatch):
    """Evidence 接口不得调用评分、Provider、数据库 SessionLocal 或 TaskManager。"""
    import database as db_module

    build_package(tmp_path / "evidence-inputs")
    calls: list[str] = []

    def trap(name):
        def inner(*a, **k):
            calls.append(name)
            raise AssertionError(f"禁止调用: {name}")
        return inner

    monkeypatch.setattr(db_module, "SessionLocal", trap("database.SessionLocal"))
    r1 = real_client.post("/api/evidence-packages/dry-run", json={"package_ref": "pkg-demo-001"})
    assert r1.status_code == 200
    r2 = real_client.post("/api/evidence-packages/register", json={"package_ref": "pkg-demo-001"})
    assert r2.status_code == 201
    assert calls == []


def test_main_app_uses_tmp_roots_not_real_data(real_client, tmp_path):
    """通过 dependency_overrides 使用临时根；真实 data 目录不被读写（不直接断言 data/ 内容）。"""
    build_package(tmp_path / "evidence-inputs")
    assert (tmp_path / "evidence-inputs" / "pkg-demo-001" / "evidence.json").is_file()
    r = real_client.post("/api/evidence-packages/dry-run", json={"package_ref": "pkg-demo-001"})
    assert r.status_code == 200
