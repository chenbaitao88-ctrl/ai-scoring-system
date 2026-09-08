"""Static contract guards for the Phase 11E-4b frontend.

The frontend has no Vitest/RTL dependency. These checks keep the HTTP payload
boundary and read-only smoke contract explicit without adding a test runtime.
"""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "src"
PAGE = FRONTEND / "pages" / "ScoringPipeline.tsx"
API = FRONTEND / "services" / "api.ts"
APP = FRONTEND / "App.tsx"
LAYOUT = FRONTEND / "components" / "Layout.tsx"
SMOKE = ROOT / "scripts" / "e2e_smoke_playwright.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _interface_fields(source: str, name: str) -> set[str]:
    match = re.search(rf"export interface {name} \{{(?P<body>.*?)\n\}}", source, re.S)
    assert match, f"missing interface {name}"
    return set(re.findall(r"^\s{2}([A-Za-z_][A-Za-z0-9_]*)(?:\?)?:", match.group("body"), re.M))


def test_create_request_exposes_only_authorized_fields():
    source = _read(API)
    assert _interface_fields(source, "CreateScoringPipelineTaskRequest") == {
        "source_task_id",
        "source_item_ids",
        "batch_id",
        "profile_version",
        "provider_model_ref",
        "concurrency",
    }
    page = _read(PAGE)
    request = re.search(
        r"const request: CreateScoringPipelineTaskRequest = \{(?P<body>.*?)\n\s*\};",
        page,
        re.S,
    )
    assert request
    request_body = request.group("body")
    for forbidden in (
        "prompt:",
        "rubric:",
        "schema:",
        "score:",
        "attempt_id:",
        "provider_id:",
        "model_id:",
    ):
        assert forbidden not in request_body


def test_safe_error_and_item_fields_are_rendered_without_sensitive_details():
    page = _read(PAGE)
    assert "评分运行环境未配置" in page
    assert "item.latest_error_code" in page
    for forbidden in (
        "latest_error_message",
        "evidence_text",
        "prompt_text",
        "model_response",
        "absolute_path",
    ):
        assert forbidden not in page


def test_page_has_explicit_actions_without_polling_or_automatic_execution():
    page = _read(PAGE)
    assert "useEffect" not in page
    assert "setInterval" not in page
    assert "scoringPipelineApi.execute(" in page
    assert "onClick={onExecute}" in page
    assert "disabled={globallyBusy || !task}" in page
    assert "OUTCOME_UNKNOWN" in page
    assert "结果状态不确定，禁止自动恢复" in page


def test_route_navigation_and_readonly_smoke_entry_are_present():
    assert '<Route path="/scoring-pipeline" element={<ScoringPipeline />} />' in _read(APP)
    assert "{ value: '/scoring-pipeline', label: '评分流水线' }" in _read(LAYOUT)

    smoke = _read(SMOKE)
    entry = re.search(
        r'\{\s*"name": "评分流水线",\s*"path": "/scoring-pipeline",(?P<body>.*?)\n\s*\},',
        smoke,
        re.S,
    )
    assert entry
    body = entry.group("body")
    assert "scoring-pipeline-create-section" in body
    assert "scoring-pipeline-task-query" in body
    assert "scoring-pipeline-execute-section" in body
    # 11F-2b：复核队列交互标记允许出现
    assert '"interaction": "review_queue"' in body

    # 11F-2b：复核队列 testid 存在于页面源码
    page_source = _read(PAGE)
    assert "scoring-pipeline-view-tasks" in page_source
    assert "scoring-pipeline-view-reviews" in page_source


