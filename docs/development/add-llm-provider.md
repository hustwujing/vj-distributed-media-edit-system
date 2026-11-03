# 新增 LLM Provider：AI 开发规范

适用于 AI 编码 Agent 和人工开发者新增模型服务地址、模型配置、认证方式或模型 HTTP 协议。新增 Agent 执行框架另读[新增 Harness](add-harness.md)。图片/视频生成、转写等业务供应商走[外部服务与供应商接入](change-external-providers.md)，不能将其异步提交/查询生命周期直接套在 Agent 模型流上。

## 1. 先选择接入方式

当前没有独立的 LLM Provider 插件注册层：服务端 `AgentProfile` 保存 `harness/protocol/model/base_url/credential_env` 等配置，`model_adapters.REGISTRY` 按**协议**选择适配器。实施前必须分清以下三种改动：

| 需求 | 接入位置 | 边界 |
| --- | --- | --- |
| 新服务完全兼容现有协议 | 新增管理员配置档及凭据引用 | 无须复制 Adapter；仍需实际验证认证、工具、终态、usage 和参数语义 |
| 同协议存在厂商差异 | 在协议层明确实现受验证的差异；必要时新增版本化配置契约 | 不在宿主或 UI 散布模型名/URL 字符串判断，不静默切换模型或丢弃必需参数 |
| 新协议或认证/事件语义不兼容 | 新 Model Adapter，加网关路由、流式保护、回执解析及兼容矩阵 | 仅登记 `REGISTRY` 不等于接线完成；对应 Harness 必须真正会发出该协议 |

目前 `responses`、`anthropic`、`chat_completions` 分别使用 `/responses`、`/messages`、`/chat/completions`。现有 Harness 组合只有 Codex→Responses、Claude Code/DeepSeek Harness→Messages；Chat Completions 有协议实现，但没有已登记的 Harness 组合。不能仅修改 `HARNESS_PROTOCOLS` 就宣布某原生 SDK 已支持另一协议。

## 2. 实施前读取

| 入口 | 职责 |
| --- | --- |
| [agent_profiles.py](../../webapp/agent_profiles.py) | 服务端配置注册、兼容矩阵、预算、密钥引用与快照 |
| [model_adapters.py](../../webapp/model_adapters.py) | `ModelAdapter`、请求字段/工具校验、协议 path/headers 与 Receipt |
| [agent_gateway.py](../../webapp/agent_gateway.py)、[gateway_process.py](../../webapp/gateway_process.py) | 固定模型路由、沙盒请求入口、图片规范化、额度、连接和计量 |
| [egress_policy.py](../../webapp/egress_policy.py)、[cancellation.py](../../webapp/cancellation.py) | 地址/DNS/凭据策略、受控 session、连接取消 |
| [safe_stream.py](../../webapp/safe_stream.py)、[usage_protocol.py](../../webapp/usage_protocol.py) | SSE 分帧、跨分片敏感信息保护、结束语义与回执 |
| [task_usage.py](../../webapp/task_usage.py)、[planning_budget.py](../../webapp/planning_budget.py) | 实际调用账本、讨论额度、缺失回执和预算消费 |
| [harness_bridge.py](../../webapp/harness_bridge.py)、[light_conversation.py](../../webapp/light_conversation.py) | 原生 SDK 实际请求格式和轻量路径的协议依赖 |

当前接口是 `ModelAdapter.protocol`、`path`、`headers(key)`、`prepare(body, model, max_output_tokens, reasoning_effort=None)`、`receipt()`。Adapter 只负责协议转换，不自己发送网络请求、持有另一份凭据或实现隐藏重试；`Gateway.forward()` 负责每次受控 HTTP 调用。若引入厂商参数或独立 Provider 层，必须同步配置校验、快照摘要、公开投影、网关、运行包与恢复行为，不宣称现有接口已支持这些字段。

## 3. 配置与凭据

