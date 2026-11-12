# 动态工作流后续 TODO

当前阶段：已获授权实施全部优化、调整真机 case、隔离部署并完成逐例验收。基线已提交至本地 `feature/unified-run`（`7e667d8`）；GitHub 推送等待可用代理地址。main 增量适配已完成，不作为未完成项。实施进度见[统一 Run 实施记录](unified-run-progress.md)。

## 条目状态口径

2026-11-09 最新确认的增量优先于下文历史“所有输入立即建立屏障”的表述：普通聊天由现有Manager先判断，使用独立MESSAGE Control Job；只读回答/澄清不改任务、不增加业务输入版本、不撤销待确认方案。只有明确修改才激活既有安全点屏障；新任务创建待确认的独立Run。仍保留原授权、预算、租约、结果提交和恢复机制，不新增Agent角色或服务。当前开发与验证入口见`workflow_messages.py`、`tests/test_workflow_messages.py`；完成状态以部署后的专项证据为准。

下一阶段（本次不实施）：代码编辑师支持按用户配置选择Claude、Codex或DeepSeek Harness，并与当前structured-profile-v4作对比。优先复用已有`agent_profiles`、`model_selection`、Harness Adapter及Runtime Profile配置，只补尚未接通的调度链路，不建立重复配置体系。不能硬编码Claude，也不能仅按模型显示名猜Harness；每个Task冻结Harness/模型/镜像/版本，统一输入输出、租约、权限、预算、取消、恢复与独立验收，按各适配器真实能力展示流式输出。先完成当前修复、部署、测试、提交，再开展切换与对比。

当前新增对话修复验收项：方案确认前零业务执行、交付范围提出/确认/回填、只读追问与父轮上下文、拒绝和澄清上限、终态单次回放、成果可达及真实执行位置。对应服务端入口为 `workflow_conversation.py`，真机专项 LC01–LC06 及关联回归见[审查矩阵](real-case-campaign/dynamic-case-review.md)。这些条目的专项通过不替代冻结 126 条完整 campaign，最终状态以独立环境当前版本证据为准。

本文条目的复选框表示**最终验收**，不表示源码状态：`[x]` 只在完成冻结发布门禁且对应真机证据齐全后勾选。源码已实现但尚未完成当前 126 条真机验收的条目保留未勾选，并以 `（已实现，待真机验收）` 标注，后附实现入口与开发回归入口。

统一 Run、Provider 拆分、用户反馈修订和运行中输入屏障四节的协议已全部落到源码，并有定向回归覆盖；它们的开放项是部署后的逐例真机验收，不是实现缺口。修改这些机制前仍按本文核对约束，特别是文末五个边界。阅读实现时以代码为准，不要据复选框推断功能缺失。

## main 适配追齐

已审查范围：`d818cd8..456d3bc`，共17个提交。逐提交事实与适用性见[main增量审查](real-case-campaign/dynamic-main-followup.md)。

- [x] 同步能力文件先父目录、后子文件的挂载顺序；验证实际JSON可读且只读。已部署于`7ef71c64bc34`。
- [x] 补齐file的magic数据依赖并纳入运行时指纹；验证真实沙盒PNG识别。已部署于`7ef71c64bc34`。
- [x] 适配按阶段、任务关联组织的提示与证据，保留完整证据查询入口与截断标记。
- [x] 适配精简能力目录及按operation查询完整契约，保持冻结版本、归属与权限检查。
- [x] 完善媒体输入/转写绑定、自由结构参数和helper/operation调用区别的说明及规划校验。
- [x] 追齐分阶段验收、用户硬要求/技术必要条件/审美建议的区分，以及Alpha通道与透明像素语义。
- [x] 提供真实已用/剩余预算与相关诊断，补充提示规模和证据覆盖观测；不记录私有推理或敏感全文。
- [x] 适配进度时间戳、明确诊断下载、停止确认与取消收尾状态；防止迟到进度覆盖已确认的控制结果。
- [x] 修正QA透明主体判据，覆盖紧裁剪、全不透明和全透明空图；全部fixed_image按登记执行器统计额度。
- [x] 适配失败/暂停页面审查方法并更新动态页面控件；浏览器验证纳入下一轮部署测试。
- [x] 汇总Harness/LLM规范中真实隔离、恢复、流式、结构诊断与用量要求；未引入旧Jev/Plan状态机或main私有GPU配置。

## 25条失败的分析与改进讨论

分析见[失败原因](real-case-campaign/dynamic-non-program-failures.md)。重点包括：计划/验收契约10条、单Task审查轮数7条、内容判断或证据不足6条、生成脚本失败2条；还需补齐诊断后重新规划及步骤拒绝后的受控恢复流程。受控自动修复的边界、Manager交接及revision/plan替代方案见[修复协议讨论稿](dynamic-repair-proposal.md)。不考虑旧数据库或旧API兼容，再次部署允许重建本分支专用数据库。

所有修复必须是通用能力，不得按case ID、测试标题、固定测试提示词、特定素材名或固定输出名分支。真机case只用于问题证据与最终回归；只改测试驱动或预期不算完成。

