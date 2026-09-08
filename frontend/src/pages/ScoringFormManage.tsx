import { useState, useEffect } from 'react';

interface Dimension {
  name: string;
  max_score: number;
  description?: string;
}

interface ScoringForm {
  id: number;
  name: string;
  dimensions: Dimension[];
  is_default: boolean;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export default function ScoringFormManage() {
  const [forms, setForms] = useState<ScoringForm[]>([]);
  const [activeForm, setActiveForm] = useState<ScoringForm | null>(null);
  const [loading, setLoading] = useState(true);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);
  const [editingForm, setEditingForm] = useState<ScoringForm | null>(null);

  // 新建表单数据
  const [newFormName, setNewFormName] = useState('');
  const [newDimensions, setNewDimensions] = useState<Dimension[]>([
    { name: '', max_score: 0, description: '' }
  ]);

  useEffect(() => {
    fetchForms();
    fetchActiveForm();
  }, []);

  const fetchForms = async () => {
    try {
      const res = await fetch('/api/scoring-forms');
      const data = await res.json();
      setForms(data);
    } catch (error) {
      console.error('获取评审表列表失败:', error);
    } finally {
      setLoading(false);
    }
  };

  const fetchActiveForm = async () => {
    try {
      const res = await fetch('/api/scoring-forms/active');
      if (res.ok) {
        const data = await res.json();
        setActiveForm(data);
      }
    } catch (error) {
      console.error('获取激活评审表失败:', error);
    }
  };

  const handleActivate = async (id: number) => {
    if (!confirm('确定要激活此评审表吗？激活后将影响后续评分。')) return;

    try {
      const res = await fetch(`/api/scoring-forms/${id}/activate`, {
        method: 'POST',
      });

      if (res.ok) {
        alert('激活成功！');
        fetchForms();
        fetchActiveForm();
      } else {
        alert('激活失败');
      }
    } catch (error) {
      alert('激活失败');
    }
  };

  const handleDelete = async (id: number) => {
    if (!confirm('确定要删除此评审表吗？此操作不可恢复。')) return;

    try {
      const res = await fetch(`/api/scoring-forms/${id}`, {
        method: 'DELETE',
      });

      if (res.ok) {
        alert('删除成功！');
        fetchForms();
      } else {
        const error = await res.json();
        alert('删除失败：' + error.detail);
      }
    } catch (error) {
      alert('删除失败');
    }
  };

