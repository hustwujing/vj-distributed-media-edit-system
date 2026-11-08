# API 并发与耗时隔离开发说明

适用于新增或修改 HTTP 路由、中间件、依赖注入、SSE、文件上传/下载、媒体检查，以及 API 内的数据库、存储、诊断和控制分类调用。目标是依赖变慢或拥塞时，事件循环、无关请求和显式取消仍能推进。业务任务的调度、租约和沙盒仍按[分布式执行开发说明](change-distributed-execution.md)处理；供应商协议按[外部服务专项](change-external-providers.md)处理。

## 1. 先定位整个请求链路

| 入口 | 阅读目的 |
| --- | --- |
| [api_execution.py](../../webapp/api_execution.py) | 有界执行器、异步准入、路由包装、期限、取消及断连 |
| [app.py](../../webapp/app.py)、[agent_api.py](../../webapp/agent_api.py)、[image_api.py](../../webapp/image_api.py) | 路由注册、会话认证、错误响应与分阶段编排 |
| [api_staging.py](../../webapp/api_staging.py)、[api_maintenance.py](../../webapp/api_maintenance.py) | 私有暂存、提交与清理、后台容量采样 |
| [agent_stream.py](../../webapp/agent_stream.py) | 原生异步 Redis 读取、连接关闭和故障退避 |
| [media_service.py](../../webapp/media_service.py)、[media_isolation.py](../../webapp/media_isolation.py) | 媒体服务调用、本地中断、FD 所有权及实际解码容量 |
| [api_capacity.py](../../webapp/api_capacity.py) | 容量指标字段、固定池名与完整快照校验 |

审查范围包含中间件、参数解析、依赖、端点、响应生成与清理；端点使用专用池不代表前后所有工作都自动隔离。先列明每段工作使用的线程/协程、锁、事务、连接、文件句柄、准入和期限，再选择接入方式。

## 2. 选择异步等待或专用执行池

- 有原生异步接口的长连接和网络等待使用异步接口；当前 SSE 通过 `AsyncStreamReader` 使用 `redis.asyncio`。不能用 `async def` 包住同步 SDK，也不能用 `asyncio.to_thread` 或默认 executor 将长等待转移到公共线程池。
- 同步 SDK、数据库和文件处理复用 `app.state.api_execution.run(pool, function, ...)`。不要每请求新建线程池、裸线程或无界后台任务，也不能直接向 `ThreadPoolExecutor` 提交来绕过有界队列。
- 当前应用在注册路由前安装 `route_class(api_execution)`：同步端点默认 GET/HEAD/OPTIONS 进 `read`，其他方法进 `write`；特殊端点用 `@lane(...)` 指定池，装饰器放在路由装饰器下方。新增 Router 或子应用必须核对实际路由类，不能假定自动继承。
- 异步端点由自身编排各阶段；`@lane(...)` 不会自动转移异步函数内部的同步调用。同步端点默认池也不能代替按依赖拆分耗时链路。
- CPU 密集操作不能在事件循环运行。线程池提供容量隔离，不保证纯 Python CPU 计算的并行加速；大量计算应使用既有媒体服务、受控进程或执行器。API 的媒体池用于隔离等待及相关处理，不能绕过 FD-only 解码协议或扩大实际解码并发。

| 池 | 工作边界 |
| --- | --- |
| `read` / `write` | 普通元数据读取 / 短写入与提交；不混入长存储、解码或分类等待 |
| `critical` | 显式任务控制及其目标核实，保持独立容量 |
| `auth` / `health` / `stream_read` | 会话数据库查询 / 依赖健康检查 / SSE 数据库快照；基础 `/api/health` 保持短异步路径 |
| `storage` | 文件复制、哈希、加密、对象上传与恢复等存储处理 |
| `media` | 媒体检查、规范化和缩略图链路 |
| `diagnostics` / `control` | Observer 等诊断 / 自然语言控制分类；显式取消不等待分类服务 |
| `cleanup` | 暂存与本地目录回收，避免阻塞普通读写 |

