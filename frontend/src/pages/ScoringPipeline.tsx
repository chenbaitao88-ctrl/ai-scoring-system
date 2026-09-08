import { useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import {
  AddIcon,
  CheckCircleIcon,
  PlayCircleIcon,
  RefreshIcon,
  RollbackIcon,
  SearchIcon,
} from 'tdesign-icons-react';
import ReviewCaseListView from '../components/scoring-pipeline/ReviewCaseListView';
import ReviewCaseDetailPanel from '../components/scoring-pipeline/ReviewCaseDetailPanel';
import ReviewDecisionForm from '../components/scoring-pipeline/ReviewDecisionForm';
import ManualAdjustmentForm from '../components/scoring-pipeline/ManualAdjustmentForm';
import EvidenceReopenPanel from '../components/scoring-pipeline/EvidenceReopenPanel';
import {
  ApiRequestError,
  authoritativeExportApi,
  scoringPipelineApi,
} from '../services/api';
import type {
  CreateScoringPipelineTaskRequest,
  ReviewCaseDetail,
  ReviewCaseFilters,
  ReviewCaseSummary,
  ScoringPipelineBatchExecutionResult,
  ScoringPipelineDryRunResult,
  ScoringPipelineItemSummary,
  ScoringPipelineRecoveryResult,
  ScoringPipelineReviewApplicationResult,
  ScoringPipelineTaskSummary,
} from '../services/api';

type NoticeKind = 'success' | 'warning' | 'error' | 'info';

interface NoticeState {
  kind: NoticeKind;
  title: string;
  code?: string | null;
}

interface CreateFormState {
  sourceTaskId: string;
  sourceItemIds: string;
  batchId: string;
  profileVersion: string;
  providerModelRef: string;
  concurrency: string;
}

const EMPTY_CREATE_FORM: CreateFormState = {
  sourceTaskId: '',
  sourceItemIds: '',
  batchId: '',
  profileVersion: '',
  providerModelRef: '',
  concurrency: '1',
};

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SAFE_ID_PATTERN = /^[A-Za-z0-9_.-]+$/;

const STATUS_LABELS: Record<string, string> = {
  pending: '等待中',
  running: '运行中',
  paused: '已暂停',
  completed: '已完成',
  completed_with_errors: '含异常完成',
  failed: '失败',
  cancelled: '已取消',
  skipped: '已跳过',
  manual_review: '需人工复核',
};

const STAGE_LABELS: Record<string, string> = {
  import: '导入',
  validate: '验证',
  score: '评分',
  review: '复核',
  export: '导出',
};

const EVIDENCE_LABELS: Record<string, string> = {
  sufficient: '证据充分',
  limited: '证据有限',
  insufficient: '证据不足',
  manual_only: '仅人工处理',
};

const FRIENDLY_ERRORS: Record<string, string> = {
  SCORING_PIPELINE_RUNTIME_NOT_CONFIGURED: '评分运行环境未配置',
  SCORING_PIPELINE_TASK_NOT_FOUND: '未找到评分任务',
  SCORING_PIPELINE_ITEM_NOT_FOUND: '未找到任务项',
  SCORING_PIPELINE_REQUEST_INVALID: '请求参数不符合评分流水线契约',
  SCORING_PROFILE_NOT_FOUND: '尚未配置已批准的评分口径',
  SCORING_PROVIDER_BINDING_NOT_FOUND: '尚未配置已批准的模型绑定',
  SCORING_CONCURRENCY_EXCEEDS_FROZEN_LIMIT: '并发数超过任务冻结上限',
  RECOVERY_OUTCOME_UNKNOWN_BLOCKED: '评分结果状态不确定，必须人工核验后处理',
  REVIEW_CASE_NOT_FOUND: '未找到对应的复核决定',
  REVIEW_FACT_BINDING_MISMATCH: '复核决定与当前评分事实不匹配',
  REVIEW_CASE_CONFLICT: '复核工单状态或 revision 已变化',
  CASE_LOCKED: '结果已锁定，不能重新打开工单',
  SCORING_PIPELINE_INTERNAL_ERROR: '服务暂时不可用，请稍后重试',
};

function statusBadge(status: string): string {
  const base = 'inline-flex items-center px-2 py-0.5 rounded text-xs font-medium';
  if (status === 'completed') return `${base} bg-green-100 text-green-700`;
  if (status === 'running') return `${base} bg-blue-100 text-blue-700`;
  if (status === 'failed') return `${base} bg-red-100 text-red-700`;
  if (status === 'manual_review' || status === 'completed_with_errors') {
    return `${base} bg-orange-100 text-orange-700`;
  }
  if (status === 'paused') return `${base} bg-amber-100 text-amber-700`;
  return `${base} bg-gray-100 text-gray-700`;
}

function noticeClass(kind: NoticeKind): string {
  if (kind === 'success') return 'border-green-200 bg-green-50 text-green-800';
  if (kind === 'warning') return 'border-amber-200 bg-amber-50 text-amber-800';
  if (kind === 'error') return 'border-red-200 bg-red-50 text-red-800';
  return 'border-blue-200 bg-blue-50 text-blue-800';
}

function makeRequestId(prefix: string): string {
  return `${prefix}-${crypto.randomUUID()}`;
}

function parseItemIds(raw: string): string[] {
  return raw
    .split(/[\s,，]+/)
    .map((value) => value.trim())
    .filter(Boolean);
}

function validateSafeId(value: string, label: string): string | null {
  if (!value) return `请填写${label}`;
  if (!SAFE_ID_PATTERN.test(value)) return `${label}只能包含字母、数字、点、下划线和连字符`;
  return null;
}

function validateCreateForm(form: CreateFormState): string | null {
  if (!UUID_PATTERN.test(form.sourceTaskId.trim())) return '来源任务 ID 必须是小写 UUID';
  const itemIds = parseItemIds(form.sourceItemIds);
  if (itemIds.length === 0) return '请至少填写一个来源 item ID';
  if (itemIds.some((value) => !UUID_PATTERN.test(value))) return '来源 item ID 必须全部是小写 UUID';
  if (new Set(itemIds).size !== itemIds.length) return '来源 item ID 不能重复';
  for (const [value, label] of [
    [form.batchId.trim(), '批次 ID'],
    [form.profileVersion.trim(), 'Profile 版本'],
    [form.providerModelRef.trim(), 'Provider/model 配置引用'],
  ]) {
    const error = validateSafeId(value, label);
    if (error) return error;
  }
  const concurrency = Number(form.concurrency);
  if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 16) {
    return '并发数必须是 1 到 16 的整数';
  }
  return null;
}

