# 分布式调度、执行与 operation 体系改造 TODO

记录日期：2026-10-31。

状态：原分布式实施范围已完成，306项测试通过；新增第 11 项 P0 可观测性专项已单独实现并完成 24 项验收，见 observability-implementation-report.md。用户明确无需数据迁移和向后兼容。下文涉及旧格式迁移/兼容的要求由此取消，保留其余功能与测试范围。后续用户已追加部署授权，单机多实例部署进度与证据见 `cluster-deployment-report.md`。进度与证据见 `distributed-execution-implementation.md`。

目标：多个调度器、执行器和 API 实例共享任务状态与持久化成果，任务可以跨机器接续执行，不依赖前一个执行器的本地文件。同时重构 operation 能力契约、处理器和生命周期，使新增能力可以复用执行框架。保留现有租约、执行代次、验收及外部调用保护机制。

范围说明：第 1–7 项为分布式改造，第 8–10 项为后续补充的 operation 体系改造和新能力接入规范，后续统筹实施。下文的接口名称及示例是目标设计，不代表当前已有实现；`media.extract_audio` 是接入流程示例，不自动计入必须新增的业务能力。

## 1. 共享状态数据库

- [x] 确定共享服务端数据库，制定本地 SQLite 数据迁移、验证和回退方案。
- [x] 改造任务领取、续租、超时接管、取消的事务与锁，替换对 SQLite `BEGIN IMMEDIATE` 的依赖。
- [x] 原子化处理并发额度、资源预占、执行记录和节点状态推进。
- [x] 统一使用数据库时间，支持多个调度器同时竞争任务。

## 2. 共享文件存储

- [x] 将素材、脚本、配置、产物和检查点接入对象存储。
- [x] 建立文件记录：文件 ID、对象位置、哈希、大小、项目归属、节点和修订版本。
- [x] 节点间通过文件引用传递数据，解除宿主绝对路径依赖。
- [x] 提供统一上传、流式读取、下载到本地接口。
- [x] 配套项目访问控制、保留策略、存储额度及孤立文件清理。

## 3. 拆分文件准备与运行环境

- [x] 分离输入解析、文件下载、沙箱运行和结果提交职责。
- [x] 宿主操作按需读取文件，不强制创建完整沙箱运行环境或恢复整个项目。
- [x] 增加按哈希复用的本地文件缓存及完整性校验。
- [x] 支持 tmpfs 和有配额的本地磁盘工作区，适配大视频处理。
- [x] 保持沙箱内稳定的逻辑路径，宿主路径由本次执行环境解析。

## 4. 防止过期执行器提交结果

- [x] 保留并完善 `attempt_id`、租约和 `generation` 校验。
- [x] 每次执行使用独立、不可变的产物存储位置。
- [x] 先完成文件上传，再事务提交产物引用和节点状态。
- [x] 提交时校验执行归属、节点修订及输入版本。
- [x] 拒绝旧执行器发布或覆盖正式成果。
- [x] 对上传成功但提交失败的对象提供安全清理机制，避免清理仍被引用的成果。

## 5. 节点输入输出与版本化检查点

- [x] 将工作流输入绑定到明确的产物版本。
- [x] 每个节点独立输出，保存不可变产物清单。
- [x] 下游按依赖和输出角色获取文件。
- [x] 将 Agent 会话、脚本及项目编辑状态单独版本化，控制并发写入。
- [x] 第一阶段保留项目串行约束，实现跨节点接力。
- [x] 后续增加同一工作流分支并行，处理项目快照覆盖、冲突和汇合问题。

## 6. 外部调用恢复与节点故障处理

- [x] 在共享数据库保存外部请求意图、任务 ID、参数、输入版本、供应商实现和调用状态。
- [x] 支持其他执行器接续查询、下载和取消，安全管理跨节点恢复所需认证上下文。
- [x] 接入供应商幂等及按请求标识查询能力；无法确认提交结果时，不盲目重复提交。
- [x] 分离节点本地资源清理与全局任务接管职责。
- [x] 本地 reaper 负责本机进程、挂载、GPU 和 UID，全局协调负责节点失联及恢复。
- [x] 处理永久失联节点，避免清理记录永久阻塞项目。
- [x] 限制旧执行器在失联后恢复时继续产生外部影响；不把租约到期等同于旧进程已停止。

## 7. 分布式调度、执行和 API

- [x] 明确调度器、执行器、API 的职责和交互协议。
- [x] 定义任务领取载荷：任务/节点/修订、执行代次、租约、输入引用、执行定义、环境版本和资源要求。
- [x] 建立执行器注册、心跳、能力上报、资源匹配和排空机制。
- [x] 共享日志、进度事件和下载入口，支持任意 API 实例承接请求。
- [x] 取消以共享数据库和执行器本地轮询为主路径，API 无需定位执行机器或定向发送终止指令；执行归属记录仍用于租约、接管和审计。
- [x] 如引入消息队列，处理重复和丢失通知；以数据库状态判断任务归属和完成状态。 （本轮不适用：未引入消息队列；无通知通道的轮询主路径已测试。）
- [x] 明确运行环境及脚本版本兼容策略，支持跨节点接管和滚动升级。

## 8. operation 体系分层与统一契约

### 8.1 业务能力、具体实现与生命周期分离

- [x] 保持 operation 为稳定业务名称，不因供应商、同步/异步方式或 CPU/GPU 实现变化而改变业务名称。
- [x] 建立 `operation → implementation → lifecycle` 的映射；同一业务能力可以注册多个实现。
- [x] 为能力契约和具体实现分别定义版本，执行时固定所选实现、环境版本和输入版本。
- [x] 持久化所选实现，恢复查询不得按当前全局配置重新选择供应商。
- [x] 评估将现有 7 个 executor 标识映射到通用生命周期；原有“四类流程”只是讲解分类，不作为必须保留的枚举。
- [x] 保留 script 与 operation 的权限区别；复用底层执行设施不代表允许 operation 调用者指定任意命令或 URL。

### 8.2 单一能力契约

- [x] 集中定义能力名称、契约版本、输入角色/类型/数量、参数模型/默认值/范围、输出角色/类型/数量。
- [x] 用同一份契约生成 Agent 能力目录、服务端参数校验和处理器接收的数据结构。
- [x] 逐步解除固定媒体能力对大而通用的 `JobInput` 和独立参数白名单的耦合。
- [x] 建立处理器注册机制，避免每新增一个普通能力都修改通用 Runner 或中央 Worker 分支。
- [x] 检查重复注册、缺失实现、参数模型和输出契约不完整等问题；未知 operation/action 明确拒绝。
- [x] 区分能力已注册、服务已配置、节点具备运行环境、上游当前健康等状态，不将配置存在等同于运行可用。

### 8.3 生命周期协议

- [x] 定义本地处理协议，例如 `execute(context, inputs, parameters)`。
- [x] 定义同步远程调用协议，例如 `invoke()` 及必要的结果收取约定。
- [x] 定义异步任务协议，例如 `submit()`、`poll()`、`collect()`。
- [x] 明确可选的 `cancel()`、`reconcile()` 能力及不支持时的行为，不要求所有实现伪造这些接口。
- [x] Runner 统一负责状态推进、依赖判断、调度时间、租约、超时、取消和结果提交；具体处理器只实现业务或供应商逻辑。
- [x] 固定媒体、固定图片按需共享本地执行流程，具体工具、GPU 和环境差异由实现描述。

### 8.4 统一输入、外部句柄与输出

