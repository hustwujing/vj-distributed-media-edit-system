# 工作流开发说明

适用于修改工作流计划和步骤契约、依赖解析、执行状态、检查点、质量验收、Agent 审查、用户决策、暂停/继续/取消、恢复或工作流 API 的任务。字段与兼容映射参考[工作流契约](../workflow-contracts.md)，并以当前代码核实；[工作流平台设计](../workflow-platform-design.md)保留历史方案背景。operation 自身实现按[新增与扩展 operation](add-operation.md)执行；底层租约、隔离和成果提交按[分布式执行开发说明](change-distributed-execution.md)处理。

## 1. 先判断变更层级

| 变更 | 主要入口 | 额外关注 |
| --- | --- | --- |
| 计划、步骤、参数或依赖契约 | `webapp/workflows.py` | Pydantic 校验、拓扑顺序、输入角色和版本绑定 |
| 本地/同步/异步步骤执行 | `webapp/workflows.py` 中的 `Runner`、`webapp/workflow_runtime.py` 中的 `Adapter`、`webapp/operation_lifecycle.py` | 状态推进、阶段调用和恢复 |
| 输入、输出和检查点 | `webapp/workflow_inputs.py`、`webapp/step_checkpoints.py`、`webapp/operation_result.py` | 项目归属、revision、sha256、角色和文件类型 |
| 验收、自动修复和 Agent 审查 | `webapp/workflow_review.py`、`webapp/quality_policy.py` | 只读审查、证据、候选产物、修复范围和用户确认 |
| 用户决策或控制 | `webapp/user_decisions.py`、`webapp/workflows.py` | 决策类型、目标 revision、暂停边界和权限 |
| 规划与直接入口授权 | `webapp/execution_intent.py`、`webapp/agent_context.py` | 已确认意图、能力范围、项目内容版本 |
| API 或页面状态展示 | `webapp/agent_api.py`、`webapp/static/` | 只展示服务端状态，不从客户端推断完成或权限 |

先找最相近的状态和测试，再决定是否需要新增字段或状态。普通工作流扩展不能通过在 Runner 中增加 operation ID 分支来绕过现有协议；新增 operation 应回到 operation 专项说明。

## 2. 编码前读取并记录不变量

先读 `workflows.py` 中受影响的模型与状态路径，再按变更选读以下入口：

- `webapp/workflows.py`：`Plan`、`Step`、`validate_plan`、`submit`、状态变更、暂停/继续/取消和质量处理。
- `webapp/workflow_runtime.py`：输入环境、阶段调用、外部任务、输出验收和错误分类。
- `webapp/workflow_inputs.py`：素材及步骤输出解析、依赖声明、revision 和哈希校验。
- `webapp/workflow_review.py`：Agent 验收快照、结构化结论、修复和恢复。
- `webapp/user_decisions.py`、`webapp/execution_intent.py`：用户决策和已确认执行意图。
- `tests/test_workflows.py`、`tests/test_workflow_input_paths.py`、`tests/test_quality_workflows.py`、`tests/test_workflow_agent_review.py`、`tests/test_user_decisions.py`、`tests/test_workflow_reliability.py`：现有契约、竞争和恢复测试。

编码前明确记录：受影响对象和状态、允许的前置状态、输入与输出 revision、是否产生新尝试、是否能重试、取消边界、是否需要用户确认、旧数据和旧 API 的兼容处理。

## 3. 修改计划和步骤契约

- 工作流步骤只有 `script` 和 `operation` 两种 `kind`。`script` 通过 `argv` 声明在受限沙盒中执行的本地命令，并使用受控输入和预检查；`operation` 只接受已登记能力的业务参数，不能通过参数开放任意命令、服务 URL、凭据或宿主路径。
- 新字段必须在 `Plan` / `Step` 的模型校验、持久化、公开状态和恢复路径中同时处理。未知字段应拒绝，枚举值和集合长度应有边界。
- 每个步骤的 `id` 唯一且按依赖拓扑顺序出现；`depends_on` 只能引用之前的步骤。步骤输入引用另一个步骤时，必须同时声明该依赖和输出 `role`。
- operation 步骤复用 operation 的参数、输入类型、输出角色和实现版本校验；已有确认方案不能因为默认实现或配置变化而改变。
- 执行意图固定 route、能力与实现、目标和预计时长，并限制外部调用范围；`acceptance`、`output_roles`、`on_failure`、`retry_safe`、`timeout` 属于随后提交的 `Step` 契约，不是 `Intent` 字段。已提交计划不能任意覆盖；现有修复路径也不允许改变这些字段，需要改变时走计划变更和重新确认，不能只递增 revision 来绕过。
- 计划校验通过不等于已授权执行。方案确认层核对内容指纹并消费确认，提交层核对执行轮次、项目归属、能力/实现及外部调用范围，输入解析层核对实际修订与哈希；资源准入由调度层完成。新增入口需接通这些校验，不能假定 `validate_plan` 一次完成所有检查。

## 4. 修改输入、输出和版本关系

- 输入只能是项目素材 `asset` 或已验收步骤的 `step + role`。服务端重新确认项目归属、父步骤成功、输出角色、文件存在性和 sha256；不能读取另一个节点目录里的“最新文件”。
- 下游步骤只能消费已验收 revision。父步骤 revision、输入哈希或 output role 变化时，必须使下游失效并重新执行/验收；不能静默继续使用旧绑定。
- 每次尝试使用独立输出目录和检查点。候选产物、已验收产物、已交付产物和被替代产物要保持不同状态，不能只靠文件名判断主结果。
- 输出通过结构化角色映射交付，服务端检查路径越界、重复文件、实际大小、哈希和媒体类型。文件存在或工具退出码为零不能直接标记步骤成功。
- 恢复时重新读取并校验输入和检查点；输入内容、revision 或类型与已保存绑定不符时拒绝继续，进入受控修订或重新规划，不能自动改绑并重排旧步骤。只有调度职责改变而任务授权仍有效时，才按调度规则释放旧尝试并重新领取。

