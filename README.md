# vj-distributed-media-edit-system

通过对话、工具面板和图片画布编辑音视频与图片的分布式媒体处理系统。Agent 可以读取素材、讨论方案、生成样式预览，并在用户确认后执行、验收和交付；多阶段任务通过持久化工作流保存进度、恢复执行。

[网页使用](WEB_UI.md) · [部署运维](docs/cluster-operations.md) · [开发与测试](docs/development.md) · [AI 开发规范](AGENTS.md) · [文档索引](docs/README.md)

## 核心能力

- **对话与确认**：轻量问答和项目查询按需交接到沙盒 Agent；方案就绪检查、内容版本绑定、按钮或自然语言确认，共同控制正式执行。
- **多 Harness 与模型服务**：支持 Codex、Claude Code 和 DeepSeek Harness；执行框架与模型协议分层，按任务冻结配置、预算和工具策略。兼容原生会话可恢复，跨 Harness 使用公开上下文交接。
- **音视频编辑**：转写、剪切、变速、转场、分屏、字幕、调色、配乐、音频清理、成片质检及版本比较。
- **图片工作台**：图层、选区、文字、调整、版本和 PNG/JPG/WebP/PSD/ORA 导出；按环境接入主体选择、抠图、OCR、局部编辑与图片分层。
- **持久化工作流**：支持 `script` / `operation` 步骤、依赖与分支汇合、检查点、质量验收、用户决策、有界重试与 Agent 修复、暂停和取消；修改需求后可复用符合条件的已验收成果。
- **受控生成**：通过服务端适配器接入文生图、文生视频、图生视频及图片编辑服务；实际可用能力取决于节点配置、模型和凭证。
- **进度与用量**：公开正文流、当前活动、工具历史、诊断下载；按当前任务、关联需求或整个项目查看模型请求、工具调用和 Token。
- **分布式执行**：PostgreSQL 保存任务、租约与执行状态，S3 兼容对象存储保存加密素材与检查点；Redis 可选，未配置时使用数据库快照同步。

## 系统结构

```text
浏览器：示例首页 / 聊天 / 工具 / 图片画布
                    │
                 API 副本
                    │
        PostgreSQL：任务、租约、版本、调用记录
                    │
            调度器 / 多个执行节点
              ├─ 轻量对话
              ├─ Agent 沙盒 → Harness Adapter → 受控模型网关 → LLM Provider
              ├─ 工作流脚本隔离沙盒
              ├─ 受控媒体和外部服务适配器
              └─ 独立 reaper：回收进程、挂载与执行身份

共享对象存储：加密素材、步骤成果、交付文件、检查点、示例库
实时传输：Redis（可选） / 数据库快照 → SSE → 浏览器
观测服务：结构化日志、指标、诊断与告警
```

调度任务分为 `agent`、`manual`、`workflow`，Agent 权限模式为 `plan` / `execute`。轻量对话属于 `agent` 的执行策略；申请沙盒分析不等于获得正式编辑权限。外部异步调用保存供应商任务 ID 后查询、收取结果；提交结果未知时不自动重复生成。

仓库内置部署是**单机多实例**：双 API、双执行节点及配套 watchdog/媒体服务，共用本机 PostgreSQL 和外部对象存储。支持跨节点恢复不等于整机故障容灾。

## Harness、模型与会话切换

| Harness | 模型协议 | 公开正文 | 会话恢复 |
| --- | --- | --- | --- |
| Codex | Responses | 增量与完整消息 | 原生 thread 恢复 |
| Claude Code / Agent SDK | Anthropic Messages | SDK 增量与完整消息 | 原生 SDK 恢复 |
| DeepSeek Harness | Anthropic Messages | 完整消息；当前固定 SDK 不支持逐 token 输出 | 原生 SDK 恢复 |

Model Adapter 另有 Chat Completions 实现，但当前没有登记使用它的 Harness 组合；支持某协议不代表任意 Harness、模型都能组合。轻量对话目前仅限 Codex 路径，Claude/DeepSeek 按完整 Agent 沙盒预留资源。固定 SDK 版本与具体边界见[适配器设计](docs/harness-adapters-design.md)。

管理员用 `VIDEO_USE_AGENT_PROFILES_JSON` 注册配置档，`VIDEO_USE_AGENT_PROFILE` 选择默认配置；用户在网页「Agent 设置」选择已配置的组合。模型地址和真实密钥留在服务端，沙盒通过受控网关访问。模型名、推理等级和预算来自生效配置，仓库默认值与某次部署配置可能不同，不能仅凭 README 判断当前服务使用哪个模型。

同 Harness 且版本、配置兼容时恢复原生会话；切换 Harness 或状态不兼容时，创建新会话，加载公开历史、已确认方案、工作流状态和保存成果。不会解析或转换厂商私有会话格式，也不会迁移私有推理。切换等待安全边界和资源清理；未知外部提交会阻断接管，已提交工作流和任务预算保持有效。

## 开始使用

| 用途 | 入口 | 需要准备 |
| --- | --- | --- |
| 使用已有网页服务 | [网页使用指南](WEB_UI.md) | 部署方提供的访问地址；上传素材或载入示例 |
| 在本地 Agent 中编辑文件 | [技能安装](install.md) | Python、FFmpeg / ffprobe、Agent；新转写需要 ElevenLabs 凭证 |
| 部署 Web 系统 | [配置说明](docs/distributed-execution-configuration.md) → [部署手册](docs/cluster-operations.md) | Linux 隔离环境、数据库、对象存储、模型配置和运行账号 |
| 开发或维护源码 | [开发与测试](docs/development.md) | 已安装的环境及对应权限 |

