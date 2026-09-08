"""Phase 11F-3d review-case evidence return transitions.

All fixtures are synthetic. The reopen flow must not upload evidence, mutate
attempt facts, or call a scoring Provider.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

import test_scoring_pipeline_execute_integration as base
from models.review_case import ManualFinalLock, ReviewCase, ReviewDecision
from routers import scoring_pipeline
from services.review_case_store import ReviewCaseStore
from services.scoring_pipeline_api_container import ScoringPipelineApiContainer
from services.scoring_task_creator import ScoringTaskCreator

T0 = datetime(2026, 8, 24, 8, 0, 0, tzinfo=timezone.utc)
REVIEWER = {"actor_type": "reviewer", "actor_id": "reviewer-11f-3d"}
SHA = "ab" * 32


def _client(container):
    app = FastAPI()
    app.include_router(scoring_pipeline.router)
    app.dependency_overrides[
        scoring_pipeline.get_scoring_pipeline_api_container
    ] = lambda: container
    return TestClient(app)


def _seed(tmp_path):
    env = base._seed_env(tmp_path)
    review_store = ReviewCaseStore(tmp_path / "reviews", attempt_store=env["attempt_store"])
    env["review_store"] = review_store
    env["orch"]._review_store = review_store
    return env


def _container(env):
    return ScoringPipelineApiContainer(
        task_manager=env["mgr"],
        task_creator=ScoringTaskCreator(env["store"], env["reg"], env["lookup"]),
        profile_lookup=env["lookup"],
        provider_binding_lookup=env["lookup"],
        provider_registry=env["registry"],
        orchestrator=env["orch"],
        review_store=env["review_store"],
        attempt_store=env["attempt_store"],
        test_runtime=True,
    )


def _score_once(env):
    item = base._item(env)
    result = base._run(env["orch"].execute_score_item(
        env["task"].task_id, item.item_id, "exec-11f-3d"))
    assert result.outcome == "succeeded", result.error_code
    item = base._item(env)
    attempt = env["attempt_store"].list_attempts(env["task"].task_id, item.item_id)[-1]
    snapshot = env["attempt_store"].get_snapshot(
        env["task"].task_id, item.item_id, attempt.result_snapshot_ref)
    assert snapshot is not None
    return item, attempt, snapshot


def _open_case(env, item, attempt, snapshot, *, status="in_review", case_id="rev-11f-3d"):
    case = ReviewCase(
        contract_version="score-attempt-review/v1",
        schema_version="review-case/v1",
        review_case_id=case_id,
        case_number=f"RC-{case_id}",
        submission_id=snapshot.submission_id,
        task_id=env["task"].task_id,
        item_id=item.item_id,
        package_id=item.package_id,
        package_revision=item.package_revision,
        attempt_ids=[attempt.attempt_id],
        snapshot_ids=[snapshot.snapshot_id],
        validation_ids=[attempt.validation_ref],
        source_type="manual",
        reason_codes=["EVIDENCE_INSUFFICIENT"],
        priority="high",
        status=status,
        assigned_at=T0 if status in ("assigned", "in_review", "waiting_for_evidence") else None,
        review_started_at=T0 if status in ("in_review", "waiting_for_evidence") else None,
        blocks_auto_adoption=True,
        blocks_export=True,
        opened_at=T0,
        updated_at=T0,
        current_revision=1,
        reopen_count=0,
        idempotency_key=f"idem-{case_id}",
    )
    env["review_store"].create_review_case(env["task"].task_id, item.item_id, case)
    return env["review_store"].get_review_case(env["task"].task_id, item.item_id, case_id)


def _request_payload(case, *, key="idem-evidence-request", requested_revision=2):
    return {
        "idempotency_key": key,
        "review_case_id": case.review_case_id,
        "decision_type": "request_additional_evidence",
        "reason_codes": ["EVIDENCE_INSUFFICIENT"],
        "requested_package_revision": requested_revision,
        "decision_note_code": "REQUEST_ADDITIONAL_EVIDENCE",
    }


def _request_url(env, item):
    return (
        f"/api/scoring-pipeline/tasks/{env['task'].task_id}"
        f"/items/{item.item_id}/review-decisions"
    )


def _reopen_url(env, item, case):
    return (
        f"/api/scoring-pipeline/tasks/{env['task'].task_id}"
        f"/items/{item.item_id}/review-cases/{case.review_case_id}/reopen"
    )


def _create_request_decision_direct(env, item, case, *, key="idem-evidence-request", requested_revision=2):
    decision = ReviewDecision(
        contract_version="score-attempt-review/v1",
        schema_version="review-decision/v1",
        decision_id=f"dec-{key}",
        review_case_id=case.review_case_id,
        case_revision=case.current_revision,
        decision_type="request_additional_evidence",
        reason_codes=["EVIDENCE_INSUFFICIENT"],
        requested_package_revision=requested_revision,
        decision_note_code="REQUEST_ADDITIONAL_EVIDENCE",
        decided_by=REVIEWER,
        decided_at=T0,
        decision_hash=SHA,
        idempotency_key=key,
    )
    env["review_store"].create_review_decision(env["task"].task_id, item.item_id, decision)
    return decision


def test_request_additional_evidence_advances_case(tmp_path):
    env = _seed(tmp_path)
    item, attempt, snapshot = _score_once(env)
    case = _open_case(env, item, attempt, snapshot)

    with _client(_container(env)) as client:
        response = client.post(_request_url(env, item), json=_request_payload(case))

    assert response.status_code == 201, response.text
    decision_id = response.json()["decision_id"]
    saved = env["review_store"].get_review_decision(env["task"].task_id, item.item_id, decision_id)
    assert saved.requested_package_revision == 2
    after = env["review_store"].get_review_case(env["task"].task_id, item.item_id, case.review_case_id)
    assert (after.status, after.current_revision, after.package_revision) == (
        "waiting_for_evidence", 2, 1)
    events = env["review_store"].load_review_events(env["task"].task_id, item.item_id, case.review_case_id)
    assert [event.event_type for event in events] == [
        "CASE_OPENED", "DECISION_RECORDED", "EVIDENCE_REQUESTED"]


def test_request_decision_idempotent_replay_does_not_duplicate(tmp_path):
    env = _seed(tmp_path)
    item, attempt, snapshot = _score_once(env)
    case = _open_case(env, item, attempt, snapshot)
    payload = _request_payload(case)

    with _client(_container(env)) as client:
        first = client.post(_request_url(env, item), json=payload)
        second = client.post(_request_url(env, item), json=payload)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["idempotent"] is True
    decisions = env["review_store"].list_review_decisions(
        env["task"].task_id, item.item_id, case.review_case_id)
    assert len(decisions) == 1
    events = env["review_store"].load_review_events(env["task"].task_id, item.item_id, case.review_case_id)
    assert [event.event_type for event in events].count("EVIDENCE_REQUESTED") == 1


def test_request_replay_recovers_decision_created_before_state_advance(tmp_path):
    env = _seed(tmp_path)
    item, attempt, snapshot = _score_once(env)
    case = _open_case(env, item, attempt, snapshot)
    _create_request_decision_direct(env, item, case)

    before = env["review_store"].get_review_case(env["task"].task_id, item.item_id, case.review_case_id)
    assert before.status == "in_review"
    with _client(_container(env)) as client:
        response = client.post(_request_url(env, item), json=_request_payload(case))

    assert response.status_code == 200, response.text
    after = env["review_store"].get_review_case(env["task"].task_id, item.item_id, case.review_case_id)
    assert (after.status, after.current_revision) == ("waiting_for_evidence", 2)
    assert len(env["review_store"].list_review_decisions(
        env["task"].task_id, item.item_id, case.review_case_id)) == 1


def test_request_additional_evidence_rejects_illegal_start_state(tmp_path):
    env = _seed(tmp_path)
    item, attempt, snapshot = _score_once(env)
    case = _open_case(env, item, attempt, snapshot, status="assigned")

    with _client(_container(env)) as client:
        response = client.post(_request_url(env, item), json=_request_payload(case))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REVIEW_CASE_CONFLICT"
    assert env["review_store"].list_review_decisions(
        env["task"].task_id, item.item_id, case.review_case_id) == []


def test_reopen_conflict_on_expected_revision(tmp_path):
    env = _seed(tmp_path)
    item, attempt, snapshot = _score_once(env)
    case = _open_case(env, item, attempt, snapshot)
    with _client(_container(env)) as client:
        created = client.post(_request_url(env, item), json=_request_payload(case))
        response = client.post(_reopen_url(env, item, case), json={
            "expected_revision": 1,
            "package_revision": 2,
            "request_decision_id": created.json()["decision_id"],
        })

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REVIEW_CASE_CONFLICT"


def test_reopen_blocked_by_active_lock(tmp_path):
    env = _seed(tmp_path)
    item, attempt, snapshot = _score_once(env)
    case = _open_case(env, item, attempt, snapshot)
    with _client(_container(env)) as client:
        created = client.post(_request_url(env, item), json=_request_payload(case))
    decision_id = created.json()["decision_id"]
    env["review_store"].create_final_lock(
        env["task"].task_id,
        item.item_id,
        ManualFinalLock(
            contract_version="score-attempt-review/v1",
            schema_version="manual-final-lock/v1",
            lock_id="flk-11f-3d",
            task_id=env["task"].task_id,
            item_id=item.item_id,
            review_case_id=case.review_case_id,
            decision_id=decision_id,
            adoption_id=None,
            snapshot_id=snapshot.snapshot_id,
            locked_by=REVIEWER,
            locked_at=T0,
            reason_code="HUMAN_FINAL_LOCKED",
            status="active",
            revision=1,
            idempotency_key="idem-flk-11f-3d",
            content_hash=SHA,
        ),
    )

    with _client(_container(env)) as client:
        response = client.post(_reopen_url(env, item, case), json={
            "expected_revision": 2,
            "package_revision": 2,
            "request_decision_id": decision_id,
        })

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CASE_LOCKED"


def test_reopen_updates_package_revision_preserves_old_facts_and_calls_no_provider(tmp_path):
    env = _seed(tmp_path)
    item, attempt, snapshot = _score_once(env)
    case = _open_case(env, item, attempt, snapshot)
    attempt_before = attempt.model_dump(mode="json")
    package_before = env["reg"].read_record_strict(item.package_id, 1).model_dump(mode="json")
    provider_calls_before = len(env["transport"].calls)

    with _client(_container(env)) as client:
        created = client.post(_request_url(env, item), json=_request_payload(case))
        decision_id = created.json()["decision_id"]
        decision_before = env["review_store"].get_review_decision(
            env["task"].task_id, item.item_id, decision_id).model_dump(mode="json")
        response = client.post(_reopen_url(env, item, case), json={
            "expected_revision": 2,
            "package_revision": 2,
            "request_decision_id": decision_id,
        })

    assert response.status_code == 201, response.text
    body = response.json()
    assert body == {
        "review_case_id": case.review_case_id,
        "status": "assigned",
        "package_revision": 2,
        "current_revision": 3,
        "reopen_count": 1,
        "outcome": "reopened",
        "idempotent": False,
    }
    after = env["review_store"].get_review_case(env["task"].task_id, item.item_id, case.review_case_id)
    assert (after.status, after.package_revision, after.reopen_count) == ("assigned", 2, 1)
    assert env["attempt_store"].get_attempt(
        env["task"].task_id, item.item_id, attempt.attempt_id).model_dump(mode="json") == attempt_before
    assert env["review_store"].get_review_decision(
        env["task"].task_id, item.item_id, decision_id).model_dump(mode="json") == decision_before
    assert env["reg"].read_record_strict(item.package_id, 1).model_dump(mode="json") == package_before
    assert base._item(env).package_revision == 1
    assert len(env["transport"].calls) == provider_calls_before
    events = [event.event_type for event in env["review_store"].load_review_events(
        env["task"].task_id, item.item_id, case.review_case_id)]
    assert events == ["CASE_OPENED", "DECISION_RECORDED", "EVIDENCE_REQUESTED", "CASE_REOPENED"]


def test_reopen_idempotent_replay(tmp_path):
    env = _seed(tmp_path)
    item, attempt, snapshot = _score_once(env)
    case = _open_case(env, item, attempt, snapshot)
    with _client(_container(env)) as client:
        created = client.post(_request_url(env, item), json=_request_payload(case))
        payload = {
            "expected_revision": 2,
            "package_revision": 2,
            "request_decision_id": created.json()["decision_id"],
        }
        first = client.post(_reopen_url(env, item, case), json=payload)
        second = client.post(_reopen_url(env, item, case), json=payload)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["idempotent"] is True
    events = [event.event_type for event in env["review_store"].load_review_events(
        env["task"].task_id, item.item_id, case.review_case_id)]
    assert events.count("CASE_REOPENED") == 1


def test_reopen_request_body_extra_fields_rejected(tmp_path):
    env = _seed(tmp_path)
    item, attempt, snapshot = _score_once(env)
    case = _open_case(env, item, attempt, snapshot)
    with _client(_container(env)) as client:
        created = client.post(_request_url(env, item), json=_request_payload(case))
        response = client.post(_reopen_url(env, item, case), json={
            "expected_revision": 2,
            "package_revision": 2,
            "request_decision_id": created.json()["decision_id"],
            "provider": "must-not-be-accepted",
        })

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SCORING_PIPELINE_REQUEST_INVALID"
