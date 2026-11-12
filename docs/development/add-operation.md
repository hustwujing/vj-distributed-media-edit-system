# 新增与扩展 operation

适用于新增业务 operation、为已有 operation 增加实现，以及修改参数、输入输出、资源或生命周期行为。本文是开发顺序与完成标准；注册代码模板见 [接入指南](../operation-extension-guide.md)，执行约束见 [接入规范](../operation-onboarding.md)。

## 1. 先确定扩展层级

阅读已有能力目录及相近实现，明确本次应落在哪一层：

| 需求 | 扩展方式 |
| --- | --- |
| 一次性的本地 Python / FFmpeg 组合处理 | 使用 `script` 步骤，不为每种编辑组合新增 operation |
| 可复用、具有独立参数和输入输出契约的业务能力 | 新增 operation，并注册版本化 implementation |
| 同一业务契约更换模型、供应商或执行方式 | 给已有 operation 注册替代 implementation |
| 现有生命周期确实无法表达的新状态转换 | 按 [operation 生命周期开发说明](change-operation-lifecycle.md)定义状态转换，再接通 Runner、持久化、调度、恢复与取消 |

operation 是业务能力，implementation 是固定版本的实现，lifecycle 是执行流程。普通能力接入通过注册和 handler 完成，不给调度器或 Runner 增加按 operation ID 判断的分支。

编码前记录能力 ID、参数、输入角色/类型、输出角色、实现版本、生命周期、资源需求和失败恢复语义。已有任务固定的实现和确认范围不得因新增默认实现而改变。

## 2. 查找实现入口

| 入口 | 阅读或修改目的 |
| --- | --- |
| [operations.py](../../webapp/operations.py) | `Operation`、`OperationContract`、注册、替代实现绑定和能力目录 |
| [operation_parameters.py](../../webapp/operation_parameters.py) | 已有内置能力的参数模型；新增模型可随扩展模块定义 |
| [operation_lifecycle.py](../../webapp/operation_lifecycle.py) | 生命周期、`Implementation` 和阶段 handler |
| [operation_extensions.py](../../webapp/operation_extensions.py) | 可信模块装载 |
| [workflow_runtime.py](../../webapp/workflow_runtime.py)、[workflow_inputs.py](../../webapp/workflow_inputs.py) | 隔离执行、输入解析、阶段结果和取消接口 |
| [media_handlers.py](../../webapp/media_handlers.py)、[worker.py](../../webapp/worker.py) | 固定媒体工具在隔离进程中的执行 |
| [generation_service.py](../../webapp/generation_service.py)、[external_cleanup.py](../../webapp/external_cleanup.py) | 生成协议、供应商句柄和外部取消清理 |
| [operation_result.py](../../webapp/operation_result.py) | `Result`、`ProducedFiles` 和输出类型验收 |
| [runtime_image.py](../../webapp/runtime_image.py) | 固定工具包、依赖和运行时指纹 |
| [契约测试](../../tests/test_operation_contracts.py)、[生命周期测试](../../tests/test_operation_lifecycles.py) | 现有注册、实现绑定、输入输出与确认约束的测试写法 |

这些是定位入口，不是每次都要修改的文件清单。先找最相近实现，再按差异确定改动范围。

## 3. 定义业务契约

- 参数使用 Pydantic 模型，设置 `extra='forbid'`，限制字符串/集合长度、数值范围和跨字段关系；数值参数拒绝 NaN/Infinity。能力目录 schema 与执行校验复用同一模型。
- 声明必需和允许的输入角色、输入数量及类型。输入通过 `asset` 或 `step + role` 引用；字幕、转写、蒙版、参考图也要显式声明，不能猜测其他节点的缓存路径。
- 声明输出角色及主产物；文件名固定时可填写 `output_files`，动态输出仍需提供明确的角色映射。
- 所有实现都必须产出的角色写入 `required_output_roles`，宿主在技术验收时逐项核对并返回 `output_role_missing`。`outputs` 是允许输出的角色集合，不等于全部必需；条件字幕、音频/视频互斥输出不得一律强制存在。目录的 `output_contract.required_roles` 与实际校验使用同一声明。
- 显式审视 `cpu`、`memory_mb`、`gpu`、`timeout`、`external_side_effects`、`budget_group`、`concurrency_group`、`route_adapter`、`provider_identity`、`cleanup_category`、`model_profile` 和 `retry_safe`。`Operation` 的 `retry_safe` 默认值为真，外部副作用尤其不能直接沿用默认值；只有证明重执行安全时才声明可安全重试。
- 不把任意命令、URL、凭据或 Python 导入路径开放为业务参数。改变现有契约时检查已有方案、步骤和检查点的兼容性；不兼容变化不能静默复用旧实现版本。

