/**
 * 结果展示与导出页面
 */
import React, { useEffect, useState } from 'react';
import { exportApi, scoreApi } from '../services/api';

interface ScoreSummary {
  total_teams: number;
  with_final_score: number;
  with_machine_score: number;
  with_human_score: number;
  flagged_count: number;
  primary_count: number;
  middle_count: number;
  score_ranges: Record<string, number>;
  results: Array<{
    id: number;
    short_code: string;
    team_name: string;
    school: string;
    district: string;
    group_type: string;
    judge_group: number;
    machine_score: number | null;
    machine_details: { theme: number; presentation: number; process: number; ai_literacy: number } | null;
    human_score_avg: number | null;
    human_judge_count: number;
    final_score: number | null;
    ranking: number | null;
    plagiarism_flag: boolean;
    // AI 过程性评价数据
    calibrated_score: number | null;
    anchor_level: string;
    confidence: string;
    flags: Array<{ rule_id: string; level: string; message: string }>;
    machine_comments: {
      theme: string | null;
      presentation: string | null;
      process: string | null;
      ai_literacy: string | null;
      overall: string | null;
    } | null;
  }>;
}

export default function Results() {
  const [summary, setSummary] = useState<ScoreSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [calculating, setCalculating] = useState(false);
  const [exporting, setExporting] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<'all' | 'primary' | 'middle'>('all');
  const [expandedRowId, setExpandedRowId] = useState<number | null>(null);

  useEffect(() => {
    loadSummary();
  }, []);

  const loadSummary = async () => {
    setLoading(true);
    try {
      const data = await exportApi.getScoreSummary() as unknown as ScoreSummary;
      setSummary(data);
    } catch (err) {
      console.error('加载汇总失败', err);
    }
    setLoading(false);
  };

  const handleCalculateAll = async () => {
    if (!confirm('将计算所有队伍的综合评分（机器60%+人工40%），确定继续？')) return;
    setCalculating(true);
    try {
      const result = await scoreApi.calculateAll() as any;
      alert(`计算完成！共计算 ${result.calculated} 支队伍，跳过 ${result.skipped} 支`);
      loadSummary();
    } catch (err: any) {
      alert(err.message || '计算失败');
    }
    setCalculating(false);
  };

  const handleExportExcel = async () => {
    setExporting('excel');
    try {
      const blob = await exportApi.exportExcel() as unknown as Blob;
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `评分汇总_${new Date().toISOString().slice(0, 10)}.xlsx`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err: any) {
      alert(err.message || '导出失败');
    }
    setExporting(null);
  };

  const handleExportWord = async () => {
    setExporting('word');
    try {
      const blob = await exportApi.exportWord() as unknown as Blob;
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `评分汇总_${new Date().toISOString().slice(0, 10)}.docx`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err: any) {
      alert(err.message || '导出失败');
    }
    setExporting(null);
  };

  const filteredResults = summary?.results.filter(r => {
    if (activeTab === 'primary') return r.group_type === '小学组';
    if (activeTab === 'middle') return r.group_type === '初中组';
    return true;
  }) || [];

  const displayedResults = filteredResults.slice(0, 100); // 最多显示100条

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-gray-400">加载中...</div>
      </div>
    );
  }

  if (!summary) {
    return (
      <div className="bg-white p-12 rounded-lg shadow-sm text-center">
        <div className="text-gray-400">加载失败，请刷新重试</div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* 操作栏 */}
      <div className="bg-white p-4 rounded-lg shadow-sm">
        <div className="flex gap-3 items-center flex-wrap">
          <button
            onClick={handleCalculateAll}
            disabled={calculating}
            className="px-4 py-2 bg-indigo-600 text-white rounded hover:bg-indigo-700 disabled:opacity-50"
          >
            {calculating ? '计算中...' : '计算综合评分'}
          </button>
          <button
            onClick={handleExportExcel}
            disabled={exporting !== null || summary.with_final_score === 0}
            className="px-4 py-2 bg-green-600 text-white rounded hover:bg-green-700 disabled:opacity-50"
          >
            {exporting === 'excel' ? '导出中...' : '导出Excel'}
          </button>
          <button
            onClick={handleExportWord}
            disabled={exporting !== null || summary.with_final_score === 0}
            className="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
          >
            {exporting === 'word' ? '导出中...' : '导出Word'}
          </button>
          <button
            onClick={loadSummary}
            className="px-4 py-2 bg-gray-100 text-gray-700 rounded hover:bg-gray-200"
          >
            刷新
          </button>
          <div className="ml-auto text-sm text-gray-500">
            {summary.with_final_score}/{summary.total_teams} 支队伍已有综合评分
          </div>
        </div>
      </div>

      {/* 统计卡片 */}
      <div className="grid grid-cols-5 gap-4">
        <div className="bg-white p-4 rounded-lg shadow-sm text-center">
          <div className="text-2xl font-bold text-blue-600">{summary.total_teams}</div>
          <div className="text-sm text-gray-500">参赛队伍</div>
        </div>
        <div className="bg-white p-4 rounded-lg shadow-sm text-center">
          <div className="text-2xl font-bold text-green-600">{summary.with_machine_score}</div>
          <div className="text-sm text-gray-500">已机器评分</div>
        </div>
        <div className="bg-white p-4 rounded-lg shadow-sm text-center">
          <div className="text-2xl font-bold text-purple-600">{summary.with_human_score}</div>
          <div className="text-sm text-gray-500">已人工评分</div>
        </div>
        <div className="bg-white p-4 rounded-lg shadow-sm text-center">
          <div className="text-2xl font-bold text-indigo-600">{summary.with_final_score}</div>
          <div className="text-sm text-gray-500">已计算综合分</div>
        </div>
        <div className="bg-white p-4 rounded-lg shadow-sm text-center">
          <div className="text-2xl font-bold text-red-600">{summary.flagged_count}</div>
          <div className="text-sm text-gray-500">疑似抄袭</div>
        </div>
      </div>

      {/* AI 过程性评价数据概览 */}
      {summary && (
        <div className="bg-white p-4 rounded-lg shadow-sm">
          <h3 className="text-sm font-medium text-gray-700 mb-3">AI 过程性评价数据概览</h3>
          <div className="grid grid-cols-4 gap-4">
            {(() => {
              const calibrated = summary.results.filter(r => r.calibrated_score !== null).length;
              const pending = summary.results.filter(r => r.machine_score !== null && r.calibrated_score === null).length;
              const flagged = summary.results.filter(r => r.flags && r.flags.length > 0).length;
              const anchorDist: Record<string, number> = {};
              summary.results.forEach(r => {
                const lv = r.anchor_level || 'Lv0';
                anchorDist[lv] = (anchorDist[lv] || 0) + 1;
              });
              return (
                <>
                  <div className="text-center">
                    <div className="text-2xl font-bold text-teal-600">{calibrated}</div>
                    <div className="text-xs text-gray-500">已校准</div>
                    {pending > 0 && <div className="text-xs text-orange-500 mt-0.5">待校准 {pending}</div>}
                  </div>
                  <div className="text-center">
                    <div className="text-2xl font-bold text-indigo-600">{anchorDist['Lv0'] || 0}</div>
                    <div className="text-xs text-gray-500">高置信 (Lv0)</div>
                    <div className="text-xs text-gray-400 mt-0.5">
                      Lv1 {(anchorDist['Lv1'] || 0)} · Lv2 {(anchorDist['Lv2'] || 0)} · Lv3 {(anchorDist['Lv3'] || 0)}
                    </div>
                  </div>
                  <div className="text-center">
                    <div className="text-2xl font-bold text-amber-600">{flagged}</div>
                    <div className="text-xs text-gray-500">Flag 标记队伍</div>
                  </div>
                  <div className="text-center">
                    <div className="text-2xl font-bold text-cyan-600">
                      {summary.with_machine_score > 0
                        ? Math.round((calibrated / summary.with_machine_score) * 100)
                        : 0}%
                    </div>
                    <div className="text-xs text-gray-500">校准覆盖率</div>
                  </div>
                </>
              );
            })()}
          </div>
        </div>
      )}

      {/* 分数段分布 */}
      <div className="bg-white p-4 rounded-lg shadow-sm">
        <h3 className="text-sm font-medium text-gray-700 mb-3">综合评分分布</h3>
        <div className="flex gap-2 items-end h-32">
          {Object.entries(summary.score_ranges).map(([range, count]) => {
            const maxCount = Math.max(...Object.values(summary.score_ranges), 1);
            const height = (count / maxCount) * 100;
            return (
              <div key={range} className="flex-1 flex flex-col items-center gap-1">
                <div className="text-sm font-bold text-gray-600">{count}</div>
                <div
                  className={`w-full rounded-t ${
                    range === '90-100' ? 'bg-green-500' :
                    range === '80-89' ? 'bg-green-400' :
                    range === '70-79' ? 'bg-yellow-400' :
                    range === '60-69' ? 'bg-orange-400' :
                    'bg-red-400'
                  }`}
                  style={{ height: `${Math.max(height, 4)}%` }}
                />
                <div className="text-xs text-gray-500">{range}</div>
              </div>
            );
          })}
        </div>
      </div>

      {/* 组别分布 */}
      <div className="grid grid-cols-2 gap-4">
        <div className="bg-green-50 p-4 rounded-lg">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-lg font-bold text-green-700">小学组</div>
              <div className="text-sm text-green-600">{summary.primary_count} 支队伍</div>
            </div>
            <div className="text-3xl">🏫</div>
          </div>
        </div>
        <div className="bg-orange-50 p-4 rounded-lg">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-lg font-bold text-orange-700">初中组</div>
              <div className="text-sm text-orange-600">{summary.middle_count} 支队伍</div>
            </div>
            <div className="text-3xl">🏢</div>
          </div>
        </div>
      </div>

      {/* 排名列表 */}
      <div className="bg-white rounded-lg shadow-sm overflow-hidden">
        <div className="px-4 py-3 bg-gray-50 border-b flex items-center justify-between">
          <h3 className="font-medium text-gray-700">评分排名</h3>
          <div className="flex gap-2">
            <button
              onClick={() => setActiveTab('all')}
              className={`px-3 py-1 text-sm rounded ${activeTab === 'all' ? 'bg-blue-100 text-blue-700' : 'text-gray-500 hover:bg-gray-100'}`}
            >
              全部
            </button>
            <button
              onClick={() => setActiveTab('primary')}
              className={`px-3 py-1 text-sm rounded ${activeTab === 'primary' ? 'bg-green-100 text-green-700' : 'text-gray-500 hover:bg-gray-100'}`}
            >
              小学组
            </button>
            <button
              onClick={() => setActiveTab('middle')}
              className={`px-3 py-1 text-sm rounded ${activeTab === 'middle' ? 'bg-orange-100 text-orange-700' : 'text-gray-500 hover:bg-gray-100'}`}
            >
              初中组
            </button>
          </div>
        </div>

        <div className="max-h-[50vh] overflow-y-auto">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 sticky top-0">
              <tr>
                <th className="px-4 py-2 text-left font-medium text-gray-500 w-16">排名</th>
                <th className="px-4 py-2 text-left font-medium text-gray-500 w-20">编号</th>
                <th className="px-4 py-2 text-left font-medium text-gray-500">团队名称</th>
                <th className="px-4 py-2 text-left font-medium text-gray-500">学校</th>
                <th className="px-4 py-2 text-center font-medium text-gray-500 w-16">组别</th>
                <th className="px-4 py-2 text-center font-medium text-gray-500 w-20">机器分</th>
                <th className="px-4 py-2 text-center font-medium text-gray-500 w-20">人工分</th>
                <th className="px-4 py-2 text-center font-medium text-gray-500 w-24">综合分</th>
                <th className="px-4 py-2 text-center font-medium text-gray-500 w-16">状态</th>
              </tr>
            </thead>
            <tbody>
              {displayedResults.map((r, idx) => (
                <React.Fragment key={r.id}>
                  <tr className={`border-b hover:bg-gray-50 ${r.plagiarism_flag ? 'bg-red-50' : idx % 2 === 0 ? 'bg-white' : 'bg-gray-50'}`}>
                    <td className="px-4 py-2">
                      {r.ranking ? (
                        <span className={`font-bold ${
                          r.ranking <= 3 ? 'text-yellow-600' :
                          r.ranking <= 10 ? 'text-blue-600' :
                          'text-gray-600'
                        }`}>
                          {r.ranking <= 3 ? '🥇🥈🥉'.charAt(r.ranking - 1) || r.ranking : r.ranking}
                        </span>
                      ) : '-'}
                    </td>
                    <td className="px-4 py-2 font-mono font-bold text-teal-700">{r.short_code}</td>
                    <td className="px-4 py-2">
                      <div className="font-medium text-gray-800">{r.team_name}</div>
                      {r.district && <div className="text-xs text-gray-400">{r.district}</div>}
                    </td>
                    <td className="px-4 py-2 text-gray-600">{r.school}</td>
                    <td className="px-4 py-2 text-center">
                      <span className={`px-2 py-0.5 rounded text-xs ${
                        r.group_type === '小学组' ? 'bg-green-100 text-green-700' : 'bg-orange-100 text-orange-700'
                      }`}>
                        {r.group_type}
                      </span>
                    </td>
                    <td className="px-4 py-2 text-center font-mono">
                      {r.machine_score !== null ? r.machine_score.toFixed(1) : '-'}
                    </td>
                    <td className="px-4 py-2 text-center font-mono">
                      {r.human_score_avg !== null ? r.human_score_avg.toFixed(1) : '-'}
                    </td>
                    <td className="px-4 py-2 text-center">
                      {r.final_score !== null ? (
                        <span className={`font-bold ${
                          r.final_score >= 80 ? 'text-green-600' :
                          r.final_score >= 60 ? 'text-yellow-600' :
                          'text-red-600'
                        }`}>
                          {r.final_score.toFixed(2)}
                        </span>
                      ) : '-'}
                    </td>
                    <td className="px-4 py-2 text-center">
                      {r.plagiarism_flag && (
                        <span className="px-2 py-0.5 rounded text-xs bg-red-100 text-red-700 font-bold">抄</span>
                      )}
                      {r.machine_score !== null && (
                        <button
                          onClick={() => setExpandedRowId(expandedRowId === r.id ? null : r.id)}
                          className="block mx-auto mt-1 text-[10px] text-gray-400 hover:text-indigo-600 underline"
                        >
                          {expandedRowId === r.id ? '收起' : '过程数据'}
                        </button>
                      )}
                    </td>
                  </tr>
                  {expandedRowId === r.id && (
                    <tr className="bg-gray-50 border-b">
                      <td colSpan={9} className="px-4 py-3">
                        <div className="grid grid-cols-4 gap-4 text-xs">
                          <div>
                            <div className="text-gray-500 mb-1">校准后参考分</div>
                            <div className="font-mono font-bold text-teal-700">
                              {r.calibrated_score !== null ? r.calibrated_score.toFixed(1) : '-'}
                            </div>
                          </div>
                          <div>
                            <div className="text-gray-500 mb-1">锚点等级</div>
                            <div className="font-medium text-indigo-700">{r.anchor_level || '-'}</div>
                          </div>
                          <div>
                            <div className="text-gray-500 mb-1">置信度</div>
                            <div className="font-medium text-gray-700">{r.confidence || '-'}</div>
                          </div>
                          <div>
                            <div className="text-gray-500 mb-1">Flag</div>
                            <div className="font-medium">
                              {r.flags && r.flags.length > 0 ? (
                                <span className="text-red-600">{r.flags.length} 条标记</span>
                              ) : (
                                <span className="text-gray-400">-</span>
                              )}
                            </div>
                          </div>
                        </div>
                        {r.machine_details && (
                          <div className="mt-3 grid grid-cols-4 gap-2">
                            {(() => {
                              const dims = [
                                { key: 'theme', label: '主题立意', max: 20, barColor: 'bg-blue-500' },
                                { key: 'presentation', label: '产品表现力', max: 30, barColor: 'bg-purple-500' },
                                { key: 'process', label: '过程完整性', max: 30, barColor: 'bg-orange-500' },
                                { key: 'ai_literacy', label: 'AI素养', max: 20, barColor: 'bg-pink-500' },
                              ];
                              return dims.map((dim) => {
                                const score = (r.machine_details as any)[dim.key];
                                if (score === undefined || score === null) return null;
                                const pct = (score / dim.max) * 100;
                                return (
                                  <div key={dim.key} className="bg-white rounded p-2">
                                    <div className="flex justify-between text-gray-600 mb-1">
                                      <span>{dim.label}</span>
                                      <span className="font-mono font-bold">{score}/{dim.max}</span>
                                    </div>
                                    <div className="w-full bg-gray-200 h-1.5 rounded-full">
                                      <div
                                        className={`h-1.5 rounded-full ${dim.barColor}`}
                                        style={{ width: `${pct}%` }}
                                      />
                                    </div>
                                  </div>
                                );
                              });
                            })()}
                          </div>
                        )}
                        {r.flags && r.flags.length > 0 && (
                          <div className="mt-3 flex flex-wrap gap-1">
                            {r.flags.map((f, i) => (
                              <span key={i} className={`text-xs px-2 py-1 rounded ${
                                f.level === 'must_review' ? 'bg-red-100 text-red-700' : 'bg-yellow-100 text-yellow-700'
                              }`}>
                                {f.message}
                              </span>
                            ))}
                          </div>
                        )}
                      </td>
                    </tr>
                  )}
                </React.Fragment>
              ))}
            </tbody>
          </table>
          {filteredResults.length === 0 && (
            <div className="p-8 text-center text-gray-400">暂无数据</div>
          )}
          {filteredResults.length > 100 && (
            <div className="p-2 text-center text-sm text-gray-400 bg-gray-50">
              显示前100条，共{filteredResults.length}条
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
