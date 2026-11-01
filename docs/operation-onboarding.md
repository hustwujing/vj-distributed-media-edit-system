# 新 operation 接入规范

声明执行意图时，服务端把各operation的实现固定在`implementations`映射中；选择替代实现时需在该映射显式声明。工作流节点未指定`implementation`时继承已批准意图，指定不同实现会被拒绝。直接工具入口也必须与批准实现一致。替代实现沿用原能力的参数契约和业务校验，初始实现版本不强制为`@1`。

业务名、具体实现及生命周期分离。普通新增能力无需修改调度器或Runner；普通脚本仍使用script节点，不能以注册能力为由开放任意命令或HTTP代理。

## 本地能力模板

以下是只在测试中登记的能力示例；不是新增生产功能：

```python
from pydantic import BaseModel, ConfigDict, Field
from webapp.operations import Operation, OperationContract, register
from webapp.operation_lifecycle import Implementation, Lifecycle, register as register_impl

class Parameters(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    sample_rate: int = Field(default=16000, ge=8000, le=48000)

def execute(adapter, workflow, step, *, cancelled=None, record=None):
    # 使用adapter的受限输入/沙箱/固定命令执行设施。
    # 不在宿主exec Agent提供的代码；产生本次独立输出路径。
    # 返回ProducedFiles(paths, roles)，由服务端检查实际文件和角色。
    raise NotImplementedError('Implement the specific business handler')

register_impl(Implementation('example_extract@1', Lifecycle.LOCAL,
                            handlers={'local': execute}, cancellation='terminate_scope'))
register(Operation('audio.example_extract', 'extract', 'example_extract',
                   input_kinds=('video',), outputs=('audio',)),
         OperationContract(Parameters, frozenset({'source'}), frozenset({'source'})))
```

实现模块由受信任服务启动代码加载；不能由Agent临时注册。注册会拒绝重名、未知实现、无参数模型和重复输出角色。能力目录和参数校验读取同一模型。`tests/test_operation_lifecycles.py`的专用测试能力验证新增handler不修改Runner。

同一业务可通过 `register_implementation(operation_id, 'implementation@version', ...)`登记其他实现，仅能调整资源/供应商/环境描述，不修改业务输入输出。任务持久化已选实现，未知版本或其他能力的实现不能被替换进来。替换实现仍须满足已确认语义、调用范围及配额，不是无授权的自动降级。

## 生命周期

- local：执行固定本地处理，可安全重执行必须显式声明，输入固定、输出独立。
- remote_sync：invoke请求返回产物，没有上游任务ID就不能假称可查询恢复。
- remote_async：submit/poll/collect独立阶段，每次调用完成返回，不能在handler内部无限轮询。

服务端Runner负责依赖、租约、超时、取消、验收、检查点和发布。供应商仅处理协议；可选cancel/reconcile如实表达不支持。稳定请求键不等于供应商幂等保证。成功返回只表示执行结束，还需技术和业务验收，才能放行下游。

输入用asset或step+role引用，解析后验证项目、修订和hash；需要字幕/转写时显式列入inputs，不能猜测另一个节点目录中的最新文件。输出Result包含version、outputs(role/path)，ProducedFiles可附角色映射；实际hash/大小/媒体类型由服务端计算，越界、重名和错误媒体类型不能发布。

使用框架的连接和对象传输包装器。`process_call`在启动前登记，句柄安装后执行sticky取消；finally注销。HTTP使用受控session，S3 SDK使用可终止专用进程。普通handler不自行创建数据库轮询线程，不把无界停止动作放进观察线程。

## 必须验证

契约错误、未知参数、越权输入、正常执行、角色/媒体类型、失败、超时取消、重复事件、租约接管、过期提交、跨节点输入与输出。外部实现额外验证：提交未知、晚到ID、认证上下文、查询/下载重试、取消accepted与confirmed区别、上游不支持、取消后实际成功的状态/用量。

阶段错误需保留phase、类型、外部状态是否已知和恢复语义；诊断不包含凭据或签名下载地址。参数/实现/输入版本与attempt一同关联，未知费用保持null。文件上传完成不等于成果生效，最终发布必须走框架的租约校验事务。

## 可观测性接入门槛

新增 operation 应复用 [可观测性专项](observability-design.md)的上下文、结构化事件、错误/脱敏、阶段指标及诊断契约，覆盖本地、同步和异步执行；不得自行输出凭据或把高基数任务 ID 用作指标标签。观测由阶段包装器、受控 HTTP 会话、任务上下文和状态审计统一执行；新增处理器仍需补其失败、取消、恢复及输出契约测试，不能绕过包装器直接访问外部服务。专项验收见 observability-implementation-report.md。
