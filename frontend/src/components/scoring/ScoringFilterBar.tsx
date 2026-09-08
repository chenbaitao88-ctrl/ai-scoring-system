interface GroupConfig {
  group_index: number;
  group_name: string;
}

interface ScoringFilterBarProps {
  judgeName: string;
  onJudgeNameChange: (value: string) => void;
  scoringMode: string;
  onModeChange: (value: string) => void;
  modeLoading: boolean;
  selectedGroup: number | null;
  onGroupChange: (value: number | null) => void;
  groupConfigs: GroupConfig[];
  shortCodeFilter: string;
  onShortCodeChange: (value: string) => void;
  memberNameFilter: string;
  onMemberNameChange: (value: string) => void;
  anchorLevelFilter: string;
  onAnchorLevelChange: (value: string) => void;
  flagFilter: string;
  onFlagChange: (value: string) => void;
  onRefresh: () => void;
  onExport: () => void;
  filteredCount: number;
  totalHumanScoredCount: number;
  totalCompositeCount: number;
}

export default function ScoringFilterBar({
  judgeName,
  onJudgeNameChange,
  scoringMode,
  onModeChange,
  modeLoading,
  selectedGroup,
  onGroupChange,
  groupConfigs,
  shortCodeFilter,
  onShortCodeChange,
  memberNameFilter,
  onMemberNameChange,
  anchorLevelFilter,
  onAnchorLevelChange,
  flagFilter,
  onFlagChange,
  onRefresh,
  onExport,
  filteredCount,
  totalHumanScoredCount,
  totalCompositeCount,
}: ScoringFilterBarProps) {
  return (
    <div className="bg-white p-4 rounded-lg shadow-sm">
      <div className="flex gap-4 items-end flex-wrap">
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">评委姓名</label>
          <input
            type="text"
            value={judgeName}
            onChange={(e) => onJudgeNameChange(e.target.value)}
            placeholder="请输入姓名"
            className="border border-gray-300 rounded px-3 py-2 w-32"
          />
        </div>
        {/* 评分模式切换 */}
        <div className="bg-indigo-50 rounded-lg p-3 space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium text-indigo-700">评分模式</span>
            <span className={`text-xs px-2 py-0.5 rounded-full ${
              scoringMode === 'mixed' ? 'bg-blue-100 text-blue-700' : 'bg-purple-100 text-purple-700'
            }`}>
              {scoringMode === 'mixed' ? '人机混合' : '纯AI'}
            </span>
          </div>
          <select
            value={scoringMode}
            onChange={(e) => onModeChange(e.target.value)}
            disabled={modeLoading}
            className="border border-indigo-200 rounded px-3 py-1.5 w-full text-sm bg-white"
          >
            <option value="mixed">人机混合模式（AI 40% + 人工 60%）</option>
            <option value="ai_only">纯AI评分模式（AI 推断全部维度）</option>
          </select>
          {modeLoading && <div className="text-xs text-indigo-500">切换中...</div>}
        </div>
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">评审组筛选</label>
          <select
            value={selectedGroup ?? ''}
            onChange={(e) => onGroupChange(e.target.value ? Number(e.target.value) : null)}
            className="border border-gray-300 rounded px-3 py-2"
          >
            <option value="">全部组别</option>
            {groupConfigs.map(g => (
              <option key={g.group_index} value={g.group_index}>
                {g.group_name}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">编号搜索</label>
          <input
            type="text"
            value={shortCodeFilter}
            onChange={(e) => onShortCodeChange(e.target.value)}
            placeholder="如 P001"
            className="border border-gray-300 rounded px-3 py-2 w-28"
          />
        </div>
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">选手姓名</label>
          <input
            type="text"
            value={memberNameFilter}
            onChange={(e) => onMemberNameChange(e.target.value)}
            placeholder="搜索选手"
            className="border border-gray-300 rounded px-3 py-2 w-28"
          />
        </div>
        {/* 锚点等级筛选 */}
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">锚点等级</label>
          <select
            value={anchorLevelFilter}
            onChange={(e) => onAnchorLevelChange(e.target.value)}
            className="border border-gray-300 rounded px-3 py-2 w-32"
          >
            <option value="">全部</option>
            <option value="Lv0">Lv0 高置信</option>
            <option value="Lv1">Lv1 中置信</option>
            <option value="Lv2">Lv2 低置信</option>
            <option value="Lv3">Lv3 未知</option>
          </select>
        </div>
        {/* Flag 筛选 */}
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Flag</label>
          <select
            value={flagFilter}
            onChange={(e) => onFlagChange(e.target.value)}
            className="border border-gray-300 rounded px-3 py-2 w-32"
          >
            <option value="">全部</option>
            <option value="has_flag">有Flag</option>
            <option value="no_flag">无Flag</option>
            <option value="must_review">仅硬规则</option>
            <option value="suggest_review">仅软规则</option>
          </select>
        </div>
        <button
          onClick={onRefresh}
          className="px-4 py-2 bg-gray-100 text-gray-700 rounded hover:bg-gray-200"
        >
          刷新
        </button>
        <div className="ml-auto flex items-center gap-3">
          <button
            onClick={onExport}
            className="text-xs bg-green-50 text-green-700 px-3 py-1.5 rounded-lg hover:bg-green-100 transition"
          >
            导出评分结果
          </button>
          <span className="text-sm text-gray-500">
            共 {filteredCount} 支队伍
            {scoringMode === 'mixed' && (
              <span> | 已人工评分 {totalHumanScoredCount} 支</span>
            )}
            <span className="ml-2 text-indigo-600">
              | 已综合 {totalCompositeCount} 支
            </span>
          </span>
        </div>
      </div>
    </div>
  );
}
