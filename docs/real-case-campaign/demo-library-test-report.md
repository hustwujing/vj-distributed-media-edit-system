# 示例库及后续契约、交互专项验收报告

## 2026-11-12：外部音视频参考能力与 74 项示例库

参考 `/data2/wujing03/sandbox-harness-platform` 的持久参考素材能力，将示例库由 70 项扩展为 74 项。新增四个受控 Operation：`video.generate_from_frames`（首尾帧）、`video.generate_from_references`（图片/视频/音频多模态参考）、`video.edit`（参考视频编辑）、`video.extend`（前向/后向延长）。参考字节先核对冻结输入摘要和真实媒体约束，再发布到目标环境专属 `aniforge/distributed-agent-sandbox/` BOSS 前缀并注册内容寻址 Ark Asset；Provider 只接收 `asset://` URI，长期 BOSS、Ark 和 Provider 凭据不进入沙箱、计划或持久调用句柄。

四种模式均完成真实 Provider、真实页面、实际下载、媒体解码、语义审查、重进终态和清理验证。不同轮次的最新成功证据如下，不拼成同一批“4/4 一次通过”：

| Case | Operation | 最新成功耗时（秒） | 结果与边界 |
|---|---|---:|---|
| `generate-video-first-last` | `video.generate_from_frames` | 415.765 | 首尾帧、1280x720、音轨、5.088 秒容器均通过 |
| `generate-video-edit` | `video.edit` | 570.575 | 只修改天空，保留地面构图和颜色，无音轨 |
| `generate-video-multimodal` | `video.generate_from_references` | 577.469 | 第一候选通过灯塔构图、轻微漂移和可听音轨 |
| `generate-video-extend` | `video.extend` | 1152.224 | `backward` 前序片段经一次受控修订后通过 |

四种模式成功证据的平均耗时为 679.008 秒。最终无介入轮中，多模态第一候选通过；延长的两个候选技术契约均通过，但云层运动方向没有让新增片段末帧衔接源首帧，第三次生成前被有界取消，归类为 Provider 内容失败而非程序失败。系统没有发布不合格片段。证据位于 `/data2/wujing03/demo-library-74-media-reference-r1` 至 `r4`；首次失败、修订和取消记录均保留。

本轮真机暴露并完成四项通用修复：Seedance 含音轨容器时长容差由 ±0.05 秒修正为 ±0.10 秒；明确 `video.extend` 只返回新增片段及 forward/backward 拼接方向；HTTP 409 `InvalidAction.RunningTaskDeletion` 改为延迟取消并持续查询原 Provider ID；示例素材改为语义一致、可独立验证的确定性输入。服务重启后对三条历史 `unconfirmed` 记录执行只查询恢复，全部收敛为 `complete / succeeded`，没有重复 submit。

最终产品提交为 `eb78c53`，正式 release 源码摘要为 `2456e9aec61da8a0ddbd55d56973eb31a17a2717203552d9f6d50e8705b403f5`，配置摘要为 `11ed79c966120ca41982c5e2d6c08ffb1070330588e180602c9f27f085ecf815`，状态 `full_gate / ready`。Python 1714/1714 通过、跳过 6 个显式 GPU 真机项；Node 105/105 通过；74/74 示例完成隔离双 API/S3 发布和加载检查；真实 Nginx 桌面/移动检查均为 74 个唯一示例、0 页面错误、0 横向溢出。

最终 Runtime 镜像与 Skill Bundle：

- Operation：`sha256:74f60f23d2d115e40566c096118d1559c91b818cc2d9da2c28fccb6871474f20`
- Codex：`sha256:93d76f197bde2c2000451dc8686dd35fccef8d048e36002faa3d974dec33a357`
- Claude：`sha256:809bc569cd584ea16ebbf427bee131a4342202e19159f36e06dea4d18e9a7414`
- DeepSeek：`sha256:7e18683ad133c924c3326b0d5f16c9d82c513e730a5b8891a9e387b425409caf`
- Skill Bundle：`sha256:249a22b2cd8fd7f935b9cf60ff41b116028ba158e68ad1e7254aac14ec54b0a7`

## 2026-11-12：70 项示例库 + Native Skill + 真实媒体生成

