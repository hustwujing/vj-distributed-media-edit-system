# Distributed Agent Sandbox Backend

面向对话式媒体处理的统一 Run 系统。本仓库在分布式 Agent 控制面上增加 OpenSandbox/gVisor Command Runtime，并保留本机 bwrap Agent 与 GPU 工具，形成按任务能力选择执行后端的混合架构。

当前开发分支为 `feature/opensandbox-hybrid-execution`。工作区是 `/data2/wujing03/distributed-agent-sandbox-backend`；独立部署入口为 `/distributed-agent-sandbox/`。历史报告只说明其绑定版本，不能替代当前工作区或当前部署验证。

## 架构

```text
浏览器 / API
      │
PostgreSQL 权威状态 ── Run / Business Task / Control Job / Attempt / Lease
      │
Orchestrator ── 规划、屏障、修订、审查、发布、恢复
      │
Executor a/b
  ├─ CPU Command operation ── OpenSandbox + gVisor
  ├─ CPU Agent / specialist ── 本机 bwrap
  ├─ GPU 与固定本机能力 ───── 本机隔离运行时
  └─ 外部 Provider ────────── 按已冻结调用协议执行

BOSS/S3：加密素材、检查点、候选与交付
Redis：带 TTL 的流式事件；数据库仍是权威状态
Observer：关联 Run、Task/Control Job、Attempt、Generation 和 Sandbox
```

- Session 保存不可变消息账本和版本化摘要游标，Run 是执行、授权和用量根。Manager 读取当前权威状态、当前消息、游标后原文和历史摘要；摘要不能授予权限或覆盖当前契约。
- Manager 使用服务端 PlanDraft 和类型化命令；业务 Task 与 PLAN/REVIEW/DIAGNOSE Control Job 分离。
- 输入与反馈写入 `run_inputs`，第一条输入建立派发屏障；已开始任务排空后统一重规划。
- 执行器只提交候选和回执，Orchestrator 校验 revision、generation、验收与审查后发布。
- 失败修复保留逻辑 Task 和不可变 revision；受影响后代重算，无关分支复用。
- 后端绑定、Runtime Profile、镜像 digest、Bundle digest、授权和 Gateway grant 按 Attempt 冻结，不能因重试静默换后端。
- 失败恢复能力由服务端按当前 Run、Graph、Authorization、租约、cleanup 和预算生成。自然语言只能选择当前签发的重试选项，不能凭关键词创建恢复权限。

会话上下文按预估 token 异步压缩。默认累计到 `12000` token 后登记摘要任务，摘要成功提交才推进消息游标，并保留约 `4000` token 的游标后原文；原始消息不删除。阈值可通过 `VIDEO_USE_CONVERSATION_COMPACT_TOKENS` 和 `VIDEO_USE_CONVERSATION_TAIL_TOKENS` 配置，完整约束见[会话记忆与重试路由](docs/development/conversation-memory.md)。

## 混合执行边界

当前独立部署的实际配置是：

| 工作类型 | 当前后端 | 说明 |
| --- | --- | --- |
| 支持的 CPU Command operation | OpenSandbox Command Runtime | gVisor、只读根文件系统、Attempt 私有目录、不可变镜像与 Bundle |
| CPU Agent，包括 Editing Implementer | OpenSandbox Agent Runtime | 按用户冻结配置选择 Codex、Claude 或 DeepSeek Harness；页面显示真实执行位置 |
| Manager、Reviewer、Diagnostician | Agent Worker 宿主控制任务 | 不伪装成业务 Task，也不宣称运行在 OpenSandbox |
| GPU operation | 本机隔离运行时 | 不因 OpenSandbox 可用而自动迁移 |
| 外部 Provider operation | 已登记 Provider 协议 | 保存调用意图、外部句柄、恢复和清理状态 |

当前部署已启用 Codex、Claude 和 DeepSeek 三种远程 Agent Runtime。GPU Agent 与未声明远程能力的任务保留本机隔离路径；不得把 Command Runtime 的验证结果外推为所有 Harness 均已通过真实模型业务验收。

OpenSandbox cleanup 顺序为：撤销 Gateway grant、停止 Harness、删除指定 Sandbox、确认实例消失，最后释放本地资源。创建回执丢失时按 Attempt 查询；未知提交和未知实例不得靠重复创建探测。

## 独立部署

本仓库只管理 `sandbox-agent` 实例，不操作来源 `distributed-agent-system`：

| 资源 | 独立值 |
| --- | --- |
| systemd 前缀 | `sandbox-agent-` |
| Web 入口 | `/distributed-agent-sandbox/` |
| API / proxy / observer | `20766, 20767` / `20765` / `20768` |
| PostgreSQL | `distributed_agent_sandbox` |
| Redis 前缀 | `distributed-agent-sandbox:events:` |
| BOSS 前缀 | `distributed-agent-sandbox` |
| 配置目录 | `/etc/distributed-agent-sandbox-backend` |
| 部署与证据 | `.sandbox-cluster/` |
| Executor 根目录 | `/data2/wujing03/das-sandbox-exec` |
| OpenSandbox API | `127.0.0.1:28090`，专用 Docker 网络与 gVisor `runsc` |

```bash
./deploy/sandbox_cluster.sh status
./deploy/sandbox_cluster.sh deploy
./deploy/sandbox_cluster.sh check
./deploy/sandbox_cluster.sh restart
```

`deploy` 冻结源码、私有基础配置和 OpenSandbox/API/Node/Orchestrator 派生配置，运行门禁、备份专用数据库并切换服务。systemd unit 只读取 release 对应的配置快照，使镜像 digest 与源码一起回滚；`restart` 只重启已部署 release。`targeted_only` 版本只允许定向复测，不能用于完整 campaign 或发布结论。

