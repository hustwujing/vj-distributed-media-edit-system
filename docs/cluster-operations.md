# 单机多实例部署与操作

部署拓扑：两个API、两个独立执行节点（各自reaper）、专用PostgreSQL、共享加密BOSS，以及每个API配套的固定媒体解码服务。入口仍为 `/video-use/`；内部Nginx在127.0.0.1:18765分流至18766/18767。PostgreSQL专用端口15432，不修改系统原有5432集群。

## 首次部署准备

此脚本面向已准备好的 Linux/systemd 主机，不是从空白系统一键安装。先按 [分布式配置](distributed-execution-configuration.md) 准备私有配置，按 [隔离运行时说明](execution-isolation-and-recovery.md) 准备系统依赖、Codex 可执行文件、服务账号与权限，再同步依赖：

```bash
uv sync --locked --extra web --extra web-test
```

`deploy/cluster.py` 当前固定使用 PostgreSQL 16 工具目录、本文端口、UID 范围、GPU 候选和本机模型路径。部署到其他机器前需逐项调整；不要直接套用历史机器配置。

部署还会同步共享示例库：首次运行需要 `workspace/demos/catalog.json` 及其引用文件，或数据库中已有已发布的共享示例库。示例媒体不随 Git 仓库提供；两者均缺失时部署会失败。同步不会自动下载素材或重新生成示例。

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

`deploy` 会冻结当前源码到 `.cluster/releases/<源码指纹>/`，复制工作区 `.venv` 为按 `uv.lock` 标识的 `.cluster/venvs/` 依赖快照，对冻结版本运行完整 Python 测试；同版本已验证时复用结果。因此部署前需先同步工作区依赖。服务从该快照运行，工作区继续编辑不会混入当前运行代码。不能用定向测试冒充完整回归；前端测试和真实浏览器验收需单独运行。也可先自行验证：

```bash
VIDEO_USE_TEST_REAL_MODELS=1 .venv/bin/python -m tests.run_distributed_suite
node --test tests/*.test.mjs
./deploy.sh deploy
```

此部署使用专用 PostgreSQL，不迁移旧 SQLite 项目。旧 `workspace/` 数据保留并备份，新界面不会自动显示旧项目。脚本不会删除或重建已有集群数据库，也不会删除 BOSS 成果。`check` 在存在发布记录时检查已部署版本，否则检查当前工作区；它核对测试报告和私有配置，不重新运行测试，也不能替代服务健康检查。

## 实例与隔离

| 实例 | 本地目录 | 服务 |
| --- | --- | --- |
| API A | `.cluster/api-a` | video-use-api@a |
| API B | `.cluster/api-b` | video-use-api@b |
| 执行节点 A | `.cluster/node-a`、`/data2/wujing03/vu-exec/a` | video-use-executor@a、video-use-watchdog@a |
| 执行节点 B | `.cluster/node-b`、`/data2/wujing03/vu-exec/b` | video-use-executor@b、video-use-watchdog@b |
| PostgreSQL | `.cluster/postgres` | video-use-db |
| 固定媒体解码 | `/run/vu-media-a`、`/run/vu-media-b` | video-use-media@a、video-use-media@b |

节点UID分别使用700000–700063和700100–700163。GPU候选默认分别为0和3，可在私有 `.distributed.env` 中通过 `VIDEO_USE_NODE_A_GPU_DEVICES` 和 `VIDEO_USE_NODE_B_GPU_DEVICES` 设置逗号分隔的设备编号；空字符串禁用该节点的 GPU。重新部署后生效，仍使用既有共享GPU租约目录及空闲显存/利用率检查，不绕过其他任务的 GPU 锁。每个执行节点初始并发1，两节点可并行。systemd挂载策略禁止节点直接读取对方的目录。

