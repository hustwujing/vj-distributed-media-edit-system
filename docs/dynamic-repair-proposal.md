# 动态工作流受控修复协议（讨论稿）

状态：待讨论，尚未实现。程序尚未上线，再次部署允许重置本分支专用数据库；因此不做旧表兼容、数据迁移、双写或旧协议适配，直接采用最终数据模型。测试保持暂停。

## 一、数据模型

修复协议使用三个层次：

1. **Logical Task**：稳定的业务身份，例如 `compose_video`，描述这一业务要求是什么。
2. **Task Revision**：Logical Task 的不可变实现版本。每次改变实现、参数、输入绑定或执行方式都创建新 revision；历史 revision 不修改。
3. **Plan Version**：某一时刻的有效执行图，明确每个 Logical Task 选择哪个 revision，以及 revision 间的依赖和输入绑定。Workflow 只有一个 current plan version。

结果、验收、attempt、lease、generation、调用意图和用量均绑定 `logical_task_id + task_revision`。Planner 和 Reviewer 绑定 plan version。发布只读取当前 plan version，历史失败不会被抹掉，也不会继续阻塞已经合法替代的要求。

数据库可以直接重建为版本化表，例如：

- `workflow_logical_tasks`：稳定 Task ID、业务目标、是否为当前交付所必需。
- `workflow_task_revisions`：不可变 spec、验收、输入绑定、创建原因、替代的旧 revision。
- `workflow_plan_versions`：plan version、父版本、触发失败、sealed、状态。
- `workflow_plan_tasks`：该 plan 对每个 Logical Task 选择的 revision。
- `workflow_plan_dependencies`：该 plan 下的依赖边。
- `workflow_failures` / `workflow_repair_requests`：失败事实、诊断、受影响闭包、修复状态与轮数。

旧的“已有 Task 不能变，只能追加新 Task”约束改为：**历史 revision 不可变，但新 plan 可以选择新的 revision**。这样不需要伪造新业务 Task ID，也不需要修改历史记录。

## 二、哪些问题允许自动修复

自动修复必须同时满足以下条件：

- 用户目标、最终交付目标和用户指定方法不变。
- 原验收标准原样继承，或仅增加更严格/更客观的证据检查；不得删除、放宽或偷换标准。
- 只使用原授权素材、已有合法派生产物和已授权 operation/Profile。
- 累计供应商调用、费用、模型 Token、时间、Task 数和修复轮数仍在原授权内。
- 外部调用状态已知；不存在 `submission_unknown`。
- 用户没有暂停、取消，Workflow 尚未正式交付。

允许自动修复的类型：

| 类型 | 例子 | 处理 |
| --- | --- | --- |
| 调用参数或绑定错误 | `background` 类型错误、角色/输出名错误、已授权输入未正确绑定 | 创建同一 Logical Task 的新 revision，改正参数或绑定，保留原目标与验收 |
| 本地实现错误 | FFmpeg/Python 脚本错误、编码参数不合法、输出路径错误 | 诊断后创建新 revision；不增加未授权外部调用 |
| 产物未达原质量标准 | 音轨、尺寸、字幕、蒙版、合成结果不合格 | 在原素材、能力和预算内创建修复 revision，重新走原验收 |
| 缺少验收证据 | 缺音高、响度、Alpha、关键帧或完整解码证据 | 优先创建证据 Task revision 或 Review revision，引用同一候选哈希；不重做媒体 |
| 明确 retry-safe 的瞬时失败 | 本地进程中断、可安全重试的查询/收取 | Orchestrator 先按原 revision 重试或恢复，无需 Manager 改图 |
| 已知终态的外部失败 | 上游明确 failed/rejected，且原授权还有调用额度 | Manager 可创建新 revision 和新的调用意图；累计调用继续计数 |

必须交给用户决策的情况：

- 改变用户目标、最终交付、用户指定方法或素材范围。
- 改变/降低验收标准。例如把“无音轨”改成“允许静音音轨”。
- 新增 operation/Profile、收费调用或任何预算扩张。
- 用户要求本身存在无法可靠消解的歧义。
- 外部提交结果未知，需要重新发起调用。
- Workflow 已经正式交付，需要做新版本。
- 达到自动修复上限，或诊断仍无法确定修复方案。

一个关键例子：如果冻结验收是 `audio=false`，自动修复可以创建“删除音轨”的新 revision；不能把验收改成“PCM 为零即可”。如果用户原意只是静音，修订验收含义必须得到用户确认。

