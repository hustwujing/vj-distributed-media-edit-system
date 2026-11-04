# 新增 LLM、模型配置或原生协议

适用于新增模型名称/服务端配置，以及扩展 Responses、Chat Completions、Anthropic 之外的模型传输协议。
执行 harness、专业角色和模型传输是不同契约；只添加模型配置不能自动获得新的工具权限。

## 同协议新增模型

在私有配置中登记模型、base URL、凭据环境变量引用、推理参数和预算，复用现有 ModelAdapter。
模型配置必须在创建执行轮次时冻结；不得从用户 Prompt 或 Task 参数读取任意服务 URL/API key。
同一原生协议也要实测图像、最大输出、推理选项、JSON 输出和用量回执，不能按模型名字推断兼容。

## 新增协议

入口为 `model_adapters.py`、`agent_profiles.py`、`usage_protocol.py`、`egress_policy.py`、
`agent_gateway.py`、`workflow_agents.py` 和 `task_usage.py`。

1. 实现并登记 `headers`、`path`、`prepare`、`receipt`、`conversation`、`response_text`。
2. `prepare` 强制使用已冻结模型，限制输出预算，并拒绝远程文件、任意 URL 和服务端扩权工具。
3. `conversation` 转换授权文本/图片；`response_text` 只返回完成的公开文本，不暴露隐藏推理。structured-profile-v3由model_tool_protocol解析唯一原生工具调用，参数先经本地Schema校验再编译；纯文本不构成控制动作。格式错误在原轮次/Token预算内反馈纠正，空或不完整的上游响应单独归类，不伪造工具结果。
4. 回执解析覆盖 JSON、SSE、跨块拆分、重复累计值、取消、不完整响应和缺失字段。
5. 原始 total 缺失时保持缺失；预算可以使用已知 input+output 推导值，但记录为 derived，不能伪造上游回执。
6. 请求前持久化调用意图和预算预留；取消后的用量只能补写既有记录，不能恢复任务或发布结果。
7. 接入配置校验、模型列表、实际执行方及诊断。SDK harness 的协议兼容表也需核实，不允许静默回落到另一协议。
8. 响应结构观测记录 payload 类型、公开文本长度/哈希、消息或动作对象数量、JSON 错误类型和位置；不记录完整提示、私有推理或敏感正文。迟到响应必须受 request epoch、Task revision 和 generation 约束，不能覆盖已确认的取消或新结论。

## 验证与完成标准

先以本地协议服务测试请求字段、流解析、错误分类、取消、限额和去重；再在独立实例测试实际模型。
真实验证至少覆盖纯文本、授权图片、结构化提案、工具后续轮次、批量检查后 finalize、格式纠正上限、超时/限流/错误响应和用量对账。
保留脱敏请求配置与响应状态、实际模型和协议、任务/尝试关联、Token 完整度及结果验证。

不能在日志、截图、报告、PR 或源码中保存认证头、Cookie、API key、完整提示词或供应商敏感正文。
外部服务不可用时报告依赖状态和程序应对，不通过切换模型、虚构产物或重置预算掩盖失败。

结构化多公开文本块、空响应、不完整响应和非法 JSON 分开分类；结构错误发生在文本提取前也要发出脱敏结构事件。不能为了接受格式而拼接多个互相冲突的动作。业务 report 传对象而非二次 JSON 字符串；控制协议和 action capability 由角色固定。Provider 元数据中的预算分组与并发分组独立，模型名称/传输协议不授予业务操作权限。

