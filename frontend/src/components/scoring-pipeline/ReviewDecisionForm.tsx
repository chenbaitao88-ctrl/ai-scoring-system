/**
 * 11F-2d：复核决策表单（纯 props 驱动，不直接调用 API，不写数据）。
 *
 * 第一版开放：adopt_existing_attempt / reject_attempt / request_additional_evidence /
 * retry_same_provider / switch_provider / dismiss_no_issue / cancel_case。
 * apply_manual_adjustment 暂不开放。
 */
import { useState } from 'react';
import type { ReviewCaseDetail, ReviewCaseDetailAttempt } from '../../services/api';

const DECISION_TYPES: { value: string; label: string; needsTarget: boolean }[] = [
  { value: 'adopt_existing_attempt', label: '采用已有评分', needsTarget: true },
  { value: 'reject_attempt', label: '驳回评分', needsTarget: true },
  { value: 'request_additional_evidence', label: '请求补充证据', needsTarget: false },
  { value: 'retry_same_provider', label: '同模型重试', needsTarget: false },
  { value: 'switch_provider', label: '切换模型', needsTarget: false },
  { value: 'dismiss_no_issue', label: '无问题驳回', needsTarget: false },
  { value: 'cancel_case', label: '取消工单', needsTarget: false },
];

const REASON_OPTIONS: { value: string; label: string }[] = [
  { value: 'EVIDENCE_INSUFFICIENT', label: '证据不足' },
  { value: 'LOW_CONFIDENCE', label: '置信度低' },
  { value: 'HIGH_SCORE_VARIANCE', label: '分数方差高' },
  { value: 'HARD_FLAG_TRIGGERED', label: '硬标记触发' },
  { value: 'PRIVACY_RISK', label: '隐私风险' },
  { value: 'RANDOM_AUDIT', label: '随机抽检' },
  { value: 'APPEAL_REQUESTED', label: '申诉请求' },
  { value: 'PROVIDER_UNAVAILABLE', label: '模型不可用' },
  { value: 'ALL_ATTEMPTS_FAILED', label: '全部尝试失败' },
  { value: 'INVALID_MODEL_RESPONSE', label: '模型响应无效' },
  { value: 'SCORE_OUT_OF_RANGE', label: '分数越界' },
  { value: 'RATIONALE_MISSING', label: '评分依据缺失' },
];

export interface ReviewDecisionFormProps {
  detail: ReviewCaseDetail;
  /** 创建决策状态 */
  creating: boolean;
  createError: string | null;
  /** 采用状态 */
  applying: boolean;
  applyError: string | null;
  /** 锁定状态 */
  locking: boolean;
  lockError: string | null;
  /** 是否已有活跃 adoption */
  hasActiveAdoption: boolean;
  /** 是否已锁定 */
  hasActiveLock: boolean;
  /** 创建决策 */
  onCreateDecision: (params: {
    decisionType: string;
    reasonCodes: string[];
    targetAttemptId?: string;
    targetSnapshotId?: string;
    requestedPackageRevision?: number;
  }) => void;
  /** 采用结果 */
  onApplyAdoption: () => void;
  /** 锁定结果 */
  onLockResult: () => void;
}