function safeNotice(error: unknown): NoticeState {
  if (error instanceof ApiRequestError) {
    const title = error.code && FRIENDLY_ERRORS[error.code]
      ? FRIENDLY_ERRORS[error.code]
      : error.status === 503
        ? '评分运行环境未配置'
        : '操作未完成，请核对任务状态后重试';
    return { kind: error.status === 503 ? 'warning' : 'error', title, code: error.code };
  }
  return { kind: 'error', title: '操作未完成，请稍后重试', code: null };
}

function resultNotice(
  kind: NoticeKind,
  title: string,
  code?: string | null,
): NoticeState {
  return { kind, title, code };
}

export default function ScoringPipeline() {
  const [createForm, setCreateForm] = useState<CreateFormState>(EMPTY_CREATE_FORM);
  const [taskIdInput, setTaskIdInput] = useState('');
  const [task, setTask] = useState<ScoringPipelineTaskSummary | null>(null);
  const [items, setItems] = useState<ScoringPipelineItemSummary[]>([]);
  const [notice, setNotice] = useState<NoticeState | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [dryRunResult, setDryRunResult] = useState<ScoringPipelineDryRunResult | null>(null);
  const [recoveryResult, setRecoveryResult] = useState<ScoringPipelineRecoveryResult | null>(null);
  const [reviewResult, setReviewResult] = useState<ScoringPipelineReviewApplicationResult | null>(null);
  const [executionResult, setExecutionResult] = useState<ScoringPipelineBatchExecutionResult | null>(null);
  const [selectedItemId, setSelectedItemId] = useState<string | null>(null);
  const [decisionId, setDecisionId] = useState('');
  const [competitionId, setCompetitionId] = useState('');
  const [batchExecutionId, setBatchExecutionId] = useState('');
  const [executionConcurrency, setExecutionConcurrency] = useState('');

  // 11F-2b：复核队列
  const [activeView, setActiveView] = useState<'tasks' | 'reviews'>('tasks');
  const [reviewCases, setReviewCases] = useState<ReviewCaseSummary[]>([]);
  const [reviewTotal, setReviewTotal] = useState(0);
  const [reviewLoading, setReviewLoading] = useState(false);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const [reviewFilters, setReviewFilters] = useState<ReviewCaseFilters>({});
  const REVIEW_FILTER_DEFAULTS: ReviewCaseFilters = {};

  // 11F-2c：复核详情
  const [selectedCaseId, setSelectedCaseId] = useState<string | null>(null);
  const selectedCaseContext = useRef<string | null>(null);
  const selectionEpoch = useRef(0);
  const detailRequest = useRef(0);
  const loadedTaskId = useRef<string | null>(null);
  const [caseDetail, setCaseDetail] = useState<ReviewCaseDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  // 11F-2d：决策/采用/锁定
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [applying, setApplying] = useState(false);
  const [applyError, setApplyError] = useState<string | null>(null);
  const [locking, setLocking] = useState(false);
  const [lockError, setLockError] = useState<string | null>(null);
  const [resolving, setResolving] = useState(false);
  const resolutionBusy = useRef(false);
  const [resolveError, setResolveError] = useState<string | null>(null);
  const [resolveSuccess, setResolveSuccess] = useState<string | null>(null);
  const [decisionKeyCounter, setDecisionKeyCounter] = useState(0);
  const [adjustmentRequestId, setAdjustmentRequestId] = useState('');
  const [adjustmentSubmitting, setAdjustmentSubmitting] = useState(false);
  const [adjustmentError, setAdjustmentError] = useState<string | null>(null);
  const [adjustmentSuccess, setAdjustmentSuccess] = useState<string | null>(null);
  const [reopenSubmitting, setReopenSubmitting] = useState(false);
  const [reopenError, setReopenError] = useState<string | null>(null);
  const [reopenSuccess, setReopenSuccess] = useState<string | null>(null);

  const selectedItem = useMemo(
    () => items.find((item) => item.item_id === selectedItemId) ?? null,
    [items, selectedItemId],
  );

  const isBusy = busyAction !== null;

  const loadTask = async (
    taskId: string,
    options: { quiet?: boolean; preserveSelection?: boolean } = {},
  ) => {
    const normalized = taskId.trim();
    if (!normalized) {
      setNotice(resultNotice('error', '请填写任务 ID'));
      return;
    }
    if (!SAFE_ID_PATTERN.test(normalized)) {
      setNotice(resultNotice('error', '任务 ID 格式不正确'));
      return;
    }
    setBusyAction('load');
    if (loadedTaskId.current !== normalized) {
      handleCloseDetail();
      loadedTaskId.current = normalized;
    }
    try {
      const [taskResponse, itemResponse] = await Promise.all([
        scoringPipelineApi.getTask(normalized),
        scoringPipelineApi.listItems(normalized),
      ]);
      if (loadedTaskId.current !== normalized) return;
      setTask(taskResponse);
      setItems(itemResponse.items);
      setTaskIdInput(taskResponse.task_id);
      if (!options.preserveSelection) {
        setSelectedItemId(null);
        setDecisionId('');
      }
      if (!options.quiet) setNotice(resultNotice('success', '任务状态已刷新'));
    } catch (error) {
      setNotice(safeNotice(error));
    } finally {
      setBusyAction(null);
    }
  };

  // 11F-2b：复核队列加载
  const loadReviewCases = async (filters: ReviewCaseFilters = reviewFilters) => {
    if (!task) return;
    setReviewLoading(true);
    setReviewError(null);
    try {
      const response = await scoringPipelineApi.listReviewCases(task.task_id, filters);
      if (loadedTaskId.current !== task.task_id) return;
      setReviewCases(response.items);
      setReviewTotal(response.total);
    } catch (error) {
      setReviewError(safeNotice(error).title);
      setReviewCases([]);
      setReviewTotal(0);
    } finally {
      setReviewLoading(false);
    }
  };

  const handleReviewSearch = () => {
    if (!task) return;
    loadReviewCases(reviewFilters);
  };

  const handleReviewReset = () => {
    setReviewFilters({ ...REVIEW_FILTER_DEFAULTS });
    if (task) {
      loadReviewCases({ ...REVIEW_FILTER_DEFAULTS });
    }
  };

  const handleReviewRetry = () => {
    if (!task) return;
    loadReviewCases(reviewFilters);
  };

  // 11F-2c：选中工单加载详情
  const handleSelectCase = async (caseId: string | null) => {
    const context = caseId && task ? `${task.task_id}/${caseId}` : null;
    if (selectedCaseContext.current !== context) selectionEpoch.current += 1;
    selectedCaseContext.current = context;
    const request = ++detailRequest.current;
    if (caseId !== selectedCaseId) {
      setResolveError(null);
      setResolveSuccess(null);
      setAdjustmentRequestId(caseId ? `adjust-${caseId}-${Date.now()}` : '');
      setAdjustmentError(null);
      setAdjustmentSuccess(null);
      setReopenError(null);
      setReopenSuccess(null);
    }
    setSelectedCaseId(caseId);
    if (caseId === null || !task) {
      setCaseDetail(null);
      setDetailError(null);
      return;
    }
    setDetailLoading(true);
    setDetailError(null);
    setCaseDetail(null);
    try {
      const detail = await scoringPipelineApi.getReviewCaseDetail(task.task_id, caseId);
      if (request !== detailRequest.current || context !== selectedCaseContext.current) return;
      setCaseDetail(detail);
    } catch (error) {
      if (request === detailRequest.current) setDetailError(safeNotice(error).title);
    } finally {
      if (request === detailRequest.current) setDetailLoading(false);
    }
  };

  const handleCloseDetail = () => {
    selectedCaseContext.current = null;
    selectionEpoch.current += 1;
    detailRequest.current += 1;
    setDetailLoading(false);
    setResolveError(null);
    setResolveSuccess(null);
    setSelectedCaseId(null);
    setCaseDetail(null);
    setDetailError(null);
    setCreateError(null);
    setApplyError(null);
    setLockError(null);
    setAdjustmentRequestId('');
    setAdjustmentError(null);
    setAdjustmentSuccess(null);
    setReopenError(null);
    setReopenSuccess(null);
  };

  const getAdjustmentRequestId = () => {
    if (adjustmentRequestId) return adjustmentRequestId;
    const nextId = `adjust-${caseDetail?.case.review_case_id ?? 'case'}-${Date.now()}`;
    setAdjustmentRequestId(nextId);
    return nextId;
  };

  const handleManualAdjustment = async (changes: Array<{
    dimension_code: string;
    before_value: number;
    after_value: number;
    change_reason_code: string;
  }>, reasonCodes: string[]) => {
    if (!task || !caseDetail || caseDetail.manual_adjustment_context.allowed !== true) {
      setAdjustmentError(caseDetail?.manual_adjustment_context.block_reason_code ?? '当前不可调整');
      return;
    }
    const requestId = getAdjustmentRequestId();
    setAdjustmentSubmitting(true);
    setAdjustmentError(null);
    setAdjustmentSuccess(null);
    try {
      const decision = await scoringPipelineApi.createReviewDecision(
        task.task_id,
        caseDetail.case.item_id,
        {
          idempotency_key: `adjust-decision-${requestId}`,
          review_case_id: caseDetail.case.review_case_id,
          decision_type: 'apply_manual_adjustment',
          reason_codes: reasonCodes,
          target_attempt_id: caseDetail.manual_adjustment_context.attempt_id ?? undefined,
          target_snapshot_id: caseDetail.manual_adjustment_context.snapshot_id ?? undefined,
          decision_note_code: 'MANUAL_SCORE_CORRECTION',
        },
      );
      await scoringPipelineApi.adjustResult(
        task.task_id,
        caseDetail.case.item_id,
        decision.decision_id,
        {
          adjustment_request_id: requestId,
          review_case_id: caseDetail.case.review_case_id,
          changes,
          reason_codes: reasonCodes,
          adjustment_note_code: 'MANUAL_SCORE_CORRECTION',
        },
      );
      setAdjustmentSuccess('人工调整已提交');
      await handleSelectCase(selectedCaseId);
      await loadReviewCases(reviewFilters);
      await loadTask(task.task_id, { quiet: true, preserveSelection: true });
    } catch (error) {
      setAdjustmentError(safeNotice(error).title);
    } finally {
      setAdjustmentSubmitting(false);
    }
  };

  const handleEvidenceReopen = async (params: {
    packageRevision: number;
    requestDecisionId: string;
  }) => {
    if (!task || !caseDetail) return;
    setReopenSubmitting(true);
    setReopenError(null);
    setReopenSuccess(null);
    try {
      await scoringPipelineApi.reopenReviewCase(
        task.task_id,
        caseDetail.case.item_id,
        caseDetail.case.review_case_id,
        {
          expected_revision: caseDetail.case.current_revision,
          package_revision: params.packageRevision,
          request_decision_id: params.requestDecisionId,
        },
      );
      setReopenSuccess('工单已重新打开');
      await handleSelectCase(selectedCaseId);
      await loadReviewCases(reviewFilters);
      await loadTask(task.task_id, { quiet: true, preserveSelection: true });
    } catch (error) {
      setReopenError(safeNotice(error).title);
    } finally {
      setReopenSubmitting(false);
    }
  };

  // 11F-2d：创建决策
  const handleCreateDecision = async (params: {
    decisionType: string;
    reasonCodes: string[];
    targetAttemptId?: string;
    targetSnapshotId?: string;
    requestedPackageRevision?: number;
  }) => {
    if (!task || !caseDetail) return;
    setCreating(true);
    setCreateError(null);
    const key = `idem-${caseDetail.case.review_case_id}-${decisionKeyCounter}`;
    try {
      await scoringPipelineApi.createReviewDecision(
        task.task_id,
        caseDetail.case.item_id,
        {
          idempotency_key: key,
          review_case_id: caseDetail.case.review_case_id,
          decision_type: params.decisionType,
          reason_codes: params.reasonCodes,
          target_attempt_id: params.targetAttemptId,
          target_snapshot_id: params.targetSnapshotId,
          requested_package_revision: params.requestedPackageRevision,
          decision_note_code: params.decisionType.toUpperCase(),
        },
      );
      setDecisionKeyCounter(c => c + 1);
      // 刷新详情
      await handleSelectCase(selectedCaseId);
      // 刷新复核队列
      loadReviewCases(reviewFilters);
    } catch (error) {
      setCreateError(safeNotice(error).title);
    } finally {
      setCreating(false);
    }
  };

  // 11F-2d：采用结果
  const handleApplyAdoption = async () => {
    if (!task || !caseDetail) return;
    const dec = caseDetail.decisions.length > 0
      ? caseDetail.decisions[caseDetail.decisions.length - 1] : null;
    if (!dec || dec.decision_type !== 'adopt_existing_attempt') {
      setApplyError('请先创建"采用已有评分"类型的复核决定');
      return;
    }
    if (!competitionId) {
      setApplyError('请填写赛事 ID（competition_id）');
      return;
    }
    setApplying(true);
    setApplyError(null);
    try {
      await scoringPipelineApi.applyReviewDecision(
        task.task_id,
        caseDetail.case.item_id,
        dec.decision_id,
        `apply-${Date.now()}`,
        competitionId,
      );
      await handleSelectCase(selectedCaseId);
      loadReviewCases(reviewFilters);
      loadTask(task.task_id, { quiet: true, preserveSelection: true });
    } catch (error) {
      setApplyError(safeNotice(error).title);
    } finally {
      setApplying(false);
    }
  };

  // 11F-2d：锁定结果
  const handleLockResult = async () => {
    if (!task || !caseDetail || !caseDetail.active_adoption) return;
    const dec = caseDetail.decisions.length > 0
      ? caseDetail.decisions[caseDetail.decisions.length - 1] : null;
    if (!dec) {
      setLockError('未找到复核决定');
      return;
    }
    setLocking(true);
    setLockError(null);
    try {
      await scoringPipelineApi.lockFinalResult(
        task.task_id,
        caseDetail.case.item_id,
        dec.decision_id,
        {
          lock_request_id: `lreq-${Date.now()}`,
          review_case_id: caseDetail.case.review_case_id,
          adoption_id: caseDetail.active_adoption.adoption_id,
          reason_code: 'HUMAN_FINAL_LOCKED',
        },
      );
      await handleSelectCase(selectedCaseId);
      loadReviewCases(reviewFilters);
    } catch (error) {
      setLockError(safeNotice(error).title);
    } finally {
      setLocking(false);
    }
  };

  const handleResolveCase = async () => {
    if (!task || !caseDetail?.active_adoption || resolutionBusy.current) return;
    resolutionBusy.current = true;
    setResolving(true);
    setResolveError(null);
    setResolveSuccess(null);
    const { item_id: itemId, review_case_id: caseId, current_revision: revision } = caseDetail.case;
    const context = `${task.task_id}/${caseId}`;
    const epoch = selectionEpoch.current;
    const isCurrent = () => selectedCaseContext.current === context && selectionEpoch.current === epoch;
    try {
      await scoringPipelineApi.resolveReviewCase(task.task_id, itemId, caseId, {
        expected_revision: revision,
        expected_adoption_id: caseDetail.active_adoption.adoption_id,
      });
      if (!isCurrent()) return;
      setResolveSuccess('工单已结案，正在刷新导出资格。');
      try {
        const [, , preview] = await Promise.all([
          handleSelectCase(caseId), loadReviewCases(reviewFilters),
          authoritativeExportApi.results(task.task_id),
        ]);
        if (!isCurrent()) return;
        const result = preview.items.find(item => item.item_id === itemId);
        setResolveSuccess(result?.exportable
          ? '工单已结案，此条目可到结果页导出。'
          : '工单已结案，此条目仍有导出阻断，请到结果页查看原因。');
      } catch {
        if (isCurrent()) setResolveSuccess('工单已结案；导出资格刷新失败，请到结果页刷新确认。');
      }
    } catch (error) {
      if (!isCurrent()) return;
      const code = error instanceof ApiRequestError ? error.code : null;
      const messages: Record<string, string> = {
        REVIEW_CASE_CONFLICT: '工单状态或版本已变化，请刷新工单后确认。',
        REVIEW_RESOLUTION_ADOPTION_CHANGED: '采用结果已变化，请刷新并重新核对。',
        REVIEW_RESOLUTION_ADOPTION_REQUIRED: '尚无有效采用结果，不能结案。',
        REVIEW_RESOLUTION_BINDING_MISMATCH: '工单、决定与已采用或锁定结果不一致，不能结案。',
        REVIEW_RESOLUTION_DECISION_STALE: '采用所依据的复核决定已过期，请重新复核。',
        REVIEW_RESOLUTION_SCORE_FACT_INVALID: '评分记录或校验结果缺失、损坏或不一致，不能结案。',
        REVIEW_DECISION_NOT_FOUND: '采用所依据的复核决定不存在，不能结案。',
      };
      setResolveError((code && messages[code]) || safeNotice(error).title);
    } finally {
      resolutionBusy.current = false;
      setResolving(false);
    }
  };

  const handleCreate = async () => {
    const validationError = validateCreateForm(createForm);
    if (validationError) {
      setNotice(resultNotice('error', validationError));
      return;
    }
    const request: CreateScoringPipelineTaskRequest = {
      source_task_id: createForm.sourceTaskId.trim(),
      source_item_ids: parseItemIds(createForm.sourceItemIds),
      batch_id: createForm.batchId.trim(),
      profile_version: createForm.profileVersion.trim(),
      provider_model_ref: createForm.providerModelRef.trim(),
      concurrency: Number(createForm.concurrency),
    };
    setBusyAction('create');
    try {
      const created = await scoringPipelineApi.createTask(request);
      const itemResponse = await scoringPipelineApi.listItems(created.task_id);
      setTask(created);
      loadedTaskId.current = created.task_id;
      handleCloseDetail();
      setItems(itemResponse.items);
      setTaskIdInput(created.task_id);
      setSelectedItemId(null);
      setDecisionId('');
      setNotice(resultNotice(
        'success',
        created.idempotent_hit ? '已返回同一幂等任务' : '评分任务已创建',
      ));
    } catch (error) {
      setNotice(safeNotice(error));
    } finally {
      setBusyAction(null);
    }
  };

  const handleDryRun = async (item: ScoringPipelineItemSummary) => {
    if (!task) return;
    setBusyAction(`dry-${item.item_id}`);
    setDryRunResult(null);
    try {
      const result = await scoringPipelineApi.dryRun(
        task.task_id,
        item.item_id,
        makeRequestId('dry'),
      );
      setDryRunResult(result);
      setSelectedItemId(item.item_id);
      setNotice(resultNotice(
        result.decision === 'ready' ? 'success' : 'warning',
        result.decision === 'ready' ? 'dry-run 检查通过' : 'dry-run 已阻断',
        result.blocking_error_code,
      ));
    } catch (error) {
      setNotice(safeNotice(error));
    } finally {
      setBusyAction(null);
    }
  };

  const handleRecover = async (item: ScoringPipelineItemSummary) => {
    if (!task || item.latest_error_code?.includes('OUTCOME_UNKNOWN')) return;
    setBusyAction(`recover-${item.item_id}`);
    setRecoveryResult(null);
    try {
      const result = await scoringPipelineApi.recover(
        task.task_id,
        item.item_id,
        makeRequestId('recover'),
      );
      setRecoveryResult(result);
      setSelectedItemId(item.item_id);
      const blocked = result.outcome === 'outcome_unknown_blocked' || result.outcome === 'retry_approval_required';
      setNotice(resultNotice(
        blocked ? 'warning' : 'success',
        blocked ? '恢复检查要求人工处理' : '恢复检查已完成',
        result.error_code,
      ));
      await loadTask(task.task_id, { quiet: true, preserveSelection: true });
    } catch (error) {
      setNotice(safeNotice(error));
    } finally {
      setBusyAction(null);
    }
  };

  const selectReviewItem = (item: ScoringPipelineItemSummary) => {
    setSelectedItemId(item.item_id);
    setDecisionId(item.review_decision_id ?? '');
    setReviewResult(null);
  };

  const handleApplyReview = async () => {
    if (!task || !selectedItem) return;
    const decisionError = validateSafeId(decisionId.trim(), '复核决定 ID');
    const competitionError = validateSafeId(competitionId.trim(), '赛事 ID');
    if (decisionError || competitionError) {
      setNotice(resultNotice('error', decisionError || competitionError || '输入不完整'));
      return;
    }
    setBusyAction(`review-${selectedItem.item_id}`);
    setReviewResult(null);
    try {
      const result = await scoringPipelineApi.applyReviewDecision(
        task.task_id,
        selectedItem.item_id,
        decisionId.trim(),
        makeRequestId('adopt'),
        competitionId.trim(),
      );
      setReviewResult(result);
      const adopted = result.outcome === 'adopted' || result.outcome === 'already_completed';
      setNotice(resultNotice(
        adopted ? 'success' : 'info',
        adopted ? '复核决定已应用' : '复核决定未推进采用',
        result.error_code,
      ));
      await loadTask(task.task_id, { quiet: true, preserveSelection: true });
    } catch (error) {
      setNotice(safeNotice(error));
    } finally {
      setBusyAction(null);
    }
  };

  const handleExecute = async () => {
    if (!task) {
      setNotice(resultNotice('error', '请先查询或创建任务'));
      return;
    }
    const idError = validateSafeId(batchExecutionId.trim(), '批量执行 ID');
    if (idError) {
      setNotice(resultNotice('error', idError));
      return;
    }
    let concurrency: number | undefined;
    if (executionConcurrency.trim()) {
      concurrency = Number(executionConcurrency);
      if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > task.concurrency) {
        setNotice(resultNotice('error', `执行并发必须是 1 到 ${task.concurrency} 的整数`));
        return;
      }
    }
    setBusyAction('execute');
    setExecutionResult(null);
    try {
      const result = await scoringPipelineApi.execute(
        task.task_id,
        batchExecutionId.trim(),
        concurrency,
      );
      setExecutionResult(result);
      setNotice(resultNotice(
        result.outcome === 'completed' ? 'success' : 'warning',
        result.outcome === 'completed' ? '批量评分已完成' : '批量评分已返回安全摘要',
      ));
      await loadTask(task.task_id, { quiet: true, preserveSelection: true });
    } catch (error) {
      setNotice(safeNotice(error));
    } finally {
      setBusyAction(null);
    }
  };

  return (
    <div className="space-y-6" data-testid="scoring-pipeline-page">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-bold text-teal-700" data-testid="scoring-pipeline-title">评分流水线</h2>
          <p className="mt-1 text-sm text-gray-500">生产评分任务控制台</p>
        </div>
        {task && (
          <div className="text-right text-xs text-gray-500">
            <div>任务 revision {task.revision}</div>
            <div>冻结并发 {task.concurrency}</div>
          </div>
        )}
      </div>

      {notice && (
        <div
          className={`flex flex-wrap items-center justify-between gap-2 rounded border px-4 py-3 text-sm ${noticeClass(notice.kind)}`}
          data-testid="scoring-pipeline-notice"
          role="status"
        >
          <span>{notice.title}</span>
          {notice.code && <code className="font-mono text-xs">{notice.code}</code>}
        </div>
      )}

      <section
        className="rounded-lg border border-gray-200 bg-white p-5 shadow-sm"
        data-testid="scoring-pipeline-create-section"
      >
        <div className="mb-4 flex items-center gap-2">
          <AddIcon size="18px" className="text-teal-600" />
          <h3 className="text-base font-semibold text-gray-800">创建评分任务</h3>
        </div>
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
          <Field label="来源任务 ID" className="lg:col-span-2">
            <input
              data-testid="scoring-pipeline-source-task-id"
              className="w-full rounded border border-gray-300 px-3 py-2 text-sm font-mono focus:border-teal-500 focus:outline-none focus:ring-2 focus:ring-teal-100"
              value={createForm.sourceTaskId}
              onChange={(event) => setCreateForm({ ...createForm, sourceTaskId: event.target.value })}
              placeholder="00000000-0000-0000-0000-000000000000"
            />
          </Field>
          <Field label="并发数">
            <input
              type="number"
              min={1}
              max={16}
              className="w-full rounded border border-gray-300 px-3 py-2 text-sm focus:border-teal-500 focus:outline-none focus:ring-2 focus:ring-teal-100"
              value={createForm.concurrency}
              onChange={(event) => setCreateForm({ ...createForm, concurrency: event.target.value })}
            />
          </Field>
          <Field label="来源 item IDs" className="lg:col-span-3">
            <textarea
              data-testid="scoring-pipeline-source-item-ids"
              className="min-h-24 w-full resize-y rounded border border-gray-300 px-3 py-2 text-sm font-mono focus:border-teal-500 focus:outline-none focus:ring-2 focus:ring-teal-100"
              value={createForm.sourceItemIds}
              onChange={(event) => setCreateForm({ ...createForm, sourceItemIds: event.target.value })}
              placeholder="每行或逗号分隔一个小写 UUID"
            />
          </Field>
          <Field label="批次 ID">
            <input
              className="w-full rounded border border-gray-300 px-3 py-2 text-sm focus:border-teal-500 focus:outline-none focus:ring-2 focus:ring-teal-100"
              value={createForm.batchId}
              onChange={(event) => setCreateForm({ ...createForm, batchId: event.target.value })}
            />
          </Field>
          <Field label="Profile 版本">
            <input
              className="w-full rounded border border-gray-300 px-3 py-2 text-sm focus:border-teal-500 focus:outline-none focus:ring-2 focus:ring-teal-100"
              value={createForm.profileVersion}
              onChange={(event) => setCreateForm({ ...createForm, profileVersion: event.target.value })}
            />
          </Field>
          <Field label="Provider/model 配置引用">
            <input
              className="w-full rounded border border-gray-300 px-3 py-2 text-sm focus:border-teal-500 focus:outline-none focus:ring-2 focus:ring-teal-100"
              value={createForm.providerModelRef}
              onChange={(event) => setCreateForm({ ...createForm, providerModelRef: event.target.value })}
            />
          </Field>
        </div>
        <div className="mt-4 flex justify-end">
          <IconButton
            testId="scoring-pipeline-create-button"
            icon={<AddIcon size="16px" />}
            label={busyAction === 'create' ? '创建中' : '创建任务'}
            title="创建评分任务"
            onClick={handleCreate}
            disabled={isBusy}
            primary
          />
        </div>
      </section>

      <section
        className="rounded-lg border border-gray-200 bg-white p-5 shadow-sm"
        data-testid="scoring-pipeline-task-query"
      >
        <div className="mb-4 flex items-center gap-2">
          <SearchIcon size="18px" className="text-teal-600" />
          <h3 className="text-base font-semibold text-gray-800">任务查询</h3>
        </div>
        <div className="flex flex-col gap-3 sm:flex-row">
          <input
            className="min-w-0 flex-1 rounded border border-gray-300 px-3 py-2 text-sm font-mono focus:border-teal-500 focus:outline-none focus:ring-2 focus:ring-teal-100"
            value={taskIdInput}
            onChange={(event) => setTaskIdInput(event.target.value)}
            placeholder="输入评分任务 ID"
            data-testid="scoring-pipeline-task-id"
          />
          <IconButton
            testId="scoring-pipeline-load-button"
            icon={<SearchIcon size="16px" />}
            label={busyAction === 'load' ? '查询中' : '查询'}
            title="查询任务"
            onClick={() => loadTask(taskIdInput)}
            disabled={isBusy}
            primary
          />
          <IconButton
            testId="scoring-pipeline-refresh-button"
            icon={<RefreshIcon size="16px" />}
            label="刷新"
            title="刷新当前任务"
            onClick={() => task && loadTask(task.task_id)}
            disabled={isBusy || !task}
          />
        </div>
      </section>

      <ExecutionSection
        task={task}
        batchExecutionId={batchExecutionId}
        concurrency={executionConcurrency}
        busy={busyAction === 'execute'}
        globallyBusy={isBusy}
        result={executionResult}
        onBatchIdChange={setBatchExecutionId}
        onConcurrencyChange={setExecutionConcurrency}
        onExecute={handleExecute}
      />

      {/* 11F-2b：选项卡切换（始终展示，无 task 时复核队列显示空态） */}
      <div className="flex gap-0 rounded-lg border border-gray-200 bg-white shadow-sm" role="tablist">
        <button
          data-testid="scoring-pipeline-view-tasks"
            role="tab"
            aria-selected={activeView === 'tasks'}
            className={`px-5 py-2.5 text-sm font-medium transition ${
              activeView === 'tasks'
                ? 'border-b-2 border-teal-600 text-teal-700'
                : 'text-gray-500 hover:text-gray-700'
            }`}
            onClick={() => setActiveView('tasks')}
          >
            任务控制
          </button>
          <button
            data-testid="scoring-pipeline-view-reviews"
            role="tab"
            aria-selected={activeView === 'reviews'}
            className={`px-5 py-2.5 text-sm font-medium transition ${
              activeView === 'reviews'
                ? 'border-b-2 border-teal-600 text-teal-700'
                : 'text-gray-500 hover:text-gray-700'
            }`}
            onClick={() => {
              setActiveView('reviews');
              if (reviewCases.length === 0 && !reviewLoading && !reviewError) {
                loadReviewCases(reviewFilters);
              }
            }}
          >
            复核队列
          </button>
        </div>

      {task && activeView === 'tasks' ? (
        <>
          <TaskSummary task={task} />
          <ItemTable
            items={items}
            busyAction={busyAction}
            onDryRun={handleDryRun}
            onRecover={handleRecover}
            onSelectReview={selectReviewItem}
          />
          {selectedItem && (
            <ItemOperationResult
              item={selectedItem}
              dryRun={dryRunResult?.item_id === selectedItem.item_id ? dryRunResult : null}
              recovery={recoveryResult?.item_id === selectedItem.item_id ? recoveryResult : null}
              review={reviewResult?.item_id === selectedItem.item_id ? reviewResult : null}
              decisionId={decisionId}
              competitionId={competitionId}
              busy={busyAction === `review-${selectedItem.item_id}`}
              globallyBusy={isBusy}
              onDecisionIdChange={setDecisionId}
              onCompetitionIdChange={setCompetitionId}
              onApply={handleApplyReview}
            />
          )}
        </>
      ) : activeView === 'reviews' ? (
        <>
          <ReviewCaseListView
            hasTask={!!task}
            cases={reviewCases}
            total={reviewTotal}
            loading={reviewLoading}
            error={reviewError}
            filters={reviewFilters}
            onFiltersChange={setReviewFilters}
            onSearch={handleReviewSearch}
            onReset={handleReviewReset}
            onRetry={handleReviewRetry}
            selectedCaseId={selectedCaseId}
            onSelectCase={handleSelectCase}
          />
          {(selectedCaseId || caseDetail || detailLoading || detailError) && (
            <>
              <ReviewCaseDetailPanel
                detail={caseDetail}
                loading={detailLoading}
                error={detailError}
                onClose={handleCloseDetail}
                onRetry={() => selectedCaseId && handleSelectCase(selectedCaseId)}
              />
              {caseDetail && !detailLoading && !detailError && (
                <>
                  <ReviewDecisionForm
                    detail={caseDetail}
                    creating={creating}
                    createError={createError}
                    applying={applying}
                    applyError={applyError}
                    locking={locking}
                    lockError={lockError}
                    resolving={resolving}
                    resolveError={resolveError}
                    resolveSuccess={resolveSuccess}
                    onResolveCase={handleResolveCase}
                    hasActiveAdoption={!!caseDetail.active_adoption}
                    hasActiveLock={!!caseDetail.active_lock}
                    onCreateDecision={handleCreateDecision}
                    onApplyAdoption={handleApplyAdoption}
                    onLockResult={handleLockResult}
                  />
                  <EvidenceReopenPanel
                    key={`${caseDetail.case.review_case_id}-${caseDetail.case.current_revision}`}
                    detail={caseDetail}
                    submitting={reopenSubmitting}
                    error={reopenError}
                    success={reopenSuccess}
                    onReopen={handleEvidenceReopen}
                  />
                  <ManualAdjustmentForm
                    context={caseDetail.manual_adjustment_context}
                    requestId={adjustmentRequestId}
                    submitting={adjustmentSubmitting}
                    error={adjustmentError}
                    success={adjustmentSuccess}
                    onSubmit={handleManualAdjustment}
                    onCancel={() => {
                      setAdjustmentRequestId('');
                      setAdjustmentError(null);
                      setAdjustmentSuccess(null);
                    }}
                  />
                </>
              )}
            </>
          )}
        </>
      ) : (
        <div className="rounded-lg border border-dashed border-gray-300 bg-white px-6 py-10 text-center text-sm text-gray-500" data-testid="scoring-pipeline-empty-state">
          尚未选择评分任务
        </div>
      )}
    </div>
  );
}

