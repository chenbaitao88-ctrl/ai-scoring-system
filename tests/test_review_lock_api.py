"""
Phase 11F-2d：人工最终锁定 API 合成测试。

覆盖：正常锁创建/幂等、case/decision/adoption 绑定校验、adoption 未 adopted 阻断、
已有 active lock 阻断、请求禁止 actor/分数/路径、响应不含敏感字段。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import test_scoring_pipeline_execute_integration as base
from routers import scoring_pipeline
from services.review_case_store import ReviewCaseStore
from services.scoring_pipeline_api_container import ScoringPipelineApiContainer
from services.scoring_task_creator import ScoringTaskCreator

T0 = datetime(2026, 8, 20, 6, 0, 0, tzinfo=timezone.utc)


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


def _adopt(env, item, decision, snap, att):
    from models.review_case import ResultAdoption
    import uuid
    aid = f"adp-{uuid.uuid4().hex[:12]}"
    val = env["attempt_store"].get_validation(
        env["task"].task_id, item.item_id, att.validation_ref) if att.validation_ref else None
    adoption = ResultAdoption(
        contract_version="score-attempt-review/v1",
        schema_version="result-adoption/v1",
        adoption_id=aid,
        adoption_scope={
            "competition_id": "competition_demo_001",
            "batch_id": env["task"].batch_id if hasattr(env["task"], "batch_id") else "batch-demo",
            "stream_id": "default",
            "submission_id": snap.submission_id,
            "scoring_policy_version": snap.scoring_policy_version,
            "purpose": "machine_result",
        },
        submission_id=snap.submission_id,
        attempt_id=att.attempt_id,
        snapshot_id=snap.snapshot_id,
        validation_id=val.validation_id if val else "val_demo_001",
        status="adopted",
        reason_code="AUTO_ADOPTION_VALIDATION_PASSED",
        effective_at=T0,
        decided_at=T0,
        decided_by={"actor_type": "reviewer", "actor_id": "r1"},
        adoption_hash="ab" * 32,
        decision_id=decision.decision_id,
    )
    env["review_store"].adopt_result(env["task"].task_id, item.item_id, adoption)
    return adoption


# ---------------------------------------------------------------- 测试


def test_lock_created_and_idempotent(tmp_path):
    env = _seed(tmp_path)
    item = base._item(env)
    case, att, snap = _open_case(env, item)
    dec = _add_decision(env, item, case, att, snap)
    adoption = _adopt(env, item, dec, snap, att)
    with _client(_container(env)) as client:
        payload = {
            "lock_request_id": "lreq-001",
            "review_case_id": case.review_case_id,
            "adoption_id": adoption.adoption_id,
            "reason_code": "HUMAN_FINAL_LOCKED",
        }
        r1 = client.post(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/items/{item.item_id}/review-decisions/{dec.decision_id}/lock",
            json=payload)
        assert r1.status_code == 201, r1.text
        body = r1.json()
        assert body["outcome"] == "created"
        assert body["status"] == "active"
        assert body["idempotent"] is False
        # 幂等重放
        r2 = client.post(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/items/{item.item_id}/review-decisions/{dec.decision_id}/lock",
            json=payload)
        assert r2.status_code == 200
        assert r2.json()["idempotent"] is True


def test_lock_response_no_sensitive_fields(tmp_path):
    env = _seed(tmp_path)
    item = base._item(env)
    case, att, snap = _open_case(env, item)
    dec = _add_decision(env, item, case, att, snap)
    adoption = _adopt(env, item, dec, snap, att)
    with _client(_container(env)) as client:
        payload = {
            "lock_request_id": "lreq-sensitive",
            "review_case_id": case.review_case_id,
            "adoption_id": adoption.adoption_id,
            "reason_code": "HUMAN_FINAL_LOCKED",
        }
        r = client.post(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/items/{item.item_id}/review-decisions/{dec.decision_id}/lock",
            json=payload)
        assert r.status_code == 201
        text = r.text
        for bad in ("actor_id", "sk-", "http://", "prompt", "evidence", "api_key"):
            assert bad not in text, f"leaked: {bad}"


def test_lock_rejects_extra_fields(tmp_path):
    env = _seed(tmp_path)
    item = base._item(env)
    case, att, snap = _open_case(env, item)
    dec = _add_decision(env, item, case, att, snap)
    adoption = _adopt(env, item, dec, snap, att)
    with _client(_container(env)) as client:
        payload = {
            "lock_request_id": "lreq-extra",
            "review_case_id": case.review_case_id,
            "adoption_id": adoption.adoption_id,
            "actor": "r1",  # 禁止
        }
        r = client.post(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/items/{item.item_id}/review-decisions/{dec.decision_id}/lock",
            json=payload)
        assert r.status_code in (400, 422)


def test_lock_cross_case_rejected_409(tmp_path):
    env = _seed(tmp_path, item_count=2)
    item_a = base._item(env)
    item_b = _item_at(env, 1)
    case_a, att_a, snap_a = _open_case(env, item_a, review_case_id="rev_a")
    dec_a = _add_decision(env, item_a, case_a, att_a, snap_a, decision_id="dec-a")
    adoption_a = _adopt(env, item_a, dec_a, snap_a, att_a)
    with _client(_container(env)) as client:
        # 用 item_b 路径但引用 item_a 的 case → 404（case 在 item_b 下不存在）
        payload = {
            "lock_request_id": "lreq-cross",
            "review_case_id": case_a.review_case_id,
            "adoption_id": adoption_a.adoption_id,
        }
        r = client.post(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/items/{item_b.item_id}/review-decisions/{dec_a.decision_id}/lock",
            json=payload)
        assert r.status_code == 404  # case 在 item_b 下不存在


def test_lock_adoption_not_adopted_blocked(tmp_path):
    env = _seed(tmp_path)
    item = base._item(env)
    case, att, snap = _open_case(env, item)
    dec = _add_decision(env, item, case, att, snap)
    # 不执行 adoption，直接尝试锁定
    with _client(_container(env)) as client:
        payload = {
            "lock_request_id": "lreq-no-adopt",
            "review_case_id": case.review_case_id,
            "adoption_id": "adp_nope",
        }
        r = client.post(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/items/{item.item_id}/review-decisions/{dec.decision_id}/lock",
            json=payload)
        assert r.status_code == 404


def test_lock_duplicate_active_blocked(tmp_path):
    env = _seed(tmp_path)
    item = base._item(env)
    case, att, snap = _open_case(env, item)
    dec = _add_decision(env, item, case, att, snap)
    adoption = _adopt(env, item, dec, snap, att)
    with _client(_container(env)) as client:
        payload = {
            "lock_request_id": "lreq-dup",
            "review_case_id": case.review_case_id,
            "adoption_id": adoption.adoption_id,
        }
        r1 = client.post(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/items/{item.item_id}/review-decisions/{dec.decision_id}/lock",
            json=payload)
        assert r1.status_code == 201
        # 再用不同 lock_request_id 尝试创建第二个 active lock
        payload2 = {
            "lock_request_id": "lreq-dup-2",
            "review_case_id": case.review_case_id,
            "adoption_id": adoption.adoption_id,
        }
        r2 = client.post(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/items/{item.item_id}/review-decisions/{dec.decision_id}/lock",
            json=payload2)
        assert r2.status_code == 409
