/**
 * 人工评分界面
 * 评委对队伍进行四维度打分
 */
import { useEffect, useState, useMemo } from 'react';
import { scoreApi, judgeGroupApi, competitionApi } from '../services/api';
import type { DimensionConfig } from '../services/api';
import { ScoringFilterBar, ScoringTeamList, AIDisplaySection, ScoreHistorySection } from '../components/scoring';

export interface TeamScoreInfo {
  id: number;
  short_code: string;
  team_name: string;
  school: string;
  group_type: string;
  judge_group: number;
  member1_name: string;
  machine_score: number | null;
  // 双轨AI维度分
  ai_dimensions: {
    theme: number;
    code_quality: number;
    completeness: number;
    aigc: number;
    total: number;
  } | null;
  machine_details: {
    theme: number;
    presentation: number;
    process: number;
    ai_literacy: number;
  } | null;
  machine_comments: {
    theme: string | null;
    presentation: string | null;
    process: string | null;
    ai_literacy: string | null;
    overall: string | null;
  } | null;
  defense_questions?: string[];  // 答辩提问参考
  human_scores: Array<{
    judge_name: string;
    // 双轨人工维度
    human_presentation_score: number;
    human_creativity_score: number;
    human_process_score: number;
    human_performance_score: number;
    human_total_score: number;
    // 旧维度（兼容）
    theme_score: number;
    presentation_score: number;
    process_score: number;
    ai_literacy_score: number;
    total_score: number;
    comment: string;
    created_at: string;
  }>;
  has_human_score: boolean;
  // 双轨综合分
  composite_score: number | null;
  scoring_mode: string | null;
  // AI 推断人工维度（ai_only 模式）
  inferred_dimensions: {
    presentation: number;
    creativity: number;
    process: number;
    performance: number;
    total: number;
  } | null;
  // P1-8 (2026-05-26): 新增校准分、锚点等级、置信度、Flag标签
  calibrated_score: number | null;
  anchor_level: string | null;      // "Lv0" | "Lv1" | "Lv2" | "Lv3"
  confidence: string | null;        // "HIGH" | "MEDIUM" | "LOW" | "UNKNOWN"
  flags: Array<{code: string; level: string; msg: string}> | null;
}

interface GroupConfig {
  group_index: number;
  group_name: string;
}

