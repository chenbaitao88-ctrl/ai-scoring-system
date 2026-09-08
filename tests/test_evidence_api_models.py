"""
Phase 11B-2a：EvidencePackage 导入 API 请求/响应模型合成测试。

覆盖（对应指令九的 13 项）：
1. 三类合法请求。
2. 绝对路径、`..`、反斜杠、盘符、空字节被拒绝。
3. 批次 0 项和超过 1000 项被拒绝。
4. 批次重复 package_ref 被拒绝。
5. expected revision 的 None/0/正整数合法，负数（及布尔）非法。
6. 响应未知字段拒绝。
7. 时间必须带时区。
8. NaN/Infinity 拒绝。
9. 批次计数不变量。
10. rejected 登记不得伪造 record/entry。
11. Error 模型不允许 traceback、绝对路径和原始异常字段。
12. 安全响应模型不包含 actual_summary 等内部字段。
13. 路径清单和 HTTP 映射完整（含契约文档 JSON 逐块解析）。
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from models.evidence_api import (
    EvidenceApiError,
    EvidenceBatchDryRunRequest,
    EvidenceBatchDryRunResponse,
    EvidenceBatchItemResponse,
    EvidenceDryRunRequest,
    EvidenceIssueResponse,
    EvidenceRegisterRequest,
    EvidenceRegistrationDetailResponse,
    EvidenceRegistrationListResponse,
    EvidenceRegistrationResponse,
    EvidenceRegistrationSummary,
    EvidenceValidationResponse,
)

UTC = timezone.utc
T0 = datetime(2026, 8, 6, 7, 0, 0, tzinfo=UTC)

# 便携仓库内随代码发布的契约副本，测试不依赖原工作区目录结构。
CONTRACT_DOC = Path(__file__).resolve().parents[1] / "docs" / "contracts" / "Phase11B-2a-Evidence导入API契约.md"


def v_response(**overrides):
    data = {
        "validation_id": "validation-demo-0001",
        "package_id": "pkg-demo-001",
        "package_revision": 1,
        "mode": "registration",
        "status": "passed",
        "evidence_level": "sufficient",
        "registration_allowed": True,
        "model_input_allowed": True,
        "issue_counts": {"info": 0, "warning": 0, "error": 0, "fatal": 0},
        "issues": [],
        "started_at": T0,
        "completed_at": T0,
    }
    data.update(overrides)
    return EvidenceValidationResponse(**data)


# ---------------------------------------------------------------- 1. 合法请求


def test_three_valid_requests():
    r1 = EvidenceDryRunRequest(package_ref="pkg-a/revisions/1")
    assert r1.package_ref == "pkg-a/revisions/1"
    r2 = EvidenceBatchDryRunRequest(package_refs=["pkg-a/1", "pkg-b/2"], fail_fast=True)
    assert r2.package_refs == ["pkg-a/1", "pkg-b/2"]
    assert r2.fail_fast is True
    r2b = EvidenceBatchDryRunRequest(package_refs=["pkg-a/1"])
    assert r2b.fail_fast is False  # 默认 false
    r3 = EvidenceRegisterRequest(package_ref="pkg-a", expected_index_revision=3)
    assert r3.expected_index_revision == 3


# ---------------------------------------------------------------- 2. 路径安全


@pytest.mark.parametrize(
    "bad",
    ["/abs/path", "C:/x", "c:\\x", "a\\b", "a\x00b", "..", "../evil", "a/../b", "a//b", "", "a b", "."],
)
def test_path_ref_rejected(bad):
    with pytest.raises(ValidationError):
        EvidenceDryRunRequest(package_ref=bad)
    with pytest.raises(ValidationError):
        EvidenceRegisterRequest(package_ref=bad)


# ---------------------------------------------------------------- 3/4. 批次边界


def test_batch_empty_rejected():
    with pytest.raises(ValidationError):
        EvidenceBatchDryRunRequest(package_refs=[])


def test_batch_over_1000_rejected():
    with pytest.raises(ValidationError):
        EvidenceBatchDryRunRequest(package_refs=["p" + str(i) for i in range(1001)])


def test_batch_duplicate_rejected():
    with pytest.raises(ValidationError):
        EvidenceBatchDryRunRequest(package_refs=["pkg-a", "pkg-b", "pkg-a"])


def test_batch_1000_valid():
    req = EvidenceBatchDryRunRequest(package_refs=["p" + str(i) for i in range(1000)])
    assert len(req.package_refs) == 1000


# ---------------------------------------------------------------- 5. expected revision


@pytest.mark.parametrize("rev", [None, 0, 1, 42])
def test_expected_revision_valid(rev):
    assert EvidenceRegisterRequest(package_ref="pkg-a", expected_index_revision=rev).expected_index_revision == rev


@pytest.mark.parametrize("rev", [-1, -100, True, False])
def test_expected_revision_invalid(rev):
    with pytest.raises(ValidationError):
        EvidenceRegisterRequest(package_ref="pkg-a", expected_index_revision=rev)


# ---------------------------------------------------------------- 6/7/8. 模型纪律


def test_response_unknown_field_rejected():
    with pytest.raises(ValidationError):
        EvidenceApiError(code="x", message_key="y", retryable=False, traceback="tb")
    with pytest.raises(ValidationError):
        EvidenceValidationResponse.model_validate(
            v_response().model_dump(mode="json") | {"actual_summary": "secret"}
        )


def test_time_must_be_aware():
    with pytest.raises(ValidationError):
        EvidenceValidationResponse(
            validation_id="v1", package_id="pkg-a", package_revision=1, mode="registration",
            status="passed", registration_allowed=True, model_input_allowed=True,
            started_at=datetime(2026, 8, 6, 7, 0, 0),  # naive
            completed_at=T0,
        )
    with pytest.raises(ValidationError):
        EvidenceValidationResponse(
            validation_id="v1", package_id="pkg-a", package_revision=1, mode="registration",
            status="passed", registration_allowed=True, model_input_allowed=True,
            started_at=T0, completed_at=T0 - __import__("datetime").timedelta(days=1),
        )


def test_nan_infinity_rejected():
    bad = {"total": math.nan, "passed": 1, "passed_with_warnings": 0, "failed": 0, "internal_error": 0, "items": []}
    with pytest.raises(ValidationError):
        EvidenceBatchDryRunResponse(**bad)
    bad2 = dict(bad)
    bad2["total"] = math.inf
    with pytest.raises(ValidationError):
        EvidenceBatchDryRunResponse(**bad2)


# ---------------------------------------------------------------- 9. 批次计数不变量


def test_batch_counts_invariant():
    with pytest.raises(ValidationError):
        EvidenceBatchDryRunResponse(
            total=2, passed=1, passed_with_warnings=0, failed=0, internal_error=0, items=[]
        )
    with pytest.raises(ValidationError):
        EvidenceBatchDryRunResponse(
            total=1, passed=1, passed_with_warnings=0, failed=0, internal_error=0, items=[]
        )
    # 合法：1 passed + 1 internal_error = total 2
    item_ok = EvidenceBatchItemResponse(package_ref="pkg-a", result=v_response())
    item_err = EvidenceBatchItemResponse(package_ref="pkg-b", error=EvidenceApiError(code="X", message_key="Y", retryable=False))
    resp = EvidenceBatchDryRunResponse(
        total=2, passed=1, passed_with_warnings=0, failed=0, internal_error=1, items=[item_ok, item_err],
    )
    assert resp.total == 2


def test_batch_item_exactly_one_of_result_error():
    with pytest.raises(ValidationError):
        EvidenceBatchItemResponse(package_ref="pkg-a")  # 两者皆无
    with pytest.raises(ValidationError):
        EvidenceBatchItemResponse(
            package_ref="pkg-a",
            result=v_response(),
            error=EvidenceApiError(code="X", message_key="Y", retryable=False),
        )


# ---------------------------------------------------------------- 10. rejected 不伪造


def test_rejected_registration_requires_validation():
    """rejected 响应必须携带结构化 validation（拒绝理由），但不得伪造登记事实。"""
    ok = EvidenceRegistrationResponse(
        idempotent_hit=False, rejected=True, package_id="pkg-a", package_revision=1, validation=v_response(),
    )
    assert ok.validation is not None
    assert ok.record_id is None and ok.entry_id is None and ok.index_revision is None
    # 缺 validation 的 rejected 响应被拒绝
    with pytest.raises(ValidationError):
        EvidenceRegistrationResponse(idempotent_hit=False, rejected=True, package_id="pkg-a", package_revision=1)


def test_rejected_registration_rejects_fake_ids():
    with pytest.raises(ValidationError):
        EvidenceRegistrationResponse(
            idempotent_hit=False, rejected=True, record_id="record-1",
            package_id="pkg-a", package_revision=1, validation=v_response(),
        )
    with pytest.raises(ValidationError):
        EvidenceRegistrationResponse(
            idempotent_hit=False, rejected=True, entry_id="entry-1",
            package_id="pkg-a", package_revision=1, validation=v_response(),
        )
    with pytest.raises(ValidationError):
        EvidenceRegistrationResponse(
            idempotent_hit=False, rejected=True, index_revision=1,
            package_id="pkg-a", package_revision=1, validation=v_response(),
        )


def test_rejected_plus_idempotent_hit_rejected():
    with pytest.raises(ValidationError):
        EvidenceRegistrationResponse(
            idempotent_hit=True, rejected=True, package_id="pkg-a", package_revision=1, validation=v_response(),
        )


def test_success_registration_requires_all_ids():
    """非 rejected 响应缺任一必要字段（validation/record_id/entry_id/index_revision>=1）均被拒绝。"""
    with pytest.raises(ValidationError):
        EvidenceRegistrationResponse(
            idempotent_hit=False, rejected=False, record_id="record-1", entry_id="entry-1",
            package_id="pkg-a", package_revision=1, index_revision=2,  # 缺 validation
        )
    with pytest.raises(ValidationError):
        EvidenceRegistrationResponse(
            idempotent_hit=False, rejected=False, entry_id="entry-1",
            package_id="pkg-a", package_revision=1, validation=v_response(), index_revision=2,  # 缺 record_id
        )
    with pytest.raises(ValidationError):
        EvidenceRegistrationResponse(
            idempotent_hit=False, rejected=False, record_id="record-1",
            package_id="pkg-a", package_revision=1, validation=v_response(), index_revision=2,  # 缺 entry_id
        )
    with pytest.raises(ValidationError):
        EvidenceRegistrationResponse(
            idempotent_hit=False, rejected=False, record_id="record-1", entry_id="entry-1",
            package_id="pkg-a", package_revision=1, validation=v_response(),  # 缺 index_revision
        )
    with pytest.raises(ValidationError):
        EvidenceRegistrationResponse(
            idempotent_hit=False, rejected=False, record_id="record-1", entry_id="entry-1",
            package_id="pkg-a", package_revision=1, validation=v_response(), index_revision=0,  # index_revision < 1
        )


def test_success_and_idempotent_valid_samples():
    """成功与幂等响应的合法样例（不允许成功但所有 ID 为空）。"""
    val = v_response()
    ok = EvidenceRegistrationResponse(
        idempotent_hit=False, rejected=False, record_id="record-1", entry_id="entry-1",
        package_id="pkg-a", package_revision=1, validation=val, index_revision=1,
    )
    assert ok.record_id == "record-1" and ok.index_revision == 1
    idem = EvidenceRegistrationResponse(
        idempotent_hit=True, rejected=False, record_id="record-1", entry_id="entry-1",
        package_id="pkg-a", package_revision=1, validation=val, index_revision=2,
    )
    assert idem.idempotent_hit is True and idem.rejected is False
    # 不允许成功但所有 ID 为空
    with pytest.raises(ValidationError):
        EvidenceRegistrationResponse(
            idempotent_hit=False, rejected=False, package_id="pkg-a", package_revision=1, validation=val,
        )


# ---------------------------------------------------------------- 11/12. 错误与安全字段


def test_error_model_no_internal_fields():
    err = EvidenceApiError(code="SIDECAR_INDEX_CORRUPTED", message_key="SIDECAR_INDEX_CORRUPTED", retryable=False)
    dumped = json.dumps(err.model_dump(mode="json"))
    for forbidden in ("traceback", "exception", "absolute_path", "raw_response", "http_status"):
        assert forbidden not in dumped
    # 模型字段白名单
    assert set(err.model_dump().keys()) == {"code", "message_key", "retryable"}


def test_validation_response_no_internal_fields():
    resp = v_response()
    dumped = json.dumps(resp.model_dump(mode="json"))
    for forbidden in ("actual_summary", "expected_summary", "manifest_sha256", "result_sha256",
                      "validator_name", "validator_version", "privacy_policy_version",
                      "duration_ms", "checks", "validated_file_count", "record_id", "file_ref"):
        assert forbidden not in dumped, f"响应泄漏内部字段: {forbidden}"


def test_issue_response_whitelist():
    issue = EvidenceIssueResponse(
        issue_id="issue-1", code="SYNTHETIC_WARNING", severity="warning", message_key="SYNTHETIC_WARNING",
    )
    assert set(issue.model_dump().keys()) == {"issue_id", "code", "severity", "message_key"}


def test_registration_summary_safe_metadata():
    s = EvidenceRegistrationSummary(
        package_id="pkg-a", package_revision=1, batch_id="b1", submission_id="s1",
        registration_status="registered", latest_validation_status="passed",
        model_input_allowed=True, manifest_sha256="a" * 64,
    )
    assert set(s.model_dump().keys()) == {
        "package_id", "package_revision", "batch_id", "submission_id",
        "registration_status", "latest_validation_status", "model_input_allowed", "manifest_sha256",
    }


def test_list_response_total_matches():
    with pytest.raises(ValidationError):
        EvidenceRegistrationListResponse(total=1, items=[])
    s = EvidenceRegistrationSummary(
        package_id="pkg-a", package_revision=1, batch_id="b1", submission_id="s1",
        registration_status="registered", latest_validation_status="passed",
        model_input_allowed=True, manifest_sha256="a" * 64,
    )
    ok = EvidenceRegistrationListResponse(total=1, items=[s])
    assert ok.total == 1


# ---------------------------------------------------------------- 13. 路径清单与 HTTP 映射


EXPECTED_PATHS = [
    "POST /api/evidence-packages/dry-run",
    "POST /api/evidence-packages/dry-run-batch",
    "POST /api/evidence-packages/register",
    "GET /api/evidence-packages/registrations",
    "GET /api/evidence-packages/registrations/{package_id}/revisions/{package_revision}",
    "GET /api/evidence-packages/registrations/{package_id}/revisions/{package_revision}/validations/{validation_id}",
    "GET /api/evidence-packages/registrations/{package_id}/revisions/{package_revision}/validations/{validation_id}/issues",
]

EXPECTED_HTTP_MAPPINGS = [
    ("200", "dry-run 完成"),
    ("200", "批次 dry-run 完成"),
    ("200", "查询成功"),
    ("200", "幂等登记命中"),
    ("201", "首次正式登记成功"),
    ("201", "创建新 ValidationResult 并更新登记成功"),
    ("404", "get_registration 返回 None"),
    ("404", "指定 ValidationResult 不存在"),
    ("409", "revision conflict"),
    ("409", "idempotency conflict"),
    ("409", "content conflict"),
    ("409", "lock conflict"),
    ("422", "请求模型不合法"),
    ("422", "包未 ready"),
    ("422", "隐私门失败"),
    ("422", "registration not allowed"),
    ("422", "不支持的 EvidencePackage 版本"),
    ("500", "Sidecar 损坏"),
    ("500", "validator internal error"),
]


def _extract_json_blocks(doc_text: str):
    blocks = []
    in_block = False
    buf = []
    for line in doc_text.splitlines():
        if line.strip().startswith("```json"):
            in_block = True
            buf = []
            continue
        if in_block and line.strip().startswith("```"):
            blocks.append("\n".join(buf))
            in_block = False
            continue
        if in_block:
            buf.append(line)
    return blocks


@pytest.fixture(scope="module")
def contract_doc():
    assert CONTRACT_DOC.is_file(), f"契约文档不存在: {CONTRACT_DOC}"
    return CONTRACT_DOC.read_text(encoding="utf-8")


def test_contract_doc_json_blocks_parse(contract_doc):
    blocks = _extract_json_blocks(contract_doc)
    assert len(blocks) >= 4, "契约文档至少应包含 4 个 JSON 示例块"
    for i, block in enumerate(blocks):
        parsed = json.loads(block)
        assert isinstance(parsed, (dict, list)), f"JSON 块 {i} 必须为对象或数组"


def test_contract_doc_paths_complete(contract_doc):
    for path in EXPECTED_PATHS:
        method, api_path = path.split(" ", 1)
        assert method in contract_doc, f"缺少 HTTP method: {method}"
        assert api_path in contract_doc, f"缺少路径: {api_path}"


def test_contract_doc_http_mapping_complete(contract_doc):
    for code, desc in EXPECTED_HTTP_MAPPINGS:
        assert code in contract_doc, f"HTTP 映射缺少状态码 {code}"
        # 描述关键词应出现（允许简写）
        keyword = desc.split(" ")[0]
        assert keyword in contract_doc, f"HTTP 映射缺少关键词: {desc}"


def test_contract_doc_none_to_404(contract_doc):
    assert "404" in contract_doc
    assert "None" in contract_doc


def test_contract_doc_no_sensitive_content(contract_doc):
    """JSON 示例块不得含 traceback/exception/absolute_path/raw_response 字段（正文说明"不返回"不在此列）。"""
    for i, block in enumerate(_extract_json_blocks(contract_doc)):
        for forbidden in ("traceback", "exception", "absolute_path", "raw_response"):
            assert forbidden not in block, f"JSON 示例块 {i} 含禁止内容: {forbidden}"


# ---------------------------------------------------------------- 11B-2a-fix-1：响应不变量


def test_validation_response_issues_are_safe_issue_models():
    """ValidationResponse.issues 必须是 EvidenceIssueResponse（含 message_key），非内部 ValidationIssueRef。"""
    from models.evidence_api import EvidenceIssueResponse as _EIR

    resp = v_response(
        issues=[
            EvidenceIssueResponse(
                issue_id="issue-1", code="SYNTHETIC_WARNING", severity="warning",
                message_key="SYNTHETIC_WARNING",
            )
        ]
    )
    assert all(isinstance(i, _EIR) for i in resp.issues)
    assert resp.issues[0].message_key == "SYNTHETIC_WARNING"
    dumped = json.dumps(resp.model_dump(mode="json"))
    assert '"message_key"' in dumped
    # 内部 ValidationIssueRef 没有 message_key 字段，不能直接充当完整 API Issue
    ref_dump = {"issue_id": "issue-1", "code": "SYNTHETIC_WARNING", "severity": "warning"}
    with pytest.raises(ValidationError):
        EvidenceValidationResponse(
            validation_id="v1", package_id="pkg-a", package_revision=1, mode="registration",
            status="passed", registration_allowed=True, model_input_allowed=True,
            started_at=T0, completed_at=T0, issues=[ref_dump],  # 缺 message_key -> 拒绝
        )


def test_expected_revision_strict_types():
    """expected_index_revision 只接受 null/0/正整数；拒绝 bool、float、字符串、负数。"""
    for rev in [True, False, 1.0, 0.0, "1", -1, -5]:
        with pytest.raises(ValidationError):
            EvidenceRegisterRequest(package_ref="pkg-a", expected_index_revision=rev), f"应拒绝: {rev!r}"
    for rev in [None, 0, 1, 42]:
        assert EvidenceRegisterRequest(package_ref="pkg-a", expected_index_revision=rev).expected_index_revision == rev


def test_contract_doc_rejected_422_response_body(contract_doc):
    """文档明确：登记被验证门拒绝时 HTTP 422，响应体为 EvidenceRegistrationResponse（rejected=true + validation）。"""
    assert "422" in contract_doc
    assert "EvidenceRegistrationResponse" in contract_doc
    assert "rejected=true" in contract_doc or "rejected = true" in contract_doc
    assert "EvidenceApiError" in contract_doc


def test_contract_doc_evidence_level_null_semantics(contract_doc):
    """文档明确 evidence_level 的 null 语义（Sidecar 未持久化时为 null，router 不得猜测）。"""
    assert "evidence_level" in contract_doc
    assert "null" in contract_doc
    assert "猜测" in contract_doc or "不得由 router" in contract_doc
