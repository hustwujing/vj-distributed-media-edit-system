# 动态工作流开发规范

适用于追加式 DAG、授权、Manager、专业 Agent、Orchestrator、执行阶段、审批和交付的修改。
协议目标见[设计](../../video-use-dynamic-agent-workflow-design.md)，实施与验收记录见
[实施记录](../dynamic-plan-implementation.md)。开发任务不得运行旧集群脚本操作 main。

统一 Run/Control Job、Provider 元数据拆分、用户反馈和运行中输入的后续重构，以[已确认方案与实施边界](../dynamic-plan-todo.md)为准，实施前必读并核对文末五项边界。当前实现与验证进度分别记录于[实施记录](../unified-run-progress.md)，源码实现不等于已部署验收。

## 实现入口与权威记录

| 职责 | 实现 | 权威记录 |
| --- | --- | --- |
| Task、授权和 Proposal 契约 | `workflow_graph.py` | `runs`、`run_tasks`、`workflow_task_revisions`、`workflow_plan_tasks`、`workflow_dependencies` |
| 冻结规划、草稿与提案 | `workflow_planning.py`、`workflow_plan_draft.py` | `planner_runs`、`workflow_plan_drafts` |
| 正式状态、恢复和交付 | `workflow_orchestrator.py` | reconcile generation、命令、审批、验收及交付事务 |
| 资源选择与领取 | `workflow_dispatch.py`、`scheduling.py`、`task_leases.py` | 既有 execution lease、attempt、资源和清理表 |
| 执行与候选结果 | `workflow_executor.py`、`execution_runtime.py` | `task_results`、既有不可变对象和步骤检查点 |
| 专业 Agent | `specialist_profiles.py`、`profile_harnesses.py`、`workflow_agents.py` | 冻结策略、检查记录、模型预算预留 |
| 用户入口 | `workflow_api.py`、`static/dynamic.*` | 工作流创建幂等键、`workflow_commands`、`workflow_decisions` |
| 受控 Agent 提交 | `workflow_gateway.py`、`agent_gateway.py` | 当前 Task lease 绑定的提案与待验证 Agent 输出 |

没有旧 Workflow 数据迁移或新旧双写。旧执行能力、适配器与测试夹具仍可复用，但独立部署
通过 `VIDEO_USE_DYNAMIC_WORKFLOW=1` 禁用旧的聊天执行/静态 DAG 写入入口。

## 修改顺序

1. 明确变更对象、当前状态、允许前置状态、绑定版本、幂等键和恢复者。
2. 更新模型和数据库记录，再接入 Orchestrator 的确定性提交。
3. 接入候选生产、调度、取消、恢复和公开状态；最后更新页面。
4. 用独立数据验证成功、失败、并发和进程中断；不修改运行中的 main 数据。

## 必须保持的不变量

- 聊天创建 Run 强制 `authorization.require_plan_confirmation=true`。第一次业务计划及用户输入导致的计划变更先形成 `plan_confirmation`，不创建新业务 Task。确认绑定 planner request、proposal digest、输入序号、图/授权版本与素材指纹；批准在同一事务中复核、补齐交付清单并应用原提案。禁止凭“开始规划”或模型文字自动批准。普通聊天修改会作废旧方案并保留被修改的方案上下文。
- `RESPOND` 只允许无业务 Task、graph_version=0 且无 Provider 调用的 Run，持久化公开答复并以 `conversation_answer` 结束；不产生媒体交付，不绕过验收。后续仍可提问。同项目父轮上下文和澄清历史只提供事实，不扩大授权或将旧产物直接变成新输入。未确认范围也允许只读回答。
- 主输入框可回答 `input_question` 或修订待确认方案，但不能隐式批准授权/质量决策。空业务轮连续三次澄清仍无进展后停止自动追问并明确原因。拒绝空业务澄清后不自行启动新规划；已有冻结业务与新的独立输入按原安全点规则处理。

