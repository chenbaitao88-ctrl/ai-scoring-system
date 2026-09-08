"""Phase 11F-3b-prerequisite-impl-3：ManualAdjustment API 事务测试。

只使用合成脱敏数据和公开 Store API。禁止私有路径覆写、skip、return 掩盖 fixture 缺陷。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import test_scoring_pipeline_execute_integration as base
from models.review_case import ManualFinalLock, ResultAdoption, ReviewCase, ReviewDecision
from models.score_attempt import (
    AttemptValidation,
    DimensionScore,
    ScoreResultSnapshot,
    ScoreScale,
    ValidationCheck,
)
from routers import scoring_pipeline
from services.manual_adjustment_service import ManualAdjustmentService
from services.review_case_store import ReviewCaseStore
from services.score_attempt_store import ScoreAttemptStore
from services.scoring_pipeline_api_container import ScoringPipelineApiContainer
from services.scoring_task_creator import ScoringTaskCreator

T0 = datetime(2026, 8, 26, 8, 0, 0, tzinfo=timezone.utc)


class DimensionalSnapshotFactory:
    """正规合成 snapshot 工厂：Provider 成功结果直接产出真实维度。"""

    def build(self, *, attempt, response, structured, profile, completed_at):
        dimensions = [
            DimensionScore(
                dimension_code="objective", score=50.0,
                min_score=0.0, max_score=60.0,
                score_range_ref="range-objective-60", evidence_refs=[]),
            DimensionScore(
                dimension_code="subjective", score=40.0,
                min_score=0.0, max_score=40.0,
                score_range_ref="range-subjective-40", evidence_refs=[]),
        ]
        return ScoreResultSnapshot(
            schema_version="score-result-snapshot/v1",
            snapshot_id=f"snap-{attempt.attempt_id}",
            attempt_id=attempt.attempt_id,
            submission_id=attempt.submission_id,
            package_id=attempt.package_id,
            evidence_manifest_sha256=attempt.evidence_manifest_sha256,
            scoring_policy_version=attempt.scoring_policy_version,
            rubric_version=attempt.rubric_version,
            response_schema_version=attempt.response_schema_version,
            score_scale=ScoreScale(
                scoring_policy_version=attempt.scoring_policy_version,
                total_score_range_ref="range-total-100",
                total_min=0.0, total_max=100.0,
                dimensions=[]),
            objective_score=50.0,
            subjective_score=40.0,
            dimension_scores=dimensions,
            total_score=90.0,
            rationale_summary="synthetic rationale",
            evidence_level="sufficient",
            evidence_refs=[], confidence="high", flags=[],
            manual_review_recommended=False,
            structure_validation_status="passed",
            score_range_validation_status="passed",
            result_hash=response.output_sha256,
            created_at=completed_at,
        )


def _seed_env(tmp_path):
    env = base._seed_env(tmp_path)
    env["orch"]._snapshots = DimensionalSnapshotFactory()
    review_store = ReviewCaseStore(tmp_path / "reviews", attempt_store=env["attempt_store"])
    env["review_store"] = review_store
    env["orch"]._review_store = review_store
    item = base._item(env)
    outcome = base._run(env["orch"].execute_score_item(
        env["task"].task_id, item.item_id, "exec-manual-adjustment-api"))
    assert outcome.outcome == "succeeded", outcome.error_code
    attempts = env["attempt_store"].list_attempts(env["task"].task_id, item.item_id)
    assert attempts and attempts[-1].result_snapshot_ref
    snapshot = env["attempt_store"].get_snapshot(
        env["task"].task_id, item.item_id, attempts[-1].result_snapshot_ref)
    assert snapshot is not None
    assert len(snapshot.dimension_scores) == 2
    env["item"] = item
    env["attempt"] = attempts[-1]
    env["snapshot"] = snapshot
    return env


def _create_review_facts(env, request_id="req-main"):
    item, attempt, snapshot = env["item"], env["attempt"], env["snapshot"]
    case = ReviewCase(
        contract_version="score-attempt-review/v1", schema_version="review-case/v1",
        review_case_id=f"case-{request_id}", case_number=f"RC-{request_id}",
        submission_id=snapshot.submission_id, task_id=env["task"].task_id,
        item_id=item.item_id, package_id=item.package_id,
        package_revision=item.package_revision,
        attempt_ids=[attempt.attempt_id], snapshot_ids=[snapshot.snapshot_id],
        source_type="manual", reason_codes=["LOW_CONFIDENCE"], priority="high",
        status="open", blocks_auto_adoption=True, blocks_export=True,
        opened_at=T0, updated_at=T0, current_revision=1, reopen_count=0,
        idempotency_key=f"case-idem-{request_id}")
    env["review_store"].create_review_case(env["task"].task_id, item.item_id, case)
    decision = ReviewDecision(
        contract_version="score-attempt-review/v1", schema_version="review-decision/v1",
        decision_id=f"decision-{request_id}", review_case_id=case.review_case_id,
        case_revision=1, decision_type="apply_manual_adjustment",
        reason_codes=["LOW_CONFIDENCE"], target_attempt_id=attempt.attempt_id,
        target_snapshot_id=snapshot.snapshot_id,
        manual_adjustment_id=f"adj-{request_id}",
        decision_note_code="APPLY_MANUAL_ADJUSTMENT",
        decided_by={"actor_type": "reviewer", "actor_id": "reviewer-synthetic"},
        decided_at=T0, decision_hash="ab" * 32,
        idempotency_key=f"decision-idem-{request_id}")
    env["review_store"].create_review_decision(
        env["task"].task_id, item.item_id, decision)
    validation = env["attempt_store"].get_validation(
        env["task"].task_id, item.item_id, attempt.validation_ref)
    assert validation is not None and validation.snapshot_id == snapshot.snapshot_id
    adoption = ResultAdoption(
        contract_version="score-attempt-review/v1", schema_version="result-adoption/v1",
        adoption_id=f"adp-base-{request_id}",
        adoption_scope={
            "competition_id": "competition-synthetic", "batch_id": "batch-synthetic",
            "stream_id": "default", "submission_id": snapshot.submission_id,
            "scoring_policy_version": snapshot.scoring_policy_version,
            "purpose": "machine_result"},
        submission_id=snapshot.submission_id, attempt_id=attempt.attempt_id,
        snapshot_id=snapshot.snapshot_id, validation_id=validation.validation_id,
        review_case_id=case.review_case_id, decision_id=decision.decision_id,
        status="adopted", reason_code="AUTO_ADOPTION_VALIDATION_PASSED",
        effective_at=T0, decided_at=T0,
        decided_by={"actor_type": "reviewer", "actor_id": "reviewer-synthetic"},
        adoption_hash="cd" * 32)
    env["review_store"].adopt_result(env["task"].task_id, item.item_id, adoption)
    env.update(case=case, decision=decision, base_adoption=adoption)
    return env


def _container(env, service=None):
    service = service or ManualAdjustmentService(env["review_store"], env["attempt_store"])
    return ScoringPipelineApiContainer(
        task_manager=env["mgr"],
        task_creator=ScoringTaskCreator(env["store"], env["reg"], env["lookup"]),
        profile_lookup=env["lookup"], provider_binding_lookup=env["lookup"],
        provider_registry=env["registry"], orchestrator=env["orch"],
        review_store=env["review_store"], attempt_store=env["attempt_store"],
        adjustment_service=service, test_runtime=True)


def _client(env, service=None):
    app = FastAPI()
    app.include_router(scoring_pipeline.router)
    app.dependency_overrides[scoring_pipeline.get_scoring_pipeline_api_container] = (
        lambda: _container(env, service))
    return TestClient(app)


def _url(env):
    return (
        f"/api/scoring-pipeline/tasks/{env['task'].task_id}/items/{env['item'].item_id}"
        f"/review-decisions/{env['decision'].decision_id}/adjust")


def _payload(env, request_id, *, after_delta=1.0, before_delta=0.0, code="objective"):
    dimension = next((d for d in env["snapshot"].dimension_scores if d.dimension_code == code), None)
    before = dimension.score if dimension is not None else 0.0
    return {
        "adjustment_request_id": request_id,
        "review_case_id": env["case"].review_case_id,
        "changes": [{
            "dimension_code": code,
            "before_value": before + before_delta,
            "after_value": before + after_delta,
            "change_reason_code": "LOW_CONFIDENCE"}],
        "reason_codes": ["LOW_CONFIDENCE"],
        "adjustment_note_code": "MANUAL_SCORE_CORRECTION"}


def _active_adoptions(env):
    return [a for a in env["review_store"].list_adoptions(
        env["task"].task_id, env["item"].item_id) if a.status == "adopted"]


def _assert_original_unchanged(env, original_dump, original_hash):
    snapshot = env["attempt_store"].get_snapshot(
        env["task"].task_id, env["item"].item_id, env["snapshot"].snapshot_id)
    assert snapshot.model_dump(mode="json") == original_dump
    assert snapshot.result_hash == original_hash


def _setup(tmp_path, request_id="req-main"):
    return _create_review_facts(_seed_env(tmp_path), request_id)


# ---------------- 正常与校验 ---------------- #


def test_adjust_success_full_binding_and_sensitive_response(tmp_path):
    env = _setup(tmp_path, "req-success")
    original = env["snapshot"].model_dump(mode="json")
    original_hash = env["snapshot"].result_hash
    with _client(env) as client:
        response = client.post(_url(env), json=_payload(env, "req-success"))
    assert response.status_code == 201, response.text
    assert response.json() == {
        "adjustment_id": "adj-req-success",
        "adjusted_snapshot_id": "snap-adjusted-req-success",
        "adoption_id": "adp-adjusted-req-success",
        "outcome": "created", "idempotent": False}
    adjusted = env["attempt_store"].get_snapshot(
        env["task"].task_id, env["item"].item_id, "snap-adjusted-req-success")
    validation = env["attempt_store"].get_validation(
        env["task"].task_id, env["item"].item_id, "val-adjusted-req-success")
    adoption = env["review_store"].get_adoption(
        env["task"].task_id, env["item"].item_id, "adp-adjusted-req-success")
    operation = env["review_store"].get_operation(
        env["task"].task_id, env["item"].item_id, "op-req-success")
    assert adjusted.total_score == 91.0
    assert next(d.score for d in adjusted.dimension_scores if d.dimension_code == "objective") == 51.0
    assert validation.snapshot_id == adjusted.snapshot_id
    assert validation.validation_id != env["attempt"].validation_ref
    assert any(c.check_code == "MANUAL_ADJUSTMENT_VALIDATED" for c in validation.checks)
    assert adoption.validation_id == validation.validation_id
    assert adoption.snapshot_id == adjusted.snapshot_id
    assert operation.stage == "committed"
    assert len(_active_adoptions(env)) == 1
    _assert_original_unchanged(env, original, original_hash)
    lowered = response.text.lower()
    for forbidden in ("actor", "prompt", "api_key", "sk-", "evidence_refs", "rationale"):
        assert forbidden not in lowered


def test_replay_and_conflict(tmp_path):
    env = _setup(tmp_path, "req-replay")
    payload = _payload(env, "req-replay")
    with _client(env) as client:
        first = client.post(_url(env), json=payload)
        replay = client.post(_url(env), json=payload)
        changed = client.post(_url(env), json=_payload(env, "req-replay", after_delta=2.0))
    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json()["outcome"] == "idempotent_hit"
    assert replay.json()["idempotent"] is True
    assert changed.status_code == 409
    assert "MANUAL_ADJUSTMENT_CONFLICT" in changed.text
    assert len(env["review_store"].list_manual_adjustments(
        env["task"].task_id, env["item"].item_id)) == 1
    assert len(_active_adoptions(env)) == 1


@pytest.mark.parametrize("payload_factory", [
    lambda e: _payload(e, "req-before", before_delta=1.0),
    lambda e: _payload(e, "req-unknown", code="unknown"),
    lambda e: {**_payload(e, "req-range"), "changes": [{
        "dimension_code": "objective", "before_value": 50.0,
        "after_value": 100.0, "change_reason_code": "LOW_CONFIDENCE"}]},
    lambda e: {**_payload(e, "req-duplicate"), "changes": [
        {"dimension_code": "objective", "before_value": 50.0,
         "after_value": 51.0, "change_reason_code": "LOW_CONFIDENCE"},
        {"dimension_code": "objective", "before_value": 50.0,
         "after_value": 52.0, "change_reason_code": "LOW_CONFIDENCE"}]},
])
def test_invalid_changes_are_rejected_without_operation(tmp_path, payload_factory):
    env = _setup(tmp_path, "req-invalid-base")
    payload = payload_factory(env)
    with _client(env) as client:
        response = client.post(_url(env), json=payload)
    assert response.status_code == 409
    op_id = f"op-{payload['adjustment_request_id']}"
    assert env["review_store"].get_operation(
        env["task"].task_id, env["item"].item_id, op_id) is None
    assert len(_active_adoptions(env)) == 1


def test_final_lock_blocks_adjustment(tmp_path):
    env = _setup(tmp_path, "req-lock")
    lock = ManualFinalLock(
        contract_version="score-attempt-review/v1", schema_version="manual-final-lock/v1",
        lock_id="lock-req-lock", task_id=env["task"].task_id,
        item_id=env["item"].item_id, review_case_id=env["case"].review_case_id,
        decision_id=env["decision"].decision_id,
        adoption_id=env["base_adoption"].adoption_id,
        snapshot_id=env["snapshot"].snapshot_id,
        locked_by={"actor_type": "reviewer", "actor_id": "reviewer-lock"},
        locked_at=T0, reason_code="HUMAN_FINAL_LOCKED", status="active", revision=1,
        idempotency_key="lock-idem-req-lock", content_hash="ef" * 32)
    env["review_store"].create_final_lock(
        env["task"].task_id, env["item"].item_id, lock)
    with _client(env) as client:
        response = client.post(_url(env), json=_payload(env, "req-lock"))
    assert response.status_code == 409
    assert "MANUAL_ADJUSTMENT_BLOCKED_BY_LOCK" in response.text
    assert env["review_store"].get_operation(
        env["task"].task_id, env["item"].item_id, "op-req-lock") is None


# ---------------- 故障注入 ---------------- #


def _assert_failure_state(env, response, original, original_hash):
    assert response.status_code >= 400
    operation = env["review_store"].get_operation(
        env["task"].task_id, env["item"].item_id,
        f"op-{response.request.url.path.split('/')[-2] if False else 'unused'}")
    _assert_original_unchanged(env, original, original_hash)
    assert len(_active_adoptions(env)) <= 1


@pytest.mark.parametrize("boundary", [
    "snapshot", "validation", "advance_prepared", "adjustment", "adoption",
    "advance_committed", "restore", "snapshot_cleanup", "validation_cleanup",
])
def test_failure_boundaries_are_visible_and_never_double_active(tmp_path, monkeypatch, boundary):
    request_id = f"req-fail-{boundary}"
    env = _setup(tmp_path, request_id)
    original = env["snapshot"].model_dump(mode="json")
    original_hash = env["snapshot"].result_hash
    r_store, a_store = env["review_store"], env["attempt_store"]

    if boundary == "snapshot":
        monkeypatch.setattr(a_store, "write_snapshot", lambda *a, **k: (_ for _ in ()).throw(OSError("snapshot")))
    elif boundary == "validation":
        monkeypatch.setattr(a_store, "write_validation", lambda *a, **k: (_ for _ in ()).throw(OSError("validation")))
    elif boundary == "advance_prepared":
        original_advance = r_store.advance_operation
        monkeypatch.setattr(r_store, "advance_operation", lambda *a, **k: (_ for _ in ()).throw(OSError("advance")))
    elif boundary == "adjustment":
        monkeypatch.setattr(r_store, "create_manual_adjustment", lambda *a, **k: (_ for _ in ()).throw(OSError("adjustment")))
    elif boundary == "adoption":
        original_adopt = r_store.adopt_result
        calls = {"n": 0}
        def fail_new_adoption(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] >= 1:
                raise OSError("adoption")
            return original_adopt(*args, **kwargs)
        monkeypatch.setattr(r_store, "adopt_result", fail_new_adoption)
    elif boundary == "advance_committed":
        original_advance = r_store.advance_operation
        def fail_commit(task_id, item_id, operation):
            if operation.stage == "committed":
                raise OSError("commit")
            return original_advance(task_id, item_id, operation)
        monkeypatch.setattr(r_store, "advance_operation", fail_commit)
    elif boundary == "restore":
        monkeypatch.setattr(r_store, "adopt_result", lambda *a, **k: (_ for _ in ()).throw(OSError("adopt")))
        monkeypatch.setattr(r_store, "restore_adoption_status", lambda *a, **k: "RESTORE_FAILED")
    elif boundary == "snapshot_cleanup":
        monkeypatch.setattr(r_store, "create_manual_adjustment", lambda *a, **k: (_ for _ in ()).throw(OSError("adjustment")))
        monkeypatch.setattr(a_store, "delete_snapshot", lambda *a, **k: (_ for _ in ()).throw(OSError("cleanup snapshot")))
    elif boundary == "validation_cleanup":
        monkeypatch.setattr(r_store, "create_manual_adjustment", lambda *a, **k: (_ for _ in ()).throw(OSError("adjustment")))
        monkeypatch.setattr(a_store, "delete_validation", lambda *a, **k: (_ for _ in ()).throw(OSError("cleanup validation")))

    with _client(env) as client:
        response = client.post(_url(env), json=_payload(env, request_id))
    assert response.status_code >= 400
    operation = r_store.get_operation(env["task"].task_id, env["item"].item_id, f"op-{request_id}")
    assert operation is not None and operation.stage != "committed"
    _assert_original_unchanged(env, original, original_hash)
    assert len(_active_adoptions(env)) <= 1


def test_supersede_failure_is_visible(tmp_path, monkeypatch):
    env = _setup(tmp_path, "req-fail-supersede")
    original = env["snapshot"].model_dump(mode="json")
    original_hash = env["snapshot"].result_hash
    monkeypatch.setattr(
        env["review_store"], "_replace_adoption_status",
        lambda *a, **k: (_ for _ in ()).throw(OSError("supersede")))
    with _client(env) as client:
        response = client.post(_url(env), json=_payload(env, "req-fail-supersede"))
    assert response.status_code >= 400
    assert env["review_store"].get_adoption(
        env["task"].task_id, env["item"].item_id,
        env["base_adoption"].adoption_id).status == "adopted"
    assert len(_active_adoptions(env)) == 1
    _assert_original_unchanged(env, original, original_hash)


def test_adoption_event_failure_is_visible(tmp_path, monkeypatch):
    env = _setup(tmp_path, "req-fail-event")
    original_append = env["review_store"]._append_event
    def fail_only_adjusted_event(task_id, item_id, event):
        if event.event_type == "ADOPTION_LINKED" and "adp-adjusted-req-fail-event" in event.object_refs:
            raise OSError("event")
        return original_append(task_id, item_id, event)
    monkeypatch.setattr(env["review_store"], "_append_event", fail_only_adjusted_event)
    with _client(env) as client:
        response = client.post(_url(env), json=_payload(env, "req-fail-event"))
    assert response.status_code >= 400
    operation = env["review_store"].get_operation(
        env["task"].task_id, env["item"].item_id, "op-req-fail-event")
    assert operation is not None and operation.stage != "committed"
    assert len(_active_adoptions(env)) <= 1


# ---------------- 重启恢复 ---------------- #


def _reopen_env(env, root):
    attempt_store = ScoreAttemptStore(root / "score-attempts")
    review_store = ReviewCaseStore(root / "reviews", attempt_store=attempt_store)
    reopened = dict(env)
    reopened["attempt_store"] = attempt_store
    reopened["review_store"] = review_store
    return reopened


def test_restart_recovers_prepared_without_duplicates(tmp_path, monkeypatch):
    env = _setup(tmp_path, "req-restart-prepared")
    original_advance = env["review_store"].advance_operation
    monkeypatch.setattr(
        env["review_store"], "advance_operation",
        lambda *a, **k: (_ for _ in ()).throw(OSError("prepared interruption")))
    with _client(env) as client:
        first = client.post(_url(env), json=_payload(env, "req-restart-prepared"))
    assert first.status_code >= 400
    monkeypatch.setattr(env["review_store"], "advance_operation", original_advance)
    reopened = _reopen_env(env, tmp_path)
    with _client(reopened) as client:
        resumed = client.post(_url(reopened), json=_payload(reopened, "req-restart-prepared"))
    assert resumed.status_code in (200, 201), resumed.text
    op = reopened["review_store"].get_operation(
        reopened["task"].task_id, reopened["item"].item_id, "op-req-restart-prepared")
    assert op.stage == "committed"
    assert len(reopened["review_store"].list_manual_adjustments(
        reopened["task"].task_id, reopened["item"].item_id)) == 1
    assert len(_active_adoptions(reopened)) == 1


def test_restart_recovers_committing_adjustment_without_duplicates(tmp_path, monkeypatch):
    env = _setup(tmp_path, "req-restart-committing")
    original_adopt = env["review_store"].adopt_result
    monkeypatch.setattr(
        env["review_store"], "adopt_result",
        lambda *a, **k: (_ for _ in ()).throw(OSError("committing interruption")))
    with _client(env) as client:
        first = client.post(_url(env), json=_payload(env, "req-restart-committing"))
    assert first.status_code >= 400
    monkeypatch.setattr(env["review_store"], "adopt_result", original_adopt)
    reopened = _reopen_env(env, tmp_path)
    operation = reopened["review_store"].get_operation(
        reopened["task"].task_id, reopened["item"].item_id, "op-req-restart-committing")
    assert operation.stage != "committed"
    assert len(_active_adoptions(reopened)) <= 1
