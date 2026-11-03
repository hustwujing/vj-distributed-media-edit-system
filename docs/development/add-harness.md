# 新增或升级 Harness：AI 开发规范

适用于 AI 编码 Agent 和人工开发者新增 Agent 执行框架、升级原生 SDK/运行时、修改事件转换、原生恢复或跨 Harness 切换。本文是[仓库开发规则](../../AGENTS.md)的一部分；实现前必须按当前源码确认接口。LLM 服务协议接入另读[新增 LLM Provider](add-llm-provider.md)，不要因更换模型服务复制一套 Harness。

## 1. 先确定边界与支持矩阵

Harness 负责 Agent 循环、原生工具、会话和公开事件；Model Adapter 负责模型请求协议与回执。新增 Harness 仍使用 `agent` 调度类型、`plan/execute` 权限及原有工作流，不创建厂商专属队列、任务类型或成果发布流程。

实施前记录：原生公开 API/SDK 版本、对应可执行运行时、模型协议、推理参数、工具类型、是否输出 delta、中断方式、原生恢复接口、状态目录和已知限制。以固定版本的公开接口和真实响应为依据；不解析厂商私有会话文件，不假定某个模型品牌必然使用某个协议。

当前基线：Codex 使用 Responses；Claude Code 和 DeepSeek Harness 使用 Anthropic Messages；DeepSeek 固定 SDK 仅提供完整消息。完整支持矩阵、配置示例与恢复设计见[Harness 与模型 Adapter](../harness-adapters-design.md)。未知能力明确标注，不用“SDK 已安装”代替可运行、可恢复或可流式的证明。

## 2. 源码入口与实际契约

| 入口 | 必须核对的接线 |
| --- | --- |
| [harness_adapters.py](../../webapp/harness_adapters.py) | `HarnessAdapter`、`HarnessCapabilities`、`REGISTRY`、`available()`、`can_use_light()` |
| [agent_profiles.py](../../webapp/agent_profiles.py) | `HARNESS_PROTOCOLS`、推理等级校验、配置档、任务/attempt 配置冻结 |
| [harness_bridge.py](../../webapp/harness_bridge.py)、[harness_sdk_events.py](../../webapp/harness_sdk_events.py) | 沙盒内固定 SDK 桥、公开事件转换、会话打开/恢复和进程结束 |
| [agent_host.py](../../webapp/agent_host.py)、[agent_sandbox.py](../../webapp/agent_sandbox.py) | 宿主生命周期、固定启动参数、只读挂载和权限 |
| [runtime_image.py](../../webapp/runtime_image.py)、[pyproject.toml](../../pyproject.toml)、[uv.lock](../../uv.lock) | SDK/二进制装载、动态库、版本锁定及运行包指纹 |
| [scheduling.py](../../webapp/scheduling.py)、[execution_runtime.py](../../webapp/execution_runtime.py) | 节点能力、资源准入、统一沙盒、租约和回收 |
| [harness_context.py](../../webapp/harness_context.py)、[harness_events.py](../../webapp/harness_events.py) | 公开上下文、分页检索和租约约束的规范事件 |
| [checkpoints.py](../../webapp/checkpoints.py)、[session_checkpoint.py](../../webapp/session_checkpoint.py)、[harness_switching.py](../../webapp/harness_switching.py) | 已提交原生快照描述、兼容性和持久化切换 |
| [agent_profiles.js](../../webapp/static/agent_profiles.js)、[task_usage.py](../../webapp/task_usage.py) | 用户选择、能力边界、切换状态与实际用量 |

当前 Adapter 方法为 `initialize(rpc, write)`、`open_session(rpc, profile, instructions, session_id=None)`、`run_turn(rpc, session_id, text, profile)`、`respond(write, message)`、`interrupt(rpc, session_id)`、`events(message)`、`launch()` 和 `available()`。准备、连接、检查点、关闭仍由宿主和 Runtime 分工完成，**不存在已经统一实现的 `prepare/checkpoint/close` 插件 API**。扩展时以这些实际入口为准；若确需改变公共契约，应同步全部实现及其调用方。

## 3. 按顺序实施

1. **声明能力与兼容配置**：新增稳定 Harness ID 和 Adapter，登记 `REGISTRY`、协议组合与推理设置。逐项核实能力字段，不能直接继承默认 `True`。SDK 缺失或版本不兼容时不可被节点领取；拒绝不支持的配置组合。
2. **固定运行包**：锁定 SDK 与配套 runtime，纳入只读运行包、依赖和指纹；禁用运行时自动升级及任意下载。证明降权后的任务 UID 能读到所需文件。不得通过放宽整个宿主目录权限解决装载问题。
3. **实现原生交互**：复用宿主 RPC/事件协议；SDK 桥在 TaskRuntime 的 OS 沙盒内运行。启动命令由可信代码确定，不从用户消息拼装命令。真实服务密钥仅在网关进程，SDK 使用本地网关地址和占位凭据。
4. **接入事件、上下文与恢复**：转换公开 SDK/RPC 事件，保留消息和工具调用 ID；将原生状态作为不透明文件快照保存；兼容则原生恢复，不兼容则建立新会话并加载公开交接包。
5. **核对调度与切换**：调度资源预留和实际分派必须使用一致的能力判断；复用 `harness_switching` 的请求、等待、阻断、激活和撤销流程。新 Agent 不得与旧执行范围同时拥有写入权。
6. **完成隔离、真实运行及页面检查**：先测试直接影响的契约，再以独立项目运行真实 SDK、模型、工具、恢复、取消和流式验证。同步设计支持矩阵、配置说明及未验证边界。

