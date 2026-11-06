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

容量模型需分别报告 command-only、混合和 Agent-only 场景，保留宿主 CPU/内存/磁盘余量并限制并发
补池。模型结果只能用于设置真机测试上限，不能声称真实可常驻相同数量。

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
