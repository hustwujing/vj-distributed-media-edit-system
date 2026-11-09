# 前端与产物展示开发说明

适用于修改聊天、工具编辑、图片工作台、实时进度、方案与决策卡片、产物排序、预览、下载、表单及画布交互。页面行为参考[页面与交互设计](../../UI_DESIGN.md)和[Web 使用指南](../../WEB_UI.md)。本文规定开发入口、步骤和验证要求；已有行为应以当前代码核实，不能将设计描述或历史浏览器验收直接作为本次通过证据。

涉及确认和工具权限时同时读取[Agent 与对话开发说明](change-agent-conversation.md)；涉及验收、候选或用户决策时读取[工作流开发说明](change-workflow.md)；涉及上传、成果发布与跨节点恢复时读取[分布式执行开发说明](change-distributed-execution.md)。

本分支 `/chat` 实际使用 `dynamic.html/js/css`，Run、输入屏障和决策以[动态工作流规范](change-dynamic-workflow.md)为准；下文旧聊天、工具和画布模块只在修改对应复用模块时适用，不能把旧 Plan/Execute 控制语义接回动态页面。

## 1. 定位页面与数据入口

前端使用原生 JavaScript 模块、HTML 和 CSS，没有 npm 构建步骤。复用现有公共模块、样式和本地资源；新增交互不要顺带引入另一套框架或外部运行时 CDN。测试与环境见[开发与测试](../development.md)。

| 改动 | 实现入口 | 需要核对 |
| --- | --- | --- |
| 页面入口和项目导航 | [app.py](../../webapp/app.py)、[home.js](../../webapp/static/home.js)、[common.js](../../webapp/static/common.js) | 页面归属、项目切换、base URL、公共组件 |
| 动态 Run 与模型选择 | [dynamic.js](../../webapp/static/dynamic.js)、[dynamic_models.js](../../webapp/static/dynamic_models.js)、[model_selection.py](../../webapp/model_selection.py)、[workflow_api.py](../../webapp/workflow_api.py) | 模型草稿、预设摘要、Run 冻结配置、输入屏障和质量决策 |
| 聊天和方案/决策卡片 | [chat.js](../../webapp/static/chat.js)、[plan_readiness.js](../../webapp/static/plan_readiness.js)、[agent_api.py](../../webapp/agent_api.py) | 当前 turn、确认 digest、决策 revision、提交结果 |
| 文本、活动和状态 | [text_stream.js](../../webapp/static/text_stream.js)、[activity.js](../../webapp/static/activity.js)、[task_progress.js](../../webapp/static/task_progress.js)、[work_status.js](../../webapp/static/work_status.js) | 版本合并、重连、阶段、等待与终态 |
| 用量 | [task_usage.js](../../webapp/static/task_usage.js)、[usage_api.py](../../webapp/usage_api.py) | 当前任务、关联需求、项目口径与未知值 |
| 产物导出和排序 | [agent_sandbox.py](../../webapp/agent_sandbox.py)、[preview_export.py](../../webapp/preview_export.py)、[artifacts.py](../../webapp/artifacts.py)、[artifact_order.js](../../webapp/static/artifact_order.js) | 服务端成果元数据、主结果、历史与过程文件 |
| 媒体及 Markdown 展示 | [common.js](../../webapp/static/common.js)、[agent_api.py](../../webapp/agent_api.py)、[app.py](../../webapp/app.py) | 链接解析、卡片、播放器、缩略图和下载授权 |
| 工具表单 | [tool_forms.js](../../webapp/static/tool_forms.js)、[tools.js](../../webapp/static/tools.js)、[edit_tools.py](../../webapp/edit_tools.py) | 动作参数、草稿、输入素材和服务端计划绑定 |
| 图片文档和画布 | [images.js](../../webapp/static/images.js)、[canvas_renderer.js](../../webapp/static/canvas_renderer.js)、[image_api.py](../../webapp/image_api.py)、[image_document.py](../../webapp/image_document.py)、[image_canvas.py](../../webapp/image_canvas.py) | 渲染、选区、版本、锁定图层和结果应用 |
| 对比与布局 | [image_compare.js](../../webapp/static/image_compare.js)、[video_compare.js](../../webapp/static/video_compare.js)、[studio.css](../../webapp/static/studio.css)、[images.css](../../webapp/static/images.css) | 对比目标、窄屏、滚动区域、弹窗与焦点 |

