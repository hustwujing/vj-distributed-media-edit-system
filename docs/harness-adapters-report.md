# Harness / LLM Adapter 专项验收

日期：2026-11-02。核心实现与本轮专项验收完成。工作分支：`codex/harness-adapters`；worktree：`/data2/wujing03/video-use-worktrees/harness-adapters`。基线为 `ef98b76`，包含建立 worktree 时主目录已有开发代码的独立快照；本轮改动另行提交，不修改主目录。未合并、未部署。

按用户最后限定，本轮不运行项目全量回归，也不重跑历史 60 个媒体 case。以下结论只适用于 Harness / LLM Adapter 及直接相关链路。[技术设计](harness-adapters-design.md)说明配置、数据库和接口；[TODO](harness-adapters-todo.md)保留未关闭检查项。

## 已实现

- Codex、Claude Code、DeepSeek Harness 的协议适配与公开事件归一；Claude/DeepSeek SDK 固定版本，在统一 OS 沙盒内执行。
- Responses、Anthropic Messages、Chat Completions 协议适配，共用受控路由、额度、取消、敏感信息检查和实际请求账本。DeepSeek 当前 Harness 实际使用 Messages 协议。
- 任务有效配置与每 attempt 配置冻结；同任务切换不重置预算，调用记录按 attempt 保留原 Harness / model 归属。
- 公开上下文交接、历史原文/工具记录分页检索、不透明原生快照及逐版本外层描述；不解析厂商私有会话格式。
- 持久化切换、撤销、安全边界和未知副作用阻断；已有 workflow / 上游 task ID 保留；旧租约迟到事件被拒绝。
- 页面 Agent 配置选择、切换阶段和按 Harness 的调用展示。

## 定向自动化

| 范围 | 结果 | 说明 |
| --- | --- | --- |
| Python 专项 | 91 / 91 通过，0 跳过 | Harness/配置、切换、SSE 流式、检查点、账本、公开正文、Redis、出站策略及取消 |
| 相关前端 | 19 / 19 通过 | Agent 设置、用量和文本流合并 |
| 真实浏览器切换 | 7 项通过 | 选择、待切换、撤销、激活、输入保留、刷新持久化、跨用户拒绝 |
| 真实跨节点工具与恢复 | 5 轮通过 | Claude 新会话/恢复 → DeepSeek 新会话/恢复 → Claude 新会话；实际读取新上传文件 |

Python 最后一轮日志为 `/tmp/vj-harness-final-targeted-2.log`，耗时 20.54 秒。使用隔离真实 PostgreSQL、Redis、HTTP S3 协议 fixture 和实际 Linux 进程/沙盒；不是生产数据库或存储服务。浏览器控制测试有两个 API 进程、Nginx 和真实 Chromium，但不启动模型执行器；真实模型/SDK 验证另列。

专项测试覆盖的异常包括：错误协议/参数、远端 URL/服务端工具绕过、跨分片凭据、缺失/中断 usage、超限不多扣调用、未闭合工具、提交结果未知、重复/过期切换请求、取消优先、租约过期/迟到事件、回收完成但旧租约 active 标记残留、检查点未提交 session ID、换配置后禁用不兼容原生恢复、历史分页和跨项目拒绝。

## 真实服务与流式

用户提供的服务地址/凭据已实际访问。凭据仅存在仓库外权限为 600 的私有文件，不写入代码、报告、浏览器配置或沙盒。初始旧凭据 401 不计通过；使用新凭据后模型目录与实际模型请求成功。

| 验证 | 结果 |
| --- | --- |
| Claude `claude-4.6-sonnet` + Messages | 实际 SDK 调用、工具执行、原生跨节点恢复、双向交接均成功 |
| DeepSeek `deepseek-v4-flash` + Messages | 实际 SDK 调用、工具执行、原生跨节点恢复、双向交接均成功 |
| DeepSeek + Chat Completions 协议适配器 | HTTP 200，9 个安全 SSE 帧，6 次公开文本分片；usage 为输入 39、输出 7、合计 46 |
| Claude 原生 usage 与账本 | 抽查真实请求：SDK 输入含缓存 10430 / 输出 9，与网关账本一致，实际请求仅 1 次 |
| Claude 长中文宿主增量 | 一轮收到 564 次增量，持续 73.359 秒，最大分片间隔 0.680 秒；完整消息补齐并保存 |
| 长中文浏览器完整性与刷新 | 通过：宿主 552 次增量，浏览器 502 次正文渐进更新；全文/尾部完整，刷新保留，0 页面 JS 错误 |
| Claude / DeepSeek 请求中取消 | 通过：本地执行分别在 0.165 / 0.133 秒结束；租约已释放、清理记录 complete；上游是否取消/免计费未知 |

