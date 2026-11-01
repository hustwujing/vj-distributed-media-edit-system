# 工作流契约、状态与兼容说明

本文描述当前代码协议。服务可用性中的 configured_unverified 只表示已配置，真正提交前仍验证上游能力；不代表已通过真实服务测试。

## 分层与旧字段映射

| 对象 | 权威字段 | 值/职责 | 兼容处理 |
| --- | --- | --- | --- |
| 调度尝试 | execution_leases.kind | agent/manual/workflow | 保持原值；每次按最新状态分派 |
| 对话轮 | agent_turns.mode | plan/execute | 保持原值；不直接等同权限 |
| 运行环境 | permission_profile | planning/execution/review/step/fixed_tool | 记录在执行尝试；由服务端挂载策略派生 |
| 操作 | operation | image.* / media.* / audio.transcribe / video.* | 模型/环境作为登记实现，不能由用户传路径 |
| 工具面板 | jobs.kind/tool | 原八类业务和功能按钮 | 原 worker 保留；图片和媒体复用同一固定实现 |
| 步骤 | kind | script/operation | 仅两种取值；存储记录一次性迁移，不保留旧格式解析 |
| 验收 | target/reviewer/severity | 对象、program/agent/user、required/signal | 兼容旧 kind；内容验收只允许 required |
| Agent 目的 | purpose | review/diagnose/final | 不增加调度类型或对话模式 |
| Agent 结论 | action/finding | pass/repair/needs_user/stop；conformant/nonconformant/inconclusive | pass 不能是 inconclusive；stop 为失败终态 |
| 用户决定 | kind | quality/clarification/selection/external_call/plan_change | 绑定目标修订、选项及提案 |
| 控制 | action | status/cancel/resume/pause | pause 只在安全边界阻止后续，不取消上游 |
| 产物 | output_roles/quality_state/workflow_delivery/is_primary | 业务角色、验收状态、交付、主展示分开 | 旧 role/purpose 和展示标记保留作为兼容信息 |

image 在素材、图层、步骤和验收中的历史同名值不做破坏性全局改名。业务操作与 UI 分组、日志 category 独立；API/CLI 能力说明由 operations.catalog 同源提供。

## 九套生命周期

| 对象 | 主要状态转换 | 更新者/约束 |
| --- | --- | --- |
| 对话轮 | queued → running → waiting/queued/paused/succeeded/failed；active → cancelling → cancelled | AgentHost/工作流控制；项目唯一活动轮 |
| manual job | queued → running → succeeded/failed；异步查询期间保持 queued 并有独立阶段 | manual_executor/manual_async；等待 next_poll 不创建运行环境 |
| 工作流 | staged → ready → reviewing/resuming → ready/paused/succeeded/failed；active → cancelling → cancelled | Runner/验收提交；脚本提交检查点后才 ready |
| 步骤 | pending → running 或 submitting → waiting → collecting → verifying → succeeded/agent_review/awaiting_review；失败可 diagnosing/paused | Runner/版本受控修复；依赖必须 succeeded |
| Agent 请求 | pending → running → staged → applied；中断回 pending；取消/替换/耗尽关闭 | workflow_review；请求 ID、修订、文件哈希检查 |
| 执行尝试 | running → finished/interrupted/expired/resource_wait | 租约协调器；exit_reason 与结果分开 |
| 外部调用 | submitting → waiting/queued/running → succeeded/failed/cancelled/rejected | provider_calls/受限代理；提交未知不重提 |
| 本地清理 | allocated → pending/cleaning → complete；失败 quarantined | runtime/reaper；清理未完成仍计入节点容量 |
| 外部清理 | pending → running → complete/unknown/unconfirmed/unsupported | 独立清理器；没有确认不能显示上游停止 |

跨对象约束：工作流 reviewing/resuming 时，对话 queued/running，Runner 不执行业务步骤；ready 时对话 waiting。同一任务只有一个有效租约。恢复后重新检查类型，类型改变则释放旧尝试重新排队。终止/取消不允许迟到验收放行；预算不因恢复重置。

等待原因与状态分开：dependency/resource/provider/retry/user/model_unconfigured/model_unavailable/node_limit/user_limit/project_busy/cleanup/paused。队列停留、调度占用、运行预算、外部 HTTP 请求时间分别记录；运行预算是隔离运行环境持有期间的墙钟预算，不是 CPU 使用秒数。外部 HTTP 查询短暂占调度槽，查询间隔不占槽。同步 API 请求仍占请求执行时间，不伪称可恢复异步任务。

## 输入、输出和版本

- 输入为 `{asset: ID}` 或 `{step: ID, role: ROLE}`，后者必须声明 depends_on。
- 服务端确认项目归属、已验收状态、修订和 sha256。输入通过 VIDEO_USE_STEP_INPUTS 传给脚本，操作参数不能改写素材路径/模型路径/服务 URL。
- 本地输出角色在 output_roles 中映射到声明的 outputs；登记能力有稳定角色：primary_image、foreground、mask、video、transcript、report、preview 等。
- 多个主候选必须用户明确选择，选择映射到原候选文件后重新验收，不重新生成。
- output_roles 属于业务语义；quality_state 决定是否已验收；workflow_delivery 决定是否交付；is_primary 只决定展示顺序。未验收和已被替代的文件不能成为主交付。
- 脚本与配置通过 preflight 返回同环境语法/依赖检查结果，并绑定代码与环境清单哈希。检查报告明确不证明业务逻辑正确。配置作为 config_files 声明，局部 Python 模块也递归纳入检查。

## 决策与恢复边界

程序验收先于内容判断，Agent 不得放行技术失败。视频证据必须填写 coverage 描述抽帧或片段范围；inconclusive 不能 pass。自动修复只能修改本地命令或安全的受控本地操作参数，验收契约、依赖和权限不变。需要改变计划时创建 plan_change 用户请求，确认后停止旧任务进入重新规划。

quality 仅批准效果；external_call 必须有明确 replacement 提案及版本，由用户单独确认，不能用 resume 授权。clarification/selection 回答进入诊断；产物选择则直接保存角色绑定后复验。重复、过期、跨项目请求拒绝。

## 迁移与运行前检查

数据库新增表/列均在既有事务内增量创建。存储中的旧 local/image 步骤一次性迁移为 script/operation，之后入口与运行时均只接受新格式，已完成文件不重放。验收 kind 的兼容属于另一维度，仍保留。旧确认方案缺执行意图时，直接模型工具入口拒绝并要求工作流或重新规划；这是权限收紧，不自动推断短任务授权。

部署需要重建 runtime manifest 与 tool-app、补服务账号及 runtime 组读取 ACL、挂载 codex-code-mode-host、启动独立 reaper。模型服务的可信内部主机由管理员配置，不能通过模型输入自选。发布前保存 SQLite 一致备份及代码快照；不得以未配置或仅模拟通过的上游能力宣布端到端完成。