私有共享数据库/会话配置在 `.distributed.env`，BOSS和文件加密配置在 `.storage.env`；均为0600、被gitignore。systemd读取这些配置，密钥不写入unit正文或命令行。各实例配置由脚本生成到 `/etc/video-use-cluster/`。更改固定拓扑、端口、UID/GPU候选需修改 `deploy/cluster.py` 并重新验证。

## 验收与维护

`/data2/wujing03/video-use/.cluster/releases/<已部署版本>/.venv/bin/python deploy/cluster_smoke.py` 对专用测试项目执行实际API切换、强杀节点A、删除该测试项目在A的缓存、节点B恢复与分支汇合、最终Agent验收和跨API取消。它会临时停止实例，仅在维护/验收窗口运行；不调用图片或视频生成服务，最终验收会调用配置的Agent模型。

日志例子：

```bash
journalctl --namespace=video-use -u video-use-executor@a -n 100 --no-pager
journalctl --namespace=video-use -u video-use-watchdog@b -n 100 --no-pager
```

脚本备份保存在 `.cluster/backups/cluster-时间戳/`，包含源码、私有配置、旧SQLite备份；已运行集群还保存专用PostgreSQL的pg_dump。恢复数据库必须另行确认备份之后的数据处理，脚本不会自动覆盖新数据。

这是单机的分布式协议和多实例部署，整台宿主机故障仍会同时影响所有实例。BOSS是外部共享存储，PostgreSQL在本机；不宣称跨机容灾。

宿主机禁止非特权用户命名空间时，API仍以video-use用户运行，通过Unix socket向固定解码服务传递已打开的文件描述符。服务只接受图片检查/规范化、音视频探测和固定缩略图操作，不接受路径或任意命令；文件必须归属API用户，实际解码在只读、无网络命名空间中降权执行。此服务不持有模型、数据库或BOSS凭据。

2026-10-31 的部署、故障接管、取消与 stop/start 验收记录保存在本地 `docs/cluster-deployment-report.md`，不随源码发布，也不作为当前服务状态的证明。

## 可观测性后续要求

目前 journal、数据库事件及执行记录属于基础排障能力。统一关联、诊断入口、集中检索、告警及留存策略已按 [P0 可观测性专项](observability-design.md)补齐，历史验收保存在本地 `docs/observability-implementation-report.md`。

观测服务随新版部署接入，业务停止时保留运行。诊断、日志、指标、告警和维护说明见 [可观测性操作手册](observability-operations.md)。

## 数据盘部署布局（2026-11-02）

当前工作仓库为 `/data2/wujing03/video-use`。源码冻结版本、依赖快照、数据库、节点缓存、
观测数据、发布备份及测试临时目录分别位于 `.cluster/releases`、`.cluster/venvs`、
`.cluster/postgres`、`.cluster/node-*`、`.cluster/observability`、`.cluster/backups`、`/data2/wujing03/vu-tmp`。
部署验证显式设置 TMPDIR 为数据盘上的测试目录，避免系统盘容量影响上传等测试。

执行器登记和实际工作区位于 `/data2/wujing03/vu-exec/{a,b}`，与仓库同级。
这里使用短目录名是为了给每次尝试的 Unix socket 留出 Linux 路径长度余量；不要任意加深目录层级。
生产配置使用 `VIDEO_USE_WORKSPACE_STORAGE=disk`，每次尝试预分配 ext4 映像，继续使用既有硬容量限制、
租约、身份隔离和独立回收器。API、媒体解码与观察服务的临时文件也位于相应数据盘目录。
`/run` 保留进程通信 socket，cgroup 仍由宿主内核管理。

迁移没有创建旧路径兼容链接。现有服务的迁移 drop-in 将旧项目、旧发布根和旧执行器目录设为不可访问，
用于验证新部署无旧路径依赖。旧源码目录由用户另行删除；历史测试挂载在确认没有使用者并复制后卸载。
备份与脱敏迁移证据在 `/data2/wujing03/video-use-storage/migrations/20261102-data2`，
实际图片处理已验证新路径的 ext4 挂载、输出像素和测试项目清理。
