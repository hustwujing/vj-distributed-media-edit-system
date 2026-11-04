# 文档索引

从[项目 README](../README.md)了解整体能力。以下区分日常指南、协议设计和历史验收；报告中的版本、数量与部署结论只适用于记录时的环境，当前服务状态请使用运维命令核实。部分机器记录被 Git 忽略，仅在原验证环境可用。

## 使用与安装

| 文档 | 内容 |
| --- | --- |
| [Web 使用指南](../WEB_UI.md) | 项目、对话确认、工具、图片画布、任务控制与用量 |
| [本地 Agent 技能安装](../install.md) | 安装依赖、注册技能、可选转写配置 |
| [页面与交互设计](../UI_DESIGN.md) | 预览、参数表单、流式正文、图层与版本交互 |
| [编辑技能](../SKILL.md) | 本地 Agent 的编辑流程与约束 |
| [沙盒公开技能](../runtime/public/SKILL.md) | Web 隔离执行环境的能力入口 |

现有机器的路径、私有地址和维护记录可放在 `INSTALL.local.md`；该文件被 Git 忽略，不随源码发布。

## 部署与排障

本分支先按[README 的独立部署入口](../README.md)和 `deploy/dynamic_cluster.py` 操作；下表旧集群操作手册描述 main，只用于理解复用机制，不用于启停本实例。

| 文档 | 内容 |
| --- | --- |
| [分布式执行配置](distributed-execution-configuration.md) | 共享数据库、加密对象存储、会话密钥和节点参数 |
| [集群操作手册](cluster-operations.md) | 单机多实例拓扑、发布、启停、备份与版本检查 |
| [执行隔离与恢复](execution-isolation-and-recovery.md) | 运行时、任务身份、资源限额、回收器和恢复边界 |
| [可观测性操作手册](observability-operations.md) | 任务诊断、日志、指标、告警及留存 |
| [自然语言任务控制](natural-language-controls.md) | 查询、暂停、继续和取消的分类及权限边界 |

## 开发与协议

| 文档 | 内容 |
| --- | --- |
| [仓库开发规则](../AGENTS.md)、[专项开发说明索引](development/README.md) | 精简规则入口与按任务读取的子文档 |
| [开发与测试](development.md) | 依赖、源码入口、隔离回归、真实服务验收和发布门禁 |
| [动态工作流开发规范](development/change-dynamic-workflow.md) | 统一 Run、Control Job、输入屏障、修订、授权和模型配置冻结 |
| [新增 Profile Harness](development/add-profile-harness.md)、[新增专业 Agent](development/add-specialist-agent.md) | 执行引擎、固定角色策略、新命令与结果契约 |
| [新增 LLM Provider](development/add-llm-provider.md) | 模型协议、strict 回退、输入预算与原始用量回执 |
| [真机测试与审查](development/run-live-campaign.md) | 配置核验、逐例证据、失败分类、复测和进度报告 |
| [API 并发与耗时隔离](development/change-api-concurrency.md) | 路由选池、异步 SSE、准入与取消、锁外准备和拥塞回归 |
| [新增与扩展 operation](development/add-operation.md) | 从能力选型、实现装载到验收交付的开发步骤 |
| [operation 生命周期开发说明](development/change-operation-lifecycle.md) | 新流程的状态转换、Runner 接线、恢复取消、资源与兼容验证 |
| [工作流平台设计](workflow-platform-design.md)、[工作流契约](workflow-contracts.md) | 步骤、依赖、状态、质量验收与恢复 |
| [能力接入指南](operation-extension-guide.md)、[接入流程](operation-onboarding.md) | 新 operation 的参数、适配器、生命周期和验证 |
| [统一生成接口](uniform-generation-design.md) | 图片/视频生成接口、输入输出与调用边界 |
| [分布式执行实现](distributed-execution-implementation.md) | 租约、调度、对象发布和故障接管 |
| [对话执行设计](conversation-execution-design.md)、[通用分析兜底](conversation-fallback.md) | 轻量入口、沙盒交接、方案检查和确认 |
| [用量统计设计](task-usage-design.md) | 调用账本、关联范围与统计口径；当前页面说明见 Web 使用指南 |
| [可观测性设计](observability-design.md) | 关联标识、事件、阶段指标及诊断 |
| [API 隔离设计与验收](api-isolation-design.md) | 有界依赖执行器、异步 SSE、发布竞争、双 API 压力与检查项 |
| [程序正确性审查原则](program-correctness-audit.md) | 状态、权限、错误与交付的通用审查要求 |

