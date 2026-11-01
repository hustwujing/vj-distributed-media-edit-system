# 分布式执行实施与验收报告

状态更新（2026-10-31）：已按追加授权部署单机双 API、双执行节点及共享 PostgreSQL/BOSS，部署验收见 [集群部署报告](cluster-deployment-report.md)。以下保留此前实现阶段的测试记录。

原实现阶段：实现与完整验收完成；当时未部署生产分布式服务。本次包含原分布式TODO、operation体系改造、测试补齐，以及按用户追加要求接入ai-tool的真实BOSS存储。

## 交付范围

- PostgreSQL共享状态，数据库时间、原子领取/续租/代次隔离、配额与资源预占；API与执行器可使用独立本地目录。
- S3兼容的不可变文件存储、显式文件引用、hash校验、按hash缓存、孤立上传回收；已选择真实可访问的BOSS配置。
- Agent脚本/配置/会话checkpoint与步骤输出checkpoint分离；跨节点接续、独立分支并行及实际汇合执行。
- 业务operation、带版本的implementation与local/remote_sync/remote_async生命周期分离；媒体参数模型、处理器注册和结构化输出。
- 一次性取消事务、统一本地监控、过期许可停止、独立外部清理租约及核实入口；晚到句柄/用量保存但不能恢复业务或发布成果。
- 节点能力白名单、排空与环境指纹匹配；共享事件关联step/revision/attempt/implementation，记录各阶段墙钟时间。

## 真实BOSS接入

参考 `/data/wujing03/ai-tool/common/storage/boss.py` 及其配置；`../ai-tool` 中的业务文档也确认BOSS使用S3协议。服务端复用BOSS的endpoint、bucket和现有授权凭据，使用独立video-use前缀。

实际测试确认：内网及HTTPS接口均支持上传、下载、删除，If-None-Match能阻止覆盖。匿名读取测试也返回200，因此文件和checkpoint不以明文保存：加入流式AES-256-GCM客户端认证加密，固定对象身份作为认证数据；密钥错误、篡改、截断、对象互换均拒绝。下载验证后才向工具提供明文，随后还验证原始文件hash。

真实BOSS测试完成加密文件往返、保存脚本/配置/会话checkpoint、删除节点A本地目录、节点B恢复并核对全部内容。创建的9个测试对象已删除，仅操作随机独立前缀，没有修改ai-tool已有文件。

私有配置在项目根目录 `.storage.env`，权限0600且gitignore。没有在报告、源码、工具输出或测试证据中写入凭据。实现阶段尚未加载生产服务；后续集群部署已加载该配置。参与同一分布式文件域的节点必须共享并备份加密密钥。

证据：[连接与条件写入探测](test-evidence/boss-connectivity.json)、[真实BOSS文件与checkpoint恢复](test-evidence/live-boss-storage.json)。

## 最终测试结果

**Python整套306项通过，0失败、0错误、0跳过，耗时247.26秒；前端12项通过，浏览器流程无页面错误。** 包含真实本地GPU/模型测试。源码SHA-256：`ddc0fd4cb13b15db959f0b96b2a063a57128c9f85d7346aecb3d6497a96d1b13`，整体测试期间源码未变化。真实BOSS与静态检查证据使用同一源码指纹。

原始169项检查：167项在所列验证范围内通过，2项不适用（未引入消息队列、无需旧数据迁移）。查看[可读检查表](distributed-checklist.md)。

## 测试与修复

测试范围包含：

| 范围 | 验证内容 | 主要证据 |
| --- | --- | --- |
| 数据库 | 多进程竞争、仅一个有效领取、接管后旧代次拒绝、事务强杀回滚、DB时间 | test_distributed_database.py |
| 文件 | 原目录删除后读取、上传未发布、取消后拒绝发布、版本冲突、跨项目拒绝、远端/缓存损坏、孤立对象GC、引用保护 | test_distributed_artifacts.py、test_object_encryption.py |
| 工作流 | 子进程强杀、另一节点恢复、分支并行、两节点本地目录删除后第三节点实际执行汇合 | test_distributed_workflow.py、test_distributed_runtime.py |
| API/网关 | 共享会话、任意实例下载、越权拒绝；真实低权限网关保存加密画布结果，第二节点读取；沙箱无存储凭据 | test_distributed_api.py、test_distributed_gateway.py |
| 取消 | 登记前后/句柄晚安装、真实阻塞HTTP及S3 SDK进程中止、200个调用批量查询、慢回调、DB断连许可到期 | test_distributed_cancellation.py |
| 控制竞态 | 取消请求幂等、完成后取消不回滚、晚到ID+Cookie、清理代次过期接管、accepted需再核实、实际成功及用量保留 | test_distributed_control.py |
| 调度 | 分支外部配额、离线节点本地清理不阻塞其他节点、能力白名单、排空、环境不匹配、事件/阶段指标 | test_distributed_races.py |
| 能力契约 | 新handler接入无Runner分支、替代实现与版本绑定、未知参数/action/版本拒绝、输出角色及类型校验 | test_operation_lifecycles.py、test_operation_contracts.py |
| 本地资源 | 真实Linux沙箱、进程/cgroup/UID、GPU模型、tmpfs与硬容量disk工作区、卸载清理 | test_execution_runtime.py及原回归 |

实际审查修复包括：取消缺少原子审计请求；Cookie与任务ID分开写导致晚到句柄可能丢失；清理缺少owner/generation/deadline；上游实际成功和用量需保留；无引用旧对象回收；低权限broker缺少新增依赖读取权限及受控存储配置；部分旧测试仍期待取消中或提交时创建沙箱。原有业务断言保留，新模型对应断言明确更新。

## 完成检查与配置文档

- [169条原始需求逐项追踪](distributed-checklist.json) / [可读检查表](distributed-checklist.md)
- [静态审查记录](distributed-static-review.md)
- [分布式配置、运行和维护](distributed-deployment.md)
- [新operation接入规范](operation-onboarding.md)
- [整体测试结果](test-evidence/distributed-suite.json)
- [静态与凭据检查](test-evidence/distributed-static.json)

旧数据迁移和旧接口兼容按既有授权明确排除；未引入消息队列，其重复通知协议不适用。无消息通道的取消和轮询是已测主路径。`media.extract_audio`只是接入模板示意，未新增为生产能力。

## 实际边界

- 测试用真实独立PostgreSQL、HTTP S3协议测试服务、真实BOSS、独立节点根目录、独立进程及真实Linux沙箱。节点在同一物理机器上；没有将其描述为已完成跨物理机部署、生产网络分区演练或容量压测。
- PostgreSQL短写事务使用全局控制面advisory lock，保证跨表一致性，未声称无限并发吞吐。读监控不等该锁。
- 不支持幂等/查回的外部服务在提交未知时暂停；断开HTTP不保证上游停止或退款。未知费用保持null。unsupported/unconfirmed可用管理reconcile只查询已知ID，不重新生成。
- 外部生成服务用确定性模拟协议覆盖竞态，不重复付费生成。此前真实生成能力证据保留，本次重点为分布式执行语义和真实文件/checkpoint存储。
- 原实现阶段未重启生产服务；后续已按追加部署授权切换为单机多实例集群，详见集群部署报告。

原实现阶段的生产服务只读复核记录（不代表当前部署状态），见 [生产状态核对](test-evidence/distributed-production-unchanged.json)。
