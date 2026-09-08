"""Phase 11E-4a scoring-pipeline HTTP API tests.

All fixtures are synthetic and use an explicitly injected mock transport. The
formal application dependency remains fail closed.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
import test_scoring_pipeline_execute_integration as base
from models.pipeline_task import PipelineError
from routers import scoring_pipeline
from services.pipeline_task_store import PipelineTaskStore
from services.scoring_batch_orchestrator import ScoringBatchOrchestrator
from services.scoring_pipeline_api_container import (
    ApprovedProviderModelBinding,
    ScoringPipelineApiContainer,
    ScoringPipelineApiRuntimeError,
)
from services.scoring_pipeline_configuration import (
    ERR_CONFIG_INVALID,
    ERR_CONFIG_MISSING,
    ScoringPipelineConfigurationError,
    ScoringPipelineConfigurationLookup,
)
from services.scoring_pipeline_orchestrator import (
    ReviewApplicationResult,
    ScoreRecoveryResult,
)
from services.scoring_task_creator import ScoringTaskCreator


class BindingLookup:
    def __init__(self, reference="approved-demo-v1"):
        self.reference = reference

    def get_provider_model_binding(self, reference):
        if reference != self.reference:
            return None
        return ApprovedProviderModelBinding(
            reference=reference,
            provider_id=base.PID,
            model_id=base.MID,
        )


class ApiOrchestratorProxy:
    def __init__(self, delegate, *, recovery_outcome=None, review_outcome=None):
        self.delegate = delegate
        self.recovery_outcome = recovery_outcome
        self.review_outcome = review_outcome
        self.recovery_calls = 0
        self.review_calls = 0

    def dry_run(self, *args, **kwargs):
        return self.delegate.dry_run(*args, **kwargs)

    def recover_score_item(self, task_id, item_id, recovery_request_id):
        self.recovery_calls += 1
        if self.recovery_outcome is None:
            return self.delegate.recover_score_item(task_id, item_id, recovery_request_id)
        outcome, error_code = self.recovery_outcome
        return ScoreRecoveryResult(
            recovery_request_id=recovery_request_id,
            task_id=task_id,
            item_id=item_id,
            outcome=outcome,
            error_code=error_code,
            provider_called=False,
        )

    def apply_review_decision(
        self,
        task_id,
        item_id,
        decision_id,
        adoption_request_id,
        *,
        competition_id,
    ):
        self.review_calls += 1
        outcome, error_code = self.review_outcome or ("not_adopted", None)
        return ReviewApplicationResult(
            adoption_request_id=adoption_request_id,
            task_id=task_id,
            item_id=item_id,
            decision_id=decision_id,
            outcome=outcome,
            adoption_id="adoption-demo" if outcome == "adopted" else None,
            review_decision_id=decision_id if outcome == "adopted" else None,
            error_code=error_code,
        )


def _container(tmp_path, *, item_count=2, execute_ready=True, proxy=None):
    env = base._seed_env(tmp_path, item_count=item_count)
    creator = ScoringTaskCreator(
        store=env["store"],
        registration=env["reg"],
        profile_lookup=env["lookup"],
    )
    orchestrator = proxy or env["orch"]
    container = ScoringPipelineApiContainer(
        task_manager=env["mgr"],
        task_creator=creator,
        profile_lookup=env["lookup"],
        provider_binding_lookup=BindingLookup(),
        provider_registry=env["registry"],
        orchestrator=orchestrator,
        batch_orchestrator=ScoringBatchOrchestrator(env["orch"], env["orch"]._task_item),
        credential_resolver=base.FakeCredentialResolver(),
        execute_runtime_ready=execute_ready,
        test_runtime=True,
    )
    return env, container


def _client(container):
    app = FastAPI()
    app.include_router(scoring_pipeline.router)
    app.dependency_overrides[scoring_pipeline.get_scoring_pipeline_api_container] = lambda: container
    return TestClient(app)


def _write_empty_formal_config(config_dir: Path) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "scoring_pipeline_config.json").write_text(
        json.dumps(
            {
                "config_version": "scoring-pipeline-config/v1",
                "updated_at": "2026-08-20T00:00:00Z",
                "revision": 1,
                "profiles": [],
                "provider_model_bindings": [],
            }
        ),
        encoding="utf-8",
    )
    (config_dir / "provider_registry.json").write_text(
        json.dumps(
            {
                "registry_version": "provider-registry/v1",
                "updated_at": "2026-08-20T00:00:00Z",
                "revision": 1,
                "providers": [],
                "capabilities": [],
            }
        ),
        encoding="utf-8",
    )


def _copy_task_facts(source_store, target_store, task_id, *, last_error=None):
    task = source_store.load_task(task_id)
    target_store.write_task_snapshot(task_id, task, expected_revision=None)
    for item in source_store.list_items(task_id):
        if last_error is not None and item.task_type == "scoring_pipeline":
            item = item.model_copy(update={"last_error": last_error})
        target_store.write_item_snapshot(task_id, item, expected_revision=None)
    return task


def _formal_client(monkeypatch, tmp_path, *, item_error=None, config_setup=True):
    seed = base._seed_env(tmp_path / "seed", item_count=1)
    data_dir = tmp_path / "formal-data"
    formal_store = PipelineTaskStore(data_dir / "pipeline-runtime")
    _copy_task_facts(
        seed["store"], formal_store, seed["task"].source_task_id
    )
    task = _copy_task_facts(
        seed["store"],
        formal_store,
        seed["task"].task_id,
        last_error=item_error,
    )
    config_dir = tmp_path / "formal-config"
    if config_setup:
        _write_empty_formal_config(config_dir)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("SCORING_DATA_DIR", str(data_dir))
    monkeypatch.setattr(
        scoring_pipeline, "SCORING_PIPELINE_CONFIG_DIR", config_dir
    )
    app = FastAPI()
    app.include_router(scoring_pipeline.router)
    return seed, task, formal_store, config_dir, TestClient(app)


def _create_body(env, *, batch_id="api-created-batch", **updates):
    body = {
        "source_task_id": env["task"].source_task_id,
        "source_item_ids": [entry.source_item_id for entry in env["task"].item_index],
        "batch_id": batch_id,
        "profile_version": base.PROFILE_V,
        "provider_model_ref": "approved-demo-v1",
        "concurrency": 2,
    }
    body.update(updates)
    return body


def _assert_safe_error(response, status, code=None):
    assert response.status_code == status
    error = response.json()["error"]
    assert set(error) == {"code", "message_key", "retryable", "request_id"}
    if code:
        assert error["code"] == code
    text = response.text.lower()
    for forbidden in ("traceback", "c:\\", "api_key", "secret", "prompt", "endpoint"):
        assert forbidden not in text


def test_create_task_and_idempotent_replay(tmp_path):
    env, container = _container(tmp_path)
    with _client(container) as client:
        first = client.post("/api/scoring-pipeline/tasks", json=_create_body(env))
        assert first.status_code == 201
        assert first.json()["idempotent_hit"] is False
        second = client.post("/api/scoring-pipeline/tasks", json=_create_body(env))
        assert second.status_code == 200
        assert second.json()["idempotent_hit"] is True
        assert second.json()["task_id"] == first.json()["task_id"]


def test_create_rejects_missing_source_and_unknown_binding(tmp_path):
    env, container = _container(tmp_path)
    with _client(container) as client:
        missing = client.post(
            "/api/scoring-pipeline/tasks",
            json=_create_body(env, source_task_id=str(uuid4())),
        )
        _assert_safe_error(missing, 404)
        binding = client.post(
            "/api/scoring-pipeline/tasks",
            json=_create_body(env, provider_model_ref="not-approved"),
        )
        _assert_safe_error(binding, 422, "SCORING_PROVIDER_BINDING_NOT_FOUND")


@pytest.mark.parametrize(
    "forbidden_field,value",
    [
        ("provider_id", "provider-injected"),
        ("model_id", "model-injected"),
        ("prompt", "hidden prompt"),
        ("rubric", {"score": 100}),
        ("score", 99),
        ("attempt_id", "attempt-injected"),
    ],
)
def test_create_forbids_authoritative_content_injection(tmp_path, forbidden_field, value):
    env, container = _container(tmp_path)
    body = _create_body(env)
    body[forbidden_field] = value
    with _client(container) as client:
        response = client.post("/api/scoring-pipeline/tasks", json=body)
    _assert_safe_error(response, 400, "SCORING_PIPELINE_REQUEST_INVALID")


def test_safe_task_and_item_queries(tmp_path):
    env, container = _container(tmp_path)
    task_id = env["task"].task_id
    with _client(container) as client:
        task = client.get(f"/api/scoring-pipeline/tasks/{task_id}")
        assert task.status_code == 200
        assert set(task.json()) == {
            "task_id", "source_task_id", "batch_id", "status", "current_stage",
            "revision", "concurrency", "counts",
        }
        items = client.get(f"/api/scoring-pipeline/tasks/{task_id}/items")
        assert items.status_code == 200
        assert items.json()["total"] == 2
        assert all("latest_error_code" in item for item in items.json()["items"])
        assert all(item["latest_error_code"] is None for item in items.json()["items"])
        serialized = items.text.lower()
        for forbidden in ("manifest_sha256", "input_fingerprint", "prompt", "provider", "model"):
            assert forbidden not in serialized


def test_get_missing_task_returns_safe_404(tmp_path):
    _, container = _container(tmp_path)
    with _client(container) as client:
        response = client.get(f"/api/scoring-pipeline/tasks/{uuid4()}")
    _assert_safe_error(response, 404, "SCORING_PIPELINE_TASK_NOT_FOUND")


def test_dry_run_returns_only_safe_decision_and_checks(tmp_path):
    env, container = _container(tmp_path)
    task = env["task"]
    item_id = task.item_index[0].item_id
    before_calls = len(env["transport"].calls)
    with _client(container) as client:
        response = client.post(
            f"/api/scoring-pipeline/tasks/{task.task_id}/items/{item_id}/dry-run",
            json={"request_id": "dry-api-001"},
        )
    assert response.status_code == 200
    assert response.json()["decision"] == "ready"
    assert len(env["transport"].calls) == before_calls
    serialized = response.text.lower()
    for forbidden in ("provider_id", "model_id", "prompt_version", "manifest_sha256"):
        assert forbidden not in serialized


def test_execute_fails_closed_without_runtime(tmp_path):
    env, container = _container(tmp_path, execute_ready=False)
    with _client(container) as client:
        response = client.post(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/execute",
            json={"batch_execution_id": "batch-runtime-missing"},
        )
    _assert_safe_error(response, 503, "SCORING_PIPELINE_RUNTIME_NOT_CONFIGURED")
    assert len(env["transport"].calls) == 0


def test_execute_with_explicit_mock_runtime_succeeds(tmp_path):
    env, container = _container(tmp_path, item_count=3, execute_ready=True)
    with _client(container) as client:
        response = client.post(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/execute",
            json={"batch_execution_id": "batch-api-mock", "concurrency": 2},
        )
    assert response.status_code == 200
    result = response.json()
    assert result["succeeded"] == 3
    assert result["started"] == 3
    assert len(env["transport"].calls) == 3


def test_execute_rejects_concurrency_above_frozen_limit(tmp_path):
    env, container = _container(tmp_path)
    with _client(container) as client:
        response = client.post(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/execute",
            json={"batch_execution_id": "batch-too-wide", "concurrency": 3},
        )
    _assert_safe_error(response, 409, "SCORING_CONCURRENCY_EXCEEDS_FROZEN_LIMIT")
    assert len(env["transport"].calls) == 0


@pytest.mark.parametrize(
    "outcome,error_code",
    [
        ("stale_precall_reset", "RECOVERY_STALE_PRECALL_RESET"),
        ("retry_approval_required", "RECOVERY_RETRY_APPROVAL_REQUIRED"),
        ("outcome_unknown_blocked", "RECOVERY_OUTCOME_UNKNOWN_BLOCKED"),
    ],
)
def test_recover_returns_controlled_outcome_without_provider_call(tmp_path, outcome, error_code):
    env, container = _container(tmp_path, item_count=1)
    proxy = ApiOrchestratorProxy(
        env["orch"], recovery_outcome=(outcome, error_code)
    )
    container.orchestrator = proxy
    task = env["task"]
    item_id = task.item_index[0].item_id
    before_calls = len(env["transport"].calls)
    with _client(container) as client:
        response = client.post(
            f"/api/scoring-pipeline/tasks/{task.task_id}/items/{item_id}/recover",
            json={"recovery_request_id": f"recover-{outcome}"},
        )
    assert response.status_code == 200
    assert response.json()["outcome"] == outcome
    assert response.json()["provider_called"] is False
    assert len(env["transport"].calls) == before_calls


@pytest.mark.parametrize("outcome", ["adopted", "not_adopted"])
def test_apply_existing_review_decision_outcomes(tmp_path, outcome):
    env, container = _container(tmp_path, item_count=1)
    proxy = ApiOrchestratorProxy(env["orch"], review_outcome=(outcome, None))
    container.orchestrator = proxy
    task = env["task"]
    item_id = task.item_index[0].item_id
    with _client(container) as client:
        response = client.post(
            f"/api/scoring-pipeline/tasks/{task.task_id}/items/{item_id}/review-decisions/decision-existing/apply",
            json={"adoption_request_id": f"apply-{outcome}", "competition_id": "competition-demo"},
        )
    assert response.status_code == 200
    assert response.json()["outcome"] == outcome
    assert proxy.review_calls == 1


def test_apply_missing_review_decision_returns_404(tmp_path):
    env, container = _container(tmp_path, item_count=1)
    proxy = ApiOrchestratorProxy(
        env["orch"], review_outcome=("blocked", "REVIEW_CASE_NOT_FOUND")
    )
    container.orchestrator = proxy
    task = env["task"]
    item_id = task.item_index[0].item_id
    with _client(container) as client:
        response = client.post(
            f"/api/scoring-pipeline/tasks/{task.task_id}/items/{item_id}/review-decisions/missing/apply",
            json={"adoption_request_id": "apply-missing", "competition_id": "competition-demo"},
        )
    _assert_safe_error(response, 404, "REVIEW_CASE_NOT_FOUND")


def test_unknown_exception_does_not_leak_internal_text(tmp_path):
    class BrokenManager:
        def get_task(self, _):
            raise RuntimeError("INTERNAL_DETAIL_SHOULD_NOT_LEAK")

    container = ScoringPipelineApiContainer(task_manager=BrokenManager())
    with _client(container) as client:
        response = client.get(f"/api/scoring-pipeline/tasks/{uuid4()}")
    _assert_safe_error(response, 500, "SCORING_PIPELINE_INTERNAL_ERROR")
    assert "internal_detail_should_not_leak" not in response.text.lower()


def test_default_container_assembles_control_plane_without_mock(tmp_path):
    env = base._seed_env(tmp_path)
    container = scoring_pipeline.get_scoring_pipeline_api_container(env["mgr"])
    assert container.task_manager is env["mgr"]
    assert container.task_creator is not None
    assert container.orchestrator is not None
    assert container.test_runtime is False
    assert container.execute_runtime_ready is False
    with pytest.raises(ScoringPipelineApiRuntimeError):
        container.require_execute_runtime()


def test_formal_routes_share_pipeline_runtime_and_expose_safe_error_code(
    monkeypatch, tmp_path
):
    safe_error = PipelineError(
        error_code="PROVIDER_TIMEOUT",
        message_key="SENSITIVE_INTERNAL_MESSAGE",
        retryable=True,
        stage="score",
    )
    _, task, formal_store, _, client = _formal_client(
        monkeypatch, tmp_path, item_error=safe_error
    )
    assert formal_store.root == tmp_path / "formal-data" / "pipeline-runtime"
    with client:
        task_response = client.get(f"/api/scoring-pipeline/tasks/{task.task_id}")
        items_response = client.get(
            f"/api/scoring-pipeline/tasks/{task.task_id}/items"
        )
    assert task_response.status_code == 200
    assert items_response.status_code == 200
    item = items_response.json()["items"][0]
    assert item["latest_error_code"] == "PROVIDER_TIMEOUT"
    assert "SENSITIVE_INTERNAL_MESSAGE" not in items_response.text


def test_formal_create_unknown_configuration_is_422_not_runtime_503(
    monkeypatch, tmp_path
):
    _, task, _, _, client = _formal_client(monkeypatch, tmp_path)
    body = {
        "source_task_id": task.source_task_id,
        "source_item_ids": [task.item_index[0].source_item_id],
        "batch_id": "formal-create",
        "profile_version": "not-configured",
        "provider_model_ref": "not-configured",
        "concurrency": 1,
    }
    with client:
        response = client.post("/api/scoring-pipeline/tasks", json=body)
    _assert_safe_error(response, 422, "SCORING_PROFILE_NOT_FOUND")


def test_formal_dry_run_is_business_blocked_without_runtime_503(
    monkeypatch, tmp_path
):
    _, task, _, _, client = _formal_client(monkeypatch, tmp_path)
    item_id = task.item_index[0].item_id
    with client:
        response = client.post(
            f"/api/scoring-pipeline/tasks/{task.task_id}/items/{item_id}/dry-run",
            json={"request_id": "formal-dry-run"},
        )
    assert response.status_code == 200
    assert response.json()["decision"] == "blocked"
    assert response.json()["blocking_error_code"] != "SCORING_PIPELINE_RUNTIME_NOT_CONFIGURED"


def test_formal_recover_and_review_apply_do_not_require_credentials(
    monkeypatch, tmp_path
):
    _, task, _, _, client = _formal_client(monkeypatch, tmp_path)
    item_id = task.item_index[0].item_id
    with client:
        recovery = client.post(
            f"/api/scoring-pipeline/tasks/{task.task_id}/items/{item_id}/recover",
            json={"recovery_request_id": "formal-recovery"},
        )
        review = client.post(
            f"/api/scoring-pipeline/tasks/{task.task_id}/items/{item_id}/review-decisions/missing/apply",
            json={
                "adoption_request_id": "formal-adoption",
                "competition_id": "competition-safe",
            },
        )
    assert recovery.status_code == 200
    assert recovery.json()["provider_called"] is False
    assert review.status_code in (404, 409)
    assert recovery.status_code != 503
    assert review.status_code != 503


def test_formal_execute_remains_fail_closed_without_provider_call(
    monkeypatch, tmp_path
):
    _, task, _, _, client = _formal_client(monkeypatch, tmp_path)
    with client:
        response = client.post(
            f"/api/scoring-pipeline/tasks/{task.task_id}/execute",
            json={"batch_execution_id": "formal-execute"},
        )
    _assert_safe_error(response, 503, "SCORING_PIPELINE_RUNTIME_NOT_CONFIGURED")


@pytest.mark.parametrize(
    "content,expected_code",
    [
        (None, ERR_CONFIG_MISSING),
        ("{not-json", ERR_CONFIG_INVALID),
        ("NaN", ERR_CONFIG_INVALID),
    ],
)
def test_configuration_lookup_missing_and_corrupt_are_stable(
    tmp_path, content, expected_code
):
    path = tmp_path / "scoring_pipeline_config.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    lookup = ScoringPipelineConfigurationLookup(path)
    with pytest.raises(ScoringPipelineConfigurationError) as exc_info:
        lookup.load()
    assert exc_info.value.error_code == expected_code


def test_formal_corrupt_configuration_is_422_for_create_and_dry_run(
    monkeypatch, tmp_path
):
    _, task, _, config_dir, client = _formal_client(monkeypatch, tmp_path)
    (config_dir / "scoring_pipeline_config.json").write_text(
        "{not-json", encoding="utf-8"
    )
    item_id = task.item_index[0].item_id
    with client:
        create = client.post(
            "/api/scoring-pipeline/tasks",
            json={
                "source_task_id": task.source_task_id,
                "source_item_ids": [task.item_index[0].source_item_id],
                "batch_id": "corrupt-config",
                "profile_version": "profile-v1",
                "provider_model_ref": "binding-v1",
                "concurrency": 1,
            },
        )
        dry_run = client.post(
            f"/api/scoring-pipeline/tasks/{task.task_id}/items/{item_id}/dry-run",
            json={"request_id": "corrupt-dry-run"},
        )
    _assert_safe_error(create, 422, "SCORING_PIPELINE_CONFIG_INVALID")
    _assert_safe_error(dry_run, 422, "SCORING_PIPELINE_CONFIG_INVALID")


def test_formal_missing_configuration_is_stable_422(monkeypatch, tmp_path):
    _, task, _, _, client = _formal_client(
        monkeypatch, tmp_path, config_setup=False
    )
    with client:
        response = client.post(
            "/api/scoring-pipeline/tasks",
            json={
                "source_task_id": task.source_task_id,
                "source_item_ids": [task.item_index[0].source_item_id],
                "batch_id": "missing-config",
                "profile_version": "profile-v1",
                "provider_model_ref": "binding-v1",
                "concurrency": 1,
            },
        )
    _assert_safe_error(response, 422, "SCORING_PIPELINE_CONFIG_MISSING")


def test_formal_corrupt_provider_registry_is_stable_422(monkeypatch, tmp_path):
    _, task, _, config_dir, client = _formal_client(monkeypatch, tmp_path)
    (config_dir / "provider_registry.json").write_text(
        "{not-json", encoding="utf-8"
    )
    with client:
        response = client.post(
            "/api/scoring-pipeline/tasks",
            json={
                "source_task_id": task.source_task_id,
                "source_item_ids": [task.item_index[0].source_item_id],
                "batch_id": "corrupt-registry",
                "profile_version": "profile-v1",
                "provider_model_ref": "binding-v1",
                "concurrency": 1,
            },
        )
    _assert_safe_error(response, 422, "SCORING_PROVIDER_REGISTRY_INVALID")


def test_api_modules_have_no_import_time_env_or_network_access():
    route_source = inspect.getsource(scoring_pipeline)
    container_source = Path(
        inspect.getsourcefile(ScoringPipelineApiContainer)
    ).read_text(encoding="utf-8")
    configuration_source = Path(
        inspect.getsourcefile(ScoringPipelineConfigurationLookup)
    ).read_text(encoding="utf-8")
    combined = (route_source + container_source + configuration_source).lower()
    for forbidden in ("os.getenv", "os.environ", "urlopen(", "requests.", "httpx.", ".env"):
        assert forbidden not in combined


def test_openapi_exposes_only_minimal_pipeline_routes():
    paths = main.app.openapi()["paths"]
    expected = {
        "/api/scoring-pipeline/tasks",
        "/api/scoring-pipeline/tasks/{task_id}",
        "/api/scoring-pipeline/tasks/{task_id}/items",
        "/api/scoring-pipeline/tasks/{task_id}/items/{item_id}/dry-run",
        "/api/scoring-pipeline/tasks/{task_id}/execute",
        "/api/scoring-pipeline/tasks/{task_id}/items/{item_id}/recover",
        "/api/scoring-pipeline/tasks/{task_id}/items/{item_id}/review-decisions/{decision_id}/apply",
    }
    assert expected <= set(paths)
    create_schema = paths["/api/scoring-pipeline/tasks"]["post"]["requestBody"]
    assert create_schema
    item_schema = main.app.openapi()["components"]["schemas"]["ScoringItemSummaryResponse"]
    assert "latest_error_code" in item_schema["properties"]


def test_api_does_not_import_or_write_legacy_score_models():
    sources = inspect.getsource(scoring_pipeline) + inspect.getsource(ScoringPipelineApiContainer)
    for forbidden in ("MachineScore", "HumanScore", "FinalScore", "SessionLocal", "database"):
        assert forbidden not in sources
