# 新 operation 接入规范

开发任务入口见 [新增与扩展 operation](development/add-operation.md)，注册代码见 [接入指南](operation-extension-guide.md)。本文保留执行、恢复与可观测性的约束。

声明执行意图时，服务端把各operation的实现固定在`implementations`映射中；选择替代实现时需在该映射显式声明。工作流节点未指定`implementation`时继承已批准意图，指定不同实现会被拒绝。直接工具入口也必须与批准实现一致。替代实现沿用原能力的参数契约和业务校验，初始实现版本不强制为`@1`。

业务名、具体实现及生命周期分离。普通新增能力无需修改调度器或Runner；普通脚本仍使用script节点，不能以注册能力为由开放任意命令或HTTP代理。

## 注册边界

注册示例统一维护在[接入指南的定义与注册](operation-extension-guide.md#定义与注册)，不在本文复制模板。

实现模块由受信任服务启动代码加载；不能由Agent临时注册。注册会拒绝重名、未知实现、无参数模型和重复输出角色。能力目录和参数校验读取同一模型。`tests/test_operation_lifecycles.py`的专用测试能力验证新增handler不修改Runner。

同一业务可通过 `register_implementation(operation_id, 'implementation@version', ...)`登记其他实现，仅能调整资源/供应商/环境描述，不修改业务输入输出。任务持久化已选实现，未知版本或其他能力的实现不能被替换进来。替换实现仍须满足已确认语义、调用范围及配额，不是无授权的自动降级。

## 生命周期

新增类型或修改阶段协议必须按 [operation 生命周期开发说明](development/change-operation-lifecycle.md)完成状态转换、持久化、Runner、调度资源、恢复、取消和兼容验证。`Protocol` 显式声明阶段、状态映射及 `RecoveryAction`，未知协议拒绝执行；Runner 调用 `Implementation.recover()` 应用恢复决策，能力目录复用同一恢复映射。新增阶段仍须接入执行分支和调度，取消描述不会自动实现取消。

- local：执行固定本地处理，可安全重执行必须显式声明，输入固定、输出独立。
- remote_sync：invoke请求返回产物，没有上游任务ID就不能假称可查询恢复。
- remote_async：submit/poll/collect独立阶段，每次调用完成返回，不能在handler内部无限轮询。

同步实现默认 `records_calls=False`，缺少调用记录时不自动重试未知提交。声明 `records_calls=True` 必须保证每次外部副作用前保存调用意图、产物持久化后保存结果列表；Runner 才能据此判断未提交或恢复已有结果。成功但产物未保存时暂停核实，不能重复生成。该契约需要实际实现和崩溃窗口测试，不能只设置标志。

服务端Runner负责依赖、租约、超时、取消、验收、检查点和发布。供应商仅处理协议，cancel 不支持时如实表达；已有任务的 reconcile 由独立清理核实入口处理，不是通用 provider 注册要求。稳定请求键不等于供应商幂等保证。成功返回只表示执行结束，还需技术和业务验收，才能放行下游。

输入用asset或step+role引用，解析后验证项目、修订和hash；需要字幕/转写时显式列入inputs，不能猜测另一个节点目录中的最新文件。输出Result包含version、outputs(role/path)，ProducedFiles可附角色映射；实际hash/大小/媒体类型由服务端计算，越界、重名和错误媒体类型不能发布。

使用框架的连接和对象传输包装器。`process_call`在启动前登记，句柄安装后执行sticky取消；finally注销。HTTP使用受控session，S3 SDK使用可终止专用进程。普通handler不自行创建数据库轮询线程，不把无界停止动作放进观察线程。

## 必须验证

验收矩阵统一见[新增与扩展 operation](development/add-operation.md#7-验证与完成标准)；外部实现追加[供应商协议验证](development/change-external-providers.md#6-验证与完成标准)。

阶段错误需保留phase、类型、外部状态是否已知和恢复语义；诊断不包含凭据或签名下载地址。参数/实现/输入版本与attempt一同关联，未知费用保持null。文件上传完成不等于成果生效，最终发布必须走框架的租约校验事务。

## 可观测性接入门槛

新增 operation 通过阶段包装器、受控 HTTP、任务上下文和状态审计接入观测。具体字段、脱敏、指标和用量规则统一见[可观测性与用量开发](development/change-observability-usage.md)；新增处理器仍需补充适用的失败、取消、恢复及输出契约证据，不能以历史专项报告代替本次验证。
