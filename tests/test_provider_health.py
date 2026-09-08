"""
Phase 11D-2b：Provider 健康快照服务合成测试。

纯内存确定性计算；不访问网络、不读取环境变量、不持久化。
所有数据均为脱敏、虚构合成数据。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from models.scoring_provider import ProviderHealth
from services.provider_health_service import (
    ProviderHealthObservation,
    ProviderHealthPolicy,
    ProviderHealthService,
)

T0 = datetime(2026, 8, 10, 6, 0, 0, tzinfo=timezone.utc)
PID = "provider_demo_001"
MID = "model_demo_v1"
CONTRACT = "scoring-provider/v1"


def make_policy(**overrides):
    base = dict(
        window_seconds=900,
        min_sample_size=5,
        healthy_success_rate=0.9,
        degraded_success_rate=0.6,
        degraded_consecutive_failures=2,
        unavailable_consecutive_failures=4,
        snapshot_ttl_seconds=300,
    )
    base.update(overrides)
    return ProviderHealthPolicy(**base)


def obs(success=True, error_code=None, latency=None, offset_minutes=0):
    return ProviderHealthObservation(
        contract_version=CONTRACT,
        provider_id=PID,
        model_id=MID,
        observed_at=T0 + timedelta(minutes=offset_minutes),
        success=success,
        error_code=error_code,
        latency_ms=latency,
    )


def ok_samples(n, latency=500, start_min=0):
    return [obs(success=True, latency=latency, offset_minutes=start_min + i) for i in range(n)]


def compute(observations, policy=None, now=None, disabled=False):
    svc = ProviderHealthService(policy or make_policy())
    return svc.compute(observations, now=now or T0 + timedelta(minutes=10), disabled=disabled)


# ---------------- 基础语义 ---------------- #


def test_no_samples_unknown():
    h = compute([])
    assert h.status == "unknown"
    assert h.sample_size == 0


def test_insufficient_samples_unknown():
    h = compute(ok_samples(2), policy=make_policy(min_sample_size=5))
    assert h.status == "unknown"
    assert h.sample_size == 2


def test_healthy():
    h = compute(ok_samples(10))
    assert h.status == "healthy"
    assert h.success_rate == 1.0
    assert h.sample_size == 10


def test_degraded_by_success_rate():
    # 7 成功 + 3 失败（时间均在 now 之前）：尾部连续失败 3 >= degraded 阈值 2 -> degraded
    samples = ok_samples(7) + [
        obs(success=False, error_code="PROVIDER_SERVER_ERROR", offset_minutes=7 + i) for i in range(3)
    ]
    h = compute(samples)
    assert h.status == "degraded"


def test_unavailable_by_consecutive_failures():
    samples = [obs(success=False, error_code="PROVIDER_SERVER_ERROR", offset_minutes=i) for i in range(5)]
    h = compute(samples)
    assert h.status == "unavailable"
    assert h.consecutive_failures == 5


def test_disabled():
    h = compute(ok_samples(10), disabled=True)
    assert h.status == "disabled"


def test_throttled_on_429():
    samples = ok_samples(5) + [obs(success=False, error_code="PROVIDER_RATE_LIMITED", offset_minutes=8)]
    h = compute(samples)
    assert h.rate_limit_state == "throttled"
    # 后续成功可解除 throttled（窗口内最后为成功且无 429 -> normal）
    samples2 = samples + [obs(success=True, offset_minutes=9)]
    h2 = compute(samples2)
    assert h2.rate_limit_state == "normal"


def test_quota_exhausted_unavailable():
    samples = ok_samples(5) + [obs(success=False, error_code="PROVIDER_QUOTA_EXHAUSTED", offset_minutes=8)]
    h = compute(samples)
    assert h.quota_state == "exhausted"
    assert h.status == "unavailable"


def test_content_safety_not_lower_overall_health():
    """单个 content safety 拒绝不影响整体健康。"""
    samples = ok_samples(9) + [obs(success=False, error_code="PROVIDER_CONTENT_SAFETY_REJECTED", offset_minutes=9)]
    h = compute(samples)
    assert h.status == "healthy"  # content safety 不计入健康统计
    assert h.sample_size == 9


def test_score_out_of_range_not_directly_unavailable():
    """单个 score out of range 不直接判 unavailable（按普通失败计数）。"""
    samples = ok_samples(9) + [obs(success=False, error_code="PROVIDER_SCORE_OUT_OF_RANGE", offset_minutes=9)]
    h = compute(samples)
    assert h.status == "healthy"  # 1 失败未达 degraded 阈值（连续 1 < 2）
    # 2 成功 + 3 失败（样本数达 min 5，尾部连续失败 3 >= 2）-> degraded，不直接 unavailable
    samples2 = ok_samples(2) + [
        obs(success=False, error_code="PROVIDER_SCORE_OUT_OF_RANGE", offset_minutes=2 + i) for i in range(3)
    ]
    h2 = compute(samples2)
    assert h2.status == "degraded"
    assert h2.consecutive_failures == 3


def test_client_errors_do_not_count_as_server_failures():
    """request/config 错误与服务健康区分，不全部算服务器故障。"""
    samples = ok_samples(9) + [
        obs(success=False, error_code="PROVIDER_REQUEST_INVALID", offset_minutes=8),
        obs(success=False, error_code="PROVIDER_CONFIG_INVALID", offset_minutes=9),
    ]
    h = compute(samples)
    assert h.status == "healthy"  # 客户端/配置错误不计入健康统计
    assert h.sample_size == 9


def test_expired_snapshot_not_health_basis():
    """过期快照不可作为健康依据：expires_at = now + ttl；窗口外样本被忽略。"""
    svc = ProviderHealthService(make_policy(window_seconds=900, snapshot_ttl_seconds=300))
    now = T0 + timedelta(minutes=10)
    old_samples = [obs(success=False, error_code="PROVIDER_SERVER_ERROR", offset_minutes=-60)]  # 窗口外
    h = svc.compute(old_samples, now=now)
    assert h.sample_size == 0
    assert h.status == "unknown"
    assert h.expires_at == now + timedelta(seconds=300)


def test_p95_only_reliable_success_latency():
    """p95 只使用带可靠 latency 的成功调用；无数据为 null。"""
    # 成功带延迟
    samples = ok_samples(10, latency=1000)
    h = compute(samples)
    assert h.p95_latency_ms is not None and h.p95_latency_ms <= 1000
    # 成功但无延迟 -> null
    h2 = compute([obs(success=True, latency=None) for _ in range(5)])
    assert h2.p95_latency_ms is None
    # 只有失败带延迟 -> null（不估算）
    h3 = compute([obs(success=False, error_code="PROVIDER_SERVER_ERROR", latency=50) for _ in range(5)])
    assert h3.p95_latency_ms is None


def test_policy_must_be_explicit():
    """HealthPolicy 必须显式提供：不接受裸 dict 或 None。"""
    with pytest.raises(TypeError):
        ProviderHealthService(None)
    with pytest.raises(TypeError):
        ProviderHealthService({"window_seconds": 900})
    with pytest.raises(Exception):
        make_policy(healthy_success_rate=0.5, degraded_success_rate=0.8)  # 阈值不一致


# ---------------- 边界（零副作用） ---------------- #


def test_health_service_no_network(monkeypatch):
    import httpx

    def _boom(*a, **k):
        raise AssertionError("network call forbidden")

    monkeypatch.setattr(httpx.AsyncClient, "post", _boom)
    monkeypatch.setattr(httpx.Client, "post", _boom)
    h = compute(ok_samples(5))
    assert h.status == "healthy"


def test_health_service_no_env_read(monkeypatch):
    import os
    os.environ["TEST_HEALTH_ENV"] = "secret-env-value"
    try:
        import services.provider_health_service as mod
        src = open(mod.__file__, encoding="utf-8").read()
        assert "os.getenv" not in src
        assert "environ" not in src
    finally:
        del os.environ["TEST_HEALTH_ENV"]


def test_health_object_has_no_secret_content():
    """健康对象中无正文、Key、URL、路径。"""
    samples = ok_samples(5) + [
        obs(success=False, error_code="PROVIDER_SERVER_ERROR", latency=10),
        obs(success=False, error_code="PROVIDER_RATE_LIMITED", latency=10),
    ]
    h = compute(samples)
    dump = json.dumps(h.model_dump(mode="json"))
    for secret in ("TOP_SECRET", "http://", "https://", "C:\\", "/data/", "Bearer", "api_key"):
        assert secret.lower() not in dump.lower(), secret


def test_observation_extra_forbid():
    """观察对象拒绝正文/凭据等字段（extra=forbid）。"""
    with pytest.raises(Exception):
        ProviderHealthObservation(
            contract_version=CONTRACT, provider_id=PID, model_id=MID,
            observed_at=T0, success=True, response_body="secret",
        )
    with pytest.raises(Exception):
        ProviderHealthObservation(
            contract_version=CONTRACT, provider_id=PID, model_id=MID,
            observed_at=T0, success=True, api_key="sk-xxx",
        )
