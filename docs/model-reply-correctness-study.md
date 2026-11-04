# 模型回复正确性：现状与改进建议

2026-11-04。用户已批准按本流程实施，另要求对比Claude和DS4.1，并在网关不支持时回退非strict。实现与模型评测进行中；119条媒体真机仍暂停，协议改善验证完成后再衔接原修复及完整回归目标。

## 停止状态

本轮deedda4df9cb共119条：启动8、成功并审查2（D39/D41）、程序失败2（D38应用缺陷、D36测试断言）、用户主动中止4（D24/D31/D33/D40）、未执行111。4条在途测试经正常取消接口结束，数据库活动租约为0，BOSS/Redis/本地清理完成；D33首次Run已经成功，successor在用户要求停测时被取消。主动中止不计模型失败或通过。原状态、诊断、planner请求和取消命令均已保存。

## 已核实事实

- `webapp/workflow_agents.py::model`把Schema包含在文本上下文，再调用普通模型输出；当前没有设置Responses的`text.format`严格JSON Schema，也没有将Agent动作声明为原生strict工具参数。
- 模型必须输出`action=proposal → proposal=ManagerResult → proposal=Proposal`，嵌套两层同名proposal；实际发生直接返回内层Proposal的错误。
- Task有22个字段。`revise_tasks`要求重新提交Task，未改字段仍须精确保持。ManagerResult schema序列化约17.6KB，Manager系统指令约1.99万字符；这些是尺寸指标，不是因果证明。
- `sealed=true`与非空`replan_after`互斥，但Schema字段分别声明，主提示没有直接说明互斥。修复冻结字段错误仅列字段族，未指出实际变更路径。
- 普通修复、用户反馈修订、新增Task、替代Task、补证据及再次审查的合法操作取决于冻结上下文；当前大量规则分散在提示与服务端校验。
- 12:02左右抽样有9次Manager回复拒绝：封口冲突2、非法JSON2、冻结字段变更2、外层结构错误1、输出角色引用错误1、无有效触发的review_again1。另1次Diagnosis error_code格式错误。这是当时快照，不是完整轮次最终错误总量。
- JSON解析错误发生在文本末尾；响应适配器已要求Provider返回completed及单一final message，因此不能直接认定网络截断。日志还需补解析错误类别、Provider结束原因和输出用量等证据，不能编造原始回复内容。
- D38/F68为合法提案应用时KeyError，不是模型错误；D36/F70为测试拒绝合法replacement，也不是模型错误。两项仍待修复复测。

## 推荐顺序与实现位置

### 1. 收紧模型输出接口

让模型面对按角色和阶段限定的动作。优先评估原生strict工具调用，分别承载inspect、contract、submit_plan、submit_review等；若保留当前JSON action协议，则对整个动作外层启用严格结构化输出，不能只约束内部Proposal。

位置：`workflow_agents.py`、`model_adapters.py`和对应harness契约。严格输出能力必须通过实际内部网关验证，不能因API兼容或模型名称推断已支持。当前Pydantic模型含动态字典、默认值及关系约束，不能未经适配直接把完整schema设置strict；需生成受支持的传输Schema并保留服务端全部业务校验。遇到拒绝、不完整响应、网关不支持时明确记录，不静默宣称仍是strict。

严格结构化输出解决语法、字段、枚举及部分结构约束；它不能保证目标理解、授权范围、依赖关系与证据内容正确。

### 2. 减少模型必须重复填写的字段

增加专门的模型传输协议，由程序编译成现有权威领域契约：

- 修复提交明确类型的变更，例如改Script argv，保留其他冻结字段；不要让模型重写整个Task再逐字段比对。
- 用户反馈修改使用其冻结允许范围专用结构；更改类型/能力/角色等仍须合法replacement或授权路径，不能当作普通patch放行。
- 文件声明只保留一个权威来源，程序产生一致的outputs/output_roles结构，避免两处相互矛盾。
- 用互斥分支表达继续扩图或完成计划，程序派生sealed与replan_after，避免两个独立开关形成非法组合。
- 请求摘要、版本、输入批次等由受信任host绑定到发起调用时的不可变请求。不能补成“当前最新版本”；提交仍校验原snapshot、授权和lease/generation，过期结果必须拒绝。

位置：`workflow_graph.py`（模型传输类型与领域契约的边界）、`workflow_planning.py`（编译/提交）、`workflow_agents.py`（请求绑定）。这不是旧字段兼容层，不增加并行权威状态。

### 3. 提供当前可操作范围与精确错误

从同一套程序策略生成当前允许的动作、可修订Task、可变字段、可替代对象、合法证据ID和再次审查资格；模型不必从长篇规则自行推算。但这些视图不授予新权限，提交时仍重新验证。