## 编辑参考

- 音视频：[本地编辑](../references/local-editing.md)、[剪辑计划](../references/edit-plan.md)、[视频交付](../references/video-delivery.md)。
- 图片：[图片编辑](../references/image-editing.md)、[图片工作台](../references/image-workbench.md)、[画布对话编辑](../references/canvas-editing.md)、[样式预览](../references/style-previews.md)。
- 工作流：[持久化步骤](../references/durable-workflows.md)、[工作流验收](../references/workflow-review.md)。
- 动画：[Manim 技能说明](../skills/manim-video/README.md)。

## 实现跟踪与历史记录

仓库保留以下设计和审查入口；状态以各文档中的范围和日期为准：

| 文档 | 范围 |
| --- | --- |
| [已确认方案与实施边界](dynamic-plan-todo.md)、[统一 Run 实施记录](unified-run-progress.md) | 已确认设计、源码实现与待部署/真机验收边界 |
| [当前真机审查矩阵](real-case-campaign/dynamic-case-review.md)、[真机清单](real-case-campaign/dynamic-cases.json) | 当前 119 条及后续模型配置，不能以历史通过替代当前轮次 |
| [模型计数缺口与改造记录](model-token-counting-gap.md) | 网关探测、本地预算估算、回执异常与验证限制 |
| [v3 模型设置部署验证](model-v3-deployment-validation.md) | 配置错配处置、冻结门禁、浏览器验证和旧轮证据边界 |
| [Agent 执行体验清单](agent-execution-experience-todo.md) | 正文与活动、规划预算、用量说明和方案检查 |
| [Harness 与模型适配、Agent 切换 TODO](harness-adapters-todo.md) | 待实现：公开接口适配、统一上下文、不透明原生快照、跨 Agent 接管及验收 |
| [文字流与示例库](stream-activity-and-demos.md) | 实时更新、共享示例目录及跨节点导入 |
| [早期分布式运行与验收配置](distributed-deployment.md) | 重构阶段配置及真实存储验收背景 |
| [历史 60 条场景设计](real-case-campaign/design.md) | 原始编辑与生成目标背景；当前清单以动态审查矩阵为准 |
| [工作流 Agent 审查待办](workflow-agent-review-backlog.md) | 后续问题与修复跟踪 |

机器专属报告与真实用户证据不随源码发布。若在原验证环境维护，可按以下文件名查找：

| 本地记录（位于 `docs/`） | 范围 |
| --- | --- |
| `seed-confirmation-release-report.md` | 初始工作区/检查点内容指纹、项目统计范围和 Token 合计来源 |
| `agent-execution-experience-report.md`、`stream-activity-release-report.md` | 执行体验、流式正文及共享示例库发布验收 |
| `conversation-execution-report.md` | 轻重路径、确认和会话恢复 |
| `task-usage-report.md`、`task-usage-real-cases.md` | 用量一致性及非零调用场景 |
| `cluster-deployment-report.md`、`distributed-final-verification.md` | 多实例切换、节点故障和取消验收 |
| `observability-implementation-report.md` | 日志、故障诊断、告警与部署验证 |
| `real-case-campaign/completion-report.md`、`real-case-campaign/final-report.md` | 真实场景完成情况 |

`project-*-audit.md`、`project-*-investigation.md` 及报告中的 `test-evidence/`、`.cluster/`、`workspace/` 证据同样属于本地记录。历史报告中的“未部署”“本次通过”和测试数量只描述当次版本；文档索引更新不代表重新运行了这些验收。
