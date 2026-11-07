# 外部服务与供应商接入

适用于新增或替换图片、视频、转写等外部处理服务，以及修改认证、请求/响应映射、结果下载、供应商任务恢复和取消核实。本文说明供应商侧协议如何接入现有执行设施。operation 的契约与实现注册按[新增与扩展 operation](add-operation.md)处理，通用状态推进按[工作流开发说明](change-workflow.md)处理，调用计量按[可观测性与用量开发](change-observability-usage.md)处理。

## 1. 先确认协议和接入层级

涉及 API 请求路径中的同步依赖或长等待时，同时遵守 [API 并发与耗时隔离](change-api-concurrency.md)：使用原生异步接口或已有专用池，不阻塞事件循环和默认工作线程。API 依赖池与业务执行器/供应商并发预算分别负责各自边界，不能互相替代。

阅读供应商当前协议和最相近适配器，记录已核实能力及证据来源。没有文档或实际响应支持的幂等、取消、重试和恢复能力保持未知，不能仅凭接口名称或传入请求键推断。

| 需要确认 | 开发前应明确 |
| --- | --- |
| 业务能力 | 输入类型、尺寸/时长/数量限制、输出角色，是否沿用已有 operation 契约 |
| 执行协议 | 同步返回产物，或异步返回 ID 后查询/收取；错误和终态取值 |
| 提交语义 | 一次请求可能产生的副作用、明确拒绝条件、结果未知时的核实方式 |
| 任务身份 | 供应商、协议版本、模型、任务 ID、项目/步骤修订及认证上下文 |
| 重试与幂等 | 查询/下载能否重试；重复提交的保证、有效期及作用范围 |
| 取消 | 不支持、接受请求、确认停止的区别，以及已经完成或正在运行时的行为 |
| 结果与费用 | 文件或链接格式、有效期、大小、真实媒体类型、用量回执和费用币种 |

同一业务契约更换后端时优先增加版本化 implementation；输入输出语义变化时重新审视业务契约。供应商适配器只转换协议，不另建任务池或提交后无限轮询，不以新增供应商为由增加调度类型。

## 2. 定位实现并检查完整调用链

| 入口 | 阅读或修改目的 |
| --- | --- |
| [generation_service.py](../../webapp/generation_service.py) | prepare、路由快照、协议注册及 submit/poll/download/cancel 分派 |
| [ark_generation.py](../../webapp/ark_generation.py)、[wan_generation.py](../../webapp/wan_generation.py)、[image_generation.py](../../webapp/image_generation.py) | 现有异步视频及同步图片适配器 |
| [generation_provider.py](../../webapp/generation_provider.py)、[image_providers.py](../../webapp/image_providers.py)、[agent_transcribe.py](../../webapp/agent_transcribe.py) | 旧 jobs 协议、图片服务和转写入口 |
| [workflow_runtime.py](../../webapp/workflow_runtime.py)、[manual_async.py](../../webapp/manual_async.py)、[agent_gateway.py](../../webapp/agent_gateway.py) | 工作流、手动任务、Agent 直接入口的授权与调用 |
| [provider_calls.py](../../webapp/provider_calls.py) | 提交意图、路由绑定、会话、回执和用量记录 |
| [egress_policy.py](../../webapp/egress_policy.py)、[cancellation.py](../../webapp/cancellation.py)、[provider_relay.py](../../webapp/provider_relay.py) | 受控 HTTP、网络策略、取消及受限代理 |
| [external_cleanup.py](../../webapp/external_cleanup.py)、[execution_reaper.py](../../webapp/execution_reaper.py) | 原任务查询/取消、清理租约及子进程配置 |
| [generation_errors.py](../../webapp/generation_errors.py)、[execution_contracts.py](../../webapp/execution_contracts.py) | 安全错误码、阶段分类和恢复语义 |
| [operations.py](../../webapp/operations.py)、[operation_extensions.py](../../webapp/operation_extensions.py)、[scheduling.py](../../webapp/scheduling.py) | 可用性、可信模块装载、节点声明与供应商并发 |