- [x] 将跨节点文件引用 `ArtifactRef`、本次执行的本地路径和外部任务句柄 `ProviderHandle` 分开建模。
- [x] 文件引用绑定归属、版本、哈希和存储位置；处理器使用显式输入，不自行猜测项目目录中的最新文件。
- [x] 字幕、已有转写、配置和脚本等必要数据均纳入显式依赖，消除跨节点恢复时的隐含文件依赖。
- [x] 外部句柄包含供应商、实现版本、任务 ID 及受控恢复上下文；认证信息不暴露给 Agent 或普通产物。
- [x] 返回统一的结构化结果，包含输出角色/路径、技术元数据、用量成本和诊断信息；未知成本保持未知。
- [x] 执行器核验处理器声明的输出，补充哈希、大小和持久化文件引用，不信任处理器自报的完整性数据。
- [x] 产物上传、引用提交、版本校验复用第 2、4、5 项，避免新增另一套存储和提交机制。

### 8.5 分阶段恢复与错误模型

- [x] 将单一 `retry_safe` 的表达逐步扩展为按阶段声明的恢复策略，并明确旧字段的迁移语义。
- [x] 本地重执行要求输入版本固定、输出目录独立且副作用受控。
- [x] 外部提交仅在确认未提交或具备有效幂等保障时重提；未知结果进入核实或暂停流程。
- [x] 查询失败继续查询既有任务，下载失败重新收取既有结果，验收失败不默认重新生成。
- [x] 结构化错误包含阶段、错误类型、外部状态是否已知及恢复建议；恢复建议由 Runner 按权限和策略裁决。
- [x] 重试、修复和实现切换不得扩大已确认操作范围或外部调用额度。

### 8.6 资源与权限独立声明

- [x] 资源需求由具体实现及阶段决定；本地 GPU 执行、远程查询、下载和媒体验收分别核算。
- [x] 将输入读取、输出写入、网络访问、外部调用和付费副作用权限与 operation 业务名称分离表达。
- [x] Agent 只选择已授权能力并提供业务参数；可执行程序、供应商地址、认证和环境权限由服务端控制。
- [x] 实现选择结合节点能力、资源和兼容性，保持用户已确认的业务语义及执行意图约束。

### 8.7 执行与验收分离

- [x] 明确“处理器执行完成”“技术检查通过”“业务验收通过”“正式交付”之间的状态边界。
- [x] 统一文件完整性、媒体元数据等技术检查；按节点要求接续程序、Agent 或用户验收。
- [x] 不把退出码为 0、供应商返回成功或文件存在直接作为节点验收通过的依据。
- [x] 下游仅消费符合依赖版本且已验收的产物；复用已有验收事件、修订和修复记录。

### 8.8 可观测性

- [x] 统一进度事件格式，支持具体阶段、进度和可展示信息，不依赖解析任意日志文本。
- [x] 日志、进度、错误、用量关联 operation、实现版本、节点修订和 attempt，接入第 7 项共享事件机制。
- [x] 区分排队、文件准备、本地计算、外部提交、外部等待、下载和验收耗时。

### 8.9 取消协议、状态与竞态（已实现并验证）

以下协议已实现并按检查表验证。取消主路径采用第 8.10 项的共享数据库加本地统一监控机制，不依赖定向消息投递。现有本地 reaper、外部 cleanup 和未知提交保护可复用，但需要按共享数据库、独立清理租约及实现版本重构。

- [x] 默认取消范围为整项 workflow：停止新节点、关闭后续验收/修复入口、保留取消提交前已正式保存的成果。未来如提供单节点取消，必须显式定义后继依赖如何阻塞或终止，不能默认跳过后继续执行。
- [x] 分开持久化业务取消请求、本地资源清理、上游实际状态和取消核实结果；业务停止不代表进程已退出、上游已取消或费用已退回。
- [x] 业务状态可在取消事务提交后进入已取消，但展示本地清理中、上游仍运行、上游状态未知等独立信息，不等待不可控供应商而无限停在单一 cancelling 状态。
- [x] 在共享数据库事务内记录取消请求 ID、作用范围、原因、时间及控制版本，阻止新业务领取/外部提交授权和后续成果生效，并原子生成持久化清理意图。
- [x] 多次取消请求幂等；本地监控线程批量轮询数据库，执行器还须在续租、开始阶段及提交成果时检查取消状态。通知仅是可选加速，不作为正确性前提。
- [x] 定义取消与成果提交的事务顺序：成果先提交则保留，取消先提交则拒绝将晚到成果作为成功结果发布；已完成的整个工作流收到取消请求应返回已完成而非回滚状态。
- [x] 业务提交权限撤销后，仍允许独立、受限的清理/对账通道记录晚到外部任务 ID、供应商实际终态和用量；该通道不能恢复业务执行、发布成果或重新生成。
- [x] 以 operation 实现及阶段声明取消能力：本地终止、仅中止客户端等待、远程取消、仅排队阶段可取消、不支持取消；取消能力随已绑定实现保存，不按当前配置猜测。
- [x] `cancel()` 返回结构化结果，区分请求已受理、已确认停止、不支持、当前阶段不可取消、状态未知；请求受理不得直接等同于上游 cancelled。
- [x] `reconcile()` / `poll()` 记录上游真实终态，保留 succeeded、failed、cancelled 的区别，不因业务已取消而将供应商完成结果改写成取消。

| 取消时所处阶段 | 目标行为 |
|---|---|
| 排队、依赖等待、资源等待，尚未开始 | 阻止领取并取消待执行节点，不创建沙箱或发起新外部请求 |
| 输入下载、缓存或临时工作区准备 | 停止传输/准备，清理本次私有文件，不删除共享缓存中仍被引用的数据 |
| 本地进程执行 | 通知协作停止，宽限期后强制终止本次进程/cgroup，核实子进程退出后回收挂载、GPU、UID 和磁盘 |
| 同步远程调用已发出 | 中止客户端等待；有取消协议则调用，否则记录上游可能继续，不宣称断开 HTTP 就停止计算 |
| 异步提交正在进行、任务 ID 未持久化 | 根据请求标识核实；晚到 ID 进入独立对账记录并安排取消；无法核实则保持 unknown，不重发生成请求 |
| 异步等待、已知任务 ID | 独立清理任务按已绑定实现调用取消并查询核实；仅排队可取消或不支持时如实记录 |
| 结果收取、上传或验收中 | 停止业务收取/验收，拒绝晚到成果生效；未引用上传交由 GC，已正式保存成果保留 |
| 节点或整个工作流已完成 | 保留已提交状态及产物，不自动撤销业务效果；整项工作流取消只影响仍未完成部分 |

- [x] 明确外部请求与数据库取消之间无法跨系统原子提交的窗口：已获授权且正在发送的请求可能仍被上游接受，必须依靠幂等/查询/清理核实，不能承诺取消后绝无在途影响。
- [x] 清理任务有独立租约、执行代次、幂等标识、重试预算、截止时间和结果回执；取消应立即创建清理意图，不依赖原计算执行器继续存活。
- [x] 远程取消和核实可由其他节点接管；物理进程、挂载和 GPU 由原节点 reaper 或明确的基础设施隔离机制处理。
- [x] 失联节点的进程状态保持未知，隔离节点并阻止其成果生效；不能由另一台机器因“本地找不到进程”而认定清理完成。
- [x] 清理回执绑定原 attempt / generation / 外部句柄，旧回执不得终止或释放新执行尝试的资源。
- [x] 区分业务占位、节点实际资源和上游并发占用；业务取消不立即代表 GPU 空闲或上游并发已释放。未知占用有独立核实和处置策略，不永久锁死项目。
- [x] 定义核实预算耗尽后 unconfirmed / unknown / unsupported 等状态的展示、后续对账和人工处置入口；仅为了核实终态的查询不恢复业务执行。
- [x] 取消不默认触发补偿、删除已交付文件、退款或其他不可逆操作；如果能力需要补偿，另定义授权、幂等和审计契约。