function Field({ label, className = '', children }: { label: string; className?: string; children: ReactNode }) {
  return (
    <label className={`block ${className}`}>
      <span className="mb-1 block text-sm font-medium text-gray-700">{label}</span>
      {children}
    </label>
  );
}

function IconButton({
  testId,
  icon,
  label,
  title,
  onClick,
  disabled,
  primary = false,
}: {
  testId?: string;
  icon: ReactNode;
  label: string;
  title: string;
  onClick: () => void;
  disabled?: boolean;
  primary?: boolean;
}) {
  return (
    <button
      type="button"
      data-testid={testId}
      title={title}
      className={`inline-flex min-h-9 items-center justify-center gap-1.5 rounded px-3 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50 ${
        primary
          ? 'bg-teal-600 text-white hover:bg-teal-700'
          : 'border border-gray-300 bg-white text-gray-700 hover:bg-gray-50'
      }`}
      onClick={onClick}
      disabled={disabled}
    >
      {icon}
      <span>{label}</span>
    </button>
  );
}

function TaskSummary({ task }: { task: ScoringPipelineTaskSummary }) {
  const counts = [
    ['总数', task.counts.total],
    ['等待', task.counts.pending],
    ['运行', task.counts.running],
    ['完成', task.counts.completed],
    ['失败', task.counts.failed],
    ['跳过', task.counts.skipped],
    ['人工复核', task.counts.manual_review],
  ];
  return (
    <section className="space-y-3" data-testid="scoring-pipeline-task-summary">
      <div className="flex flex-wrap items-center gap-3">
        <h3 className="text-base font-semibold text-gray-800">任务摘要</h3>
        <span className={statusBadge(task.status)}>{STATUS_LABELS[task.status] ?? task.status}</span>
        <span className="text-sm text-gray-500">阶段：{task.current_stage ? STAGE_LABELS[task.current_stage] ?? task.current_stage : '-'}</span>
        <code className="ml-auto max-w-full break-all text-xs text-gray-500">{task.task_id}</code>
      </div>
      <div className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-gray-200 bg-gray-200 sm:grid-cols-4 lg:grid-cols-7">
        {counts.map(([label, value]) => (
          <div key={label} className="bg-white px-3 py-3">
            <div className="text-xs text-gray-500">{label}</div>
            <div className="mt-1 text-lg font-semibold text-gray-800">{value}</div>
          </div>
        ))}
      </div>
    </section>
  );
}

