# 分布式执行开发说明

适用于修改任务调度、执行租约、资源预留、沙盒启动、取消与回收、检查点、共享存储和跨节点成果发布。工作流业务状态和验收见[工作流开发说明](change-workflow.md)，对话路由和确认见[Agent 与对话开发说明](change-agent-conversation.md)，业务能力接入见[新增与扩展 operation](add-operation.md)。状态、权限和交付改动遵循[程序正确性审查原则](../program-correctness-audit.md)。

## 1. 核实运行边界和实现入口

修改 API 请求内的存储、媒体等待、上传发布或删除清理时，同时按 [API 并发与耗时隔离](change-api-concurrency.md)选择执行池和短事务边界。API 线程池容量不替代本篇的任务资源、租约和实际进程回收约束。

当前分布式模式使用 PostgreSQL 保存共享任务与执行记录，使用 S3 兼容对象存储保存加密素材、检查点和成果。各实例的数据目录是本地缓存；Redis 用于可选实时传输，不承担任务归属和正式成果的权威记录。应用仍有 SQLite 兼容路径，不能据此推断多节点可通过共享 SQLite 文件协调。

内置部署是单机多实例，本机 PostgreSQL 和多个执行实例仍会一起受宿主故障影响。跨节点协议和测试不能证明整机容灾。配置以[分布式执行配置](../distributed-execution-configuration.md)为准，实际拓扑和发布过程以[集群操作手册](../cluster-operations.md)为准；[执行隔离与恢复](../execution-isolation-and-recovery.md)提供隔离与恢复机制说明，并单独标识历史实施记录。

| 变更 | 实现入口 | 阅读重点 |
| --- | --- | --- |
| 候选、公平性和资源准入 | [scheduling.py](../../webapp/scheduling.py)、[agent_host.py](../../webapp/agent_host.py) | 候选与领取复核、节点能力、等待、实际资源分配 |
| 租约、执行尝试和预算 | [task_leases.py](../../webapp/task_leases.py)、[task_lifecycle.py](../../webapp/task_lifecycle.py) | claim、validate、renew、release、累计预算、取消和清理请求 |
| 跨进程事务 | [store.py](../../webapp/store.py)、[database.py](../../webapp/database.py) | 嵌套事务、租约检查、PostgreSQL 控制锁和只读观察 |
| 隔离运行环境 | [execution_runtime.py](../../webapp/execution_runtime.py)、[execution_resources.py](../../webapp/execution_resources.py)、[execution_contracts.py](../../webapp/execution_contracts.py) | TaskRuntime、身份池、cgroup、挂载、权限 profile |
| 固定工具和运行包 | [image_sandbox.py](../../webapp/image_sandbox.py)、[media_isolation.py](../../webapp/media_isolation.py)、[sandbox_limits.py](../../webapp/sandbox_limits.py)、[runtime_image.py](../../webapp/runtime_image.py) | GPU 租约、固定入口、seccomp、工具清单和运行时指纹 |
| 调用中断与独立回收 | [cancellation.py](../../webapp/cancellation.py)、[process_handles.py](../../webapp/process_handles.py)、[execution_reaper.py](../../webapp/execution_reaper.py) | 观察线程、本地期限、晚装载句柄、进程和身份回收 |
| 外部任务和清理 | [provider_calls.py](../../webapp/provider_calls.py)、[external_cleanup.py](../../webapp/external_cleanup.py) | 提交意图、供应商句柄、查询/取消、独立清理租约 |
| 检查点与恢复 | [checkpoints.py](../../webapp/checkpoints.py)、[step_checkpoints.py](../../webapp/step_checkpoints.py)、[session_checkpoint.py](../../webapp/session_checkpoint.py) | 项目与步骤快照、修订、会话打包、版本保留 |
| 对象上传和成果引用 | [artifact_store.py](../../webapp/artifact_store.py)、[artifact_transfer.py](../../webapp/artifact_transfer.py)、[object_encryption.py](../../webapp/object_encryption.py)、[artifacts.py](../../webapp/artifacts.py) | 不可变对象、加密校验、逻辑引用、提交和 GC |
| 部署与运维 | [cluster.py](../../deploy/cluster.py)、[prepare-executor.sh](../../deploy/prepare-executor.sh)、[executor_admin.py](../../webapp/executor_admin.py) | 运行包、系统权限、服务生命周期、排空和核实 |