开始前明确触发动作、所在项目/轮次、服务端字段、浏览器本地状态及受影响页面。公共模块修改要检查所有调用方，不能只验证聊天页而遗漏工具页或图片工作台。

## 2. 明确数据来源与状态归属

| 数据 | 权威来源与开发约束 |
| --- | --- |
| 任务状态、等待原因、取消与清理 | 服务端持久化记录；连接状态与业务状态分别展示 |
| 方案是否可执行 | `plan_readiness` 及对应方案/digest；回复成功或出现“确认”文字不能替代 |
| 质量、交付和输出角色 | 服务端验收及发布元数据；浏览器排序不改变质量或交付状态 |
| 画布已保存版本 | 服务端 document revision；浏览器未保存编辑是独立草稿 |
| 工具和聊天草稿 | 区分项目 draft、按项目保存的浏览器设置及当前未提交内容 |
| 当前选区、焦点、展开项和播放位置 | 浏览器交互状态；刷新业务数据时按需保留，切项目时不得串用 |

新增字段先明确由谁写入、缺失时如何兼容、如何恢复以及是否允许客户端修改。关闭按钮只是交互提示；归属、revision、确认范围和有效状态仍须由 API 重新校验。历史数据缺少字段时使用明确兼容规则，不能默认把候选结果升级为交付。

执行位置通过 `execution_locations.for_run` 批量读取当前 revision 最近 attempt 的本地 Runtime / OpenSandbox cleanup 记录。不得用节点当前配置、任务类型或旧 bindings 推断已经使用的沙箱；重试、revision 变更、已销毁实例和实例创建未确认均须区分。前端位置缺失显示未记录，活动行缓存包含位置及 attempt ID。定向验证使用 `tests.test_dynamic_api` 和 `tests/browser_execution_locations.cjs`；后者用实际渲染器与 CSS 验证桌面/窄屏、位置更新、紧凑排版和代码块换行，不启动真实模型任务。

运行图按可见宽度逐行蛇形排列：拓扑有序的每个节点占一个格子，同层的平行节点也继续填满当前行；到边界后在同一x坐标下移一行并反向，不把一个拓扑层堆成无限长的竖列。连线保留真实依赖，不将相邻布局误当作执行依赖。缩放窗口重新计算自动布局，手动位置按 Run/图版本/布局版本/可见宽度隔离保存；默认布局不得靠横向溢出展示长链。连线支持向左和换行，拖动后仍连接实际节点边界。
每个节点右上角显示最近尝试的位置标记；程序宿主与工具沙箱不同则同时标注。外部服务状态仅来自当前 Task revision 的调用账本，提交未知不能冒称已在外部执行。Run 汇总节点标注控制面，不代表整轮都在同一宿主或沙箱。位置/工具/attempt/外部调用状态都参与节点渲染缓存键。
API 的 `selected` 统一输出布尔值，避免存储层的数字0绕过前端退役过滤。执行过程首次进入每个Run默认折叠，数据刷新保留用户手动展开状态；待确认方案和介入决策不随执行过程折叠。

计时来自持久化Attempt的started/finished/status，按Task及revision关联；节点显示最近三次尝试，每次一行，新增挤掉最早一行。Agent输出通过公开消息在累积正文中的UTF-16跨度绑定Attempt，不按角色名字或页面接收时间猜归属。运行中每秒只更新time文本，不重建节点、不滚动聊天；结束后显示固定耗时及结束时间，过期/中断单独标注。没有开始记录或缺少结束回执明确显示未知，终态不继续计数。API不公开worker私有路径。页面退出清理计时器。

## 3. 修改实时更新与交互

