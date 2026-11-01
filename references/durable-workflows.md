# 已确认方案的后台步骤与自动续跑

网页 Agent 在用户确认方案后，可以把较长任务交给持久化调度器。
讨论和普通短任务仍沿用原流程。后台等待不占用 Agent 进程，也不计入
Agent 单轮的 15 分钟运行时间；步骤结束后，服务自动恢复原会话做最终检查。

## 提交与查询

先准备本地脚本与计划，再执行：

```bash
python /opt/workflow.py validate edit/workflow.json
python /opt/workflow.py submit edit/workflow.json
python /opt/workflow.py status
```

提交成功后结束本次回复，告诉用户后台仍在处理，完成后会继续检查。
不要自己轮询、重复生成，或把提交成功说成成片已完成。
每个执行轮只能提交一个计划；相同内容重复提交返回原任务，不创建新任务。
计划不可覆盖。需要改变已确认范围时，先停止当前任务，再讨论新方案。

## 工作流计划格式（script/operation）

```json
{
  "key": "product-edit-v1",
  "name": "图片生成与成片包装",
  "steps": [
    {
      "id": "image",
      "name": "生成产品场景",
      "kind": "operation",
      "operation": "image.edit",
      "inputs": {"source": {"asset": "从 assets.json 读取的素材 ID"}},
      "goal": "按已确认方案生成浅蓝色产品场景并保留主体",
      "on_failure": "diagnose",
      "acceptance": [{"kind": "image"}, {"kind": "agent", "criterion": "浅蓝色背景，产品主体与原素材一致"}],
      "parameters": {
        "prompt": "将背景改为浅蓝色，保留产品主体",
        "quality": "fast"
      }
    },
    {
      "id": "package",
      "name": "添加标题并导出",
      "kind": "script",
      "goal": "将生成图制作成 1080×1920 的标题海报",
      "on_failure": "diagnose",
      "acceptance": [{"kind": "image", "file": "final.png", "width": 1080, "height": 1920}],
      "depends_on": ["image"],
      "inputs": {"source": {"step": "image", "role": "primary_image"}},
      "argv": ["python", "edit/package.py"],
      "outputs": ["final.png"],
      "retry_safe": true,
      "timeout": 600
    }
  ]
}
```

- 最多 32 个步骤，依赖只能引用前面的步骤；最多 4 个图片生成步骤。
- 步骤 kind 只允许 `script` 和 `operation`，旧 `local`/`image` 步骤及旧 image 字段不再接受。
- 图片处理使用登记操作，例如 `image.edit`、`image.decompose`、`image.remove_background`。
  抠图输出 foreground.png 和 mask.png，可声明 retry_safe；已完成结果复用。
  输入用 `inputs` 引用素材或已验收的前置步骤输出角色。GPU、模型及外部调用受服务端约束。
- `script` 执行 argv 数组，可以调用现有 helpers、FFmpeg 或已保存的 Python 脚本；
  脚本在项目沙箱运行，没有模型网关。
- `outputs` 是必须生成且非空的文件名。产物先成为候选，经固定检查器验收后
  才能进入后续步骤。使用下面的 `acceptance` 保存明确的验收条件。
- Agent 必须自动填写每步 goal、acceptance、on_failure（pause/diagnose），无需用户配置。
  所有本地产物应有对应检查；仅需基础文件检查时使用 file，并用 basic_reason 解释。
  先 validate 补齐缺项再 submit。服务端检查结构完整性，不保证条件已覆盖全部用户意图。
- 每次本地执行使用新的输出目录。脚本从 `VIDEO_USE_STEP_OUTPUT` 读取目录，
  argv 中也可用 `{output}` 替代输出目录。禁止覆盖源素材与前面步骤的文件。
- `VIDEO_USE_STEP_INPUTS` 是 JSON 对象：声明的输入角色 → 已解析的路径数组。
  优先用明确的业务角色引用产物，多个候选不能自行默取第一张。
- 本地超时默认 900 秒，可设置 10～3600 秒。`retry_safe` 默认 false；仅当脚本
  只读取输入、向本次输出目录写文件、无其他副作用时设 true。失败或重启后最多
  自动尝试 3 次，且每次使用新目录。质量修复与故障重试分别计数。

## 验收条件与本地修复

在每个步骤中声明最多 20 条 `acceptance`，例如：

```json
{
  "acceptance": [
    {"kind": "image", "file": "final.png", "width": 1080, "height": 1920},
    {"kind": "agent", "criterion": "完整保留产品主体，标题没有遮挡主体，风格符合已确认方案"}
  ],
  "repairs": [
    {"reason": "尺寸不符时使用已准备的等比缩放加留白方案", "when": ["width", "height"], "argv": ["python", "edit/package.py", "--fit", "1080x1920"]}
  ]
}
```

