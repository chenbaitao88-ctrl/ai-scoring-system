/**
 * 11F-2b：复核队列列表视图（纯 props 驱动，不调用 API、不保存业务状态）。
 */
import type { ReactNode } from 'react';
import { RefreshIcon, RollbackIcon, SearchIcon } from 'tdesign-icons-react';
import type { ReviewCaseSummary, ReviewCaseFilters } from '../../services/api';

// ---------------------------------------------------------------- 中文映射

const PRIORITY_LABELS: Record<string, string> = {
  urgent: '紧急',
  high: '高',
  normal: '普通',
  low: '低',
};

const STATUS_LABELS: Record<string, string> = {
  open: '待处理',
  in_review: '复核中',
  resolved: '已解决',
  dismissed: '已驳回',
  cancelled: '已取消',
};

const REASON_LABELS: Record<string, string> = {
  EVIDENCE_INSUFFICIENT: '证据不足',
  EVIDENCE_REJECTED: '证据不通过',
  MATERIAL_MISSING: '材料缺失',
  MATERIAL_CORRUPTED: '材料损坏',
  LINK_INVALID: '链接无效',
  LARGE_VIDEO_MANUAL: '大视频需人工',
  PROVIDER_CAPABILITY_MISMATCH: '模型能力不匹配',
  PROVIDER_UNAVAILABLE: '模型不可用',
  ALL_ATTEMPTS_FAILED: '全部尝试失败',
  INVALID_MODEL_RESPONSE: '模型响应无效',
  SCORE_OUT_OF_RANGE: '分数越界',
  SCORE_COMPONENT_MISMATCH: '分项不匹配',
  RATIONALE_MISSING: '评分依据缺失',
  UNSUPPORTED_INFERENCE: '不支持的推理',
  LOW_CONFIDENCE: '置信度低',
  HIGH_SCORE_VARIANCE: '分数方差高',
  HARD_FLAG_TRIGGERED: '硬标记触发',
  PRIVACY_RISK: '隐私风险',
  RANDOM_AUDIT: '随机抽检',
  APPEAL_REQUESTED: '申诉请求',
  EXPORT_RESULT_MISSING: '导出结果缺失',
};

function reasonLabel(code: string): string {
  return REASON_LABELS[code] ?? `未知复核原因（${code}）`;
}

function priorityBadge(priority: string): string {
  const base = 'inline-flex items-center rounded px-2 py-0.5 text-xs font-medium';
  if (priority === 'urgent') return `${base} bg-red-100 text-red-700`;
  if (priority === 'high') return `${base} bg-orange-100 text-orange-700`;
  if (priority === 'low') return `${base} bg-gray-100 text-gray-600`;
  return `${base} bg-blue-100 text-blue-700`;
}

function statusBadge(status: string): string {
  const base = 'inline-flex items-center rounded px-2 py-0.5 text-xs font-medium';
  if (status === 'open') return `${base} bg-yellow-100 text-yellow-700`;
  if (status === 'in_review') return `${base} bg-blue-100 text-blue-700`;
  if (status === 'resolved') return `${base} bg-green-100 text-green-700`;
  if (status === 'dismissed') return `${base} bg-gray-100 text-gray-600`;
  if (status === 'cancelled') return `${base} bg-gray-100 text-gray-500`;
  return `${base} bg-gray-100 text-gray-700`;
}

