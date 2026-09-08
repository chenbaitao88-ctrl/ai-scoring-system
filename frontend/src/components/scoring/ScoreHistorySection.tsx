/**
 * 评分历史折叠区（纯展示）
 * 所有状态与 API 调用由父组件 Scoring.tsx 管理
 */

interface ScoreHistoryItem {
  id: number;
  ai_score: number;
  calibrated_score: number;
  anchor_level?: string;
  model?: string;
  is_adopted?: boolean;
  created_at?: string;
}

interface ScoreHistorySectionProps {
  showHistory: boolean;
  historyLoading: boolean;
  scoreHistory: ScoreHistoryItem[];
  onToggleHistory: () => void;
}

export default function ScoreHistorySection({
  showHistory,
  historyLoading,
  scoreHistory,
  onToggleHistory,
}: ScoreHistorySectionProps) {
  return (
    <div className="mt-6 border-t pt-4">
      <button
        onClick={onToggleHistory}
        className="flex items-center gap-2 text-sm text-gray-600 hover:text-gray-800"
      >
        <span>{showHistory ? '▼' : '▶'}</span>
        <span>评分历史 ({scoreHistory.length} 个版本)</span>
      </button>
      {showHistory && (
        <div className="mt-3 space-y-2">
          {historyLoading ? (
            <div className="text-sm text-gray-500">加载中...</div>
          ) : scoreHistory.length === 0 ? (
            <div className="text-sm text-gray-500">暂无历史记录</div>
          ) : (
            scoreHistory.map((h, idx) => (
              <div
                key={h.id}
                className={`p-3 rounded text-sm ${
                  h.is_adopted ? 'bg-green-50 border border-green-200' : 'bg-gray-50'
                }`}
              >
                <div className="flex justify-between">
                  <span className="font-medium">
                    {idx === 0 ? '最新' : `第 ${scoreHistory.length - idx} 次`}
                    {h.is_adopted && <span className="ml-2 text-green-600 text-xs">当前采用</span>}
                  </span>
                  <span className="text-gray-500 text-xs">{h.created_at?.slice(0, 16).replace('T', ' ')}</span>
                </div>
                <div className="mt-1 text-gray-700">
                  AI: {h.ai_score} → 校准: {h.calibrated_score}
                  {h.anchor_level && <span className="ml-2 text-xs px-1.5 py-0.5 rounded bg-gray-200">{h.anchor_level}</span>}
                  {h.model && <span className="ml-2 text-xs text-gray-500">{h.model}</span>}
                </div>
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}
