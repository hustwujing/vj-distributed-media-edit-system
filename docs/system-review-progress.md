# 系统分包审查进度

本记录用于两轮逐包审查。每包先核对不变量和竞争边界，发现问题即修复，只运行新增与受影响测试；两轮全部完成后才执行全量门禁、冻结部署和真机回归。

## 横向异常输入口径

每个相关审查包都必须验证异常输入不会把系统或 DAG 留在不可收敛状态。不能只断言 HTTP/Schema 拒绝，还要检查拒绝后的 Run、Task、Control Job、barrier、Decision、Draft、lease、Provider 句柄、staged result 和 cleanup 能继续推进或进入明确终态：

- 用户输入：空值/超限、重复幂等键、同键改内容、过期目标、跨项目素材、附件变化、决策并发和终态输入。
- LLM 输入：非法 JSON/Schema、合法但语义错误、错误枚举/引用、旧 snapshot/version、重复错误、越权动作和晚到结果；任何非法结果不得部分落 DAG。
- 发布与非优雅重启：事务中断、旧进程晚到写入、过期 lease、staged result、已有外部 ID、提交未知、清理未完成和多节点接管；不得依赖优雅停机成立。

## 审查包

1. Session、Run、项目归属与配额
2. Run 输入邮箱、排空屏障与 successor
3. Manager、PlanDraft、计划编译与授权
4. 业务 Task、DAG、revision、replacement 与输入绑定
5. 调度、租约、Attempt、资源并发与结果提交
6. Operation、Provider、副作用、取消与清理
7. 专业 Agent、Harness、模型协议与 Token 预算
8. 技术验收、Reviewer、Diagnosis、修复与最终交付
9. 用户决策、暂停、取消、重启与故障恢复
10. Checkpoint、对象存储、成果复用与项目删除
11. API、SSE、可观测性、诊断与页面状态
12. 部署隔离、配置、开发规范与真机 case 体系

## 第一轮

### 1. Session、Run、项目归属与配额（完成）

发现并修复：

- `/runs` 仍保留旧的项目级单活门禁，与“同一项目可有多个独立 DAG、同一 Session 串行”冲突；移除该门禁，由用户执行并发配额和 Session 单活分别约束。
- 页面已有 DAG 列表但没有创建独立 DAG 的入口；增加“新建任务”，并隔离新 DAG 与既有 Run 的未发送草稿。轮询不会退出新建模式。
- `WAITING_USER` 不再占执行并发后缺少开放 Run 总量保护；新增 `VIDEO_USE_USER_OPEN_RUN_LIMIT`，默认每用户 50 个非终态 Run。
- 内部 `graph.create` 对已有 ID 只检查授权；补充 project、Session、parent 和 scope confirmation 不可变校验。

保持的不变量：

- 同一 Session 最多一个非终态 Run；successor 留在原 Session。
- 同一项目可并行创建不同 Session 的 Run；所有项目合计仍受 owner 执行并发限制。
- 启动请求幂等键绑定 owner、project、授权、parent 和模型选择；跨项目访问继续返回 404。
- 执行配额与等待用户的开放 Run 上限分开统计，均在控制事务内判定。

验证：

- 隔离 Python：`tests.test_dynamic_api`、`tests.test_runs`、`tests.test_run_admission_races`、`tests.test_studio_ui`，28/28 通过。
- 真实临时 PostgreSQL：两节点并发创建的 owner 配额和 Session 单活竞争均只放行一个事务。
- 本地隔离浏览器：`browser_dynamic_workspace.cjs` 通过；覆盖新建模式、2 秒轮询、Run/新 DAG 草稿切换、桌面和移动布局。
- JavaScript 语法及 `git diff --check` 通过。

### 2. Run 输入邮箱、排空屏障与 successor（完成）

发现并修复：

