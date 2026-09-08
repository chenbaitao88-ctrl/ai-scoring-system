import { useState, useEffect } from 'react';

interface TaskBook {
  id: number;
  name: string;
  group_type: string | null;
  content_md: string | null;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export default function TaskBookManage() {
  const [taskBooks, setTaskBooks] = useState<TaskBook[]>([]);
  const [activeTaskBook, setActiveTaskBook] = useState<TaskBook | null>(null);
  const [loading, setLoading] = useState(true);
  const [showUploadModal, setShowUploadModal] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);
  const [editingTaskBook, setEditingTaskBook] = useState<TaskBook | null>(null);
  const [editContent, setEditContent] = useState('');

  useEffect(() => {
    fetchTaskBooks();
    fetchActiveTaskBook();
  }, []);

  const fetchTaskBooks = async () => {
    try {
      const res = await fetch('/api/task-books');
      const data = await res.json();
      setTaskBooks(data);
    } catch (error) {
      console.error('获取任务书列表失败:', error);
    } finally {
      setLoading(false);
    }
  };

  const fetchActiveTaskBook = async () => {
    try {
      const res = await fetch('/api/task-books/active');
      if (res.ok) {
        const data = await res.json();
        setActiveTaskBook(data);
      }
    } catch (error) {
      console.error('获取激活任务书失败:', error);
    }
  };

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    const formData = new FormData();
    formData.append('file', file);

