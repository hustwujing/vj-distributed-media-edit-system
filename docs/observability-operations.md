# 可观测性操作与排障手册

本手册适用于当前仓库的 `dynamic-agent` 独立部署；服务前缀、端口和私有配置目录以根目录
[README](../README.md#本机隔离部署) 及 `deploy/dynamic_cluster.py` 为准。

## 入口

以 root 在项目目录执行，命令始终读取已部署版本：

```bash
./deploy.sh observe status
./deploy.sh observe task TASK_ID
./deploy.sh observe task TASK_ID --full
./deploy.sh observe task TASK_ID --output /root/task-diagnosis.json
./deploy.sh observe request REQUEST_ID
./deploy.sh observe logs TASK_ID --after 0 --limit 100
./deploy.sh observe metrics
./deploy.sh observe alerts
./deploy.sh observe maintenance on
./deploy.sh observe maintenance off
```

`task` 默认给出状态、当前尝试、最近错误、等待信息、后续建议与证据缺口；`--full` 展开有界证据，`--output` 创建新的 0600 文件，拒绝覆盖已有文件。所有诊断命令只读；maintenance 是显式的告警维护开关，不修改业务任务。不会在查询时重新生成、重试或取消任务。

网页聊天任务提供“下载任务诊断”，请求出错显示可复制查询编号。项目诊断 API 为 `GET /api/projects/{project}/diagnostics/{task}`，支持 `after/limit/download`；必须通过项目所有者鉴权。运维拓扑与堆栈只在 root 命令/运维接口提供。

## 集中检索、指标与看板

`dynamic-agent-observer.service` 将 `dynamic-agent` 专用 journal 中的结构化 JSON 收集到 `.cluster/observability/observations.sqlite3`。业务不依赖此服务完成状态提交。HTTP 仅监听 `127.0.0.1:19768`，所有数据端点要求 Bearer 凭据；凭据保存在 `/etc/distributed-agent-system/observation.env`，不放在命令参数或 URL 中，不在报告中展示。

通过 SSH 本地端口转发访问 `/dashboard`，将凭据填入密码框；页面不将其持久化到浏览器存储。视图包含集群/资源、任务日志、上游调用、存储依赖、告警与观测健康。数据接口：`/health`、`/snapshot`、`/logs`、`/alerts`、`/metrics`。`/metrics` 是 Prometheus 文本格式；当前看板直接读取采集器，无需另外安装 Grafana/Prometheus。

`/logs` 支持 task_id、request_id、trace_id、attempt_id、node、release、operation、error_code、service、level，以及 since/until/after/limit。`newest=1` 从最新记录向前分页；默认按采集序号递增。跨来源时间戳不构成严格因果顺序，事务审计 seq、attempt、generation 和关联 ID 一起用于判断。

这是当前单机多实例的集中观测实现。JSON 日志含 trace/span 标识；没有宣称已部署 OTLP/Jaeger 后端或跨机日志容灾。后续跨物理机时需要将各机采集结果汇聚至统一后端。

## 典型排障

1. 用户提供任务 ID 或页面查询编号，先运行 `observe task` 或 `observe request`。无对应任务的 HTTP 错误仍可通过请求日志查看。
2. 排队：检查等待原因、节点心跳、资源和队列最老等待时间。正在等待供应商时，不因无百分比就判断卡死。
3. 失败：查看 latest_errors 中的错误码、阶段、attempt 和部署版本，展开日志中的脱敏代码位置。脚本退出码、信号和 OOM 计数独立显示；没有 cgroup 证据时 OOM 为未知。
4. 接管：比较旧尝试 expired、新尝试状态和各自节点；以事务审计和成果引用为准，日志中的“意图”不代表提交成功。
5. 提交未知：检查已保存的上游任务 ID、调用记录和外部清理状态。诊断不会自动重新提交收费请求。
6. 取消：分别看业务 cancelled、本地清理、上游取消及其确认状态。上游未确认不能当作已停止。
7. 数据库不可达：root 诊断尽可能返回采集器证据并标注缺口；用户接口无法确认权限时拒绝返回任务信息。采集器自身故障时检查 systemd 和专用 journal。

```bash
journalctl --namespace=dynamic-agent -u dynamic-agent-executor@a --since '30 minutes ago' -o cat
journalctl --namespace=dynamic-agent -u dynamic-agent-observer --since '30 minutes ago' -o cat
systemctl status dynamic-agent-observer.service
```

HTTP 延迟指标记录处理到响应头就绪的时间，SSE 长连接的持续时间不作为任务计算耗时。phase 指标为各次调用累计耗时；任务 wall_seconds 单独给出，两者不能直接相减推断并行任务等待时间。查询有上限，complete=false 或 gaps 表示部分证据缺失/截断，不能宣称完整。

## 告警与健康

监控队列积压、节点不足、持续 5xx、提交未知/提交过久、本地清理超期、对象存储异常、磁盘容量和遥测丢弃。告警经过 pending→firing，恢复转 resolved；重复状态不重复生成历史记录，维护期间 suppressed。默认持续窗口 30 秒、队列最老等待阈值 300 秒，可通过 `VIDEO_USE_ALERT_HOLD_SECONDS` / `VIDEO_USE_ALERT_QUEUE_SECONDS` 调整。提交中超过 600 秒才计入待核实，不能自动判为上游失败。

通知渠道当前明确显示 `not_configured`：告警在本地 CLI/看板提供，没有擅自接入邮件、聊天或外部收件人。实际通知接收端需另行配置与授权。整机断电无法由同机观测器向外报告，需要独立外部监控。

存活是观测进程可响应；ready 表示已完成观测扫描；数据库、对象存储、worker 另有状态。对象存储探测最多每 60 秒一次，HEAD 已发布对象（无对象时 HEAD bucket），不读取用户内容、不创建生成任务。GPU 状态来自有 2 秒超时的 nvidia-smi；不可读时显示 unavailable。

## 容量、留存与维护

- 业务进程：异步输出队列最多 1024 条；满时丢弃遥测并记录计数，业务不等待输出。日志不保存异常消息、源码行、局部变量、请求体或完整脚本，保留异常类、因果链及代码位置。
- 专用 journal：持久化，最多 512 MiB、7 天，保留磁盘安全余量；避免修改其他项目的全局日志策略。
- 集中索引：默认 20 万条、7 天、512 MiB 物理上限；告警历史 30 天。清理后回收 SQLite 空间。参数分别为 `VIDEO_USE_OBSERVATION_MAX_ROWS`、`VIDEO_USE_OBSERVATION_RETENTION_SECONDS`、`VIDEO_USE_OBSERVATION_MAX_BYTES`。
- 事务审计与任务关联：属于任务生命周期记录，保留到项目删除；不随日志采样或轮转丢弃。项目删除同步清理，不影响活跃任务恢复必需数据。
- 诊断包：按需生成，调用者负责保管和删除，默认不会自动上传第三方。
- 采集器：CPUQuota 100%、MemoryMax 512 MiB、最多 16 个并发 HTTP 处理线程；查询限制最多 200 条，sqlite 访问超时 0.2 秒。采集故障不授权任务重放。

`deploy.sh stop` 停止业务服务并进入维护模式，**保留观测服务运行**以支持停机排障；`start` 恢复业务并退出维护。完整关闭观测器可由运维显式运行 `systemctl stop dynamic-agent-observer.service`，业务启动脚本会再次启动它。部署升级会重启观测服务，短暂中断由 journal cursor 回补；cursor 不再有效时回读最近五分钟，并标记采集缺口。

回退时使用保留的已验证 release 及备份 unit/配置，保留新增观测表（属于增量表/触发器），不覆盖新产生的业务数据。正式数据库恢复应单独处理备份后的数据，诊断工具不提供破坏性一键回滚。

阶段计量是非授权性数据，使用独立连接，语句/锁等待预算 50 毫秒（PostgreSQL 建连超时下限 1 秒）。写入失败只记录观测降级，不改变任务结果；不能与任务预算、租约等必须提交的控制状态混淆。真实锁竞争用例已验证返回时间小于 0.5 秒。
