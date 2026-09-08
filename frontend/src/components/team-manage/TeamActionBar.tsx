interface TeamActionBarProps {
  importing: boolean;
  showGroupConfig: boolean;
  showWorksPanel: boolean;
  showScoringPanel: boolean;
  showPlagiarismPanel: boolean;
  onToggleGroupConfig: () => void;
  onToggleWorksPanel: () => void;
  onToggleScoringPanel: () => void;
  onTogglePlagiarismPanel: () => void;
  onAssignGroups: () => void;
  onRefresh: () => void;
  onImport: (e: React.ChangeEvent<HTMLInputElement>) => void;
}

export default function TeamActionBar({
  importing,
  showGroupConfig,
  showWorksPanel,
  showScoringPanel,
  showPlagiarismPanel,
  onToggleGroupConfig,
  onToggleWorksPanel,
  onToggleScoringPanel,
  onTogglePlagiarismPanel,
  onAssignGroups,
  onRefresh,
  onImport,
}: TeamActionBarProps) {
  return (
    <div className="bg-white p-4 rounded-lg shadow-sm">
      <div className="flex gap-3 flex-wrap">
        <label className="px-4 py-2 bg-blue-600 text-white rounded cursor-pointer hover:bg-blue-700">
          {importing ? '导入中...' : '导入Excel'}
          <input
            type="file"
            accept=".xlsx,.xls"
            className="hidden"
            onChange={onImport}
            disabled={importing}
          />
        </label>
        <button
          onClick={onToggleGroupConfig}
          className="px-4 py-2 bg-purple-600 text-white rounded hover:bg-purple-700"
        >
          {showGroupConfig ? '收起评审组配置' : '评审组配置'}
        </button>
        <button
          onClick={onAssignGroups}
          className="px-4 py-2 bg-green-600 text-white rounded hover:bg-green-700"
        >
          随机分组
        </button>
        <button
          onClick={onToggleWorksPanel}
          className="px-4 py-2 bg-orange-600 text-white rounded hover:bg-orange-700"
        >
          {showWorksPanel ? '收起作品管理' : '作品管理'}
        </button>
        <button
          onClick={onToggleScoringPanel}
          className="px-4 py-2 bg-indigo-600 text-white rounded hover:bg-indigo-700"
        >
          {showScoringPanel ? '收起AI评分' : 'AI评分'}
        </button>
        <button
          onClick={onTogglePlagiarismPanel}
          className="px-4 py-2 bg-red-600 text-white rounded hover:bg-red-700"
        >
          {showPlagiarismPanel ? '收起抄袭检测' : '抄袭检测'}
        </button>
        <button
          onClick={onRefresh}
          className="px-4 py-2 bg-gray-100 text-gray-700 rounded hover:bg-gray-200"
        >
          刷新
        </button>
      </div>
    </div>
  );
}
