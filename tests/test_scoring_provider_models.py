"""
Phase 11D-1b-fix-1：ScoringProvider 契约模型合成测试。

契约对齐（fix-1）：
- 能力三值 = Literal[True, False, "unknown"]（JSON 布尔；字符串 "true"/"false" 拒绝）。
- capability_ref = capability/<provider_id>/<model_id>/<capability_version>（canonical 三段）。

所有数据均为脱敏、虚构合成数据，不包含真实 Provider、Key、Token、URL 或学生材料。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from models.scoring_provider import (
    ModelCapability,
    ProviderError,
    ProviderHealth,
    ProviderRequest,
    ProviderResponse,
    ProviderSelection,
    ProviderSwitchDecision,
    ScoringProvider,
    build_capability_ref,
    parse_capability_ref,
)

T0 = datetime(2026, 8, 10, 6, 0, 0, tzinfo=timezone.utc)
SHA256 = "a" * 64
FP = "b" * 64

REF = build_capability_ref("provider_demo_001", "model_demo_v1", "cap-demo-001")


# ---------------- 合成 builder ---------------- #


def make_provider(**overrides):
    base = dict(
        contract_version="scoring-provider/v1",
        provider_id="provider_demo_001",
        provider_type="openai_compatible",
        display_name="Demo Provider",
        endpoint_profile="profile_demo_v1",
        model_id="model_demo_v1",
        capability_version="cap-demo-001",
        config_version="cfg-demo-001",
        enabled=True,
        credential_ref="TEST_PROVIDER_API_KEY",
        capability_ref=REF,
        created_at=T0,
        updated_at=T0,
    )
    base.update(overrides)
    return ScoringProvider(**base)


def make_capability(**overrides):
    base = dict(
        contract_version="scoring-provider/v1",
        capability_version="cap-demo-001",
        provider_id="provider_demo_001",
        model_id="model_demo_v1",
        text_input=True,
        image_input=False,
        structured_json_output=True,
        system_message=True,
        temperature_supported=True,
        seed_supported=True,
        verification_source="static_config",
    )
    base.update(overrides)
    return ModelCapability(**base)


def make_request(**overrides):
    base = dict(
        contract_version="scoring-provider/v1",
        request_id="provider_request_demo_001",
        task_id="pipeline_task_demo_001",
        item_id="item_demo_001",
        attempt_id="attempt_demo_001",
        provider_id="provider_demo_001",
        model_id="model_demo_v1",
        capability_version="cap-demo-001",
        config_version="cfg-demo-001",
        evidence_package_id="evidence_demo_001",
        evidence_manifest_sha256=SHA256,
        input_fingerprint=FP,
        scoring_policy_version="policy-demo-001",
        scoring_mode="mixed",
        prompt_version="prompt-demo-001",
        response_schema_version="score-response-v1",
        timeout_seconds=120,
        requested_at=T0,
        input_modalities=["text"],
        evidence_refs=["evidence_demo_001"],
        request_payload_sha256=FP,
        selection_id="selection_demo_001",
    )
    base.update(overrides)
    return ProviderRequest(**base)


def make_response(**overrides):
    base = dict(
        contract_version="scoring-provider/v1",
        response_id="provider_response_demo_001",
        request_id="provider_request_demo_001",
        task_id="pipeline_task_demo_001",
        item_id="item_demo_001",
        attempt_id="attempt_demo_001",
        provider_id="provider_demo_001",
        model_id="model_demo_v1",
        provider_request_id=None,
        http_status_summary=None,
        result_status="success",
        structured_output_ref=None,
        output_sha256=SHA256,
        usage=None,
        latency_ms=800,
        completed_at=T0,
        schema_validation_passed=True,
        scoring_rule_validation_passed=True,
        error_ref=None,
        input_fingerprint=FP,
        prompt_version="prompt-demo-001",
        evidence_manifest_sha256=SHA256,
    )
    base.update(overrides)
    return ProviderResponse(**base)


def make_error(**overrides):
    base = dict(
        contract_version="scoring-provider/v1",
        error_id="provider_error_demo_001",
        request_id="provider_request_demo_001",
        task_id="pipeline_task_demo_001",
        item_id="item_demo_001",
        attempt_id="attempt_demo_001",
        provider_id="provider_demo_001",
        model_id="model_demo_v1",
        error_code="PROVIDER_RATE_LIMITED",
        error_category="rate_limit",
        message_safe="Provider 返回限流状态，未记录响应正文",
        retryable="conditional",
        provider_switch_allowed="approval_required",
        manual_review_required="after_exhaustion",
        consumes_provider_call_attempt=True,
        stop_entire_batch="no",
        retry_after_seconds=30,
        occurred_at=T0,
        details_sha256=SHA256,
    )
    base.update(overrides)
    return ProviderError(**base)


def make_health(**overrides):
    base = dict(
        contract_version="scoring-provider/v1",
        health_id="provider_health_demo_001",
        provider_id="provider_demo_001",
        model_id="model_demo_v1",
        status="healthy",
        observed_at=T0,
        window_seconds=900,
        sample_size=10,
        success_rate=0.95,
        rate_limit_state="normal",
        quota_state="available",
        p95_latency_ms=2000,
        consecutive_failures=0,
        last_error_code=None,
        source="passive",
        expires_at=datetime(2026, 8, 10, 6, 15, 0, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return ProviderHealth(**base)


def make_selection(**overrides):
    base = dict(
        contract_version="scoring-provider/v1",
        selection_id="selection_demo_001",
        task_id="pipeline_task_demo_001",
        item_id="item_demo_001",
        attempt_id="attempt_demo_001",
        candidate_provider_ids=["provider_demo_001"],
        required_modalities=["text"],
        required_capabilities=["structured_json_output", "system_message"],
        evidence_manifest_sha256=SHA256,
        input_fingerprint=FP,
        scoring_policy_version="policy-demo-001",
        prompt_version="prompt-demo-001",
        response_schema_version="score-response-v1",
        selected_provider_id="provider_demo_001",
        selected_model_id="model_demo_v1",
        selection_status="selected",
        selection_strategy="approved_priority_then_capability",
        rejected_candidates=[],
        health_snapshot_refs=[],
        requires_human_approval=False,
        approved_by=None,
        approved_at=None,
        created_at=T0,
    )
    base.update(overrides)
    return ProviderSelection(**base)


def make_switch(**overrides):
    base = dict(
        contract_version="scoring-provider/v1",
        switch_decision_id="switch_demo_001",
        task_id="pipeline_task_demo_001",
        item_id="item_demo_001",
        source_attempt_id="attempt_demo_001",
        target_attempt_id=None,
        source_provider_id="provider_demo_001",
        source_model_id="model_demo_v1",
        target_provider_id="provider_demo_002",
        target_model_id="model_demo_v2",
        reason_error_id="provider_error_demo_001",
        reason_error_code="PROVIDER_RATE_LIMITED",
        decision_status="pending_approval",
        same_scoring_policy=True,
        same_prompt_version=True,
        same_evidence_manifest_sha256=True,
        same_input_fingerprint=True,
        same_response_schema_version=True,
        same_scoring_mode=True,
        same_temperature=True,
        same_seed=True,
        same_max_output_tokens=True,
        successful_result_exists=False,
        approval_required=True,
        approved_by=None,
        approved_at=None,
        created_at=T0,
        executed_at=None,
        event_ref=None,
    )
    base.update(overrides)
    return ProviderSwitchDecision(**base)


def make_approved_switch(**overrides):
    """构造合法 approved 切换决定（九个 same_* 全 true + 批准字段完整 + target attempt 已分配）。"""
    base = dict(
        decision_status="approved",
        target_attempt_id="attempt_target_001",
        approved_by="controller",
        approved_at=T0,
    )
    base.update(overrides)
    return make_switch(**base)


# ---------------- 基础模型约束 ---------------- #


def test_all_objects_valid_examples():
    """8 类对象合法示例全部通过。"""
    assert make_provider().provider_id == "provider_demo_001"
    assert make_capability().text_input is True
    assert make_request().timeout_seconds == 120
    assert make_response().result_status == "success"
    assert make_error().error_code == "PROVIDER_RATE_LIMITED"
    assert make_health().status == "healthy"
    assert make_selection().selection_status == "selected"
    assert make_switch().decision_status == "pending_approval"


def test_extra_fields_rejected():
    """extra=forbid：未知字段拒绝。"""
    for builder in (make_provider, make_capability, make_request, make_response,
                    make_error, make_health, make_selection, make_switch):
        with pytest.raises(ValidationError):
            builder(**{"unexpected_field": 1})


def test_naive_datetime_rejected():
    """无时区时间拒绝（AwareDatetime）。"""
    naive = datetime(2026, 8, 10, 6, 0, 0)  # 无 tzinfo
    for builder, field in (
        (make_provider, "created_at"),
        (make_request, "requested_at"),
        (make_response, "completed_at"),
        (make_error, "occurred_at"),
        (make_health, "observed_at"),
        (make_selection, "created_at"),
        (make_switch, "created_at"),
    ):
        with pytest.raises(ValidationError):
            builder(**{field: naive})


def test_bad_sha256_rejected():
    """hash 格式错误拒绝：非 64 位小写 hex。"""
    for builder, field in (
        (make_request, "evidence_manifest_sha256"),
        (make_request, "input_fingerprint"),
        (make_response, "output_sha256"),
        (make_error, "details_sha256"),
    ):
        for bad in ("ABC" * 22, "a" * 63, "a" * 65, "a" * 64 + "x", "sha256:" + "a" * 64):
            with pytest.raises(ValidationError):
                builder(**{field: bad})


def test_provider_request_rejects_credential_fields():
    """ProviderRequest 不接受凭据值字段。"""
    with pytest.raises(ValidationError):
        make_request(api_key="")
    with pytest.raises(ValidationError):
        make_request(authorization="Bearer abc123")


def test_provider_error_rejects_traceback_and_raw_exception():
    """ProviderError 不接受 traceback / 任意异常原文字段。"""
    with pytest.raises(ValidationError):
        make_error(traceback="Traceback (most recent call last)...")
    with pytest.raises(ValidationError):
        make_error(raw_exception="ConnectionResetError: [Errno 104]")
    e = make_error(message_safe="限流，未记录正文")
    assert e.message_safe == "限流，未记录正文"


def test_switch_decision_approval_and_success_protection():
    """SwitchDecision 批准与成功结果保护。"""
    sw = make_switch()
    assert sw.decision_status == "pending_approval"
    with pytest.raises(ValidationError):
        make_switch(decision_status="approved", successful_result_exists=True)
    with pytest.raises(ValidationError):
        make_switch(decision_status="executed", successful_result_exists=True)
    assert make_switch(decision_status="rejected", successful_result_exists=True).decision_status == "rejected"
    with pytest.raises(ValidationError):
        make_switch(decision_status="approved_by_magic")


# ---------------- 11D-3a-prerequisite-fix-1：scoring_mode 与同口径门 ---------------- #


def test_provider_request_requires_scoring_mode():
    """ProviderRequest 缺 scoring_mode 拒绝。"""
    d = make_request().model_dump()
    del d["scoring_mode"]
    with pytest.raises(ValidationError):
        ProviderRequest(**d)


def test_scoring_mode_accepted_values():
    """mixed/ai_only 接受。"""
    assert make_request(scoring_mode="mixed").scoring_mode == "mixed"
    assert make_request(scoring_mode="ai_only").scoring_mode == "ai_only"


def test_scoring_mode_other_values_rejected():
    """其他字符串拒绝。"""
    for bad in ("both", "rule_only", "", "Mixed"):
        with pytest.raises(ValidationError):
            make_request(scoring_mode=bad)


def test_switch_decision_requires_new_same_fields():
    """SwitchDecision 缺四个新字段拒绝。"""
    d = make_switch().model_dump()
    for f in ("same_scoring_mode", "same_temperature", "same_seed", "same_max_output_tokens"):
        d2 = dict(d)
        del d2[f]
        with pytest.raises(ValidationError):
            ProviderSwitchDecision(**d2)


def test_approved_rejects_new_same_field_false():
    """任一新增 same_* 为 false 时 approved 拒绝。"""
    for f in ("same_scoring_mode", "same_temperature", "same_seed", "same_max_output_tokens"):
        with pytest.raises(ValidationError):
            make_approved_switch(**{f: False})


def test_approved_rejects_original_same_field_false():
    """任一原有 same_* 为 false 时 approved 拒绝。"""
    for f in ("same_scoring_policy", "same_prompt_version", "same_evidence_manifest_sha256",
              "same_input_fingerprint", "same_response_schema_version"):
        with pytest.raises(ValidationError):
            make_approved_switch(**{f: False})


def test_approved_requires_approval_fields():
    """approved 缺 approved_by/approved_at 拒绝。"""
    with pytest.raises(ValidationError):
        make_approved_switch(approved_by=None)
    with pytest.raises(ValidationError):
        make_approved_switch(approved_at=None)


def test_approved_requires_target_attempt():
    """approved 缺 target_attempt_id 拒绝。"""
    with pytest.raises(ValidationError):
        make_approved_switch(target_attempt_id=None)


def test_executed_requires_executed_at():
    """executed 缺 executed_at 拒绝。"""
    with pytest.raises(ValidationError):
        make_approved_switch(decision_status="executed", executed_at=None)
    # 合法 executed
    sw = make_approved_switch(decision_status="executed", executed_at=T0)
    assert sw.decision_status == "executed"
    assert sw.executed_at == T0


def test_pending_approval_allows_missing_target_attempt():
    """pending_approval 可以无 target_attempt_id。"""
    sw = make_switch(target_attempt_id=None)
    assert sw.decision_status == "pending_approval"
    assert sw.target_attempt_id is None


def test_json_roundtrip_preserves_scoring_identity():
    """JSON round-trip 保留 scoring_mode 与九类一致性字段。"""
    req = make_request(scoring_mode="ai_only")
    again = ProviderRequest(**json.loads(json.dumps(req.model_dump(mode="json"))))
    assert again.scoring_mode == "ai_only"
    sw = make_approved_switch(decision_status="executed", executed_at=T0)
    sw2 = ProviderSwitchDecision(**json.loads(json.dumps(sw.model_dump(mode="json"))))
    assert sw2.same_scoring_mode is True
    assert sw2.same_temperature is True
    assert sw2.same_seed is True
    assert sw2.same_max_output_tokens is True
    assert sw2.target_attempt_id == "attempt_target_001"


# ---------------- 三值能力（fix-1） ---------------- #


def test_contract_141_boolean_capabilities_parse():
    """契约 14.1 示例布尔能力可解析（true -> True / false -> False）。"""
    cap = make_capability(
        text_input=True, image_input=True, structured_json_output=True,
        system_message=True, temperature_supported=True, seed_supported=True,
    )
    assert cap.text_input is True
    assert cap.image_input is True
    assert cap.structured_json_output is True


def test_contract_142_enabled_unknown_capability_loads():
    """契约 14.2：enabled=true + verification_source=unknown + temperature_supported=unknown 可加载。"""
    cap = make_capability(
        image_input=False,
        temperature_supported="unknown",
        seed_supported=False,
        verification_source="unknown",
        retry_capability={
            "supports_retry_after": "unknown",
            "idempotent_request_supported": "unknown",
            "max_safe_retries": None,
            "retryable_error_codes": [],
        },
    )
    assert cap.verification_source == "unknown"
    assert cap.temperature_supported == "unknown"
    assert cap.image_input is False
    assert cap.retry_capability.supports_retry_after == "unknown"


def test_json_roundtrip_keeps_boolean_type():
    """JSON round-trip 保持 true/false 布尔类型。"""
    cap = make_capability(text_input=True, image_input=False)
    dumped = cap.model_dump(mode="json")
    assert dumped["text_input"] is True and dumped["image_input"] is False
    again = ModelCapability(**json.loads(json.dumps(dumped)))
    assert again.text_input is True and again.image_input is False
    assert json.loads(json.dumps(again.model_dump(mode="json")))["text_input"] is True


def test_string_true_false_rejected():
    """字符串 "true"/"false" 拒绝。"""
    for bad in ("true", "false"):
        with pytest.raises(ValidationError):
            make_capability(text_input=bad)
        with pytest.raises(ValidationError):
            make_capability(system_message=bad)


def test_unknown_capability_not_auto_supported():
    """unknown 不自动变 supported；false 保持 false。"""
    cap = make_capability(image_input="unknown")
    assert cap.image_input == "unknown"
    cap2 = make_capability(text_input=False)
    assert cap2.text_input is False
    with pytest.raises(ValidationError):
        make_capability(text_input="yes")


# ---------------- capability_ref（fix-1） ---------------- #


def test_canonical_capability_ref_build_and_parse():
    """canonical capability_ref 构造/解析正常。"""
    ref = build_capability_ref("provider_demo_001", "model_demo_v1", "cap-demo-001")
    assert ref == "capability/provider_demo_001/model_demo_v1/cap-demo-001"
    assert parse_capability_ref(ref) == ("provider_demo_001", "model_demo_v1", "cap-demo-001")
    # Provider 构造通过（ref 三段与字段一致）
    assert make_provider().capability_ref == ref


def test_capability_ref_invalid_segments_rejected():
    """ref 空段、`..`、URL、反斜杠、多余层级拒绝。"""
    for bad in (
        "capability//model/cap",          # 空段
        "capability/p1/../cap",           # ..
        "capability/p1/m1",               # 层级不足
        "capability/p1/m1/cap/extra",     # 多余层级
        "capability/https://x/m1/cap",    # URL 段
        "capability/p1\\m1/cap",          # 反斜杠
        "capability/p1/m1/cap?v=1",       # 查询参数
        "capability/../p1/m1/cap",        # 前缀后 .. 段
        "other/p1/m1/cap",                # 错误前缀
    ):
        with pytest.raises(ValidationError):
            make_provider(capability_ref=bad)


def test_capability_ref_identity_mismatch_rejected():
    """ref 与 Provider identity 不一致拒绝（模型层）。"""
    with pytest.raises(ValidationError):
        make_provider(capability_ref=build_capability_ref("provider_demo_001", "model_demo_v1", "cap-OTHER"))
    with pytest.raises(ValidationError):
        make_provider(capability_ref=build_capability_ref("provider_other", "model_demo_v1", "cap-demo-001"))
    with pytest.raises(ValidationError):
        make_provider(capability_ref=build_capability_ref("provider_demo_001", "model_other", "cap-demo-001"))
