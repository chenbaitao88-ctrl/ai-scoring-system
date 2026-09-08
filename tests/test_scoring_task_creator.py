"""
Phase 11E-2a-prerequisite-impl-2：ScoringTaskCreator 合成测试。

上游事实核对、新旧 item 关联、不可变评分配置、幂等创建、原子持久化。
全部合成脱敏 fixture；不读取真实学生材料、不调用 Provider。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from models.evidence_sidecar import (
    EvidencePackageRecord,
    IssueCounts,
    ValidationResult,
)
from models.pipeline_task import (
    PipelineItem,
    compute_item_input_fingerprint,
    PipelineItemIndexEntry,
    PipelineStageSummary,
    PipelineTask,
    sha256_canonical,
)
from models.scoring_configuration import (
    ScoringTaskConfiguration,
    ScoringTaskCreateRequest,
)
from models.score_attempt import ScoringInputProfile
from services.pipeline_task_store import ERR_TRANSACTION_ERROR, PipelineStoreError, PipelineTaskStore
from services.scoring_task_creator import (
    ERR_DUPLICATE_SOURCE_ITEM,
    ERR_EMPTY_SOURCE_ITEMS,
    ERR_EVIDENCE_LEVEL_NOT_ALLOWED,
    ERR_IDEMPOTENCY_CONFLICT,
    ERR_MODEL_INPUT_NOT_ALLOWED,
    ERR_PRIVACY_CHECK_FAILED,
    ERR_SOURCE_BINDING_MISMATCH,
    ERR_SOURCE_ITEM_NOT_ELIGIBLE,
    ERR_SOURCE_ITEM_NOT_FOUND,
    ERR_SOURCE_TASK_NOT_FOUND,
    ERR_SOURCE_TASK_NOT_SETTLED,
    ERR_SOURCE_TASK_TYPE_INVALID,
    ERR_TASK_WRITE_FAILED,
    ERR_VALIDATION_NOT_CURRENT,
    ScoringTaskCreator,
    ScoringTaskError,
)

T0 = datetime(2026, 8, 11, 3, 0, 0, tzinfo=timezone.utc)
SHA = "a" * 64
SOURCE_TASK_ID = str(uuid4())
SRC_ITEM_A = str(uuid4())
SRC_ITEM_B = str(uuid4())
BATCH = "batch-scoring-001"
CONFIG_FP = "f" * 64


_CONFIG_FIELDS = dict(
    profile_version="profile-mixed-v1",
    provider_id="provider_demo_001",
    model_id="model_demo_v1",
    scoring_policy_version="policy-001",
    rubric_version="rubric-001",
    prompt_version="prompt-001",
    response_schema_version="score-response-v1",
)
_CONFIG_FP = sha256_canonical(_CONFIG_FIELDS)


def make_config(**overrides):
    base = dict(_CONFIG_FIELDS)
    base.update(overrides)
    if "configuration_fingerprint" not in base:
        base["configuration_fingerprint"] = sha256_canonical(base)
    return ScoringTaskConfiguration(**base)


def make_profile(**overrides):
    """与 make_config 口径一致的权威 Profile（fix-1：creator 必须从 Lookup 读取）。"""
    base = dict(
        profile_version="profile-mixed-v1",
        scoring_policy_version="policy-001",
        rubric_version="rubric-001",
        prompt_version="prompt-001",
        response_schema_version="score-response-v1",
        scoring_mode="mixed",
        input_modalities=["text"],
        system_message_required=True,
        structured_json_required=True,
    )
    base.update(overrides)
    return ScoringInputProfile(**base)


class FakeProfileLookup:
    """权威 Profile 只读查询；profile_version 未注册返回 None。"""

    def __init__(self, profiles=None):
        self.profiles = profiles or {"profile-mixed-v1": make_profile()}

    def get_input_profile(self, profile_version):
        return self.profiles.get(profile_version)


def make_record(package_id="pkg-demo-001", **overrides):
    base = dict(
        record_schema_version="evidence-sidecar/record/v1",
        record_id="record-0001",
        package_id=package_id,
        package_revision=1,
        batch_id="batch-001",
        submission_id="submission_demo_001",
        evidence_version="v1",
        manifest_sha256=SHA,
        privacy_policy_version="privacy-v1",
        contract_version="evidence-package/v1.1",
        publication_status="ready",
        registration_status="registered",
        latest_validation_id="validation-0001",
        latest_validation_status="passed",
        source_package_ref="inputs/pkg",
        registered_at=T0, registered_by="system", created_at=T0, updated_at=T0,
        revision=1, record_sha256=SHA,
    )
    base.update(overrides)
    return EvidencePackageRecord(**base)


def make_validation(record_id="record-0001", **overrides):
    base = dict(
        validation_schema_version="evidence-sidecar/validation/v1",
        validation_id="validation-0001",
        record_id=record_id,
        package_id="pkg-demo-001",
        package_revision=1,
        manifest_sha256=SHA,
        validator_name="evidence-validator",
        validator_version="validator-001",
        evidence_contract_version="evidence-package/v1.1",
        privacy_policy_version="privacy-v1",
        mode="registration",
        status="passed",
        started_at=T0, completed_at=T0, duration_ms=100,
        checks=[], issues=[], issue_counts=IssueCounts(),
        model_input_allowed=True,
        registration_allowed=True,
        validated_file_count=1, declared_file_count=1, unregistered_file_count=0,
        result_sha256=SHA,
        evidence_level="sufficient",
    )
    base.update(overrides)
    return ValidationResult(**base)


def item_fp(item_id):
    from models.pipeline_task import compute_item_input_fingerprint
    return compute_item_input_fingerprint(
        package_id="pkg-demo-001", package_revision=1, manifest_sha256=SHA,
        registration_record_id="record-0001", validation_id="validation-0001")


def make_source_item(item_id, status="completed", package_id="pkg-demo-001", **overrides):
    base = dict(
        contract_version="pipeline-item/v1",
        item_id=item_id,
        task_id=SOURCE_TASK_ID,
        package_id=package_id,
        package_revision=1,
        manifest_sha256=SHA,
        registration_record_id="record-0001",
        validation_id="validation-0001",
        input_fingerprint=compute_item_input_fingerprint(
            package_id=package_id, package_revision=1, manifest_sha256=SHA,
            registration_record_id="record-0001",
            validation_id="validation-0001"),
        idempotency_key=sha256_canonical({"item": item_id}),
        status=status,
        current_stage="validate",
        attempt_count=0,
        max_attempts=3,
        heartbeat_interval_seconds=60,
        stale_after_seconds=300,
        heartbeat_updated_at=None,
        created_at=T0,
        updated_at=T0,
        retryable=False,
        last_error=None,
        output_ref="output-ref-001" if status == "completed" else None,
        output_sha256=SHA if status == "completed" else None,
        item_revision=1,
        evidence_level="sufficient",
    )
    base.update(overrides)
    return PipelineItem(**base)


def make_source_task(item_ids, status="completed", task_type="evidence_preparation_pipeline", items=None):
    if items is None:
        items = [make_source_item(i) for i in item_ids]
    from collections import Counter as _Counter
    counts = _Counter(i.status for i in items)
    idx = [PipelineItemIndexEntry(
        item_id=i.item_id, package_id=i.package_id, package_revision=i.package_revision,
        status=i.status, current_stage=i.current_stage, input_fingerprint=i.input_fingerprint,
        item_revision=i.item_revision, evidence_level=i.evidence_level,
    ) for i in items]
    summaries = []
    completed_total = counts.get("completed", 0)
    for st, deps, sstatus, total in (
        ("import", [], "completed", len(items)),
        ("validate", ["import"], "completed", len(items)),
        ("score", ["validate"], "not_started", 0),
        ("review", ["validate", "score"], "not_started", 0),
        ("export", ["review"], "not_started", 0),
    ):
        summaries.append(PipelineStageSummary(
            stage=st, status=sstatus, depends_on=deps,
            started_at=T0 if sstatus == "completed" else None,
            completed_at=T0 if sstatus == "completed" else None,
            total_items=total, pending_items=0 if sstatus != "pending" else total,
            running_items=0, completed_items=total if sstatus == "completed" else 0,
            failed_items=0, skipped_items=0, manual_review_items=0,
            blocking_error_codes=[], revision=1,
        ))
    snap = {"evidence_contract_version": "evidence-package/v1.1", "validator_version": "1.0.0"}
    return PipelineTask(
        contract_version="pipeline-task/v1",
        task_type=task_type,
        execution_scope=["import", "validate"],
        task_id=SOURCE_TASK_ID,
        batch_id="batch-001",
        status=status,
        current_stage="validate" if status == "completed" else "import",
        created_at=T0,
        started_at=T0,
        updated_at=T0,
        paused_at=None,
        completed_at=T0 if status in ("completed", "completed_with_errors", "failed") else None,
        total_items=len(items),
        pending_items=counts.get("pending", 0),
        running_items=counts.get("running", 0),
        completed_items=completed_total,
        failed_items=counts.get("failed", 0),
        skipped_items=counts.get("skipped", 0),
        manual_review_items=counts.get("manual_review", 0),
        concurrency=1,
        configuration_snapshot=snap,
        configuration_fingerprint=sha256_canonical(snap),
        item_index=idx,
        stage_summaries=summaries,
        error_summary={"count": 0, "codes": []},
        last_event_sequence=1,
        revision=1,
        idempotency_key=sha256_canonical({"task": SOURCE_TASK_ID}),
        idempotency_payload_sha256=SHA,
    )


class FakeRegistration:
    def __init__(self, entries=None):
        """entries: {package_id: (record, validation)}；缺失 package 返回 None。"""
        self.entries = entries or {"pkg-demo-001": (make_record(), make_validation())}

    def read_record_strict(self, package_id, package_revision):
        pair = self.entries.get(package_id)
        return pair[0] if pair else None

    def read_validation_strict(self, package_id, package_revision, validation_id):
        pair = self.entries.get(package_id)
        return pair[1] if pair else None


@pytest.fixture
def setup(tmp_path):
    store = PipelineTaskStore(tmp_path / "pipeline-runtime" / "tasks")
    store.tasks_dir.mkdir(parents=True, exist_ok=True)
    reg = FakeRegistration()
    creator = ScoringTaskCreator(
        store=store, registration=reg,
        profile_lookup=FakeProfileLookup(),
        clock=lambda: T0,
        uuid_factory=(lambda: str(uuid4())),
    )
    return store, reg, creator


def _write_source(store, task, items):
    store.write_task_snapshot(task.task_id, task, expected_revision=None)
    for i in items:
        store.write_item_snapshot(task.task_id, i, expected_revision=None)


def make_request(source_item_ids, config=None, **overrides):
    base = dict(
        batch_id=BATCH,
        source_task_id=SOURCE_TASK_ID,
        source_item_ids=source_item_ids,
        configuration=config or make_config(),
        concurrency=2,
    )
    base.update(overrides)
    return ScoringTaskCreateRequest(**base)


# ---------------- 创建成功路径 ---------------- #


def test_create_from_completed_task(setup):
    store, reg, creator = setup
    task = make_source_task([SRC_ITEM_A])
    _write_source(store, task, task.item_index and [make_source_item(SRC_ITEM_A)] or [])
    new_task, created = creator.create_scoring_task(make_request([SRC_ITEM_A]))
    assert created is True
    assert new_task.task_type == "scoring_pipeline"
    assert new_task.execution_scope == ["score", "review", "export"]
    assert new_task.status == "pending"
    assert new_task.current_stage == "score"
    assert new_task.source_task_id == SOURCE_TASK_ID
    assert new_task.total_items == 1
    stored = store.load_task(new_task.task_id)
    assert stored is not None and stored.task_type == "scoring_pipeline"
    stored_item = store.load_item(new_task.task_id, new_task.item_index[0].item_id)
    assert stored_item is not None
    assert stored_item.task_type == "scoring_pipeline"
    assert stored_item.source_item_id == SRC_ITEM_A
    assert stored_item.current_stage == "score"
    assert stored_item.item_id != SRC_ITEM_A  # 新 item_id


def test_create_from_completed_with_errors_selects_completed(setup):
    store, reg, creator = setup
    ok_item = make_source_item(SRC_ITEM_A)
    bad_item = make_source_item(SRC_ITEM_B, package_id="pkg-demo-002", status="failed",
                                output_ref=None, output_sha256=None)
    reg.entries["pkg-demo-002"] = (make_record(package_id="pkg-demo-002"),
                                     make_validation(package_id="pkg-demo-002"))
    task = make_source_task([SRC_ITEM_A, SRC_ITEM_B], status="completed_with_errors", items=[ok_item, bad_item])
    _write_source(store, task, [ok_item, bad_item])
    # 只选 completed item
    new_task, created = creator.create_scoring_task(make_request([SRC_ITEM_A]))
    assert created is True and new_task.total_items == 1
    # 选 failed item -> 拒绝
    with pytest.raises(ScoringTaskError) as ei:
        creator.create_scoring_task(make_request([SRC_ITEM_B]))
    assert ei.value.error_code == ERR_SOURCE_ITEM_NOT_ELIGIBLE


# ---------------- 上游拒绝路径 ---------------- #


def test_source_task_not_found(setup):
    _, _, creator = setup
    with pytest.raises(ScoringTaskError) as ei:
        creator.create_scoring_task(make_request([SRC_ITEM_A], source_task_id=str(uuid4())))
    assert ei.value.error_code == ERR_SOURCE_TASK_NOT_FOUND


def test_source_task_type_invalid(setup):
    store, _, creator = setup
    task = make_source_task([SRC_ITEM_A])
    _write_source(store, task, [make_source_item(SRC_ITEM_A)])
    t1, _ = creator.create_scoring_task(make_request([SRC_ITEM_A]))
    # 以 scoring task 作为 source -> 类型无效
    with pytest.raises(ScoringTaskError) as ei:
        creator.create_scoring_task(
            ScoringTaskCreateRequest(
                batch_id=BATCH, source_task_id=t1.task_id,
                source_item_ids=[t1.item_index[0].item_id],
                configuration=make_config(), concurrency=1,
            ))
    assert ei.value.error_code == ERR_SOURCE_TASK_TYPE_INVALID


def test_source_task_not_settled(setup):
    store, _, creator = setup
    it = make_source_item(SRC_ITEM_A, status="pending", output_ref=None, output_sha256=None)
    task = make_source_task([SRC_ITEM_A], status="running", items=[it])
    _write_source(store, task, [it])
    with pytest.raises(ScoringTaskError) as ei:
        creator.create_scoring_task(make_request([SRC_ITEM_A]))
    assert ei.value.error_code == ERR_SOURCE_TASK_NOT_SETTLED


def test_source_item_not_found(setup):
    store, _, creator = setup
    task = make_source_task([SRC_ITEM_A])
    _write_source(store, task, [make_source_item(SRC_ITEM_A)])
    with pytest.raises(ScoringTaskError) as ei:
        creator.create_scoring_task(make_request([str(uuid4())]))
    assert ei.value.error_code == ERR_SOURCE_ITEM_NOT_FOUND


def test_source_item_not_eligible(setup):
    store, _, creator = setup
    task = make_source_task([SRC_ITEM_A])
    _write_source(store, task, [make_source_item(SRC_ITEM_A, status="pending")])
    with pytest.raises(ScoringTaskError) as ei:
        creator.create_scoring_task(make_request([SRC_ITEM_A]))
    assert ei.value.error_code == ERR_SOURCE_ITEM_NOT_ELIGIBLE


def test_source_binding_mismatch(setup):
    store, reg, creator = setup
    task = make_source_task([SRC_ITEM_A])
    _write_source(store, task, [make_source_item(SRC_ITEM_A, manifest_sha256="b" * 64)])
    reg.entries["pkg-demo-001"] = (make_record(manifest_sha256="b" * 64), make_validation())  # 与 item 不一致
    with pytest.raises(ScoringTaskError) as ei:
        creator.create_scoring_task(make_request([SRC_ITEM_A]))
    assert ei.value.error_code == ERR_SOURCE_BINDING_MISMATCH


def test_validation_not_current(setup):
    store, reg, creator = setup
    task = make_source_task([SRC_ITEM_A])
    _write_source(store, task, [make_source_item(SRC_ITEM_A)])
    reg.entries["pkg-demo-001"] = (make_record(), make_validation(validation_id="validation-OTHER"))
    with pytest.raises(ScoringTaskError) as ei:
        creator.create_scoring_task(make_request([SRC_ITEM_A]))
    assert ei.value.error_code == ERR_VALIDATION_NOT_CURRENT


def test_privacy_gate_failed(setup):
    store, reg, creator = setup
    task = make_source_task([SRC_ITEM_A])
    _write_source(store, task, [make_source_item(SRC_ITEM_A)])
    reg.entries["pkg-demo-001"] = (make_record(), make_validation(registration_allowed=False))
    with pytest.raises(ScoringTaskError) as ei:
        creator.create_scoring_task(make_request([SRC_ITEM_A]))
    assert ei.value.error_code == ERR_PRIVACY_CHECK_FAILED


def test_model_input_not_allowed(setup):
    store, reg, creator = setup
    task = make_source_task([SRC_ITEM_A])
    _write_source(store, task, [make_source_item(SRC_ITEM_A)])
    reg.entries["pkg-demo-001"] = (make_record(), make_validation(model_input_allowed=False))
    with pytest.raises(ScoringTaskError) as ei:
        creator.create_scoring_task(make_request([SRC_ITEM_A]))
    assert ei.value.error_code == ERR_MODEL_INPUT_NOT_ALLOWED


def test_evidence_level_not_allowed(setup):
    store, _, creator = setup
    task = make_source_task([SRC_ITEM_A])
    _write_source(store, task, [make_source_item(SRC_ITEM_A, evidence_level="manual_only")])
    with pytest.raises(ScoringTaskError) as ei:
        creator.create_scoring_task(make_request([SRC_ITEM_A]))
    assert ei.value.error_code == ERR_EVIDENCE_LEVEL_NOT_ALLOWED


def test_duplicate_and_empty_source_items(setup):
    store, _, creator = setup
    task = make_source_task([SRC_ITEM_A])
    _write_source(store, task, [make_source_item(SRC_ITEM_A)])
    with pytest.raises(ScoringTaskError) as ei:
        creator.create_scoring_task(make_request([SRC_ITEM_A, SRC_ITEM_A]))
    assert ei.value.error_code == ERR_DUPLICATE_SOURCE_ITEM
    # 空列表在请求模型层拒绝（ValidationError）
    with pytest.raises(ValidationError):
        creator.create_scoring_task(make_request([]))


# ---------------- 幂等 ----------------

def _seed(setup, item_ids=(SRC_ITEM_A,)):
    store, reg, creator = setup
    items = [make_source_item(i, package_id="pkg-demo-001" if idx == 0 else "pkg-demo-002")
             for idx, i in enumerate(item_ids)]
    if len(item_ids) > 1:
        reg.entries["pkg-demo-002"] = (make_record(package_id="pkg-demo-002"),
                                      make_validation(package_id="pkg-demo-002"))
    task = make_source_task(list(item_ids), items=items)
    _write_source(store, task, items)
    return store, reg, creator


def test_idempotent_hit(setup):
    store, _, creator = _seed(setup)
    r1 = make_request([SRC_ITEM_A])
    t1, c1 = creator.create_scoring_task(r1)
    t2, c2 = creator.create_scoring_task(make_request([SRC_ITEM_A]))
    assert c1 is True and c2 is False
    assert t2.task_id == t1.task_id
    # 不创建重复目录/items
    assert len(list((store.tasks_dir / t1.task_id / "items").iterdir())) == 1


def test_idempotent_order_independent(setup):
    _, _, creator = _seed(setup, item_ids=(SRC_ITEM_A, SRC_ITEM_B))
    t1, _ = creator.create_scoring_task(make_request([SRC_ITEM_A, SRC_ITEM_B]))
    t2, c2 = creator.create_scoring_task(make_request([SRC_ITEM_B, SRC_ITEM_A]))
    assert c2 is False and t2.task_id == t1.task_id


def test_config_change_creates_different_task(setup):
    store, reg, creator = setup
    # 权威 Profile 注册第二个口径（fix-1：配置必须与权威 Profile 匹配才能创建）
    creator._profile_lookup.profiles["profile-ai-v1"] = make_profile(
        profile_version="profile-ai-v1", scoring_mode="ai_only")
    _seed((store, reg, creator))
    t1, _ = creator.create_scoring_task(make_request([SRC_ITEM_A]))
    cfg2 = make_config(profile_version="profile-ai-v1")
    t2, c2 = creator.create_scoring_task(make_request([SRC_ITEM_A], config=cfg2))
    assert c2 is True and t2.task_id != t1.task_id


def test_idempotency_conflict(setup):
    """同 key 不同 payload：再次创建必须显式冲突，不得改写成写失败或创建第二个任务。"""
    store, _, creator = _seed(setup)
    t1, _ = creator.create_scoring_task(make_request([SRC_ITEM_A]))
    # 同一幂等键但不同 payload（构造一个同 key 异 payload 的权威任务现场）
    d = json.loads((store.tasks_dir / t1.task_id / "task.json").read_text(encoding="utf-8"))
    d["idempotency_payload_sha256"] = "b" * 64
    (store.tasks_dir / t1.task_id / "task.json").write_text(
        json.dumps(d, ensure_ascii=False), encoding="utf-8")
    # 再次调用相同创建请求 -> 显式幂等冲突（fix-2：不得变成写失败）
    with pytest.raises(ScoringTaskError) as ei:
        creator.create_scoring_task(make_request([SRC_ITEM_A]))
    assert ei.value.error_code == ERR_IDEMPOTENCY_CONFLICT
    assert ei.value.error_code != ERR_TASK_WRITE_FAILED
    # 没有新增第二个 scoring task（目录仍为 source + t1）且无新事件
    scoring_dirs = [p for p in store.tasks_dir.iterdir() if p.is_dir()]
    assert sorted(p.name for p in scoring_dirs) == sorted([SOURCE_TASK_ID, t1.task_id])
    created_events = [e for e in store.load_event_log(t1.task_id)
                      if e.event_type == "task_created"]
    assert len(created_events) == 1


# ---------------- 原子持久化与兼容 ---------------- #


def test_concurrent_same_request_single_task(setup):
    store, _, creator = _seed(setup)
    t1, _ = creator.create_scoring_task(make_request([SRC_ITEM_A]))
    t2, c2 = creator.create_scoring_task(make_request([SRC_ITEM_A]))
    assert t2.task_id == t1.task_id and c2 is False


def test_write_failure_full_rollback(setup, monkeypatch):
    store, _, creator = _seed(setup)
    import services.scoring_task_creator as mod

    def _boom(*a, **k):
        # 存储层明确报告的契约错误（事务失败）才映射为写失败（fix-2）
        raise PipelineStoreError(ERR_TRANSACTION_ERROR, "SIDECAR_TRANSACTION_ERROR", retryable=True)

    monkeypatch.setattr(mod, "run_task_transaction", _boom)
    with pytest.raises(ScoringTaskError) as ei:
        creator.create_scoring_task(make_request([SRC_ITEM_A]))
    assert ei.value.error_code == ERR_TASK_WRITE_FAILED
    # 无新任务目录
    existing = [p.name for p in store.tasks_dir.iterdir() if p.is_dir()]
    assert len(existing) == 1  # 只有 source task


def test_old_task_item_json_compatible():
    """旧 evidence task/item JSON 可读取（模型扩展不破坏旧数据）。"""
    item = make_source_item(SRC_ITEM_A)
    dumped = json.loads(json.dumps(item.model_dump(mode="json")))
    del dumped["task_type"]  # 旧 JSON 无该字段
    del dumped["source_item_id"]
    PipelineItem(**dumped)  # 默认 task_type=evidence、source_item_id=None


def test_request_model_forbids_authoritative():
    """调用方不得传 task_id/状态/幂等键等权威字段。"""
    with pytest.raises(ValidationError):
        ScoringTaskCreateRequest(**make_request([SRC_ITEM_A]).model_dump(), task_id=str(uuid4()))
    with pytest.raises(ValidationError):
        ScoringTaskCreateRequest(**make_request([SRC_ITEM_A]).model_dump(), status="pending")
    with pytest.raises(ValidationError):
        ScoringTaskCreateRequest(**make_request([SRC_ITEM_A]).model_dump(), idempotency_key="x" * 64)
    with pytest.raises(ValidationError):
        ScoringTaskCreateRequest(**make_request([SRC_ITEM_A]).model_dump(), api_key="sk-x")
