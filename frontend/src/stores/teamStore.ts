/**
 * 队伍状态管理
 */
import { create } from 'zustand';
import { teamApi } from '../services/api';

interface WorkBrief {
  id: number;
  has_source: boolean;
  has_aigc_log: boolean;
  has_screenshots: boolean;
  is_parsed: boolean;
  code_language: string | null;
  code_line_count: number;
  plagiarism_flag: boolean;
}

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
  members: Array<{ name: string; grade: string }>;
  judge_group: number | null;
  work: WorkBrief | null;
  created_at: string | null;
  updated_at: string | null;
}

interface TeamStats {
  total: number;
  primary: number;
  junior: number;
  groups: Record<string, number>;
}

interface TeamState {
  teams: Team[];
  stats: TeamStats | null;
  loading: boolean;
  error: string | null;

  fetchTeams: (params?: { group_type?: string; judge_group?: number; search?: string }) => Promise<void>;
  fetchStats: () => Promise<void>;
  importFromExcel: (file: File) => Promise<{ imported: number; skipped: number; errors: string[] }>;
  assignGroups: (numGroups?: number) => Promise<void>;
}

export const useTeamStore = create<TeamState>((set) => ({
  teams: [],
  stats: null,
  loading: false,
  error: null,

  fetchTeams: async (params) => {
    set({ loading: true, error: null });
    try {
      const data = await teamApi.getTeams(params) as any;
      set({ teams: data.teams || [], loading: false });
    } catch (err: any) {
      set({ error: err.message, loading: false });
    }
  },

  fetchStats: async () => {
    try {
      const stats = await teamApi.getStats() as any;
      set({ stats });
    } catch (err: any) {
      set({ error: err.message });
    }
  },

  importFromExcel: async (file) => {
    set({ loading: true, error: null });
    try {
      const result = await teamApi.importFromExcel(file) as any;
      set({ loading: false });
      return result;
    } catch (err: any) {
      set({ error: err.message, loading: false });
      throw err;
    }
  },

  assignGroups: async (numGroups = 4) => {
    set({ loading: true, error: null });
    try {
      await teamApi.assignGroups(numGroups);
      set({ loading: false });
    } catch (err: any) {
      set({ error: err.message, loading: false });
    }
  },
}));