def test_scoring_pipeline_api_calls_send_only_contract_bodies():
    source = _read(API)
    assert "{ request_id: requestId }" in source
    assert "{ recovery_request_id: recoveryRequestId }" in source
    assert "{ adoption_request_id: adoptionRequestId, competition_id: competitionId }" in source
    assert "const body: { batch_execution_id: string; concurrency?: number }" in source
    assert "localhost" not in source

    # 11F-2b：复核队列 API 类型
    assert "interface ReviewCaseSummary" in source
    assert "interface ReviewCaseListResponse" in source
    assert "interface ReviewCaseFilters" in source
    assert "listReviewCases" in source
    # 不发送学生信息或证据正文
    assert "student_name" not in source
    assert "evidence_body" not in source
    assert "evidence_text" not in source

    # 11F-2c：复核详情 API 类型
    assert "interface ReviewCaseDetail" in source
    assert "interface ReviewCaseDetailAttempt" in source
    assert "interface ReviewCaseDetailDecision" in source
    assert "getReviewCaseDetail" in source
    # 11F-3c prerequisite：人工调分上下文白名单必须与后端一致
    assert _interface_fields(source, "ManualAdjustmentDimensionContext") == {
        "dimension_code", "current_score", "min_score", "max_score",
    }
    assert _interface_fields(source, "ManualAdjustmentContext") == {
        "allowed", "block_reason_code", "attempt_id", "snapshot_id",
        "current_total_score", "dimensions",
    }
    assert "manual_adjustment_context: ManualAdjustmentContext;" in source
    for field in (
        "package_revision: number | null;",
        "current_revision: number;",
        "reopen_count: number;",
    ):
        assert field in source
    assert "requested_package_revision: number | null;" in source
    for forbidden in (
        "evidence_refs", "rationale_summary", "provider_response",
        "absolute_path", "file_path", "student_name",
    ):
        assert forbidden not in source
    # 不得暴露 actor_id 或个人信息
    assert "actor_id" not in source
    assert "student_name" not in source

    # 11F-2c：详情面板组件存在
    page_source = _read(PAGE)
    assert "ReviewCaseDetailPanel" in page_source
    assert "selectedCaseId" in page_source
    assert "handleSelectCase" in page_source

    # 11F-2d：决策表单与锁定 API
    assert "createReviewDecision" in source
    assert "lockFinalResult" in source
    assert "ReviewDecisionForm" in page_source
    assert "handleCreateDecision" in page_source
    assert "handleApplyAdoption" in page_source
    assert "handleLockResult" in page_source

    # 11F-3c：人工调分表单与 decision -> adjust 编排
    component = _read(FRONTEND / "components" / "scoring-pipeline" / "ManualAdjustmentForm.tsx")
    assert "ManualAdjustmentForm" in component
    assert "manual-adjustment-form" in component
    assert "manual-adjustment-submit" in component
    assert "manual-adjustment-cancel" in component
    assert "manual-adjustment-total-preview" in component
    assert "manual-adjustment-error" in component
    assert "manual-adjustment-success" in component
    assert "onSubmit" in component
    assert "scoringPipelineApi" not in component
    assert "before_value: dimension.current_score" in component
    assert "min={dimension.min_score}" in component
    assert "max={dimension.max_score}" in component
    assert "changes.length === 0" in component
    assert "reasonCodes.length === 0" in component
    assert "adjustResult" in source
    assert "ManualAdjustmentChangeRequest" in source
    assert "ManualAdjustmentRequest" in source
    assert "ManualAdjustmentResponse" in source
    assert "adjust-decision-${requestId}" in page_source
    assert "decision.decision_id" in page_source
    assert "handleManualAdjustment" in page_source
    assert "await loadReviewCases(reviewFilters)" in page_source
    assert "await loadTask(task.task_id" in page_source
    assert "caseDetail.manual_adjustment_context.allowed !== true" in page_source
    for forbidden in ("base_snapshot_id:", "adjusted_snapshot_id:", "total_score:", "actor:", "evidence_refs:"):
        assert forbidden not in component

    # 11F-3d：补证返回后手动 reopen。组件只负责展示/交互，不直接调用 API。
    reopen_component = _read(FRONTEND / "components" / "scoring-pipeline" / "EvidenceReopenPanel.tsx")
    assert "EvidenceReopenPanel" in reopen_component
    assert "evidence-reopen-panel" in reopen_component
    assert "evidence-reopen-package-revision" in reopen_component
    assert "evidence-reopen-submit" in reopen_component
    assert "evidence-reopen-error" in reopen_component
    assert "evidence-reopen-success" in reopen_component
    assert "onReopen" in reopen_component
    assert "scoringPipelineApi" not in reopen_component
    assert "detail.case.status !== 'waiting_for_evidence'" in reopen_component
    assert "request_additional_evidence" in reopen_component
    assert _interface_fields(source, "ReviewCaseReopenRequest") == {
        "expected_revision", "package_revision", "request_decision_id",
    }
    assert _interface_fields(source, "ReviewCaseReopenResponse") == {
        "review_case_id", "status", "package_revision", "current_revision",
        "reopen_count", "outcome", "idempotent",
    }
    assert "reopenReviewCase" in source
    assert "/review-cases/${encodeURIComponent(caseId)}/reopen" in source
    assert "handleEvidenceReopen" in page_source
    assert "EvidenceReopenPanel" in page_source
    assert "expected_revision: caseDetail.case.current_revision" in page_source
    assert "request_decision_id: params.requestDecisionId" in page_source
