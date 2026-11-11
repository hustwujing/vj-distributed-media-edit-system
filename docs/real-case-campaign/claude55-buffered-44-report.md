# Claude 5.5 buffered 示例库 44 项报告

日期：2026-11-11。环境：独立 `sandbox-agent` 命名空间、数据库
`distributed_agent_sandbox`、API 端口 20766/20767、对象前缀
`distributed-agent-sandbox`。本报告不复用主环境的数据或服务。

## 冻结配置

- 用户模型及五个专家角色：`claude55` / `claude-sonnet-5-5`。
- Manager、Diagnostician、Task/Delivery Reviewer、Editing Implementer：
  `response_transport=buffered`。
- 原生 Claude Harness：`native_response_transport=buffered`；Gateway 向上游发送
  `stream=false`，再把完成响应转换为 SDK 完成事件。
- Claude Runtime：`agent-runtime-claude@sha256:63148fb54918b2164dc0f07f6c699c4ad1060758bb3171f55390f6d2a5d2680d`。
- Runtime 镜像定向只读/无网络验证通过；镜像 UID/GID 为 10001:10001，未发现凭据标记。

## 稳定性验证

- 最小文本请求 3/3 成功，工具请求成功。
- 真实 Manager 请求 3/3 成功：工具 Schema 33.7 KB、422 个联合分支、上下文 45.5 KB，
  响应模型均为 `claude-sonnet-5-5`，耗时 5.1–15.5 秒。
- 原生 Claude Agent SDK 经 buffered Gateway 成功：SDK 收到完整 Assistant/Result，
  上游模型未改写为 Kiro。
- 真机 `image-layers` 实际在 OpenSandbox 中运行 `claude_code`，多轮模型与工具调用、
  产物校验、审查和 cleanup 全部完成。

## 修复记录

- `3ccbe11`：专家角色与原生 Harness 传输方式独立配置并冻结；原生 buffered 响应转换为 SDK SSE。
- `26dd2a8`：44 项驱动冻结并校验 Claude 5.5 配置。
- `ae68860`：最终审查沿 `repair_candidate` 保留退役 Operation 的实际执行来源。
- `aa00edd`：显式变换参考系优先，不把素材偶然方向升级为冲突验收条件。
- `ba8e115`：源输入缺少明确要求保留的属性时，执行前请求用户决定，禁止静默缩小要求。
- `faa4659`：inspect 无可用 receipt 时允许诚实的空证据 inconclusive/reject；pass 标准不变。

## 轮次结果

首轮跨定向修复完成 44 项：38 项业务成功，6 项非程序边界；该轮用于发现和修复通用问题，
不能作为最终干净轮。

最终代码完整轮：44 项全部启动并结束，36 项成功，8 项非程序/外部边界，程序错误 0，
明显可优化提示词错误 0。成功项平均 193.326 秒，最短 92.270 秒，最长 518.893 秒。

| Case | 分类 | 原因与用户可见行为 |
|---|---|---|
| agent-portrait | 配置契约冲突 | 示例明确要求 Codex，本轮强制 Claude；审查拒绝虚假 Codex 来源并展示选择。 |
| local-split | 输入能力冲突 | 源素材无音轨；执行前说明不能保留并提供取消要求、补音频或停止。 |
| image-text-effects | 缺少输入 | 未给标题文字及替换/并存关系；执行前提出具体问题。 |
| image-layout-guides | 缺少输入 | 仅有一张图但要求排列三张；请求补图或明确复制及画布。 |
| video-version-compare | 信息型请求 | Manager 直接比较现有两个版本并给出证据限制与建议，无需制造新产物。 |
| video-shot-notes | 缺少输入 | 未给时间段与修改内容；请求逐条时间线批注和交付选择。 |
| image-adjustment-layers | 模型实现质量 | Claude 重复 JSON 结构错误；有界诊断后展示精确修订/停止选项，未放宽 Schema。 |
| image-erase | 外部模型质量 | Qwen 三次重绘文字；每次候选均被拒绝，最终展示继续、换方法、组合清理或停止。 |

## 证据

- 首轮及定向修复：`/data2/wujing03/demo-library-claude55-*-r24` 至 `r30`。
- 最终完整轮：`/data2/wujing03/demo-library-claude55-clean-round-r31`、`r33`、
  `/data2/wujing03/demo-library-claude55-local-split-r32`、
  `/data2/wujing03/demo-library-claude55-mask-refine-r34`、
  `/data2/wujing03/demo-library-claude55-clean-round-r35`。
- 每项保留冻结配置、方案确认、状态快照、浏览器截图、交付文件、重进和 cleanup 证据。

最终正式发布必须使用同一源码、Claude Runtime digest 和私有配置通过一次完整门禁；
`targeted_only` 发布不能作为正式发布结论。