### 8.10 本地统一取消监控（已实现主路径）

```text
API / 网关将取消意图写入共享数据库，并保存清理意图
    ↑ 各执行实例主动批量检查自己持有的活跃调用
本地统一监控线程
    ├─ 本地工具：触发停止 → 宽限期 → 必要时终止进程组 / cgroup
    └─ 同步 HTTP：中止请求 / 关闭连接 → 必要时终止专用调用进程
           ↓
业务调用退出 → finally 收尾 → 核实资源回收并记录回执

独立清理执行器扫描持久化外部清理任务
    → 使用已保存句柄调用供应商 cancel / reconcile
```

- [x] 每个执行器实例维护一个统一监控线程和本地活跃调用注册表，不为每次工具调用新建独立数据库轮询线程。
- [x] 进入本地工具、同步 HTTP、输入下载或其他可阻塞阶段前登记运行句柄；字段至少包含 task_id、step_id（如适用）、attempt_id、generation、call_id、调用类型、取消信号、停止句柄及本地许可期限。
- [x] 登记和启动使用统一包装器：登记后、实际启动前检查取消；若取消发生在进程/请求句柄创建过程中，取消信号必须保持，句柄安装后立即执行停止，不能因当时句柄尚不存在而漏掉取消。
- [x] 正常结束和异常退出都在 finally 中注销登记；停止与注销并发安全，停止操作幂等，按 call_id / attempt / generation 识别资源，避免 PID 或连接复用导致误伤。
- [x] 监控只批量查询本实例活跃任务的取消、终止、租约归属/代次及期限信息，避免每个调用单独查库或扫描全量历史任务。
- [x] 轮询周期可配置，以 0.5–1 秒作为初始评估值；明确取消发现延迟包含轮询、数据库响应和调度延迟，物理停止另有宽限期，不承诺瞬时完成。
- [x] 监控使用独立且有超时的数据库读取，不复用阻塞业务线程的连接/事务，不因业务租约失效而失去读取控制状态的能力。
- [x] 检测用户取消、任务终止、期限到达、租约失效或执行代次被替换时，触发本地停止；不要将“暂时未收到通知”视为仍有执行许可。
- [x] 监控线程仅触发有界的停止动作，不等待网络收尾、长时间进程退出或资源回收；后续升级终止和清理由独立管理机制推进，单个失败回调不能拖住其他调用。
- [x] 本地工具使用独立进程组/cgroup，先协作停止或 SIGTERM，宽限期后 SIGKILL/cgroup 清理；核实所有子进程退出后才释放对应资源。
- [x] HTTP 调用使用经验证可中断的客户端/取消句柄；不能仅设置布尔值、调用 Future.cancel()，或假定任意客户端 close() 都能打断阻塞调用。
- [x] 无法可靠中断的同步客户端放入独立调用进程，必要时终止该进程；不强杀共享执行器或模型网关，不关闭其他调用共用的连接资源。
- [x] 如果供应商连接由独立网关实例持有，该实例同样登记 call_id 与任务执行身份，并主动轮询共享取消状态；由实际持有连接的实例中断请求，无需网关间定向取消路由。
- [x] 续租成功后保守计算本地单调时钟许可期限；查库超时或断连不能无限延长许可，到期则触发停止，暂停新业务启动。取消恢复后的旧许可不能重新生效。
- [x] 保留独立于业务执行进程的本机 reaper/watchdog，处理监控线程卡死、执行器崩溃、租约过期及资源清理失败；失联时继续按第 8.9 项记录物理状态不确定性。
- [x] 异步外部等待不保留长期本地监控线程或沙箱；由数据库中的外部句柄和独立清理任务驱动供应商取消及核实。submit/poll/collect 期间实际存在的阻塞 HTTP 调用仍纳入本地监控。
- [x] 本地中断不等同于供应商计算停止；晚到任务 ID、上游终态和用量通过独立受限对账通道处理，保持第 8.9 项的未知状态和成果提交保护。
- [x] 消息或长连接通知只作为后续降低延迟的可选优化；没有消息通道、通知全部丢失时，轮询和本地许可机制仍能工作。

## 9. fixed_media 与现有能力的渐进迁移

- [x] 将 Worker 中具体媒体处理拆成独立处理器，通过注册表选择；移除未知 action 落入默认渲染分支的行为。
- [x] 将各媒体 operation 的参数模型集中到对应能力契约，保持必要的旧请求兼容。
- [x] 用显式输入映射替代多层临时素材 ID 转换，在符合隔离和完整性要求的前提下减少重复复制。
- [x] 将 `result.json` 从文件名列表升级为带输出角色的结构化结果，减少通过文件名和扩展名猜测角色。
- [x] 节点产物与 Agent 项目/会话检查点分开保存，避免每个媒体步骤复制整个工作区和会话状态。
- [x] 逐项迁移现有 operation，验证参数、输出、权限、取消、恢复和验收行为不回退。
- [x] 为旧工作流定义、旧执行结果及仍在运行的外部任务制定版本兼容与迁移方案；不以迁移为由重新提交外部任务。 （本轮不适用：按已确认范围不提供旧数据/旧协议迁移；新任务绑定的实现版本和外部ID继续按原记录恢复。）
- [x] 保持 Agent 直接调用、manual 和 workflow 对同一能力契约及业务实现的复用，同时保留各入口的授权和任务归属差异。

## 10. 新 operation 接入规范与开发流程

- [x] 提供能力接入模板和文档：定义契约 → 实现处理器 → 声明执行特性 → 注册 → 验收及测试。
- [x] 契约步骤：明确稳定名称、版本、输入角色、参数模型、默认值、输出角色及业务约束。
- [x] 实现步骤：只处理具体业务，复用框架的文件准备、沙箱管理、超时监控、结果上传和提交。
- [x] 执行特性步骤：声明实现名称/版本、生命周期、环境版本、资源、权限和分阶段恢复策略。
- [x] 注册步骤：关联 operation 与实现，并使能力目录可发现；正常新增能力不修改调度器或 Runner。
- [x] 验收步骤：优先复用已有技术和业务验收器，确实无法表达时再增加验收插件。
- [x] 外部能力额外要求：声明提交、查询、收取、取消、幂等和未知状态核实语义。
- [x] 所有实现必须声明各阶段取消能力，外部实现补充取消结果及核实映射，并通过第 8.9 项取消协议测试。
- [x] 通过框架统一包装器登记和注销阻塞调用，接入第 8.10 项本地监控，不在每个 operation 内重复实现数据库轮询和进程管理。
- [x] 测试步骤：覆盖契约错误、正常执行、输出角色、失败、超时取消、恢复及跨节点输入输出。
- [x] 仅当新能力确有现有生命周期无法表达的执行方式时，才扩展协议和通用 Runner，并补充通用恢复测试。
- [x] 以已有能力或专用测试能力验证接入模板；`media.extract_audio` 可作为说明示例，是否新增生产能力另按业务范围确定。

## 建议实施顺序

1. 确定能力契约、生命周期、文件引用、外部句柄、结果及错误模型（8）；同时设计共享数据库和文件存储接口（1、2）。
2. 建立处理器注册和契约校验，先迁移有代表性的本地及远程能力，验证接入模板（8、9、10）。
3. 完成共享数据库、文件存储及数据迁移，拆分文件准备和运行环境，完善成果提交校验（1–4），接入统一引用和结果模型。
4. 完成现有能力迁移，打通远程检查点和跨节点串行接力，完善恢复、资源调度和多实例服务（5 的串行部分、6、7、9）。
5. 增加节点级分支并行及文件下载优化（5 的并行部分及相关优化），完成全体系验收。

## 验收 TODO