职责划分已记录在[修复协议讨论稿](dynamic-repair-proposal.md#九系统改动职责划分)：主要修改控制面、类型化契约和确定性测量工具；少量修改Agent提示/Schema及动态页面；当前不新增Agent角色。

产品口径已确认：主体范围模式、`character_consistent`一致性默认、蒙版A/B/C/D修复等级、动作时序默认值。复杂媒体命令根因已通过24段真实输入复现：单进程24路解码线程耗尽TasksMax；通用修复为兼容片段concat demuxer、不兼容片段先规范化检查点再顺序拼接，并显式估算/限制资源。证据见`.cluster/c22-rootcause-report.json`。因此25条失败的方案层面已齐全，尚待实现与验证。

## Operation Provider 元数据拆分（已实现，待真机验收）

实现入口：`operations.py` 的 `Operation` 已移除 `provider` 字段，改为 `external_side_effects / budget_group /
concurrency_group / route_adapter / provider_identity / cleanup_category`，由 `provider_metadata.validate` 在
构造时校验；`scheduling.py` 读 `concurrency_group`，`provider_calls.py` 与 `workflow_graph.py` 读 `budget_group`，
`workflow_runtime.py` 读 `route_adapter`，`external_cleanup.py` 读 `cleanup_category` 并复核保存的路由，
`workflow_reuse.py` 按 `external_side_effects` 判定严格复用。开发回归：`tests.test_provider_metadata`
覆盖本地契约无外部额度、字段互不耦合、Wan/Ark/jobs 身份与清理类别区分、未知路由拒绝而非回退图片端点，
以及配置变化后仍使用调用时持久化的路由。下列条目保留为真机验收项。

- [ ] 拆分当前 `Operation.provider` 的多重职责。该字符串同时承担外部副作用标记、前端额度入口、授权计数、调度并发分组、运行时路由类别、外部清理类别和成果复用禁用条件，无法准确表达具体供应商、协议、模型及取消能力。
- [ ] 为 operation/implementation 定义类型化元数据，至少区分 `external_side_effects`、`budget_group`、`concurrency_group`、`route_adapter` 和 `provider_identity`；具体协议、模型及原始调用路由继续随调用意图持久化，凭据和私有 URL 不进入能力目录或 Manager 快照。
- [ ] 直接采用拆分后的最终数据模型，不保留旧 `provider` 字段兼容读取，不做旧数据迁移或双写；允许重建本分支专用数据库。新模型内已冻结的 implementation、授权额度和调用记录不能被后续配置改写；提交未知、原句柄查询、下载、取消和外部清理仍使用该次调用持久化的路由，不得根据新配置重提请求。
- [ ] 更新能力目录、授权页面、Manager 精简索引、调度资源声明、`provider_calls`、`task_lifecycle`、严格成果复用及诊断口径，移除依赖任意字符串分支的隐式行为；新增 provider 时必须注册完整路由和清理适配器，未知类别应拒绝而不是回退到现有图片编辑端点。
- [ ] 补充契约、授权、并发、恢复及扩展模块测试，覆盖本地 operation、Wan/Ark 图生视频、同步图片生成、转写、替代 implementation、提交未知，以及新模型调用记录在重启或配置变化后的恢复；所有读写统一使用拆分后的类型化字段，不提供旧字段回退。

## 移除 Plan / Execute 历史分层（已实现，待真机验收）

实现入口：`workflow_graph.py` 建立单一 `Run` 聚合与 `runs / run_tasks / control_jobs` 表，权威状态收敛为
`ACTIVE / WAITING_USER / CANCELLING / SUCCEEDED / FAILED / CANCELLED`，`activity()` 从控制任务和业务任务派生
`PLANNING / EXECUTING / REVIEWING / IDLE`，动态路径不再写 `agent_turns`（仅在历史列表中只读合并旧记录），
`internal`、`__manager_*`、`__review_*` 约定已不存在。PLAN/REVIEW/DIAGNOSE 走 `control_jobs.control_kind`；
业务 Task 保存真实 `phase`，生命周期与恢复动作由 `operation_lifecycle.py` 的版本化 protocol 决定；
沙盒策略改为 `execution_contracts.WorkspacePolicy` 的四种类型化值；Profile 改为 `specialist_profiles.py` 的
`AgentRole` + `ActionCapability`；API 收敛为 `workflow_api.py` 的 `/runs`；reconcile 判定见
`workflow_orchestrator.Orchestrator.reconcile`。开发回归：`tests.test_runs`、`tests.test_dynamic_execution`、
`tests.test_dynamic_api`、`tests.test_lifecycle_protocols`、`tests.test_scheduler_v2`。下列条目保留为真机验收项。

以下按不考虑旧数据、旧 API 和旧任务恢复兼容的目标模型设计；本次已获准隔离部署及重建本分支专用数据库；不得修改 main 数据和服务。

- [ ] 以单一 `Run` 聚合替代动态路径中的 `agent_turn + workflow_run` 一对一结构，统一使用 `run_id / task_id / attempt_id / generation`；事件、用量、诊断、租约、命令、交付和下载不再同时传播 `turn_id` 与 `workflow_id`。
- [ ] 将 Session 仅用于用户会话和消息归属；一次确认执行范围创建一个 Run，同一 Session 可串行拥有多个 Run，并通过 `parent_run_id` 和显式资产复用记录谱系，不再创建 `plan turn` 或 `execute turn`。
- [ ] 将 Run 权威状态收敛为 `ACTIVE / WAITING_USER / CANCELLING / SUCCEEDED / FAILED / CANCELLED`；`PLANNING / EXECUTING / REVIEWING / IDLE` 由当前控制任务、业务任务、租约和待决事项派生，仅用于活动展示，不作为互相覆盖的第二套状态。
- [ ] 新增独立 `control_jobs`，承载 `PLAN / REVIEW / DIAGNOSE` 的冻结快照、Profile、graph/authorization version、lease、staged result 和应用结果；移除 `workflow_tasks.internal`、`__manager_*`、`__review_*` 等用业务 Task 模拟控制任务的约定。`editing_implementer` 等会产生可依赖业务成果的 Agent 仍属于业务 DAG。
- [ ] 业务 Task 不再使用含义模糊的 `phase=execute`；保存真实 implementation lifecycle phase（`LOCAL / SYNC / SUBMIT / POLL / COLLECT / VALIDATE`）及独立 Task state，恢复动作只由版本化 implementation protocol 决定。
- [ ] 将沙盒 `mode=plan/execute` 改为类型化 workspace policy，例如 `WORKSPACE_READ_ONLY / TASK_OUTPUT_ONLY / REVIEW_SCRATCH_ONLY / FULL_PROJECT_EDIT`；动态 script 和 `editing_implementer` 明确使用 `TASK_OUTPUT_ONLY`，不再用“plan”表示只读挂载。
- [ ] 将 Profile 的 `permission=planning/step/review` 拆为明确角色与 action capability，例如 `PLANNER + INSPECT/READ_CONTRACT/PROPOSE`、`IMPLEMENTER + INSPECT/RUN_SCRIPT/SUBMIT_RESULT`、`REVIEWER + INSPECT/SUBMIT_DECISION`，避免与 Run 活动和 lifecycle phase 混名。
- [ ] 收敛动态 API 为 `/runs` 单一身份：创建 Run 时直接提交目标、授权和可选 `parent_run_id`；状态、命令、事件、Task、诊断与交付均以 `run_id` 访问，移除动态路径对 `/agent/{turn_id}` 与 `/workflows/{workflow_id}` 两套标识的依赖。
- [ ] 重写 Orchestrator reconcile 判定：graph 为空或开放且 `replan_after` 已满足时创建 PLAN control job；业务 Task 按依赖和生命周期执行；sealed 且无活动/清理/外部调用/待决事项时创建 FINAL REVIEW control job。规划是可重复的控制任务，不再是 Run 的前置阶段。
- [ ] 建立新模型的契约和竞争测试，覆盖多轮滚动规划、并行业务 Task、控制任务唯一性、授权版本变化、晚到 Proposal/结果、lease generation、最终验收、取消清理，以及 `state` 与派生 `activity` 不矛盾。

## 用户反馈驱动的同 Run 修订（已实现，待真机验收）

实现入口：`workflow_graph.DecisionAnswer` 定义 `approve / revise / reject`，`revise` 强制非空 `feedback`
并绑定 `decision_id` 与 `snapshot_digest`，只有 `revise` 能带 `attachments` 和 `upstream_tasks`；
`run_inputs.accept` 在同一 `BEGIN IMMEDIATE` 事务内写输入、建立屏障并按 `identity_digest` 幂等；
`run_inputs.revision_closure` 计算用户修订闭包，`workflow_graph.validate_proposal` 按
`user_revision_scope` 限制可改字段并分别累计 `repair_count / user_revision_count`；
`workflow_planning.apply` 为 subject 与受影响后代增加 revision 并清空候选/绑定/租约，保留旧 revision 与
检查点。前端 `static/dynamic.js` 的决策区提供修改意见、关联素材、上游选择与"按意见修改并继续"，
草稿按 Run/决策持久化，`canApproveDecision` 限制只有原声明的人工 Review 可批准。开发回归：
`tests.test_run_feedback`、`tests.test_run_replacements`、`tests.test_user_decisions`。
下列条目保留为真机验收项（真机要求见本节"完成标准"）。

目标：用户拒绝候选并补充明确修改意见后，在不创建新 Run 的情况下安全地修订受影响子图、重新执行和验收。该协议只处理原目标内的内容修正；改变顶层目标或最终交付类型时创建新 Run，避免把历史成功节点隐式解释成新需求的一部分。

已确认采用下文“运行中输入的排空屏障与统一重规划”：质量决策 `revise` 与普通运行中业务输入共用 `run_inputs`、全 Run 派发屏障和统一安全点。`run_inputs` 保存用户输入及顺序，`repair_context` 保存候选身份与允许修订范围；不另建直接触发 Manager 的质量反馈流程。以下条目是该统一流程中的质量决策约束。

### 决策与界面

- [ ] 将质量决策动作扩展为 `approve / revise / reject`。`revise` 必须携带非空 `feedback`、原 `decision_id` 和 `snapshot_digest`；`approve` 保持接受当前 revision，`reject` 表示停止当前方向但不自动重规划。
- [ ] 不提供质量豁免或“强制通过”能力。`approve` 仅适用于 Task 原本声明的用户 `review`，且程序验收、完整性检查和所有必需 Agent Review 已通过；程序失败、契约失败或 `visual_quality_reviewer` 拒绝后，用户只能 `revise` 补充意见或 `reject` 停止，不能把原 candidate 改成 accepted result。
- [ ] 动态页面在质量确认区增加反馈输入框和“按意见修改并继续”按钮；允许在该决策中上传同一目标所需的新素材，并在提交前展示素材、operation/Profile、Provider 次数和内容指纹的授权差异。
- [ ] 决策提交使用独立 `request_id` 幂等；其中 `revise` 绑定唯一 `run_inputs` 记录，决策答复、输入入队和首次屏障建立在同一事务提交。`approve/reject` 不额外创建业务输入。重复请求返回同一结果，不得重复增加 input sequence、authorization version、repair count、规划轮次或 Provider 额度。

### Orchestrator 协议

- [ ] Orchestrator 接受 `revise` 前重新校验 pending decision、Run 状态、graph/authorization version、subject task/revision、candidate/quality 身份和 snapshot digest；候选或验收条件变化时拒绝过期反馈。
- [ ] 将用户反馈作为不可变决策答复保存，并在同一事务写入 `run_inputs`：包含 feedback、attachments、decision ID、subject task/revision、candidate 身份和提交时的 graph/authorization version。类型化 `repair_context` 通过 input ID/sequence 关联这些证据，记录 `source=user_feedback` 及允许修订闭包；多条输入不得相互覆盖。统一安全点的 Manager 快照包含本批输入与修订上下文。
- [ ] 接受 `revise` 时，原质量 Decision 按正常拒绝/暂停语义完成，当前候选不获批准；同时建立全 Run 派发屏障并冻结 drain set。其他已开始的 Task、Wan 等外部任务及其必要验收/诊断正常结束，禁止领取新业务 Task。仅当全 Run 达到下文统一安全点时才解除 sealed 并创建一轮 PLAN control job，不只等待 subject Task，也不通过普通 `resume` 重跑旧 revision。
- [ ] 若用户只修改构图、样式、阈值等且不扩大范围，沿用当前 authorization version。输入接收不授予新权限；新素材只向 Manager 暴露待授权元数据。在统一安全点发现需要新增素材、operation、Profile 或 Provider 次数时，返回 `REQUEST_AUTHORIZATION`；用户批准完整新 authorization 后，重新校验项目归属和 content fingerprint，原子增加授权版本，并在同一安全点重新规划。
- [ ] 有未终结外部句柄或 `submission_unknown` 等未核实状态时，用户反馈可以入队，但不得直接修订或重放该 Task。继续核实、查询和收取原 external ID，未解决时保持屏障或进入显式用户决策。任何需要再次调用 Provider 的修改都创建新 revision/新 Task、消耗新额度，不能覆盖或重放原 request key。

### Manager 修订范围

- [ ] 在统一安全点的 Manager 快照中提供本批 `run_inputs` 的 ID/sequence、`user_feedback`、原候选、程序质量报告、Reviewer evidence、已验收上游和允许修订闭包；Manager 结果绑定本批输入及当前 decision/repair context，并使用下文六类结果包络，不能把自由文本反馈解释为扩大权限。
- [ ] 定义用户修订闭包：默认允许修改 subject Task 及其所有下游 revision；只有用户反馈明确要求改变上游成果且该上游在授权范围内时，Orchestrator 才把对应祖先加入闭包。闭包外已验收结果保持不变。
- [ ] 扩展 `revise_tasks` 的用户反馈模式：允许在闭包内修改 objective、parameters/argv、acceptance、semantics 和已授权输入引用；Task 类型、operation/Profile、implementation、输出角色或交付阶段发生变化时必须新增逻辑 Task，而不是静默改写原 Task 身份。
- [ ] Proposal 应用时为 subject 和全部受影响下游增加 revision，清空新 revision 的 candidate/accepted result、quality、bindings、external ID、lease 和 attempts，保留旧 revision、checkpoint、Reviewer 结论和用户反馈作为审计记录；同一事务标记本批输入 APPLIED 并记录 graph version。若仍有后续未应用输入，保留或推进 barrier，只有屏障解除后才恢复业务派发。
- [ ] 用户修订与自动诊断修订分别计数并设置有界上限；达到上限后进入显式用户决策，不允许 Manager/Reviewer 无限循环消耗模型和 Provider 预算。

### 完成标准

- [ ] 增加 API、Orchestrator、Manager snapshot 和浏览器测试，覆盖批准、拒绝、带反馈修订、空反馈、重复 request ID、过期 snapshot、并发候选变化和普通 resume 被拒绝；证明 revise 与普通输入进入同一邮箱、建立同一屏障、排空后只创建一轮 Manager，规划期间新输入不丢失。
- [ ] 覆盖新素材授权、content fingerprint 变化、未授权 operation/Profile、Provider 预算增加、提交未知、已有外部句柄、晚到旧 generation 结果和旧 revision 不能解锁下游。
- [ ] 真机验证至少包含图片抠图边缘修改、两图合成构图修改和 Wan 生成前参考图修改；逐例证明反馈进入 Manager 快照、只重跑受影响闭包、旧候选不发布、最终交付绑定新 revision。

## 运行中输入的排空屏障与统一重规划（已实现，待真机验收）

实现入口：`run_inputs.py` 的 `run_inputs` 表承载 Run 级输入邮箱（`ACCEPTED / PLANNING / APPLIED /
SUPERSEDED / FAILED`），`accept` 在首条未应用输入时原子写 `runs.input_barrier_sequence` 与
`run_input_drain` 快照，并限制 pending 条数与总字节；`can_dispatch` 让调度只放行 drain set 中
已开始的工作；`safe_point_reason` 检查活动租约、未完成 cleanup、staged result、待决决策、未终结
Provider 调用和控制任务，区分 `input_waiting_external_unknown` 与 `input_waiting_user_decision`；
`freeze_batch / finish_batch / defer_batch` 冻结本批 sequence 上界并在应用后推进或清除屏障。
`Orchestrator.process_inputs` 在安全点解除 sealed 并只创建一个 PLAN control job，
`Orchestrator.queue_plan` 在屏障存在时不另起规划，使诊断事实与用户输入合并为同一轮；
`workflow_graph.ManagerResult` 实现六类结果包络。开发回归：`tests.test_run_inputs`、
`tests.test_run_input_races`、`tests.test_run_recovery`。下列条目保留为真机验收项。

设计原则：运行中的用户业务输入不注入当前 Task、不判断与单个 Task 是否相关，也不默认取消模型、脚本、Operation 或 Provider 请求。输入只建立 Run 级派发屏障；屏障建立前已经开始的工作按原状态机完整结束并正常提交结果，屏障后不再启动新的业务 Task。存量工作全部排空后，Orchestrator 在统一安全点把新输入和最新 Run 事实交给一轮 Manager 重新规划。暂停、取消、状态查询仍走独立控制命令，不进入业务输入邮箱。

### 持久输入邮箱与屏障

- [ ] 新增 `run_inputs` 作为 Run 级权威输入邮箱，保存 `id / run_id / sequence / idempotency_key / content / attachments / base_graph_version / base_authorization_version / status / accepted_at / applied_at / error_code`；输入状态至少包含 `ACCEPTED / PLANNING / APPLIED / SUPERSEDED / FAILED`。
- [ ] 第一条未应用业务输入写入时，在同一事务中设置 `runs.input_barrier_sequence`，并记录此刻已经开始或已提交外部请求的 drain set。API 返回 accepted 只表示输入和屏障已持久化，不表示 Manager 已经处理。
- [ ] 同一 Run 的输入按 sequence 聚合到下一轮 Manager，不逐条启动规划；限制 pending 条数和总字节。重复 idempotency key 返回原记录，不得重复增加规划轮次、authorization version 或 Provider 额度。
- [ ] 新附件先登记为项目 asset；在安全点进入 Manager 前完成项目归属、存储额度和 content fingerprint 检查。未纳入授权的新素材可以随输入保存，但 Manager 只能看到其“待授权”元数据，不能读取内容或规划执行。

### 排空期间的正常处理

- [ ] 屏障不修改已运行 Task 的 spec、revision、lease 或状态。`structured-profile-v4` 完成当前模型/工具循环，script 和本地 Operation 正常退出，Provider Task 继续 submit/poll/collect/validate；Executor 仍保存 checkpoint、诊断、Provider receipt 和 staged result。
- [ ] Orchestrator 继续按原规则处理 drain set：结果可以正式变为 `succeeded / paused / retry_wait / reviewing / failed`，程序验收、Agent Review、用户已在处理的 Decision、失败诊断和安全重试均照常完成；不得仅因新输入把正常结果预先标记为 superseded。
- [ ] 失败 Task 的诊断可以继续创建和完成，但诊断后的自动 Manager revision 暂缓到统一安全点，与用户新输入合并为同一轮规划，避免并发产生两套 Proposal。
- [ ] Scheduler/Coordinator 在 claim 事务中检查 input barrier：允许 drain set 中已经开始的 Task 继续 poll、collect、validate、安全重试及完成其必要 Review/Diagnosis；拒绝领取屏障建立后新解锁或此前尚未开始的业务 Task。已经是 `ready` 的 Task 可保持 ready，但在屏障解除前不可 claim。
- [ ] 输入写入与 Task claim 存在竞争时以数据库事务顺序为准：claim 先提交则该 Task 进入 drain set 并正常完成；barrier 先提交则 claim 必须失败。不得通过前端时间或进程内标志判断先后。
- [ ] 对已提交 Wan 等外部任务不做隐式取消或替代提交；继续核实并收取原 external ID。`submission_unknown` 仍按原安全协议处理，不因为等待统一重规划而放宽重复调用约束。

### 统一安全点

- [ ] Orchestrator 仅在以下条件同时满足时进入输入安全点：drain set 没有 active lease、没有未完成 cleanup、没有 staged result 等待提交、没有仍需 poll/collect/validate 的外部 Task、没有由 drain set 派生且尚未完成的 Review/Diagnosis/Decision。
- [ ] 到达安全点前不解锁下游：`advance()` 和 Scheduler 都必须检查 barrier。上游 Task 即使正常 succeeded，其 blocked 下游也保持不可领取，避免新输入到达后旧 DAG 继续扩散。
- [ ] 到达安全点后，Orchestrator 解除 sealed、将全部 ACCEPTED 输入标记为 PLANNING，创建且只创建一个 PLAN control job（独立控制工作，不是业务 Task）；其冻结快照包含输入序列、刚完成的 Task/候选/验收/诊断、当前 graph 和 authorization version、剩余预算及未终结外部事实。
- [ ] Manager 可以保留刚完成结果、修订已有 Task、增加新 Task、调整后续依赖或请求扩权；在新 Proposal 应用前，现有成功结果保持正式且可审计，不预先清空。只有 Proposal 明确产生新 revision 时，旧 revision 才按既有 revision 规则被替代并重跑受影响下游。
- [ ] Proposal 成功应用后，将本批输入原子标记为 APPLIED 并记录 applied graph version，清除或推进 barrier；若规划期间又收到新输入，保留新 sequence 的 barrier，在本轮 Proposal 应用后再次排空并规划，不丢失输入。
- [ ] 如果 Run 在屏障处理前已满足旧图交付条件，禁止发布；先进入统一安全点。已经完成正式交付后到达的新输入创建同 Session 的 successor Run，不回写已 delivered Run。

### 统一重规划结果

- [ ] Manager 在安全点返回显式结果包络，不再仅靠空 `add_tasks/revise_tasks` 与 `sealed` 的组合推断意图。结果限定为六类：`CONTINUE / APPLY_PLAN / REQUEST_AUTHORIZATION / REQUEST_USER_INPUT / COMPLETE / NEW_RUN_REQUIRED`。
- [ ] `CONTINUE`：新输入不要求改变现有 graph；保留全部正常完成结果，标记本批输入 APPLIED，解除 barrier，继续派发旧图中尚未开始的 Task。
- [ ] `APPLY_PLAN`：提交一个版本化 Proposal，可同时保留已完成 Task、修订受影响 Task revision、增加新 Task/依赖并设置新的 `replan_after`；Orchestrator 校验并应用后解除或推进 barrier。
- [ ] `REQUEST_AUTHORIZATION`：计划需要新增素材、operation/Profile、Provider 次数或数据范围；创建冻结的用户授权决策，批准后在同一安全点重新运行 Manager，不先落半套 DAG。
- [ ] `REQUEST_USER_INPUT`：目标、验收标准或修改意见存在关键歧义；创建带明确问题和选项的用户决策，Run 保持 barrier，不猜测、不继续旧下游。
- [ ] `COMPLETE`：现有正常结果已经满足最新输入和交付目标；提交 sealed Proposal，随后仍走 Final Review 和一次性发布，不能由 Manager 直接宣布成功。
- [ ] `NEW_RUN_REQUIRED`：输入改变顶层目标、交付类型或需要与已完成 Run 分离的全新工作；当前 Run 不被静默改写，Orchestrator 等用户确认后创建同 Session successor Run，并显式选择可复用资产。
- [ ] 六类结果只有 `APPLY_PLAN/COMPLETE` 携带 Proposal；`CONTINUE` 不增加 graph version，其他决策类结果不修改业务 DAG。所有结果绑定 input sequence、graph/authorization version 和 snapshot digest，过期结果必须拒绝。

### 授权与用户反馈

### 用户输入导致契约变化的统一分类（新增）

用户在规划开始后追加的内容不能只按“是否有新文字”处理。Orchestrator 必须把输入相对当前冻结
Run/Task 契约的变化分类，并在安全点生成唯一的结构化结果。分类由宿主依据冻结快照、已应用输入、
当前 Proposal 和确定性字段差异复核，不能只信任 Manager 的自然语言判断。

| 变化类型 | 例子 | 正式路径 | 是否改变授权版本 |
| --- | --- | --- | --- |
| 同一 Run 内修订 | 修改已有 Task 的参数、目标细节、验收标准、语义或反馈 | 输入屏障 → `APPLY_PLAN` → 受影响 Task 新 revision；旧 revision 保留 | 否，除非触及授权字段 |
| 交付范围变化 | “只要 MP4”、增加 SRT/封面、修改主文件名或交付角色 | `delivery_scope_change` 提案 → 用户确认 → 更新交付授权 → 重新绑定 Final Review；已有合格成果可复用 | 是 |
| 素材范围变化 | 新增附件、替换素材、删除补充附件、素材内容版本变化 | 新素材/版本先进入授权决策；已开始 Task 继续排空；拒绝失效输入只 supersede 该输入 | 新素材或版本变化时是 |
| 能力范围变化 | 新增 Operation、Agent Profile、Provider 或 Gateway 能力 | `REQUEST_AUTHORIZATION` / `plan_change`，宿主按当前 capability catalog 重派生绑定 | 是 |
| 调用额度变化 | 增加 Provider 次数、模型 Token、时限、Task/规划上限 | 授权决策；已消耗额度不可回写，批准后从剩余额度继续 | 是 |
| Task 契约变化 | 改变 Task 类型、implementation、output role、依赖、`delivery_stage` | `APPLY_PLAN` 中显式 replacement 或新逻辑 Task；旧 Task/候选不可变 | 通常否；若扩大权限则是 |
| 顶层目标/交付类型变化 | 从图片改成视频、从剪辑改成完全不同任务 | `NEW_RUN_REQUIRED`，创建同 Session successor；原 Run 不静默改写 | 新 Run 重新确认 |
| 证据或审查范围变化 | 要求新增时间窗、音频对比、人工 Review | 新增 evidence Task 或 Review Decision；不把 evidence 直接升格为交付 | 通常否 |
| 执行控制 | 暂停、继续、取消、停止本地任务 | 独立 Control Command；不能伪装成业务输入或契约修订 | 否 |
| 已交付后的变化 | 交付完成后追加要求或改变主成果 | successor Run，显式选择可复用的已验收资产 | 新 Run 重新确认 |
| 模型/Runtime/安全策略变化 | 切换模型、Harness、沙箱权限、Gateway 能力 | 重新绑定当前策略并确认；不能静默切换已冻结 Run | 是 |

#### 统一约束

- `delivery_scope_change` 不能只写入 `run_inputs` 后继续沿用旧 `authorization.delivery_targets`。确认后必须
  生成新的授权版本，记录旧范围、新范围、来源 input ID、确认者和变更时间；发布事务只使用新版本。
- 任何 `WAITING_USER` 都必须带结构化 `blocking_action`：原因、当前状态、缺失项、建议动作、decision ID
  和可执行 CTA。仅显示“等待处理”或裸 `terminal_reason` 不合格。
- Final Review、发布和输入处理必须绑定同一 `authorization_version`。交付范围确认后，旧 Review 只能在
  审查身份重新计算并确认候选/证据未变化时复用，否则创建轻量 Final Review，不重跑媒体 Task。
- 已应用输入改变目标、交付范围、授权、预算或素材版本时，必须在 Run 摘要和输入历史中显示“改变了什么、
  是否需要确认、确认后影响哪些 Task”；用户不能通过重复点击旧按钮绕过决策快照。
- 用户拒绝范围/授权变化与取消整个 Run 使用不同 Decision 和文案。拒绝后保留原授权和已完成成果，清理
  未应用输入与对应 barrier；取消仍走 `CANCELLING → CANCELLED` 和完整回收。
- Manager 只能提交结构化变化意图，宿主派生授权版本、依赖、revision、交付阶段和 Provider 绑定；自然
  语言中的“已交付/不需要其他文件”不能直接覆盖服务端字段。

#### 当前缺口与真机验收

当前 Run `96df55a61b104ad1851c41ae1c8e226a` 已证明两个缺口：用户输入“只需要成片 MP4”已经 `APPLIED`，
但旧的六项 `authorization.delivery_targets` 没有升级，导致 Final Review 已通过仍被发布器以
`delivery_targets_incomplete` 阻塞；页面只显示“等待处理”，没有显示缺失范围、decision 或 CTA。修复必须
覆盖该 Run 的现有 `final.mp4` 复用路径，并增加以下矩阵：

1. 交付范围收窄、扩大、改主文件名、增加交付角色；分别验证授权版本、Review 身份、发布和不重复执行。
2. 新增/替换/删除素材，素材内容指纹变化，以及补充输入被拒绝；验证原 Task、barrier 和 successor 关系。
3. 新增 Operation/Profile/Provider、增加调用额度、模型预算/时限变化；验证授权决策、剩余额度和旧回执。
4. 修改已有 Task 的 objective/parameters/acceptance/semantics、Task 类型、依赖、output role、delivery stage；
   验证 revision/replacement、旧候选保留和下游重算。
5. 改变顶层目标、已交付后追加要求、模型/Runtime/安全策略变化；验证 `NEW_RUN_REQUIRED` 或重新授权。
6. 每一类都验证页面存在可执行提示；没有可执行决策的 `WAITING_USER`、只有裸错误码或旧范围摘要均为程序错误。

#### 组合契约变更真机矩阵

单点 case 不能证明多个输入同时到达时的版本和授权绑定正确。补充真机测试按以下组合执行；每个组合
都要求保存 input sequence、旧/新 authorization version、graph version、decision snapshot、受影响
Task/revision、预算账本、最终交付和清理证据。没有实际变更到对应字段时，不能把 case 计作覆盖。

| Case | 同时变化 | 必须验证 |
| --- | --- | --- |
| CC01 | 交付范围收窄 + 新增证据要求 | 旧媒体不重跑；交付授权先确认，证据 Task 绑定新范围，Final Review 重绑定后发布 |
| CC02 | 交付范围扩大 + 新增素材 | 先完成授权决策；新素材进入新授权版本，旧 Task 继续排空，不能让新下游偷跑 |
| CC03 | 素材替换 + Task 参数/语义变化 | 旧素材 revision 保留，新 revision 只重跑受影响闭包，未受影响结果复用 |
| CC04 | Operation/Profile 扩展 + Provider 调用额度增加 | capability/policy/额度一次性确认，旧额度账本保留，不得先发起外部调用 |
| CC05 | Token/时限预算变化 + 交付范围变化 | 授权版本和预算原子更新；已消耗额度不能归零，发布只使用新交付范围 |
| CC06 | 验收标准变化 + 输出角色/文件名变化 | 新 revision 和 Final Review 身份同时变化，旧候选不能按新文件名冒充成果 |
| CC07 | 顶层目标变化 + 新模型/Harness | `NEW_RUN_REQUIRED` 创建 successor；原 Run 不改写，模型和 Runtime 在 successor 冻结 |
| CC08 | 已交付后追加要求 + 复用旧成果 + 新交付格式 | successor 显式复用不可变旧成果，新格式只执行必要增量，原交付记录不回写 |
| CC09 | 新输入附件失效 + 用户拒绝 + 同时修改交付范围 | 输入 barrier、`SUPERSEDED`、授权决策和原 Task 排空顺序正确，不能重复提问 |
| CC10 | 质量 Review 反馈 + 增加证据范围 + 预算不足 | 先创建 evidence Task；预算不足有界停止，不能用新预算偷偷覆盖旧失败 |

组合 case 的调度规则：同一安全点内合并同批输入，只创建一个 PLAN；授权字段先进入决策，决策批准后
才允许 Proposal 应用。并发输入按 sequence 处理，旧 snapshot、旧 authorization version 或旧 decision
回答必须拒绝并生成新的可操作提示。组合 case 失败时分别归因每个变化字段，不能用其中一个模型失败
掩盖另一个程序性版本/授权错误。

- [ ] Manager Proposal 使用现有确定性授权校验：新增素材、operation/Profile、Provider 次数或交付范围时创建 `plan_change`；授权批准后继续同一安全点规划。Orchestrator 不需要在输入到达时判断“相关/不相关”。
- [ ] 前述质量决策 `revise` 仅负责把 feedback 和候选身份写入同一 `run_inputs` 邮箱并建立 barrier；原质量 Decision 按正常拒绝/暂停语义完成，后续与其他运行中输入一起统一规划，不维护第二套修订入口。
- [ ] UI 始终允许在活动 Run 中追加业务要求，并显示“已接收，等待当前任务结束后重新规划”；另保留显式“立即停止当前本地任务”控制按钮，但它属于取消/中断命令，不是普通输入的默认行为。对不可取消 Provider 明确提示仍会继续核实结果。

### 测试与真机证据

- [ ] 覆盖输入与 claim 原子竞争、多个并行 Task 排空、已有 ready 下游不被领取、模型/脚本正常结束、失败诊断与输入合并为一轮 Manager、Reviewer/用户 Decision 排空，以及安全点只创建一个 PLAN job。
- [ ] 覆盖 Wan submit/poll/collect、submission unknown、安全重试、staged result 提交、cleanup、输入在旧图即将发布时到达、规划期间再次输入和 delivered 后 successor Run；证明不重复收费、不丢 checkpoint、不发布旧图、不丢输入。
- [ ] 浏览器真机验证用户可连续追加多条要求，所有存量 Task 均按正常规则结束，新下游在 barrier 后零领取；证据绑定 input sequence、drain set、Run/Task/revision、attempt/generation、安全点时间、applied graph version、Provider 回执和最终交付。

## Planner 工具协议与异常恢复（2026-11-12 现场新增）

项目 `4f818eb59bc842dcaf68706cac4a58bc` 的 Run `6da1b5de70d546c19ec81a529c06561e`
在业务图创建前停止：第一、第二个 Planner Attempt 分别在模型调用等待 240 秒后收到
`RemoteDisconnected`；第二、第三个 Attempt 又分别收到一次包含多个工具调用的回复。请求已携带
`tool_choice.auto + disable_parallel_tool_use=true`，宿主仍按权威协议拒绝并以
`ambiguous_tool_calls` 记录。相同错误累计两次后进入
`WAITING_USER / planning_contract_exhausted`。该 Run 保持 `graph_version=0`，没有业务 Task、Provider
提交或交付，未知模型用量保留上限，Lease 与 cleanup 均已收敛；安全与计量行为正确，但恢复和用户状态
仍需改进。

2026-11-12 用户确认的协议方向：支持服务端受控批量执行相互独立的只读 `inspect` / `contract`；所有草稿
修改、提交、授权、用户决策和外部副作用仍保持每轮一个工具调用。批量只读是一个类型化工具动作，不是
放开任意并行 tool calls。

该 Run 从首次 Planner claim 到有界停止约 15 分钟。已知模型输入累计 495396 tokens，另有 346377
tokens 的两次中断调用按未知上限保留；10 次模型调用中，两次达到约 240 秒后连接中断，另有约 197 秒和
140 秒的成功调用。四次长等待合计约 817 秒，是总规划时间的主要部分。`inspect` 和 `contract` 工具本身
通常在 0～5 秒完成；长耗时主要来自大上下文的串行模型往返、上游响应延迟/超时，以及 Attempt 恢复后
重复读取素材和能力契约。控制角色使用 buffered 传输，等待期间页面也没有模型增量可展示。

- [ ] 将没有 pending decision、也不需要用户补充业务事实的 Planner 契约耗尽从真实
  `WAITING_USER` 中分离。公开状态应明确表示系统规划失败或可自动恢复，并提供与恢复策略一致的重试、
  切换路由/模型或停止入口；不能让用户猜测该补充什么。
- [ ] 区分模型传输中断、模型违反工具协议和草稿编译失败。连接中断不增加模型纠错计数；同一模型/路由
  连续传输失败后按冻结策略有界切换健康路由，仍保留原始未知用量，不能重放已经产生外部副作用的工具。
- [ ] 对 `disable_parallel_tool_use=true` 仍返回多个工具调用的情况记录请求能力、实际路由和响应工具数量，
  以区分模型违规与中间网关未透传；纠错反馈继续明确“本次没有执行任何工具”。
- [ ] 保持状态变更工具单调用：草稿增删改、提交、授权请求、用户决策和任何可能产生副作用的动作必须在
  单一 snapshot/lease/generation 下串行提交，避免顺序依赖、并发草稿 revision、部分成功和不可判定回滚。
- [ ] 只读且彼此独立的 `inspect` / `contract` 不必永久受顶层单调用限制。优先提供一个服务端有界批量查询
  工具，或只接受全部声明为 read-only、同 snapshot、无重复引用且有确定结果顺序的多调用；批次作为一个
  账本动作和一次观察返回。禁止把只读与草稿修改/提交混在同一模型回复中。
- [ ] Planner Attempt 恢复时复用服务端已验证的素材检查摘要和冻结契约查询结果，避免模型连接中断后重复
  下载、检查和查询；缓存身份必须绑定素材哈希、能力契约摘要、Run snapshot 和授权版本，不能复用可变或
  未验证内容。
- [ ] 为 Planner 快照和工具回执做 Token 体积审计：目标原文、授权能力索引、工具 Schema、已查询完整契约
  和历史观察分别计量；只向当前动作投影引用闭包，避免每轮重复发送无关完整契约。保留截断标识，不能以
  压缩为由遗漏授权、交付要求或用户修改。
- [ ] 模型等待步骤公开展示请求已提交、首包等待时长、当前路由及有界超时；连续慢调用或传输中断按冻结
  策略切换健康路由。buffered 只限制正文增量，不得让页面看起来像停止运行。
- [ ] 增加定向与真机回归：单个合法工具；零工具；两个只读工具；只读与写混合；两个草稿写；提交与其他
  工具并存；网关忽略 parallel-disable；模型连接在查询前、查询后和提交后中断。核对草稿 revision、纠错
  计数、用量、公开状态、Lease、cleanup 和 Provider 零重复提交。

## 资源等待与审查修复可达性（2026-11-06 现场新增）

现场发现通用状态机缺口：内容审查返回 `inconclusive` 后，Orchestrator 将候选 Task 暂停并打开
`repair_context`；若首次创建修复 PLAN 时执行器能力目录短暂不可用，Run 会留下
`terminal_reason=workers_unavailable`。后续普通推进又可能等待 `replan_after` 中同一个已暂停 Task
成功，形成没有活动租约或 Control Job、但 Run 持续为 `ACTIVE` 的 reconcile 热循环。以下条目不得按
单个媒体 case 特判。

- [ ] 区分审查结论：明确内容缺陷使用 `quality_failed`；没有确认缺陷、仅缺少充分证据的
  `inconclusive` 使用 `review_incomplete`。后者冻结原候选并进入 evidence repair，不默认重做媒体。
- [ ] `open_repair` 持久保存待规划意图、修复模式、subject revision、审查证据和错误类别。存在活动
  `repair_context` 且没有 pending/running/staged PLAN 时，Orchestrator 优先补建 repair PLAN，不受普通
  `replan_after` 就绪条件阻塞；执行器或 Orchestrator 重启后仍能恢复该意图。
- [ ] Proposal 校验拒绝把本轮未 revision、replace 或 `review_again` 的 `paused/failed` Task 作为
  `replan_after`。`replan_after` 只能引用能够独立推进到成功的新增或修订工作；失败候选取证继续使用
  冻结 `repair_candidate`，不得等待其 accepted output。
- [ ] Reconcile 检测存量不可达状态：活动 repair context、无活动 Planner、且 `replan_after` 包含暂停或
  失败节点时，原子清除不可达等待并补建 repair PLAN；无法恢复或预算耗尽时进入明确失败或真实用户
  决策，禁止保持伪 `ACTIVE` 每秒空转。
- [ ] `workers_unavailable` 由 Orchestrator 作为可恢复资源等待写入和清除。规划快照恢复且 PLAN/control
  job 创建成功时在同一事务条件清空该原因并记录恢复事件；采用有界退避，Observer 只告警、不修改
  Run。页面不得把历史残留原因当作当前执行器状态。
- [ ] 配额和公开状态区分正在执行、资源等待、系统恢复、真实用户决策与终态失败。没有 pending
  decision 的 `WAITING_USER`、资源等待和恢复检查不得伪装成用户可操作任务，也不得永久占用活跃执行
  名额。
- [ ] 增加回归：Reviewer `inconclusive` 后补证据并 `review_again`；Reviewer 明确 reject 后修订候选；
  首次 repair PLAN 创建期间全部 worker 下线再恢复；滚动部署能力目录短暂消失；不可达
  `replan_after` 存量自愈；恢复后清除 `workers_unavailable`；全程无重复 Provider 调用、无重复审查、
  无 reconcile 热循环。
- [ ] 部署中断按工作类型恢复，不能统一转成无决策入口的 `WAITING_USER`：PLAN/REVIEW/DIAGNOSE 等无外部
  副作用的 Control Job 在 lease generation 和冻结 snapshot 仍有效时由新 worker 有界重试；本地确定性
  operation 在清理完成且没有外部调用意图时按 implementation protocol 安全重跑；只有外部提交未知才
  进入专门的核实状态并沿用原 request/external ID，禁止重复提交。
- [ ] 进程退出、lease interrupted 和部署接管本身不是 `agent_failed` 或 `result_unavailable` 的充分条件。
  `agent_failed` 只表示新尝试实际耗尽格式/语义纠错仍无合法结果；`result_unavailable` 只用于无法通过
  checkpoint、Provider 查询、不可变对象或确定性重跑恢复的结果。恢复耗尽且不需要用户提供业务事实时
  进入 `FAILED`，不得写入没有 pending decision 的 `WAITING_USER`。
- [ ] 增加滚动发布回归：分别在 Reviewer 模型调用中、本地 operation 执行中、Provider submit 前、提交
  未知后和 poll/collect 中停止 worker/Orchestrator。核对新 release 接管原 Run、旧 generation 结果拒绝、
  Control Job 与本地 operation 有界恢复、外部请求不重提、清理完成，且公开状态和用户配额不出现僵尸
  `WAITING_USER`。

### 硬杀与跨 Release 恢复审查补充

以下条目以任意服务可在任意指令边界被 `SIGKILL` 为前提；不得依赖 signal handler、context manager、
`finally`、正常 lease release 或优雅 drain。Drain 只能降低中断概率，不能成为正确性的必要条件。

- [ ] 将基础设施中断与业务尝试分账。`claim` 可以增加 dispatch generation，但 worker/orchestrator 退出、
  lease 过期、节点失联和部署切换不能增加模型纠错、内容修订或业务失败次数；分别保存
  `dispatch_count / infrastructure_interruptions / execution_failures / model_corrections`。恢复上限使用对应
  计数，连续部署不得耗尽业务 `retry_policy`。
- [ ] 为 `workflow_model_reservations` 增加硬杀恢复者。Reservation 绑定 attempt、请求开始事实和可取得的
  原始回执；attempt 终结后仍为 `reserved` 时，按状态转换为明确的 unknown usage，保留有界 output 包络，
  能独立确认的 input 单独结算。不得永久占用整笔预留、伪造零用量或删除真实未知成本；预算展示、追加
  授权和终态报告必须使用收敛后的账本。
- [ ] 增加孤儿 Token 预留扫描与告警：检测 `reserved` reservation 对应 attempt 已
  `interrupted/expired/finished` 且超过恢复窗口；记录数量、Token 和最老年龄。修复前保留现场四条、合计
  262574 tokens 的证据，不能直接清零冒充供应商未计费。
- [ ] 处理跨 release runtime/profile 不兼容。新 worker 发现 Task 的 `_environment`、Harness/Profile digest、
  implementation 或 operation contract 与当前 release 不一致时，必须写入类型化
  `runtime_profile_changed / policy_changed / implementation_unavailable` 状态并触发受控 revision、重新授权
  或明确失败；禁止在 scheduler/claim 中静默 `continue/return None` 使 ready Task 永久无人领取。
- [ ] 不要求保留旧字段兼容，但必须选择明确的跨 release 策略：能够由冻结旧 release 安全恢复的工作交给
  对应 recovery worker；无法执行旧契约时由 Orchestrator 生成新 revision 并重新验收；不能用新代码静默
  执行旧冻结契约，也不能依赖人工发现无领取节点。
- [ ] 统一 Run 必须调用 `operation_lifecycle.Implementation.recover()` 或同一份类型化 recovery protocol，
  删除 Orchestrator 内与 LOCAL/SYNC/ASYNC 重复且已漂移的手写判断。每种 phase 明确 durable fact、允许的
  action、重试预算、下一状态和恢复者。
- [ ] Provider 恢复先按持久 `state` 分流，再检查 external ID/result：`succeeded+result` 进入 validate，
  `succeeded` 无结果进入 bounded collect/result recovery，`failed/rejected` 进入已知失败，`cancelled` 进入
  取消收敛，只有 `submitting/submission_unknown` 才按是否有 ID 查询或保持未知。终态调用不得重新 poll，
  已知失败不得误报提交未知。
- [ ] 利用 durable checkpoint 恢复本地结果。硬杀发生在输出上传/检查点完成与 `phase=validate` 落库之间时，
  Orchestrator 应按 attempt、Task revision、manifest 和哈希恢复 candidate 并进入 validate；不能因状态仍为
  local 就丢弃已持久结果或重做不可重试工作。
- [ ] 为确定性 reconcile 错误增加按 Run、graph version、exception/error signature 的熔断与退避。重复相同
  错误达到上限后进入明确的程序恢复状态并停止每秒写循环，同时保留诊断和运维恢复入口；版本或相关
  状态变化后才能解除熔断。`workflow_reconcile_failures` 不能只记录而不参与控制。
- [ ] 修正部署编排：不并行停掉全部 API、worker 和 Orchestrator 后再依赖进程自行收尾。发布可先启动可用
  控制面/恢复者并分批替换 worker，等待新 capability catalog 可见后再移除旧实例；但测试仍必须直接
  `SIGKILL` 全部相关进程，证明即使完全没有优雅窗口也能恢复。
- [ ] 对 API、Orchestrator、worker、watchdog/reaper、media helper 分别列出硬杀窗口：事务前后、claim 后、
  模型预留后、Provider intent 后/HTTP 前/HTTP 后、external ID 回写前后、checkpoint 前后、result staged
  前后、cleanup 中、Proposal staged/apply 中、输入 freeze/apply 中及发布事务中。每个窗口明确数据库事实、
  不可重复副作用、接管者和可观察终态。
- [ ] 保持已核实的正确边界并增加防回归：Orchestrator 事务硬杀回滚；Task result 继续绑定
  attempt/generation/revision；staged Proposal 等待旧 lease 与 cleanup；Provider intent 先于副作用；本地
  watchdog 使用单调许可；输入屏障与发布继续原子竞争。修复不得通过放松 fencing 或提前标清理完成来
  获得表面恢复。
- [ ] 改写当前把中断后 `WAITING_USER/result_unavailable` 当成功预期的测试。新增真实 PostgreSQL、多进程
  `SIGKILL -9` 矩阵，至少覆盖 Control PLAN/REVIEW/DIAGNOSE、editing Implementer、Script、固定本地
  Operation、SYNC Provider、ASYNC submit/poll/collect、模型回执、checkpoint、输入屏障、授权决策、取消、
  cleanup 和最终发布；断言内部状态、浏览器、预算、对象、Redis、Provider 与配额全部收敛。

## 重新授权的当前策略重绑定（2026-11-06 现场新增）

现场发现 `plan_change` 因 Agent policy digest 变化要求重新授权时，页面从旧 Run authorization 复制
`agent_profile_digests`，仅清空 `allowed_asset_digests` 后再次提交。服务端 `bind_scope` 正确识别旧摘要并
拒绝，但 Orchestrator 将具体错误折叠为 `invalid_or_stale_decision`，形成用户反复点击也无法通过的授权
循环。修复必须遵守以下通用边界：

- [ ] 重新授权请求只接受用户可决定字段：目标、交付范围、素材 ID、允许的 operation/Profile、Provider
  次数和时间/Token/Task/规划预算。页面不得回传 `agent_profile_digests`、
  `operation_implementations`、`operation_policy_digests`、`allowed_asset_digests` 等服务端派生绑定。
- [ ] Orchestrator 在批准事务内使用当前健康执行器 capability catalog、当前 Specialist policy 和当前素材
  内容重新派生实现、Provider policy、Agent policy 及 asset digest；绑定结果写入新的
  `authorization_version`。重新绑定不能扩大用户未确认的逻辑范围。
- [ ] 决策仍绑定 decision snapshot、graph version 和旧 authorization version。策略或能力在用户查看后
  再次变化时，废弃旧 decision 并生成包含当前差异的新 decision；素材 content fingerprint 变化时要求
  用户核对当前素材，但不得复用旧摘要制造无法批准的循环。
- [ ] 保留明确错误分类，例如 `decision_snapshot_stale`、`content_changed`、
  `agent_policy_changed_again`、`operation_policy_changed_again` 和 `scope_invalid`；API 与页面显示可行动原因，
  不把所有校验失败压成 `invalid_or_stale_decision`。
- [ ] 增加 API、浏览器和部署回归：创建 Run 后升级 Specialist policy、滚动更新 operation catalog、素材在
  确认前变化、重复批准和并发策略再次变化；证明一次有效确认能生成新授权并继续同一 Run，过期确认
  安全拒绝且能取得新的可批准快照。
- [ ] `plan_change` / `successor_authorization` 的 `reject` 不得复用质量候选拒绝的
  `WAITING_USER/user_rejected` 语义。拒绝重新授权只废弃对应 Planner 提案和该决策消费的输入批次：将
  输入标为 `SUPERSEDED` 或明确的 `REJECTED`，清除或推进 input barrier，并恢复到提案前可解释的 Run
  状态；不得留下开放图、已接受输入和无可操作决策同时存在。
- [ ] “拒绝本次范围变更”与“停止整个 Run”使用不同动作和文案。前者保留已完成节点和原授权范围，后者
  必须走 `CANCELLING -> CANCELLED`、关闭待决决策、终结或转移未应用输入并完成租约/Provider/清理核实；
  页面不得让“停止当前方向”看起来等同于取消处理。
- [ ] 拒绝或取消后的 DAG 继续保留不可变历史：已成功业务节点为 succeeded、被拒绝的授权 PLAN 为历史
  failed/rejected，且没有 running 呼吸节点。Run 摘要、输入状态、按钮和活跃配额必须与权威终态或恢复态
  一致；没有可操作决策的 `WAITING_USER` 不能继续占用用户活跃任务上限。
- [ ] 增加定向回归：拒绝 policy renewal、拒绝带输入批次的 plan change、拒绝后继续原图、拒绝后再提交
  新输入，以及从同一决策分别执行 reject 和 cancel；核对 decision、run_inputs、barrier、Run state、DAG
  节点、租约、清理、Provider 调用和用户配额没有残留或重复执行。

## 实施时必须落实的五个边界

以下是已确认统一方案的工程约束。五个边界的协议和定向竞争测试均已落到源码（实现与回归入口见下表末列），
但**都尚未完成部署后的当前 126 条真机验收**；不得仅凭本段记录、其他 TODO 勾选或开发回归通过宣称完成。
修改相关机制时按本表核对约束，不要据复选框推断实现缺口。

| 边界 | 实现要求 | 必须验证的结果 | 实现与开发回归 |
| --- | --- | --- | --- |
| 输入与最终发布竞争 | 输入接收与正式发布使用同一 Run 的事务互斥或条件更新确定顺序；发布事务复核未应用输入、屏障及当前图版本。 | 输入先提交则旧图不得发布；发布先提交则后到输入创建同 Session successor Run。并发与重复请求不能双重发布或重复创建 successor。 | `run_inputs.accept` 与 `Orchestrator._publish` 均在 `BEGIN IMMEDIATE` 内执行；发布前复核 `delivered_at`、`input_barrier_sequence` 和未应用输入，终态 `UPDATE` 带 `delivered_at IS NULL AND sealed=1 AND input_barrier_sequence IS NULL` 条件；`accept` 在 `delivered_at` 非空时复用或创建 `scope_confirmed=false` 的 successor。回归 `tests.test_run_input_races` |
| 规划期间再次输入 | 每个 PLAN control job 冻结本批输入 ID 和明确的 sequence 上界；只提交该批处理结果，后到输入保留 ACCEPTED 状态及屏障。 | 后到输入不被旧 Proposal 消费或覆盖；本轮结果应用后继续处理下一批，期间没有业务 Task 偷跑或旧图抢先交付。 | `run_inputs.freeze_batch` 冻结 `through_sequence` 并置 PLANNING，`finish_batch` 按剩余最小 sequence 推进屏障，`defer_batch` 回退本批；`ManagerResult.input_through_sequence` 绑定本批上界；`can_dispatch` 在屏障期间只放行 drain set。回归 `tests.test_run_inputs` |
| 排空遇到外部未知或用户决策 | 保留屏障，按原句柄核实外部状态；公开具体等待原因、所需决策和已有进展。未知提交不得自动重放，安全恢复须有界。 | 在途 Wan 继续原 external ID；待用户决策或外部核实不能长期伪装成正常执行，也不能通过超时直接清除屏障。 | `run_inputs.safe_point_reason` 区分 `draining_*`、`input_waiting_external_unknown`、`input_waiting_user_decision`，`Orchestrator.process_inputs` 对后两类置 `WAITING_USER` 并保留屏障；正常 poll 不消耗中断恢复次数，恢复次数有界。回归 `tests.test_run_recovery` |
| 修复替代与旧节点关系 | 新 plan 显式记录选中的 Task revision、仍必需的逻辑任务及合法替代关系；替代必须保留原目标覆盖、授权和验收，旧历史保持不可变。 | 被合法替代的旧失败 revision 不再阻塞当前交付；仍必需且未修复的失败节点禁止封口/交付。仅新增相似节点不能自动消除旧要求。 | `Proposal.replacements` 经 `validate_proposal` 校验（替代须有冻结修复触发、不得与 revise 并用、不得弱化 objective/acceptance/semantics/delivery_stage、不得引入无关素材或丢输出角色）；`workflow_planning.apply` 写 `run_task_replacements` 并置旧 Task `selected=0`；封口校验要求 `paused/failed` 节点已被 revise、replace 或 review_again 覆盖。回归 `tests.test_run_replacements` |
| 相同证据的重复审查 | 以候选内容哈希、验收标准摘要和证据集合身份识别审查对象；仅在出现新证据，或新 revision 实际改变候选内容/验收时允许新的内容审查。传输恢复、格式纠正归同一审查请求并有界。 | 空 Proposal、单纯增加 graph version、改 Task 名或重建 Reviewer 不得重审同一对象碰运气；无新依据时进入明确的待用户处理状态，不能豁免技术或 Agent Review 失败。 | `review_identity.identity` 按候选文件 SHA、输入内容、objective、acceptance、semantics 与已核实证据摘要计算身份，不含 Task 名和 graph version；`workflow_reviews` 上 `unique_review_content(run_id,purpose,review_identity)` 唯一索引，`Orchestrator.queue_review` 命中旧结论时改为 `quality_feedback` / `input_question` 决策并记 `review_reused_or_blocked`。回归 `tests.test_run_replacements` |

跨上述边界统一验证多节点竞争、事务中断、旧 generation/快照晚到和进程重启；证据需关联 Run、input sequence、plan version、Task revision、control job、attempt/lease 与发布记录。当前这些证据只覆盖隔离开发回归，不覆盖部署后真机。
