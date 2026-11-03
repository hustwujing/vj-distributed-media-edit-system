# Agent 与对话开发说明

适用于修改轻量对话与完整 Agent 的路由、上下文和提示、方案生成与确认、工具授权、任务控制、历史续接以及公开文本和活动事件。工作流内部执行、验收和用户决策按[工作流开发说明](change-workflow.md)处理；新增业务能力同时读取[新增与扩展 operation](add-operation.md)。

本文按开发顺序组织。行为背景见[对话执行设计](../conversation-execution-design.md)、[轻量分析交接](../conversation-fallback.md)和[自然语言任务控制](../natural-language-controls.md)；这些文档中的历史测试与发布结论不能作为当前变更的验收证据。状态和交付相关变更遵循[程序正确性审查原则](../program-correctness-audit.md)。

新增或升级 Agent 执行框架先读[新增 Harness](add-harness.md)，模型服务或协议接入先读[新增 LLM Provider](add-llm-provider.md)；本文负责对话路由、确认和授权语义。

## 1. 确定改动范围和入口

先说明触发消息、当前任务状态、预期行为，以及是否改变权限、确认依据、任务关联或可见事件。按范围读取对应实现，不要求每次修改所有文件。

| 变更 | 实现入口 | 需要核对 |
| --- | --- | --- |
| 请求、项目归属和任务控制 | [agent_api.py](../../webapp/agent_api.py)、[control_intent.py](../../webapp/control_intent.py) | 创建轮次、确认按钮、控制目标、并发请求 |
| 轻量回答、查询和交接 | [light_conversation.py](../../webapp/light_conversation.py) | 允许工具、参数、轮数限制、持久化交接 |
| 上下文、历史和关联任务 | [agent_context.py](../../webapp/agent_context.py)、[conversation_history.py](../../webapp/conversation_history.py)、[continuation_context.py](../../webapp/continuation_context.py) | 截断、项目范围、父任务、只读历史 |
| 执行意图与方案就绪 | [execution_intent.py](../../webapp/execution_intent.py)、[plan_readiness.py](../../webapp/plan_readiness.py)、[workspace_state.py](../../webapp/workspace_state.py) | 能力与实现、digest、内容指纹、确认消费 |
| 语义判断和讨论预算 | [semantic_review.py](../../webapp/semantic_review.py)、[planning_budget.py](../../webapp/planning_budget.py) | 结构化输出、不可用状态、限额和有界重试 |
| 完整 Agent 与工具入口 | [agent_host.py](../../webapp/agent_host.py)、[agent_sandbox.py](../../webapp/agent_sandbox.py)、[agent_gateway.py](../../webapp/agent_gateway.py)、[gateway_process.py](../../webapp/gateway_process.py) | 运行权限、受控调用、检查点和成果提交 |
| 持久化、文本和活动 | [agent_store.py](../../webapp/agent_store.py)、[agent_stream.py](../../webapp/agent_stream.py)、[agent_text.py](../../webapp/agent_text.py)、[live_activity.py](../../webapp/live_activity.py) | 正文快照、事件顺序、终态和重连 |
| 页面展示 | [chat.js](../../webapp/static/chat.js)、[plan_readiness.js](../../webapp/static/plan_readiness.js)、[task_progress.js](../../webapp/static/task_progress.js) | 方案卡、控制反馈、输入保留和状态恢复 |

调度类型 `agent/manual/workflow`、对话模式 `plan/execute`、轻重路由和隔离权限 profile 是不同维度。新增分析方式通常扩展现有路由或能力；不能仅为页面标签增加调度类型或把模式字段当作全部授权依据。

## 2. 修改对话路由和上下文

- 普通问答与项目元数据查询可以留在轻量入口；需要读取实际文件、分析画面或生成获准预览时，通过受控能力交接到完整 Agent。轻量入口不提供宿主代码执行能力。
- 修改轻量工具时同时检查工具描述、参数校验、项目归属、返回数据上限和调用记录。未知工具、非法参数、越权素材、服务失败不能被统一吞掉后当作正常升级。
- 交接必须在事务内检查当前租约、轮次状态和路由阶段，保存能力与原因，再将同一任务重新排队。由后续调度分配沙盒；重复交接、取消或租约失效不能再次排队。
- 查询与兜底有界。修改轻量查询轮数或讨论预算时，同步超时、取消、调用计数和耗尽后的行为；不能靠不断切换轻重路径重置预算。
- 交接原因、素材内容、历史对话和用户文档都是任务数据。模型提出 `general_analysis` 或其他能力请求不代表其判断已核实，也不授予正式编辑权限。
- 近期摘要要保留截断标记，并提供按需查询原始历史的路径。历史查询限定当前项目；关联任务沿明确的父子关系读取，不能任取项目最近一次工作流作为本轮上下文。
- 后续对话可参考旧步骤、候选产物和已回答决策，但历史执行授权不能继承为新轮次的授权。新增执行仍通过当前方案确认；上下文关联和用量关联不能扩大权限。