- [x] A 完成一步后下线，B 无需访问 A 的本地目录即可继续。
- [x] 多个调度器竞争同一任务，只有一个有效领取成功。
- [x] A 租约过期后恢复，无法覆盖 B 提交的结果。
- [x] 上传完成但数据库提交失败时，任务不会被误标成功，孤立对象可清理。
- [x] 外部任务提交、查询和收取中断后，可以恢复且不盲目重复生成。
- [x] 节点永久失联不会导致项目永久卡住，旧节点恢复后的资源清理不会误伤后继任务。
- [x] 数据库断连、存储不可用、重复通知和滚动升级均有明确恢复行为。
- [x] 不同节点恢复的输入文件与已验收版本一致；越权、损坏或缺失文件被拒绝。
- [x] 分支并行时，各节点成果不会相互覆盖，汇合节点只读取符合依赖版本的成果。
- [x] 任意 API 实例都能展示一致的任务状态、日志、进度及可下载产物。
- [x] 普通新 operation 只需新增契约、实现/注册、运行配置和测试，无需修改调度器或通用 Runner。
- [x] Agent 能力目录、服务端参数校验和处理器参数结构来自同一份契约。
- [x] 同一 operation 的不同实现符合统一输入输出契约；运行中配置变化不改变已保存的实现或外部任务归属。
- [x] 处理器返回的输出角色、路径及元数据经过服务端验证，缺失、越界或损坏产物不会成为正式成果。
- [x] 查询、下载或验收失败不会触发未经授权的重新生成；已知结果能够复用。
- [x] 未知 operation/action、缺失处理器和不兼容契约版本得到明确拒绝，不落入默认执行分支。
- [x] 现有能力迁移前后的输入、输出、授权、取消和恢复行为有回归证据，旧任务可按兼容策略继续。
- [x] 本地、同步远程、异步远程协议均覆盖正常执行、取消、超时、失联接管和过期提交场景。
- [x] 技术成功与业务验收分离，只有已验收且版本匹配的产物才能放行下游。
- [x] 取消与领取、外部提交、结果上传、验收提交并发时，有唯一确定的业务结果，已取消节点不放行下游。
- [x] 提交期间取消、晚到外部任务 ID 和原执行器宕机均可通过独立清理/对账处理，不能重发生成或遗失可核实句柄。
- [x] 同步调用断开、取消仅获受理、排队限定取消、供应商不支持取消和核实超时分别如实展示，不误报上游停止。
- [x] 多次取消、清理任务重复领取、清理租约过期及旧回执晚到不会误杀后继尝试或重复释放资源。
- [x] 本地终止覆盖子进程、GPU 和挂载；节点失联不能被误认为物理清理完成。
- [x] 上游在业务取消后仍成功完成时，保留其真实终态和用量，不发布为本次成功交付或伪记为供应商已取消。
- [x] 无消息队列、无定向取消通知时，任意 API 实例写入取消状态后，其他机器上的执行器能通过本地批量轮询发现并停止对应调用。
- [x] 取消覆盖登记前、登记后启动前、句柄安装中和执行完成注销期间，不漏停、不误伤其他调用或后继 attempt。
- [x] 实测本地工具、阻塞同步 HTTP、下载和独立网关持有请求的取消发现/退出耗时，满足配置的轮询、查询超时及终止宽限策略。
- [x] 同一执行器的一个停止回调卡住或抛错，不影响其他调用发现取消；活跃调用大量增加时，线程数和数据库查询数不按每调用独立轮询膨胀。
- [x] 数据库断连或查询阻塞时，本地许可到期停止执行；业务进程或监控线程失效时，独立 reaper/watchdog 能继续处置。
- [x] 重复取消、请求连接复用和旧停止句柄晚到不会关闭其他任务的连接，无法中断的客户端能通过专用进程终止。

## 11. P0 可观测性专项（已实现并验收）

用户要求：可观测性一定要好。技术设计与实施步骤见 [可观测性设计](observability-design.md)，24 项独立验收见 [专项检查表](observability-checklist.md)。

- [x] O1：统一结构化日志、请求/任务/执行尝试关联、完整错误证据和脱敏，审查静默异常路径。
- [x] O2：提供按任务 ID 的只读诊断、完整时间线、用户错误编号/等待原因和受权限保护的诊断包。
- [x] O3：部署集中采集/检索、指标和看板，确定留存/容量/性能预算，确保观测故障不拖垮业务。
- [x] O4：告警、排障手册及部署脚本联动，完成真实故障演练、逐步 review/单测/功能测试和整体回归。

完成标准：只有一个任务 ID 即可定位到步骤、节点、版本、错误证据和安全后续动作；未知信息明确标注。实现、验证、部署证据齐备后才能勾选；禁止以“有日志文件”代替可排障验收。

## 12. Redis Streams 中转流式输出（代码已实现，部署与测试待完成）

记录日期：2026-11-01。用户已认可方案；本轮已完成代码改造，暂不部署和测试。

目标：降低回复全文频繁更新、SSE 轮询及前端重复刷新带来的数据库压力，同时改善文本增量展示延迟。

方案：每轮对话任务按 `turn_id` 建立独立 Stream，例如 `agent:events:{turn_id}`，不按用户共用一个 Stream。Worker 写入回复增量和进度，持有浏览器 SSE 连接的 API 实例读取并转发；调度器和 Worker 无需知道 SSE 所在机器，浏览器可连接任意 API 实例。

- [x] Worker 将文本增量、进度和结束通知写入共享 Redis Streams；定义事件类型、消息 ID、执行 attempt/generation 和去重顺序，避免重试或旧执行器污染当前回复。
- [x] SSE 使用普通 `XREAD` 独立读取，支持多个页面订阅同一任务；不使用会分摊事件的共享消费组。订阅前校验项目及任务归属。
- [x] 将 Stream ID 映射为 SSE 事件 ID，支持 `Last-Event-ID` 跨 API 实例断线补读，无需会话粘滞。
- [x] 数据库改为低频回复快照（初步考虑每 5～10 秒，可配置）及结束时立即保存；任务状态、租约、取消和最终结果仍以数据库为准。
- [x] 快照保存对应的事件游标，定义快照与增量衔接及幂等处理；Stream 已裁剪或过期时，通过快照恢复，避免重复拼接或静默漏字。
- [x] 任务结束后设置可配置 TTL，保留断线补读窗口；设置 Stream 容量及裁剪策略，覆盖长任务、异常退出和无人订阅时的清理。
- [x] SSE 正常连接时降低前端重复轮询；明确 Redis 不可用、执行器崩溃及终态通知丢失时的降级、重同步与最终落盘策略。
- [ ] 验证跨机器 Worker/API、跨 API 重连、同任务多订阅、重复事件、旧 attempt、事件过期和 Redis 故障；压测对比数据库读写量、首字/增量延迟和 Redis 容量。

涉及入口：`webapp/agent_host.py`、`webapp/agent_store.py`、`webapp/agent_api.py`、`webapp/static/chat.js`，以及 Redis 配置和部署。

## 13. OpenSandbox/gVisor 混合执行后端（设计已对齐，尚未实现）

记录日期：2026-11-05。

状态：方案边界已完成讨论和静态探查，尚未修改本系统代码、数据库和部署。当前 Agent 与本地任务仍使用既有 `bubblewrap + 独立 UID + cgroup + seccomp + node watchdog`；本机独立安装的 OpenSandbox/gVisor 仅用于基础设施验证，不代表本系统已接入。以下复用既有租约、授权、检查点、外部调用、用量、验收和清理机制，不建立第二套业务状态机。

### 13.1 目标、非目标与路由原则

目标：把运行环境抽象成版本化 Backend，在不改变 Workflow、Run、Task、Control Job、候选验收及 Orchestrator 唯一提交语义的前提下，提高 CPU Agent 和动态脚本的宿主内核隔离强度，并保留现有 GPU 执行能力。