`generation_service.register_provider(protocol, provider)` 当前要求 submit、poll、download，可选 cancel。它注册的是协议分派，**不会自动接入新供应商的 prepare、配置选择、认证、能力判断或同步返回处理**。新增同步协议需核对对应同步实现；不能为了套注册接口伪造可查询任务。

逐一核对计划校验、实际提交、恢复查询、结果收取和外部清理用到的配置及模块。独立清理进程使用受限环境变量清单，执行器能调用不代表清理进程拥有相同接线。仅对本次支持的业务入口开放能力，未接通的入口应明确拒绝。

## 3. 实现准备、认证和受控请求

- 在提交前验证参数、参考素材归属、输入修订/哈希和实际媒体约束。失败时明确“未提交”；不能把本地参数错误消耗成一次外部生成。能力目录的 configured_unverified 只说明配置存在，实际提交仍需校验。
- endpoint、模型和凭据由可信服务配置选择，Agent 只提供业务参数及受控素材引用。沿用受控 session、地址和 DNS 检查、连接/读取期限与取消句柄；不通过裸 requests、curl 或新增通用 HTTP 代理绕过。
- 内部服务主机需由部署配置显式允许。不要因适配失败放开元数据地址、任意内网地址、重定向、环境代理或 netrc 凭据；权限与下载策略改变必须单独验证。
- 保存恢复所需的 provider、protocol、model、operation、输入摘要及实现标识。API key 和内联素材内容不进入路由快照或公开结果；如服务依赖 Cookie 会话，复用受保护的服务端会话保存/恢复路径，不导出到 Agent、日志或诊断包。
- 查询、收取和取消按保存的协议/供应商身份路由。默认配置、模型或地址变化不能将旧任务静默发送到新后端；凭据轮换需确认仍可访问原账号任务，不确定时停止调用并说明恢复条件。
- 视频提交句柄保存provider/protocol/model/operation与宿主凭据的HMAC绑定值，绝不保存密钥。查询/取消校验该绑定并使用冻结地址，不依赖当前默认地址/模型；密钥变化、句柄改写或缺失绑定均在HTTP前拒绝。新提交仍要求prepare时地址/模型与当前配置一致。不要为支持恢复而取消网络或凭据边界校验。
- 如果新增供应商要求预上传素材，定义持久化上传句柄、归属、有效期、重启复用及清理语义。现有内联参考图适配器不自动提供这些保证。

## 4. 保存提交意图并处理恢复

正常顺序是：校验授权和输入 → 保存提交意图并消费适用额度 → 发起一次请求 → 保存 ID/回执 → 调度后续查询和收取。调用意图或预算提交失败时不能继续发起外部副作用；网络调用不放在长控制事务内。

- 复用 `provider_calls.intent`，关联任务、步骤、revision、attempt 和已确认意图。现有请求键以工作流/步骤/修订绑定，同一修订不能重复提交；此本地防重不等于上游幂等。
- 对同步服务按返回的真实产物处理；没有可靠 ID 时不能宣称可查询恢复。网络中断、超时或解析失败可能发生在上游接单之后，不能默认归为“未收费、可重提”。
- 异步 submit 得到 ID 后持久化并返回，poll 每次只查询原任务，collect 收取已有产物。未知状态、成功却缺少产物、非法 ID 或无法解析的响应不能伪装成功。
- jobs 服务的原生 queued 回执在适配层映射为调用账本的 waiting，同时保留原始轮询响应。不能把正常排队当作轮询错误，也不能把所有未知状态一律降级为等待。
- 通过结构化错误码和协议状态区分提交前失败、明确拒绝、提交未知、任务失败、下载失败和产物不合格。复用错误分类，不能按错误文案的语言或关键词决定是否重试。
- 查询、下载和验收重试应复用已有任务/结果，次数与期限有界。需要新生成时走显式修订及用户授权流程；不得静默切换供应商、模型或输入来补救失败。
- 正常回执经过当前租约校验。失权或取消后到达的任务 ID、终态及用量走受限的 late_receipt 路径，只更新已有调用证据并安排清理；不能创建新业务、恢复步骤或发布成果。
- 调用额度、供应商并发和实际用量分别记录。提交未知仍需保留未结束调用占用；轮询和下载不算新生成授权，但应记录其网络调用和耗时。新增额度类别或 provider_class 要同步直接、手动和工作流入口，避免漏计或重复消费。

