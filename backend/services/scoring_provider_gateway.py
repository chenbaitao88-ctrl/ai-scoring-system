"""
ScoringProvider 统一请求网关（Phase 11D-2a）。

- Gateway 只依赖注入式协议（EndpointProfileResolver / CredentialResolver / ProviderTransport），不直接依赖 httpx。
- 不调用真实模型、不读取凭据值（os.getenv 禁用）、不读取 evidence 文件。
- 严格按 15 步调用顺序执行；任何前置校验失败不得调用 transport。
- 输出解析复用三层 JSON 提取思路（直接 / fenced / 单个对象），不调用旧全局 LLM 单例。
- 错误分类与五维策略来自 11A-2c 契约 8.4 冻结矩阵，不由异常字符串临时决定。
- 成功只生成 ProviderResponse（内存结构化输出），不落盘、不写 MachineScore、不执行规则评分 fallback、
  不修改 PipelineTask。

本阶段不实现：真实 URL 配置、真实凭据解析、真实网络调用、Evidence 适配（Phase 11E）。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Literal, Optional, Protocol, Tuple

from models.scoring_provider import (
    ModelCapability,
    ProviderError,
    ProviderErrorCode,
    ProviderRequest,
    ProviderResponse,
    ScoringProvider,
    Usage,
)
from services.scoring_provider_registry import ProviderRegistryError, ScoringProviderRegistry

# ---------------- 安全常量 ---------------- #

_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```")

# 结构化 transport 失败标记（11D-2b）：错误分类只依据该枚举，不从响应正文/异常字符串猜测。
TransportFailureKind = Literal[
    "config_invalid",
    "credential_invalid",
    "capability_mismatch",
    "request_invalid",
    "rate_limited",
    "quota_exhausted",
    "timeout",
    "network_error",
    "server_error",
    "invalid_json",
    "content_safety",
    "unknown",
]

_FAILURE_KIND_TO_CODE: Dict[TransportFailureKind, ProviderErrorCode] = {
    "config_invalid": "PROVIDER_CONFIG_INVALID",
    "credential_invalid": "PROVIDER_CREDENTIAL_INVALID",
    "capability_mismatch": "MODEL_CAPABILITY_MISMATCH",
    "request_invalid": "PROVIDER_REQUEST_INVALID",
    "rate_limited": "PROVIDER_RATE_LIMITED",
    "quota_exhausted": "PROVIDER_QUOTA_EXHAUSTED",
    "timeout": "PROVIDER_TIMEOUT",
    "network_error": "PROVIDER_NETWORK_ERROR",
    "server_error": "PROVIDER_SERVER_ERROR",
    "invalid_json": "PROVIDER_INVALID_JSON",
    "content_safety": "PROVIDER_CONTENT_SAFETY_REJECTED",
    "unknown": "PROVIDER_UNKNOWN_ERROR",
}


class GatewayError(Exception):
    """网关内部稳定错误（供 resolver 等注入组件抛出）。"""

    def __init__(self, error_code: str, message_key: str = "", retryable: bool = False):
        self.error_code = error_code
        self.message_key = message_key or error_code
        self.retryable = retryable
        super().__init__(error_code)


# ---------------- 注入式协议与内部类型（仅内存） ---------------- #


@dataclass(frozen=True)
class EndpointConfig:
    """供 transport 使用的临时 endpoint 配置；不持久化、不进入任何模型/日志。"""

    base_url: str = ""
    extra_headers: Tuple[Tuple[str, str], ...] = ()
    scheme: str = "mock"


class EndpointProfileResolver(Protocol):
    def resolve(self, endpoint_profile: str) -> EndpointConfig:
        """endpoint_profile -> 临时 endpoint 配置；未解析抛 GatewayError(PROVIDER_CONFIG_INVALID)。"""
        ...


class CredentialResolver(Protocol):
    def resolve(self, credential_ref: str) -> str:
        """credential_ref -> 凭据值；结果只允许交给 transport。"""
        ...


class ResponseSchemaValidator(Protocol):
    def validate(self, parsed: dict) -> None:
        """结构化结果 schema 校验；失败抛 GatewayError(PROVIDER_RESPONSE_SCHEMA_INVALID)。"""
        ...


class ScoringRuleValidator(Protocol):
    def validate(self, parsed: dict) -> None:
        """评分规则校验（分值范围等）；失败抛 GatewayError(PROVIDER_SCORE_OUT_OF_RANGE)。"""
        ...


@dataclass(frozen=True)
class TransportResult:
    """transport 返回的原始结果；仅内存。错误分类只依据结构化 failure_kind，不从 body 猜测。"""

    status_code: Optional[int] = None
    body: str = ""  # 仅用于成功 JSON 解析；失败正文不进入分类、日志或模型
    retry_after_seconds: Optional[int] = None
    failure_kind: Optional[TransportFailureKind] = None  # 结构化错误标记（含 quota/content_safety 等）
    content_safety: bool = False  # 兼容标记；等价 failure_kind="content_safety"


class ProviderTransport(Protocol):
    async def call(
        self,
        endpoint: EndpointConfig,
        credential: str,
        model_id: str,
        payload: "ProviderCallPayload",
        timeout_seconds: int,
        temperature: Optional[float],
        seed: Optional[int],
        max_output_tokens: Optional[int],
    ) -> TransportResult:
        """发起一次 Provider 调用并返回原始结果。"""
        ...


@dataclass(frozen=True)
class ImageRef:
    """安全图片引用（不是本地路径；真实图片加载属 Phase 11E）。"""

    ref: str
    format: Optional[str] = None


@dataclass(frozen=True)
class ProviderCallPayload:
    """仅内存临时调用负载：已批准的 system/user 文本 + 安全图片引用。

    - repr/异常信息不得暴露正文（自定义 __repr__）。
    - 不提供通用 model_dump 持久化路径。
    """

    system_prompt: str
    user_prompt: str
    images: Tuple[ImageRef, ...] = ()
    modalities: Tuple[str, ...] = ("text",)

    def __repr__(self) -> str:
        return (
            f"ProviderCallPayload(system_len={len(self.system_prompt)}, "
            f"user_len={len(self.user_prompt)}, images={len(self.images)})"
        )


def canonical_payload_sha256(payload: ProviderCallPayload) -> str:
    """规范化调用负载 SHA-256（与 ProviderRequest.request_payload_sha256 比对用）。"""
    images = [{"ref": i.ref, "format": i.format} for i in payload.images]
    canonical = json.dumps(
        {
            "system": payload.system_prompt,
            "user": payload.user_prompt,
            "images": images,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------- 三层 JSON 提取（复用 chat_json 思路，不调旧单例） ---------------- #


def extract_json_object(content: str) -> Optional[dict]:
    """三层提取：直接解析 -> fenced -> 单个可确定对象。

    - 空内容返回 None。
    - 多个冲突 JSON 对象拒绝（返回 None）。
    """
    if not content or not content.strip():
        return None
    # 1. 直接解析
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            return parsed
        return None
    except json.JSONDecodeError:
        pass
    # 2. fenced
    m = _FENCE_RE.search(content)
    if m:
        try:
            parsed = json.loads(m.group(1))
            if isinstance(parsed, dict):
                return parsed
            return None
        except json.JSONDecodeError:
            pass
    # 3. 所有平衡 JSON 区间；唯一结果返回，冲突拒绝
    candidates: List[dict] = []
    stack = []
    start = -1
    for i, ch in enumerate(content):
        if ch == "{":
            if not stack:
                start = i
            stack.append(i)
        elif ch == "}":
            if stack:
                stack.pop()
                if not stack and start != -1:
                    try:
                        parsed = json.loads(content[start:i + 1])
                        if isinstance(parsed, dict):
                            candidates.append(parsed)
                    except json.JSONDecodeError:
                        pass
                    start = -1
    if not candidates:
        return None
    first = candidates[0]
    for c in candidates[1:]:
        if c != first:
            return None  # 多个冲突 JSON 对象拒绝
    return first


# ---------------- 五维策略表（契约 8.4 冻结） ---------------- #


_ERROR_POLICY: Dict[ProviderErrorCode, dict] = {
    "PROVIDER_CONFIG_INVALID": {"retryable": "no", "switch": "approval_required", "review": "no", "consumes": False, "stop": "if_no_approved_fallback"},
    "PROVIDER_CREDENTIAL_INVALID": {"retryable": "no", "switch": "approval_required", "review": "no", "consumes": True, "stop": "if_no_approved_fallback"},
    "MODEL_CAPABILITY_MISMATCH": {"retryable": "no", "switch": "approval_required", "review": "yes", "consumes": False, "stop": "no"},
    "PROVIDER_REQUEST_INVALID": {"retryable": "no", "switch": "no", "review": "yes", "consumes": False, "stop": "no"},
    "PROVIDER_RATE_LIMITED": {"retryable": "conditional", "switch": "approval_required", "review": "after_exhaustion", "consumes": True, "stop": "no"},
    "PROVIDER_QUOTA_EXHAUSTED": {"retryable": "no", "switch": "approval_required", "review": "after_exhaustion", "consumes": True, "stop": "if_no_approved_fallback"},
    "PROVIDER_TIMEOUT": {"retryable": "conditional", "switch": "approval_required", "review": "after_exhaustion", "consumes": True, "stop": "no"},
    "PROVIDER_NETWORK_ERROR": {"retryable": "conditional", "switch": "approval_required", "review": "after_exhaustion", "consumes": True, "stop": "no"},
    "PROVIDER_SERVER_ERROR": {"retryable": "conditional", "switch": "approval_required", "review": "after_exhaustion", "consumes": True, "stop": "no"},
    "PROVIDER_INVALID_JSON": {"retryable": "conditional", "switch": "approval_required", "review": "yes", "consumes": True, "stop": "no"},
    "PROVIDER_RESPONSE_SCHEMA_INVALID": {"retryable": "conditional", "switch": "approval_required", "review": "yes", "consumes": True, "stop": "no"},
    "PROVIDER_CONTENT_SAFETY_REJECTED": {"retryable": "no", "switch": "approval_required", "review": "yes", "consumes": True, "stop": "no"},
    "PROVIDER_SCORE_OUT_OF_RANGE": {"retryable": "no", "switch": "approval_required", "review": "yes", "consumes": True, "stop": "no"},
    "PROVIDER_UNKNOWN_ERROR": {"retryable": "no", "switch": "no", "review": "yes", "consumes": True, "stop": "if_no_approved_fallback"},
}


def _policy(code: ProviderErrorCode) -> dict:
    return _ERROR_POLICY[code]


# ---------------- Gateway ---------------- #


class ScoringProviderGateway:
    """统一请求网关：能力匹配 -> 请求构造校验 -> endpoint/credential 解析 -> transport -> 输出验证。"""

    def __init__(
        self,
        registry: ScoringProviderRegistry,
        endpoint_resolver: EndpointProfileResolver,
        credential_resolver: CredentialResolver,
        transport: ProviderTransport,
        schema_validator: Optional[ResponseSchemaValidator] = None,
        rule_validator: Optional[ScoringRuleValidator] = None,
        clock: Optional[Callable[[], datetime]] = None,
        uuid_factory: Optional[Callable[[], str]] = None,
    ):
        self._registry = registry
        self._endpoint_resolver = endpoint_resolver
        self._credential_resolver = credential_resolver
        self._transport = transport
        self._schema_validator = schema_validator
        self._rule_validator = rule_validator
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._uuid = uuid_factory or (lambda: f"gateway-{datetime.now(timezone.utc).timestamp()}")

    # ---------- 主入口 ---------- #

    async def call(
        self,
        request: ProviderRequest,
        payload: ProviderCallPayload,
    ) -> Tuple[ProviderResponse, Optional[dict]]:
        """执行调用。返回 (ProviderResponse, structured_output)；失败返回 (ProviderError, None)。"""
        now = self._clock()
        # 1-5. 前置校验（Registry / 身份 / 能力 / 参数）
        try:
            provider, capability = self._registry.resolve_provider_capability(
                request.provider_id, request.model_id,
            )
        except ProviderRegistryError as exc:
            return self._error(request, "PROVIDER_CONFIG_INVALID", now, safe=exc.message_key or "registry resolve failed"), None
        pre = self._preflight(request, provider, capability)
        if pre is not None:
            return self._error(request, pre, now, safe=f"preflight failed: {pre}"), None
        # 6. payload hash
        if canonical_payload_sha256(payload) != request.request_payload_sha256:
            return self._error(request, "PROVIDER_REQUEST_INVALID", now, safe="payload hash mismatch"), None
        # 7-8. endpoint / credential（endpoint_profile 属于 Provider 记录，不在 ProviderRequest 中）
        try:
            endpoint = self._endpoint_resolver.resolve(provider.endpoint_profile)
        except GatewayError as exc:
            return self._error(request, exc.error_code, now, safe=exc.message_key), None
        except Exception:
            return self._error(request, "PROVIDER_CONFIG_INVALID", now, safe="endpoint resolve failed"), None
        try:
            credential = self._credential_resolver.resolve(provider.credential_ref)
        except GatewayError as exc:
            return self._error(request, exc.error_code, now, safe=exc.message_key), None
        except Exception:
            return self._error(request, "PROVIDER_CONFIG_INVALID", now, safe="credential resolve failed"), None
        # 9. transport
        try:
            result = await self._transport.call(
                endpoint, credential, request.model_id, payload,
                timeout_seconds=request.timeout_seconds,
                temperature=request.temperature,
                seed=request.seed,
                max_output_tokens=request.max_output_tokens,
            )
        except Exception:
            return self._error(request, "PROVIDER_UNKNOWN_ERROR", now, safe="transport unexpected failure"), None
        # 10-14. 内容提取与验证
        return self._finalize(request, result, now)

    # ---------- 前置校验 ---------- #

    def _preflight(
        self,
        request: ProviderRequest,
        provider: ScoringProvider,
        capability: ModelCapability,
    ) -> Optional[ProviderErrorCode]:
        # 3. provider/model/version 一致
        if (
            provider.provider_id != request.provider_id
            or provider.model_id != request.model_id
            or provider.capability_version != request.capability_version
            or provider.config_version != request.config_version
        ):
            return "PROVIDER_CONFIG_INVALID"
        # 4. modalities 与必需能力
        if "text" in request.input_modalities and capability.text_input is not True:
            return "MODEL_CAPABILITY_MISMATCH"
        if "image" in request.input_modalities and capability.image_input is not True:
            return "MODEL_CAPABILITY_MISMATCH"
        if capability.structured_json_output is not True:
            return "MODEL_CAPABILITY_MISMATCH"
        if capability.system_message is not True:
            return "MODEL_CAPABILITY_MISMATCH"
        # 5. 参数能力与范围
        if request.temperature is not None:
            if capability.temperature_supported is not True:
                return "MODEL_CAPABILITY_MISMATCH"
            if capability.temperature_min is not None and request.temperature < capability.temperature_min:
                return "MODEL_CAPABILITY_MISMATCH"
            if capability.temperature_max is not None and request.temperature > capability.temperature_max:
                return "MODEL_CAPABILITY_MISMATCH"
        if request.seed is not None and capability.seed_supported is not True:
            return "MODEL_CAPABILITY_MISMATCH"
        if request.max_output_tokens is not None and capability.max_output_tokens is not None:
            if request.max_output_tokens > capability.max_output_tokens:
                return "MODEL_CAPABILITY_MISMATCH"
        if capability.request_timeout_seconds is not None:
            if request.timeout_seconds > capability.request_timeout_seconds:
                return "MODEL_CAPABILITY_MISMATCH"
        return None

    # ---------- 结果加工 ---------- #

    def _finalize(
        self,
        request: ProviderRequest,
        result: TransportResult,
        now: datetime,
    ) -> Tuple[ProviderResponse, Optional[dict]]:
        # transport 层错误分类
        code = self._classify_transport_error(request, result)
        if code is not None:
            return self._error(request, code, now, retry_after=result.retry_after_seconds,
                               safe=f"{code} without body detail"), None
        # 10-11. 内容提取与 JSON 解析
        content = result.body
        parsed = extract_json_object(content)
        if parsed is None:
            return self._error(request, "PROVIDER_INVALID_JSON", now, safe="no valid json object in response"), None
        # 12. schema
        if self._schema_validator is not None:
            try:
                self._schema_validator.validate(parsed)
            except GatewayError as exc:
                return self._error(request, exc.error_code, now, safe=exc.message_key), None
            except Exception:
                return self._error(request, "PROVIDER_RESPONSE_SCHEMA_INVALID", now, safe="schema validation failed"), None
        # 13. scoring rule
        if self._rule_validator is not None:
            try:
                self._rule_validator.validate(parsed)
            except GatewayError as exc:
                return self._error(request, exc.error_code, now, safe=exc.message_key), None
            except Exception:
                return self._error(request, "PROVIDER_SCORE_OUT_OF_RANGE", now, safe="scoring rule validation failed"), None
        # 14. ProviderResponse（identity/hash/version 原样绑定；不落盘）
        output_sha = hashlib.sha256(
            json.dumps(parsed, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        response = ProviderResponse(
            contract_version=request.contract_version,
            response_id=self._uuid(),
            request_id=request.request_id,
            task_id=request.task_id,
            item_id=request.item_id,
            attempt_id=request.attempt_id,
            provider_id=request.provider_id,
            model_id=request.model_id,
            provider_request_id=None,
            http_status_summary=str(result.status_code) if result.status_code is not None else None,
            result_status="success",
            structured_output_ref=None,  # 本阶段不落盘，不伪造引用
            output_sha256=output_sha,
            usage=None,
            latency_ms=None,
            completed_at=now,
            schema_validation_passed=True,
            scoring_rule_validation_passed=True,
            error_ref=None,
            input_fingerprint=request.input_fingerprint,
            prompt_version=request.prompt_version,
            evidence_manifest_sha256=request.evidence_manifest_sha256,
        )
        return response, parsed

    def _classify_transport_error(self, request: ProviderRequest, result: TransportResult) -> Optional[ProviderErrorCode]:
        """错误分类（11D-2b）：优先结构化 failure_kind；无标记时按保守 HTTP 映射。

        - 响应正文绝不用于分类；失败正文不进入错误对象、日志或模型。
        - quota exhausted 只能来自结构化 failure_kind。
        """
        if result.content_safety:
            return "PROVIDER_CONTENT_SAFETY_REJECTED"
        if result.failure_kind is not None:
            return _FAILURE_KIND_TO_CODE[result.failure_kind]
        status = result.status_code
        if status is None:
            return "PROVIDER_UNKNOWN_ERROR"
        if 200 <= status < 300:
            return None  # 2xx：继续解析与验证
        if status in (400, 413, 422):
            return "PROVIDER_REQUEST_INVALID"
        if status == 401:
            return "PROVIDER_CREDENTIAL_INVALID"
        if status == 403:
            # 未明确标注 credential/quota/content_safety 时不猜测
            return "PROVIDER_UNKNOWN_ERROR"
        if status == 404:
            return "PROVIDER_CONFIG_INVALID"
        if status == 408:
            return "PROVIDER_TIMEOUT"
        if status == 429:
            return "PROVIDER_RATE_LIMITED"
        if 500 <= status < 600:
            return "PROVIDER_SERVER_ERROR"
        return "PROVIDER_UNKNOWN_ERROR"

    def _error(
        self,
        request: ProviderRequest,
        code: ProviderErrorCode,
        now: datetime,
        retry_after: Optional[int] = None,
        safe: str = "",
    ) -> ProviderError:
        policy = _policy(code)
        return ProviderError(
            contract_version=request.contract_version,
            error_id=self._uuid(),
            request_id=request.request_id,
            task_id=request.task_id,
            item_id=request.item_id,
            attempt_id=request.attempt_id,
            provider_id=request.provider_id,
            model_id=request.model_id,
            error_code=code,
            error_category=_code_to_category(code),
            message_safe=safe or code,
            retryable=policy["retryable"],
            provider_switch_allowed=policy["switch"],
            manual_review_required=policy["review"],
            consumes_provider_call_attempt=policy["consumes"],
            stop_entire_batch=policy["stop"],
            retry_after_seconds=retry_after,
            occurred_at=now,
            details_sha256=None,
        )


def _code_to_category(code: ProviderErrorCode) -> str:
    mapping = {
        "PROVIDER_CONFIG_INVALID": "config",
        "PROVIDER_CREDENTIAL_INVALID": "credential",
        "MODEL_CAPABILITY_MISMATCH": "capability",
        "PROVIDER_REQUEST_INVALID": "request",
        "PROVIDER_RATE_LIMITED": "rate_limit",
        "PROVIDER_QUOTA_EXHAUSTED": "quota",
        "PROVIDER_TIMEOUT": "timeout",
        "PROVIDER_NETWORK_ERROR": "network",
        "PROVIDER_SERVER_ERROR": "server",
        "PROVIDER_INVALID_JSON": "parse",
        "PROVIDER_RESPONSE_SCHEMA_INVALID": "schema",
        "PROVIDER_CONTENT_SAFETY_REJECTED": "content_safety",
        "PROVIDER_SCORE_OUT_OF_RANGE": "range",
        "PROVIDER_UNKNOWN_ERROR": "unknown",
    }
    return mapping[code]
