# 开发与测试

本文描述源码和测试入口；Web 操作见 [使用指南](../WEB_UI.md)，实际服务发布见 [集群操作手册](cluster-operations.md)。以下命令均从仓库根目录执行。

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
| `webapp/workflows.py`、`workflow_runtime.py`、`workflow_review.py` | 工作流状态、运行与验收 |
| `webapp/operations.py`、`operation_parameters.py` | 能力目录、参数及受控操作 |
| `webapp/execution_runtime.py`、`execution_reaper.py`、`runtime_image.py` | 沙盒资源、回收与运行时包 |
| `webapp/artifact_store.py`、`checkpoints.py`、`demo_library.py` | 对象存储、版本恢复和共享示例 |
| `webapp/task_usage.py`、`live_activity.py`、`telemetry.py` | 调用统计、活动与诊断事件 |

应用代码中保留部分 SQLite 兼容路径；当前集群使用 PostgreSQL。不要把开发进程指向已有生产数据目录来验证迁移。

## 常规回归

```bash
.venv/bin/python -m tests.run_distributed_suite
node --test tests/*.test.mjs
```

Python 入口发现全部 `tests/test_*.py`，不是仅运行名称包含 distributed 的测试。入口通过 `tests/isolation.py` 清理服务连接和凭据环境，创建独立目录；分布式用例使用临时真实 PostgreSQL、HTTP S3 协议测试服务、独立进程与节点根目录。以 Linux root 运行时还创建仅环回可用的网络命名空间，不访问线上付费供应商。

完整隔离验证需要：

- PostgreSQL 的 `initdb` / `pg_ctl` 等工具；测试会创建自己的数据库。
- FFmpeg / ffprobe、Bubblewrap、降权和运行时固定工具，及可用的 Linux 命名空间、cgroup 与独立 UID 范围。
- 需要对应能力的用例所用模型、运行时与权限；缺失时可能跳过或失败，必须查看报告。

`deploy/prepare-executor.sh` 用于受信任主机准备运行时及访问权限，会修改系统状态，不是普通依赖安装命令。完整要求见 [执行隔离与恢复](execution-isolation-and-recovery.md)。只有安装 Python 依赖或执行 `--help` 不能证明沙盒可运行。

定向排查也使用同一隔离入口：

```bash
.venv/bin/python -m tests.run_distributed_suite tests.test_conversation_routing
node --test tests/text_stream.test.mjs tests/plan_readiness.test.mjs
```

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
| `tests/campaign_*.py`、`tests/campaign_*.cjs` | 专项真实场景与浏览器验收 |

真实服务驱动可能消耗额度或写入外部存储。运行前读取对应驱动的配置和目标，使用独立测试项目；常规协议测试不能证明真实服务的质量、取消能力或网络表现。`deploy/cluster_smoke.py` 会临时停止实例做接管验证，仅在维护窗口运行。

## 发布与文档检查

部署脚本把源码冻结到 `.cluster/releases/<源码指纹>/`，在冻结版本运行完整 Python 测试并核对报告；前端 `node --test` 和真实浏览器验收不由这一门禁自动代跑。部署使用依赖快照，更新前先同步工作区依赖。`./deploy.sh restart` 重启已部署版本，发布工作区改动使用 `./deploy.sh deploy`。

文档更新应核对链接、命令、配置名和当前代码；没有修改代码时，无需仅为文案重跑付费调用或整套模型测试。涉及示例交付、状态或权限变化时，选择对应回归并按发布要求完成全量检查。

历史结果集中在 [文档索引](README.md)的验收记录。新报告应注明验证版本、范围及限制，避免把历史通过结果写成当前工作区已通过。