条件支持 `file`（非空、`max_bytes`）、`image`（尺寸）、`media`（尺寸、
`min_duration`/`max_duration`、`audio`/`video`）、`text`（`contains` 列表）、
`agent`（Agent 内容验收）、`review`（用户确认）。`file` 是输出文件名或 `*`。技术有效不代表语义正确；
风格、字幕视觉布局和主体完整性等要求按需声明 Agent 内容验收；确需用户决定时声明 review。
不要编造质量分数。criterion 只能用于 agent/review，不能写在 file 等程序条件中。人工验收也不能放行文件损坏或明确不符合技术条件的结果。

本地步骤可预先声明最多两种修复命令，必须在原确认范围内，并先保存所需脚本。
修复继续受同样的只读输入、独立输出、时限和预算限制，每次重新验收。
禁止为图片生成步骤声明自动重新生成；重新调用外部服务需用户明确确认。
预设修复按 when 错误代码匹配，不能仅按顺序盲试。可用代码：integrity、missing、
width、height、audio、video、min_duration、max_duration、max_bytes、contains、review。
未匹配预设修复且 on_failure 为 diagnose 时，会唤回只读 Agent 诊断并提交受控修复；
需要用户决定或无法安全处理时暂停，网页展示候选、
检查证据和反馈入口。修改上游步骤只使其依赖链结果失效，其余结果保留。

任务默认从入队起最多 24 小时，本地累计运行最多 2 小时；重启、继续和修复
均不重置。实际限制由服务端决定。进度查询和停止走控制接口，不需要启动模型。

Python 脚本读取输入与输出的方式：

```python
import json, os
from pathlib import Path
from PIL import Image
inputs = json.loads(os.environ['VIDEO_USE_STEP_INPUTS'])
source = next(Path(p) for p in inputs['image'] if Path(p).suffix.lower() in ('.png', '.jpg', '.webp'))
output = Path(os.environ['VIDEO_USE_STEP_OUTPUT'])
with Image.open(source) as im:
    # 按已确认要求处理，再写入本次目录。
    im.save(output / 'final.png')
```

## 自动恢复、暂停和停止

服务端先保存提交意图，再向模型服务提交。获取任务 ID 后持久化记录；等待、
刷新页面或服务重启后，仅查询该 ID，不会再提交一次。网络查询失败会退避重试，
连续 5 次失败后暂停，可在网页点击继续查询。

如果进程在提交请求与保存任务 ID 之间中断，无法确认服务是否接单，任务会暂停，
不会自动重提。现有图片服务的幂等保证未经确认，客户端不能仅凭请求头就假定安全。
这时应核实上游任务，再停止当前任务并重新讨论；网页不允许随意绑定其他任务 ID。

已完成步骤与结果不因暂停或停止而删除。停止会阻止后续提交，并尝试取消已知外部
任务；上游取消失败会明确显示。继续操作只恢复可安全重试的本地步骤或已有任务查询。
服务重启会恢复等待状态；未登记为后台步骤的普通 Agent 命令仍不自动重放。

步骤需要内容验收或诊断时，以及后台步骤全部完成后，服务将同一个执行轮重新入队。
Agent 读取 references/workflow-review.md 和当前 review_context，提交结构化结论；
服务端检查实际文件、版本和验收标准后应用。最终交付必须明确列出通过检查的结果；
不能仅凭 Agent 正常结束就标记成功。不要再次提交已完成步骤。

只读验收中断时，已完成结果保留，系统有限恢复该检查；未提交结论时暂停并提供继续检查。
普通执行轮仍不会自动重放可能包含外部副作用的对话。当前调度器按项目串行推进步骤，默认两个执行槽，可配置并受资源上限约束，
不提供跨项目任务依赖或无限期自主生成。

## 统一操作协议（当前新增）

使用 `python /opt/workflow.py capabilities` 或项目 capabilities.json 查询实际登记能力与参数。available 表示配置条件满足；configured_unverified 不代表上游已经健康。纯文生图和图生视频必须有已适配的服务，validate 会核对操作、参考图、时长与尺寸。不能用循环/定格擅自凑时长。

规划阶段将执行意图保存到本轮预览目录：

```json
{"route":"workflow","operations":["image.remove_background","script"],"goal":"透明图标叠加视频","estimated_seconds":300}
```

执行 `python /opt/workflow.py intent 文件路径`。独立短任务可用 single，但仅限一个操作且预计不超过 120 秒。确认后继承意图，直接图片/转写入口会检查它；多阶段操作不得提前同步调用。用户无需手填这些字段。

新步骤可以使用 operation，复用固定工具：

```json
{
  "id":"cutout","name":"提取透明图标","kind":"operation",
  "operation":"image.remove_background",
  "inputs":{"source":{"asset":"当前项目素材ID"}},
  "parameters":{},"goal":"保留完整主体并移除背景",
  "on_failure":"diagnose","retry_safe":true,
  "acceptance":[
    {"target":"image","reviewer":"program","file":"foreground.png","has_alpha":true},
    {"target":"image","reviewer":"agent","file":"foreground.png","criterion":"主体完整，无明显背景残留"}
  ]
}
```

