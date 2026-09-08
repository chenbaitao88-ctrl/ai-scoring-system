/**
 * 11F-2c：复核详情面板（纯 props 驱动，不调用 API、不写数据）。
 */
import type { ReviewCaseDetail } from '../../services/api';

const PRIORITY_LABELS: Record<string, string> = {
  urgent: '紧急', high: '高', normal: '普通', low: '低',
};

const STATUS_LABELS: Record<string, string> = {
  open: '待处理',
  assigned: '已分配',
  in_review: '复核中',
  waiting_for_evidence: '等待补充证据',
  resolved: '已解决',
  dismissed: '已驳回',
  cancelled: '已取消',
};

const REASON_LABELS: Record<string, string> = {
  EVIDENCE_INSUFFICIENT: '证据不足', LOW_CONFIDENCE: '置信度低',
  HIGH_SCORE_VARIANCE: '分数方差高', HARD_FLAG_TRIGGERED: '硬标记触发',
  PRIVACY_RISK: '隐私风险', RANDOM_AUDIT: '随机抽检',
  APPEAL_REQUESTED: '申诉请求', PROVIDER_UNAVAILABLE: '模型不可用',
  ALL_ATTEMPTS_FAILED: '全部尝试失败', INVALID_MODEL_RESPONSE: '模型响应无效',
  SCORE_OUT_OF_RANGE: '分数越界', SCORE_COMPONENT_MISMATCH: '分项不匹配',
  RATIONALE_MISSING: '评分依据缺失', UNSUPPORTED_INFERENCE: '不支持的推理',
  EVIDENCE_REJECTED: '证据不通过', MATERIAL_MISSING: '材料缺失',
  MATERIAL_CORRUPTED: '材料损坏', LINK_INVALID: '链接无效',
  LARGE_VIDEO_MANUAL: '大视频需人工', PROVIDER_CAPABILITY_MISMATCH: '模型能力不匹配',
  EXPORT_RESULT_MISSING: '导出结果缺失',
};

const ATTEMPT_STATUS_LABELS: Record<string, string> = {
  succeeded: '成功', failed: '失败', running: '运行中',
  outcome_unknown: '结果未知', created: '创建', pending: '等待',
};

const DECISION_LABELS: Record<string, string> = {
  adopt_existing_attempt: '采用已有评分',
  reject_attempt: '驳回评分',
  dismiss_no_issue: '无问题驳回',
  request_additional_evidence: '请求补充证据',
  retry_same_provider: '同模型重试',
  switch_provider: '切换模型',
  apply_manual_adjustment: '人工调分',
  cancel_case: '取消工单',
};

function reasonLabel(code: string): string {
  return REASON_LABELS[code] ?? `未知（${code}）`;
}

export interface ReviewCaseDetailPanelProps {
  detail: ReviewCaseDetail | null;
  loading: boolean;
  error: string | null;
  onClose: () => void;
  onRetry: () => void;
}