### 资源、权限和工具

- `light_conversation=True` 只有在轻量执行路径确实无需完整沙盒且已接通时才能声明。`can_use_light()` 同时用于调度和宿主分派；不能按轻量 256 MB 预留后启动完整沙盒。新 Harness 默认走完整执行器。
- 保留任务租约、UID、挂载、网络和 cgroup 隔离；SDK 的 `bypassPermissions` 等选项只作用于外层沙盒内部，不能当作宿主授权。原生工具白名单和 SDK 配置来源均要核对，拒绝任意远端 MCP/服务端工具绕过。
- 图片、视频、转写等业务能力仍走已有受控工具或 workflow/operation；原生 Bash 不得直接获得外部服务凭据。确认方案、输入归属、revision/hash 和发布校验不因 Harness 改变而省略。
- `respond()` 对未支持的原生权限或用户交互请求明确拒绝；没有完整接线不能自动批准，也不能声称支持原生问卷或子 Agent 全量事件。

### 公开事件和流式

- delta、完整消息和最终消息必须归属稳定消息 ID；完整消息补齐后不重复追加，不吞尾部。只公开正文、过程说明和工具证据，私有推理不进入页面、日志或交接包。
- 工具开始/完成/错误保留同一调用 ID。完成事件用于更新记录，不触发宿主重复执行；取消或进程退出要闭合用户侧活动，缺失成功回执不能画成成功。
- SDK 仅输出完整消息时声明 `streaming_text=False`；不能用前端打字动画冒充真实增量。真实流式检查必须同时观察上游事件、宿主发布与浏览器 DOM，记录首段、分片间隔、尾部和刷新后的全文。
- `turn.ended` 只表示 Agent 本轮结束；正式交付仍必须通过服务端工作流、质量和成果发布校验。

### 恢复、切换和取消

- `execution_native_descriptors` 的外层描述必须绑定实际已提交 checkpoint、Harness/adapter/runtime 指纹、配置摘要与 session ID。崩溃前刚获得的 session ID 不能代替已保存会话的 ID。
- 相同 Harness 且兼容时交给原生公开恢复接口；换 Harness 或不兼容时加载数据库中的公开历史、已确认方案、工作流、有效成果和预算信息。保留覆盖缺口，按需分页检索，不翻译私有存储、不重放历史工具。
- 切换必须等待旧执行结束和物理清理；未闭合工具、提交结果未知等副作用阻断接管。已有 workflow 和供应商任务 ID 不变，预算不重置，迟到事件不能覆盖新配置或取消状态。
- 中断需验证本地模型等待、SDK 子进程、沙盒和资源均有界停止；本地停止不能被报告为上游停止或免计费。故障恢复约束见[分布式执行开发说明](change-distributed-execution.md)。

## 4. 测试与完成检查

| 检查项 | 通过条件 |
| --- | --- |
| 注册与配置 | 缺依赖、错误版本、未知协议和不支持的参数被拒绝；任务和 attempt 的配置不跟随默认值漂移 |
| 调度与隔离 | 完整 Harness 在资源不足时不被领取；实际 UID、挂载、网络、只读素材和工具授权有效 |
| 文字与工具 | 分片/完整消息合并、中文跨字节、尾部、错误、缺终态、重复事件和稳定工具 ID 均覆盖 |
| 原生恢复与交接 | 第二节点恢复已提交会话；不兼容版本/配置与跨 Harness 使用公开上下文，不丢已确认约束、不重放操作 |
| 竞争与故障 | 重复切换、取消、进程崩溃、过期租约、迟到结果、未知外部调用保持唯一执行权与原成果 |
| 用量 | SDK 汇总不重复计入网关账本；多次真实请求逐次计数；缺失 Token 保持未知，切换不重置预算 |
| 用户侧 | Harness/model/能力和切换状态准确，普通聊天不出现无依据执行按钮，刷新后正文/结果/工具状态一致 |
| 真实可用 | 实际模型回答、工具读写、流式（如声明支持）、恢复和取消有原始证据；不能只测 `/models` 或 HTTP 200 |

定向回归入口：

```bash
.venv/bin/python -m tests.run_distributed_suite tests.test_harness_adapters tests.test_harness_streaming tests.test_harness_switching tests.test_session_checkpoint tests.test_agent_isolation
node --test tests/agent_profiles.test.mjs tests/text_stream.test.mjs tests/task_usage.test.mjs
```

浏览器控制验证参考 [run_harness_browser.py](../../tests/run_harness_browser.py)；真实 SDK、两节点恢复和流式/取消参考 [run_live_harness_smoke.py](../../tests/run_live_harness_smoke.py)；部署页面验证参考 [campaign_harness_live.cjs](../../tests/campaign_harness_live.cjs)。后两者当前针对 Claude/DeepSeek，新增 Harness 必须扩展实际场景，不能原样运行后宣称新框架通过。先核对驱动的凭据文件格式、私有输出目录、浏览器依赖路径和目标环境，不复制历史机器凭据。

完成报告列出代码接线、固定版本、支持矩阵、原生恢复/公开交接区别、流式实测、资源与取消证据、用量核对和剩余边界。发布按[开发与测试](../development.md)完成全量门禁；仅改文档时检查链接与源码一致性，不调用付费模型或重启服务。
