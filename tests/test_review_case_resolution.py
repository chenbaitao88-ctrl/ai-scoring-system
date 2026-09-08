"""Restricted resolution of synthetic cases; no Provider or real work data."""
import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timezone
from threading import Event
from types import SimpleNamespace

import pytest

import test_review_case_store as rc
import test_score_attempt_store as sa
from test_authoritative_file_export import env, TASK, ITEM
from services.review_case_resolution_service import ReviewCaseResolutionService, ReviewResolutionError
from services.review_case_store import ReviewCaseStore
from services.score_attempt_store import ScoreAttemptStore
from services.manual_adjustment_service import ManualAdjustmentService


@pytest.fixture
def resolution_env(env):
    return seed_resolution(env)


def seed_resolution(env):
    env.reviews.create_review_case(TASK, ITEM, rc.make_case(status="in_review"))
    env.reviews.create_review_decision(TASK, ITEM, rc.make_decision(decision_type="adopt_existing_attempt"))
    env.reviews.create_adoption(TASK, ITEM, rc.make_adoption(
        review_case_id=rc.REV, decision_id=rc.DEC, decided_by=rc.REVIEWER))
    env.resolution = ReviewCaseResolutionService(env.reviews, env.attempts)
    return env


def mount_resolution_api(e):
    from routers import scoring_pipeline
    e.manager.get_item = lambda task_id, item_id: e.items[0] if (task_id, item_id) == (TASK, ITEM) else None
    container = SimpleNamespace(task_manager=e.manager, require_review_store=lambda: e.reviews,
                                require_attempt_store=lambda: e.attempts)
    e.client.app.include_router(scoring_pipeline.router)
    e.client.app.dependency_overrides[scoring_pipeline.get_scoring_pipeline_api_container] = lambda: container
    return f"/api/scoring-pipeline/tasks/{TASK}/items/{ITEM}/review-cases/{rc.REV}/resolve"


def resolve(env, **overrides):
    args = dict(expected_revision=1, expected_adoption_id=rc.ADP,
                actor=rc.REVIEWER_REF, occurred_at=datetime.now(timezone.utc))
    args.update(overrides)
    return env.resolution.resolve(TASK, ITEM, rc.REV, **args)


def facts(env):
    return {str(p.relative_to(env.root)): p.read_bytes() for p in env.root.rglob("*") if p.is_file()}


def rewrite(env, directory, object_id, **changes):
    root = env.attempts.root if directory in ("attempts", "snapshots", "validations") else env.reviews.root
    path = root / "tasks" / TASK / "items" / ITEM / directory / f"{object_id}.json"
    content = json.loads(path.read_text())
    content.update(changes)
    path.write_text(json.dumps(content))


@pytest.mark.parametrize("locked", [False, True])
def test_resolution_preserves_authority_and_records_one_event(resolution_env, locked):
    e = resolution_env
    if locked:
        e.reviews.create_final_lock(TASK, ITEM, rc.make_lock(task_id=TASK, item_id=ITEM))
    original_attempts = {p: b for p, b in facts(e).items() if p.startswith("attempts/")}
    adoption = e.reviews.get_active_adoption(TASK, ITEM)
    lock = e.reviews.get_active_final_lock(TASK, ITEM)
    result = resolve(e)
    assert result.status == "resolved" and result.current_revision == 2
    assert result.resolution_decision_id == rc.DEC and result.blocks_export is True
    assert e.reviews.get_active_adoption(TASK, ITEM) == adoption
    assert e.reviews.get_active_final_lock(TASK, ITEM) == lock
    assert {p: b for p, b in facts(e).items() if p.startswith("attempts/")} == original_attempts
    events = [event for event in e.reviews.load_review_events(TASK, ITEM) if event.event_type == "CASE_RESOLVED"]
    assert len(events) == 1 and events[0].case_revision_after == 2