获取源码和 Web 开发依赖：

```bash
git clone https://github.com/hustwujing/vj-distributed-media-edit-system.git
cd vj-distributed-media-edit-system
uv sync --locked --extra web --extra web-test
# 使用 Claude / DeepSeek Harness 时安装对应固定 SDK
uv sync --locked --extra web --extra web-test --extra harnesses
# 已有 .env 时保留其内容
[ -f .env ] || cp .env.example .env
```

`.env.example` 只包含转写、自然语言控制与实时传输示例，**不是完整部署配置**。首次部署还需准备 `.agent.env`、`.storage.env`、`.distributed.env`，并核对数据库、会话密钥、节点端口、UID、GPU 和运行时；详见配置与部署手册。部分图片能力依赖独立模型环境，示例媒体也不随源码发布。内置部署要求本地示例目录或已发布的共享示例库可用。

`deploy.sh` 使用 systemd 管理集群，需在准备好的 Linux 主机以 root 运行：

```bash
./deploy.sh status      # 查看服务状态
./deploy.sh check       # 检查已部署版本测试报告和私有配置
./deploy.sh deploy      # 冻结源码、验证、备份并更新部署
./deploy.sh restart     # 重启已部署版本，不发布工作区改动
./deploy.sh observe status
```

项目名和 Python 包名为 `vj-distributed-media-edit-system`。`VIDEO_USE_*` 环境变量、`video-use-*` 服务名、`/video-use/` URL 前缀及沙盒接口路径保持兼容。

## 测试

在仓库根目录运行：

```bash
# 全部 Python 测试；使用独立数据库、对象存储协议测试服务和沙盒
.venv/bin/python -m tests.run_distributed_suite

# 前端状态、正文流、用量和结果展示
node --test tests/*.test.mjs
```

完整隔离回归需要 Linux、PostgreSQL 工具、沙盒系统依赖和相应权限。`VIDEO_USE_TEST_REAL_MODELS=1` 开启真实本地模型测试；真实外部服务与浏览器验收使用独立驱动，不能用此开关代替。部署脚本对冻结版本运行完整 Python 测试，前端测试需单独运行。环境准备、定向测试和报告位置见 [开发与测试](docs/development.md)。

## AI 辅助开发

让 AI 修改本仓库时，先读取 [AGENTS.md](AGENTS.md)，再按[专项开发说明索引](docs/development/README.md)选择规范。根目录 `SKILL.md` 面向媒体编辑，不是仓库开发流程。

| 扩展目标 | 开发规范 |
| --- | --- |
| 新增或升级 Harness | [新增 Harness](docs/development/add-harness.md)：固定 SDK、沙盒启动、事件、资源准入、原生恢复与切换 |
| 新增 LLM 服务或协议 | [新增 LLM Provider](docs/development/add-llm-provider.md)：配置接入或协议扩展、网关、流式、取消、用量与真实验收 |
| 新增媒体业务能力 | [新增 operation](docs/development/add-operation.md)：契约、实现、装载、生命周期与交付 |

兼容既有模型协议的服务优先新增配置档；新的 Agent 循环才新增 Harness。两者都复用已有调度、隔离、授权、检查点与调用账本，不能通过新工具或直连服务绕过这些边界。

## 验证记录

[2026-11-03 合并与完整真实回归报告](docs/post-merge-real-validation-report.md)记录了版本 `f2fe68121bc7` 的 Python 686/686、前端 43/43，以及 77 条真实场景：70 条成功交付、7 条模型或质量异常处理符合预期。原始 60 条媒体场景使用真实浏览器；R01–R16 使用明确标注的规划夹具，实际执行与成果为真实环境，R17 验证真实自然语言重新规划。

报告包含 root 转写缓存权限修复、Harness 资源准入修复、节点强杀接管、页面/用量/文件检查及未验证边界。它是该版本的历史证据，不是新代码自动通过的证明；长媒体生成中途跨 Harness 切换的完整组合仍未验收。

## 目录与文档

| 目录 | 内容 |
| --- | --- |
| `webapp/` | API、调度、执行器、工作流、适配器及原生 JavaScript 前端 |
| `helpers/` | 转写、剪辑、调色、字幕、时间线、图片与画布处理脚本 |
| `runtime/public/` | 沙盒公开技能、能力索引和客户端 |
| `deploy/` | 集群部署、运行时准备与观测工具 |
| `tests/` | 后端/前端回归、故障注入、浏览器和真实服务验收驱动 |
| `docs/` | 操作指南、设计、审查与历史验收报告 |
| `references/` | Agent 编辑和工作流协议参考 |
| `skills/manim-video/` | 可选 Manim 动画技能 |

从 [文档索引](docs/README.md) 按使用、部署、开发或设计查找资料。机器专属说明可保存在不随仓库发布的 `INSTALL.local.md`。凭证、运行数据库、素材、检查点和原始用户测试记录不纳入版本控制；经过脱敏、选取的验收报告与结构化证据可随代码保存。历史报告中的地址、版本、测试数量和本地证据属于当次验收，不代表当前部署状态。

## 来源与许可证

本项目基于 [browser-use/video-use](https://github.com/browser-use/video-use) 扩展，保留原项目的 MIT 许可证与版权声明，见 [LICENSE](LICENSE)。字体许可证见 [webapp/static/fonts/LICENSE-wqy.txt](webapp/static/fonts/LICENSE-wqy.txt)。
