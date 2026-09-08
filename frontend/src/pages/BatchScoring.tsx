/**
 * 批量评分管理页面（纯 AI 批量评分模式）
 * - 启动批量评分任务
 * - 查看任务列表与进度
 * - 导出评分结果为 CSV
 */
import { useEffect, useState, useCallback } from 'react';
import { batchScoringApi, scoreApi } from '../services/api';

interface BatchTask {
  task_id: string;
  status: string;
  total: number;
  completed: number;
  failed: number;
  started_at: string;
  completed_at: string | null;
}

interface BatchResultItem {
  team_id: number;
  team_name: string;
  short_code: string;
  group_type: string;
  school: string;
  model_name: string;
  scores: {
    theme: number;
    presentation: number;
    process: number;
    ai_literacy: number;
    total: number;
  };
  calibrated_score: number;
  anchor_level: string;
  confidence: string;
  flags: Array<{ code: string; level: string; msg: string }>;
  comments: {
    theme: string;
    presentation: string;
    process: string;
    ai_literacy: string;
    overall: string;
  };
  defense_questions: string[];
  created_at: string;
}

const AVAILABLE_MODELS = [
  { key: 'qwen3.8-max', name: '千问 Qwen3.8 Max', description: '图像+文本，旗舰质量，适合深度评分' },
  { key: 'qwen3.8-flash', name: '千问 Qwen3.8 Flash', description: '图像+文本，快速响应，适合快速初评' },
  { key: 'k3', name: 'Kimi K3', description: '图像+视频+文本，适合长材料综合判断' },
  { key: 'glm-5v-turbo', name: '智谱 GLM-5V-Turbo', description: '兼顾视觉理解与代码分析' },
  { key: 'doubao-seed-2.1-turbo', name: '豆包 Seed 2.1 Turbo', description: '适合高并发批量评审' },
  { key: 'MiniMax-M3', name: 'MiniMax M3', description: '原生多模态，适合长上下文作品分析' },
];

