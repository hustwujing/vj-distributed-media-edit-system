# Agent 提示词与上下文优化

本轮将提示组织为公共规则、阶段职责、服务端状态、关联证据、所需工具契约和当前用户要求。它不改变调度类型、授权模式、业务验收条件或调用账本口径；程序门禁继续是执行和交付的依据。

## 实现与检查项

| 项目 | 实现 | 必须保持的行为 |
| --- | --- | --- |
| 阶段职责 | `agent_context.INSTRUCTIONS` + `role_instructions()` | plan/execute/review/diagnose/final 分开；自动审查不重新请求原方案授权；工作流交付使用 decision |
| 关联上下文 | `agent_prompt_context.lineage/feedback` | 只沿本项目 plan_id 和 parent_task_id 追溯；不带入无关任务反馈；展示原修订、当前修订和是否随后成功 |
| 原生恢复与交接 | `agent_host`、`harness_context`、`checkpoints.profile` | 原生恢复不反复注入最近六轮摘要；新会话提供索引与覆盖信息；提示契约变化使旧原生状态转公开交接 |
| 验收与诊断 | `workflow_review.context` | 单步只展开目标及其祖先；最终验收保留全部步骤；按ID查看详情不扩大修复权限；日志截断有标记、-9不冒充OOM |
| 工具契约 | `operations.catalog(names)`、`workflow.py capabilities --operation ID` | schema来自实际注册实现，最多8个ID；未知ID拒绝；脚本输入仍是角色到路径数组 |
| 预算 | `context.py budget --task ID` | 返回冻结配置与已用额度/剩余时间，只读不重置；只允许当前项目任务 |
| 用户批注 | `context.py project_data --kind ... --offset ...` | 只读分页 notes/chapters/branches/brand_templates，精简提示仍能取回原约束，不能跨项目 |
| 生成提示 | `generation_prompts`、`image_document.edit_prompt` | 明确主体/动作/构图/参考职责/保留项；保留原始用户描述，不自动加审美要求；尺寸时长仍使用参数和实际验收 |
| 标准来源 | 规划角色与公开参考 | 在方案中区分用户硬要求、披露的技术条件、可选建议；既有已确认 acceptance 不降级、不偷偷改字段 |
| 可观测性 | `prompt_context` 事件与诊断 `prompt_contexts` | 记录角色、字符数、恢复方式、关联/反馈/契约数量，不保存完整提示或私有推理 |

`prompt_context.characters` 只计宿主本轮组装的消息，不包含SDK原生历史、内置工具定义或后续工具结果；实际Token仍以模型回执账本为准。

能力发现的 CLI 和沙盒 capabilities.json 默认只列操作索引；具体参数通过 --operation 查询，--full 显式获取完整目录。服务端原有完整目录接口保持可用。优先使用接口参考及 capabilities/validate，未说明行为或具体故障才读取相关源码；多帧检查可使用带时间码的联系表，再放大疑点，不能遗漏关键事件。

公共规则保持稳定，阶段说明随本轮实际职责附加。能力 schema 按已确认意图提供，不超过8个；较多能力可逐项查询。规划预算仍由服务端强制限制，提示中的剩余量只是当时快照。

## 只读工具用法

```bash
python /opt/workflow.py capabilities  # 默认精简索引
python /opt/workflow.py capabilities --operation media.edit
python /opt/workflow.py capabilities --full  # 显式检查所有 schema
python /opt/workflow.py status --step target --step upstream
python /opt/context.py budget --task TASK_ID
python /opt/context.py feedback --task TASK_ID --offset 0
python /opt/context.py project_data --kind notes --offset 0
```

`project_data` 或 `feedback` 返回 `next_offset` 时继续分页；`feedback` 返回明确任务关联下的完整质量反馈原文，摘要截断不删除原记录。文件元信息、旧工具输出、历史反馈都是数据，不授予执行权限。没有查看的内容保持未知；查询更多详情不意味着可以修复其他分支。`preflight` 的参数是引用脚本的完整计划 JSON，不是单个 Python 文件；覆盖范围仍是语法、导入与JSON，不是滤镜/媒体业务逻辑测试。沙盒命令以已提供能力为准，哈希和文件比较可用 Python 标准库。

自由结构的参数契约补充 usage_guidance。media.edit 明确 clips.asset、输入角色、transcript/transcript_ROLE 绑定以及 helper 的不同格式，参考 [固定媒体调用说明](../references/fixed-media.md)。规划提示区分中间与最终格式、Alpha 通道与透明像素；既有确认条件仍不能在执行中降级。

## 同轮修复

- Agent 设置在 HTTP 页面上可能没有 `crypto.randomUUID()`。请求键优先用原生 UUID，否则使用 `crypto.getRandomValues()` 生成标准 UUID v4；不降级为 Math.random。保持原有 request_key 防重与配置 digest 校验。
- `file(1)` 二进制及 libmagic 存在，但沙盒未挂载公开 magic 数据库，造成媒体检查命令失败。将该数据文件作为只读运行依赖并纳入指纹，真实降权沙盒内检查 PNG MIME；不挂载整个宿主目录。

- 修正能力索引的挂载顺序：先挂载工作区，再覆盖只读 capabilities.json，避免父挂载隐藏真实内容。真实降权沙盒测试必须解析索引并验证不可写，不能仅检查文件存在。

## 验证方法

定向测试覆盖关联范围、跨项目拒绝、预算不重置、分阶段职责、验收详情/修复权限分离、最终逐步证据、截断、工具 schema、用户描述保留和安全诊断。浏览器覆盖 randomUUID 缺失时应用/撤销/激活、刷新和输入保留。

静态比较使用同一24步工作流，比较优化前后的单步验收证据字节数；这不是 Token 或性能测试。真实任务另记录实际模型/工具次数、Token与缓存子集、持续时间、结果、重试和页面表现。一次样本不能当作稳定的性能收益或分类准确率保证。

发布前全量测试、部署后的真实验证及未验证边界记录到本轮报告。历史完整77条场景的通过不代替本轮验收，局部通过也不能冒称重跑了77条。