本轮以并发 8 对扩展后的 70 项示例库完成去重真机执行：44 项既有示例、25 项 Native Skill、1 项同时调用真实图片/视频/音频 Provider 的组合示例。70/70 均已启动；69 项业务通过，0 项程序失败，1 项非程序失败。69 项成功平均耗时 255.072 秒，最长为三媒体组合生成 814.832 秒。主动中断的诊断轮次不计失败，按每个 case 最近一次通过证据去重。

唯一非程序失败为 `skill-anatomy`：两次 Claude Sonnet 5.5 Native Harness Attempt 均在大模型调用中收到 `llmapi.bilibili.co` 的 `RemoteDisconnected`；OpenSandbox、Skill 注入、Node/Chromium 路径、预算和清理正常。该项没有发布半成品，不能归为程序错误或伪报成功。

最终源码提交为 `fe96756669fafa5d4b118e63e9f2f59001d14314`，工作树干净并已推送。正式独立部署 release 为 `39ee2494f41b99a8f25ddf0d6f6c7834d815d67bddf73e62cd3370b79b163e9e`，配置摘要 `9bb450f03b56869e00e6bbecd802ce8bbfdcf25d6a4d6216d9d8c3b4019f2313`，标记 `full_gate / ready`。数据库 `distributed_agent_sandbox`、对象前缀 `distributed-agent-sandbox`、事件前缀 `distributed-agent-sandbox:events:`、端口 20766/20767 均与来源环境隔离。

最终 Runtime 镜像来自同一提交和同一 base：

- Operation：`sha256:d3bd11d82e6b19c3f8865a66492a571cb1670d2c5f06582d7b9d45ee0b1cb390`
- Codex：`sha256:f8c1b1579f554ffa9f3fddcfbb19a04e090d772fc32099696f0a89f8d9be5ec1`
- Claude：`sha256:5bfb7e5843d62eddea6c9945708b8e3771d584c1059b596dd20e9bb7b08db6c5`
- DeepSeek：`sha256:f52a516af4b6be771082208ee016ad36523f057cc8acd1a1b15960c177df2f5e`

最终门禁：Python 1707/1707 通过、跳过 6；Node 105/105 通过；四镜像离线 Runtime 契约通过。第一次全量 Python 门禁因根分区只剩 1.3 GB，上传统一返回 507，造成 10 个失败和 6 个派生超时；清理本项目遗留的 `/tmp/pip-unpack-*`、旧 wheel/验证/浏览器临时目录后恢复至 2.9 GB，相关 7 个模块 38/38 通过，正式部署的隔离全量门禁随后 1707/1707 通过。

公网域名在最终检查时本机 DNS 暂不可解析；通过 `127.0.0.1` 映射同一 `Host: updream-lab.bilibili.co` 走真实 Nginx 配置验证：API health 正常，示例 70 项、Native Skill 25 项、三媒体生成 1 项；桌面 1440x1000 与移动端 390x844 均显示 70 张卡片、0 控制台错误、0 横向溢出，示例弹窗正常。截图位于 `/data2/wujing03/final-ui-desktop.png` 和 `/data2/wujing03/final-ui-mobile.png`。

本轮均为通用修复，没有按 case ID 特判。主要包括：Native Skill/Runtime/Gateway 冻结绑定；Claude 大消息与模型调用预算；Native 工具 PATH；HTML/Office/GIF 统一验收；PPTX/DOCX/XLSX/PDF 结构和视觉审查；文档图片走视觉通道而非 base64 文本；禁网 LibreOffice 二层隔离；Native Skill 受控图片 Gateway；音频数值句柄规范化；音频转写证据；视频 RGB/对比度证据；计划草稿互斥修改与字段别名规范化。

更新：2026-11-09。按用户最新要求，完成失败项修复、定向真机测试和部署后停止，不再重跑44项。当前产品提交为 `1139d01`，独立环境部署就绪。最近完整44项轮次为final-007，42项通过、1项程序失败、1项提示词/契约失败；不能把后续不同版本的定向结果拼成“当前版本44项全通过”。

## 当前部署与验证

