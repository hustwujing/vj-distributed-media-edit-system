# 可观测性与用量开发

适用于修改结构化日志、事务审计、阶段指标、当前活动、任务诊断、观测采集与告警，以及调用账本、Token 和费用汇总。业务状态及恢复约束见[分布式执行开发说明](change-distributed-execution.md)，上游回执见[外部服务与供应商接入](change-external-providers.md)，页面呈现见[前端与产物展示开发说明](change-ui-artifacts.md)。

设计背景见[可观测性设计](../observability-design.md)和[任务消耗账本设计](../task-usage-design.md)，已部署观测组件和运维入口见[可观测性手册](../observability-operations.md)。历史验收不能代替当前改动验证；不要将设计中的候选后端写成已经部署的组件。

## 1. 先确定数据职责与实现入口

| 数据 | 职责及故障边界 |
| --- | --- |
| 任务、租约、授权预算和供应商提交意图 | 控制执行与恢复，必须按既有事务可靠提交；不能按“指标丢失”处理 |
| 事务状态审计 | 与对应状态写入一同提交或回滚，用于还原已提交转换 |
| 调用账本与用量回执 | 保存消耗证据，不授权执行；提交前记录与提交后补回执分别处理 |
| 阶段指标和结构化遥测 | 辅助诊断，写入/输出有界，失败应暴露观测缺口，不改变业务结果 |
| 公开活动和正文 | 用户可见进展，不等同于内部推理、调用账本或正式完成状态 |

| 入口 | 阅读或修改目的 |
| --- | --- |
| [telemetry.py](../../webapp/telemetry.py)、[observation_http.py](../../webapp/observation_http.py) | 字段白名单、上下文、异常投影、非阻塞输出、请求关联 |
| [observation_schema.py](../../webapp/observation_schema.py) | 任务关联和事务审计触发器 |
| [execution_metrics.py](../../webapp/execution_metrics.py)、[live_activity.py](../../webapp/live_activity.py) | 阶段计量、有限连接预算与公开活动 |
| [task_usage.py](../../webapp/task_usage.py)、[usage_protocol.py](../../webapp/usage_protocol.py) | task/run 关联、调用记录、流式回执解析及汇总 |
| [provider_calls.py](../../webapp/provider_calls.py) | 供应商提交与查询证据、用量及费用原始来源 |
| [usage_api.py](../../webapp/usage_api.py)、[task_usage.js](../../webapp/static/task_usage.js) | 授权范围、分页及任务/需求/项目用量展示 |
| [diagnostics.py](../../webapp/diagnostics.py)、[diagnostics_api.py](../../webapp/diagnostics_api.py) | 只读证据汇聚、部分结果、所有者视图和诊断下载 |
| [observation_collector.py](../../webapp/observation_collector.py)、[observability.html](../../webapp/static/observability.html) | journal 采集、索引、指标导出、告警和受保护看板 |
| [dynamic_cluster.py](../../deploy/dynamic_cluster.py) | 当前独立观测服务的配置、部署与维护流程 |

新增字段或事件前，说明采集点、单位、关联键、权限、存储位置、去重规则、缺失语义和留存方式。优先扩展已有数据来源，避免为同一调用维护第二份累加总数。

## 2. 修改关联、日志和事务审计

- 复用 `telemetry.bind`、span 和子进程上下文，关联适用的 request、task、workflow、step/revision、attempt/generation、worker/node、release 与供应商请求标识。不要把业务步骤重试与整个任务重新创建混为一谈。
- 线程、子进程、独立清理和接管后需传递对应上下文；嵌套 span 退出应恢复上层。子进程错误内容不能伪造 task/attempt 或覆盖父进程的可信关联。
- `telemetry.clean` 采用字段白名单及受限标识格式。新字段需同时核对生产者、脱敏投影、采集索引、查询和测试；随意传入一个字段可能被丢弃，不能据此宣称已观测。
- 结构化遥测日志不保存 prompt、响应正文、源码行、局部变量、完整脚本、认证头、Cookie、签名 URL 或任意异常消息。使用安全错误码、异常类及代码位置，不能为了调试绕过过滤；公开对话和工具历史由各自受控路径保存，不应复制进遥测。脱敏覆盖嵌套字段和跨分片敏感值。
- 区分“准备提交”“观察到响应”和“事务已提交”。日志发出后业务事务仍可能回滚；新增审计对象检查 SQLite 与 PostgreSQL 触发器和迁移，审计状态随业务事务回滚。
- 跨来源时间戳只用于辅助排序。因果关系结合审计 seq、attempt/generation、请求键及版本判断，不能仅凭最后一行日志覆盖业务状态。

## 3. 修改指标、活动与故障降级