## 三、如何交回 Manager

借鉴main后，失败处理分三层，不是所有问题都直接交给Manager：

1. **恢复/重试层**：已有外部句柄继续poll/collect；明确retry-safe的瞬时失败按同revision有界重试，不改变方案。
2. **确定性repair policy层**：Task revision可携带服务端登记的修复选项，以程序错误码/失败检查码匹配。选项必须在确认时已冻结，只能改变允许的本地argv或安全本地operation参数。每个选项最多使用一次，默认最多2次修复。
3. **Manager repair层**：没有确定性策略、需要组合新步骤或重绑受影响下游时，才创建repair request交给Manager。

具体流程：

1. Orchestrator 保存 `workflow_failure`：失败 revision、attempt/generation、阶段、错误分类、候选哈希、验收反馈、调用状态和预算快照。
2. 对运行时中断，先执行恢复/重试层；技术检查报告命中冻结repair policy时，Orchestrator在事务中选择一个尚未使用的选项并创建新revision。
3. 原因不明确时运行诊断 Agent。诊断只提交事实、不确定性和建议，不能直接改状态。诊断结论由Orchestrator决定进入策略修复、Manager修复或用户决策。
4. Orchestrator 使用真实图遍历计算 **affected closure**：失败 Logical Task 以及所有输入依赖于它的下游任务；不能依赖数据库返回顺序做单次扫描。无关分支不受影响。
5. 创建 `repair_request`，冻结：当前 plan version、authorization version、失败事实、原目标与验收摘要、受影响闭包、可复用 accepted 结果、已有待用户决策、剩余预算和允许的修复字段。
6. Manager 返回 `RepairProposal`，只能：
   - 为受影响 Logical Task 创建新 revision；
   - 选择继续复用哪些未受影响 revision；
   - 提供新 plan 下的依赖和输入绑定；
   - 声明需要用户决策或无法修复。
7. Orchestrator 在事务中校验并创建新 plan version。Manager 不提交正式状态，也不能自行放宽验收。

Reviewer/诊断Agent先提交staged decision；必须等Agent沙盒停止、修复脚本检查点持久化后，Orchestrator才应用repair。用户停止、revision变化或plan变化优先，迟到decision拒绝。这个stage/commit分离直接借鉴main。

建议的 Proposal 核心字段：

```json
{
  "repair_request_id": "...",
  "base_plan_version": 4,
  "base_authorization_version": 1,
  "reason": "...",
  "task_revisions": [
    {
      "logical_task_id": "remove_audio",
      "supersedes_revision": 0,
      "spec": {},
      "acceptance_digest": "原验收摘要"
    }
  ],
  "plan_tasks": {},
  "dependencies": [],
  "replan_after": [],
  "sealed": false
}
```

同一个 failure 只能存在一个活动 repair request。Proposal 若绑定的 plan/authorization 已变化则拒绝，重新生成快照。用户暂停时不启动 Manager；取消后所有修复 Proposal 和迟到结果都无效。

## 四、修复 revision 如何替代旧结果并衔接下游

以 A → B → C → D 为例，B revision 0 失败：

1. Manager 创建 B revision 1。
2. 如果 C、D 的输入绑定直接或间接依赖 B，则为 C、D 创建 revision 1，把结构化输入改指向当前 plan 中 B/C 的新 revision。
3. 新 plan version 选择：A r0、B r1、C r1、D r1，并建立 A→B r1→C r1→D r1。
4. 旧 plan 及 B/C/D r0 全部保留历史状态；它们不再属于 current plan，因此不参与新调度和最终交付。
5. A r0 已 accepted 且与修复无关，可以按不可变 artifact ID/SHA 复用，不重复执行。

这不是“修改旧 Task 的依赖”，而是创建新的不可变 revision，再由新 plan 选择它。

不同下游状态：

| 状态 | 行为 |
| --- | --- |
| 未领取 | 旧 revision 从新 plan 退出；只调度新 revision |
| 已运行 | 请求停止，等待 lease 失效和本地清理完成后才能激活替代 revision；迟到结果只留证 |
| 已成功但依赖旧输入 | 历史成功保留；新 plan 必须选择重算 revision，旧结果不得进入新交付 |
| 与失败无依赖 | 原 revision 和 accepted artifact 继续复用 |
| 仅缺证据 | 复用同一候选哈希，新增证据/Review revision，不重做媒体或外部生成 |

