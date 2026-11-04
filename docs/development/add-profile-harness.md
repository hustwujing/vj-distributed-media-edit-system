# 新增或修改 harness

适用于专业 Agent 的执行引擎，以及现有 SDK Agent 引擎的扩展。首先区分两个入口：

- `profile_harnesses.py` 承载动态 Task 的策略执行引擎。当前默认是服务端受控的
  `structured-profile-v3`，通过原生inspect/contract/script/submit_plan/submit_result工具执行受控循环。
- `harness_adapters.py` 承载 Codex、Claude Code、DeepSeek 等 SDK/子进程协议。
  它们已有启动、会话、事件和恢复机制；需要用 SDK 实现一个专业 Agent harness 时复用这些实现，
  不得把 SDK 可用性直接当作已满足专业 Agent 权限契约。

动态 Task 的实际 harness 来自冻结的 SpecialistProfile；项目模型配置提供模型、协议、凭据引用、
推理参数和预算。不能因为项目模型配置中保留了 SDK harness 字段，就把结构化 HTTP 循环的调用
报告成某个 SDK 已执行。

## 实现入口

`profile_harnesses.ProfileHarness`、`specialist_profiles.SpecialistProfile`、`workflow_agents.py`、
`workflow_gateway.py`、`agent_gateway.py`、`gateway_process.py`、`harness_adapters.py`、
`harness_events.py`、`agent_sandbox.py`、`task_leases.py`。

## 开发步骤

1. 登记稳定的版本化 ID，实现 `run(host, lease, workflow, task)` 和 `fingerprint()`；未知 ID 拒绝执行。
2. 指纹覆盖实际执行代码和协议依赖。Profile 快照保存该指纹；代码或权限变化时，旧任务不得静默换引擎。
3. 定义如何使用冻结输入、固定提示模板、实际加载的注册技能、工具白名单、输出 Schema、Token/时间上限。检查、格式纠正和 finalize 预算分别声明；必须为最终结构化结果保留调用，不能让工具检查耗尽结论机会。技能文件内容摘要必须绑定策略，执行代码指纹覆盖技能装载逻辑。
4. 复用已有 TaskRuntime、最小挂载、独立 UID、资源上限、取消监控和 reaper；不另建特权执行通道。
5. 模型请求经过已登记 ModelAdapter、受控 egress、调用意图和实际用量账本；不能在 SDK 中私自切供应商或重放未知提交。
6. 若通过 Unix gateway 调用，使用 `/workflow/proposal`、`/workflow/agent-result`、`/workflow/inspect`
   的当前租约作用域。普通 Agent 不能调用 Manager 的提案接口；原始输出必须经过固定验证器。
7. 结果先冻结、校验、保存检查点，再暂存候选。harness 不写 Task succeeded、不直接发布、不降低验收条件。
8. 原生会话续接必须绑定引擎版本与配置；不能直接把另一引擎的私有会话目录加载为当前会话。
9. 上下文按角色、阶段和当前 Task 收敛；完整证据和 operation 契约保留受控查询入口、摘要绑定及截断标记。查询是数据读取，不扩大 Task 权限。
10. 运行包准备后以实际服务 UID 和独立任务 UID 分别验证 import、只读挂载和执行。ACL/文件模式、父目录遍历、运行数据文件及构建顺序都纳入指纹和部署门禁。

## 验证与完成标准

覆盖启动失败、模型中断、超时、取消、旧 generation、输出 Schema 错误、检查预算耗尽、finalize 保留、恶意工具请求、
跨项目读取、越权写入、用量缺失和切换后的恢复。协议测试使用替身；隔离测试必须实际降权。
还需在独立部署上运行真实模型、浏览器和产物校验，记录实际 harness ID、指纹、模型及用量，
不能只以“可 import”“SDK 返回一句话”或模块名存在判定接入完成。

新能力同时更新 Profile 目录、运行包/依赖清单、诊断投影、接入说明和真机用例；发布遵循冻结版本门禁。

统一 Run 接入还必须验证：Control Job 不创建业务 Task；运行中输入只建立派发屏障，不取消当前模型/工具循环；所有结果仍绑定原 revision/lease。业务报告使用 report 对象，控制角色使用专属 envelope。实际调用记入 specialist_attempt_profiles，不能根据项目默认配置猜测执行的 harness。

Script工具必须返回结构化执行回执：退出码、已声明输出的存在状态/大小/哈希、有界stdout及截断标记。模型应收到实际已执行动作与回执，不能只有泛化的“执行完成”。探索性/分步脚本不要求每次都产出全部最终文件；只有result阶段才独立验收完整声明输出。finalize阶段只接受result/proposal，不再执行script/inspect/contract。每个脚本执行时限还须受当前Agent剩余时间限制；回执不等于业务验收通过。

## 原生结构化命令

structured-profile-v3通过ModelAdapter发送原生工具参数，模型传输DTO在`workflow_model_protocol.py`中编译为权威ManagerResult/Task契约。请求身份由宿主绑定到原冻结快照；任何编译都不能改用最新图版本绕过旧generation检查。变更只携带允许字段；自动replacement由宿主继承目标、验收、语义和交付阶段。Operation须先读取冻结contract，随后在工具Schema中提供精确角色和参数。

严格模式是请求策略，不等于网关已执行约束。明确的strict参数拒绝可在同一预算内回退非strict；HTTP 200但返回违反传输Schema的参数时，拒绝动作、记录约束未执行，并在后续纠正使用非strict。普通超时、鉴权、限流或服务错误不能伪装成strict能力回退。每个HTTP调用保留独立回执，未知用量不能填零；本地Schema、授权及业务验证始终执行。

finalize有独立调用上限，提前耗尽纠正额度后也不能把所有剩余普通轮数当作额外finalize调用。可执行动作按阶段收敛；重复错误保留计数，反馈指向实际字段，不重复堆积所有旧错误正文。
