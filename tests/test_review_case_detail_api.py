"""
Phase 11F-2c：复核详情 API 合成测试。

覆盖：正常详情、字段白名单、actor_id 不泄露、404/409/422、attempt/decision 跨 case 不泄漏、
adoption 歧义 fail closed、历史稳定排序。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import test_scoring_pipeline_execute_integration as base
from models.score_attempt import DimensionScore, ScoreResultSnapshot, ScoreScale
from routers import scoring_pipeline
from services.review_case_store import ReviewCaseStore
from services.scoring_pipeline_api_container import ScoringPipelineApiContainer
from services.scoring_task_creator import ScoringTaskCreator

T0 = datetime(2026, 8, 20, 6, 0, 0, tzinfo=timezone.utc)


class _DimensionalSnapshotFactory:
    def build(self, *, attempt, response, structured, profile, completed_at):
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
                total_min=0.0, total_max=100.0, dimensions=[]),
            objective_score=50.0, subjective_score=40.0,
            dimension_scores=[
                DimensionScore(
                    dimension_code="objective", score=50.0,
                    min_score=0.0, max_score=60.0,
                    score_range_ref="range-objective-60", evidence_refs=[]),
                DimensionScore(
                    dimension_code="subjective", score=40.0,
                    min_score=0.0, max_score=40.0,
                    score_range_ref="range-subjective-40", evidence_refs=[]),
            ],
            total_score=90.0, rationale_summary="synthetic detail rationale",
            evidence_level="sufficient", evidence_refs=[], confidence="high", flags=[],
            manual_review_recommended=False,
            structure_validation_status="passed", score_range_validation_status="passed",
            result_hash=response.output_sha256, created_at=completed_at)


def _client(container):
    app = FastAPI()
    app.include_router(scoring_pipeline.router)
    app.dependency_overrides[
        scoring_pipeline.get_scoring_pipeline_api_container
    ] = lambda: container
    return TestClient(app)


def _item_at(env, index=0):
    return env["store"].load_item(
        env["task"].task_id, env["task"].item_index[index].item_id)


def _seed(tmp_path, item_count=1):
    env = base._seed_env(tmp_path, item_count=item_count)
    env["orch"]._snapshots = _DimensionalSnapshotFactory()
    attempt_store = env["attempt_store"]
    review_store = ReviewCaseStore(tmp_path / "reviews", attempt_store=attempt_store)
    env["review_store"] = review_store
    env["orch"]._review_store = review_store
    for index in range(item_count):
        item = _item_at(env, index)
        result = base._run(env["orch"].execute_score_item(
            env["task"].task_id, item.item_id, f"exec-ctl-{index}"))
        assert result.outcome == "succeeded", result.error_code
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


def _open_case(env, item, *, review_case_id="rev_ctl_001"):
    attempts = env["attempt_store"].list_attempts(env["task"].task_id, item.item_id)
    att = attempts[-1]
    snap = env["attempt_store"].get_snapshot(
        env["task"].task_id, item.item_id, att.result_snapshot_ref)
    from models.review_case import ReviewCase
    case = ReviewCase(
        contract_version="score-attempt-review/v1",
        schema_version="review-case/v1",
        review_case_id=review_case_id,
        case_number=f"RC-{review_case_id}",
        submission_id=snap.submission_id,
        task_id=env["task"].task_id,
        item_id=item.item_id,
        package_id=item.package_id,
        package_revision=item.package_revision,
        attempt_ids=[att.attempt_id],
        snapshot_ids=[snap.snapshot_id],
        source_type="manual",
        reason_codes=["LOW_CONFIDENCE"],
        priority="high",
        status="open",
        blocks_auto_adoption=True,
        blocks_export=True,
        opened_at=T0,
        updated_at=T0,
        current_revision=1,
        reopen_count=0,
        idempotency_key=f"idem-{review_case_id}",
    )
    env["review_store"].create_review_case(env["task"].task_id, item.item_id, case)
    return case, att, snap


def _add_decision(env, item, case, att, snap, decision_id="dec-ctl-001"):
    from models.review_case import ReviewDecision
    dec = ReviewDecision(
        contract_version="score-attempt-review/v1",
        schema_version="review-decision/v1",
        decision_id=decision_id,
        review_case_id=case.review_case_id,
        case_revision=case.current_revision,
        decision_type="adopt_existing_attempt",
        reason_codes=["LOW_CONFIDENCE"],
        target_attempt_id=att.attempt_id,
        target_snapshot_id=snap.snapshot_id,
        decision_note_code="ADOPT_EXISTING_ATTEMPT",
        decided_by={"actor_type": "reviewer", "actor_id": "r1"},
        decided_at=T0,
        decision_hash="ab" * 32,
        idempotency_key=f"idem-{decision_id}",
    )
    env["review_store"].create_review_decision(
        env["task"].task_id, item.item_id, dec)
    return dec


def _adopt(env, item, case, decision, attempt, snapshot, adoption_id="adp-detail-001"):
    from models.review_case import ResultAdoption
    validation = env["attempt_store"].get_validation(
        env["task"].task_id, item.item_id, attempt.validation_ref)
    assert validation is not None
    adoption = ResultAdoption(
        contract_version="score-attempt-review/v1", schema_version="result-adoption/v1",
        adoption_id=adoption_id,
        adoption_scope={
            "competition_id": "competition-detail", "batch_id": "batch-detail",
            "stream_id": "default", "submission_id": snapshot.submission_id,
            "scoring_policy_version": snapshot.scoring_policy_version,
            "purpose": "machine_result"},
        submission_id=snapshot.submission_id, attempt_id=attempt.attempt_id,
        snapshot_id=snapshot.snapshot_id, validation_id=validation.validation_id,
        review_case_id=case.review_case_id, decision_id=decision.decision_id,
        status="adopted", reason_code="AUTO_ADOPTION_VALIDATION_PASSED",
        effective_at=T0, decided_at=T0,
        decided_by={"actor_type": "reviewer", "actor_id": "reviewer-detail"},
        adoption_hash="cd" * 32)
    env["review_store"].adopt_result(env["task"].task_id, item.item_id, adoption)
    return adoption


def _detail_url(env, case):
    return (
        f"/api/scoring-pipeline/tasks/{env['task'].task_id}"
        f"/review-cases/{case.review_case_id}/detail")


# ---------------------------------------------------------------- 测试


def test_detail_returns_whitelist_fields(tmp_path):
    env = _seed(tmp_path)
    item = base._item(env)
    case, att, snap = _open_case(env, item)
    _add_decision(env, item, case, att, snap)
    with _client(_container(env)) as client:
        r = client.get(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/review-cases/{case.review_case_id}/detail")
        assert r.status_code == 200, r.text
        d = r.json()
        assert "case" in d
        assert "attempts" in d
        assert "decisions" in d
        assert "active_adoption" in d
        assert "active_lock" in d
        assert d["manual_adjustment_context"] == {
            "allowed": False,
            "block_reason_code": "NO_ACTIVE_ADOPTION",
            "attempt_id": None,
            "snapshot_id": None,
            "current_total_score": None,
            "dimensions": [],
        }
        assert d["case"]["review_case_id"] == case.review_case_id
        assert len(d["attempts"]) == 1
        assert d["attempts"][0]["total_score"] is not None
        assert len(d["decisions"]) == 1
        text = r.text
        assert "actor_id" not in text
        assert "evidence" not in text.lower()
        assert "prompt" not in text.lower()
        assert "api_key" not in text


def test_detail_case_not_found_404(tmp_path):
    env = _seed(tmp_path)
    item = base._item(env)
    with _client(_container(env)) as client:
        r = client.get(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/review-cases/rev_nope/detail")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "REVIEW_CASE_NOT_FOUND"


def test_detail_cross_case_attempt_not_leaked(tmp_path):
    env = _seed(tmp_path, item_count=2)
    item_a = base._item(env)
    item_b = _item_at(env, 1)
    case_a, att_a, snap_a = _open_case(env, item_a, review_case_id="rev_a")
    _open_case(env, item_b, review_case_id="rev_b")
    with _client(_container(env)) as client:
        r = client.get(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/review-cases/rev_a/detail")
        assert r.status_code == 200
        d = r.json()
        # 只有 rev_a 引用的 attempt
        assert len(d["attempts"]) == 1
        assert d["attempts"][0]["attempt_id"] == att_a.attempt_id


def test_detail_decisions_not_leaked_across_cases(tmp_path, item_count=1):
    env = _seed(tmp_path)
    item = base._item(env)
    case1, att1, snap1 = _open_case(env, item, review_case_id="rev_1")
    _open_case(env, item, review_case_id="rev_2")
    _add_decision(env, item, case1, att1, snap1, decision_id="dec-1")
    with _client(_container(env)) as client:
        # rev_2 无 decision
        r = client.get(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/review-cases/rev_2/detail")
        assert r.status_code == 200
        assert len(r.json()["decisions"]) == 0


def test_detail_attempts_sorted_by_number(tmp_path):
    env = _seed(tmp_path)
    item = base._item(env)
    case, _, _ = _open_case(env, item)
    # 第二次执行会返回 already_completed（已有成功快照），但 attempt 仍被记录
    base._run(env["orch"].execute_score_item(
        env["task"].task_id, item.item_id, "exec-2"))
    with _client(_container(env)) as client:
        r = client.get(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/review-cases/{case.review_case_id}/detail")
        assert r.status_code == 200
        nums = [a["attempt_number"] for a in r.json()["attempts"]]
        assert nums == sorted(nums)


def test_detail_no_actor_id_or_sensitive_fields(tmp_path):
    env = _seed(tmp_path)
    item = base._item(env)
    case, att, snap = _open_case(env, item)
    _add_decision(env, item, case, att, snap)
    with _client(_container(env)) as client:
        r = client.get(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/review-cases/{case.review_case_id}/detail")
        assert r.status_code == 200
        text = r.text
        for bad in ("actor_id", "r1", "api_key", "sk-", "http://", "https://",
                     "prompt", "evidence_body", "score_components", "rationale"):
            assert bad not in text, f"leaked: {bad}"


def test_manual_adjustment_context_uses_active_adoption_snapshot(tmp_path):
    env = _seed(tmp_path)
    item = base._item(env)
    case, attempt, snapshot = _open_case(env, item, review_case_id="rev-adjustment-ok")
    decision = _add_decision(env, item, case, attempt, snapshot, "dec-adjustment-ok")
    adoption = _adopt(env, item, case, decision, attempt, snapshot, "adp-adjustment-ok")
    with _client(_container(env)) as client:
        response = client.get(_detail_url(env, case))
    assert response.status_code == 200, response.text
    context = response.json()["manual_adjustment_context"]
    assert context == {
        "allowed": True,
        "block_reason_code": None,
        "attempt_id": attempt.attempt_id,
        "snapshot_id": snapshot.snapshot_id,
        "current_total_score": 90.0,
        "dimensions": [
            {"dimension_code": "objective", "current_score": 50.0,
             "min_score": 0.0, "max_score": 60.0},
            {"dimension_code": "subjective", "current_score": 40.0,
             "min_score": 0.0, "max_score": 40.0},
        ],
    }
    assert response.json()["active_adoption"]["adoption_id"] == adoption.adoption_id


def test_manual_adjustment_context_lock_disables_but_keeps_dimensions(tmp_path):
    from models.review_case import ManualFinalLock
    env = _seed(tmp_path)
    item = base._item(env)
    case, attempt, snapshot = _open_case(env, item, review_case_id="rev-adjustment-lock")
    decision = _add_decision(env, item, case, attempt, snapshot, "dec-adjustment-lock")
    adoption = _adopt(env, item, case, decision, attempt, snapshot, "adp-adjustment-lock")
    lock = ManualFinalLock(
        contract_version="score-attempt-review/v1", schema_version="manual-final-lock/v1",
        lock_id="lock-adjustment-context", task_id=env["task"].task_id,
        item_id=item.item_id, review_case_id=case.review_case_id,
        decision_id=decision.decision_id, adoption_id=adoption.adoption_id,
        snapshot_id=snapshot.snapshot_id,
        locked_by={"actor_type": "reviewer", "actor_id": "reviewer-lock"},
        locked_at=T0, reason_code="HUMAN_FINAL_LOCKED", status="active", revision=1,
        idempotency_key="lock-adjustment-context-idem", content_hash="ef" * 32)
    env["review_store"].create_final_lock(env["task"].task_id, item.item_id, lock)
    with _client(_container(env)) as client:
        response = client.get(_detail_url(env, case))
    context = response.json()["manual_adjustment_context"]
    assert context["allowed"] is False
    assert context["block_reason_code"] == "MANUAL_ADJUSTMENT_BLOCKED_BY_LOCK"
    assert len(context["dimensions"]) == 2


def test_manual_adjustment_context_missing_snapshot_fail_closed(tmp_path):
    env = _seed(tmp_path)
    item = base._item(env)
    case, attempt, snapshot = _open_case(env, item, review_case_id="rev-adjustment-missing")
    decision = _add_decision(env, item, case, attempt, snapshot, "dec-adjustment-missing")
    adoption = _adopt(env, item, case, decision, attempt, snapshot, "adp-adjustment-missing")
    env["attempt_store"].delete_snapshot(env["task"].task_id, item.item_id, snapshot.snapshot_id)
    with _client(_container(env)) as client:
        response = client.get(_detail_url(env, case))
    context = response.json()["manual_adjustment_context"]
    assert context["allowed"] is False
    assert context["block_reason_code"] == "ADJUSTMENT_SNAPSHOT_UNAVAILABLE"
    assert context["snapshot_id"] == adoption.snapshot_id
    assert context["dimensions"] == []


def test_manual_adjustment_context_empty_dimensions_fail_closed(tmp_path):
    class _NoDimensionFactory(_DimensionalSnapshotFactory):
        def build(self, **kwargs):
            snapshot = super().build(**kwargs)
            return snapshot.model_copy(update={
                "dimension_scores": [], "objective_score": None, "subjective_score": None})

    env = base._seed_env(tmp_path)
    env["orch"]._snapshots = _NoDimensionFactory()
    env["review_store"] = ReviewCaseStore(tmp_path / "reviews", attempt_store=env["attempt_store"])
    env["orch"]._review_store = env["review_store"]
    item = base._item(env)
    result = base._run(env["orch"].execute_score_item(
        env["task"].task_id, item.item_id, "exec-empty-dimensions"))
    assert result.outcome == "succeeded"
    case, attempt, snapshot = _open_case(env, item, review_case_id="rev-adjustment-empty")
    decision = _add_decision(env, item, case, attempt, snapshot, "dec-adjustment-empty")
    _adopt(env, item, case, decision, attempt, snapshot, "adp-adjustment-empty")
    with _client(_container(env)) as client:
        response = client.get(_detail_url(env, case))
    context = response.json()["manual_adjustment_context"]
    assert context["allowed"] is False
    assert context["block_reason_code"] == "ADJUSTMENT_DIMENSIONS_UNAVAILABLE"
    assert context["current_total_score"] == 90.0


def test_manual_adjustment_context_binding_conflict_rejected(tmp_path, monkeypatch):
    env = _seed(tmp_path)
    item = base._item(env)
    case, attempt, snapshot = _open_case(env, item, review_case_id="rev-adjustment-bind")
    decision = _add_decision(env, item, case, attempt, snapshot, "dec-adjustment-bind")
    adoption = _adopt(env, item, case, decision, attempt, snapshot, "adp-adjustment-bind")
    conflicting = adoption.model_copy(update={"review_case_id": "another-case"})
    monkeypatch.setattr(env["review_store"], "get_active_adoption", lambda *a, **k: conflicting)
    with _client(_container(env)) as client:
        response = client.get(_detail_url(env, case))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REVIEW_FACT_BINDING_MISMATCH"


def test_manual_adjustment_context_recursive_sensitive_scan(tmp_path):
    env = _seed(tmp_path)
    item = base._item(env)
    case, attempt, snapshot = _open_case(env, item, review_case_id="rev-adjustment-safe")
    decision = _add_decision(env, item, case, attempt, snapshot, "dec-adjustment-safe")
    _adopt(env, item, case, decision, attempt, snapshot, "adp-adjustment-safe")
    with _client(_container(env)) as client:
        response = client.get(_detail_url(env, case))
    assert response.status_code == 200
    text = response.text.lower()
    for forbidden in (
        "actor_id", "evidence_refs", "rationale_summary", "prompt", "provider_response",
        "api_key", "absolute_path", "student_name", "file_path", "flags"):
        assert forbidden not in text


def test_detail_sidecar_corrupt_500(tmp_path):
    env = _seed(tmp_path)
    item = base._item(env)
    case, _, _ = _open_case(env, item)
    # 破坏 case JSON
    import os
    case_dir = env["review_store"]._obj_dir(
        env["task"].task_id, item.item_id, "cases")
    case_path = case_dir / f"{case.review_case_id}.json"
    case_path.write_text("not valid json {{{", encoding="utf-8")
    with _client(_container(env)) as client:
        r = client.get(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/review-cases/{case.review_case_id}/detail")
        assert r.status_code == 500