export default function ReviewDecisionForm({
  detail,
  creating,
  createError,
  applying,
  applyError,
  locking,
  lockError,
  hasActiveAdoption,
  hasActiveLock,
  onCreateDecision,
  onApplyAdoption,
  onLockResult,
}: ReviewDecisionFormProps) {
  const [decisionType, setDecisionType] = useState('adopt_existing_attempt');
  const [reasonCodes, setReasonCodes] = useState<string[]>(detail.case.reason_codes);
  const [targetAttemptId, setTargetAttemptId] = useState(
    detail.attempts.length > 0 ? detail.attempts[detail.attempts.length - 1].attempt_id : '');
  const [requestedRevision, setRequestedRevision] = useState(2);

  const dt = DECISION_TYPES.find(d => d.value === decisionType);
  const needsTarget = dt?.needsTarget ?? false;

  const selectedAttempt = detail.attempts.find(a => a.attempt_id === targetAttemptId);

  const toggleReason = (code: string) => {
    setReasonCodes(prev =>
      prev.includes(code) ? prev.filter(c => c !== code) : [...prev, code]);
  };

  const handleCreate = () => {
    const params: {
      decisionType: string;
      reasonCodes: string[];
      targetAttemptId?: string;
      targetSnapshotId?: string;
      requestedPackageRevision?: number;
    } = { decisionType, reasonCodes };
    if (needsTarget && selectedAttempt) {
      params.targetAttemptId = selectedAttempt.attempt_id;
      params.targetSnapshotId = selectedAttempt.snapshot_id ?? undefined;
    }
    if (decisionType === 'request_additional_evidence') {
      params.requestedPackageRevision = requestedRevision;
    }
    onCreateDecision(params);
  };

  const isNonAdopt = decisionType !== 'adopt_existing_attempt';

  return (
    <div className="space-y-4" data-testid="review-decision-form">
      {/* 决策类型 */}
      <div>
        <label className="mb-1 block text-xs font-medium text-gray-700">决策类型</label>
        <select
          data-testid="review-decision-type"
          className="w-full rounded border border-gray-300 px-2 py-1.5 text-sm"
          value={decisionType}
          onChange={e => setDecisionType(e.target.value)}
        >
          {DECISION_TYPES.map(dt => (
            <option key={dt.value} value={dt.value}>{dt.label}</option>
          ))}
        </select>
      </div>

      {/* 目标 attempt */}
      {needsTarget && (
        <div>
          <label className="mb-1 block text-xs font-medium text-gray-700">目标评分</label>
          <select
            data-testid="review-decision-target-attempt"
            className="w-full rounded border border-gray-300 px-2 py-1.5 text-sm"
            value={targetAttemptId}
            onChange={e => setTargetAttemptId(e.target.value)}
          >
            {detail.attempts.map(a => (
              <option key={a.attempt_id} value={a.attempt_id}>
                #{a.attempt_number} / {a.status} / 总分 {a.total_score ?? '-'}
              </option>
            ))}
          </select>
        </div>
      )}

      {/* 原因码 */}
      <div>
        <label className="mb-1 block text-xs font-medium text-gray-700">原因码</label>
        <div className="flex flex-wrap gap-1" data-testid="review-decision-reason-codes">
          {REASON_OPTIONS.map(opt => (
            <button
              key={opt.value}
              type="button"
              className={`rounded px-2 py-0.5 text-xs border transition ${
                reasonCodes.includes(opt.value)
                  ? 'bg-teal-100 border-teal-300 text-teal-800'
                  : 'border-gray-200 text-gray-600 hover:bg-gray-50'
              }`}
              onClick={() => toggleReason(opt.value)}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </div>

      {/* 补充证据 revision */}
      {decisionType === 'request_additional_evidence' && (
        <div>
          <label className="mb-1 block text-xs font-medium text-gray-700">新 Package Revision</label>
          <input
            type="number"
            min={1}
            data-testid="review-decision-package-revision"
            className="w-full rounded border border-gray-300 px-2 py-1.5 text-sm"
            value={requestedRevision}
            onChange={e => setRequestedRevision(Number(e.target.value))}
          />
        </div>
      )}

      {/* 操作按钮 */}
      <div className="space-y-2">
        <button
          data-testid="review-decision-create"
          className="w-full rounded bg-teal-600 px-3 py-2 text-sm font-medium text-white hover:bg-teal-700 disabled:bg-teal-300"
          disabled={creating || applying || locking || reasonCodes.length === 0}
          onClick={handleCreate}
        >
          {creating ? '创建中...' : '创建复核决定'}
        </button>
        {createError && (
          <div className="rounded border border-red-200 bg-red-50 px-3 py-1.5 text-xs text-red-700" data-testid="review-decision-create-error">
            {createError}
          </div>
        )}

        {isNonAdopt && decisionType && (
          <div className="text-xs text-gray-500 text-center">
            当前阶段仅记录，不自动执行后续动作
          </div>
        )}

        {/* 采用 */}
        {decisionType === 'adopt_existing_attempt' && !hasActiveAdoption && (
          <button
            data-testid="review-decision-apply"
            className="w-full rounded bg-blue-600 px-3 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:bg-blue-300"
            disabled={creating || applying || locking}
            onClick={onApplyAdoption}
          >
            {applying ? '采用中...' : '采用结果'}
          </button>
        )}
        {applyError && (
          <div className="rounded border border-red-200 bg-red-50 px-3 py-1.5 text-xs text-red-700" data-testid="review-decision-apply-error">
            {applyError}
          </div>
        )}

        {/* 锁定 */}
        {hasActiveAdoption && !hasActiveLock && (
          <button
            data-testid="review-decision-lock"
            className="w-full rounded bg-amber-600 px-3 py-2 text-sm font-medium text-white hover:bg-amber-700 disabled:bg-amber-300"
            disabled={creating || applying || locking}
            onClick={onLockResult}
          >
            {locking ? '锁定中...' : '锁定结果'}
          </button>
        )}
        {hasActiveLock && (
          <div className="rounded border border-green-200 bg-green-50 px-3 py-1.5 text-xs text-green-700">
            结果已锁定
          </div>
        )}
        {lockError && (
          <div className="rounded border border-red-200 bg-red-50 px-3 py-1.5 text-xs text-red-700" data-testid="review-decision-lock-error">
            {lockError}
          </div>
        )}
      </div>
    </div>
  );
}
