# Distributed Agent System

通过自然语言执行媒体处理任务的分布式 Agent 系统。用户先确认目标、素材和预算，Manager 根据实际结果逐轮追加 DAG；专业 Agent、脚本和注册 operation 生成候选结果，独立 Orchestrator 验证后提交正式状态与交付。

当前开发分支为 `feature/unified-run`。设计见[动态工作流方案](video-use-dynamic-agent-workflow-design.md)，逐例验收见[真机测试报告](docs/real-case-campaign/dynamic-test-report.md)。报告中的未执行、未完成和历史版本结果不代表当前版本通过。

## 工作方式

```text
浏览器：目标 / 素材 / 授权预算 / 暂停取消 / 验收 / 下载
                            │
                         API 副本
                            │
                 PostgreSQL 权威状态与命令
                    ↕                 ↕
          独立 Orchestrator       调度器与执行节点
         校验 / 恢复 / 发布       ├─ Manager：只提交追加提案
                                ├─ operation / script / 专业 Agent
                                └─ 固定工具、隔离沙盒、独立 reaper

BOSS/S3：加密素材、不可变检查点、候选与交付产物
Redis：带过期时间的事件传输；业务状态仍以数据库为准
观测服务：关联 run / task 或 control job / attempt / generation 的日志、用量、诊断
```

- **统一身份**：Session 记录会话和消息；Run 是执行与用量根。业务 Task 与 PLAN/REVIEW/DIAGNOSE Control Job 分表，使用同一租约体系，API 统一为 `/runs`。
- **持续输入与反馈**：输入写入 `run_inputs`，第一条输入建立全 Run 派发屏障；已开始的工作正常结束，在安全点合并重规划。质量反馈也走同一入口，不能豁免技术或 Agent 拒绝；正式交付后输入创建待确认范围的 successor Run。
- **滚动计划与修复**：支持 `operation`、`script`、`agent` 三类 Task。旧 revision 和结果历史不可改写；失败节点可在原授权、目标、输入输出与验收内产生新 revision，依赖后代随之失效，无关分支继续复用。
- **明确授权**：绑定素材内容指纹、操作实现、专业 Agent 策略、模型 Token、外部调用额度和任务期限；扩大范围需要用户确认。
- **Provider 治理**：外部副作用、授权预算组、调度并发组、路由适配器和实际 Provider 身份分别记录；调用前冻结元数据与恢复句柄，配置变化不得重放生成。
- **可靠执行**：租约及 generation 约束领取、结果提交与回收。已知外部句柄可继续查询，提交结果未知时禁止自动重复生成。
- **独立验收**：执行器只暂存候选；技术完整性、内容审核及封口后的最终验收通过后，由 Orchestrator 发布成果。
- **可复用结果**：已验收成果可以按固定修订和哈希加入下一轮素材，重新授权后执行新目标。
- **可追踪失败**：保留错误分类、执行证据、用量、清理状态；模型判断不能覆盖程序完整性约束。
- **类型化媒体语义**：音轨存在、静音、响度、文本匹配、Alpha/蒙版、主体范围、一致性和动作时序分别表达。多片段拼接和常用音频处理走受控 operation，并记录资源与测量报告。

部署形态为单机多实例，支持节点接管；它不提供整机故障容灾。专业 Agent 默认使用注册的 `structured-profile-v2` 有界工具循环，底层通过既有模型协议适配器调用 LLM；这不等同于运行某个厂商的 CLI/SDK harness。

## 本机隔离部署

工作区：`/data2/wujing03/distributed-agent-system`。服务入口：`/distributed-agent-system/`。

| 资源 | 独立实例 |
| --- | --- |
| systemd 服务前缀 | `dynamic-agent-` |
| API / 内部代理 / 观测端口 | `19766, 19767` / `19765` / `19768` |
| PostgreSQL 数据库 | `distributed_agent_system` |
| 业务 / 控制器数据库角色 | `distributed_agent_system` / `distributed_agent_orchestrator` |
| Redis key 前缀 | `distributed-agent-system:events:` |
| BOSS 对象前缀 | `distributed-agent-system`，独立加密密钥 |
| 私有配置 / 运行目录 | `/etc/distributed-agent-system` / 本仓库 `.cluster` |
| 执行节点注册目录 | `/data2/wujing03/da-exec/a`、`b` |