- `input_question` 的 reject 没有消费对应输入；Decision 结束后原输入仍为 `ACCEPTED`，会在安全点反复生成同一问题。现在按冻结 cutoff 拒绝批次，或只废弃附件已失效的输入；旧图可继续，其他有效输入仍保留。
- 用户为失效附件提交替代输入后，旧输入虽被 supersede，`input_barrier_sequence` 仍指向旧 sequence。新增统一 `supersede_inputs`，在同一事务推进或清除 barrier、恢复 sealed 状态并维护 drain set。
- 异常的 `SUCCEEDED` 但未发布交付的 Run 会接收输入，而终态 Run 不再 reconcile。现在明确拒绝；只有带正式 `delivered_at` 的成功 Run 才创建 successor。

保持的不变量：

- input、claim 与最终发布仍由同一数据库控制事务决定先后；屏障前已开始工作进入 drain set，屏障后未开始业务 Task 不可领取。
- 同一幂等键只产生一个 sequence；规划期间新输入进入下一批，已冻结批次不会吞掉晚到输入。
- 附件在安全点重新核对项目事实；失效输入只能由用户替换或拒绝，不能把变化后的素材静默当成原授权内容。
- delivered Run 的新输入创建同 Session、`scope_confirmed=false` 的 successor，原 Run 和交付保持不可变。

验证：

- 核心邮箱与真实 PostgreSQL 竞争：18/18 通过。
- 影响面定向回归：`test_run_inputs`、`test_run_input_races`、`test_run_feedback`、`test_dynamic_graph`、`test_run_recovery`、`test_dynamic_api`，97/97 通过。
- 新增覆盖 Manager 澄清拒绝、失效附件拒绝、附件替换后 barrier 前移、无交付成功终态拒绝输入。

### 3. Manager、PlanDraft、计划编译与授权（完成）

发现并修复：

- `continue_run / request_user_input / request_authorization / new_run_required` 已提交合法 Manager 结果时没有绑定 Draft revision/digest；结果 staged 后 Draft 仍显示 `OPEN`，直到后续 reconcile。现在所有 plan-draft-v1 终结命令都在 stage 事务内校验并关闭当前 Draft。
- Draft 的修改和提交有 lease fence，但相同错误计数 `record_error` 没有；过期 Manager Attempt 可污染接管 Attempt 的纠错预算。现在错误记账也验证 run/step/attempt/generation 和 OPEN Draft。
- `continue_run` 在无输入批次的初始/滚动规划中仍向模型开放，但宿主应用阶段只允许它消费 input batch。现在工具只在冻结 `input_batch.through_sequence>0` 时出现，`ManagerResult` 领域契约也拒绝无批次 CONTINUE。

保持的不变量：

- 模型只提交状态允许的白名单命令；snapshot、graph/authorization version、input cutoff、Proposal ID、依赖与技术验收继续由宿主绑定或校验。
- strict 不受网关支持时只降级传输，Schema、语义、授权、引用和 lease 校验不降级。
- typed batch 在完整编译与图校验前不持久化；局部纠错受 Draft revision/digest、次数和重复错误上限约束。
- scope expansion 只产生用户授权决策；Manager 不能用 Proposal、Draft 或当前默认配置授予新权限。

验证：

- 协议与 Gateway 定向回归 88/88 通过。
- 联合计划回归：`test_model_command_protocol`、`test_dynamic_gateway`、`test_dynamic_graph`、`test_run_inputs`、`test_media_plan_compiler`，170/170 通过。
- 新增覆盖控制结果 Draft 原子关闭、过期 lease 不能写错误预算、无 input batch 不可 CONTINUE。

### 4. 业务 Task、DAG、revision、replacement 与输入绑定（完成）

发现并修复：

