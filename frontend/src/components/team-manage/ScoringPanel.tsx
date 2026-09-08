import React from 'react';
import type { DimensionConfig } from '../../services/api';

interface ScoringPanelProps {
  scoringInProgress: boolean;
  scoringResult: any;
  scoringStats: any;
  selectedModel: string;
  availableModels: any[];
  parallelEnabled: boolean;
  concurrency: number;
  dimensions: Record<string, DimensionConfig>;
  batchTasks: any[];
  onRefreshTasks: () => void;
  onStartScoring: () => void;
  onAdoptBestScore: () => void;
  onModelChange: (modelId: string) => void;
  onToggleParallel: () => void;
  onConcurrencyChange: (n: number) => void;
}

const ScoringPanel: React.FC<ScoringPanelProps> = ({
  scoringInProgress,
  scoringResult,
  scoringStats,
  selectedModel,
  availableModels,
  parallelEnabled,
  concurrency,
  dimensions,
  batchTasks,
  onRefreshTasks,
  onStartScoring,
  onAdoptBestScore,
  onModelChange,
  onToggleParallel,
  onConcurrencyChange,
}) => {
  return (
    <div className="bg-white p-6 rounded-lg shadow-sm space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-base font-medium text-gray-800">AI自动评分</h3>
        <div className="flex gap-2">
          <button
            onClick={onAdoptBestScore}
            className="px-3 py-2 bg-teal-600 text-white text-sm rounded hover:bg-teal-700"
          >
            全局最优采纳
          </button>
          <button
            onClick={onStartScoring}
            disabled={scoringInProgress}
            className="px-4 py-2 bg-indigo-600 text-white rounded hover:bg-indigo-700 disabled:opacity-50"
          >
            {scoringInProgress ? '评分中...' : '开始评分'}
          </button>
        </div>
      </div>

      {/* 模型选择和并行配置 */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        {/* 模型选择 */}
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">LLM模型</label>
          <select
            value={selectedModel}
            onChange={e => onModelChange(e.target.value)}
            disabled={scoringInProgress}
            className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 disabled:bg-gray-100"
          >
            {availableModels.map(m => (
              <option key={m.id} value={m.id}>
                {m.name}
              </option>
            ))}
          </select>
          {/* 显示当前选中模型的详细信息 */}
          {selectedModel && availableModels.length > 0 && (
            <div className="mt-2 p-3 bg-blue-50 rounded-lg text-sm text-blue-700">
              {availableModels.find(m => m.id === selectedModel)?.description}
            </div>
          )}
        </div>

        {/* 并行开关 */}
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">并行评分</label>
          <div className="flex items-center gap-3 mt-1">
            <button
              onClick={onToggleParallel}
              disabled={scoringInProgress}
              className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
                parallelEnabled ? 'bg-indigo-600' : 'bg-gray-300'
              } disabled:opacity-50`}
            >
              <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                parallelEnabled ? 'translate-x-6' : 'translate-x-1'
              }`} />
            </button>
            <span className="text-sm text-gray-600">{parallelEnabled ? '已开启' : '已关闭'}</span>
          </div>
        </div>

        {/* 并发数 */}
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">并发数</label>
          <select
            value={concurrency}
            onChange={e => onConcurrencyChange(Number(e.target.value))}
            disabled={scoringInProgress || !parallelEnabled}
            className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 disabled:bg-gray-100"
          >
            {[3, 5, 8, 10, 15].map(n => (
              <option key={n} value={n}>{n} 个并发</option>
            ))}
          </select>
        </div>
      </div>

      {/* 当前模型说明 */}
      {availableModels.length > 0 && (
        <div className="bg-blue-50 p-3 rounded-lg text-sm text-blue-700">
          当前模型: <span className="font-medium">{availableModels.find(m => m.id === selectedModel)?.name}</span>
          {' - '}
          {availableModels.find(m => m.id === selectedModel)?.description}
          {parallelEnabled && ` | 并发${concurrency}个，预计提速${concurrency}倍`}
        </div>
      )}

      {/* 评分统计 */}
      {scoringStats && (
        <div className="space-y-4">
          <div className="grid grid-cols-5 gap-3">
            <div className="bg-indigo-50 p-4 rounded-lg text-center">
              <div className="text-2xl font-bold text-indigo-600">{scoringStats.progress}</div>
              <div className="text-sm text-indigo-500">评分进度</div>
            </div>
            <div className="bg-green-50 p-4 rounded-lg text-center">
              <div className="text-2xl font-bold text-green-600">{scoringStats.avg_scores?.total || 0}</div>
              <div className="text-sm text-green-500">平均总分</div>
            </div>
            {Object.entries(dimensions).map(([key, dim]) => (
              <div key={key} className="bg-blue-50 p-4 rounded-lg text-center">
                <div className="text-2xl font-bold text-blue-600">{scoringStats.avg_scores?.[key] || 0}</div>
                <div className="text-sm text-blue-500">{dim.name}/{dim.max_score}</div>
              </div>
            ))}
            {Object.keys(dimensions).length === 0 && (
              <>
                <div className="bg-blue-50 p-4 rounded-lg text-center">
                  <div className="text-2xl font-bold text-blue-600">{scoringStats.avg_scores?.theme || 0}</div>
                  <div className="text-sm text-blue-500">主题立意</div>
                </div>
                <div className="bg-purple-50 p-4 rounded-lg text-center">
                  <div className="text-2xl font-bold text-purple-600">{scoringStats.avg_scores?.presentation || 0}</div>
                  <div className="text-sm text-purple-500">产品表现力</div>
                </div>
                <div className="bg-orange-50 p-4 rounded-lg text-center">
                  <div className="text-2xl font-bold text-orange-600">{scoringStats.avg_scores?.process || 0}</div>
                  <div className="text-sm text-orange-500">过程完整性</div>
                </div>
              </>
            )}
          </div>
          <div className="grid grid-cols-2 gap-3">
            {Object.keys(dimensions).length === 0 && (
              <div className="bg-pink-50 p-3 rounded-lg flex justify-between items-center">
                <span className="text-sm text-pink-600">AI素养平均分</span>
                <span className="font-bold text-pink-700">{scoringStats.avg_scores?.ai_literacy || 0}</span>
              </div>
            )}
            <div className="bg-gray-50 p-3 rounded-lg flex justify-between items-center">
              <span className="text-sm text-gray-600">已评队伍数</span>
              <span className="font-bold text-gray-700">{scoringStats.scored_teams}</span>
            </div>
          </div>
        </div>
      )}

      {/* 评分维度说明 */}
      <div className="bg-gray-50 p-4 rounded-lg text-sm">
        <div className="font-medium text-gray-700 mb-2">评分维度（总分100分）</div>
        {Object.keys(dimensions).length > 0 ? (
          <div className="grid grid-cols-2 gap-2 text-gray-600">
            {Object.entries(dimensions).map(([key, dim]) => (
              <div key={key}>• {dim.name}（{dim.max_score}分）：{dim.description || ''}</div>
            ))}
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-2 text-gray-600">
            <div>• 主题立意：创意特征、功能多样性</div>
            <div>• 产品表现力：代码质量、用户体验</div>
            <div>• 过程完整性：AIGC交互、迭代深度</div>
            <div>• AI素养：工具策略、问题质量</div>
          </div>
        )}
      </div>

      {/* 评分进度条 */}
      {scoringInProgress && scoringResult && (
        <div className="bg-indigo-50 border border-indigo-200 rounded-lg p-4">
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm font-medium text-indigo-700">
              评分进行中...
            </span>
            <span className="text-sm text-indigo-600">
              {scoringResult.success + scoringResult.failed} / {scoringResult.total}
            </span>
          </div>
          <div className="w-full bg-indigo-200 rounded-full h-3">
            <div
              className="bg-indigo-600 h-3 rounded-full transition-all duration-300"
              style={{ width: `${((scoringResult.success + scoringResult.failed) / scoringResult.total) * 100}%` }}
            />
          </div>
          <div className="flex justify-between text-xs text-indigo-600 mt-1">
            <span>成功: {scoringResult.success}</span>
            <span>失败: {scoringResult.failed}</span>
          </div>
        </div>
      )}

      {/* 评分完成状态 */}
      {scoringResult && !scoringInProgress && scoringResult.status === 'completed' && (
        <div className="bg-green-50 border border-green-200 rounded-lg p-4">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 bg-green-500 rounded-full flex items-center justify-center">
              <svg className="w-6 h-6 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
              </svg>
            </div>
            <div>
              <div className="font-bold text-green-700">AI评分完成！</div>
              <div className="text-sm text-green-600">
                成功 {scoringResult.success} 个 / 失败 {scoringResult.failed} 个 / 总计 {scoringResult.total} 个
              </div>
            </div>
          </div>
          {scoringResult.errors && scoringResult.errors.length > 0 && (
            <div className="mt-3 max-h-32 overflow-y-auto text-xs text-red-600 bg-white p-2 rounded">
              {scoringResult.errors.slice(0, 5).map((err: string, idx: number) => (
                <div key={idx}>• {err}</div>
              ))}
              {scoringResult.errors.length > 5 && (
                <div className="text-gray-500">...还有 {scoringResult.errors.length - 5} 条错误</div>
              )}
            </div>
          )}
        </div>
      )}

      {/* 评分失败状态 */}
      {scoringResult && !scoringInProgress && scoringResult.status === 'failed' && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-4">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 bg-red-500 rounded-full flex items-center justify-center">
              <svg className="w-6 h-6 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </div>
            <div>
              <div className="font-bold text-red-700">评分任务失败</div>
              <div className="text-sm text-red-600">{scoringResult.error || '请查看错误信息'}</div>
            </div>
          </div>
        </div>
      )}

      {/* 历史评分结果（非当前任务） */}
      {scoringResult && !scoringInProgress && !scoringResult.status && (
        <div className="border border-gray-200 rounded-lg p-4">
          <h4 className="text-sm font-medium text-gray-700 mb-2">
            上次评分结果：成功 {scoringResult.success} / 失败 {scoringResult.failed} / 总计 {scoringResult.total}
          </h4>
          {scoringResult.errors && scoringResult.errors.length > 0 && (
            <div className="max-h-32 overflow-y-auto text-xs text-red-500">
              {scoringResult.errors.map((err: string, idx: number) => (
                <div key={idx}>{err}</div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* 评分任务历史 */}
      <div className="border border-gray-200 rounded-lg overflow-hidden">
        <div className="bg-gray-50 px-4 py-2 flex items-center justify-between">
          <span className="text-sm font-medium text-gray-700">
            评分任务历史 {batchTasks.length > 0 && `(${batchTasks.length})`}
          </span>
          <button
            onClick={onRefreshTasks}
            className="text-xs px-2 py-1 bg-white border border-gray-300 rounded text-gray-600 hover:bg-gray-100 hover:text-gray-800 transition-colors"
          >
            刷新任务
          </button>
        </div>
        {batchTasks.length > 0 ? (
          <div className="divide-y divide-gray-100">
            {batchTasks.map((task: any) => {
              const total = typeof task.total === 'number' ? task.total : 0;
              const completed = typeof task.completed === 'number' ? task.completed : 0;
              const failed = typeof task.failed === 'number' ? task.failed : 0;
              const progress = total > 0 ? Math.round((completed / total) * 100) : 0;
              return (
                <div key={task.task_id} className="px-4 py-3 text-sm space-y-2">
                  {/* 第一行：状态 + 总数 + 时间 */}
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <span className={`px-2 py-0.5 rounded text-xs ${
                        task.status === 'completed' ? 'bg-green-50 text-green-600' :
                        task.status === 'running' ? 'bg-blue-50 text-blue-600' :
                        'bg-gray-50 text-gray-500'
                      }`}>
                        {task.status === 'completed' ? '已完成' : task.status === 'running' ? '进行中' : task.status}
                      </span>
                      {total > 0 ? (
                        <span className="text-gray-500 text-xs">{completed} / {total}</span>
                      ) : (
                        <span className="text-gray-400 text-xs">-</span>
                      )}
                    </div>
                    {task.started_at && (
                      <span className="text-gray-400 text-xs">
                        {new Date(task.started_at).toLocaleString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
                      </span>
                    )}
                  </div>
                  {/* 第二行：进度条 + 成功/失败数 */}
                  {total > 0 && (
                    <div className="space-y-1">
                      <div className="w-full bg-gray-200 rounded-full h-2">
                        <div
                          className={`h-2 rounded-full transition-all duration-300 ${
                            task.status === 'completed' ? 'bg-green-500' : 'bg-blue-500'
                          }`}
                          style={{ width: `${progress}%` }}
                        />
                      </div>
                      <div className="flex items-center justify-between text-xs">
                        <span className="text-gray-500">{progress}%</span>
                        <div className="flex items-center gap-2">
                          <span className="text-green-600">成功 {completed}</span>
                          {failed > 0 && (
                            <span className="text-red-500">失败 {failed}</span>
                          )}
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        ) : (
          <div className="text-center text-sm text-gray-400 py-6">
            暂无评分任务历史
          </div>
        )}
      </div>
    </div>
  );
};

export default ScoringPanel;