- 文本快照、文本增量、任务快照和活动事件分别处理。正文通过现有 `mergeTurn`、`applyText` 等入口按版本合并，不按网络到达顺序直接覆盖；增量缺少对应基础版本时重新获取快照。
- 版本比较保留 generation 与 sequence 的含义，旧尝试、重复事件和过期轮询不能覆盖新正文或恢复已结束活动。连接切换后，旧 EventSource 回调不能继续更新当前页面。
- 轮询的finally同样受请求epoch约束：过期请求不能清除新请求安装的下一轮定时器，否则页面可能停在旧状态而SSE文字仍继续更新。验证成功与失败响应乱序时轮询均持续。
- 核对 SSE 的 `Last-Event-ID`、数据库事件分页与 Redis 游标各自的处理方式；不能把它们当成同一种序号。Redis 不可用时继续用数据库快照校准，并如实显示连接状态。
- 排队、资源等待、上游处理、等待用户、暂停、取消中和终态使用真实字段；不要模拟进度百分比，也不要把一段时间没有文字视为任务失败。
- 增量更新只改受影响区域。避免轮询时重建输入框、播放器、打开的决策表单或整个画布；用户向上阅读时保持位置，仅在接近底部或主动选择后跟随滚动。
- 提交期间防止重复点击；成功后按服务器返回值更新，失败时保留输入并显示可理解的错误。发送请求后用户继续输入的新内容不能被旧请求的成功回调清空。
- 方案卡读取 `can_execute`，确认请求绑定当前方案；用户决策绑定 request ID 和 revision。处理过期、重复提交和并发更新时刷新状态，不能用重发新请求绕过冲突。
- 组件或页面退出时清理定时器、订阅、监听器和临时对象 URL；慢请求返回时核对它仍属于当前项目、轮次或选择目标。
- 正文、公开过程说明、当前活动和最终答复保持可辨识。展示公开事件，不渲染内部推理或服务凭据；用量保留未知值，不能将缺失 Token 显示为零。

## 4. 修改产物展示、预览和下载

首次加载也须恢复浏览器草稿：HTML预置data-run=new不代表草稿已恢复；仅初始化或切换Run时读取，普通轮询不覆盖正在输入的内容，无浏览器草稿时保留服务端示例提示。

图片画布在初始文档、素材及版本信息加载完成前保持inert/aria-busy，避免用户添加的图层被迟到的初始化结果覆盖。加载失败保持禁用并显示失败状态，不能以空白文档继续覆盖已有版本。

先检查服务端成果导出，再检查聊天卡片、结果库和对比入口。以下字段分别有独立含义：

| 字段或概念 | 含义 |
| --- | --- |
| `output_roles` / `source_step` | 业务角色及来源步骤，帮助定位结果；不自动表示最终交付 |
| `quality_state` | 验收状态；pending、superseded 等不能成为合格主交付 |
| `workflow_delivery` | 工作流是否明确将此结果作为交付 |
| `is_primary` / `alternate_version` | 展示优先级与同系列版本；不能改变文件授权或验收结论 |
| `purpose=style_preview` | 讨论阶段样式预览，须与正式成果区分 |

