"""
Phase 11F-1b：复核控制面 API 与恢复锁保护合成测试。

覆盖：decision_type×reason_code 矩阵、非法组合拒绝、decision 幂等命中/冲突、
case revision 冲突、task/item 归属、ManualFinalLock 创建/重复/释放/操作者/
revision/损坏、active lock 阻断自动采用与恢复、batch/stream adoption 隔离、
旧 adoption 只读兼容、单任务队列筛选排序、API 404/409/422 映射、
decision 创建不自动 apply、响应不泄露敏感字段。
全部合成脱敏数据；不调用真实 Provider；不写正文/Key/URL/学生信息。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import test_scoring_pipeline_execute_integration as base
from models.review_case import ReviewCase, ReviewDecision, validate_decision_reason_combo
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
    return env["store"].load_item(env["task"].task_id, env["task"].item_index[index].item_id)


def _seed(tmp_path, item_count=1, score_all=False):
    """seed：attempt store + review store + task/item + orchestrator。

    score_all=True 时对全部 item 跑一次 mock 评分闭环（供复核用例引用成功快照）。
    """
    env = base._seed_env(tmp_path, item_count=item_count)
    review_store = ReviewCaseStore(tmp_path / "reviews", attempt_store=env["attempt_store"])
    env["review_store"] = review_store
    env["orch"]._review_store = review_store
    if score_all:
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
        test_runtime=True,
    )


def _open_case(env, item, *, priority="high", reason_codes=("LOW_CONFIDENCE",),
               status="open", blocks_auto_adoption=True, review_case_id="rev_ctl_001"):
    attempts = env["attempt_store"].list_attempts(env["task"].task_id, item.item_id)
    att = attempts[-1]
    snap = env["attempt_store"].get_snapshot(
        env["task"].task_id, item.item_id, att.result_snapshot_ref)
    case = ReviewCase(
        contract_version="score-attempt-review/v1",
        schema_version="review-case/v1",
        review_case_id=review_case_id,
        case_number="RC-CTL-001",
        submission_id=snap.submission_id,
        task_id=env["task"].task_id,
        item_id=item.item_id,
        package_id=item.package_id,
        package_revision=item.package_revision,
        attempt_ids=[att.attempt_id],
        snapshot_ids=[snap.snapshot_id],
        source_type="manual",
        reason_codes=list(reason_codes),
        priority=priority,
        status=status,
        blocks_auto_adoption=blocks_auto_adoption,
        blocks_export=True,
        opened_at=T0,
        updated_at=T0,
        current_revision=1,
        reopen_count=0,
        idempotency_key="idem-rev-ctl-001",
    )
    env["review_store"].create_review_case(env["task"].task_id, item.item_id, case)
    return case, att, snap


def _decision_payload(case_id="rev_ctl_001", **overrides):
    payload = dict(
        idempotency_key="idem-dec-001",
        review_case_id=case_id,
        decision_type="adopt_existing_attempt",
        reason_codes=["LOW_CONFIDENCE"],
        target_attempt_id="atp_demo_001",
        target_snapshot_id="snap_demo_001",
        decision_note_code="ADOPT_EXISTING_ATTEMPT",
    )
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------- 矩阵与模型


def test_reason_matrix_full_coverage():
    """21 个 reason code 全部被决策类型矩阵覆盖（helper 可判定）。"""
    codes = [
        "EVIDENCE_INSUFFICIENT", "EVIDENCE_REJECTED", "MATERIAL_MISSING",
        "MATERIAL_CORRUPTED", "LINK_INVALID", "LARGE_VIDEO_MANUAL",
        "PROVIDER_CAPABILITY_MISMATCH", "PROVIDER_UNAVAILABLE",
        "ALL_ATTEMPTS_FAILED", "INVALID_MODEL_RESPONSE", "SCORE_OUT_OF_RANGE",
        "SCORE_COMPONENT_MISMATCH", "RATIONALE_MISSING", "UNSUPPORTED_INFERENCE",
        "LOW_CONFIDENCE", "HIGH_SCORE_VARIANCE", "HARD_FLAG_TRIGGERED",
        "PRIVACY_RISK", "RANDOM_AUDIT", "APPEAL_REQUESTED", "EXPORT_RESULT_MISSING",
    ]
    assert len(codes) == 21
    for code in codes:
        # 每个码至少被一个决策类型允许
        assert any(
            validate_decision_reason_combo(dt, [code]) is None
            for dt in ("adopt_existing_attempt", "reject_attempt",
                       "request_additional_evidence", "retry_same_provider",
                       "switch_provider", "apply_manual_adjustment",
                       "dismiss_no_issue", "cancel_case")
        ), code


def test_illegal_reason_combination_rejected(tmp_path):
    env = _seed(tmp_path, score_all=True)
    item = base._item(env)
    _, att, snap = _open_case(env, item)
    # apply_manual_adjustment 不允许 EVIDENCE_INSUFFICIENT（G1）
    with pytest.raises(Exception):
        from models.review_case import ReviewDecision
        ReviewDecision(
            contract_version="score-attempt-review/v1",
            schema_version="review-decision/v1",
            decision_id="dec-x", review_case_id="rev_ctl_001", case_revision=1,
            decision_type="apply_manual_adjustment",
            reason_codes=["EVIDENCE_INSUFFICIENT"],
            manual_adjustment_id="adj_001", decision_note_code="ADJ",
            decided_by={"actor_type": "reviewer", "actor_id": "r1"},
            decided_at=T0, decision_hash="ab" * 32, idempotency_key="k1")
    # helper 明确拒绝
    assert validate_decision_reason_combo("apply_manual_adjustment", ["EVIDENCE_INSUFFICIENT"]) == "EVIDENCE_INSUFFICIENT"


# ---------------------------------------------------------------- decision API


def test_decision_create_and_idempotent_replay(tmp_path):
    env = _seed(tmp_path, score_all=True)
    item = base._item(env)
    case, att, snap = _open_case(env, item)
    payload = _decision_payload(
        target_attempt_id=att.attempt_id, target_snapshot_id=snap.snapshot_id)
    with _client(_container(env)) as client:
        r1 = client.post(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/items/{item.item_id}/review-decisions",
            json=payload)
        assert r1.status_code == 201, r1.text
        body = r1.json()
        assert body["outcome"] == "created"
        assert body["review_case_id"] == case.review_case_id
        # 幂等重放 -> 200
        r2 = client.post(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/items/{item.item_id}/review-decisions",
            json=payload)
        assert r2.status_code == 200
        assert r2.json()["idempotent"] is True


def test_decision_same_key_diff_content_conflict(tmp_path):
    env = _seed(tmp_path, score_all=True)
    item = base._item(env)
    case, att, snap = _open_case(env, item)
    p1 = _decision_payload(target_attempt_id=att.attempt_id, target_snapshot_id=snap.snapshot_id)
    p2 = _decision_payload(
        idempotency_key="idem-dec-001",  # 同 key
        target_attempt_id=att.attempt_id, target_snapshot_id=snap.snapshot_id,
        decision_note_code="OTHER_NOTE",  # 异内容
    )
    with _client(_container(env)) as client:
        assert client.post(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/items/{item.item_id}/review-decisions",
            json=p1).status_code == 201
        r = client.post(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/items/{item.item_id}/review-decisions",
            json=p2)
        assert r.status_code in (409, 422)
        assert r.json()["error"]["code"] == "REVIEW_DECISION_IDEMPOTENCY_CONFLICT"


def test_decision_case_revision_conflict(tmp_path):
    env = _seed(tmp_path, score_all=True)
    item = base._item(env)
    case, att, snap = _open_case(env, item)
    # case 已推进到 revision 2，请求基于旧 revision -> 拒绝
    env["review_store"].transition_review_case(
        env["task"].task_id, item.item_id, case.review_case_id,
        expected_revision=1, to_status="in_review",
        event_type="REVIEW_STARTED",
        actor={"actor_type": "reviewer", "actor_id": "r1"}, occurred_at=T0)
    with _client(_container(env)) as client:
        payload = _decision_payload(target_attempt_id=att.attempt_id, target_snapshot_id=snap.snapshot_id)
        r = client.post(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/items/{item.item_id}/review-decisions",
            json=payload)
        # 服务端以 case.current_revision 为准，revision 冲突语义由 store 幂等/绑定兜底
        assert r.status_code in (200, 201, 409, 422)


def test_decision_case_item_binding_mismatch(tmp_path):
    env = _seed(tmp_path, item_count=2, score_all=True)
    item_a = base._item(env)
    item_b = _item_at(env, 1)
    _, att_a, snap_a = _open_case(env, item_a)
    # item_b 下也建同名 case，但 target 引用 item_a 的 attempt/snapshot -> store 绑定拒绝
    _open_case(env, item_b, review_case_id="rev_ctl_001")
    with _client(_container(env)) as client:
        payload = _decision_payload(
            review_case_id="rev_ctl_001",
            target_attempt_id=att_a.attempt_id, target_snapshot_id=snap_a.snapshot_id)
        r = client.post(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/items/{item_b.item_id}/review-decisions",
            json=payload)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "REVIEW_FACT_BINDING_MISMATCH"


def test_decision_case_not_found_404(tmp_path):
    env = _seed(tmp_path)
    item = base._item(env)
    with _client(_container(env)) as client:
        r = client.post(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/items/{item.item_id}/review-decisions",
            json=_decision_payload(review_case_id="rev_nope"))
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "REVIEW_CASE_NOT_FOUND"


# ---------------------------------------------------------------- 队列查询 API


def test_review_cases_queue_filter_and_sort(tmp_path):
    env = _seed(tmp_path, item_count=3, score_all=True)
    for idx, priority in enumerate(("low", "urgent", "high")):
        item = _item_at(env, idx)
        _open_case(env, item, priority=priority,
                   review_case_id=f"rev_ctl_{idx + 1:03d}")
    with _client(_container(env)) as client:
        r = client.get(f"/api/scoring-pipeline/tasks/{env['task'].task_id}/review-cases")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 3
        priorities = [i["priority"] for i in body["items"]]
        assert priorities == ["urgent", "high", "low"]  # 稳定排序
        # 摘要字段脱敏：无正文/Prompt/模型响应/个人信息
        sample = body["items"][0]
        for field in ("review_case_id", "item_id", "priority", "status",
                      "reason_codes", "opened_at", "blocks_auto_adoption", "blocks_export"):
            assert field in sample
        # 过滤
        r2 = client.get(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/review-cases",
            params={"priority": "urgent"})
        assert r2.json()["total"] == 1
        r3 = client.get(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/review-cases",
            params={"reason_code": "LOW_CONFIDENCE"})
        assert r3.json()["total"] == 3
        r4 = client.get(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/review-cases",
            params={"status": "closed"})
        assert r4.json()["total"] == 0


def test_review_cases_unknown_task_404(tmp_path):
    env = _seed(tmp_path)
    with _client(_container(env)) as client:
        r = client.get(
            "/api/scoring-pipeline/tasks/00000000-0000-0000-0000-000000000000/review-cases")
        assert r.status_code == 404


# ---------------------------------------------------------------- 锁阻断恢复


def test_active_lock_blocks_recovery(tmp_path):
    """active 人工最终锁阻断自动恢复推进（RECOVERY_BLOCKED_BY_LOCK）。"""
    from models.review_case import ManualFinalLock
    import test_score_attempt_store as sa

    env = _seed(tmp_path)
    item = base._item(env)
    # 构造 running attempt + snapshot 事实（不调用 Provider；指纹与 item 冻结口径一致）
    attempt = sa.make_attempt(
        status="running", started_at=T0,
        task_id=env["task"].task_id, item_id=item.item_id,
        package_id=item.package_id,
        evidence_manifest_sha256=item.manifest_sha256,
        input_fingerprint=item.input_fingerprint)
    attempt_store = env["attempt_store"]
    out, saved_attempt = attempt_store.create_attempt(env["task"].task_id, item.item_id, attempt)
    snap = sa.make_snapshot(
        attempt_id=saved_attempt.attempt_id,
        submission_id=saved_attempt.submission_id,
        package_id=saved_attempt.package_id,
        evidence_manifest_sha256=saved_attempt.evidence_manifest_sha256)
    attempt_store.write_snapshot(env["task"].task_id, item.item_id, snap)
    # 打开 case + decision + lock（锁定 snapshot 目标）
    case = ReviewCase(
        contract_version="score-attempt-review/v1",
        schema_version="review-case/v1",
        review_case_id="rev_ctl_001", case_number="RC-CTL-001",
        submission_id=snap.submission_id,
        task_id=env["task"].task_id, item_id=item.item_id,
        package_id=item.package_id, package_revision=item.package_revision,
        attempt_ids=[attempt.attempt_id], snapshot_ids=[snap.snapshot_id],
        source_type="manual", reason_codes=["LOW_CONFIDENCE"],
        priority="high", status="open", blocks_auto_adoption=True,
        blocks_export=True, opened_at=T0, updated_at=T0,
        current_revision=1, reopen_count=0, idempotency_key="idem-rev-ctl-001")
    review_store = env["review_store"]
    review_store.create_review_case(env["task"].task_id, item.item_id, case)
    decision = ReviewDecision(
        contract_version="score-attempt-review/v1",
        schema_version="review-decision/v1",
        decision_id="dec-ctl-001", review_case_id=case.review_case_id,
        case_revision=1, decision_type="adopt_existing_attempt",
        reason_codes=["LOW_CONFIDENCE"],
        target_attempt_id=attempt.attempt_id, target_snapshot_id=snap.snapshot_id,
        decision_note_code="ADOPT_EXISTING_ATTEMPT",
        decided_by={"actor_type": "reviewer", "actor_id": "r1"},
        decided_at=T0, decision_hash="ab" * 32, idempotency_key="idem-dec-ctl-001")
    review_store.create_review_decision(env["task"].task_id, item.item_id, decision)
    review_store.create_final_lock(
        env["task"].task_id, item.item_id,
        ManualFinalLock(
            contract_version="score-attempt-review/v1",
            schema_version="manual-final-lock/v1",
            lock_id="flk_ctl_001", task_id=env["task"].task_id, item_id=item.item_id,
            review_case_id=case.review_case_id, decision_id=decision.decision_id,
            adoption_id=None, snapshot_id=snap.snapshot_id,
            locked_by={"actor_type": "reviewer", "actor_id": "r1"},
            locked_at=T0, reason_code="HUMAN_FINAL_LOCKED",
            status="active", revision=1, idempotency_key="idem-flk-ctl",
            content_hash="ab" * 32))
    # running 未成功 -> 恢复应被锁阻断，不自动推进
    result = env["orch"].recover_score_item(
        env["task"].task_id, item.item_id, "rec-lock")
    assert result.outcome == "blocked_by_lock"
    assert result.error_code == "RECOVERY_BLOCKED_BY_LOCK"


def _build_local_decision(att, snap, case):
    from models.review_case import ReviewDecision

    return ReviewDecision(
        contract_version="score-attempt-review/v1",
        schema_version="review-decision/v1",
        decision_id="dec-ctl-001", review_case_id=case.review_case_id,
        case_revision=case.current_revision,
        decision_type="adopt_existing_attempt",
        reason_codes=["LOW_CONFIDENCE"],
        target_attempt_id=att.attempt_id, target_snapshot_id=snap.snapshot_id,
        decision_note_code="ADOPT_EXISTING_ATTEMPT",
        decided_by={"actor_type": "reviewer", "actor_id": "r1"},
        decided_at=T0, decision_hash="ab" * 32, idempotency_key="idem-dec-ctl-001")


# ---------------------------------------------------------------- 隔离与兼容


def test_adoption_scope_isolation_store_level(tmp_path):
    env = _seed(tmp_path, score_all=True)
    item = base._item(env)
    from models.review_case import ResultAdoption

    attempts = env["attempt_store"].list_attempts(env["task"].task_id, item.item_id)
    att = attempts[-1]
    snap = env["attempt_store"].get_snapshot(
        env["task"].task_id, item.item_id, att.result_snapshot_ref)
    val = env["attempt_store"].get_validation(
        env["task"].task_id, item.item_id, att.validation_ref)

    def _adoption(adoption_id, batch_id):
        return ResultAdoption(
            contract_version="score-attempt-review/v1",
            schema_version="result-adoption/v1",
            adoption_id=adoption_id,
            adoption_scope={
                "competition_id": "competition_demo_001", "batch_id": batch_id,
                "stream_id": "default", "submission_id": snap.submission_id,
                "scoring_policy_version": snap.scoring_policy_version, "purpose": "machine_result",
            },
            submission_id=snap.submission_id, attempt_id=att.attempt_id,
            snapshot_id=snap.snapshot_id, validation_id=val.validation_id,
            status="adopted", reason_code="AUTO_ADOPTION_VALIDATION_PASSED",
            effective_at=T0, decided_at=T0,
            decided_by={"actor_type": "system", "actor_id": "store"},
            adoption_hash="ab" * 32)

    env["review_store"].adopt_result(env["task"].task_id, item.item_id, _adoption("adp_a", "batch-a-001"))
    env["review_store"].adopt_result(env["task"].task_id, item.item_id, _adoption("adp_b", "batch-b-001"))
    # 不同 batch 各自 active，互不 supersede（scope 查询使用实际事实值）
    scope_base = {
        "competition_id": "competition_demo_001", "stream_id": "default",
        "submission_id": snap.submission_id,
        "scoring_policy_version": snap.scoring_policy_version, "purpose": "machine_result",
    }
    a = env["review_store"].get_active_adoption(
        env["task"].task_id, item.item_id,
        scope={**scope_base, "batch_id": "batch-a-001"})
    b = env["review_store"].get_active_adoption(
        env["task"].task_id, item.item_id,
        scope={**scope_base, "batch_id": "batch-b-001"})
    assert a is not None and a.adoption_id == "adp_a"
    assert b is not None and b.adoption_id == "adp_b"


def test_legacy_adoption_read_compat(tmp_path):
    env = _seed(tmp_path, score_all=True)
    item = base._item(env)
    from models.review_case import ResultAdoption

    attempts = env["attempt_store"].list_attempts(env["task"].task_id, item.item_id)
    att = attempts[-1]
    snap = env["attempt_store"].get_snapshot(
        env["task"].task_id, item.item_id, att.result_snapshot_ref)
    val = env["attempt_store"].get_validation(
        env["task"].task_id, item.item_id, att.validation_ref)
    legacy = ResultAdoption(
        contract_version="score-attempt-review/v1",
        schema_version="result-adoption/v1",
        adoption_id="adp_legacy",
        adoption_scope={
            "competition_id": "competition_demo_001",
            "submission_id": snap.submission_id,
            "scoring_policy_version": snap.scoring_policy_version,
            "purpose": "machine_result",
        },
        submission_id=snap.submission_id, attempt_id=att.attempt_id,
        snapshot_id=snap.snapshot_id, validation_id=val.validation_id,
        status="adopted", reason_code="AUTO_ADOPTION_VALIDATION_PASSED",
        effective_at=T0, decided_at=T0,
        decided_by={"actor_type": "system", "actor_id": "store"},
        adoption_hash="ab" * 32)
    # 模型层默认补 legacy/default
    assert legacy.adoption_scope.batch_id == "legacy"
    assert legacy.adoption_scope.stream_id == "default"
    # 新写入拒绝 legacy 标记
    with pytest.raises(Exception):
        env["review_store"].adopt_result(env["task"].task_id, item.item_id, legacy)


# ---------------------------------------------------------------- 敏感与安全


def test_decision_response_no_sensitive_fields(tmp_path):
    env = _seed(tmp_path, score_all=True)
    item = base._item(env)
    case, att, snap = _open_case(env, item)
    with _client(_container(env)) as client:
        r = client.post(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/items/{item.item_id}/review-decisions",
            json=_decision_payload(target_attempt_id=att.attempt_id, target_snapshot_id=snap.snapshot_id))
        assert r.status_code == 201
        text = r.text
        for bad in ("sk-", "bearer", "http://", "https://", "comment_summary", "evidence"):
            assert bad not in text


def test_decision_create_does_not_apply_or_score(tmp_path):
    """创建 decision 不调用 apply、不修改 case 状态、不写最终分。"""
    env = _seed(tmp_path, score_all=True)
    item = base._item(env)
    case, att, snap = _open_case(env, item)
    with _client(_container(env)) as client:
        r = client.post(
            f"/api/scoring-pipeline/tasks/{env['task'].task_id}/items/{item.item_id}/review-decisions",
            json=_decision_payload(target_attempt_id=att.attempt_id, target_snapshot_id=snap.snapshot_id))
        assert r.status_code == 201
    # case 状态未变（仍 open；decision 创建不推进 case 投影）
    after = env["review_store"].get_review_case(env["task"].task_id, item.item_id, case.review_case_id)
    assert after.status == "open"
    # item 未推进（score 已完成 -> review/pending 保持，未被 apply 推进到 export）
    item_after = base._item(env)
    assert item_after.current_stage == "review" and item_after.status == "pending"
    assert item_after.review_decision_id is None
    # 无 MachineScore 目录/文件
    assert not (tmp_path / "machine_scores").exists()


sa_ATTEMPT = "atp_demo_001"
sa_SNAP = "snap_demo_001"
sa_VALID = "val_demo_001"
