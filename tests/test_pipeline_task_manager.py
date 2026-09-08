"""
Phase 11C-2c：PipelineTaskManager 任务创建与状态流转合成测试。

所有数据为脱敏合成数据；测试全部注入 tmp_path；不读真实材料、不调模型。
覆盖：创建（sufficient/limited/manual_only/insufficient/None 拒绝）、身份与门预检、
幂等命中/冲突、合法与非法状态转换、completed 保护、聚合一致性、回滚零残留、
manager 重建后可继续。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from models.evidence_sidecar import EvidencePackageRecord, ValidationResult
from models.pipeline_task import (
    PipelineConfigurationSnapshot,
    PipelineTask,
    sha256_canonical,
)
from services.pipeline_task_manager import (
    ERR_EVIDENCE_LEVEL_MISSING,
    ERR_EVIDENCE_LEVEL_NOT_ALLOWED,
    ERR_IDEMPOTENCY_CONFLICT,
    ERR_INVALID_STATE_TRANSITION,
    ERR_PACKAGE_IDENTITY_CONFLICT,
    ERR_PACKAGE_NOT_REGISTERED,
    ERR_REVISION_CONFLICT,
    ERR_SCOPE_VIOLATION,
    ERR_SUCCESS_RESULT_PROTECTED,
    ERR_TASK_NOT_SETTLED,
    ERR_VALIDATION_NOT_ALLOWED,
    REASON_LIMITED_EVIDENCE,
    REASON_MANUAL_ONLY_EVIDENCE,
    PackageRef,
    PipelineTaskCreateRequest,
    PipelineTaskError,
    PipelineTaskManager,
)
from services.pipeline_task_store import PipelineTaskStore

UTC = timezone.utc
T0 = datetime(2026, 8, 10, 8, 0, 0, tzinfo=UTC)
SHA256 = "a" * 64
PKG = "pkg-demo-001"
REC = "record-0001"
VAL = "validation-0001"
CONFIG = PipelineConfigurationSnapshot(
    evidence_contract_version="evidence-package/v1.1", validator_version="1.0.0",
)


def _uuid_seq():
    i = [0]

    def gen() -> str:
        i[0] += 1
        return f"00000000-0000-0000-0000-{i[0]:012d}"

    return gen


def make_record(**overrides):
    data = {
        "record_schema_version": "evidence-sidecar/record/v1",
        "record_id": REC,
        "package_id": PKG,
        "package_revision": 1,
        "batch_id": "batch-demo-001",
        "submission_id": "sub-demo-001",
        "evidence_version": "1",
        "privacy_policy_version": "privacy-policy/v1.1",
        "contract_version": "evidence-package/v1.1",
        "publication_status": "ready",
        "registration_status": "registered",
        "latest_validation_id": VAL,
        "latest_validation_status": "passed",
        "source_package_ref": f"{PKG}/1/evidence.json",
        "registered_at": T0,
        "registered_by": "synthetic-registrar",
        "created_at": T0,
        "updated_at": T0,
        "revision": 1,
        "record_sha256": SHA256,
        "manifest_sha256": SHA256,
    }
    data.update(overrides)
    return EvidencePackageRecord.model_validate(data)


def make_validation(evidence_level: str = "sufficient", **overrides):
    data = {
        "validation_schema_version": "evidence-sidecar/validation/v1",
        "validation_id": VAL,
        "record_id": REC,
        "package_id": PKG,
        "package_revision": 1,
        "manifest_sha256": SHA256,
        "validator_name": "validator-demo",
        "validator_version": "1.0.0",
        "evidence_contract_version": "evidence-package/v1.1",
        "privacy_policy_version": "privacy-policy/v1.1",
        "mode": "registration",
        "status": "passed",
        "started_at": T0,
        "completed_at": T0 + timedelta(seconds=2),
        "duration_ms": 2000,
        "checks": [{"check_id": "JSON_PARSEABLE", "status": "passed", "message_key": "CHECK_OK"}],
        "issues": [],
        "issue_counts": {"info": 0, "warning": 0, "error": 0, "fatal": 0},
        "model_input_allowed": True,
        "registration_allowed": True,
        "validated_file_count": 1,
        "declared_file_count": 1,
        "unregistered_file_count": 0,
        "result_sha256": SHA256,
        "evidence_level": evidence_level,
    }
    data.update(overrides)
    return ValidationResult.model_validate(data)


class FakeRegistrationService:
    def __init__(self, records=None, validations=None):
        self.records = records or {}
        self.validations = validations or {}

    def read_record_strict(self, package_id, package_revision):
        return self.records.get((package_id, package_revision))

    def read_validation_strict(self, package_id, package_revision, validation_id):
        return self.validations.get((package_id, package_revision, validation_id))


def make_manager(tmp_path, records=None, validations=None):
    store = PipelineTaskStore(tmp_path / "evidence-runtime" / "pipeline")
    reg = FakeRegistrationService(records, validations)
    clock = [T0]

    def now():
        clock[0] = clock[0] + timedelta(seconds=1)
        return clock[0]

    return PipelineTaskManager(store, reg, clock=now, uuid_factory=_uuid_seq())


def default_registry(evidence_level: str = "sufficient", validation_overrides=None):
    val = make_validation(evidence_level, **(validation_overrides or {}))
    rec = make_record()
    return (
        {(PKG, 1): rec},
        {(PKG, 1, VAL): val},
    )


def ref(**overrides):
    data = {
        "package_id": PKG,
        "package_revision": 1,
        "manifest_sha256": SHA256,
        "registration_record_id": REC,
        "validation_id": VAL,
    }
    data.update(overrides)
    return PackageRef(**data)


def request(package_refs=None, batch_id="batch-demo-001", concurrency=1, config=CONFIG):
    return PipelineTaskCreateRequest(
        batch_id=batch_id,
        package_refs=package_refs or [ref()],
        concurrency=concurrency,
        configuration_snapshot=config,
    )


def create_sufficient(tmp_path):
    records, validations = default_registry("sufficient")
    mgr = make_manager(tmp_path, records, validations)
    task, hit = mgr.create_task(request())
    return mgr, task, hit


# ---------------------------------------------------------------- 1-3 创建与流向


def test_sufficient_create_success(tmp_path):
    mgr, task, hit = create_sufficient(tmp_path)
    assert hit is False
    assert task.status == "pending" and task.revision == 1 and task.last_event_sequence == 1
    assert task.pending_items == 1 and task.total_items == 1
    item = mgr.get_item(task.task_id, task.item_index[0].item_id)
    assert item is not None
    assert item.status == "pending" and item.current_stage == "import"
    assert item.evidence_level == "sufficient" and item.item_revision == 1
    events = mgr.get_event_log(task.task_id)
    assert len(events) == 1 and events[0].event_type == "task_created"
    assert events[0].revision_before == 0 and events[0].revision_after == 1
    # 后三阶段恒 not_started
    for s in task.stage_summaries:
        if s.stage in ("score", "review", "export"):
            assert s.status == "not_started"


def test_limited_flows_to_manual_review_after_validate(tmp_path):
    records, validations = default_registry("limited")
    mgr = make_manager(tmp_path, records, validations)
    task, hit = mgr.create_task(request())
    item_id = task.item_index[0].item_id
    task = mgr.start_task(task.task_id, 1)
    mgr.start_item_stage(task.task_id, item_id, 2, 1)  # import running
    mgr.complete_item_stage(task.task_id, item_id, 3, 2)  # import -> validate（pending）
    mgr.start_item_stage(task.task_id, item_id, 4, 3)  # validate running
    item = mgr.complete_item_stage(task.task_id, item_id, 5, 4)  # validate 完成 -> manual_review
    assert item.status == "manual_review"
    assert item.last_error is not None and item.last_error.message_key == REASON_LIMITED_EVIDENCE
    assert item.evidence_level == "limited"


def test_manual_only_created_directly_in_manual_review(tmp_path):
    records, validations = default_registry("manual_only")
    mgr = make_manager(tmp_path, records, validations)
    task, hit = mgr.create_task(request())
    item = mgr.get_item(task.task_id, task.item_index[0].item_id)
    assert item.status == "manual_review"
    assert item.last_error is not None and item.last_error.message_key == REASON_MANUAL_ONLY_EVIDENCE
    assert task.manual_review_items == 1


# ---------------------------------------------------------------- 4-7 拒绝路径


def test_insufficient_rejected(tmp_path):
    records, validations = default_registry("insufficient")
    mgr = make_manager(tmp_path, records, validations)
    with pytest.raises(PipelineTaskError) as ei:
        mgr.create_task(request())
    assert ei.value.error_code == ERR_EVIDENCE_LEVEL_NOT_ALLOWED
    assert not list(mgr.store.tasks_dir.iterdir())  # 零写入


def test_evidence_level_none_rejected(tmp_path):
    records, validations = default_registry(None)
    mgr = make_manager(tmp_path, records, validations)
    with pytest.raises(PipelineTaskError) as ei:
        mgr.create_task(request())
    assert ei.value.error_code == ERR_EVIDENCE_LEVEL_MISSING


def test_identity_conflict_rejected(tmp_path):
    records, validations = default_registry("sufficient")
    mgr = make_manager(tmp_path, records, validations)
    # 请求 record_id 与登记不符
    with pytest.raises(PipelineTaskError) as ei:
        mgr.create_task(request(package_refs=[ref(registration_record_id="record-9999")]))
    assert ei.value.error_code == ERR_PACKAGE_IDENTITY_CONFLICT
    # 请求 validation_id 与 record.latest_validation_id 不符（validation 存在但 record 指向不同）
    records2, validations2 = default_registry("sufficient")
    validations2[("pkg-demo-001", 1, "validation-9999")] = make_validation(
        "sufficient", validation_id="validation-9999")
    mgr2 = make_manager(tmp_path, records2, validations2)
    with pytest.raises(PipelineTaskError) as ei:
        mgr2.create_task(request(package_refs=[ref(validation_id="validation-9999")]))
    assert ei.value.error_code == ERR_PACKAGE_IDENTITY_CONFLICT
    # manifest 不一致
    with pytest.raises(PipelineTaskError) as ei:
        mgr.create_task(request(package_refs=[ref(manifest_sha256="b" * 64)]))
    assert ei.value.error_code == ERR_PACKAGE_IDENTITY_CONFLICT


def test_unregistered_and_gate_rejected(tmp_path):
    # 未登记
    mgr = make_manager(tmp_path, {}, {})
    with pytest.raises(PipelineTaskError) as ei:
        mgr.create_task(request())
    assert ei.value.error_code == ERR_PACKAGE_NOT_REGISTERED
    # registration_allowed=false
    records, validations = default_registry("sufficient", {"registration_allowed": False})
    mgr = make_manager(tmp_path, records, validations)
    with pytest.raises(PipelineTaskError) as ei:
        mgr.create_task(request())
    assert ei.value.error_code == ERR_VALIDATION_NOT_ALLOWED
    # model_input_allowed=false（隐私门）
    records, validations = default_registry("sufficient", {"model_input_allowed": False})
    mgr = make_manager(tmp_path, records, validations)
    with pytest.raises(PipelineTaskError) as ei:
        mgr.create_task(request())
    assert ei.value.error_code == ERR_VALIDATION_NOT_ALLOWED
    # record 未注册（registration_status != registered）
    records, validations = default_registry("sufficient")
    rec = make_record(registration_status="rejected")
    mgr = make_manager(tmp_path, {(PKG, 1): rec}, validations)
    with pytest.raises(PipelineTaskError) as ei:
        mgr.create_task(request())
    assert ei.value.error_code == ERR_VALIDATION_NOT_ALLOWED


# ---------------------------------------------------------------- 8-10 整批与幂等


def test_multi_item_one_failure_zero_write(tmp_path):
    rec2 = make_record(package_id="pkg-demo-002", record_id="record-0002", latest_validation_id="validation-0002")
    records = {(PKG, 1): make_record(), ("pkg-demo-002", 1): rec2}
    val2 = make_validation("insufficient", validation_id="validation-0002", record_id="record-0002",
                           package_id="pkg-demo-002")
    validations = {
        (PKG, 1, VAL): make_validation("sufficient"),
        ("pkg-demo-002", 1, "validation-0002"): val2,
    }
    mgr = make_manager(tmp_path, records, validations)
    req = request(package_refs=[
        ref(),
        ref(package_id="pkg-demo-002", registration_record_id="record-0002", validation_id="validation-0002"),
    ])
    with pytest.raises(PipelineTaskError) as ei:
        mgr.create_task(req)
    assert ei.value.error_code == ERR_EVIDENCE_LEVEL_NOT_ALLOWED
    assert not list(mgr.store.tasks_dir.iterdir())  # 整批零写入


def test_duplicate_create_idempotent_hit(tmp_path):
    mgr, task, hit = create_sufficient(tmp_path)
    again, hit2 = mgr.create_task(request())
    assert hit2 is True
    assert again.task_id == task.task_id  # 返回原任务
    assert len(list(mgr.store.tasks_dir.iterdir())) == 1  # 无新目录
    assert len(mgr.get_event_log(task.task_id)) == 1  # 不追加事件


def test_same_key_different_payload_conflict(tmp_path):
    """同 idempotency_key 但 payload 不同 -> SIDECAR_IDEMPOTENCY_CONFLICT（防御分支）。
    冻结算法下 key 是 payload 哈希，正常输入不可达；通过预置不同 payload 的任务验证扫描分支。"""
    from services.pipeline_task_manager import _CONTRACT_VERSION, _TASK_TYPE
    from models.pipeline_task import _SCOPE, compute_item_input_fingerprint, compute_task_idempotency

    records, validations = default_registry("sufficient")
    mgr = make_manager(tmp_path, records, validations)
    fp = compute_item_input_fingerprint(package_id=PKG, package_revision=1, manifest_sha256=SHA256,
                                        registration_record_id=REC, validation_id=VAL)
    cfg_fp = sha256_canonical(CONFIG.model_dump(mode="json"))
    key = compute_task_idempotency(contract_version=_CONTRACT_VERSION, task_type=_TASK_TYPE,
                                   batch_id="batch-demo-001", execution_scope=list(_SCOPE),
                                   items=[(PKG, 1, fp)], configuration_fingerprint=cfg_fp)
    # 预置一个同 key 但不同 payload_sha256 的合成任务（无正常任务在盘）
    from models.pipeline_task import PipelineStageSummary
    stage = lambda stage, deps, status="not_started": PipelineStageSummary(  # noqa: E731
        stage=stage, status=status, depends_on=deps, total_items=0, pending_items=0,
        running_items=0, completed_items=0, failed_items=0, skipped_items=0,
        manual_review_items=0, blocking_error_codes=[], revision=1)
    fake = PipelineTask(
        contract_version=_CONTRACT_VERSION, task_type=_TASK_TYPE, execution_scope=list(_SCOPE),
        task_id="00000000-0000-0000-0000-000000009999", batch_id="batch-other",
        status="pending", current_stage="import", created_at=T0, started_at=None, updated_at=T0,
        paused_at=None, completed_at=None, total_items=0, pending_items=0, running_items=0,
        completed_items=0, failed_items=0, skipped_items=0, manual_review_items=0, concurrency=1,
        configuration_snapshot=CONFIG, configuration_fingerprint=cfg_fp, item_index=[],
        stage_summaries=[
            stage("import", []), stage("validate", ["import"]), stage("score", ["validate"]),
            stage("review", ["validate", "score"]), stage("export", ["review"]),
        ],
        error_summary={"count": 0, "codes": []}, last_event_sequence=0, revision=1,
        idempotency_key=key, idempotency_payload_sha256="d" * 64,  # 同 key 不同 payload
    )
    mgr.store.write_task_snapshot(fake.task_id, fake, expected_revision=None)
    with pytest.raises(PipelineTaskError) as ei:
        mgr.create_task(request())
    assert ei.value.error_code == ERR_IDEMPOTENCY_CONFLICT


# ---------------------------------------------------------------- 11-15 状态转换


def test_full_sufficient_lifecycle(tmp_path):
    """合法 task/item/stage 转换全流程 + 每步一个事件。"""
    mgr, task, _ = create_sufficient(tmp_path)
    item_id = task.item_index[0].item_id
    task = mgr.start_task(task.task_id, 1)
    assert task.status == "running" and task.started_at is not None
    assert mgr.get_event_log(task.task_id)[-1].event_type == "task_started"
    item = mgr.start_item_stage(task.task_id, item_id, task.revision, 1)
    assert item.status == "running" and item.attempt_count == 1
    assert item.heartbeat_updated_at is not None
    item = mgr.complete_item_stage(task.task_id, item_id, task.revision + 1, item.item_revision)
    assert item.current_stage == "validate" and item.status == "pending"
    assert item.heartbeat_updated_at is None  # 离开 running 清空
    item = mgr.start_item_stage(task.task_id, item_id, task.revision + 2, item.item_revision)
    assert item.status == "running" and item.current_stage == "validate"
    item = mgr.complete_item_stage(task.task_id, item_id, task.revision + 3, item.item_revision,
                                   output_ref="outputs/result.json", output_sha256=SHA256)
    assert item.status == "completed" and item.output_ref == "outputs/result.json"
    task = mgr.finalize_task_if_settled(task.task_id, task.revision + 4)
    assert task.status == "completed" and task.completed_at is not None
    assert task.current_stage is None
    events = mgr.get_event_log(task.task_id)
    assert len(events) == 7  # created+started+item_started+item_completed+item_started+item_completed+task_completed


def test_invalid_transition_rejected_zero_side_effect(tmp_path):
    mgr, task, _ = create_sufficient(tmp_path)
    item_id = task.item_index[0].item_id
    task = mgr.start_task(task.task_id, 1)
    before_events = len(mgr.get_event_log(task.task_id))
    before_item_rev = mgr.get_item(task.task_id, item_id).item_revision
    # 未运行 item 直接 complete -> 非法
    with pytest.raises(PipelineTaskError) as ei:
        mgr.complete_item_stage(task.task_id, item_id, task.revision, before_item_rev)
    assert ei.value.error_code == ERR_INVALID_STATE_TRANSITION
    # revision 冲突（item 过期）
    with pytest.raises(PipelineTaskError) as ei:
        mgr.start_item_stage(task.task_id, item_id, task.revision, before_item_rev + 5)
    assert ei.value.error_code == ERR_REVISION_CONFLICT
    # 磁盘零副作用
    assert len(mgr.get_event_log(task.task_id)) == before_events
    assert mgr.get_item(task.task_id, item_id).item_revision == before_item_rev
    assert mgr.get_task(task.task_id).revision == task.revision


def test_completed_result_protected_and_idempotent(tmp_path):
    mgr, task, _ = create_sufficient(tmp_path)
    item_id = task.item_index[0].item_id
    task = mgr.start_task(task.task_id, 1)
    mgr.start_item_stage(task.task_id, item_id, task.revision, 1)
    task = mgr.get_task(task.task_id)
    mgr.complete_item_stage(task.task_id, item_id, task.revision, 2)
    task = mgr.get_task(task.task_id)
    mgr.start_item_stage(task.task_id, item_id, task.revision, 3)
    task = mgr.get_task(task.task_id)
    mgr.complete_item_stage(task.task_id, item_id, task.revision, 4,
                            output_ref="outputs/result.json", output_sha256=SHA256)
    # 幂等完成：相同输出 -> 返回原状态（不冲突）
    task = mgr.get_task(task.task_id)
    item = mgr.complete_item_stage(task.task_id, item_id, task.revision, 5,
                                   output_ref="outputs/result.json", output_sha256=SHA256)
    assert item.status == "completed" and item.output_sha256 == SHA256
    # 不同输出 -> 保护冲突
    task = mgr.get_task(task.task_id)
    with pytest.raises(PipelineTaskError) as ei:
        mgr.complete_item_stage(task.task_id, item_id, task.revision, 5,
                                output_ref="outputs/other.json", output_sha256="e" * 64)
    assert ei.value.error_code == ERR_SUCCESS_RESULT_PROTECTED
    # completed item 不得重新进入 running
    task = mgr.get_task(task.task_id)
    with pytest.raises(PipelineTaskError) as ei:
        mgr.start_item_stage(task.task_id, item_id, task.revision, 5)
    assert ei.value.error_code == ERR_INVALID_STATE_TRANSITION


def test_limited_cannot_enter_score(tmp_path):
    """limited item 的 current_stage 恒在 import/validate；validate 完成即转 manual_review。"""
    records, validations = default_registry("limited")
    mgr = make_manager(tmp_path, records, validations)
    task, _ = mgr.create_task(request())
    item_id = task.item_index[0].item_id
    task = mgr.start_task(task.task_id, 1)
    mgr.start_item_stage(task.task_id, item_id, 2, 1)
    item = mgr.complete_item_stage(task.task_id, item_id, 3, 2)
    assert item.current_stage == "validate"  # 只进入 validate，不可能 score
    assert item.status == "pending"


def test_manual_only_cannot_enter_automated_stage(tmp_path):
    records, validations = default_registry("manual_only")
    mgr = make_manager(tmp_path, records, validations)
    task, _ = mgr.create_task(request())
    item_id = task.item_index[0].item_id
    task = mgr.start_task(task.task_id, 1)
    with pytest.raises(PipelineTaskError) as ei:
        mgr.start_item_stage(task.task_id, item_id, task.revision, 1)
    assert ei.value.error_code == ERR_SCOPE_VIOLATION


# ---------------------------------------------------------------- 16-18 聚合与重建


def test_counts_and_index_consistent(tmp_path):
    """task 六类计数 = item 聚合；stage summary 计数；item_index 与 item 文件对账。"""
    mgr, task, _ = create_sufficient(tmp_path)
    item_id = task.item_index[0].item_id
    task = mgr.start_task(task.task_id, 1)
    mgr.start_item_stage(task.task_id, item_id, task.revision, 1)
    task = mgr.get_task(task.task_id)
    counts = Counter(i.status for i in [mgr.get_item(task.task_id, e.item_id) for e in task.item_index])
    assert task.pending_items == counts.get("pending", 0)
    assert task.running_items == counts.get("running", 0)
    assert task.total_items == len(task.item_index) == 1
    import_stage = next(s for s in task.stage_summaries if s.stage == "import")
    assert import_stage.running_items == 1  # item 在 import running
    # store 一致性（commit 时已对账）；再读任务确认可解析
    assert mgr.get_task(task.task_id).item_index[0].item_id == item_id


def test_write_failure_rolls_back_no_empty_task_dir(tmp_path, monkeypatch):
    from services import pipeline_task_manager as ptm

    mgr, _, _ = create_sufficient(tmp_path)
    before_dirs = set(mgr.store.tasks_dir.iterdir())

    def boom(*args, **kwargs):
        raise Exception("injected create failure")

    monkeypatch.setattr(ptm, "run_task_transaction", boom)
    with pytest.raises(Exception):
        mgr.create_task(request(batch_id="batch-other"))  # 不同输入避免幂等命中
    monkeypatch.undo()
    after_dirs = set(mgr.store.tasks_dir.iterdir())
    assert after_dirs == before_dirs  # 无新任务目录残留


def test_recreate_manager_continues(tmp_path):
    """进程重建 manager（同 store）后可读取并继续合法操作。"""
    records, validations = default_registry("sufficient")
    store = PipelineTaskStore(tmp_path / "evidence-runtime" / "pipeline")
    reg = FakeRegistrationService(records, validations)
    m1 = PipelineTaskManager(store, reg, clock=lambda: T0 + timedelta(seconds=5), uuid_factory=_uuid_seq())
    task, _ = m1.create_task(request())
    item_id = task.item_index[0].item_id
    m1.start_task(task.task_id, 1)
    # 重建 manager
    m2 = PipelineTaskManager(store, reg, clock=lambda: T0 + timedelta(seconds=10), uuid_factory=_uuid_seq())
    reloaded = m2.get_task(task.task_id)
    assert reloaded is not None and reloaded.status == "running"
    m2.start_item_stage(task.task_id, item_id, reloaded.revision, 1)
    item = m2.get_item(task.task_id, item_id)
    assert item.status == "running" and item.attempt_count == 1


# ---------------------------------------------------------------- 附加边界


def test_input_limits_and_sensitive_scan(tmp_path):
    """输入约束：重复 package 拒绝、超限拒绝；合成数据敏感扫描。"""
    mgr = make_manager(tmp_path, *default_registry("sufficient"))
    with pytest.raises(Exception):
        PipelineTaskCreateRequest(
            batch_id="batch-demo-001",
            package_refs=[ref(), ref()],  # 重复 package/revision
            concurrency=1,
            configuration_snapshot=CONFIG,
        )
    with pytest.raises(Exception):
        PipelineTaskCreateRequest(
            batch_id="batch-demo-001",
            package_refs=[ref(package_id="张三")],  # 非 ASCII
            concurrency=1,
            configuration_snapshot=CONFIG,
        )
    # 敏感扫描：创建后 dump 无敏感模式
    mgr, task, _ = create_sufficient(tmp_path)
    dumped = json.dumps(task.model_dump(mode="json"), ensure_ascii=False, default=str)
    for pat in (r"1[3-9]\d{9}", r"https?://", r"姓名", r"网盘", r"D:/", r"C:\\Users"):
        assert not re.search(pat, dumped), f"合成数据含敏感模式: {pat}"
