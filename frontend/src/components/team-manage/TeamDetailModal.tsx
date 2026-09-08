import React from 'react';

interface TeamDetailModalProps {
  team: any;
  reScoringTeamId: number | null;
  allScoreVersions: any[];
  selectedVersionId: number | null;
  loadingDetail: boolean;
  teamScoreDetail: any | null;
  onClose: () => void;
  onReScore: () => void;
  onSwitchVersion: (scoreId: number) => void;
  onAdoptScore: (scoreId: number) => void;
}

const TeamDetailModal: React.FC<TeamDetailModalProps> = ({
  team,
  reScoringTeamId,
  allScoreVersions,
  selectedVersionId,
  loadingDetail,
  teamScoreDetail,
  onClose,
  onReScore,
  onSwitchVersion,
  onAdoptScore,
}) => {
  return (
    <div
      className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50"
      onClick={onClose}
    >
      <div
        className="bg-white rounded-lg shadow-xl max-w-2xl w-full mx-4 max-h-[80vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="p-6">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-lg font-bold text-gray-800">
              {team.team_code} - {team.team_name}
            </h3>
            <div className="flex items-center gap-2">
              {team.work && (
                <button
                  onClick={onReScore}
                  disabled={reScoringTeamId === team.id}
                  className="px-3 py-1.5 bg-indigo-600 text-white text-sm rounded hover:bg-indigo-700 disabled:opacity-50"
                >
                  {reScoringTeamId === team.id ? '评分中...' : '重新AI评分'}
                </button>
              )}
              <button
                onClick={onClose}
                className="text-gray-400 hover:text-gray-600 text-2xl"
              >
                &times;
              </button>
            </div>
          </div>

          {/* 基本信息 */}
          <div className="grid grid-cols-2 gap-3 mb-4 text-sm">
            <div className="bg-gray-50 p-3 rounded">
              <span className="text-gray-500">编号：</span>
              <span className="font-medium">{team.team_code}</span>
            </div>
            <div className="bg-gray-50 p-3 rounded">
              <span className="text-gray-500">团队成员1：</span>
              <span className="font-medium">{team.members?.[0]?.name || '-'}</span>
            </div>
            <div className="bg-gray-50 p-3 rounded">
              <span className="text-gray-500">团队成员2：</span>
              <span className="font-medium">{team.members?.[1]?.name || '-'}</span>
            </div>
            <div className="bg-gray-50 p-3 rounded">
              <span className="text-gray-500">组别：</span>
              <span className="font-medium">{team.group_type}</span>
            </div>
            <div className="bg-gray-50 p-3 rounded">
              <span className="text-gray-500">学校：</span>
              <span className="font-medium">{team.school}</span>
            </div>
            <div className="bg-gray-50 p-3 rounded">
              <span className="text-gray-500">指导老师：</span>
              <span className="font-medium">{team.teacher_name}</span>
            </div>
            <div className="bg-gray-50 p-3 rounded">
              <span className="text-gray-500">老师手机号：</span>
              <span className="font-medium">{team.teacher_phone}</span>
            </div>
            <div className="bg-gray-50 p-3 rounded">
              <span className="text-gray-500">评审组：</span>
              <span className="font-medium">
                {team.judge_group ? `第${team.judge_group}组` : '未分配'}
              </span>
            </div>
          </div>

          {/* 作品状态 */}
          {team.work && (
            <div className="mb-4 p-3 bg-blue-50 rounded">
              <div className="text-sm font-medium text-blue-700 mb-2">作品状态</div>
              <div className="flex gap-2">
                <span
                  className={`px-2 py-1 rounded text-xs ${
                    team.work.has_source
                      ? 'bg-green-100 text-green-700'
                      : 'bg-gray-100 text-gray-400'
                  }`}
                >
                  源代码
                </span>
                <span
                  className={`px-2 py-1 rounded text-xs ${
                    team.work.has_aigc_log
                      ? 'bg-blue-100 text-blue-700'
                      : 'bg-gray-100 text-gray-400'
                  }`}
                >
                  AIGC日志
                </span>
                <span
                  className={`px-2 py-1 rounded text-xs ${
                    team.work.has_screenshots
                      ? 'bg-purple-100 text-purple-700'
                      : 'bg-gray-100 text-gray-400'
                  }`}
                >
                  截图/视频
                </span>
                {team.work.code_language && (
                  <span className="px-2 py-1 rounded text-xs bg-gray-100 text-gray-600">
                    {team.work.code_language} ({team.work.code_line_count}行)
                  </span>
                )}
              </div>
            </div>
          )}

          {/* AI评分版本选择器 */}
          {allScoreVersions.length > 0 && (
            <div className="mb-4 p-3 bg-indigo-50 rounded-lg">
              <div className="text-sm font-medium text-indigo-700 mb-2 flex items-center gap-2">
                <span>多模型评分</span>
                <span className="text-xs text-indigo-400">
                  （共 {allScoreVersions.length} 个版本）
                </span>
              </div>
              <div className="flex flex-wrap gap-2">
                {allScoreVersions.map((v: any) => (
                  <div key={v.id} className="flex items-center gap-1">
                    <button
                      onClick={() => onSwitchVersion(v.id)}
                      className={`px-3 py-1.5 rounded text-xs font-medium transition-colors ${
                        selectedVersionId === v.id
                          ? 'bg-indigo-600 text-white shadow'
                          : 'bg-white text-indigo-600 border border-indigo-200 hover:bg-indigo-100'
                      }`}
                    >
                      {v.model_name || '混元默认'} · {v.total_score}分
                      {v.created_at && !isNaN(new Date(v.created_at).getTime()) && (
                        <span className="text-[10px] text-gray-400 ml-1">
                          {new Date(v.created_at).toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })}
                        </span>
                      )}
                    </button>
                    {v.is_adopted ? (
                      <span className="px-1.5 py-0.5 bg-green-500 text-white text-[10px] rounded font-bold">
                        已采用
                      </span>
                    ) : (
                      <button
                        onClick={() => onAdoptScore(v.id)}
                        className="px-1.5 py-0.5 bg-white border border-green-300 text-green-600 text-[10px] rounded hover:bg-green-50"
                        title="采用该版本"
                      >
                        采用
                      </button>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* AI评分详情 */}
          {loadingDetail ? (
            <div className="text-center py-8 text-gray-400">加载中...</div>
          ) : teamScoreDetail ? (
            <div className="space-y-4">
              <div className="text-sm font-medium text-gray-700">AI评分详情</div>

              {/* 总分 */}
              <div className="bg-indigo-50 p-4 rounded-lg text-center">
                <div className="text-3xl font-bold text-indigo-600">
                  {teamScoreDetail.total_score}
                </div>
                <div className="text-sm text-indigo-500">总分 / 100</div>
              </div>

              {/* 各维度得分 */}
              <div className="grid grid-cols-2 gap-3">
                <div className="bg-blue-50 p-3 rounded">
                  <div className="flex justify-between items-center">
                    <span className="text-sm text-blue-600">主题立意</span>
                    <span className="font-bold text-blue-700">
                      {teamScoreDetail.theme_score}/20
                    </span>
                  </div>
                  <div className="w-full bg-blue-200 h-2 rounded mt-1">
                    <div
                      className="bg-blue-500 h-2 rounded"
                      style={{
                        width: `${(teamScoreDetail.theme_score / 20) * 100}%`,
                      }}
                    />
                  </div>
                </div>
                <div className="bg-purple-50 p-3 rounded">
                  <div className="flex justify-between items-center">
                    <span className="text-sm text-purple-600">产品表现力</span>
                    <span className="font-bold text-purple-700">
                      {teamScoreDetail.presentation_score}/30
                    </span>
                  </div>
                  <div className="w-full bg-purple-200 h-2 rounded mt-1">
                    <div
                      className="bg-purple-500 h-2 rounded"
                      style={{
                        width: `${(teamScoreDetail.presentation_score / 30) * 100}%`,
                      }}
                    />
                  </div>
                </div>
                <div className="bg-orange-50 p-3 rounded">
                  <div className="flex justify-between items-center">
                    <span className="text-sm text-orange-600">过程完整性</span>
                    <span className="font-bold text-orange-700">
                      {teamScoreDetail.process_score}/30
                    </span>
                  </div>
                  <div className="w-full bg-orange-200 h-2 rounded mt-1">
                    <div
                      className="bg-orange-500 h-2 rounded"
                      style={{
                        width: `${(teamScoreDetail.process_score / 30) * 100}%`,
                      }}
                    />
                  </div>
                </div>
                <div className="bg-pink-50 p-3 rounded">
                  <div className="flex justify-between items-center">
                    <span className="text-sm text-pink-600">AI素养</span>
                    <span className="font-bold text-pink-700">
                      {teamScoreDetail.ai_literacy_score}/20
                    </span>
                  </div>
                  <div className="w-full bg-pink-200 h-2 rounded mt-1">
                    <div
                      className="bg-pink-500 h-2 rounded"
                      style={{
                        width: `${(teamScoreDetail.ai_literacy_score / 20) * 100}%`,
                      }}
                    />
                  </div>
                </div>
              </div>

              {/* AI 过程性评价评语 */}
              {(teamScoreDetail.theme_comment || teamScoreDetail.presentation_comment || teamScoreDetail.process_comment || teamScoreDetail.ai_literacy_comment || teamScoreDetail.overall_comment) ? (
                <div className="space-y-2">
                  <div className="text-sm font-medium text-gray-700">AI 过程性评价评语</div>
                  {teamScoreDetail.theme_comment && (
                    <div className="bg-blue-50 rounded p-3 text-sm">
                      <div className="font-medium text-blue-700 mb-1">主题立意评语</div>
                      <div className="text-gray-600">{teamScoreDetail.theme_comment}</div>
                    </div>
                  )}
                  {teamScoreDetail.presentation_comment && (
                    <div className="bg-purple-50 rounded p-3 text-sm">
                      <div className="font-medium text-purple-700 mb-1">产品表现力评语</div>
                      <div className="text-gray-600">{teamScoreDetail.presentation_comment}</div>
                    </div>
                  )}
                  {teamScoreDetail.process_comment && (
                    <div className="bg-orange-50 rounded p-3 text-sm">
                      <div className="font-medium text-orange-700 mb-1">过程完整性评语</div>
                      <div className="text-gray-600">{teamScoreDetail.process_comment}</div>
                    </div>
                  )}
                  {teamScoreDetail.ai_literacy_comment && (
                    <div className="bg-pink-50 rounded p-3 text-sm">
                      <div className="font-medium text-pink-700 mb-1">AI素养评语</div>
                      <div className="text-gray-600">{teamScoreDetail.ai_literacy_comment}</div>
                    </div>
                  )}
                  {teamScoreDetail.overall_comment && (
                    <div className="bg-indigo-50 rounded p-3 text-sm">
                      <div className="font-medium text-indigo-700 mb-1">辅助评审综合评价</div>
                      <div className="text-gray-600">{teamScoreDetail.overall_comment}</div>
                    </div>
                  )}
                </div>
              ) : (
                <div className="text-sm text-gray-400 py-2">暂无 AI 过程性评价评语</div>
              )}

              {/* 代码分析详情 */}
              {teamScoreDetail.code_quality_details && (
                <div className="bg-gray-50 p-3 rounded">
                  <div className="text-sm font-medium text-gray-700 mb-2">代码分析</div>
                  <div className="grid grid-cols-3 gap-2 text-xs">
                    <div>
                      语言:{' '}
                      <span className="font-medium">
                        {teamScoreDetail.code_quality_details.language}
                      </span>
                    </div>
                    <div>
                      代码行数:{' '}
                      <span className="font-medium">
                        {teamScoreDetail.code_quality_details.total_lines}
                      </span>
                    </div>
                    <div>
                      函数数:{' '}
                      <span className="font-medium">
                        {teamScoreDetail.code_quality_details.function_count}
                      </span>
                    </div>
                    <div>
                      注释率:{' '}
                      <span className="font-medium">
                        {teamScoreDetail.code_quality_details.comment_rate?.toFixed(1)}%
                      </span>
                    </div>
                    <div>
                      圈复杂度:{' '}
                      <span className="font-medium">
                        {teamScoreDetail.code_quality_details.cyclomatic_complexity}
                      </span>
                    </div>
                    <div>
                      质量分:{' '}
                      <span className="font-medium">
                        {teamScoreDetail.code_quality_details.quality_score}
                      </span>
                    </div>
                  </div>
                  {teamScoreDetail.code_quality_details.game_mechanics?.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-1">
                      {teamScoreDetail.code_quality_details.game_mechanics.map(
                        (m: string, i: number) => (
                          <span
                            key={i}
                            className="px-2 py-0.5 bg-green-100 text-green-700 rounded text-xs"
                          >
                            {m}
                          </span>
                        )
                      )}
                    </div>
                  )}
                </div>
              )}

              {/* AIGC分析详情 */}
              {teamScoreDetail.aigc_analysis_details && (
                <div className="bg-gray-50 p-3 rounded">
                  <div className="text-sm font-medium text-gray-700 mb-2">
                    AIGC交互分析
                  </div>
                  <div className="grid grid-cols-3 gap-2 text-xs">
                    <div>
                      工具:{' '}
                      <span className="font-medium">
                        {teamScoreDetail.aigc_analysis_details.tool_name}
                      </span>
                    </div>
                    <div>
                      交互次数:{' '}
                      <span className="font-medium">
                        {teamScoreDetail.aigc_analysis_details.user_messages}
                      </span>
                    </div>
                    <div>
                      迭代深度:{' '}
                      <span className="font-medium">
                        {teamScoreDetail.aigc_analysis_details.iteration_depth}
                      </span>
                    </div>
                  </div>
                  {teamScoreDetail.aigc_analysis_details.features?.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-1">
                      {teamScoreDetail.aigc_analysis_details.features.map(
                        (f: string, i: number) => (
                          <span
                            key={i}
                            className="px-2 py-0.5 bg-blue-100 text-blue-700 rounded text-xs"
                          >
                            {f}
                          </span>
                        )
                      )}
                    </div>
                  )}
                </div>
              )}

              {/* 功能检测 */}
              {teamScoreDetail.feature_detection_details && (
                <div className="bg-gray-50 p-3 rounded">
                  <div className="text-sm font-medium text-gray-700 mb-2">功能检测</div>
                  <div className="flex flex-wrap gap-1">
                    {teamScoreDetail.feature_detection_details.detected_features?.map(
                      (f: string, i: number) => (
                        <span
                          key={i}
                          className="px-2 py-0.5 bg-purple-100 text-purple-700 rounded text-xs"
                        >
                          {f}
                        </span>
                      )
                    )}
                  </div>
                  <div className="mt-2 text-xs text-gray-500">
                    完整性评分: {teamScoreDetail.feature_detection_details.completeness_score}
                  </div>
                </div>
              )}

              {/* 答辩提问参考 */}
              {teamScoreDetail?.defense_questions?.length > 0 && (
                <div className="bg-yellow-50 p-3 rounded">
                  <div className="text-sm font-medium text-yellow-700 mb-2">
                    答辩提问参考（AI生成）
                  </div>
                  <ol className="list-decimal list-inside text-sm text-yellow-600 space-y-1">
                    {teamScoreDetail.defense_questions.map((q: string, i: number) => (
                      <li key={i}>{q}</li>
                    ))}
                  </ol>
                </div>
              )}
            </div>
          ) : (
            <div className="text-center py-8 text-gray-400">暂无评分数据</div>
          )}
        </div>
      </div>
    </div>
  );
};

export default TeamDetailModal;
