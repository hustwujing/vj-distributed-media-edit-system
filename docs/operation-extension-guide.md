# 新 operation 接入

operation 表示业务能力，implementation 表示版本固定的实现，lifecycle 表示通用执行流程。普通能力接入不应修改调度器或 Runner 的分支。

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

提交外部副作用前必须保存调用意图；返回任务 ID 后持久化供应商句柄。供应商协议通过 `generation_service.register_provider()` 注册 `submit`、`poll`、`download`，可选择实现 `cancel`。不支持取消时明确报告，不伪造取消成功。现有供应商句柄查询及取消不会根据新的默认配置悄悄换供应商。

不得让 Agent 指定 executable、URL、凭据或任意导入路径。任务中保存实现 ID/版本，只有服务端注册且与 operation 匹配的实现可用。配置与原供应商凭据路由不一致时，应停止调用并显示明确原因。

## 输出与验收

工具的结构化结果使用 `operation_result.Result`：

```json
{"version":1,"outputs":[{"role":"audio","path":"audio.wav"}]}
```

结果路径是本次输出目录下的文件名。禁止路径穿越、重复路径和歧义角色。宿主核验实际文件、大小、哈希和媒体类型；处理器或 Agent 的声明不能代替技术验收。需要内容判断时再进入 Agent/用户验收，只有通过验收的指定版本才能被下游消费。

## 必须验证的情况

- 参数缺失、越界、额外字段及输入角色/类型不匹配。
- 实现版本未知，或将另一个能力的实现强行用于本 operation。
- 正常执行、输出角色正确、内容不符时拒绝验收。
- 排队、启动窗口、运行、收取、上传及验收阶段的取消。
- 本地子进程不响应正常终止，HTTP 阻塞，数据库观察阻塞和连接断开。
- 外部提交结果未知、晚到任务 ID、重复清理、过期执行器和清理器提交。
- 跨节点恢复输入和输出，输入哈希/修订变化，不允许重复收费生成。
- 独立分支并行、资源额度和下游依赖验收。

只在现有生命周期无法表达新能力时扩展通用 Runner，并为新增状态转换补充竞争及恢复测试。
