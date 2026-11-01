# vj-distributed-media-edit-system

支持对话式音视频、图片编辑的分布式媒体处理系统。用户可以与 Agent 讨论方案并确认执行，也可以通过工具面板或图片工作台直接提交处理任务。

## 核心能力

- **对话与执行**：轻量对话、素材分析、方案确认、隔离沙盒执行，以及实时正文和工具进度。
- **持久化工作流**：按依赖执行 `script` / `operation` 步骤，保存检查点，支持重试、取消、质量验收和故障恢复。
- **媒体处理**：转写、剪辑、字幕、调色、图片编辑与抠图；通过受控适配器接入文生图、文生视频和图生视频服务。
- **分布式调度**：PostgreSQL 保存任务、租约与执行状态；多个执行节点共享加密对象存储，Redis 支持实时事件传输。
- **可观测性**：关联任务、步骤、模型及工具调用，展示耗时、Token、重试与调用统计，提供日志、指标和诊断入口。

## 系统结构

```text
浏览器 → API → PostgreSQL 任务队列 → 调度器 / 执行节点
                │                        ├─ Agent 沙盒
                │                        ├─ 本地脚本沙盒
                │                        └─ 受控媒体 / 外部服务适配器
                ├─ Redis 实时事件
                └─ 共享对象存储：素材、产物、加密检查点
```

调度任务分为 `agent`、`manual`、`workflow`；Agent 使用 `plan` / `execute` 模式。持久化工作流由结构化步骤组成，外部异步调用由适配器提交、查询和收取结果。

## 安装与配置

```bash
git clone https://github.com/hustwujing/vj-distributed-media-edit-system.git
cd vj-distributed-media-edit-system
uv sync --locked --extra web --extra web-test
cp .env.example .env
```

服务端执行环境需要 Linux、FFmpeg / ffprobe、PostgreSQL、S3 兼容对象存储，以及隔离执行所需的系统依赖与权限。部分图片能力需要 GPU 和模型环境；外部生成服务需要自行配置地址、模型及凭证。

- [分布式配置](docs/distributed-execution-configuration.md)：数据库、对象存储、加密密钥与节点参数。
- [执行隔离与恢复](docs/execution-isolation-and-recovery.md)：沙盒、运行时和回收器。
- [部署操作手册](docs/cluster-operations.md)：单机多实例拓扑及完整部署步骤。
- [可观测性操作手册](docs/observability-operations.md)：日志、诊断、指标与告警。
- [独立 Agent 技能安装](install.md)：将编辑能力挂载到本地 Agent。

`deploy.sh` 封装了部署、启动、停止和检查。当前部署拓扑使用 systemd，首次部署前应按手册准备运行环境、私有配置并检查节点、端口、UID 和 GPU 配置。

```bash
./deploy.sh status
./deploy.sh deploy
./deploy.sh stop
./deploy.sh start
```

项目名和 Python 包名为 `vj-distributed-media-edit-system`。现有 `VIDEO_USE_*` 环境变量、`video-use-*` 服务名、`/video-use/` URL 前缀及沙盒内路径仍是运行时配置接口，不随仓库更名自动迁移。

## 测试

```bash
# 集成测试使用独立数据库、对象存储协议测试服务和沙盒
.venv/bin/python -m tests.run_distributed_suite

# 前端状态、流式正文与用量统计测试
node --test tests/*.test.mjs
```

完整环境验证可设置 `VIDEO_USE_TEST_REAL_MODELS=1`。隔离测试需要相应系统依赖及权限；真实模型和外部服务测试还需要独立配置，不能仅凭源码克隆完成。生产部署脚本对冻结的源码版本执行完整测试。

## 目录与设计文档

| 目录 | 内容 |
| --- | --- |
| `webapp/` | API、调度、执行器、工作流、适配器及前端 |
| `helpers/` | 媒体处理脚本 |
| `runtime/public/` | 沙盒公开运行时与技能说明 |
| `deploy/` | 集群部署、运行环境与观测工具 |
| `tests/` | 单元、集成、故障及真实场景测试驱动 |
| `docs/` | 技术设计、接口约定和操作手册 |
| `references/` | Agent 使用的编辑及工作流参考 |

设计入口：[工作流平台](docs/workflow-platform-design.md)、[统一生成接口](docs/uniform-generation-design.md)、[能力接入](docs/operation-extension-guide.md)、[消耗统计](docs/task-usage-design.md)、[60 条场景设计](docs/real-case-campaign/design.md)。

凭证、运行数据库、用户素材、检查点、日志和真实用户测试记录不纳入版本控制。历史文档中的本机路径、服务地址及部署记录仅供实现背景参考，部署时请使用自己的配置；部分验收文档引用的本地证据不会随仓库发布。

## 来源与许可证

本项目基于 [browser-use/video-use](https://github.com/browser-use/video-use) 扩展，保留原项目的 MIT 许可证与版权声明，见 [LICENSE](LICENSE)。字体的许可证见 [webapp/static/fonts/LICENSE-wqy.txt](webapp/static/fonts/LICENSE-wqy.txt)。