按任务选读入口。只有需要改变通用机制时才修改调度或隔离层，operation 特例留在其实现中。

## 2. 先定义归属、状态与失败窗口

编码前写清本次影响的对象、权威记录、进入条件、提交条件、失权后的动作和恢复者。至少区分：

| 对象 | 开发时要保留的含义 |
| --- | --- |
| 任务与工作流步骤 | 业务目标及其修订；恢复不能重置已消耗预算或改变确认范围 |
| node 与 worker | node 表示执行节点，worker 表示本次执行器实例；进程重启不能沿用旧 worker 所有权 |
| slot、attempt 与 generation | 根阶段和步骤使用不同租约槽；attempt 标识一次执行，generation 区分执行代次 |
| 运行环境与资源声明 | 身份、进程、挂载、GPU 和实际容量；不能只看业务状态判断是否释放 |
| 本地/外部清理记录 | 本地物理回收与上游任务停止分别追踪，不由任务终态代替 |
| 检查点和成果引用 | 已提交、可恢复的版本；未提交的本地目录或对象不构成正式成果 |

同一 slot 不能同时有两个有效执行者。工作流独立步骤可各持有效租约；根 Agent、验收阶段与步骤执行互斥。修改并发策略要保留这一层次，并分别检查业务执行与取消控制路径，不能简单施加“整任务仅一条租约”的假设。

按实际流程列出中断窗口：领取后、分配身份后、启动前后、提交外部请求前后、上传后提交前、写入清理记录前后。为每个窗口确定可重试、复用结果、暂停核实或隔离等待的行为。

## 3. 修改调度、租约和资源预留

- 候选列表只是观察结果，`Coordinator.claim` 必须在事务中重新检查任务/步骤状态、暂停要求、依赖、节点白名单、运行环境绑定和租约冲突。读取候选后发生的验收或用户等待不能被旧候选绕过。
- 复用 Store 事务与租约绑定。当前 PostgreSQL 使用事务级 advisory lock 串行化短控制写事务；进程内锁不能代替跨实例约束。不要为改善吞吐直接绕过控制锁；改变锁范围需证明跨表领取、预算和发布仍原子，并补真实数据库竞争测试。
- 网络请求、对象传输、模型调用和耗时工具放在控制事务外。只读观察使用既有有界查询路径，避免与取消和续租争用长事务。
- 业务写入和正式发布验证当前 attempt、worker、generation、租约期限、取消和清理状态。领取成功只证明当时获权；租约过期后即使还没有接管者，旧执行器也不能续写。
- 数据库时间用于共享租约判断，本地单调时钟用于运行许可和看门狗。数据库失联或观察阻塞不能无限延长本地运行许可；调整续租周期和期限时联动检查这两条路径。
- 准入同时考虑节点并发、CPU/内存、GPU、用户额度、项目互斥、供应商并发及未完成清理。先判断资源是否可用，再分配昂贵运行环境；实际分配失败仍需保存等待原因并释放本次已获得的预留。
- 根任务与手动任务不能长期饿死彼此。保留公平候选顺序和 `next_poll`/重试时刻；无模型配置只能阻塞真正需要模型的执行，取消与可执行的本地步骤应继续推进。
- 异步上游等待期间释放计算资源，但供应商并发仍依据未完成调用计数；提交未知也不能当作空闲额度。查询既有任务与申请新的外部提交分别处理。
- 等待资源、等待上游、运行预算和累计调用数保持不同口径。恢复、换节点、暂停/继续不能重置预算；恢复后若调度类型改变，释放原尝试并按当前状态重新领取。

## 4. 修改沙盒启动、取消与回收

按“分配身份 → 准备运行目录和限制 → 启动 → 保存成果 → 清理”逐段审查。构造函数部分失败、执行器被强杀、取消与启动并发都必须有回收路径。

