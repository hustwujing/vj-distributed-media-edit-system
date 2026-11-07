# OpenSandbox Runtime 开发与发布准备

适用于修改 OpenSandbox/gVisor 后端、Runtime Daemon、Agent/Command OCI 镜像、Skill Bundle、
Profile、Node Capability Gateway grant、远程清理，以及在不部署的前提下准备本地发布证据。
调度、租约、检查点和跨节点成果仍按[分布式执行开发说明](change-distributed-execution.md)；
真实环境测试按[真机测试与审查](run-live-campaign.md)。

## 1. 当前阶段与默认边界

当前开发分支已具备 OpenSandbox Agent/Command 传输、四张本地 Runtime 镜像、冻结 Bundle/Profile、
离线契约检查和本地安全报告，但远程 factory 默认关闭，尚未完成数据库持久化绑定、registry 发布、
生产 Gateway/TLS、真实 gVisor 沙箱、多机恢复和 Watchdog 故障演练。

除非用户本轮明确授权对应动作，默认只能执行开发工作区和本地 Docker 镜像内的可逆操作：

| 默认允许 | 需要新授权 |
| --- | --- |
| 修改源码、Dockerfile、测试和文档 | 修改或重建任何现有业务数据库 |
| 构建本地、未部署的镜像 | 推送镜像、签名或写 registry |
| `docker run --rm` 离线契约检查 | 修改 Docker daemon、runsc、网络、防火墙或 Nginx |
| 读取本地镜像并生成 SBOM/漏洞报告 | 启动 OpenSandbox 真沙箱或改变现有服务配置 |
| 使用隔离临时数据库运行开发回归 | 部署、重启、停服或执行节点故障注入 |
| 纯计算容量模型和 Profile dry-run | 真实 30 沙箱压测、多机演练或真实模型调用 |

“继续官方部署”“开始真机验收”等指令只授权其明确范围，不自动授权数据库重建、宿主重启、
防火墙变更或生产流量切换。无法在上述默认范围内继续获得新证据时，应报告停止点并等待授权；
不要用更多 mock、文档勾选或重复静态扫描冒充下一阶段进展。

## 2. 实现与构建入口

| 范围 | 入口 |
| --- | --- |
| Backend 路由和 Lifecycle | `webapp/sandbox_backends.py`、`opensandbox_backend.py`、`opensandbox_command.py` |
| Runtime 控制协议 | `runtime_protocol.py`、`runtime_client.py`、`runtime_daemon.py`、`runtime_bootstrap.py` |
| Harness 与 Workspace | `harness_adapters.py`、`harness_bridge.py`、`runtime_workspace.py`、`runtime_injection.py` |
| Gateway grant | `node_gateway.py`、`node_gateway_service.py`、`runtime_gateway.py`、`gateway_business.py` |
| OCI 定义 | `runtime/opensandbox/*.Dockerfile`、`deploy/build-agent-runtime.sh` |
| Bundle/Profile 冻结 | `runtime/public/bundle.json`、`runtime/opensandbox/release-spec.json` |
| 本地发布证据 | `deploy/runtime_release.py`、`verify_runtime_images.py`、`runtime_security_report.py` |

四张业务镜像职责固定：`operation-runtime` 承载 CPU Command Runtime；Codex、Claude、DeepSeek
镜像分别增加一个 Harness。GPU、外部 Provider、manual 任务和未声明为 `opensandbox_command` 的
Operation 不得因为镜像可用而自动迁移。

## 3. 镜像与 Bundle 规则

- foundation 是唯一允许联网安装系统包的构建阶段；业务 Runtime 层使用本地 wheelhouse 离线安装。
- 上游 Python、foundation、Runtime base 和正式发布镜像必须使用不可变 digest。本地链式构建可用
  Docker image ID，但它不能写入多机 Profile 或被称为 registry digest。
