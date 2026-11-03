# 新 operation 接入

开发任务先读 [新增与扩展 operation](development/add-operation.md)，按步骤完成实现、装载与验证；本文提供注册接口和代码模板。

operation 表示业务能力，implementation 表示版本固定的实现，lifecycle 表示通用执行流程。普通能力接入不应修改调度器或 Runner 的分支。

本文的注册模板复用已有生命周期。新增生命周期或修改阶段协议属于框架扩展，必须另按 [operation 生命周期开发说明](development/change-operation-lifecycle.md)接通状态推进、持久化、恢复、取消和调度。

## 定义与注册

在服务端安装的 Python 模块中定义严格的参数模型、输入契约、输出角色和实现。以下为接口示例，不会自动新增生产业务能力：

```python
from pydantic import BaseModel, ConfigDict, Field
from webapp.operations import Operation, OperationContract, register
from webapp.operation_lifecycle import Implementation, Lifecycle, register as register_implementation

class ExtractParameters(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    sample_rate: int = Field(default=16000, ge=8000, le=96000)

def extract(adapter, workflow, step, *, cancelled, record=None):
    # 在这里接入固定沙箱工具。使用 adapter 的隔离运行设施准备
    # 声明输入和输出目录；不要在宿主执行 Agent 提供的命令。
    # 返回经处理产生的文件列表，或带角色的 ProducedFiles。
    raise NotImplementedError('Implement the fixed audio extraction tool')

register_implementation(Implementation(
    id='audio_extract@1',
    lifecycle=Lifecycle.LOCAL,
    cancellation='terminate_scope',
    handlers={'local': extract},
))
register(
    Operation('media.extract_audio', 'extract_audio', 'audio_extract',
              input_kinds=('video',), outputs=('audio',),
              cpu=2, memory_mb=2048, retry_safe=True),
    OperationContract(ExtractParameters,
                      required_inputs=frozenset({'source'}),
                      allowed_inputs=frozenset({'source'})),
)
```

通过可信服务配置 `VIDEO_USE_OPERATION_MODULES=your_package.audio_operations` 加载模块。该配置不是 Agent 请求参数。所有需要执行或清理该能力的服务实例都必须安装相同版本的模块。参数模型生成能力目录，同时用于请求校验。

同一业务能力的替代实现先注册到生命周期注册表，再通过 `operations.register_implementation(operation_id, implementation_id, **resources)` 绑定。业务输入输出契约保持不变；资源、供应商和模型配置可由具体实现声明。任务保存所选实现标识，默认配置改变不能改写已经提交的节点。

执行意图的 `implementations` 映射固定已批准实现；未显式指定时由服务端选择并保存默认实现。工作流提交时继承该映射，不能通过在步骤上换实现绕过确认。供应商额度按调用时绑定的后端类别计算，外部调用数不能因声明可安全重试而超过批准范围。

本地实现应复用沙箱、独立输出目录、统一取消监控及结构化结果。固定媒体工具可在 `media_handlers` 注册自己的处理函数。流程框架负责依赖、领取、租约、状态推进、成果发布和验收。

## 远程实现

同步能力使用 `remote_sync` / `sync` 处理阶段；异步能力使用 `remote_async` 的 `submit`、`poll`、`collect` 阶段。处理器可以提供已注册的回调，不需要向中央 Adapter 增加任意业务分支。

同步实现可在完成调用记录契约后声明 `Implementation(..., records_calls=True)`：每次外部副作用前保存调用意图，结果形成可恢复检查点后保存产物列表。Runner 使用该证据恢复已有结果；默认未声明时，缺少记录仍按提交未知暂停。该字段不会替实现自动写入记录，详细要求及恢复动作见[生命周期规范](development/change-operation-lifecycle.md)。

提交外部副作用前保存调用意图；返回任务 ID 后持久化供应商句柄。`generation_service.register_provider()` 注册 submit/poll/download 和可选 cancel 的协议分派，准备、认证及清理仍需单独接通。完整接线和验证见[外部服务与供应商接入](development/change-external-providers.md)。

operation 参数不得让 Agent 指定 executable、服务 URL、凭据或任意导入路径。任务中保存实现 ID/版本，只有服务端注册且与 operation 匹配的实现可用。配置与原供应商凭据路由不一致时，应停止调用并显示明确原因；`script` 步骤的受限本地命令是另一种契约。

## 输出与验收

工具的结构化结果使用 `operation_result.Result`：

```json
{"version":1,"outputs":[{"role":"audio","path":"audio.wav"}]}
```

结果路径是本次输出目录下的文件名。禁止路径穿越、重复路径和歧义角色。宿主核验实际文件、大小、哈希和媒体类型；处理器或 Agent 的声明不能代替技术验收。需要内容判断时再进入 Agent/用户验收，只有通过验收的指定版本才能被下游消费。

## 验证入口

按[新增与扩展 operation 的验收矩阵](development/add-operation.md#7-验证与完成标准)选择测试；取消、租约和跨节点机制改动另按[分布式执行说明](development/change-distributed-execution.md)。只有现有生命周期无法表达新能力时才扩展通用 Runner，并为新增状态转换补充竞争及恢复测试。

### 声明严格产物复用

`Operation(..., reuse_safe=True)` 是受信任实现作者的完整依赖声明，不等于 `retry_safe`。
只有本地生命周期、`external_side_effects=False`、model_profile 为 none、输入完整通过角色声明且不读取隐含项目状态的
实现才可启用。替代实现通过 `register_implementation(..., reuse_safe=True)` 单独声明，默认关闭。
框架匹配规范参数、输入角色顺序/哈希/元数据、实现和运行环境，在独立输出范围复制已验收来源，
再按新版 acceptance 检查。带任意 preflight/config_files 或仅排序而无输入绑定的依赖不支持。
更改输出相关配置时必须纳入契约/运行时版本；不能因为实现仍叫 `@1` 就假定历史输出可复用。
完整协议见 [工作流契约](workflow-contracts.md#跨方案严格复用)。
