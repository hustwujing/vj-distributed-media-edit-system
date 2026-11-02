# 新增与修改 operation 生命周期

适用于新增 `Lifecycle` 类型、改变已有阶段顺序/返回契约，或修改 operation 的阶段持久化、唤醒、恢复和取消语义。业务能力和实现注册先按[新增与扩展 operation](add-operation.md)确定扩展层级；本文负责生命周期框架的改动顺序与完成标准。状态和交付遵循[程序正确性审查原则](../program-correctness-audit.md)。

## 1. 先说明现有流程的缺口

当前支持 `local`、`remote_sync` 和 `remote_async`。换模型、换供应商或增加能在已有阶段内完成的处理，优先注册 implementation；步骤间组合用工作流依赖表达。只有现有生命周期无法表达需要持久化、调度或恢复的新转换时，才扩展通用框架。

新增生命周期前写出状态图或转换表，并说明它与现有流程的区别。例如“提交后等待外部回调”只是待设计场景，不能把它作为当前已经支持的第四种生命周期。

每个阶段必须定义以下内容；工作流状态、步骤状态、执行 attempt、外部任务状态分别记录，不能用一个字段混合表达。

| 项目 | 必须明确 |
| --- | --- |
| 进入与退出 | 前置状态、依赖与授权、允许的下一状态、终态及非法转换 |
| 调用契约 | handler 参数、返回结构、错误类型、候选产物及外部句柄 |
| 持久化边界 | 调用前保存什么、调用后提交什么、数据库事务与外部副作用之间的未知窗口 |
| 唤醒与期限 | 调度/轮询/事件入口、去重依据、下一次执行时间、阶段及整任务期限 |
| 恢复与重试 | 崩溃后依赖哪些持久化证据，允许重执行、查询已有结果还是暂停核实，重试上限 |
| 暂停与取消 | 安全暂停点、本地中断、上游停止确认、迟到回执和清理责任 |
| 容量与身份 | CPU/内存/GPU、供应商额度、连接、租约、执行身份及各自实际释放条件 |
| 兼容与展示 | 旧记录和旧版本如何处理，公开状态、等待原因、错误及用户可用操作 |

## 2. 核实当前框架中的分支

生命周期目前并非仅靠注册就可扩展。修改前逐项核对以下入口；表格用于影响分析，不要求无差别修改所有文件。

| 入口 | 当前需要核对的接线 |
| --- | --- |
| [operation_lifecycle.py](../../webapp/operation_lifecycle.py) | `Lifecycle`、`Protocol/PROTOCOLS`、`RecoveryAction`、`Implementation.phase/invoke/recovery/recover/resume_state` 及注册校验 |
| [workflows.py](../../webapp/workflows.py) | `Runner.step/tick/claim/recover`、`resume`、暂停/取消、状态写入、异常处理和公开投影 |
| [workflow_runtime.py](../../webapp/workflow_runtime.py) | `Adapter` 的受控阶段调用、输入环境、结果收取、检查点、验收及发布 |
| [operations.py](../../webapp/operations.py) | 能力目录中由协议生成的 `recovery_by_phase`、取消描述、实现绑定与参数校验 |
| [scheduling.py](../../webapp/scheduling.py)、[task_leases.py](../../webapp/task_leases.py) | 可领取状态、资源准入、等待唤醒、接管及运行环境绑定 |
| [provider_calls.py](../../webapp/provider_calls.py)、[external_cleanup.py](../../webapp/external_cleanup.py) | 调用意图、上游句柄、晚到回执、独立取消与核实 |
| [execution_contracts.py](../../webapp/execution_contracts.py)、[task_lifecycle.py](../../webapp/task_lifecycle.py) | 错误/等待分类、预算、期限及终止控制 |
| [execution_metrics.py](../../webapp/execution_metrics.py)、[observation_collector.py](../../webapp/observation_collector.py) | 阶段展示、采集白名单与指标汇总 |
| [manual_async.py](../../webapp/manual_async.py)、[manual_executor.py](../../webapp/manual_executor.py)、[agent_gateway.py](../../webapp/agent_gateway.py) | 手动和直接工具入口是否经过新协议；不能假定它们都调用通用 Runner |

当前协议的执行边界：

