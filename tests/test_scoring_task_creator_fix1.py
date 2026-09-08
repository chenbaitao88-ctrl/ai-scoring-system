"""
Phase 11E-2a-prerequisite-impl-2-fix-1：ScoringTaskCreator 加固测试。

覆盖：独立导入顺序、真实并发幂等（双线程 Barrier）、创建锁冲突、source item
权威快照不二次读取、损坏显式传播、未知异常不吞、Profile 权威绑定、
concurrency 上限、指纹内容一致性、旧 evidence 配置指纹回归。

全部合成脱敏 fixture；不读取真实学生材料、不调用 Provider。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
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
    PipelineItemIndexEntry,
    PipelineStageSummary,
    PipelineTask,
    compute_item_input_fingerprint,
    sha256_canonical,
)
from models.score_attempt import ScoringInputProfile
from models.scoring_configuration import (
    MAX_SCORING_CONCURRENCY,
    ScoringTaskConfiguration,
    ScoringTaskCreateRequest,
)
from services.evidence_registration_service import ERR_RECORD_CORRUPTED, RegistrationError
from services.pipeline_task_store import (
    ERR_LOCK_CONFLICT,
    PipelineStoreError,
    PipelineTaskStore,
)
from services.scoring_task_creator import (
    ERR_CONFIGURATION_PROFILE_MISMATCH,
    ERR_CREATION_LOCK_CONFLICT,
    ERR_EVIDENCE_RECORD_CORRUPTED,
    ERR_PROFILE_NOT_FOUND,
    ERR_SOURCE_ITEM_CORRUPTED,
    ERR_SOURCE_TASK_CORRUPTED,
    ERR_VALIDATION_CORRUPTED,
    ScoringTaskCreator,
    ScoringTaskError,
)

T0 = datetime(2026, 8, 11, 3, 0, 0, tzinfo=timezone.utc)
SHA = "a" * 64
SOURCE_TASK_ID = str(uuid4())
SRC_ITEM_A = str(uuid4())
SRC_ITEM_B = str(uuid4())
BATCH = "batch-scoring-001"

_BACKEND_DIR = str(Path(__file__).resolve().parents[1] / "backend")

_CONFIG_FIELDS = dict(
    profile_version="profile-mixed-v1",
    provider_id="provider_demo_001",
    model_id="model_demo_v1",
    scoring_policy_version="policy-001",
    rubric_version="rubric-001",
    prompt_version="prompt-001",
    response_schema_version="score-response-v1",
)


# ---------------- fixture 构造 ---------------- #


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


def make_record(package_id="pkg-demo-001", **overrides):
    base = dict(
        record_schema_version="evidence-sidecar/record/v1",
        record_id="record-0001",
        package_id=package_id,
        package_revision=1,
        batch_id="batch-001",
        submission_id="sub-001",
        evidence_version="v1",
        manifest_sha256=SHA,
        privacy_policy_version="pp-001",
        contract_version="evidence-package/v1.1",
        publication_status="ready",
        registration_status="registered",
        latest_validation_id="validation-0001",
        latest_validation_status="passed",
        source_package_ref="ref-001",
        registered_at=T0,
        registered_by="system",
        created_at=T0,
        updated_at=T0,
        revision=1,
        record_sha256=SHA,
    )
    base.update(overrides)
    return EvidencePackageRecord(**base)


def make_validation(package_id="pkg-demo-001", **overrides):
    base = dict(
        validation_schema_version="evidence-sidecar/validation/v1",
        validation_id="validation-0001",
        record_id="record-0001",
        package_id=package_id,
        package_revision=1,
        manifest_sha256=SHA,
        validator_name="validator-demo",
        validator_version="1.0.0",
        evidence_contract_version="evidence-package/v1.1",
        privacy_policy_version="pp-001",
        mode="registration",
        status="passed",
        started_at=T0,
        completed_at=T0,
        duration_ms=1,
        issue_counts=IssueCounts(),
        model_input_allowed=True,
        registration_allowed=True,
        validated_file_count=1,
        declared_file_count=1,
        unregistered_file_count=0,
        result_sha256=SHA,
        evidence_level="sufficient",
    )
    base.update(overrides)
    return ValidationResult(**base)


def item_fp(package_id="pkg-demo-001"):
    return compute_item_input_fingerprint(
        package_id=package_id, package_revision=1, manifest_sha256=SHA,
        registration_record_id="record-0001", validation_id="validation-0001",
    )


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
        input_fingerprint=item_fp(package_id),
        idempotency_key=sha256_canonical({"source": item_id}),
        status=status,
        current_stage="validate" if status == "completed" else "import",
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


def make_source_task(item_ids, status="completed", items=None):
    if items is None:
        items = [make_source_item(i) for i in item_ids]
    from collections import Counter
    counts = Counter(i.status for i in items)
    idx = [PipelineItemIndexEntry(
        item_id=i.item_id, package_id=i.package_id, package_revision=i.package_revision,
        status=i.status, current_stage=i.current_stage, input_fingerprint=i.input_fingerprint,
        item_revision=i.item_revision, evidence_level=i.evidence_level,
    ) for i in items]
    summaries = []
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
            total_items=total, pending_items=0,
            running_items=0, completed_items=total if sstatus == "completed" else 0,
            failed_items=0, skipped_items=0, manual_review_items=0,
            blocking_error_codes=[], revision=1,
        ))
    snap = {"evidence_contract_version": "evidence-package/v1.1", "validator_version": "1.0.0"}
    return PipelineTask(
        contract_version="pipeline-task/v1",
        task_type="evidence_preparation_pipeline",
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
        completed_items=counts.get("completed", 0),
        failed_items=counts.get("failed", 0),
        skipped_items=counts.get("skipped", 0),
        manual_review_items=counts.get("manual_review", 0),
        concurrency=1,
        configuration_snapshot=snap,
        configuration_fingerprint=sha256_canonical(snap),
        item_index=idx,
        stage_summaries=summaries,
        error_summary=__import__("models.pipeline_task", fromlist=["PipelineErrorSummary"]).PipelineErrorSummary(count=0, codes=[]),
        last_event_sequence=1,
        revision=1,
        idempotency_key=sha256_canonical({"task": SOURCE_TASK_ID}),
        idempotency_payload_sha256=SHA,
    )


class FakeRegistration:
    def __init__(self, entries=None):
        self.entries = entries or {"pkg-demo-001": (make_record(), make_validation())}

    def read_record_strict(self, package_id, package_revision):
        pair = self.entries.get(package_id)
        return pair[0] if pair else None

    def read_validation_strict(self, package_id, package_revision, validation_id):
        pair = self.entries.get(package_id)
        return pair[1] if pair else None


class FakeProfileLookup:
    def __init__(self, profiles=None):
        self.profiles = profiles or {"profile-mixed-v1": make_profile()}

    def get_input_profile(self, profile_version):
        return self.profiles.get(profile_version)


def make_request(source_item_ids, config=None, profile_lookup=None, **overrides):
    base = dict(
        batch_id=BATCH,
        source_task_id=SOURCE_TASK_ID,
        source_item_ids=source_item_ids,
        configuration=config or make_config(),
        concurrency=2,
    )
    base.update(overrides)
    return ScoringTaskCreateRequest(**base)


def _make_env(tmp_path, reg=None, lookup=None, store=None):
    if store is None:
        store = PipelineTaskStore(tmp_path / "pipeline-runtime" / "tasks")
        store.tasks_dir.mkdir(parents=True, exist_ok=True)
    reg = reg or FakeRegistration()
    lookup = lookup or FakeProfileLookup()
    creator = ScoringTaskCreator(
        store=store, registration=reg, profile_lookup=lookup,
        clock=lambda: T0, uuid_factory=(lambda: str(uuid4())),
    )
    return store, reg, lookup, creator


def _write_source(store, task, items):
    store.write_task_snapshot(task.task_id, task, expected_revision=None)
    for i in items:
        store.write_item_snapshot(task.task_id, i, expected_revision=None)


def _seed(tmp_path, item_ids=(SRC_ITEM_A,)):
    store, reg, lookup, creator = _make_env(tmp_path)
    items = [make_source_item(i) for i in item_ids]
    task = make_source_task(list(item_ids), items=items)
    _write_source(store, task, items)
    return store, reg, lookup, creator


# ---------------- 1. 独立导入顺序 ---------------- #


def _run_import(code: str) -> int:
    env = dict(os.environ)
    env["PYTHONPATH"] = _BACKEND_DIR
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60,
    )
    return result.returncode, result.stderr


def test_import_order_scoring_configuration_first():
    rc, err = _run_import(
        "from models.scoring_configuration import ScoringTaskConfiguration; print('ok')")
    assert rc == 0, err


def test_import_order_pipeline_task_first_then_config():
    rc, err = _run_import(
        "from models.pipeline_task import PipelineTask; "
        "from models.scoring_configuration import ScoringTaskConfiguration; print('ok')")
    assert rc == 0, err


# ---------------- 2-4. 真实并发幂等 ---------------- #


def test_concurrent_same_request_single_task(tmp_path):
    store, reg, lookup, creator = _seed(tmp_path)
    req = make_request([SRC_ITEM_A])
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

    assert len(results) == 2
    assert len({r[0].task_id for r in results}) == 1
    assert sorted(r[1] for r in results) == [False, True]
    # 只新增一个 scoring task 目录（排除 source task）
    scoring_dirs = [d for d in store.tasks_dir.iterdir()
                    if d.is_dir() and d.name != SOURCE_TASK_ID]
    assert len(scoring_dirs) == 1
    # 只一组 scoring items
    items = list((scoring_dirs[0] / "items").glob("*.json"))
    assert len(items) == 1
    # 只一个 task_created 事件
    created = [e for e in store.load_event_log(scoring_dirs[0].name)
               if e.event_type == "task_created"]
    assert len(created) == 1


# ---------------- 5. 创建锁冲突 ---------------- #


def test_store_creation_lock_conflict_retryable(tmp_path):
    from services.pipeline_task_store import _TaskFileLock

    store = PipelineTaskStore(tmp_path / "pipeline-runtime" / "tasks")
    store.tasks_dir.mkdir(parents=True, exist_ok=True)
    key = sha256_canonical({"x": "lock-key-demo"})
    # 锁文件名只含合法 SHA-256（与 store.acquire_creation_lock 一致）
    digest = sha256_canonical({"creation_lock": key})
    lock_path = store.root / "locks" / f"create-{digest}.lock"
    holder = _TaskFileLock(lock_path, timeout_ms=2000)
    holder.acquire()
    try:
        contender = _TaskFileLock(lock_path, timeout_ms=150)
        with pytest.raises(PipelineStoreError) as ei:
            contender.acquire()
        assert ei.value.error_code == ERR_LOCK_CONFLICT
        assert ei.value.retryable is True
    finally:
        holder.release()
    # 释放后可再次获取（锁不残留）
    again = _TaskFileLock(lock_path, timeout_ms=2000)
    again.acquire()
    again.release()


def test_creator_maps_lock_conflict_to_retryable(tmp_path, monkeypatch):
    store, reg, lookup, creator = _seed(tmp_path)

    def _boom(key, timeout_ms=None):
        raise PipelineStoreError(ERR_LOCK_CONFLICT, "SIDECAR_LOCK_CONFLICT", retryable=True)

    monkeypatch.setattr(store, "acquire_creation_lock", _boom)
    with pytest.raises(ScoringTaskError) as ei:
        creator.create_scoring_task(make_request([SRC_ITEM_A]))
    assert ei.value.error_code == ERR_CREATION_LOCK_CONFLICT
    assert ei.value.retryable is True


# ---------------- 6. source item 权威快照不二次读取 ---------------- #


class CountingStore:
    """包装 store：统计 load_item 调用；二次读取返回篡改副本。"""

    def __init__(self, store):
        self.store = store
        self.item_loads = 0

    def load_item(self, task_id, item_id):
        self.item_loads += 1
        item = self.store.load_item(task_id, item_id)
        if self.item_loads > 1 and item is not None:
            return item.model_copy(update={"package_id": "pkg-TAMPERED"})
        return item

    def __getattr__(self, name):
        return getattr(self.store, name)


def test_source_item_snapshot_not_read_again(tmp_path):
    store, reg, lookup, _ = _seed(tmp_path)
    counting = CountingStore(store)
    creator = ScoringTaskCreator(
        store=counting, registration=reg, profile_lookup=lookup,
        clock=lambda: T0, uuid_factory=(lambda: str(uuid4())),
    )
    new_task, created = creator.create_scoring_task(make_request([SRC_ITEM_A]))
    assert created is True
    # 核对循环只读取一次；幂等/构造不再二次读取
    assert counting.item_loads == 1
    # 创建结果使用已核对快照（未被篡改版本污染）
    assert new_task.item_index[0].package_id == "pkg-demo-001"
    stored = store.load_item(new_task.task_id, new_task.item_index[0].item_id)
    assert stored.package_id == "pkg-demo-001"


# ---------------- 7-11. 损坏显式传播 / 未知异常不吞 ---------------- #


def _corrupt(path):
    path.write_text("{broken json", encoding="utf-8")


def test_task_json_corrupted_reports_corrupted(tmp_path):
    store, reg, lookup, creator = _seed(tmp_path)
    _corrupt(store.tasks_dir / SOURCE_TASK_ID / "task.json")
    with pytest.raises(ScoringTaskError) as ei:
        creator.create_scoring_task(make_request([SRC_ITEM_A]))
    assert ei.value.error_code == ERR_SOURCE_TASK_CORRUPTED


def test_item_json_corrupted_reports_corrupted(tmp_path):
    store, reg, lookup, creator = _seed(tmp_path)
    _corrupt(store.tasks_dir / SOURCE_TASK_ID / "items" / f"{SRC_ITEM_A}.json")
    with pytest.raises(ScoringTaskError) as ei:
        creator.create_scoring_task(make_request([SRC_ITEM_A]))
    assert ei.value.error_code == ERR_SOURCE_ITEM_CORRUPTED


def test_evidence_record_corrupted_reports_corrupted(tmp_path):
    store, reg, lookup, creator = _make_env(tmp_path, reg=FakeRegistration())
    it = make_source_item(SRC_ITEM_A)
    _write_source(store, make_source_task([SRC_ITEM_A], items=[it]), [it])

    def _boom(package_id, package_revision):
        raise RegistrationError(ERR_RECORD_CORRUPTED, "SIDECAR_RECORD_CORRUPTED")

    reg.read_record_strict = _boom
    with pytest.raises(ScoringTaskError) as ei:
        creator.create_scoring_task(make_request([SRC_ITEM_A]))
    assert ei.value.error_code == ERR_EVIDENCE_RECORD_CORRUPTED


def test_validation_corrupted_reports_corrupted(tmp_path):
    store, reg, lookup, creator = _make_env(tmp_path, reg=FakeRegistration())
    it = make_source_item(SRC_ITEM_A)
    _write_source(store, make_source_task([SRC_ITEM_A], items=[it]), [it])

    def _boom(package_id, package_revision, validation_id):
        raise RegistrationError(ERR_RECORD_CORRUPTED, "SIDECAR_RECORD_CORRUPTED")

    reg.read_validation_strict = _boom
    with pytest.raises(ScoringTaskError) as ei:
        creator.create_scoring_task(make_request([SRC_ITEM_A]))
    assert ei.value.error_code == ERR_VALIDATION_CORRUPTED


def test_unknown_exception_not_swallowed_to_not_found(tmp_path):
    store, reg, lookup, creator = _seed(tmp_path)

    def _boom(package_id, package_revision):
        raise RuntimeError("sidecar exploded")

    reg.read_record_strict = _boom
    with pytest.raises(RuntimeError):
        creator.create_scoring_task(make_request([SRC_ITEM_A]))
    # store 层未知异常同样传播
    def _boom_task(task_id):
        raise RuntimeError("store exploded")

    store.load_task = _boom_task
    with pytest.raises(RuntimeError):
        creator.create_scoring_task(make_request([SRC_ITEM_A]))


# ---------------- 12-14. Profile 权威绑定 ---------------- #


def test_profile_not_found_rejected(tmp_path):
    store, reg, lookup, creator = _seed(tmp_path)
    lookup.profiles = {}  # 权威 Profile 不存在
    with pytest.raises(ScoringTaskError) as ei:
        creator.create_scoring_task(make_request([SRC_ITEM_A]))
    assert ei.value.error_code == ERR_PROFILE_NOT_FOUND


@pytest.mark.parametrize("field,changed", [
    ("scoring_policy_version", "policy-999"),
    ("rubric_version", "rubric-999"),
    ("prompt_version", "prompt-999"),
    ("response_schema_version", "schema-999"),
])
def test_profile_version_mismatch_rejected(tmp_path, field, changed):
    store, reg, lookup, creator = _seed(tmp_path)
    lookup.profiles["profile-mixed-v1"] = make_profile(**{field: changed})
    with pytest.raises(ScoringTaskError) as ei:
        creator.create_scoring_task(make_request([SRC_ITEM_A]))
    assert ei.value.error_code == ERR_CONFIGURATION_PROFILE_MISMATCH


def test_matching_profile_creates(tmp_path):
    store, reg, lookup, creator = _seed(tmp_path)
    new_task, created = creator.create_scoring_task(make_request([SRC_ITEM_A]))
    assert created is True
    assert new_task.configuration_snapshot.configuration_fingerprint == \
        make_config().configuration_fingerprint


# ---------------- 15. concurrency 上限 ---------------- #


def test_concurrency_over_limit_rejected(tmp_path):
    with pytest.raises(ValidationError):
        make_request([SRC_ITEM_A], concurrency=MAX_SCORING_CONCURRENCY + 1)


# ---------------- 16. 指纹内容一致性 ---------------- #


def test_config_fingerprint_forgery_rejected():
    with pytest.raises(ValidationError):
        make_config(configuration_fingerprint="9" * 64)
    # 合法内容 -> 自动指纹成功
    cfg = make_config()
    assert cfg.configuration_fingerprint == sha256_canonical(
        cfg.model_dump(exclude={"configuration_fingerprint"}))


# ---------------- 17. 旧 evidence 配置指纹回归 ---------------- #


def test_old_evidence_config_fingerprint_regression():
    """evidence task 的 configuration fingerprint 行为不变（PipelineConfigurationSnapshot）。"""
    snap = {"evidence_contract_version": "evidence-package/v1.1", "validator_version": "1.0.0"}
    from models.pipeline_task import PipelineConfigurationSnapshot
    obj = PipelineConfigurationSnapshot(**snap)
    assert sha256_canonical(obj.model_dump()) == sha256_canonical(snap)
    task = make_source_task([SRC_ITEM_A])
    assert task.configuration_fingerprint == sha256_canonical(snap)
    # 旧 JSON 重新序列化无破坏
    raw = json.loads(task.model_dump_json())
    again = PipelineTask(**raw)
    assert again.configuration_fingerprint == task.configuration_fingerprint
