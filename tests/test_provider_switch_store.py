"""
Phase 11D-3b：ProviderSwitchStore 合成测试。

文件型持久化：revision/current/events/idempotency/CAS/损坏显式失败/重启可读。
全部合成脱敏数据；不写正文/Key/URL。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from models.provider_switch import ProviderSwitchRecord, ProviderSwitchTargetProposal
from models.scoring_provider import ProviderSwitchDecision
from services.provider_switch_store import (
    ERR_IDEMPOTENCY_CONFLICT,
    ERR_REVISION_CONFLICT,
    ERR_TRANSACTION_ERROR,
    ProviderSwitchStore,
    SwitchStoreError,
)

T0 = datetime(2026, 8, 10, 6, 0, 0, tzinfo=timezone.utc)
TASK = "pipeline_task_switch_001"
ITEM = "item_switch_001"
DECISION_ID = "switch_decision_001"
IDEM = "d" * 64
CONTRACT = "scoring-provider/v1"


def make_proposal(**overrides):
    base = dict(
        target_provider_id="provider_target_001",
        target_model_id="model_target_v1",
        scoring_policy_version="policy-demo-001",
        prompt_version="prompt-demo-001",
        evidence_package_id="evidence_demo_001",
        evidence_manifest_sha256="a" * 64,
        input_fingerprint="b" * 64,
        response_schema_version="score-response-v1",
        scoring_mode="mixed",
    )
    base.update(overrides)
    return ProviderSwitchTargetProposal(**base)


def make_decision(status="pending_approval", decision_id=DECISION_ID, **overrides):
    base = dict(
        contract_version=CONTRACT,
        switch_decision_id=decision_id,
        task_id=TASK, item_id=ITEM,
        source_attempt_id="attempt_source_001",
        target_attempt_id=None,
        source_provider_id="provider_source_001",
        source_model_id="model_source_v1",
        target_provider_id="provider_target_001",
        target_model_id="model_target_v1",
        reason_error_id="provider_error_001",
        reason_error_code="PROVIDER_RATE_LIMITED",
        decision_status=status,
        same_scoring_policy=True, same_prompt_version=True,
        same_evidence_manifest_sha256=True, same_input_fingerprint=True,
        same_response_schema_version=True, same_scoring_mode=True,
        same_temperature=True, same_seed=True, same_max_output_tokens=True,
        successful_result_exists=False,
        approval_required=True,
        approved_by=None, approved_at=None,
        created_at=T0, executed_at=None, event_ref=None,
    )
    base.update(overrides)
    return ProviderSwitchDecision(**base)


def make_record(revision=1, decision=None, proposal=None, idem=IDEM, **overrides):
    base = dict(
        contract_version=CONTRACT,
        revision=revision,
        decision=decision or make_decision(),
        proposal=proposal or make_proposal(),
        idempotency_key=idem,
        transition_reason_code=None,
        created_at=T0,
        updated_at=T0,
        event_sequence=1 if revision == 1 else revision,
    )
    base.update(overrides)
    return ProviderSwitchRecord(**base)


@pytest.fixture
def store(tmp_path):
    return ProviderSwitchStore(tmp_path / "provider-runtime" / "switches")


def _dir(store, task=TASK, item=ITEM, decision_id=DECISION_ID):
    return store.root / task / item / decision_id


# ---------------- 创建与 revision ---------------- #


def test_create_revision_1(store):
    out = store.create(make_record())
    assert out == "created"
    rec = store.read(TASK, ITEM, DECISION_ID)
    assert rec.revision == 1
    assert rec.decision.decision_status == "pending_approval"
    assert rec.event_sequence == 1


def test_revision_increments(store):
    store.create(make_record())
    rec = store.read(TASK, ITEM, DECISION_ID)
    new = make_record(revision=2, decision=make_decision(status="approved",
                                                         target_attempt_id="attempt_target_001",
                                                         approved_by="controller", approved_at=T0),
                      event_sequence=2)
    from models.provider_switch import SwitchEvent
    store.update(DECISION_ID, TASK, ITEM, new, expected_revision=1, event=SwitchEvent(
        sequence=2, decision_id=DECISION_ID, transition="approved",
        from_status="pending_approval", to_status="approved",
        revision_before=1, revision_after=2, occurred_at=T0, approved_by="controller"))
    rec2 = store.read(TASK, ITEM, DECISION_ID)
    assert rec2.revision == 2
    assert rec2.decision.decision_status == "approved"


def test_history_revisions_not_overwritten(store):
    store.create(make_record())
    d = _dir(store)
    rev1 = d / "revisions" / "000001.json"
    before = rev1.read_bytes()
    from models.provider_switch import SwitchEvent
    store.update(DECISION_ID, TASK, ITEM,
                 make_record(revision=2, decision=make_decision(status="rejected"), event_sequence=2),
                 expected_revision=1,
                 event=SwitchEvent(sequence=2, decision_id=DECISION_ID, transition="rejected",
                                   from_status="pending_approval", to_status="rejected",
                                   revision_before=1, revision_after=2, occurred_at=T0,
                                   reason_code="REJECTED"))
    assert rev1.read_bytes() == before  # 历史 revision 未被覆盖
    assert (d / "revisions" / "000002.json").exists()


def test_current_points_to_latest(store):
    store.create(make_record())
    d = _dir(store)
    from models.provider_switch import SwitchEvent
    store.update(DECISION_ID, TASK, ITEM,
                 make_record(revision=2, decision=make_decision(status="rejected"), event_sequence=2),
                 expected_revision=1,
                 event=SwitchEvent(sequence=2, decision_id=DECISION_ID, transition="rejected",
                                   from_status="pending_approval", to_status="rejected",
                                   revision_before=1, revision_after=2, occurred_at=T0,
                                   reason_code="REJECTED"))
    cur = json.loads((d / "current.json").read_text(encoding="utf-8"))
    assert cur["revision"] == 2
    assert cur["ref"] == "revisions/000002.json"


def test_event_sequence_continuous(store):
    store.create(make_record())
    d = _dir(store)
    events = [json.loads(line) for line in (d / "events.ndjson").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [e["sequence"] for e in events] == [1]


def test_idempotent_hit_no_new_revision_or_event(store):
    store.create(make_record())
    before_files = sorted(str(p.relative_to(_dir(store))) for p in _dir(store).rglob("*") if p.is_file())
    out = store.create(make_record())  # 同 decision_id 同 idem key
    assert out == "idempotent_hit"
    after_files = sorted(str(p.relative_to(_dir(store))) for p in _dir(store).rglob("*") if p.is_file())
    assert before_files == after_files  # 不新增任何文件


def test_idempotency_conflict(store):
    store.create(make_record(idem=IDEM))
    with pytest.raises(SwitchStoreError) as ei:
        store.create(make_record(idem="e" * 64))
    assert ei.value.error_code == ERR_IDEMPOTENCY_CONFLICT


def test_revision_conflict_zero_side_effect(store):
    store.create(make_record())
    from models.provider_switch import SwitchEvent
    with pytest.raises(SwitchStoreError) as ei:
        store.update(DECISION_ID, TASK, ITEM,
                     make_record(revision=2, decision=make_decision(status="rejected"), event_sequence=2),
                     expected_revision=99,  # 错误 expected
                     event=SwitchEvent(sequence=2, decision_id=DECISION_ID, transition="rejected",
                                       from_status="pending_approval", to_status="rejected",
                                       revision_before=1, revision_after=2, occurred_at=T0))
    assert ei.value.error_code == ERR_REVISION_CONFLICT
    rec = store.read(TASK, ITEM, DECISION_ID)
    assert rec.revision == 1  # 零副作用


def test_write_failure_no_partial(store, monkeypatch):
    store.create(make_record())
    import services.provider_switch_store as mod

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(mod, "_append_ndjson", _boom)
    from models.provider_switch import SwitchEvent
    with pytest.raises(SwitchStoreError):
        store.update(DECISION_ID, TASK, ITEM,
                     make_record(revision=2, decision=make_decision(status="rejected"), event_sequence=2),
                     expected_revision=1,
                     event=SwitchEvent(sequence=2, decision_id=DECISION_ID, transition="rejected",
                                       from_status="pending_approval", to_status="rejected",
                                       revision_before=1, revision_after=2, occurred_at=T0))
    rec = store.read(TASK, ITEM, DECISION_ID)
    assert rec.revision == 1  # 保留旧现场（revision 文件未提交前失败或 current 未更新）


def test_corrupted_json_explicit_failure(store):
    store.create(make_record())
    (store.root / TASK / ITEM / DECISION_ID / "current.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(SwitchStoreError) as ei:
        store.read(TASK, ITEM, DECISION_ID)
    assert ei.value.error_code == "SIDECAR_SWITCH_CORRUPTED"


def test_current_revision_mismatch_explicit_failure(store):
    store.create(make_record())
    d = _dir(store)
    (d / "current.json").write_text(json.dumps({"revision": 99, "ref": "revisions/000001.json"}), encoding="utf-8")
    with pytest.raises(SwitchStoreError) as ei:
        store.read(TASK, ITEM, DECISION_ID)
    assert ei.value.error_code == "SIDECAR_SWITCH_CORRUPTED"


def test_restart_readable(store, tmp_path):
    store.create(make_record())
    store2 = ProviderSwitchStore(tmp_path / "provider-runtime" / "switches")
    rec = store2.read(TASK, ITEM, DECISION_ID)
    assert rec is not None and rec.revision == 1
    assert rec.decision.decision_status == "pending_approval"


def test_stable_sort_and_pagination(store):
    ids = ["switch_b_002", "switch_a_001", "switch_c_003"]
    for i, did in enumerate(ids):
        store.create(make_record(decision=make_decision(decision_id=did), idem=chr(97 + i) * 64))
    all_recs = store.list(TASK)
    assert [r.decision.switch_decision_id for r in all_recs] == sorted(ids)
    page = store.list(TASK, offset=1, limit=1)
    assert len(page) == 1
    assert page[0].decision.switch_decision_id == "switch_b_002"


def test_no_empty_dirs_created(store, tmp_path):
    assert store.list("task_ghost") == []
    assert store.list("task_ghost", item_id="item_x") == []
    # 查询不创建目录
    assert not (store.root / "task_ghost").exists()


# ---------------- 11D-3b-fix-1：多文件写入事务一致性 ---------------- #


def _fail_on(monkeypatch, filename):
    """注入指定文件名写入失败；返回恢复函数（撤销注入）。"""
    import services.provider_switch_store as mod
    orig_json = mod._atomic_write_json
    orig_append = mod._append_ndjson

    def failing_json(target, obj):
        if Path(target).name == filename:
            raise OSError(f"inject failure: {filename}")
        return orig_json(target, obj)

    def failing_append(path, obj):
        if Path(path).name == filename:
            raise OSError(f"inject failure: {filename}")
        return orig_append(path, obj)

    monkeypatch.setattr(mod, "_atomic_write_json", failing_json)
    monkeypatch.setattr(mod, "_append_ndjson", failing_append)

    def restore():
        monkeypatch.setattr(mod, "_atomic_write_json", orig_json)
        monkeypatch.setattr(mod, "_append_ndjson", orig_append)

    return restore


def _event_count(store, task=TASK, item=ITEM, decision_id=DECISION_ID):
    ep = _dir(store, task, item, decision_id) / "events.ndjson"
    if not ep.exists():
        return 0
    return len([l for l in ep.read_text(encoding="utf-8").splitlines() if l.strip()])


CREATE_FAIL_POINTS = ["idempotency.json", "000001.json", "current.json", "request.json", "events.ndjson"]


@pytest.mark.parametrize("fail_point", CREATE_FAIL_POINTS)
def test_create_failure_cleans_up(store, monkeypatch, fail_point):
    """create 任一写入点失败：清理新建目录，不留下可读取/可幂等命中的半成品，返回 TRANSACTION_ERROR。"""
    restore = _fail_on(monkeypatch, fail_point)
    with pytest.raises(SwitchStoreError) as ei:
        store.create(make_record())
    assert ei.value.error_code == ERR_TRANSACTION_ERROR
    # 无半成品：read 与幂等查询均不可见
    assert store.read(TASK, ITEM, DECISION_ID) is None
    assert store.find_by_idempotency(TASK, ITEM, IDEM) is None
    # decision 目录及空父目录已清理
    assert not _dir(store).exists()
    assert not (store.root / TASK / ITEM).exists()
    # 恢复后幂等键仍可正常创建
    restore()
    assert store.create(make_record()) == "created"
    rec = store.read(TASK, ITEM, DECISION_ID)
    assert rec is not None and rec.revision == 1


def test_create_failure_does_not_affect_existing(store, monkeypatch):
    """create 失败清理不影响既有 decision 目录。"""
    store.create(make_record())  # 既有
    _fail_on(monkeypatch, "current.json")
    with pytest.raises(SwitchStoreError) as ei:
        store.create(make_record(decision=make_decision(decision_id="switch_decision_002"), idem="f" * 64))
    assert ei.value.error_code == ERR_TRANSACTION_ERROR
    # 既有记录完整可读
    rec = store.read(TASK, ITEM, DECISION_ID)
    assert rec is not None and rec.revision == 1
    # 失败的新 decision 目录已清理
    assert not (store.root / TASK / ITEM / "switch_decision_002").exists()


def test_create_cleanup_failure_still_raises_transaction_error(store, monkeypatch):
    """清理本身失败仍返回稳定事务错误，不吞原始失败语义。"""
    import services.provider_switch_store as mod
    orig_unlink = Path.unlink

    def _boom_unlink(self, *a, **k):
        raise OSError("unlink denied")

    def _boom_json(target, obj):
        raise OSError("inject failure")

    monkeypatch.setattr(Path, "unlink", _boom_unlink)
    monkeypatch.setattr(mod, "_atomic_write_json", _boom_json)
    with pytest.raises(SwitchStoreError) as ei:
        store.create(make_record())
    assert ei.value.error_code == ERR_TRANSACTION_ERROR


UPDATE_FAIL_POINTS = ["000002.json", "current.json", "request.json", "events.ndjson"]


@pytest.mark.parametrize("fail_point", UPDATE_FAIL_POINTS)
def test_update_failure_rolls_back(store, monkeypatch, fail_point):
    """update 任一写入点失败：current/request 恢复旧 revision，旧记录可读，事件序列不增加。"""
    store.create(make_record())
    from models.provider_switch import SwitchEvent
    new_rec = make_record(revision=2, decision=make_decision(status="rejected"), event_sequence=2)
    ev = SwitchEvent(sequence=2, decision_id=DECISION_ID, transition="rejected",
                     from_status="pending_approval", to_status="rejected",
                     revision_before=1, revision_after=2, occurred_at=T0, reason_code="REJECTED")
    restore = _fail_on(monkeypatch, fail_point)
    with pytest.raises(SwitchStoreError) as ei:
        store.update(DECISION_ID, TASK, ITEM, new_rec, expected_revision=1, event=ev)
    assert ei.value.error_code == ERR_TRANSACTION_ERROR
    # 旧记录仍可正常读取（revision 1, pending）
    rec = store.read(TASK, ITEM, DECISION_ID)
    assert rec is not None and rec.revision == 1
    assert rec.decision.decision_status == "pending_approval"
    # current 与 request 恢复到旧 revision
    d = _dir(store)
    cur = json.loads((d / "current.json").read_text(encoding="utf-8"))
    assert cur["revision"] == 1
    req = json.loads((d / "request.json").read_text(encoding="utf-8"))
    assert req["revision"] == 1
    # 事件序列不增加
    assert _event_count(store) == 1
    # 幂等查询仍命中（原 decision 不受影响）
    assert store.find_by_idempotency(TASK, ITEM, IDEM) == DECISION_ID
    # 失败后仍可基于 revision 1 重试成功
    restore()
    assert store.update(DECISION_ID, TASK, ITEM, new_rec, expected_revision=1, event=ev) is not None
    assert store.read(TASK, ITEM, DECISION_ID).revision == 2


def test_persisted_files_sensitive_scan(store):
    store.create(make_record())
    for f in _dir(store).rglob("*.json"):
        text = f.read_text(encoding="utf-8")
        low = text.lower()
        assert "top_secret" not in low
        assert "bearer" not in low
        assert "sk-" not in low
        assert "http://" not in low and "https://" not in low
        assert "api_key" not in low
    events = ( _dir(store) / "events.ndjson").read_text(encoding="utf-8")
    assert "top_secret" not in events.lower()
