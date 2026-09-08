/**
 * PipelineTask 只读监控台（Phase 11C-4）。
 *
 * 只展示任务状态，不提供创建/启动/暂停/恢复/重试操作。
 * 页面定位：安静、实用的运行监控台。不使用图表库、不新增依赖、不使用 emoji。
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { pipelineTaskApi } from '../services/api';
import type { PipelineEvent, PipelineItem, PipelineTask } from '../services/api';

// ---------------------------------------------------------------- 常量与文案

const REFRESH_INTERVAL_MS = 10000;

const STATUS_FILTERS = [
  { value: '', label: '全部' },
  { value: 'pending', label: '等待中' },
  { value: 'running', label: '运行中' },
  { value: 'paused', label: '已暂停' },
  { value: 'completed', label: '已完成' },
  { value: 'completed_with_errors', label: '含异常完成' },
  { value: 'failed', label: '失败' },
  { value: 'cancelled', label: '已取消' },
];

const STATUS_BADGE: Record<string, string> = {
  pending: 'inline-block px-2 py-0.5 rounded text-xs font-medium bg-gray-100 text-gray-700',
  running: 'inline-block px-2 py-0.5 rounded text-xs font-medium bg-blue-100 text-blue-700',
  paused: 'inline-block px-2 py-0.5 rounded text-xs font-medium bg-amber-100 text-amber-700',
  completed: 'inline-block px-2 py-0.5 rounded text-xs font-medium bg-green-100 text-green-700',
  completed_with_errors: 'inline-block px-2 py-0.5 rounded text-xs font-medium bg-orange-100 text-orange-700',
  failed: 'inline-block px-2 py-0.5 rounded text-xs font-medium bg-red-100 text-red-700',
  cancelled: 'inline-block px-2 py-0.5 rounded text-xs font-medium bg-gray-200 text-gray-600',
};

const STATUS_LABEL: Record<string, string> = {
  pending: '等待中',
  running: '运行中',
  paused: '已暂停',
  completed: '已完成',
  completed_with_errors: '含异常完成',
  failed: '失败',
  cancelled: '已取消',
};

const ITEM_STATUS_LABEL: Record<string, string> = {
  pending: '等待中',
  running: '运行中',
  completed: '已完成',
  failed: '失败',
  skipped: '已跳过',
  manual_review: '需人工复核',
};

const LEVEL_LABEL: Record<string, string> = {
  sufficient: '证据充分',
  limited: '证据有限',
  manual_only: '仅人工处理',
};

const STAGE_LABEL: Record<string, string> = {
  import: '导入',
  validate: '验证',
  score: '评分',
  review: '复核',
  export: '导出',
};

const STAGE_STATUS_LABEL: Record<string, string> = {
  pending: '等待中',
  running: '运行中',
  completed: '已完成',
  failed: '失败',
  skipped: '已跳过',
  not_started: '未开始',
};

const EVENT_TYPE_LABEL: Record<string, string> = {
  task_created: '任务创建',
  task_started: '任务启动',
  task_paused: '任务暂停',
  task_resumed: '任务恢复',
  task_completed: '任务完成',
  task_failed: '任务失败',
  item_started: '项启动',
  item_completed: '项完成',
  item_failed: '项失败',
  item_skipped: '项跳过',
  item_sent_to_manual_review: '转人工复核',
  lease_acquired: '心跳续租',
  resume_requested: '恢复请求',
  resume_decided: '恢复决定',
};

// 未实施阶段（score/review/export）固定展示
const UNIMPLEMENTED_STAGES = ['score', 'review', 'export'];

function fmtTime(value: string | null): string {
  if (!value) return '-';
  try {
    return new Date(value).toLocaleString('zh-CN', { hour12: false });
  } catch {
    return '-';
  }
}

function shortId(id: string | undefined | null, len = 8): string {
  if (!id) return '-';
  return id.length > len ? `${id.slice(0, len)}…` : id;
}

// ---------------------------------------------------------------- 组件

export default function PipelineTaskMonitor() {
  const [tasks, setTasks] = useState<PipelineTask[]>([]);
  const [statusFilter, setStatusFilter] = useState('');
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [lastUpdated, setLastUpdated] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [detail, setDetail] = useState<{ task: PipelineTask; items: PipelineItem[]; events: PipelineEvent[] } | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const inFlight = useRef(false);

  const fetchTasks = useCallback(async () => {
    if (inFlight.current) return; // 上一次请求未结束时不得叠加请求
    inFlight.current = true;
    setLoading(true);
    try {
      const resp = await pipelineTaskApi.listTasks({ status: statusFilter || undefined, limit: 100 });
      setTasks(resp.items);
      setLastUpdated(new Date().toLocaleTimeString('zh-CN', { hour12: false }));
      setError(null);
    } catch (e) {
      // 自动刷新失败不得清空已有数据；仅温和提示
      setError(e instanceof Error ? '加载失败，请检查服务是否可用' : '加载失败');
      // 不抛未处理 Promise
    } finally {
      inFlight.current = false;
      setLoading(false);
    }
  }, [statusFilter]);

  const fetchDetail = useCallback(async (taskId: string) => {
    setDetailLoading(true);
    setDetailError(null);
    try {
      const [task, itemsResp, eventsResp] = await Promise.all([
        pipelineTaskApi.getTask(taskId),
        pipelineTaskApi.listItems(taskId),
        pipelineTaskApi.getEvents(taskId),
      ]);
      const items = itemsResp.items.sort((a, b) => (a.item_id < b.item_id ? -1 : 1));
      const events = eventsResp.items.sort((a, b) => a.sequence - b.sequence);
      setDetail({ task, items, events });
    } catch (e) {
      setDetailError('详情加载失败，请稍后重试');
    } finally {
      setDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchTasks();
  }, [fetchTasks]);

  useEffect(() => {
    if (!autoRefresh) return;
    const timer = window.setInterval(() => {
      fetchTasks();
    }, REFRESH_INTERVAL_MS);
    return () => window.clearInterval(timer); // 页面卸载时清理定时器
  }, [autoRefresh, fetchTasks]);

  const handleSelectTask = (taskId: string) => {
    if (selectedTaskId === taskId) {
      setSelectedTaskId(null);
      setDetail(null);
      return;
    }
    setSelectedTaskId(taskId);
    fetchDetail(taskId);
  };

  const showEmpty = !loading && !error && tasks.length === 0;

  return (
    <div className="space-y-6" data-testid="pipeline-task-monitor">
      {/* 顶部工具栏 */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-xl font-bold text-teal-700" data-testid="pipeline-tasks-title">流水线任务</h2>
        <span className="text-sm text-gray-500" data-testid="pipeline-scope-hint">当前范围：证据导入与验证</span>
      </div>

      <div className="flex flex-wrap items-center gap-3 bg-white rounded-lg shadow p-3">
        <label className="text-sm text-gray-600" htmlFor="pipeline-status-filter">状态</label>
        <select
          id="pipeline-status-filter"
          data-testid="pipeline-status-filter"
          className="border border-gray-300 rounded px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-teal-500"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
        >
          {STATUS_FILTERS.map((f) => (
            <option key={f.value || 'all'} value={f.value}>{f.label}</option>
          ))}
        </select>
        <button
          type="button"
          data-testid="pipeline-refresh-button"
          className="px-3 py-1 rounded text-sm font-medium bg-teal-600 text-white hover:bg-teal-700 disabled:opacity-50"
          onClick={() => fetchTasks()}
          disabled={loading}
        >
          刷新
        </button>
        <label className="flex items-center gap-2 text-sm text-gray-600">
          <input
            type="checkbox"
            data-testid="pipeline-auto-refresh-toggle"
            className="h-4 w-4 rounded border-gray-300 text-teal-600 focus:ring-teal-500"
            checked={autoRefresh}
            onChange={(e) => setAutoRefresh(e.target.checked)}
          />
          自动刷新（10 秒）
        </label>
        <span className="ml-auto text-xs text-gray-400" data-testid="pipeline-last-updated">
          {lastUpdated ? `最近更新：${lastUpdated}` : '尚未加载'}
        </span>
      </div>

      {/* 错误态：保留页面结构，不白屏 */}
      {error && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-4 text-sm text-red-700" data-testid="pipeline-error-state">
          <p>{error}</p>
          <button
            type="button"
            className="mt-2 px-3 py-1 rounded text-sm font-medium bg-red-600 text-white hover:bg-red-700"
            onClick={() => fetchTasks()}
          >
            重试
          </button>
        </div>
      )}

      {/* 空态 */}
      {showEmpty && (
        <div className="bg-white rounded-lg shadow p-8 text-center text-gray-500" data-testid="pipeline-empty-state">
          暂无流水线任务
        </div>
      )}

      {/* 任务列表 */}
      {!showEmpty && (
        <div className="bg-white rounded-lg shadow overflow-x-auto">
          <table className="min-w-full divide-y divide-gray-200 text-sm" data-testid="pipeline-task-table">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-3 py-2 text-left text-xs font-semibold text-gray-600">批次</th>
                <th className="px-3 py-2 text-left text-xs font-semibold text-gray-600">任务 ID</th>
                <th className="px-3 py-2 text-left text-xs font-semibold text-gray-600">状态</th>
                <th className="px-3 py-2 text-left text-xs font-semibold text-gray-600">当前阶段</th>
                <th className="px-3 py-2 text-left text-xs font-semibold text-gray-600">范围</th>
                <th className="px-3 py-2 text-left text-xs font-semibold text-gray-600">计数</th>
                <th className="px-3 py-2 text-left text-xs font-semibold text-gray-600">创建时间</th>
                <th className="px-3 py-2 text-left text-xs font-semibold text-gray-600">更新时间</th>
                <th className="px-3 py-2 text-left text-xs font-semibold text-gray-600">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {tasks.map((t) => (
                <tr key={t.task_id} className="hover:bg-gray-50" data-testid="pipeline-task-row">
                  <td className="px-3 py-2 text-gray-800">{t.batch_id}</td>
                  <td className="px-3 py-2 font-mono text-gray-700">{shortId(t.task_id)}</td>
                  <td className="px-3 py-2">
                    <span className={STATUS_BADGE[t.status] || STATUS_BADGE.pending}>
                      {STATUS_LABEL[t.status] || t.status}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-gray-700">{t.current_stage ? STAGE_LABEL[t.current_stage] || t.current_stage : '-'}</td>
                  <td className="px-3 py-2 text-gray-700">{t.execution_scope.join(' / ')}</td>
                  <td className="px-3 py-2 text-gray-700">
                    {t.completed_items}/{t.total_items} 完成
                    {t.failed_items > 0 ? `，${t.failed_items} 失败` : ''}
                    {t.manual_review_items > 0 ? `，${t.manual_review_items} 人工` : ''}
                  </td>
                  <td className="px-3 py-2 text-gray-600">{fmtTime(t.created_at)}</td>
                  <td className="px-3 py-2 text-gray-600">{fmtTime(t.updated_at)}</td>
                  <td className="px-3 py-2">
                    <button
                      type="button"
                      data-testid={`pipeline-detail-${t.task_id}`}
                      className="px-2 py-1 rounded text-xs font-medium bg-gray-100 text-gray-700 hover:bg-gray-200"
                      onClick={() => handleSelectTask(t.task_id)}
                    >
                      {selectedTaskId === t.task_id ? '收起' : '查看详情'}
                    </button>
                  </td>
                </tr>
              ))}
              {tasks.length === 0 && !loading && (
                <tr>
                  <td colSpan={9} className="px-3 py-6 text-center text-gray-500">
                    当前筛选条件下暂无流水线任务
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {/* 任务详情（同页展开） */}
      {selectedTaskId && (
        <div className="bg-white rounded-lg shadow p-5 space-y-5" data-testid="pipeline-task-detail">
          {detailLoading && <p className="text-sm text-gray-500">加载详情中…</p>}
          {detailError && (
            <div className="text-sm text-red-700">
              <p>{detailError}</p>
              <button
                type="button"
                className="mt-2 px-3 py-1 rounded text-sm font-medium bg-red-600 text-white hover:bg-red-700"
                onClick={() => fetchDetail(selectedTaskId)}
              >
                重试
              </button>
            </div>
          )}
          {detail && !detailLoading && !detailError && <TaskDetail detail={detail} />}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- 详情子组件

function TaskDetail({ detail }: { detail: { task: PipelineTask; items: PipelineItem[]; events: PipelineEvent[] } }) {
  const { task, items, events } = detail;
  const isCompleted = task.status === 'completed' || task.status === 'completed_with_errors';

  return (
    <div className="space-y-5">
      {/* 基本信息 */}
      <div className="flex flex-wrap items-center gap-3">
        <h3 className="text-lg font-semibold text-gray-800">任务详情</h3>
        <span className={STATUS_BADGE[task.status] || STATUS_BADGE.pending}>
          {STATUS_LABEL[task.status] || task.status}
        </span>
        <span className="text-sm text-gray-500" data-testid="pipeline-detail-scope">当前范围：证据导入与验证</span>
        <span className="ml-auto text-sm text-gray-500">
          {isCompleted ? '当前流水线范围已完成' : '证据导入与验证进行中'}
        </span>
      </div>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-sm">
        <div><span className="text-gray-500">批次：</span><span className="text-gray-800">{task.batch_id}</span></div>
        <div><span className="text-gray-500">任务 ID：</span><span className="font-mono text-gray-800">{task.task_id}</span></div>
        <div><span className="text-gray-500">范围：</span><span className="text-gray-800">{task.execution_scope.join(' / ')}</span></div>
        <div><span className="text-gray-500">并发：</span><span className="text-gray-800">{task.concurrency}</span></div>
      </div>

      {/* 五阶段状态 */}
      <div>
        <h4 className="text-sm font-semibold text-gray-700 mb-2">阶段状态</h4>
        <div className="grid grid-cols-1 md:grid-cols-5 gap-2" data-testid="pipeline-stage-summaries">
          {['import', 'validate', 'score', 'review', 'export'].map((stage) => {
            const s = task.stage_summaries.find((x) => x.stage === stage);
            if (!s) return null;
            const unimplemented = UNIMPLEMENTED_STAGES.includes(stage);
            return (
              <div key={stage} className="border border-gray-200 rounded p-2 text-sm" data-testid={`pipeline-stage-${stage}`}>
                <div className="flex items-center justify-between">
                  <span className="font-medium text-gray-700">{STAGE_LABEL[stage] || stage}</span>
                  {unimplemented ? (
                    <span className="inline-block px-1.5 py-0.5 rounded text-xs bg-gray-100 text-gray-500">未执行</span>
                  ) : (
                    <span className="inline-block px-1.5 py-0.5 rounded text-xs bg-teal-50 text-teal-700">
                      {STAGE_STATUS_LABEL[s.status] || s.status}
                    </span>
                  )}
                </div>
                {unimplemented ? (
                  <p className="mt-1 text-xs text-gray-400">当前版本未执行</p>
                ) : (
                  <p className="mt-1 text-xs text-gray-500">
                    {s.completed_items}/{s.total_items} 完成{s.failed_items > 0 ? `，${s.failed_items} 失败` : ''}
                    {s.manual_review_items > 0 ? `，${s.manual_review_items} 人工` : ''}
                  </p>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* item 列表 */}
      <div>
        <h4 className="text-sm font-semibold text-gray-700 mb-2">项列表（{items.length}）</h4>
        <div className="overflow-x-auto border border-gray-200 rounded">
          <table className="min-w-full divide-y divide-gray-200 text-sm">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-2 py-1.5 text-left text-xs font-semibold text-gray-600">项 ID</th>
                <th className="px-2 py-1.5 text-left text-xs font-semibold text-gray-600">材料包</th>
                <th className="px-2 py-1.5 text-left text-xs font-semibold text-gray-600">等级</th>
                <th className="px-2 py-1.5 text-left text-xs font-semibold text-gray-600">状态</th>
                <th className="px-2 py-1.5 text-left text-xs font-semibold text-gray-600">阶段</th>
                <th className="px-2 py-1.5 text-left text-xs font-semibold text-gray-600">尝试</th>
                <th className="px-2 py-1.5 text-left text-xs font-semibold text-gray-600">心跳</th>
                <th className="px-2 py-1.5 text-left text-xs font-semibold text-gray-600">输出</th>
                <th className="px-2 py-1.5 text-left text-xs font-semibold text-gray-600">原因</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {items.map((it) => (
                <tr key={it.item_id} className="hover:bg-gray-50" data-testid="pipeline-item-row">
                  <td className="px-2 py-1.5 font-mono text-gray-700">{shortId(it.item_id)}</td>
                  <td className="px-2 py-1.5 text-gray-700">{it.package_id} / {it.package_revision}</td>
                  <td className="px-2 py-1.5 text-gray-700">{LEVEL_LABEL[it.evidence_level] || it.evidence_level}</td>
                  <td className="px-2 py-1.5">
                    <span className={STATUS_BADGE[it.status] || STATUS_BADGE.pending}>
                      {ITEM_STATUS_LABEL[it.status] || it.status}
                    </span>
                  </td>
                  <td className="px-2 py-1.5 text-gray-700">{STAGE_LABEL[it.current_stage] || it.current_stage}</td>
                  <td className="px-2 py-1.5 text-gray-700">{it.attempt_count}/{it.max_attempts}</td>
                  <td className="px-2 py-1.5 text-gray-600">{fmtTime(it.heartbeat_updated_at)}</td>
                  <td className="px-2 py-1.5 text-gray-700">{it.output.present ? '有' : '无'}</td>
                  <td className="px-2 py-1.5 text-gray-600">
                    {it.status === 'manual_review'
                      ? (it.last_error ? it.last_error.message_key : '需人工复核')
                      : (it.last_error ? it.last_error.code : '-')}
                  </td>
                </tr>
              ))}
              {items.length === 0 && (
                <tr>
                  <td colSpan={9} className="px-2 py-4 text-center text-gray-500">暂无项</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* 事件时间线 */}
      <div>
        <h4 className="text-sm font-semibold text-gray-700 mb-2">事件时间线（{events.length}）</h4>
        <div className="border border-gray-200 rounded overflow-x-auto">
          <table className="min-w-full divide-y divide-gray-200 text-sm">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-2 py-1.5 text-left text-xs font-semibold text-gray-600">序号</th>
                <th className="px-2 py-1.5 text-left text-xs font-semibold text-gray-600">时间</th>
                <th className="px-2 py-1.5 text-left text-xs font-semibold text-gray-600">事件</th>
                <th className="px-2 py-1.5 text-left text-xs font-semibold text-gray-600">阶段</th>
                <th className="px-2 py-1.5 text-left text-xs font-semibold text-gray-600">项</th>
                <th className="px-2 py-1.5 text-left text-xs font-semibold text-gray-600">原因码</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {events.map((ev) => (
                <tr key={ev.event_id} data-testid="pipeline-event-row">
                  <td className="px-2 py-1.5 text-gray-500">{ev.sequence}</td>
                  <td className="px-2 py-1.5 text-gray-600">{fmtTime(ev.occurred_at)}</td>
                  <td className="px-2 py-1.5 text-gray-700">{EVENT_TYPE_LABEL[ev.event_type] || ev.event_type}</td>
                  <td className="px-2 py-1.5 text-gray-600">{ev.stage ? STAGE_LABEL[ev.stage] || ev.stage : '-'}</td>
                  <td className="px-2 py-1.5 font-mono text-gray-600">{shortId(ev.item_id)}</td>
                  <td className="px-2 py-1.5 text-gray-600">{ev.reason_code || '-'}</td>
                </tr>
              ))}
              {events.length === 0 && (
                <tr>
                  <td colSpan={6} className="px-2 py-4 text-center text-gray-500">暂无事件</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
