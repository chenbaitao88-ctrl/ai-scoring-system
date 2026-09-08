/**
 * 证据包导入验证（Phase 11B-3b）
 *
 * 纯前端、只读的 EvidencePackage 批量 dry-run 页面：
 * - 多行输入 package_ref，逐行去空白；
 * - 重复引用前端阻止并提示；最多 1000 项；
 * - 统一调用 evidencePackageApi.dryRunBatch（不调用 register/评分/Provider/数据库）；
 * - 结果概览 + 结果表格 + 问题展开（仅展示 code / severity / message_key）；
 * - 不展示 actual_summary、绝对路径、原始正文、traceback、异常对象或学生个人信息。
 *
 * 未接入 App 路由与菜单（由后续阶段接入）。
 */
import { Fragment, useMemo, useState } from 'react';
import { evidencePackageApi } from '../services/api';
import type { EvidenceBatchDryRunResponse, EvidenceBatchItem } from '../services/api';

const MAX_ITEMS = 1000;

interface RowIssue {
  code: string;
  severity: string;
  message_key: string;
}

function statusLabel(status: string): string {
  switch (status) {
    case 'passed':
      return '通过';
    case 'passed_with_warnings':
      return '有警告';
    case 'failed':
      return '未通过';
    case 'internal_error':
      return '内部错误';
    default:
      return status;
  }
}

/** 状态 badge 完整静态类名映射（不在 JSX 内多层动态拼接） */
function statusBadgeClass(status: string): string {
  const map: Record<string, string> = {
    passed: 'bg-green-100 text-green-700',
    passed_with_warnings: 'bg-yellow-100 text-yellow-700',
    failed: 'bg-red-100 text-red-700',
    internal_error: 'bg-gray-100 text-gray-700',
  };
  return map[status] || 'bg-gray-100 text-gray-700';
}

const ERROR_BADGE_CLASS = 'inline-block px-2 py-1 rounded text-xs font-medium bg-gray-100 text-gray-700';

function issueRows(item: EvidenceBatchItem): RowIssue[] {
  if (!item.result) return [];
  return item.result.issues.map((i) => ({
    code: i.code,
    severity: i.severity,
    message_key: i.message_key,
  }));
}

