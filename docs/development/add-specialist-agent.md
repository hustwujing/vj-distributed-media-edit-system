# 新增专业 Agent Profile

适用于添加规划、诊断、执行或验收类专业角色。Manager 的业务 objective 是数据，不能成为新系统提示、
任意 Skill、工具或权限的注册入口。

## 实现入口

`specialist_profiles.py`、`workflow_agent_contracts.py`、`workflow_graph.Task`、
`workflow_agents.py`、`workflow_gateway.py`、`workflow_orchestrator.py`。

## 开发步骤

1. 定义 Profile ID、版本、用途、固定提示模板、AgentRole、action_capabilities、harness、model policy、
   检查/纠正/finalize轮数、时间和输出 Token 上限，以及输出 Schema 和产物策略。
2. 通过服务端 `register()` 登记，验证工具与权限一致。只读角色不允许 script；提案权只属于 Manager。
3. 新输出 Schema 必须同时实现模型提示、解析、服务端验证和错误处理，不能只新增一个 Schema 名称。
   Control Job Reviewer 使用冻结 ReviewDecision；专业视觉报告与诊断文件分别使用对应的结构化契约。
4. 冻结 Profile 快照和 harness 指纹，绑定授权包络中的 Profile 摘要。用户没有授权的角色必须先走 plan_change。
5. 明确允许读取哪些素材、已验收角色、候选或失败证据；素材与历史都不能扩大模型权限。
6. 仅在 Task 输出目录生成候选。需要模型判断的通过结论必须关联实际检查记录，不能用模型自述替代证据。
7. `skills` 必须指向服务端 `SKILL_FILES` 中登记的技术文档；内容经实际 `model_instructions()` 注入，摘要写入 Profile 快照。未知技能、越界路径和超过32KiB的单文档被拒绝；不能只登记名字而不接线。
8. 保留真实模型、工具、Token、调用次数及错误阶段。未知用量保留预留并标识未知，不能填写零。

## 内部控制角色

Manager、Final Reviewer 和失败诊断由 Orchestrator 创建。它们在同一资源/租约体系中运行，
但不计为业务 DAG 的可由 Manager 任意增删的控制节点。内部结果协议由控制请求固定；
Manager 不能将业务 Agent 伪装成控制角色，质量批准也不能授权新的外部调用。

## 验证与完成标准

单元测试覆盖注册拒绝、参数及输出校验、权限/工具一致、策略变更与旧快照、预算耗尽和越权输入。
集成测试验证真实沙盒最小输入、结果哈希、取消后无提交及恢复时不重放副作用。
真机 case 至少包含一个正常结果和一个模型错误/证据不足场景，并检查浏览器、日志、数据库、缓存、BOSS 与清理。

对重复模型失败还需检查工程侧诱因：模型实际收到的技能说明是否明确字段类型、默认值和输出语义，验收条款是否对应用户目标，检查工具与最终输出协议是否在预算内可完成。例如静音与无音轨是不同条件，不能用`audio=false`代替静音检测；布尔开关不能只列字段名而不说明类型。归为“程序处理正确”仍须记录业务失败与可改进点，不能据此免除接口、提示和验收设计审查。

角色上线前同步能力说明、API 返回、提示模板、Developer 索引及 case 审查矩阵；不能用少量关键词测试替代真实产物验收。

检查图像的预算使用冻结的 `max_images`，与 `max_turns`、Token和时间上限共同生效。超限不得标记为已检查；返回有界反馈，允许使用既有证据完成结论或报告无法确定。新增Profile必须在真实目录与执行器中使用同一预算值。

`inspect.refs` 可一次检查最多8个冻结引用，整批计一个检查轮、每项独立保存证据。进入 finalize 阶段后禁止继续检查；格式错误使用独立纠正预算。相同不可变引用和 offset 不得通过重复审查随机获得通过结论。

最终验收的`ReviewDecision.primary`以完整不可变引用指定主交付，必须属于已检查的`deliverables`；多文件通过时必填。中间步骤验收不得声明最终主交付。主成果由用户目标和验收决定，不由列表首项或`final.mp4`等文件名猜测；解析、worker验证、控制器验证和发布阶段须一致。

inspect须按格式分流：ZIP只读取有界目录元数据，不解包或解码成员，明确contents_verified=false；它不能进入媒体探测。未知格式及超限目录返回有界输入反馈，不记录伪造检查证据。媒体解码故障保留异常，诊断同时存储引用SHA256以便与不可变候选关联，不记录原始私有路径。

当前结构化引擎为 structured-profile-v2。业务 JSON 报告提交 `{"action":"result","report":{...}}`，服务端验证并序列化到唯一声明 JSON 文件；不要把 JSON 再编码为字符串。Manager、Reviewer、Diagnosis 的控制结果使用各自固定 envelope，不能复用业务 files 报告格式。提示词按角色给出唯一对应协议。

WorkspacePolicy 独立于角色与能力，必须声明只读工作区、Task 输出、Review scratch 或完整编辑范围；不得从旧 plan/execute 文本推断权限。Control Job 与业务 Task 分开记录真实 harness、profile、模型、phase、尝试和 Token。审查检查/格式纠正/最终提交预算耗尽时标记 review_incomplete 并交回 Manager 补证据，不能自动重生成候选或无界重复 Review。
