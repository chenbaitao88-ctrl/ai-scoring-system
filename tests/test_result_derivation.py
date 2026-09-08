"""Phase 11F-3e-1: authoritative result derivation synthetic tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import test_review_case_store as rc
import test_score_attempt_store as sa
from models.review_case import ManualFinalLock
from services.result_derivation_service import (
    EXPORT_BLOCKED_BY_REVIEW,
    ResultDerivationError,
    ResultDerivationService,
)
from services.review_case_store import ReviewCaseStore
from services.score_attempt_store import ScoreAttemptStore


@pytest.fixture
def env(tmp_path):
    attempts = ScoreAttemptStore(tmp_path / "attempts")
    attempt = sa.make_attempt(status="succeeded")
    attempts.create_attempt(rc.TASK, rc.ITEM, attempt)
    attempts.write_snapshot(rc.TASK, rc.ITEM, sa.make_snapshot())
    attempts.write_validation(rc.TASK, rc.ITEM, sa.make_validation())
    reviews = ReviewCaseStore(tmp_path / "reviews", attempt_store=attempts)
    return attempts, reviews, ResultDerivationService(reviews, attempts, tmp_path / "derived")


def seed_case(reviews, status="open"):
    reviews.create_review_case(rc.TASK, rc.ITEM, rc.make_case(status=status))


def seed_adoption(
    reviews,
    attempts,
    adoption_id=rc.ADP,
    snapshot_id=sa.SNAPSHOT_ID,
    validation_id=sa.VALIDATION_ID,
    **overrides,
):
    reviews.adopt_result(
        rc.TASK, rc.ITEM,
        rc.make_adoption(
            adoption_id=adoption_id,
            snapshot_id=snapshot_id,
            validation_id=validation_id,
            **overrides,
        ),
    )


def seed_lock(reviews, adoption_id=rc.ADP, snapshot_id=sa.SNAPSHOT_ID, **overrides):
    reviews.create_review_decision(
        rc.TASK, rc.ITEM,
        rc.make_decision(
            decision_type="adopt_existing_attempt",
            target_attempt_id=sa.ATTEMPT_ID,
            target_snapshot_id=sa.SNAPSHOT_ID,
            idempotency_key="idem-result-derivation-lock",
        ),
    )
    case = reviews.get_review_case(rc.TASK, rc.ITEM, rc.REV)
    if case is not None and case.status == "open":
        reviews.transition_review_case(
            rc.TASK, rc.ITEM, rc.REV,
            expected_revision=1,
            to_status="in_review",
            event_type="REVIEW_STARTED",
            actor=rc.REVIEWER_REF,
            occurred_at=rc.T1,
        )
        reviews.transition_review_case(
            rc.TASK, rc.ITEM, rc.REV,
            expected_revision=2,
            to_status="resolved",
            event_type="CASE_RESOLVED",
            actor=rc.REVIEWER_REF,
            occurred_at=rc.T2,
            resolution_decision_id=rc.DEC,
        )
    reviews.create_final_lock(
        rc.TASK, rc.ITEM,
        rc.make_lock(adoption_id=adoption_id, snapshot_id=snapshot_id, **overrides),
    )


def test_active_lock_has_priority_over_active_adoption(env):
    attempts, reviews, service = env
    seed_adoption(reviews, attempts)
    seed_case(reviews)
    seed_lock(reviews)
    result = service.derive(rc.TASK, rc.ITEM)
    assert result.authority_type == "manual_final_lock"
    assert result.lock_id == "flk_demo_001"
    assert result.adoption_id == rc.ADP
    assert result.snapshot_id == sa.SNAPSHOT_ID


def test_active_adoption_uses_original_snapshot(env):
    attempts, reviews, service = env
    seed_adoption(reviews, attempts)
    result = service.derive(rc.TASK, rc.ITEM)
    assert result.authority_type == "result_adoption"
    assert result.snapshot_id == sa.SNAPSHOT_ID
    assert result.total_score == 82.0


def test_active_adoption_uses_adjusted_snapshot(env):
    attempts, reviews, service = env
    adjusted_id = "snap_adjusted_001"
    attempts.write_snapshot(
        rc.TASK, rc.ITEM,
        sa.make_snapshot(snapshot_id=adjusted_id, total_score=85.0),
    )
    attempts.write_validation(
        rc.TASK, rc.ITEM,
        sa.make_validation(validation_id="val_adjusted_001", snapshot_id=adjusted_id),
    )
    seed_adoption(
        reviews,
        attempts,
        snapshot_id=adjusted_id,
        validation_id="val_adjusted_001",
    )
    result = service.derive(rc.TASK, rc.ITEM)
    assert result.snapshot_id == adjusted_id
    assert result.total_score == 85.0


def test_no_lock_or_adoption_is_blocked(env):
    _, _, service = env
    with pytest.raises(ResultDerivationError) as exc:
        service.derive(rc.TASK, rc.ITEM)
    assert exc.value.error_code == EXPORT_BLOCKED_BY_REVIEW
    assert {reason["code"] for reason in exc.value.reasons} == {"NO_AUTHORITATIVE_RESULT"}


@pytest.mark.parametrize("status", ["open", "assigned", "in_review", "waiting_for_evidence"])
def test_open_export_blocking_case_blocks(status, env):
    attempts, reviews, service = env
    seed_adoption(reviews, attempts)
    seed_case(reviews, status=status)
    with pytest.raises(ResultDerivationError) as exc:
        service.derive(rc.TASK, rc.ITEM)
    assert exc.value.error_code == EXPORT_BLOCKED_BY_REVIEW
    assert any(r["code"] == "UNRESOLVED_REVIEW_CASE" for r in exc.value.reasons)


def test_resolved_nonblocking_case_does_not_block(env):
    attempts, reviews, service = env
    seed_case(reviews, status="resolved")
    seed_adoption(reviews, attempts)
    result = service.derive(rc.TASK, rc.ITEM)
    assert result.snapshot_id == sa.SNAPSHOT_ID


def test_missing_adoption_snapshot_is_blocked(env):
    attempts, reviews, service = env
    # Bypass create binding only to model an already-corrupt sidecar fact.
    seed_adoption(reviews, attempts)
    path = reviews.root / "tasks" / rc.TASK / "items" / rc.ITEM / "adoptions" / f"{rc.ADP}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["snapshot_id"] = "snap_missing_001"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ResultDerivationError) as exc:
        service.derive(rc.TASK, rc.ITEM)
    assert exc.value.error_code == EXPORT_BLOCKED_BY_REVIEW
    assert any(r["code"] == "SNAPSHOT_MISSING_OR_CORRUPTED" for r in exc.value.reasons)


def test_corrupt_adoption_snapshot_is_blocked(env):
    attempts, reviews, service = env
    seed_adoption(reviews, attempts)
    snap_path = attempts.root / "tasks" / rc.TASK / "items" / rc.ITEM / "snapshots" / f"{sa.SNAPSHOT_ID}.json"
    snap_path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(ResultDerivationError) as exc:
        service.derive(rc.TASK, rc.ITEM)
    assert exc.value.error_code == EXPORT_BLOCKED_BY_REVIEW
    assert any(r["code"] == "SNAPSHOT_MISSING_OR_CORRUPTED" for r in exc.value.reasons)


def test_lock_snapshot_missing_is_blocked(env):
    attempts, reviews, service = env
    seed_adoption(reviews, attempts)
    seed_case(reviews)
    seed_lock(reviews, snapshot_id="snap_missing_001")
    with pytest.raises(ResultDerivationError) as exc:
        service.derive(rc.TASK, rc.ITEM)
    assert exc.value.error_code == EXPORT_BLOCKED_BY_REVIEW


def test_task_item_snapshot_binding_conflict_is_blocked(env):
    attempts, reviews, service = env
    seed_adoption(reviews, attempts)
    snap_path = attempts.root / "tasks" / rc.TASK / "items" / rc.ITEM / "snapshots" / f"{sa.SNAPSHOT_ID}.json"
    payload = json.loads(snap_path.read_text(encoding="utf-8"))
    payload["submission_id"] = "sub_other_001"
    snap_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ResultDerivationError) as exc:
        service.derive(rc.TASK, rc.ITEM)
    assert exc.value.error_code == EXPORT_BLOCKED_BY_REVIEW


def test_replay_is_idempotent_and_does_not_duplicate_records(env):
    attempts, reviews, service = env
    seed_adoption(reviews, attempts)
    first = service.derive(rc.TASK, rc.ITEM)
    second = service.derive(rc.TASK, rc.ITEM)
    assert second.to_dict() == first.to_dict()
    files = list((service.root / "tasks" / rc.TASK / "items" / rc.ITEM).glob("*.json"))
    assert len(files) == 1


def test_changed_authority_input_creates_new_history_record(env):
    attempts, reviews, service = env
    seed_adoption(reviews, attempts)
    first = service.derive(rc.TASK, rc.ITEM)
    adjusted_id = "snap_adjusted_002"
    attempts.write_snapshot(rc.TASK, rc.ITEM, sa.make_snapshot(snapshot_id=adjusted_id, total_score=84.0))
    attempts.write_validation(
        rc.TASK, rc.ITEM,
        sa.make_validation(validation_id="val_adjusted_002", snapshot_id=adjusted_id),
    )
    reviews.adopt_result(
        rc.TASK, rc.ITEM,
        rc.make_adoption(
            adoption_id=rc.ADP2,
            snapshot_id=adjusted_id,
            validation_id="val_adjusted_002",
            supersedes_adoption_id=rc.ADP,
        ),
    )
    second = service.derive(rc.TASK, rc.ITEM)
    assert second.derivation_id != first.derivation_id
    assert second.snapshot_id == adjusted_id
    history = list((service.root / "tasks" / rc.TASK / "items" / rc.ITEM).glob("*.json"))
    assert {p.name for p in history} == {f"{first.derivation_id}.json", f"{second.derivation_id}.json"}
    assert json.loads((service.root / "tasks" / rc.TASK / "items" / rc.ITEM / f"{first.derivation_id}.json").read_text(encoding="utf-8"))["snapshot_id"] == sa.SNAPSHOT_ID


def test_response_is_redacted_and_does_not_expose_sensitive_snapshot_fields(env):
    attempts, reviews, service = env
    seed_adoption(reviews, attempts)
    result = service.derive(rc.TASK, rc.ITEM)
    encoded = json.dumps(result.to_dict(), ensure_ascii=False)
    for forbidden in ("evidence_refs", "rationale_summary", "provider_id", "model_id", "prompt_version", "evidence_manifest_sha256"):
        assert forbidden not in encoded
    assert "demo rationale" not in encoded
    assert "total_score" in result.to_dict()["final_result"]


def test_zero_provider_and_database_writes(env):
    attempts, reviews, service = env
    seed_adoption(reviews, attempts)
    calls = []
    class NoProvider:
        def __getattr__(self, name):
            calls.append(name)
            raise AssertionError("provider must not be called")
    service._provider = NoProvider()
    result = service.derive(rc.TASK, rc.ITEM)
    assert result.snapshot_id == sa.SNAPSHOT_ID
    assert calls == []
    assert not any(path.suffix in (".db", ".sqlite") for path in Path(env[1].root).rglob("*"))


def test_lock_and_adoption_binding_conflict_is_blocked(env):
    attempts, reviews, service = env
    seed_adoption(reviews, attempts)
    seed_case(reviews)
    seed_lock(reviews, snapshot_id=sa.SNAPSHOT_ID, adoption_id="adp_other_001")
    with pytest.raises(ResultDerivationError) as exc:
        service.derive(rc.TASK, rc.ITEM)
    assert exc.value.error_code == EXPORT_BLOCKED_BY_REVIEW
    assert any(r["code"] == "LOCK_ADOPTION_BINDING_MISMATCH" for r in exc.value.reasons)
