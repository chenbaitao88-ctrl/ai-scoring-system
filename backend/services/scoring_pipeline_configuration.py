"""Non-sensitive configuration lookup for the scoring-pipeline control plane.

The module is inert at import time. Files are read only when ``load`` is
called by the request-scoped dependency assembly.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Literal, Optional

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from models.identity_validation import _validate_ascii_id
from models.score_attempt import ScoringInputProfile


CONFIG_VERSION = "scoring-pipeline-config/v1"
ERR_CONFIG_MISSING = "SCORING_PIPELINE_CONFIG_MISSING"
ERR_CONFIG_INVALID = "SCORING_PIPELINE_CONFIG_INVALID"


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON value: {value}")


class ScoringPipelineConfigurationError(Exception):
    """Stable configuration error without filesystem or parser details."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.message_key = error_code
        self.retryable = False


class ApprovedProviderModelBinding(BaseModel):
    """Approved non-sensitive reference to a provider and model pair."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reference: str
    provider_id: str
    model_id: str

    _safe_fields = field_validator(
        "reference", "provider_id", "model_id", mode="after"
    )(_validate_ascii_id)


class ScoringPipelineConfigurationDocument(BaseModel):
    """Versioned control-plane configuration; no credentials or endpoints."""

    model_config = ConfigDict(extra="forbid")

    config_version: Literal["scoring-pipeline-config/v1"] = CONFIG_VERSION
    updated_at: AwareDatetime
    revision: int = Field(gt=0)
    profiles: List[ScoringInputProfile] = Field(default_factory=list)
    provider_model_bindings: List[ApprovedProviderModelBinding] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def _unique_references(self) -> "ScoringPipelineConfigurationDocument":
        profile_versions = [profile.profile_version for profile in self.profiles]
        binding_refs = [binding.reference for binding in self.provider_model_bindings]
        if len(profile_versions) != len(set(profile_versions)):
            raise ValueError("duplicate profile_version")
        if len(binding_refs) != len(set(binding_refs)):
            raise ValueError("duplicate provider_model_ref")
        return self


class ScoringPipelineConfigurationLookup:
    """Strict read-only lookup loaded explicitly by dependency assembly."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._document: Optional[ScoringPipelineConfigurationDocument] = None

    def load(self) -> ScoringPipelineConfigurationDocument:
        if not self._path.is_file():
            raise ScoringPipelineConfigurationError(ERR_CONFIG_MISSING)
        try:
            raw = json.loads(
                self._path.read_text(encoding="utf-8"),
                parse_constant=_reject_nonfinite,
            )
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise ScoringPipelineConfigurationError(ERR_CONFIG_INVALID) from exc
        if not isinstance(raw, dict) or raw.get("config_version") != CONFIG_VERSION:
            raise ScoringPipelineConfigurationError(ERR_CONFIG_INVALID)
        try:
            document = ScoringPipelineConfigurationDocument.model_validate(raw)
        except ValidationError as exc:
            raise ScoringPipelineConfigurationError(ERR_CONFIG_INVALID) from exc
        self._document = document
        return document

    def _require_loaded(self) -> ScoringPipelineConfigurationDocument:
        if self._document is None:
            raise ScoringPipelineConfigurationError(ERR_CONFIG_MISSING)
        return self._document

    def get_input_profile(self, profile_version: str) -> Optional[ScoringInputProfile]:
        _validate_ascii_id(profile_version)
        for profile in self._require_loaded().profiles:
            if profile.profile_version == profile_version:
                return profile
        return None

    def get_provider_model_binding(
        self, reference: str
    ) -> Optional[ApprovedProviderModelBinding]:
        _validate_ascii_id(reference)
        for binding in self._require_loaded().provider_model_bindings:
            if binding.reference == reference:
                return binding
        return None