- 复用 `artifacts.py` 与 `artifact_order.js` 的对应规则。修改主结果选择、自然数字版本、过程文件分类或兼容字段时，同时核对服务端序列化、聊天卡片及结果库行为。
- 正式任务的主交付只能从实际返回且符合交付条件的文件中选择。样式预览可有自己的主展示，但仍保留预览身份。显式声明、回复中唯一引用及排序回退规则用于展示，不能凭文件名、扩展名或最新消息绕过验收。
- 结果库去重与原始记录分开。当前展示合并历史重复副本时仍保留原轮次预览/下载地址；不要只按 basename 或跨项目键合并不同文件，也不要因隐藏旧卡片删除对象。
- 候选或中间成果可按权限用于检查、对比和人工选择；能够下载、导入为素材或查看不代表它已通过工作流验收。结果应用与下游放行沿用对应业务接口。
- 复用公共 Preview、媒体卡片和链接绑定逻辑，区分图片、视频、音频及文本。切换内容时处理加载失败和旧响应，关闭弹窗时停止播放；下载作为独立动作。
- 用户文字、模型回复和文件名按不可信文本处理，使用统一转义与 Markdown 解析。代码块保持字面文本，链接及属性不能注入可执行 HTML 或脚本协议。
- Markdown 中本地媒体引用只映射到当前允许的成果/素材清单和服务端 URL。处理空格、中文、特殊字符及编码时不能扩大授权范围；不得把模型提供的任意宿主路径直接变成下载接口。
- 预览、缩略图、原文件、下载和图片导入都要由服务端验证项目及文件归属。前端藏起链接不能代替授权；使用已有 `base` / `href` 路径机制，兼容 `/video-use/` 前缀，不能硬编码部署地址或暴露对象桶凭据。
- 动态聊天中显式点击预览必须打开可见弹窗；自动展示可使用侧边预览区，不能因同一URL已自动加载或侧栏在屏幕外而让点击没有反馈。共用Preview按可信MIME分流，缺失/通用二进制MIME时按文件名识别视频、音频、图片、PDF和文本；未知二进制只提供明确的下载提示。媒体加载失败在弹窗内显示错误，关闭弹窗暂停播放器。验证入口`tests/browser_artifact_preview.cjs`，覆盖桌面与窄屏、同一已自动加载MP4、真实播放和关闭、图片、音频、字幕/JSON及失败反馈。
- 若修改缩略图缓存或媒体引用，确保同路径新版本不会显示旧画面。跨 API 实例访问应从已发布记录和共享存储恢复，不依赖原实例磁盘。

## 5. 修改工具表单和图片画布

动态模型表单只提交服务端登记的预设 ID、推理强度和 `profile_digest`，不得接收任意模型路由或凭据。选择草稿按项目保存到 sessionStorage；提交前重新核对能力，过期预设需用户再次确认。当前 Run 的型号和强度只显示其持久化 `model_configuration`，缺失时明确未知，不能从草稿或当前默认值推断。自动 successor 与显式新 Run 的继承规则见动态工作流规范。

相关验证覆盖选择与刷新、上传期间保存选择、预设变更、提交期间禁用、已有 Run 不变、移动端布局及跨项目隔离。使用 `tests/test_model_selection.py`、`tests/dynamic_models.test.mjs`；部署后浏览器入口为 `tests/campaign_model_deployment_smoke.cjs`，必须记录实际执行结果，脚本存在不表示已通过。

工具表单先明确动作、所选素材、参数默认值及适用条件，再同步序列化和后端校验。隐藏的旧字段不能残留在另一个动作的请求中；数值范围、缺省与显式零值需分别处理。浏览器提交素材 ID 和业务参数，路径绑定与计划验证由服务端完成。

草稿按项目和工具隔离。切换工具保留尚未完成的输入，自动保存失败如实提示；慢保存回执不能将之后的新编辑错误标为已保存。缺转写或模型配置时说明可选动作，不能因打开表单、选择素材或绘制选区自动发起收费调用。

图片工作台另外检查：

- 图层位置、旋转、画布尺寸和源图片坐标不是同一坐标系。修改点、框、画笔和蒙版处理时验证缩放、旋转及边界；预览渲染与实际导出使用同一文档语义。
- 保存绑定 document revision，服务端拒绝过期写入。冲突时保留本地草稿并提供载入当前版本的操作，不能自动把旧草稿换成新 revision 后重试覆盖。
- 保存请求发出后仍可发生编辑；成功回执仅证明所提交快照已保存。恢复旧版本也要创建新版本并遵守当前锁定规则，不能倒退服务端 revision。
- 图层锁定在后端执行。普通保存、删除、重排、结果应用和 Agent 回写均不能绕过；显式解锁走现有操作。
- 导出任务绑定提交时的完整文档快照。用户之后改变画布不应改变已排队任务的输入；结果回来后明确展示来源，不覆盖当前未保存编辑。
- 模型结果通过主动载入或应用操作进入画布。候选对比期间固定目标 layer ID 和 revision，应用前由服务端重新核对项目、目标类型、锁定及版本；保留未修改图层与几何属性。
- Agent 编辑按已确认的目标和版本回写。旧轮次、取消轮次、目标变化或其他页面修改后拒绝旧结果，不用前端选择状态替换批准目标。

