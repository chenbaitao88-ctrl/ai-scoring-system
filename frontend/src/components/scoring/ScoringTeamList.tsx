import type { TeamScoreInfo } from '../../pages/Scoring';

interface ScoringTeamListProps {
  filteredTeams: TeamScoreInfo[];
  loading: boolean;
  judgeName: string;
  selectedTeamId: number | null;
  onSelectTeam: (team: TeamScoreInfo) => void;
}

export default function ScoringTeamList({
  filteredTeams,
  loading,
  judgeName,
  selectedTeamId,
  onSelectTeam,
}: ScoringTeamListProps) {
  return (
    <div className="bg-white rounded-lg shadow-sm overflow-hidden col-span-1">
      <div className="px-4 py-3 bg-gray-50 border-b font-medium text-gray-700">
        待评分队伍
      </div>
      <div className="max-h-[60vh] overflow-y-auto">
        {loading ? (
          <div className="p-8 text-center text-gray-400">加载中...</div>
        ) : filteredTeams.length === 0 ? (
          <div className="p-8 text-center text-gray-400">
            {judgeName ? '该评审组暂无队伍' : '请先输入评委姓名'}
          </div>
        ) : (
          filteredTeams.map(team => (
            <div
              key={team.id}
              onClick={() => onSelectTeam(team)}
              className={`px-4 py-3 border-b cursor-pointer hover:bg-gray-50 ${
                selectedTeamId === team.id ? 'bg-indigo-50 border-l-4 border-l-indigo-500' : ''
              }`}
            >
              <div className="flex items-center justify-between">
                <span className="font-mono font-bold text-teal-700">{team.short_code}</span>
                {team.has_human_score && (
                  <span className="text-xs bg-green-100 text-green-700 px-1.5 py-0.5 rounded">已评分</span>
                )}
              </div>
              <div className="text-sm text-gray-700 mt-0.5">{team.team_name === 'nan' ? '' : team.team_name}</div>
              <div className="text-xs text-gray-400 mt-0.5">{team.school}</div>
              {team.member1_name && (
                <div className="text-xs text-teal-600 mt-0.5">👤 {team.member1_name}</div>
              )}
              {(team.machine_score !== null && team.machine_score !== undefined) && (
                <div className="mt-1">
                  {((team.machine_score ?? 0) <= 5 || Number.isNaN(team.machine_score)) ? (
                    <div>
                      <div className="text-sm font-bold text-gray-400">N/A</div>
                      <div className="text-[10px] text-orange-500">AI 未能有效解析</div>
                    </div>
                  ) : (
                    <div>
                      <div className="text-sm font-bold text-indigo-600">
                        {team.calibrated_score !== null && team.calibrated_score !== undefined && !Number.isNaN(team.calibrated_score)
                          ? team.calibrated_score + '分'
                          : 'N/A'}
                        {team.calibrated_score !== null && team.calibrated_score !== undefined && !Number.isNaN(team.calibrated_score) && team.calibrated_score !== team.machine_score && (
                          <span className="ml-1 text-[10px] font-normal text-blue-600 bg-blue-50 px-1 rounded">已校准</span>
                        )}
                      </div>
                    </div>
                  )}
                  {team.ai_dimensions && (team.ai_dimensions.total ?? 0) > 0 && (
                    <div className="text-[10px] text-sky-600 mt-0.5">
                      AI维度: {team.ai_dimensions.total}/40
                    </div>
                  )}
                  {team.anchor_level && (
                    <span className={`inline-block mt-0.5 text-[10px] px-1.5 py-0.5 rounded-full font-medium border ${
                      team.anchor_level === 'Lv0' ? 'bg-emerald-100 text-emerald-700 border-emerald-200' :
                      team.anchor_level === 'Lv1' ? 'bg-sky-100 text-sky-700 border-sky-200' :
                      team.anchor_level === 'Lv2' ? 'bg-amber-100 text-amber-700 border-amber-200' :
                      'bg-rose-100 text-rose-700 border-rose-200'
                    }`}>
                      {team.anchor_level}
                    </span>
                  )}
                  {team.flags && team.flags.length > 0 && (
                    <div className="flex flex-wrap gap-1 mt-1">
                      {team.flags.map((f, i) => (
                        <span
                          key={i}
                          title={f.msg}
                          className={`text-[10px] px-1 py-0.5 rounded font-medium ${
                            f.level === 'MUST_REVIEW'
                              ? 'bg-red-500 text-white'
                              : 'bg-yellow-400 text-yellow-900'
                          }`}
                        >
                          {f.code}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
