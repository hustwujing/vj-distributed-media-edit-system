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

跨对象约束：工作流 reviewing/resuming 时，对话 queued/running，Runner 不领取新的业务步骤；ready 时对话 waiting。同一租约 slot 只有一个有效执行者，独立步骤可各持有效租约；根 Agent/验收阶段与步骤业务执行互斥，取消控制可通过专门路径撤销业务执行。恢复后重新检查调度类型，类型改变则释放旧尝试重新排队；输入修订或哈希不符则拒绝继续，不能自动改绑。终止/取消不允许迟到验收放行；预算不因恢复重置。

等待原因与状态分开：dependency/resource/provider/retry/user/model_unconfigured/model_unavailable/node_limit/user_limit/project_busy/cleanup/paused。队列停留、调度占用、运行预算、外部 HTTP 请求时间分别记录；运行预算是隔离运行环境持有期间的墙钟预算，不是 CPU 使用秒数。外部 HTTP 查询短暂占调度槽，查询间隔不占槽。同步 API 请求仍占请求执行时间，不伪称可恢复异步任务。

## 输入、输出和版本

- 输入为 `{asset: ID}` 或 `{step: ID, role: ROLE}`，后者必须声明 depends_on。
- 服务端确认项目归属、已验收状态、修订和 sha256。输入通过 VIDEO_USE_STEP_INPUTS 传给脚本，操作参数不能改写素材路径/模型路径/服务 URL。
- 本地输出角色在 output_roles 中映射到声明的 outputs；登记能力有稳定角色：primary_image、foreground、mask、video、transcript、report、preview 等。
- 多个主候选必须用户明确选择，选择映射到原候选文件后重新验收，不重新生成。
- output_roles 属于业务语义；quality_state 决定是否已验收；workflow_delivery 决定是否交付；is_primary 只决定展示顺序。未验收和已被替代的文件不能成为主交付。
- 脚本与配置通过 preflight 返回同环境语法/依赖检查结果，并绑定代码与环境清单哈希。检查报告明确不证明业务逻辑正确。配置作为 config_files 声明，局部 Python 模块也递归纳入检查。

## 决策与恢复边界

operation 实现绑定的生命周期通过 `operation_lifecycle.PROTOCOLS` 显式选择阶段，未知协议或状态与句柄不匹配时暂停为 `lifecycle` 错误，不默认执行异步提交。`Implementation.recovery()` 返回的 `RecoveryAction` 由 `Implementation.recover()` 实际用于恢复决策，Runner 负责按现有租约与事务约束提交；人工继续通过同一实现的 `resume_state()` 校验。能力目录中的 `recovery_by_phase` 仅列出该实现适用的阶段及 `validate`，不再为每个实现列出全部生命周期阶段。

同步实现默认不能凭“没有调用记录”自动重试。声明 `records_calls=True` 的实现须在外部副作用前记录意图，并在结果可持久恢复后保存产物列表；恢复时可复用已有结果进入验收，已成功但产物缺失则暂停核实。内置同步生图和转写保留这一路径，扩展实现按契约显式接入。当前改动沿用原步骤状态与持久化字段，无需新增数据迁移；新生命周期的完整开发要求见[生命周期开发说明](development/change-operation-lifecycle.md)。

程序验收先于内容判断，Agent 不得放行技术失败。视频证据必须填写 coverage 描述抽帧或片段范围；inconclusive 不能 pass。自动修复只能修改本地命令或安全的受控本地操作参数，验收契约、依赖和权限不变。需要改变计划时创建 plan_change 用户请求，确认后停止旧任务进入重新规划。

quality 仅批准效果；external_call 必须有明确 replacement 提案及版本，由用户单独确认，不能用 resume 授权。clarification/selection 回答进入诊断；产物选择则直接保存角色绑定后复验。重复、过期、跨项目请求拒绝。

## 迁移与运行前检查

数据库新增表/列均在既有事务内增量创建。存储中的旧 local/image 步骤一次性迁移为 script/operation，之后入口与运行时均只接受新格式，已完成文件不重放。验收 kind 的兼容属于另一维度，仍保留。旧确认方案缺执行意图时，直接模型工具入口拒绝并要求工作流或重新规划；这是权限收紧，不自动推断短任务授权。

运行环境改动需核对 runtime manifest、tool-app、服务账号/ACL、固定入口及独立 reaper。可信服务主机由管理员配置，不能通过模型输入自选。当前集群使用 PostgreSQL 与共享对象存储，备份、冻结源码和发布步骤按[集群操作手册](cluster-operations.md)执行；SQLite 兼容数据的备份不能替代 PostgreSQL 备份。不得以未配置或仅模拟通过的上游能力宣布端到端完成。

## 跨方案严格复用

`Step.reuse_from` 为可选 `{workflow, step, revision}`，workflow 为 32 位十六进制 ID，
revision 为非负整数。新版必须保留完整业务操作声明；复用不扩大已确认执行意图。
提交时检查来源项目、成功修订、不可变记录与静态规格；执行时核对实际声明输入、运行环境与文件字节。
支持范围由受信任 `Operation.reuse_safe` 显式登记，默认为 false；替代 implementation 默认不继承此能力。
任意脚本、外部服务及模型依赖不支持严格复用。

新增 `workflow_reuse_receipts` 表，以 workflow/step/revision 为唯一键，保存接受后的执行规格、
输入签名、原计划/轮次/attempt、固定输出清单及摘要。共享对象通过 `reuse-receipts/` 逻辑引用保留，
不依赖源节点缓存。记录写入与步骤成功在同一租约保护事务中提交；旧记录不迁移或推测补填。

复用遵循 `pending → running → verifying → 原验收流程`，输出写入新版步骤目录并独立检查点。
复用阶段失败以 `error_code=reuse` 暂停且不可直接恢复为原操作；需要重新规划。
复制阶段中断可在 3 次尝试内重新读取固定来源，验收中断只恢复验收。
新旧方案的验收标准可以不同，因此每次复用均重新执行本版验收，不能继承源审批。
`bindings._reuse` 保存实际来源和 execution_digest；公开步骤暴露 `reuse_from`、`reuse`，
续接上下文给出可引用来源和原步骤 spec。`_reuse_contract` 在处理前绑定宿主契约代码指纹，
`_environment` 沿用运行时绑定；跨不兼容发布不自动复用。

测试见 [跨方案复用用例](../tests/test_workflow_reuse.py)，包括真实 PostgreSQL、S3 协议服务、
隔离操作、清除源节点缓存、跨节点读取以及验收中断恢复。