- 阶段计量复用 `execution_metrics.phase/measured`。阶段名保持有限集合，新增时同步采集器和 Prometheus 导出允许列表；任务、用户、请求、文件名及上游 ID 不作为指标标签。
- 明确测量单位和范围。HTTP 到响应头的耗时、首字节时间、网络等待、阶段累计时长、任务墙钟时间和沙盒持有时间分别展示；并行阶段之和可能大于墙钟时间，不能直接相减计算等待时间。
- CPU/GPU 使用率、显存、CPU 时间和 OOM 结论需要相应实测来源。没有 cgroup OOM 计数等证据时保持未知，不能仅从退出码或错误文案推断。
- 公开活动通过现有 actor/activity ID 和阶段记录产生，正常、失败、取消和接管时均能收束。允许多个实际活动并行展示；推理阶段标签不意味着可以公开模型推理内容。
- 阶段指标使用独立短预算连接，遥测队列和输出有容量边界。保持当前非阻塞输出与丢弃计数；不能让日志接收端、采集器或指标锁等待阻塞业务线程。
- 指标写入失败仅记录观测降级，不能使已成功上传变成业务失败或触发重新生成。反过来，不得吞掉租约、预算、提交意图和正式成果事务的失败来伪装成功。
- `measurement_connection` 不获取业务控制锁，适用于既有非授权计量及受限回执写入；新代码不得借它或 administrative 上下文更新步骤、授权或成果。需要控制状态写入时仍走原事务与租约校验。

## 4. 修改调用账本和用量汇总

先沿实际请求路径确定一条记录代表什么：模型 HTTP 请求、Agent 工具、工作流步骤启动、供应商提交/查询/收取是不同类别。一次业务任务可能包含多类调用，不能将总行数作为生成次数或统一收费次数。

- `usage_tasks` 保存 task → project/run/parent。新需求建立独立 run，确认执行及显式续接按已有父子关系关联并校验同项目；不能按相似文字、最近任务或项目相同自动合并。
- `usage_calls` 以稳定 call_id 去重，关联 attempt、step 和 revision。复用 `start/complete/tool/model_call`；重复完成回执不能累加，晚到开始事件不能覆盖终态。
- 主模型实际请求前保存调用记录，记录失败时不继续发起请求。回执写入与业务授权分离，取消或租约失效后只能补充已有调用的消耗；没有先前调用记录的回执不得新建业务或发布成果。
- 回执写入失败、进程退出或断流应保留缺失证据。旧尝试已经结束而调用仍显示 running 时按现有汇总规则表达 unknown，不因此重发模型请求。
- 供应商生成以 `workflow_provider_calls` 为权威调用记录；`provider_submit/poll/collect` 表示阶段请求。不要把一次生成的多次轮询或下载计为多次新生成，也不要重复累计两个表中的同一费用。
- 工作流 retry 按同任务、同一步骤、同 revision 的额外实际启动统计；repair_count 单列。恢复调度、等待资源、控制操作及轮询不自动算重试；无关联证据时不猜测 SDK 内部重试次数。
- 新增 category 时同时检查计数初始化、聚合、明细、诊断和前端展示。新字段和历史迁移需保持无回执历史数据为未知，不能从授权配额反推实际消耗。

回执解析和 Token 规则：

| 情况 | 处理规则 |
| --- | --- |
| JSON/SSE、任意字节分片和 CRLF | 复用有界 Receipt 解析；旁路观察不改变转发内容，不持久化模型正文 |
| 成功、失败、取消或 incomplete 回执 | 有有效用量就保留，业务失败不代表消耗为零 |
| 缺失、非法、负数、布尔或非有限数值 | 按字段校验；缺失/非法保持 null，明确零值才记为零 |
| 缓存与推理 Token | 分别是输入与输出的子集，不再次加入总数 |
| 原始 total 缺失 | 只有 input/output 均可信且无冲突时提供单独的计算合计，并标注来源，不改写原回执 |
| 实际输出与零输出回执矛盾 | 保留原始 output_tokens=0，标记 conflict，不纳入可信计算合计，保留预算预留 |
| 重复、补充及冲突回执 | 重复不累计；补齐原未知字段；非空值矛盾保留冲突，不静默覆盖或相加 |
| 费用 | 仅展示有证据的数值与明确币种；不混加不同币种，不按当前价格补造历史费用 |

汇总范围为 task/run/project，均限定当前项目；总数覆盖范围内全部记录，不能被明细分页截断。维持一致读快照，检查翻页时新增回执和跨节点补记；公开完整度、reported/missing、冲突及派生统计。原始回执、计算值、授权配额、实测耗时分别表达。

输入估算是请求准入，不是实际 usage。`workflow.model_input_estimate` 记录策略摘要、参考分词、图片和余量；预算预留、回执偏差及结算事件通过 `reservation_id` 关联。估算事件发生在预留创建前，应通过同一调用上下文关联，不能声称它已持有尚不存在的 reservation_id。真实用量超出预留要如实入账并报告偏差，不能截断到预留值；回执缺失或矛盾时不得用估算补造实际消耗。策略开发及校准见[LLM 接入规范](add-llm-provider.md)。