@pytest.mark.parametrize("failure,code", [
    ("stale_revision", "REVIEW_CASE_CONFLICT"),
    ("stale_adoption", "REVIEW_RESOLUTION_ADOPTION_CHANGED"),
    ("revoked_adoption", "REVIEW_RESOLUTION_ADOPTION_REQUIRED"),
    ("wrong_case", "REVIEW_RESOLUTION_BINDING_MISMATCH"),
    ("wrong_decision", "REVIEW_RESOLUTION_BINDING_MISMATCH"),
    ("stale_decision", "REVIEW_RESOLUTION_DECISION_STALE"),
    ("wrong_snapshot", "REVIEW_RESOLUTION_SCORE_FACT_INVALID"),
    ("failed_validation", "REVIEW_RESOLUTION_SCORE_FACT_INVALID"),
    ("corrupt_snapshot", "REVIEW_RESOLUTION_SCORE_FACT_INVALID"),
    ("failed_attempt", "REVIEW_RESOLUTION_SCORE_FACT_INVALID"),
    ("wrong_lock", "REVIEW_RESOLUTION_BINDING_MISMATCH"),
])
def test_invalid_resolution_has_no_fact_or_event_side_effect(resolution_env, failure, code):
    e = resolution_env
    args = {}
    if failure == "stale_revision":
        args["expected_revision"] = 9
    elif failure == "stale_adoption":
        args["expected_adoption_id"] = "adoption-stale"
    elif failure == "revoked_adoption":
        e.reviews.transition_adoption(TASK, ITEM, rc.ADP, to_status="revoked", actor=rc.REVIEWER_REF, occurred_at=rc.T2)
    elif failure == "wrong_case":
        rewrite(e, "adoptions", rc.ADP, review_case_id=rc.REV2)
    elif failure == "wrong_decision":
        rewrite(e, "decisions", rc.DEC, review_case_id=rc.REV2)
    elif failure == "stale_decision":
        rewrite(e, "decisions", rc.DEC, case_revision=2)
    elif failure == "wrong_snapshot":
        rewrite(e, "adoptions", rc.ADP, snapshot_id="snapshot-missing")
    elif failure == "failed_validation":
        rewrite(e, "validations", sa.VALIDATION_ID, overall_status="failed", adoption_candidate=False)
    elif failure == "corrupt_snapshot":
        path = e.attempts.root / "tasks" / TASK / "items" / ITEM / "snapshots" / f"{sa.SNAPSHOT_ID}.json"
        path.write_text("{broken")
    elif failure == "failed_attempt":
        rewrite(e, "attempts", sa.ATTEMPT_ID, status="failed")
    elif failure == "wrong_lock":
        e.reviews.create_final_lock(TASK, ITEM, rc.make_lock(task_id=TASK, item_id=ITEM))
        rewrite(e, "final-locks", "flk_demo_001", adoption_id="different-adoption")
    before = facts(e)
    with pytest.raises(ReviewResolutionError) as caught:
        resolve(e, **args)
    assert caught.value.error_code == code
    assert facts(e) == before


def test_closed_case_cannot_be_resolved_again(resolution_env):
    e = resolution_env
    resolve(e)
    before = facts(e)
    with pytest.raises(ReviewResolutionError) as caught:
        resolve(e, expected_revision=2)
    assert caught.value.error_code == "REVIEW_CASE_CONFLICT"
    assert facts(e) == before


def test_store_reopen_and_case_reopen_restore_export_block(resolution_env):
    e = resolution_env
    resolve(e)
    attempts = ScoreAttemptStore(e.attempts.root)
    reviews = ReviewCaseStore(e.reviews.root, attempt_store=attempts)
    case = reviews.get_review_case(TASK, ITEM, rc.REV)
    assert case.status == "resolved" and case.resolution_decision_id == rc.DEC
    assert e.client.get(f"/api/result-export/tasks/{TASK}/export").status_code == 200
    reopened = reviews.transition_review_case(
        TASK, ITEM, rc.REV, expected_revision=2, to_status="open", event_type="CASE_REOPENED",
        actor=rc.REVIEWER_REF, occurred_at=datetime.now(timezone.utc))
    assert reopened.previous_resolution_decision_id == rc.DEC
    assert e.client.get(f"/api/result-export/tasks/{TASK}/export").status_code == 409