  const handleCreate = async () => {
    if (!newFormName.trim()) {
      alert('请输入评审表名称');
      return;
    }

    // 验证维度
    const validDimensions = newDimensions.filter(d => d.name.trim() && d.max_score > 0);
    if (validDimensions.length === 0) {
      alert('请至少添加一个有效维度');
      return;
    }

    // 检查总分是否为100
    const totalScore = validDimensions.reduce((sum, d) => sum + d.max_score, 0);
    if (totalScore !== 100) {
      alert(`维度满分之和必须为100分，当前为${totalScore}分`);
      return;
    }

    try {
      const res = await fetch('/api/scoring-forms', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: newFormName,
          dimensions: validDimensions
        }),
      });

      if (res.ok) {
        alert('创建成功！');
        fetchForms();
        setShowCreateModal(false);
        setNewFormName('');
        setNewDimensions([{ name: '', max_score: 0, description: '' }]);
      } else {
        alert('创建失败');
      }
    } catch (error) {
      alert('创建失败');
    }
  };

  const handleEdit = (form: ScoringForm) => {
    setEditingForm(form);
    setShowEditModal(true);
  };

  const handleSaveEdit = async () => {
    if (!editingForm) return;

    try {
      const res = await fetch(`/api/scoring-forms/${editingForm.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: editingForm.name,
          dimensions: editingForm.dimensions
        }),
      });

      if (res.ok) {
        alert('保存成功！');
        fetchForms();
        setShowEditModal(false);
      } else {
        alert('保存失败');
      }
    } catch (error) {
      alert('保存失败');
    }
  };

  const addDimension = () => {
    setNewDimensions([...newDimensions, { name: '', max_score: 0, description: '' }]);
  };

  const removeDimension = (index: number) => {
    setNewDimensions(newDimensions.filter((_, i) => i !== index));
  };

  const updateDimension = (index: number, field: keyof Dimension, value: string | number) => {
    const updated = [...newDimensions];
    updated[index] = { ...updated[index], [field]: value };
    setNewDimensions(updated);
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-gray-500">加载中...</div>
      </div>
    );
  }

  return (
    <div className="p-6">
      {/* 顶部统计 */}
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-gray-800 mb-2">评审表管理</h1>
        <p className="text-gray-600">
          配置评分维度，支持自定义名称、满分值和说明
        </p>
      </div>

      {/* 当前激活评审表 */}
      {activeForm && (
        <div className="mb-6 p-4 bg-blue-50 border border-blue-200 rounded-lg">
          <div className="flex items-center justify-between mb-3">
            <div>
              <span className="text-sm text-blue-600 font-medium">当前激活评审表</span>
              <h3 className="text-lg font-bold text-gray-800 mt-1">{activeForm.name}</h3>
            </div>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            {activeForm.dimensions.map((dim, idx) => (
              <div key={idx} className="bg-white p-3 rounded border">
                <div className="text-sm text-gray-600">{dim.name}</div>
                <div className="text-xl font-bold text-gray-800">{dim.max_score}分</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 操作栏 */}
      <div className="mb-4 flex items-center justify-between">
        <div className="text-sm text-gray-600">
          共 {forms.length} 套评审表
        </div>
        <button
          onClick={() => setShowCreateModal(true)}
          className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700"
        >
          新建评审表
        </button>
      </div>

      {/* 评审表列表 */}
      <div className="space-y-4">
        {forms.map((form) => (
          <div
            key={form.id}
            className={`p-4 border rounded-lg ${
              form.is_active ? 'border-blue-500 bg-blue-50' : 'border-gray-200 bg-white'
            }`}
          >
            <div className="flex items-start justify-between">
              <div className="flex-1">
                <div className="flex items-center gap-2">
                  <h3 className="font-bold text-gray-800">{form.name}</h3>
                  {form.is_default && (
                    <span className="px-2 py-0.5 bg-gray-200 text-gray-600 text-xs rounded">
                      系统默认
                    </span>
                  )}
                  {form.is_active && (
                    <span className="px-2 py-0.5 bg-blue-600 text-white text-xs rounded">
                      激活中
                    </span>
                  )}
                </div>
                <div className="mt-2 flex flex-wrap gap-2">
                  {form.dimensions.map((dim, idx) => (
                    <span
                      key={idx}
                      className="px-2 py-1 bg-white border border-gray-300 rounded text-sm"
                    >
                      {dim.name}：{dim.max_score}分
                    </span>
                  ))}
                </div>
              </div>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => handleEdit(form)}
                  className="px-3 py-1 text-sm text-blue-600 border border-blue-600 rounded hover:bg-blue-50"
                >
                  编辑
                </button>
                {!form.is_active && !form.is_default && (
                  <button
                    onClick={() => handleActivate(form.id)}
                    className="px-3 py-1 text-sm text-white bg-blue-600 rounded hover:bg-blue-700"
                  >
                    激活
                  </button>
                )}
                {!form.is_default && (
                  <button
                    onClick={() => handleDelete(form.id)}
                    className="px-3 py-1 text-sm text-red-600 border border-red-600 rounded hover:bg-red-50"
                  >
                    删除
                  </button>
                )}
              </div>
            </div>
          </div>
        ))}
      </div>

      {/* 新建弹窗 */}
      {showCreateModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 w-full max-w-2xl max-h-[90vh] overflow-auto">
            <h2 className="text-xl font-bold mb-4">新建评审表</h2>

            <div className="mb-4">
              <label className="block text-sm text-gray-600 mb-1">评审表名称</label>
              <input
                type="text"
                value={newFormName}
                onChange={(e) => setNewFormName(e.target.value)}
                className="w-full border border-gray-300 rounded-lg p-2"
                placeholder="例如：2026年春季赛评审表"
              />
            </div>

            <div className="mb-4">
              <div className="flex items-center justify-between mb-2">
                <label className="text-sm text-gray-600">评分维度</label>
                <button
                  onClick={addDimension}
                  className="text-sm text-blue-600 hover:text-blue-700"
                >
                  + 添加维度
                </button>
              </div>

              <div className="space-y-3">
                {newDimensions.map((dim, idx) => (
                  <div key={idx} className="flex items-start gap-2 p-3 bg-gray-50 rounded">
                    <div className="flex-1">
                      <input
                        type="text"
                        value={dim.name}
                        onChange={(e) => updateDimension(idx, 'name', e.target.value)}
                        className="w-full border border-gray-300 rounded p-2 mb-2"
                        placeholder="维度名称"
                      />
                      <input
                        type="number"
                        value={dim.max_score}
                        onChange={(e) => updateDimension(idx, 'max_score', parseInt(e.target.value) || 0)}
                        className="w-24 border border-gray-300 rounded p-2"
                        placeholder="满分"
                        min={0}
                        max={100}
                      />
                      <span className="ml-1 text-gray-500">分</span>
                    </div>
                    <button
                      onClick={() => removeDimension(idx)}
                      className="text-red-500 hover:text-red-600"
                    >
                      删除
                    </button>
                  </div>
                ))}
              </div>

              <div className="mt-2 text-sm text-gray-500">
                维度满分之和：
                <span className={`font-bold ${
                  newDimensions.reduce((s, d) => s + d.max_score, 0) === 100 ? 'text-green-600' : 'text-red-600'
                }`}>
                  {newDimensions.reduce((s, d) => s + d.max_score, 0)} / 100
                </span>
              </div>
            </div>

            <div className="flex justify-end gap-2">
              <button
                onClick={() => {
                  setShowCreateModal(false);
                  setNewFormName('');
                  setNewDimensions([{ name: '', max_score: 0, description: '' }]);
                }}
                className="px-4 py-2 border border-gray-300 rounded-lg hover:bg-gray-50"
              >
                取消
              </button>
              <button
                onClick={handleCreate}
                className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700"
              >
                创建
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 编辑弹窗 */}
      {showEditModal && editingForm && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 w-full max-w-2xl max-h-[90vh] overflow-auto">
            <h2 className="text-xl font-bold mb-4">编辑评审表</h2>

            <div className="mb-4">
              <label className="block text-sm text-gray-600 mb-1">评审表名称</label>
              <input
                type="text"
                value={editingForm.name}
                onChange={(e) => setEditingForm({ ...editingForm, name: e.target.value })}
                className="w-full border border-gray-300 rounded-lg p-2"
              />
            </div>

            <div className="mb-4">
              <label className="text-sm text-gray-600 mb-2 block">评分维度</label>
              <div className="space-y-3">
                {editingForm.dimensions.map((dim, idx) => (
                  <div key={idx} className="flex items-center gap-2 p-3 bg-gray-50 rounded">
                    <input
                      type="text"
                      value={dim.name}
                      onChange={(e) => {
                        const updated = [...editingForm.dimensions];
                        updated[idx].name = e.target.value;
                        setEditingForm({ ...editingForm, dimensions: updated });
                      }}
                      className="flex-1 border border-gray-300 rounded p-2"
                      placeholder="维度名称"
                    />
                    <input
                      type="number"
                      value={dim.max_score}
                      onChange={(e) => {
                        const updated = [...editingForm.dimensions];
                        updated[idx].max_score = parseInt(e.target.value) || 0;
                        setEditingForm({ ...editingForm, dimensions: updated });
                      }}
                      className="w-20 border border-gray-300 rounded p-2"
                      min={0}
                      max={100}
                    />
                    <span className="text-gray-500">分</span>
                  </div>
                ))}
              </div>

              <div className="mt-2 text-sm text-gray-500">
                维度满分之和：
                <span className={`font-bold ${
                  editingForm.dimensions.reduce((s, d) => s + d.max_score, 0) === 100 ? 'text-green-600' : 'text-red-600'
                }`}>
                  {editingForm.dimensions.reduce((s, d) => s + d.max_score, 0)} / 100
                </span>
              </div>
            </div>

            <div className="flex justify-end gap-2">
              <button
                onClick={() => setShowEditModal(false)}
                className="px-4 py-2 border border-gray-300 rounded-lg hover:bg-gray-50"
              >
                取消
              </button>
              <button
                onClick={handleSaveEdit}
                className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700"
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
