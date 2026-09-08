"""
Phase 11E-2a-prerequisite-impl-1：PipelineTask 评分任务类型扩展 + ScoringTaskConfiguration 合成测试。

覆盖：旧任务 JSON 兼容、两种 task_type 固定 scope、交叉污染拒绝、scoring 初始形态、
ScoringTaskConfiguration 必填/不可变/fingerprint 校验、未知 task type 拒绝。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from models.pipeline_task import (
    PipelineTask,
    compute_task_idempotency,
    sha256_canonical,
)
from models.scoring_configuration import ScoringTaskConfiguration

T0 = datetime(2026, 8, 11, 3, 0, 0, tzinfo=timezone.utc)
TASK_ID = "00000000-0000-0000-0000-000000000601"
ITEM_ID = "00000000-0000-0000-0000-000000000602"
FP = "c" * 64
IDEM = "d" * 64
SHA256 = "e" * 64
CONFIG_FP = "f" * 64


def _config_snapshot():
    return {"evidence_contract_version": "evidence-package/v1.1", "validator_version": "1.0.0"}


_FIXED_DEPENDS = {
    "import": [], "validate": ["import"], "score": ["validate"],
    "review": ["validate", "score"], "export": ["review"],
}
# 11E-2a-prerequisite-impl-1-fix-1：scoring 范围内依赖（score 上游前置由 source_task_id 负责）
_SCORING_DEPENDS = {
    "import": [], "validate": ["import"], "score": [],
    "review": ["score"], "export": ["review"],
}


def _stage_summaries(stages, task_type="evidence_preparation_pipeline", score_status="pending"):
    """按 task_type 生成五阶段摘要（scoring 使用独立依赖与初始状态，不复用 evidence 构造）。"""
    deps_map = _SCORING_DEPENDS if task_type == "scoring_pipeline" else _FIXED_DEPENDS
    out = []
    for i, s in enumerate(stages):
        if task_type == "scoring_pipeline" and s == "score":
            status = score_status
        elif task_type == "scoring_pipeline":
            status = "not_started"  # 范围外（import/validate）与未启动（review/export）
        else:
            status = "pending" if i == 0 else "not_started"
        out.append({
            "stage": s, "status": status,
            "depends_on": list(deps_map.get(s, [])), "started_at": None, "completed_at": None,
            "total_items": 0, "pending_items": 0, "running_items": 0, "completed_items": 0,
            "failed_items": 0, "skipped_items": 0, "manual_review_items": 0,
            "blocking_error_codes": [], "revision": 1,
        })
    return out


def _item_index_entry(stage):
    return {
        "item_id": ITEM_ID, "package_id": "pkg-demo-001", "package_revision": 1,
        "status": "pending", "current_stage": stage, "input_fingerprint": FP,
        "item_revision": 1, "evidence_level": "sufficient",
    }


def _task_data(**overrides):
    snap = _config_snapshot()
    data = {
        "contract_version": "pipeline-task/v1",
        "task_type": "evidence_preparation_pipeline",
        "execution_scope": ["import", "validate"],
        "task_id": TASK_ID,
        "batch_id": "batch-demo-001",
        "status": "pending",
        "current_stage": "import",
        "created_at": T0,
        "started_at": None,
        "updated_at": T0,
        "paused_at": None,
        "completed_at": None,
        "total_items": 1,
        "pending_items": 1,
        "running_items": 0,
        "completed_items": 0,
        "failed_items": 0,
        "skipped_items": 0,
        "manual_review_items": 0,
        "concurrency": 1,
        "configuration_snapshot": snap,
        "configuration_fingerprint": sha256_canonical(snap),
        "item_index": [_item_index_entry("import")],
        "stage_summaries": [],  # 按 task_type 在下方生成
        "error_summary": {"count": 0, "codes": []},
        "last_event_sequence": 0,
        "revision": 1,
        "idempotency_key": IDEM,
        "idempotency_payload_sha256": SHA256,
    }
    data.update(overrides)
    data["stage_summaries"] = _stage_summaries(
        ["import", "validate", "score", "review", "export"],
        task_type=data["task_type"],
    )
    return data


SOURCE_TASK_ID = "00000000-0000-0000-0000-000000000701"


def _scoring_config():
    fields = dict(
        profile_version="profile-mixed-v1",
        provider_id="provider_demo_001",
        model_id="model_demo_v1",
        scoring_policy_version="policy-001",
        rubric_version="rubric-001",
        prompt_version="prompt-001",
        response_schema_version="score-response-v1",
    )
    return ScoringTaskConfiguration(**fields, configuration_fingerprint=sha256_canonical(fields))


def _scoring_task_data(**overrides):
    data = _task_data(
        task_type="scoring_pipeline",
        execution_scope=["score", "review", "export"],
        source_task_id=SOURCE_TASK_ID,
        configuration_snapshot=_scoring_config(),
        configuration_fingerprint=_scoring_config().configuration_fingerprint,
        current_stage="score",
        total_items=0,
        pending_items=0,
        item_index=[],
    )
    data.update(overrides)
    return data


def make_config(**overrides):
    fields = dict(
        profile_version="profile-mixed-v1",
        provider_id="provider_demo_001",
        model_id="model_demo_v1",
        scoring_policy_version="policy-001",
        rubric_version="rubric-001",
        prompt_version="prompt-001",
        response_schema_version="score-response-v1",
    )
    fields.update(overrides)
    if "configuration_fingerprint" not in fields:
        fields["configuration_fingerprint"] = sha256_canonical(fields)
    return ScoringTaskConfiguration(**fields)


# ---------------- 兼容性 ---------------- #


def test_old_task_json_roundtrip():
    """旧 evidence_preparation_pipeline JSON 反序列化 + 重新序列化无破坏性变化。"""
    raw = _task_data()
    t = PipelineTask(**raw)
    assert t.task_type == "evidence_preparation_pipeline"
    assert t.execution_scope == ["import", "validate"]
    assert t.current_stage == "import"
    dumped = t.model_dump(mode="json")
    import json as _json
    from datetime import datetime as _dt
    raw_json = _json.loads(_json.dumps(
        raw, default=lambda o: o.isoformat() if isinstance(o, _dt) else str(o)))
    for k, v in raw_json.items():
        if k in ("created_at", "started_at", "updated_at", "paused_at", "completed_at"):
            # datetime 展示格式差异（Z vs +00:00）为等价；None 保持 None
            assert dumped[k] == v or (
                dumped[k] is not None and v is not None
                and _dt.fromisoformat(dumped[k].replace("Z", "+00:00")) == _dt.fromisoformat(v)
            ), k
        elif k == "stage_summaries":
            assert len(dumped[k]) == len(v)
        elif k == "item_index":
            assert dumped[k][0]["item_id"] == v[0]["item_id"]
        else:
            assert dumped[k] == v, k
    # 重新序列化后再次解析仍合法
    PipelineTask(**dumped)


def test_old_task_cannot_enter_score_stages():
    """旧任务不能进入 score/review/export（scope/current_stage 校验拒绝）。"""
    with pytest.raises(ValidationError):
        PipelineTask(**_task_data(execution_scope=["import", "validate", "score"]))
    with pytest.raises(ValidationError):
        PipelineTask(**_task_data(current_stage="score"))


# ---------------- 类型与 scope ---------------- #


def test_legal_evidence_task():
    """合法 evidence_preparation_pipeline task。"""
    t = PipelineTask(**_task_data())
    assert t.task_type == "evidence_preparation_pipeline"
    assert t.execution_scope == ["import", "validate"]


def test_legal_scoring_task_initial_form():
    """合法 scoring_pipeline task 初始形态：pending + current_stage=score + 固定 scope。"""
    t = PipelineTask(**_scoring_task_data())
    assert t.task_type == "scoring_pipeline"
    assert t.execution_scope == ["score", "review", "export"]
    assert t.status == "pending"
    assert t.current_stage == "score"


def test_scope_cross_contamination_rejected():
    """两种 task_type 的 scope 交叉污染均被拒绝。"""
    with pytest.raises(ValidationError):
        PipelineTask(**_task_data(execution_scope=["score", "review", "export"]))  # evidence + scoring scope
    with pytest.raises(ValidationError):
        PipelineTask(**_scoring_task_data(execution_scope=["import", "validate"]))  # scoring + evidence scope
    with pytest.raises(ValidationError):
        PipelineTask(**_scoring_task_data(execution_scope=["import", "score"]))  # 混合
    with pytest.raises(ValidationError):
        PipelineTask(**_scoring_task_data(current_stage="import"))  # scoring item 阶段混入 import


def test_scoring_task_stage_scope_consistency():
    """scoring task current_stage 属于 score/review/export。

    初始（pending）必须 score；推进形态（非 pending）可表达 review/export 但必须在 scope 内。
    """
    # pending + review -> 拒绝（初始不变量）
    with pytest.raises(ValidationError):
        PipelineTask(**_scoring_task_data(current_stage="review"))
    # 合法推进形态：running + review（score 已完成、review 已启动）
    d = _scoring_task_data(status="running", current_stage="review", started_at=T0)
    d["stage_summaries"] = _stage_summaries(
        ["import", "validate", "score", "review", "export"],
        task_type="scoring_pipeline", score_status="completed",
    )
    d["stage_summaries"][3]["status"] = "pending"  # review 启动
    d["stage_summaries"][2]["completed_at"] = T0.isoformat()  # score 完成时间
    t = PipelineTask(**d)
    assert t.current_stage == "review"
    # 不在 scope 的阶段拒绝
    with pytest.raises(ValidationError):
        PipelineTask(**_scoring_task_data(current_stage="validate"))


def test_unknown_task_type_rejected():
    """未知 task type 被拒绝。"""
    with pytest.raises(ValidationError):
        PipelineTask(**_task_data(task_type="unknown_pipeline"))
    with pytest.raises(ValueError):
        compute_task_idempotency(
            contract_version="pipeline-task/v1", task_type="unknown_pipeline",
            batch_id="b1", execution_scope=["import", "validate"],
            items=[("pkg-a", 1, FP)], configuration_fingerprint=CONFIG_FP,
        )


def test_compute_task_idempotency_type_dispatch():
    """compute_task_idempotency 按 task_type 分派校验 scope。"""
    kw = dict(contract_version="pipeline-task/v1", batch_id="batch-demo-001",
              items=[("pkg-a", 1, FP)], configuration_fingerprint=CONFIG_FP)
    k1 = compute_task_idempotency(task_type="evidence_preparation_pipeline",
                                  execution_scope=["import", "validate"], **kw)
    k2 = compute_task_idempotency(task_type="scoring_pipeline",
                                  execution_scope=["score", "review", "export"], **kw)
    assert k1 and k2 and k1 != k2
    with pytest.raises(ValueError):
        compute_task_idempotency(task_type="scoring_pipeline",
                                 execution_scope=["import", "validate"], **kw)  # 类型-范围不匹配
    with pytest.raises(ValueError):
        compute_task_idempotency(task_type="evidence_preparation_pipeline",
                                 execution_scope=["score", "review", "export"], **kw)


# ---------------- ScoringTaskConfiguration ---------------- #


def test_config_required_fields():
    """缺少 ScoringTaskConfiguration 必需字段时失败。"""
    base = make_config()
    for field in ("profile_version", "provider_id", "model_id", "scoring_policy_version",
                  "rubric_version", "prompt_version", "response_schema_version",
                  "configuration_fingerprint"):
        d = base.model_dump()
        del d[field]
        with pytest.raises(ValidationError):
            ScoringTaskConfiguration(**d)


def test_config_immutable():
    """ScoringTaskConfiguration 不可原地修改。"""
    cfg = make_config()
    with pytest.raises(Exception):
        cfg.prompt_version = "prompt-002"
    with pytest.raises(Exception):
        cfg.provider_id = "provider_hacked"
    # 构造后原始快照字段不变
    assert cfg.provider_id == "provider_demo_001"


def test_config_fingerprint_format():
    """fingerprint 格式 + 内容一致性（fix-1：伪造指纹在构造时拒绝）。"""
    cfg = make_config()
    assert cfg.configuration_fingerprint == sha256_canonical(
        cfg.model_dump(exclude={"configuration_fingerprint"}))
    for bad in ("short", "G" * 64, "f" * 63, "f" * 65):
        data = dict(make_config().model_dump())
        data["configuration_fingerprint"] = bad
        with pytest.raises(ValidationError):
            ScoringTaskConfiguration(**data)
    # 合法格式但与配置内容不符（伪造）-> 拒绝
    with pytest.raises(ValidationError):
        make_config(configuration_fingerprint=CONFIG_FP)


def test_config_extra_forbidden():
    """extra=forbid：不允许未声明字段。"""
    with pytest.raises(ValidationError):
        ScoringTaskConfiguration(**make_config().model_dump(), api_key="sk-x")
    with pytest.raises(ValidationError):
        ScoringTaskConfiguration(**make_config().model_dump(), temperature=0.5)


def test_config_sensitive_rejected():
    """配置对象不含正文/Key/URL（安全版本标识校验拒绝不安全值）。"""
    with pytest.raises(ValidationError):
        make_config(prompt_version="https://evil/x")
    with pytest.raises(ValidationError):
        make_config(profile_version="path/with/slash")


# ---------------- 11E-2a-prerequisite-impl-1-fix-1：scoring 阶段不变量 ---------------- #


def _summary_by_stage(task_data):
    return {s["stage"]: s for s in task_data["stage_summaries"]}


def _scoring_pending(d=None, **kw):
    """标准 scoring pending 初始数据（支持覆盖参数）。"""
    return _scoring_task_data(**kw) if d is None else d


def test_scoring_pending_initial_invariants():
    """scoring pending 初始状态完整通过：current_stage=score、score=pending、
    review/export=not_started、import/validate=not_started 且计数为零。"""
    d = _scoring_pending()
    t = PipelineTask(**d)
    assert t.status == "pending" and t.current_stage == "score"
    sm = _summary_by_stage(d)
    assert sm["score"]["status"] == "pending"
    assert sm["review"]["status"] == "not_started"
    assert sm["export"]["status"] == "not_started"
    for st in ("import", "validate"):
        assert sm[st]["status"] == "not_started"
        for k in ("total_items", "pending_items", "running_items", "completed_items",
                  "failed_items", "skipped_items", "manual_review_items"):
            assert sm[st][k] == 0, (st, k)


def test_scoring_pending_current_stage_not_score_rejected():
    """pending scoring current_stage=review/export/null 拒绝。"""
    with pytest.raises(ValidationError):
        PipelineTask(**_scoring_pending(current_stage="review"))
    with pytest.raises(ValidationError):
        PipelineTask(**_scoring_pending(current_stage="export"))
    with pytest.raises(ValidationError):
        PipelineTask(**_scoring_pending(current_stage=None))


def test_scoring_current_stage_score_with_score_not_started_rejected():
    """current_stage=score 但 score summary=not_started 拒绝（非 pending 推进场景）。"""
    d = _scoring_pending(status="running", current_stage="score", started_at=T0)
    d["stage_summaries"] = _stage_summaries(
        ["import", "validate", "score", "review", "export"],
        task_type="scoring_pipeline", score_status="not_started",
    )
    with pytest.raises(ValidationError):
        PipelineTask(**d)


def test_scoring_import_validate_must_stay_not_started():
    """scoring import/validate 非 not_started 拒绝。"""
    for st in ("import", "validate"):
        for bad_status in ("pending", "running", "completed", "failed"):
            d = _scoring_pending()
            d["stage_summaries"] = _stage_summaries(
                ["import", "validate", "score", "review", "export"],
                task_type="scoring_pipeline",
            )
            idx = ["import", "validate", "score", "review", "export"].index(st)
            d["stage_summaries"][idx]["status"] = bad_status
            with pytest.raises(ValidationError):
                PipelineTask(**d)
    # 范围外阶段计数非零拒绝
    d = _scoring_pending()
    d["stage_summaries"][0]["total_items"] = 1
    d["stage_summaries"][0]["pending_items"] = 1
    with pytest.raises(ValidationError):
        PipelineTask(**d)


def test_scoring_dependencies_frozen():
    """scoring 依赖：score=[]、review=[score]、export=[review]（由模型冻结并强制）。"""
    d = _scoring_pending()
    sm = _summary_by_stage(d)
    assert sm["score"]["depends_on"] == []
    assert sm["review"]["depends_on"] == ["score"]
    assert sm["export"]["depends_on"] == ["review"]
    # 篡改依赖拒绝
    for stage, bad in (("score", ["validate"]), ("review", ["validate", "score"]),
                       ("export", ["score"])):
        dd = _scoring_pending()
        idx = ["import", "validate", "score", "review", "export"].index(stage)
        dd["stage_summaries"][idx]["depends_on"] = bad
        with pytest.raises(ValidationError):
            PipelineTask(**dd)


def test_staggered_review_while_score_active_allowed():
    """错位合法（impl-3-fix-1）：item 级流水线，A=review 时 B 仍可 score，无整批屏障。"""
    d = _scoring_pending(status="running", current_stage="score", started_at=T0)
    d["stage_summaries"] = _stage_summaries(
        ["import", "validate", "score", "review", "export"],
        task_type="scoring_pipeline", score_status="running",
    )
    d["stage_summaries"][2].update(running_items=1, total_items=1, started_at=T0.isoformat())  # B 在 score/running
    d["stage_summaries"][3].update(status="pending", pending_items=1, total_items=1)  # A 在 review/pending
    t = PipelineTask(**d)  # 模型允许错位
    assert t.current_stage == "score"  # 取最早活动阶段


def test_staggered_export_while_score_review_active_allowed():
    """错位合法：A=export、B=review、C=score 同时存在（score/review/export 可并行聚合）。"""
    d = _scoring_pending(status="running", current_stage="score", started_at=T0)
    d["stage_summaries"] = _stage_summaries(
        ["import", "validate", "score", "review", "export"],
        task_type="scoring_pipeline", score_status="running",
    )
    d["stage_summaries"][2].update(running_items=1, total_items=1, started_at=T0.isoformat())   # C 在 score
    d["stage_summaries"][3].update(status="pending", pending_items=1, total_items=1)  # B 在 review
    d["stage_summaries"][4].update(status="pending", pending_items=1, total_items=1)  # A 在 export
    t = PipelineTask(**d)
    assert t.current_stage == "score"
    # 单 item 不跨阶段（A 在 export 时不能回到 score/review）由 Manager 转换入口保证，
    # 模型只校验依赖结构与初始状态（见 test_scoring_pipeline_transitions.py）