- 用户反馈 replacement 仍由模型重填 objective、acceptance、semantics 和 delivery_stage，可能在“换实现”时弱化要求。现在所有 replacement 都由宿主继承这些 requirement 字段，图校验层再次独立复核。
- replacement 后新 Task 仍引用旧逻辑 Task ID 时会落入内部 `KeyError`。命令编译器现在把普通 step 引用和依赖映射到 replacement；绕过编译器的原始 Proposal 明确拒绝 retired 引用，不会部分落图。
- 依赖表主键不能表达同一边 `add → remove → re-add`。重新添加会被 `INSERT OR IGNORE` 吞掉，造成校验图与持久图分叉。依赖改为按 created graph version 分代，退休只更新 active 代，重新添加创建新代并保留历史。

保持的不变量：

- revision 只修改对应自动/用户范围允许字段；Task 原契约、候选、检查点和历史 revision 不被覆盖。
- replacement 显式保留旧失败节点和谱系，重写下游引用并使受影响后代产生新 revision；无关分支继续复用。
- Task 输入只接受项目内冻结素材、已验收 step+role 或绑定 revision/hash 的 repair candidate；异常引用在 Proposal 应用前拒绝。
- DAG 每个版本保持无环、依赖端点存在且输入引用有对应边；任何校验失败均在控制事务回滚，不产生部分 Task/edge。

验证：

- replacement、模型命令和动态图定向回归 122/122 通过。
- 联合输入/成果回归：`test_dynamic_graph`、`test_run_replacements`、`test_model_command_protocol`、`test_repair_inputs`、`test_workflow_input_paths`、`test_workflow_reuse`、`test_operation_output_filenames`，163/163 通过。
- 新增覆盖用户 replacement 不可弱化、旧 Task 引用映射/拒绝、依赖 remove/re-add 三代历史与最终 execution view。

### 5. 调度、租约、Attempt、资源并发与结果提交（完成）

发现并修复：

- Dynamic Task 实际资源分配失败时，`defer_resource` 仍写旧 `agent_steps`，动态任务会被当成基础设施中断并在数次后误判 `result_unavailable`。现在写当前 `run_tasks/control_jobs`，保留退避且不消耗 interruption budget。
- Control Job 在旧 runtime 首次 claim 后执行器消失，会保留 `_environment`；新 worker 拒绝 claim，而 mismatch 恢复又跳过控制任务。现在失权恢复清除 Control Job 的旧 runtime 绑定，在同一冻结请求/Profile 下允许接管。
- `stage_result` 接受未知 outcome、非 object data/manifest；畸形 manifest 可让 Orchestrator 抛未捕获属性错误。现在 staging 边界限制合法 outcome 和结构，历史损坏记录也收敛为 `invalid_result_manifest`。

保持的不变量：

- claim 在数据库控制事务复核 Run/Task 状态、依赖、barrier、资源、用户/节点并发、项目清理和当前 lease；同一 slot 仅一个有效 generation。
- 旧/过期 lease 不能续租、写结果、释放接管者或增加 Draft 错误预算；staged result 绑定 attempt、generation、Task revision。
- 资源等待不算业务失败或基础设施中断；实际 cleanup 未完成继续占用容量，不能提前复用身份。
- 非优雅中断后的 Control Job 只重跑冻结请求，业务 Task 的 runtime 变化仍要求 repair/revision。

验证：

- 调度、恢复、lease 与动态图 97/97 通过。
- 跨进程/真实 PostgreSQL 联合回归：`test_execution_leases`、`test_scheduler_v2`、`test_execution_reaper`、`test_distributed_races`、`test_distributed_runtime`、`test_distributed_database`、`test_dynamic_execution`、`test_run_input_races`、`test_run_recovery`，61/61 通过。
- 新增覆盖 dynamic resource wait、Control Job runtime 接管、异常 result envelope 零落库。

### 6. Operation、Provider、副作用、取消与清理（完成）

发现并修复：

- Normal Provider receipt 可写任意 state、覆盖已保存 external ID、在无 intent 时静默更新零行。现在使用状态白名单、ID 格式与不可变校验，waiting/running 必须有 ID，terminal 状态单调且所有检查与写入同事务完成。
- API 取最新单 worker 目录，Planner 对多 worker 目录无序覆盖；非优雅/滚动发布时同一 operation contract 可能随机冻结。现在 API 与 Planner 统一使用健康 worker 目录交集，只有完整 contract digest 在所有健康 worker 一致的 operation 才可授权。

