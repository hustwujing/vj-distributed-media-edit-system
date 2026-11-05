# 分布式执行配置与验证

此文档描述当前分布式执行的应用协议与配置契约。共享 PostgreSQL、S3 对象存储、节点本地缓存和租约机制可用于跨物理机拓扑，但当前仓库内置的 `deploy/dynamic_cluster.py` 只实现单机多实例部署，不提供跨机发布、负载均衡或整机容灾。写入配置和执行测试不等于已部署；不要把新代码指向旧 SQLite 数据目录并假定它会迁移历史数据。

## API 依赖容量与期限

API 将同步依赖调用隔离到不同的有界执行器。异步 Redis SSE 不占这些线程；静态文件发送和受准入限制的 multipart 临时文件仍使用框架的短文件 I/O，不代表所有框架代码都不用 AnyIO。实际解码服务仍最多两个并行操作，没有扩大解码权限或增加业务任务类型。设计及验收见 [API 隔离设计](api-isolation-design.md)。

| 池名 | 工作线程 | 等待队列 | 排队期限（秒） | 执行期限（秒） |
| --- | ---: | ---: | ---: | ---: |
| read | 6 | 12 | 1 | 15 |
| write | 4 | 8 | 1 | 30 |
| critical | 2 | 8 | 1 | 15 |
| storage | 4 | 8 | 1 | 120 |
| media | 2 | 4 | 1 | 100 |
| diagnostics | 2 | 4 | 1 | 15 |
| control | 2 | 4 | 1 | 45 |
| cleanup | 1 | 8 | 2 | 120 |
| auth | 2 | 4 | 0.5 | 5 |
| stream_read | 2 | 8 | 1 | 15 |
| health | 2 | 4 | 0.5 | 5 |

覆盖方式为 `VIDEO_USE_API_<大写池名>_WORKERS`、`_QUEUE`、`_QUEUE_SECONDS`、`_RUN_SECONDS`。单池最多 64 线程、256 排队、30 秒排队期和 600 秒执行期；所有池线程上限合计不能超过 128，media 不能超过 2。启动时拒绝非法配置。

`VIDEO_USE_API_DATABASE_SECONDS` 默认 5 秒，范围 0.1–30，限制 API 数据库连接/语句等待；单条语句期限不同于整请求期限。`VIDEO_USE_API_STREAM_LIMIT` 默认 64，范围 1–1024，超过时在 SSE 响应开始前返回 503。

上传、图片导入、示例加载、删除分别有请求准入（活动/排队）：2/4、2/4、1/2、2/4，解析 multipart 前取得名额。整请求期限使用 `VIDEO_USE_API_UPLOAD_SECONDS`、`VIDEO_USE_API_IMAGE_IMPORT_SECONDS`、`VIDEO_USE_API_DEMO_SECONDS`、`VIDEO_USE_API_DELETE_SECONDS`，前三者默认 600 秒，删除默认 30 秒，范围 0.01–3600。

排队满或超时返回稳定 `API_*` 错误码、`Retry-After: 1` 和查询编号。响应中的 `not_started` 仅表示未开始任何依赖工作；已开始或先前阶段可能提交时使用 `unknown`，需刷新确认，不能据此盲目重试非幂等写入。调用方停止等待不会提前释放仍在运行的同步 SDK 线程。媒体 Socket 可本地中断，但不宣称远端解码已停止。

临时上传与已删除项目的本地缓存由 API 独立维护循环清理；数据库不可用时不删除归属未知的项目缓存。未提交远端对象沿用原有延迟 GC，仍需运维按既有对象清理流程执行，不能将本地维护循环当作远端 GC。

受保护观测页提供“API 容量与等待”，包括每实例/每池活动数、队列、拒绝、超时、实际完成、取消和等待/运行累计秒数，以及事件循环滞后、默认线程占用。每 5 秒发布，采集器只展示最近 20 秒快照；缺失保持未知。计数、累计和最大值自进程启动计算；`completed` 是实际工作返回次数，可能同时属于失败或调用方已取消，不能视作成功业务次数。Prometheus 指标以 `video_use_api_pool_`、`video_use_api_gate_`、`video_use_api_loop_` 开头，只使用实例和固定池名标签。

