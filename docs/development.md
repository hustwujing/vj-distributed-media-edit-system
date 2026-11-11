# 开发与测试

本文描述源码和测试入口。本分支的 OpenSandbox 混合执行、独立部署和动态图入口见 [README](../README.md)；旧 Web、`distributed-agent-system` 来源实例及历史集群文档仅供复用模块参考。以下命令均从本仓库根目录执行。

仓库开发规则从 [AGENTS.md](../AGENTS.md) 进入；具体任务按 [专项开发说明索引](development/README.md) 选读对应主题。本文统一维护环境、测试与发布检查，各专项只补充受影响行为及定向验证示例。

## 环境与源码入口

```bash
uv sync --locked --extra web --extra web-test
```

`pyproject.toml` 声明 Python 最低版本和依赖，`uv.lock` 固定依赖解析结果；部署环境还需满足系统工具和模型运行时要求。`animations` 是可选依赖组。辅助脚本通过 `.venv/bin/python helpers/<name>.py` 调用，没有安装后的 console script。

前端为 `webapp/static/` 下的原生 JavaScript 模块、HTML 和 CSS，无 npm 构建步骤。JavaScript 回归使用 Node.js 内置测试运行器；浏览器驱动还需准备相应浏览器及其脚本使用的依赖。

| 入口 | 负责内容 |
| --- | --- |
| `webapp/app.py`、`agent_api.py`、`image_api.py` | Web 页面、项目归属、素材、聊天与图片接口 |
| `webapp/api_execution.py`、`api_staging.py`、`api_maintenance.py`、`api_capacity.py` | API 有界依赖执行器、请求准入、暂存提交、清理与容量观测；修改按 [API 并发与耗时隔离](development/change-api-concurrency.md) |
| `webapp/light_conversation.py`、`agent_context.py` | 轻量对话、项目上下文和沙盒提示 |
| `webapp/plan_readiness.py`、`workspace_state.py` | 方案就绪、内容版本与确认依据 |
| `webapp/agent_host.py`、`scheduling.py`、`task_leases.py` | 执行器、资源调度及跨节点租约 |
| `webapp/workflow_graph.py`、`workflow_planning.py`、`workflow_orchestrator.py` | 新 DAG 契约、滚动提案、正式状态与验收提交 |
| `webapp/conversation_memory.py`、`workflow_messages.py`、`workflow_conversation.py` | Session 消息账本、token 压缩游标、Manager 连续对话和受控失败重试；规则见[会话记忆与重试路由](development/conversation-memory.md) |
| `webapp/workflow_dispatch.py`、`workflow_executor.py`、`workflow_agents.py` | 调度衔接、执行候选、受限专业 Agent 循环 |
| `webapp/model_selection.py`、`agent_profiles.py`、`static/dynamic_models.js` | 服务端模型预设、端上选择、强度和 Run 冻结配置 |
| `webapp/profile_harnesses.py`、`workflow_model_protocol.py`、`model_tool_protocol.py`、`strict_schema.py` | 专业执行引擎、新命令编译、strict 传输与本地验证 |
| `webapp/model_token_estimate.py`、`model_adapters.py`、`deploy/prepare_tokenizer.py` | 输入预算预估、原始用量回执与离线参考分词资产 |
| `webapp/workflow_runtime.py`、`workflow_inputs.py` | 复用的媒体执行适配器与输入绑定 |
| `webapp/operations.py`、`operation_parameters.py` | 能力目录、参数及受控操作 |
| `webapp/execution_runtime.py`、`execution_reaper.py`、`runtime_image.py` | 本机 bwrap 资源、回收与运行时包 |
| `webapp/sandbox_backends.py`、`opensandbox_backend.py`、`opensandbox_command.py`、`opensandbox_runtime.py` | Agent/Command 后端选择、Attempt 绑定及远程执行 |
| `webapp/runtime_bootstrap.py`、`runtime_daemon.py`、`node_gateway.py`、`runtime/opensandbox/` | Runtime 协议、Gateway grant、Bundle/Profile 和 OCI 镜像；开发边界见[专项规范](development/change-opensandbox-runtime.md) |
| `webapp/artifact_store.py`、`checkpoints.py`、`demo_library.py` | 对象存储、版本恢复和共享示例 |
| `webapp/task_usage.py`、`live_activity.py`、`telemetry.py` | 调用统计、活动与诊断事件 |

应用代码中保留部分 SQLite 兼容路径；当前集群使用 PostgreSQL。不要把开发进程指向已有生产数据目录来验证迁移。

## 常规回归

```bash
.venv/bin/python -m tests.run_distributed_suite
node --test tests/*.test.mjs
```