- `PROTOCOLS` 为每种生命周期显式定义允许阶段、步骤状态到阶段的映射及恢复动作。`Implementation.phase()` 校验状态和外部句柄；未知生命周期、非法状态、缺失句柄或错误携带句柄均拒绝推进，不再默认落入异步提交。Runner 将不受支持的协议记录为 `lifecycle` 错误并暂停。
- `Implementation.recovery()` 返回 `RecoveryAction`；`Implementation.recover()` 执行该动作的决策并返回状态变更，由 `Runner.recover()` 在现有租约/事务机制下写入。能力目录从同一协议生成适用阶段的恢复信息。新恢复动作必须同时有可执行分支，不能只填写目录说明。
- `resume_state()` 校验人工继续的目标状态，公开 `resumable` 和实际继续复用该校验。新增阶段还须接入 Runner 的可运行/可恢复状态及调度入口，不能只接正常执行。
- `records_calls=False` 为同步实现的保守默认：缺少调用记录不能证明未提交，崩溃或超时后暂停核实。只有每次外部副作用前都保存 `provider_calls.intent`，并在结果持久化后保存对应 `result` 的实现，才可声明 `records_calls=True`。内置转写和同步生图已经使用该契约；其他实现必须补齐代码与故障证据后启用。
- 对声明上述契约的同步实现，恢复时无意图可回到提交前；有意图但结果未知则暂停；成功且保存了产物列表则进入验收；成功却缺少持久化产物则暂停为 `result_unavailable`。恢复不再依赖 executor 名称。
- `register()` 拒绝未知协议、拼错的 handler 阶段和缺失的阶段入口；适配器方法是否实际存在仍需集成验证。`Protocol` 校验 Runner 支持的阶段及完整恢复映射。`cancellation` 字段仍是能力描述，取消和实际回收需按后文单独接通。

## 3. 实现协议、持久化和状态推进

1. 在 `operation_lifecycle.py` 定义类型及对应 `Protocol`，明确阶段、状态映射和 `RecoveryAction`，完善注册时校验。新增阶段要同时接入 `invoke`、Runner 的执行分支及协议支持集合；新增恢复动作要接入 `recover` 的可执行决策。通用转换按生命周期分派；供应商解析及业务特例留在 handler，不新增按 operation ID 分支。
2. 在 Runner 接入阶段选择、领取、调用结果提交、失败分类和下一次唤醒。每次 handler 完成一个有界阶段后返回；等待外部事件或结果通过持久化状态让出调度，不自行创建无限轮询线程。
3. 检查阶段状态能否由现有记录无歧义恢复。只有不足时才新增字段或状态；同时处理初始化/迁移、读取、公开投影、状态写入、恢复及继续操作。不能把进程内 future 或本地临时目录当作唯一恢复依据。
4. 外部副作用前保存调用意图，结果返回后持久化句柄/回执/路由。事务与网络调用之间发生崩溃时，按已保存证据核实或暂停；请求键本身不证明上游幂等，结果未知不能自动重提。
5. 状态提交继续校验项目、任务、revision、attempt 和有效租约。重复、乱序和迟到结果不得回退终态、覆盖新修订或重复发布。事件/回调驱动的新增流程还须定义来源认证、事件去重和丢失后的恢复；请求入口只记录受控证据或安排唤醒，不能绕过执行授权直接发布。

步骤状态、质量决策和恢复的共同约束见[工作流开发说明](change-workflow.md)，外部协议及调用记录见[外部服务与供应商接入](change-external-providers.md)。

## 4. 接通资源、取消、验收与各业务入口

- 更新调度候选、可领取状态及资源计算，证明新等待状态不会永远无法调度，也不会被忙轮询。等待阶段按需释放本地计算容量；外部结果未知时仍保留对应未结束调用的占用。新增生命周期不意味着新增 `agent/manual/workflow` 调度类型。
- 检查新流程需要的运行包、节点能力及环境绑定。正常结束、取消、期限到达和租约失效都必须触发适用的本地回收；实际资源未清理前不能释放身份或伪造容量归还。具体机制复用[分布式执行开发说明](change-distributed-execution.md)。
- 本地停止等待与上游停止分别记录。外部清理复用独立租约和有界重试；晚到句柄可补充证据及安排清理，不能恢复已取消业务。取消先于句柄安装、清理失败及晚回执都需有明确路径。
- 输出继续走 `Result` / `ProducedFiles`、实际文件校验、候选保存、技术及适用的业务验收、检查点和正式发布。注册新 handler 不自动保证检查点已保存；必须验证原节点缓存丢失后仍能验收和收取。验收和发布失败只能恢复相应阶段，不能重新生成已有成果。
- 明确本次支持工作流、手动工具、Agent 直接调用中的哪些入口。每个开放入口都验证实现版本与确认范围；尚未接通的入口明确拒绝，不用默认实现静默降级。
- 如需新增 API 回调、查询或控制路由，遵守 [API 并发与耗时隔离](change-api-concurrency.md)：异步等待或专用有界池、短事务、独立控制容量；API 线程池不替代任务调度和执行租约。

