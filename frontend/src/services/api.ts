/**
 * 后端API服务封装
 */
import axios from 'axios';

interface ApiErrorEnvelopePayload {
  error?: {
    code?: string;
    message_key?: string;
    retryable?: boolean;
    request_id?: string;
  };
  detail?: unknown;
}

export class ApiRequestError extends Error {
  readonly code: string | null;
  readonly messageKey: string | null;
  readonly retryable: boolean;
  readonly requestId: string | null;
  readonly status: number | null;

  constructor(
    message: string,
    metadata: {
      code?: string;
      messageKey?: string;
      retryable?: boolean;
      requestId?: string;
      status?: number;
    } = {},
  ) {
    super(message);
    this.name = 'ApiRequestError';
    this.code = metadata.code ?? null;
    this.messageKey = metadata.messageKey ?? null;
    this.retryable = metadata.retryable ?? false;
    this.requestId = metadata.requestId ?? null;
    this.status = metadata.status ?? null;
  }
}

const api = axios.create({
  baseURL: '/api',
  timeout: 120000,  // 120秒超时，给AI评分足够时间
  headers: {
    'Content-Type': 'application/json',
  },
});

// 响应拦截器
api.interceptors.response.use(
  (response) => response.data,
  (error) => {
    const payload = (error.response?.data ?? {}) as ApiErrorEnvelopePayload;
    const envelope = payload.error;
    const detail = typeof payload.detail === 'string' ? payload.detail : null;
    const message = detail || envelope?.message_key || error.message || '请求失败';
    console.error('API Error:', message);
    return Promise.reject(new ApiRequestError(message, {
      code: envelope?.code,
      messageKey: envelope?.message_key,
      retryable: envelope?.retryable,
      requestId: envelope?.request_id,
      status: error.response?.status,
    }));
  }
);