- 程序目录：`/data2/wujing03/distributed-agent-sandbox-backend`；命名空间 `sandbox-agent`，独立数据库 `distributed_agent_sandbox`，来源环境未修改。
- 入口：http://updream-lab.bilibili.co/distributed-agent-sandbox-backend/
- 产品提交：`1139d01ff8d5592d23c7cfa486bdbd8fdbd1be51`。
- 冻结源码：`7a6d0039cb4177be1b05522aef74132abfc1b1746f44e7baf4cedcc05091099b`。
- 配置摘要：`4ca12186cbbeadcb0d04499975307dda29bb96a5bb9c69836abba8d61363c407`。
- 部署标记：`targeted_only / ready`。当前版本按用户要求运行关联测试，未冒称完成最新全量门禁。
- 使用 `deploy/sandbox_cluster.sh deploy --targeted-unverified --source-from /data2/wujing03/das-runtime-conversation-8a70f75` 冻结干净1139d01源码；并行工作区未提交改动没有进入部署。source-from只改变源码来源，不改变本环境的数据、配置、端口和命名空间。
- 实际Operation镜像：`127.0.0.1:25001/sandbox-agent/operation-runtime@sha256:845edb042209147228e87a0272e93def12fb10950dcef933c52d94331d69a616`；Profile为 `command-1139d01`。
- Skill Bundle：`sha256:48c7169f4e51e8dd3f4086b3bb3f2faa4c4bd1d75ae8e3e289a9bf0e58ef5e39`。
- 四种Runtime镜像来自同一冻结提交/基础镜像，契约、清单、离线扫描及发布证据在 `/data2/wujing03/das-runtime-conversation-8a70f75/runtime/build/1139d01/`。扫描记录保留依赖风险及与基线的比较，不表示零漏洞。
- 当前CPU Command走OpenSandbox/gVisor；动态专家Agent仍是宿主structured-profile-v4控制循环、工具在bwrap。GPU路径未变。尚未启用原生Claude/Codex/DS Harness；后续切换复用用户配置及现有Profile/adapter，不固定Claude。

## 本次通用修复

| 范围 | 修复及提交 | 验证 |
|---|---|---|
| 方法证据与通过结论冲突 | c224f57增加必填method_use；reference_only/unverified不能与通过并存，worker和controller均复核 | 244项关联检查；真实局部编辑后续正确拒绝仅参考统计的方法替代 |
| 蛇形运行图与耗时 | 1800515按节点依次填满蛇形单元；实时耗时只刷新文本，结束固定时间；同节点保留最新3条Attempt记录 | 实际桌面/窄屏布局、重进、连线、位置标记及默认折叠检查通过 |
| 成果预览无响应 | d5c90be显式预览打开可见弹窗，按视频/音频/图片/PDF/文本分型，未知类型及加载失败明确反馈，关闭停止播放 | 真实MP4播放；类型与错误分支浏览器测试通过 |
| 闲聊误触发业务屏障 | c6c8c60复用Manager处理MESSAGE，直接回复不改变业务序列、方案确认或Task；明确变更才进入安全点重规划，新任务另建未批准Run | MC01、MC02、MC03覆盖规划中、待确认、执行中消息和重进 |
| 用户修改交付契约仍要求旧值 | f29953d、44e039b以明确审批回执及匹配授权版本确定新完整清单，统一名称/格式/角色/数量与旧交付叶节点规则 | MC02改名真实通过；组合变更MC03发现发布器遗漏后继续修复 |
| 发布器遗漏与旧审查复用 | 1139d01发布器名称/格式/报告角色一次读取同一有效契约；每次发布核验当前Review identity，DAG版本未变也不能复用过期审查 | 142项关联回归；干净部署源码106项Python、102项Node通过；MC03真实发布通过 |

交付范围变更仍需明确确认；不能借改名删除画面内容、方法或技术验收要求。时长、尺寸、帧率、字幕/音频、素材、方法、验收、主成果、权限及预算的逐项审查见[契约一致性审查](user-contract-change-review.md)。

完整门禁最近运行于44e039b：1475项Python，0失败、6跳过；102项Node通过。1139d01运行上述关联检查与真机定向验证，未再次运行完整门禁。测试数量属于不同范围，不相加作为去重测试总数。

## 后续定向真机结果

以下是各例最近一次结果，版本不同，不视为同一批全量测试。

| Case | 版本 | 结果 | 耗时（秒） | 主要覆盖 |
|---|---|---|---:|---|
| MC01初始闲聊 | c6c8c60 | 通过 | 8.430 | 真实Manager回复、无业务Task、重进可见 |
| MC02改名与多阶段闲聊 | 44e039b | 通过 | 185.361 | 新名替代旧名、确认前不执行、解释不作废确认、执行中回复、真实预览与清理 |
| MC03组合契约变更 | 1139d01 | 通过 | 183.701 | 同时修改名称、删除报告、尺寸及帧率；保留变速/定格；新审查与发布一致 |