保持的不变量：

- 外部副作用前先持久化 intent 并消费授权预算；同一 Run/Task/revision 的 request key 不可重复提交。
- budget、concurrency、route、provider identity、cleanup 和 reuse policy 独立冻结；未知 route/cleanup fail closed。
- submit unknown 不重提；poll/collect/cancel 只使用已保存 external ID 与冻结 route，late receipt 只能补调用事实和安排 cleanup。
- cleanup 使用 generation/owner/lease fence，有界重试后明确 `unsupported/unconfirmed`，不把本地取消冒充上游已停止。

验证：

- Provider 状态、能力目录、API 与 DAG 回归 85/85 通过。
- Operation 生命周期/Provider/取消清理联合回归：9 个相关模块，85/85 通过。
- 新增覆盖非法 receipt state、冲突 external ID、无 intent/无 ID、终态不倒退、异构 worker contract 不授权。

### 7. 专业 Agent、Harness、模型协议与 Token 预算（完成）

发现并修复：

- 模型调用次数在 Token reservation 前消费；本地预估判定 Token 不足且没有发 HTTP generation 时，仍会减少 Manager/Reviewer 调用额度。现在 Token 检查、模型调用次数/日额度消费和 reservation 插入在同一控制事务完成，任一不足均零消费。

保持的不变量：

- Specialist Profile、harness 指纹、角色模型配置、协议、推理强度、输入计数与视觉策略随 Run/Attempt 冻结；恢复不读取新默认值冒充历史配置。
- strict 只在明确不支持或已证明未执行约束时回退；回退后本地 Schema、语义、授权和 lease 校验完整执行，普通网络/鉴权错误不触发回退。
- Claude/DeepSeek 本地预估只用于登记的模型/协议，缺少或损坏 tokenizer 拒绝生成；实际回执用于结算，未知用量保留预算。
- 实际有输出但 `output_tokens=0` 时只结算可信 input，保留当前阶段 output envelope；调用账本标记 conflict，不填成免费输出。
- 非法 JSON/工具/枚举/引用、多个动作、截断与隐藏推理均有界拒绝；模型回复不能直接提交 DAG、Task 成功或交付。

验证：

- Token 与 Gateway 定向回归 75/75 通过。
- 模型选择、估算、输入计数、Gateway、usage、Harness、命令协议和 outcome 联合回归 162/162 通过。
- 新增断言 Token 预算拒绝时无 HTTP、无 reservation、无模型调用次数消费；成功时三者一致。

### 8. 技术验收、Reviewer、Diagnosis、修复与最终交付（完成）

发现并修复：

- `accept_review` 先写 review passed/Control Job succeeded，再检查 subject 技术验收；异常路径会半提交矛盾状态。现在 `technical_valid` 和程序 `passed` 在任何状态写入前校验。
- 最终发布只信调用方传入 `decision_json`，没有重读当前权威 final review。现在发布事务按 review ID 重新查询并要求当前 Run/graph 的 `purpose=final / status=passed`，调用方 payload 不再有发布权。
- Final Review 的 `inconclusive` 被当成明确 quality failure，Planner 又没有 evidence collection 选项，容易无谓重生成。现在最终证据不足进入 `review_incomplete/evidence_planning`，以 accepted result 或冻结 repair candidate 为基础补证据；明确 reject 才修改内容。

保持的不变量：