export default function ReviewCaseDetailPanel({
  detail,
  loading,
  error,
  onClose,
  onRetry,
}: ReviewCaseDetailPanelProps) {
  if (loading) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-5 shadow-sm" data-testid="review-case-detail-loading">
        <div className="text-center text-sm text-gray-400">加载详情中...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded-lg border border-red-200 bg-red-50 p-5 shadow-sm" data-testid="review-case-detail-error">
        <div className="flex items-center justify-between">
          <span className="text-sm text-red-800">{error}</span>
          <div className="flex gap-2">
            <button className="rounded border border-red-300 px-3 py-1 text-xs text-red-700 hover:bg-red-100" onClick={onRetry}>重试</button>
            <button className="rounded border border-gray-300 px-3 py-1 text-xs text-gray-600 hover:bg-gray-100" onClick={onClose}>关闭</button>
          </div>
        </div>
      </div>
    );
  }

  if (!detail) return null;

  const c = detail.case;
  return (
    <div className="rounded-lg border border-gray-200 bg-white shadow-sm" data-testid="review-case-detail-panel">
      <div className="flex items-center justify-between border-b border-gray-200 px-5 py-3">
        <h3 className="text-sm font-semibold text-gray-800">
          工单详情 <span className="font-mono text-xs text-gray-500">{c.review_case_id.slice(0, 16)}</span>
        </h3>
        <button className="text-sm text-gray-400 hover:text-gray-600" onClick={onClose} title="关闭详情">✕</button>
      </div>

      <div className="space-y-4 p-5">
        {/* 工单信息 */}
        <Section title="工单信息">
          <Row label="优先级" value={PRIORITY_LABELS[c.priority] ?? c.priority} />
          <Row label="状态" value={STATUS_LABELS[c.status] ?? c.status} />
          <Row label="证据包 revision" value={c.package_revision === null ? '-' : String(c.package_revision)} />
          <Row label="工单 revision" value={`${c.current_revision} / 重开 ${c.reopen_count}`} />
          <Row label="原因" value={c.reason_codes.map(reasonLabel).join(' / ')} />
          <Row label="阻断自动采用" value={c.blocks_auto_adoption ? '是' : '否'} />
          <Row label="阻断导出" value={c.blocks_export ? '是' : '否'} />
          <Row label="创建时间" value={c.opened_at} />
        </Section>

        {/* 评分历史 */}
        <Section title={`评分历史（${detail.attempts.length}）`}>
          {detail.attempts.length === 0 ? (
            <EmptyText />
          ) : (
            <table className="min-w-full text-xs">
              <thead>
                <tr className="border-b border-gray-100 text-left text-gray-500">
                  <th className="py-1 pr-3 font-normal">#</th>
                  <th className="py-1 pr-3 font-normal">状态</th>
                  <th className="py-1 pr-3 font-normal">总分</th>
                  <th className="py-1 font-normal">时间</th>
                </tr>
              </thead>
              <tbody>
                {detail.attempts.map((a) => (
                  <tr key={a.attempt_id} className="border-b border-gray-50">
                    <td className="py-1 pr-3 font-mono">{a.attempt_number}</td>
                    <td className="py-1 pr-3">{ATTEMPT_STATUS_LABELS[a.status] ?? a.status}</td>
                    <td className="py-1 pr-3 font-mono">{a.total_score ?? '-'}</td>
                    <td className="py-1 text-gray-500">{a.created_at ? a.created_at.slice(0, 19) : '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Section>

        {/* 决策历史 */}
        <Section title={`决策历史（${detail.decisions.length}）`}>
          {detail.decisions.length === 0 ? (
            <EmptyText />
          ) : (
            <table className="min-w-full text-xs">
              <thead>
                <tr className="border-b border-gray-100 text-left text-gray-500">
                  <th className="py-1 pr-3 font-normal">类型</th>
                  <th className="py-1 pr-3 font-normal">操作者</th>
                  <th className="py-1 font-normal">时间</th>
                </tr>
              </thead>
              <tbody>
                {detail.decisions.map((d) => (
                  <tr key={d.decision_id} className="border-b border-gray-50">
                    <td className="py-1 pr-3">{DECISION_LABELS[d.decision_type] ?? d.decision_type}</td>
                    <td className="py-1 pr-3">{d.decided_by?.actor_type === 'reviewer' ? '复核员' : '系统'}</td>
                    <td className="py-1 text-gray-500">{d.decided_at ? d.decided_at.slice(0, 19) : '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Section>

        {/* 采用状态 */}
        <Section title="采用状态">
          {detail.active_adoption ? (
            <>
              <Row label="采用 ID" value={detail.active_adoption.adoption_id.slice(0, 16)} />
              <Row label="状态" value={detail.active_adoption.status} />
              <Row label="时间" value={detail.active_adoption.decided_at} />
            </>
          ) : (
            <EmptyText text="暂无采用记录" />
          )}
        </Section>

        {/* 锁定状态 */}
        <Section title="锁定状态">
          {detail.active_lock ? (
            <>
              <Row label="锁定 ID" value={detail.active_lock.lock_id.slice(0, 16)} />
              <Row label="原因" value={reasonLabel(detail.active_lock.reason_code)} />
              <Row label="时间" value={detail.active_lock.locked_at} />
            </>
          ) : (
            <EmptyText text="未锁定" />
          )}
        </Section>
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <h4 className="mb-2 text-xs font-semibold uppercase tracking-wider text-gray-500">{title}</h4>
      <div className="space-y-1">{children}</div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-2 text-xs">
      <span className="w-24 shrink-0 text-gray-500">{label}</span>
      <span className="text-gray-800">{value}</span>
    </div>
  );
}

function EmptyText({ text = '暂无数据' }: { text?: string }) {
  return <div className="text-xs text-gray-400">{text}</div>;
}
