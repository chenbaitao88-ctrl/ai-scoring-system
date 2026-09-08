import { useState, useEffect } from 'react';

interface Team {
  id: number;
  team_code: string;
  short_code: string;
  team_name: string;
  group_type: string;
  school: string;
  district: string;
  teacher: string;  // 兼容旧数据
  teacher_name: string;  // 指导老师姓名
  teacher_phone: string;  // 指导老师手机号
  members: any[];
  judge_group?: number;
  status: string;
  source: string;
  created_at: string;
}

interface Stats {
  pending: number;
  confirmed: number;
  total: number;
  pending_by_source: {
    excel: number;
    work: number;
  };
}

export default function TeamReview() {
  const [pendingTeams, setPendingTeams] = useState<Team[]>([]);
  const [confirmedTeams, setConfirmedTeams] = useState<Team[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState<'pending' | 'confirmed'>('pending');
  const [showEditModal, setShowEditModal] = useState(false);
  const [editingTeam, setEditingTeam] = useState<Team | null>(null);
  const [selectedTeams, setSelectedTeams] = useState<number[]>([]);

  useEffect(() => {
    fetchData();
  }, []);

  const fetchData = async () => {
    setLoading(true);
    try {
      // 获取统计
      const statsRes = await fetch('/api/teams/review-stats');
      const statsData = await statsRes.json();
      setStats(statsData);

      // 获取待确认队伍
      const pendingRes = await fetch('/api/teams/pending');
      const pendingData = await pendingRes.json();
      setPendingTeams(pendingData.teams);

      // 获取已确认队伍
      const confirmedRes = await fetch('/api/teams/confirmed');
      const confirmedData = await confirmedRes.json();
      setConfirmedTeams(confirmedData.teams);
    } catch (error) {
      console.error('获取数据失败:', error);
    } finally {
      setLoading(false);
    }
  };

  const handleConfirm = async (teamId: number) => {
    try {
      const res = await fetch(`/api/teams/${teamId}/confirm`, {
        method: 'POST',
      });

      if (res.ok) {
        alert('确认成功！');
        fetchData();
        setSelectedTeams(selectedTeams.filter(id => id !== teamId));
      } else {
        alert('确认失败');
      }
    } catch (error) {
      alert('确认失败');
    }
  };

  const handleBatchConfirm = async () => {
    if (selectedTeams.length === 0) {
      alert('请先选择队伍');
      return;
    }

    if (!confirm(`确定要批量确认 ${selectedTeams.length} 支队伍吗？`)) return;

    try {
      const res = await fetch('/api/teams/batch-confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ team_ids: selectedTeams }),
      });

      if (res.ok) {
        const data = await res.json();
        alert(`成功确认 ${data.confirmed_count} 支队伍！`);
        fetchData();
        setSelectedTeams([]);
      } else {
        alert('批量确认失败');
      }
    } catch (error) {
      alert('批量确认失败');
    }
  };

  const handleEdit = (team: Team) => {
    setEditingTeam(team);
    setShowEditModal(true);
  };

  const handleSaveEdit = async () => {
    if (!editingTeam) return;

    try {
      const res = await fetch(`/api/teams/${editingTeam.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          team_name: editingTeam.team_name,
          school: editingTeam.school,
          district: editingTeam.district,
          teacher: editingTeam.teacher,
          members: editingTeam.members
        }),
      });

      if (res.ok) {
        alert('保存成功！');
        fetchData();
        setShowEditModal(false);
      } else {
        alert('保存失败');
      }
    } catch (error) {
      alert('保存失败');
    }
  };

  const handleDelete = async (teamId: number) => {
    if (!confirm('确定要删除此队伍吗？此操作不可恢复。')) return;

    try {
      const res = await fetch(`/api/teams/${teamId}`, {
        method: 'DELETE',
      });

      if (res.ok) {
        alert('删除成功！');
        fetchData();
      } else {
        alert('删除失败');
      }
    } catch (error) {
      alert('删除失败');
    }
  };

  const toggleSelect = (teamId: number) => {
    if (selectedTeams.includes(teamId)) {
      setSelectedTeams(selectedTeams.filter(id => id !== teamId));
    } else {
      setSelectedTeams([...selectedTeams, teamId]);
    }
  };

  const toggleSelectAll = () => {
    const currentTeams = activeTab === 'pending' ? pendingTeams : confirmedTeams;
    if (selectedTeams.length === currentTeams.length) {
      setSelectedTeams([]);
    } else {
      setSelectedTeams(currentTeams.map(t => t.id));
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-gray-500">加载中...</div>
      </div>
    );
  }

  const currentTeams = activeTab === 'pending' ? pendingTeams : confirmedTeams;

  return (
    <div className="p-6">
      {/* 顶部统计 */}
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-gray-800 mb-2">队伍审核</h1>
        <p className="text-gray-600">
          审核自动生成的队伍信息，补充学校等详细信息后确认进入评分流程
        </p>
      </div>

      {/* 统计卡片 */}
      {stats && (
        <div className="grid grid-cols-4 gap-4 mb-6">
          <div className="p-4 bg-yellow-50 border border-yellow-200 rounded-lg">
            <div className="text-sm text-yellow-600">待确认</div>
            <div className="text-2xl font-bold text-gray-800">{stats.pending}</div>
          </div>
          <div className="p-4 bg-green-50 border border-green-200 rounded-lg">
            <div className="text-sm text-green-600">已确认</div>
            <div className="text-2xl font-bold text-gray-800">{stats.confirmed}</div>
          </div>
          <div className="p-4 bg-blue-50 border border-blue-200 rounded-lg">
            <div className="text-sm text-blue-600">作品生成</div>
            <div className="text-2xl font-bold text-gray-800">{stats.pending_by_source.work}</div>
          </div>
          <div className="p-4 bg-gray-50 border border-gray-200 rounded-lg">
            <div className="text-sm text-gray-600">Excel导入</div>
            <div className="text-2xl font-bold text-gray-800">{stats.pending_by_source.excel}</div>
          </div>
        </div>
      )}

      {/* 标签切换 */}
      <div className="mb-4 flex items-center gap-4">
        <button
          onClick={() => {
            setActiveTab('pending');
            setSelectedTeams([]);
          }}
          className={`px-4 py-2 rounded-lg ${
            activeTab === 'pending'
              ? 'bg-yellow-600 text-white'
              : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
          }`}
        >
          待确认 ({stats?.pending || 0})
        </button>
        <button
          onClick={() => {
            setActiveTab('confirmed');
            setSelectedTeams([]);
          }}
          className={`px-4 py-2 rounded-lg ${
            activeTab === 'confirmed'
              ? 'bg-green-600 text-white'
              : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
          }`}
        >
          已确认 ({stats?.confirmed || 0})
        </button>

        {activeTab === 'pending' && selectedTeams.length > 0 && (
          <button
            onClick={handleBatchConfirm}
            className="px-4 py-2 bg-teal-600 text-white rounded-lg hover:bg-teal-700"
          >
            批量确认 ({selectedTeams.length})
          </button>
        )}
      </div>

      {/* 队伍列表 */}
      <div className="space-y-3">
        {/* 全选 */}
        {currentTeams.length > 0 && (
          <div className="flex items-center gap-2 p-2 bg-gray-50 rounded">
            <input
              type="checkbox"
              checked={selectedTeams.length === currentTeams.length}
              onChange={toggleSelectAll}
              className="w-4 h-4"
            />
            <span className="text-sm text-gray-600">全选</span>
          </div>
        )}

        {currentTeams.map((team) => (
          <div
            key={team.id}
            className={`p-4 border rounded-lg ${
              team.status === 'pending'
                ? 'border-yellow-300 bg-yellow-50'
                : 'border-green-300 bg-green-50'
            }`}
          >
            <div className="flex items-start gap-3">
              {/* 选择框 */}
              <input
                type="checkbox"
                checked={selectedTeams.includes(team.id)}
                onChange={() => toggleSelect(team.id)}
                className="w-4 h-4 mt-1"
              />

              {/* 队伍信息 */}
              <div className="flex-1">
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-sm text-gray-500">{team.short_code}</span>
                  <h3 className="font-bold text-gray-800">{team.team_name}</h3>
                  <span className={`px-2 py-0.5 text-xs rounded ${
                    team.source === 'work'
                      ? 'bg-blue-100 text-blue-700'
                      : 'bg-gray-100 text-gray-600'
                  }`}>
                    {team.source === 'work' ? '作品生成' : 'Excel导入'}
                  </span>
                </div>

                <div className="flex flex-wrap gap-2 text-sm text-gray-600">
                  <span className="px-2 py-1 bg-white rounded border">
                    {team.group_type}
                  </span>
                  {team.school && (
                    <span className="px-2 py-1 bg-white rounded border">
                      {team.school}
                    </span>
                  )}
                  {team.district && (
                    <span className="px-2 py-1 bg-white rounded border">
                      {team.district}
                    </span>
                  )}
                  {team.teacher_name && (
                    <span className="px-2 py-1 bg-white rounded border">
                      指导老师：{team.teacher_name}
                    </span>
                  )}
                  {team.teacher_phone && (
                    <span className="px-2 py-1 bg-white rounded border">
                      老师手机号：{team.teacher_phone}
                    </span>
                  )}
                </div>

                {/* 缺失信息提示 */}
                {team.status === 'pending' && !team.school && (
                  <div className="mt-2 text-xs text-yellow-600">
                    ⚠️ 学校信息缺失，请编辑补充
                  </div>
                )}
              </div>

              {/* 操作按钮 */}
              <div className="flex items-center gap-2">
                <button
                  onClick={() => handleEdit(team)}
                  className="px-3 py-1 text-sm text-teal-600 border border-teal-600 rounded hover:bg-teal-50"
                >
                  编辑
                </button>
                {team.status === 'pending' && (
                  <button
                    onClick={() => handleConfirm(team.id)}
                    className="px-3 py-1 text-sm text-white bg-teal-600 rounded hover:bg-teal-700"
                  >
                    确认
                  </button>
                )}
                <button
                  onClick={() => handleDelete(team.id)}
                  className="px-3 py-1 text-sm text-red-600 border border-red-600 rounded hover:bg-red-50"
                >
                  删除
                </button>
              </div>
            </div>
          </div>
        ))}

        {currentTeams.length === 0 && (
          <div className="text-center py-12 text-gray-500">
            {activeTab === 'pending' ? '暂无待确认队伍' : '暂无已确认队伍'}
          </div>
        )}
      </div>

      {/* 编辑弹窗 */}
      {showEditModal && editingTeam && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 w-full max-w-lg">
            <h2 className="text-xl font-bold mb-4">编辑队伍信息</h2>

            <div className="space-y-4">
              <div>
                <label className="block text-sm text-gray-600 mb-1">团队名称</label>
                <input
                  type="text"
                  value={editingTeam.team_name}
                  onChange={(e) => setEditingTeam({ ...editingTeam, team_name: e.target.value })}
                  className="w-full border border-gray-300 rounded-lg p-2"
                />
              </div>

              <div>
                <label className="block text-sm text-gray-600 mb-1">学校名称</label>
                <input
                  type="text"
                  value={editingTeam.school}
                  onChange={(e) => setEditingTeam({ ...editingTeam, school: e.target.value })}
                  className="w-full border border-gray-300 rounded-lg p-2"
                  placeholder="请输入学校名称"
                />
              </div>

              <div>
                <label className="block text-sm text-gray-600 mb-1">区县</label>
                <input
                  type="text"
                  value={editingTeam.district}
                  onChange={(e) => setEditingTeam({ ...editingTeam, district: e.target.value })}
                  className="w-full border border-gray-300 rounded-lg p-2"
                  placeholder="请输入区县"
                />
              </div>

              <div>
                <label className="block text-sm text-gray-600 mb-1">指导老师</label>
                <input
                  type="text"
                  value={editingTeam.teacher}
                  onChange={(e) => setEditingTeam({ ...editingTeam, teacher: e.target.value })}
                  className="w-full border border-gray-300 rounded-lg p-2"
                  placeholder="请输入指导老师姓名"
                />
              </div>
            </div>

            <div className="flex justify-end gap-2 mt-6">
              <button
                onClick={() => setShowEditModal(false)}
                className="px-4 py-2 border border-gray-300 rounded-lg hover:bg-gray-50"
              >
                取消
              </button>
              <button
                onClick={handleSaveEdit}
                className="px-4 py-2 bg-teal-600 text-white rounded-lg hover:bg-teal-700"
              >
                保存
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