```text
Task / Control Job
    -> ExecutionRequirements
    -> Scheduler + Resource Claim
        -> OpenSandboxBackend：CPU Agent、CPU Script、普通受控工具
        -> LocalBwrapBackend：GPU Operation、GPU Script、暂未迁移的宿主能力
```

固定原则：

- [ ] Agent 默认进入 OpenSandbox + gVisor；GPU 能力优先拆成类型化 Operation，由 Agent 经 Node Capability Gateway 调用。
- [ ] SAM/ZIM、CUDA/PyTorch、NVENC/NVDEC 等 GPU Operation 暂时继续使用现有 bwrap、GPU lease 和指定 GPU UUID。
- [ ] 动态代码不自动等于 Direct GPU Agent。优先由 CPU Agent 生成有界脚本，冻结脚本摘要后交给 GPU Script Executor。
- [ ] 只有无法用已注册 Operation、Operation 组合、有界 GPU Script 或长驻 GPU Session Operation 表达，并通过受信任 Profile 审查的任务，才允许 Direct GPU Agent。
- [ ] Backend、runtime profile、镜像摘要和 Skill Bundle 在 Attempt 创建时冻结；重试、恢复或容量不足时不得静默切换到权限更高的 Backend。
- [ ] OpenSandbox 容量不足进入 `resource_wait`；不能自动回退 bwrap。GPU 无容量同样等待，不能降级 CPU 或选择未授权设备。
- [ ] 沙箱 Backend 只负责运行空间，不接管业务状态、授权、预算、外部调用语义、验收或成果发布。

首期非目标：gVisor GPU、任意公网访问、跨 Backend 热迁移、自动权限降级、替换现有 Artifact/Checkpoint 系统、替换 Orchestrator，以及把 Node Gateway 变成通用 HTTP 代理。

### 13.2 多机部署拓扑与服务职责

```text
中央控制面
├── PostgreSQL：权威租约、Attempt、预算、Provider Call、Usage 和 Cleanup
├── Scheduler：Backend、节点和资源匹配
├── Orchestrator：业务状态唯一提交、节点失联 Fencing 和全局 Reconcile
└── Redis / 中央观测后端：实时事件与观测，不承担正确性

每台物理执行机
├── Executor：公共执行协调
├── Watchdog：本机异常清理
├── Node Capability Gateway：新增业务服务
├── OpenSandbox Server -> Docker -> runsc/gVisor
└── Observer Agent：日志和指标上报
```

- [ ] 每台物理执行机部署一个 Node Capability Gateway，不按沙箱部署，也不经过公网 Nginx。
- [ ] 每台 OpenSandbox Docker 执行机运行本机 Lifecycle Server；Docker Backend 不通过远程 Docker Socket 跨机控制。
- [ ] 现有 Watchdog 增加 OpenSandbox 清理驱动，不新增独立 Sandbox Reaper 服务。
- [ ] 现有 Orchestrator 增加节点心跳 Fencing、未结算调用和超时 Cleanup 的全局扫描，不另建中央 Sweeper 服务。
- [ ] Usage outbox 由 Node Gateway 本机重放；全局未结算检查进入 Orchestrator Reconcile，规模达到独立扩容要求后再拆服务。
- [ ] Console 只需中央部署一份，不随每台执行机部署；它不是执行正确性依赖。
- [ ] 当前单机 journal Observer 不能冒充多机观测。跨物理机时每机采集并上送中央日志/指标后端。

职责边界：

| 组件 | 负责 | 不负责 |
| --- | --- | --- |
| Scheduler | Backend、节点、资源和 Profile 匹配 | 启动 Harness、提交正式业务状态 |
| Executor | Attempt 执行编排、文件准备、结果收集 | Workflow 正式状态提交 |
| SandboxBackend | 创建、运行、命令、文件、销毁和 Reconcile | Provider 凭据、业务预算和验收 |
| Node Capability Gateway | 沙箱身份、模型/工具出口、预算准入、Usage 结算、连接取消 | 创建沙箱、修改 Workflow 正式状态 |
| Watchdog | 撤权、物理销毁、Cleanup Receipt | 业务重试和新 Attempt 创建 |
| Orchestrator | Fencing、权威状态和最终提交 | 直接操作节点 Docker |
| Observer | 日志、指标、告警和诊断 | Usage 与计费事实来源 |

### 13.3 公共执行抽象与数据库绑定

目标接口保持最小，具体 Backend 不向上泄露 Docker、bwrap 或 runsc 细节：

```text
SandboxBackend
  capacity
  prepare
  start
  execute
  cancel
  collect_outputs
  checkpoint
  cleanup
  reconcile
```

`ExecutionRequirements` 至少包含 CPU、内存、GPU 数量与 capability、runtime profile、workspace policy、network policy 和所需 Gateway capability。要求来自注册实现和冻结 Task，不信任 Agent 自报。

- [ ] 先将现有 `TaskRuntime`、UID/cgroup、bwrap、GPU lease 和 reaper 封装为 `LocalBwrapBackend`，第一阶段不得改变行为。
- [ ] 新增 `OpenSandboxBackend`，只通过 Lifecycle API、Files/Command API 和 server proxy 操作沙箱。
- [ ] `execution_attempts` 增加 `backend`、`runtime_profile`、`backend_handle`、`runtime_image_digest`、`skill_bundle_digest`、`provisioning_state`、`provisioning_request_id` 和 `node_epoch`。
- [ ] `execution_resource_claims` 增加 `backend`、`runtime_profile` 和可选 `gpu_uuid`；资源释放仍以 Cleanup 完成而非业务终态为准。
- [ ] OpenSandbox metadata 写入 `attempt_id`、`task_id`、`generation`、`backend` 和 runtime profile，用于创建结果未知后的唯一找回与孤儿清理。
- [ ] 创建响应超时时不得直接重建。按 Attempt metadata 查找：唯一匹配则恢复句柄；无匹配且确认未创建才重试；多个匹配全部隔离并告警。

建议的 Backend 生命周期：

```text
requested -> provisioning -> ready -> running -> stopping -> cleaning -> complete
                                |                       |          -> quarantined
                                -> failed               -> retrying
```

### 13.4 CPU Agent Runtime OCI 镜像

OpenSandbox基础镜像不包含 Codex、Claude或DeepSeek Harness，不能直接调度 Agent。必须通过受控构建产出版本化 OCI 镜像；运行时禁止联网安装 npm/pip/apt 依赖。

首个链路验证可使用统一 CPU 镜像，正式环境按 Harness 拆分：

```text
agent-runtime-codex@sha256:...
agent-runtime-claude@sha256:...
agent-runtime-deepseek@sha256:...
```

镜像包含：

- [ ] Agent Runtime Daemon、`sandbox_entry`、Harness Bridge、统一事件适配和 Gateway Client。
- [ ] 固定版本 Codex、Claude Agent SDK或DeepSeek Harness及其二进制摘要。
- [ ] Python、Node、FFmpeg/ffprobe及受控系统工具；依赖和本地动态库由构建清单固定。
- [ ] 版本化 Skill Bundle：`runtime/public/SKILL.md`、公开 references、确定性 helpers、capabilities和Tool Schema。
- [ ] Node Gateway内部 CA 证书、Gateway协议Schema及Runtime Daemon协议Schema。
- [ ] SBOM、镜像签名、构建来源和漏洞扫描结果。

镜像禁止包含：Provider API key、PostgreSQL/Redis/BOSS长期凭据、用户素材、Checkpoint、Gateway临时Token、Docker Socket、宿主机私有目录和GPU设备。

运行时注入：`/workspace`、`/state`、本次 Attempt配置和短期 Gateway Token。镜像默认只启动无业务副作用的 init；完成恢复、Token写入和Daemon健康检查后，Executor才允许启动Harness Turn。