参数合法不等于输入已获授权；项目归属、修订和哈希仍由框架在解析与执行时校验。

媒体逐词剪辑和转写字幕通过 `media_transcript_inputs` 共用源角色/音轨绑定：
`transcript_<source_role>_t<audio_track>`；一个源只有一个使用中的音轨时允许
`transcript_<source_role>`，整个操作只有一个源/音轨组合时允许 `transcript`。
能力目录公开同一规则，规划校验拒绝缺失或歧义绑定；执行器把已授权输入映射写入只读 manifest。
worker 不查找未声明的项目转写缓存，多源、多音轨不能共享一个含义不明的转写别名。

`media.edit` 配置字幕时同时产生正式 `subtitle` 角色的 `final.srt`，与烧录字幕使用相同的分组和时间轴。
无字幕配置时该角色不存在；下游可引用角色，交付可直接选择现有文件，不需另写导出 Agent。
字幕角色必须经实际字幕格式验收，JSON/普通文本不能冒充。

## 4. 实现并注册执行流程

以下是已有三种流程。新增生命周期或改变阶段协议时，必须同时阅读 [operation 生命周期开发说明](change-operation-lifecycle.md)；只添加枚举、注册 handler 或填写恢复/取消描述不能完成框架接入。

| 生命周期 | 枚举 | handler 键 | 恢复要求 |
| --- | --- | --- | --- |
| 本地执行 | `Lifecycle.LOCAL` | `local` | 固定输入、独立输出，只有明确安全时可重执行 |
| 同步远程请求 | `Lifecycle.SYNC` | `sync` | 提交结果未知时核实或暂停；无任务 ID 不宣称可查询恢复 |
| 异步远程任务 | `Lifecycle.ASYNC` | `submit`、`poll`、`collect` | 保存上游句柄，查询和收取已有任务，不重复生成 |

handler 的调用形态为 `handler(adapter, workflow, step, *, cancelled=None, record=None)`。各阶段参数和返回值以现有实现及 Runner 消费方式为准；异步 handler 每次完成一个阶段，不在内部无限轮询。

阶段选择与恢复由 `Protocol` 和 `RecoveryAction` 显式定义，Runner 使用 `Implementation.phase/recover/resume_state`，未知协议拒绝执行。同步实现的 `records_calls` 默认为假；只有完整接入副作用前意图和产物持久化记录后才可启用，具体恢复条件见[生命周期规范](change-operation-lifecycle.md)。

