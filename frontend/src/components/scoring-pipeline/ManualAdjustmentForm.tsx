import { useMemo, useState } from 'react';
import type { ManualAdjustmentContext, ManualAdjustmentChangeRequest } from '../../services/api';

const REASONS = [
  { value: 'LOW_CONFIDENCE', label: '置信度低' },
  { value: 'SCORE_COMPONENT_MISMATCH', label: '分项不匹配' },
  { value: 'RANDOM_AUDIT', label: '随机抽检' },
];

export interface ManualAdjustmentFormProps {
  context: ManualAdjustmentContext;
  submitting: boolean;
  error: string | null;
  success: string | null;
  requestId: string;
  onSubmit: (changes: ManualAdjustmentChangeRequest[], reasonCodes: string[]) => void;
  onCancel: () => void;
}

export default function ManualAdjustmentForm({
  context, submitting, error, success, requestId, onSubmit, onCancel,
}: ManualAdjustmentFormProps) {
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(context.dimensions.map((d) => [d.dimension_code, String(d.current_score)])));
  const [reasonCodes, setReasonCodes] = useState<string[]>([]);

  const changes = useMemo(() => context.dimensions.flatMap((dimension) => {
    const next = Number(values[dimension.dimension_code]);
    if (!Number.isFinite(next) || next === dimension.current_score) return [];
    return [{
      dimension_code: dimension.dimension_code,
      before_value: dimension.current_score,
      after_value: next,
      change_reason_code: reasonCodes[0] ?? 'OTHER',
    }];
  }), [context.dimensions, values, reasonCodes]);

  const previewTotal = (context.current_total_score ?? 0) + changes.reduce(
    (sum, change) => sum + change.after_value - change.before_value, 0);
  const invalidRange = context.dimensions.some((dimension) => {
    const next = Number(values[dimension.dimension_code]);
    return Number.isFinite(next) && (next < dimension.min_score || next > dimension.max_score);
  });

  const submit = () => {
    if (!context.allowed || submitting || invalidRange || changes.length === 0 || reasonCodes.length === 0) return;
    onSubmit(changes, reasonCodes);
  };

  if (!context.allowed) {
    return (
      <div className="rounded border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800" data-testid="manual-adjustment-blocked">
        当前不可调整：{context.block_reason_code ?? '操作被阻断'}
      </div>
    );
  }

  return (
    <div className="space-y-3 rounded border border-gray-200 bg-white p-4" data-testid="manual-adjustment-form">
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-semibold text-gray-800">人工调整分数</h4>
        <span className="text-xs text-gray-500" data-testid="manual-adjustment-request-id">请求 {requestId}</span>
      </div>
      <div className="text-xs text-gray-600">当前总分：<strong>{context.current_total_score ?? '-'}</strong>　调整后预览：<strong data-testid="manual-adjustment-total-preview">{previewTotal}</strong></div>
      <div className="space-y-2">
        {context.dimensions.map((dimension) => (
          <div key={dimension.dimension_code} className="grid grid-cols-[1fr_100px_140px] items-center gap-2 text-xs" data-testid={`manual-adjustment-dimension-${dimension.dimension_code}`}>
            <div><div className="font-medium text-gray-700">{dimension.dimension_code}</div><div className="text-gray-400">范围 {dimension.min_score} - {dimension.max_score}，当前 {dimension.current_score}</div></div>
            <input
              type="number" step="0.1" min={dimension.min_score} max={dimension.max_score}
              value={values[dimension.dimension_code] ?? ''}
              onChange={(event) => setValues((current) => ({ ...current, [dimension.dimension_code]: event.target.value }))}
              disabled={submitting}
              className="rounded border border-gray-300 px-2 py-1"
              data-testid={`manual-adjustment-input-${dimension.dimension_code}`}
            />
          </div>
        ))}
      </div>
      <div>
        <div className="mb-1 text-xs font-medium text-gray-700">调整原因码</div>
        <div className="flex flex-wrap gap-1" data-testid="manual-adjustment-reasons">
          {REASONS.map((reason) => (
            <button key={reason.value} type="button" disabled={submitting} onClick={() => setReasonCodes((current) => current.includes(reason.value) ? current.filter((item) => item !== reason.value) : [...current, reason.value])} className={`rounded border px-2 py-1 text-xs ${reasonCodes.includes(reason.value) ? 'border-blue-300 bg-blue-50 text-blue-700' : 'border-gray-200 text-gray-600'}`}>{reason.label}</button>
          ))}
        </div>
      </div>
      <div className="text-xs text-gray-500">调整将创建新的评分快照，原始评分将完整保留。总分仅用于预览。</div>
      {invalidRange && <div className="text-xs text-red-600" data-testid="manual-adjustment-error">调整分数超出允许范围</div>}
      {error && <div className="text-xs text-red-600" data-testid="manual-adjustment-error">{error}</div>}
      {success && <div className="text-xs text-green-700" data-testid="manual-adjustment-success">{success}</div>}
      <div className="flex justify-end gap-2">
        <button type="button" onClick={onCancel} disabled={submitting} className="rounded border border-gray-300 px-3 py-1.5 text-xs text-gray-600" data-testid="manual-adjustment-cancel">取消</button>
        <button type="button" onClick={submit} disabled={submitting || invalidRange || changes.length === 0 || reasonCodes.length === 0} className="rounded bg-blue-600 px-3 py-1.5 text-xs text-white disabled:bg-blue-300" data-testid="manual-adjustment-submit">{submitting ? '提交中...' : '确认调整'}</button>
      </div>
    </div>
  );
}