需要使用失败候选做修复时，输入必须显式绑定 `logical_task_id/revision/artifact_id/sha256`，并标记为 repair-only read input。它不会因此变成 accepted，也不能绕过对象完整性检查。

## 五、验收与完成条件

- 新 Task revision 默认继承对应 Logical Task 的原验收摘要；修改摘要必须证明等价或更严格，任何放宽进入用户决策。
- 相同产物、相同证据不能反复请求 Reviewer 直到偶然通过。重新审查必须有新产物或新增的可核验证据。
- 修复决定绑定review request、失败revision、plan version、authorization version及snapshot digest；同一请求已stage后不能换结论。
- 当前步骤或其依赖祖先可作为根因修复目标；不能修复无关兄弟分支。修复目标的全部后继revision失效，无关成功分支和独立待用户决策继续保留。
- 只有根因Task消耗一次repair_count；因输入版本变化而重跑的后继仅增加revision，不消耗自己的修复额度。retry_count、repair_count和重新规划轮数分别记账。
- 候选和结果被替代时标记superseded并退出交付选择，不直接删除；日志、质量事件、调用账本和失败证据保留。
- 修复会使旧 sealed 状态失效。最终 Review 绑定 current plan version、authorization version、全部有效 revision 和实际 artifact 哈希。
- Workflow 完成条件是 current plan 中所有必需 Logical Task 的所选 revision 均通过；不存在活动旧 lease、待清理资源、未解决 failure、待用户决策和未知外部调用。
- 历史失败保留，但不要求被替代的历史 revision 变成 succeeded。

建议每个 Logical Task 默认最多 2 个自动修复 revision；同一修复策略/输入/验收摘要的指纹不得重复。新 revision 不重置供应商调用、Token、时间或修复次数。

审查轮数耗尽时，Manager 可以追加客观测量、合并检查证据或拆成职责明确的检查步骤，但不能通过新 ID 无限重跑同一个 Reviewer，也不能擅自提高冻结 Profile 上限。额度或证据仍不足时请求用户决策。

## 六、对应上一轮 25 条失败

- 契约/参数 10 条：多数可以创建新 revision 自动修正；如果原验收含义本身偏离用户目标，则必须请求用户修订标准。
- 审查轮数 7 条：先改检查协议和结论预算；允许补证据/重组检查，禁止简单重跑绕限。
- 内容质量/判断 6 条：真实缺陷可在原授权内修复；证据不足先补证据；需求范围歧义交给用户。
- 脚本失败 2 条：确定根因后换实现 revision；外部状态未知时禁止重新提交。

当前实现中，最终 Review 拒绝会返回 Manager，但步骤拒绝通常直接暂停，诊断完成固定进入 `diagnosis_ready`。实现本协议时应统一接入 repair request，而不是把所有诊断和步骤拒绝都停在人工处理。

## 七、从main重做逻辑借鉴的机制与需要避开的坑

值得直接吸收的机制：

- `repair`与`replan`分开：原目标/输入/依赖/输出/验收均不变时才是同方案repair；改变图、目标、输入或验收走新plan及必要确认。
- 自动repair只改本地脚本argv或显式声明安全的本地operation参数；外部/模型调用默认禁止自动重发。
- 修复范围允许定位当前失败步骤的依赖祖先，再使修复目标及其全部后继失效；无关成功分支保留。
- 修复前检查输入revision，修复后所有验收保存并核对`input_versions`，防止旧结果或旧Review放行新输入。
- 预登记修复选项按技术错误码选择，选项一次性使用；没有匹配方案时不盲试。
- 修复最多2次；目标修复次数与后继重算、同revision重试分别计数。
- 独立分支的待用户决策不能因另一分支修复而被清掉；仅supersede受影响闭包内的旧Review和决策。
- 外部调用需绑定明确replacement和用户确认，普通quality批准或resume不能授权重新生成。
- 跨方案复用使用固定workflow/step/revision和不可变receipt，校验项目、结果状态、spec、输入签名、artifact SHA、运行环境及服务端契约，并在新版重新验收。
- staged review decision在Agent退出和检查点落盘后才由服务端提交；取消、失权和revision变化优先。

main实现中不直接照搬的部分：

