# 项目开发规则

本文件指导本仓库的代码与文档开发。媒体编辑任务使用根目录 `SKILL.md`；不要将编辑流程直接套用于仓库开发。

## 开始工作

- 先检查工作区状态，保留已有改动；以当前代码核实文档中的接口和行为。
- 开发环境、源码入口和测试命令见 [开发与测试](docs/development.md)。
- 根据任务读取 [专项开发说明索引](docs/development/README.md)，只加载相关子文档；子文档是对应任务的开发规则。
- 本分支的动态工作流控制面按 [动态工作流开发规范](docs/development/change-dynamic-workflow.md)；
  新增 harness、专业 Agent、LLM 分别读取索引中的对应接入说明。
- 实施统一 Run/Control Job、Provider 拆分、用户反馈或运行中输入重构前，必读[已确认方案与实施边界](docs/dynamic-plan-todo.md)。质量反馈统一走 run_inputs 和全 Run 排空安全点，Provider 不保留旧字段兼容；逐项核对文末五个边界及验证要求。
- 新增 operation、为现有 operation 增加实现，或修改其契约/生命周期时，必须先读 [新增与扩展 operation](docs/development/add-operation.md)。
- 修改或执行真机 case、归因、复测及交接审查时，读取[真机测试与审查规范](docs/development/run-live-campaign.md)。模型选择和计量修改同时读取 LLM、动态工作流及可观测性专项；模型版本、执行 harness 和 strict 传输模式必须分别核实。

## 通用约定

- 复用现有契约、调度、隔离、取消、恢复和成果发布机制；业务特例留在对应能力实现中。
- 输入归属、方案确认、版本绑定和租约校验不能因功能扩展被绕过。状态与交付相关改动参考 [程序正确性审查原则](docs/program-correctness-audit.md)。
- 测试使用独立数据与隔离入口；先验证直接影响的行为，发布检查按现有部署流程执行。文档改动检查链接、示例和代码一致性即可。
- 交付时说明改动、验证结果及未验证边界；历史验收不代表当前代码通过，工作区修改不代表已部署。
- 本机独立部署通过 `deploy/dynamic_cluster.py` / `deploy.sh` 管理。不得以旧 `deploy/cluster.py`
  或旧真机驱动操作 main；服务名、端口、数据目录、UID/cgroup、数据库、Redis 前缀和 BOSS 前缀必须独立。
- 动态 Task 的正式状态只由 Orchestrator 提交；执行器仅保存候选和回执。不得绕过 sealed、
  授权包络、候选验证或旧 generation 防护来让测试通过。
- 修复使用逻辑 Task + 不可变 revision；只允许原授权、目标、输入输出和验收内的实现修订。修复根计数，受影响后代换 revision，无关分支复用；旧 revision、结果和 plan 选择记录不得覆盖。
- 专业 Agent 的检查、格式纠正和最终提交预算分开；进入 finalize 后不能继续扩张检查范围。能力目录使用冻结摘要和按需契约查询，查询不授予权限。
- Manager 规划必须通过 snapshot 绑定的服务端 PlanDraft 和类型化命令；只有原子 `commit_plan` 可进入权威 Proposal 校验。Planner 不得提交原始 shell、SQL、URL、代码或 Script Task；动态本地实现委派获授权 Implementer，确定性处理使用注册 operation。
- Manager 不填写 continuation、replacement ID、依赖派生、revision、技术验收或 delivery_stage；宿主按 `APPLY_PLAN`/`COMPLETE`、输入绑定和 `stage=final` 业务验收编译这些字段。自动修复只修改冻结范围，Reviewer/Diagnostician 报告不能成为交付输入。
- 模型输入/输出预算分账。非空输出的 `output_tokens=0` 是冲突回执：保留原始回执，可信 input 可单独结算，输出只保留阶段上限并明确标记 unknown；不得记作零成本或永久占用整笔输入估算。
- 真机用例变更先更新审查矩阵，再修改驱动。逐例保留浏览器和内部状态证据、首次失败及复测版本；
  模型/依赖异常与程序缺陷分开报告，未执行、未触发边界和证据缺失不能计为通过。

## 统一 Run 开发边界

- 不维护旧 Provider 字段兼容读取，不做新旧执行身份双写；专用数据库按部署流程备份后重置。
- 输入、反馈、重新授权均通过 run_inputs、安全点与版本化 grant；禁止新增绕过屏障的修订 API。
- Control Job 不进入业务 DAG；日志和指标区分控制调用、业务重试与正常 poll/collect。
- 真机用例共 119 条，以冻结 manifest/驱动/部署摘要共同绑定结果；旧轮结果不能替代当前验证。

## 维护开发说明

新增主题放入 `docs/development/`，并在其索引登记适用任务。本文仅保留通用约定和必要的读取入口，不堆放实现教程。

专项说明应包含适用范围、实现入口、开发步骤和完成标准；已有接口模板或协议文档通过链接复用，行为变化时同步相关文档。
