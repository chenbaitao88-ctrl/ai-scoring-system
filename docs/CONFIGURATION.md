# 模型与运行配置

先用默认合成案例运行应用，可以查看材料、评分、复核和结果页面。合成案例不验证模型效果。

## 本地工作台

`start_demo.command` 和 `start_demo.ps1` 会隔离演示数据目录，清空模型调用相关配置，并关闭在线批量评分。代码和页面完整启动，示例评分由初始化脚本提供。

数据目录、端口和功能开关在 [config.py](../backend/config.py)、[feature_flags.py](../backend/feature_flags.py) 和启动脚本中定义；字段示例见 [.env.example](../.env.example)。不要把真实 `.env` 或数据目录提交到仓库。

后端默认监听127.0.0.1；确有其他部署需求时，可以显式设置SCORING_HOST。当前版本未验收多用户或公网部署。

## 基础评分服务

[llm_service.py](../backend/services/llm_service.py)使用模型 API 地址、模型名称和凭据进行调用，评分组织见 [scorer.py](../backend/services/scorer.py)。如需启用，使用独立配置与数据目录，核对输入材料允许提交到所选服务，并先完成小样本人工核验。

默认演示启动脚本会覆盖模型配置，因此不应通过修改演示脚本把真实调用混进默认案例。

## 新版任务流水线

新版评分把配置与执行依赖拆开：

- [scoring_pipeline_configuration.py](../backend/services/scoring_pipeline_configuration.py)读取评分输入配置与模型绑定。
- [scoring_provider_registry.py](../backend/services/scoring_provider_registry.py)维护 Provider 与模型能力。
- [scoring_provider_gateway.py](../backend/services/scoring_provider_gateway.py)依赖端点解析、凭据解析、传输、响应格式和评分规则校验。
- [scoring_pipeline_api_container.py](../backend/services/scoring_pipeline_api_container.py)负责 API 服务装配；未提供执行运行时则拒绝执行。

仓库里的两个 `config/*.example.json` 是关闭在线执行的占位示例，不能直接当作完整实时评分配置。默认容器只组装不调用 Provider 的服务，尚未提供通用的在线执行装配入口。完整实现与集成测试可以阅读，在线运行时接入和真实 Provider 联调仍在待办中。

更换 Provider 时需要保持 rubric、Prompt、输入与响应格式一致；模型效果要另外验证。不能用模拟传输通过的测试代替真实服务结果。

[系统架构](ARCHITECTURE.md) · [运行手册](RUNBOOK.md)
