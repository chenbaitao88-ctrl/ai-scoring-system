"""
Phase 11D-3b：ProviderSwitchService 合成测试。

权威查询全部注入式 fake；不调用 Gateway/Transport、不写数据库、不改 PipelineTask、
不读取环境变量值。全部合成脱敏数据。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from models.provider_switch import ProviderSwitchTargetProposal
from models.scoring_provider import (
    ProviderError,
    ProviderRequest,
)
from services.provider_switch_service import ProviderSwitchService
from services.provider_switch_store import (
    ERR_BINDING_MISMATCH,
    ERR_IDEMPOTENCY_CONFLICT,
    ERR_INVALID_STATE,
    ERR_NOT_FOUND,
    ERR_REVISION_CONFLICT,
    ERR_SAME_PROVIDER,
    ERR_SCORING_IDENTITY_MISMATCH,
    ERR_SOURCE_ERROR_NOT_ALLOWED,
    ERR_SOURCE_ERROR_NOT_FOUND,
    ERR_SOURCE_REQUEST_NOT_FOUND,
    ERR_SUCCESS_EXISTS,
    ERR_TARGET_CAPABILITY_MISMATCH,
    ERR_TARGET_DISABLED,
    ERR_TARGET_NOT_FOUND,
    ProviderSwitchStore,
    SwitchStoreError,
)
from services.scoring_provider_registry import ScoringProviderRegistry

T0 = datetime(2026, 8, 10, 6, 0, 0, tzinfo=timezone.utc)
TASK = "pipeline_task_switch_001"
ITEM = "item_switch_001"
SRC_ATTEMPT = "attempt_source_001"
SRC_PROVIDER = "provider_source_001"
SRC_MODEL = "model_source_v1"
TGT_PROVIDER = "provider_target_001"
TGT_MODEL = "model_target_v1"
EVIDENCE = "a" * 64
FP = "b" * 64
CONTRACT = "scoring-provider/v1"
ERROR_ID = "provider_error_001"


# ---------------- source 对象 ---------------- #


def make_source_request(**overrides):
    base = dict(
        contract_version=CONTRACT,
        request_id="provider_request_source_001",
        task_id=TASK, item_id=ITEM, attempt_id=SRC_ATTEMPT,
        provider_id=SRC_PROVIDER, model_id=SRC_MODEL,
        capability_version="cap-source-001", config_version="cfg-source-001",
        evidence_package_id="evidence_demo_001",
        evidence_manifest_sha256=EVIDENCE,
        input_fingerprint=FP,
        scoring_policy_version="policy-demo-001",
        scoring_mode="mixed",
        prompt_version="prompt-demo-001",
        response_schema_version="score-response-v1",
        timeout_seconds=120,
        requested_at=T0,
        input_modalities=["text"],
        evidence_refs=["evidence_demo_001"],
        request_payload_sha256="c" * 64,
        selection_id="selection_001",
        temperature=0.3,
    )
    base.update(overrides)
    return ProviderRequest(**base)


def make_source_error(**overrides):
    base = dict(
        contract_version=CONTRACT,
        error_id=ERROR_ID,
        request_id="provider_request_source_001",
        task_id=TASK, item_id=ITEM, attempt_id=SRC_ATTEMPT,
        provider_id=SRC_PROVIDER, model_id=SRC_MODEL,
        error_code="PROVIDER_RATE_LIMITED",
        error_category="rate_limit",
        message_safe="限流，未记录正文",
        retryable="conditional",
        provider_switch_allowed="approval_required",
        manual_review_required="after_exhaustion",
        consumes_provider_call_attempt=True,
        stop_entire_batch="no",
        retry_after_seconds=30,
        occurred_at=T0,
        details_sha256=None,
    )
    base.update(overrides)
    return ProviderError(**base)


def make_proposal(**overrides):
    base = dict(
        target_provider_id=TGT_PROVIDER,
        target_model_id=TGT_MODEL,
        scoring_policy_version="policy-demo-001",
        prompt_version="prompt-demo-001",
        evidence_package_id="evidence_demo_001",
        evidence_manifest_sha256=EVIDENCE,
        input_fingerprint=FP,
        response_schema_version="score-response-v1",
        scoring_mode="mixed",
        temperature=0.3,
    )
    base.update(overrides)
    return ProviderSwitchTargetProposal(**base)


# ---------------- registry fixture（source + target） ---------------- #


def registry_data():
    def cap(pid, mid, cver):
        return dict(
            contract_version=CONTRACT, capability_version=cver, provider_id=pid, model_id=mid,
            text_input=True, image_input=False, structured_json_output=True,
            system_message=True, temperature_supported=True, seed_supported=True,
            verification_source="static_config",
        )

    def prov(pid, mid, cver, enabled=True, dep="active"):
        return dict(
            contract_version=CONTRACT, provider_id=pid, provider_type="mock",
            display_name="D", endpoint_profile=f"profile_{pid}", model_id=mid,
            capability_version=cver, config_version="cfg-1", enabled=enabled,
            credential_ref="TEST_PROVIDER_API_KEY",
            capability_ref=f"capability/{pid}/{mid}/{cver}",
            created_at=T0.isoformat(), updated_at=T0.isoformat(), deprecation_status=dep,
        )

    return {
        "registry_version": "provider-registry/v1",
        "updated_at": T0.isoformat(),
        "revision": 1,
        "providers": [
            prov(SRC_PROVIDER, SRC_MODEL, "cap-source-001"),
            prov(TGT_PROVIDER, TGT_MODEL, "cap-target-001"),
        ],
        "capabilities": [
            cap(SRC_PROVIDER, SRC_MODEL, "cap-source-001"),
            cap(TGT_PROVIDER, TGT_MODEL, "cap-target-001"),
        ],
    }


@pytest.fixture
def registry(tmp_path):
    p = tmp_path / "registry.json"
    p.write_text(json.dumps(registry_data(), ensure_ascii=False), encoding="utf-8")
    r = ScoringProviderRegistry(p)
    r.load()
    return r


class FakeAttemptLookup:
    def __init__(self, request=None, error=None):
        self.request = request or make_source_request()
        self.error = error or make_source_error()

    def get_source_request(self, task_id, item_id, attempt_id):
        return self.request

    def get_source_error(self, error_id):
        return self.error


class FakeSuccessLookup:
    def __init__(self, has=False):
        self.has = has
        self.calls = 0

    def has_successful_result(self, task_id, item_id, attempt_id):
        self.calls += 1
        return self.has


def make_service(registry, store_root, success_has=False, attempt=None, error=None):
    store = ProviderSwitchStore(store_root)
    return ProviderSwitchService(
        store=store,
        registry=registry,
        attempt_lookup=FakeAttemptLookup(request=attempt, error=error),
        success_lookup=FakeSuccessLookup(has=success_has),
        clock=lambda: T0,
        uuid_factory=_uuid_seq(),
    ), store


def _uuid_seq():
    n = [0]

    def _next():
        n[0] += 1
        return f"switch-uuid-{n[0]:04d}"

    return _next


@pytest.fixture
def svc(tmp_path, registry):
    s, _st = make_service(registry, tmp_path / "switches")
    return s


# ---------------- 前置拒绝 ---------------- #


def test_source_request_missing(tmp_path, registry):
    s, _ = make_service(registry, tmp_path / "switches", attempt=None)
    s._attempt_lookup.request = None
    with pytest.raises(SwitchStoreError) as ei:
        s.create_switch_request(TASK, ITEM, make_proposal(), SRC_ATTEMPT, ERROR_ID)
    assert ei.value.error_code == ERR_SOURCE_REQUEST_NOT_FOUND


def test_source_error_missing(tmp_path, registry):
    s, _ = make_service(registry, tmp_path / "switches", error=None)
    s._attempt_lookup.error = None
    with pytest.raises(SwitchStoreError) as ei:
        s.create_switch_request(TASK, ITEM, make_proposal(), SRC_ATTEMPT, ERROR_ID)
    assert ei.value.error_code == ERR_SOURCE_ERROR_NOT_FOUND


def test_binding_mismatch(tmp_path, registry):
    err = make_source_error(task_id="task_other")
    s, _ = make_service(registry, tmp_path / "switches", error=err)
    with pytest.raises(SwitchStoreError) as ei:
        s.create_switch_request(TASK, ITEM, make_proposal(), SRC_ATTEMPT, ERROR_ID)
    assert ei.value.error_code == ERR_BINDING_MISMATCH


def test_source_error_not_allowed(tmp_path, registry):
    err = make_source_error(provider_switch_allowed="no")
    s, _ = make_service(registry, tmp_path / "switches", error=err)
    with pytest.raises(SwitchStoreError) as ei:
        s.create_switch_request(TASK, ITEM, make_proposal(), SRC_ATTEMPT, ERROR_ID)
    assert ei.value.error_code == ERR_SOURCE_ERROR_NOT_ALLOWED


def test_success_result_exists(tmp_path, registry):
    s, _ = make_service(registry, tmp_path / "switches", success_has=True)
    with pytest.raises(SwitchStoreError) as ei:
        s.create_switch_request(TASK, ITEM, make_proposal(), SRC_ATTEMPT, ERROR_ID)
    assert ei.value.error_code == ERR_SUCCESS_EXISTS


def test_target_not_found(tmp_path, registry):
    s, _ = make_service(registry, tmp_path / "switches")
    with pytest.raises(SwitchStoreError) as ei:
        s.create_switch_request(TASK, ITEM, make_proposal(target_provider_id="provider_ghost"), SRC_ATTEMPT, ERROR_ID)
    assert ei.value.error_code == ERR_TARGET_NOT_FOUND


def test_target_disabled_retired(tmp_path, registry):
    # disabled target
    p = tmp_path / "registry_disabled.json"
    data = registry_data()
    data["providers"][1]["enabled"] = False
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    r = ScoringProviderRegistry(p)
    r.load()
    s, _ = make_service(r, tmp_path / "switches2")
    with pytest.raises(SwitchStoreError) as ei:
        s.create_switch_request(TASK, ITEM, make_proposal(), SRC_ATTEMPT, ERROR_ID)
    assert ei.value.error_code == ERR_TARGET_DISABLED
    # retired target
    p2 = tmp_path / "registry_retired.json"
    data2 = registry_data()
    data2["providers"][1]["deprecation_status"] = "retired"
    p2.write_text(json.dumps(data2, ensure_ascii=False), encoding="utf-8")
    r2 = ScoringProviderRegistry(p2)
    r2.load()
    s2, _ = make_service(r2, tmp_path / "switches3")
    with pytest.raises(SwitchStoreError) as ei:
        s2.create_switch_request(TASK, ITEM, make_proposal(), SRC_ATTEMPT, ERROR_ID)
    assert ei.value.error_code == ERR_TARGET_DISABLED


def test_target_capability_mismatch(tmp_path, registry):
    p = tmp_path / "registry_cap.json"
    data = registry_data()
    data["capabilities"][1]["image_input"] = False
    data["providers"][0]["capability_version"] = "cap-source-001"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    r = ScoringProviderRegistry(p)
    r.load()
    s, _ = make_service(r, tmp_path / "switches")
    req = make_source_request(input_modalities=["text", "image"])
    s._attempt_lookup.request = req
    with pytest.raises(SwitchStoreError) as ei:
        s.create_switch_request(TASK, ITEM, make_proposal(), SRC_ATTEMPT, ERROR_ID)
    assert ei.value.error_code == ERR_TARGET_CAPABILITY_MISMATCH


def test_same_provider_model_rejected(tmp_path, registry):
    s, _ = make_service(registry, tmp_path / "switches")
    with pytest.raises(SwitchStoreError) as ei:
        s.create_switch_request(
            TASK, ITEM,
            make_proposal(target_provider_id=SRC_PROVIDER, target_model_id=SRC_MODEL),
            SRC_ATTEMPT, ERROR_ID,
        )
    assert ei.value.error_code == ERR_SAME_PROVIDER


def test_scoring_identity_any_change_rejected(tmp_path, registry):
    s, _ = make_service(registry, tmp_path / "switches")
    cases = [
        dict(scoring_policy_version="policy-other"),
        dict(prompt_version="prompt-other"),
        dict(evidence_manifest_sha256="f" * 64),
        dict(input_fingerprint="9" * 64),
        dict(response_schema_version="schema-other"),
        dict(scoring_mode="ai_only"),
        dict(temperature=0.5),
        dict(seed=42),
        dict(max_output_tokens=100),
    ]
    for overrides in cases:
        with pytest.raises(SwitchStoreError) as ei:
            s.create_switch_request(TASK, ITEM, make_proposal(**overrides), SRC_ATTEMPT, ERROR_ID)
        assert ei.value.error_code == ERR_SCORING_IDENTITY_MISMATCH, overrides


def test_proposal_rejects_same_fields_and_sensitive(tmp_path, registry):
    """调用方无法传 same_*（proposal extra=forbid）与敏感字段。"""
    with pytest.raises(Exception):
        make_proposal(same_scoring_policy=True)
    with pytest.raises(Exception):
        make_proposal(api_key="sk-x")
    with pytest.raises(Exception):
        make_proposal(target_attempt_id="attempt_x")


# ---------------- 创建与批准流 ---------------- #


def test_pending_created(svc):
    decision, created = svc.create_switch_request(TASK, ITEM, make_proposal(), SRC_ATTEMPT, ERROR_ID)
    assert created is True
    assert decision.decision_status == "pending_approval"
    assert decision.target_attempt_id is None
    assert decision.approval_required is True
    assert decision.successful_result_exists is False
    assert decision.same_scoring_mode is True
    assert decision.same_temperature is True
    # store 已持久化
    got = svc.get_switch_decision(TASK, ITEM, decision.switch_decision_id)
    assert got is not None and got.decision_status == "pending_approval"


def test_create_idempotent(svc):
    d1, c1 = svc.create_switch_request(TASK, ITEM, make_proposal(), SRC_ATTEMPT, ERROR_ID)
    d2, c2 = svc.create_switch_request(TASK, ITEM, make_proposal(), SRC_ATTEMPT, ERROR_ID)
    assert c1 is True and c2 is False
    assert d2.switch_decision_id == d1.switch_decision_id
    # 幂等不新增 revision/event
    records = svc.list_switch_decisions(TASK)
    assert len(records) == 1


def test_approve_generates_identity_and_target_attempt(svc):
    d, _ = svc.create_switch_request(TASK, ITEM, make_proposal(), SRC_ATTEMPT, ERROR_ID)
    did = d.switch_decision_id
    approved = svc.approve_switch(TASK, ITEM, did, "controller", expected_revision=1)
    assert approved.decision_status == "approved"
    assert approved.approved_by == "controller"
    assert approved.approved_at is not None
    assert approved.target_attempt_id is not None
    assert approved.target_attempt_id != approved.source_attempt_id
    # revision/event 正确
    rec = svc._store.read(TASK, ITEM, did)
    assert rec.revision == 2
    assert rec.event_sequence == 2


def test_approve_rechecks_success(tmp_path, registry):
    s, _ = make_service(registry, tmp_path / "switches")
    d, _ = s.create_switch_request(TASK, ITEM, make_proposal(), SRC_ATTEMPT, ERROR_ID)
    # approve 前发现成功结果 -> 拒绝
    s._success_lookup.has = True
    with pytest.raises(SwitchStoreError) as ei:
        s.approve_switch(TASK, ITEM, d.switch_decision_id, "controller", expected_revision=1)
    assert ei.value.error_code == ERR_SUCCESS_EXISTS


def test_reject_and_cancel(tmp_path, registry):
    # reject 场景（独立目录避免幂等命中）
    s1, _ = make_service(registry, tmp_path / "s1")
    d, _ = s1.create_switch_request(TASK, ITEM, make_proposal(), SRC_ATTEMPT, ERROR_ID)
    did = d.switch_decision_id
    rej = s1.reject_switch(TASK, ITEM, did, "REJECTED_BY_CONTROLLER", expected_revision=1)
    assert rej.decision_status == "rejected"
    assert rej.target_attempt_id is None
    # pending 新决定 -> cancel
    s2, _ = make_service(registry, tmp_path / "s2")
    d2, _ = s2.create_switch_request(TASK, ITEM, make_proposal(), SRC_ATTEMPT, ERROR_ID)
    cancelled = s2.cancel_switch(TASK, ITEM, d2.switch_decision_id, expected_revision=1)
    assert cancelled.decision_status == "cancelled"
    # approved 后取消保留 target attempt
    s3, _ = make_service(registry, tmp_path / "s3")
    d3, _ = s3.create_switch_request(TASK, ITEM, make_proposal(), SRC_ATTEMPT, ERROR_ID)
    app = s3.approve_switch(TASK, ITEM, d3.switch_decision_id, "controller", expected_revision=1)
    c3 = s3.cancel_switch(TASK, ITEM, d3.switch_decision_id, expected_revision=2)
    assert c3.decision_status == "cancelled"
    assert c3.target_attempt_id == app.target_attempt_id


def test_approved_to_executed(svc):
    d, _ = svc.create_switch_request(TASK, ITEM, make_proposal(), SRC_ATTEMPT, ERROR_ID)
    did = d.switch_decision_id
    svc.approve_switch(TASK, ITEM, did, "controller", expected_revision=1)
    ex = svc.mark_executed(TASK, ITEM, did, expected_revision=2)
    assert ex.decision_status == "executed"
    assert ex.executed_at is not None
    assert ex.target_attempt_id is not None
    rec = svc._store.read(TASK, ITEM, did)
    assert rec.revision == 3 and rec.event_sequence == 3


def test_invalid_transitions(tmp_path, registry):
    s1, _ = make_service(registry, tmp_path / "t1")
    d, _ = s1.create_switch_request(TASK, ITEM, make_proposal(), SRC_ATTEMPT, ERROR_ID)
    did = d.switch_decision_id
    s1.reject_switch(TASK, ITEM, did, "REJECTED_BY_CONTROLLER", expected_revision=1)
    # rejected -> approved 非法
    with pytest.raises(SwitchStoreError) as ei:
        s1.approve_switch(TASK, ITEM, did, "controller", expected_revision=2)
    assert ei.value.error_code == ERR_INVALID_STATE
    # pending -> executed 非法
    s2, _ = make_service(registry, tmp_path / "t2")
    d2, _ = s2.create_switch_request(TASK, ITEM, make_proposal(), SRC_ATTEMPT, ERROR_ID)
    with pytest.raises(SwitchStoreError) as ei:
        s2.mark_executed(TASK, ITEM, d2.switch_decision_id, expected_revision=1)
    assert ei.value.error_code == ERR_INVALID_STATE
    # executed -> 任意 非法
    s3, _ = make_service(registry, tmp_path / "t3")
    d3, _ = s3.create_switch_request(TASK, ITEM, make_proposal(), SRC_ATTEMPT, ERROR_ID)
    s3.approve_switch(TASK, ITEM, d3.switch_decision_id, "controller", expected_revision=1)
    s3.mark_executed(TASK, ITEM, d3.switch_decision_id, expected_revision=2)
    with pytest.raises(SwitchStoreError) as ei:
        s3.cancel_switch(TASK, ITEM, d3.switch_decision_id, expected_revision=3)
    assert ei.value.error_code == ERR_INVALID_STATE


def test_repeated_operations_idempotent(svc):
    d, _ = svc.create_switch_request(TASK, ITEM, make_proposal(), SRC_ATTEMPT, ERROR_ID)
    did = d.switch_decision_id
    r1 = svc.approve_switch(TASK, ITEM, did, "controller", expected_revision=1)
    r2 = svc.approve_switch(TASK, ITEM, did, "controller", expected_revision=2)  # 幂等
    assert r2.decision_status == "approved"
    rec = svc._store.read(TASK, ITEM, did)
    assert rec.revision == 2  # 未追加
    # 内容不一致（不同批准人）-> 冲突（已 approved 且非幂等 -> invalid state）
    with pytest.raises(SwitchStoreError) as ei:
        svc.approve_switch(TASK, ITEM, did, "other", expected_revision=2)
    assert ei.value.error_code == ERR_INVALID_STATE


def test_revision_conflict_zero_side_effect(svc):
    d, _ = svc.create_switch_request(TASK, ITEM, make_proposal(), SRC_ATTEMPT, ERROR_ID)
    with pytest.raises(SwitchStoreError) as ei:
        svc.approve_switch(TASK, ITEM, d.switch_decision_id, "controller", expected_revision=99)
    assert ei.value.error_code == ERR_REVISION_CONFLICT
    got = svc.get_switch_decision(TASK, ITEM, d.switch_decision_id)
    assert got.decision_status == "pending_approval"


def test_all_state_changes_revision_event_correct(svc):
    d, _ = svc.create_switch_request(TASK, ITEM, make_proposal(), SRC_ATTEMPT, ERROR_ID)
    did = d.switch_decision_id
    svc.approve_switch(TASK, ITEM, did, "controller", expected_revision=1)
    svc.mark_executed(TASK, ITEM, did, expected_revision=2)
    rec = svc._store.read(TASK, ITEM, did)
    events = [json.loads(l) for l in (svc._store._decision_dir(TASK, ITEM, did) / "events.ndjson")
              .read_text(encoding="utf-8").splitlines() if l.strip()]
    assert [e["sequence"] for e in events] == [1, 2, 3]
    assert [e["to_status"] for e in events] == ["pending_approval", "approved", "executed"]
    assert rec.revision == 3


# ---------------- 边界（零副作用） ---------------- #


def test_no_gateway_or_transport_calls(svc, monkeypatch):
    import services.scoring_provider_gateway as gw

    def _boom(*a, **k):
        raise AssertionError("gateway/transport called")

    monkeypatch.setattr(gw.ScoringProviderGateway, "call", _boom)
    d, _ = svc.create_switch_request(TASK, ITEM, make_proposal(), SRC_ATTEMPT, ERROR_ID)
    svc.approve_switch(TASK, ITEM, d.switch_decision_id, "controller", expected_revision=1)
    svc.mark_executed(TASK, ITEM, d.switch_decision_id, expected_revision=2)
    assert d.decision_status == "pending_approval"


def test_no_pipeline_task_modification(svc, tmp_path, monkeypatch):
    import services.pipeline_task_store as pts

    def _boom(*a, **k):
        raise AssertionError("pipeline store touched")

    monkeypatch.setattr(pts.PipelineTaskStore, "write_task_snapshot", _boom)
    d, _ = svc.create_switch_request(TASK, ITEM, make_proposal(), SRC_ATTEMPT, ERROR_ID)
    assert not (tmp_path / "pipeline-runtime").exists()


def test_no_database_writes(svc, monkeypatch):
    import database as db

    def _boom(*a, **k):
        raise AssertionError("database touched")

    monkeypatch.setattr(db, "SessionLocal", _boom)
    d, _ = svc.create_switch_request(TASK, ITEM, make_proposal(), SRC_ATTEMPT, ERROR_ID)
    assert d.decision_status == "pending_approval"


def test_no_env_value_read(svc):
    import os
    os.environ["TEST_SWITCH_ENV"] = "secret-env"
    try:
        import services.provider_switch_service as mod
        src = open(mod.__file__, encoding="utf-8").read()
        assert "os.getenv" not in src and "environ" not in src
        import services.provider_switch_store as st
        src2 = open(st.__file__, encoding="utf-8").read()
        assert "os.getenv" not in src2 and "environ" not in src2
    finally:
        del os.environ["TEST_SWITCH_ENV"]


def test_no_sensitive_leak(svc):
    d, _ = svc.create_switch_request(TASK, ITEM, make_proposal(), SRC_ATTEMPT, ERROR_ID)
    svc.approve_switch(TASK, ITEM, d.switch_decision_id, "controller", expected_revision=1)
    dump = json.dumps(d.model_dump(mode="json"))
    assert "TOP_SECRET" not in dump
    # store 文件无敏感
    for f in svc._store.root.rglob("*.json"):
        low = f.read_text(encoding="utf-8").lower()
        assert "top_secret" not in low and "bearer" not in low and "api_key" not in low
