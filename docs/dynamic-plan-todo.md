# 动态工作流后续 TODO

当前阶段：已获授权实施全部优化、调整真机 case、隔离部署并完成逐例验收。基线已提交至本地 `feature/unified-run`（`7e667d8`）；GitHub 推送等待可用代理地址。main 增量适配已完成，不作为未完成项。实施进度见[统一 Run 实施记录](unified-run-progress.md)。

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

## Operation Provider 元数据拆分

- [ ] 拆分当前 `Operation.provider` 的多重职责。该字符串同时承担外部副作用标记、前端额度入口、授权计数、调度并发分组、运行时路由类别、外部清理类别和成果复用禁用条件，无法准确表达具体供应商、协议、模型及取消能力。
- [ ] 为 operation/implementation 定义类型化元数据，至少区分 `external_side_effects`、`budget_group`、`concurrency_group`、`route_adapter` 和 `provider_identity`；具体协议、模型及原始调用路由继续随调用意图持久化，凭据和私有 URL 不进入能力目录或 Manager 快照。
- [ ] 直接采用拆分后的最终数据模型，不保留旧 `provider` 字段兼容读取，不做旧数据迁移或双写；允许重建本分支专用数据库。新模型内已冻结的 implementation、授权额度和调用记录不能被后续配置改写；提交未知、原句柄查询、下载、取消和外部清理仍使用该次调用持久化的路由，不得根据新配置重提请求。
- [ ] 更新能力目录、授权页面、Manager 精简索引、调度资源声明、`provider_calls`、`task_lifecycle`、严格成果复用及诊断口径，移除依赖任意字符串分支的隐式行为；新增 provider 时必须注册完整路由和清理适配器，未知类别应拒绝而不是回退到现有图片编辑端点。
- [ ] 补充契约、授权、并发、恢复及扩展模块测试，覆盖本地 operation、Wan/Ark 图生视频、同步图片生成、转写、替代 implementation、提交未知，以及新模型调用记录在重启或配置变化后的恢复；所有读写统一使用拆分后的类型化字段，不提供旧字段回退。

## 移除 Plan / Execute 历史分层

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

## 用户反馈驱动的同 Run 修订

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

## 运行中输入的排空屏障与统一重规划

设计原则：运行中的用户业务输入不注入当前 Task、不判断与单个 Task 是否相关，也不默认取消模型、脚本、Operation 或 Provider 请求。输入只建立 Run 级派发屏障；屏障建立前已经开始的工作按原状态机完整结束并正常提交结果，屏障后不再启动新的业务 Task。存量工作全部排空后，Orchestrator 在统一安全点把新输入和最新 Run 事实交给一轮 Manager 重新规划。暂停、取消、状态查询仍走独立控制命令，不进入业务输入邮箱。

### 持久输入邮箱与屏障

- [ ] 新增 `run_inputs` 作为 Run 级权威输入邮箱，保存 `id / run_id / sequence / idempotency_key / content / attachments / base_graph_version / base_authorization_version / status / accepted_at / applied_at / error_code`；输入状态至少包含 `ACCEPTED / PLANNING / APPLIED / SUPERSEDED / FAILED`。
- [ ] 第一条未应用业务输入写入时，在同一事务中设置 `runs.input_barrier_sequence`（当前结构下写入 `workflow_runs`），并记录此刻已经开始或已提交外部请求的 drain set。API 返回 accepted 只表示输入和屏障已持久化，不表示 Manager 已经处理。
- [ ] 同一 Run 的输入按 sequence 聚合到下一轮 Manager，不逐条启动规划；限制 pending 条数和总字节。重复 idempotency key 返回原记录，不得重复增加规划轮次、authorization version 或 Provider 额度。
- [ ] 新附件先登记为项目 asset；在安全点进入 Manager 前完成项目归属、存储额度和 content fingerprint 检查。未纳入授权的新素材可以随输入保存，但 Manager 只能看到其“待授权”元数据，不能读取内容或规划执行。

### 排空期间的正常处理

- [ ] 屏障不修改已运行 Task 的 spec、revision、lease 或状态。`structured-profile-v1` 完成当前模型/工具循环，script 和本地 Operation 正常退出，Provider Task 继续 submit/poll/collect/validate；Executor 仍保存 checkpoint、诊断、Provider receipt 和 staged result。
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

