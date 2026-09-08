# 系统架构

AI Scoring System 是一个本机 Web 应用。浏览器负责展示材料和操作，FastAPI 处理请求，业务服务管理评分与复核，数据留在本地。

![AI Scoring System 系统架构](assets/system-architecture.png)

## 一个后端进程托管页面与接口

前端由 React、TypeScript、TDesign 和 Zustand 组成。[App.tsx](../frontend/src/App.tsx)组织页面路由，[api.ts](../frontend/src/services/api.ts)集中调用接口。

构建后的静态文件由[backend/main.py](../backend/main.py)托管，和 API 使用同一个端口。前端开发时可以运行 Vite；日常本机启动不需要额外维持一个前端进程。

## 把评分拆成几个可以单独检查的环节

| 环节 | 主要实现 | 负责的事情 |
|---|---|---|
| 材料准备 | [evidence_registration_service.py](../backend/services/evidence_registration_service.py)、[evidence_package_validator.py](../backend/services/evidence_package_validator.py) | 登记材料、校验输入与证据状态 |
| 评分任务 | [scoring_task_creator.py](../backend/services/scoring_task_creator.py)、[pipeline_task_manager.py](../backend/services/pipeline_task_manager.py) | 核对上游条件、绑定标准版本、创建任务和推进阶段 |
| 评分执行 | [scoring_pipeline_orchestrator.py](../backend/services/scoring_pipeline_orchestrator.py)、[scoring_provider_gateway.py](../backend/services/scoring_provider_gateway.py) | 编排请求、校验调用条件和输出；在线执行需要注入运行时依赖 |
| 评分记录 | [score_attempt_store.py](../backend/services/score_attempt_store.py) | 保存评分尝试、结果快照和事件 |
| 人工复核 | [review_case_store.py](../backend/services/review_case_store.py)、[manual_adjustment_service.py](../backend/services/manual_adjustment_service.py) | 工单、决定、采用、人工调整与最终锁定 |
| 结果输出 | [result_derivation_service.py](../backend/services/result_derivation_service.py)、[result_export.py](../backend/routers/result_export.py) | 检查权威来源和阻断条件，推导可追溯结果 |

[逐环节的业务流程](WORKFLOW.md)解释为什么这样拆分。

## 为什么同时使用 SQLite 和文件记录

[database.py](../backend/database.py)用 SQLAlchemy/SQLite 保存队伍、作品和基础评分。这部分承接应用已有的列表、详情和历史综合分参考。正式文件导出不再读取这些综合分。

新的任务流水线使用独立文件记录任务、评分尝试、结果快照、复核决定和最终锁定。每类事实有明确标识和关联，重试不覆盖成功尝试，人工采用可以指向具体快照。

正式Excel、Word、CSV由[统一导出服务](../backend/services/authoritative_export_service.py)按评分任务读取尝试、采用和锁定记录。整任务导出不跳过失败条目；界面允许明确选择已确认的子集，并在生成前后检查结果版本。旧下载URL必须携带task_id，否则返回409。旧数据库与材料编号没有可靠映射，本次没有猜测队伍关系或重新加权旧分数。

## AI 建议怎样进入最终结果

我把“模型给过什么”“人工采用什么”“最终确认什么”分开存储。否则，一个新分数可能覆盖已确认的结果，事后很难解释。

1. 评分任务关联确定版本的输入、rubric、Prompt 和响应格式。
2. 一次评分形成尝试与快照；重试保留新的记录。
3. 复核决定表达人工处理意见，采用记录选择具体结果。
4. 人工最终锁定明确确认版本。
5. 结果服务优先读取有效锁定，没有锁定再读取有效采用，同时检查未结案阻断工单与快照绑定。

即使已经锁定，未结案的阻断工单仍然会拒绝输出。[结果推导测试](../tests/test_result_derivation.py)和[同案操作记录](REVIEW_EXAMPLE.md)可以对照查看。

## 两条模型调用路径

基础评分通过 [llm_service.py](../backend/services/llm_service.py)调用模型服务，评分与规则处理见 [scorer.py](../backend/services/scorer.py)。

新版任务流水线通过注入式 Provider Gateway 拆分能力匹配、端点与凭据解析、传输和结果校验。默认 API 容器只组装不调用 Provider 的服务；执行运行时未配置时会明确拒绝执行。完整服务实现与测试都在仓库中，但默认启动不是一个填入 Key 就完成全部新版流水线配置的入口。

[配置说明](CONFIGURATION.md)列出这两条路径与当前限制。

## 当前适用范围

单用户、本机、单后端实例是当前运行范围。多用户权限、集中部署、新版在线运行时和真实模型效果评估，还需要继续完成。

[返回首页](../README.md)