def test_adoption_changed_before_locked_guard_is_rejected(resolution_env, monkeypatch):
    e = resolution_env
    original = e.reviews.transition_review_case
    def changed(*args, **kwargs):
        e.reviews.adopt_result(TASK, ITEM, rc.make_adoption(
            adoption_id=rc.ADP2, supersedes_adoption_id=rc.ADP,
            review_case_id=rc.REV, decision_id=rc.DEC, decided_by=rc.REVIEWER))
        return original(*args, **kwargs)
    monkeypatch.setattr(e.reviews, "transition_review_case", changed)
    with pytest.raises(ReviewResolutionError) as caught:
        resolve(e)
    assert caught.value.error_code == "REVIEW_RESOLUTION_ADOPTION_CHANGED"
    assert e.reviews.get_review_case(TASK, ITEM, rc.REV).current_revision == 1
    assert not any(event.event_type == "CASE_RESOLVED" for event in e.reviews.load_review_events(TASK, ITEM))


@pytest.mark.parametrize("directory,object_id,field", [
    ("cases", rc.REV, "review_case_id"),
    ("decisions", rc.DEC, "decision_id"),
    ("snapshots", sa.SNAPSHOT_ID, "snapshot_id"),
    ("validations", sa.VALIDATION_ID, "validation_id"),
    ("attempts", sa.ATTEMPT_ID, "attempt_id"),
])
def test_valid_json_with_misplaced_fact_id_is_rejected(resolution_env, directory, object_id, field):
    e = resolution_env
    rewrite(e, directory, object_id, **{field: "misplaced-fact-id"})
    before = facts(e)
    with pytest.raises(ReviewResolutionError):
        resolve(e)
    assert facts(e) == before


def test_actual_manual_adjustment_can_be_resolved(resolution_env):
    e = resolution_env
    e.reviews.create_review_decision(TASK, ITEM, rc.make_decision(
        decision_id=rc.DEC2, decision_type="apply_manual_adjustment",
        manual_adjustment_id="adj-resolution-adjust", target_snapshot_id=sa.SNAPSHOT_ID))
    result, error = ManualAdjustmentService(e.reviews, e.attempts).build_and_apply(
        TASK, ITEM, rc.REV, rc.DEC2, "resolution-adjust",
        [{"dimension_code": "objective", "before_value": 58, "after_value": 60,
          "change_reason_code": "TOTAL_RECALCULATED"}], ["LOW_CONFIDENCE"], "CORRECTED_SCORE")
    assert error is None
    adoption = e.reviews.get_active_adoption(TASK, ITEM)
    assert adoption.snapshot_id == result["adjusted_snapshot_id"]
    resolved = resolve(e, expected_adoption_id=adoption.adoption_id)
    assert resolved.resolution_decision_id == rc.DEC2
    assert e.derivation.derive(TASK, ITEM).total_score == 84


def test_review_fact_change_waits_for_resolution_guard(resolution_env, monkeypatch):
    e = resolution_env
    entered, release, changing = Event(), Event(), Event()
    original = e.attempts.get_snapshot
    def paused(*args):
        entered.set()
        assert release.wait(3), "test did not release resolution guard"
        return original(*args)
    monkeypatch.setattr(e.attempts, "get_snapshot", paused)
    def revoke():
        changing.set()
        return e.reviews.transition_adoption(TASK, ITEM, rc.ADP, to_status="revoked",
                                            actor=rc.REVIEWER_REF, occurred_at=rc.T3)
    with ThreadPoolExecutor(max_workers=2) as pool:
        resolving = pool.submit(resolve, e)
        try:
            assert entered.wait(3)
            revoking = pool.submit(revoke)
            assert changing.wait(3)
            with pytest.raises(TimeoutError):
                revoking.result(timeout=0.05)
        finally:
            release.set()
        assert resolving.result(timeout=3).status == "resolved"
        assert revoking.result(timeout=3).status == "revoked"
