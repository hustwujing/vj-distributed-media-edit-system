# 动态工作流开发规范

适用于追加式 DAG、授权、Manager、专业 Agent、Orchestrator、执行阶段、审批和交付的修改。
协议目标见[设计](../../video-use-dynamic-agent-workflow-design.md)，实施与验收记录见
[实施记录](../dynamic-plan-implementation.md)。开发任务不得运行旧集群脚本操作 main。

统一 Run/Control Job、Provider 元数据拆分、用户反馈和运行中输入的后续重构，以[已确认方案与实施边界](../dynamic-plan-todo.md)为准，实施前必读并核对文末五项边界。当前实现与验证进度分别记录于[实施记录](../unified-run-progress.md)，源码实现不等于已部署验收。

## 实现入口与权威记录

| 职责 | 实现 | 权威记录 |
| --- | --- | --- |
| Task、授权和 Proposal 契约 | `workflow_graph.py` | `runs`、`run_tasks`、`workflow_task_revisions`、`workflow_plan_tasks`、`workflow_dependencies` |
| 冻结规划与提案 | `workflow_planning.py` | `planner_runs` |
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

- Manager 提交绑定 snapshot、授权版本、graph 版本与输入 cutoff 的 ManagerResult。结果仅限 CONTINUE、APPLY_PLAN、REQUEST_AUTHORIZATION、REQUEST_USER_INPUT、COMPLETE、NEW_RUN_REQUIRED；只有 APPLY_PLAN/COMPLETE 携带 Proposal；专业 Agent 的 JSON 或进程退出码不构成业务成功。
- 只有 Orchestrator 提交正式 Task/Workflow 状态、批准产物和最终交付。
- 领取必须同时满足 `ready` 和空 `lease_id`。执行器提交结果后，调度器不得在控制面处理之前
  重领；没有结果的中断也必须先经过确定性恢复判断。
- execution lease 同时绑定 attempt、generation、step 和 task revision；检查、写入和发布均复核。
- reconcile lease 使用 owner、期限和独立 generation。旧控制器不能在失权后提交。
- 普通追加只影响新节点。受控修复通过 `revise_tasks` 创建不可变 revision；不得改目标、权限、输入输出、验收或策略。旧 revision 保留，修复根计数，依赖后代换 revision，无关分支复用。
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

失败要保留阶段、错误分类、来源（模型脚本/注册 operation/Agent）、退出码和关联标识；
可保留失败检查点，不能把它作为已验收输入。日志不输出提示词、完整脚本、供应商正文或凭据。
结果未知、提交前尚未发送、已失败、产物丢失、质量失败、资源不足和预算耗尽必须区分。只有固定提交适配器在调用意图尚未创建时给出的 request_sent=false 可以证明未提交，不能从模型文字或脚本日志推断。

## 验证与完成标准

先运行 `tests.test_dynamic_graph`、`tests.test_dynamic_gateway`、`tests.test_dynamic_api`；
执行或恢复变化追加 `tests.test_dynamic_execution`、lease、scheduler、取消、对象存储和诊断相关模块。
真实分布式用例必须使用独立 PostgreSQL、对象协议服务、节点目录、UID 与真实沙盒。

完整发布必须绑定冻结源码摘要，完成 Python、JavaScript、实际浏览器及对应真机 case。
修改 case 时维护[审查矩阵](../real-case-campaign/dynamic-case-review.md)；报告区分业务成功、
模型失败已正确处理、依赖阻塞、程序缺陷、未触发边界和未执行，不把采样缺口计为通过。

共享基础设施的systemd生命周期不得控制独立业务实例：共享PostgreSQL使用Wants/After，不使用Requires/BindsTo/PartOf形成连带停止。自身media/watchdog仍按实际需要保持强依赖。新增依赖须以专用临时服务验证停止与恢复行为，不能通过重启main共享服务做回归。

## 输入、修订与冻结授权

Session 不承担执行状态；Run 正式 state 与派生 activity 分离。PLAN/REVIEW/DIAGNOSE 只写 control_jobs，不创建 internal 业务 Task 或 agent_turn。调度和取消监控读取 execution_subjects 的统一身份，不能漏掉 Run。

所有运行中输入和质量 revise 都写 run_inputs。claim、首条输入屏障与发布在同一控制事务内确定先后。drain set 中的原 revision 正常完成，包括外部句柄查询、候选检查点、验收和诊断；屏障外不领取新业务任务。Manager 冻结输入 sequence 上界；规划期间的新输入留给下一轮，禁止丢失或抢先发布旧图。

授权追加写 run_authorizations；已有 Task 保留原 grant，新建或修订 Task 使用当前 grant。增加预算不能丢弃历史使用或未知预留。已交付输入建立 scope_confirmed=false 的 successor；未完整确认不得规划业务执行。

质量决策携带候选摘要与快照，重复请求幂等、过期请求拒绝。只有原本声明的人工 Review 可 approve，不能批准技术失败或 Agent 拒绝。用户可指定修改根，服务器计算后代闭包；无关节点保持 revision、哈希和验收记录。自动修复不得改变原授权/要求。replacement 必须显式声明，被替代失败和历史 plan selection 保留。

失败候选通过 RepairCandidateRef 的 revision/artifact/hash/role 冻结引用，只供 repair/evidence Task 读取。media.evidence 的真实输出与源/参考/预览哈希绑定。review_again 只接受新增且已验收的证据，不能把技术失败改成通过。相同候选内容、验收标准和证据不因重命名或递增 revision 而重新抽取审查结论。

异构片段使用独立 media.normalize_clip Task，统一明确规格并保存验收检查点，media.concat_sequence 只负责兼容流顺序汇合。禁止在单一脚本中隐藏大量解码器或让一次失败重做所有片段。