`deploy.sh` 只管理此实例。它复用已有 PostgreSQL、Redis、BOSS 服务，但不重启 main 服务、不写 main 数据，也不使用 main 的会话 Cookie。运行需 root 及已有主机依赖；首次配置从同机现有私有配置读取共享依赖连接信息，并生成独立密钥、账号和命名空间。凭据不纳入 Git。

```bash
./deploy.sh status
./deploy.sh deploy       # 冻结源码，完整 Python/JS 门禁，备份独立数据库，部署并检查健康
./deploy.sh check        # 检查实际已部署版本
./deploy.sh restart      # 重启已部署版本，不发布工作区代码
./deploy.sh observe status
```

本分支不迁移旧 Workflow 数据、不双写旧状态。复用的媒体 operation、工具、图层处理及 CLI 能力仍在仓库中。旧集群运维文档描述 main；维护此分支时以本页和[动态图开发规范](docs/development/change-dynamic-workflow.md)为准。

## 开发与测试

依赖安装：`uv sync --locked --extra web --extra web-test`。真实沙盒测试需要 Linux、PostgreSQL 工具、bubblewrap、FFmpeg 和必要权限。

```bash
.venv/bin/python -m tests.run_distributed_suite
node --test tests/*.test.mjs

# 已部署隔离实例上的真实浏览器、模型和外部依赖测试
.venv/bin/python tests/campaign_suite.py list
.venv/bin/python tests/campaign_suite.py run --cases S01 --jobs 1
.venv/bin/python tests/campaign_suite.py run --group all --jobs 2
.venv/bin/python tests/campaign_suite.py report
```

当前真机清单覆盖修复 revision、批量 Reviewer 检查、透明主体边界、静音/音轨、受控音频和大规模顺序拼接。报告按业务成功、程序错误、非程序错误但应对正确、依赖阻塞和未执行分别统计。

真机清单共 118 条：原有 99 条逐条保留业务目标并调整协议，新增 19 条统一 Run/通用边界用例；当前开发状态见[实施记录](docs/unified-run-progress.md)，见[审查矩阵](docs/real-case-campaign/dynamic-case-review.md)。每次尝试保存独立版本、输入哈希、浏览器截图、日志、数据库与缓存状态、BOSS 审计、产物校验和清理记录。程序检查通过后仍需逐例审查实际触发边界与浏览器表现；模型/依赖失败必须证明程序应对正确才能单独结案。

源码、测试快照和已部署版本是不同对象；只认绑定源码摘要的验证证据。冻结部署验证会建立可供测试账号读取的独立源码副本，避免为了测试开放私有部署数据目录。

破坏性架构变更可用 `./deploy.sh deploy --reset-database` 重置专用数据库：完整冻结测试通过后停止本实例、备份数据库，再检查未完成的外部调用与清理记录。存在未收尾记录时拒绝重置；共享 PostgreSQL 服务和 main 数据库不受操作。旧 BOSS 对象及历史证据不会被该命令批量删除。

## 开发规范与目录

从 [AGENTS.md](AGENTS.md)、[开发测试](docs/development.md)及[专项规范索引](docs/development/README.md)进入：

- [动态图、控制器与验收](docs/development/change-dynamic-workflow.md)
- [新增专业 Agent](docs/development/add-specialist-agent.md)
- [新增 Profile Harness](docs/development/add-profile-harness.md)
- [新增 LLM Provider 与模型协议](docs/development/add-llm-provider.md)
- [新增或扩展 operation](docs/development/add-operation.md)

`webapp/` 包含控制器、执行器、协议、API 和原生 JavaScript 界面；`helpers/` 包含媒体能力；`deploy/` 管理独立部署；`tests/` 保存单元、集成与真机驱动；`runtime/public/` 是沙盒公开工具协议；`docs/` 保存规范与报告。

## 来源与许可证

本项目基于 [browser-use/video-use](https://github.com/browser-use/video-use) 扩展，保留 MIT 许可证与版权声明，见 [LICENSE](LICENSE)。字体许可证见 [webapp/static/fonts/LICENSE-wqy.txt](webapp/static/fonts/LICENSE-wqy.txt)。Python 包名仍为 `vj-distributed-media-edit-system`，环境变量保留 `VIDEO_USE_*` 前缀。