- 程序技术验收先于 Agent/用户内容判断，任何 Reviewer/用户批准都不能豁免技术失败、哈希变化或未通过程序检查。
- Review 绑定 snapshot、graph/revision、candidate/quality、attempt inspection ID；错误/旧 evidence ref 和未覆盖 criterion 均拒绝。
- 同一候选、标准和证据的旧结论不重复抽样；inconclusive 只补证据，replacement/revision 保留候选和历史。
- Final Review 必须检查并选择每个 delivery-stage DAG leaf，满足明确格式/文件名/role，并声明多交付主成果。
- 发布只读取已验收 immutable checkpoint/object，在同一事务复核输入 barrier、待决 Decision、存储额度与权威 Review。

验证：

- 动态 Review/交付定向回归 76/76、证据策略回归 139/139 通过。
- 质量、Reviewer、交付、replacement、repair candidate 和恢复联合回归 189/189 通过。
- 新增覆盖程序失败 Review 零半提交、伪造 Review payload 不发布、Final inconclusive 补证据且不重做交付节点。

### 9. 用户决策、暂停、取消、重启与故障恢复（完成）

发现并修复：

- fatal Task/Control 失败和 reconcile 熔断直接把 Run 设为 `FAILED`，终态不再 reconcile，可能遗留并行 lease、Provider 句柄和 cleanup。现在失败统一先进入 `CANCELLING`、请求本地/外部 cleanup、取消其余工作，待 lease/本地 cleanup 收敛后落 `FAILED`。
- 对模型/规划预算耗尽、交付清单不完整、诊断策略未授权等状态执行 resume 不会改变前置事实，只会 ACTIVE/WAITING 循环。现在明确拒绝为 `replan_required`，原状态保持稳定；用户可补充输入、处理条件或新建 Run。

保持的不变量：

- command/decision 的读取、状态修改和回执在同一控制事务；任意指令间 SIGKILL 要么整笔提交，要么整笔回滚，幂等键可安全重放。
- 暂停阻止新 claim，已开始工作正常提交；取消优先于 pause/resume/answer，旧 generation 晚到结果只记 late，不恢复业务。
- 用户取消最终为 `CANCELLED`；deadline/fatal/reconcile 熔断最终为 `FAILED`，两者都先完成本地撤销，外部 cleanup 独立保留真实 confirmed/unknown 状态。
- 旧 worker 失权后不能写业务状态；reaper 先按本地单调 permit 停进程，再依赖数据库记录收敛，数据库暂不可用也不会延长执行权。

验证：

- 决策、恢复、取消、reaper 与调度回归 111/111 通过。
- 真实多进程/临时 PostgreSQL 故障回归 53/53 通过，包含节点强杀、并行分支接管、跨节点汇合、用户门禁和 cleanup。
- 新增覆盖 fatal 等待 sibling cleanup 后 FAILED、不可恢复 WAITING 的 resume 零状态扰动。

### 10. Checkpoint、对象存储、成果复用与项目删除（完成）

发现并修复：

- 项目删除只在共享对象后端调用 `release_prefix`；本地后端的 published logical refs 永久残留，GC 无法回收。现在所有后端都在项目删除事务内释放引用，对象进入 staged/延迟 GC。
- artifact upload 完成后项目可能已删除，晚到 `publish()` 仍可为已删除项目重建孤儿 `artifact_files`。现在 publish 事务重新校验 project 存在，删除后的晚到提交明确失败。

复核结论：

- `delete_project_data` 虽调用多次 `store.execute`，但路由外层已有可重入连接和 `BEGIN IMMEDIATE`，全部元数据删除与 logical ref 释放属于同一事务；SIGKILL 不会留下逐条提交的半删除项目。

保持的不变量：

- upload 只产生 staged immutable object，logical publish 另行复核 project、lease/调用方状态和 expected revision；上传完成不等于成果发布。
- 跨节点恢复只使用已提交 checkpoint/object 元数据和校验后的 bytes，缓存损坏重新下载，部分文件不替换正式缓存。
- reuse 绑定同项目、当前 Task revision、已验收文件、artifact ID、size/SHA 和来源 receipt；不能按文件名或旧 revision 猜测。
- 项目有活动 lease、本地 cleanup 或正在确认的外部 cleanup 时拒绝删除；元数据提交后才异步删目录。