export default function EvidencePackageImport() {
  const [inputText, setInputText] = useState('');
  const [failFast, setFailFast] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<EvidenceBatchDryRunResponse | null>(null);
  const [expandedIndex, setExpandedIndex] = useState<number | null>(null);
  // 本次请求实际提交的 package_ref 数量快照（独立于 refs 派生，编辑 textarea 不改变它）
  const [submittedCount, setSubmittedCount] = useState(0);

  const refs = useMemo(() => {
    return inputText
      .split('\n')
      .map((line) => line.trim())
      .filter((line) => line.length > 0);
  }, [inputText]);

  const duplicateSet = useMemo(() => {
    const seen = new Set<string>();
    const dups = new Set<string>();
    for (const ref of refs) {
      if (seen.has(ref)) dups.add(ref);
      seen.add(ref);
    }
    return dups;
  }, [refs]);

  const canSubmit =
    refs.length > 0 && duplicateSet.size === 0 && refs.length <= MAX_ITEMS && !loading;

  const handleValidate = async () => {
    setError('');
    if (refs.length === 0) {
      setError('请输入至少一个包引用（package_ref）。');
      return;
    }
    if (duplicateSet.size > 0) {
      setError(`存在重复引用：${Array.from(duplicateSet).join('、')}。请去重后再验证。`);
      return;
    }
    if (refs.length > MAX_ITEMS) {
      setError(`最多允许 ${MAX_ITEMS} 项，当前 ${refs.length} 项。`);
      return;
    }
    setLoading(true);
    setResult(null);
    // 发送 API 请求前记录本次实际提交的引用数（结果与提交快照一致）
    setSubmittedCount(refs.length);
    try {
      const res = await evidencePackageApi.dryRunBatch(refs, failFast);
      setResult(res);
      setExpandedIndex(null);
    } catch {
      // 安全通用错误：不展示响应原文、路径或异常对象；
      // 请求失败不保留会被误解为有效结果的 submittedCount
      setError('验证请求失败，请稍后重试。');
      setSubmittedCount(0);
    } finally {
      setLoading(false);
    }
  };

  const handleClear = () => {
    setInputText('');
    setError('');
    setResult(null);
    setExpandedIndex(null);
    setSubmittedCount(0);
  };

  const stopEarly = result !== null && submittedCount > 0 && result.total < submittedCount;

  return (
    <div className="space-y-6">
      <h2 className="text-xl font-bold text-teal-700">证据包导入验证</h2>

      {/* 输入区 */}
      <div data-testid="evidence-package-input" className="bg-white rounded-lg shadow p-6">
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-lg font-semibold text-gray-800">输入包引用</h3>
          <span className="text-sm text-gray-500">有效行数：{refs.length} / {MAX_ITEMS}</span>
        </div>
        <textarea
          value={inputText}
          onChange={(e) => setInputText(e.target.value)}
          rows={8}
          placeholder={'每行一个 package_ref，例如：\npkg-demo-001\npkg-demo-002'}
          className="w-full border border-gray-300 rounded px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-teal-500"
        />
        <div className="mt-3 space-y-2">
          <div className="flex items-center gap-2">
            <input
              id="fail-fast-toggle"
              type="checkbox"
              checked={failFast}
              onChange={(e) => setFailFast(e.target.checked)}
              className="h-4 w-4 rounded border-gray-300 text-teal-600 focus:ring-teal-500"
            />
            <label htmlFor="fail-fast-toggle" className="text-sm text-gray-700">
              fail_fast：遇到首个未通过或内部错误立即停止
            </label>
          </div>
          <div className="flex items-center gap-3">
            <button
              type="button"
              data-testid="evidence-dry-run-button"
              disabled={!canSubmit}
              onClick={handleValidate}
              className="px-4 py-2 rounded bg-teal-600 text-white text-sm font-medium disabled:opacity-50 disabled:cursor-not-allowed hover:bg-teal-700 focus:outline-none"
            >
              {loading ? '验证中…' : '开始验证'}
            </button>
            <button
              type="button"
              onClick={handleClear}
              className="px-4 py-2 rounded border border-gray-300 text-gray-700 text-sm font-medium hover:bg-gray-50 focus:outline-none"
            >
              清空
            </button>
            {duplicateSet.size > 0 && (
              <span className="text-sm text-red-600">
                存在重复引用：{Array.from(duplicateSet).join('、')}（已阻止提交）
              </span>
            )}
          </div>
        </div>
        {error && <div className="mt-3 text-sm text-red-600">{error}</div>}
      </div>

      {/* 结果概览 */}
      {result && (
        <div data-testid="evidence-summary" className="bg-white rounded-lg shadow p-6">
          <h3 className="text-lg font-semibold text-gray-800 mb-3">验证结果概览</h3>
          <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
            <div>
              <div className="text-sm text-gray-500">实际执行</div>
              <div className="text-xl font-bold text-gray-900">{result.total}</div>
            </div>
            <div>
              <div className="text-sm text-gray-500">通过</div>
              <div className="text-xl font-bold text-green-700">{result.passed}</div>
            </div>
            <div>
              <div className="text-sm text-gray-500">有警告</div>
              <div className="text-xl font-bold text-yellow-700">{result.passed_with_warnings}</div>
            </div>
            <div>
              <div className="text-sm text-gray-500">未通过</div>
              <div className="text-xl font-bold text-red-700">{result.failed}</div>
            </div>
            <div>
              <div className="text-sm text-gray-500">内部错误</div>
              <div className="text-xl font-bold text-gray-700">{result.internal_error}</div>
            </div>
          </div>
          {stopEarly && (
            <div className="mt-3 text-sm text-amber-700">
              本次仅执行 {result.total} / {submittedCount} 项，剩余项目未执行
            </div>
          )}
        </div>
      )}

      {/* 结果表格 */}
      {result && result.items.length > 0 && (
        <div data-testid="evidence-results-table" className="bg-white rounded-lg shadow p-6">
          <h3 className="text-lg font-semibold text-gray-800 mb-3">逐项结果</h3>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-200 text-left text-gray-500">
                  <th className="py-2 pr-4 font-medium">package_ref</th>
                  <th className="py-2 pr-4 font-medium">验证状态</th>
                  <th className="py-2 pr-4 font-medium">证据等级</th>
                  <th className="py-2 pr-4 font-medium">允许登记</th>
                  <th className="py-2 pr-4 font-medium">允许模型输入</th>
                  <th className="py-2 pr-4 font-medium">警告 / 错误 / 致命</th>
                  <th className="py-2 font-medium" />
                </tr>
              </thead>
              <tbody>
                {result.items.map((item, index) => {
                  const issues = issueRows(item);
                  const expanded = expandedIndex === index;
                  return (
                    <Fragment key={item.package_ref + '-' + index}>
                      <tr className="border-b border-gray-100">
                        <td className="py-2 pr-4 font-mono">{item.package_ref}</td>
                        <td className="py-2 pr-4">
                          {item.result ? (
                            <span className={`inline-block px-2 py-1 rounded text-xs font-medium ${statusBadgeClass(item.result.status)}`}>
                              {statusLabel(item.result.status)}
                            </span>
                          ) : (
                            <span className={ERROR_BADGE_CLASS}>
                              {item.error ? item.error.message_key : '未知错误'}
                            </span>
                          )}
                        </td>
                        <td className="py-2 pr-4">{item.result ? item.result.evidence_level ?? '—' : '—'}</td>
                        <td className="py-2 pr-4">
                          {item.result ? (item.result.registration_allowed ? '是' : '否') : '—'}
                        </td>
                        <td className="py-2 pr-4">
                          {item.result ? (item.result.model_input_allowed ? '是' : '否') : '—'}
                        </td>
                        <td className="py-2 pr-4">
                          {item.result
                            ? `${item.result.issue_counts.warning} / ${item.result.issue_counts.error} / ${item.result.issue_counts.fatal}`
                            : '—'}
                        </td>
                        <td className="py-2">
                          {item.result && issues.length > 0 && (
                            <button
                              type="button"
                              onClick={() => setExpandedIndex(expanded ? null : index)}
                              className="text-teal-700 text-xs font-medium hover:underline focus:outline-none"
                            >
                              {expanded ? '收起问题' : `查看问题（${issues.length}）`}
                            </button>
                          )}
                        </td>
                      </tr>
                      {expanded && (
                        <tr className="bg-gray-50">
                          <td colSpan={7} className="py-3 px-4">
                            <div className="text-xs text-gray-500 mb-2">问题明细（仅 code / severity / message_key）</div>
                            {issues.map((issue, i) => (
                              <div key={i} className="flex items-start gap-3 py-1 text-sm">
                                <span className="font-mono text-gray-800">{issue.code}</span>
                                <span className="text-gray-500">{issue.severity}</span>
                                <span className="text-gray-600">{issue.message_key}</span>
                              </div>
                            ))}
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