function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes}分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}小时前`;
  const days = Math.floor(hours / 24);
  return `${days}天前`;
}

// ---------------------------------------------------------------- 组件

export interface ReviewCaseListViewProps {
  /** 是否已选择 task */
  hasTask: boolean;
  /** 复核列表数据 */
  cases: ReviewCaseSummary[];
  /** 总数 */
  total: number;
  /** 是否加载中 */
  loading: boolean;
  /** 错误信息 */
  error: string | null;
  /** 当前筛选参数 */
  filters: ReviewCaseFilters;
  /** 筛选变更回调 */
  onFiltersChange: (filters: ReviewCaseFilters) => void;
  /** 查询 */
  onSearch: () => void;
  /** 重置 */
  onReset: () => void;
  /** 重试 */
  onRetry: () => void;
  /** 11F-2c：当前选中工单 ID */
  selectedCaseId: string | null;
  /** 11F-2c：选中工单回调 */
  onSelectCase: (caseId: string | null) => void;
}

export default function ReviewCaseListView({
  hasTask,
  cases,
  total,
  loading,
  error,
  filters,
  onFiltersChange,
  onSearch,
  onReset,
  onRetry,
  selectedCaseId,
  onSelectCase,
}: ReviewCaseListViewProps) {
  if (!hasTask) {
    return (
      <div
        className="rounded-lg border border-dashed border-gray-300 bg-white px-6 py-10 text-center text-sm text-gray-500"
        data-testid="review-case-empty"
      >
        请先选择评分任务
      </div>
    );
  }

  return (
    <div className="space-y-4" data-testid="review-case-list-view">
      {/* 摘要 */}
      <div className="flex items-center justify-between rounded-lg border border-gray-200 bg-white px-4 py-3 shadow-sm">
        <span className="text-sm text-gray-600">
          待复核工单：<span className="font-semibold text-gray-800">{total}</span> 件
        </span>
        <IconButton
          testId="review-case-retry"
          icon={<RefreshIcon size="14px" />}
          label="刷新"
          title="刷新复核列表"
          onClick={onRetry}
          disabled={loading}
        />
      </div>

      {/* 筛选项 */}
      <div className="flex flex-wrap items-end gap-3 rounded-lg border border-gray-200 bg-white px-4 py-3 shadow-sm">
        <FilterSelect
          testId="review-case-status-filter"
          label="状态"
          value={filters.status ?? ''}
          options={[
            { value: '', label: '全部状态' },
            { value: 'open', label: '待处理' },
            { value: 'in_review', label: '复核中' },
            { value: 'resolved', label: '已解决' },
            { value: 'dismissed', label: '已驳回' },
          ]}
          onChange={(v) => onFiltersChange({ ...filters, status: v || undefined })}
        />
        <FilterSelect
          testId="review-case-priority-filter"
          label="优先级"
          value={filters.priority ?? ''}
          options={[
            { value: '', label: '全部优先级' },
            { value: 'urgent', label: '紧急' },
            { value: 'high', label: '高' },
            { value: 'normal', label: '普通' },
            { value: 'low', label: '低' },
          ]}
          onChange={(v) => onFiltersChange({ ...filters, priority: v || undefined })}
        />
        <FilterSelect
          testId="review-case-reason-filter"
          label="原因"
          value={filters.reason_code ?? ''}
          options={[
            { value: '', label: '全部原因' },
            { value: 'EVIDENCE_INSUFFICIENT', label: '证据不足' },
            { value: 'LOW_CONFIDENCE', label: '置信度低' },
            { value: 'HIGH_SCORE_VARIANCE', label: '分数方差高' },
            { value: 'HARD_FLAG_TRIGGERED', label: '硬标记触发' },
            { value: 'PRIVACY_RISK', label: '隐私风险' },
            { value: 'RANDOM_AUDIT', label: '随机抽检' },
            { value: 'APPEAL_REQUESTED', label: '申诉请求' },
          ]}
          onChange={(v) => onFiltersChange({ ...filters, reason_code: v || undefined })}
        />
        <IconButton
          testId="review-case-search"
          icon={<SearchIcon size="14px" />}
          label="查询"
          title="查询复核工单"
          onClick={onSearch}
          disabled={loading}
          primary
        />
        <IconButton
          testId="review-case-reset"
          icon={<RollbackIcon size="14px" />}
          label="重置"
          title="重置筛选条件"
          onClick={onReset}
          disabled={loading}
        />
      </div>

      {/* 错误 */}
      {error && (
        <div
          className="flex items-center justify-between rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800"
          data-testid="review-case-error"
          role="alert"
        >
          <span>{error}</span>
          <button
            className="ml-4 rounded border border-red-300 px-3 py-1 text-xs font-medium text-red-700 hover:bg-red-100"
            onClick={onRetry}
          >
            重试
          </button>
        </div>
      )}

      {/* loading */}
      {loading && (
        <div className="rounded-lg border border-gray-200 bg-white px-6 py-10 text-center text-sm text-gray-400">
          加载中...
        </div>
      )}

      {/* 空态 */}
      {!loading && !error && cases.length === 0 && (
        <div
          className="rounded-lg border border-dashed border-gray-300 bg-white px-6 py-10 text-center text-sm text-gray-500"
          data-testid="review-case-empty"
        >
          暂无待复核工单
        </div>
      )}

      {/* 列表 */}
      {!loading && !error && cases.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white shadow-sm">
          <table className="min-w-full text-sm" data-testid="review-case-table">
            <thead>
              <tr className="border-b border-gray-200 bg-gray-50 text-left text-xs font-medium uppercase tracking-wider text-gray-500">
                <th className="px-4 py-3">工单 ID</th>
                <th className="px-4 py-3">作品 ID</th>
                <th className="px-4 py-3">优先级</th>
                <th className="px-4 py-3">状态</th>
                <th className="px-4 py-3">原因</th>
                <th className="px-4 py-3">创建时间</th>
                <th className="px-4 py-3">阻断自动采用</th>
                <th className="px-4 py-3">阻断导出</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {cases.map((c) => (
                <tr
                  key={c.review_case_id}
                  className={`cursor-pointer transition hover:bg-gray-50 ${
                    selectedCaseId === c.review_case_id ? 'bg-blue-50' : ''
                  }`}
                  onClick={() => onSelectCase(
                    selectedCaseId === c.review_case_id ? null : c.review_case_id)}
                  data-testid={`review-case-row-${c.review_case_id}`}
                >
                  <td className="px-4 py-2.5 font-mono text-xs text-gray-600">
                    {c.review_case_id.slice(0, 16)}
                  </td>
                  <td className="px-4 py-2.5 font-mono text-xs text-gray-600">
                    {c.item_id.slice(0, 16)}
                  </td>
                  <td className="px-4 py-2.5">
                    <span className={priorityBadge(c.priority)}>
                      {PRIORITY_LABELS[c.priority] ?? c.priority}
                    </span>
                  </td>
                  <td className="px-4 py-2.5">
                    <span className={statusBadge(c.status)}>
                      {STATUS_LABELS[c.status] ?? c.status}
                    </span>
                  </td>
                  <td className="px-4 py-2.5 text-xs text-gray-600">
                    {c.reason_codes.map((code) => (
                      <span key={code} className="mr-1 inline-block rounded bg-gray-100 px-1.5 py-0.5">
                        {reasonLabel(code)}
                      </span>
                    ))}
                  </td>
                  <td
                    className="px-4 py-2.5 text-xs text-gray-500"
                    title={c.opened_at}
                  >
                    {relativeTime(c.opened_at)}
                  </td>
                  <td className="px-4 py-2.5 text-xs text-gray-500">
                    {c.blocks_auto_adoption ? '是' : '否'}
                  </td>
                  <td className="px-4 py-2.5 text-xs text-gray-500">
                    {c.blocks_export ? '是' : '否'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- 内部辅助

function FilterSelect({
  testId,
  label,
  value,
  options,
  onChange,
}: {
  testId: string;
  label: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (value: string) => void;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-xs text-gray-500">{label}</span>
      <select
        data-testid={testId}
        className="rounded border border-gray-300 px-2 py-1.5 text-sm focus:border-teal-500 focus:outline-none focus:ring-2 focus:ring-teal-100"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        {options.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {opt.label}
          </option>
        ))}
      </select>
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
      data-testid={testId}
      className={`inline-flex items-center gap-1.5 rounded px-3 py-1.5 text-xs font-medium transition ${
        primary
          ? 'bg-teal-600 text-white hover:bg-teal-700 disabled:bg-teal-300'
          : 'border border-gray-300 bg-white text-gray-700 hover:bg-gray-50 disabled:opacity-50'
      }`}
      title={title}
      onClick={onClick}
      disabled={disabled}
    >
      {icon}
      <span>{label}</span>
    </button>
  );
}
