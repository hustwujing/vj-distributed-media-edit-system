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
3. `conversation` 转换授权文本/图片；`response_text` 只返回完成的公开文本，不暴露隐藏推理。结构化引擎只解析单个 JSON 对象（可接受完整 JSON 围栏）；非空格式错误在原轮次/Token预算内反馈纠正，空或不完整的上游响应单独归类，不伪造工具结果。
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
