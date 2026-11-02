# Harness 与模型 Adapter

本轮在独立 worktree 开发。实现状态与验证证据见 [验收报告](harness-adapters-report.md)；本文件描述实际边界，不代表已部署。

## 分层和执行路径

仍使用 `agent` 调度类型和 `plan/execute` 权限。API 创建轮次时冻结执行配置，调度器领取任务后由统一 TaskRuntime 分配身份、挂载工作区和只读工具、设置网络/cgroup/seccomp 限制，再启动所选 Harness。`manual/workflow` 不因切换 Agent 改名或改为另一套调度。

| 层 | 实现 | 职责 |
| --- | --- | --- |
| 执行配置 | `agent_profiles.py` | 注册、兼容协议、密钥引用、有效配置冻结、每 attempt 的历史配置 |
| Harness | `harness_adapters.py`、`harness_bridge.py` | 原生握手、开会话/恢复、提交消息、公开事件转换；SDK 桥接进程在 OS 沙盒内 |
| 运行与权限 | `agent_host.py`、`agent_sandbox.py`、`execution_runtime.py` | prepare/connect/运行/中断/检查点/关闭的总生命周期；适配器不自行启动宿主业务命令 |
| 模型协议 | `model_adapters.py` | Responses、Messages、Chat Completions 编码与回执转换 |
| 网关策略 | `agent_gateway.py`、`safe_stream.py` | 固定目标、图片规范化、密钥、额度、取消、敏感信息检查、每次实际请求账本 |
| 公开上下文 | `harness_context.py`、`harness_events.py` | 原始要求、已确认方案、工作流、成果索引、公开事件和按需历史查询 |
| 切换 | `harness_switching.py` | 持久化意图、安全边界、未明副作用检查、配置激活和撤销 |

Harness 的原生权限选项仅避免在既有 OS 沙盒内再嵌套不可用的沙盒。它不能解除外层隔离，也不能获得真实服务密钥。Claude 的原生工具限定为 Bash/Read/Write/Edit/Glob/Grep；受控业务仍使用 `/opt/workflow.py` 等既有入口。

## 当前支持矩阵

| Harness | 固定版本 | 模型协议 | 公开文字 | 原生恢复 |
| --- | --- | --- | --- | --- |
| Codex | 部署运行包中的二进制及其 hash | Responses | 增量 + 完整消息 | `thread/resume` |
| Claude Code | `claude-agent-sdk==0.2.176` | Anthropic Messages | SDK `StreamEvent` 增量 + 完整消息 | SDK `get_session_info` / `resume` |
| DeepSeek Harness | `deepseek-harness-sdk==0.2.1rc2` 及同版本 runtime | Anthropic Messages | 每条完整公开消息；不宣称 token 增量 | SDK `resume_session` |

DeepSeek 这一固定版本的实际模型请求走 Messages，不能因为模型名叫 DeepSeek 就套用 Chat Completions。Chat Completions 的协议适配器可以独立测试，但当前没有登记使用它的 Harness 组合。未知组合直接拒绝。图像、超长上下文及任意模型名仍需逐组合验证，真实文本与工具测试不能证明全部模型能力。

用户交互问卷未作为原生 Harness 通用能力开放；现有 workflow 用户决策仍有效。缺失能力通过 `capabilities` 明示。DeepSeek 的公开工具事件目前覆盖根会话；不宣称已验证原生子 Agent 的全量观测。

## 配置

安装可选依赖：`uv sync --extra web --extra harnesses`。部署运行包必须重新生成，以收录 SDK 运行时及动态库；只修改配置不会自动安装依赖。

使用 `VIDEO_USE_AGENT_PROFILES_JSON` 注册配置档，使用 `VIDEO_USE_AGENT_PROFILE` 选择全局默认值。例子中的地址是占位符，密钥只通过服务端环境变量提供：

```json
{
  "claude": {
    "harness": "claude_code",
    "protocol": "anthropic",
    "model": "claude-4.6-sonnet",
    "base_url": "https://your-gateway.example/v1",
    "credential_env": "VIDEO_USE_CLAUDE_API_KEY",
    "reasoning_effort": "high"
  },
  "deepseek": {
    "harness": "deepseek_harness",
    "protocol": "anthropic",
    "model": "deepseek-v4-flash",
    "base_url": "https://your-gateway.example/v1",
    "credential_env": "VIDEO_USE_DEEPSEEK_API_KEY",
    "reasoning_effort": "high"
  }
}
```

未显式注册时，`default` 沿用 `VIDEO_USE_MODEL`、`VIDEO_USE_MODEL_BASE_URL`、`VIDEO_USE_MODEL_API_KEY` 和 `VIDEO_USE_MODEL_REASONING_EFFORT`，Harness 为 Codex。配置快照保存密钥环境变量名，不保存密钥值；公开 API 也不返回服务地址或密钥引用名。

创建任务时冻结：Harness、adapter 版本、模型/协议/路由、推理设置、工具策略、预算和摘要。实际出站请求强制使用冻结模型、推理等级及输出上限。运行包指纹另外绑定原生快照，不兼容时走新会话交接。当前轻量入口仍限 Codex；Claude/DeepSeek 使用完整沙盒执行器。既有语义审核模型配置保持独立，不能把主 Agent 切换解释成所有辅助模型也一起切换。