// ============ 队伍管理 ============
export const teamApi = {
  /** 获取队伍列表 */
  getTeams: (params?: { group_type?: string; judge_group?: number; search?: string }) =>
    api.get('/teams', { params }),

  /** 获取队伍统计 */
  getStats: () => api.get('/teams/stats'),

  /** 获取队伍详情 */
  getTeam: (id: number) => api.get(`/teams/${id}`),

  /** 从Excel导入队伍 */
  importFromExcel: (file: File) => {
    const formData = new FormData();
    formData.append('file', file);
    return api.post('/teams/import', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
  },

  /** 随机分配评审组 */
  assignGroups: (numGroups: number = 4) =>
    api.post('/teams/assign-groups', null, { params: { num_groups: numGroups } }),

  /** 删除队伍 */
  deleteTeam: (id: number) => api.delete(`/teams/${id}`),
};

// ============ 作品管理 ============
export const workApi = {
  /** 上传单个作品 */
  uploadWork: (file: File) => {
    const formData = new FormData();
    formData.append('file', file);
    return api.post('/works/upload', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
  },

  /** 批量上传作品 */
  batchUploadWorks: (files: File[]) => {
    const formData = new FormData();
    files.forEach(f => formData.append('files', f));
    return api.post('/works/batch-upload', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
  },

  /** 获取作品状态统计 */
  getWorksStatus: () => api.get('/works/status'),

  /** 获取队伍作品详情 */
  getTeamWork: (teamId: number) => api.get(`/works/team/${teamId}`),

  /** 上传文件夹（单个） */
  uploadFolder: (folderName: string, files: File[]) => {
    const formData = new FormData();
    formData.append('folder_name', folderName);
    files.forEach(f => formData.append('files', f));
    return api.post('/works/upload-folder', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
  },

  /** 批量上传文件夹 */
  batchUploadFolders: (files: File[]) => {
    const formData = new FormData();
    files.forEach(f => formData.append('files', f));
    return api.post('/works/batch-upload-folder', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
  },

  /** 解析指定作品 */
  parseWork: (workId: number) => api.post(`/works/parse/${workId}`),

  /** 批量解析所有作品 */
  parseAllWorks: () => api.post('/works/parse-all'),
};

// ============ 评分管理 ============
export const scoreApi = {
  /** AI自动评分（单个队伍） */
  autoScore: (teamId: number, model?: string) =>
    api.post(`/scores/auto/${teamId}`, null, { params: model ? { model } : {} }),

  /** 重新评分（单个队伍，覆盖旧分） */
  reScoreTeam: (teamId: number, model?: string) =>
    api.post(`/scores/re-score/${teamId}`, null, { params: model ? { model } : {} }),

  /** 获取评分历史记录 */
  getScoreHistory: (teamId: number) =>
    api.get(`/scores/history/${teamId}`),

  /** AI自动评分（全部 - 同步版本） */
  autoScoreAll: () => api.post('/scores/auto-all'),

  /** AI自动评分（全部 - 异步版本，后台执行）
   * @param model LLM模型名称
   * @param parallel 是否并行评分
   * @param concurrency 并发数
   */
  autoScoreAllAsync: (model: string = 'qwen3.8-max', parallel: boolean = true, concurrency: number = 5) =>
    api.post('/scores/auto-all-async', null, { params: { model, parallel, concurrency } }),

  /** 获取可用LLM模型列表 */
  getAvailableModels: () => api.get('/scores/models'),

  /** 查询异步批量评分任务状态 */
  getAsyncTaskStatus: (taskId: string) =>
    api.get(`/scores/auto-all-async/status/${taskId}`),

  /** 列出所有批量评分任务 */
  listAsyncTasks: () => api.get('/scores/auto-all-async/tasks'),

  /** 获取机器评分结果 */
  getScoreResult: (teamId: number) =>
    api.get(`/scores/result/${teamId}`),

  /** 根据ID获取单条评分完整详情（用于版本切换） */
  getScoreById: (scoreId: number) =>
    api.get(`/scores/score/${scoreId}`),

  /** 采用指定评分版本 */
  adoptScore: (teamId: number, scoreId: number) =>
    api.post(`/scores/result/${teamId}/adopt/${scoreId}`),

  /** 全局最优采纳：每个队伍自动采纳最高分版本 */
  adoptBestScore: () => api.post('/scores/adopt-best'),

  /** 校准分数：线性拉伸到 [60, 90] 区间 */

  /** 获取评分统计 */
  getScoringStats: () => api.get('/scores/stats'),

  /** 获取待评分队伍列表 */
  getTeamsForScoring: (params?: { judge_group?: number; short_code?: string; member_name?: string }) =>
    api.get('/scores/teams-for-scoring', { params: params || {} }),

  /** 提交人工评分（双轨评分模式） */
  submitHumanScore: (data: {
    team_id: number;
    judge_name: string;
    // 旧维度（兼容）
    theme_score?: number;
    presentation_score?: number;
    process_score?: number;
    ai_literacy_score?: number;
    // 双轨人工维度
    human_presentation_score: number;
    human_creativity_score: number;
    human_process_score: number;
    human_performance_score: number;
    comment?: string;
  }) => api.post('/scores/human', data),

  /** 计算双轨综合分 */
  computeDualScore: (teamId: number) =>
    api.post(`/scores/compute-dual/${teamId}`),

  /** 获取综合评分 */
  getFinalScore: (teamId: number) =>
    api.get(`/scores/final/${teamId}`),

  /** 批量计算所有综合评分 */
  calculateAll: () => api.post('/scores/calculate-all'),
};

// ============ 批量评分管理 ============
export const batchScoringApi = {
  /** 启动批量评分任务 */
  start: (data: {
    model: string;
    parallel: boolean;
    concurrency: number;
    competition_id?: number;
  }) => api.post('/batch-scoring/start', data),

  /** 查询任务状态 */
  getStatus: (taskId: string) =>
    api.get(`/batch-scoring/status/${taskId}`),

  /** 获取任务完整结果 */
  getResults: (taskId: string) =>
    api.get(`/batch-scoring/results/${taskId}`),

  /** 列出所有批量评分任务 */
  listTasks: () => api.get('/batch-scoring/tasks'),
};

export interface DimensionConfig {
  name: string;
  max_score: number;
  description?: string;
}

export interface Competition {
  id: number;
  name: string;
  description?: string;
  is_active: boolean;
  dimensions_config?: Record<string, DimensionConfig>;
  naming_pattern?: string;
  naming_example?: string;
  scoring_mode?: string;
  ai_weight?: number;
  human_weight?: number;
}

/** 比赛配置 API */
export const competitionApi = {
  /** 获取当前激活比赛 */
  getCurrent: () => api.get('/competitions/current') as Promise<{ competition: Competition }>,
  /** 获取比赛评分维度 */
  getDimensions: (id: number | 'current' = 'current') =>
    api.get(`/competitions/${id}/dimensions`) as Promise<Record<string, DimensionConfig>>,
  /** 更新比赛配置 */
  update: (id: number, data: {
    name?: string;
    scoring_mode?: string;
    ai_weight?: number;
    human_weight?: number;
    description?: string;
  }) => api.put(`/competitions/${id}`, data),
};

// ============ 导出管理 ============
export const exportApi = {
  /** 获取评分汇总数据 */
  getScoreSummary: () => api.get('/export/score-summary'),

  /** 导出Excel */
  exportExcel: (taskId: string) => api.get('/export/excel', { params: { task_id: taskId }, responseType: 'blob' }),

  /** 导出Word */
  exportWord: (taskId: string) => api.get('/export/word', { params: { task_id: taskId }, responseType: 'blob' }),

  /** 备份数据 */
  backupData: () => api.post('/export/backup'),
};

export interface ExportTask {
  task_id: string;
  batch_id: string;
  total_items: number;
  status: string;
}

export interface AuthoritativeExportRow {
  item_id: string;
  exportable: boolean;
  authority_type?: string;
  derivation_id?: string;
  adoption_id?: string | null;
  lock_id?: string | null;
  result?: {
    submission_id: string;
    snapshot_id: string;
    attempt_id: string;
    total_score: number;
    objective_score: number | null;
    subjective_score: number | null;
    dimension_scores: Array<{ dimension_code: string; score: number; min_score: number; max_score: number; score_range_ref: string }>;
  };
  error?: { code: string; reasons: Array<{ code: string }> };
}

export type AuthoritativeExportFormat = 'xlsx' | 'docx' | 'csv';

export const authoritativeExportApi = {
  tasks: () => api.get<{ items: ExportTask[] }, { items: ExportTask[] }>('/result-export/tasks'),
  results: (taskId: string) => api.get<
    { task_id: string; total_items: number; items: AuthoritativeExportRow[] },
    { task_id: string; total_items: number; items: AuthoritativeExportRow[] }
  >(`/result-export/tasks/${encodeURIComponent(taskId)}/results`),
  download: async (taskId: string, itemIds: string[], format: AuthoritativeExportFormat, expectedDerivations: Record<string, string>): Promise<Blob> => {
    if (itemIds.length === 0) throw new Error('请先选择导出条目。');
    const response = await fetch(`/api/result-export/tasks/${encodeURIComponent(taskId)}/export`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ format, item_ids: itemIds, expected_derivations: expectedDerivations }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      throw new Error(body?.error?.message_key || '导出失败，请刷新结果并检查复核状态。');
    }
    return response.blob();
  },
};

// ============ 评审组配置 ============
export const judgeGroupApi = {
  /** 获取评审组配置 */
  getGroups: () => api.get('/judge-groups'),

  /** 保存评审组配置 */
  saveGroups: (groups: Array<{
    group_index: number;
    group_name: string;
    description?: string;
    judges?: string[];
  }>) => api.post('/judge-groups', { groups }),

  /** 分配队伍到评审组（随机分配） */
  assignTeams: () => api.post('/judge-groups/assign'),

  /** 上传评审老师分配表（Excel） */
  uploadAssignment: (file: File) => {
    const formData = new FormData();
    formData.append('file', file);
    return api.post('/judge-groups/upload-assignment', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
  },
};

// ============ 抄袭检测 ============
export const plagiarismApi = {
  /** 执行抄袭检测 */
  detect: (language: string = 'Python') =>
    api.post('/plagiarism/detect', null, { params: { language } }),

  /** 获取可疑队伍列表 */
  getSuspicious: () => api.get('/plagiarism/suspicious'),

  /** 清除抄袭标记 */
  clearFlags: () => api.post('/plagiarism/clear-flags'),
};

// ============ EvidencePackage 导入（Phase 11B-3a）============
// 类型与后端 models/evidence_api.py 保持一致；nullable 字段准确表示 null；
// 不声明后端未返回的 actual_summary、绝对路径、原始正文或内部 sidecar 字段。

/** 问题严重级别 */
export type EvidenceSeverity = 'info' | 'warning' | 'error' | 'fatal';
/** 验证模式 */
export type EvidenceValidationMode = 'dry_run' | 'registration' | 'revalidation';
/** 验证状态 */
export type EvidenceValidationStatus = 'passed' | 'passed_with_warnings' | 'failed' | 'internal_error';
/** 登记状态 */
export type EvidenceRegistrationStatus = 'registered' | 'rejected' | 'superseded' | 'corrupted';
/** 证据等级（evidence_level 允许 null） */
export type EvidenceLevel = 'sufficient' | 'limited' | 'insufficient' | 'manual_only';

/** 安全 Issue（仅 4 字段，对应 EvidenceIssueResponse） */
export interface EvidenceIssue {
  issue_id: string;
  code: string;
  severity: EvidenceSeverity;
  message_key: string;
}

/** 按 severity 计数（对应 IssueCounts） */
export interface EvidenceIssueCounts {
  info: number;
  warning: number;
  error: number;
  fatal: number;
}

/** 验证结果安全响应（对应 EvidenceValidationResponse；evidence_level 允许 null） */
export interface EvidenceValidation {
  validation_id: string;
  package_id: string;
  package_revision: number;
  mode: EvidenceValidationMode;
  status: EvidenceValidationStatus;
  evidence_level: EvidenceLevel | null;
  registration_allowed: boolean;
  model_input_allowed: boolean;
  issue_counts: EvidenceIssueCounts;
  issues: EvidenceIssue[];
  started_at: string;
  completed_at: string;
}

/** 稳定错误响应（对应 EvidenceApiError） */
export interface EvidenceApiError {
  code: string;
  message_key: string;
  retryable: boolean;
}

/** 批次单项（result 与 error 二选一；对应 EvidenceBatchItemResponse） */
export interface EvidenceBatchItem {
  package_ref: string;
  result: EvidenceValidation | null;
  error: EvidenceApiError | null;
}

/** 批次 dry-run 响应（计数不变量由后端保证；对应 EvidenceBatchDryRunResponse） */
export interface EvidenceBatchDryRunResponse {
  total: number;
  passed: number;
  passed_with_warnings: number;
  failed: number;
  internal_error: number;
  items: EvidenceBatchItem[];
}

/** 登记响应（rejected 时 record_id/entry_id/index_revision 为 null；对应 EvidenceRegistrationResponse） */
export interface EvidenceRegistrationResponse {
  idempotent_hit: boolean;
  rejected: boolean;
  record_id: string | null;
  entry_id: string | null;
  package_id: string;
  package_revision: number;
  validation: EvidenceValidation | null;
  index_revision: number | null;
}

/** 登记列表元素（Index 安全元数据；对应 EvidenceRegistrationSummary） */
export interface EvidenceRegistrationSummary {
  package_id: string;
  package_revision: number;
  batch_id: string;
  submission_id: string;
  registration_status: EvidenceRegistrationStatus;
  latest_validation_status: EvidenceValidationStatus;
  model_input_allowed: boolean;
  manifest_sha256: string;
}

/** 登记列表响应（对应 EvidenceRegistrationListResponse） */
export interface EvidenceRegistrationListResponse {
  total: number;
  items: EvidenceRegistrationSummary[];
}

/** 登记详情（安全子模型；对应 EvidenceRegistrationDetailResponse） */
export interface EvidenceRegistrationDetail {
  package_id: string;
  package_revision: number;
  record_id: string;
  entry_id: string;
  registration_status: EvidenceRegistrationStatus;
  record_revision: number;
  latest_validation: EvidenceValidation;
  issues: EvidenceIssue[];
}

/** 登记列表过滤参数（查询参数只发送已定义值） */
export interface EvidenceRegistrationFilters {
  package_id?: string;
  batch_id?: string;
  submission_id?: string;
  registration_status?: string;
  latest_validation_status?: string;
  model_input_allowed?: boolean;
}

/** EvidencePackage 导入 API 客户端（7 条接口，复用现有 api 实例） */
export const evidencePackageApi = {
  /** 单包 dry-run（验证结论 failed 仍返回 200） */
  dryRun: (packageRef: string): Promise<EvidenceValidation> =>
    api.post('/evidence-packages/dry-run', { package_ref: packageRef }),

  /** 批次 dry-run（fail_fast 默认 false） */
  dryRunBatch: (
    packageRefs: string[],
    failFast: boolean = false,
  ): Promise<EvidenceBatchDryRunResponse> =>
    api.post('/evidence-packages/dry-run-batch', { package_refs: packageRefs, fail_fast: failFast }),

  /** 正式登记（expectedIndexRevision 可选；缺省时不发送该字段） */
  register: (
    packageRef: string,
    expectedIndexRevision?: number | null,
  ): Promise<EvidenceRegistrationResponse> => {
    const body: { package_ref: string; expected_index_revision?: number | null } = { package_ref: packageRef };
    if (expectedIndexRevision !== undefined) {
      body.expected_index_revision = expectedIndexRevision;
    }
    return api.post('/evidence-packages/register', body);
  },

  /** 登记列表（只发送已定义过滤值） */
  listRegistrations: (filters?: EvidenceRegistrationFilters): Promise<EvidenceRegistrationListResponse> => {
    const params: Record<string, string | boolean> = {};
    if (filters) {
      if (filters.package_id !== undefined) params.package_id = filters.package_id;
      if (filters.batch_id !== undefined) params.batch_id = filters.batch_id;
      if (filters.submission_id !== undefined) params.submission_id = filters.submission_id;
      if (filters.registration_status !== undefined) params.registration_status = filters.registration_status;
      if (filters.latest_validation_status !== undefined) params.latest_validation_status = filters.latest_validation_status;
      if (filters.model_input_allowed !== undefined) params.model_input_allowed = filters.model_input_allowed;
    }
    return api.get('/evidence-packages/registrations', { params });
  },

  /** 登记详情（未登记返回 404） */
  getRegistration: (packageId: string, packageRevision: number): Promise<EvidenceRegistrationDetail> =>
    api.get(`/evidence-packages/registrations/${encodeURIComponent(packageId)}/revisions/${packageRevision}`),

  /** 指定验证结果（含历史；不存在返回 404） */
  getValidation: (packageId: string, packageRevision: number, validationId: string): Promise<EvidenceValidation> =>
    api.get(
      `/evidence-packages/registrations/${encodeURIComponent(packageId)}/revisions/${packageRevision}` +
      `/validations/${encodeURIComponent(validationId)}`,
    ),

  /** 验证问题明细（安全字段数组） */
  getValidationIssues: (packageId: string, packageRevision: number, validationId: string): Promise<EvidenceIssue[]> =>
    api.get(
      `/evidence-packages/registrations/${encodeURIComponent(packageId)}/revisions/${packageRevision}` +
      `/validations/${encodeURIComponent(validationId)}/issues`,
    ),
};

// ============ PipelineTask（只读监控，Phase 11C-4）============
// 类型与后端白名单响应完全一致（backend/models/pipeline_api.py），不补造后端未返回字段。

export interface PipelineStageSummary {
  stage: string;
  status: string;
  depends_on: string[];
  started_at: string | null;
  completed_at: string | null;
  total_items: number;
  pending_items: number;
  running_items: number;
  completed_items: number;
  failed_items: number;
  skipped_items: number;
  manual_review_items: number;
  blocking_error_codes: string[];
  revision: number;
}

export interface PipelineTask {
  task_id: string;
  batch_id: string;
  status: string;
  current_stage: string | null;
  execution_scope: string[];
  total_items: number;
  pending_items: number;
  running_items: number;
  completed_items: number;
  failed_items: number;
  skipped_items: number;
  manual_review_items: number;
  concurrency: number;
  stage_summaries: PipelineStageSummary[];
  created_at: string | null;
  started_at: string | null;
  updated_at: string | null;
  paused_at: string | null;
  completed_at: string | null;
  revision: number;
  last_event_sequence: number;
  idempotent_hit?: boolean;
}

export interface PipelineItemOutput {
  present: boolean;
  sha256: string | null;
}

export interface PipelineItemLastError {
  code: string;
  message_key: string;
  retryable: boolean;
}

export interface PipelineItem {
  item_id: string;
  task_id: string;
  package_id: string;
  package_revision: number;
  evidence_level: string;
  status: string;
  current_stage: string;
  attempt_count: number;
  max_attempts: number;
  retryable: boolean;
  heartbeat_updated_at: string | null;
  output: PipelineItemOutput;
  last_error: PipelineItemLastError | null;
  item_revision: number;
}

export interface PipelineEvent {
  event_id: string;
  task_id: string;
  sequence: number;
  event_type: string;
  occurred_at: string | null;
  stage: string | null;
  item_id: string | null;
  attempt_id: string | null;
  revision_before: number;
  revision_after: number;
  reason_code: string | null;
  metadata: Record<string, unknown>;
}

export interface PipelineTaskListResponse {
  items: PipelineTask[];
  total: number;
  offset: number;
  limit: number;
}

export interface PipelineItemListResponse {
  items: PipelineItem[];
  total: number;
}

export interface PipelineEventListResponse {
  items: PipelineEvent[];
  total: number;
}

export interface PipelineTaskListFilters {
  status?: string;
  offset?: number;
  limit?: number;
}

/** 只读监控 API：本阶段前端不调用 create/start/pause/heartbeat/resume/apply */
export const pipelineTaskApi = {
  /** 任务列表（分页 + 状态过滤） */
  listTasks: (filters?: PipelineTaskListFilters): Promise<PipelineTaskListResponse> => {
    const params: Record<string, string | number> = {};
    if (filters) {
      if (filters.status !== undefined) params.status = filters.status;
      if (filters.offset !== undefined) params.offset = filters.offset;
      if (filters.limit !== undefined) params.limit = filters.limit;
    }
    return api.get('/pipeline-tasks', { params });
  },

  /** 任务详情（不存在返回 404） */
  getTask: (taskId: string): Promise<PipelineTask> =>
    api.get(`/pipeline-tasks/${encodeURIComponent(taskId)}`),

  /** item 列表 */
  listItems: (taskId: string): Promise<PipelineItemListResponse> =>
    api.get(`/pipeline-tasks/${encodeURIComponent(taskId)}/items`),

  /** item 详情（不存在返回 404） */
  getItem: (taskId: string, itemId: string): Promise<PipelineItem> =>
    api.get(`/pipeline-tasks/${encodeURIComponent(taskId)}/items/${encodeURIComponent(itemId)}`),

  /** 事件时间线（sequence 升序） */
  getEvents: (taskId: string): Promise<PipelineEventListResponse> =>
    api.get(`/pipeline-tasks/${encodeURIComponent(taskId)}/events`),
};

// ============ 生产评分流水线操作台（Phase 11E-4b）============
// 这些类型严格对应 /api/scoring-pipeline 的安全响应，不包含 Prompt、证据正文或模型原文。

export interface ScoringPipelineTaskCounts {
  total: number;
  pending: number;
  running: number;
  completed: number;
  failed: number;
  skipped: number;
  manual_review: number;
}

export interface ScoringPipelineTaskSummary {
  task_id: string;
  source_task_id: string;
  batch_id: string;
  status: string;
  current_stage: string | null;
  revision: number;
  concurrency: number;
  counts: ScoringPipelineTaskCounts;
  idempotent_hit?: boolean | null;
}

export interface ScoringPipelineItemSummary {
  item_id: string;
  source_item_id: string;
  status: string;
  current_stage: string;
  item_revision: number;
  attempt_count: number;
  retryable: boolean;
  evidence_level: string;
  review_decision_id: string | null;
  latest_error_code: string | null;
}

export interface ScoringPipelineItemList {
  task_id: string;
  total: number;
  items: ScoringPipelineItemSummary[];
}

export interface CreateScoringPipelineTaskRequest {
  source_task_id: string;
  source_item_ids: string[];
  batch_id: string;
  profile_version: string;
  provider_model_ref: string;
  concurrency: number;
}

export interface ScoringPipelineDryRunCheck {
  check_code: string;
  passed: boolean;
  blocking: boolean;
}

export interface ScoringPipelineDryRunResult {
  request_id: string;
  task_id: string;
  item_id: string;
  decision: string;
  checks: ScoringPipelineDryRunCheck[];
  blocking_error_code: string | null;
}

export interface ScoringPipelineBatchExecutionResult {
  batch_execution_id: string;
  task_id: string;
  outcome: string;
  task_status: string;
  current_stage: string | null;
  task_revision: number;
  total_items: number;
  started: number;
  succeeded: number;
  skipped: number;
  blocked: number;
  recovery_required: number;
  pending_remaining: boolean;
  error_code_counts: Record<string, number>;
}

export interface ScoringPipelineRecoveryResult {
  recovery_request_id: string;
  task_id: string;
  item_id: string;
  outcome: string;
  error_code: string | null;
  attempt_id: string | null;
  snapshot_id: string | null;
  validation_id: string | null;
  provider_called: false;
}

export interface ScoringPipelineReviewApplicationResult {
  adoption_request_id: string;
  task_id: string;
  item_id: string;
  decision_id: string;
  outcome: string;
  adoption_id: string | null;
  review_decision_id: string | null;
  error_code: string | null;
}

// ---------------------------------------------------------------- 11F-2b：复核队列

export interface ReviewCaseSummary {
  review_case_id: string;
  item_id: string;
  priority: string;
  status: string;
  reason_codes: string[];
  opened_at: string;
  blocks_auto_adoption: boolean;
  blocks_export: boolean;
}

export interface ReviewCaseListResponse {
  task_id: string;
  total: number;
  items: ReviewCaseSummary[];
}

export interface ReviewCaseFilters {
  status?: string;
  priority?: string;
  reason_code?: string;
}

// 11F-2c：复核详情

export interface ManualAdjustmentDimensionContext {
  dimension_code: string;
  current_score: number;
  min_score: number;
  max_score: number;
}

export interface ManualAdjustmentContext {
  allowed: boolean;
  block_reason_code: string | null;
  attempt_id: string | null;
  snapshot_id: string | null;
  current_total_score: number | null;
  dimensions: ManualAdjustmentDimensionContext[];
}

export interface ReviewCaseDetail {
  case: {
    review_case_id: string;
    item_id: string;
    priority: string;
    status: string;
    package_revision: number | null;
    current_revision: number;
    reopen_count: number;
    reason_codes: string[];
    opened_at: string;
    blocks_auto_adoption: boolean;
    blocks_export: boolean;
  };
  attempts: ReviewCaseDetailAttempt[];
  decisions: ReviewCaseDetailDecision[];
  active_adoption: ReviewCaseDetailAdoption | null;
  active_lock: ReviewCaseDetailLock | null;
  manual_adjustment_context: ManualAdjustmentContext;
}

export interface ReviewCaseDetailAttempt {
  attempt_id: string;
  attempt_number: number;
  status: string;
  snapshot_id: string | null;
  total_score: number | null;
  created_at: string;
}

export interface ReviewCaseDetailDecision {
  decision_id: string;
  decision_type: string;
  requested_package_revision: number | null;
  decided_at: string;
  decided_by: { actor_type: string };
}

export interface ReviewCaseDetailAdoption {
  adoption_id: string;
  status: string;
  snapshot_id: string;
  decided_at: string;
}

export interface ReviewCaseDetailLock {
  lock_id: string;
  locked_at: string;
  reason_code: string;
}

export interface ManualAdjustmentChangeRequest {
  dimension_code: string;
  before_value: number;
  after_value: number;
  change_reason_code: string;
}

export interface ManualAdjustmentRequest {
  adjustment_request_id: string;
  review_case_id: string;
  changes: ManualAdjustmentChangeRequest[];
  reason_codes: string[];
  adjustment_note_code: string;
}

export interface ManualAdjustmentResponse {
  adjustment_id: string;
  adjusted_snapshot_id: string;
  adoption_id: string | null;
  outcome: string;
  idempotent: boolean;
}

export interface ReviewCaseReopenRequest {
  expected_revision: number;
  package_revision: number;
  request_decision_id: string;
}

export interface ReviewCaseReopenResponse {
  review_case_id: string;
  status: string;
  package_revision: number | null;
  current_revision: number;
  reopen_count: number;
  outcome: string;
  idempotent: boolean;
}

export const scoringPipelineApi = {
  createTask: (body: CreateScoringPipelineTaskRequest): Promise<ScoringPipelineTaskSummary> =>
    api.post('/scoring-pipeline/tasks', body),

  getTask: (taskId: string): Promise<ScoringPipelineTaskSummary> =>
    api.get(`/scoring-pipeline/tasks/${encodeURIComponent(taskId)}`),

  listItems: (taskId: string): Promise<ScoringPipelineItemList> =>
    api.get(`/scoring-pipeline/tasks/${encodeURIComponent(taskId)}/items`),

  listReviewCases: (
    taskId: string,
    filters?: ReviewCaseFilters,
  ): Promise<ReviewCaseListResponse> => {
    const params: Record<string, string> = {};
    if (filters?.status) params.status = filters.status;
    if (filters?.priority) params.priority = filters.priority;
    if (filters?.reason_code) params.reason_code = filters.reason_code;
    return api.get(
      `/scoring-pipeline/tasks/${encodeURIComponent(taskId)}/review-cases`,
      { params },
    );
  },

  getReviewCaseDetail: (
    taskId: string,
    caseId: string,
  ): Promise<ReviewCaseDetail> =>
    api.get(
      `/scoring-pipeline/tasks/${encodeURIComponent(taskId)}/review-cases/${encodeURIComponent(caseId)}/detail`,
    ),

  createReviewDecision: (
    taskId: string,
    itemId: string,
    body: {
      idempotency_key: string;
      review_case_id: string;
      decision_type: string;
      reason_codes: string[];
      target_attempt_id?: string;
      target_snapshot_id?: string;
      requested_package_revision?: number;
      decision_note_code?: string;
    },
  ): Promise<{ outcome: string; idempotent: boolean; decision_id: string }> =>
    api.post(
      `/scoring-pipeline/tasks/${encodeURIComponent(taskId)}/items/${encodeURIComponent(itemId)}/review-decisions`,
      body,
    ),

  lockFinalResult: (
    taskId: string,
    itemId: string,
    decisionId: string,
    body: {
      lock_request_id: string;
      review_case_id: string;
      adoption_id: string;
      reason_code?: string;
    },
  ): Promise<{ lock_id: string; status: string; outcome: string; idempotent: boolean }> =>
    api.post(
      `/scoring-pipeline/tasks/${encodeURIComponent(taskId)}/items/${encodeURIComponent(itemId)}` +
      `/review-decisions/${encodeURIComponent(decisionId)}/lock`,
      body,
    ),

  adjustResult: (
    taskId: string,
    itemId: string,
    decisionId: string,
    body: ManualAdjustmentRequest,
  ): Promise<ManualAdjustmentResponse> =>
    api.post(
      `/scoring-pipeline/tasks/${encodeURIComponent(taskId)}/items/${encodeURIComponent(itemId)}` +
      `/review-decisions/${encodeURIComponent(decisionId)}/adjust`,
      body,
    ),

  reopenReviewCase: (
    taskId: string,
    itemId: string,
    caseId: string,
    body: ReviewCaseReopenRequest,
  ): Promise<ReviewCaseReopenResponse> =>
    api.post(
      `/scoring-pipeline/tasks/${encodeURIComponent(taskId)}/items/${encodeURIComponent(itemId)}` +
      `/review-cases/${encodeURIComponent(caseId)}/reopen`,
      body,
    ),

  dryRun: (
    taskId: string,
    itemId: string,
    requestId: string,
  ): Promise<ScoringPipelineDryRunResult> =>
    api.post(
      `/scoring-pipeline/tasks/${encodeURIComponent(taskId)}/items/${encodeURIComponent(itemId)}/dry-run`,
      { request_id: requestId },
    ),

  execute: (
    taskId: string,
    batchExecutionId: string,
    concurrency?: number,
  ): Promise<ScoringPipelineBatchExecutionResult> => {
    const body: { batch_execution_id: string; concurrency?: number } = {
      batch_execution_id: batchExecutionId,
    };
    if (concurrency !== undefined) body.concurrency = concurrency;
    return api.post(`/scoring-pipeline/tasks/${encodeURIComponent(taskId)}/execute`, body);
  },

  recover: (
    taskId: string,
    itemId: string,
    recoveryRequestId: string,
  ): Promise<ScoringPipelineRecoveryResult> =>
    api.post(
      `/scoring-pipeline/tasks/${encodeURIComponent(taskId)}/items/${encodeURIComponent(itemId)}/recover`,
      { recovery_request_id: recoveryRequestId },
    ),

  applyReviewDecision: (
    taskId: string,
    itemId: string,
    decisionId: string,
    adoptionRequestId: string,
    competitionId: string,
  ): Promise<ScoringPipelineReviewApplicationResult> =>
    api.post(
      `/scoring-pipeline/tasks/${encodeURIComponent(taskId)}/items/${encodeURIComponent(itemId)}` +
      `/review-decisions/${encodeURIComponent(decisionId)}/apply`,
      { adoption_request_id: adoptionRequestId, competition_id: competitionId },
    ),
};

export default api;