验证：

- 删除/晚到发布/API 并发回归 26/26 通过。
- 对象、跨节点 runtime、reuse、step/session checkpoint、上传/发布竞争和项目删除联合回归 65/65 通过。
- 新增覆盖本地引用释放、对象转 staged、删除后 late publish 拒绝。

### 11. API、SSE、可观测性、诊断与页面状态（完成）

发现并修复：

- 页面在 Run ACTIVE 但无真实 active item 时伪造“流程协调器正在衔接”并呼吸。现在只有真实 ready/running/waiting/reviewing 工作可见，只有 running 呼吸。
- 数据库兜底文本只要历史任一 message 为 streaming 就持续返回 streaming；执行器被强杀时光标可能永久闪烁。现在仅当前最新 generation 且 attempt 仍有有效 lease 才为 streaming，失权/终态为 idle。
- 集群指标和诊断建议只识别 `unknown`，漏掉实际状态 `submission_unknown`。现在 provider_unknown gauge、任务建议和 external cleanup 诊断完整覆盖。
- 动态诊断步骤缺少 `recovery_count / infrastructure_interruptions / repair_count / user_revision_count`，已补齐以区分业务重试、发布中断和修复。

保持的不变量：

- Redis 只负责有界、TTL 实时 fan-out；数据库消息、事件和状态是兜底与权威。Redis 失败不阻断任务，SSE 退回有界 DB 读取。
- 文本 delta/snapshot 按 generation+sequence 合并，gap 触发 snapshot；旧 attempt、旧快照和晚到事件不恢复终态正文或活动。
- 用户向上阅读时不自动拉底；只有接近底部或主动返回最新才跟随。终态关闭 SSE，正文和折叠步骤不持续重建。
- API SSE/读写/存储/媒体/诊断使用独立有界容量；压力下 health 和 critical control 保持可用。

验证：

- SSE、文本、活动、UI、可观测性定向回归 51/51 通过。
- API 压力/容量联合回归 58/58，前端状态合并 Node 22/22 通过。
- 本地隔离真实浏览器通过桌面/移动、DAG 拖拽、草稿切换、历史滚动、无假呼吸和终态稳定断言。

### 12. 部署隔离、配置、开发规范与真机 case 体系（完成）

发现并修复：

- systemd unit 切到新源码后，`release.json` 仍要等健康检查结束才写；发布进程被杀会造成运行源码与冻结记录不一致。现在先原子写 `state=deploying`，再重建 schema/安装 unit，健康后原子写 `state=ready`；partial release 的普通 restart 明确拒绝。
- unit 直接读取工作区 `.agent.env` 等文件；新字段提前落盘会让旧 release 在 watchdog 重启时加载不兼容配置。现在四份私有 env 按内容 SHA 冻结到只读配置快照，unit、release 记录和 campaign 同时绑定源码/配置摘要。
- 当前规范仍把历史 119 条当现行清单，并残留 structured-profile-v2 表述。现行入口已统一为 126 条、structured-profile-v4、沙盒 Claude 4.6/high、专家 Claude Sonnet 5.5。

## 模型传输性能复核

- 历史`cb27fbdd8723`完整119轮的中位case耗时为165秒；后续全角色强制`stream=true`后，C03相同33次模型调用从491秒增长到4008秒。受控请求A/B证明TextPublisher本身只有毫秒级开销，但stream请求由网关回显为`kiro-claude-sonnet-5-5`，buffered请求回显为`claude-sonnet-5-5`。
- 响应传输现按角色冻结：Manager、Reviewer、Diagnostician和Delivery Reviewer使用buffered，Editing Implementer保留stream。缺少commentary改为宿主摘要；Redis delta异步合并，完成快照同步封口，TTL定期刷新。
- targeted版本`c5e0f2ff5cf8`验证C03 636秒业务成功、C06 443秒业务成功、C07 346秒非程序失败且程序处理正确；0程序错误。C07的重复非法规划现准确归类为`planning_contract_exhausted`。三条均完成浏览器、数据库、BOSS、Redis和节点缓存审查与清理。
- SIGINT恢复以S01真机验证：父驱动不依赖已退出的Playwright进程，使用本轮落盘session取消Run，等待终态、零活动工作和本地cleanup后读取完整内部审计；安全时删除项目。最终状态为`user_stopped`，BOSS、Redis和节点缓存零残留。