- main在`agent_steps`当前行上原地增加revision并清空结果，另用`workflow_step_versions`留部分历史。我们可以重置数据库，应直接采用不可变revision和current plan，不需要原地覆盖。
- main同Workflow repair不能改变输入或依赖，较大变化必须取消旧Workflow、重新确认并新建Workflow。动态系统可在原授权/原目标/原验收内创建新plan version；只有越过授权边界才请求确认。
- main的affected集合通过步骤列表顺序逐步扩展。新实现必须做显式图遍历，避免非拓扑顺序或复杂汇合漏掉后继。
- main跨Workflow严格复用只允许`reuse_safe`本地operation，不允许任意script。对跨Workflow继续保持这一保守边界；同Workflow的新plan复用未受影响的同一accepted revision则不是跨方案复制，不应重复执行。
- main的严格reuse要求来源step仍是当前succeeded revision。不可变revision模型中，来源revision即使不再是current也可保留历史有效性，但必须明确是否被撤销验收、对象是否完整，以及授权是否允许在新Workflow使用。
- main允许用户确认后重跑可能涉及外部步骤；新实现还必须按provider request key和实际调用状态逐项判断。`submission_unknown`永远不能靠确认repair直接重发，应先对账或开启明确的新用户决策流程。

main还验证了一个容易漏掉的竞争条件：修复一个并行分支时，兄弟分支已存在的用户选择仍然是门禁；修复完成不能误把整个Workflow设为可无条件推进。

## 八、完成标准

实现前允许重置独立数据库。完成至少覆盖：revision/plan原子切换、受影响闭包、无关分支复用、运行中旧revision取消、迟到结果、修复去重、汇合分支、候选来源、验收不可降级、预算累计、外部提交未知、最终发布和跨节点恢复。

本协议不处理旧数据库兼容，也不提供旧 Workflow API 兼容层。

## 九、系统改动职责划分

当前结论：主要修改程序，少量修改Agent提示和输出Schema，补充前端确认展示；暂不新增Agent角色。现有Manager、Editing Implementer、Visual Reviewer、Failure Diagnostician和Final Reviewer足以承担职责。

| 能力 | 改动类型 | 实现位置 |
| --- | --- | --- |
| 静音/无音轨、声道、增益等媒体状态类型 | 程序契约 | `quality_policy.py`、`operation_parameters.py`、`operations.py` |
| 文本、Alpha、蒙版、动作时间等类型化验收 | 程序契约 | `quality_policy.py`、`quality_check.py`、`workflow_graph.py` |
| 验收来源与适用阶段 | 程序契约 | `workflow_graph.py`、`workflow_planning.py` |
| Proposal参数与验收语义一致性校验 | 程序门禁 | `workflow_planning.py`、`workflow_graph.validate_proposal()` |
| 音高、响度、完整解码、时序、Alpha和蒙版测量 | 确定性程序 | 新建`media_evidence.py`，由`quality_check.py`调用 |
| Reviewer批量检查及检查/纠错/finalize预算 | Agent运行协议 | `workflow_agents.py`、`specialist_profiles.py` |
| repair request、不可变Task revision、current Plan version | 控制面程序 | `workflow_graph.py`、`workflow_orchestrator.py`，新建`workflow_repair.py` |
| 声明式大规模媒体执行图 | 确定性程序 | `workflow_runtime.py`、`operations.py`，新建`media_plan_compiler.py` |
| 展示主体范围、动作时间和修复/用户决策 | API与前端 | `workflow_api.py`、`static/dynamic.js` |
| 告诉模型如何生成和使用上述契约 | 提示与技能说明 | `specialist_profiles.py`、`references/dynamic-operations.md`、`references/dynamic-media-editing.md` |

原则：提示词只负责指导模型使用契约，服务端程序负责校验和强制执行。不能仅靠提示词区分静音/无音轨、字段类型或验收阶段。

不新增“证据Agent”：精确媒体测量必须由确定性程序生成结构化报告。不新增“修复Agent”：Orchestrator判断恢复、策略修复、Manager修复或用户决策；Manager负责新方案。Failure Diagnostician只需扩展结构化输出，例如`failure_class`、`repairability`、`required_user_decision`，不直接提交状态。

`media_plan_compiler.py`必须按输入兼容性选择执行形态：同规格片段优先concat demuxer；不同规格片段拆成独立规范化Task并提交不可变检查点，再由单输入顺序拼接Task汇合。编译器基于输入数、codec、分辨率、线程配置和节点TasksMax进行资源准入；模型不能直接提交无限规模的多输入FFmpeg filter graph。解码与编码线程分别显式限制，完整诊断绑定Task revision和执行资源快照。

大致工作量构成为：约70%状态机/契约/确定性工具，20%提示及Agent输出Schema，10%前端与用户决策展示；这是范围估算，不是交付进度。
