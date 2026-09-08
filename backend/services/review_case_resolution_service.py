"""Explicit case resolution; never adopts, unlocks, calls a model or exports."""
from datetime import datetime

from models.review_case import ActorRef, ReviewCase
from services.review_case_store import ReviewCaseStore, ReviewFactStoreError
from services.score_attempt_store import ScoreAttemptStore, ScoreFactStoreError


class ReviewResolutionError(Exception):
    def __init__(self, error_code: str, status_code: int = 409):
        super().__init__(error_code)
        self.error_code = error_code
        self.status_code = status_code


class ReviewCaseResolutionService:
    def __init__(self, review_store: ReviewCaseStore, attempt_store: ScoreAttemptStore):
        self._reviews = review_store
        self._attempts = attempt_store

    def resolve(self, task_id: str, item_id: str, case_id: str, *,
                expected_revision: int, expected_adoption_id: str,
                actor: ActorRef, occurred_at: datetime) -> ReviewCase:
        def guard(case: ReviewCase) -> str:
            if case.review_case_id != case_id:
                raise ReviewResolutionError("REVIEW_RESOLUTION_BINDING_MISMATCH")
            return self._validate_authority(task_id, item_id, case, expected_adoption_id)

        try:
            return self._reviews.transition_review_case(
                task_id, item_id, case_id, expected_revision=expected_revision,
                to_status="resolved", event_type="CASE_RESOLVED", actor=actor,
                occurred_at=occurred_at, _resolution_guard=guard,
            )
        except ReviewFactStoreError as exc:
            raise ReviewResolutionError(
                exc.error_code, 404 if "NOT_FOUND" in exc.error_code else 409) from exc
        except ScoreFactStoreError as exc:
            raise ReviewResolutionError("REVIEW_RESOLUTION_SCORE_FACT_INVALID") from exc

    def _validate_authority(self, task_id: str, item_id: str, case: ReviewCase,
                            expected_adoption_id: str) -> str:
        # Runs inside the review-store item lock. Attempt facts are separately
        # stored; this is not a cross-store transaction or a new adoption check.
        mismatch = "REVIEW_RESOLUTION_BINDING_MISMATCH"
        invalid_score = "REVIEW_RESOLUTION_SCORE_FACT_INVALID"
        if case.task_id != task_id or case.item_id != item_id:
            raise ReviewResolutionError(mismatch)
        adoption = self._reviews.get_active_adoption(task_id, item_id)
        if adoption is None:
            raise ReviewResolutionError("REVIEW_RESOLUTION_ADOPTION_REQUIRED")
        if adoption.adoption_id != expected_adoption_id:
            raise ReviewResolutionError("REVIEW_RESOLUTION_ADOPTION_CHANGED")
        if (adoption.review_case_id != case.review_case_id
                or adoption.submission_id != case.submission_id
                or adoption.decision_id is None
                or adoption.decided_by.actor_type != "reviewer"):
            raise ReviewResolutionError(mismatch)
        decision = self._reviews.get_review_decision(task_id, item_id, adoption.decision_id)
        if decision is None:
            raise ReviewResolutionError("REVIEW_DECISION_NOT_FOUND", 404)
        if (decision.decision_id != adoption.decision_id
                or decision.review_case_id != case.review_case_id
                or decision.decided_by.actor_type != "reviewer"):
            raise ReviewResolutionError(mismatch)
        if (decision.case_revision != case.current_revision
                or any(d.supersedes_decision_id == decision.decision_id for d in
                       self._reviews.list_review_decisions(task_id, item_id, case.review_case_id))):
            raise ReviewResolutionError("REVIEW_RESOLUTION_DECISION_STALE")

        snapshot = self._attempts.get_snapshot(task_id, item_id, adoption.snapshot_id)
        validation = self._attempts.get_validation(task_id, item_id, adoption.validation_id)
        if (snapshot is None or validation is None
                or snapshot.snapshot_id != adoption.snapshot_id
                or validation.validation_id != adoption.validation_id):
            raise ReviewResolutionError(invalid_score)
        attempt = self._attempts.get_attempt(task_id, item_id, snapshot.attempt_id)
        if (attempt is None or attempt.status != "succeeded" or attempt.attempt_id != snapshot.attempt_id
                or attempt.task_id != task_id or attempt.item_id != item_id
                or attempt.submission_id != case.submission_id
                or attempt.package_id != case.package_id
                or attempt.package_revision != case.package_revision
                or adoption.attempt_id != snapshot.attempt_id
                or snapshot.submission_id != case.submission_id
                or snapshot.package_id != attempt.package_id
                or snapshot.evidence_manifest_sha256 != attempt.evidence_manifest_sha256
                or snapshot.scoring_policy_version != attempt.scoring_policy_version
                or snapshot.rubric_version != attempt.rubric_version
                or snapshot.response_schema_version != attempt.response_schema_version
                or adoption.adoption_scope.scoring_policy_version != snapshot.scoring_policy_version
                or validation.attempt_id != snapshot.attempt_id
                or validation.snapshot_id != snapshot.snapshot_id
                or validation.overall_status == "failed"
                or snapshot.structure_validation_status != "passed"
                or snapshot.score_range_validation_status != "passed"):
            raise ReviewResolutionError(invalid_score)
        self._attempts.verify_fact_bindings(task_id, item_id, attempt.attempt_id)

        if decision.decision_type == "adopt_existing_attempt":
            if (decision.target_attempt_id != snapshot.attempt_id
                    or decision.target_snapshot_id != snapshot.snapshot_id):
                raise ReviewResolutionError(mismatch)
        elif decision.decision_type == "apply_manual_adjustment":
            adjustment = self._reviews.get_manual_adjustment(task_id, item_id, decision.manual_adjustment_id)
            if (adjustment is None or adjustment.adjustment_id != decision.manual_adjustment_id
                    or adjustment.decision_id != decision.decision_id
                    or adjustment.review_case_id != case.review_case_id
                    or adjustment.submission_id != case.submission_id
                    or adjustment.base_attempt_id != snapshot.attempt_id
                    or adjustment.adjusted_snapshot_id != snapshot.snapshot_id
                    or (decision.target_snapshot_id is not None
                        and decision.target_snapshot_id != adjustment.base_snapshot_id)):
                raise ReviewResolutionError(mismatch)
        else:
            raise ReviewResolutionError(mismatch)

        lock = self._reviews.get_active_final_lock(task_id, item_id)
        if lock is not None and (
            lock.task_id != task_id or lock.item_id != item_id
            or lock.review_case_id != case.review_case_id
            or lock.decision_id != decision.decision_id
            or lock.adoption_id != adoption.adoption_id
            or lock.snapshot_id != snapshot.snapshot_id
        ):
            raise ReviewResolutionError(mismatch)
        return decision.decision_id
