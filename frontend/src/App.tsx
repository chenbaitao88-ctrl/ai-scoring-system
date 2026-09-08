/**
 * 应用入口组件 - 通用版
 */
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import Layout from './components/Layout';
import Dashboard from './pages/Dashboard';
import TeamManage from './pages/TeamManage';
import Scoring from './pages/Scoring';
import Results from './pages/Results';
import TaskBookManage from './pages/TaskBookManage';
import ScoringFormManage from './pages/ScoringFormManage';
import TeamReview from './pages/TeamReview';
import BatchScoring from './pages/BatchScoring';
import EvidencePackageImport from './pages/EvidencePackageImport';
import PipelineTaskMonitor from './pages/PipelineTaskMonitor';
import ScoringPipeline from './pages/ScoringPipeline';

function App() {
  return (
    <BrowserRouter>
      <Layout>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/teams" element={<TeamManage />} />
          <Route path="/scoring" element={<Scoring />} />
          <Route path="/batch-scoring" element={<BatchScoring />} />
          <Route path="/results" element={<Results />} />
          <Route path="/task-books" element={<TaskBookManage />} />
          <Route path="/scoring-forms" element={<ScoringFormManage />} />
          <Route path="/team-review" element={<TeamReview />} />
          <Route path="/evidence-packages" element={<EvidencePackageImport />} />
          <Route path="/pipeline-tasks" element={<PipelineTaskMonitor />} />
          <Route path="/scoring-pipeline" element={<ScoringPipeline />} />
        </Routes>
      </Layout>
    </BrowserRouter>
  );
}

export default App;
