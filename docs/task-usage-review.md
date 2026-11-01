# 任务消耗实现静态审查

## 已审查路径

1. 配额在原位置执行；账本只记录证据，不赋予执行/恢复权限。主模型开始记录失败时不发送请求；结束回执失败时记录 usage.receipt_failed，保留原调用为未知。
2. run_id 来自服务端建账，execute 使用已确认 plan_id，显式继续须通过同项目校验；读取还需项目所有权。查询使用参数绑定，详情不返回 prompt、tool command、响应内容、凭据、签名 URL。
3. PostgreSQL 汇总使用独立只读 repeatable-read 快照；SQLite 使用同一读事务。全量统计与分页明细分离，输入/输出的缓存与推理子集不重复相加。
4. 结束回执有固定 call_id 和 task_id，冲突回执保留既有数字并标记 conflict；部分回执仅补充空字段。进程退出但未终结调用由 attempt 状态呈现 unknown。
5. 工具按 task/attempt/item 去重；workflow 仅在 pending→真正启动时记账，轮询不记步骤重试；按 step/revision 统计额外执行，repair_count 独立展示。
6. 上游生成用量/费用仍来自原有 provider 账本，提交/查询/收取尝试单列。提供者提交意图与实际进入提交阶段的尝试不混为“成功生成次数”。费用没有明确数值和币种时为未知。
7. Redis 发布仍保留在 agent_store.event 之后；持久化工具事件与 usage 写入在同一事务。Redis 失效仍可查询 PostgreSQL/SQLite 账本；真实 Redis 双端订阅与失效回退有测试。
8. 控制分类器的真实模型请求也纳入账本；固定指令不调用模型、不虚增次数。CPU/GPU 时间未采集，阶段耗时标注为墙钟时长。

## 审查/测试中发现并修复

- script 的 operation 字段为 null：使用明确 script 默认值，恢复脚本节点正常执行。
- 已建立普通 PostgreSQL 事务后不能切换 repeatable-read：改为独立只读连接，在首条查询之前设置隔离级别。
- 汇总与明细对中断调用状态不一致：统一按执行 attempt 的终态呈现 unknown。
- 部分用量回执后续补全：保留已知字段，填入缺失字段，互相矛盾时标记冲突而不重复相加。
- 真实 PostgreSQL 的 BIGINT 求和返回 Decimal，诊断 JSONResponse 无法序列化：转换为普通整数，增加 PostgreSQL → 诊断 JSONResponse 的回归断言。真实旧版本已复现 HTTP 500，最终修复版已验证 HTTP 200 和 CLI 导出成功。
- 完整工作区新增 Redis 依赖但锁文件未更新：补齐 uv.lock，并验证依赖兼容。

## 明确边界

- 调用数表示进入受控调用路径的尝试，不等于提供者收费成功数；网络提交结果不确定时保留 unknown。
- 历史主模型 Tokens 无法补造；历史工具事件与原 quota/provider 证据单列。
- SDK 内部重试若没有可靠关联，不能从文本相似度猜测；当前可精确显示工作流同 revision 的重试与修复次数。
- 未收到的主模型用量不会用 tokenizer 估算冒充上游回执；费用、CPU/GPU 实测未接入处明确未知。
