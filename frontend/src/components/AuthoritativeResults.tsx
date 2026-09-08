import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { authoritativeExportApi } from '../services/api';
import type { AuthoritativeExportFormat, AuthoritativeExportRow, ExportTask } from '../services/api';

const formats: Array<[AuthoritativeExportFormat, string]> = [['xlsx', 'Excel'], ['docx', 'Word'], ['csv', 'CSV']];
const reasonLabels: Record<string, string> = {
  UNRESOLVED_REVIEW_CASE: '复核工单尚未结案',
  NO_AUTHORITATIVE_RESULT: '尚未采用或锁定结果',
  SNAPSHOT_MISSING_OR_CORRUPTED: '结果版本缺失或损坏',
  ACTIVE_LOCK_FACT_INVALID: '锁定记录异常',
  ACTIVE_ADOPTION_FACT_INVALID: '采用记录异常',
};

function blockedReason(row: AuthoritativeExportRow): string {
  return row.error?.reasons.map(reason => reasonLabels[reason.code] || '结果记录需要检查').join('；') || '请检查复核与结果记录';
}

export default function AuthoritativeResults() {
  const [tasks, setTasks] = useState<ExportTask[]>([]);
  const [taskId, setTaskId] = useState('');
  const [rows, setRows] = useState<AuthoritativeExportRow[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [tasksLoading, setTasksLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const [taskRefresh, setTaskRefresh] = useState(0);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [downloading, setDownloading] = useState<AuthoritativeExportFormat | null>(null);
  const downloadBusy = useRef(false);

  useEffect(() => {
    let cancelled = false;
    setTasksLoading(true);
    authoritativeExportApi.tasks().then(data => {
      if (!cancelled) { setTasks(data.items); setError(''); }
    }).catch((cause: unknown) => {
      if (!cancelled) setError(cause instanceof Error ? cause.message : '任务列表加载失败');
    }).finally(() => { if (!cancelled) setTasksLoading(false); });
    return () => { cancelled = true; };
  }, [taskRefresh]);

  useEffect(() => {
    let cancelled = false;
    setRows([]);
    setSelected([]);
    if (!taskId) { setLoading(false); return () => { cancelled = true; }; }
    setLoading(true);
    authoritativeExportApi.results(taskId).then(data => {
      if (!cancelled) setRows(data.items);
    }).catch((cause: unknown) => {
      if (!cancelled) setError(cause instanceof Error ? cause.message : '结果加载失败');
    }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [taskId, refresh]);

  const readyIds = rows.filter(row => row.exportable).map(row => row.item_id);
  const busy = loading || downloading !== null;
  const download = async (format: AuthoritativeExportFormat) => {
    if (downloadBusy.current || !taskId || selected.length === 0) return;
    downloadBusy.current = true;
    setDownloading(format);
    setError('');
    setNotice('');
    try {
      const expected = Object.fromEntries(rows.filter(row => selected.includes(row.item_id)).map(row => [row.item_id, row.derivation_id ?? '']));
      const blob = await authoritativeExportApi.download(taskId, selected, format, expected);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `authoritative-results-${taskId}.${format}`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      setNotice(`已生成${selected.length}条结果的${format.toUpperCase()}文件。`);
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : '导出失败');
      setRefresh(value => value + 1);
    } finally {
      setDownloading(null);
      downloadBusy.current = false;
    }
  };

  return <section className="bg-white rounded-lg shadow-sm p-4 sm:p-6 space-y-4" aria-labelledby="authoritative-heading">
    <div>
      <h2 id="authoritative-heading" className="text-xl font-semibold text-gray-900">已确认结果导出</h2>
      <p className="text-sm text-gray-600 mt-2">选择评分任务和条目。仅导出已采用或锁定的结果；未结案工单会阻止下载。</p>
    </div>
    <div className="flex flex-wrap items-end gap-3">
      <label className="flex flex-col gap-1 text-sm min-w-0 flex-1 basis-full sm:basis-0">
        评分任务
        <select aria-label="评分任务" className="border rounded p-2 w-full min-w-0" value={taskId} disabled={busy || tasksLoading}
          onChange={event => { setTaskId(event.target.value); setError(''); setNotice(''); }}>
          <option value="">{tasksLoading ? '正在加载任务…' : '请选择评分任务'}</option>
          {tasks.map(task => <option key={task.task_id} value={task.task_id}>{task.batch_id} · {task.total_items}条 · {task.task_id.slice(0, 8)}</option>)}
        </select>
      </label>
      <button className="px-3 py-2 border rounded text-sm disabled:opacity-50" disabled={busy || tasksLoading}
        onClick={() => { setError(''); setNotice(''); setRefresh(value => value + 1); setTaskRefresh(value => value + 1); }}>刷新结果</button>
      <Link to="/scoring-pipeline" className="px-3 py-2 text-sm text-blue-700 underline">前往人工复核</Link>
    </div>
    {error ? <p role="alert" className="p-3 rounded bg-red-50 text-red-800 text-sm">{error}</p> : null}
    {notice ? <p role="status" className="text-green-800 text-sm">{notice}</p> : null}
    {!tasksLoading && tasks.length === 0 ? <p className="text-sm text-gray-600">暂无评分任务。请先准备材料并创建评分任务；历史综合分不作为本页导出来源。</p> : null}
    {loading ? <p role="status" className="text-sm text-gray-600">正在核对结果…</p> : null}
    {taskId && !loading ? <>
      <div className="flex flex-wrap gap-3 items-center text-sm">
        <span>已选 {selected.length} / {rows.length} 条；可导出 {readyIds.length} 条</span>
        <button className="text-blue-700 underline disabled:opacity-50" disabled={busy || readyIds.length === 0}
          onClick={() => setSelected(readyIds)}>选择可导出条目</button>
        <button className="text-gray-600 underline disabled:opacity-50" disabled={busy || selected.length === 0}
          onClick={() => setSelected([])}>清空选择</button>
      </div>
      <p className="text-xs text-gray-500">文件仅包含选中的条目，保留材料编号与结果版本；不合并旧版人工加权综合分。</p>
      {rows.length === 0 ? <p className="text-sm text-gray-600">当前没有可显示的结果条目。</p> : <div className="overflow-x-auto">
        <table className="w-full min-w-[680px] text-sm text-left">
          <thead><tr className="border-b text-gray-600"><th className="p-2">选择</th><th className="p-2">条目</th><th className="p-2">确认状态</th><th className="p-2">总分</th><th className="p-2">结果版本或待办</th></tr></thead>
          <tbody>{rows.map(row => <tr key={row.item_id} className="border-b align-top">
            <td className="p-2"><input type="checkbox" aria-label={`选择条目 ${row.item_id}`} checked={selected.includes(row.item_id)} disabled={busy || !row.exportable}
              onChange={event => setSelected(current => event.target.checked ? [...current, row.item_id] : current.filter(id => id !== row.item_id))} /></td>
            <td className="p-2 font-mono" title={row.item_id}>{row.item_id.slice(0, 8)}<span className="sr-only">{row.item_id.slice(8)}</span></td>
            <td className="p-2 whitespace-nowrap">{!row.exportable ? '待处理' : row.authority_type === 'manual_final_lock' ? '已锁定' : '已采用'}</td>
            <td className="p-2 font-semibold">{row.exportable ? row.result?.total_score ?? '—' : '—'}</td>
            <td className="p-2 break-all max-w-xs">{row.exportable ? <><span>{row.result?.snapshot_id}</span><span className="block text-xs text-gray-500">材料 {row.result?.submission_id}</span></> : <span className="text-amber-800">{blockedReason(row)}</span>}</td>
          </tr>)}</tbody>
        </table>
      </div>}
      <div className="flex flex-wrap gap-2">
        {formats.map(([format, label]) => <button key={format} className="px-4 py-2 bg-indigo-600 text-white rounded disabled:opacity-50" disabled={busy || selected.length === 0}
          onClick={() => download(format)}>{downloading === format ? '生成中…' : `导出${label}`}</button>)}
      </div>
    </> : null}
  </section>;
}
