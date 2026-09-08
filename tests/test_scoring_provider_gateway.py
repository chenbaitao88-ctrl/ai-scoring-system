"""
Phase 11D-2a：ScoringProvider Gateway 合成测试。

全部使用注入式 fake/mock：不调用真实模型、不读取凭据值、不访问网络、不落盘、不写数据库。
所有数据均为脱敏、虚构合成数据。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from models.scoring_provider import (
    ModelCapability,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    ScoringProvider,
)
from services.scoring_provider_gateway import (
    EndpointConfig,
    GatewayError,
    ImageRef,
    ProviderCallPayload,
    ProviderTransport,
    ScoringProviderGateway,
    TransportResult,
    canonical_payload_sha256,
    extract_json_object,
    _ERROR_POLICY,
)
from services.scoring_provider_registry import ScoringProviderRegistry

T0 = datetime(2026, 8, 10, 6, 0, 0, tzinfo=timezone.utc)
TASK = "pipeline_task_demo_001"
ITEM = "item_demo_001"
ATTEMPT = "attempt_demo_001"
PID = "provider_demo_001"
MID = "model_demo_v1"
EVIDENCE = "e" * 64
FP = "f" * 64
CONTRACT = "scoring-provider/v1"


# ---------------- 合成 helper ---------------- #


def registry_dict(capability_overrides=None, provider_overrides=None):
    cap = dict(
        contract_version=CONTRACT, capability_version="cap-demo-001",
        provider_id=PID, model_id=MID,
        text_input=True, image_input=True, structured_json_output=True,
        system_message=True, temperature_supported=True,
        temperature_min=0.0, temperature_max=1.0,
        request_timeout_seconds=300, max_output_tokens=4096,
        seed_supported=True, verification_source="static_config",
    )
    if capability_overrides:
        cap.update(capability_overrides)
    prov = dict(
        contract_version=CONTRACT, provider_id=PID, provider_type="mock",
        display_name="Demo", endpoint_profile="profile_demo_v1", model_id=MID,
        capability_version="cap-demo-001", config_version="cfg-demo-001",
        enabled=True, credential_ref="TEST_PROVIDER_API_KEY",
        capability_ref="capability/provider_demo_001/model_demo_v1/cap-demo-001",
        created_at=T0.isoformat(), updated_at=T0.isoformat(),
    )
    if provider_overrides:
        prov.update(provider_overrides)
    return {
        "registry_version": "provider-registry/v1",
        "updated_at": T0.isoformat(),
        "revision": 1,
        "providers": [prov],
        "capabilities": [cap],
    }


@pytest.fixture
def registry(tmp_path):
    p = tmp_path / "provider_registry.json"
    p.write_text(json.dumps(registry_dict(), ensure_ascii=False), encoding="utf-8")
    r = ScoringProviderRegistry(p)
    r.load()
    return r


def make_payload(user="作品描述…", system="评分标准…", modalities=("text",), images=()):
    return ProviderCallPayload(system_prompt=system, user_prompt=user, modalities=modalities, images=images)


def make_request(payload, **overrides):
    base = dict(
        contract_version=CONTRACT,
        request_id="provider_request_demo_001",
        task_id=TASK, item_id=ITEM, attempt_id=ATTEMPT,
        provider_id=PID, model_id=MID,
        capability_version="cap-demo-001", config_version="cfg-demo-001",
        evidence_package_id="evidence_demo_001",
        evidence_manifest_sha256=EVIDENCE,
        input_fingerprint=FP,
        scoring_policy_version="policy-demo-001",
        scoring_mode="mixed",
        prompt_version="prompt-demo-001",
        response_schema_version="score-response-v1",
        timeout_seconds=120,
        requested_at=T0,
        input_modalities=list(payload.modalities),
        evidence_refs=["evidence_demo_001"],
        request_payload_sha256=canonical_payload_sha256(payload),
        selection_id="selection_demo_001",
        temperature=0.3,
    )
    base.update(overrides)
    return ProviderRequest(**base)


class FakeEndpointResolver:
    def __init__(self, ok=True, code="PROVIDER_CONFIG_INVALID"):
        self.ok = ok
        self.code = code
        self.calls = []

    def resolve(self, profile):
        self.calls.append(profile)
        if not self.ok:
            raise GatewayError(self.code, "endpoint unregistered")
        return EndpointConfig(base_url="mock://local", scheme="mock")


class FakeCredentialResolver:
    def __init__(self, value="MOCK_CRED_VALUE"):
        self.value = value
        self.calls = []

    def resolve(self, ref):
        self.calls.append(ref)
        return self.value


class FakeTransport:
    """确定性 mock transport：按 scenario 返回固定结果。"""

    def __init__(self, scenario="direct", body="", status_code=200, retry_after=None,
                 failure_kind=None, content_safety=False):
        self.scenario = scenario
        self.body = body
        self.status_code = status_code
        self.retry_after = retry_after
        self.failure_kind = failure_kind
        self.content_safety = content_safety
        self.calls = []

    async def call(self, endpoint, credential, model_id, payload, timeout_seconds,
                   temperature, seed, max_output_tokens):
        self.calls.append({
            "endpoint": endpoint, "credential": credential, "model_id": model_id,
            "payload": payload, "timeout_seconds": timeout_seconds,
            "temperature": temperature, "seed": seed, "max_output_tokens": max_output_tokens,
        })
        if self.scenario == "direct":
            return TransportResult(status_code=200, body='{"total_score": 90, "comment": "ok"}')
        if self.scenario == "fenced":
            return TransportResult(status_code=200, body='text\n```json\n{"total_score": 88}\n```\n')
        if self.scenario == "embedded":
            return TransportResult(status_code=200, body='前缀 {"total_score": 85} 后缀')
        if self.scenario == "multiple":
            return TransportResult(status_code=200, body='{"total_score": 1} 和 {"total_score": 2}')
        if self.scenario == "empty":
            return TransportResult(status_code=200, body="")
        if self.scenario == "invalid_json":
            return TransportResult(status_code=200, body="不是 JSON")
        if self.scenario == "boom":
            raise RuntimeError("unexpected internal boom with secret detail")
        return TransportResult(
            status_code=self.status_code,
            body=self.body,
            retry_after_seconds=self.retry_after,
            failure_kind=self.failure_kind,
            content_safety=self.content_safety,
        )


class FakeSchemaValidator:
    def __init__(self, fail=False, code="PROVIDER_RESPONSE_SCHEMA_INVALID"):
        self.fail = fail
        self.code = code
        self.calls = []

    def validate(self, parsed):
        self.calls.append(parsed)
        if self.fail:
            raise GatewayError(self.code, "schema mismatch")


class FakeRuleValidator:
    def __init__(self, fail=False, code="PROVIDER_SCORE_OUT_OF_RANGE"):
        self.fail = fail
        self.code = code
        self.calls = []

    def validate(self, parsed):
        self.calls.append(parsed)
        if self.fail:
            raise GatewayError(self.code, "score out of range")


def _uuid_seq():
    n = [0]

    def _next():
        n[0] += 1
        return f"gateway-uuid-{n[0]:04d}"

    return _next


def _clock():
    return lambda: T0


def make_gateway(registry, transport, endpoint_ok=True, schema=None, rule=None, cred_value="MOCK_CRED_VALUE"):
    return ScoringProviderGateway(
        registry=registry,
        endpoint_resolver=FakeEndpointResolver(ok=endpoint_ok),
        credential_resolver=FakeCredentialResolver(value=cred_value),
        transport=transport,
        schema_validator=schema,
        rule_validator=rule,
        clock=_clock(),
        uuid_factory=_uuid_seq(),
    )


def run(coro):
    return asyncio.run(coro)


# ---------------- 成功路径 ---------------- #


def test_text_request_success(registry):
    """合法文本请求成功。"""
    transport = FakeTransport(scenario="direct")
    gw = make_gateway(registry, transport)
    payload = make_payload()
    resp, out = run(gw.call(make_request(payload), payload))
    assert isinstance(resp, ProviderResponse) and resp.result_status == "success"
    assert out == {"total_score": 90, "comment": "ok"}
    assert resp.output_sha256 is not None
    assert transport.calls


def test_image_request_success(registry):
    """合法图片能力请求成功（image=True + image modality）。"""
    transport = FakeTransport(scenario="direct")
    gw = make_gateway(registry, transport)
    payload = make_payload(modalities=("text", "image"),
                           images=(ImageRef(ref="img_ref_001", format="png"),))
    resp, _ = run(gw.call(make_request(payload), payload))
    assert resp.result_status == "success"
    assert len(transport.calls) == 1


def test_image_capability_false_unknown_rejected_before_call(registry):
    """image false/unknown 时调用前拒绝且不调用 transport。"""
    for val in (False, "unknown"):
        p = tmp_registry_with_capability(registry, {"image_input": val})
        transport = FakeTransport(scenario="direct")
        gw = make_gateway(p, transport)
        payload = make_payload(modalities=("text", "image"))
        resp, _ = run(gw.call(make_request(payload), payload))
        assert resp.error_code == "MODEL_CAPABILITY_MISMATCH"
        assert transport.calls == []


def test_structured_json_unknown_rejected(registry):
    """structured JSON unknown 时拒绝（调用前）。"""
    p = tmp_registry_with_capability(registry, {"structured_json_output": "unknown"})
    transport = FakeTransport(scenario="direct")
    gw = make_gateway(p, transport)
    payload = make_payload()
    resp, _ = run(gw.call(make_request(payload), payload))
    assert resp.error_code == "MODEL_CAPABILITY_MISMATCH"
    assert transport.calls == []


def test_temperature_unsupported_or_out_of_range_rejected(registry):
    """temperature 不支持或超出范围拒绝。"""
    p = tmp_registry_with_capability(registry, {"temperature_supported": False})
    gw = make_gateway(p, FakeTransport())
    payload = make_payload()
    resp, _ = run(gw.call(make_request(payload, temperature=0.3), payload))
    assert resp.error_code == "MODEL_CAPABILITY_MISMATCH"
    p2 = tmp_registry_with_capability(registry, {"temperature_supported": True, "temperature_max": 0.2})
    gw2 = make_gateway(p2, FakeTransport())
    resp2, _ = run(gw2.call(make_request(payload, temperature=0.5), payload))
    assert resp2.error_code == "MODEL_CAPABILITY_MISMATCH"


def test_timeout_exceeds_capability_rejected(registry):
    """timeout 超出能力声明拒绝。"""
    p = tmp_registry_with_capability(registry, {"request_timeout_seconds": 60})
    gw = make_gateway(p, FakeTransport())
    payload = make_payload()
    resp, _ = run(gw.call(make_request(payload, timeout_seconds=120), payload))
    assert resp.error_code == "MODEL_CAPABILITY_MISMATCH"
    assert gw._transport.calls == []


def test_payload_hash_mismatch_rejected(registry):
    """payload hash 不一致拒绝（REQUEST_INVALID）。"""
    transport = FakeTransport(scenario="direct")
    gw = make_gateway(registry, transport)
    payload = make_payload(user="A")
    req = make_request(payload, request_payload_sha256="d" * 64)
    resp, _ = run(gw.call(req, payload))
    assert resp.error_code == "PROVIDER_REQUEST_INVALID"
    assert transport.calls == []


def test_provider_model_version_mismatch_rejected(registry):
    """provider/model/version 不一致拒绝。"""
    transport = FakeTransport(scenario="direct")
    gw = make_gateway(registry, transport)
    payload = make_payload()
    resp, _ = run(gw.call(make_request(payload, model_id="model_other"), payload))
    assert resp.error_code == "PROVIDER_CONFIG_INVALID"
    resp2, _ = run(gw.call(make_request(payload, config_version="cfg-other"), payload))
    assert resp2.error_code == "PROVIDER_CONFIG_INVALID"
    assert transport.calls == []


def test_resolver_failure_no_transport_call(registry):
    """endpoint resolver 失败时不调用 transport（CONFIG_INVALID）。"""
    transport = FakeTransport(scenario="direct")
    gw = make_gateway(registry, transport, endpoint_ok=False)
    payload = make_payload()
    resp, _ = run(gw.call(make_request(payload), payload))
    assert resp.error_code == "PROVIDER_CONFIG_INVALID"
    assert transport.calls == []


def test_credential_value_never_leaked(registry):
    """credential 值不进入模型、日志、repr 或错误（transport 是唯一合法接收方）。"""
    transport = FakeTransport(scenario="direct")
    gw = make_gateway(registry, transport, cred_value="TOP_SECRET_CRED_999")
    payload = make_payload(user="真实内容不该出现")
    resp, out = run(gw.call(make_request(payload), payload))
    dump = json.dumps(resp.model_dump(mode="json"))
    assert "TOP_SECRET_CRED_999" not in dump
    assert "TOP_SECRET_CRED_999" not in repr(transport.calls[0]["payload"])
    assert "TOP_SECRET_CRED_999" not in resp.message_safe if isinstance(resp, ProviderError) else True
    # transport 收到了凭据（仅内存传递，合法）
    assert transport.calls[0]["credential"] == "TOP_SECRET_CRED_999"


# ---------------- 输出解析 ---------------- #


def test_three_legal_json_extractions(registry):
    """三种合法 JSON 提取均成功。"""
    for scenario in ("direct", "fenced", "embedded"):
        transport = FakeTransport(scenario=scenario)
        gw = make_gateway(registry, transport)
        payload = make_payload()
        resp, out = run(gw.call(make_request(payload), payload))
        assert resp.result_status == "success", scenario
        assert isinstance(out, dict)


def test_multiple_empty_invalid_json_rejected(registry):
    """多 JSON、空输出、无效 JSON 拒绝（INVALID_JSON）。"""
    for scenario in ("multiple", "empty", "invalid_json"):
        transport = FakeTransport(scenario=scenario)
        gw = make_gateway(registry, transport)
        payload = make_payload()
        resp, _ = run(gw.call(make_request(payload), payload))
        assert resp.error_code == "PROVIDER_INVALID_JSON", scenario


def test_schema_validator_failure(registry):
    """schema validator 失败 -> RESPONSE_SCHEMA_INVALID。"""
    transport = FakeTransport(scenario="direct")
    gw = make_gateway(registry, transport, schema=FakeSchemaValidator(fail=True))
    payload = make_payload()
    resp, _ = run(gw.call(make_request(payload), payload))
    assert resp.error_code == "PROVIDER_RESPONSE_SCHEMA_INVALID"


def test_rule_validator_failure(registry):
    """scoring rule validator 失败 -> SCORE_OUT_OF_RANGE。"""
    transport = FakeTransport(scenario="direct")
    gw = make_gateway(registry, transport, rule=FakeRuleValidator(fail=True))
    payload = make_payload()
    resp, _ = run(gw.call(make_request(payload), payload))
    assert resp.error_code == "PROVIDER_SCORE_OUT_OF_RANGE"


def test_success_binds_all_identities(registry):
    """成功响应所有身份/hash/version 原样绑定。"""
    transport = FakeTransport(scenario="direct")
    gw = make_gateway(registry, transport)
    payload = make_payload()
    req = make_request(payload)
    resp, _ = run(gw.call(req, payload))
    assert resp.request_id == req.request_id
    assert resp.task_id == req.task_id
    assert resp.item_id == req.item_id
    assert resp.attempt_id == req.attempt_id
    assert resp.provider_id == req.provider_id
    assert resp.model_id == req.model_id
    assert resp.input_fingerprint == req.input_fingerprint
    assert resp.prompt_version == req.prompt_version
    assert resp.evidence_manifest_sha256 == req.evidence_manifest_sha256
    assert resp.structured_output_ref is None  # 不伪造持久化引用


# ---------------- 错误分类与策略 ---------------- #


def _error_case(registry, scenario, **kw):
    transport = FakeTransport(scenario=scenario, **kw)
    gw = make_gateway(registry, transport)
    payload = make_payload()
    resp, _ = run(gw.call(make_request(payload), payload))
    return resp, transport


def test_error_policy_matches_contract(registry):
    """14 类错误五维策略与契约 8.4 一致。"""
    checks = []
    # config invalid（endpoint resolver 失败）
    t = FakeTransport(scenario="direct")
    gw = make_gateway(registry, t, endpoint_ok=False)
    p = make_payload()
    r, _ = run(gw.call(make_request(p), p)); checks.append(r)
    # credential invalid（401）
    t = FakeTransport(scenario="status", status_code=401)
    gw = make_gateway(registry, t)
    r, _ = run(gw.call(make_request(make_payload()), make_payload())); checks.append(r)
    # capability mismatch（image false）
    reg2 = tmp_registry_with_capability(registry, {"image_input": False})
    t = FakeTransport(scenario="direct")
    gw = make_gateway(reg2, t)
    pl = make_payload(modalities=("text", "image"))
    r, _ = run(gw.call(make_request(pl), pl)); checks.append(r)
    # request invalid（hash 不一致）
    t = FakeTransport(scenario="direct")
    gw = make_gateway(registry, t)
    pl = make_payload()
    r, _ = run(gw.call(make_request(pl, request_payload_sha256="d" * 64), pl)); checks.append(r)
    # rate limited（429）
    t = FakeTransport(scenario="status", status_code=429)
    gw = make_gateway(registry, t)
    r, _ = run(gw.call(make_request(make_payload()), make_payload())); checks.append(r)
    # quota exhausted（结构化 failure kind，不从正文猜测）
    t = FakeTransport(scenario="status", status_code=429, failure_kind="quota_exhausted")
    gw = make_gateway(registry, t)
    r, _ = run(gw.call(make_request(make_payload()), make_payload())); checks.append(r)
    # timeout
    t = FakeTransport(scenario="status", failure_kind="timeout")
    gw = make_gateway(registry, t)
    r, _ = run(gw.call(make_request(make_payload()), make_payload())); checks.append(r)
    # network error
    t = FakeTransport(scenario="status", failure_kind="network_error")
    gw = make_gateway(registry, t)
    r, _ = run(gw.call(make_request(make_payload()), make_payload())); checks.append(r)
    # server error（500）
    t = FakeTransport(scenario="status", status_code=500)
    gw = make_gateway(registry, t)
    r, _ = run(gw.call(make_request(make_payload()), make_payload())); checks.append(r)
    # invalid json
    t = FakeTransport(scenario="invalid_json")
    gw = make_gateway(registry, t)
    r, _ = run(gw.call(make_request(make_payload()), make_payload())); checks.append(r)
    # response schema invalid
    t = FakeTransport(scenario="direct")
    gw = make_gateway(registry, t, schema=FakeSchemaValidator(fail=True))
    r, _ = run(gw.call(make_request(make_payload()), make_payload())); checks.append(r)
    # content safety rejected
    t = FakeTransport(scenario="status", content_safety=True)
    gw = make_gateway(registry, t)
    r, _ = run(gw.call(make_request(make_payload()), make_payload())); checks.append(r)
    # score out of range
    t = FakeTransport(scenario="direct")
    gw = make_gateway(registry, t, rule=FakeRuleValidator(fail=True))
    r, _ = run(gw.call(make_request(make_payload()), make_payload())); checks.append(r)
    # unknown error
    t = FakeTransport(scenario="status", failure_kind="unknown")
    gw = make_gateway(registry, t)
    r, _ = run(gw.call(make_request(make_payload()), make_payload())); checks.append(r)

    expected_codes = [
        "PROVIDER_CONFIG_INVALID", "PROVIDER_CREDENTIAL_INVALID", "MODEL_CAPABILITY_MISMATCH",
        "PROVIDER_REQUEST_INVALID", "PROVIDER_RATE_LIMITED", "PROVIDER_QUOTA_EXHAUSTED",
        "PROVIDER_TIMEOUT", "PROVIDER_NETWORK_ERROR", "PROVIDER_SERVER_ERROR",
        "PROVIDER_INVALID_JSON", "PROVIDER_RESPONSE_SCHEMA_INVALID",
        "PROVIDER_CONTENT_SAFETY_REJECTED", "PROVIDER_SCORE_OUT_OF_RANGE", "PROVIDER_UNKNOWN_ERROR",
    ]
    got_codes = [r.error_code for r in checks]
    assert got_codes == expected_codes, got_codes
    # 五维策略与冻结表一致
    for r in checks:
        pol = _ERROR_POLICY[r.error_code]
        assert r.retryable == pol["retryable"], r.error_code
        assert r.provider_switch_allowed == pol["switch"], r.error_code
        assert r.manual_review_required == pol["review"], r.error_code
        assert r.consumes_provider_call_attempt == pol["consumes"], r.error_code
        assert r.stop_entire_batch == pol["stop"], r.error_code


def test_retry_after_enters_safe_field(registry):
    """429 的 retry_after 正确进入安全字段。"""
    t = FakeTransport(scenario="status", status_code=429, retry_after=37)
    gw = make_gateway(registry, t)
    payload = make_payload()
    resp, _ = run(gw.call(make_request(payload), payload))
    assert resp.error_code == "PROVIDER_RATE_LIMITED"
    assert resp.retry_after_seconds == 37
    assert resp.message_safe  # 脱敏短句


def test_unknown_exception_no_raw_leak(registry):
    """transport 抛未知异常 -> UNKNOWN_ERROR，不泄露原文。"""
    t = FakeTransport(scenario="boom")
    gw = make_gateway(registry, t)
    payload = make_payload()
    resp, _ = run(gw.call(make_request(payload), payload))
    assert resp.error_code == "PROVIDER_UNKNOWN_ERROR"
    assert "boom" not in resp.message_safe
    assert "Traceback" not in resp.message_safe
    assert "secret detail" not in resp.message_safe


# ---------------- 边界（零副作用） ---------------- #


def test_structured_output_not_persisted(registry, tmp_path):
    """structured output 不落盘：成功后目录无输出文件、structured_output_ref 为 null。"""
    transport = FakeTransport(scenario="direct")
    gw = make_gateway(registry, transport)
    payload = make_payload()
    resp, out = run(gw.call(make_request(payload), payload))
    assert out is not None
    assert resp.structured_output_ref is None
    files = list(tmp_path.rglob("*"))
    assert not any(f.suffix in (".json", ".out", ".txt") and "output" in f.name for f in files)


def test_gateway_does_not_call_old_llm_service(registry, monkeypatch):
    """Gateway 不调用旧 llm_service。"""
    import services.llm_service as ls

    def _boom(*a, **k):
        raise AssertionError("old llm_service called")

    monkeypatch.setattr(ls.LLMService, "chat", _boom)
    monkeypatch.setattr(ls.LLMService, "chat_json", _boom)
    transport = FakeTransport(scenario="direct")
    gw = make_gateway(registry, transport)
    payload = make_payload()
    resp, _ = run(gw.call(make_request(payload), payload))
    assert resp.result_status == "success"


def test_gateway_does_not_call_autoscorer(registry, monkeypatch):
    """Gateway 不调用 AutoScorer。"""
    import services.scorer as sc

    def _boom(*a, **k):
        raise AssertionError("AutoScorer called")

    monkeypatch.setattr(sc.AutoScorer, "score_team", _boom)
    transport = FakeTransport(scenario="direct")
    gw = make_gateway(registry, transport)
    payload = make_payload()
    resp, _ = run(gw.call(make_request(payload), payload))
    assert resp.result_status == "success"


def test_gateway_does_not_write_database(registry, monkeypatch):
    """Gateway 不写数据库。"""
    import database as db

    def _boom(*a, **k):
        raise AssertionError("database touched")

    monkeypatch.setattr(db, "SessionLocal", _boom)
    transport = FakeTransport(scenario="direct")
    gw = make_gateway(registry, transport)
    payload = make_payload()
    resp, _ = run(gw.call(make_request(payload), payload))
    assert resp.result_status == "success"


def test_gateway_does_not_modify_pipeline_task(registry, tmp_path, monkeypatch):
    """Gateway 不修改 PipelineTask：不创建任务目录、不写事件。"""
    runtime = tmp_path / "pipeline-runtime"
    import services.pipeline_task_store as pts

    def _boom(*a, **k):
        raise AssertionError("pipeline store touched")

    monkeypatch.setattr(pts.PipelineTaskStore, "write_task_snapshot", _boom)
    transport = FakeTransport(scenario="direct")
    gw = make_gateway(registry, transport)
    payload = make_payload()
    resp, _ = run(gw.call(make_request(payload), payload))
    assert resp.result_status == "success"
    assert not runtime.exists()


def test_full_flow_zero_network(registry, monkeypatch):
    """mock 全流程零网络。"""
    import httpx

    def _boom(*a, **k):
        raise AssertionError("network call forbidden")

    monkeypatch.setattr(httpx.AsyncClient, "post", _boom)
    monkeypatch.setattr(httpx.Client, "post", _boom)
    transport = FakeTransport(scenario="fenced")
    gw = make_gateway(registry, transport)
    payload = make_payload()
    resp, _ = run(gw.call(make_request(payload), payload))
    assert resp.result_status == "success"


def test_old_routes_and_demo_model_catalog_remain_compatible():
    """旧评分路由保留，演示模型清单使用当前默认模型。"""
    import config
    assert list(config.AVAILABLE_MODELS) == [
        "qwen3.8-max",
        "qwen3.8-flash",
        "k3",
        "glm-5v-turbo",
        "doubao-seed-2.1-turbo",
        "MiniMax-M3",
    ]
    assert config.AVAILABLE_MODELS["qwen3.8-max"]["name"] == "千问 Qwen3.8 Max"
    assert config.AVAILABLE_MODELS["MiniMax-M3"]["name"] == "MiniMax M3"
    assert config.PARALLEL_SCORING_CONCURRENCY == 5
    import routers.batch_scoring as bs
    assert bs.BatchScoringStartRequest.model_fields["model"].default == "qwen3.8-max"
    import routers.scores_auto as sa
    assert hasattr(sa, "auto_score_team")


# ---------------- 辅助 ---------------- #


def tmp_registry_with_capability(registry, capability_overrides):
    """基于 registry fixture 构造改能力的新注册表（共享同一 path 会脏，故用新文件）。"""
    p = Path(registry._path)
    new_path = p.parent / f"provider_registry_{abs(hash(json.dumps(capability_overrides, sort_keys=True)))}.json"
    data = registry_dict(capability_overrides=capability_overrides)
    new_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    r = ScoringProviderRegistry(new_path)
    r.load()
    return r


# ---------------- 11D-2b：结构化错误分类边界 ---------------- #


def _call_status(registry, status_code=None, failure_kind=None, body="", content_safety=False):
    t = FakeTransport(scenario="status", status_code=status_code, failure_kind=failure_kind,
                      body=body, content_safety=content_safety)
    gw = make_gateway(registry, t)
    payload = make_payload()
    resp, _ = run(gw.call(make_request(payload), payload))
    return resp, t


def test_classification_ignores_body_keywords(registry):
    """错误分类不读取 body 关键词：body 含 quota 但无结构化标记 -> 不判 quota。"""
    resp, _ = _call_status(registry, status_code=429, body='{"error": "quota exhausted, out of quota"}')
    assert resp.error_code == "PROVIDER_RATE_LIMITED"  # 默认 429；quota 需结构化标记


def test_same_body_different_kind_different_code(registry):
    """相同 body、不同 structured failure kind 得到不同稳定错误码。"""
    body = '{"error": "forbidden"}'
    r1, _ = _call_status(registry, status_code=403, body=body, failure_kind="credential_invalid")
    r2, _ = _call_status(registry, status_code=403, body=body, failure_kind="quota_exhausted")
    r3, _ = _call_status(registry, status_code=403, body=body, failure_kind="content_safety")
    r4, _ = _call_status(registry, status_code=403, body=body)  # 未标注 -> unknown
    assert r1.error_code == "PROVIDER_CREDENTIAL_INVALID"
    assert r2.error_code == "PROVIDER_QUOTA_EXHAUSTED"
    assert r3.error_code == "PROVIDER_CONTENT_SAFETY_REJECTED"
    assert r4.error_code == "PROVIDER_UNKNOWN_ERROR"


def test_http_mapping_conservative(registry):
    """400/401/403/404/408/413/422/429/5xx 映射正确。"""
    cases = {
        400: "PROVIDER_REQUEST_INVALID",
        401: "PROVIDER_CREDENTIAL_INVALID",
        403: "PROVIDER_UNKNOWN_ERROR",
        404: "PROVIDER_CONFIG_INVALID",
        408: "PROVIDER_TIMEOUT",
        413: "PROVIDER_REQUEST_INVALID",
        422: "PROVIDER_REQUEST_INVALID",
        429: "PROVIDER_RATE_LIMITED",
        500: "PROVIDER_SERVER_ERROR",
        503: "PROVIDER_SERVER_ERROR",
    }
    for code, expected in cases.items():
        resp, _ = _call_status(registry, status_code=code)
        assert resp.error_code == expected, code


def test_quota_only_from_structured_kind(registry):
    """quota exhausted 只能来自结构化标记。"""
    # 429 + 正文含 quota 字样 -> 仍是 RATE_LIMITED
    r1, _ = _call_status(registry, status_code=429, body="quota exhausted")
    assert r1.error_code == "PROVIDER_RATE_LIMITED"
    # 429 + 结构化 quota_exhausted -> QUOTA_EXHAUSTED
    r2, _ = _call_status(registry, status_code=429, failure_kind="quota_exhausted")
    assert r2.error_code == "PROVIDER_QUOTA_EXHAUSTED"
    # 200 + 结构化 quota_exhausted -> QUOTA_EXHAUSTED（结构化优先于 HTTP）
    r3, _ = _call_status(registry, status_code=200, failure_kind="quota_exhausted")
    assert r3.error_code == "PROVIDER_QUOTA_EXHAUSTED"


def test_14_policies_locked(registry):
    """14 类错误五维策略全部锁定（含结构化标记触发路径）。"""
    kinds = [
        "config_invalid", "credential_invalid", "capability_mismatch", "request_invalid",
        "rate_limited", "quota_exhausted", "timeout", "network_error", "server_error",
        "invalid_json", "content_safety", "unknown",
    ]
    got = []
    for kind in kinds:
        resp, _ = _call_status(registry, failure_kind=kind)
        got.append(resp.error_code)
    assert set(got) >= {
        "PROVIDER_CONFIG_INVALID", "PROVIDER_CREDENTIAL_INVALID", "MODEL_CAPABILITY_MISMATCH",
        "PROVIDER_REQUEST_INVALID", "PROVIDER_RATE_LIMITED", "PROVIDER_QUOTA_EXHAUSTED",
        "PROVIDER_TIMEOUT", "PROVIDER_NETWORK_ERROR", "PROVIDER_SERVER_ERROR",
        "PROVIDER_INVALID_JSON", "PROVIDER_CONTENT_SAFETY_REJECTED", "PROVIDER_UNKNOWN_ERROR",
    }
    for code in got:
        assert _ERROR_POLICY[code]


def test_message_safe_excludes_response_body(registry):
    """message_safe 不含响应正文。"""
    secret_body = "TOP_SECRET_RESPONSE_BODY_777"
    resp, _ = _call_status(registry, status_code=500, body=secret_body)
    assert resp.error_code == "PROVIDER_SERVER_ERROR"
    assert secret_body not in resp.message_safe
    assert secret_body not in json.dumps(resp.model_dump(mode="json"))


def test_unknown_exception_structured_no_raw_leak(registry):
    """unknown exception 经结构化 kind 分类，不泄露异常原文。"""
    t = FakeTransport(scenario="boom")
    gw = make_gateway(registry, t)
    payload = make_payload()
    resp, _ = run(gw.call(make_request(payload), payload))
    assert resp.error_code == "PROVIDER_UNKNOWN_ERROR"
    assert "boom" not in resp.message_safe
    assert "Traceback" not in resp.message_safe