## 3. 修改方案生成、就绪和确认

先梳理完整链路：声明执行意图 → 保存 digest 与项目内容指纹 → 审核方案 → 对外暴露就绪状态 → 在事务中复核并消费确认 → 调度执行。修改任一环节都要检查按钮与自然语言两条入口。

- 普通回复成功不等于形成可执行方案。需要真实的结构化执行意图和就绪结果，页面才提供执行入口；模型说“已准备好”不能替代服务端校验。
- 意图中的 `single/workflow`、operation 列表及实现版本按当前模型验证。纯本地 helpers/脚本使用 `script`，外部生成式图片编辑使用对应 operation，不能混用能力名来获得授权。
- 确认绑定当前项目、方案轮次、意图 digest 和内容指纹，同时核对所选素材及画布目标。修改素材、正式工作内容、画布或意图时旧确认必须失效；仅缓存落地、检查点保存或节点切换不应使内容相同的方案失效。
- 按钮确认和自然语言确认复用 `plan_readiness` 门禁，事务内再次验证并只消费一次。语义服务返回确认之后，不能省略期间内容变化和任务状态变化的检查。
- 语义分类只提供判断结果。修改、撤回、疑问、歧义、无效响应或服务不可用不能当作确认；不要增加基于任意业务关键词的备用授权路径。
- 区分缺信息、讨论、审核中、审核不可用、过期和已消费状态。审核失败后的重试绑定同一方案与 digest，检查内容是否变化及是否已有新对话，并保留次数限制。
- 方案轮次失败或取消后，不应留下永久“审核中”的可见状态，也不能通过审核重试使中断任务恢复为可执行方案。

## 4. 修改提示、工具权限和任务交接

- 提示说明目标和操作流程，权限由服务端校验及隔离环境执行。讨论模式允许只读分析和指定目录内的样式预览，正式编辑、外部生成和新转写必须获得相应执行授权。
- 完整 Agent 可以在获准沙盒中编写并执行本地脚本。不得由宿主直接执行模型命令，也不能让脚本获取宿主网络、凭据、任意挂载或受控模型环境；外部服务与模型操作走登记能力。
- 新工具按其实际入口检查项目归属、当前尝试、运行权限、输入、预算和已确认意图。同时核对宿主回调、独立 gateway 进程和工作流入口，避免只有某条调用路径执行校验。
- 保留 `single` 直接工具与 `workflow` 提交的边界；不能因工作流尚未创建就允许多阶段计划走直接调用。审查 Agent 的只读权限与普通执行权限分别检查。
- 提示修改要同步轻量工具描述、完整 Agent 指令及适用的沙盒公开说明。公共能力入口见 [runtime/public/SKILL.md](../../runtime/public/SKILL.md) 和 [capabilities.json](../../runtime/public/capabilities.json)；新增沙盒文件需核对运行时打包，不能只在宿主目录添加文件。
- 模型回复结束、工具退出、检查点保存、工作流提交、技术验收和正式交付是不同事件。提交后台工作流后依赖持久化调度继续；不能重复提交或提前标记完成。直接执行保留 `completion_problem` 等完成检查。
- 若改变模型请求或响应解析，覆盖增量文本、工具参数、最终消息和异常响应，复用取消、脱敏和用量记录。不要把未识别的模型事件当作成功结果。

## 5. 修改任务控制和恢复

- 活动任务的查询、暂停、继续和取消作用于指定 turn；普通修改需求不自动变成控制命令，也不能将旧 turn 的控制转移给后来创建的任务。
- 执行中的修改需求由控制意图 `replan` 单独处理：服务端在重新核对目标仍活动后停止旧 turn，创建带 `parent_task_id` 的新 `plan` turn，并保留旧轮的素材/画布选择和只读 continuation。新轮必须重新声明意图、审核方案和确认；旧轮确认不能继承。`dispatch_kind` 仍只决定调度执行器，不能代替这次计划交接。
- 按钮和固定口令沿用确定性路径。自然语言分类前检查项目归属，分类期间不持有数据库/项目锁，返回后重新检查目标状态；分类器不能选择任务或直接执行控制。
- 分类不确定、无效或不可用时保持任务不变，并保留用户输入。否定、引用、假设、局部修改和混合指令应覆盖在测试中；`message` 与显式 `action` 同时提供时不能互相矛盾。
- 暂停、继续和取消复用既有工作流及任务生命周期接口。继续不能代替新计划确认或外部重生成授权；取消不依赖模型可用性，并单独表达本地停止和上游停止状态。
- 恢复以持久化任务、历史、检查点和租约为依据。修改会话恢复时核对会话 profile、线程记录与已提交检查点是否匹配；只有线程 ID 不足以证明会话状态可恢复。
- 失效租约、旧 attempt、取消后的回调不能重新排队、修改画布或发布成果。涉及隔离与检查点的变更同时阅读[执行隔离与恢复](../execution-isolation-and-recovery.md)，以当前运行实现核实部署边界。

