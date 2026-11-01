# 单机多实例部署与操作

部署拓扑：两个API、两个独立执行节点（各自reaper）、专用PostgreSQL、共享加密BOSS，以及每个API配套的固定媒体解码服务。入口仍为 `/video-use/`；内部Nginx在127.0.0.1:18765分流至18766/18767。PostgreSQL专用端口15432，不修改系统原有5432集群。

## 统一命令

在项目根目录以root运行：

```bash
./deploy.sh status   # 状态，不改变服务
./deploy.sh check    # 核对已部署版本完整测试和私有配置
./deploy.sh stop     # 维护入口、排空执行器、等待清理、停止数据库
./deploy.sh deploy   # 备份、停止、更新运行环境/units/schema、启动并检查
./deploy.sh start    # 启动已部署集群，恢复入口
./deploy.sh restart  # 停止后启动；不重新安装源码或修改配置
```

`stop` 保留数据库、BOSS文件与私有配置。若资源未清理完，会保留reaper和数据库运行并报错，不伪称全部停止。正在处理的任务遵循30秒排空窗口与90秒服务退出上限；中断后由持久化状态恢复，同步外部提交未知不自动重提。

`deploy` 会冻结当前源码到 `/opt/vu/r/<源码指纹>/`，依赖按uv.lock快照保存在 `.cluster/venvs/`，对冻结版本运行完整测试；同版本已验证时复用结果。服务从该快照运行，工作区继续编辑不会混入当前运行代码。不能用定向测试冒充完整回归。也可先自行验证：

```bash
VIDEO_USE_TEST_REAL_MODELS=1 .venv/bin/python -m tests.run_distributed_suite
./deploy.sh deploy
```

此部署按先前确认的范围使用新PostgreSQL，不迁移旧SQLite项目。旧 `workspace/` 数据保留并备份，新界面不会自动显示旧项目。脚本不会删除或重建已有集群数据库，也不会删除BOSS成果。

## 实例与隔离

| 实例 | 本地目录 | 服务 |
| --- | --- | --- |
| API A | `.cluster/api-a` | video-use-api@a |
| API B | `.cluster/api-b` | video-use-api@b |
| 执行节点 A | `.cluster/node-a`、`/var/lib/vu-a` | video-use-executor@a、video-use-watchdog@a |
| 执行节点 B | `.cluster/node-b`、`/var/lib/vu-b` | video-use-executor@b、video-use-watchdog@b |
| PostgreSQL | `.cluster/postgres` | video-use-db |
| 固定媒体解码 | `/run/vu-media-a`、`/run/vu-media-b` | video-use-media@a、video-use-media@b |

节点UID分别使用700000–700063和700100–700163。GPU候选分别为0和3，仍使用既有共享GPU租约目录；实际可用容量由GPU管理器检查。每个执行节点初始并发1，两节点可并行。systemd挂载策略禁止节点直接读取对方的目录。

私有共享数据库/会话配置在 `.distributed.env`，BOSS和文件加密配置在 `.storage.env`；均为0600、被gitignore。systemd读取这些配置，密钥不写入unit正文或命令行。各实例配置由脚本生成到 `/etc/video-use-cluster/`。更改固定拓扑、端口、UID/GPU候选需修改 `deploy/cluster.py` 并重新验证。

## 验收与维护

`/opt/vu/r/<已部署版本>/.venv/bin/python deploy/cluster_smoke.py` 对专用测试项目执行实际API切换、强杀节点A、删除该测试项目在A的缓存、节点B恢复与分支汇合、最终Agent验收和跨API取消。它会临时停止实例，仅在维护/验收窗口运行；不调用图片或视频生成服务，最终验收会调用配置的Agent模型。

日志例子：

```bash
journalctl --namespace=video-use -u video-use-executor@a -n 100 --no-pager
journalctl --namespace=video-use -u video-use-watchdog@b -n 100 --no-pager
```

脚本备份保存在 `/var/backups/video-use/cluster-时间戳/`，包含源码、私有配置、旧SQLite备份；已运行集群还保存专用PostgreSQL的pg_dump。恢复数据库必须另行确认备份之后的数据处理，脚本不会自动覆盖新数据。

这是单机的分布式协议和多实例部署，整台宿主机故障仍会同时影响所有实例。BOSS是外部共享存储，PostgreSQL在本机；不宣称跨机容灾。

宿主机禁止非特权用户命名空间时，API仍以video-use用户运行，通过Unix socket向固定解码服务传递已打开的文件描述符。服务只接受图片检查/规范化、音视频探测和固定缩略图操作，不接受路径或任意命令；文件必须归属API用户，实际解码在只读、无网络命名空间中降权执行。此服务不持有模型、数据库或BOSS凭据。

2026-10-31 已实际完成部署、故障接管、取消与 stop/start 验收，详见 [部署报告](cluster-deployment-report.md)。

## 可观测性后续要求

目前 journal、数据库事件及执行记录属于基础排障能力。统一关联、诊断入口、集中检索、告警及留存策略已按 [P0 可观测性专项](observability-design.md)补齐，实际验收见 [专项报告](observability-implementation-report.md)。

观测服务随新版部署接入，业务停止时保留运行。诊断、日志、指标、告警和维护说明见 [可观测性操作手册](observability-operations.md)。