- Manager 提交绑定 snapshot、授权版本、graph 版本与输入 cutoff 的 ManagerResult。结果仅限 CONTINUE、APPLY_PLAN、REQUEST_AUTHORIZATION、REQUEST_USER_INPUT、COMPLETE、NEW_RUN_REQUIRED、RESPOND；只有 APPLY_PLAN/COMPLETE 携带 Proposal；专业 Agent 的 JSON 或进程退出码不构成业务成功。
- Manager 不直接生成完整 Proposal。每个 PLAN Control Job 创建唯一 `plan-draft-v1` 草稿；按 Task 类型和已查询 Operation 契约使用白名单命令追加或局部替换内容，`commit_plan` 在同一事务复核草稿 revision/digest、租约和冻结快照后暂存权威 ManagerResult。草稿命令不创建业务 Task、不执行工具，也不授予权限。
- Manager 只用 `APPLY_PLAN` 表示本批增加了可执行工作，或用 `COMPLETE` 封口。等待叶节点由宿主从本批变更和依赖图推导；无变更时使用 `continue_run`。模型不填写 continuation。
- replacement 的既有依赖由权威图重映射；编译器从输入绑定派生新增依赖时，须排除重映射后已存在的边。不能要求模型修改输入来绕过宿主生成的重复依赖，也不能放宽原始 Proposal 的重复边校验。
- `delivery_stage` 由宿主根据业务验收的 `stage=final` 派生。Reviewer、Diagnostician和证据报告不能作为交付Task的输入；纯转交已验收成果时直接让真实生产Task拥有final验收，不增加空转复制Agent。
- Operation能力快照包含当前Provider约束；参考图尺寸、时长和其他可确定条件在Proposal应用前校验。校验失败不得进入Provider提交阶段。
- 新 Planner 协议不暴露 Script Task。确定性处理使用注册 Operation；需要动态代码时创建获授权的 `editing_implementer` Agent Task，由其在受限沙箱内执行。Manager、外部 harness 和 `/workflow/proposal` 均不得提交原始 shell、SQL、URL 或代码。
- `delivery_stage`表示Task的实际输出文件可直接交付，不表示它是DAG最后一步。Reviewer、Diagnostician、证据和内部检查报告不得成为交付阶段；sealed图必须包含实际业务交付物。内容Reviewer可以阻断上游，但其JSON报告不取得上游媒体的交付所有权。
- 只有 Orchestrator 提交正式 Task/Workflow 状态、批准产物和最终交付。
- 取消或控制任务失败若拒绝尚未应用的规划请求，关联草稿即使已由worker提交为COMMITTED也须收尾为REJECTED；不能只处理OPEN。按被拒绝的planning_request_id限定更新，已应用的历史草稿继续保留COMMITTED。
- 领取必须同时满足 `ready` 和空 `lease_id`。执行器提交结果后，调度器不得在控制面处理之前
  重领；没有结果的中断也必须先经过确定性恢复判断。
- execution lease 同时绑定 attempt、generation、step 和 task revision；检查、写入和发布均复核。
- reconcile lease 使用 owner、期限和独立 generation。旧控制器不能在失权后提交。
- 普通追加只影响新节点。受控修复通过 `revise_tasks` 创建不可变 revision；不得改目标、权限、输入输出、验收或策略。旧 revision 保留，修复根计数，依赖后代换 revision，无关分支复用。
- 自动原地修订的重试安全条件由 `automatic_revision_retry_safe` 同时供权威图校验、模型可改字段和修复选项使用。`retry_safe=false` 的操作不提供原地修订选项，按既有授权/预算走 `repair_replacements` 新任务；显式用户输入修订仍单独校验本轮授权，不能因隐藏非法自动动作而扩大或缩小用户权限。
- 规划快照保留当前 Task 的自动修复与用户修订计数；`revision_budget_remaining` 同时供权威图校验和模型工具选项使用。额度耗尽后隐藏对应修订/替换动作，公开剩余额度，不让模型反复提交必被拒绝的动作。普通新增 Task 不隐式替换旧交付，也不能重置修复额度。
- 方案能力、操作实现、专业 Agent 策略、素材及累计预算必须在已确认授权包络内。
- 沙盒只装载声明的素材、输入角色及本任务可恢复输出；不能因同属项目就暴露全部历史文件。
- 上游调用意图先于副作用，提交未知不得重提；poll/collect 恢复只能使用已有句柄。
- 技术验收独立于模型意见。媒体最小完整性检查不能被模型的 acceptance 删除。
- 步骤验收绑定不可变 Task 契约、修订、候选及输入证据；不因无关扩图重复验收。
  task review仅使用当前步骤objective及review_criteria，不得因未来步骤尚未完成而拒绝中间成果；worker和控制器均校验criterion范围。
  Final Review 必须绑定完整 sealed graph及全局交付目标，且检查过每个选定交付物。
