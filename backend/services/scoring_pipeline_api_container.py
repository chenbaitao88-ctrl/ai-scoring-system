"""Dependency container for the production scoring-pipeline HTTP API.

This module is intentionally inert at import time: it does not read environment
variables, resolve credentials, open files, or make network calls. Production
assembly remains fail closed until authoritative profiles, provider bindings,
and a credential-backed runtime are explicitly supplied.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from models.identity_validation import sha256_canonical
from models.scoring_configuration import ScoringTaskConfiguration, ScoringTaskCreateRequest
from services.review_case_store import ReviewCaseStore
from services.score_attempt_store import ScoreAttemptStore
from services.manual_adjustment_service import ManualAdjustmentService  # 11F-3b
from services.scoring_pipeline_configuration import (
    ApprovedProviderModelBinding,
    ERR_CONFIG_INVALID,
    ERR_CONFIG_MISSING,
    ScoringPipelineConfigurationError,
    ScoringPipelineConfigurationLookup,
)
from services.scoring_pipeline_orchestrator import ScoringPipelineOrchestrator
from services.scoring_provider_registry import (
    ERR_REGISTRY_MISSING,
    ProviderRegistryError,
    ScoringProviderRegistry,
)
from services.scoring_task_creator import ScoringTaskCreator


ERR_RUNTIME_NOT_CONFIGURED = "SCORING_PIPELINE_RUNTIME_NOT_CONFIGURED"
ERR_PROFILE_NOT_FOUND = "SCORING_PROFILE_NOT_FOUND"
ERR_PROVIDER_BINDING_NOT_FOUND = "SCORING_PROVIDER_BINDING_NOT_FOUND"
ERR_PROVIDER_NOT_APPROVED = "SCORING_PROVIDER_NOT_APPROVED"
ERR_PROVIDER_REGISTRY_MISSING = "SCORING_PROVIDER_REGISTRY_MISSING"
ERR_PROVIDER_REGISTRY_INVALID = "SCORING_PROVIDER_REGISTRY_INVALID"


class ScoringPipelineApiRuntimeError(Exception):
    """Stable configuration/runtime failure exposed without internal details."""

    def __init__(
        self,
        error_code: str,
        *,
        message_key: Optional[str] = None,
        retryable: bool = False,
        status_code: int = 503,
    ) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.message_key = message_key or error_code
        self.retryable = retryable
        self.status_code = status_code


class RegisteredEvidenceLookup:
    """Read-only adapter over the authoritative evidence sidecar."""

    def __init__(self, registration: Any) -> None:
        self._registration = registration

    def _latest_registered_entry(self, package_id: str) -> Optional[Any]:
        index = self._registration.read_index_strict()
        if index is None:
            return None
        entries = [
            entry
            for entry in index.entries
            if entry.package_id == package_id
            and entry.registration_status == "registered"
        ]
        if not entries:
            return None
        return max(entries, key=lambda entry: entry.package_revision)

    def get_evidence_record(self, package_id: str) -> Optional[Any]:
        entry = self._latest_registered_entry(package_id)
        if entry is None:
            return None
        return self._registration.read_record_strict(
            entry.package_id, entry.package_revision
        )

    def get_validation_result(self, package_id: str) -> Optional[Any]:
        record = self.get_evidence_record(package_id)
        if record is None:
            return None
        return self._registration.read_validation_strict(
            record.package_id,
            record.package_revision,
            record.latest_validation_id,
        )


@dataclass
class ScoringPipelineApiContainer:
    """Explicit API dependencies; absent mutation/runtime capabilities fail closed."""

    task_manager: Any
    task_creator: Optional[Any] = None
    profile_lookup: Optional[Any] = None
    provider_binding_lookup: Optional[Any] = None
    provider_registry: Optional[Any] = None
    orchestrator: Optional[Any] = None
    batch_orchestrator: Optional[Any] = None
    credential_resolver: Optional[Any] = None
    review_store: Optional[Any] = None  # 11F-1b：复核控制面只读依赖
    attempt_store: Optional[Any] = None  # 11F-2c：复核详情只读依赖
    adjustment_service: Optional[Any] = None  # 11F-3b：人工调整服务
    execute_runtime_ready: bool = False
    test_runtime: bool = False
    control_plane_error: Optional[ScoringPipelineApiRuntimeError] = None

    def ensure_control_plane_configuration(self) -> None:
        if self.control_plane_error is not None:
            raise self.control_plane_error

    def build_create_request(
        self,
        *,
        batch_id: str,
        source_task_id: str,
        source_item_ids: list[str],
        profile_version: str,
        provider_model_ref: str,
        concurrency: int,
    ) -> ScoringTaskCreateRequest:
        self.ensure_control_plane_configuration()
        if (
            self.task_creator is None
            or self.profile_lookup is None
            or self.provider_binding_lookup is None
            or self.provider_registry is None
        ):
            raise ScoringPipelineApiRuntimeError(ERR_RUNTIME_NOT_CONFIGURED)

        profile = self.profile_lookup.get_input_profile(profile_version)
        if profile is None:
            raise ScoringPipelineApiRuntimeError(
                ERR_PROFILE_NOT_FOUND, status_code=422
            )

        binding = self.provider_binding_lookup.get_provider_model_binding(provider_model_ref)
        if binding is None:
            raise ScoringPipelineApiRuntimeError(
                ERR_PROVIDER_BINDING_NOT_FOUND, status_code=422
            )

        try:
            provider, _ = self.provider_registry.resolve_provider_capability(
                binding.provider_id, binding.model_id
            )
        except Exception as exc:
            raise ScoringPipelineApiRuntimeError(
                ERR_PROVIDER_NOT_APPROVED, status_code=422
            ) from exc
        if not provider.enabled or provider.deprecation_status == "retired":
            raise ScoringPipelineApiRuntimeError(
                ERR_PROVIDER_NOT_APPROVED, status_code=422
            )

        fields = {
            "profile_version": profile.profile_version,
            "provider_id": binding.provider_id,
            "model_id": binding.model_id,
            "scoring_policy_version": profile.scoring_policy_version,
            "rubric_version": profile.rubric_version,
            "prompt_version": profile.prompt_version,
            "response_schema_version": profile.response_schema_version,
        }
        configuration = ScoringTaskConfiguration(
            **fields,
            configuration_fingerprint=sha256_canonical(fields),
        )
        return ScoringTaskCreateRequest(
            batch_id=batch_id,
            source_task_id=source_task_id,
            source_item_ids=source_item_ids,
            configuration=configuration,
            concurrency=concurrency,
        )

    def require_orchestrator(self) -> Any:
        if self.orchestrator is None:
            raise ScoringPipelineApiRuntimeError(ERR_RUNTIME_NOT_CONFIGURED)
        return self.orchestrator

    def require_dry_run_orchestrator(self) -> Any:
        self.ensure_control_plane_configuration()
        return self.require_orchestrator()

    def require_execute_runtime(self) -> Any:
        if (
            not self.execute_runtime_ready
            or self.batch_orchestrator is None
            or self.credential_resolver is None
        ):
            raise ScoringPipelineApiRuntimeError(ERR_RUNTIME_NOT_CONFIGURED)
        return self.batch_orchestrator

    def require_review_store(self) -> Any:
        """11F-1b：复核控制面（队列查询/决策创建）只读依赖。"""
        if self.review_store is None:
            raise ScoringPipelineApiRuntimeError(ERR_RUNTIME_NOT_CONFIGURED)
        return self.review_store

    def require_attempt_store(self) -> Any:
        """11F-2c：复核详情（评分历史查询）只读依赖。"""
        if self.attempt_store is None:
            raise ScoringPipelineApiRuntimeError(ERR_RUNTIME_NOT_CONFIGURED)
        return self.attempt_store


def _configuration_runtime_error(
    exc: Exception,
) -> ScoringPipelineApiRuntimeError:
    if isinstance(exc, ScoringPipelineConfigurationError):
        code = (
            ERR_CONFIG_MISSING
            if exc.error_code == ERR_CONFIG_MISSING
            else ERR_CONFIG_INVALID
        )
        return ScoringPipelineApiRuntimeError(code, status_code=422)
    if isinstance(exc, ProviderRegistryError):
        code = (
            ERR_PROVIDER_REGISTRY_MISSING
            if exc.error_code == ERR_REGISTRY_MISSING
            else ERR_PROVIDER_REGISTRY_INVALID
        )
        return ScoringPipelineApiRuntimeError(code, status_code=422)
    return ScoringPipelineApiRuntimeError(ERR_CONFIG_INVALID, status_code=422)


def assemble_non_provider_scoring_api_container(
    *,
    task_manager: Any,
    config_dir: Path,
) -> ScoringPipelineApiContainer:
    """Assemble request-scoped control-plane services without execution runtime."""

    store = task_manager.store
    registration = task_manager.reg
    runtime_root = Path(store.root)

    profile_lookup = ScoringPipelineConfigurationLookup(
        Path(config_dir) / "scoring_pipeline_config.json"
    )
    provider_registry = ScoringProviderRegistry(
        Path(config_dir) / "provider_registry.json"
    )
    control_plane_error: Optional[ScoringPipelineApiRuntimeError] = None
    try:
        profile_lookup.load()
        provider_registry.load()
    except (ScoringPipelineConfigurationError, ProviderRegistryError) as exc:
        control_plane_error = _configuration_runtime_error(exc)

    attempt_store = ScoreAttemptStore(runtime_root / "score-attempts")
    review_store = ReviewCaseStore(
        runtime_root / "reviews", attempt_store=attempt_store
    )
    orchestrator = ScoringPipelineOrchestrator(
        task_item_lookup=task_manager,
        evidence_lookup=RegisteredEvidenceLookup(registration),
        attempt_lookup=attempt_store,
        success_lookup=attempt_store,
        review_case_lookup=review_store,
        profile_lookup=profile_lookup,
        registry=provider_registry,
        attempt_store=attempt_store,
        item_operator=task_manager,
        review_store=review_store,
    )
    task_creator = ScoringTaskCreator(
        store=store,
        registration=registration,
        profile_lookup=profile_lookup,
    )
    return ScoringPipelineApiContainer(
        task_manager=task_manager,
        task_creator=task_creator,
        profile_lookup=profile_lookup,
        provider_binding_lookup=profile_lookup,
        provider_registry=provider_registry,
        orchestrator=orchestrator,
        batch_orchestrator=None,
        credential_resolver=None,
        review_store=review_store,  # 11F-1b
        attempt_store=attempt_store,  # 11F-2c
        adjustment_service=ManualAdjustmentService(review_store, attempt_store),  # 11F-3b
        execute_runtime_ready=False,
        test_runtime=False,
        control_plane_error=control_plane_error,
    )