- 使用多阶段构建，禁止把 wheelhouse、安装缓存、扫描数据库和构建工具留在最终镜像层。
- Runtime 用户固定为 `10001:10001`；根文件系统按只读设计，仅 `/workspace`、`/state` 和
  `/run/video-use` 是 Attempt 私有可写目录。
- 镜像不得包含 Provider、数据库、Redis、对象存储长期凭据，Gateway 临时 Token、用户素材、
  Checkpoint、Docker Socket、宿主私有 Skill 或 GPU 设备。
- Skill Bundle 只包含 `release-spec.json` 明确列出的公开 references 和 helper。禁止复制整个
  `helpers/`、`references/`、`~/.codex/skills` 或 `~/.updream_skills`。
- 修改 Bundle 成员后先运行 `python deploy/runtime_release.py --freeze-bundle`，提交更新后的
  `bundle.json`；Runtime Bootstrap 必须在激活控制端点前逐文件校验路径、大小和 SHA-256。
- Helper/Skill 声明的路径必须实际存在于镜像并由 UID 10001 可读；仅校验摘要而不执行代表性 helper
  不足以证明 Bundle 可用。

### 3.1 变更路径与强制重建矩阵

是否重建以“当前源码相对已部署镜像所记录的源码 commit”做 diff，不能只看最近一次 commit，也不能因为
控制面测试通过就继续使用旧镜像。下表是**完整发布**的强制矩阵。当前 `base.Dockerfile` 会安装完整
`webapp` wheel，因此现阶段 `webapp/**` 的任何变化都会改变共享 Runtime base，即使文件看起来只服务于控制面。

| 发生变化的路径或输入 | 必须重建 |
| --- | --- |
| `runtime/opensandbox/foundation.Dockerfile`、Python base digest、Debian 包源或系统包集合 | foundation，然后下列四张业务镜像全部重建 |
| `runtime/opensandbox/base.Dockerfile`、`pyproject.toml`、`uv.lock`、`wheels/**`、`webapp/**` | `operation-runtime`、Codex、Claude、DeepSeek 四张全部重建 |
| `.dockerignore` 中影响 Runtime context 的规则 | 按受影响输入处理；无法证明只影响单张时四张全部重建 |
| `runtime/public/**`、`references/**`、release spec 中的 Bundle 成员、被复制的 `helpers/*.py` | 更新并冻结 Bundle，四张全部重建 |
| `runtime/opensandbox/codex.Dockerfile`、`vendor/codex` | 只重建 Codex；共享 base digest 不得变化 |
| `runtime/opensandbox/claude.Dockerfile` 或 Claude SDK/Harness 专属 wheel/version | 只重建 Claude；共享 base digest 不得变化 |
| `runtime/opensandbox/deepseek.Dockerfile` 或 DeepSeek Harness 专属 wheel/version | 只重建 DeepSeek；共享 base digest 不得变化 |
| `runtime/opensandbox/release-spec.json` 中仅发布名、路由或容量元数据变化，且 Bundle 文件集合和镜像输入未变 | 不重建镜像，但必须重生成 release manifest 并重新验证 Profile/Gateway 契约 |
| `deploy/**`、`tests/**`、`docs/**` 且不影响 Dockerfile、构建上下文、Bundle 或镜像验证输入 | 不重建镜像；运行对应控制面或测试回归 |

修改 `webapp/opensandbox_command.py`、`workflow_io.py`、`worker.py`、`media_handlers.py` 或任何沙箱内
会 import 的模块，既要部署宿主 Executor 代码，也要重建四张共享-base 镜像。宿主与镜像混用不同源码
版本属于发布错误，不能靠重试或回退本机执行掩盖。

定向开发复测允许只构建当前实际启用、被测且受影响的镜像。例如 Agent backend 仍为 `local_bwrap` 时，
Command Runtime 修复可以只构建和推送 `operation-runtime`。这种部署必须标记 `targeted_only`，记录其余
三张 Agent 镜像尚未基于新 base 重建，不得生成完整 release manifest、跑完整 campaign 或宣称四镜像
发布完成。进入最终 full-gate 发布前，仍必须按上表补齐四张镜像并统一验证。

