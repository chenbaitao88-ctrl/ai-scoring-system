"""
Phase 11E-2a-prerequisite-impl-2-fix-2：错误传播收紧测试。

覆盖：已分类 ScoringTaskError 原样传播（不包装成写失败）；store 事务错误映射为
TASK_WRITE_FAILED；revision 冲突不降级为 NOT_FOUND；未知 RuntimeError 不被包装；
锁冲突仍为可重试稳定错误；真实并发测试继续通过。

故障注入一律使用 PipelineStoreError 真实错误码，不用 RuntimeError 冒充存储层契约错误。
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from models.pipeline_task import (
    PipelineItem,
    PipelineItemIndexEntry,
    PipelineStageSummary,
    PipelineTask,
    compute_item_input_fingerprint,
    sha256_canonical,
)
from models.score_attempt import ScoringInputProfile
from models.scoring_configuration import ScoringTaskConfiguration, ScoringTaskCreateRequest
from services.pipeline_task_store import (
    ERR_LOCK_CONFLICT,
    ERR_NOT_FOUND,
    ERR_REVISION_CONFLICT,
    ERR_TRANSACTION_ERROR,
    PipelineStoreError,
    PipelineTaskStore,
)
from services.scoring_task_creator import (
    ERR_CREATION_LOCK_CONFLICT,
    ERR_IDEMPOTENCY_CONFLICT,
    ERR_SOURCE_TASK_CORRUPTED,
    ERR_TASK_WRITE_FAILED,
    ScoringTaskCreator,
    ScoringTaskError,
)

T0 = datetime(2026, 8, 11, 3, 0, 0, tzinfo=timezone.utc)
SHA = "a" * 64
SOURCE_TASK_ID = str(uuid4())
SRC_ITEM_A = str(uuid4())
BATCH = "batch-scoring-001"

_CONFIG_FIELDS = dict(
    profile_version="profile-mixed-v1",
    provider_id="provider_demo_001",
    model_id="model_demo_v1",
    scoring_policy_version="policy-001",
    rubric_version="rubric-001",
    prompt_version="prompt-001",
    response_schema_version="score-response-v1",
)


def make_config(**overrides):
    fields = dict(_CONFIG_FIELDS)
    fields.update(overrides)
    if "configuration_fingerprint" not in fields:
        fields["configuration_fingerprint"] = sha256_canonical(fields)
    return ScoringTaskConfiguration(**fields)


def make_profile(**overrides):
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


class FakeRegistration:
    def __init__(self):
        pass

    def read_record_strict(self, package_id, package_revision):
        from models.evidence_sidecar import EvidencePackageRecord
        return EvidencePackageRecord(
            record_schema_version="evidence-sidecar/record/v1",
            record_id="record-0001", package_id=package_id, package_revision=1,
            batch_id="b", submission_id="s", evidence_version="v1", manifest_sha256=SHA,
            privacy_policy_version="p", contract_version="evidence-package/v1.1",
            publication_status="ready", registration_status="registered",
            latest_validation_id="validation-0001", latest_validation_status="passed",
            source_package_ref="x", registered_at=T0, registered_by="system",
            created_at=T0, updated_at=T0, revision=1, record_sha256=SHA)

    def read_validation_strict(self, package_id, package_revision, validation_id):
        from models.evidence_sidecar import IssueCounts, ValidationResult
        return ValidationResult(
            validation_schema_version="evidence-sidecar/validation/v1",
            validation_id="validation-0001", record_id="record-0001",
            package_id=package_id, package_revision=1, manifest_sha256=SHA,
            validator_name="v", validator_version="1", evidence_contract_version="evidence-package/v1.1",
            privacy_policy_version="p", mode="registration", status="passed",
            started_at=T0, completed_at=T0, duration_ms=1, issue_counts=IssueCounts(),
            model_input_allowed=True, registration_allowed=True,
            validated_file_count=1, declared_file_count=1, unregistered_file_count=0,
            result_sha256=SHA, evidence_level="sufficient")


class FakeProfileLookup:
    def __init__(self):
        self.profiles = {"profile-mixed-v1": make_profile()}

    def get_input_profile(self, profile_version):
        return self.profiles.get(profile_version)


def make_source_item(item_id):
    return PipelineItem(
        contract_version="pipeline-item/v1",
        item_id=item_id, task_id=SOURCE_TASK_ID, package_id="pkg-demo-001",
        package_revision=1, manifest_sha256=SHA,
        registration_record_id="record-0001", validation_id="validation-0001",
        input_fingerprint=compute_item_input_fingerprint(
            package_id="pkg-demo-001", package_revision=1, manifest_sha256=SHA,
            registration_record_id="record-0001", validation_id="validation-0001"),
        idempotency_key=sha256_canonical({"source": item_id}),
        status="completed", current_stage="validate", attempt_count=0, max_attempts=3,
        heartbeat_interval_seconds=60, stale_after_seconds=300, heartbeat_updated_at=None,
        created_at=T0, updated_at=T0, retryable=False, last_error=None,
        output_ref="output-ref-001", output_sha256=SHA, item_revision=1,
        evidence_level="sufficient")


def make_source_task():
    items = [make_source_item(SRC_ITEM_A)]
    idx = [PipelineItemIndexEntry(
        item_id=i.item_id, package_id=i.package_id, package_revision=i.package_revision,
        status=i.status, current_stage=i.current_stage, input_fingerprint=i.input_fingerprint,
        item_revision=i.item_revision, evidence_level=i.evidence_level) for i in items]
    summaries = []
    for st, deps, sstatus, total in (
        ("import", [], "completed", 1), ("validate", ["import"], "completed", 1),
        ("score", ["validate"], "not_started", 0), ("review", ["validate", "score"], "not_started", 0),
        ("export", ["review"], "not_started", 0),
    ):
        summaries.append(PipelineStageSummary(
            stage=st, status=sstatus, depends_on=deps,
            started_at=T0 if sstatus == "completed" else None,
            completed_at=T0 if sstatus == "completed" else None,
            total_items=total, pending_items=0, running_items=0,
            completed_items=total if sstatus == "completed" else 0,
            failed_items=0, skipped_items=0, manual_review_items=0,
            blocking_error_codes=[], revision=1))
    snap = {"evidence_contract_version": "evidence-package/v1.1", "validator_version": "1.0.0"}
    return PipelineTask(
        contract_version="pipeline-task/v1", task_type="evidence_preparation_pipeline",
        execution_scope=["import", "validate"], task_id=SOURCE_TASK_ID, batch_id="b",
        status="completed", current_stage="validate", created_at=T0, started_at=T0,
        updated_at=T0, paused_at=None, completed_at=T0, total_items=1, pending_items=0,
        running_items=0, completed_items=1, failed_items=0, skipped_items=0,
        manual_review_items=0, concurrency=1, configuration_snapshot=snap,
        configuration_fingerprint=sha256_canonical(snap), item_index=idx,
        stage_summaries=summaries,
        error_summary=__import__("models.pipeline_task", fromlist=["PipelineErrorSummary"]).PipelineErrorSummary(count=0, codes=[]),
        last_event_sequence=1, revision=1,
        idempotency_key=sha256_canonical({"task": SOURCE_TASK_ID}),
        idempotency_payload_sha256=SHA)


def make_request():
    return ScoringTaskCreateRequest(
        batch_id=BATCH, source_task_id=SOURCE_TASK_ID, source_item_ids=[SRC_ITEM_A],
        configuration=make_config(), concurrency=2)


def _env(tmp_path):
    store = PipelineTaskStore(tmp_path / "pipeline-runtime" / "tasks")
    store.tasks_dir.mkdir(parents=True, exist_ok=True)
    reg = FakeRegistration()
    lookup = FakeProfileLookup()
    creator = ScoringTaskCreator(
        store=store, registration=reg, profile_lookup=lookup,
        clock=lambda: T0, uuid_factory=(lambda: str(uuid4())))
    item = make_source_item(SRC_ITEM_A)
    store.write_task_snapshot(SOURCE_TASK_ID, make_source_task(), expected_revision=None)
    store.write_item_snapshot(SOURCE_TASK_ID, item, expected_revision=None)
    return store, creator


# ---------------- fix-2 错误传播 ---------------- #


def test_classified_error_passthrough(tmp_path, monkeypatch):
    """已分类 ScoringTaskError 在锁内原样传播，不得改写成写失败。"""
    store, creator = _env(tmp_path)

    def _conflict(*a, **k):
        raise ScoringTaskError(ERR_IDEMPOTENCY_CONFLICT)

    monkeypatch.setattr(creator, "_find_existing_by_idempotency", _conflict)
    with pytest.raises(ScoringTaskError) as ei:
        creator.create_scoring_task(make_request())
    assert ei.value.error_code == ERR_IDEMPOTENCY_CONFLICT
    assert ei.value.error_code != ERR_TASK_WRITE_FAILED


def test_task_corrupted_stays_corrupted(tmp_path):
    store, creator = _env(tmp_path)
    (store.tasks_dir / SOURCE_TASK_ID / "task.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(ScoringTaskError) as ei:
        creator.create_scoring_task(make_request())
    assert ei.value.error_code == ERR_SOURCE_TASK_CORRUPTED


def test_store_transaction_error_maps_to_write_failed(tmp_path, monkeypatch):
    store, creator = _env(tmp_path)
    import services.scoring_task_creator as mod

    def _txn_error(*a, **k):
        raise PipelineStoreError(ERR_TRANSACTION_ERROR, "SIDECAR_TRANSACTION_ERROR", retryable=True)

    monkeypatch.setattr(mod, "run_task_transaction", _txn_error)
    with pytest.raises(ScoringTaskError) as ei:
        creator.create_scoring_task(make_request())
    assert ei.value.error_code == ERR_TASK_WRITE_FAILED


def test_store_revision_conflict_not_mapped_to_not_found(tmp_path, monkeypatch):
    """revision 冲突是存储错误，不得降级成 NOT_FOUND 相关 scoring 错误。"""
    store, creator = _env(tmp_path)

    def _rev_conflict(task_id):
        raise PipelineStoreError(ERR_REVISION_CONFLICT, "SIDECAR_REVISION_CONFLICT", retryable=False)

    monkeypatch.setattr(store, "load_task", _rev_conflict)
    with pytest.raises(PipelineStoreError) as ei:
        creator.create_scoring_task(make_request())
    assert ei.value.error_code == ERR_REVISION_CONFLICT
    assert ei.value.error_code != ERR_NOT_FOUND


def test_unknown_runtime_error_not_wrapped(tmp_path, monkeypatch):
    """未知程序错误原样传播，不包装成写失败（暴露真实缺陷）。"""
    store, creator = _env(tmp_path)

    def _boom(task_id):
        raise RuntimeError("programming bug")

    monkeypatch.setattr(store, "load_task", _boom)
    with pytest.raises(RuntimeError):
        creator.create_scoring_task(make_request())

    # 写路径 RuntimeError 同样原样传播
    def _txn_boom(*a, **k):
        raise RuntimeError("write exploded")

    import services.scoring_task_creator as mod
    monkeypatch.setattr(mod, "run_task_transaction", _txn_boom)
    with pytest.raises(RuntimeError):
        creator.create_scoring_task(make_request())


def test_creation_lock_conflict_still_retryable(tmp_path, monkeypatch):
    store, creator = _env(tmp_path)

    def _lock_boom(key, timeout_ms=None):
        raise PipelineStoreError(ERR_LOCK_CONFLICT, "SIDECAR_LOCK_CONFLICT", retryable=True)

    monkeypatch.setattr(store, "acquire_creation_lock", _lock_boom)
    with pytest.raises(ScoringTaskError) as ei:
        creator.create_scoring_task(make_request())
    assert ei.value.error_code == ERR_CREATION_LOCK_CONFLICT
    assert ei.value.retryable is True


def test_real_concurrency_still_single_task(tmp_path):
    """fix-2 后真实并发仍只创建一个任务（回归）。"""
    store, creator = _env(tmp_path)
    req = make_request()
    results = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        results.append(creator.create_scoring_task(req))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert not any(t.is_alive() for t in threads), "并发创建线程超时（疑似死锁）"
    assert len({r[0].task_id for r in results}) == 1
    assert sorted(r[1] for r in results) == [False, True]
    scoring_dirs = [d for d in store.tasks_dir.iterdir()
                    if d.is_dir() and d.name != SOURCE_TASK_ID]
    assert len(scoring_dirs) == 1
    items = list((scoring_dirs[0] / "items").glob("*.json"))
    assert len(items) == 1