## 6. 同步公开事件、用量和页面

Agent 生产端复用文本发布器、持久化事件与当前活动模型，区分公开正文、过程说明、工具活动和最终答复，不输出内部推理。新增状态要同步 API 和消费方；SSE 版本合并、重连及交互状态保留按[前端专项](change-ui-artifacts.md)处理，不能以回复文字代替服务端就绪或完成状态。

轻量请求、语义审核、完整 Agent 和工具调用都接入实际调用记录，交接时保留 task/run/parent 关联。脱敏、计量去重、Token 未知值及汇总口径按[可观测性与用量专项](change-observability-usage.md)处理，不能由对话层另建累计值。

## 7. 验证与完成标准

按变更选择有行为断言的测试，不只检查提示包含某句话或接口返回 200。优先验证状态、权限和恢复不变量。

| 场景 | 应验证的结果 | 测试入口 |
| --- | --- | --- |
| 轻量回答、查询和交接 | 问答不创建沙盒；交接持久化且保持权限；非法请求、重复交接及取消不能升级 | [test_conversation_routing.py](../../tests/test_conversation_routing.py) |
| 就绪、确认及内容变化 | 无意图不能执行；按钮/自然语言只消费一次；旧 digest、目标变化被拒绝；相同内容检查点不误失效 | [test_conversation_routing.py](../../tests/test_conversation_routing.py)、[test_workspace_readiness.py](../../tests/test_workspace_readiness.py)、[plan_readiness.test.mjs](../../tests/plan_readiness.test.mjs) |
| 工具和预览权限 | 讨论、执行及审查权限隔离；跨项目或取消后写入被拒绝；预览不冒充正式成果 | [test_distributed_gateway.py](../../tests/test_distributed_gateway.py)、[test_style_previews.py](../../tests/test_style_previews.py) |
| 控制与竞争 | 分类失败不改任务；分类期间任务结束不会误操作；暂停/恢复/取消遵守状态机；replan 创建正确父子轮次且保留只读上下文 | [test_control_intent.py](../../tests/test_control_intent.py)、[test_distributed_control.py](../../tests/test_distributed_control.py) |
| 历史、续接和会话恢复 | 原始约束可查询；旧授权不继承；检查点恢复不重复提交业务操作 | [test_conversation_routing.py](../../tests/test_conversation_routing.py)、[test_user_decisions.py](../../tests/test_user_decisions.py)、[test_session_checkpoint.py](../../tests/test_session_checkpoint.py) |
| 正文、活动和用量 | 分片、重连和乱序不回退；终态一致；不泄露内部文本；计量不重复 | [test_agent_text.py](../../tests/test_agent_text.py)、[test_live_activity.py](../../tests/test_live_activity.py)、[test_task_usage.py](../../tests/test_task_usage.py)、[text_stream.test.mjs](../../tests/text_stream.test.mjs) |

例如，修改路由或确认时使用[隔离测试入口](../development.md#常规回归)：

```bash
.venv/bin/python -m tests.run_distributed_suite tests.test_conversation_routing tests.test_workspace_readiness
node --test tests/plan_readiness.test.mjs tests/text_stream.test.mjs tests/task_progress.test.mjs
```

其他改动选择表中相关模块；发布前按[开发与测试](../development.md)完成全量 Python、前端及适用的浏览器检查。协议替身验证程序面对模型输出的处理，不能证明真实模型对自然表达的识别质量。提示、分类或交接行为变化时，用真实模型检查问候、分析、追问、修改、否定、歧义和确认样例；页面变化补浏览器验证。真实服务不可用时记录未验证范围。

完成时应具备：触发条件与行为变化、受影响权限和状态、接口/事件/提示变更、兼容处理及对应验证证据。同步[网页使用指南](../../WEB_UI.md)、[页面交互说明](../../UI_DESIGN.md)或相关协议文档；仅有文案变更时检查链接、入口和示例即可。交付说明明确真实模型、浏览器与部署的验证边界，工作区修改不代表已部署。
