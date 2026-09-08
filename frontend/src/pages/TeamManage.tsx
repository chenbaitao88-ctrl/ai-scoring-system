/**
 * 队伍管理页面 - 含评审组配置、作品上传
 */
import { useEffect, useState } from 'react';
import { useTeamStore } from '../stores/teamStore';
import { judgeGroupApi, workApi, scoreApi, plagiarismApi, competitionApi, batchScoringApi } from '../services/api';
import type { DimensionConfig } from '../services/api';
import { TeamFilterBar, TeamActionBar, PlagiarismPanel, WorksPanel, ScoringPanel, GroupConfigPanel, TeamListTable, TeamDetailModal } from '../components/team-manage';

interface GroupConfig {
  group_index: number;
  group_name: string;
  description: string;
  judges: string[];
  team_count?: number;
}

interface WorksStatus {
  total_teams: number;
  works_uploaded: number;
  works_parsed: number;
  works_with_source: number;
  works_with_aigc: number;
  works_with_screenshots: number;
  works_flagged: number;
  upload_progress: string;
}

export default function TeamManage() {
  const { teams, fetchTeams, fetchStats, importFromExcel } = useTeamStore();
  const [search, setSearch] = useState('');
  const [groupFilter, setGroupFilter] = useState('');
  const [importing, setImporting] = useState(false);

  // 评审组配置相关
  const [showGroupConfig, setShowGroupConfig] = useState(false);
  const [groupConfigs, setGroupConfigs] = useState<GroupConfig[]>([]);
  const [savingGroups, setSavingGroups] = useState(false);

  // 作品管理相关
  const [worksStatus, setWorksStatus] = useState<WorksStatus | null>(null);
  const [uploadingWorks, setUploadingWorks] = useState(false);
  const [uploadResult, setUploadResult] = useState<any>(null);
  const [showWorksPanel, setShowWorksPanel] = useState(false);

  const [reScoringTeamId, setReScoringTeamId] = useState<number | null>(null);

  // AI评分相关
  const [scoringStats, setScoringStats] = useState<any>(null);
  const [scoringInProgress, setScoringInProgress] = useState(false);
  const [scoringResult, setScoringResult] = useState<any>(null);
  const [showScoringPanel, setShowScoringPanel] = useState(false);
  const [batchTasks, setBatchTasks] = useState<any[]>([]);

  // 模型选择相关
  const [availableModels, setAvailableModels] = useState<any[]>([]);
  const [selectedModel, setSelectedModel] = useState('qwen3.8-max');
  const [parallelEnabled, setParallelEnabled] = useState(true);
  const [concurrency, setConcurrency] = useState(5);

  // 抄袭检测相关
  const [showPlagiarismPanel, setShowPlagiarismPanel] = useState(false);
  const [plagiarismResult, setPlagiarismResult] = useState<any>(null);
  const [plagiarismInProgress, setPlagiarismInProgress] = useState(false);

  // 队伍详情弹窗
  const [selectedTeam, setSelectedTeam] = useState<any>(null);
  const [teamScoreDetail, setTeamScoreDetail] = useState<any>(null);
  const [allScoreVersions, setAllScoreVersions] = useState<any[]>([]);
  const [selectedVersionId, setSelectedVersionId] = useState<number | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);

  // 赛事配置
  const [dimensions, setDimensions] = useState<Record<string, DimensionConfig>>({});
  const [namingExample, setNamingExample] = useState('组别+姓名.zip');

  useEffect(() => {
    fetchTeams();
    fetchStats();
    loadGroupConfig();
    loadWorksStatus();
    loadScoringStats();
    loadAvailableModels();
    loadCompetitionConfig();
  }, []);

  const loadCompetitionConfig = async () => {
    try {
      const res = await competitionApi.getCurrent();
      if (res.competition?.naming_example) {
        setNamingExample(res.competition.naming_example);
      }
      if (res.competition?.id) {
        const dims = await competitionApi.getDimensions(res.competition.id);
        if (dims && Object.keys(dims).length > 0) {
          setDimensions(dims);
        }
      }
    } catch (err) {
      console.error('加载赛事配置失败', err);
    }
  };

  const loadAvailableModels = async () => {
    try {
      const data = await scoreApi.getAvailableModels() as any;
      setAvailableModels(data.models || []);
    } catch (err) {
      console.error('加载模型列表失败', err);
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

  const loadWorksStatus = async () => {
    try {
      const data = await workApi.getWorksStatus() as any;
      setWorksStatus(data);
    } catch (err) {
      console.error('加载作品状态失败', err);
    }
  };

  const loadScoringStats = async () => {
    try {
      const data = await scoreApi.getScoringStats() as any;
      setScoringStats(data);
    } catch (err) {
      console.error('加载评分统计失败', err);
    }
  };

  const loadBatchTasks = async () => {
    try {
      const data = await batchScoringApi.listTasks() as any;
      setBatchTasks(data.tasks || []);
    } catch (err) {
      console.error('加载评分任务历史失败', err);
    }
  };

  // ScoringPanel 薄包装回调（业务逻辑保留在父组件）
  const handleAdoptBestScore = async () => {
    if (!confirm('将对每个队伍采纳最高分版本，确定继续？')) return;
    try {
      const r = await (scoreApi as any).adoptBestScore() as any;
      alert(r.message);
      fetchTeams();
    } catch (err: any) {
      alert('操作失败: ' + (err.message || '未知错误'));
    }
  };

  const handleModelChange = (modelId: string) => setSelectedModel(modelId);
  const handleToggleParallel = () => setParallelEnabled(!parallelEnabled);
  const handleConcurrencyChange = (n: number) => setConcurrency(n);

  const handleAutoScoreAll = async () => {
    const modelInfo = availableModels.find(m => m.id === selectedModel);
    const confirmMsg = `将对所有已解析作品进行AI自动评分：

模型: ${modelInfo?.name || selectedModel} (${modelInfo?.description || ''})
并行: ${parallelEnabled ? `是 (并发${concurrency}个)` : '否'}

确定继续？（评分在后台执行，可关闭此页面）`;

    if (!confirm(confirmMsg)) return;
    setScoringInProgress(true);
    setScoringResult(null);
    try {
      // 启动异步批量评分
      const startResult = await scoreApi.autoScoreAllAsync(selectedModel, parallelEnabled, concurrency) as any;

      if (!startResult.task_id) {
        alert(startResult.message || '没有待评分的作品');
        setScoringInProgress(false);
        return;
      }

      const taskId = startResult.task_id;
      // 不用alert，直接显示进度
      setScoringResult({
        total: startResult.total,
        success: 0,
        failed: 0,
        status: 'running',
        model_name: startResult.model_name,
        concurrency: startResult.concurrency
      });

      // 轮询任务状态，每3秒检查一次
      const pollInterval = setInterval(async () => {
        try {
          const status = await scoreApi.getAsyncTaskStatus(taskId) as any;

          if (!status || !status.task) {
            clearInterval(pollInterval);
            setScoringResult((prev: any) => ({ ...prev, status: 'error', error: '无法获取任务状态' }));
            setScoringInProgress(false);
            return;
          }

          // 更新本地状态显示进度
          setScoringResult({
            total: status.task.total,
            success: status.task.success,
            failed: status.task.failed,
            status: status.task.status,
            errors: status.task.errors
          });

          // 如果任务完成或失败，停止轮询
          if (status.task.status === 'completed' || status.task.status === 'failed') {
            clearInterval(pollInterval);
            setScoringInProgress(false);
            loadScoringStats();
            fetchTeams();
          }
        } catch (err) {
          console.error('轮询任务状态失败', err);
        }
      }, 3000);

    } catch (err: any) {
      alert(err.message || '启动评分失败');
      setScoringInProgress(false);
    }
  };

  // 抄袭检测
  const handlePlagiarismDetect = async () => {
    if (!confirm('将对所有Python作品进行抄袭检测，确定继续？')) return;
    setPlagiarismInProgress(true);
    try {
      const result = await plagiarismApi.detect('Python') as any;
      setPlagiarismResult(result);
      fetchTeams(); // 刷新以显示标记状态
    } catch (err: any) {
      alert(err.message || '检测失败');
    }
    setPlagiarismInProgress(false);
  };

  const handleClearPlagiarismFlags = async () => {
    if (!confirm('确定清除所有抄袭标记？')) return;
    try {
      await plagiarismApi.clearFlags();
      setPlagiarismResult(null);
      fetchTeams();
      alert('已清除所有抄袭标记');
    } catch (err: any) {
      alert(err.message || '清除失败');
    }
  };

  const handleViewTeamDetail = async (team: any) => {
    setSelectedTeam(team);
    setLoadingDetail(true);
    try {
      const data = await scoreApi.getScoreResult(team.id) as any;
      setTeamScoreDetail(data.score);
      setAllScoreVersions(data.all_scores || []);
      // 默认选中已采用的版本，否则选最新的
      const adoptedOrLatest = data.all_scores?.find((s: any) => s.is_adopted) || data.all_scores?.[0];
      setSelectedVersionId(adoptedOrLatest?.id || null);
    } catch (err) {
      setTeamScoreDetail(null);
      setAllScoreVersions([]);
      setSelectedVersionId(null);
    }
    setLoadingDetail(false);
  };

  const handleAdoptScore = async (scoreId: number) => {
    if (!selectedTeam) return;
    if (!confirm('确定采用该评分版本？将覆盖现有采纳状态。')) return;
    try {
      await (scoreApi as any).adoptScore(selectedTeam.id, scoreId);
      // 重新加载详情
      const data = await scoreApi.getScoreResult(selectedTeam.id) as any;
      setTeamScoreDetail(data.score);
      setAllScoreVersions(data.all_scores || []);
      // 更新队伍列表的显示
      fetchTeams();
    } catch (err: any) {
      alert('采纳失败: ' + (err.message || '未知错误'));
    }
  };

  const handleReScoreTeam = async () => {
    if (!selectedTeam) return;
    if (!selectedTeam.work) {
      alert('该队伍没有作品，无法评分');
      return;
    }
    if (!confirm(`确定对【${selectedTeam.team_name}】重新进行AI评分？`)) return;
    setReScoringTeamId(selectedTeam.id);
    try {
      const result = await (scoreApi as any).autoScore(selectedTeam.id, selectedModel) as any;
      alert(`评分完成！总分：${result.scores.total}分`);
      // 重新加载详情
      const data = await scoreApi.getScoreResult(selectedTeam.id) as any;
      setTeamScoreDetail(data.score);
      setAllScoreVersions(data.all_scores || []);
      // 更新队伍列表
      fetchTeams();
      loadScoringStats();
    } catch (err: any) {
      alert('评分失败: ' + (err.message || err.response?.data?.detail || '未知错误'));
    } finally {
      setReScoringTeamId(null);
    }
  };

  const handleSwitchVersion = async (scoreId: number) => {
    setSelectedVersionId(scoreId);
    setLoadingDetail(true);
    try {
      const data = await scoreApi.getScoreById(scoreId);
      setTeamScoreDetail(data);
    } catch {
      // 切换失败，保持原详情
      const data = await scoreApi.getScoreResult(selectedTeam.id) as any;
      setTeamScoreDetail(data.score);
    }
    setLoadingDetail(false);
  };

  const handleSearch = () => {
    fetchTeams({
      search: search || undefined,
      group_type: groupFilter || undefined,
    });
  };

  const handleImportTeams = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    setImporting(true);
    try {
      const result = await importFromExcel(file);
      alert(`导入成功！新增 ${result.imported} 支，跳过 ${result.skipped} 支`);
      fetchTeams();
      fetchStats();
    } catch (err: any) {
      alert(err.message || '导入失败');
    }
    setImporting(false);
  };

  const handleAssignGroups = async () => {
    if (!confirm('将根据当前评审组配置分配队伍。确定继续？')) return;
    try {
      // 先保存当前配置
      await judgeGroupApi.saveGroups(groupConfigs);
      // 再分配队伍
      const result = await judgeGroupApi.assignTeams() as any;
      alert(`评审组分配完成！共分配 ${result.total_teams} 支队伍`);
      fetchTeams();
      fetchStats();
      loadGroupConfig();
    } catch (err: any) {
      alert(err.message || '分配失败');
    }
  };

  // 添加评审组
  const addGroup = () => {
    const newIndex = groupConfigs.length + 1;
    setGroupConfigs([...groupConfigs, {
      group_index: newIndex,
      group_name: `第${newIndex}评审组`,
      description: '',
      judges: []
    }]);
  };

  // 删除评审组
  const removeGroup = (index: number) => {
    const newConfigs = groupConfigs.filter((_, i) => i !== index);
    // 重新编号
    newConfigs.forEach((g, i) => { g.group_index = i + 1; });
    setGroupConfigs(newConfigs);
  };

  // 更新评审组名称
  const updateGroupName = (index: number, name: string) => {
    const newConfigs = [...groupConfigs];
    newConfigs[index] = { ...newConfigs[index], group_name: name };
    setGroupConfigs(newConfigs);
  };

  // 更新评委
  const updateJudges = (index: number, judgesStr: string) => {
    const newConfigs = [...groupConfigs];
    const judges = judgesStr.split(/[,，、]/).map(j => j.trim()).filter(j => j);
    newConfigs[index] = { ...newConfigs[index], judges };
    setGroupConfigs(newConfigs);
  };

  // 保存评审组配置
  const saveGroupConfig = async () => {
    setSavingGroups(true);
    try {
      await judgeGroupApi.saveGroups(groupConfigs);
      alert('评审组配置保存成功');
      loadGroupConfig();
    } catch (err: any) {
      alert(err.message || '保存失败');
    }
    setSavingGroups(false);
  };

  // GroupConfigPanel 薄包装回调（业务逻辑保留在父组件）
  const handleRandomAssign = async () => {
    if (!confirm('将随机分配所有队伍到评审组，确定继续？')) return;
    try {
      const result = await judgeGroupApi.assignTeams() as any;
      alert(`分配完成！共 ${result.total_teams} 支队伍`);
      loadGroupConfig();
      fetchTeams();
    } catch (err: any) {
      alert(err.message || '分配失败');
    }
  };

  const handleUploadAssignment = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const result = await judgeGroupApi.uploadAssignment(file) as any;
      alert(`分配完成！匹配 ${result.matched} 个，未匹配 ${result.unmatched} 个`);
      loadGroupConfig();
      fetchTeams();
    } catch (err: any) {
      alert(err.message || '上传失败');
    }
    e.target.value = '';
  };

  // 批量上传作品（循环调用单个上传接口）
  const handleBatchUploadWorks = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files || files.length === 0) return;

    const zipFiles = Array.from(files).filter(f => f.name.endsWith('.zip'));
    if (zipFiles.length === 0) {
      alert('请选择ZIP格式的作品文件');
      return;
    }
    if (zipFiles.length !== files.length) {
      alert(`已过滤非ZIP文件，共 ${zipFiles.length} 个ZIP文件待上传`);
    }

    setUploadingWorks(true);
    const result = { total: zipFiles.length, success: 0, failed: 0, details: [] as any[] };

    for (const file of zipFiles) {
      try {
        const r = await workApi.uploadWork(file) as any;
        result.success += 1;
        result.details.push({ filename: file.name, team_id: r.team_id, matched: r.team_id !== undefined, error: null });
      } catch (err: any) {
        result.failed += 1;
        result.details.push({ filename: file.name, team_id: null, matched: false, error: err.message || '上传失败' });
      }
    }

    setUploadResult(result);
    alert(`上传完成！成功 ${result.success} 个，失败 ${result.failed} 个`);
    loadWorksStatus();
    fetchTeams(); // 刷新队伍列表以显示作品状态
    setUploadingWorks(false);
    // 清空input以便可以再次选择同一批文件
    e.target.value = '';
  };

  // 批量上传文件夹
  const handleBatchUploadFolders = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files || files.length === 0) return;

    setUploadingWorks(true);
    try {
      const result = await workApi.batchUploadFolders(Array.from(files)) as any;
      alert(`文件夹上传完成！成功 ${result.success} 个文件夹，失败 ${result.failed} 个`);
      loadWorksStatus();
      fetchTeams();
    } catch (err: any) {
      alert(err.message || '上传失败');
    }
    setUploadingWorks(false);
    e.target.value = '';
  };

  return (
    <div className="space-y-4">
      <TeamFilterBar
        search={search}
        onSearchChange={setSearch}
        groupFilter={groupFilter}
        onGroupFilterChange={setGroupFilter}
        onSearch={handleSearch}
      />

      <TeamActionBar
        importing={importing}
        showGroupConfig={showGroupConfig}
        showWorksPanel={showWorksPanel}
        showScoringPanel={showScoringPanel}
        showPlagiarismPanel={showPlagiarismPanel}
        onToggleGroupConfig={() => setShowGroupConfig(!showGroupConfig)}
        onToggleWorksPanel={() => setShowWorksPanel(!showWorksPanel)}
        onToggleScoringPanel={() => {
          setShowScoringPanel(!showScoringPanel);
          if (!showScoringPanel) {
            loadScoringStats();
            loadBatchTasks();
          }
        }}
        onTogglePlagiarismPanel={() => setShowPlagiarismPanel(!showPlagiarismPanel)}
        onAssignGroups={handleAssignGroups}
        onRefresh={() => {
          fetchTeams();
          fetchStats();
          loadGroupConfig();
          loadWorksStatus();
        }}
        onImport={handleImportTeams}
      />

      {/* 评审组配置面板 */}
      {showGroupConfig && (
        <GroupConfigPanel
          groupConfigs={groupConfigs}
          savingGroups={savingGroups}
          onAddGroup={addGroup}
          onSaveGroupConfig={saveGroupConfig}
          onUpdateGroupName={updateGroupName}
          onUpdateJudges={updateJudges}
          onRemoveGroup={removeGroup}
          onRandomAssign={handleRandomAssign}
          onUploadAssignment={handleUploadAssignment}
        />
      )}

      {/* AI评分面板 */}
      {showScoringPanel && (
        <ScoringPanel
          scoringInProgress={scoringInProgress}
          scoringResult={scoringResult}
          scoringStats={scoringStats}
          selectedModel={selectedModel}
          availableModels={availableModels}
          parallelEnabled={parallelEnabled}
          concurrency={concurrency}
          dimensions={dimensions}
          batchTasks={batchTasks}
          onRefreshTasks={loadBatchTasks}
          onStartScoring={handleAutoScoreAll}
          onAdoptBestScore={handleAdoptBestScore}
          onModelChange={handleModelChange}
          onToggleParallel={handleToggleParallel}
          onConcurrencyChange={handleConcurrencyChange}
        />
      )}

      {/* 抄袭检测面板 */}
      {showPlagiarismPanel && (
        <PlagiarismPanel
          plagiarismResult={plagiarismResult}
          plagiarismInProgress={plagiarismInProgress}
          onDetect={handlePlagiarismDetect}
          onClearFlags={handleClearPlagiarismFlags}
        />
      )}

      {/* 作品管理面板 */}
      {showWorksPanel && (
        <WorksPanel
          worksStatus={worksStatus}
          uploadResult={uploadResult}
          uploadingWorks={uploadingWorks}
          namingExample={namingExample}
          onBatchUploadWorks={handleBatchUploadWorks}
          onBatchUploadFolders={handleBatchUploadFolders}
        />
      )}

      {/* 队伍列表 */}
      <TeamListTable
        teams={teams}
        groupConfigs={groupConfigs}
        onViewTeamDetail={handleViewTeamDetail}
      />

      {/* 队伍详情弹窗 */}
      {selectedTeam && (
        <TeamDetailModal
          team={selectedTeam}
          reScoringTeamId={reScoringTeamId}
          allScoreVersions={allScoreVersions}
          selectedVersionId={selectedVersionId}
          loadingDetail={loadingDetail}
          teamScoreDetail={teamScoreDetail}
          onClose={() => setSelectedTeam(null)}
          onReScore={handleReScoreTeam}
          onSwitchVersion={handleSwitchVersion}
          onAdoptScore={handleAdoptScore}
        />
      )}
    </div>
  );
}