MC02首次在round-002失败：旧名和新名同时必需，258.753秒；已按正常取消流程终止，cleanup=0。MC03首次在round-003-MC03失败：视频与Final Review已按640×360/24fps通过，发布器却从原Run重新要求报告，187.955秒；修复前回归可复现，正常取消、cleanup=0。首次失败证据保留，不覆盖为成功。round-001的MC02浏览器驱动在导航后读取响应造成夹具失败，业务未启动，已修复并单独记录，不计产品失败。

| 独立产物检查 | MC02 | MC03 |
|---|---|---|
| 真实交付 | speed_review.mp4 | speed_multi.mp4（无JSON报告） |
| 尺寸/帧率/时长/帧数 | 960×540 / 30fps / 7秒 / 210帧 | 640×360 / 24fps / 7秒 / 168帧 |
| 字节数 | 1,311,380 | 686,025 |
| SHA-256 | fa44c7445583ceafed8b77000cbf365139f3a5ffbc1ba77f72f16286709cbacc | 9d049c8f5ce5b3d399614b27d1e5b81ad7b863c4969987370021ecbb3a5eaacf |
| 下载摘要、完整解码 | 通过 | 通过 |
| 桌面/窄屏播放、关闭、重进 | 通过 | 通过 |
| 终态及清理 | SUCCEEDED / cleanup=0 | SUCCEEDED / cleanup=0 |

证据位于 `.sandbox-cluster/campaigns/manager-messages/round-001/`、`round-003-MC02/`、`round-004-MC03/`，包含冻结驱动、状态、页面及independent-media.json。最新MC03 Run为 `45422bfc87ed4f8f8e654483c624436f`；项目 `efdcaacda91843ac87181e70525325bb`。

最新公共页面只读专项 `.sandbox-cluster/campaigns/demo-library/ui-graph-1139d01-001/` 在1440/390宽度通过：11个节点，桌面三行蛇形，节点不重叠、连线/拖动/重置正常，默认折叠和耗时显示正常。10条执行位置记录与账本一致：1个OpenSandbox、9个宿主Agent控制执行；另有控制面汇总节点，不虚构沙箱执行。收尾时本环境无ACTIVE/CANCELLING Run，测试清理完成，API、Executor、Orchestrator和Observer服务正常。

## 最近完整44项：final-007（历史版本）

产品版本c505faa，源码 `33588e49afaf25dbdffe4d4623c473cb035c2f83f7390709284be27dc8f0534f`。并发8，44项全部启动及独立审查：42通过、1程序失败、1提示词/契约失败、0未归因。平均334.457秒，中位209.003秒，最长1642.491秒。耗时包括排队、模型、修复、人工/驱动交互与清理检查，不等同单次模型耗时。

