"""
Phase 11E-2b-1：ScoreAttemptStore 合成测试。

文件型事实存储：attempt/snapshot/validation 创建与读取、稳定排序、幂等、冲突、
引用一致性、非法 ID、损坏显式失败、写入回滚零残留、并发同 ID 写入、事件只追加、
成功快照保护、敏感内容扫描、PipelineTask 隔离、零 Provider/Gateway 调用。
全部合成脱敏数据；不写正文/Key/URL/学生信息。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from models.score_attempt import AttemptValidation, ScoreAttempt, ScoreResultSnapshot
from services.score_attempt_store import (
    ERR_ATTEMPT_CONFLICT,
    ERR_ATTEMPT_CORRUPTED,
    ERR_ATTEMPT_LOCK_CONFLICT,
    ERR_ATTEMPT_NOT_FOUND,
    ERR_ATTEMPT_WRITE_FAILED,
    ERR_BINDING_MISMATCH,
    ERR_EVENT_CORRUPTED,
    ERR_SNAPSHOT_CONFLICT,
    ERR_SNAPSHOT_CORRUPTED,
    ERR_SNAPSHOT_NOT_FOUND,
    ERR_UNSAFE_PATH,
    ERR_VALIDATION_CONFLICT,
    ERR_VALIDATION_CORRUPTED,
    ERR_VALIDATION_NOT_FOUND,
    ScoreAttemptStore,
    ScoreFactStoreError,
)

T0 = datetime(2026, 8, 12, 6, 0, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 8, 12, 6, 0, 1, tzinfo=timezone.utc)
T2 = datetime(2026, 8, 12, 6, 0, 2, tzinfo=timezone.utc)
TASK = "task_demo_001"
ITEM = "item_demo_001"
ATTEMPT_ID = "atp_demo_001"
SNAPSHOT_ID = "snap_demo_001"
VALIDATION_ID = "val_demo_001"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


# ---------------- 合成数据 ---------------- #


def make_attempt(attempt_id=ATTEMPT_ID, task_id=TASK, item_id=ITEM, status="created", **overrides):
    base = dict(
        contract_version="score-attempt-review/v1",
        schema_version="score-attempt/v1",
        attempt_id=attempt_id,
        attempt_number=1,
        task_id=task_id,
        item_id=item_id,
        submission_id="sub_demo_001",
        package_id="pkg_demo_001",
        package_revision=1,
        evidence_manifest_sha256=SHA_A,
        input_fingerprint=SHA_B,
        provider_id="provider_demo_001",
        provider_config_version="provider-config-v1",
        model_id="model_demo_001",
        model_capability_version="capability-v1",
        scoring_policy_version="policy-demo-v1",
        rubric_version="rubric-demo-v1",
        prompt_version="prompt-demo-v1",
        response_schema_version="score-response-v1",
        status=status,
        created_at=T0,
        created_by={"actor_type": "orchestrator", "actor_id": "pipeline-worker-01"},
    )
    if status == "succeeded":
        base.update(dict(
            started_at=T1, completed_at=T2, duration_ms=8000,
            response_hash=SHA_C, result_snapshot_ref=SNAPSHOT_ID,
            validation_ref=VALIDATION_ID,
        ))
    base.update(overrides)
    return ScoreAttempt(**base)


def make_snapshot(snapshot_id=SNAPSHOT_ID, attempt_id=ATTEMPT_ID, **overrides):
    base = dict(
        schema_version="score-result-snapshot/v1",
        snapshot_id=snapshot_id,
        attempt_id=attempt_id,
        submission_id="sub_demo_001",
        package_id="pkg_demo_001",
        evidence_manifest_sha256=SHA_A,
        scoring_policy_version="policy-demo-v1",
        rubric_version="rubric-demo-v1",
        response_schema_version="score-response-v1",
        score_scale={
            "scoring_policy_version": "policy-demo-v1",
            "total_score_range_ref": "range-total-100",
            "total_min": 0.0,
            "total_max": 100.0,
            "dimensions": [],
        },
        objective_score=58.0,
        subjective_score=24.0,
        dimension_scores=[{
            "dimension_code": "objective",
            "score": 58.0,
            "min_score": 0.0,
            "max_score": 60.0,
            "score_range_ref": "range-objective-60",
            "evidence_refs": [],
        }],
        total_score=82.0,
        rationale_summary="demo rationale",
        evidence_level="sufficient",
        evidence_refs=[],
        confidence="high",
        flags=[],
        manual_review_recommended=False,
        structure_validation_status="passed",
        score_range_validation_status="passed",
        result_hash=SHA_D,
        created_at=T2,
    )
    base.update(overrides)
    return ScoreResultSnapshot(**base)


def make_validation(validation_id=VALIDATION_ID, attempt_id=ATTEMPT_ID, snapshot_id=SNAPSHOT_ID, **overrides):
    base = dict(
        schema_version="attempt-validation/v1",
        validation_id=validation_id,
        attempt_id=attempt_id,
        snapshot_id=snapshot_id,
        validation_revision=1,
        validator_version="validator-demo-v1",
        checks=[{"check_code": "JSON_PARSEABLE", "passed": True}],
        overall_status="passed",
        adoption_candidate=True,
        manual_review_required=False,
        review_reason_codes=[],
        validated_at=T2,
        validated_by="validator-demo-01",
    )
    base.update(overrides)
    return AttemptValidation(**base)


@pytest.fixture
def store(tmp_path):
    return ScoreAttemptStore(tmp_path / "score-attempts")


def _attempt_path(store, task=TASK, item=ITEM, attempt_id=ATTEMPT_ID):
    return store.root / "tasks" / task / "items" / item / "attempts" / f"{attempt_id}.json"


def _events(store, task=TASK, item=ITEM):
    ep = store.root / "tasks" / task / "items" / item / "events.ndjson"
    if not ep.exists():
        return []
    return [json.loads(l) for l in ep.read_text(encoding="utf-8").splitlines() if l.strip()]


# ---------------- attempt：创建/读取/列表 ---------------- #


def test_create_attempt_and_read(store):
    out, obj = store.create_attempt(TASK, ITEM, make_attempt())
    assert out == "created"
    assert obj.attempt_id == ATTEMPT_ID
    loaded = store.get_attempt(TASK, ITEM, ATTEMPT_ID)
    assert loaded is not None and loaded.status == "created"
    assert loaded.attempt_number == 1


def test_get_attempt_missing_returns_none(store):
    assert store.get_attempt(TASK, ITEM, "atp_missing_001") is None


def test_list_attempts_stable_order(store):
    ids = ["atp_b_002", "atp_a_001", "atp_c_003"]
    for i, aid in enumerate(ids):
        store.create_attempt(TASK, ITEM, make_attempt(attempt_id=aid, attempt_number=i + 1))
    listed = store.list_attempts(TASK, ITEM)
    assert [a.attempt_id for a in listed] == sorted(ids)
    # 与 11E-1b ScoreAttemptLookup 对齐的别名
    assert [a.attempt_id for a in store.list_attempts_for_item(TASK, ITEM)] == sorted(ids)


def test_list_attempts_empty(store):
    assert store.list_attempts(TASK, ITEM) == []


# ---------------- attempt：幂等/冲突 ---------------- #


def test_attempt_idempotent_hit_no_duplicate_event(store):
    store.create_attempt(TASK, ITEM, make_attempt())
    before = _events(store)
    out, obj = store.create_attempt(TASK, ITEM, make_attempt())
    assert out == "idempotent_hit"
    assert obj.attempt_id == ATTEMPT_ID
    after = _events(store)
    assert len(after) == len(before) == 1  # 幂等命中不重复生成事件
    assert [e["event_type"] for e in after] == ["ATTEMPT_CREATED"]


def test_attempt_conflict_same_id_different_content(store):
    store.create_attempt(TASK, ITEM, make_attempt())
    with pytest.raises(ScoreFactStoreError) as ei:
        store.create_attempt(TASK, ITEM, make_attempt(attempt_number=2))
    assert ei.value.error_code == ERR_ATTEMPT_CONFLICT
    # 原事实未被覆盖
    loaded = store.get_attempt(TASK, ITEM, ATTEMPT_ID)
    assert loaded.attempt_number == 1


def test_attempt_create_only_no_overwrite_bytes(store):
    store.create_attempt(TASK, ITEM, make_attempt())
    p = _attempt_path(store)
    before = p.read_bytes()
    with pytest.raises(ScoreFactStoreError):
        store.create_attempt(TASK, ITEM, make_attempt(attempt_number=9))
    assert p.read_bytes() == before  # 文件字节不变


# ---------------- snapshot：创建/读取/幂等/冲突/成功保护 ---------------- #


def test_write_snapshot_and_read(store):
    store.create_attempt(TASK, ITEM, make_attempt(status="succeeded"))
    out, snap = store.write_snapshot(TASK, ITEM, make_snapshot())
    assert out == "created"
    assert snap.snapshot_id == SNAPSHOT_ID
    loaded = store.get_snapshot(TASK, ITEM, SNAPSHOT_ID)
    assert loaded is not None and loaded.total_score == 82.0


def test_snapshot_idempotent_hit(store):
    store.create_attempt(TASK, ITEM, make_attempt(status="succeeded"))
    store.write_snapshot(TASK, ITEM, make_snapshot())
    before = _events(store)
    out, _ = store.write_snapshot(TASK, ITEM, make_snapshot())
    assert out == "idempotent_hit"
    assert len(_events(store)) == len(before) == 2  # ATTEMPT + SNAPSHOT，无重复


def test_snapshot_conflict_success_never_overwritten(store):
    store.create_attempt(TASK, ITEM, make_attempt(status="succeeded"))
    store.write_snapshot(TASK, ITEM, make_snapshot())
    p = store.root / "tasks" / TASK / "items" / ITEM / "snapshots" / f"{SNAPSHOT_ID}.json"
    before = p.read_bytes()
    with pytest.raises(ScoreFactStoreError) as ei:
        store.write_snapshot(TASK, ITEM, make_snapshot(total_score=90.0))
    assert ei.value.error_code == ERR_SNAPSHOT_CONFLICT
    assert p.read_bytes() == before  # 已成功 snapshot 永不覆盖


def test_has_successful_snapshot(store):
    assert store.has_successful_snapshot(TASK, ITEM) is False
    store.create_attempt(TASK, ITEM, make_attempt(status="succeeded"))
    assert store.has_successful_snapshot(TASK, ITEM) is False  # 只有 attempt 无 snapshot
    store.write_snapshot(TASK, ITEM, make_snapshot())
    assert store.has_successful_snapshot(TASK, ITEM) is True
    assert store.has_successful_snapshot(TASK, ITEM, attempt_id=ATTEMPT_ID) is True
    assert store.has_successful_snapshot(TASK, ITEM, attempt_id="atp_other_001") is False


# ---------------- validation：创建/读取/幂等/冲突 ---------------- #


def test_write_validation_and_read(store):
    store.create_attempt(TASK, ITEM, make_attempt(status="succeeded"))
    store.write_snapshot(TASK, ITEM, make_snapshot())
    out, val = store.write_validation(TASK, ITEM, make_validation())
    assert out == "created"
    assert val.validation_id == VALIDATION_ID
    loaded = store.get_validation(TASK, ITEM, VALIDATION_ID)
    assert loaded is not None and loaded.overall_status == "passed"
    assert loaded.adoption_candidate is True


def test_validation_idempotent_and_conflict(store):
    store.create_attempt(TASK, ITEM, make_attempt(status="succeeded"))
    store.write_snapshot(TASK, ITEM, make_snapshot())
    store.write_validation(TASK, ITEM, make_validation())
    out, _ = store.write_validation(TASK, ITEM, make_validation())
    assert out == "idempotent_hit"
    with pytest.raises(ScoreFactStoreError) as ei:
        store.write_validation(TASK, ITEM, make_validation(validation_revision=2))
    assert ei.value.error_code == ERR_VALIDATION_CONFLICT


def test_list_validations_with_attempt_filter(store):
    store.create_attempt(TASK, ITEM, make_attempt(status="succeeded"))
    store.write_snapshot(TASK, ITEM, make_snapshot())
    store.write_validation(TASK, ITEM, make_validation())
    store.write_validation(TASK, ITEM, make_validation(
        validation_id="val_demo_002", validation_revision=1, checks=[{"check_code": "PRIVACY_OUTPUT_PASSED", "passed": True}],
    ))
    all_v = store.list_validations(TASK, ITEM)
    assert len(all_v) == 2
    filtered = store.list_validations(TASK, ITEM, attempt_id=ATTEMPT_ID)
    assert len(filtered) == 2
    assert store.list_validations(TASK, ITEM, attempt_id="atp_other_001") == []


# ---------------- 引用一致性：写时绑定校验 ---------------- #


def test_snapshot_requires_existing_attempt(store):
    with pytest.raises(ScoreFactStoreError) as ei:
        store.write_snapshot(TASK, ITEM, make_snapshot())
    assert ei.value.error_code == ERR_ATTEMPT_NOT_FOUND


def test_validation_requires_existing_attempt(store):
    store.create_attempt(TASK, ITEM, make_attempt(status="succeeded"))
    store.write_snapshot(TASK, ITEM, make_snapshot())
    with pytest.raises(ScoreFactStoreError) as ei:
        store.write_validation(TASK, ITEM, make_validation(attempt_id="atp_ghost_001"))
    assert ei.value.error_code == ERR_ATTEMPT_NOT_FOUND


def test_validation_requires_existing_snapshot(store):
    store.create_attempt(TASK, ITEM, make_attempt(status="succeeded"))
    with pytest.raises(ScoreFactStoreError) as ei:
        store.write_validation(TASK, ITEM, make_validation())
    assert ei.value.error_code == ERR_SNAPSHOT_NOT_FOUND


def test_validation_snapshot_attempt_mismatch(store):
    store.create_attempt(TASK, ITEM, make_attempt(status="succeeded"))
    store.write_snapshot(TASK, ITEM, make_snapshot())
    # attempt B 及其快照
    store.create_attempt(TASK, ITEM, make_attempt(attempt_id="atp_demo_002", attempt_number=2, status="succeeded",
                                                  result_snapshot_ref="snap_demo_002"))
    store.write_snapshot(TASK, ITEM, make_snapshot(snapshot_id="snap_demo_002", attempt_id="atp_demo_002"))
    # validation 声称校验 attempt A，但绑定快照属于 attempt B -> BINDING_MISMATCH
    with pytest.raises(ScoreFactStoreError) as ei:
        store.write_validation(TASK, ITEM, make_validation(attempt_id=ATTEMPT_ID, snapshot_id="snap_demo_002"))
    assert ei.value.error_code == ERR_BINDING_MISMATCH


def test_snapshot_cross_field_mismatch(store):
    store.create_attempt(TASK, ITEM, make_attempt(status="succeeded"))
    with pytest.raises(ScoreFactStoreError) as ei:
        store.write_snapshot(TASK, ITEM, make_snapshot(submission_id="sub_other_001"))
    assert ei.value.error_code == ERR_BINDING_MISMATCH
    with pytest.raises(ScoreFactStoreError) as ei:
        store.write_snapshot(TASK, ITEM, make_snapshot(evidence_manifest_sha256="e" * 64))
    assert ei.value.error_code == ERR_BINDING_MISMATCH


def test_attempt_task_item_context_mismatch(store):
    with pytest.raises(ScoreFactStoreError) as ei:
        store.create_attempt(TASK, ITEM, make_attempt(task_id="task_other_001"))
    assert ei.value.error_code == ERR_BINDING_MISMATCH
    with pytest.raises(ScoreFactStoreError) as ei:
        store.create_attempt(TASK, ITEM, make_attempt(item_id="item_other_001"))
    assert ei.value.error_code == ERR_BINDING_MISMATCH


# ---------------- verify_fact_bindings ---------------- #


def test_verify_fact_bindings_full_chain_ok(store):
    store.create_attempt(TASK, ITEM, make_attempt(status="succeeded"))
    store.write_snapshot(TASK, ITEM, make_snapshot())
    store.write_validation(TASK, ITEM, make_validation())
    assert store.verify_fact_bindings(TASK, ITEM, ATTEMPT_ID) is None


def test_verify_fact_bindings_attempt_not_found(store):
    with pytest.raises(ScoreFactStoreError) as ei:
        store.verify_fact_bindings(TASK, ITEM, "atp_ghost_001")
    assert ei.value.error_code == ERR_ATTEMPT_NOT_FOUND


def test_verify_fact_bindings_missing_snapshot(store):
    store.create_attempt(TASK, ITEM, make_attempt(status="succeeded"))
    with pytest.raises(ScoreFactStoreError) as ei:
        store.verify_fact_bindings(TASK, ITEM, ATTEMPT_ID)
    assert ei.value.error_code == ERR_SNAPSHOT_NOT_FOUND


def test_verify_fact_bindings_missing_validation(store):
    store.create_attempt(TASK, ITEM, make_attempt(status="succeeded"))
    store.write_snapshot(TASK, ITEM, make_snapshot())
    with pytest.raises(ScoreFactStoreError) as ei:
        store.verify_fact_bindings(TASK, ITEM, ATTEMPT_ID)
    assert ei.value.error_code == ERR_VALIDATION_NOT_FOUND


def test_verify_fact_bindings_validation_snapshot_mismatch(store):
    # attempt A 引用 snap_demo_001，validation 绑定 snap_demo_002（同属 A）-> 引用不一致
    store.create_attempt(TASK, ITEM, make_attempt(status="succeeded", result_snapshot_ref="snap_demo_001",
                                                  validation_ref="val_demo_001"))
    store.write_snapshot(TASK, ITEM, make_snapshot())  # snap_demo_001 属于 A
    store.write_snapshot(TASK, ITEM, make_snapshot(snapshot_id="snap_demo_002"))  # snap_demo_002 也属于 A
    store.write_validation(TASK, ITEM, make_validation(snapshot_id="snap_demo_002"))
    # validation.snapshot_id=snap_demo_002 != attempt.result_snapshot_ref=snap_demo_001 -> BINDING_MISMATCH
    with pytest.raises(ScoreFactStoreError) as ei:
        store.verify_fact_bindings(TASK, ITEM, ATTEMPT_ID)
    assert ei.value.error_code == ERR_BINDING_MISMATCH


def test_verify_fact_bindings_snapshot_attempt_mismatch(store):
    # attempt A 引用 snap_demo_002，但该快照属于 attempt B -> BINDING_MISMATCH
    store.create_attempt(TASK, ITEM, make_attempt(status="succeeded", result_snapshot_ref="snap_demo_002"))
    store.create_attempt(TASK, ITEM, make_attempt(attempt_id="atp_demo_002", attempt_number=2, status="succeeded",
                                                  result_snapshot_ref="snap_demo_002"))
    store.write_snapshot(TASK, ITEM, make_snapshot(snapshot_id="snap_demo_002", attempt_id="atp_demo_002"))
    with pytest.raises(ScoreFactStoreError) as ei:
        store.verify_fact_bindings(TASK, ITEM, ATTEMPT_ID)
    assert ei.value.error_code == ERR_BINDING_MISMATCH


# ---------------- 非法 ID 与路径穿越 ---------------- #


@pytest.mark.parametrize("bad_id", [
    "", "..", "../evil", "a/b", "a\\b", "a\x00b", "x" * 200, "con", "NUL", ".hidden",
    "with space", "中文id",
])
def test_invalid_ids_rejected(store, bad_id):
    """存储层 ID 校验独立于模型校验：model_copy 绕过 pydantic 后仍被 _validate_id 拦截。"""
    with pytest.raises(ScoreFactStoreError) as ei:
        store.get_attempt(bad_id, ITEM, ATTEMPT_ID)
    assert ei.value.error_code == ERR_UNSAFE_PATH
    with pytest.raises(ScoreFactStoreError) as ei:
        store.create_attempt(TASK, bad_id, make_attempt().model_copy(update={"item_id": bad_id}))
    assert ei.value.error_code == ERR_UNSAFE_PATH
    with pytest.raises(ScoreFactStoreError) as ei:
        store.create_attempt(TASK, ITEM, make_attempt().model_copy(update={"attempt_id": bad_id}))
    assert ei.value.error_code == ERR_UNSAFE_PATH


# ---------------- 损坏显式失败 ---------------- #


def test_corrupted_attempt_explicit(store):
    store.create_attempt(TASK, ITEM, make_attempt())
    _attempt_path(store).write_text("{broken", encoding="utf-8")
    with pytest.raises(ScoreFactStoreError) as ei:
        store.get_attempt(TASK, ITEM, ATTEMPT_ID)
    assert ei.value.error_code == ERR_ATTEMPT_CORRUPTED
    with pytest.raises(ScoreFactStoreError) as ei:
        store.list_attempts(TASK, ITEM)
    assert ei.value.error_code == ERR_ATTEMPT_CORRUPTED


def test_corrupted_snapshot_not_masked_as_no_success(store):
    store.create_attempt(TASK, ITEM, make_attempt(status="succeeded"))
    store.write_snapshot(TASK, ITEM, make_snapshot())
    sp = store.root / "tasks" / TASK / "items" / ITEM / "snapshots" / f"{SNAPSHOT_ID}.json"
    sp.write_text("not json at all", encoding="utf-8")
    with pytest.raises(ScoreFactStoreError) as ei:
        store.has_successful_snapshot(TASK, ITEM)
    assert ei.value.error_code == ERR_SNAPSHOT_CORRUPTED  # 不得伪装成"无成功快照"


def test_corrupted_validation_explicit(store):
    store.create_attempt(TASK, ITEM, make_attempt(status="succeeded"))
    store.write_snapshot(TASK, ITEM, make_snapshot())
    store.write_validation(TASK, ITEM, make_validation())
    vp = store.root / "tasks" / TASK / "items" / ITEM / "validations" / f"{VALIDATION_ID}.json"
    vp.write_text('{"schema_version": 123}', encoding="utf-8")  # schema 不合法
    with pytest.raises(ScoreFactStoreError) as ei:
        store.get_validation(TASK, ITEM, VALIDATION_ID)
    assert ei.value.error_code == ERR_VALIDATION_CORRUPTED


def test_corrupted_events_explicit(store):
    store.create_attempt(TASK, ITEM, make_attempt())
    ep = store.root / "tasks" / TASK / "items" / ITEM / "events.ndjson"
    ep.write_text("{broken line\n", encoding="utf-8")
    with pytest.raises(ScoreFactStoreError) as ei:
        store.load_events(TASK, ITEM)
    assert ei.value.error_code == ERR_EVENT_CORRUPTED


# ---------------- 写入失败回滚：零半成品、零空业务目录 ---------------- #


def test_attempt_write_failure_no_partial_no_empty_dirs(store, monkeypatch):
    import services.score_attempt_store as mod

    def _boom(target, data):
        raise OSError("disk full")

    monkeypatch.setattr(mod, "_atomic_write_bytes", _boom)
    with pytest.raises(ScoreFactStoreError) as ei:
        store.create_attempt(TASK, ITEM, make_attempt())
    assert ei.value.error_code == ERR_ATTEMPT_WRITE_FAILED
    # 无半成品
    assert store.get_attempt(TASK, ITEM, ATTEMPT_ID) is None
    # 无空业务目录（tasks/... 全链被清理）
    assert not (store.root / "tasks" / TASK).exists()
    assert not (store.root / "tasks").exists()


def test_event_append_failure_rolls_back_object(store, monkeypatch):
    import services.score_attempt_store as mod

    def _boom(path, line):
        raise OSError("event append failed")

    monkeypatch.setattr(mod, "_append_ndjson_bytes", _boom)
    with pytest.raises(ScoreFactStoreError) as ei:
        store.create_attempt(TASK, ITEM, make_attempt())
    assert ei.value.error_code == ERR_ATTEMPT_WRITE_FAILED
    # 对象被回滚删除，不留下"有事实无事件"的半成品
    assert store.get_attempt(TASK, ITEM, ATTEMPT_ID) is None
    assert not (store.root / "tasks" / TASK).exists()


def test_snapshot_write_failure_no_partial(store, monkeypatch):
    import services.score_attempt_store as mod
    store.create_attempt(TASK, ITEM, make_attempt(status="succeeded"))

    def _boom(target, data):
        raise OSError("disk full")

    monkeypatch.setattr(mod, "_atomic_write_bytes", _boom)
    with pytest.raises(ScoreFactStoreError):
        store.write_snapshot(TASK, ITEM, make_snapshot())
    assert store.get_snapshot(TASK, ITEM, SNAPSHOT_ID) is None
    assert store.has_successful_snapshot(TASK, ITEM) is False
    # attempts 目录保留（已有 attempt 数据），items 目录不被误删
    assert store.get_attempt(TASK, ITEM, ATTEMPT_ID) is not None


def test_validation_event_failure_rolls_back(store, monkeypatch):
    import services.score_attempt_store as mod
    store.create_attempt(TASK, ITEM, make_attempt(status="succeeded"))
    store.write_snapshot(TASK, ITEM, make_snapshot())

    def _boom(path, line):
        raise OSError("event append failed")

    monkeypatch.setattr(mod, "_append_ndjson_bytes", _boom)
    with pytest.raises(ScoreFactStoreError):
        store.write_validation(TASK, ITEM, make_validation())
    assert store.get_validation(TASK, ITEM, VALIDATION_ID) is None
    # 既有事实不受影响
    assert store.get_attempt(TASK, ITEM, ATTEMPT_ID) is not None
    assert store.get_snapshot(TASK, ITEM, SNAPSHOT_ID) is not None


# ---------------- 并发同 ID 写入 ---------------- #


def test_concurrent_same_id_single_first(store):
    results = []
    barrier = threading.Barrier(2)

    def writer():
        barrier.wait()
        try:
            out, _ = store.create_attempt(TASK, ITEM, make_attempt())
            results.append(out)
        except ScoreFactStoreError as exc:
            results.append(exc.error_code)

    t1 = threading.Thread(target=writer)
    t2 = threading.Thread(target=writer)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    outcomes = sorted(results)
    # 一个首次成功 created，另一个幂等命中 idempotent_hit（同一内容）
    assert outcomes == ["created", "idempotent_hit"]
    events = _events(store)
    assert len(events) == 1  # 只追加一次事件
    assert store.get_attempt(TASK, ITEM, ATTEMPT_ID) is not None


def test_concurrent_same_id_different_content_conflict(store):
    results = []
    barrier = threading.Barrier(2)

    def writer(number):
        barrier.wait()
        try:
            out, _ = store.create_attempt(TASK, ITEM, make_attempt(attempt_number=number))
            results.append(out)
        except ScoreFactStoreError as exc:
            results.append(exc.error_code)

    t1 = threading.Thread(target=writer, args=(1,))
    t2 = threading.Thread(target=writer, args=(2,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert "created" in results
    assert ERR_ATTEMPT_CONFLICT in results


# ---------------- 事件：只追加 ---------------- #


def test_events_append_only_and_sequences(store):
    store.create_attempt(TASK, ITEM, make_attempt(status="succeeded"))
    store.write_snapshot(TASK, ITEM, make_snapshot())
    store.write_validation(TASK, ITEM, make_validation())
    events = _events(store)
    assert [e["event_type"] for e in events] == ["ATTEMPT_CREATED", "SNAPSHOT_CREATED", "VALIDATION_CREATED"]
    assert [e["sequence"] for e in events] == [1, 2, 3]
    assert all(e["object_id"] and e["task_id"] == TASK and e["item_id"] == ITEM for e in events)
    # 再次幂等写不新增事件
    store.create_attempt(TASK, ITEM, make_attempt(status="succeeded"))
    store.write_snapshot(TASK, ITEM, make_snapshot())
    assert len(_events(store)) == 3


def test_events_isolated_per_item(store):
    store.create_attempt(TASK, ITEM, make_attempt())
    store.create_attempt(TASK, "item_demo_002", make_attempt(item_id="item_demo_002"))
    assert len(_events(store, item=ITEM)) == 1
    assert len(_events(store, item="item_demo_002")) == 1


# ---------------- 重启可读 ---------------- #


def test_restart_readable(tmp_path):
    root = tmp_path / "score-attempts"
    s1 = ScoreAttemptStore(root)
    s1.create_attempt(TASK, ITEM, make_attempt(status="succeeded"))
    s1.write_snapshot(TASK, ITEM, make_snapshot())
    s1.write_validation(TASK, ITEM, make_validation())
    s2 = ScoreAttemptStore(root)
    assert s2.get_attempt(TASK, ITEM, ATTEMPT_ID) is not None
    assert s2.get_snapshot(TASK, ITEM, SNAPSHOT_ID) is not None
    assert s2.get_validation(TASK, ITEM, VALIDATION_ID) is not None
    assert s2.has_successful_snapshot(TASK, ITEM) is True
    assert s2.verify_fact_bindings(TASK, ITEM, ATTEMPT_ID) is None


# ---------------- 安全边界 ---------------- #


def test_error_messages_sensitive_scan(store):
    """所有触发的错误 str 只含稳定错误码，不含路径/Key/URL/正文/ID。"""
    store.create_attempt(TASK, ITEM, make_attempt())
    cases = [
        lambda: store.create_attempt(TASK, ITEM, make_attempt(attempt_number=2)),       # ATTEMPT_CONFLICT
        lambda: store.write_snapshot(TASK, ITEM, make_snapshot()),                        # ATTEMPT_NOT_FOUND
        lambda: store.create_attempt(TASK, "../evil", make_attempt().model_copy(update={"item_id": "../evil"})),  # UNSAFE_PATH
    ]
    root_str = str(store.root)
    for fn in cases:
        try:
            fn()
        except ScoreFactStoreError as exc:
            msg = str(exc)
            assert "Users" not in msg
            assert ":\\" not in msg and "/" not in msg
            assert root_str not in msg
            assert TASK not in msg and ITEM not in msg and ATTEMPT_ID not in msg
            low = msg.lower()
            for bad in ("api_key", "bearer", "sk-", "token", "http://", "https://", "secret"):
                assert bad not in low


def test_persisted_files_sensitive_scan(store):
    store.create_attempt(TASK, ITEM, make_attempt(status="succeeded"))
    store.write_snapshot(TASK, ITEM, make_snapshot())
    store.write_validation(TASK, ITEM, make_validation())
    for f in (store.root / "tasks").rglob("*.json"):
        low = f.read_text(encoding="utf-8").lower()
        for bad in ("top_secret", "bearer", "sk-", "api_key", "http://", "https://", "张", "李"):
            assert bad not in low
    for f in (store.root / "tasks").rglob("*.ndjson"):
        low = f.read_text(encoding="utf-8").lower()
        for bad in ("top_secret", "bearer", "api_key"):
            assert bad not in low


def test_store_does_not_touch_pipeline_files(tmp_path):
    """store 只写自己的注入根目录，不修改任何 PipelineTask 文件（隔离验证）。"""
    root = tmp_path / "score-attempts"
    store = ScoreAttemptStore(root)
    before = sorted(str(p) for p in tmp_path.rglob("*") if p.is_file())
    store.create_attempt(TASK, ITEM, make_attempt())
    after = sorted(str(p) for p in tmp_path.rglob("*") if p.is_file())
    added = [p for p in after if p not in before]
    assert added
    assert all(p.startswith(str(root)) for p in added)


def test_store_no_gateway_provider_resolver():
    """store 源码不引用 Gateway/Provider 调用/CredentialResolver 调用/httpx/requests/环境变量。
    （docstring 中允许出现能力边界描述词，检查的是 import 与调用形态。）"""
    import services.score_attempt_store as mod
    src = open(mod.__file__, encoding="utf-8").read()
    for bad in ("ScoringProviderGateway", "httpx", "os.getenv", "environ"):
        assert bad not in src, bad
    assert "CredentialResolver(" not in src
    assert "import requests" not in src
    assert "urllib" not in src


def test_store_satisfies_readonly_protocol(store):
    """store 是 11E-2b-1 ScoreFactStoreLookup 只读协议的结构子类型。"""
    from services.score_pipeline_protocols import ScoreFactStoreLookup
    assert isinstance(store, ScoreFactStoreLookup)
    for name in ("get_attempt", "list_attempts", "get_snapshot",
                 "has_successful_snapshot", "get_validation", "list_validations"):
        assert callable(getattr(store, name))
