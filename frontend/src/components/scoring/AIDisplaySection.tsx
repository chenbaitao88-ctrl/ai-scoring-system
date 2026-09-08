import type { TeamScoreInfo } from '../../pages/Scoring';
import type { DimensionConfig } from '../../services/api';

interface AIDisplaySectionProps {
  team: TeamScoreInfo;
  dimensions: Record<string, DimensionConfig>;
}

export default function AIDisplaySection({ team, dimensions }: AIDisplaySectionProps) {
  return (
    <>
      {/* AI 维度分展示（双轨 - 只读） */}
      {team.ai_dimensions && (team.ai_dimensions.total ?? 0) > 0 ? (
        <div className="bg-sky-50 rounded-lg p-4 space-y-2">
          <div className="text-sm font-medium text-sky-700">AI 维度分（仅供参考）</div>
          <div className="grid grid-cols-5 gap-2 text-sm">
            <div className="bg-white/60 rounded p-2 text-center">
              <div className="text-blue-600 font-bold">{team.ai_dimensions.theme ?? '-'}</div>
              <div className="text-gray-500">主题契合/5</div>
            </div>
            <div className="bg-white/60 rounded p-2 text-center">
              <div className="text-purple-600 font-bold">{team.ai_dimensions.code_quality ?? '-'}</div>
              <div className="text-gray-500">代码质量/15</div>
            </div>
            <div className="bg-white/60 rounded p-2 text-center">
              <div className="text-orange-600 font-bold">{team.ai_dimensions.completeness ?? '-'}</div>
              <div className="text-gray-500">材料完整/10</div>
            </div>
            <div className="bg-white/60 rounded p-2 text-center">
              <div className="text-pink-600 font-bold">{team.ai_dimensions.aigc ?? '-'}</div>
              <div className="text-gray-500">AIGC规范/10</div>
            </div>
            <div className="bg-white/80 rounded p-2 text-center border-2 border-sky-200">
              <div className="text-sky-700 font-bold text-lg">{team.ai_dimensions.total}</div>
              <div className="text-gray-500">AI总分/40</div>
            </div>
          </div>
        </div>
      ) : (
        <div className="bg-gray-50 rounded-lg p-4 text-center text-sm text-gray-400">
          暂无 AI 维度分数据
        </div>
      )}

      {/* AI 评语展示 */}
      {team.machine_comments && (
        <div className="space-y-2">
          {(['theme', 'presentation', 'process', 'ai_literacy'] as const).map((key) => {
            const comment = team.machine_comments?.[key];
            if (!comment) return null;
            const label = dimensions?.[key]?.name || {
              theme: '主题契合',
              presentation: '产品表现力',
              process: '过程完整性',
              ai_literacy: 'AI素养',
            }[key];
            const colorClass = {
              theme: { bg: 'bg-blue-50', text: 'text-blue-700' },
              presentation: { bg: 'bg-purple-50', text: 'text-purple-700' },
              process: { bg: 'bg-orange-50', text: 'text-orange-700' },
              ai_literacy: { bg: 'bg-pink-50', text: 'text-pink-700' },
            }[key];
            return (
              <div key={key} className={`${colorClass.bg} rounded p-3 text-sm`}>
                <div className={`font-medium ${colorClass.text} mb-1`}>{label}评语</div>
                <div className="text-gray-600">{comment}</div>
              </div>
            );
          })}
          {team.machine_comments.overall && (
            <div className="bg-indigo-50 rounded p-3 text-sm text-gray-600">
              <div className="font-medium text-indigo-700 mb-1">AI总评</div>
              <div>{team.machine_comments.overall}</div>
            </div>
          )}
        </div>
      )}

      {/* 答辩提问参考 */}
      {team.defense_questions && team.defense_questions.length > 0 && (
        <div className="bg-yellow-50 rounded-lg p-4">
          <div className="text-sm font-medium text-yellow-700 mb-2">答辩提问参考（AI生成）</div>
          <ol className="list-decimal list-inside text-sm text-yellow-600 space-y-1">
            {team.defense_questions.map((q, idx) => (
              <li key={idx}>{q}</li>
            ))}
          </ol>
        </div>
      )}

      {/* 已有评分记录 */}
      {team.human_scores.length > 0 && (
        <div className="bg-yellow-50 rounded-lg p-4">
          <div className="text-sm font-medium text-yellow-700 mb-2">
            已有的评分记录（共{team.human_scores.length}条）
          </div>
          <div className="space-y-2">
            {team.human_scores.map((h, idx) => {
              const hasNewDims = h.human_presentation_score !== null || h.human_creativity_score !== null;
              const hasOldDims = h.theme_score !== null || h.presentation_score !== null;
              const displayTotal = h.human_total_score ?? h.total_score ?? 0;
              return (
                <div key={idx} className="bg-white rounded p-3 text-sm">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-gray-700">{h.judge_name}</span>
                      {!hasNewDims && hasOldDims && (
                        <span className="text-[10px] bg-gray-100 text-gray-500 px-1.5 py-0.5 rounded">旧版评分</span>
                      )}
                    </div>
                    <span className="font-bold text-yellow-600">{displayTotal}分</span>
                  </div>
                  {hasNewDims && (
                    <div className="text-xs text-gray-400 mt-1">
                      表现力{h.human_presentation_score ?? '-'} + 创意{h.human_creativity_score ?? '-'} + 过程{h.human_process_score ?? '-'} + 现场{h.human_performance_score ?? '-'}
                    </div>
                  )}
                  {!hasNewDims && hasOldDims && (
                    <div className="text-xs text-gray-400 mt-1">
                      主题{h.theme_score ?? '-'} + 表现力{h.presentation_score ?? '-'} + 过程{h.process_score ?? '-'} + AI素养{h.ai_literacy_score ?? '-'}
                    </div>
                  )}
                  {!hasNewDims && !hasOldDims && displayTotal > 0 && (
                    <div className="text-xs text-gray-400 mt-1">仅总分，无维度明细</div>
                  )}
                  {h.comment && <div className="text-xs text-gray-500 mt-1 italic">"{h.comment}"</div>}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </>
  );
}
