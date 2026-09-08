interface TeamFilterBarProps {
  search: string;
  onSearchChange: (value: string) => void;
  groupFilter: string;
  onGroupFilterChange: (value: string) => void;
  onSearch: () => void;
}

export default function TeamFilterBar({
  search,
  onSearchChange,
  groupFilter,
  onGroupFilterChange,
  onSearch,
}: TeamFilterBarProps) {
  return (
    <div className="bg-white p-4 rounded-lg shadow-sm">
      <div className="flex gap-3 items-center">
        <input
          type="text"
          placeholder="搜索队伍编号/名称/学校"
          value={search}
          onChange={(e) => onSearchChange(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && onSearch()}
          className="border border-gray-300 rounded px-3 py-2 w-64"
        />
        <select
          value={groupFilter}
          onChange={(e) => onGroupFilterChange(e.target.value)}
          className="border border-gray-300 rounded px-3 py-2"
        >
          <option value="">全部组别</option>
          <option value="小学组">小学组</option>
          <option value="初中组">初中组</option>
        </select>
        <button
          onClick={onSearch}
          className="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700"
        >
          查询
        </button>
      </div>
    </div>
  );
}