### 3.2 镜像发布顺序

1. 停止使用旧源码继续跑真机 case；确认没有活动 sandbox 依赖待替换 tag。
2. 更新 `release-spec.json` 的 Bundle 文件集合，运行 `runtime_release.py --freeze-bundle` 并提交
   `runtime/public/bundle.json`。
3. 定向复测只构建当前被测镜像；最终发布按上表构建 foundation/base/Harness，共享 base 变化时四张
   必须来自同一个 base image ID。
4. 运行 `test_runtime_images`、`test_runtime_bootstrap` 及受影响 Runtime 单测，再执行四张镜像的离线
   `verify_runtime_images.py`。门禁必须实际 import 或执行新增 helper，不能只检查文件存在。
5. 先提交源码，再用该 commit 标记镜像；推送到隔离 registry，记录四个 registry manifest digest。
6. 配置只能写 `image@sha256:<manifest>` 和对应 Bundle digest，禁止 `latest`、裸 tag、本地 image ID，
   也禁止新的 operation 镜像搭配旧 Agent base。
7. 先用 `targeted_only` 部署跑一个受影响的真实 Command/Agent case，核对 gVisor、Attempt、日志、产物
   与 DELETE 404；稳定后再做一次 full-gate 部署和完整 campaign。

发布证据至少记录：源码 commit、dirty 状态、foundation/base image ID、四个 registry digest、Bundle digest、
构建命令、离线契约结果和真机 case ID。缺少任一项时只能称为本地构建，不能称为已发布 Runtime。

## 4. 本地离线验证

本地证据写入被忽略的 `runtime/build/`，临时目录和扫描缓存必须位于 `/data2`，不得隐式使用根分区
`/tmp`。推荐顺序：

```bash
python deploy/runtime_release.py --freeze-bundle
DOCKER=/path/to/docker python deploy/runtime_release.py --max-resident 30
DOCKER=/path/to/docker python deploy/verify_runtime_images.py
python deploy/runtime_security_report.py
```

镜像内容变化后必须按新 image ID 重新生成 SBOM、漏洞报告和契约报告。契约检查至少覆盖：断网、
只读根文件系统、零 capability、`no-new-privileges`、UID/GID、三个可写目录、Bootstrap、Harness
版本、FFmpeg、Bundle digest、reference 可读和一个白名单 helper 实际执行。

Release manifest 必须记录源码 commit/dirty 状态、上游 digest、OS 包版本、wheel 和 Harness 摘要、
本地 image ID、Profile/模型路由/Gateway 能力及容量模型。只有 `dirty=false` 的清单可作为提交对应的
本地证据；它仍不是 registry 发布证明。

漏洞扫描保留原始结果和扫描器/数据库时间。Critical/High 数量、可修复版本、基础发行版口径、
OpenCV 等依赖内嵌二进制和许可证未知项必须单列审查；“完成扫描”不等于“允许发布”。

## 5. 回归与故障契约

定向测试至少覆盖 Runtime、OpenSandbox、Gateway、租约和镜像发布工具。改动触及共享 Lease、Host
cleanup 或 Workspace 时按分布式规范追加对应模块，并完成全量非真机套件。开发 worktree 复用本地
`.venv` 或 bwrap runtime 资产时，它们只能是 Git 忽略的测试准备，不能写成部署依赖或提交绝对路径。

续租失败和清理顺序固定为：撤销 Gateway grant 并关闭活跃连接，quiesce Harness，删除指定
Sandbox ID，按 Attempt metadata 确认消失，最后才释放资源。`begin_cleanup` 和 `cleanup` 必须幂等；
撤销、删除或确认失败保留重试/隔离状态，不能因为业务终态或 Lease 过期假定物理资源已回收。