真机驱动固定指向本实例：

```bash
./deploy/sandbox_campaign.sh list
./deploy/sandbox_campaign.sh run --cases C06,R01,R02 --jobs 3 --order risk
./deploy/sandbox_campaign.sh report

# 要求工作流集群空闲；覆盖 orphan、create 回执丢失和服务中断恢复
./deploy/sandbox_opensandbox_recovery.sh
```

共享 PostgreSQL、Redis 和 BOSS 服务只表示复用基础设施进程；数据库、角色、key、对象前缀、Cookie、密钥和证据目录均独立。部署或测试不得写来源实例。

## Runtime 镜像

`runtime/opensandbox/` 定义 foundation、共享 base、Command Runtime 和 Codex/Claude/DeepSeek Agent Runtime。运行配置只接受 registry manifest digest；裸 tag 和本地 image ID 只能用于本机构建链，不能成为正式 Profile。

当前 `base.Dockerfile` 安装完整 `webapp` wheel，因此 `webapp/**` 变化默认要求四张业务镜像统一重建。定向复测可以只重建实际启用且受影响的镜像，但必须标记 `targeted_only`；进入 full gate 前仍要补齐统一 base、四镜像、Bundle、离线契约和扫描证据。

```bash
python deploy/runtime_release.py --freeze-bundle
DOCKER=/path/to/docker python deploy/runtime_release.py --max-resident 30
DOCKER=/path/to/docker python deploy/verify_runtime_images.py
python deploy/runtime_security_report.py
```

镜像不得包含长期 Provider、数据库、Redis、对象存储或模型凭据。Attempt 临时配置和短期 Gateway token 注入 `/run/video-use`；Runtime 用户固定为 `10001:10001`。完整规则见 [OpenSandbox Runtime 开发规范](docs/development/change-opensandbox-runtime.md)。

## 模型与审查

专家角色模型由服务端配置并按 Run 冻结，用户页面不能覆盖。页面选择只控制沙盒 Implementer 的 Harness、模型和推理强度。模型选择不改变角色权限、工具策略、Workspace Policy 或业务授权。

结构化输出采用 strict 优先、显式不支持时有界回退；本地 Schema、语义、权限和授权校验始终执行。Reviewer 的通过必须与 inspection、候选选择和方法使用证据一致；审查 JSON 不是交付产物。实际交付集合由当前有效 revision、用户目标和替代关系确定，不能因文件同名或历史 delivery 标记丢失兄弟产物。

成果预览按可信 MIME 或文件名识别视频、音频、图片、PDF、HTML 和文本。HTML 使用无脚本 sandbox 与严格 CSP，只允许内联样式及 `data/blob` 媒体，不允许预览内容访问网络；大页面在独立 viewport 中滚动或拖拽查看。

## 开发与测试

```bash
uv sync --locked --extra web --extra web-test

VIDEO_USE_TEST_TMP_ROOT=/data2/wujing03/vu-tmp \
TMPDIR=/data2/wujing03/t \
VIDEO_USE_TEST_SUITE_LOCK=/data2/wujing03/video-use-test-suite-0.lock \
XDG_CACHE_HOME=/data2/wujing03/vu-tmp/cache \
PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m tests.run_distributed_suite

node --test tests/*.test.mjs
```

Python 测试必须走 `tests.run_distributed_suite`，以获得独立 PostgreSQL、S3 协议服务、环境清理和报告；不要直接用 `unittest` 绕过隔离。定向测试在命令末尾追加模块名。临时文件、缓存、锁和媒体证据放在 `/data2/wujing03`，不要写根分区 `/tmp`。

当前示例库真机清单为 44 项。最新完整轮和后续定向复测见 [示例库真机报告](docs/real-case-campaign/demo-library-test-report.md)；OpenSandbox 专项历史见 [部署后审查记录](docs/real-case-campaign/opensandbox-post-review-live.md)。系统 `SUCCEEDED` 不能替代实际产物、浏览器、数据库、Redis、BOSS、Sandbox 和 cleanup 审查。

## 开发规范

从 [AGENTS.md](AGENTS.md)、[开发与测试](docs/development.md)和[专项规范索引](docs/development/README.md)进入。常用专题：

- [OpenSandbox Runtime](docs/development/change-opensandbox-runtime.md)
- [分布式执行](docs/development/change-distributed-execution.md)
- [动态工作流](docs/development/change-dynamic-workflow.md)
- [会话记忆与重试路由](docs/development/conversation-memory.md)
- [前端与产物展示](docs/development/change-ui-artifacts.md)
- [operation](docs/development/add-operation.md)
- [专业 Agent](docs/development/add-specialist-agent.md)
- [Harness](docs/development/add-profile-harness.md)
- [LLM Provider](docs/development/add-llm-provider.md)
- [真机测试与审查](docs/development/run-live-campaign.md)

`webapp/` 是控制面、执行器和页面；`runtime/` 是公开 Bundle 与 OCI Runtime；`deploy/` 管理独立部署和发布证据；`tests/` 包含单元、集成、浏览器和真机驱动；`docs/` 保存规范与绑定版本的报告。

## 来源与许可证

项目基于 [browser-use/video-use](https://github.com/browser-use/video-use) 扩展，保留 MIT 许可证与版权声明，见 [LICENSE](LICENSE)。Python 包名仍为 `vj-distributed-media-edit-system`，环境变量保留 `VIDEO_USE_*` 前缀。