## 5. 收取产物和核实取消

下载按供应商配置校验协议、主机、端口及重定向，API 的 Authorization 不能转发给产物存储域。响应内容和文件名均不可信，使用有大小限制、可中断的流式收取及受控落盘路径，再交给现有技术验收和成果发布流程。

文件必须满足声明角色、媒体类型及适用的尺寸/时长约束，不能只相信 Content-Type 或 HTTP 200。链接过期时优先查询原任务核实或刷新下载信息；没有协议支持时保留可诊断失败，不重新生成来换链接。正式发布仍校验租约与版本，具体约束见[分布式执行开发说明](change-distributed-execution.md)。

取消分为业务停止、本地连接中断和供应商是否停止。只有协议明确的确认或后续查询的终态才能结束核实；“已接受取消”不等于“已停止”。不支持时明确返回不支持，不猜测取消 URL。

外部清理复用独立清理租约、代次、次数上限和期限。清理进程只取消/核实已保存任务，不能再次提交生成。已成功、已失败、已取消均可意味着上游已经结束，但不能将“清理完成”统一解释为“成功取消”；终态和晚到用量分别保存，原业务取消状态保持有效。

## 6. 验证与完成标准

按生命周期覆盖真实协议解析和故障窗口，不只验证注册或提交返回 200。测试替身记录请求次数与身份，证明恢复查的是原任务且没有额外提交生成请求；取消等其他动作可能使用 POST，需按业务动作区分。

| 场景 | 应证明的结果 | 相关测试入口 |
| --- | --- | --- |
| 参数、路由和协议 | 输入错误先拒绝；同步/异步语义真实；未知状态不成功；配置变化不重路由旧任务 | [test_uniform_generation.py](../../tests/test_uniform_generation.py) |
| 认证与网络 | 重启恢复同一供应商会话；越权、非法地址和 DNS 被拒绝；下载不带 API 凭据 | [test_provider_sessions.py](../../tests/test_provider_sessions.py)、[test_egress_policy.py](../../tests/test_egress_policy.py) |
| 提交未知及恢复 | 不重复生成；有 ID 查询原任务；迟到回执不能发布业务成果 | [test_provider_relay.py](../../tests/test_provider_relay.py)、[test_distributed_races.py](../../tests/test_distributed_races.py) |
| 取消与清理 | 支持/不支持/未确认有区别；连接可中断；清理有界且不重提生成 | [test_external_cleanup.py](../../tests/test_external_cleanup.py)、[test_distributed_cancellation.py](../../tests/test_distributed_cancellation.py) |
| 收取、验收和交付 | 真实文件符合契约；已有结果可跨步骤使用；不合格产物阻止下游 | [test_generation_chain.py](../../tests/test_generation_chain.py)、[test_quality_workflows.py](../../tests/test_quality_workflows.py) |
| 用量与诊断 | 重复、失败及取消回执不误计；缺失费用保持未知；错误不泄露上游正文 | [test_task_usage.py](../../tests/test_task_usage.py)、[test_observation_faults.py](../../tests/test_observation_faults.py) |

例如，修改供应商协议、会话与清理时：

```bash
.venv/bin/python -m tests.run_distributed_suite tests.test_uniform_generation tests.test_provider_sessions tests.test_external_cleanup tests.test_egress_policy
```

按实际影响增加对应模块；发布前按[开发与测试](../development.md)完成全量检查。真实服务验收另用独立项目和明确的调用范围执行，参考 [run_live_generation_case.py](../../tests/run_live_generation_case.py) 及适用的 live 驱动；先确认脚本使用的配置和上游，不把本地模型开关当作真实供应商验证。

交付说明列出 operation/implementation、供应商协议与模型、配置与装载、各入口支持范围、提交未知策略、已核实的取消/幂等能力、请求次数和实际文件验证结果。未验证的取消或恢复能力明确保留限制；同步配置、能力参考及相关用户说明。[统一生成设计](../uniform-generation-design.md)可作为既有方案背景，其历史测试不证明新供应商可用。仅修改开发文档时检查链接、接口和命令即可，不触发真实调用或部署。
