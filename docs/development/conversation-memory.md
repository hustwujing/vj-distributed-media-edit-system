# 会话记忆与重试路由

本规范适用于 Session 连续聊天、Manager 消息路由、历史摘要、失败解释和聊天内重试。相关实现集中在 `webapp/conversation_memory.py`、`workflow_messages.py`、`workflow_conversation.py`、`workflow_graph.py` 和 `workflow_orchestrator.py`。

## 权威数据与上下文顺序

`session_messages` 是追加式原始消息账本。Run 状态变化、摘要提交、重新规划和项目恢复均不得删除或改写其记录。`conversation_summaries` 是可重建的派生索引，`conversation_summary_jobs` 记录后台压缩进度。

Manager 与 Planner 使用同一份会话快照，优先级固定为：

1. 当前服务端 Run 状态、Authorization、交付契约和待处理决策；
2. 当前用户消息；
3. 已提交摘要游标后的原始消息；
4. 游标前的历史摘要。

低优先级历史只能提供上下文，不能授予素材、Provider、operation、预算、交付范围或恢复权限，也不能覆盖当前服务端契约。

## Token 阈值与后台压缩

系统使用不依赖在线 tokenizer 的保守 token 预估器。已提交摘要与游标后消息累计达到 `VIDEO_USE_CONVERSATION_COMPACT_TOKENS` 时登记后台压缩，默认 `12000`。新游标之后保留不超过 `VIDEO_USE_CONVERSATION_TAIL_TOKENS` 的原文，默认 `4000`。

消息写入路径只登记任务，不同步等待摘要。Orchestrator 在后续 reconcile 中处理压缩：

- 任务冻结目标 `(created_at, message_id)` 和目标前原始消息 digest；
- 压缩结果保存结构化当前事实和一条有界历史摘要消息；
- 只有源 digest 仍匹配且摘要落库成功，才以 CAS 推进游标；
- 新消息可以在目标游标之后继续写入，不会使已冻结前缀失效；
- 失败记录错误并退避重试，保留旧摘要、旧游标和全部原始消息；
- 项目删除时，按 Session 顺序清理摘要任务、摘要、原始消息和 Session。

不得通过删除原文、先推进游标后生成摘要、按消息条数代替 token 阈值，或在摘要失败时静默截断上下文来降低输入大小。

## 服务端受控重试

用户语言不直接创建恢复动作。服务端仅在当前 Run 为安全暂停态、没有待决策、活动租约或未完成 cleanup，并且预算和规划轮次仍可用时，生成绑定以下字段的 opaque retry option：

- Run ID；
- Graph Version；
- Authorization Version；
- 当前 terminal reason；
- 失败 Control Job 和 revision。

只有存在服务端选项时，Message Manager 才能看到 `retry_current_failure`。模型只能选择给定 ID；应用阶段再次按当前状态计算选项，过期或不再安全的 ID 必须拒绝。通过后写入正常、持久化、可审计的 `resume` command，不直接修改 Planner 或 Run 状态。

错误必须显示真实分类和已脱敏详情。可重试错误显示明确重试动作；需要修订时说明需要修改的契约或参数；确实缺输入时列出缺少字段和示例。不得把模型传输、Runtime、预算或程序错误统一写成“请补充信息”。

## 验证

定向 Python 测试必须使用隔离入口：

```bash
VIDEO_USE_TEST_TMP_ROOT=/data2/wujing03/vu-tmp TMPDIR=/data2/wujing03/t \
VIDEO_USE_TEST_SUITE_LOCK=/data2/wujing03/video-use-test-suite-conversation.lock \
XDG_CACHE_HOME=/data2/wujing03/vu-tmp/cache PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m tests.run_distributed_suite \
  tests.test_conversation_memory tests.test_workflow_messages tests.test_workflow_conversation
```

至少覆盖 token 触发、后台任务、成功后游标推进、digest 不匹配不推进、原始消息保留、摘要加游标后消息输入、无服务端选项时工具不可见、选项过期拒绝，以及规划传输失败重试后真实创建新 Planner。发布前由 `sandbox_cluster.sh deploy` 运行完整门禁；真实模型或故障注入只在需求涉及其行为时另跑真机专项。