export default function BatchScoring() {
  const [model, setModel] = useState('qwen3.8-max');
  const [parallel, setParallel] = useState(true);
  const [concurrency, setConcurrency] = useState(5);
  const [starting, setStarting] = useState(false);
  const [startMessage, setStartMessage] = useState('');

  const [tasks, setTasks] = useState<BatchTask[]>([]);
  const [tasksLoading, setTasksLoading] = useState(false);

  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [results, setResults] = useState<BatchResultItem[]>([]);
  const [resultsLoading, setResultsLoading] = useState(false);
  const [resultsStatus, setResultsStatus] = useState<BatchTask | null>(null);

  const [pollInterval, setPollInterval] = useState<ReturnType<typeof setInterval> | null>(null);

  const loadTasks = useCallback(async () => {
    setTasksLoading(true);
    try {
      const data = await batchScoringApi.listTasks() as any;
      setTasks(data.tasks || []);
    } catch (err) {
      console.error('加载任务列表失败', err);
    }
    setTasksLoading(false);
  }, []);

  useEffect(() => {
    loadTasks();
    const interval = setInterval(loadTasks, 5000);
    return () => clearInterval(interval);
  }, [loadTasks]);

  useEffect(() => {
    if (selectedTaskId) {
      loadResults(selectedTaskId);
      const interval = setInterval(() => loadResults(selectedTaskId), 3000);
      setPollInterval(interval);
      return () => clearInterval(interval);
    } else {
      if (pollInterval) {
        clearInterval(pollInterval);
        setPollInterval(null);
      }
    }
  }, [selectedTaskId]);

  const handleStart = async () => {
    if (!confirm(`确认启动批量评分？\n模型：${model}\n并发：${parallel ? concurrency : 1}`)) return;

    setStarting(true);
    setStartMessage('');
    try {
      const res = await batchScoringApi.start({
        model,
        parallel,
        concurrency: Math.max(1, Math.min(20, concurrency)),
      }) as any;
      if (res.task_id) {
        setStartMessage(`任务已启动，ID: ${res.task_id}`);
        loadTasks();
      } else {
        setStartMessage(res.message || '启动失败');
      }
    } catch (err: any) {
      setStartMessage(err.message || '启动失败');
    }
    setStarting(false);
  };

  const loadResults = async (taskId: string) => {
    setResultsLoading(true);
    try {
      const data = await batchScoringApi.getResults(taskId) as any;
      setResults(data.results || []);
      setResultsStatus({
        task_id: data.task_id,
        status: data.status,
        total: data.progress?.total || 0,
        completed: data.progress?.completed || 0,
        failed: data.progress?.failed || 0,
        started_at: '',
        completed_at: null,
      });
    } catch (err) {
      console.error('加载结果失败', err);
    }
    setResultsLoading(false);
  };

  const exportCSV = () => {
    if (!results.length) return;

    const headers = [
      '编号', '队伍名称', '组别', '学校',
      '主题立意', '产品表现力', '过程完整性', 'AI素养', '总分',
      '校准分', '锚点等级', '置信度', '模型',
      '总评语', '标记',
    ];

    const rows = results.map((r) => [
      r.short_code,
      r.team_name,
      r.group_type,
      r.school,
      r.scores.theme,
      r.scores.presentation,
      r.scores.process,
      r.scores.ai_literacy,
      r.scores.total,
      r.calibrated_score,
      r.anchor_level,
      r.confidence,
      r.model_name || '',
      (r.comments.overall || '').replace(/\n/g, ' '),
      r.flags.map((f) => `${f.code}(${f.level})`).join('; '),
    ]);

    const csvContent = [
      headers.join(','),
      ...rows.map((row) =>
        row
          .map((cell) => {
            const str = String(cell ?? '');
            if (str.includes(',') || str.includes('\n') || str.includes('"')) {
              return `"${str.replace(/"/g, '""')}"`;
            }
            return str;
          })
          .join(',')
      ),
    ].join('\n');

    const blob = new Blob(['\ufeff' + csvContent], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `batch_scoring_${selectedTaskId}_${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  };

  const statusBadge = (status: string) => {
    const map: Record<string, string> = {
      pending: 'bg-yellow-100 text-yellow-700',
      running: 'bg-blue-100 text-blue-700',
      completed: 'bg-green-100 text-green-700',
      failed: 'bg-red-100 text-red-700',
    };
    const labelMap: Record<string, string> = {
      pending: '等待中',
      running: '运行中',
      completed: '已完成',
      failed: '失败',
    };
    return (
      <span className={`px-2 py-1 rounded text-xs font-medium ${map[status] || 'bg-gray-100 text-gray-700'}`}>
        {labelMap[status] || status}
      </span>
    );
  };

  return (
    <div className="space-y-6">
      <h2 className="text-xl font-bold text-teal-700">批量评分管理</h2>

      {/* 启动批量评分 */}
      <div className="bg-white rounded-lg shadow p-6">
        <h3 className="text-lg font-semibold text-gray-800 mb-4">启动批量评分</h3>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">选择模型</label>
            <select
              value={model}
              onChange={(e) => setModel(e.target.value)}
              className="w-full border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-teal-500"
            >
              {AVAILABLE_MODELS.map((m) => (
                <option key={m.key} value={m.key}>
                  {m.name}
                </option>
              ))}
            </select>
            <p className="text-xs text-gray-500 mt-1">
              {AVAILABLE_MODELS.find((m) => m.key === model)?.description}
            </p>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">并发数</label>
            <div className="flex items-center gap-3">
              <input
                type="checkbox"
                checked={parallel}
                onChange={(e) => setParallel(e.target.checked)}
                className="w-4 h-4 text-teal-600"
              />
              <span className="text-sm text-gray-700">启用并行</span>
            </div>
            {parallel && (
              <input
                type="number"
                min={1}
                max={20}
                value={concurrency}
                onChange={(e) => setConcurrency(parseInt(e.target.value) || 1)}
                className="mt-2 w-full border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-teal-500"
              />
            )}
          </div>

          <div className="flex items-end">
            <button
              onClick={handleStart}
              disabled={starting}
              className="w-full bg-teal-600 text-white px-4 py-2 rounded hover:bg-teal-700 disabled:bg-gray-400 text-sm font-medium"
            >
              {starting ? '启动中...' : '启动批量评分'}
            </button>
          </div>
        </div>
        {startMessage && (
          <p className={`mt-3 text-sm ${startMessage.includes('失败') || startMessage.includes('不存在') ? 'text-red-600' : 'text-green-600'}`}>
            {startMessage}
          </p>
        )}
      </div>

      {/* 任务列表 */}
      <div className="bg-white rounded-lg shadow p-6">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-semibold text-gray-800">批量评分任务</h3>
          <button
            onClick={loadTasks}
            disabled={tasksLoading}
            className="text-sm text-teal-600 hover:text-teal-700"
          >
            {tasksLoading ? '刷新中...' : '刷新'}
          </button>
        </div>

        {tasks.length === 0 ? (
          <p className="text-gray-500 text-sm">暂无批量评分任务</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full text-sm">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-3 py-2 text-left font-medium text-gray-600">任务ID</th>
                  <th className="px-3 py-2 text-left font-medium text-gray-600">状态</th>
                  <th className="px-3 py-2 text-left font-medium text-gray-600">进度</th>
                  <th className="px-3 py-2 text-left font-medium text-gray-600">成功/失败</th>
                  <th className="px-3 py-2 text-left font-medium text-gray-600">开始时间</th>
                  <th className="px-3 py-2 text-left font-medium text-gray-600">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {tasks.map((task) => (
                  <tr
                    key={task.task_id}
                    className={`hover:bg-gray-50 cursor-pointer ${selectedTaskId === task.task_id ? 'bg-blue-50' : ''}`}
                    onClick={() => setSelectedTaskId(task.task_id)}
                  >
                    <td className="px-3 py-2 font-mono text-gray-700">{task.task_id}</td>
                    <td className="px-3 py-2">{statusBadge(task.status)}</td>
                    <td className="px-3 py-2">
                      <div className="w-32 bg-gray-200 rounded-full h-2">
                        <div
                          className="bg-teal-500 h-2 rounded-full transition-all"
                          style={{
                            width: task.total > 0 ? `${((task.completed + task.failed) / task.total) * 100}%` : '0%',
                          }}
                        />
                      </div>
                      <span className="text-xs text-gray-500">
                        {task.completed + task.failed}/{task.total}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-xs">
                      <span className="text-green-600">{task.completed}</span>
                      <span className="text-gray-400 mx-1">/</span>
                      <span className="text-red-600">{task.failed}</span>
                    </td>
                    <td className="px-3 py-2 text-gray-500 text-xs">
                      {task.started_at ? new Date(task.started_at).toLocaleString() : '-'}
                    </td>
                    <td className="px-3 py-2">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          setSelectedTaskId(task.task_id);
                        }}
                        className="text-xs text-teal-600 hover:underline"
                      >
                        查看结果
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* 结果详情 */}
      {selectedTaskId && (
        <div className="bg-white rounded-lg shadow p-6">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-lg font-semibold text-gray-800">
              任务结果 <span className="font-mono text-sm text-gray-500">{selectedTaskId}</span>
            </h3>
            <div className="flex items-center gap-3">
              {resultsStatus && (
                <span className="text-sm text-gray-600">
                  进度: {resultsStatus.completed + resultsStatus.failed}/{resultsStatus.total}
                  {resultsStatus.status === 'running' && ' (实时更新)'}
                </span>
              )}
              <button
                onClick={exportCSV}
                disabled={results.length === 0}
                className="bg-blue-600 text-white px-3 py-1.5 rounded text-sm hover:bg-blue-700 disabled:bg-gray-400"
              >
                导出 CSV
              </button>
            </div>
          </div>

          {resultsLoading && results.length === 0 ? (
            <p className="text-gray-500 text-sm">加载中...</p>
          ) : results.length === 0 ? (
            <p className="text-gray-500 text-sm">暂无评分结果</p>
          ) : (
            <div className="overflow-x-auto max-h-[600px]">
              <table className="min-w-full text-sm">
                <thead className="bg-gray-50 sticky top-0">
                  <tr>
                    <th className="px-3 py-2 text-left font-medium text-gray-600">编号</th>
                    <th className="px-3 py-2 text-left font-medium text-gray-600">队伍</th>
                    <th className="px-3 py-2 text-left font-medium text-gray-600">组别</th>
                    <th className="px-3 py-2 text-left font-medium text-gray-600">主题</th>
                    <th className="px-3 py-2 text-left font-medium text-gray-600">表现力</th>
                    <th className="px-3 py-2 text-left font-medium text-gray-600">过程</th>
                    <th className="px-3 py-2 text-left font-medium text-gray-600">AI素养</th>
                    <th className="px-3 py-2 text-left font-medium text-gray-600">总分</th>
                    <th className="px-3 py-2 text-left font-medium text-gray-600">校准分</th>
                    <th className="px-3 py-2 text-left font-medium text-gray-600">锚点</th>
                    <th className="px-3 py-2 text-left font-medium text-gray-600">标记</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {results.map((r) => (
                    <tr key={r.team_id} className="hover:bg-gray-50">
                      <td className="px-3 py-2 font-mono text-gray-700">{r.short_code}</td>
                      <td className="px-3 py-2 text-gray-800">{r.team_name}</td>
                      <td className="px-3 py-2 text-gray-600">{r.group_type}</td>
                      <td className="px-3 py-2">{r.scores.theme}</td>
                      <td className="px-3 py-2">{r.scores.presentation}</td>
                      <td className="px-3 py-2">{r.scores.process}</td>
                      <td className="px-3 py-2">{r.scores.ai_literacy}</td>
                      <td className="px-3 py-2 font-semibold text-teal-700">{r.scores.total}</td>
                      <td className="px-3 py-2">{r.calibrated_score}</td>
                      <td className="px-3 py-2 text-xs">
                        <span
                          className={`px-1.5 py-0.5 rounded ${
                            r.anchor_level === 'Lv3'
                              ? 'bg-purple-100 text-purple-700'
                              : r.anchor_level === 'Lv2'
                              ? 'bg-blue-100 text-blue-700'
                              : r.anchor_level === 'Lv1'
                              ? 'bg-green-100 text-green-700'
                              : 'bg-gray-100 text-gray-600'
                          }`}
                        >
                          {r.anchor_level}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-xs">
                        {r.flags?.length > 0 ? (
                          <span className="text-orange-600">
                            {r.flags.length} 条标记
                          </span>
                        ) : (
                          <span className="text-gray-400">-</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