- 暂停阻止新调度，取消不可被随后到达的 pause/resume/answer 撤销。终态命令立即拒绝，不能永远 pending。
- 未 sealed、有未完成必要任务、活动租约、待清理资源、用户决策或提交未知时不交付。
- Reviewer选定交付集合的列表顺序不代表主成果声明。最终通过多个交付物时，必须使用`primary`明确选定其中已实际检查的不可变引用，遵守用户主成果要求；worker和控制器都校验。发布按该引用标记恰好一个主成果，文件名排序不得覆盖；缺失/越界选择不交付，同名文件也不能产生多个主成果。
- 复用已验收产物通过同项目、固定 revision、哈希及不可变对象引用进行，不以文件名猜测来源。

## 输入输出与错误

`workflow_graph.Task` 是新任务契约；`execution_spec()` 仅将它转换为复用执行适配器所需的形态，
不授予状态修改权。适配器允许固定输入绑定，状态提交必须通过 `task_results`。
Agent 的原始 `/workflow/agent-result` 只是待验证输出，不能携带可信 technical report 冒充执行器结果。

执行及程序检查失败按冻结错误策略处理；安全同 revision 恢复、Manager revision、外部状态核实和用户决策必须区分。诊断完成和步骤内容拒绝会把实际证据交回 Manager，不能停在诊断终态，也不能通过反复继续刷出通过意见。最终验收拒绝时，后续规划输入包含实际验收反馈。

Planner 首轮只接收冻结能力摘要；完整 operation 契约通过 `contract` 动作按 ID 查询并校验摘要。Reviewer 的检查、格式纠正和 finalize 调用分别计数，finalize 阶段拒绝新增检查。响应结构诊断只记录长度、哈希、对象数量和错误位置，不保存私有推理或敏感正文。

模型命令 Schema 必须从代码类型生成并执行复杂度门禁，至少限制总字节、最大深度、对象数和联合分支数。每个命令只携带其引用闭包内的 `$defs`；不得把完整 Task/Operation 联合契约复制到所有工具。校验失败返回字段路径、错误码和允许的局部草稿动作；相同错误连续两次有界停止，不能重新生成整份计划或无限重试。

### Agent 工具集原则

- 工具按Agent实际工作流和决策边界设计，不镜像内部API。正常Manager阶段只暴露一个`submit_typed_plan`写入口；`inspect`和`contract`因结果会改变后续判断而保持独立，审批、外部副作用和最终发布不并入草稿工具。
- 只合并总是连续、确定性、封闭域且可在宿主事务中共同校验的步骤。可选分支、需要观察中间结果的步骤、不可逆副作用和用户确认保持分离。复杂规划允许合理的2–4次调用，不以最少调用数替代成功率。
- 工具目的必须互斥。正常阶段不同时暴露add/configure/submit/commit等重叠入口；提交失败后才按错误临时开放replace/remove和commit，修复完成后收回。
- 宿主已知的snapshot、版本、授权、实现、replacement身份、默认技术验收和确定性引用映射不由模型填写。工具输入使用语义化短ID和强类型枚举，结果只返回下一决策所需的有界结构。
- 专用 `request_authorization` / `new_run_required` 工具的授权字段必须为非空对象，不能沿用总结果联合模型中的 nullable 字段。授权目标由宿主分别绑定原冻结目标或该命令唯一的 `new_goal`；新目标须包含非空白内容，不要求模型在授权内重复填写目标。完整范围仍经权威校验和用户确认，工具调用不授予执行权限。
- 初始工具集保持小型，Operation完整契约按需加载。工具定义本身计入输入Token，Schema复杂度、模型调用、总Token、p50/p95耗时、重复错误、最终合法率和非法落地数必须共同评测。
- 草稿修改幂等且无业务副作用；Provider调用、审批和发布继续使用各自的意图、授权、幂等与恢复协议。工具描述或annotation只是提示，执行层仍独立校验。