function ExecutionSection({
  task,
  batchExecutionId,
  concurrency,
  busy,
  globallyBusy,
  result,
  onBatchIdChange,
  onConcurrencyChange,
  onExecute,
}: {
  task: ScoringPipelineTaskSummary | null;
  batchExecutionId: string;
  concurrency: string;
  busy: boolean;
  globallyBusy: boolean;
  result: ScoringPipelineBatchExecutionResult | null;
  onBatchIdChange: (value: string) => void;
  onConcurrencyChange: (value: string) => void;
  onExecute: () => void;
}) {
  return (
    <section
      className="rounded-lg border border-gray-200 bg-white p-5 shadow-sm"
      data-testid="scoring-pipeline-execute-section"
    >
      <div className="mb-4 flex items-center gap-2">
        <PlayCircleIcon size="18px" className="text-teal-600" />
        <h3 className="text-base font-semibold text-gray-800">批量执行</h3>
      </div>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-[minmax(0,1fr)_160px_auto]">
        <Field label="批量执行 ID">
          <input
            data-testid="scoring-pipeline-batch-execution-id"
            className="w-full rounded border border-gray-300 px-3 py-2 text-sm focus:border-teal-500 focus:outline-none focus:ring-2 focus:ring-teal-100"
            value={batchExecutionId}
            onChange={(event) => onBatchIdChange(event.target.value)}
          />
        </Field>
        <Field label={`并发（上限 ${task?.concurrency ?? '-'}）`}>
          <input
            type="number"
            min={1}
            max={task?.concurrency}
            className="w-full rounded border border-gray-300 px-3 py-2 text-sm focus:border-teal-500 focus:outline-none focus:ring-2 focus:ring-teal-100"
            value={concurrency}
            onChange={(event) => onConcurrencyChange(event.target.value)}
            placeholder="使用冻结值"
          />
        </Field>
        <div className="flex items-end">
          <IconButton
            testId="scoring-pipeline-execute-button"
            icon={<PlayCircleIcon size="16px" />}
            label={busy ? '执行中' : '启动批量评分'}
            title="启动批量评分"
            onClick={onExecute}
            disabled={globallyBusy || !task}
            primary
          />
        </div>
      </div>
      {result && (
        <div className="mt-4 grid grid-cols-2 gap-2 rounded border border-gray-200 bg-gray-50 p-3 text-sm sm:grid-cols-4 lg:grid-cols-7" data-testid="scoring-pipeline-execution-result">
          <span>结果：{result.outcome}</span>
          <span>启动：{result.started}</span>
          <span>成功：{result.succeeded}</span>
          <span>跳过：{result.skipped}</span>
          <span>阻断：{result.blocked}</span>
          <span>待恢复：{result.recovery_required}</span>
          <span>仍有待办：{result.pending_remaining ? '是' : '否'}</span>
        </div>
      )}
    </section>
  );
}