Claude 的增量经真实 SDK、受控网关、统一文本发布器进入公开消息。SSE 单元检查在调用 `finish()` 或到达下一帧之前断言完整正常文本已转发；覆盖中文按字节拆分、完整消息覆盖 delta、尾部和跨帧保护。

最后长文本轮耗时 281.95 秒，首段文字约 8.61 秒，增量持续 270.96 秒，最大分片间隔 1.178 秒。上游生成速度波动明显，但浏览器持续更新；本次不将这些单次观测解释为性能 SLA。最终流式/取消运行 `live-stream-08` 整体通过。`cross-harness-live-07` 的前 5 项通过，第 6 项因测试脚本误判恢复类型而整体未通过；修正后仅重跑受影响的流式与取消项，未把失败报告改写为成功。

精简且不含凭据的证据见 [harness-adapters-evidence.json](harness-adapters-evidence.json)。原始输出保留在 `/data2/wujing03/harness-test-tmp/` 和私有测试目录，未上传用户服务凭据。静态检查 `git diff --check` 通过，已检查本轮变更文件不含用户提供的 API Key。

**DeepSeek 当前 SDK 只有完整消息事件，本轮没有将其伪称为逐字流式。** 同一轮可在每条消息完成时展示，工具事件正常推进；更细粒度正文需要可用的公开增量协议再接入。公开协议参考：[SDK 通知](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sdk/protocol/README.md)、[Python SDK](https://github.com/deepseek-ai/deepseek-harness/blob/master/python/sdk/README.md)。实际行为以固定的 `0.2.1rc2` 和本次测量为准，主分支文档不代替版本验证。

## 发现与处理

| 问题 | 处理 |
| --- | --- |
| 初次将 DeepSeek Harness 当成 Chat Completions | 按实际 SDK 请求改为 Messages，重新验证真实服务 |
| DeepSeek 原生工具尝试嵌套不可用沙盒 | 在已经受 OS 隔离的桥接进程内配置原生权限模式；保留外层 UID、命名空间、网络及 cgroup 限制 |
| SDK 依赖在任务 UID 下不可读 | 修正独立 worktree 的运行包 ACL，真实沙盒复测通过 |
| 最新数据库 session ID 不一定已 checkpoint | 增加按检查点版本原子保存的外层描述，恢复读取已提交 ID |
| 长历史仅靠近期交接片段 | 增加归属/租约约束的只读分页查询 |
| 新协议的文本/工具参数可能跨 SSE 分片 | 扩展逻辑通道保护，同时证明正常帧不会留固定尾部 |
| 测试代码绕过构造函数、晚修改预算默认值 | 按冻结配置和新 Adapter 初始化更新夹具，保持原断言含义 |
| 真实测试脚本重复发布素材、空正文等可见、误判新增轮次恢复方式 | 修正测试脚本，保留失败记录；不作为系统功能通过证明 |

## 尚未验证或开放的边界

- 不运行项目全量/历史 60 case；没有真实长媒体链路中途切换并完成付费视频合成的验收结论。
- 提供的网关目录不含 Codex 使用的 GPT 模型；Codex 适配抽取做协议/正文/隔离回归，本轮不宣称完成真实 Codex 服务验收。
- Chat Completions 协议真实可用，不等于已经登记一个使用它的 Harness 组合。
- 不保证任意模型的图像、超长上下文和推理参数兼容；当前真实验证以表中模型为准。
- 原生用户问卷和子 Agent 全量工具事件未开放；原生会话的私有推理不会跨 Harness 迁移。
- 两节点为同一宿主上的独立目录/身份，S3 为协议 fixture；不是异构多机容灾或真实生产对象服务验收。
- 进行中切换在安全边界生效；未知副作用或异常未恢复的运行态会阻断，而不是自动重跑。取消本地执行不等于上游已取消或免计费。

## 合并阶段补充

主目录既有改动先保存为 `030da95`。在独立合并区仅应用本轮相对开发基线的增量，保留其他窗口的最新修改。合并代码专项复测：Python 110/110，0 跳过；前端 19/19；浏览器切换 7 项通过。新临时目录最初缺少任务读取 ACL，补齐后真实隔离测试通过。

用户随后追加了合入后全量测试、部署和全部真实 case 的要求；这些工作将另记完整验证报告，本报告的专项通过不替代后续全量验收。