参考：[OpenAI Function Calling](https://developers.openai.com/api/docs/guides/function-calling)、[OpenAI Programmatic Tool Calling](https://developers.openai.com/api/docs/guides/tools-programmatic-tool-calling)、[Anthropic Writing Effective Tools](https://www.anthropic.com/engineering/writing-tools-for-agents)、[Gemini Function Calling](https://ai.google.dev/gemini-api/docs/function-calling)。

失败要保留阶段、错误分类、来源（模型脚本/注册 operation/Agent）、退出码和关联标识；
可保留失败检查点，不能把它作为已验收输入。日志不输出提示词、完整脚本、供应商正文或凭据。
结果未知、提交前尚未发送、已失败、产物丢失、质量失败、资源不足和预算耗尽必须区分。只有固定提交适配器在调用意图尚未创建时给出的 request_sent=false 可以证明未提交，不能从模型文字或脚本日志推断。

## 验证与完成标准

复审证据资格由 `review_evidence.relationship` 统一供工具 Schema 和权威 Proposal 校验使用。除匹配当前候选 revision 的证据外，也接受被审任务实际读取过的源素材证据：必须同时匹配 asset ID、双方持久化输入 SHA 和已验证报告的 source_reference/source_sha256。规划快照仅投影所需绑定，不依赖当前可变素材名。源素材证据说明输入，不能冒充输出质量或音频语义证明。转写的声音事件是供应商检测结果；空事件列表不等于已证明遗漏，也不能被画面或能量包络推断补写。任务与最终审查均接收注册 operation 的输出契约和能力限制。

先运行 `tests.test_dynamic_graph`、`tests.test_dynamic_gateway`、`tests.test_dynamic_api`；
执行或恢复变化追加 `tests.test_dynamic_execution`、lease、scheduler、取消、对象存储和诊断相关模块。
真实分布式用例必须使用独立 PostgreSQL、对象协议服务、节点目录、UID 与真实沙盒。

完整发布必须绑定冻结源码摘要，完成 Python、JavaScript、实际浏览器及对应真机 case。
工作区有并行未提交修改时，可使用`./deploy/sandbox_cluster.sh deploy --source-from /data2/wujing03/干净工作树`冻结已提交源码；仅更换代码来源，命名空间、私有配置、端口和数据根仍由当前sandbox wrapper固定。该来源必须是干净Git checkout，未提交修改会在冻结前拒绝；不得临时移动、覆盖或清理他人的工作区文件来制造干净目录。
修改 case 时维护[审查矩阵](../real-case-campaign/dynamic-case-review.md)；报告区分业务成功、
模型失败已正确处理、依赖阻塞、程序缺陷、未触发边界和未执行，不把采样缺口计为通过。

Final Reviewer的检查回执同时提供内部inspection_id、短candidate_key、不可变ref及是否可交付。证据引用inspection_id；最终deliverables/primary优先使用candidate_key，宿主在当前attempt和snapshot内解析。精确ref、唯一basename及权威output role可规范化；单一已检查候选可以由宿主自动补齐选择。旧attempt、任意路径和歧义映射拒绝。发布器只接收控制器再次校验后的不可变ref。

共享基础设施的systemd生命周期不得控制独立业务实例：共享PostgreSQL使用Wants/After，不使用Requires/BindsTo/PartOf形成连带停止。自身media/watchdog仍按实际需要保持强依赖。新增依赖须以专用临时服务验证停止与恢复行为，不能通过重启main共享服务做回归。

## 输入、修订与冻结授权

交付清单修改必须显式确认：后续plan_confirmation优先展示本批Manager提出的完整delivery_targets，展示原/新清单及被移除的文件名、格式和角色；确认后才写新授权版本，不能因为旧清单非空而忽略新清单。自动修复不得修改交付目标。plan_change改变交付清单需绑定待处理用户输入，顶层goal改变仍创建新Run；清空已有清单拒绝，不能以空目标跳过交付。

“初始计划”只按当前 Run 判断，不按项目、会话或用户历史判断：当前 Run 必须同时满足 `graph_version=0`、没有业务 Task、且没有 `workflow_plan_tasks` 记录。初始方案经用户确认时可以补齐该 Run 的结构化交付契约；当前项目即使已有已交付 Run，也不能把这次补齐误判为旧契约修改。已有计划后的交付契约变化必须绑定当前 Run 新的显式 `run_input`。已交付 Run 收到新的制作请求时创建 `scope_confirmed=false` 的 successor Run；活动 Run 中修改同一成果走 replan，明确独立的新工作走 new_task，歧义时只追问且不改变图。语义分类由 MESSAGE Manager 的结构化动作完成，禁止用业务关键词或正则决定。

`effective_delivery.confirmed_scope`从授权历史及已批准方案回执派生最新交付变更证明；Planner、Final Reviewer、发布器共用同一证明。确认后的完整清单覆盖旧的交付名称、格式、附属角色和文件数量，不覆盖内容/方法/内部图层轨道数及技术质量要求。旧授权下已完成的交付叶节点不再强制逐个交付，但可复用合格候选；新授权下声明的交付任务仍必需，失败任务不被隐藏或当作成功。Final Review身份包含该证明，预算等无关授权变动不改变交付证明身份。

普通聊天先由`workflow_messages`创建MESSAGE Control Job，复用media_planner和原模型预算，不创建业务Task、不提供媒体/规划写工具、不计规划轮次。输入先为ROUTING；reply转REPLIED并保存逐条答复，既不增加业务input_sequence也不建立屏障。只有Manager明确选择replan，或用户通过既有决策按钮提交修改时，才调用`run_inputs.activate`建立屏障及作废受影响确认。此规则替代下文历史“所有运行中输入一入队即建立屏障”的默认行为。

消息按sequence串行判断，后续消息包含此前问题与答复；到安全点后才生成实际新方案。纯聊天不进入APPLIED user_changes。消息失败只更新自己的状态及可见错误，不暂停业务Run；回复中的状态标明快照时间。MESSAGE沿用Attempt/generation、账本与有界恢复，但不阻塞业务最终发布；发布先发生时，晚到的明确修改创建scope_confirmed=false的successor，不回写原交付。SUCCEEDED Run可收尾已经接收的MESSAGE，失败/取消则明确收尾未回复消息。服务端和前端都须区分业务终态与消息是否仍待回复。

Session 不承担执行状态；Run 正式 state 与派生 activity 分离。PLAN/REVIEW/DIAGNOSE 只写 control_jobs，不创建 internal 业务 Task 或 agent_turn。调度和取消监控读取 execution_subjects 的统一身份，不能漏掉 Run。

所有运行中输入和质量 revise 都写 run_inputs。claim、首条输入屏障与发布在同一控制事务内确定先后。drain set 中的原 revision 正常完成，包括外部句柄查询、候选检查点、验收和诊断；屏障外不领取新业务任务。Manager 冻结输入 sequence 上界；规划期间的新输入留给下一轮，禁止丢失或抢先发布旧图。

授权追加写 run_authorizations；已有 Task 保留原 grant，新建或修订 Task 使用当前 grant。增加预算不能丢弃历史使用或未知预留。已交付输入建立 scope_confirmed=false 的 successor；未完整确认不得规划业务执行。

质量决策携带候选摘要与快照，重复请求幂等、过期请求拒绝。只有原本声明的人工 Review 可 approve，不能批准技术失败或 Agent 拒绝。用户可指定修改根，服务器计算后代闭包；无关节点保持 revision、哈希和验收记录。自动修复不得改变原授权/要求。replacement 必须显式声明，被替代失败和历史 plan selection 保留。

修订权限只来自本轮冻结 `input_batch` 或自动修复触发；`user_changes` 中已 APPLIED 的输入只提供历史事实，不能在后续普通规划轮重新授予修改 objective/acceptance 的权限。不得按交付物类型、尺寸或验收签名猜测 replacement；新增相似交付物保留原 Task，替代关系必须来自授权范围内的显式替代请求。在线 worker 目录损坏或非空目录无一致契约时延后规划，恢复后再创建 PLAN，不把暂不可知的能力当作空目录交给 Manager。

失败候选通过 RepairCandidateRef 的 revision/artifact/hash/role 冻结引用，只供 repair/evidence Task 读取。media.evidence 的真实输出与源/参考/预览哈希绑定。review_again 只接受新增且已验收的证据，不能把技术失败改成通过。相同候选内容、验收标准和证据不因重命名或递增 revision 而重新抽取审查结论。

修复Task后续修订必须保留全部既有输入，包括已绑定的历史repair_candidate，不能把过滤后的普通输入与未过滤旧输入比较。已冻结授权的同一候选可随该Task修订或显式replacement继续读取，即使原生产Task已退役；仍逐次校验其Run、revision、角色、对象与哈希。新候选及无关新Task仍按本轮修复作用域校验，不因该例外获得通用历史文件访问权。

候选的重审额度耗尽后，Planner 不再提供该候选的 review_again 或继续补证选项；权威 Proposal 校验也拒绝为该未变候选追加自动证据任务，避免花费计算和规划额度生成无法再审查的证据。实际内容修订、显式用户授权的证据工作及最终交付审查分别遵守各自边界，不通过放宽重审次数解决。

最终审查身份还包含已应用的用户要求及目标上下文，不能在收到新要求后复用此前的结论。纯图版本变化若未改变内容、目标、验收、证据和用户要求，则仍复用原通过结论；发布器须在事务中重算当前最终审查身份并匹配，继续核对当前交付资格。保留原审查快照和图版本，不改写历史，也不放宽候选/素材校验。

最终交付审查补证使用 collect_delivery_evidence 指引；新证据验收成功后调用 complete_run，重新封口进入最终审查。任务级候选补证才调用 review_again。能力提示和空 APPLY_PLAN 的纠错指引必须与当前工具 Schema 一致，不能把最终审查恢复引向未开放的任务级字段。

交付文件名与验收条件绑定必须匹配实际文件类型：SRT不能绑定视频尺寸/音轨条件，JSON报告也不能冒充媒体。
将通配验收绑定到某个交付物时保留原通配规则，为每个匹配文件派生明确规则，不能因处理第一份输出而丢掉后续同类型输出的要求。

规划图像验收时区分存储通道与像素覆盖：RGBA并不要求每层含透明像素，分层文档的完整背景可以不透明。只有用户明确要求透明前景、抠图、蒙版覆盖或某层透明范围时才增加对应条件；不能把格式名称扩成额外透明比例或语义拆层标准。独立可编辑结构、有效图层内容和合成结果仍需核验，不以允许不透明背景为由接收空图层。

最终审查清单必须保留完整用户目标的有界条目，不能仅使用 Planner 汇总的交付名称。
多行要求分别形成审查条目；不能仅因 Task acceptance 漏写，就将用户要求降为建议或豁免。可修改参数在用户实际修改前仍按原值执行。
原生工具协议只移除旧返回包络的格式说明，`review_policy` 等审查语义必须保留到实际模型上下文。回归需捕获执行链路发送给模型的内容，不能只检查上游 frozen 字典里存在规则。
SVG 可编辑性通过有界 XML 结构、真实 text/tspan、滤镜与嵌入源图摘要检查，不从渲染预览猜测；结构读取不执行脚本、不解析外部实体、不访问 href。
最终审查因证据不足暂停时，公开提示必须包含最近正式审查的具体反馈，即使所有业务 Task 都已通过；不能仅显示“查看下方反馈”却没有内容，也不依赖流式过程文字补齐事实。
每个条目都需举证；图层、可编辑参数、独立音轨等结构要求不能用外观相似的扁平预览代替。
已应用用户修改继续优先于旧内容/参数，原始目标条目不是恢复已被替换要求的许可。

既有证据节点可以继续等待未验收上游；上游随后暂停，不应使无关的新补证提案失效。暂停候选必须使用冻结引用的校验针对新增或修改的节点，旧等待关系仍受原有依赖派发规则约束，不能提前读取候选。

替换 Task 继承的验收与交付阶段保持原值；全 Run 的交付文件名/格式提示不得再次重写中间 Task 的冻结要求。Operation 替换为 Agent 也遵守同一规则。

显式交付文件名提取须区分输出要求与“不要重新处理原始输入”的否定约束，尤其是只授权复用成果的新 Run，原素材名可能不在其输入资产列表。否定语境中的原素材引用不增加交付文件或格式；其他位置明确要求交付同名文件时仍须校验，不得因出现过否定处理语句而全局忽略该文件。

明确“不交付”的文件声明覆盖此前创建/保留候选时的同名提及；后续用户明确要求交付该文件时才解除排除。最终工具选择、Review校验和发布共用排除后的候选集合；仅因候选缺失不能跳过必交付Task。报告角色只在同一语句的正向交付要求中派生，不把“交付WAV；内部证据报告”跨句连成报告要求，purpose=evidence也不得因全局格式/报告需求被宿主提升为交付阶段。

全局交付名绑定不得改名明确排除的候选，否则会把原先不允许交付的文件变成同名交付候选，且与其业务objective矛盾。排除文件保留其声明路径、输出角色和验收文件名。

存在明确带文件名的“交付/deliver”要求时，全局必交付名称以这些语句为准，不把前文步骤objective或内部复制过程的文件名一并列为最终要求。没有明确命名的交付语句时保留直接文件目标识别；改名和明确排除规则继续生效。

注册Operation的原生输出名可能固定为image.png等；当某格式只有一个确认目标名、一个合格候选且归属唯一时，宿主可在发布元数据中绑定目标名。最终Review快照提供publication_names，选择校验和发布使用相同映射；原始候选路径、哈希、检查点及来源Task保持不变，不新增复制Task或对象。多候选/多目标歧义、格式不同、Agent自定义输出和明确排除的候选不做自动映射。

异构片段使用独立 media.normalize_clip Task，统一明确规格并保存验收检查点，media.concat_sequence 只负责兼容流顺序汇合。禁止在单一脚本中隐藏大量解码器或让一次失败重做所有片段。

用户批准原本声明的Review后，最终Reviewer快照提供server-owned user_reviews，只包含当前选中Task、当前revision且candidate_digest仍匹配的已应用approve决定。该事实不豁免技术或Agent失败。Manager回显错误的快照绑定字段仍拒绝暂存，但在同一有效租约/请求内给予有界纠正；失效租约、过期请求及图版本竞争不改为可忽略错误。

Manager与最终Reviewer均接收同一来源的已批准用户Review事实。控制快照另含有界history：不可变Task规格/摘要、依赖创建和退休版本、plan选择及replacement关系；最终Review还接收失败事实。旧失败仅作为历史，不重新成为当前交付要求。截断必须明确标识，不能让Agent伪造历史文件来补缺失的数据库事实。

## 用户选择模型与强度

`runs/capabilities.model_choices`提供已配置的模型预设、原生强度档位和profile_digest；创建Run可带model_choice，但不能传入路由、凭据或执行引擎。请求幂等摘要包含model_choice，同键改型号或强度必须拒绝。幂等重放先读取原Run，再考虑当前注册表，不能因后台配置更新拒绝已成功的同请求重放。

profile_digest用于校验预设版本；configuration_digest保存最终配置，覆盖推理强度时两者可以不同。最终配置写入agent_run_profiles并成为所有Control Job和Task的模型来源。已有Run不可改写；自动successor继承父Run冻结配置，显式的新Run选择可以覆盖继承。public.model_configuration只读取持久化记录，不能用当前默认值冒充缺失的历史配置。