## 5. 修改执行和状态转换

工作流状态、步骤状态、执行尝试和外部调用是不同对象，不能合并成一个状态字段。修改状态时同时检查：

- 领取事务检查资源预算、互斥与状态并创建租约；执行阶段及提交时继续验证该租约。等待资源或外部轮询间隔不能错误占用长期计算槽。
- 本地阶段或收取阶段返回后，由 Runner 保存 candidate 并进入技术验收；适配器需保证结果在允许下游或跨节点恢复前已有有效检查点，不能假定所有自定义 handler 都会自动保存。同步调用没有可靠任务 ID 时，提交结果未知必须暂停核实。
- 异步调用按 `submit → poll → collect → validate` 分阶段持久化供应商句柄；查询或下载重试不能变成重新生成。
- 取消会阻止未开始步骤并回收本地执行范围；外部任务是否真正停止要单独记录，不能把本地取消显示为上游已停止。
- 暂停只在安全边界生效，不能中断事务后留下可继续发布的半状态；继续时重新检查预算、租约、输入和未完成清理。
- 过期租约、重复事件和旧尝试不能覆盖新 revision 或发布成果。迟到外部回执只能按[供应商接入规则](change-external-providers.md)补充调用证据并安排清理，不能恢复业务。跨对象写入沿用现有事务和条件更新。

只有现有状态无法表达新语义时才新增状态。新增状态必须定义进入条件、允许的下一状态、等待原因、终态、恢复动作、取消动作、对外展示和数据库迁移，并补充竞争测试。涉及 operation 生命周期或阶段协议变化时，按 [operation 生命周期开发说明](change-operation-lifecycle.md)核对 Runner、调度、恢复和各业务入口的完整接线。

## 6. 修改验收、修复和用户决策

- 程序技术验收先于 Agent 或用户内容判断；技术失败、输入版本变化或证据不足时不能放行下游。
- Agent 审查使用冻结快照和结构化 `Decision`，只能读取输入/结果并准备受限修复，不能自行改变验收契约、实现、权限或提交外部调用。
- `pass` 必须对应 `conformant` 且有满足要求的证据；`inconclusive` 不能通过。视频或批量结果应记录实际覆盖范围。
- 自动修复只能修改明确允许的本地脚本参数或安全受控字段，修复会创建新 revision，并使受影响的下游步骤重新进入 `pending`。
- 用户决策区分 `quality`、`clarification`、`selection`、`external_call` 和 `plan_change`。质量批准不能代替外部调用授权；选择候选后要重新验收；计划变更应停止旧计划并重新确认。
- Agent `Decision` 绑定当前审查请求及冻结快照，在 reviewing/resuming 路径暂存并提交；用户 `Answer` 绑定 request ID 和步骤 revision，要求工作流 paused 且没有有效执行租约。不要把用户的暂停前置条件套到 Agent 审查；两条路径都要拒绝过期请求、错误归属和版本变化。

## 7. 同步 API、展示和文档

公开状态应保留步骤状态、等待原因、revision、候选/正式产物、验收结论、用户决策和错误阶段，供 API、Agent gateway 与页面一致消费。事件合并和产物呈现按[前端专项](change-ui-artifacts.md)处理，错误投影与诊断按[可观测性专项](change-observability-usage.md)处理。

若变更了公开字段、状态含义、迁移策略或用户操作，更新[工作流契约](../workflow-contracts.md)、[网页使用指南](../../WEB_UI.md)或对应 API 文档，并在交付说明中写明兼容边界。

## 8. 验证和完成标准

按变更范围运行定向测试，至少覆盖：

| 场景 | 必须证明 |
| --- | --- |
| 计划和契约 | 缺字段、未知字段、非法依赖、错误输入角色/类型、未登记 operation 被拒绝 |
| 版本和授权 | 旧确认、旧 revision、跨项目输入、哈希变化和未批准实现不能执行或发布 |
| 正常执行 | 依赖按序完成，输入绑定正确，输出角色和技术验收正确 |
| 失败与恢复 | 启动、运行、收取、上传、验收失败有界；重启/接管后不重复收费、不丢检查点 |
| 暂停、继续、取消 | 安全边界生效；本地资源回收；外部停止状态单独表达 |
| 审查与决策 | Agent 快照、用户 revision、候选选择、自动修复和计划变更不能绕过确认 |
| 并发与迟到事件 | 过期租约、重复回执、晚到结果和旧尝试不能覆盖新状态 |
| 展示与诊断 | API/SSE/UI 与持久化状态一致，等待原因和错误阶段可追踪且已脱敏 |

例如，跨契约、输入和审查路径的变更可通过[隔离测试入口](../development.md#常规回归)运行以下模块；局部变更按影响选取：

```bash
.venv/bin/python -m tests.run_distributed_suite tests.test_workflows tests.test_workflow_input_paths tests.test_quality_workflows tests.test_workflow_agent_review tests.test_user_decisions tests.test_workflow_reliability
```

发布前的全量检查及仅文档改动的验证方式统一按[开发与测试](../development.md)。涉及浏览器、真实模型或供应商时补适用验收，记录配置是否齐备、调用范围和未验证边界，不在报告中写凭据值。交付时列出受影响状态/字段、迁移、验证证据和恢复限制。
