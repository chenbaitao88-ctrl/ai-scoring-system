import React from 'react';

interface PlagiarismPanelProps {
  plagiarismResult: any;
  plagiarismInProgress: boolean;
  onDetect: () => void;
  onClearFlags: () => void;
}

const PlagiarismPanel: React.FC<PlagiarismPanelProps> = ({
  plagiarismResult,
  plagiarismInProgress,
  onDetect,
  onClearFlags,
}) => {
  return (
    <div className="bg-white p-6 rounded-lg shadow-sm space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-base font-medium text-gray-800">抄袭检测</h3>
        <div className="flex gap-2">
          <button
            onClick={onDetect}
            disabled={plagiarismInProgress}
            className="px-4 py-2 bg-red-600 text-white rounded hover:bg-red-700 disabled:opacity-50"
          >
            {plagiarismInProgress ? '检测中...' : '开始检测'}
          </button>
          {plagiarismResult && (
            <button
              onClick={onClearFlags}
              className="px-4 py-2 bg-gray-100 text-gray-700 rounded hover:bg-gray-200"
            >
              清除标记
            </button>
          )}
        </div>
      </div>

      {/* 检测说明 */}
      <div className="bg-gray-50 p-4 rounded-lg text-sm">
        <div className="font-medium text-gray-700 mb-2">检测方法</div>
        <div className="grid grid-cols-2 gap-2 text-gray-600">
          <div>• Token序列相似度：基于代码Token的3-gram匹配</div>
          <div>• AST结构相似度：基于Python AST节点类型分布</div>
          <div>• 综合相似度 = 0.6×Token + 0.4×AST</div>
          <div>• 高风险 ≥80% | 中风险 ≥60% | 低风险 ≥40%</div>
        </div>
      </div>

      {/* 检测结果统计 */}
      {plagiarismResult && (
        <div className="space-y-4">
          <div className="grid grid-cols-5 gap-3">
            <div className="bg-red-50 p-4 rounded-lg text-center">
              <div className="text-2xl font-bold text-red-600">{plagiarismResult.total_pairs}</div>
              <div className="text-sm text-red-500">总对比对</div>
            </div>
            <div className="bg-orange-50 p-4 rounded-lg text-center">
              <div className="text-2xl font-bold text-orange-600">{plagiarismResult.suspicious_pairs}</div>
              <div className="text-sm text-orange-500">可疑对</div>
            </div>
            <div className="bg-red-100 p-4 rounded-lg text-center">
              <div className="text-2xl font-bold text-red-700">{plagiarismResult.high_risk}</div>
              <div className="text-sm text-red-600">高风险</div>
            </div>
            <div className="bg-yellow-100 p-4 rounded-lg text-center">
              <div className="text-2xl font-bold text-yellow-700">{plagiarismResult.medium_risk}</div>
              <div className="text-sm text-yellow-600">中风险</div>
            </div>
            <div className="bg-green-100 p-4 rounded-lg text-center">
              <div className="text-2xl font-bold text-green-700">{plagiarismResult.low_risk}</div>
              <div className="text-sm text-green-600">低风险</div>
            </div>
          </div>

          {/* 可疑队伍列表 */}
          {plagiarismResult.results && plagiarismResult.results.length > 0 && (
            <div className="border border-gray-200 rounded-lg overflow-hidden">
              <div className="bg-gray-50 px-4 py-2 text-sm font-medium text-gray-700">
                可疑作品对（按相似度降序）
              </div>
              <div className="max-h-64 overflow-y-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-gray-50 border-b">
                      <th className="px-4 py-2 text-left">队伍1</th>
                      <th className="px-4 py-2 text-left">队伍2</th>
                      <th className="px-4 py-2 text-center">Token相似度</th>
                      <th className="px-4 py-2 text-center">结构相似度</th>
                      <th className="px-4 py-2 text-center">综合相似度</th>
                      <th className="px-4 py-2 text-center">风险等级</th>
                    </tr>
                  </thead>
                  <tbody>
                    {plagiarismResult.results.map((pair: any, idx: number) => (
                      <tr key={idx} className="border-b hover:bg-gray-50">
                        <td className="px-4 py-2">
                          <div className="font-medium">{pair.team1.name}</div>
                          <div className="text-xs text-gray-400">ID: {pair.team1.id}</div>
                        </td>
                        <td className="px-4 py-2">
                          <div className="font-medium">{pair.team2.name}</div>
                          <div className="text-xs text-gray-400">ID: {pair.team2.id}</div>
                        </td>
                        <td className="px-4 py-2 text-center">
                          <span className={`font-mono ${pair.token_similarity > 0.6 ? 'text-red-600 font-bold' : 'text-gray-600'}`}>
                            {(pair.token_similarity * 100).toFixed(1)}%
                          </span>
                        </td>
                        <td className="px-4 py-2 text-center">
                          <span className="font-mono text-gray-600">
                            {(pair.structure_similarity * 100).toFixed(1)}%
                          </span>
                        </td>
                        <td className="px-4 py-2 text-center">
                          <span className={`font-mono font-bold ${pair.overall_similarity > 0.8 ? 'text-red-600' : pair.overall_similarity > 0.6 ? 'text-orange-600' : 'text-yellow-600'}`}>
                            {(pair.overall_similarity * 100).toFixed(1)}%
                          </span>
                        </td>
                        <td className="px-4 py-2 text-center">
                          <span className={`px-2 py-1 rounded text-xs font-bold ${
                            pair.suspicion_level === 'high' ? 'bg-red-100 text-red-700' :
                            pair.suspicion_level === 'medium' ? 'bg-yellow-100 text-yellow-700' :
                            'bg-green-100 text-green-700'
                          }`}>
                            {pair.suspicion_level === 'high' ? '高' : pair.suspicion_level === 'medium' ? '中' : '低'}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {(!plagiarismResult.results || plagiarismResult.results.length === 0) && (
            <div className="text-center py-8 text-gray-400">
              未发现可疑抄袭作品
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export default PlagiarismPanel;
