"""
Phase 11D-1b-fix-1：ScoringProvider 静态注册表合成测试。

契约对齐（fix-1）：
- 能力三值 = True/False/"unknown"；能力匹配只用 `is True`。
- capability_ref 为完整 canonical ref（capability/<pid>/<mid>/<cap_version>），注册表按完整 ref 映射。
- enabled Provider + verification_source=unknown 允许加载（unknown 只在能力匹配时不放行）。

所有注册表数据均为脱敏、虚构合成数据；不读取环境变量值、不发起网络请求。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from models.scoring_provider import ModelCapability, ScoringProvider, build_capability_ref
from services.scoring_provider_registry import (
    ERR_CAPABILITY_MISMATCH,
    ERR_CAPABILITY_NOT_FOUND,
    ERR_DANGLING_CAPABILITY_REF,
    ERR_DUPLICATE_CAPABILITY,
    ERR_DUPLICATE_PROVIDER,
    ERR_IDENTITY_MISMATCH,
    ERR_PROVIDER_NOT_FOUND,
    ERR_REGISTRY_INVALID_JSON,
    ERR_REGISTRY_MISSING,
    ERR_REGISTRY_UNSUPPORTED_VERSION,
    ERR_UNSAFE_CREDENTIAL_REF,
    ERR_UNSAFE_ENDPOINT_PROFILE,
    ProviderRegistryError,
    ScoringProviderRegistry,
)

T0 = datetime(2026, 8, 10, 6, 0, 0, tzinfo=timezone.utc)


# ---------------- fixtures ---------------- #


def provider_dict(provider_id="provider_demo_001", model_id="model_demo_v1",
                  capability_version="cap-demo-001", config_version="cfg-demo-001",
                  enabled=True, credential_ref="TEST_PROVIDER_API_KEY",
                  endpoint_profile="profile_demo_v1", **overrides):
    d = dict(
        contract_version="scoring-provider/v1",
        provider_id=provider_id,
        provider_type="openai_compatible",
        display_name="Demo",
        endpoint_profile=endpoint_profile,
        model_id=model_id,
        capability_version=capability_version,
        config_version=config_version,
        enabled=enabled,
        credential_ref=credential_ref,
        capability_ref=build_capability_ref(provider_id, model_id, capability_version),
        created_at=T0.isoformat(),
        updated_at=T0.isoformat(),
    )
    d.update(overrides)
    return d


def capability_dict(provider_id="provider_demo_001", model_id="model_demo_v1",
                    capability_version="cap-demo-001", **overrides):
    d = dict(
        contract_version="scoring-provider/v1",
        capability_version=capability_version,
        provider_id=provider_id,
        model_id=model_id,
        text_input=True,
        image_input=False,
        structured_json_output=True,
        system_message=True,
        temperature_supported=True,
        seed_supported=True,
        verification_source="static_config",
    )
    d.update(overrides)
    return d


def registry_dict(providers=None, capabilities=None, revision=1, version="provider-registry/v1"):
    return {
        "registry_version": version,
        "updated_at": T0.isoformat(),
        "revision": revision,
        "providers": providers if providers is not None else [],
        "capabilities": capabilities if capabilities is not None else [],
    }


@pytest.fixture
def reg_dir(tmp_path):
    return tmp_path


def write_registry(reg_dir, data):
    p = reg_dir / "provider_registry.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


# ---------------- 加载与错误 ---------------- #


def test_empty_default_registry_loads(reg_dir):
    """空默认注册表加载成功。"""
    p = write_registry(reg_dir, registry_dict())
    r = ScoringProviderRegistry(p)
    doc = r.load()
    assert doc.revision == 1
    assert doc.providers == [] and doc.capabilities == []
    assert r.list_providers() == [] and r.list_enabled() == []


def test_contract_141_registry_loads(reg_dir):
    """契约 14.1 风格 Provider（布尔能力 + canonical ref）可加载。"""
    p = write_registry(reg_dir, registry_dict(
        providers=[provider_dict(
            provider_id="provider_vision_primary_demo", model_id="model_vision_demo_v1",
            capability_version="cap-vision-demo-001", config_version="cfg-demo-001",
        )],
        capabilities=[capability_dict(
            provider_id="provider_vision_primary_demo", model_id="model_vision_demo_v1",
            capability_version="cap-vision-demo-001",
            text_input=True, image_input=True, structured_json_output=True,
        )],
    ))
    r = ScoringProviderRegistry(p)
    r.load()
    pr = r.get_provider("provider_vision_primary_demo", "model_vision_demo_v1")
    assert pr.enabled is True
    assert r.get_capability("provider_vision_primary_demo", "model_vision_demo_v1").image_input is True


def test_contract_142_enabled_unknown_loads(reg_dir):
    """契约 14.2：enabled=true + verification_source=unknown + temperature_supported=unknown 正常加载。"""
    p = write_registry(reg_dir, registry_dict(
        providers=[provider_dict(
            provider_id="provider_text_backup_demo", model_id="model_text_demo_v2",
            capability_version="cap-text-demo-001", config_version="cfg-demo-002",
        )],
        capabilities=[capability_dict(
            provider_id="provider_text_backup_demo", model_id="model_text_demo_v2",
            capability_version="cap-text-demo-001",
            image_input=False, temperature_supported="unknown", seed_supported=False,
            verification_source="unknown",
            retry_capability={"supports_retry_after": "unknown", "idempotent_request_supported": "unknown",
                              "max_safe_retries": None, "retryable_error_codes": []},
        )],
    ))
    r = ScoringProviderRegistry(p)
    doc = r.load()  # 不得因 unknown 失败
    assert len(doc.providers) == 1
    cap = r.get_capability("provider_text_backup_demo", "model_text_demo_v2")
    assert cap.verification_source == "unknown" and cap.temperature_supported == "unknown"


def test_valid_registry_loads_with_stable_sort(reg_dir):
    """合法合成注册表加载与稳定排序。"""
    p = write_registry(reg_dir, registry_dict(
        providers=[
            provider_dict(provider_id="p_b", model_id="m2", capability_version="cap-2", config_version="c2"),
            provider_dict(provider_id="p_a", model_id="m1", capability_version="cap-1", config_version="c1"),
        ],
        capabilities=[
            capability_dict(provider_id="p_b", model_id="m2", capability_version="cap-2"),
            capability_dict(provider_id="p_a", model_id="m1", capability_version="cap-1"),
        ],
    ))
    r = ScoringProviderRegistry(p)
    r.load()
    ids = [(pr.provider_id, pr.model_id) for pr in r.list_providers()]
    assert ids == [("p_a", "m1"), ("p_b", "m2")]


def test_same_capability_version_different_providers_ok(reg_dir):
    """相同 capability_version、不同 Provider 不冲突（canonical ref 区分）。"""
    p = write_registry(reg_dir, registry_dict(
        providers=[
            provider_dict(provider_id="p1", model_id="m1", capability_version="cap-shared"),
            provider_dict(provider_id="p2", model_id="m2", capability_version="cap-shared"),
        ],
        capabilities=[
            capability_dict(provider_id="p1", model_id="m1", capability_version="cap-shared"),
            capability_dict(provider_id="p2", model_id="m2", capability_version="cap-shared"),
        ],
    ))
    r = ScoringProviderRegistry(p)
    r.load()
    assert len(r.list_providers()) == 2
    assert r.get_capability("p1", "m1").provider_id == "p1"
    assert r.get_capability("p2", "m2").provider_id == "p2"


def test_missing_file_explicit_failure(reg_dir):
    """文件不存在显式失败。"""
    r = ScoringProviderRegistry(reg_dir / "nope.json")
    with pytest.raises(ProviderRegistryError) as ei:
        r.load()
    assert ei.value.error_code == ERR_REGISTRY_MISSING


def test_invalid_json_explicit_failure(reg_dir):
    """JSON 损坏显式失败。"""
    p = reg_dir / "provider_registry.json"
    p.write_text("{not valid json", encoding="utf-8")
    r = ScoringProviderRegistry(p)
    with pytest.raises(ProviderRegistryError) as ei:
        r.load()
    assert ei.value.error_code == ERR_REGISTRY_INVALID_JSON


def test_unsupported_version_rejected(reg_dir):
    """版本不支持拒绝。"""
    p = write_registry(reg_dir, registry_dict(version="provider-registry/v999"))
    r = ScoringProviderRegistry(p)
    with pytest.raises(ProviderRegistryError) as ei:
        r.load()
    assert ei.value.error_code == ERR_REGISTRY_UNSUPPORTED_VERSION


def test_duplicate_provider_rejected(reg_dir):
    """重复 Provider（provider_id+model_id+config_version）拒绝。"""
    p = write_registry(reg_dir, registry_dict(
        providers=[
            provider_dict(),
            provider_dict(),  # 同 key
        ],
        capabilities=[capability_dict()],
    ))
    r = ScoringProviderRegistry(p)
    with pytest.raises(ProviderRegistryError) as ei:
        r.load()
    assert ei.value.error_code == ERR_DUPLICATE_PROVIDER


def test_duplicate_capability_rejected(reg_dir):
    """重复 capability key 拒绝。"""
    p = write_registry(reg_dir, registry_dict(
        providers=[provider_dict()],
        capabilities=[
            capability_dict(),
            capability_dict(),  # 同 key
        ],
    ))
    r = ScoringProviderRegistry(p)
    with pytest.raises(ProviderRegistryError) as ei:
        r.load()
    assert ei.value.error_code == ERR_DUPLICATE_CAPABILITY


def test_dangling_full_ref_rejected(reg_dir):
    """悬空完整 capability ref 显式失败。"""
    p = write_registry(reg_dir, registry_dict(
        providers=[provider_dict(capability_version="cap-missing")],  # ref 指向 cap-missing，无对应 capability
        capabilities=[],
    ))
    r = ScoringProviderRegistry(p)
    with pytest.raises(ProviderRegistryError) as ei:
        r.load()
    assert ei.value.error_code == ERR_DANGLING_CAPABILITY_REF


def test_identity_mismatch_rejected(reg_dir):
    """capability_ref 三段与 Provider 自身身份不一致拒绝（稳定码）。"""
    # provider (p1, m1) 但 capability_ref 显式写为指向 (p1, m2) 的完整 ref -> 身份不一致
    p = write_registry(reg_dir, registry_dict(
        providers=[provider_dict(
            provider_id="p1", model_id="m1", capability_version="cap-1",
            capability_ref=build_capability_ref("p1", "m2", "cap-1"),
        )],
        capabilities=[capability_dict(provider_id="p1", model_id="m1", capability_version="cap-1")],
    ))
    r = ScoringProviderRegistry(p)
    with pytest.raises(ProviderRegistryError) as ei:
        r.load()
    assert ei.value.error_code == ERR_IDENTITY_MISMATCH


def test_unsafe_credential_ref_rejected(reg_dir):
    """不安全 credential_ref 拒绝（值/URL/路径形式）。"""
    for bad in ("sk-actual-secret", "https://api.example.com/key=abc", "path/to/secret", "KEY=value"):
        p = write_registry(reg_dir, registry_dict(
            providers=[provider_dict(credential_ref=bad)],
            capabilities=[capability_dict()],
        ))
        r = ScoringProviderRegistry(p)
        with pytest.raises(ProviderRegistryError) as ei:
            r.load()
        assert ei.value.error_code == ERR_UNSAFE_CREDENTIAL_REF


def test_url_endpoint_profile_rejected(reg_dir):
    """URL 形式 endpoint_profile 拒绝。"""
    p = write_registry(reg_dir, registry_dict(
        providers=[provider_dict(endpoint_profile="https://internal.example.com/v1")],
        capabilities=[capability_dict()],
    ))
    r = ScoringProviderRegistry(p)
    with pytest.raises(ProviderRegistryError) as ei:
        r.load()
    assert ei.value.error_code == ERR_UNSAFE_ENDPOINT_PROFILE


# ---------------- 查询与筛选 ---------------- #


def test_query_missing_objects_explicit_failure(reg_dir):
    """查询不存在对象显式失败。"""
    p = write_registry(reg_dir, registry_dict(
        providers=[provider_dict()],
        capabilities=[capability_dict()],
    ))
    r = ScoringProviderRegistry(p)
    r.load()
    with pytest.raises(ProviderRegistryError) as ei:
        r.get_provider("provider_ghost")
    assert ei.value.error_code == ERR_PROVIDER_NOT_FOUND
    with pytest.raises(ProviderRegistryError) as ei:
        r.get_capability("provider_demo_001", "model_ghost")
    assert ei.value.error_code == ERR_CAPABILITY_NOT_FOUND


def test_capability_filter_true_false_unknown(reg_dir):
    """能力筛选：true 匹配；false/unknown 不匹配；disabled 不参与。"""
    p = write_registry(reg_dir, registry_dict(
        providers=[
            provider_dict(provider_id="p_all", model_id="m1", capability_version="cap-1", enabled=True),
            provider_dict(provider_id="p_no_image", model_id="m2", capability_version="cap-2", enabled=True),
            provider_dict(provider_id="p_unknown", model_id="m3", capability_version="cap-3", enabled=True),
            provider_dict(provider_id="p_disabled", model_id="m4", capability_version="cap-4", enabled=False),
        ],
        capabilities=[
            capability_dict(provider_id="p_all", model_id="m1", capability_version="cap-1", image_input=True),
            capability_dict(provider_id="p_no_image", model_id="m2", capability_version="cap-2", image_input=False),
            capability_dict(provider_id="p_unknown", model_id="m3", capability_version="cap-3", image_input="unknown"),
            capability_dict(provider_id="p_disabled", model_id="m4", capability_version="cap-4", image_input=True),
        ],
    ))
    r = ScoringProviderRegistry(p)
    r.load()
    assert [x.provider_id for x in r.filter_by_capabilities(["image_input"])] == ["p_all"]
    assert [x.provider_id for x in r.filter_by_capabilities(["text_input", "structured_json_output"])] == [
        "p_all", "p_no_image", "p_unknown",
    ]
    # unknown 能力名显式报错
    with pytest.raises(ProviderRegistryError) as ei:
        r.filter_by_capabilities(["voice_input"])
    assert ei.value.error_code == ERR_CAPABILITY_MISMATCH


def test_json_roundtrip_registry_boolean(reg_dir):
    """注册表 round-trip 保持布尔能力类型。"""
    p = write_registry(reg_dir, registry_dict(
        providers=[provider_dict()],
        capabilities=[capability_dict(text_input=True, image_input=False)],
    ))
    r = ScoringProviderRegistry(p)
    r.load()
    dumped = r.get_capability("provider_demo_001", "model_demo_v1").model_dump(mode="json")
    assert dumped["text_input"] is True and dumped["image_input"] is False


def test_query_zero_side_effect(reg_dir):
    """查询不修改文件。"""
    p = write_registry(reg_dir, registry_dict(
        providers=[provider_dict()],
        capabilities=[capability_dict()],
    ))
    before = p.read_bytes()
    r = ScoringProviderRegistry(p)
    r.load()
    r.list_providers()
    r.list_enabled()
    r.filter_by_capabilities(["text_input"])
    r.get_provider("provider_demo_001")
    r.get_capability("provider_demo_001", "model_demo_v1")
    assert p.read_bytes() == before


def test_no_env_value_read(reg_dir):
    """注册表加载不读取环境变量值。"""
    os.environ["TEST_PROVIDER_API_KEY"] = "do-not-leak-this-value"
    try:
        p = write_registry(reg_dir, registry_dict(
            providers=[provider_dict()],
            capabilities=[capability_dict()],
        ))
        r = ScoringProviderRegistry(p)
        r.load()
        pr = r.get_provider("provider_demo_001")
        assert pr.credential_ref == "TEST_PROVIDER_API_KEY"  # 只保存变量名
        assert "do-not-leak-this-value" not in json.dumps(pr.model_dump(mode="json"))
    finally:
        del os.environ["TEST_PROVIDER_API_KEY"]


def test_no_network_requests(reg_dir, monkeypatch):
    """注册表不发起网络请求。"""
    import httpx

    def _boom(*args, **kwargs):
        raise AssertionError("network call forbidden")

    monkeypatch.setattr(httpx.AsyncClient, "post", _boom)
    monkeypatch.setattr(httpx.Client, "post", _boom)
    p = write_registry(reg_dir, registry_dict(
        providers=[provider_dict()],
        capabilities=[capability_dict()],
    ))
    r = ScoringProviderRegistry(p)
    r.load()
    r.list_enabled()
    r.filter_by_capabilities(["text_input"])


def test_demo_model_catalog_and_old_routes_remain_compatible():
    """演示模型清单更新，旧评分路由仍保持可用。"""
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
    assert hasattr(bs, "BatchScoringStartRequest")
    assert bs.BatchScoringStartRequest.model_fields["model"].default == "qwen3.8-max"
