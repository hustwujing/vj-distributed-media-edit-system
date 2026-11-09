# 工作流错误透出规范

错误不能从底层异常直接退化成“补充信息”或一个通用失败。Executor、Orchestrator、API 和前端统一使用 `webapp/workflow_error_catalog.py` 中的稳定错误码；新增错误必须同时声明来源类别、用户标题、无详情时的兜底原因、重试策略、用户动作和确定性恢复动作。

## 分类

| 类别 | 典型错误 | 系统行为 |
| --- | --- | --- |
| 超时 | `agent_timeout` | 到墙钟上限立即终止，不重放同 revision，清理沙箱，建议拆小或修订任务 |
| 配额 | `model_token_budget_exhausted`、`agent_turn_budget_exhausted`、`agent_finalize_budget_exhausted`、`budget_exhausted`、`daily_model_quota_exhausted` | 发起下一请求前拒绝；说明具体是哪类额度、已用/上限以及恢复方式 |
| 沙箱基础设施 | `agent_runtime_unavailable`、`runtime_profile_changed`、`policy_changed` | 明确不是业务输入问题；修复 Runtime 或创建新 revision |
| Agent/脚本 | `agent_failed`、`execution_failed` | 展示脱敏后的工具/运行时错误、异常类型和分类；仅安全瞬时错误可自动重试 |
| 外部 Provider | `pre_submit`、`provider_failed`、`poll_failed`、`collect_failed`、`submission_unknown` | 区分尚未提交、已失败、查询/收取失败和提交结果未知；禁止因错误而重复收费请求 |
| 合同 | `planning_contract_exhausted`、`review_contract_exhausted`、`invalid_result_manifest` | 展示精确字段/校验错误，不伪装成缺少用户确认或素材 |
| 验收 | `technical_validation_failed`、`quality_failed`、`review_incomplete`、`model_result` | 保留候选和证据，说明未发布原因以及需要修订的范围 |

## API 契约

每条 `failure_details` 除 `code/message/error_type/classification` 外，还必须返回：

- `failure_kind`：稳定来源类别；
- `failure_title`：面向用户的准确标题；
- `retry_policy`：是否可同 revision、同外部句柄或仅在修复后重试；
- `user_action`：等待、重试、重新规划、修复 Runtime 或核实外部状态；
- `known_error_code`：未知错误必须为 `false`，页面显示真实详情和“未登记错误”，不得回退成“请补充信息”。

底层 `error_detail` 必须先经过凭据与隐私脱敏，再作为首要原因透出。目录文案只在没有详情时兜底，或附加“不重放、不重复收费、需修复 Runtime”等恢复约束。Cleanup 是独立状态：失败原因和沙箱是否已销毁必须分别展示，不能用任务失败冒充资源已回收。
