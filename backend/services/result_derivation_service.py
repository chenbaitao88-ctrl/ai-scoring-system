"""Authoritative result derivation for the Phase 11 export boundary.

This service only reads ReviewCase/ResultAdoption/ManualFinalLock and
ScoreResultSnapshot facts.  It never calls a Provider, writes a database, or
changes an upstream fact.  Derived records are immutable sidecar facts.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from services.review_case_store import ReviewFactStoreError
from services.score_attempt_store import (
    ScoreFactStoreError,
    _atomic_write_bytes,
    _canonical_bytes,
)


EXPORT_BLOCKED_BY_REVIEW = "EXPORT_BLOCKED_BY_REVIEW"
OPEN_EXPORT_BLOCKING_STATUSES = frozenset(
    {"open", "assigned", "in_review", "waiting_for_evidence"}
)


class ResultDerivationError(Exception):
    """Stable, redacted failure from the result derivation boundary."""

    def __init__(self, error_code: str, reasons: Sequence[Dict[str, Any]]) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.reasons = tuple(dict(reason) for reason in reasons)

    def __str__(self) -> str:
        return self.error_code

    def to_dict(self) -> Dict[str, Any]:
        return {"error_code": self.error_code, "reasons": [dict(r) for r in self.reasons]}


@dataclass(frozen=True)
class DerivedResult:
    """Redacted authoritative result returned to internal callers."""

    task_id: str
    item_id: str
    derivation_id: str
    input_fingerprint: str
    authority_type: str
    snapshot_id: str
    attempt_id: str
    submission_id: str
    adoption_id: Optional[str]
    lock_id: Optional[str]
    total_score: float
    objective_score: Optional[float]
    subjective_score: Optional[float]
    dimension_scores: tuple
    derived_at: str

    @property
    def final_snapshot(self) -> Dict[str, Any]:
        """Only score fields are exposed; evidence/rationale/provider data is not."""
        return {
            "snapshot_id": self.snapshot_id,
            "attempt_id": self.attempt_id,
            "submission_id": self.submission_id,
            "total_score": self.total_score,
            "objective_score": self.objective_score,
            "subjective_score": self.subjective_score,
            "dimension_scores": list(self.dimension_scores),
        }

    @property
    def snapshot(self) -> Dict[str, Any]:
        return self.final_snapshot

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "item_id": self.item_id,
            "derivation_id": self.derivation_id,
            "input_fingerprint": self.input_fingerprint,
            "authority_type": self.authority_type,
            "snapshot_id": self.snapshot_id,
            "attempt_id": self.attempt_id,
            "submission_id": self.submission_id,
            "adoption_id": self.adoption_id,
            "lock_id": self.lock_id,
            "final_result": self.final_snapshot,
            "derived_at": self.derived_at,
        }


class ResultDerivationService:
    """Derive one item's export authority without touching legacy exports."""

    def __init__(self, review_store: Any, attempt_store: Any, root: Optional[Path] = None) -> None:
        self._review_store = review_store
        self._attempt_store = attempt_store
        base = Path(root) if root is not None else Path(review_store.root)
        self.root = base / "result_derivation"

    def derive(self, task_id: str, item_id: str) -> DerivedResult:
        """Derive and persist the current authoritative result.

        Lock is checked first.  Adoption is consulted only when no active lock
        exists.  Every blocked path raises ResultDerivationError with stable,
        redacted reason objects.
        """
        lock = self._read_active_lock(task_id, item_id)
        adoption = self._read_active_adoption(task_id, item_id)
        cases = self._read_cases(task_id, item_id)
        blocking_cases = [
            case for case in cases
            if case.blocks_export and case.status in OPEN_EXPORT_BLOCKING_STATUSES
        ]
        reasons: List[Dict[str, Any]] = []
        if blocking_cases:
            reasons.append({
                "code": "UNRESOLVED_REVIEW_CASE",
                "count": len(blocking_cases),
                "statuses": sorted({case.status for case in blocking_cases}),
            })

        # A lock is the highest authority, even if an adoption also exists.
        authority_type = "manual_final_lock" if lock is not None else None
        selected_snapshot_id: Optional[str] = None
        selected_adoption_id: Optional[str] = None
        lock_id: Optional[str] = None
        if lock is not None:
            lock_id = lock.lock_id
            if lock.task_id != task_id or lock.item_id != item_id:
                reasons.append({"code": "LOCK_BINDING_MISMATCH"})
            if lock.snapshot_id is None and lock.adoption_id is None:
                reasons.append({"code": "LOCK_TARGET_MISSING"})
            if lock.adoption_id is not None:
                if adoption is None or adoption.adoption_id != lock.adoption_id:
                    reasons.append({"code": "LOCK_ADOPTION_BINDING_MISMATCH"})
                else:
                    selected_adoption_id = adoption.adoption_id
                    selected_snapshot_id = adoption.snapshot_id
            if lock.snapshot_id is not None:
                if selected_snapshot_id is not None and selected_snapshot_id != lock.snapshot_id:
                    reasons.append({"code": "LOCK_SNAPSHOT_BINDING_MISMATCH"})
                selected_snapshot_id = lock.snapshot_id
        elif adoption is not None:
            authority_type = "result_adoption"
            selected_adoption_id = adoption.adoption_id
            selected_snapshot_id = adoption.snapshot_id
        else:
            reasons.append({"code": "NO_AUTHORITATIVE_RESULT"})

        snapshot = None
        if selected_snapshot_id is not None:
            snapshot = self._read_snapshot(task_id, item_id, selected_snapshot_id, reasons)
            if snapshot is not None:
                self._validate_snapshot_binding(
                    task_id, item_id, snapshot, lock, adoption,
                    selected_snapshot_id, reasons,
                )

        if snapshot is None and not any(r["code"] == "SNAPSHOT_MISSING_OR_CORRUPTED" for r in reasons):
            if selected_snapshot_id is not None:
                reasons.append({"code": "SNAPSHOT_MISSING_OR_CORRUPTED"})
        if blocking_cases or reasons:
            raise ResultDerivationError(EXPORT_BLOCKED_BY_REVIEW, reasons)

        assert snapshot is not None
        input_payload = self._input_payload(
            task_id, item_id, lock, adoption, snapshot, cases,
        )
        fingerprint = hashlib.sha256(_canonical_bytes(input_payload)).hexdigest()
        derivation_id = f"der-{fingerprint[:48]}"
        record = self._record(
            derivation_id, fingerprint, task_id, item_id, authority_type or "none",
            lock, adoption, snapshot,
        )
        path = self.root / "tasks" / task_id / "items" / item_id / f"{derivation_id}.json"
        canonical = _canonical_bytes(record)
        if path.exists():
            try:
                if path.read_bytes() != canonical:
                    raise ResultDerivationError(
                        EXPORT_BLOCKED_BY_REVIEW,
                        ({"code": "DERIVATION_ID_CONFLICT"},),
                    )
            except OSError as exc:
                raise ResultDerivationError(
                    EXPORT_BLOCKED_BY_REVIEW, ({"code": "DERIVATION_RECORD_UNREADABLE"},)
                ) from exc
        else:
            try:
                _atomic_write_bytes(path, canonical)
            except OSError as exc:
                raise ResultDerivationError(
                    EXPORT_BLOCKED_BY_REVIEW, ({"code": "DERIVATION_WRITE_FAILED"},)
                ) from exc
        return self._result_from_snapshot(
            derivation_id, fingerprint, task_id, item_id,
            authority_type or "none", lock, adoption, snapshot,
        )

    derive_authoritative_result = derive
    derive_result = derive

    def _read_active_lock(self, task_id: str, item_id: str) -> Any:
        try:
            return self._review_store.get_active_final_lock(task_id, item_id)
        except ReviewFactStoreError as exc:
            raise ResultDerivationError(
                EXPORT_BLOCKED_BY_REVIEW, ({"code": "ACTIVE_LOCK_FACT_INVALID"},)
            ) from exc

    def _read_active_adoption(self, task_id: str, item_id: str) -> Any:
        try:
            return self._review_store.get_active_adoption(task_id, item_id)
        except ReviewFactStoreError as exc:
            raise ResultDerivationError(
                EXPORT_BLOCKED_BY_REVIEW, ({"code": "ACTIVE_ADOPTION_FACT_INVALID"},)
            ) from exc

    def _read_cases(self, task_id: str, item_id: str) -> List[Any]:
        try:
            return list(self._review_store.list_review_cases(task_id, item_id))
        except ReviewFactStoreError as exc:
            raise ResultDerivationError(
                EXPORT_BLOCKED_BY_REVIEW, ({"code": "REVIEW_CASE_FACT_INVALID"},)
            ) from exc

    def _read_snapshot(self, task_id: str, item_id: str, snapshot_id: str, reasons: List[Dict[str, Any]]) -> Any:
        try:
            snapshot = self._attempt_store.get_snapshot(task_id, item_id, snapshot_id)
        except ScoreFactStoreError as exc:
            reasons.append({"code": "SNAPSHOT_MISSING_OR_CORRUPTED"})
            return None
        if snapshot is None:
            reasons.append({"code": "SNAPSHOT_MISSING_OR_CORRUPTED"})
        return snapshot

    def _validate_snapshot_binding(
        self, task_id: str, item_id: str, snapshot: Any, lock: Any,
        adoption: Any, snapshot_id: str, reasons: List[Dict[str, Any]],
    ) -> None:
        try:
            attempt = self._attempt_store.get_attempt(task_id, item_id, snapshot.attempt_id)
        except ScoreFactStoreError:
            attempt = None
        if attempt is None:
            reasons.append({"code": "SNAPSHOT_ATTEMPT_BINDING_MISMATCH"})
        else:
            if attempt.task_id != task_id or attempt.item_id != item_id:
                reasons.append({"code": "SNAPSHOT_ATTEMPT_BINDING_MISMATCH"})
            if attempt.submission_id != snapshot.submission_id:
                reasons.append({"code": "SNAPSHOT_SUBMISSION_BINDING_MISMATCH"})
        if adoption is not None:
            if adoption.snapshot_id != snapshot_id:
                reasons.append({"code": "ADOPTION_SNAPSHOT_BINDING_MISMATCH"})
            if adoption.attempt_id != snapshot.attempt_id:
                reasons.append({"code": "ADOPTION_ATTEMPT_BINDING_MISMATCH"})
            if adoption.submission_id != snapshot.submission_id:
                reasons.append({"code": "ADOPTION_SUBMISSION_BINDING_MISMATCH"})
        if lock is not None and lock.snapshot_id is not None and lock.snapshot_id != snapshot_id:
            reasons.append({"code": "LOCK_SNAPSHOT_BINDING_MISMATCH"})

    @staticmethod
    def _input_payload(task_id: str, item_id: str, lock: Any, adoption: Any, snapshot: Any, cases: List[Any]) -> Dict[str, Any]:
        return {
            "task_id": task_id,
            "item_id": item_id,
            "lock": lock.model_dump(mode="json") if lock is not None else None,
            "adoption": adoption.model_dump(mode="json") if adoption is not None else None,
            "snapshot": snapshot.model_dump(mode="json"),
            "review_cases": [case.model_dump(mode="json") for case in cases],
        }

    @staticmethod
    def _record(derivation_id: str, fingerprint: str, task_id: str, item_id: str,
                authority_type: str, lock: Any, adoption: Any, snapshot: Any) -> Dict[str, Any]:
        return {
            "schema_version": "result-derivation/v1",
            "derivation_id": derivation_id,
            "task_id": task_id,
            "item_id": item_id,
            "input_fingerprint": fingerprint,
            "authority_type": authority_type,
            "lock_id": lock.lock_id if lock is not None else None,
            "adoption_id": adoption.adoption_id if adoption is not None else None,
            "snapshot_id": snapshot.snapshot_id,
            "attempt_id": snapshot.attempt_id,
            "submission_id": snapshot.submission_id,
            "score": {
                "total_score": snapshot.total_score,
                "objective_score": snapshot.objective_score,
                "subjective_score": snapshot.subjective_score,
                "dimension_scores": [
                    ResultDerivationService._redact_dimension_score(d)
                    for d in snapshot.dimension_scores
                ],
            },
            "source_refs": {
                "lock_id": lock.lock_id if lock is not None else None,
                "adoption_id": adoption.adoption_id if adoption is not None else None,
                "snapshot_id": snapshot.snapshot_id,
                "attempt_id": snapshot.attempt_id,
            },
            "derived_at": snapshot.created_at.isoformat(),
        }

    @staticmethod
    def _result_from_snapshot(derivation_id: str, fingerprint: str, task_id: str, item_id: str,
                              authority_type: str, lock: Any, adoption: Any, snapshot: Any) -> DerivedResult:
        dimensions = tuple(
            ResultDerivationService._redact_dimension_score(d)
            for d in snapshot.dimension_scores
        )
        return DerivedResult(
            task_id=task_id,
            item_id=item_id,
            derivation_id=derivation_id,
            input_fingerprint=fingerprint,
            authority_type=authority_type,
            snapshot_id=snapshot.snapshot_id,
            attempt_id=snapshot.attempt_id,
            submission_id=snapshot.submission_id,
            adoption_id=adoption.adoption_id if adoption is not None else None,
            lock_id=lock.lock_id if lock is not None else None,
            total_score=snapshot.total_score,
            objective_score=snapshot.objective_score,
            subjective_score=snapshot.subjective_score,
            dimension_scores=dimensions,
            derived_at=snapshot.created_at.isoformat(),
        )

    @staticmethod
    def _redact_dimension_score(dimension_score: Any) -> Dict[str, Any]:
        return {
            "dimension_code": dimension_score.dimension_code,
            "score": dimension_score.score,
            "min_score": dimension_score.min_score,
            "max_score": dimension_score.max_score,
            "score_range_ref": dimension_score.score_range_ref,
        }


AuthoritativeResultDerivationService = ResultDerivationService
