import { useMemo, useState } from 'react';
import { RefreshIcon } from 'tdesign-icons-react';
import type { ReviewCaseDetail } from '../../services/api';

export interface EvidenceReopenPanelProps {
  detail: ReviewCaseDetail;
  submitting: boolean;
  error: string | null;
  success: string | null;
  onReopen: (params: { packageRevision: number; requestDecisionId: string }) => void;
}

export default function EvidenceReopenPanel({
  detail,
  submitting,
  error,
  success,
  onReopen,
}: EvidenceReopenPanelProps) {
  const requestDecision = useMemo(
    () => [...detail.decisions].reverse().find(
      (decision) => decision.decision_type === 'request_additional_evidence',
    ) ?? null,
    [detail.decisions],
  );
  const suggestedRevision = requestDecision?.requested_package_revision
    ?? ((detail.case.package_revision ?? 0) + 1);
  const [revision, setRevision] = useState(String(suggestedRevision));

  if (detail.case.status !== 'waiting_for_evidence') {
    return null;
  }

  const parsedRevision = Number(revision);
  const invalid = !Number.isInteger(parsedRevision) || parsedRevision < 1 || !requestDecision;

  return (
    <div className="space-y-3 rounded border border-amber-200 bg-amber-50 p-4" data-testid="evidence-reopen-panel">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h4 className="text-sm font-semibold text-amber-900">等待补充证据</h4>
          <div className="text-xs text-amber-800">
            当前 revision {detail.case.current_revision}，证据包 revision {detail.case.package_revision ?? '-'}，已重开 {detail.case.reopen_count} 次
          </div>
        </div>
        {requestDecision && (
          <code className="max-w-full break-all text-xs text-amber-700">{requestDecision.decision_id}</code>
        )}
      </div>
      <label className="block">
        <span className="mb-1 block text-xs font-medium text-amber-900">新 package revision</span>
        <input
          type="number"
          min={1}
          value={revision}
          onChange={(event) => setRevision(event.target.value)}
          disabled={submitting}
          className="w-full rounded border border-amber-300 bg-white px-2 py-1.5 text-sm focus:border-amber-500 focus:outline-none focus:ring-2 focus:ring-amber-100"
          data-testid="evidence-reopen-package-revision"
        />
      </label>
      {error && (
        <div className="rounded border border-red-200 bg-red-50 px-3 py-1.5 text-xs text-red-700" data-testid="evidence-reopen-error">
          {error}
        </div>
      )}
      {success && (
        <div className="rounded border border-green-200 bg-green-50 px-3 py-1.5 text-xs text-green-700" data-testid="evidence-reopen-success">
          {success}
        </div>
      )}
      <button
        type="button"
        className="inline-flex min-h-9 items-center justify-center gap-1.5 rounded bg-amber-600 px-3 py-2 text-sm font-medium text-white hover:bg-amber-700 disabled:bg-amber-300"
        disabled={submitting || invalid}
        onClick={() => requestDecision && onReopen({
          packageRevision: parsedRevision,
          requestDecisionId: requestDecision.decision_id,
        })}
        data-testid="evidence-reopen-submit"
      >
        <RefreshIcon size="16px" />
        <span>{submitting ? '重新打开中' : '重新打开'}</span>
      </button>
    </div>
  );
}