后续 script 步骤用 `inputs: {"logo":{"step":"cutout","role":"foreground"}}` 并声明 depends_on。服务端绑定版本和哈希，禁止自己拼接上游结果路径或任意 URL。脚本通过 `from step import input_file, output_file, progress, report` 使用固定 SDK，例如 `input_file('logo')`、`output_file('final.mp4')`。进度只能报告真实工作量。

图片操作 parameters 使用原 ImageTask 参数（不含 action/asset/references/mask/document）；高级参数仍位于 parameters 内部的 parameters 字段。媒体操作 media.render/edit/timeline/analyze/review/pack 复用已有固定 worker，参数中的 asset 可引用 inputs 角色名。具体允许字段以 capabilities 的参数 schema 为准。

新脚本先调用 `python /opt/workflow.py preflight edit/workflow.json`，按报告修复语法、配置和目标环境缺失依赖，再 submit。preflight 不执行正式业务、不调用外部服务；必要的小样本验证应另用受限本地脚本，明确覆盖范围。复杂处理按可复用、可验收、重算成本高的中间成果拆步，同时考虑磁盘和重复编码成本。

子进程失败时保留标准输出和标准错误的末尾诊断，不能只抛退出码或空的 RuntimeError；捕获 FFmpeg stderr 后须在失败时输出，供调度器诊断。FFmpeg concat 清单内相对路径按清单所在目录解析，使用已绑定输入的绝对路径，避免把工作区相对路径直接写进深层输出目录中的清单。严格帧数和切点验收不要假设流复制一定精确；超界时按已确认回退方案精确编码并重新验收。

`VIDEO_USE_STEP_INPUTS` 和 `step.input_file()` 提供沙箱工作区内的绝对路径，直接传给文件读取、FFmpeg 或 concat 清单即可。不要再手动添加 `/workspace/` 前缀；脚本或子进程改变工作目录不会改变这些输入绑定。

验收支持 target=file/image/media/text/json/subtitle 与 reviewer=program/agent/user。程序 signal 只提示，不覆盖基础文件完整性要求；内容判断是放行条件。可用 has_alpha、min_transparent_fraction、fps、decode、required_keys，或 checker 指向 edit 内的 Python 检查脚本。检查器读取 VIDEO_USE_CHECK_FILES，输出 `{ "passed": true, "reason": "实际检查说明" }`，在只读环境和独立时限内执行。

安全边界暂停只阻止后续步骤，不取消已提交上游。继续、重新生成和重新规划是不同操作。外部调用日志中的费用未知保持 null，不把网络失败解释为未计费。

纯文生图也可使用本项目已配置的模型网关 Image API（服务端选定模型，不接收 Agent 提供的 URL/密钥）。本机验证发现上游可能忽略请求尺寸，因此参数 fit=native 保留实际原图且不保证尺寸；精确尺寸要求必须明确选择 contain（等比留白）或 cover（裁剪填充），或另加本地步骤。contain 的 background 默认白色，应纳入确认方案；原始返回文件保留在步骤检查点中。技术验收检查最终实际文件，不能因接口请求了某尺寸就认为输出符合。

## Uniform generation operations

Use `image.generate_from_text`, `video.generate_from_image`, or `video.generate_from_text` as registered operations. Only image-to-video accepts `inputs.source`; no operation accepts provider credentials or URLs. Query the capability catalogue for current sizes, durations, audio and cancellation limits. Provider implementation is pinned in the server call record. See [uniform generation design](../docs/uniform-generation-design.md).

### 单次本地脚本的执行意图

通过执行命令运行现有 helpers，或编写 Pillow、FFmpeg、OpenCV 等本地脚本时，使用 `script` 标记：

```json
{"route":"single","operations":["script"],"goal":"在本地旋转图片并保存PNG","estimated_seconds":30}
```

只有实际调用受控 operation 时才声明其 ID。`image.edit` 是外部生成式图片编辑，不是本地图片处理的泛称；`image.batch_export` 和 `media.edit` 也不能代替任意脚本的声明。原方案含外部操作、现在改为脚本或反向变化时，先修正讨论轮意图再让用户确认，不在执行轮静默变更。

### 输出角色必须在提交前闭合

脚本的 `outputs` 只声明文件名；下游要引用时，上游还必须显式写 `output_roles`。每个角色只指向一个文件，例如 `{"left":"left.mp4","right":"right.mp4"}`；下游分别声明 left/right 输入并指向对应 step+role。一个 JSON 清单中的文件名不会自动获得读取其他文件的权限，多片段必须逐个绑定或显式打包为一个合法产物。校验会拒绝未声明的角色，不能提交后再猜目录或补契约。
