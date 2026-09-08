"""
ScoringProvider 只读静态注册表（Phase 11D-1b）。

- 容器 `ProviderRegistryDocument`：稳定 JSON 文档模型，加载时一次性完整校验。
- `ScoringProviderRegistry`：只读查询服务；不解析凭据、不做网络健康检查、不选择最终 Provider、不自动切换。

冻结语义（11D-1a 设计第 7 节与 11D-1b 指令）：
- `credential_ref` 只保存环境变量名称（模型层已校验）。
- `endpoint_profile` 只保存配置引用（模型层已校验）。
- 一致性规则：provider_id+model_id+config_version 唯一；capability 引用必须存在；
  capability 的 provider_id/model_id 必须与 Provider 一致；enabled Provider 不得引用 unknown/missing capability；
  不允许重复 capability key、悬空引用；registry revision 必须为正整数。
- 错误使用稳定错误类型与错误码，不自动修复、不自动创建默认 Provider、查询零副作用。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from models.scoring_provider import (
    ModelCapability,
    ProviderRequest,
    REGISTRY_VERSION,
    ScoringProvider,
    _ENV_NAME_RE,
    _PROFILE_RE,
    build_capability_ref,
    parse_capability_ref,
)

# ---------------- 稳定错误码 ---------------- #

ERR_REGISTRY_MISSING = "SIDECAR_PROVIDER_REGISTRY_MISSING"
ERR_REGISTRY_INVALID_JSON = "SIDECAR_PROVIDER_REGISTRY_INVALID_JSON"
ERR_REGISTRY_UNSUPPORTED_VERSION = "SIDECAR_PROVIDER_REGISTRY_UNSUPPORTED_VERSION"
ERR_DUPLICATE_PROVIDER = "SIDECAR_PROVIDER_DUPLICATE"
ERR_DUPLICATE_CAPABILITY = "SIDECAR_PROVIDER_DUPLICATE_CAPABILITY"
ERR_DANGLING_CAPABILITY_REF = "SIDECAR_PROVIDER_DANGLING_CAPABILITY_REF"
ERR_IDENTITY_MISMATCH = "SIDECAR_PROVIDER_IDENTITY_MISMATCH"
ERR_UNSAFE_CREDENTIAL_REF = "SIDECAR_PROVIDER_UNSAFE_CREDENTIAL_REF"
ERR_UNSAFE_ENDPOINT_PROFILE = "SIDECAR_PROVIDER_UNSAFE_ENDPOINT_PROFILE"
ERR_PROVIDER_NOT_FOUND = "SIDECAR_PROVIDER_NOT_FOUND"
ERR_CAPABILITY_NOT_FOUND = "SIDECAR_PROVIDER_CAPABILITY_NOT_FOUND"
ERR_CAPABILITY_MISMATCH = "SIDECAR_PROVIDER_CAPABILITY_MISMATCH"


class ProviderRegistryError(Exception):
    """注册表稳定错误。"""

    def __init__(self, error_code: str, message_key: str = "", retryable: bool = False):
        self.error_code = error_code
        self.message_key = message_key or error_code
        self.retryable = retryable
        super().__init__(error_code)


# ---------------- 容器模型 ---------------- #


class ProviderRegistryDocument(BaseModel):
    """JSON 注册表稳定容器（11D-1b 指令五）。"""

    model_config = ConfigDict(extra="forbid")

    registry_version: Literal["provider-registry/v1"] = REGISTRY_VERSION
    updated_at: AwareDatetime
    revision: int = Field(gt=0)
    providers: List[ScoringProvider] = Field(default_factory=list)
    capabilities: List[ModelCapability] = Field(default_factory=list)


# ---------------- Registry Service ---------------- #


# 能力筛选支持的能力名 -> capability 字段（三值：true 匹配）
_SUPPORTED_CAPABILITY_FIELDS: Dict[str, str] = {
    "text_input": "text_input",
    "image_input": "image_input",
    "structured_json_output": "structured_json_output",
    "system_message": "system_message",
    "temperature": "temperature_supported",
}


class ScoringProviderRegistry:
    """只读 Provider/Capability 注册表。"""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._doc: Optional[ProviderRegistryDocument] = None

    # ---------- 加载与校验 ---------- #

    def load(self) -> ProviderRegistryDocument:
        if not self._path.exists():
            raise ProviderRegistryError(ERR_REGISTRY_MISSING)
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            raise ProviderRegistryError(ERR_REGISTRY_INVALID_JSON) from exc
        if not isinstance(raw, dict):
            raise ProviderRegistryError(ERR_REGISTRY_INVALID_JSON)
        if raw.get("registry_version") != REGISTRY_VERSION:
            raise ProviderRegistryError(ERR_REGISTRY_UNSUPPORTED_VERSION)
        self._precheck_safety(raw)
        try:
            doc = ProviderRegistryDocument(**raw)
        except ValidationError as exc:
            raise ProviderRegistryError(ERR_REGISTRY_INVALID_JSON, message_key=str(exc.errors()[0]["type"])) from exc
        self._validate_consistency(doc)
        self._doc = doc
        return doc

    def _precheck_safety(self, raw: dict) -> None:
        """pydantic 解析前对 providers 的 credential_ref / endpoint_profile / capability_ref 做安全与身份预检，
        保证不安全引用与身份不一致直接得到稳定错误码（模型层校验仍作为纵深防御保留）。"""
        for p in raw.get("providers", []):
            if not isinstance(p, dict):
                continue
            cred = p.get("credential_ref")
            if not isinstance(cred, str) or not _ENV_NAME_RE.fullmatch(cred):
                raise ProviderRegistryError(
                    ERR_UNSAFE_CREDENTIAL_REF,
                    message_key=f"unsafe credential_ref in provider {p.get('provider_id', '?')}",
                )
            prof = p.get("endpoint_profile")
            if (
                not isinstance(prof, str)
                or not _PROFILE_RE.fullmatch(prof)
                or "://" in prof or "/" in prof or "\\" in prof
            ):
                raise ProviderRegistryError(
                    ERR_UNSAFE_ENDPOINT_PROFILE,
                    message_key=f"unsafe endpoint_profile in provider {p.get('provider_id', '?')}",
                )
            ref = p.get("capability_ref")
            if isinstance(ref, str):
                try:
                    rp, rm, rc = parse_capability_ref(ref)
                except ValueError:
                    continue  # 格式非法由模型层拒绝（INVALID_JSON）
                if (rp, rm, rc) != (p.get("provider_id"), p.get("model_id"), p.get("capability_version")):
                    raise ProviderRegistryError(
                        ERR_IDENTITY_MISMATCH,
                        message_key=f"provider {p.get('provider_id', '?')} capability_ref identity mismatch",
                    )

    def _validate_consistency(self, doc: ProviderRegistryDocument) -> None:
        """一次性完整一致性校验（11D-1b 指令五）。"""
        # 1. provider 唯一性：provider_id + model_id + config_version
        provider_keys: Dict[Tuple[str, str, str], str] = {}
        for p in doc.providers:
            key = (p.provider_id, p.model_id, p.config_version)
            if key in provider_keys:
                raise ProviderRegistryError(ERR_DUPLICATE_PROVIDER, message_key=f"duplicate provider {key}")
            provider_keys[key] = p.provider_id

        # 2. capability 唯一性：provider_id + model_id + capability_version
        capability_keys: Dict[Tuple[str, str, str], str] = {}
        for c in doc.capabilities:
            key = (c.provider_id, c.model_id, c.capability_version)
            if key in capability_keys:
                raise ProviderRegistryError(ERR_DUPLICATE_CAPABILITY, message_key=f"duplicate capability {key}")
            capability_keys[key] = c.capability_version

        # 3. capability 引用必须存在且身份一致（11D-1b-fix-1：通过完整 canonical ref 映射，不用 capability_version；
        #    enabled Provider 允许 verification_source=unknown，unknown 只在能力匹配时不放行）
        cap_by_ref: Dict[str, ModelCapability] = {
            build_capability_ref(c.provider_id, c.model_id, c.capability_version): c for c in doc.capabilities
        }
        for p in doc.providers:
            cap = cap_by_ref.get(p.capability_ref)
            if cap is None:
                raise ProviderRegistryError(
                    ERR_DANGLING_CAPABILITY_REF,
                    message_key=f"provider {p.provider_id} dangling capability_ref {p.capability_ref}",
                )
            if cap.provider_id != p.provider_id or cap.model_id != p.model_id:
                raise ProviderRegistryError(
                    ERR_IDENTITY_MISMATCH,
                    message_key=f"provider {p.provider_id} capability identity mismatch",
                )

        # 4. credential_ref / endpoint_profile 安全校验（模型层已校验，此处防御性复核并给出稳定码）
        for p in doc.providers:
            if "://" in p.credential_ref or "=" in p.credential_ref or "/" in p.credential_ref:
                raise ProviderRegistryError(ERR_UNSAFE_CREDENTIAL_REF, message_key=f"unsafe credential_ref {p.provider_id}")
            if "://" in p.endpoint_profile or "/" in p.endpoint_profile or "\\" in p.endpoint_profile:
                raise ProviderRegistryError(ERR_UNSAFE_ENDPOINT_PROFILE, message_key=f"unsafe endpoint_profile {p.provider_id}")

    # ---------- 只读查询 ---------- #

    def list_providers(self) -> List[ScoringProvider]:
        """稳定排序：provider_id -> model_id -> config_version。"""
        self._require_loaded()
        return sorted(
            self._doc.providers,
            key=lambda p: (p.provider_id, p.model_id, p.config_version),
        )

    def list_capabilities(self) -> List[ModelCapability]:
        self._require_loaded()
        return sorted(
            self._doc.capabilities,
            key=lambda c: (c.provider_id, c.model_id, c.capability_version),
        )

    def get_provider(self, provider_id: str, model_id: Optional[str] = None) -> ScoringProvider:
        """按 provider_id（+可选 model_id）精确定位；缺失显式失败。"""
        self._require_loaded()
        matches = [p for p in self._doc.providers if p.provider_id == provider_id]
        if not matches:
            raise ProviderRegistryError(ERR_PROVIDER_NOT_FOUND, message_key=f"provider {provider_id} not found")
        if model_id is not None:
            exact = [p for p in matches if p.model_id == model_id]
            if not exact:
                raise ProviderRegistryError(ERR_PROVIDER_NOT_FOUND, message_key=f"provider {provider_id} model {model_id} not found")
            matches = exact
        return matches[0]

    def get_capability(self, provider_id: str, model_id: str) -> ModelCapability:
        """按 provider_id + model_id 返回能力记录；缺失显式失败。"""
        self._require_loaded()
        matches = [
            c for c in self._doc.capabilities
            if c.provider_id == provider_id and c.model_id == model_id
        ]
        if not matches:
            raise ProviderRegistryError(ERR_CAPABILITY_NOT_FOUND, message_key=f"capability {provider_id}/{model_id} not found")
        return matches[0]

    def resolve_provider_capability(self, provider_id: str, model_id: str) -> Tuple[ScoringProvider, ModelCapability]:
        """返回 (provider, capability)；任意缺失显式失败。"""
        provider = self.get_provider(provider_id, model_id)
        capability = self.get_capability(provider_id, model_id)
        return provider, capability

    def list_enabled(self) -> List[ScoringProvider]:
        """enabled=true 的 Provider（稳定排序）；未做任何健康/能力筛选。"""
        self._require_loaded()
        return [p for p in self.list_providers() if p.enabled]

    def filter_by_capabilities(self, required: List[str]) -> List[ScoringProvider]:
        """按能力要求筛选候选 Provider（只产生候选，不等于最终选择）。

        - 能力名支持：text_input / image_input / structured_json_output / system_message / temperature。
        - `true` 匹配；`false` 与 `unknown` 不匹配。
        - 未知能力名显式报错（稳定码 ERR_CAPABILITY_MISMATCH）。
        - 不因优先级/顺序绕过能力不足。
        """
        self._require_loaded()
        unknown = [name for name in required if name not in _SUPPORTED_CAPABILITY_FIELDS]
        if unknown:
            raise ProviderRegistryError(ERR_CAPABILITY_MISMATCH, message_key=f"unknown capability requirement {unknown}")
        candidates = []
        for p in self.list_providers():
            if not p.enabled:
                continue
            cap = self.get_capability(p.provider_id, p.model_id)
            ok = True
            for name in required:
                field_name = _SUPPORTED_CAPABILITY_FIELDS[name]
                if getattr(cap, field_name) is not True:
                    ok = False
                    break
            if ok:
                candidates.append(p)
        return candidates

    # ---------- 内部 ---------- #

    def _require_loaded(self) -> None:
        if self._doc is None:
            raise ProviderRegistryError(ERR_REGISTRY_MISSING, message_key="registry not loaded")