- 身份记录应包含可核实的 attempt、有效运行许可和所分配资源。文件锁、节点记录及启动标识共同保护身份复用；数据库租约到期不代表 UID、GPU、挂载和进程已经可复用。
- 保留独立 UID/GID、最小只读挂载、命名空间、cgroup 和 seccomp 限制。新增工具不能通过挂载整个宿主目录、继承服务凭据或扩大普通脚本网络权限来解决依赖问题。
- 输入准备和大文件复制也要纳入资源限制。tmpfs 和可选磁盘工作区都需要硬容量限制、文件数限制及清理；磁盘配额环境不可用时明确失败，不能退回无限制目录。
- 子进程、HTTP 和对象传输复用已有调用登记与取消包装器。启动前登记，句柄安装后仍应用此前到达的取消，结束时注销；中断动作不能无限阻塞观察线程。
- 普通脚本与固定模型工具分别检查权限 profile、运行包和资源范围。新增依赖核对工具清单、固定入口、读取权限和指纹；宿主能导入不代表隔离环境能使用。
- 本地回收要核实进程/cgroup、挂载、临时文件和身份锁。正常 teardown 与独立 reaper 均应能推进，不能只依赖执行器的 `finally`。旧清理回执须绑定旧 attempt，不能撤销后来复用身份的新任务。
- reaper 在查询数据库前处理本地许可到期，数据库不可用时仍能停止失权执行。清理重试和超时有界；失败保持身份隔离、资源占用及相应节点限制，不能先标 `complete` 再尝试回收。
- 不在租约释放或任务结束时直接清空所有资源占用。CPU/内存、节点槽和 GPU 的释放依据各自实际回收状态；检查等待中的其他任务能否在确认清理后正确恢复调度。
- 上游取消通过已保存的供应商任务与独立清理机制处理，本地回收不能代替上游停止确认；协议和结果映射按[外部服务专项](change-external-providers.md)处理。

## 5. 修改检查点、共享存储和成果发布

- 保留项目工作区/会话检查点与步骤检查点的分工。并行步骤只提交自身输出，不能覆盖整个项目快照；步骤恢复自身及依赖的当前修订，汇合前仍验证上游验收状态和哈希。
- 新对话可以恢复项目中保留的步骤成果用于查看，但文件可见性不改变候选/已验收状态，也不授予旧任务执行权限。检查点协议变化要同时检查 Agent、工作流步骤、验收与后续对话路径。
- 保存前停止或冻结相关写入，确保文件及会话状态一致；会话数据库及 WAL 按现有打包方式处理。静态技能、可重建索引、锁和缓存不应混入持久化会话。
- 上传对象和提交引用分开：先复制、校验、上传，再在短事务内复核租约、取消、所属项目、适用的 revision 和存储额度，提交逻辑引用与检查点。对象上传完成不代表交付完成。
- 使用项目内逻辑路径和不可变对象标识，检查目录穿越、链接、特殊文件、大小和 sha256；加密对象还需认证校验。下载校验完成后再替换本地缓存，不能让部分下载被下游消费。
- 恢复从已提交的共享记录和对象重建，不能依赖原节点缓存或猜测“最新”目录。失败时保留可诊断状态；外部提交未知且无可靠找回方式时暂停核实，不通过重提请求试探。
- 上传成功但引用提交失败的对象走现有延迟 GC；复用对象必须满足项目归属和有效引用条件。修改 GC 时覆盖保留版本、晚到上传、删除墓碑及同时发布，不能删除仍被引用的成果。
- 迁移检查点清单、会话 profile、运行时指纹或数据库字段时，明确旧记录能否读取、恢复或需要暂停。不能静默用新的运行环境替代已绑定环境，也不能通过清空数据库/旧成果完成迁移。

## 6. 验证故障、竞争与恢复

先运行受影响模块，再按发布流程完成全量检查。测试使用独立数据库、对象服务、节点目录和运行包；不得在已有服务的数据目录上注入故障。多进程及真实 PostgreSQL 用例用于验证跨实例事务，内存替身不能代替这些证据。

