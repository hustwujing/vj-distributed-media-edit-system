# 分布式执行配置与验证

此文档描述本次重构的部署契约。写入配置和执行测试不等于已部署；不要把新代码指向旧 SQLite 数据目录并假定它会迁移历史数据。

本项目已配置的 BOSS 接入、加密及现场验收步骤以 [分布式运行与验收配置](distributed-deployment.md) 为准。下面同时保留通用架构和独立测试环境的说明。

## 共享服务与节点目录

API、执行器、节点 reaper 和外部清理进程使用同一个 PostgreSQL 数据库。素材、检查点、节点输出和交付文件使用同一个 S3 兼容对象桶。各机器的 `VIDEO_USE_DATA_DIR` 是独立缓存目录，可以不同，不要求共享文件系统。

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
uv sync --extra web --extra web-test
.venv/bin/python -m tests.run_distributed_suite
```

测试需要本机 PostgreSQL `initdb`/`pg_ctl`。分布式用例自动启动独立的真实 PostgreSQL 和 HTTP S3 协议测试服务，使用不同节点目录及多个进程；不会连接部署数据库或真实付费生成接口。特权沙箱测试要求 Linux root、Bubblewrap 和可用 cgroup。

磁盘工作区的硬配额另可验证：

```bash
VIDEO_USE_WORKSPACE_STORAGE=disk .venv/bin/python -m tests.run_distributed_suite tests.test_execution_runtime.RuntimeTests.test_tmp_storage_has_a_hard_limit
```

报告写入 `docs/test-evidence/distributed-suite.json`，包含源码摘要、执行用例数、失败和跳过原因。源码在测试期间发生变化会使报告不通过。真实供应商取消能力、对象存储产品差异和生产网络拓扑仍应在上线前使用目标环境做验收；协议测试不能冒充供应商实际能力证明。
