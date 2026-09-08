/**
 * 页面布局组件 - 简化版
 */
import type { ReactNode } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useEffect, useState } from 'react';
import { competitionApi } from '../services/api';

interface LayoutProps {
  children: ReactNode;
}

const menuItems = [
  { value: '/', label: '仪表盘' },
  { value: '/teams', label: '队伍管理' },
  { value: '/team-review', label: '队伍审核' },
  { value: '/scoring', label: '评分界面' },
  { value: '/batch-scoring', label: '批量评分' },
  { value: '/scoring-pipeline', label: '评分流水线' },
  { value: '/results', label: '结果导出' },
];

const configItems = [
  { value: '/task-books', label: '任务书管理' },
  { value: '/scoring-forms', label: '评审表配置' },
  { value: '/evidence-packages', label: '证据包验证' },
  { value: '/pipeline-tasks', label: '流水线任务' },
];

export default function Layout({ children }: LayoutProps) {
  const location = useLocation();
  const navigate = useNavigate();
  const [competitionName, setCompetitionName] = useState('创意编程评分系统');

  useEffect(() => {
    competitionApi.getCurrent().then((res) => {
      if (res.competition?.name) {
        setCompetitionName(res.competition.name);
      }
    }).catch(() => {
      // fallback to default
    });
  }, []);

  return (
    <div className="min-h-screen flex">
      {/* 侧边栏 */}
      <aside className="hidden md:block w-56 shrink-0 bg-white border-r border-gray-200">
        <div className="h-16 flex items-center justify-center border-b border-gray-200 px-3">
          <h1 className="text-sm font-semibold text-teal-600 text-center leading-tight">{competitionName}</h1>
        </div>
        <nav className="p-2">
          {menuItems.map((item) => (
            <div
              key={item.value}
              onClick={() => navigate(item.value)}
              className={`px-4 py-3 cursor-pointer rounded ${
                location.pathname === item.value
                  ? 'bg-blue-50 text-blue-600'
                  : 'text-gray-700 hover:bg-gray-50'
              }`}
            >
              {item.label}
            </div>
          ))}

          {/* 系统配置分组 */}
          <div className="mt-4 pt-4 border-t border-gray-200">
            <div className="px-4 py-2 text-xs text-gray-400 font-medium">
              系统配置
            </div>
            {configItems.map((item) => (
              <div
                key={item.value}
                onClick={() => navigate(item.value)}
                className={`px-4 py-3 cursor-pointer rounded ${
                  location.pathname === item.value
                    ? 'bg-blue-50 text-blue-600'
                    : 'text-gray-700 hover:bg-gray-50'
                }`}
              >
                {item.label}
              </div>
            ))}
          </div>
        </nav>
      </aside>

      {/* 主内容区 */}
      <main className="flex-1 min-w-0">
        <header className="h-16 bg-white border-b border-gray-200 flex items-center justify-center px-6">
          <h1 className="text-lg font-bold text-teal-700 text-center">
            {competitionName}
          </h1>
        </header>
        <nav className="md:hidden bg-white border-b border-gray-200 px-3 py-2">
          <label className="flex items-center gap-2 text-sm">
            页面导航
            <select aria-label="页面导航" value={location.pathname} onChange={event => navigate(event.target.value)} className="min-w-0 flex-1 border rounded p-2">
              {[...menuItems, ...configItems].map(item => <option key={item.value} value={item.value}>{item.label}</option>)}
            </select>
          </label>
        </nav>
        <div className="p-3 sm:p-8 bg-gray-50 min-h-[calc(100vh-4rem)]">
          {children}
        </div>
      </main>
    </div>
  );
}
