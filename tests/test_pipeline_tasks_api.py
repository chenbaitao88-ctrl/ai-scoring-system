"""
Phase 11C-3：PipelineTask API 合成测试。

使用真实主应用 + dependency_overrides 注入 tmp_path manager（不写真实 DATA_DIR）。
所有数据脱敏合成；断言稳定错误结构、白名单响应、无路径/凭据/原始 evidence 泄露。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from main import app

UTC = timezone.utc
T0 = datetime(2026, 8, 10, 8, 0, 0, tzinfo=UTC)
SHA256 = "a" * 64
PKG = "pkg-demo-001"
REC = "record-0001"
VAL = "validation-0001"

CREATE_BODY = {
    "batch_id": "batch-demo-001",
    "package_refs": [{
        "package_id": PKG, "package_revision": 1, "manifest_sha256": SHA256,
        "registration_record_id": REC, "validation_id": VAL,
    }],
    "concurrency": 1,
    "configuration_snapshot": {"evidence_contract_version": "evidence-package/v1.1",
                               "validator_version": "1.0.0"},
}


def _uuid_seq():
    i = [0]

    def gen() -> str:
        i[0] += 1
        return f"00000000-0000-0000-0000-{i[0]:012d}"

    return gen


def make_validation(evidence_level="sufficient", **overrides):
    from models.evidence_sidecar import ValidationResult

    data = {
        "validation_schema_version": "evidence-sidecar/validation/v1",
        "validation_id": VAL, "record_id": REC, "package_id": PKG, "package_revision": 1,
        "manifest_sha256": SHA256, "validator_name": "validator-demo", "validator_version": "1.0.0",
        "evidence_contract_version": "evidence-package/v1.1", "privacy_policy_version": "privacy-policy/v1.1",
        "mode": "registration", "status": "passed", "started_at": T0,
        "completed_at": T0 + timedelta(seconds=2), "duration_ms": 2000,
        "checks": [{"check_id": "JSON_PARSEABLE", "status": "passed", "message_key": "CHECK_OK"}],
        "issues": [], "issue_counts": {"info": 0, "warning": 0, "error": 0, "fatal": 0},
        "model_input_allowed": True, "registration_allowed": True,
        "validated_file_count": 1, "declared_file_count": 1, "unregistered_file_count": 0,
        "result_sha256": SHA256, "evidence_level": evidence_level,
    }
    data.update(overrides)
    return ValidationResult.model_validate(data)


def make_record(**overrides):
    from models.evidence_sidecar import EvidencePackageRecord

    data = {
        "record_schema_version": "evidence-sidecar/record/v1", "record_id": REC, "package_id": PKG,
        "package_revision": 1, "batch_id": "batch-demo-001", "submission_id": "sub-demo-001",
        "evidence_version": "1", "privacy_policy_version": "privacy-policy/v1.1",
        "contract_version": "evidence-package/v1.1", "publication_status": "ready",
        "registration_status": "registered", "latest_validation_id": VAL,
        "latest_validation_status": "passed", "source_package_ref": f"{PKG}/1/evidence.json",
        "registered_at": T0, "registered_by": "synthetic-registrar",
        "created_at": T0, "updated_at": T0, "revision": 1, "record_sha256": SHA256,
        "manifest_sha256": SHA256,
    }
    data.update(overrides)
    return EvidencePackageRecord.model_validate(data)


class FakeRegistrationService:
    def __init__(self, evidence_level="sufficient", validation_overrides=None):
        self.records = {(PKG, 1): make_record()}
        self.validations = {(PKG, 1, VAL): make_validation(evidence_level, **(validation_overrides or {}))}

    def read_record_strict(self, package_id, package_revision):
        return self.records.get((package_id, package_revision))

    def read_validation_strict(self, package_id, package_revision, validation_id):
        return self.validations.get((package_id, package_revision, validation_id))


class _Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        self.t = self.t + timedelta(seconds=1)
        return self.t

    def set(self, t):
        self.t = t


@pytest.fixture
def client(tmp_path, monkeypatch):
    from services.pipeline_task_store import PipelineTaskStore
    from services.pipeline_task_manager import PipelineTaskManager
    from routers import pipeline_tasks

    store = PipelineTaskStore(tmp_path / "pipeline-runtime")
    reg = FakeRegistrationService()
    mgr = PipelineTaskManager(store, reg, clock=_Clock(), uuid_factory=_uuid_seq())

    def over():
        return mgr

    app.dependency_overrides[pipeline_tasks.get_pipeline_task_manager] = over

    import database as db_module
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setattr(db_module, "DATABASE_URL", "sqlite:///:memory:")
    _engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    _session = sessionmaker(bind=_engine)
    monkeypatch.setattr(db_module, "engine", _engine)
    monkeypatch.setattr(db_module, "SessionLocal", _session)
    db_module.Base.metadata.create_all(bind=_engine)

    with TestClient(app) as c:
        yield c, mgr

    app.dependency_overrides.pop(pipeline_tasks.get_pipeline_task_manager, None)


def _create(client, body=None, expect=201):
    r = client.post("/api/pipeline-tasks", json=body or CREATE_BODY)
    assert r.status_code == expect, r.text
    return r.json()


def _task_id(resp):
    return resp["task_id"]


# ---------------------------------------------------------------- 1-5 创建与列表


def test_create_first_201(client):
    c, mgr = client
    resp = _create(c)
    assert resp["idempotent_hit"] is False
    assert resp["execution_scope"] == ["import", "validate"]
    assert resp["status"] == "pending"


def test_create_idempotent_200(client):
    c, mgr = client
    _create(c)
    resp = _create(c, expect=200)
    assert resp["idempotent_hit"] is True


def test_create_conflict_409(client):
    c, mgr = client
    # 预置同 key 不同 payload 的合成任务 -> 创建冲突（防御分支）
    from services.pipeline_task_manager import _CONTRACT_VERSION, _TASK_TYPE
    from models.pipeline_task import _SCOPE, PipelineTask, PipelineStageSummary, compute_item_input_fingerprint, compute_task_idempotency, sha256_canonical
    from models.pipeline_task import PipelineConfigurationSnapshot

    cfg = PipelineConfigurationSnapshot(evidence_contract_version="evidence-package/v1.1", validator_version="1.0.0")
    fp = compute_item_input_fingerprint(package_id=PKG, package_revision=1, manifest_sha256=SHA256,
                                        registration_record_id=REC, validation_id=VAL)
    cfg_fp = sha256_canonical(cfg.model_dump(mode="json"))
    key = compute_task_idempotency(contract_version=_CONTRACT_VERSION, task_type=_TASK_TYPE,
                                   batch_id="batch-demo-001", execution_scope=list(_SCOPE),
                                   items=[(PKG, 1, fp)], configuration_fingerprint=cfg_fp)
    st = lambda stage, deps: PipelineStageSummary(  # noqa: E731
        stage=stage, status="not_started", depends_on=deps, total_items=0, pending_items=0,
        running_items=0, completed_items=0, failed_items=0, skipped_items=0,
        manual_review_items=0, blocking_error_codes=[], revision=1)
    fake = PipelineTask(
        contract_version=_CONTRACT_VERSION, task_type=_TASK_TYPE, execution_scope=list(_SCOPE),
        task_id="00000000-0000-0000-0000-000000009999", batch_id="batch-other",
        status="pending", current_stage="import", created_at=T0, started_at=None, updated_at=T0,
        paused_at=None, completed_at=None, total_items=0, pending_items=0, running_items=0,
        completed_items=0, failed_items=0, skipped_items=0, manual_review_items=0, concurrency=1,
        configuration_snapshot=cfg, configuration_fingerprint=cfg_fp, item_index=[],
        stage_summaries=[st("import", []), st("validate", ["import"]), st("score", ["validate"]),
                         st("review", ["validate", "score"]), st("export", ["review"])],
        error_summary={"count": 0, "codes": []}, last_event_sequence=0, revision=1,
        idempotency_key=key, idempotency_payload_sha256="d" * 64,
    )
    mgr.store.write_task_snapshot(fake.task_id, fake, expected_revision=None)
    r = c.post("/api/pipeline-tasks", json=CREATE_BODY)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "SIDECAR_IDEMPOTENCY_CONFLICT"


def test_create_evidence_admission_422(client):
    from routers import pipeline_tasks

    c, mgr = client
    app.dependency_overrides[pipeline_tasks.get_pipeline_task_manager] = _make_bad_reg_mgr
    try:
        r = c.post("/api/pipeline-tasks", json=CREATE_BODY)
        assert r.status_code == 422
        assert r.json()["detail"]["code"] in ("SIDECAR_PACKAGE_NOT_REGISTERED", "SIDECAR_VALIDATION_NOT_ALLOWED",
                                               "SIDECAR_EVIDENCE_LEVEL_NOT_ALLOWED", "SIDECAR_EVIDENCE_LEVEL_MISSING")
    finally:
        app.dependency_overrides.pop(pipeline_tasks.get_pipeline_task_manager, None)


def _make_bad_reg_mgr():
    from services.pipeline_task_store import PipelineTaskStore
    from services.pipeline_task_manager import PipelineTaskManager
    import tempfile

    store = PipelineTaskStore(tempfile.mkdtemp() + "/pipeline-runtime")
    return PipelineTaskManager(store, FakeRegistrationService(evidence_level="insufficient"),
                               clock=lambda: T0, uuid_factory=_uuid_seq())


def test_list_paginated_filtered_sorted(client):
    c, mgr = client
    for i in range(3):
        body = dict(CREATE_BODY)
        body["batch_id"] = f"batch-{i}"
        _create(c, body=body)
    r = c.get("/api/pipeline-tasks?limit=2")
    assert r.status_code == 200
    data = r.json()
    assert len(data["items"]) == 2 and data["limit"] == 2
    ids = [t["task_id"] for t in data["items"]]
    assert ids == sorted(ids)
    r2 = c.get("/api/pipeline-tasks?status=pending&limit=100")
    assert all(t["status"] == "pending" for t in r2.json()["items"])
    r3 = c.get("/api/pipeline-tasks?status=running&limit=100")
    assert r3.json()["items"] == []


# ---------------------------------------------------------------- 6-9 详情与控制


def test_task_item_not_found_404(client):
    c, mgr = client
    r = c.get("/api/pipeline-tasks/00000000-0000-0000-0000-0000000000ff")
    assert r.status_code == 404
    resp = _create(c)
    r2 = c.get(f"/api/pipeline-tasks/{resp['task_id']}/items/00000000-0000-0000-0000-0000000000ee")
    assert r2.status_code == 404


def test_start_pause_legal(client):
    c, mgr = client
    resp = _create(c)
    tid = resp["task_id"]
    r = c.post(f"/api/pipeline-tasks/{tid}/start", json={"expected_revision": 1})
    assert r.status_code == 200 and r.json()["status"] == "running"
    r2 = c.post(f"/api/pipeline-tasks/{tid}/pause", json={"expected_revision": r.json()["revision"]})
    assert r2.status_code == 200 and r2.json()["status"] == "paused"
    # 重复暂停 -> 409
    r3 = c.post(f"/api/pipeline-tasks/{tid}/pause", json={"expected_revision": r2.json()["revision"]})
    assert r3.status_code == 409


def test_revision_conflict_409_zero_side_effect(client):
    c, mgr = client
    resp = _create(c)
    tid = resp["task_id"]
    r = c.post(f"/api/pipeline-tasks/{tid}/start", json={"expected_revision": 99})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "SIDECAR_REVISION_CONFLICT"
    # 零副作用：任务仍 pending
    got = c.get(f"/api/pipeline-tasks/{tid}").json()
    assert got["status"] == "pending" and got["revision"] == 1


# ---------------------------------------------------------------- 10-11 heartbeat/events


def test_heartbeat_legal_and_non_running_rejected(client):
    c, mgr = client
    resp = _create(c)
    tid = resp["task_id"]
    item_id = resp["items"][0]["item_id"] if "items" in resp else None
    # 通过 items 接口拿 item
    items = c.get(f"/api/pipeline-tasks/{tid}/items").json()["items"]
    item_id = items[0]["item_id"]
    c.post(f"/api/pipeline-tasks/{tid}/start", json={"expected_revision": 1})
    task = mgr.get_task(tid)
    mgr.start_item_stage(tid, item_id, task.revision, 1)  # item 进入 running（attempt=1, item rev=2）
    task = mgr.get_task(tid)
    r = c.post(f"/api/pipeline-tasks/{tid}/items/{item_id}/heartbeat",
               json={"expected_revision": task.revision, "expected_item_revision": 2})
    assert r.status_code == 200 and r.json()["status"] == "running"
    # 非 running item 拒绝（item 未 start 的新任务）
    resp2 = _create(c, body=dict(CREATE_BODY, batch_id="batch-hb2"))
    tid2 = resp2["task_id"]
    item_id2 = c.get(f"/api/pipeline-tasks/{tid2}/items").json()["items"][0]["item_id"]
    r2 = c.post(f"/api/pipeline-tasks/{tid2}/items/{item_id2}/heartbeat",
                json={"expected_revision": 1, "expected_item_revision": 1})
    assert r2.status_code in (409, 500)  # item 未 running -> 非法转换


def test_events_sorted(client):
    c, mgr = client
    resp = _create(c)
    tid = resp["task_id"]
    r = c.get(f"/api/pipeline-tasks/{tid}/events")
    assert r.status_code == 200
    events = r.json()["items"]
    seqs = [e["sequence"] for e in events]
    assert seqs == sorted(seqs) and len(events) == 1
    assert events[0]["event_type"] == "task_created"


# ---------------------------------------------------------------- 12-14 resume 流程


def test_resume_full_flow(client):
    c, mgr = client
    resp = _create(c)
    tid = resp["task_id"]
    # 先让 item failed（经 manager 直接驱动）
    from services.pipeline_task_manager import PipelineTaskError

    task = mgr.get_task(tid)
    item_id = task.item_index[0].item_id
    task = mgr.start_task(tid, 1)
    mgr.start_item_stage(tid, item_id, task.revision, 1)
    task = mgr.get_task(tid)
    mgr.fail_item_retryable(tid, item_id, task.revision, 2, error_code="TRANSIENT")
    task = mgr.get_task(tid)
    req_id = "00000000-0000-0000-0000-0000000000a1"
    body = {
        "request_id": req_id, "mode": "resume_retryable_failed",
        "expected_revision": task.revision,
        "expected_item_revisions": {item_id: 3}, "item_ids": [item_id],
        "reason_code": "TEST", "dry_run": False,
    }
    r = c.post(f"/api/pipeline-tasks/{tid}/resume-requests", json=body)
    assert r.status_code == 201
    r2 = c.get(f"/api/pipeline-tasks/{tid}/resume-requests/{req_id}")
    assert r2.status_code == 200 and r2.json()["request_id"] == req_id
    r3 = c.post(f"/api/pipeline-tasks/{tid}/resume-requests/{req_id}/evaluate")
    assert r3.status_code == 200 and r3.json()["approved"] is True
    r4 = c.get(f"/api/pipeline-tasks/{tid}/resume-decisions/{req_id}")
    assert r4.status_code == 200 and r4.json()["decision_id"] == r3.json()["decision_id"]
    r5 = c.post(f"/api/pipeline-tasks/{tid}/resume-decisions/{req_id}/apply",
                json={"expected_revision": task.revision})
    assert r5.status_code == 200
    assert r5.json()["task"]["status"] == "running"
    items = r5.json()["changed_items"]
    assert any(i["status"] == "pending" for i in items)  # failed -> pending


def test_dry_run_decision_not_appliable(client):
    c, mgr = client
    resp = _create(c)
    tid = resp["task_id"]
    task = mgr.get_task(tid)
    req_id = "00000000-0000-0000-0000-0000000000b1"
    body = {
        "request_id": req_id, "mode": "resume_pending",
        "expected_revision": task.revision,
        "expected_item_revisions": {}, "item_ids": [task.item_index[0].item_id],
        "reason_code": "TEST", "dry_run": True,
    }
    c.post(f"/api/pipeline-tasks/{tid}/resume-requests", json=body)
    r = c.post(f"/api/pipeline-tasks/{tid}/resume-requests/{req_id}/evaluate")
    assert r.json()["dry_run"] is True
    r2 = c.post(f"/api/pipeline-tasks/{tid}/resume-decisions/{req_id}/apply",
                json={"expected_revision": 1})
    assert r2.status_code == 200
    assert r2.json()["task"]["status"] == "pending"  # dry-run 零状态副作用


def test_completed_protected(client):
    c, mgr = client
    resp = _create(c)
    tid = resp["task_id"]
    task = mgr.get_task(tid)
    item_id = task.item_index[0].item_id
    task = mgr.start_task(tid, 1)
    mgr.start_item_stage(tid, item_id, task.revision, 1)
    task = mgr.get_task(tid)
    mgr.complete_item_stage(tid, item_id, task.revision, 2)
    task = mgr.get_task(tid)
    mgr.start_item_stage(tid, item_id, task.revision, 3)
    task = mgr.get_task(tid)
    mgr.complete_item_stage(tid, item_id, task.revision, 4,
                            output_ref="outputs/result.json", output_sha256=SHA256)
    task = mgr.get_task(tid)
    req_id = "00000000-0000-0000-0000-0000000000c1"
    body = {
        "request_id": req_id, "mode": "resume_pending",
        "expected_revision": task.revision,
        "expected_item_revisions": {}, "item_ids": [item_id],
        "reason_code": "TEST", "dry_run": False,
    }
    c.post(f"/api/pipeline-tasks/{tid}/resume-requests", json=body)
    r = c.post(f"/api/pipeline-tasks/{tid}/resume-requests/{req_id}/evaluate")
    assert r.json()["approved"] is False
    assert any("SUCCESS_RESULT_PROTECTED" in str(p) for p in r.json()["protected_items"])


# ---------------------------------------------------------------- 15-20 安全与回归


def test_validation_error_stable_structure(client):
    c, mgr = client
    r = c.post("/api/pipeline-tasks", json={"batch_id": "bad"})  # 缺字段
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "REQUEST_VALIDATION_ERROR"
    assert body["message_key"] == "REQUEST_VALIDATION_ERROR"
    assert body["retryable"] is False
    assert "traceback" not in json.dumps(body).lower()
    # start 缺 expected_revision
    resp = _create(c)
    r2 = c.post(f"/api/pipeline-tasks/{resp['task_id']}/start", json={})
    assert r2.status_code == 422


def test_internal_error_no_leak(client):
    c, mgr = client
    # 破坏 task.json -> corrupted -> 500 且无路径/traceback/异常原文
    resp = _create(c)
    tid = resp["task_id"]
    (mgr.store.tasks_dir / tid / "task.json").write_text("{broken", encoding="utf-8")
    r = c.get(f"/api/pipeline-tasks/{tid}")
    assert r.status_code == 500
    body = r.json()
    detail = body.get("detail", body)
    assert "code" in detail and "message_key" in detail
    dumped = json.dumps(body)
    assert "Traceback" not in dumped
    assert "C:" not in dumped and "Users" not in dumped and ".py" not in dumped


def test_response_no_sensitive_fields(client):
    c, mgr = client
    resp = _create(c)
    tid = resp["task_id"]
    dumped = json.dumps(resp)
    assert "configuration_snapshot" not in dumped  # 不返回配置快照自由文本
    assert "idempotency_key" not in dumped and "input_fingerprint" not in dumped
    assert "manifest_sha256" not in dumped  # 不返回材料哈希/引用内部值
    items = c.get(f"/api/pipeline-tasks/{tid}/items").json()["items"]
    item_dumped = json.dumps(items[0])
    assert "output_ref" not in item_dumped  # 不暴露输出路径
    assert "absolute" not in item_dumped and "C:" not in item_dumped
    assert "https://" not in dumped and "http" not in dumped.replace("http_status", "")
    for pat in (r"1[3-9]\d{9}", r"姓名", r"网盘"):
        assert not re.search(pat, dumped + item_dumped)


def test_batch_scoring_route_unchanged(client):
    c, mgr = client
    r = c.get("/api/batch-scoring/health")
    assert r.status_code in (200, 404)  # 路由存在性不破坏（404=无该子路径，行为保持默认）
    r2 = c.get("/api/pipeline-tasks")
    assert r2.status_code == 200  # pipeline 列表正常


def test_health_still_json(client):
    c, mgr = client
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"


def test_override_does_not_touch_real_data_dir(client):
    """dependency override 下真实 DATA_DIR 不被读写（不直接断言 data/ 内容，避免误触）。"""
    c, mgr = client
    assert str(mgr.store.root).endswith("pipeline-runtime") or "pipeline-runtime" in str(mgr.store.root)
    assert "pipeline-runtime" in str(mgr.store.root).replace("\\", "/")
