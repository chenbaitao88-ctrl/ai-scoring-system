import React from 'react';

interface GroupConfig {
  group_index: number;
  group_name: string;
  description: string;
  judges: string[];
  team_count?: number;
}

interface GroupConfigPanelProps {
  groupConfigs: GroupConfig[];
  savingGroups: boolean;
  onAddGroup: () => void;
  onSaveGroupConfig: () => void;
  onUpdateGroupName: (index: number, name: string) => void;
  onUpdateJudges: (index: number, judgesStr: string) => void;
  onRemoveGroup: (index: number) => void;
  onRandomAssign: () => void;
  onUploadAssignment: (e: React.ChangeEvent<HTMLInputElement>) => void;
}

const GroupConfigPanel: React.FC<GroupConfigPanelProps> = ({
  groupConfigs,
  savingGroups,
  onAddGroup,
  onSaveGroupConfig,
  onUpdateGroupName,
  onUpdateJudges,
  onRemoveGroup,
  onRandomAssign,
  onUploadAssignment,
}) => {
  return (
    <div className="bg-white p-6 rounded-lg shadow-sm">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-base font-medium text-gray-800">评审组配置</h3>
        <div className="flex gap-2">
          <button
            onClick={onAddGroup}
            className="px-3 py-1.5 text-sm bg-blue-50 text-blue-600 rounded hover:bg-blue-100"
          >
            + 添加评审组
          </button>
          <button
            onClick={onSaveGroupConfig}
            disabled={savingGroups}
            className="px-3 py-1.5 text-sm bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
          >
            {savingGroups ? '保存中...' : '保存配置'}
          </button>
        </div>
      </div>

      <div className="space-y-3">
        {groupConfigs.map((group, idx) => (
          <div key={idx} className="flex items-center gap-3 p-3 bg-gray-50 rounded">
            <span className="text-sm font-medium text-gray-500 w-8">#{group.group_index}</span>
            <input
              type="text"
              value={group.group_name}
              onChange={(e) => onUpdateGroupName(idx, e.target.value)}
              placeholder="组名称"
              className="border border-gray-300 rounded px-3 py-1.5 w-40"
            />
            <input
              type="text"
              value={group.judges?.join('、') || ''}
              onChange={(e) => onUpdateJudges(idx, e.target.value)}
              placeholder="评委姓名（用、分隔）"
              className="border border-gray-300 rounded px-3 py-1.5 flex-1"
            />
            {group.team_count !== undefined && (
              <span className="text-sm text-gray-400">{group.team_count}支队伍</span>
            )}
            <button
              onClick={() => onRemoveGroup(idx)}
              className="text-red-400 hover:text-red-600 text-sm"
            >
              删除
            </button>
          </div>
        ))}
      </div>

      {groupConfigs.length === 0 && (
        <div className="text-center text-gray-400 py-4">暂无评审组，请点击「添加评审组」</div>
      )}

      {/* 分配方式 */}
      <div className="mt-4 pt-4 border-t border-gray-200">
        <div className="text-sm font-medium text-gray-700 mb-3">队伍分配方式</div>
        <div className="flex gap-3 items-center">
          <button
            onClick={onRandomAssign}
            className="px-4 py-2 bg-teal-600 text-white rounded hover:bg-teal-700 text-sm"
          >
            随机分配
          </button>
          <span className="text-gray-400">或</span>
          <label className="px-4 py-2 bg-purple-600 text-white rounded cursor-pointer hover:bg-purple-700 text-sm">
            上传分配表（Excel）
            <input
              type="file"
              accept=".xlsx,.xls"
              className="hidden"
              onChange={onUploadAssignment}
            />
          </label>
        </div>
        <div className="mt-2 text-xs text-gray-400">
          <div>分配表Excel格式要求：</div>
          <div>1. <strong>必填列</strong>：编号（或短编号）+ 评审组（组号，数字1/2/3...）</div>
          <div>2. <strong>可选列</strong>：队伍名称、评审老师（多个用逗号分隔）</div>
          <div>3. <strong>列名支持</strong>：编号/短编号/team_code + 评审组/judge_group/组号</div>
          <div>4. <strong>示例</strong>：编号=P001, 评审组=1 或 短编号=P002, 评审组=2</div>
        </div>
      </div>
    </div>
  );
};

export default GroupConfigPanel;
