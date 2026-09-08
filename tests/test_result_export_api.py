"""Phase 11F-3e-2: authoritative result export readiness API synthetic tests."""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import test_result_derivation as rd
import test_review_case_store as rc
import test_score_attempt_store as sa
from routers import result_export
from services.result_derivation_service import ResultDerivationService
from services.review_case_store import ReviewCaseStore
from services.score_attempt_store import ScoreAttemptStore

DERIVE_URL = "/api/result-export/tasks/{task_id}/items/{item_id}/derive"


@pytest.fixture
def env(tmp_path):
    attempts = ScoreAttemptStore(tmp_path / "attempts")
    attempts.create_attempt(rc.TASK, rc.ITEM, sa.make_attempt(status="succeeded"))
    attempts.write_snapshot(rc.TASK, rc.ITEM, sa.make_snapshot())
    attempts.write_validation(rc.TASK, rc.ITEM, sa.make_validation())
    reviews = ReviewCaseStore(tmp_path / "reviews", attempt_store=attempts)
    service = ResultDerivationService(reviews, attempts, tmp_path / "derived")
    app = FastAPI()
    app.include_router(result_export.router)
    app.dependency_overrides[
        result_export.get_result_derivation_service
    ] = lambda: service
    with TestClient(app) as client:
        yield attempts, reviews, service, client
    app.dependency_overrides.clear()


def _url(task_id=rc.TASK, item_id=rc.ITEM):
    return DERIVE_URL.format(task_id=task_id, item_id=item_id)


def test_active_adoption_derives_successfully(env):
    _, reviews, _, client = env
    rd.seed_adoption(reviews, env[0])
    resp = client.post(_url())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_id"] == rc.TASK
    assert body["item_id"] == rc.ITEM
    assert body["exportable"] is True
    assert body["authority_type"] == "result_adoption"
    assert body["derivation_id"].startswith("der-")
    result = body["result"]
    assert result["snapshot_id"] == sa.SNAPSHOT_ID
    assert result["attempt_id"] == sa.ATTEMPT_ID
    assert result["total_score"] == 82.0
    assert "objective_score" in result
    assert "subjective_score" in result
    assert isinstance(result["dimension_scores"], list)


def test_active_lock_has_priority_over_adoption(env):
    attempts, reviews, _, client = env
    rd.seed_adoption(reviews, attempts)
    rd.seed_case(reviews)
    rd.seed_lock(reviews)
    resp = client.post(_url())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["authority_type"] == "manual_final_lock"
    assert body["result"]["snapshot_id"] == sa.SNAPSHOT_ID


@pytest.mark.parametrize("status", ["open", "assigned", "in_review", "waiting_for_evidence"])
def test_unresolved_blocking_case_returns_409(status, env):
    attempts, reviews, _, client = env
    rd.seed_adoption(reviews, attempts)
    rd.seed_case(reviews, status=status)
    resp = client.post(_url())
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["exportable"] is False
    assert body["error"]["code"] == "EXPORT_BLOCKED_BY_REVIEW"
    assert any(
        reason["code"] == "UNRESOLVED_REVIEW_CASE"
        for reason in body["error"]["reasons"]
    )


def test_no_authoritative_result_returns_409(env):
    _, _, _, client = env
    resp = client.post(_url())
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["exportable"] is False
    assert body["error"]["code"] == "EXPORT_BLOCKED_BY_REVIEW"
    assert any(
        reason["code"] == "NO_AUTHORITATIVE_RESULT"
        for reason in body["error"]["reasons"]
    )


def test_missing_snapshot_returns_409(env):
    attempts, reviews, _, client = env
    rd.seed_adoption(reviews, attempts)
    path = reviews.root / "tasks" / rc.TASK / "items" / rc.ITEM / "adoptions" / f"{rc.ADP}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["snapshot_id"] = "snap_missing_001"
    path.write_text(json.dumps(payload), encoding="utf-8")
    resp = client.post(_url())
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "EXPORT_BLOCKED_BY_REVIEW"


def test_corrupted_snapshot_returns_409(env):
    attempts, reviews, _, client = env
    rd.seed_adoption(reviews, attempts)
    snap_path = attempts.root / "tasks" / rc.TASK / "items" / rc.ITEM / "snapshots" / f"{sa.SNAPSHOT_ID}.json"
    snap_path.write_text("{not-json", encoding="utf-8")
    resp = client.post(_url())
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"]["code"] == "EXPORT_BLOCKED_BY_REVIEW"
    assert any(
        reason["code"] == "SNAPSHOT_MISSING_OR_CORRUPTED"
        for reason in body["error"]["reasons"]
    )


@pytest.mark.parametrize(
    "task_id,item_id",
    [
        ("task$bad", rc.ITEM),
        (rc.TASK, "item bad"),
        ("con", rc.ITEM),
        (rc.TASK, "nul."),
    ],
)
def test_invalid_task_or_item_id_returns_400(task_id, item_id, env):
    _, _, _, client = env
    resp = client.post(_url(task_id=task_id, item_id=item_id))
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["exportable"] is False
    assert body["error"]["code"] == "RESULT_EXPORT_REQUEST_INVALID"


def test_repeated_calls_return_same_derivation_id(env):
    attempts, reviews, service, client = env
    rd.seed_adoption(reviews, attempts)
    first = client.post(_url())
    second = client.post(_url())
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["derivation_id"] == second.json()["derivation_id"]
    records = list((service.root / "tasks" / rc.TASK / "items" / rc.ITEM).glob("*.json"))
    assert len(records) == 1


def test_success_response_excludes_sensitive_and_internal_fields(env, tmp_path):
    attempts, reviews, _, client = env
    rd.seed_adoption(reviews, attempts)
    resp = client.post(_url())
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {
        "task_id", "item_id", "exportable", "authority_type", "derivation_id", "result",
    }
    assert set(body["result"].keys()) == {
        "snapshot_id", "attempt_id", "submission_id",
        "total_score", "objective_score", "subjective_score", "dimension_scores",
    }
    text = resp.text
    for forbidden in (
        "input_fingerprint",
        "prompt",
        "provider",
        "evidence",
        "rationale",
        "demo rationale",
        "derived_at",
        str(tmp_path),
    ):
        assert forbidden not in text, f"leaked: {forbidden}"


def test_error_response_excludes_internal_details(env):
    _, _, _, client = env
    resp = client.post(_url())
    assert resp.status_code == 409
    text = resp.text
    for forbidden in ("Traceback", "\\", "stack", "exception"):
        assert forbidden not in text, f"leaked: {forbidden}"


def test_main_app_registers_route_before_spa_catch_all():
    from main import app as main_app

    paths = [getattr(route, "path", None) for route in main_app.routes]
    target = "/api/result-export/tasks/{task_id}/items/{item_id}/derive"
    assert target in paths
    target_route = next(
        route for route in main_app.routes if getattr(route, "path", None) == target
    )
    assert "POST" in target_route.methods
    assert paths.index(target) < paths.index("/{path:path}")