## 5. 版本兼容、观测和文档

实现使用固定版本 ID。改变阶段语义或返回结构时，明确旧 implementation、已有方案、持久化任务和检查点的兼容策略；不能在同一个版本 ID 下静默换协议。旧节点遇到新协议应拒绝领取或执行，发布顺序须避免不兼容节点接管；回退前说明新状态能否由旧代码读取和处理。

新增阶段同步错误分类、等待原因、活动展示、指标白名单及 API/SSE/UI 投影。当前 handler 键 `sync` 与指标阶段 `remote_sync` 命名不同，维护时核实两端映射；不要只加显示标签而漏掉采集。用量和回执关联任务、步骤、revision、attempt，重复及迟到事件不得重复计费；完整规则见[可观测性与用量开发](change-observability-usage.md)。

同时更新[工作流契约](../workflow-contracts.md)、[operation 接入规范](../operation-onboarding.md)及生命周期表；注册接口变化同步[接入指南](../operation-extension-guide.md)。公开行为、配置或部署要求变化时，再更新对应用户/运维说明。

## 6. 验证和完成标准

新增生命周期需要行为测试，不能以注册成功、目录出现枚举或单次正常执行作为完成依据。用同步屏障、故障注入和真实持久化证据覆盖以下边界：

| 场景 | 必须证明 |
| --- | --- |
| 协议与正常推进 | 必需 handler、阶段参数/返回值正确；非法阶段拒绝；正常路径及条件分支可完成验收发布 |
| 持久化边界崩溃 | 每个外部调用前后、句柄保存前后、候选/检查点/引用提交前后中断，恢复不重复副作用、不丢已提交结果 |
| 多实例竞争 | 同阶段重复领取、事件重复/乱序、旧租约和旧 revision 回执不能覆盖当前状态或重复交付 |
| 暂停、取消与期限 | 每阶段控制有效、等待有界，晚安装句柄仍可中断；本地回收与上游停止如实区分 |
| 资源与唤醒 | 等待不长期占用计算槽，未知外部调用不提前释放额度；恢复按新阶段资源需求准入，无泄漏或超配 |
| 检查点与发布 | 原节点缓存丢失后可恢复；验收/发布失败复用既有产物，下游只消费正确的已验收修订 |
| 入口、版本与观测 | 已开放入口遵守同一授权与协议，旧实现/数据兼容，指标、用量和展示与真实状态一致 |
| 原有流程回归 | `local`、`remote_sync`、`remote_async` 的执行、恢复及取消行为仍成立 |

复用 [test_lifecycle_protocols.py](../../tests/test_lifecycle_protocols.py) 的协议拒绝、持久化恢复及同步提交未知用例，以及 [test_operation_lifecycles.py](../../tests/test_operation_lifecycles.py)、[test_workflows.py](../../tests/test_workflows.py)、[test_workflow_reliability.py](../../tests/test_workflow_reliability.py) 的夹具；调度、租约和竞争参考 [test_scheduler_v2.py](../../tests/test_scheduler_v2.py)、[test_execution_leases.py](../../tests/test_execution_leases.py)、[test_distributed_races.py](../../tests/test_distributed_races.py)。外部清理追加 [test_external_cleanup.py](../../tests/test_external_cleanup.py) 及受影响入口的用例。这些现有测试是起点，不能代替新增阶段的故障覆盖。

定向验证示例，另追加新生命周期及受影响机制的测试模块：

```bash
.venv/bin/python -m tests.run_distributed_suite tests.test_lifecycle_protocols tests.test_operation_lifecycles tests.test_operation_contracts tests.test_workflows tests.test_scheduler_v2
```

交付应包含状态转换定义、持久化/兼容策略、支持入口、恢复和取消行为、资源释放依据及故障证据。尚未验证的外部取消、真实供应商或混合版本恢复明确列出限制。全量检查和发布按[开发与测试](../development.md)；仅修改本文时核对链接、接口与示例，不触发业务执行或部署。