优先复用 `VIDEO_USE_AGENT_PROFILES_JSON` 和 `VIDEO_USE_AGENT_PROFILE`。以下是兼容 Messages 服务的配置档结构；地址和模型是占位符，须替换为实际验证值后才能启用：

```json
{
  "example_claude": {
    "harness": "claude_code",
    "protocol": "anthropic",
    "model": "replace-with-verified-model-id",
    "base_url": "https://gateway.example/v1",
    "credential_env": "VIDEO_USE_EXAMPLE_API_KEY",
    "reasoning_effort": "high"
  }
}
```

- `base_url` 是基础地址，当前网关会追加 Adapter 的 `path`，不要填写完整 `/messages` 路径导致重复拼接。内部地址按既有部署允许列表配置；不放开任意内网、元数据地址或重定向。
- 密钥只在受保护的服务端配置中注入；JSON 保存环境变量引用。公开 API 不返回真实密钥、服务地址或密钥引用名。不要提交 `.agent.env`、Cookie、私有测试配置或带认证头的日志。
- SDK 在沙盒内连接本地网关，不能直连供应商并携带真实 key。服务端从可信配置构造认证头，不透传 SDK 自带的目标地址或认证头。
- 新任务冻结有效路由、模型、推理设置、输出上限和预算；已有任务不能被默认值变化静默改路由。凭据值可由引用重新解析，但轮换需确认账号和服务能力仍兼容。
- 主 Agent、轻量路径和语义审核模型的配置来源不同。接入范围必须明确，不能把新增主 Agent profile 描述为全部辅助模型都已迁移。可配置字段和预算见[模型 Adapter 设计](../harness-adapters-design.md#配置)。

## 4. 协议实现与网关接线

1. **记录协议事实**：核实基础路径、认证、请求格式、工具调用 ID/结果、图像输入、上下文限制、推理参数、输出限制、HTTP 错误、SSE 终态和 usage 单位。模型列表可访问只证明发现接口可用，至少实际完成一轮生成和一轮工具往返。
2. **实现纯转换**：在 Adapter 内设置固定协议路径、认证头、字段白名单和回执类。强制使用已冻结的 model/effort/output cap，拒绝不支持的必需能力；参数转换写明语义，不静默降级。已有白名单会过滤部分字段，不能把过滤描述成所有未知参数都显式报错。
3. **完整接线**：新增协议同时更新 `agent_gateway.py` 端点白名单与 frozen protocol 校验、`model_adapters.REGISTRY`、`HARNESS_PROTOCOLS` 及实际 Harness 请求路径。新图片结构还需接入 `normalize_native_images()` 或现有 Broker 规范化；新文本/工具结构需接入 `SafeEvents` 的逻辑通道保护。
4. **复用网络与预算**：通过受控 session、明确连接/读取期限、禁用自动重定向的网关发起请求；提交前检查租约和额度。SDK 隐式重试也必须再次经过网关计量，禁止在 Adapter 里无限重试或网络失败后无记录地切换供应商。
5. **验证异常与恢复**：连接失败、401/403、429/5xx、首包前/流中断、空结果、缺结束事件和缺 usage 分别验证。不能把 HTTP 200、Socket EOF 或 SDK 正常退出直接认作完整业务成功。

### 输入与权限

复用 `model_input()`、`validate_messages()`、地址检查和隔离图片规范化。新协议不能通过远程图片 URL、document/file、厂商服务端搜索工具等获得额外网络或素材访问权限。合法素材也必须经过项目归属、大小和类型检查；不能直接依赖上游 Content-Type。涉及新权限时按[Agent 与对话开发说明](change-agent-conversation.md)及[分布式执行说明](change-distributed-execution.md)补充验证。

### 流式与取消

完整 SSE 事件到达后应立即交给 SDK，保留跨事件逻辑通道的敏感信息检查；正常正文不能因固定尾部缓冲一直等到请求结束。测试中文/UTF-8 按字节拆分、多事件合并、工具参数分片、换行、结束边界和敏感值跨分片；不能通过关闭保护换取流畅。

Provider 支持 SSE 不代表 Harness 发布增量。需分别记录供应商分片、SDK 消费、公开正文、浏览器 DOM 和持久化结果；模型输出慢、网关滞留和前端合并延迟分开定位。声明支持流式时必须证明中途可见、尾部完整、刷新后不丢；完整消息模式则明确展示能力限制。

取消应沿现有控制、连接中断和进程回收机制结束本地等待，不能等下一块文本才检查取消。没有供应商取消协议/确认时，上游是否停止及是否计费保持未知；不伪造可轮询任务 ID，也不套用媒体生成的 submit/poll/collect 计数。若确需引入持久化异步 LLM 协议，先补充状态机和恢复设计，不能仅改 Adapter 方法。

### 用量与故障语义

- 每次实际模型请求只在统一账本计一次。SDK 的单步/整轮 usage 是核对证据，不再与网关回执相加；工具一次往返不等于一次模型请求，SDK `max_turns` 也不等于 HTTP 请求预算。
- 按协议核实 delta usage 是增量还是累计。Messages 当前将非缓存输入、缓存读取和缓存创建计入输入；缓存命中是输入子集，推理 Token 是输出子集，不额外加到总数。
- 明确终态和完整回执才记成功及最终 Token；中断、达到输出上限、缺 usage、无供应商费用回执不能伪装成完成或零消耗。派生 total 标明 calculated，费用保留币种和证据。
- 原始回执、请求次数、重试、预算额度和耗时分别记录；task/run/attempt/Harness/model 关联复用现有机制。错误响应做安全投影，不把上游正文、认证头或私有推理写入诊断和公开页面。详细口径见[可观测性与用量开发](change-observability-usage.md)。

## 5. 测试与交付检查

| 层级 | 必须有的证据 |
| --- | --- |
| 配置/纯协议 | 正确编码、模型/推理/输出上限强制覆盖、工具往返、usage/终态解析；非法配置和输入拒绝 |
| 网关/隔离集成 | 真实 HTTP/SSE 替身验证路径与认证、地址限制、图片规范化、配额、取消和账本次数；凭据不进入沙盒 |
| 负向与竞争 | 无 usage、断流、上限终止、错误响应、取消后迟到结果、租约失效不误记成功或重复执行业务 |
| 真实 Provider + Harness | 实际回答、工具、长中文流式/完整消息、Token 核对及取消；报告模型、SDK、协议与生效配置 |
| 页面与切换 | 正文/工具状态一致、刷新保留、配置正确、同 Harness 换模型的不兼容原生状态走公开交接 |
| 业务与发布 | 原有方案确认、工作流和交付仍有效；按改动范围跑回归，部署门禁见开发文档 |

定向入口：

```bash
.venv/bin/python -m tests.run_distributed_suite tests.test_harness_adapters tests.test_harness_streaming tests.test_egress_policy tests.test_task_usage tests.test_usage_stream_integration
node --test tests/text_stream.test.mjs tests/task_usage.test.mjs
```

真实驱动参考 [run_live_harness_smoke.py](../../tests/run_live_harness_smoke.py) 和 [campaign_harness_live.cjs](../../tests/campaign_harness_live.cjs)，它们当前有固定 Harness/模型和环境假设，新 Provider 需按实际目标适配。使用独立数据、私有凭据文件和充分的普通场景预算；故意测试额度耗尽的场景另行隔离。服务不可用时明确未通过或未验证，不以本地替身冒充真实服务。

提交前做静态 review，逐项核对配置→Harness→Gateway→Adapter→Receipt→页面/账本链路；报告已实现范围、实际请求和 Token 证据、取消边界、恢复策略及未验证能力。同步 README 支持矩阵、设计/配置文档及专项索引。仅文档改动按[开发与测试](../development.md)检查链接和代码一致性，不触发付费请求或部署。