    try {
      const res = await fetch('/api/task-books/upload', {
        method: 'POST',
        body: formData,
      });

      if (res.ok) {
        alert('上传成功！');
        fetchTaskBooks();
        setShowUploadModal(false);
      } else {
        const error = await res.json();
        alert('上传失败：' + error.detail);
      }
    } catch (error) {
      alert('上传失败');
    }
  };

  const handleActivate = async (id: number) => {
    if (!confirm('确定要激活此任务书吗？激活后将影响后续AI评分。')) return;

    try {
      const res = await fetch(`/api/task-books/${id}/activate`, {
        method: 'POST',
      });

      if (res.ok) {
        alert('激活成功！');
        fetchTaskBooks();
        fetchActiveTaskBook();
      } else {
        alert('激活失败');
      }
    } catch (error) {
      alert('激活失败');
    }
  };

  const handleDeactivate = async (id: number) => {
    if (!confirm('确定要取消激活此任务书吗？')) return;

    try {
      const res = await fetch(`/api/task-books/${id}/deactivate`, {
        method: 'POST',
      });

      if (res.ok) {
        alert('取消激活成功！');
        fetchTaskBooks();
        fetchActiveTaskBook();
      } else {
        alert('取消激活失败');
      }
    } catch (error) {
      alert('取消激活失败');
    }
  };

  const handleDelete = async (id: number) => {
    if (!confirm('确定要删除此任务书吗？此操作不可恢复。')) return;

    try {
      const res = await fetch(`/api/task-books/${id}`, {
        method: 'DELETE',
      });

      if (res.ok) {
        alert('删除成功！');
        fetchTaskBooks();
      } else {
        alert('删除失败');
      }
    } catch (error) {
      alert('删除失败');
    }
  };

  const handleEdit = (taskBook: TaskBook) => {
    setEditingTaskBook(taskBook);
    setEditContent(taskBook.content_md || '');
    setShowEditModal(true);
  };

  const handleSaveEdit = async () => {
    if (!editingTaskBook) return;

    try {
      const res = await fetch(`/api/task-books/${editingTaskBook.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content_md: editContent }),
      });

      if (res.ok) {
        alert('保存成功！');
        fetchTaskBooks();
        setShowEditModal(false);
      } else {
        alert('保存失败');
      }
    } catch (error) {
      alert('保存失败');
    }
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
        <h1 className="text-2xl font-bold text-gray-800 mb-2">任务书管理</h1>
        <p className="text-gray-600">
          管理赛事任务书，支持Word上传、在线编辑、多套切换
        </p>
      </div>

      {/* 当前激活任务书 */}
      {activeTaskBook && (
        <div className="mb-6 p-4 bg-teal-50 border border-teal-200 rounded-lg">
          <div className="flex items-center justify-between">
            <div>
              <span className="text-sm text-teal-600 font-medium">当前激活任务书</span>
              <h3 className="text-lg font-bold text-gray-800 mt-1">{activeTaskBook.name}</h3>
              {activeTaskBook.group_type && (
                <span className="inline-block mt-2 px-2 py-1 bg-teal-100 text-teal-700 text-xs rounded">
                  {activeTaskBook.group_type}
                </span>
              )}
            </div>
            <button
              onClick={() => handleDeactivate(activeTaskBook.id)}
              className="px-4 py-2 bg-white border border-gray-300 rounded-lg hover:bg-gray-50"
            >
              取消激活
            </button>
          </div>
        </div>
      )}

      {/* 操作栏 */}
      <div className="mb-4 flex items-center justify-between">
        <div className="text-sm text-gray-600">
          共 {taskBooks.length} 套任务书
        </div>
        <button
          onClick={() => setShowUploadModal(true)}
          className="px-4 py-2 bg-teal-600 text-white rounded-lg hover:bg-teal-700"
        >
          上传任务书
        </button>
      </div>

      {/* 任务书列表 */}
      <div className="space-y-4">
        {taskBooks.map((tb) => (
          <div
            key={tb.id}
            className={`p-4 border rounded-lg ${
              tb.is_active ? 'border-teal-500 bg-teal-50' : 'border-gray-200 bg-white'
            }`}
          >
            <div className="flex items-start justify-between">
              <div className="flex-1">
                <div className="flex items-center gap-2">
                  <h3 className="font-bold text-gray-800">{tb.name}</h3>
                  {tb.is_active && (
                    <span className="px-2 py-0.5 bg-teal-600 text-white text-xs rounded">
                      激活中
                    </span>
                  )}
                </div>
                <div className="mt-1 text-sm text-gray-500">
                  {tb.group_type && <span className="mr-3">组别：{tb.group_type}</span>}
                  <span>创建时间：{tb.created_at}</span>
                </div>
              </div>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => handleEdit(tb)}
                  className="px-3 py-1 text-sm text-teal-600 border border-teal-600 rounded hover:bg-teal-50"
                >
                  编辑
                </button>
                {!tb.is_active ? (
                  <button
                    onClick={() => handleActivate(tb.id)}
                    className="px-3 py-1 text-sm text-white bg-teal-600 rounded hover:bg-teal-700"
                  >
                    激活
                  </button>
                ) : null}
                <button
                  onClick={() => handleDelete(tb.id)}
                  className="px-3 py-1 text-sm text-red-600 border border-red-600 rounded hover:bg-red-50"
                >
                  删除
                </button>
              </div>
            </div>
          </div>
        ))}

        {taskBooks.length === 0 && (
          <div className="text-center py-12 text-gray-500">
            暂无任务书，点击"上传任务书"添加
          </div>
        )}
      </div>

      {/* 上传弹窗 */}
      {showUploadModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 w-full max-w-md">
            <h2 className="text-xl font-bold mb-4">上传任务书</h2>
            <div className="mb-4">
              <label className="block text-sm text-gray-600 mb-2">
                支持 .doc / .docx 格式
              </label>
              <input
                type="file"
                accept=".doc,.docx"
                onChange={(e) => {
                  handleUpload(e);
                }}
                className="w-full border border-gray-300 rounded-lg p-2"
              />
            </div>
            <div className="flex justify-end gap-2">
              <button
                onClick={() => setShowUploadModal(false)}
                className="px-4 py-2 border border-gray-300 rounded-lg hover:bg-gray-50"
              >
                取消
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 编辑弹窗 */}
      {showEditModal && editingTaskBook && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 w-full max-w-4xl max-h-[90vh] overflow-hidden flex flex-col">
            <h2 className="text-xl font-bold mb-4">编辑任务书：{editingTaskBook.name}</h2>
            <div className="flex-1 overflow-auto">
              <label className="block text-sm text-gray-600 mb-2">
                任务书内容（Markdown格式）
              </label>
              <textarea
                value={editContent}
                onChange={(e) => setEditContent(e.target.value)}
                className="w-full h-96 border border-gray-300 rounded-lg p-3 font-mono text-sm"
                placeholder="在此编辑任务书内容..."
              />
            </div>
            <div className="flex justify-end gap-2 mt-4">
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