预算默认从以下服务端配置解析后冻结：

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `VIDEO_USE_PLAN_SECONDS` | 240 | 讨论累计时间 |
| `VIDEO_USE_PLAN_MODELS` | 14 | 讨论模型请求数 |
| `VIDEO_USE_PLAN_TOOLS` | 24 | 讨论工具次数 |
| `VIDEO_USE_PLAN_INSPECTIONS` | 8 | 讨论画面检查次数 |
| `VIDEO_USE_AGENT_RUN_SECONDS` | 900 | 单次 Agent 执行时限 |
| `VIDEO_USE_AGENT_RUN_MODEL_CALLS` | 160 | 单次网关运行的模型请求上限 |
| `VIDEO_USE_TASK_MODEL_CALLS` | 40 | 同一任务累计模型请求上限 |
| `VIDEO_USE_MODEL_OUTPUT_TOKENS` | 16000 | 模型单次输出上限 |

已有任务生命周期、用户日额度、外部服务额度继续生效；上表不是全部服务配额的替代物。同一任务切换时沿用源预算和已消耗记录，不重置、不扩大。新任务按创建时配置冻结。

## 原生恢复与跨 Harness 交接

原生目录按不透明文件保存，复用加密对象存储、文件大小/hash 校验和检查点版本保留。不会把 Codex 原生文件翻译成 Claude/DeepSeek 私有格式。

`execution_native_descriptors` 将外层描述与具体 checkpoint path 原子绑定：schema、Harness、adapter/runtime、配置摘要、工具策略、session ID、公开事件游标及 state 对象引用。`native_format=sdk_owned_opaque` 明确表示不解释厂商格式。恢复只使用已提交描述中的 session ID，不使用崩溃前可能已更新但尚未 checkpoint 的最新 ID。

兼容的同 Harness/配置/运行包交给原生公开恢复接口；恢复不兼容或原生接口拒绝则新建会话，从业务数据库和共享文件恢复公开上下文。新会话不重放历史工具；任何新动作仍经过权限和版本校验。回滚同时处理会话描述，不能继续使用被回滚的新版本元数据。

`/workspace/harness-context.json` 包含当前请求、已确认方案、execution intent、关联 workflow、公开历史/工具证据、成果索引和预算已用值。截断及历史事件不完整均显式标记。历史检索限定当前租约所属项目：

```bash
python /opt/context.py turns
python /opt/context.py turns --before TASK_ID
python /opt/context.py message --task TASK_ID --field prompt --offset 0
python /opt/context.py message --task TASK_ID --field text --offset 16000
python /opt/context.py tools --task TASK_ID --after 0
```

返回 `next_before` / `next_offset` / `next_after` 后继续分页。它只读取本系统公开记录，不读取厂商原生文件或私有推理。缺失工具结果须核实，不能据此推断“从未执行”。

## 切换状态和页面

项目「Agent 设置」可选择配置；无活动任务时设置后续任务偏好，有活动任务时提交带 `request_key` 和 `expected_digest` 的切换请求。新 API 复用归属校验与有界控制池，不在请求线程等待旧 Agent。

`requested → waiting/blocked → activated` 是活动任务切换路径。等待中的切换阻止下一次 Agent claim；已经提交的 workflow 继续按原图执行。现有 Agent 正在运行时等待它结束并完成资源回收，不强杀并重放操作。任务已结束则记录 `next_turn`，不篡改历史配置。取消原任务优先，用户也可撤销尚未激活的切换。

旧租约过期仍需核实物理清理；运行状态未收敛、未闭合工具或外部提交结果未知时进入 `blocked`，不自动重复付费生成。激活只修改后续 Agent 配置和原生会话关联，不修改步骤 ID、上游任务 ID、验收、授权或已保存成果。

## 流式输出与计量

公开文本按稳定 message ID 合并 delta 与完整消息，最终完整消息立即刷新尾部并持久化。工具完成事件更新观测记录，不触发第二次工具执行。私有推理不进入页面或交接包。

网关在完整 SSE 事件到达后立即转发，通过按逻辑内容通道的跨分片检查保护凭据；正常文字不保留固定长度尾部。Responses、Messages、Chat Completions 都检查公开文本/工具参数分片。DeepSeek 当前完整消息事件不等于逐字流式，页面不应伪造增量能力。

实际每次模型 HTTP 请求进入统一账本；SDK usage 仅为核对证据，不能重复累加。Anthropic 缓存读取/创建输入计入完整输入；未报告字段保持未知，推算总量标为 calculated。每个 attempt 冻结自己的配置，汇总通过 attempt 关联区分 Harness/model。模型请求进行中取消会终止本地等待和执行范围，但不宣称上游必然取消或未计费。

## 验证范围

按用户最新要求，只运行 Harness、LLM Adapter 及直接相关恢复、隔离、计量、流式和浏览器测试，不运行整个项目全量测试。正式媒体链路中途切换、全部厂商模型能力、原生子 Agent 观测、异构节点安装组合等未验证范围在报告和 TODO 中保留，不以文本问答测试替代。

### 部署场景发现的资源准入修复

`HarnessCapabilities.light_conversation` 明确轻量对话能力。`can_use_light()` 同时供调度资源准入和宿主分派使用；Claude/DeepSeek 的新任务即使初始路由是 light，也按完整 Agent 的 CPU/内存请求准入。不能在预留 256MB 后启动完整沙盒。