`runtime_image.py` 现有工具、Harness、库和执行契约指纹应扩展为OCI构建输入，而不是在OpenSandbox中继续挂载整棵宿主目录。Runtime Profile冻结镜像digest、Harness版本、Daemon协议、Skill digest、Tool Schema digest、Gateway协议和Checkpoint格式。

Skill只描述如何使用能力，不授予权限。不得打包整机 `~/.codex/skills` 或 `~/.updream_skills`；内部平台、数据库、运维和带宿主认证的Skill通过可信控制面或类型化Gateway能力提供。Skill更新产生新Bundle和新Profile，运行中Attempt不热更新。

### 13.5 Agent Runtime Daemon与三条数据通道

Agent Runtime Daemon运行在每个Agent沙箱内，负责管理Harness进程和双向协议：初始化、打开/恢复Session、运行Turn、Tool响应、Cancel、事件流、退出状态及Session导出。它不持有数据库或Provider凭据，不提交正式状态。

```text
控制与Agent事件：Executor <-> OpenSandbox server proxy <-> Runtime Daemon <-> Harness
模型和受控工具：Harness -> Gateway Client -> Node Capability Gateway -> Provider/工具
文件和成果：Artifact Store <-> Executor/OpenSandbox Files API <-> Workspace
```

- [ ] Runtime Daemon使用双向WebSocket或等价的有界双向协议；普通 `commands.run` 日志流不足以承载长期Harness stdin/stdout RPC。
- [ ] Runtime Daemon输出统一 `message.delta`、`tool.call`、`tool.result`、`turn.completed`、错误和诊断事件；Executor继续经现有事件/Redis/API SSE送达浏览器。
- [ ] Runtime Daemon与Executor的控制Token、Node Gateway Capability Token分离，互不替代。
- [ ] Cancel先通过控制通道终止Harness进程组；Daemon无响应时Executor请求删除沙箱，Watchdog最终确认物理销毁。
- [ ] Runtime Daemon不直接连接浏览器；Node Gateway不承载Workspace、浏览器SSE或OpenSandbox生命周期流量。

### 13.6 Node Capability Gateway与内部网络

Node Capability Gateway运行在沙箱宿主机、gVisor沙箱之外。它是现有Unix Gateway业务校验的网络化Transport，不是OpenSandbox Ingress Gateway，也不是通用代理。

每台执行机预建专用Docker internal bridge，例如：

```text
network: opensandbox-agent
subnet:  172.30.0.0/24（部署前必须检查与节点路由不冲突）
host:    172.30.0.1
gateway: 172.30.0.1:18880
```

OpenSandbox使用自定义`network_mode`，动态端口只发布到`127.0.0.1`。网络还必须关闭容器间通信，并通过节点防火墙只允许沙箱访问Node Gateway端口，拒绝宿主其他端口、其他沙箱、公司内网、公网和云metadata。`--internal`本身不替代这些规则。

- [ ] Gateway仅监听内部bridge地址，不监听`0.0.0.0`，不经公网Nginx暴露。
- [ ] 沙箱使用内部TLS校验Gateway证书；不允许`-k`、任意URL、环境代理或netrc。
- [ ] Gateway路由固定为版本化模型、上下文、Workflow和工具接口；上游URL由可信配置构造，用户和Agent不能指定。
- [ ] 现有`agent_gateway.py`业务核心提取为共享`GatewayCore`，保留Unix Transport供bwrap，新增HTTPS Transport供OpenSandbox，避免两份授权和计量实现漂移。
- [ ] Gateway进程使用低权限账号，不持有Docker Socket和任意宿主命令权限；数据库角色只开放租约、授权、Provider调用和Usage所需接口。

每个Attempt生成短期随机Capability Token。数据库只保存Token哈希，Grant绑定Attempt、Generation、Sandbox ID、来源IP、允许路由、过期时间、调用/字节预算。实际授权取Runtime Profile需求、节点Gateway支持能力与Task冻结授权的交集。

每次调用依次检查Token、来源、Sandbox ID、Generation、Lease、路由Schema和预算。Grant撤销时拒绝新请求并主动关闭该Grant全部活跃Provider Socket；仅把数据库字段标为revoked不足以停止已建立的流。

### 13.7 Provider调用、用户用量与多机账本

多机部署中PostgreSQL是唯一权威账本。Gateway内存、Redis、Observer和本地outbox均不能作为用户消耗事实来源。

```text
调用前：写Provider Call Intent -> 原子预留estimated input + max output -> 发起请求
调用后：保存原始Usage Receipt -> 幂等结算actual usage -> 释放未使用预留
```

- [ ] 复用现有`provider_calls`、`task_usage`和`usage_protocol`，记录user/run/task/attempt/generation/gateway/model/provider关联。
- [ ] 多Gateway并发预算通过PostgreSQL短事务与唯一约束控制，不能依赖进程锁或本地计数。
- [ ] Streaming期间Gateway本地累计，最终按Provider Receipt结算；不按每个delta写账本。
- [ ] 非空输出却`output_tokens=0`沿用冲突回执处理，不能记作零成本；可信input可单独结算，output保持unknown和阶段上限。
- [ ] 请求字节可能已发出但未取得可靠回执时进入`submission_unknown`，不能自动重提模型或生成请求。
- [ ] 数据库不可用时拒绝新Provider副作用；已建立流可有界完成，Receipt写入本机持久outbox，恢复后按`provider_call_id`幂等补交。未结算前不能宣称费用已确认。
- [ ] Observer采集Gateway事件并构建视图，但不参与预算正确性。跨机必须每机采集并上送中央后端。

### 13.8 OpenSandbox恢复与检查点

权威恢复采用“新建干净沙箱 + 逻辑检查点恢复”，不恢复原容器PID、内存、Socket或正在运行的进程，也不把Docker/gVisor快照当作权威数据源。

```text
固定Runtime镜像
+ Artifact Store中的Workspace Checkpoint
+ Session Bundle
+ 当前Revision的Step Checkpoint
+ PostgreSQL权威上下文
+ 新Gateway Grant
= 新OpenSandbox执行环境
```

恢复顺序：创建新Attempt/Generation，按固定image digest创建空闲沙箱，读取最后一个已提交Checkpoint Manifest，从对象存储恢复并校验project/revision/size/SHA-256，设置UID和权限，重新生成只读业务上下文，签发绑定新Sandbox ID的Gateway Grant，完成恢复前检查后再启动Harness。

- [ ] 只认数据库已提交的Checkpoint指针；对象已上传但引用未提交属于孤儿，不能参与恢复。
- [ ] 节点本地目录和镜像缓存只是缓存，跨机恢复不得依赖原节点。
- [ ] Session恢复要求Harness、runtime image、Skill Bundle、Gateway协议和Checkpoint格式兼容；不兼容时恢复业务文件和步骤成果，但创建新原生会话。
- [ ] 动态Workflow仅恢复当前Task revision、声明依赖、已验收上游输出和授权修复候选；旧revision与未验收文件不能进入新沙箱。
- [ ] OpenSandbox Docker snapshot主要保存可写文件层，不是进程/内存快照；gVisor overlay/filestore还存在完整性限制，且本地镜像不能天然跨机。它未来最多作为可丢弃缓存优化，不替代现有Artifact/Checkpoint协议。
- [ ] Executor或进程崩溃默认创建新Attempt和新沙箱；MVP不接管仍在运行的Harness进程。

### 13.9 Watchdog、Fencing和异常状态

正常路径由Executor主动清理；异常路径由独立Watchdog权威清理；OpenSandbox TTL只在Executor和Watchdog同时失效时兜底。