Python 入口发现全部 `tests/test_*.py`，不是仅运行名称包含 distributed 的测试。入口通过 `tests/isolation.py` 清理服务连接和凭据环境，创建独立目录；分布式用例使用临时真实 PostgreSQL、HTTP S3 协议测试服务、独立进程与节点根目录。以 Linux root 运行时还创建仅环回可用的网络命名空间，不访问线上付费供应商。

测试临时根默认是仓库同级的 `../vu-tmp`（本机为 `/data2/wujing03/vu-tmp`），也可用 `VIDEO_USE_TEST_TMP_ROOT` 指定。不要把媒体、临时 PostgreSQL、运行时镜像或真机证据写入根分区 `/tmp`。

完整隔离验证需要：

- PostgreSQL 的 `initdb` / `pg_ctl` 等工具；测试会创建自己的数据库。
- FFmpeg / ffprobe、Bubblewrap、降权和运行时固定工具，及可用的 Linux 命名空间、cgroup 与独立 UID 范围。
- 需要对应能力的用例所用模型、运行时与权限；缺失时可能跳过或失败，必须查看报告。

OpenSandbox 镜像构建、Bundle/Profile 冻结、离线契约、扫描和真机恢复命令见
[OpenSandbox Runtime 开发规范](development/change-opensandbox-runtime.md)。本地 Docker 镜像、SBOM 或容量模型
不等于部署后真机验收；当前独立实例真实运行 OpenSandbox/gVisor，修改其配置或镜像后必须建立新的
源码、配置、镜像和 Bundle 绑定，不能沿用历史报告。
该规范包含强制镜像重建矩阵。当前共享 base 安装完整 `webapp` wheel，因此任何 `webapp/**` 变化默认
要求重建 `operation-runtime`、Codex、Claude、DeepSeek 四张镜像；只有能证明共享 base digest 不变的
Harness 专属变化，才允许只重建对应单张镜像。

`deploy/prepare-executor.sh` 用于受信任主机准备运行时及访问权限，会修改系统状态，不是普通依赖安装命令。完整要求见 [执行隔离与恢复](execution-isolation-and-recovery.md)。只有安装 Python 依赖或执行 `--help` 不能证明沙盒可运行。

启用本地输入预估还需要依赖环境中的固定参考分词资产；部署准备负责下载、SHA 校验、安装和服务 UID 读取检查。推理时不得隐式下载。仅安装 `tiktoken` 不代表资产已经可用；模型/策略适配与校准要求见 [LLM 接入规范](development/add-llm-provider.md)。

定向排查也使用同一隔离入口：

外部决策卡有独立 Chromium 启动器，不启动应用、数据库或 Worker，也不调用真实模型。Node 绑定与浏览器二进制是两份依赖。本机已将 Playwright 1.62.1 和 playwright-core 1.62.1 放在 `/data2/wujing03/tool-cache/browser-node/node_modules`，工作区忽略的 `node_modules` 链接到该缓存；原共享依赖不删除。新的机器可用 `npm install --prefix <工具缓存> --save-exact playwright@1.62.1` 准备 Node 绑定，并预先安装对应 Chromium。不要把本机缓存路径提交为部署依赖。

```bash
VIDEO_USE_TEST_TMP_ROOT=/data2/wujing03/vu-tmp TMPDIR=/data2/wujing03/t PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m tests.run_browser_external_decisions \
  --node-modules /data2/wujing03/tool-cache/browser-node/node_modules \
  --browser-cache /data2/wujing03/tool-cache/ms-playwright \
  --output /data2/wujing03/vu-tmp/external-decisions-browser.json
```

本机也可省略三个参数，使用工作区 Node 依赖、同级 `tool-cache/ms-playwright` 和独立测试输出目录。启动器清理服务凭据、在 Linux root 下隔离网络，缺依赖时明确失败，不隐式下载。证据注明真实 DOM/点击使用应用代码、命令 API 是替身，不能替代部署后真机 campaign。

```bash
.venv/bin/python -m tests.run_distributed_suite tests.test_conversation_routing
node --test tests/text_stream.test.mjs tests/plan_readiness.test.mjs
```

会话记忆或聊天重试修改至少运行：

```bash
VIDEO_USE_TEST_TMP_ROOT=/data2/wujing03/vu-tmp TMPDIR=/data2/wujing03/t \
VIDEO_USE_TEST_SUITE_LOCK=/data2/wujing03/video-use-test-suite-conversation.lock \
XDG_CACHE_HOME=/data2/wujing03/vu-tmp/cache PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m tests.run_distributed_suite \
  tests.test_conversation_memory tests.test_workflow_messages tests.test_workflow_conversation
```