## 6. 验证模块行为与真实页面

逻辑测试验证数据与状态，浏览器验证实际交互、布局和媒体播放。按变更选择以下入口；测试名字存在不代表已经覆盖本次新增行为。

| 场景 | 应验证的结果 | 相关测试入口 |
| --- | --- | --- |
| 正文、状态与重连 | 乱序/重复/缺片不损坏正文；终态稳定；断线恢复后无旧活动 | [text_stream.test.mjs](../../tests/text_stream.test.mjs)、[task_progress.test.mjs](../../tests/task_progress.test.mjs)、[test_agent_text.py](../../tests/test_agent_text.py)、[test_live_activity.py](../../tests/test_live_activity.py) |
| 方案和用户动作 | 旧方案不能执行；失败/取消的检查不永久转圈；过期决策被拒绝 | [plan_readiness.test.mjs](../../tests/plan_readiness.test.mjs)、[test_conversation_routing.py](../../tests/test_conversation_routing.py)、[test_user_decisions.py](../../tests/test_user_decisions.py) |
| 交付、排序和链接 | 候选不升格；数字版本与显式主结果一致；特殊文件名可用；未知文件和危险链接被拒绝 | [artifact_order.test.mjs](../../tests/artifact_order.test.mjs)、[markdown_links.test.mjs](../../tests/markdown_links.test.mjs)、[test_artifact_delivery.py](../../tests/test_artifact_delivery.py) |
| 文件和表单权限 | 外项目文件/参数被拒绝；草稿正确恢复；跨实例仍可获取授权文件 | [test_manual_tools.py](../../tests/test_manual_tools.py)、[test_webapp.py](../../tests/test_webapp.py)、[test_distributed_api.py](../../tests/test_distributed_api.py) |
| 画布版本和像素 | 并发写入不覆盖；锁定有效；导出/应用目标正确；选区外内容按契约保留 | [test_image_workbench.py](../../tests/test_image_workbench.py)、[test_canvas_editing.py](../../tests/test_canvas_editing.py) |
| 用量展示 | 三种范围不混算；未知值与已知零值不同；不重复汇总 | [task_usage.test.mjs](../../tests/task_usage.test.mjs)、[test_task_usage.py](../../tests/test_task_usage.py) |

例如，修改成果排序或链接时：

```bash
node --test tests/artifact_order.test.mjs tests/markdown_links.test.mjs
.venv/bin/python -m tests.run_distributed_suite tests.test_artifact_delivery
```

按影响追加相关模块。浏览器入口可参考 [run_browser_workflow_smoke.py](../../tests/run_browser_workflow_smoke.py) 与 [browser_workflow_smoke.cjs](../../tests/browser_workflow_smoke.cjs)，先确认其隔离方式、依赖和实际覆盖页面；该驱动不能代替所有图片画布或媒体交互验证。

可见交互变化至少按适用范围实测：首次打开与刷新、项目/任务切换、断线重连、慢请求、提交失败、播放中刷新、未保存输入、空结果、长文件名、多个候选、画布版本冲突。布局和控件变化检查桌面与窄屏、键盘访问、焦点可见、按钮名称、弹窗关闭及焦点返回；状态不能仅靠颜色区分。不要只凭截图或静态 HTML 断言交互通过。

发布检查遵循[开发与测试](../development.md)，执行完整前端测试及发布要求的 Python 回归。浏览器和真实服务验收分别记录；页面能显示模拟状态不证明实际模型产物、视频解码或外部服务可用。

## 7. 完成标准和文档同步

交付应说明受影响页面、API 字段/事件、状态归属和兼容行为，并提供相应逻辑测试与浏览器证据。涉及产物规则时核对服务端、聊天和结果库三处；涉及画布时核对保存、导出、恢复和结果应用。

用户操作变化同步[Web 使用指南](../../WEB_UI.md)与[页面交互设计](../../UI_DESIGN.md)，权限、确认或验收语义变化同步对应专项。仅修改文档时检查链接、命令及代码一致性即可；说明未验证的浏览器、媒体格式和真实服务边界，工作区修改不代表已部署。
