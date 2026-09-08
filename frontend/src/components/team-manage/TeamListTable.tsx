import React from 'react';

interface GroupConfig {
  group_index: number;
  group_name: string;
  description: string;
  judges: string[];
  team_count?: number;
}

interface TeamListTableProps {
  teams: any[];
  groupConfigs: GroupConfig[];
  onViewTeamDetail: (team: any) => void;
}

const TeamListTable: React.FC<TeamListTableProps> = ({
  teams,
  groupConfigs,
  onViewTeamDetail,
}) => {
  return (
    <div className="bg-white rounded-lg shadow-sm overflow-hidden">
      <table className="w-full text-sm">
        <thead>
          <tr className="bg-gray-50 border-b">
            <th className="px-4 py-3 text-left">编号</th>
            <th className="px-4 py-3 text-left">团队成员1</th>
            <th className="px-4 py-3 text-left">团队成员2</th>
            <th className="px-4 py-3 text-left">评审组</th>
            <th className="px-4 py-3 text-left">作品状态</th>
            <th className="px-4 py-3 text-left">AI评分</th>
            <th className="px-4 py-3 text-left">团队名称</th>
            <th className="px-4 py-3 text-left">组别</th>
            <th className="px-4 py-3 text-left">学校</th>
            <th className="px-4 py-3 text-left">区县</th>
            <th className="px-4 py-3 text-left">指导老师</th>
            <th className="px-4 py-3 text-left">老师手机号</th>
          </tr>
        </thead>
        <tbody>
          {teams.length === 0 ? (
            <tr>
              <td colSpan={12} className="px-4 py-12 text-center text-gray-400">
                暂无队伍数据，请先导入Excel
              </td>
            </tr>
          ) : (
            teams.map((team) => (
              <tr key={team.id} className="border-b hover:bg-gray-50 cursor-pointer" onClick={() => onViewTeamDetail(team)}>
                {/* 1. 编号 */}
                <td className="px-4 py-3">
                  <span className="font-mono bg-teal-50 text-teal-700 px-2 py-0.5 rounded font-bold">
                    {team.team_code}
                  </span>
                </td>
                {/* 2. 团队成员1 */}
                <td className="px-4 py-3">{team.members?.[0]?.name || '-'}</td>
                {/* 3. 团队成员2 */}
                <td className="px-4 py-3">{team.members?.[1]?.name || '-'}</td>
                {/* 4. 评审组 */}
                <td className="px-4 py-3">
                  {team.judge_group
                    ? (groupConfigs.find(g => g.group_index === team.judge_group)?.group_name || `第${team.judge_group}组`)
                    : '-'
                  }
                </td>
                {/* 5. 作品状态 */}
                <td className="px-4 py-3">
                  {team.work ? (
                    <div className="flex gap-1 text-xs">
                      <span className={`px-1.5 py-0.5 rounded ${team.work.has_source ? 'bg-green-50 text-green-600' : 'bg-gray-100 text-gray-400'}`} title="源代码">
                        码
                      </span>
                      <span className={`px-1.5 py-0.5 rounded ${team.work.has_aigc_log ? 'bg-blue-50 text-blue-600' : 'bg-gray-100 text-gray-400'}`} title="AIGC日志">
                        AI
                      </span>
                      <span className={`px-1.5 py-0.5 rounded ${team.work.has_screenshots ? 'bg-purple-50 text-purple-600' : 'bg-gray-100 text-gray-400'}`} title="截图/视频">
                        图
                      </span>
                      {team.work.plagiarism_flag && (
                        <span className="px-1.5 py-0.5 rounded bg-red-100 text-red-600 font-bold" title="疑似抄袭">
                          抄
                        </span>
                      )}
                      <span className="text-gray-400 ml-1">
                        {team.work.code_language || ''}
                      </span>
                    </div>
                  ) : (
                    <span className="text-xs text-gray-300">未上传</span>
                  )}
                </td>
                {/* 6. AI评分 */}
                <td className="px-4 py-3">
                  {(team as any).machine_score !== undefined && (team as any).machine_score !== null ? (
                    <div className="space-y-0.5">
                      <div className="flex items-center gap-1.5">
                        <span className="font-mono text-sm font-bold text-indigo-600">{(team as any).machine_score}分</span>
                        {(team as any).machine_is_adopted ? (
                          <span className="px-1.5 py-0.5 rounded text-xs bg-green-50 text-green-600">已采纳</span>
                        ) : (
                          <span className="px-1.5 py-0.5 rounded text-xs bg-orange-50 text-orange-600">待采纳</span>
                        )}
                      </div>
                      <div className="flex gap-0.5 text-[10px]">
                        <span className="bg-blue-100 text-blue-700 px-1 rounded" title="主题立意">立{(team as any).machine_theme || ''}</span>
                        <span className="bg-purple-100 text-purple-700 px-1 rounded" title="产品表现力">表{(team as any).machine_presentation || ''}</span>
                        <span className="bg-orange-100 text-orange-700 px-1 rounded" title="过程完整性">过{(team as any).machine_process || ''}</span>
                        <span className="bg-pink-100 text-pink-700 px-1 rounded" title="AI素养">AI{(team as any).machine_ai_literacy || ''}</span>
                      </div>
                    </div>
                  ) : (
                    <span className="px-1.5 py-0.5 rounded text-xs bg-gray-100 text-gray-400">未评分</span>
                  )}
                </td>
                {/* 7. 团队名称 */}
                <td className="px-4 py-3">{team.team_name}</td>
                {/* 8. 组别 */}
                <td className="px-4 py-3 whitespace-nowrap">
                  <span className={`px-2 py-0.5 rounded text-xs ${
                    team.group_type === '小学组' ? 'bg-green-50 text-green-600' : 'bg-orange-50 text-orange-600'
                  }`}>
                    {team.group_type}
                  </span>
                </td>
                {/* 9. 学校 */}
                <td className="px-4 py-3">{team.school}</td>
                {/* 10. 区县 */}
                <td className="px-4 py-3">{team.district}</td>
                {/* 11. 指导老师 */}
                <td className="px-4 py-3">{team.teacher_name}</td>
                {/* 12. 老师手机号 */}
                <td className="px-4 py-3">{team.teacher_phone}</td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
};

export default TeamListTable;