成果预览修改运行 `node --test tests/preview_type.test.mjs`，并使用真实本地 MP4 执行 `tests/browser_artifact_preview.cjs`。HTML 预览必须同时验证脚本未执行、外部资源请求为零、桌面和窄屏可见以及大页面拖拽有效。PDF、DOCX、XLS/XLSX 预览还要运行 `tests/browser_document_preview.cjs`，验证真实解析、Sheet 切换、错误反馈和下载兜底。

Python 报告写入 `docs/test-evidence/distributed-suite.json`，记录用例选择、源码摘要、失败、错误和跳过原因。定向运行会覆盖该报告，不能作为全量发布证明；测试期间相关源码变化也会使报告不通过。测试数量随代码演进，以当次报告为准。

## 本地模型、浏览器与外部服务

```bash
# 开启依赖本机模型环境的测试
VIDEO_USE_TEST_REAL_MODELS=1 .venv/bin/python -m tests.run_distributed_suite
```

此开关不等于开启真实外部供应商调用。以下驱动按各自环境和凭证单独准备、单独验收：

| 驱动 | 范围 |
| --- | --- |
| `tests/run_browser_workflow_smoke.py` | 浏览器工作流交互 |
| `tests/run_live_workflow_smoke.py` | 真实 Agent 与工作流 |
| `tests/run_live_image_smoke.py` | 真实图片能力 |
| `tests/run_live_generation_case.py` | 真实生成链路 |
| `tests/run_live_transcribe_smoke.py` | 真实转写 |
| `tests/run_live_boss_storage.py` | 真实对象存储加密、跨节点恢复与测试对象清理 |
| `deploy/sandbox_campaign.sh` → `tests/campaign_suite.py` | 当前独立实例的真机驱动；示例库 44 项及专项 case，逐例审查内部状态、浏览器、Sandbox 和清理 |
| `tests/campaign_model_deployment_smoke.cjs` | 隔离部署上的模型选择、草稿恢复、Run 配置冻结及低预算拦截；不是媒体 case 或模型质量验收 |
| `deploy/sandbox_opensandbox_recovery.sh` | 独立 OpenSandbox 的 orphan、回执丢失和服务中断 cleanup 真机恢复；要求工作流集群空闲 |
| `tests/campaign_model_eval.py`、`campaign_model_eval_report.py` | 冻结模型回复与协议对照实验；不代替部署后业务测试 |

表中旧 smoke 驱动仍可能默认指向来源实例；本分支验收使用 `deploy/sandbox_campaign.sh` 固定隔离入口，旧驱动仅作为实现参考。

修改模型选择与预算时，可定向运行 `tests.test_model_selection`、`tests.test_model_token_estimate`、`tests.test_model_input_count`、`tests.test_dynamic_gateway`、`tests.test_task_usage` 及 `tests/dynamic_models.test.mjs`。真机启动、版本绑定、失败归因和每例进度汇报遵守[真机测试与审查](development/run-live-campaign.md)。暂停中的测试不会因代码门禁通过而自动恢复。

真实服务驱动可能消耗额度或写入外部存储。运行前读取对应驱动的配置和目标，使用独立测试项目；常规协议测试不能证明真实服务的质量、取消能力或网络表现。需要停服或故障注入的当前验收统一通过 `tests/campaign_suite.py` 执行，并遵守[真机测试与审查](development/run-live-campaign.md)。

## 发布与文档检查

`deploy/sandbox_cluster.sh deploy` 把源码冻结到 `.sandbox-cluster/releases/<源码指纹>/`，在独立可读副本运行 Python 与前端门禁并核对报告；真实浏览器验收由 `deploy/sandbox_campaign.sh` 在部署后运行。`restart` 只重启已部署版本，发布工作区改动必须使用 `deploy`。`targeted_only` release 只能做定向复测；完整 campaign 和发布结论要求同一源码/配置/镜像/Bundle 的 full gate。

文档更新应核对链接、命令、配置名和当前代码；没有修改代码时，无需仅为文案重跑付费调用或整套模型测试。涉及示例交付、状态或权限变化时，选择对应回归并按发布要求完成全量检查。

历史结果集中在 [文档索引](README.md)的验收记录。新报告应注明验证版本、范围及限制，避免把历史通过结果写成当前工作区已通过。

## 本机共享 Redis 存储

本机 Redis 的数据与文件日志位于 `/data2/wujing03/redis/data` 和 `/data2/wujing03/redis/logs`，端口仍为6379。服务的systemd读写白名单和日志轮转已随目录迁移。业务仍通过各自Redis key前缀隔离。

共享依赖也必须检查实际落盘目录及所在文件系统，不能仅检查业务命名空间。迁移需保留原配置、数据及权限，验证临时TTL键跨重启恢复、PING与新目录持久化成功，再归档旧文件；不通过关闭持久化错误保护掩盖磁盘不足。共享服务停启须有明确授权，并记录依赖应用恢复情况。