远程 Agent 与本地 Agent 共用轮次终态处理：Harness 明确失败、运行超时、导出或检查点失败必须结束
无工作流的轮次，未提交检查点的工作流暂停，不能当作节点中断反复重派。已输出公开文本在失败前封口；
Shutdown、LeaseLost、TaskLimitReached 仍向执行器上层传播，保留接管、取消及总预算终止语义。
租约过期清理在撤销 active 时必须同时保留 interrupted，确保接管方仍执行恢复逻辑，而非按首次执行派发。

Command Runtime 下载不信任远端目录响应：路径必须在声明的输出子目录内，禁止 `..` 和链接/特殊文件；
宿主逐级使用目录 FD 与 `O_NOFOLLOW`，以私有临时文件完成后原子替换。按实际流字节累计执行
`VIDEO_USE_TASK_DISK_BYTES` 下载上限，每次清单最多 50000 条，失败不留下半写文件。
宿主 `command-attempts/<attempt>` 由 Runtime 持有 `.owner` 排他文件锁；节点 watchdog 每轮清理完成后
间隔 60 秒启动单独子进程，分批扫描超过 300 秒的遗留目录，单次子进程超过 30 秒即终止。
只有本地锁可取、无有效租约、无未完成本地 cleanup 且远端 sandbox cleanup 完成时才删除；项目删除后
已无 attempt/cleanup 行的孤儿也可回收。数据库未知时保留，不能由 API 节点扫描代替执行节点回收。

容量模型需分别报告 command-only、混合和 Agent-only 场景，保留宿主 CPU/内存/磁盘余量并限制并发
补池。模型结果只能用于设置真机测试上限，不能声称真实可常驻相同数量。

独立部署的 OpenSandbox cleanup 真机恢复入口为：

```bash
./deploy/sandbox_opensandbox_recovery.sh
```

该驱动必须验证已知 Sandbox ID orphan、create 回执丢失后按 Attempt 查询、Lifecycle 服务暂时不可用后
重试收敛三条路径。运行前要求 `sandbox-agent` 工作流集群空闲；报告写入
`.sandbox-cluster/recovery/opensandbox-recovery.json`。通过标准包括 cleanup 状态 `complete/deleted`、
实际 GET 返回 404、服务恢复 active、测试行删除且 sandbox 列表无残留。它只操作独立部署，不能指向
来源数据库、来源 OpenSandbox 端口或共享生产命名空间。

## 6. 升级到真机阶段

进入下一阶段前，必须明确获得并记录以下隔离资源和授权：

1. 测试镜像 registry、命名空间和签名策略；推送后使用 registry manifest digest 重建 Profile。
2. 独立测试数据库、对象存储前缀、Redis 前缀、节点目录和可清理的测试用户/项目。
3. 独立 OpenSandbox/gVisor 节点、专用 bridge 子网、Gateway TLS/CA、端口与防火墙变更范围。
4. 允许启动的真实沙箱数、CPU/内存/磁盘水位、预热池规模及停止条件。
5. Watchdog、Executor kill、节点重启、数据库断连等故障演练窗口和恢复责任人。

真机阶段按“单个 Command → 单个 Agent → Gateway 模型/工具 → Checkpoint 恢复 → cleanup/故障 →
多节点 → 容量/预热”的顺序扩大。每步保存 registry digest、源码/配置摘要、Attempt/Generation、
Gateway grant、Usage、Checkpoint、Cleanup Receipt 和实际残留资源证据；前一步失败不得通过扩大范围
绕过。

## 7. 完成口径

可称为“本地发布准备完成”的最低条件：四张本地镜像构建成功；Bundle/Profile 冻结；强化契约、
定向及全量非真机测试通过；最新 image ID 有对应 SBOM/漏洞/许可证证据；工作区提交且 release
manifest 为 `dirty=false`。

只有完成 registry digest/签名、独立数据库和 Gateway 接线、至少两台物理节点的真实恢复与清理、
网络隔离、账本、故障演练及批准的容量测试后，才可称为“OpenSandbox 后端完成”或“可上线”。
本地镜像、mock SDK、Docker 容器冒烟、容量计算和历史测试均不能替代这些证据。