- [ ] Manager Proposal 使用现有确定性授权校验：新增素材、operation/Profile、Provider 次数或交付范围时创建 `plan_change`；授权批准后继续同一安全点规划。Orchestrator 不需要在输入到达时判断“相关/不相关”。
- [ ] 前述质量决策 `revise` 仅负责把 feedback 和候选身份写入同一 `run_inputs` 邮箱并建立 barrier；原质量 Decision 按正常拒绝/暂停语义完成，后续与其他运行中输入一起统一规划，不维护第二套修订入口。
- [ ] UI 始终允许在活动 Run 中追加业务要求，并显示“已接收，等待当前任务结束后重新规划”；另保留显式“立即停止当前本地任务”控制按钮，但它属于取消/中断命令，不是普通输入的默认行为。对不可取消 Provider 明确提示仍会继续核实结果。

### 测试与真机证据

- [ ] 覆盖输入与 claim 原子竞争、多个并行 Task 排空、已有 ready 下游不被领取、模型/脚本正常结束、失败诊断与输入合并为一轮 Manager、Reviewer/用户 Decision 排空，以及安全点只创建一个 PLAN job。
- [ ] 覆盖 Wan submit/poll/collect、submission unknown、安全重试、staged result 提交、cleanup、输入在旧图即将发布时到达、规划期间再次输入和 delivered 后 successor Run；证明不重复收费、不丢 checkpoint、不发布旧图、不丢输入。
- [ ] 浏览器真机验证用户可连续追加多条要求，所有存量 Task 均按正常规则结束，新下游在 barrier 后零领取；证据绑定 input sequence、drain set、Run/Task/revision、attempt/generation、安全点时间、applied graph version、Provider 回执和最终交付。

## 实施时必须落实的五个边界

以下是已确认统一方案的工程约束，均待实现与验证；不得仅凭本段记录或其他 TODO 勾选宣称完成。无需再次确认产品口径，实施时按此补齐协议、竞争测试和真机证据。

| 边界 | 实现要求 | 必须验证的结果 |
| --- | --- | --- |
| 输入与最终发布竞争 | 输入接收与正式发布使用同一 Run 的事务互斥或条件更新确定顺序；发布事务复核未应用输入、屏障及当前图版本。 | 输入先提交则旧图不得发布；发布先提交则后到输入创建同 Session successor Run。并发与重复请求不能双重发布或重复创建 successor。 |
| 规划期间再次输入 | 每个 PLAN control job 冻结本批输入 ID 和明确的 sequence 上界；只提交该批处理结果，后到输入保留 ACCEPTED 状态及屏障。 | 后到输入不被旧 Proposal 消费或覆盖；本轮结果应用后继续处理下一批，期间没有业务 Task 偷跑或旧图抢先交付。 |
| 排空遇到外部未知或用户决策 | 保留屏障，按原句柄核实外部状态；公开具体等待原因、所需决策和已有进展。未知提交不得自动重放，安全恢复须有界。 | 在途 Wan 继续原 external ID；待用户决策或外部核实不能长期伪装成正常执行，也不能通过超时直接清除屏障。 |
| 修复替代与旧节点关系 | 新 plan 显式记录选中的 Task revision、仍必需的逻辑任务及合法替代关系；替代必须保留原目标覆盖、授权和验收，旧历史保持不可变。 | 被合法替代的旧失败 revision 不再阻塞当前交付；仍必需且未修复的失败节点禁止封口/交付。仅新增相似节点不能自动消除旧要求。 |
| 相同证据的重复审查 | 以候选内容哈希、验收标准摘要和证据集合身份识别审查对象；仅在出现新证据，或新 revision 实际改变候选内容/验收时允许新的内容审查。传输恢复、格式纠正归同一审查请求并有界。 | 空 Proposal、单纯增加 graph version、改 Task 名或重建 Reviewer 不得重审同一对象碰运气；无新依据时进入明确的待用户处理状态，不能豁免技术或 Agent Review 失败。 |

跨上述边界统一验证多节点竞争、事务中断、旧 generation/快照晚到和进程重启；证据需关联 Run、input sequence、plan version、Task revision、control job、attempt/lease 与发布记录。