| 故障窗口 | 应证明的结果 | 相关测试入口 |
| --- | --- | --- |
| 同时领取、旧候选、资源不足 | 同一 slot 只获权一次；根/步骤互斥正确；等待不越过用户门禁、不重置次数 | [test_execution_leases.py](../../tests/test_execution_leases.py)、[test_scheduler_v2.py](../../tests/test_scheduler_v2.py) |
| 租约过期、续租失败、执行器重启 | 旧执行器不能续写或释放接管者；预算延续；新 worker 不重置健康任务 | [test_execution_leases.py](../../tests/test_execution_leases.py)、[test_distributed_races.py](../../tests/test_distributed_races.py) |
| 初始化失败、启动中取消、数据库阻塞 | 部分资源可回收；晚装载句柄仍取消；本地期限仍生效 | [test_execution_runtime.py](../../tests/test_execution_runtime.py)、[test_distributed_cancellation.py](../../tests/test_distributed_cancellation.py) |
| 清理失败、超时、重复或迟到清理 | 身份保持隔离；不提前释放容量；旧记录不影响新 attempt；后续可恢复清理 | [test_execution_reaper.py](../../tests/test_execution_reaper.py)、[test_scheduler_v2.py](../../tests/test_scheduler_v2.py) |
| 对象上传后失权、下载损坏、并发发布 | 不发布旧成果；错误对象不进入缓存；引用与清单一致；GC 保留有效对象 | [test_distributed_artifacts.py](../../tests/test_distributed_artifacts.py)、[test_distributed_races.py](../../tests/test_distributed_races.py) |
| 原节点强杀并丢失缓存、并行分支汇合 | 其他节点恢复已提交成果；分支互不覆盖；下游只使用正确修订 | [test_distributed_runtime.py](../../tests/test_distributed_runtime.py)、[test_distributed_workflow.py](../../tests/test_distributed_workflow.py) |
| 检查点打包、历史恢复和后续对话 | 会话一致；恢复范围受控；候选可查看但不能越过验收 | [test_session_checkpoint.py](../../tests/test_session_checkpoint.py)、[test_step_checkpoint_visibility.py](../../tests/test_step_checkpoint_visibility.py) |
| 数据库传输及 schema 变化 | PostgreSQL 与保留的 SQLite 路径行为一致；事务回滚、锁和时钟语义正确 | [test_distributed_database.py](../../tests/test_distributed_database.py) |

故障用例同时断言业务状态、租约/attempt、清理记录、实际资源和成果引用，不只检查异常类型。使用同步屏障或故障注入定位竞争窗口，避免只靠短暂 sleep 偶然命中。

例如，修改领取与清理时使用[隔离测试入口](../development.md#常规回归)：

```bash
.venv/bin/python -m tests.run_distributed_suite tests.test_execution_leases tests.test_scheduler_v2 tests.test_execution_reaper
```

修改运行环境、存储或接管时追加对应模块，发布前的完整检查统一按[开发与测试](../development.md)。

具体依赖和报告规则见[开发与测试](../development.md)。记录权限、系统依赖或模型缺失导致的跳过；HTTP S3 协议测试不证明真实存储产品行为，模拟供应商不证明其取消和幂等能力。目标环境的浏览器、存储和真实服务验收按受影响链路单独执行。

## 7. 完成标准与部署影响

交付前列明变更涉及的状态和数据库字段、租约/清理语义、检查点格式、运行包指纹、配置、系统权限和服务进程。同步[配置说明](../distributed-execution-configuration.md)及[部署手册](../cluster-operations.md)中的对应行为，避免只改代码而让旧服务缺少回收器或依赖。

发布按既有冻结源码、验证、备份、排空、更新及健康检查流程执行。重启只运行已部署版本；工作区代码发布需走部署入口。停止期间仍有未清理资源时保留所需 reaper 和数据库，不能将所有进程退出当成清理成功。数据库恢复需明确处理备份之后的数据，不能把代码回退等同于数据库回滚。

`deploy/cluster_smoke.py` 会停止实例并验证接管，只在维护/验收窗口执行。开发测试与生产故障演练分开安排。完成说明应包含定向及全量证据、故障后的实际资源状态、兼容与恢复限制，以及真实存储、供应商、多机环境和部署的未验证边界；文档修改不要求执行上述部署或故障演练。