Responses 的 `phase=commentary` 是过程消息，不能作为业务动作执行；只解析唯一的 `final_answer`。没有 phase 的当前协议单消息仍按唯一完整文本处理；多个最终消息、最终与未标记消息混杂、未知 phase 或不完整消息均拒绝，不拼接或任取最后一条。响应结构错误与最终文本的 JSON 语法错误必须分别记录，不能把内部错误提示的长度当作模型回复长度。参考[官方阶段定义](https://developers.openai.com/api/docs/guides/deployment-checklist)。

业务报告在系统提示和冻结 Task 上下文中复用同一结果契约，均要求 report 对象；不能一处要求对象、另一处要求 JSON 字符串。观测记录公开消息阶段的数量与剩余检查/纠正额度，不记录私有推理内容。

结构化 harness 在生成前按冻结的`input_token_policy`确定预留方式。`native`调用适配器的`input_token_request`：Responses使用`/responses/input_tokens`，Messages使用`/messages/count_tokens`，沿用冻结模型、路由及凭据；此模式下接口不可用或计数非法仍拒绝生成。对于已验证且明确配置的Claude/DeepSeek路由，可选本地预估策略，避免调用已知缺失的计数接口。任何方式都必须先校验租约和预算再生成，不按零计数放行。真实回执用于结算；缺失或存在矛盾的回执保留预算。

计数接口不一定完整实现官方协议。本机网关实测漏计图片，因此视觉计量策略 `vision_tokenizer` 随 AgentProfile 冻结，参与配置摘要；默认配置可由 `VIDEO_USE_MODEL_VISION_TOKENIZER` 指定。策略必须匹配实际模型与路由，不能因原生协议相同就采用同一模型费率。当前已实测 gpt-5.6 路由使用 `patch32_1_2`：按未缩放32px patch数量×1.2向上取整，每图另留256；8张1034×650联系表实测输入6683，预留9030（含文本和结构余量）。`native` 只用于已验证完整计入图片的计数服务；`conservative_tiles512` 是可显式选择的保守tile策略，不作为所有模型的默认值；未配置的模型拒绝图片请求，纯文本不受影响。新增模型必须对照真实多图回执核实策略。

另加计数的1%（至少256）作为请求结构余量。禁止把Base64字节数当作图片Token，也不得用其他模型的高费率导致系统性假耗尽。日志分别保存native count、图片预留、结构余量、计量策略摘要及实际回执，不能保存图片或完整提示正文。

协议依据：[OpenAI 输入计数](https://developers.openai.com/api/docs/guides/token-counting)、[图像计量](https://developers.openai.com/api/docs/guides/images-vision)、[Claude Token 计数](https://platform.claude.com/docs/en/build-with-claude/token-counting)。

原生工具接入必须单独实测工具选择、strict参数和Token计数；HTTP成功不能当作Schema约束确已生效。本机DS4.1路由拒绝强制tool_choice，但auto可以返回工具调用，宿主仍严格要求恰好一个已授权工具动作。不要按模型名猜测或隐藏适配器差异。记录实际请求模式、Schema摘要、回退原因及逐次费用/用量完整度；提供非法结构、缺字段、多动作、额外字段、结束不完整和网关拒绝的测试。

Responses的非strict回退必须显式发送工具字段`strict: false`；省略该字段仍可能由上游自动启用strict。Anthropic和Chat Completions省略strict扩展走各自默认非strict协议。不能把三个协议的默认值混用，也不能仅用“请求不含strict”证明降级成功。若Responses网关连false字段也拒绝，应保留真实接口失败，不能谎报已完成非strict回退。参考[Function calling默认值](https://developers.openai.com/api/docs/guides/function-calling)。

## 本地输入预算预估

配置发布必须与代码版本配套。新增 AgentProfile 字段或改变策略语义时，候选私有配置单独暂存，用候选冻结源码加载验证；不得提前覆盖仍被旧服务重启读取的 `.agent.env`。通过门禁后，停止旧实例、切换服务启动代码，再原子替换候选配置并启动验证。回滚也须恢复配套配置。运行进程仍持有旧环境不能证明磁盘配置可安全重启；检查必须使用实际部署副本重新加载当前配置，不得通过忽略未知字段掩盖版本错配。

用户已明确授权对缺少原生计数接口的Claude和DeepSeek采用本地预估。`model_token_estimate.py`提供版本化`claude_bpe_estimate_v1`和`deepseek_bpe_estimate_v1`；策略与模型、协议一起进入AgentProfile快照。它使用固定cl100k参考分词、逐模型校准系数及消息余量，不声称该分词器等于厂商原生tokenizer。不能仅按模型家族复用系数，也不能据少数短文本验证整个模型。未登记的模型/协议组合拒绝此策略。

文本和完整工具Schema都计入；图片从真实内容块中提取，按尺寸/已验证上界另计，工具参数里的普通JSON不得误认成图片。Base64不会作为文本Token累加。参考分词数据在部署时下载并核验固定SHA，安装到依赖环境的share目录；推理时只读本地文件，缺失或损坏拒绝生成，禁止运行中隐式联网下载。部署必须验证实际服务UID能读取并执行分词。

校准须重建真实请求并比对原request_sha256，再对照上游input_tokens；明确这不是独立验证上游精确性。覆盖中英文、代码、工具Schema、长上下文、单图及多图，保存低估次数和最不利余量。预估只是准入预留，无法保证厂商永不超差；真实用量超出预留仍如实入账并占用预算，后续请求不能继续超支。未知回执不改为预估实际用量。

`workflow.model_input_estimate`在创建预留前记录策略摘要、参考计数、图片预留及余量；`workflow.model_input_estimate_checked`和`workflow.model_budget_reserved/settled`通过reservation_id记录预留、回执偏差和结算。前者通过调用上下文关联，不能伪造尚未创建的预留ID。禁止记录提示正文。实际存在输出但回执output_tokens=0时保留原始数字、标记conflict，不生成可信合计、不释放模型预算。

端上模型选择复用服务端预设，不允许前端提交任意model/base_url/key。新增模型同步model_selection.efforts，按实际原生档位展示；DeepSeek的medium别名映射为high，不能作为独立“中”档展示。见[官方推理档位](https://api-docs.deepseek.com/guides/thinking_mode/)。