具体默认容量、配置名和合法范围统一见[API 依赖容量与期限](../distributed-execution-configuration.md#api-依赖容量与期限)。容量按 API 实例计算，多副本还要核对共享数据库、存储和媒体服务的总负载。独立线程池不表示同一数据库连接或事务可以并行复用。

## 3. 保留有界准入、期限与真实取消语义

- 执行器在提交前限制活动数和排队数。大请求使用现有 `@admission('upload'/'image_import'/'demo'/'delete')` 对应机制，在读取/解析请求体前准入；SSE 在响应开始前取得 `streams` 名额，结束时释放。新增准入类别需同步配置、指标白名单和测试。
- 排队期限、单次执行期限、整请求期限及底层连接/读取期限分别核实。多阶段请求不能靠每阶段重置超时无限延长；底层函数自身抛出的 `TimeoutError` 不能冒充执行器期限到达。
- 排队取消后工作不得启动；已经运行的同步调用直到实际结束才释放槽位。调用方超时、断连和取消只表示停止等待，不代表线程、远端请求或解码已结束。
- 分块处理和发布前复用 `check_cancelled()` / `Staging.check()`；可本地中断的句柄使用 `install_interrupt()` 并正确移除。回调应非阻塞，覆盖取消先于句柄安装的竞争，保留 FD、Socket 和临时文件的清理责任。
- 普通请求复用路由包装的 `while_connected`，不要假定 Uvicorn 会自动取消断连端点。SSE 声明 `StreamingResponse`，由流响应管理接收通道，避免两个消费者同时监听断连；关闭时等待 Redis 客户端释放。
- Run 详情的状态、Task、用户输入、命令和待决事项在同一个短只读快照中投影。PostgreSQL 使用局部 REPEATABLE READ，不获取控制面写锁；不能把旧 WAITING_USER 与新“决定已处理”组合返回。普通取消监控仍使用 READ COMMITTED，不持有固定快照。
- 容量错误复用 `APICapacityError` 与统一响应，保留稳定错误码、查询编号、`Retry-After` 和 `not_started` / `unknown` 区分。早期阶段可能已经执行时不能宣称未开始，不能自动重试非幂等写入。
- 关闭执行器时拒绝新工作并撤销排队工作；运行中的线程仍按真实完成回收。清理失败或超时保留可诊断的待清理状态，不能提前报告资源已释放。

SSE 的数据库事件、重连游标、终态补齐、Redis 失败回退和退避必须保留；Redis 仅负责实时传输。框架静态文件响应与受准入限制的 multipart 暂存可能短暂使用默认工作线程，这不构成把长依赖调用放回默认池的理由。

## 4. 将耗时准备与短事务提交分开

上传、图片导入和示例导入沿用现有阶段：读取并校验归属 → 私有暂存 → 媒体检查 → 对象上传 → 短事务复核并提交引用。参考 `app.py` 的上传与示例导入、`image_api.py` 的素材导入；不要把整个链路塞进 `write` 池。

- 解码、网络等待、大文件复制/哈希/加密、对象上传和递归目录删除放在全局 `mutation_lock` 与业务事务之外。提交时重新检查项目存在、归属、素材上限、额度，以及本链路适用的来源、版本和任务状态；锁外准备时的检查不能替代提交复核。
- 复用 `Staging.commit` / `finish` 协调取消、提交与清理。取消或删除后到达的准备结果不得发布；数据库已提交但响应丢失时，保留正式文件并允许查询事实，不能将断连当作回滚。
- 删除先在短事务中校验任务/租约并撤销引用，提交后进入清理池。失败走既有延后回收；数据库状态未知时不能删除归属不明的项目缓存。未引用远端对象沿用现有延迟 GC，本地维护循环不代替远端回收。
- 自然语言控制先取目标，锁外分类，再在短事务中复核目标状态；显式控制走独立容量。隔离、降级和超时都不能放宽归属、方案确认或发布门禁。

## 5. 验证与完成标准

按修改范围选择定向验证，使用[统一隔离入口](../development.md#常规回归)。文档改动只核对链接、接口和示例；实现改动应覆盖实际阻塞、取消和竞争窗口，不能只断言返回 200。

| 变化 | 验证重点 | 测试入口 |
| --- | --- | --- |
| 执行器、路由与容量 | 队列有界、排队不执行、超时不提前释放槽位、上下文与关闭行为、健康检查仍可用 | `tests.test_api_execution`、`tests.test_api_isolation` |
| SSE | 原生异步读取、断连关闭、流数上限、重连与数据库回退 | `tests.test_api_stream_async` |
| 上传、导入与发布 | 解析前准入、锁外准备、额度/删除/版本竞争、提交前后取消、整请求期限 | `tests.test_api_upload_races`、`tests.test_api_publication_races` |
| 媒体调用 | 本地取消与期限、FD/Socket 回收、超大响应拒绝、实际解码容量 | `tests.test_api_media_cancellation` |
| 指标与多实例拥塞 | 低基数完整快照、真实活动/队列/默认线程占用、慢依赖时健康与控制可响应、恢复无超配 | `tests.test_api_capacity`、`tests.test_api_pressure` |

例如修改执行器与路由包装时：

```bash
.venv/bin/python -m tests.run_distributed_suite tests.test_api_execution tests.test_api_isolation
```

涉及拥塞时的页面行为，使用 [run_api_browser_isolation.py](../../tests/run_api_browser_isolation.py) 验证错误展示、项目状态和实际取消。指标扩展同时检查 `api_capacity.py`、生产采样、采集/导出和受保护页面，不能以手写快照替代真实池占用；规则见[可观测性与用量开发](change-observability-usage.md)。

交付说明列明路由和池、准入/期限、锁与事务范围、真实取消能力、竞争测试及未验证边界。配置变化同步[配置说明](../distributed-execution-configuration.md)，设计及阶段证据放入[API 隔离设计与验收](../api-isolation-design.md)。历史压测不作为当前 SLA 或发布通过证明；完整检查及部署仍按[开发与测试](../development.md)执行。
