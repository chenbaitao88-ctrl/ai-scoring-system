/**
 * 仪表盘页面 - 简化版
 */
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTeamStore } from '../stores/teamStore';
import { judgeGroupApi, competitionApi } from '../services/api';

interface GroupInfo {
  group_index: number;
  group_name: string;
  team_count: number;
}

export default function Dashboard() {
  const navigate = useNavigate();
  const { stats, fetchStats, importFromExcel } = useTeamStore();
  const [importing, setImporting] = useState(false);
  const [groupConfigs, setGroupConfigs] = useState<GroupInfo[]>([]);
  const [competitionName, setCompetitionName] = useState('');

  useEffect(() => {
    fetchStats();
    loadGroupConfig();
    loadCompetition();
  }, []);

  const loadCompetition = async () => {
    try {
      const res = await competitionApi.getCurrent();
      if (res.competition?.name) {
        setCompetitionName(res.competition.name);
      }
    } catch (err) {
      console.error('加载赛事信息失败', err);
    }
  };

  const loadGroupConfig = async () => {
    try {
      const data = await judgeGroupApi.getGroups() as any;
      setGroupConfigs(data.groups || []);
    } catch (err) {
      console.error('加载评审组配置失败', err);
    }
  };

  const handleImportTeams = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    setImporting(true);
    try {
      const result = await importFromExcel(file);
      alert(`导入成功！新增 ${result.imported} 支队伍，跳过 ${result.skipped} 支`);
      fetchStats();
      loadGroupConfig();
    } catch (err: any) {
      alert(err.message || '导入失败');
    }
    setImporting(false);
  };

  return (
    <div className="space-y-6">
      {/* 赛事信息 */}
      {competitionName && (
        <div className="bg-white p-4 rounded-lg shadow-sm">
          <h2 className="text-lg font-bold text-teal-700">{competitionName}</h2>
          <p className="text-sm text-gray-500 mt-1">当前赛事</p>
        </div>
      )}

      {/* 统计卡片 */}
      <div className="grid grid-cols-4 gap-4">
        <div className="bg-white p-6 rounded-lg shadow-sm">
          <div className="text-sm text-gray-500">队伍总数</div>
          <div className="text-3xl font-bold text-gray-800 mt-2">{stats?.total || 0}</div>
        </div>
        <div className="bg-white p-6 rounded-lg shadow-sm">
          <div className="text-sm text-gray-500">小学组</div>
          <div className="text-3xl font-bold text-green-600 mt-2">{stats?.primary || 0}</div>
        </div>
        <div className="bg-white p-6 rounded-lg shadow-sm">
          <div className="text-sm text-gray-500">初中组</div>
          <div className="text-3xl font-bold text-orange-600 mt-2">{stats?.junior || 0}</div>
        </div>
        <div className="bg-white p-6 rounded-lg shadow-sm">
          <div className="text-sm text-gray-500">已评分</div>
          <div className="text-3xl font-bold text-blue-600 mt-2">0</div>
        </div>
      </div>

      {/* 评审组分配情况 */}
      <div className="bg-white p-6 rounded-lg shadow-sm">
        <h3 className="text-base font-medium text-gray-800 mb-4">评审组分配情况</h3>
        <div className={`grid gap-4`} style={{ gridTemplateColumns: `repeat(${Math.min(groupConfigs.length || 4, 6)}, 1fr)` }}>
          {groupConfigs.length > 0 ? (
            groupConfigs.map((group) => (
              <div key={group.group_index} className="text-center p-4 bg-gray-50 rounded">
                <div className="text-sm text-gray-600">{group.group_name}</div>
                <div className="text-2xl font-semibold text-gray-800 mt-1">{group.team_count || 0}</div>
                <div className="text-xs text-gray-500">支队伍</div>
              </div>
            ))
          ) : (
            // 默认显示4组
            [1, 2, 3, 4].map((i) => (
              <div key={i} className="text-center p-4 bg-gray-50 rounded">
                <div className="text-sm text-gray-600">第{i}评审组</div>
                <div className="text-2xl font-semibold text-gray-800 mt-1">{stats?.groups?.[`group_${i}`] || 0}</div>
                <div className="text-xs text-gray-500">支队伍</div>
              </div>
            ))
          )}
        </div>
      </div>

      {/* 快捷操作 */}
      <div className="bg-white p-6 rounded-lg shadow-sm">
        <h3 className="text-base font-medium text-gray-800 mb-4">快捷操作</h3>
        <div className="flex gap-4">
          <label className="px-4 py-2 bg-blue-600 text-white rounded cursor-pointer hover:bg-blue-700">
            {importing ? '导入中...' : '导入队伍Excel'}
            <input
              type="file"
              accept=".xlsx,.xls"
              className="hidden"
              onChange={handleImportTeams}
              disabled={importing}
            />
          </label>
          <button
            className="px-4 py-2 bg-gray-100 text-gray-700 rounded hover:bg-gray-200"
            onClick={() => navigate('/teams')}
          >
            导入作品ZIP
          </button>
          <button
            className="px-4 py-2 bg-gray-100 text-gray-700 rounded hover:bg-gray-200"
            onClick={() => navigate('/results')}
          >
            导出结果
          </button>
        </div>
      </div>

      {/* 使用说明 */}
      <div className="bg-white p-6 rounded-lg shadow-sm">
        <h3 className="text-base font-medium text-gray-800 mb-4">使用说明</h3>
        <div className="text-sm text-gray-600 space-y-2">
          <p>1. 点击「导入队伍Excel」上传队伍信息表，系统自动解析并入库</p>
          <p>2. 在「队伍管理」页面配置评审组，并批量上传作品ZIP文件</p>
          <p>3. 在「评分界面」进行人工评分，系统自动计算机器评分</p>
          <p>4. 在「结果导出」页面导出Excel汇总表和Word记录表</p>
        </div>
      </div>
    </div>
  );
}