function ItemTable({
  items,
  busyAction,
  onDryRun,
  onRecover,
  onSelectReview,
}: {
  items: ScoringPipelineItemSummary[];
  busyAction: string | null;
  onDryRun: (item: ScoringPipelineItemSummary) => void;
  onRecover: (item: ScoringPipelineItemSummary) => void;
  onSelectReview: (item: ScoringPipelineItemSummary) => void;
}) {
  return (
    <section data-testid="scoring-pipeline-items-section">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h3 className="text-base font-semibold text-gray-800">任务项</h3>
        <span className="text-sm text-gray-500">{items.length} 项</span>
      </div>
      <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white shadow-sm">
        <table className="min-w-[1120px] w-full table-fixed divide-y divide-gray-200 text-sm" data-testid="scoring-pipeline-item-table">
          <colgroup>
            <col className="w-40" />
            <col className="w-40" />
            <col className="w-24" />
            <col className="w-24" />
            <col className="w-24" />
            <col className="w-20" />
            <col className="w-32" />
            <col className="w-40" />
            <col className="w-64" />
          </colgroup>
          <thead className="bg-gray-50">
            <tr>
              {['item ID', '来源 item', '状态', '阶段', '证据等级', 'revision', '最近错误码', '复核决定', '操作'].map((label) => (
                <th key={label} className="px-3 py-2 text-left text-xs font-semibold text-gray-600">{label}</th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {items.map((item) => {
              const outcomeUnknown = Boolean(item.latest_error_code?.includes('OUTCOME_UNKNOWN'));
              const canDryRun = item.status === 'pending' && item.current_stage === 'score';
              const canRecover = !outcomeUnknown && (
                item.status === 'running' || (item.status === 'failed' && item.retryable)
              );
              const canReview = item.current_stage === 'review';
              return (
                <tr key={item.item_id} className="hover:bg-gray-50" data-testid="scoring-pipeline-item-row">
                  <td className="truncate px-3 py-2 font-mono text-xs text-gray-700" title={item.item_id}>{item.item_id}</td>
                  <td className="truncate px-3 py-2 font-mono text-xs text-gray-600" title={item.source_item_id}>{item.source_item_id}</td>
                  <td className="px-3 py-2"><span className={statusBadge(item.status)}>{STATUS_LABELS[item.status] ?? item.status}</span></td>
                  <td className="px-3 py-2 text-gray-700">{STAGE_LABELS[item.current_stage] ?? item.current_stage}</td>
                  <td className="px-3 py-2 text-gray-700">{EVIDENCE_LABELS[item.evidence_level] ?? item.evidence_level}</td>
                  <td className="px-3 py-2 text-gray-700">{item.item_revision}</td>
                  <td className="break-all px-3 py-2 font-mono text-xs text-red-700">{item.latest_error_code ?? '-'}</td>
                  <td className="truncate px-3 py-2 font-mono text-xs text-gray-600" title={item.review_decision_id ?? undefined}>{item.review_decision_id ?? '-'}</td>
                  <td className="px-3 py-2">
                    <div className="flex flex-wrap gap-1.5">
                      <IconButton
                        icon={<CheckCircleIcon size="15px" />}
                        label={busyAction === `dry-${item.item_id}` ? '检查中' : 'dry-run'}
                        title="执行只读 dry-run"
                        onClick={() => onDryRun(item)}
                        disabled={busyAction !== null || !canDryRun}
                      />
                      <IconButton
                        icon={<RollbackIcon size="15px" />}
                        label={outcomeUnknown ? '需人工处理' : busyAction === `recover-${item.item_id}` ? '检查中' : '恢复'}
                        title={outcomeUnknown ? '结果状态不确定，禁止自动恢复' : '执行不调用 Provider 的恢复检查'}
                        onClick={() => onRecover(item)}
                        disabled={busyAction !== null || !canRecover}
                      />
                      <IconButton
                        icon={<CheckCircleIcon size="15px" />}
                        label="复核决定"
                        title="选择已有复核决定"
                        onClick={() => onSelectReview(item)}
                        disabled={busyAction !== null || !canReview}
                      />
                    </div>
                  </td>
                </tr>
              );
            })}
            {items.length === 0 && (
              <tr>
                <td colSpan={9} className="px-4 py-8 text-center text-gray-500">当前任务暂无 item</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function ItemOperationResult({
  item,
  dryRun,
  recovery,
  review,
  decisionId,
  competitionId,
  busy,
  globallyBusy,
  onDecisionIdChange,
  onCompetitionIdChange,
  onApply,
}: {
  item: ScoringPipelineItemSummary;
  dryRun: ScoringPipelineDryRunResult | null;
  recovery: ScoringPipelineRecoveryResult | null;
  review: ScoringPipelineReviewApplicationResult | null;
  decisionId: string;
  competitionId: string;
  busy: boolean;
  globallyBusy: boolean;
  onDecisionIdChange: (value: string) => void;
  onCompetitionIdChange: (value: string) => void;
  onApply: () => void;
}) {
  return (
    <section className="rounded-lg border border-gray-200 bg-white p-5 shadow-sm" data-testid="scoring-pipeline-item-operation">
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <h3 className="text-base font-semibold text-gray-800">单项处理</h3>
        <code className="break-all text-xs text-gray-500">{item.item_id}</code>
      </div>

      {(dryRun || recovery || review) && (
        <div className="mb-4 grid grid-cols-1 gap-2 md:grid-cols-3">
          {dryRun && (
            <div className="rounded border border-gray-200 bg-gray-50 p-3 text-sm" data-testid="scoring-pipeline-dry-run-result">
              <div className="font-medium text-gray-700">dry-run：{dryRun.decision}</div>
              <div className="mt-1 text-xs text-gray-500">检查 {dryRun.checks.length} 项</div>
              {dryRun.blocking_error_code && <code className="mt-1 block break-all text-xs text-red-700">{dryRun.blocking_error_code}</code>}
            </div>
          )}
          {recovery && (
            <div className="rounded border border-gray-200 bg-gray-50 p-3 text-sm" data-testid="scoring-pipeline-recovery-result">
              <div className="font-medium text-gray-700">恢复：{recovery.outcome}</div>
              {recovery.error_code && <code className="mt-1 block break-all text-xs text-red-700">{recovery.error_code}</code>}
            </div>
          )}
          {review && (
            <div className="rounded border border-gray-200 bg-gray-50 p-3 text-sm" data-testid="scoring-pipeline-review-result">
              <div className="font-medium text-gray-700">复核：{review.outcome}</div>
              {review.error_code && <code className="mt-1 block break-all text-xs text-red-700">{review.error_code}</code>}
            </div>
          )}
        </div>
      )}

      <div className="grid grid-cols-1 gap-3 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]">
        <Field label="已有复核决定 ID">
          <input
            className="w-full rounded border border-gray-300 px-3 py-2 text-sm font-mono focus:border-teal-500 focus:outline-none focus:ring-2 focus:ring-teal-100"
            value={decisionId}
            onChange={(event) => onDecisionIdChange(event.target.value)}
          />
        </Field>
        <Field label="赛事 ID">
          <input
            className="w-full rounded border border-gray-300 px-3 py-2 text-sm focus:border-teal-500 focus:outline-none focus:ring-2 focus:ring-teal-100"
            value={competitionId}
            onChange={(event) => onCompetitionIdChange(event.target.value)}
          />
        </Field>
        <div className="flex items-end">
          <IconButton
            testId="scoring-pipeline-apply-review-button"
            icon={<CheckCircleIcon size="16px" />}
            label={busy ? '应用中' : '应用既有决定'}
            title="应用已存在的复核决定"
            onClick={onApply}
            disabled={globallyBusy || item.current_stage !== 'review'}
            primary
          />
        </div>
      </div>
    </section>
  );
}