先通过 `operation_lifecycle.register()` 注册带版本的实现（例如 `audio_extract@1`），再通过 `operations.register()` 注册能力和契约。建议显式设置 `implementation_id`；省略时会使用 `executor + '@1'`，非 `@1` 版本必须显式指定。完整示例见 [注册模板](../operation-extension-guide.md#定义与注册)。

已有能力增加替代实现时，先注册生命周期实现，再调用 `operations.register_implementation(operation_id, implementation_id, **resources)`。该入口保留业务契约，只允许受支持的资源/供应商描述覆盖；不能用替代实现绕过已确认语义或扩大外部调用范围。

本地 handler 复用现有隔离运行设施及固定工具，输出落在本次独立目录。通过 `media_handlers` 增加固定媒体动作时，还要检查 worker 的结果角色转换是否覆盖该动作。

远程 handler 复用受控 HTTP、取消监控和调用记录。提交前保存意图，取得任务 ID 后持久化句柄与路由；同步/异步接入、认证、额度、下载和清理接线按[外部服务与供应商接入](change-external-providers.md)核对。注册协议不代表整条调用链已经可用。

`audio.generate_from_text` 使用 MiniMax 异步 T2A 适配器：文本、音色、语速和格式属于冻结业务参数；Provider URL、模型、密钥及下载域名 allowlist 分别由 `VIDEO_USE_AUDIO_GENERATION_URL`、`VIDEO_USE_AUDIO_GENERATION_MODEL`、`VIDEO_USE_AUDIO_GENERATION_KEY`、`VIDEO_USE_AUDIO_GENERATION_DOWNLOAD_HOSTS` 配置。密钥只进入宿主执行器和外部清理进程；任务句柄、协议、模型、格式和凭据绑定摘要进入不可变调用记录。该 Provider 不支持取消，取消流程必须公开 `unsupported`，不能重提同一 revision。下载只接受 HTTPS allowlist 域名；tar 包只在内存中读取唯一有界音频成员，不解压任意路径。

Seedance 参考模式使用独立业务 ID：`video.generate_from_frames`、`video.generate_from_references`、`video.edit`、`video.extend`。不要把模式作为任意字符串下发，也不要让 Agent 构造素材 URL。输入角色、媒体类型、数量和总时长由 `OperationContract` 冻结；`workflow_runtime` 只读取已绑定版本，`ark_generation` 校验实际字节，`ark_reference_assets` 用内容摘要发布目标环境专属 BOSS 对象并注册 Ark Asset。新增参考角色、格式或 Provider 限制时必须同步这三层和真实 case。Ark/BOSS 长期凭据只在宿主私有环境中，不能写入调用 implementation、诊断或 Runtime 镜像。

Seedance 运行中拒绝 DELETE 是阶段性取消限制，不等于协议永久不支持，也不是普通传输失败。适配器以结构化错误码抛出 `CancellationDeferred`，外部清理回到 `poll` 并持续查询同一 Provider ID；轮询到终态才释放调用占用。真正不可查询的协议仍明确 `unsupported`，离线或认证故障仍按既有三次有界失败进入 `unconfirmed`。

不要在 handler 中另建任务池、数据库轮询线程或成果发布事务；依赖、租约、超时、状态推进、验收和正式发布由现有框架处理。新实现仍需实际接入这些设施，仅填写生命周期或 cancellation 字段不会自动实现取消行为。

音频窗口证据必须在重采样后按采样点数截取，再输出 PCM；FFmpeg 的 `-t` 受包时间戳和 AAC 填充影响，不能单独保证解码样本数上限。验证包括上限窗口、带 offset 的短窗口和合法但不均匀的包时间戳，不通过增大资源上限掩盖溢出。

audio_compare 的 offset 定位候选窗口，reference_offset 可独立定位参考窗口；缺省时两者使用同一起点。参考偏移只适用于音频对比，参与证据参数和输入哈希绑定。稳定窄带主频是测量事实，噪声、静音或不稳定信号返回未知，不能把主频一致升级为通用语音音高或身份一致证明。

视频取证使用保留时间戳的源帧选择，报告 `timestamp_basis=decoded_frame_pts`，单帧另保留请求时间。
不能用 fps 重采样后的人造时间轴标记源帧；短窗口不复制帧凑足16格，预览空格须明确为填充。
结构测量与联系表使用同一取样规则。回归包含24fps视频的1.975秒请求，实际第一帧应为2.0秒。

音频淡出必须按实际解码样本数、通道数和采样率计算音轨末端，不能使用视频容器时长。
`audio.process` 复用全流测量得到的样本数，并在报告记录 `duration_basis=decoded_samples`；
回归应包含视频比音轨长的输入，并检查输出末尾样本确实淡至零。

验收的 `audio_silent=true` 表示无声音：没有音轨，或所有音轨均测得静音。
必须保留静音音轨时同时声明 `audio_track_present=true`；无音轨不能满足非静音、音高或响度测量要求。
多音轨静音检查不能只检查第一条轨道；响度字段仍明确测量第一条音轨。

自动图片调整不应把纯色图拉成黑色。`local_adjust` 的自动色阶使用可见像素的统一亮度统计，
忽略无动态范围的输入，限制黑点偏移和增益以保留主体颜色；自动白平衡只使用明亮近中性像素，
没有可信参考时保持原平衡。色温参数独立执行，不能隐式打开白平衡；透明度保持原样。

## 5. 确认服务与隔离运行时都能装载

扩展模块可以使用可信服务配置加载：

```dotenv
VIDEO_USE_OPERATION_MODULES=your_package.audio_operations
```

该值由部署方配置，不接受 Agent 请求传入。模块应纳入发布源码或固定依赖；API、执行节点和外部清理进程需要装载其职责所需的注册信息。核对节点 `VIDEO_USE_NODE_OPERATIONS` 白名单、模型/系统依赖、凭据和资源容量。

**服务端可以 import 不等于沙盒中可用。** `runtime_image.py` 使用显式的 `TOOL_MODULES` 和 [capabilities.json](../../runtime/public/capabilities.json) 中的 helpers 清单构建工具包。新增沙盒内模块或脚本时需检查打包清单、固定入口导入、依赖、读取权限和运行时指纹；不要通过挂载整个宿主目录解决缺文件问题。服务端扩展加载配置也不等于隔离 worker 自动完成注册。

检查实际能力目录、节点声明与调用路径。当前 `Operation.available()` 含内置能力判断，新注册能力不能仅凭目录中的 `available` 字段就认定外部配置可用。分别验证配置缺失时的行为和配置正确后的执行。

Agent 通过能力目录发现 operation；如需求还包含工具页按钮、图片动作或直接工具入口，另行接入对应表单/API/授权校验。注册 operation 本身不会生成这些入口。

## 6. 验收输出并保留诊断

工具输出通过 `operation_result.Result` 表达，例如：

```json
{"version":1,"outputs":[{"role":"audio","path":"audio.wav"}]}
```

`Result.outputs[].path` 是输出目录内的文件名；返回 `ProducedFiles` 时按适配器约定传入路径及角色映射。拒绝越界、重复路径、未声明角色和错误媒体类型。宿主计算实际大小、哈希和技术检查结果，业务质量需要时再走 Agent/用户验收；下游只消费已验收修订。

动态文件名 operation 的 acceptance.file 可以引用已注册输出角色；可信宿主必须在当前 attempt 结果返回后，使用 `ProducedFiles.roles` 将角色绑定到实际文件，再运行质量检查。角色未知时不得猜测路径。固定文件名仍按文件契约校验；脚本输出不通过 operation 角色解析。计划编译、运行时验收和下游 `step + role` 必须使用同一权威角色定义。

新角色若不在既有类型检查范围内，应补充对应验证，不能假定任意角色名都会自动验证媒体类型。不能仅以文件存在、工具退出码为零或 Agent 声明成功作为交付依据。

Alpha 测量使用明确的 8 位阈值：`transparent_fraction` 只计 alpha=0，`opaque_fraction` 只计 alpha=255，`partial_alpha_fraction` 计 1..254，三者互斥且总和为 1。`visible_fraction` 另计 alpha≥128 的主体覆盖率，与连通分量/孔洞的二值阈值一致；它不是第四个互斥分区，也不代表肉眼可见程度。`transparent_subject` 要求同时存在完全透明背景和达到该覆盖阈值的主体像素；是否选中正确语义主体仍需内容审查。独立真机检查器必须使用同一阈值定义，不能用“具有 Alpha 通道”或“alpha<255”替代背景已去除的事实。

复用阶段指标、任务/步骤/attempt 关联和脱敏错误；保留失败阶段、上游状态是否已知及可恢复性。调用用量与结果分别记录，计量与诊断接入按[可观测性与用量开发](change-observability-usage.md)处理。

## 7. 验证与完成标准

新增针对本能力行为的测试，复用已有测试夹具；不要仅断言注册成功。按适用生命周期覆盖：

| 场景 | 应验证的结果 |
| --- | --- |
| 契约与授权 | 缺失/越界/未知参数、错误输入类型、跨项目引用被拒绝；实现不能替换成其他能力或未批准版本 |
| 正常执行与质量 | 真实产物角色和类型正确；伪造结果、错误内容不通过验收；下游使用正确修订 |
| 失败与取消 | 启动、运行、收取、上传及验收中的取消有效；超时和阻塞有界，资源最终回收 |
| 分布式恢复 | 删除原节点缓存后可恢复；过期租约或晚到回执不能发布成果；重复处理不重复收费 |
| 外部服务（适用时） | 提交未知、晚到 ID、查询/下载重试、认证路由变化、不支持取消和取消未确认有明确行为 |
| 集成与展示 | 能力目录、节点白名单、规划确认、用量/诊断可用；新增 UI 时补对应交互验证 |

用[隔离测试入口](../development.md#常规回归)运行新能力及受影响的契约/生命周期模块。完整回归、真实服务验收和仅文档变更的验证方式统一按[开发与测试](../development.md)。

依赖本机模型时按测试指南启用 `VIDEO_USE_TEST_REAL_MODELS=1`；它不代替真实外部服务验收。环境缺失导致跳过或未验证时注明边界，不用模拟结果宣称真实供应商已通过。

完成时应具备：可执行实现与装载方式、输入输出契约、适用的取消/恢复/验收证据，以及所需配置和依赖说明。同步能力参考、用户入口说明和相关开发文档；交付说明列出 operation ID、实现版本、验证结果与限制。实际发布遵循根目录 [README](../../README.md#本机隔离部署) 和 `deploy/dynamic_cluster.py`，源码完成不等于已经部署。