反馈采用结构化错误码、字段路径和允许操作，例如`sealed`与`replan_after`冲突、`revise_tasks[0].output_roles`不可修改。敏感值不复制到公共日志；必要原值通过有界只读契约提供。不能只说“计划不合法，请重试”。重复相同错误应识别并有界结束，保留真实失败而非增加无限重试。

位置：`workflow_graph.py::validate_proposal`、`workflow_planning.py::snapshot`、`workflow_agents.py::proposal_feedback`、`specialist_profiles.py`与`references/dynamic-operations.md`。说明、Schema与校验必须同源核对；增加约束可见性回归。

### 4. 聚焦上下文和角色职责

保留当前冻结请求、有关Task和必要历史，其他能力按需查询。纠错保留当前候选和最新精确反馈，压缩重复错误记录，但不可抹去影响授权/目标的用户输入。初次规划、反馈修订、失败恢复、补证据使用同一Manager的分模式协议；先不新增Agent角色，以免增加交接。

不能直接删去现有输入、历史或证据来降低Token；每项裁剪需证明相关决策可恢复且版本绑定一致。长指令本身不是已证实根因，应在对照试验中测量。

### 5. 用小型模型协议评测选择方案

从真实失败冻结快照和正常样本构建约30个通用场景，各重复3–5次；划分开发与保留集。先固定同一模型和配置，对比现状、strict、精简传输协议、精确反馈，再组合验证；不同时更换模型、提示与恢复逻辑后无法归因。

评测覆盖首次规划、sealed互斥、普通repair、用户feedback、replacement、证据补充、失效snapshot、禁止扩权及合法拒绝。只收集模型提案并在隔离状态校验，不执行媒体、Wan或付费业务Operation，也不修改真实Run。

统计：首次JSON/Schema合格率、首次业务契约合格率、每次合法提案平均模型调用、同错误复发率、有界纠正收敛率、p50/p95耗时与Token、非法提案落地数（必须0）。明确分母是submit_plan回复还是全部inspect/contract调用；不能把工具查询计入规划成功率。多个合法计划均可通过，不和单一标准答案逐字匹配。

改善协议正确率后，再恢复失败case复测及完整119条验证。模型对照作为后续独立实验，当前证据不足以认定模型能力退化。

## 旧版比较边界

main的`docs/agent-prompt-optimization-report.md`记录C26曾有8次工具失败（含2次计划契约错误）后成功，以及字幕输入/参数连续3次拒绝后停止。旧`completion-report.md`中37/60有修复或重新规划，但混合执行、质量和程序问题；不能当作旧版规划回复错误率。新旧对照应以同类请求及相同分母衡量。

## 官方依据与待验证项

- [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)：严格Schema输出/工具参数、Schema子集限制以及结构正确仍可能内容错误。
- [Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)：明确目标、真实数据集、指标、对照与持续评测。

官方API能力不证明本机内部代理网关已透传支持。实际网关能力探测和上述对照评测尚未执行；本文件不宣称正确率已经提高。

## 当前实施与评测状态

- 已实现structured-profile-v3、原生工具参数、明确回退及本地强校验；Task局部变更、自动替代目标继承、文件单点声明、互斥规划方式、按类型拆分验收、按已查询Operation契约限定输入/参数。
- 宿主仍绑定原snapshot、图/授权版本和input sequence，stage/apply继续复核lease/generation；不会把旧模型结果绑定到新版本。
- 修复F68新增下游revision传播及F70反馈replacement断言。87项当前定向回归通过；此前两个冻结候选已进行全量检查，最终候选705dcb9a9211的完整门禁正在执行。尚未部署新harness。
- 模型为gpt-5.6、claude-4.6-sonnet、deepseek-v4.1-flash，均记录协议和high配置；不将不同厂商high当作等价计算量。
- 第一轮6场景试运行缺少Operation完整契约，结果保留但不用于正式模型比较。第二轮补齐同等契约，暴露preflight语义和strict未执行；这些反馈已进入最终候选。
- 正式样本30个，开发12/保留18；已暴露的试运行请求全部进入开发集。主比较540组（30×3模型×2接口×3重复），另开发样本324组拆分实验。每组最多两次模型纠正，strict参数拒绝后的机械回退不占模型纠正机会。
- 这是终局提案合法性评测，提供相同完整相关能力契约；不模拟工具发现、媒体执行或发布事务。缺失的持久化计数规范化为0，不能据此声称预算/竞争边界已完成真机验证。
- Claude路由出现过strict=true仍返回额外字段，故只记录strict_requested及实际合规，不宣称网关保证严格输出。其部分用量回执output_tokens=0但有实际输出，标记可疑，不能据此宣称输出免费或作完整Token成本比较。

正式冻结输入/结果位于`.cluster/model-evals/reply-final-20261104/`；试运行分别保存在reply-v1和reply-v3，未覆盖旧结果。