观测还须区分模型预设/版本、实际 profile harness 及传输 strict 模式。记录 Schema 摘要、回退错误码和逐次调用回执；HTTP 200 不证明 strict 被执行，回退重发也不是同一条 HTTP 记录。

额度终态必须区分：`model_token_budget_exhausted`表示真实模型Token准入不足，`review_contract_exhausted`表示Reviewer在有界纠错/finalize次数内未提交合法结果，普通`budget_exhausted`保留给其他执行额度。strict约束未执行的首次传输回退单列事件，不计业务纠错；页面、诊断和报告不得把turn/finalize耗尽显示为Token耗尽。

## 5. 修改诊断、采集与告警

诊断只读取持久化状态、审计、调用、清理、成果及有限日志证据，不在查询中重试、取消、恢复或生成。新增建议必须有证据支持；缺失、截断和来源不可用用 `complete/gaps` 等字段明确表达。

- 用户诊断、用量、下载和请求编号查询先验证项目及任务归属。数据库无法完成授权验证时拒绝用户查询；受保护的运维诊断可以返回已有采集证据并标注缺口，不能把该回退用于普通用户接口。
- 输出使用安全字段投影；用户视图剔除运维节点信息、内部地址和堆栈，供应商用量只导出合法数值与费用字段。新增数据集合需同步用户投影，不能直接导出数据库整行。
- 诊断分页、查询时限和响应大小有界，取到部分日志不应宣称完整。数据库事务审计、采样/轮转遥测和公开活动具有不同保留语义，删除项目时按既有归属清理。
- 当前采集器从专用 journal 汇入独立 SQLite 索引，提供受保护的本地 HTTP 看板和 Prometheus 文本指标；不能据此声称已有跨机聚合、OTLP/Jaeger 或独立整机故障告警。
- 修改采集要验证 cursor 回补、重复入库、丢弃计数、容量/时间留存及物理空间回收。保留业务恢复所需记录；观测索引不能成为任务完成的依赖。
- 告警阈值覆盖持续窗口、去重、恢复与维护静默。长视频正常处理不应仅因时间较长就被判断失败；采集不可用也不能报告依赖健康。
- 通知渠道未配置时如实显示。测试使用本地接收端；接入或发送真实邮件、聊天通知需有明确授权，不因新增告警自动联系外部收件人。

## 6. 验证与完成标准

按不变量构造测试，不只断言日志字符串存在。至少对比业务事实、原始回执、汇总和用户视图，故障注入后确认没有额外业务调用或错误状态转换。

| 场景 | 应证明的结果 | 相关测试入口 |
| --- | --- | --- |
| 关联、审计与安全字段 | 跨线程/进程上下文正确；审计随事务回滚；异常和子进程不能泄露/伪造上下文 | [test_observability.py](../../tests/test_observability.py) |
| 观测故障 | 阻塞日志、锁竞争、数据库/存储不可用仍有有界行为和缺口；业务结果不被指标失败改变 | [test_observation_faults.py](../../tests/test_observation_faults.py)、[test_observability.py](../../tests/test_observability.py) |
| 回执和幂等 | 分片、重复、补充、冲突、失败、取消及断流均不误计；迟到回执不恢复业务 | [test_task_usage.py](../../tests/test_task_usage.py) |
| 关联、范围和分页 | 独立需求不混合；跨项目拒绝；详情分页不截断汇总；未知、零与计算值可区分 | [test_task_usage.py](../../tests/test_task_usage.py)、[task_usage.test.mjs](../../tests/task_usage.test.mjs) |
| 事件与公开活动 | 提交事件保持一次计量；重连/接管后状态正确；不暴露推理正文 | [test_usage_stream_integration.py](../../tests/test_usage_stream_integration.py)、[test_live_activity.py](../../tests/test_live_activity.py) |
| 采集、告警和权限 | 受保护查询、留存、低基数指标、持续窗口、去重、恢复及静默有效 | [test_observability.py](../../tests/test_observability.py) |

例如，修改计量与诊断时：

```bash
.venv/bin/python -m tests.run_distributed_suite tests.test_task_usage tests.test_observability tests.test_observation_faults
node --test tests/task_usage.test.mjs
```

关联实时事件时追加相应集成测试；发布前按[开发与测试](../development.md)完成全量检查。采集和查询策略变化时参考 [benchmark_observability.py](../../tests/benchmark_observability.py)，核对其数据和目标后使用独立环境记录规模、查询延迟、采集开销和磁盘上限；小样本通过不能证明高负载可用。

交付说明应列出事件/字段/统计口径变化、关联与去重规则、迁移和历史缺口、留存/告警配置、故障注入及前后端核对结果。涉及真实模型时另核对真实回执与账本/API/页面；没有回执不能声称 Token 已核实。同步[可观测性手册](../observability-operations.md)、[任务消耗设计](../task-usage-design.md)及适用的用户说明。仅修改开发文档时检查链接和当前代码即可，不触发付费模型、通知或部署。
