"""
Provider 健康快照服务（Phase 11D-2b）。

- 纯内存、确定性健康快照计算：输入 ProviderResponse/ProviderError 的安全摘要序列，输出 ProviderHealth。
- 不读取请求正文、响应正文、Key、URL 或材料；不持久化；不调用网络；不主动健康探测；不选择/切换 Provider。
- 阈值全部显式注入（ProviderHealthPolicy），服务内部不隐藏默认阈值；生产值由 Phase 11E 决定。

健康语义（11D-2b 指令三）：
- 无样本或样本不足 -> unknown
- 满足显式健康阈值 -> healthy
- 成功率下降或连续失败达 degraded 阈值 -> degraded
- 连续失败达 unavailable 阈值 -> unavailable
- registry 中 provider disabled -> disabled
- quota exhausted -> quota_state=exhausted，通常 unavailable
- 429 -> rate_limit_state=throttled；后续成功可按策略解除 throttled
- 单个 content safety 拒绝不影响整体健康
- 单个 score out of range 不直接判整个 Provider unavailable
- request/config 错误与 Provider 服务健康区分，不全部算服务器故障
- 健康快照过期后不能作为健康依据（expires_at 由调用方判断）
- p95 latency 只基于带可靠 latency_ms 的成功调用；无数据为 null
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Literal, Optional

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from models.scoring_provider import (
    CONTRACT_VERSION,
    ProviderHealth,
)

# 客户端/配置类错误：不计入 Provider 服务健康（区分服务故障）
_CLIENT_SIDE_ERROR_CODES = {
    "PROVIDER_REQUEST_INVALID",
    "PROVIDER_CONFIG_INVALID",
    "MODEL_CAPABILITY_MISMATCH",
}
# 内容安全拒绝：不影响整体健康（契约 9.3.5）
_CONTENT_SAFETY_ERROR_CODES = {
    "PROVIDER_CONTENT_SAFETY_REJECTED",
}


class ProviderHealthPolicy(BaseModel):
    """健康阈值策略（全部必填，显式注入；无默认值）。"""

    model_config = ConfigDict(extra="forbid")

    window_seconds: int = Field(gt=0)
    min_sample_size: int = Field(ge=0)
    healthy_success_rate: float = Field(ge=0, le=1)
    degraded_success_rate: float = Field(ge=0, le=1)
    degraded_consecutive_failures: int = Field(ge=1)
    unavailable_consecutive_failures: int = Field(ge=1)
    snapshot_ttl_seconds: int = Field(gt=0)

    @model_validator(mode="after")
    def _thresholds_consistent(self) -> "ProviderHealthPolicy":
        if self.degraded_consecutive_failures > self.unavailable_consecutive_failures:
            raise ValueError("degraded threshold must not exceed unavailable threshold")
        if self.degraded_success_rate > self.healthy_success_rate:
            raise ValueError("degraded_success_rate must not exceed healthy_success_rate")
        return self


class ProviderHealthObservation(BaseModel):
    """一次 Provider 调用的安全健康摘要（观察对象）。

    只保存 Provider、模型、时间、错误码、成功状态、延迟等安全摘要；
    不保存请求/响应正文、endpoint、credential、Prompt、Evidence 内容。
    """

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["scoring-provider/v1"] = CONTRACT_VERSION
    provider_id: str
    model_id: str
    observed_at: AwareDatetime
    success: bool
    error_code: Optional[str] = None
    latency_ms: Optional[int] = Field(default=None, ge=0)


class ProviderHealthService:
    """纯内存健康快照计算（无状态纯函数语义）。"""

    def __init__(
        self,
        policy: ProviderHealthPolicy,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        if not isinstance(policy, ProviderHealthPolicy):
            raise TypeError("policy must be an explicit ProviderHealthPolicy instance")
        self._policy = policy
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def compute(
        self,
        observations: List[ProviderHealthObservation],
        now: Optional[datetime] = None,
        disabled: bool = False,
    ) -> ProviderHealth:
        now = now or self._clock()
        policy = self._policy
        # 窗口过滤：observed_at in [now - window, now]
        window_start = now.replace(tzinfo=now.tzinfo or timezone.utc) - timedelta(seconds=policy.window_seconds)
        in_window = [
            o for o in observations
            if o.observed_at >= window_start and o.observed_at <= now
        ]
        in_window.sort(key=lambda o: o.observed_at)

        # 健康统计样本：排除 content_safety 与客户端/配置类错误
        stat_samples = [
            o for o in in_window
            if o.error_code not in _CONTENT_SAFETY_ERROR_CODES
            and o.error_code not in _CLIENT_SIDE_ERROR_CODES
        ]
        sample_size = len(stat_samples)
        success_count = sum(1 for o in stat_samples if o.success)
        consecutive_failures = self._tail_consecutive_failures(stat_samples)
        success_rate = (success_count / sample_size) if sample_size else None

        # 独立状态
        has_quota = any(o.error_code == "PROVIDER_QUOTA_EXHAUSTED" for o in in_window)
        # throttled 基于最近一次观察：后续成功可解除；窗口内更早的 429 不持续生效
        last = in_window[-1] if in_window else None
        rate_limit_state = "throttled" if last is not None and last.error_code == "PROVIDER_RATE_LIMITED" else "normal"
        quota_state = "exhausted" if has_quota else "available"

        # status 判定
        if disabled:
            status = "disabled"
        elif sample_size < policy.min_sample_size:
            status = "unknown"
        elif has_quota:
            status = "unavailable"
        elif consecutive_failures >= policy.unavailable_consecutive_failures:
            status = "unavailable"
        elif consecutive_failures >= policy.degraded_consecutive_failures:
            status = "degraded"
        elif success_rate is not None and success_rate < policy.degraded_success_rate:
            status = "degraded"
        elif success_rate is not None and success_rate >= policy.healthy_success_rate:
            status = "healthy"
        else:
            status = "unknown"

        return ProviderHealth(
            contract_version=CONTRACT_VERSION,
            health_id=f"provider-health-{self._clock().timestamp()}",
            provider_id=observations[0].provider_id if observations else "unknown",
            model_id=observations[0].model_id if observations else "unknown",
            status=status,
            observed_at=now,
            window_seconds=policy.window_seconds,
            sample_size=sample_size,
            success_rate=success_rate,
            rate_limit_state=rate_limit_state,
            quota_state=quota_state,
            p95_latency_ms=self._p95_latency(stat_samples),
            consecutive_failures=consecutive_failures,
            last_error_code=self._last_error_code(in_window),
            source="passive",
            expires_at=now.replace(tzinfo=now.tzinfo or timezone.utc) + timedelta(seconds=policy.snapshot_ttl_seconds),
        )

    @staticmethod
    def _tail_consecutive_failures(samples: List[ProviderHealthObservation]) -> int:
        """窗口内按时间排序的最长尾部连续失败。"""
        count = 0
        for o in reversed(samples):
            if o.success:
                break
            count += 1
        return count

    @staticmethod
    def _last_error_code(in_window: List[ProviderHealthObservation]) -> Optional[str]:
        if not in_window:
            return None
        return in_window[-1].error_code

    @staticmethod
    def _p95_latency(samples: List[ProviderHealthObservation]) -> Optional[int]:
        """p95 只基于带可靠 latency_ms 的成功调用；无数据为 null。"""
        latencies = sorted(
            o.latency_ms for o in samples if o.success and o.latency_ms is not None
        )
        if not latencies:
            return None
        idx = max(0, math.ceil(0.95 * len(latencies)) - 1)
        return latencies[idx]