真机 case 调整：

- 新增 D48“安全点前附件失效并拒绝补充输入”，通过真实浏览器输入、受控项目内附件删除和用户 reject，验证 input supersede、barrier 收敛、原 Attempt 正常完成和最终交付。
- manifest 从 125 扩为 126；browser/probe/fault 驱动要求 `release.state=ready` 且源码/配置摘要全程不变。

验证：

- 部署 identity/reset/proxy/reaper 22/22 通过。
- manifest 唯一 ID/计数、D48 hook、Python/Node 驱动语法、case list 和 `git diff --check` 通过。
- 本包只完成 case 设计和驱动静态验证；D48 及完整 126 条真实执行留到两轮 review、全量门禁和正式部署之后。

## 第二轮

进行中。

### 1. Session、Run、项目归属与配额（完成）

- 交叉入口发现开放 Run 上限误放在通用 `admit_tasks`，会阻断 manual/image/旧 Agent；改为仅 `graph.create` 显式启用。
- 多 Run、Session 单活、owner 配额、usage/project 交叉回归 53/53 通过。

### 2. Run 输入邮箱、排空屏障与 successor（完成）

- 中断残留反查发现：目标 input 已被其他恢复路径处理时，空 `reject_batch` 不重算 barrier。现在即使无待更新行也推进/清除 barrier 和 drain set。
- 输入、PostgreSQL 竞争、反馈和恢复回归 35/35 通过。

### 3. Manager、PlanDraft、计划编译与授权（完成）

- 复核控制结果 Draft 终态、旧 lease 错误记账、typed batch 原子提交和 CONTINUE 状态命令，未发现新增缺陷。
- 模型命令、Gateway 和动态图联合回归 156/156 通过。

### 4. 业务 Task、DAG、revision、replacement 与输入绑定（完成）

- 复核 replacement 继承、依赖代际、repair input、输入路径与成果复用，未发现新增缺陷。
- 相关 5 个模块 50/50 通过；运行期间源码摘要保持不变。

### 5. 调度、租约、Attempt、资源并发与结果提交（完成）

- 复核 staged result 强杀窗口、旧 generation 晚到、过期 lease 接管、资源等待和异常结果边界，未发现新增缺陷。
- 真实 PostgreSQL、多进程、跨节点 runtime、reaper、动态执行和输入竞争 61/61 通过；运行期间源码摘要保持不变。

### 6. Operation、Provider、副作用、取消与清理（完成）

- 交叉检查发现普通 Provider receipt 的终态单调性依赖 lease 上下文触发事务；无 lease 的 SQLite 调用可能在并发读写中竞争。现在 receipt 始终显式进入控制事务。
- 新增双节点并发成功/失败回执回归，首个终态保持单调，后续 running 不可倒退。
- Operation、Provider、外部清理、动态执行和恢复 82/82 通过；运行期间源码摘要保持不变。

### 7. 专业 Agent、Harness、模型协议与 Token 预算（完成）

- 复核 strict 探测回退、Claude 零 output 回执、输入估算、Token reservation、模型调用计数和中断结算，未发现新增缺陷。
- strict 参数被拒且无可信 usage 时保留独立未知 reservation；回退请求使用独立回执结算，不把网关拒绝臆造为零消耗。
- 有效 Python 回归 173/173、Profile 前端回归 2/2 通过；一次不存在模块名导致的测试选择器错误已剔除并由实际模块补测。