| 序号 | Case | 示例 | 本轮结论 | 耗时（秒） |
|---:|---|---|---|---:|
| 1 | image-local | 只改杯子的颜色 | 程序失败 | 1319.883 |
| 2 | image-layers | 拆成可编辑的透明图层 | 提示词/契约失败 | 563.414 |
| 3 | agent-portrait | 一句话，定制竖屏与中文标题 | 通过 | 198.013 |
| 4 | image-text-effects | 自动换行、描边与阴影 | 通过 | 1642.491 |
| 5 | image-adjustment-layers | 用调整图层控制整体色调 | 通过 | 786.544 |
| 6 | image-cutout | 抠出主体，换一个背景 | 通过 | 762.450 |
| 7 | image-select | 框选杯子，精确选择主体 | 通过 | 512.801 |
| 8 | timeline | 把画面、波形和词放在一起 | 通过 | 85.827 |
| 9 | subtitles | 让字幕跟上每一句对白 | 通过 | 168.429 |
| 10 | video-caption-refine | 逐句修正字幕时间与文本 | 通过 | 208.572 |
| 11 | video-version-compare | 同步比较两个视频版本 | 通过 | 495.107 |
| 12 | loudness | 把过小的声音调整清楚 | 通过 | 257.573 |
| 13 | video-chapter-shorts | 从章节生成独立短片 | 通过 | 400.163 |
| 14 | image-product-layout | 统一商品留白与自然投影 | 通过 | 551.861 |
| 15 | image-perspective | 四点校正文档透视 | 通过 | 113.596 |
| 16 | image-erase | 移除文字，补全背景 | 通过 | 293.630 |
| 17 | image-sam3-zim | SAM3 选择主体，ZIM 精修边缘 | 通过 | 256.536 |
| 18 | video-shot-notes | 按时间点批注并局部返工 | 通过 | 509.529 |
| 19 | analysis | 先分析曝光，再决定调色 | 通过 | 309.811 |
| 20 | image-ocr | 识别图片文字和位置 | 通过 | 349.402 |
| 21 | image-layout-guides | 对齐、吸附与等距排版 | 通过 | 613.732 |
| 22 | transcribe | 把对白变成逐词文字稿 | 通过 | 211.306 |
| 23 | local-reverse | 倒放，再循环一次 | 通过 | 165.702 |
| 24 | video-quality-review | 检查黑帧、静音和轨道结构 | 通过 | 221.789 |
| 25 | video-brand-batch | 按品牌规则批量输出视频 | 通过 | 183.051 |
| 26 | local-split | 左右分屏，对比两种色彩 | 通过 | 202.884 |
| 27 | local-geometry | 裁剪镜像，重新安排画幅 | 通过 | 165.241 |
| 28 | overlay | 把第二个画面叠加进来 | 通过 | 429.473 |
| 29 | local-word-edit | 按文字剪辑，让字幕逐词亮起 | 通过 | 464.247 |
| 30 | local-audio-replace | 换掉原声，安排新的声音 | 通过 | 171.697 |
| 31 | local-transition | 用叠化连接两个片段 | 通过 | 162.074 |
| 32 | local-music | 配乐遇到对白，自动轻一点 | 通过 | 208.839 |
| 33 | local-mask | 按时段打码，添加中文说明 | 通过 | 161.605 |
| 34 | cuts | 去掉停顿，让对白更紧凑 | 通过 | 170.161 |
| 35 | grade | 给森林加一点电影感 | 通过 | 145.183 |
| 36 | multitrack | 选对音轨，只转写人声 | 通过 | 153.926 |
| 37 | batch | 多段素材，整理成一份笔记 | 通过 | 198.899 |
| 38 | local-speed | 快慢切换，让节奏有变化 | 通过 | 144.188 |
| 39 | image-replace-text | 擦除原字并替换为新文字 | 通过 | 209.328 |
| 40 | image-watermark-batch | 统一尺寸、格式与水印 | 通过 | 86.290 |
| 41 | image-color-match | 参考另一张图统一色彩 | 通过 | 76.193 |
| 42 | image-mask-refine | 清理蒙版边缘与孔洞 | 通过 | 88.095 |
| 43 | local-audio-clean | 提取对白，减轻持续底噪 | 通过 | 87.406 |
| 44 | image-auto-adjust | 自动白平衡、色阶与曲线 | 通过 | 209.168 |

逐例证据及统计位于 `.sandbox-cluster/campaigns/demo-library/final-007/round-metrics.json`。native-015对44个原生页面检查均通过；页面通过不将业务失败改成成功。独立检查包括实际产物、方法、页面、重进和清理；相同来源/目标/字节的内容证明允许复用，本次运行与方法另核验。模型自评或系统SUCCEEDED不单独作为业务成功依据。

## 两个历史失败的后续与边界

- image-local：合法修订被历史候选输入比较误拒绝，8c8952b通用修复并通过174项关联测试；round-020又发现方法证据只作参考却被审查通过，c224f57修复声明/结论冲突。round-021真实复测中该错误被正确拒绝，后续合法替换可以执行，但实际生成画面出现明显接缝，修复预算到限后停止，归为非程序质量失败，616.249秒。未发布不合格成果，具体选项、取消、重进及cleanup=0通过。该例不计业务成功。
- image-layers：错误把RGBA要求扩大成每层必须含透明区域，d80b4a2纠正契约解释，13项关联检查通过。round-020新契约不再含该错误要求，但三次真实生成未满足已确认语义分层，归为供应商质量失败，不计业务成功。
- round-020共2项：0通过、1审查协议失败、1非程序质量失败，平均933.213秒。首次失败与后续质量失败都保留。
- 按用户后续指令，没有再运行完整44项，也没有继续反复生成以上质量失败案例。当前交付范围是通用修复、受影响契约/交互专项通过及部署，不能据此宣称44项最终全通过。
- 全部修复按通用契约/控制/显示逻辑实现，没有按示例ID特判。方法证据字段限制结构化声明一致性，不能形式化证明任意脚本的数据流，仍保留独立审查职责。