export default function Scoring() {
  const [judgeName, setJudgeName] = useState('');
  const [selectedGroup, setSelectedGroup] = useState<number | null>(null);
  const [shortCodeFilter, setShortCodeFilter] = useState('');
  const [memberNameFilter, setMemberNameFilter] = useState('');
  // P1-8 (2026-05-26): 新增锚点等级和Flag筛选
  const [anchorLevelFilter, setAnchorLevelFilter] = useState<string>('');
  const [flagFilter, setFlagFilter] = useState<string>('');
  const [teams, setTeams] = useState<TeamScoreInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [groupConfigs, setGroupConfigs] = useState<GroupConfig[]>([]);

  // 当前选中打分的队伍
  const [selectedTeam, setSelectedTeam] = useState<TeamScoreInfo | null>(null);
  // 评分表单（双轨人工维度）
  const [humanPresentationScore, setHumanPresentationScore] = useState(0);
  const [humanCreativityScore, setHumanCreativityScore] = useState(0);
  const [humanProcessScore, setHumanProcessScore] = useState(0);
  const [humanPerformanceScore, setHumanPerformanceScore] = useState(0);
  const [comment, setComment] = useState('');
  const [submitting, setSubmitting] = useState(false);
  // P1-10 (2026-05-26): 重新评分状态
  const [reScoring, setReScoring] = useState(false);
  const [reScoreError, setReScoreError] = useState('');
  // P1-11 (2026-05-26): 评分历史折叠区
  const [showHistory, setShowHistory] = useState(false);
  const [scoreHistory, setScoreHistory] = useState<any[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);

  useEffect(() => {
    loadGroups();
    loadCompetitionMode();
  }, []);

  const loadCompetitionMode = async () => {
    try {
      const res = await competitionApi.getCurrent() as any;
      if (res.competition) {
        setScoringMode(res.competition.scoring_mode || 'mixed');
        setCompetitionName(res.competition.name || '');
        // 从当前比赛配置获取维度配置
        if (res.competition.dimensions_config && Object.keys(res.competition.dimensions_config).length > 0) {
          setDimensions(res.competition.dimensions_config);
        }
      }
    } catch (err) {
      console.error('加载比赛配置失败', err);
    }
  };

  const handleModeChange = async (newMode: string) => {
    if (newMode === scoringMode) return;
    if (!confirm(`确认切换评分模式为「${newMode === 'mixed' ? '人机混合模式' : '纯AI评分模式'}」？`)) return;

    setModeLoading(true);
    try {
      const res = await competitionApi.getCurrent() as any;
      if (res.competition?.id) {
        await competitionApi.update(res.competition.id, { scoring_mode: newMode });
        setScoringMode(newMode);
        alert('评分模式切换成功');
      }
    } catch (err: any) {
      alert(err.message || '切换失败');
    }
    setModeLoading(false);
  };

  useEffect(() => {
    loadTeams();
  }, [selectedGroup, shortCodeFilter, memberNameFilter]);

  const loadGroups = async () => {
    try {
      const data = await judgeGroupApi.getGroups() as any;
      setGroupConfigs(data.groups || []);
    } catch (err) {
      console.error('加载评审组失败', err);
    }
  };

  const loadTeams = async () => {
    setLoading(true);
    try {
      const params: any = {};
      if (selectedGroup) params.judge_group = selectedGroup;
      if (shortCodeFilter.trim()) params.short_code = shortCodeFilter.trim();
      if (memberNameFilter.trim()) params.member_name = memberNameFilter.trim();
      const data = await scoreApi.getTeamsForScoring(params) as any;
      setTeams(data.teams || []);
    } catch (err) {
      console.error('加载队伍失败', err);
    }
    setLoading(false);
  };

  const handleSelectTeam = (team: TeamScoreInfo) => {
    setSelectedTeam(team);
    // 如果已有该评委的评分，加载双轨人工维度
    const existingScore = team.human_scores.find(h => h.judge_name === judgeName);
    if (existingScore) {
      setHumanPresentationScore(existingScore.human_presentation_score || 0);
      setHumanCreativityScore(existingScore.human_creativity_score || 0);
      setHumanProcessScore(existingScore.human_process_score || 0);
      setHumanPerformanceScore(existingScore.human_performance_score || 0);
      setComment(existingScore.comment || '');
    } else {
      // 重置表单
      setHumanPresentationScore(0);
      setHumanCreativityScore(0);
      setHumanProcessScore(0);
      setHumanPerformanceScore(0);
      setComment('');
    }
  };

  const handleSubmit = async () => {
    if (!selectedTeam || !judgeName.trim()) {
      alert('请输入评委姓名并选择队伍');
      return;
    }
    if (!confirm(`确认提交对「${selectedTeam.team_name}」的评分？`)) return;

    setSubmitting(true);
    try {
      await scoreApi.submitHumanScore({
        team_id: selectedTeam.id,
        judge_name: judgeName.trim(),
        human_presentation_score: humanPresentationScore,
        human_creativity_score: humanCreativityScore,
        human_process_score: humanProcessScore,
        human_performance_score: humanPerformanceScore,
        comment: comment.trim() || undefined,
      });
      // 提交后自动计算双轨综合分
      try {
        await scoreApi.computeDualScore(selectedTeam.id);
      } catch (e) {
        console.error('综合分计算失败', e);
      }
      alert('评分提交成功');
      loadTeams(); // 刷新列表
      setSelectedTeam(null);
    } catch (err: any) {
      alert(err.message || '提交失败');
    }
    setSubmitting(false);
  };

  // P1-10 (2026-05-26): 重新评分处理
  const handleReScore = async () => {
    if (!selectedTeam) return;
    if (!confirm(`确定重新评分「${selectedTeam.team_name}」？\n当前评分将被覆盖（旧版本仍可在历史中查看）。`)) return;

    setReScoring(true);
    setReScoreError('');
    try {
      await scoreApi.reScoreTeam(selectedTeam.id);
      alert('重新评分完成');
      loadTeams(); // 刷新列表
      // 重新获取该队伍详情
      const updated = await scoreApi.getScoreResult(selectedTeam.id) as any;
      if (updated.score) {
        setSelectedTeam({
          ...selectedTeam,
          machine_score: updated.score.total_score,
          calibrated_score: updated.score.calibrated_score,
          anchor_level: updated.score.anchor_level,
          confidence: updated.score.confidence,
          flags: updated.score.flags || [],
          machine_details: {
            theme: updated.score.theme_score,
            presentation: updated.score.presentation_score,
            process: updated.score.process_score,
            ai_literacy: updated.score.ai_literacy_score,
          },
          machine_comments: {
            theme: updated.score.theme_comment,
            presentation: updated.score.presentation_comment,
            process: updated.score.process_comment,
            ai_literacy: updated.score.ai_literacy_comment,
            overall: updated.score.overall_comment,
          },
        });
      }
    } catch (err: any) {
      setReScoreError(err.message || '重新评分失败');
      alert(err.message || '重新评分失败');
    }
    setReScoring(false);
  };

  // P1-11 (2026-05-26): 评分历史折叠区
  const toggleHistory = async () => {
    if (!selectedTeam) return;
    if (!showHistory) {
      setHistoryLoading(true);
      try {
        const res = await scoreApi.getScoreHistory(selectedTeam.id) as any;
        setScoreHistory(res.history || []);
      } catch (err) {
        console.error('加载历史失败', err);
      }
      setHistoryLoading(false);
    }
    setShowHistory(!showHistory);
  };

  // 人工总分（双轨：0-60）
  const humanTotalScore = humanPresentationScore + humanCreativityScore + humanProcessScore + humanPerformanceScore;

  // 比赛评分模式
  const [scoringMode, setScoringMode] = useState<string>('mixed');
  const [competitionName, setCompetitionName] = useState<string>('');
  const [modeLoading, setModeLoading] = useState(false);

  // 评分维度配置
  const [dimensions, setDimensions] = useState<Record<string, DimensionConfig>>({});

  // P1-8 (2026-05-26): 前端过滤（锚点等级 + Flag）
  const filteredTeams = useMemo(() => {
    return teams.filter(team => {
      if (anchorLevelFilter && team.anchor_level !== anchorLevelFilter) return false;
      if (flagFilter) {
        const hasFlags = (team.flags?.length ?? 0) > 0;
        if (flagFilter === 'has_flag' && !hasFlags) return false;
        if (flagFilter === 'no_flag' && hasFlags) return false;
        if (flagFilter === 'must_review') {
          const hasMust = team.flags?.some(f => f.level === 'MUST_REVIEW') ?? false;
          if (!hasMust) return false;
        }
        if (flagFilter === 'suggest_review') {
          const hasSuggest = team.flags?.some(f => f.level === 'SUGGEST_REVIEW') ?? false;
          if (!hasSuggest) return false;
        }
      }
      return true;
    });
  }, [teams, anchorLevelFilter, flagFilter]);

  // 双轨人工维度配置（动态从API获取，失败回退默认）
  const humanDimensionRanges = useMemo(() => {
    const defaults = [
      { key: 'human_presentation', label: '产品表现力', score: humanPresentationScore, max: 25, color: 'blue' },
      { key: 'human_creativity', label: '创意深度', score: humanCreativityScore, max: 15, color: 'purple' },
      { key: 'human_process', label: '过程深度', score: humanProcessScore, max: 15, color: 'orange' },
      { key: 'human_performance', label: '现场表现', score: humanPerformanceScore, max: 5, color: 'pink' },
    ];
    if (!dimensions || Object.keys(dimensions).length === 0) {
      return defaults;
    }
    const colorMap: Record<string, string> = {
      human_presentation: 'blue',
      human_creativity: 'purple',
      human_process: 'orange',
      human_performance: 'pink',
    };
    const knownKeys = ['human_presentation', 'human_creativity', 'human_process', 'human_performance'];
    const entries = Object.entries(dimensions);
    const hasKnownKeys = entries.some(([k]) => knownKeys.includes(k));
    if (hasKnownKeys) {
      return entries
        .filter(([k]) => knownKeys.includes(k))
        .sort((a, b) => knownKeys.indexOf(a[0]) - knownKeys.indexOf(b[0]))
        .map(([key, config]) => ({
          key,
          label: config.name,
          score: key === 'human_presentation' ? humanPresentationScore :
                 key === 'human_creativity' ? humanCreativityScore :
                 key === 'human_process' ? humanProcessScore :
                 key === 'human_performance' ? humanPerformanceScore : 0,
          max: config.max_score,
          color: colorMap[key] || 'blue',
        }));
    }
    return defaults;
  }, [dimensions, humanPresentationScore, humanCreativityScore, humanProcessScore, humanPerformanceScore]);

  const handleExport = async () => {
    try {
      const response = await fetch('/api/scores/export');
      if (!response.ok) throw new Error('导出失败');
      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `scores_export_${new Date().toISOString().slice(0, 10)}.csv`;
      a.click();
      window.URL.revokeObjectURL(url);
    } catch (err: any) {
      alert(err.message || '导出失败');
    }
  };

  return (
    <div className="space-y-4">
      {/* 评委信息栏 */}
      <ScoringFilterBar
        judgeName={judgeName}
        onJudgeNameChange={setJudgeName}
        scoringMode={scoringMode}
        onModeChange={handleModeChange}
        modeLoading={modeLoading}
        selectedGroup={selectedGroup}
        onGroupChange={setSelectedGroup}
        groupConfigs={groupConfigs}
        shortCodeFilter={shortCodeFilter}
        onShortCodeChange={setShortCodeFilter}
        memberNameFilter={memberNameFilter}
        onMemberNameChange={setMemberNameFilter}
        anchorLevelFilter={anchorLevelFilter}
        onAnchorLevelChange={setAnchorLevelFilter}
        flagFilter={flagFilter}
        onFlagChange={setFlagFilter}
        onRefresh={loadTeams}
        onExport={handleExport}
        filteredCount={filteredTeams.length}
        totalHumanScoredCount={teams.filter(t => t.has_human_score).length}
        totalCompositeCount={teams.filter(t => t.composite_score !== null).length}
      />

      <div className="grid grid-cols-3 gap-4">
        {/* 队伍列表 */}
        <ScoringTeamList
          filteredTeams={filteredTeams}
          loading={loading}
          judgeName={judgeName}
          selectedTeamId={selectedTeam?.id ?? null}
          onSelectTeam={handleSelectTeam}
        />

        {/* 评分表单 */}
        <div className="bg-white rounded-lg shadow-sm col-span-2">
          {selectedTeam ? (
            <div className="p-6 space-y-4">
              <div className="flex items-center justify-between">
                <div>
                  <h3 className="text-lg font-bold text-gray-800">
                    {selectedTeam.team_name === 'nan' ? selectedTeam.short_code : selectedTeam.team_name}
                  </h3>
                  <p className="text-sm text-gray-500">{selectedTeam.school} | {selectedTeam.group_type}</p>
                </div>
                {selectedTeam.machine_score !== null && (
                  <div className="flex items-center gap-2">
                    {/* P1-10: 重新评分按钮 */}
                    <button
                      onClick={handleReScore}
                      disabled={reScoring}
                      className="px-3 py-1.5 bg-orange-100 text-orange-700 rounded hover:bg-orange-200 disabled:opacity-50 text-sm"
                    >
                      {reScoring ? '评分中...' : '重新评分'}
                    </button>
                    <div className="text-right">
                    {/* P1-8: 大字显示校准分 */}
                    {/* raw <= 5 的作品 AI 未能有效解析 */}
                    {selectedTeam.machine_score <= 5 ? (
                      <>
                        <div className="text-2xl font-bold text-gray-400">N/A</div>
                        <div className="text-xs text-orange-500 font-medium">
                          AI 未能有效解析作品
                        </div>
                        <div className="text-[10px] text-gray-400 mt-0.5">
                          请以现场评审分为准
                        </div>
                      </>
                    ) : (
                      <>
                        <div className="text-2xl font-bold text-indigo-600">
                          {selectedTeam.calibrated_score !== null && selectedTeam.calibrated_score !== undefined && !Number.isNaN(selectedTeam.calibrated_score)
                            ? selectedTeam.calibrated_score
                            : selectedTeam.machine_score}
                        </div>
                        <div className="text-xs text-gray-500">
                          AI评分（参考）
                        </div>
                      </>
                    )}
                    {/* 原始分 + 锚点等级 */}
                    <div className="text-[10px] text-gray-400 mt-0.5">
                      原始: {selectedTeam.machine_score}分
                      {selectedTeam.anchor_level && (
                        <span className={`ml-1 px-1 py-0.5 rounded-full border ${
                          selectedTeam.anchor_level === 'Lv0' ? 'bg-emerald-100 text-emerald-700 border-emerald-200' :
                          selectedTeam.anchor_level === 'Lv1' ? 'bg-sky-100 text-sky-700 border-sky-200' :
                          selectedTeam.anchor_level === 'Lv2' ? 'bg-amber-100 text-amber-700 border-amber-200' :
                          'bg-rose-100 text-rose-700 border-rose-200'
                        }`}>
                          {selectedTeam.anchor_level} · {selectedTeam.confidence || 'UNKNOWN'}
                        </span>
                      )}
                    </div>
                    {/* Flag 标签 */}
                    {selectedTeam.flags && selectedTeam.flags.length > 0 && (
                      <div className="flex flex-wrap justify-end gap-1 mt-1">
                        {selectedTeam.flags.map((f, i) => (
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
                  </div>
                )}
              </div>

              <AIDisplaySection team={selectedTeam} dimensions={dimensions} />

              {/* 人工评分维度 / AI推断维度 */}
              {scoringMode === 'ai_only' && selectedTeam.inferred_dimensions && selectedTeam.inferred_dimensions.total > 0 ? (
                <div className="bg-purple-50 rounded-lg p-4 space-y-2">
                  <div className="text-sm font-medium text-purple-700">AI 推断人工维度（准确性有限，仅供参考）</div>
                  <div className="grid grid-cols-5 gap-2 text-sm">
                    <div className="bg-white/60 rounded p-2 text-center">
                      <div className="text-blue-600 font-bold">{selectedTeam.inferred_dimensions.presentation ?? '-'}</div>
                      <div className="text-gray-500">表现力/25</div>
                    </div>
                    <div className="bg-white/60 rounded p-2 text-center">
                      <div className="text-purple-600 font-bold">{selectedTeam.inferred_dimensions.creativity ?? '-'}</div>
                      <div className="text-gray-500">创意/15</div>
                    </div>
                    <div className="bg-white/60 rounded p-2 text-center">
                      <div className="text-orange-600 font-bold">{selectedTeam.inferred_dimensions.process ?? '-'}</div>
                      <div className="text-gray-500">过程/15</div>
                    </div>
                    <div className="bg-white/60 rounded p-2 text-center">
                      <div className="text-pink-600 font-bold">{selectedTeam.inferred_dimensions.performance ?? '-'}</div>
                      <div className="text-gray-500">现场/5</div>
                    </div>
                    <div className="bg-white/80 rounded p-2 text-center border-2 border-purple-200">
                      <div className="text-purple-700 font-bold text-lg">{selectedTeam.inferred_dimensions.total}</div>
                      <div className="text-gray-500">推断总分/60</div>
                    </div>
                  </div>
                </div>
              ) : scoringMode === 'ai_only' ? (
                <div className="bg-gray-50 rounded-lg p-4 text-center text-sm text-gray-400">
                  该作品暂无 AI 推断维度分
                </div>
              ) : (
                <div className="space-y-3">
                  <div className="text-sm font-medium text-gray-700">人工评分（当前评委）</div>
                  {humanDimensionRanges.map(dim => (
                    <div key={dim.key} className="flex items-center gap-3">
                      <span className="w-24 text-sm text-gray-600">{dim.label}</span>
                      <input
                        type="number"
                        min={0}
                        max={dim.max}
                        step={0.5}
                        value={dim.score}
                        onChange={(e) => {
                          const val = parseFloat(e.target.value) || 0;
                          const clamped = Math.min(dim.max, Math.max(0, val));
                          if (dim.key === 'human_presentation') setHumanPresentationScore(clamped);
                          if (dim.key === 'human_creativity') setHumanCreativityScore(clamped);
                          if (dim.key === 'human_process') setHumanProcessScore(clamped);
                          if (dim.key === 'human_performance') setHumanPerformanceScore(clamped);
                        }}
                        className="border border-gray-300 rounded px-3 py-1.5 w-24 text-center font-mono"
                      />
                      <span className="text-sm text-gray-400">/ {dim.max}分</span>
                      <input
                        type="range"
                        min={0}
                        max={dim.max}
                        step={0.5}
                        value={dim.score}
                        onChange={(e) => {
                          const val = parseFloat(e.target.value);
                          if (dim.key === 'human_presentation') setHumanPresentationScore(val);
                          if (dim.key === 'human_creativity') setHumanCreativityScore(val);
                          if (dim.key === 'human_process') setHumanProcessScore(val);
                          if (dim.key === 'human_performance') setHumanPerformanceScore(val);
                        }}
                        className={`flex-1 h-2 bg-${dim.color}-200 rounded-lg appearance-none cursor-pointer accent-${dim.color}-500`}
                      />
                    </div>
                  ))}
                </div>
              )}

              {/* 人工总分 + 综合分 */}
              <div className="grid grid-cols-2 gap-4">
                <div className="flex items-center justify-between bg-gray-50 rounded-lg p-4">
                  <span className="text-gray-600">人工总分</span>
                  <div className="text-center">
                    <span className={`text-3xl font-bold ${
                      humanTotalScore >= 45 ? 'text-green-600' : humanTotalScore >= 30 ? 'text-yellow-600' : 'text-red-600'
                    }`}>
                      {humanTotalScore}
                    </span>
                    <span className="text-gray-400 text-lg">/ 60</span>
                  </div>
                </div>
                {selectedTeam.composite_score !== null && (
                  <div className="flex items-center justify-between bg-indigo-50 rounded-lg p-4">
                    <div>
                      <span className="text-gray-600">综合分</span>
                      <div className="text-xs text-gray-400">
                        {selectedTeam.scoring_mode === 'mixed' ? '混合模式 (AI 40% + 人工 60%)' : '纯AI模式（人工维度为AI推断，准确性有限）'}
                      </div>
                    </div>
                    <div className="text-center">
                      <span className="text-3xl font-bold text-indigo-600">
                        {selectedTeam.composite_score}
                      </span>
                      <span className="text-gray-400 text-lg">/ 100</span>
                    </div>
                  </div>
                )}
              </div>

              {/* 评语 */}
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">评委评语（可选）</label>
                <textarea
                  value={comment}
                  onChange={(e) => setComment(e.target.value)}
                  placeholder="请输入评语..."
                  rows={3}
                  className="w-full border border-gray-300 rounded px-3 py-2"
                />
              </div>

              {/* 提交按钮（mixed 模式显示，ai_only 隐藏） */}
              {scoringMode === 'mixed' && (
                <div className="flex justify-end gap-3">
                  <button
                    onClick={() => setSelectedTeam(null)}
                    className="px-6 py-2 bg-gray-100 text-gray-700 rounded hover:bg-gray-200"
                  >
                    取消
                  </button>
                  <button
                    onClick={handleSubmit}
                    disabled={submitting || !judgeName.trim()}
                    className="px-6 py-2 bg-indigo-600 text-white rounded hover:bg-indigo-700 disabled:opacity-50"
                  >
                    {submitting ? '提交中...' : '提交评分'}
                  </button>
                </div>
              )}
              {scoringMode === 'ai_only' && (
                <div className="flex justify-end">
                  <button
                    onClick={() => setSelectedTeam(null)}
                    className="px-6 py-2 bg-gray-100 text-gray-700 rounded hover:bg-gray-200"
                  >
                    关闭
                  </button>
                </div>
              )}

              {/* P1-11: 评分历史 */}
              <ScoreHistorySection
                showHistory={showHistory}
                historyLoading={historyLoading}
                scoreHistory={scoreHistory}
                onToggleHistory={toggleHistory}
              />
            </div>
          ) : (
            <div className="p-12 text-center text-gray-400">
              <div className="text-4xl mb-2">📝</div>
              <div>请从左侧选择要评分的队伍</div>
              <div className="text-sm mt-1">输入评委姓名后即可开始评分</div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