### 8. 技术验收、Reviewer、Diagnosis、修复与最终交付（完成）

- 复核程序验收优先级、Reviewer snapshot/evidence、inconclusive 补证据、Diagnosis 权限和最终发布事务，未发现新增缺陷。
- 发布重新读取当前通过的 final review，并复核当前 graph、active Task、delivery leaf、格式/文件名/role、checkpoint、对象 SHA、存储额度及输入屏障；模型 payload 本身无发布权。
- Reviewer、Diagnosis、证据、repair、用户反馈和交付联合回归 161/161 通过；运行期间源码摘要保持不变。

### 9. 用户决策、暂停、取消、重启与故障恢复（完成）

- 复核 fatal/reconcile 熔断、取消与暂停竞争、旧 worker 晚写、本地 permit、cleanup 收敛和数据库不可用时的 reaper 行为，未发现新增缺陷。
- fatal 路径继续先进入 `CANCELLING` 并请求 cleanup；本地 lease/cleanup 收敛后才落终态，外部 cleanup 保持独立真实状态。
- 恢复、取消、reaper、PostgreSQL 竞争和跨节点 runtime 59/59 通过；运行期间源码摘要保持不变。

### 10. Checkpoint、对象存储、成果复用与项目删除（完成）

- 复核 upload/publish/delete 竞争、checkpoint 跨节点恢复、reuse 绑定和对象 GC，未发现新增缺陷。
- upload 仍只创建 staged 对象；publish 在事务内复核项目与 revision；项目删除与 logical ref 释放原子提交，删除后的晚到 publish 被拒绝。
- 有效回归 60/60 通过；两个不存在模块名造成的选择器错误已剔除并由真实 checkpoint 模块补测。

### 11. API、SSE、可观测性、诊断与页面状态（完成）

- 复核 Redis fan-out/数据库兜底、generation/sequence 合并、失权 Attempt 光标、真实活动呼吸、诊断计数和 provider unknown 指标，未发现新增缺陷。
- Python API/SSE/诊断/UI 62/62、Node 文本流状态机 16/16 通过。
- 第一轮浏览器证据已覆盖桌面/移动、DAG 拖动、并行呼吸错峰、历史滚动锁定和终态停止刷新；本轮唯一源码修改位于 Provider 事务，与页面无交集，未重复启动浏览器服务。

### 12. 部署隔离、配置、开发规范与真机 case 体系（完成）

- 复核源码/配置双摘要、`deploying → ready` 发布日志、半发布 restart 拒绝、专用数据库 reset 和 campaign source lock，未发现新增缺陷。
- 部署 identity/reset 15/15 通过；126 条 manifest 唯一且 D48 已登记，Python/Node 驱动语法、case list 和 `git diff --check` 通过。

## 两轮结论

- 第一轮 12/12、第二轮 12/12 均已完成。
- 第二轮新增修复 3 项：开放 Run 上限不再误伤通用任务准入；空 reject batch 也会修复陈旧 barrier；普通 Provider receipt 无 lease 时也显式进入控制事务并保持终态单调。
- 下一阶段是最终全量门禁、冻结部署、重置专用数据库，然后执行当前 126 条真机 case。历史测试结果不能替代该阶段。

## 最终门禁跟进

- 首次完整 Python 门禁 1034 项中出现 3 个同根错误、6 个显式 GPU/OCR skip：Provider ledger 强化后，旧 workflow 对没有持久化 intent 的超时仍写 `submission_unknown` receipt，边界拒绝异常打断了安全暂停。
- 修复限定在旧 workflow 调用方：仅已有 Provider intent 时更新 receipt；没有 receipt contract 的实现只记录 workflow diagnosis 并暂停，不伪造 Provider ledger，也不重试未知外部副作用。
- 受影响 lifecycle、legacy workflow、Provider、Operation 和 cleanup 回归 68/68 通过；完整门禁需按新源码摘要重跑。
