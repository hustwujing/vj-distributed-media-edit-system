# 分布式运行与验收配置

本次完成代码与测试，不安装或重启生产分布式服务。每台机器独立运行 API、执行器或 reaper；共享 PostgreSQL 和对象存储，本地目录仅作缓存/执行暂存。测试中的节点是同机独立目录与进程，不等于已经通过真实跨物理机网络分区验收。

## 共享配置

- `VIDEO_USE_DISTRIBUTED=1`
- `VIDEO_USE_DATABASE_URL=postgresql://...`：所有实例连接同一服务端数据库，凭据由服务端私有配置提供。不要把SQLite/WAL放在NFS。
- `VIDEO_USE_SESSION_SECRET`：所有API实例使用相同且高熵的会话签名密钥。
- 对象存储使用项目根目录私有 `.storage.env`（gitignore、0600）；该文件已经用ai-tool的BOSS配置完成真实验证，但未加载进现有生产服务。
- `.storage.env` 包含 `VIDEO_USE_OBJECT_BUCKET`、`VIDEO_USE_OBJECT_ENDPOINT`、`VIDEO_USE_OBJECT_PREFIX`、AWS认证变量、`VIDEO_USE_OBJECT_REQUIRE_ENCRYPTION=1` 和 `VIDEO_USE_OBJECT_ENCRYPTION_KEY`。
- BOSS共享桶可以匿名读取对象，因此所有文件、产物及checkpoint均先做AES-256-GCM认证加密；对象key作为附加认证数据，防止对象互换。每次随机nonce，下载在认证通过之后才对调用者提供明文，再核对SHA-256。
- 加密密钥必须在参与同一文件域的节点间一致，并独立备份；丢失会无法恢复数据。不要直接覆盖该密钥轮换；当前版本不提供自动重加密迁移。
- API鉴权后服务端解密并返回文件，不向浏览器提供绕过鉴权的BOSS公开原始地址。数据库保存对象ID、位置、hash、大小、项目与版本，不保存解密密钥。

## 每个节点的配置

- `VIDEO_USE_DATA_DIR`：本机独立数据/缓存目录，不能作为唯一成果存储。
- `VIDEO_USE_EXECUTOR_ROOT`：本机root私有执行身份、挂载、GPU记录目录。一个节点的执行器和reaper必须共用，不能与另一套数据库或测试共用。
- `VIDEO_USE_UID_START/COUNT`：本机任务身份范围，避免与其他执行服务冲突。
- `VIDEO_USE_NODE_OPERATIONS`：逗号分隔的能力ID白名单，默认`*`。GPU节点只声明已安装模型/环境的能力；CPU节点可以仅声明媒体工具；远程服务节点需要对应凭据。直接领取与候选筛选都会检查白名单。
- `VIDEO_USE_NODE_CPU_SLOTS`、`VIDEO_USE_NODE_MEMORY_MB`、`VIDEO_USE_NODE_CONCURRENCY`、`VIDEO_USE_USER_CONCURRENCY`：资源/业务并发上限；外部并发通过共享数据库计数。
- `VIDEO_USE_WORKSPACE_STORAGE=tmpfs|disk`；disk使用按任务固定大小的ext4 loop映像，`VIDEO_USE_TASK_DISK_BYTES`是硬容量；两种模式都由本机reaper回收。
- `VIDEO_USE_CANCEL_POLL_SECONDS`默认0.5秒；监控有独立读取连接和本地许可watchdog。数据库失联不延长许可，资源最终清理由本机reaper处理。

每台执行节点构建相同批准运行镜像与固定工具。具体环境版本由runtime fingerprint绑定，不能让运行到一半的本地步骤随意换环境；不兼容执行器拒绝领取。滚动发布时先排空旧worker，保留兼容worker完成已绑定步骤，再撤掉旧环境。

## 进程职责

- API：项目归属、上传/下载、用户取消、共享事件查询。多个实例使用相同数据库/会话密钥/存储配置。
- AgentHost：候选发现、原子领取、续租、执行、成果提交；多个实例可竞争。独立分支按step slot领取，根Agent/验收执行与分支互斥。
- reaper：每台机器独立，按本地单调时钟许可清理进程、cgroup、挂载、UID和GPU；可领取共享外部清理记录。不能把另一台机器进程不可见视为已清理。
- 对象GC：由受信任的管理定时任务调用 `python -m webapp.executor_admin gc --limit 100`。仅处理无引用且超过保留期的对象，保留tombstone应对晚到上传。物理对象删除不在业务数据库事务内执行。

运行进程前安全加载私有环境（或由systemd EnvironmentFile注入），不要把密钥放入命令行。代码入口仍为 `python -m webapp.agent_host`、`python -m webapp.execution_reaper`、`uvicorn webapp.app:app`，不引入必须依赖的消息队列。

## 维护与取消核实

`python -m webapp.executor_admin status` 显示worker、租约、本地/外部清理。`drain WORKER_ID`停止该worker领取新业务，已开始工作遵守原租约与预算。

取消事务保存稳定request_id，停止业务和新提交，保留此前正式成果；本地清理和上游清理各自显示。远程取消只是accepted时继续核实，不能直接标记cancelled。清理有独立owner、generation、lease_until、deadline和重试计数；旧回执不能覆盖接管者结果。

`python -m webapp.executor_admin reconcile CALL_ID` 对unknown/unconfirmed/unsupported且已有任务ID的调用重新安排有限查询，不提交新的生成，不恢复业务。没有ID且供应商不支持查回时保留unknown；不能承诺“恰好一次”外部副作用。实际成功的上游保留succeeded及用量，即使本次业务已取消。金额未知保持null。

## 事务与性能边界

PostgreSQL使用短事务和事务级advisory lock串行化控制平面的跨表领取/配额/取消更新；读取观察不获取该锁。文件传输在发布事务外完成；发布时校验租约、代次和版本。不把当前实现描述为无限吞吐的行级分片队列。

各步骤输出独立checkpoint；Agent脚本/会话单独版本化，汇合只消费已验收且版本一致的成果。阶段指标包含prepare_environment、input_download、local、submit、poll、collect、validate、output_upload；部分指标为嵌套墙钟时间，不能简单相加当作CPU时间。原有队列、执行占槽、网络耗时与调用次数继续保留。

## 验证命令

完整隔离回归（真实本地模型需本机运行环境）：

```bash
VIDEO_USE_TEST_REAL_MODELS=1 .venv/bin/python -m tests.run_distributed_suite
node --test tests/artifact_order.test.mjs tests/task_progress.test.mjs
.venv/bin/python -m tests.run_browser_workflow_smoke
```

测试会启动独立真实PostgreSQL、HTTP S3协议测试服务，使用不同本地根目录、独立进程及真实Linux沙箱。生产服务和生产数据库不参与。

真实BOSS验证（会在随机测试前缀写入并删除少量测试对象）：

```bash
.venv/bin/python -m tests.run_live_boss_storage
```

该测试读取`.storage.env`，创建隔离PostgreSQL，验证加密往返、原节点本地目录丢失后的脚本/配置/会话恢复，最后只删除其独立前缀下DB记录的测试对象。不创建或清空共享桶，不修改其他ai-tool文件。