```text
Lease/permit失效
  -> 标记cleanup_requested
  -> 撤销Gateway Grant并关闭活跃连接
  -> 中断Runtime Daemon/Harness
  -> DELETE OpenSandbox sandbox
  -> GET/list+metadata确认不存在
  -> 清理本地句柄和临时数据
  -> Cleanup Receipt complete
  -> 释放Resource Claim
```

- [ ] 未确认沙箱消失前不释放物理资源，不把业务取消或数据库Lease过期当作清理完成。
- [ ] 删除失败进入有界退避；超过Cleanup deadline后对应Backend/节点进入draining或quarantined。
- [ ] Watchdog删除前核对Sandbox metadata中的Attempt和Generation，旧Cleanup回执不能误删后继沙箱。
- [ ] 节点心跳超时后中央Orchestrator fence节点、递增Generation、撤销全部Grant并停止调度；不能由其他节点因“本地找不到容器”宣称旧节点已清理。
- [ ] 旧节点恢复后，本机Watchdog扫描OpenSandbox metadata，删除全部失权沙箱；清理完成后节点才能重新ready。
- [ ] 数据库故障时新副作用fail closed；本地单调Permit仍会到期并触发清理。纯本地结果可暂存但不能正式提交。

异常必须结构化区分：创建明确失败、创建结果未知、Provider提交未知、Usage未结算、输出收集失败、Checkpoint损坏、删除重试、Cleanup隔离、节点失联和旧Generation迟到。安全自动重试仅限查询、同一Sandbox ID删除、幂等结算、条件对象写及可证明请求字节未发送的连接失败。

### 13.10 GPU边界和后续条件

本机静态探查表明RTX 4090架构可被gVisor nvproxy覆盖，但当前宿主驱动、NVIDIA Container Toolkit、CDI、精确GPU UUID分配和OpenSandbox GPU租约均不满足生产迁移要求。首期不修改驱动、不停止现有GPU任务，也不以旧runsc或强制ABI冒充作为生产方案。

- [ ] GPU Operation继续使用现有bwrap、指定`/dev/nvidiaN`、GPU UUID和GPU lease。
- [ ] OpenSandbox API当前只表达GPU数量，未来若迁移GPU需增加设备ID/UUID、能力、节点租约和并发冲突控制。
- [ ] Direct GPU Agent仅允许注册Profile；判定前必须证明无法用类型化Operation、组合Operation、有界Script或长驻GPU Session表达。
- [ ] gVisor GPU迁移需单独完成驱动严格兼容、Toolkit/CDI、`nvproxy`、compute/video capability、CUDA/PyTorch/SAM/ZIM/NVENC/NVDEC及并发回收验收。

### 13.11 实施顺序与验收门禁

实施顺序：

1. 提取`LocalBwrapBackend`及公共Backend contract，保持线上行为不变。
2. 增加数据库字段、生命周期状态和Backend contract tests；先不部署OpenSandbox路径。
3. 建设内部网络、Node Capability Gateway、Grant和共享Usage账本对接。
4. 构建并签名首个CPU Agent Runtime镜像及Runtime Daemon。
5. 实现OpenSandboxBackend的create/files/daemon/collect/delete/reconcile。
6. 先迁移无GPU确定性Script，再迁移Reviewer/Diagnostician，最后迁移需要长期Harness会话的CPU Implementer。
7. 扩展多机Observer、节点Fencing和Watchdog故障演练；GPU保持原Backend。

验收清单：

- [ ] 同一Task在两种Backend上的授权、输入、输出、验收和Cleanup contract一致。
- [ ] 沙箱只能访问Node Gateway；公网、内网、metadata、宿主其他端口和同网沙箱均失败。
- [ ] Token复制到其他沙箱、旧Generation、过期Lease和未授权Route均被拒绝。
- [ ] Runtime镜像、Harness、Skill、Tool Schema和Gateway协议摘要写入Attempt并可审计。
- [ ] 创建响应丢失时找回唯一沙箱，不重复创建；Executor kill后Watchdog销毁并确认无残留。
- [ ] Node Gateway kill、数据库断连、节点重启和删除失败均有有界恢复，不能丢Usage或提前释放资源。
- [ ] 多Gateway并发预算不超限；重复Receipt不重复累计；未知Usage不记零。
- [ ] 跨物理机从共享Checkpoint恢复，不读取原节点目录；损坏、越权或不兼容Session明确拒绝或降级新会话。
- [ ] Provider提交未知、旧Attempt晚到和节点恢复不会产生第二次外部副作用或覆盖新成果。
- [ ] OpenSandbox达到容量上限时进入资源等待，不回退bwrap；GPU等待不改用未授权设备。
- [ ] 为现有119条真机矩阵补充Backend、Gateway、恢复、清理和故障证据；历史通过不代替新路径验收。

### 13.12 沙箱启动速度优化

目标不是只压缩Docker/runsc创建时间，而是分别控制调度、镜像、沙箱、Runtime、Workspace、Session和Harness恢复。优化前必须记录每段延迟；不能用单一“create latency”掩盖大文件恢复或Harness初始化。

```text
Agent Ready时间
= scheduler_wait
+ image_prepare
+ sandbox_create/acquire
+ daemon_ready
+ workspace_restore
+ session_restore
+ harness_resume
```

#### 13.12.1 不可变Runtime镜像与节点预拉取

- [ ] 按Harness构建`agent-runtime-codex`、`agent-runtime-claude`和`agent-runtime-deepseek`，使用digest调度，不使用浮动tag。
- [ ] 把Harness、Runtime Daemon、Skill Bundle、Tool Schema、Python/Node/FFmpeg和固定依赖移到构建期；运行时不执行apt/pip/npm安装。
- [ ] Runtime Profile发布后，由节点后台预拉取active镜像并校验签名、digest和架构。
- [ ] 节点上报`cached`、`pulling`、`pullable`和`unavailable`，Scheduler优先缓存命中节点，但缓存不能改变Backend、授权或冻结Profile。
- [ ] 优化镜像层次：变化频率低的系统/Harness层在下，Skill和Runtime Daemon独立分层，减少小版本升级的重复拉取。

#### 13.12.2 干净Warm Pool

每个Runtime Profile维护少量从未装载用户数据的预热沙箱。预热完成条件是gVisor已启动、Runtime Daemon健康、Harness二进制和公共依赖可用；不提前创建真实Agent Session或Gateway Grant。

- [ ] 定义每Profile的`min_idle`、`target_idle`、`max_idle`、并发补池和缩容窗口，容量计入节点及OpenSandbox总上限。
- [ ] 领取必须原子地把干净沙箱绑定Attempt；领取失败不能让两个Attempt共享同一沙箱。
- [ ] 任务完成后的用户沙箱始终销毁，不清洗后回池；Pool只补充新建的干净沙箱。
- [ ] Warm Pool禁止包含Workspace、Session、素材、Attempt配置、Gateway Token、Provider连接和用户缓存。
- [ ] Pool补充在任务清理之外异步执行，补池失败影响后续冷启动但不能把已完成Cleanup改回失败。
- [ ] 记录pool命中、冷启动、补池失败、空闲成本和不同Profile的实际需求，再决定常驻数量；不能为每个低频Profile无界预热。

#### 13.12.3 每物理节点Artifact Cache

每台物理执行机维护一份可信、可删除、容量有界的Artifact缓存；同一物理机上的逻辑执行节点共享该缓存，跨物理机不要求同步。

```text
PostgreSQL Manifest + Artifact Store = 权威状态
Node Artifact Cache                 = 可丢弃副本
Sandbox Workspace                   = 单Attempt临时副本
```