当前仓库的内置单机多实例部署以根目录 [README](../README.md#本机隔离部署) 和 `deploy/dynamic_cluster.py` 为准。早期 BOSS 接入和现场验收背景保存在 [分布式运行与验收配置](distributed-deployment.md)。

## 配置文件职责

`.env.example` 只提供部分应用配置，不包含完整部署所需凭证。内置部署使用以下私有文件，由 systemd 加载；文件保持 `0600`，不提交版本库：

| 文件 | 内容 |
| --- | --- |
| `.env` | ElevenLabs 转写、自然语言控制、可选 Redis 正文/活动传输 |
| `.agent.env` | `VIDEO_USE_MODEL`、`VIDEO_USE_MODEL_BASE_URL`、`VIDEO_USE_MODEL_API_KEY` 等模型和适配器配置 |
| `.storage.env` | S3/BOSS endpoint、bucket、prefix、AWS 凭证及对象加密密钥 |
| `.distributed.env` | 共享数据库、会话密钥、并发与资源配置；内置部署首次缺失时生成 |

当前独立实例的各服务配置由部署脚本写入 `/etc/distributed-agent-system/`。`VIDEO_USE_REDIS_URL` 为空时使用数据库快照同步；Redis 不作为任务或成果的权威存储。对话模型、图片/视频供应商和本地图片模型需要分别准备，配置主模型不会自动启用全部能力。

### 对话模型与沙盒推理强度

在私有 `.agent.env` 中配置，并通过完整部署重启服务使其生效：

```dotenv
VIDEO_USE_MODEL=gpt-5.6
VIDEO_USE_MODEL_REASONING_EFFORT=high
```

`VIDEO_USE_MODEL` 同时用于沙盒 Codex Agent、服务端轻量对话，以及主方案检查服务不可用时的备用模型检查。`VIDEO_USE_MODEL_REASONING_EFFORT` 控制沙盒 Codex Agent，包括规划、正式执行和工作流验收；未设置时为 `medium`，支持 `minimal`、`low`、`medium`、`high`、`xhigh`，实际支持范围还需由选定模型确认。无效值会在准备沙盒前报错，不会静默降级。

轻量对话和备用方案检查目前使用各自固定的 `low` 推理强度；Jev 结构化分类器由独立配置控制。更改主模型不会改变图片或视频生成模型。部署前应验证网关模型列表及实际请求，部署后以真实任务的模型调用记录和沙盒配置核对生效结果；不要仅凭修改配置文件认定运行中的进程已经切换。

## 共享服务与节点目录

API、执行器、节点 reaper 和外部清理进程使用同一个 PostgreSQL 数据库。素材、检查点、节点输出和交付文件使用同一个 S3 兼容对象桶。在自行实现的跨机部署中，各机器的 `VIDEO_USE_DATA_DIR` 应是独立缓存目录，可以不同，不要求共享文件系统；这项协议能力不表示当前脚本已能部署这些机器。

```dotenv
VIDEO_USE_DISTRIBUTED=1
VIDEO_USE_DATABASE_URL=postgresql://video_use:REPLACE_PASSWORD@db.example/video_use
VIDEO_USE_OBJECT_ENDPOINT=https://objects.example
VIDEO_USE_OBJECT_BUCKET=video-use
VIDEO_USE_OBJECT_PREFIX=video-use
VIDEO_USE_OBJECT_REQUIRE_ENCRYPTION=1
VIDEO_USE_OBJECT_ENCRYPTION_KEY=REPLACE_WITH_BASE64_ENCODED_32_BYTE_KEY
AWS_ACCESS_KEY_ID=REPLACE_ACCESS_KEY
AWS_SECRET_ACCESS_KEY=REPLACE_SECRET_KEY
AWS_DEFAULT_REGION=us-east-1
VIDEO_USE_SESSION_SECRET=REPLACE_WITH_THE_SAME_64_HEX_CHARACTERS_ON_ALL_API_REPLICAS
VIDEO_USE_DATA_DIR=/var/lib/video-use-data
VIDEO_USE_EXECUTOR_ROOT=/var/lib/video-use-executor
VIDEO_USE_RUNTIME_BUILD=/opt/video-use/runtime/build
VIDEO_USE_CANCEL_POLL_SECONDS=0.5
VIDEO_USE_WORKSPACE_STORAGE=tmpfs
```

密钥只配置在可信服务端，不能写入 Agent 参数、文件引用、日志或运行包。对象桶由运维预先创建；接口不会自动创建生产桶。API 副本必须配置相同的会话签名密钥，不能各自在本地生成不同密钥。

参与同一文件域的节点还需共享并备份对象加密密钥。对象在上传前进行认证加密，下载时通过认证和哈希核验后才向工具提供明文；不能将公开可读桶当成权限边界，也不能直接覆盖密钥来代替旧数据重加密。

执行器上的沙箱仍须具备 Bubblewrap、cgroup、独立 UID、seccomp 和所需固定工具。节点 reaper 必须与执行器共同运行。`VIDEO_USE_RUNTIME_BUILD` 指向通过构建生成的工具包；测试使用自己的临时工具包，不覆盖服务运行目录。

## 状态与并发

PostgreSQL 使用短事务和事务级 advisory lock 保证跨表领取、资源额度和成果发布的原子性。只读观察不获取该控制锁。该设计优先保证一致性：控制面的写事务串行，计算和外部等待不串行；不应在控制事务内执行耗时网络或工具操作。大规模部署需要实测控制事务吞吐，不能把测试中的少量执行器结果等同于无限扩展能力。

工作流节点有独立租约槽位和 attempt，Agent/验收/控制阶段使用根槽位。根阶段与执行中的节点互斥；独立节点可以并行。原节点失联后，新节点依据数据库租约接管，旧 attempt 无法提交新的正式成果。

所有节点应采用一致的用户和供应商额度策略；CPU、内存、GPU 属于节点自身容量：

```dotenv
VIDEO_USE_CONCURRENCY=2
VIDEO_USE_NODE_CONCURRENCY=2
VIDEO_USE_USER_CONCURRENCY=2
VIDEO_USE_NODE_CPU_SLOTS=4
VIDEO_USE_NODE_MEMORY_MB=12288
VIDEO_USE_PROVIDER_CONCURRENCY=4
VIDEO_USE_NODE_OFFLINE_SECONDS=60
```

GPU 默认根据 `/dev/nvidia*` 设备发现，也可通过 `VIDEO_USE_GPU_DEVICES=0,1` 显式限定。`VIDEO_USE_GPU_MANAGER_PATH`、`VIDEO_USE_GPU_LEASE_ROOT` 可覆盖现有 GPU 管理模块和本机租约目录；固定图片模型及环境仍须提前安装。

## 运维入口

以下命令使用与服务相同的数据库和对象存储配置，应由可信服务运维身份执行：

```bash
.venv/bin/python -m webapp.executor_admin status
.venv/bin/python -m webapp.executor_admin drain WORKER_ID
.venv/bin/python -m webapp.executor_admin reconcile PROVIDER_CALL_ID
.venv/bin/python -m webapp.executor_admin gc --limit 100
```

`drain` 停止该执行器领取新业务；`reconcile` 只重新核实已有外部任务，不重新生成，也不恢复已取消业务。GC 删除未引用且超过保留期限的对象，可由运维定时触发；它不把仍被正式文件引用的对象当成垃圾。

## 文件与恢复

原素材和交付文件通过项目内的逻辑路径映射到不可变对象。Agent 工作区及会话使用项目检查点；工作流步骤使用独立的节点检查点，按节点修订保存输出。汇合节点只恢复自身及依赖的节点成果，避免并行分支覆盖项目快照。

对象上传先于正式引用提交。提交时检查执行代次、租约、取消状态和修订；上传成功但提交失败的对象不能成为交付。未引用对象延迟回收，并保留删除墓碑以处理晚到上传。文件下载校验大小和 SHA-256，校验通过后原子替换本地缓存。

默认工作区使用有容量上限的 tmpfs。设置 `VIDEO_USE_WORKSPACE_STORAGE=disk` 时，每次执行预分配一个大小受 `VIDEO_USE_TASK_DISK_BYTES` 限制的 ext4 镜像并挂载；节点必须具备 loop 挂载和 `mkfs.ext4`，空间不足或不支持时明确失败，不退回无配额目录。两种模式都由 reaper 回收。

## 取消和失联

取消不依赖网关定位执行机器或消息队列。共享状态中保存取消要求后，每个执行实例的统一监控批量检查自己的活跃调用，触发本地停止。观察线程与本地期限看门狗分开，数据库查询卡住不能延长本地运行许可。

本地工具按本次执行进程组/cgroup 终止。同步 HTTP 通过持有的 socket 句柄中断；TLS 包装过程中仍保留可 shutdown 的内核连接句柄。执行期间的 S3 SDK 传输放在有父进程死亡保护的专用子进程中，可被取消和强杀。

断开本地请求不保证供应商停止计算。异步任务依据已保存的供应商句柄单独取消、查询和核实；上游已成功、已失败、已取消或状态未知分别记录。未知提交不得重新生成来试探。业务停止、物理资源清理和上游实际状态是不同维度。

## 验证命令与边界

```bash
uv sync --locked --extra web --extra web-test
.venv/bin/python -m tests.run_distributed_suite
node --test tests/*.test.mjs
```

Python 入口运行全部后端测试。分布式用例需要本机 PostgreSQL `initdb`/`pg_ctl`，自动启动独立的真实 PostgreSQL 和 HTTP S3 协议测试服务，使用不同节点目录及多个进程；不会连接部署数据库或真实付费生成接口。特权沙箱测试要求 Linux root、Bubblewrap 和可用 cgroup。环境、定向测试及真实服务驱动说明见 [开发与测试](development.md)。

磁盘工作区的硬配额另可验证：

```bash
VIDEO_USE_WORKSPACE_STORAGE=disk .venv/bin/python -m tests.run_distributed_suite tests.test_execution_runtime.RuntimeTests.test_tmp_storage_has_a_hard_limit
```

报告写入 `docs/test-evidence/distributed-suite.json`，包含源码摘要、执行用例数、失败和跳过原因。源码在测试期间发生变化会使报告不通过。真实供应商取消能力、对象存储产品差异和生产网络拓扑仍应在上线前使用目标环境做验收；协议测试不能冒充供应商实际能力证明。