- [ ] 缓存按Artifact ID及密文SHA-256寻址，优先保存对象存储密文；Executor在Attempt私有staging中校验、解密和物化明文。
- [ ] 缓存目录不挂载给沙箱，不保存Gateway Token、Provider凭据、Session Cookie、可写Workspace或进程状态。
- [ ] 下载使用临时文件、大小/hash/认证校验和原子rename；同一对象并发下载使用文件锁或single-flight去重。
- [ ] 配置容量上限、LRU/访问时间淘汰、损坏隔离、磁盘水位和清理指标；缓存完全删除只降低性能，不影响恢复正确性。
- [ ] Executor按Checkpoint Manifest从缓存命中或对象存储下载，再把本次授权文件写入OpenSandbox；沙箱不能列举节点缓存或其他项目对象。
- [ ] 节点可上报受限的cache hint供调度评分，不向普通任务暴露完整用户Artifact清单。

#### 13.12.4 增量Checkpoint与恢复

Checkpoint保存当前完整逻辑Manifest，但保存时只上传新增/修改对象，未变化文件复用旧Artifact；授权删除从新Manifest移除或记录墓碑。恢复不顺序重放任意长度的delta链，而是以一个已提交完整Manifest确定当前文件集合。

- [ ] 保存阶段比较路径、大小和SHA-256，只上传变化对象；复用对象必须满足同项目归属、published状态和有效引用。
- [ ] 恢复阶段只下载当前节点缓存缺失的Artifact；全新节点无缓存时仍可从Artifact Store完整恢复。
- [ ] 使用Attempt私有临时目录恢复，全部文件完成project/revision/size/hash、路径、链接和数量校验后再原子发布Workspace。
- [ ] 删除墓碑、blocked deletion和Workspace Policy保持现有授权语义，不能因增量算法把未授权删除推广到后续版本。
- [ ] Session Bundle和Step Checkpoint分别版本化，不与普通Workspace文件混成一个不可审计大包。
- [ ] 对每个Checkpoint记录逻辑总大小、变化字节、复用字节、远端下载字节和本地缓存命中率。

#### 13.12.5 按角色最小恢复

恢复集合由Task、Role、Revision和授权确定，不默认把整个项目复制进每个Agent沙箱：

| 角色/任务 | 默认恢复范围 |
| --- | --- |
| Manager | PlanDraft、能力目录、上下文、元数据和必要低清证据 |
| Reviewer | 当前候选、预览、验收条件、上游已验收摘要 |
| Diagnostician | 相关日志、诊断、失败输入摘要和受控证据 |
| Implementer | 授权脚本、Workspace、必要素材和依赖Step成果 |
| GPU Operation | 明确输入文件和参数，不恢复Agent Session |

- [ ] 大视频和模型资产不因普通Agent Turn自动恢复；优先由类型化Operation读取，Agent使用元数据、预览或受控查询。
- [ ] Role恢复策略属于版本化Runtime/Task contract，不能由Agent临时扩大。
- [ ] 缺少声明输入时明确失败，不能通过扫描项目目录猜测最新文件。

#### 13.12.6 Prefetch与并行恢复

- [ ] Scheduler确定候选节点后可预拉取固定镜像和不可变Artifact，但正式获得Lease前不得启动Harness、签发可用Grant或产生Provider副作用。
- [ ] Workspace、Session Bundle、Step Checkpoint和授权素材可并行预取；每项独立限流、取消和记录错误。
- [ ] Lease获得后并行执行文件物化、Attempt配置生成和Grant准备，最终统一校验后进入Ready。
- [ ] Prefetch结果绑定Artifact digest而非Attempt执行权；候选变化时可保留为普通节点缓存，不能据此提交业务状态。
- [ ] 限制并行恢复的磁盘、网络和CPU峰值，防止批量冷启动拖垮Gateway、对象存储或控制面。

#### 13.12.7 Session体积与短期保活边界

- [ ] Session Bundle只保留不可重建的Harness状态、线程标识和已批准项目记忆；排除下载缓存、日志、锁、临时文件和可重建索引。
- [ ] 恢复前校验Harness、镜像、Skill、Daemon协议和Checkpoint格式；不兼容时恢复业务文件并创建新Session。
- [ ] 默认Attempt完成后销毁沙箱。未来可实验同用户、同Session、同Profile、同Generation的短期保活窗口，但需单独授权、容量、撤权和清理设计。
- [ ] 短期保活不能成为恢复正确性依赖，不能跨用户复用完成过任务的沙箱，也不能让旧Token和权限跨Attempt存活。

#### 13.12.8 Lazy Pull与gVisor原生快照的后续阶段

- [ ] 在镜像、Warm Pool和Artifact恢复稳定后，再评估eStargz/Nydus/OverlayBD等Lazy Pull；记录首个文件访问尾延迟和Registry依赖，不能只比较pull完成时间。
- [ ] gVisor原生Checkpoint/Restore仅用于公共Runtime初始化加速的后续实验，不替代逻辑Checkpoint。快照点必须在无用户数据、无Token、无Provider连接时建立。
- [ ] Restore Hook必须重置随机/唯一状态、网络连接、Attempt配置和Gateway Grant；不能复制预热实例的身份。
- [ ] 验证CPU特征、runsc版本、镜像digest、Skill、Daemon协议和节点文件系统兼容；不兼容时回退干净Warm Pool而不是强制恢复。
- [ ] OpenSandbox Docker filesystem snapshot不作为进程/内存恢复或跨机权威状态；任何快照缓存均可删除和重建。

#### 13.12.9 指标、基线和完成条件

必须分别记录：

```text
scheduler_wait_ms
image_pull_ms
warm_pool_wait_ms
sandbox_create_ms / sandbox_acquire_ms
workspace_restore_ms
session_restore_ms
daemon_ready_ms
harness_resume_ms
agent_first_event_ms
artifact_cache_hit_ratio
warm_pool_hit_ratio
remote_restore_bytes
```

- [ ] 基线覆盖冷节点、镜像缓存命中、Warm Pool命中、Artifact缓存命中/未命中及跨物理机恢复；报告并发、数据规模、Profile和p50/p95/p99。
- [ ] Ready边界固定为Runtime Daemon健康、Workspace/Session校验完成、Grant生效且Harness可接收Turn；不能使用较早的容器Running冒充Agent Ready。
- [ ] 优化不得跳过权限、hash、Session兼容、Lease、Generation和Cleanup检查；性能失败可回退冷启动，不能回退高权限Backend。
- [ ] Warm Pool、节点缓存和Prefetch均有独立容量及故障指标，不得让性能组件成为新的权威状态或业务单点。

启动优化实施优先级：先完成Harness专用镜像、节点预拉取、干净Warm Pool、Node Artifact Cache、增量Checkpoint、角色最小恢复及并行Prefetch；随后优化镜像层和Lazy Pull；最后才评估gVisor内存Checkpoint、后台恢复和短期Session保活。

完成标准：CPU Agent在至少两台物理执行机上通过固定镜像和Skill运行；任一节点、Executor、Gateway或OpenSandbox进程故障后，旧Generation失权、沙箱可回收、Usage可核实、Checkpoint可在另一节点恢复；GPU链路保持现有行为且无权限回退。启动优化还需证明缓存全失效时仍正确恢复，命中时达到单独批准的Agent Ready延迟目标。所有实现、部署和真机证据齐备前，本节保持未完成状态。

## 本会话的固定版本验收

共享工作区存在并行修改。本会话已对源码指纹 `695955c0ed2cdb84cfb73b73c6043becce5884c79971d9c255035a5d393685d0` 完成独立副本验收：336项Python测试、12项前端测试、浏览器回归及真实加密BOSS恢复全部通过，无失败或跳过，9个远端测试对象已清理。源码和证据已封存，参见[固定版本最终验收](distributed-final-verification.md)。该结论不自动适用于之后的工作区修改；真实跨物理机生产演练不在此记录范围内。
