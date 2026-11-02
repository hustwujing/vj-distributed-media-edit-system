# Harness 合并、部署与完整真实回归报告

完成时间：2026-11-03 03:16:44 CST。发布指纹：`f2fe68121bc7a641fde227876007fb9db72d51dca4e429304cda2569eea4668c`。

77/77 条已实际执行并完成程序正确性核验：70 条交付并核验文件，7 条遇到模型/产物质量问题，程序按约定拒绝交付、暂停或请求确认。后者不能记为成功交付。本轮覆盖范围内没有发现仍未修复的程序错误。没有把模型结果不佳自动归类为程序 bug，也没有使用 case 文本匹配修改产品行为。

原始 60 条中，53 条交付通过，7 条异常处理通过；另外 17 条为复用、干预和真实重新规划验证。

## 合并与部署

独立 worktree 的 Harness / LLM Adapter 改动已合入 main。主目录的既有改动先保存，整合其他窗口的生命周期及转写缓存权限修复；没有只部署本 worktree 的局部代码。当前服务运行冻结目录 `.cluster/releases/f2fe68121bc7`，默认主 Agent 为 gpt-5.6 high。

root 所有的转写缓存不再由低权限 Gateway 直接 chmod，改由校验租约、归属和文件类型的 Broker 管理权限。已核对主目录和部署目录两个修复文件的哈希一致；完整测试包含真正的低权限 Gateway、root 缓存、共享加密对象存储和第二节点恢复。

## 执行轮次与修复

| 阶段 | 发现 | 处理与复测 |
| --- | --- | --- |
| 全量自动化第 1 轮 | 680 项中 1 个旧 RPC 测试绕过 TaskRuntime，却期望原生恢复 | 改用真实租约、检查点与新 attempt；21 项定向复测通过 |
| 全量自动化第 2 轮 | 680/680，0 跳过 | 部署 839082fa9437；真实 API 切换、BOSS 跨节点读取、强杀接管和跨 API 取消通过 |
| 真实场景第 1 轮 | 原始 case 已启动 13 条：11 条交付、1 条模型交付声明被拒绝、1 条 GPU 等待后主动取消；部署后 Harness 检查发现资源低估 | Claude/DeepSeek 实际启动完整沙盒，却曾按 light 预留 256 MB；以统一能力声明同时控制准入与执行，修复 HA-RESOURCE-01 |
| 环境调整 | 原 GPU 候选可用显存不足，任务正确等待 | 节点候选改为可配置 A=1,5 / B=7，仍遵守共享锁与显存准入 |
| 全量自动化第 3 轮 | 686/686，0 跳过；前端 43/43 | 部署 f2fe68121bc7；真实五轮 Harness 新建/恢复/切换全部通过，数据库预留均为 CPU 2 / 4096 MB |
| 真实场景第 2 轮 | 完整 77 条实际执行，含外部文生图、图生视频、文生视频 | 逐项核对程序行为、媒体文件、页面与持久化证据；结果如下 |
| QA 检查器修正 | C04 紧裁切透明 Logo 被“透明面积大于 5%”的额外要求误拒 | 通用检查改为同时存在透明和可见像素，4 项单测通过；同一组产物重新检查，保留误判原记录；未改生产代码 |
| 补充回查 | 52 条已交付原始场景重新进入页面，570 个下载文件核对哈希；R01/R02 补采页面和终态诊断 | 无哈希变化、错误主成果、残留工具转圈或页面 JS 错误；C29 最后单独做故障接管 |

完整 Python 回归耗时 496.02 秒。首次全量失败、第一轮未完成尝试与 QA 误判记录均保留，没有用后续成功覆盖原始失败证据。第一轮 S29 曾因模型漏掉参考图准备而在外部提交前暂停（0 次提交），按原任务重新规划后通过；并非增加产品 case 匹配规则。

## 真实异常与程序反应

| Case | 真实异常 | 程序反应 |
| --- | --- | --- |
| S09 | SAM3/ZIM 修复后仍漏掉头盔、局部 Alpha 不符合要求 | 有界修复后终止，下游未执行，GPU 与本地资源清理 |
| S11 | 模型交付清单重复包含 edit/ 前缀 | 拒绝错误交付声明，保留已产生的 JPEG，不伪报成功 |
| C05 | 初次分割未覆盖头盔 | 额外 GPU 调用前请求授权，保留已有结果，停止下游 |
| C18 | 模型错误理解 media.edit 嵌套参数并声称契约冲突 | 拒绝不合法参数并暂停；独立验证正确 edit_plan 可通过提交与运行绑定，因此是模型参数错误 |
| C20 | 多次修复后仍有明显漏孔/背景碎片 | 预算内修复完成后请求方案变更，不放宽验收、不继续盲重试 |
| C27 | 图片编辑改变杯子形状、位置与构图 | 保留候选和参考图，额外外部重试须确认 |
| C30 | 15 秒真实跑步视频后半段腿脚出画，未达到确认的全身条件 | 已生成图、Logo、视频保留；合成与质量检查步骤未启动，请求额外生成授权 |

暂停场景在保存浏览器、调用与清理证据后，由测试所有者取消自己的 QA 任务，未自动增加付费生成。调用缺少最终回执时显示未知；未知不等于失败、成功或零费用。

## 页面、数据、可观测性与成果核验

原始场景通过真实 Chromium 提交、确认、观察步骤并下载成果，保存页面截图、HTTP 状态和请求关联、最终诊断、调用账本、资源清理及可留存的遥测/事务审计。成功媒体独立解码、核对尺寸时长、像素/音频与内容；异常场景检查等待原因、下游未执行及安全停止。

R01–R16 的规划记录是明确标注的数据库夹具；业务步骤、复用回执、最终 Agent 验收和文件均由实际部署执行。检查复用哈希、未变步骤不再次消耗本地图片额度、变化步骤重算、旧方案拒绝、完成竞争、刷新重进与项目清理。R17 使用真实浏览器自然语言重新规划和确认，不种入成功状态。运行时诊断截图有时间戳；分页截断和缺失回执保留其缺口，不冒称完整。

观测服务在执行期间 alive/ready，数据库与对象存储 available，遥测 dropped=0。生产业务状态在 PostgreSQL；检查点/文件使用共享加密 BOSS。本地缓存与独立观测索引并非业务权威状态。

原始 60 条本轮端到端耗时（含规划、排队、执行、验收；非纯模型耗时）：平均 427.3 秒，中位数 365.5 秒。

## C29：真实故障、修复与接管

在视频母版 FFmpeg 已运行约3.65秒时，对节点 A 执行一次 SIGKILL 并停用自动重启。旧 attempt 过期且清理完成，节点 B 接管；最终母版检查点由 B 的 attempt 提交，revision=2，最终成片和质检也在 B 完成。音乐和 Logo 分支各只执行1次，未因接管或上游修复重复执行。

模型脚本先出现 xfade 时间基错误，系统有限重试后触发第一次结构化修复；接管后的编码又因长分支缓存被内存上限终止，遥测两次明确记录 oom_kill=1。第二次结构化修复改用四段顺序编码再拼接，保留原60秒/1800帧和验收标准，随后成功。最终工作流调用11次（5成功、6失败），重试4次、修复2次，未闭合调用0，16条本地清理记录均complete。这里的脚本修复由被测 Agent 完成，测试操作者没有修改业务脚本或成功状态。

独立解码确认60秒1920×1080/30fps，查看首、中、尾帧；96像素Logo位于(1792,32)，与母版加透明Logo的独立参考比较MAE=2.594；x264内嵌参数确认CRF18，实际执行脚本使用preset slow。左右声道分别与原单声道music比较，相关系数0.999965、增益0.999014；直接比较不同声道布局的单声道下混会得到不同系数，因此不使用该错误口径判断增益。

节点不足告警在故障期间触发；结束后 A 恢复、两节点健康、活跃任务0、所有告警resolved。C29最后12个文件的页面下载/哈希复查通过，加上之前52条570个文件，共53条已交付原始场景、582个文件复查通过。

## Harness / 模型专项补充

已部署环境实际完成 Claude → Claude 原生恢复 → DeepSeek 公开上下文交接 → DeepSeek 原生恢复 → Claude 公开上下文交接五轮，标记信息保留，页面无 JS 错误；每轮调度均预留完整沙盒资源。此前真实 Claude 长中文流式测试宿主 552 次增量、浏览器 502 次正文更新，全文和尾部完整、刷新保留；取消等待测试 Claude/DeepSeek 分别为 0.165/0.133 秒。

DeepSeek 当前固定 SDK 只提供完整消息，尚非逐 token 输出。未宣称完成“外部视频生成中跨 Harness 接管后继续付费合成”的组合验收；这项仍在 TODO 保留。单机两实例验证不等同于跨物理机容灾。公开域名在测试宿主无法解析，因此真实浏览器走同服务 IP 入口 `http://10.194.49.8/video-use`。

## 逐项结果

| Case | 场景 | 结果 | 本轮耗时（秒） | 证据目录 |
| --- | --- | --- | --- | --- |
| S01 | 图片中心裁切 | 交付及程序检查通过 | 113 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S01/attempt-010/state.json) |
| S02 | 图片等比缩小 | 交付及程序检查通过 | 137 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S02/attempt-010/state.json) |
| S03 | 图片顺时针旋转 | 交付及程序检查通过 | 117 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S03/attempt-009/state.json) |
| S04 | 图片水平镜像 | 交付及程序检查通过 | 119 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S04/attempt-009/state.json) |
| S05 | 图片亮度调整 | 交付及程序检查通过 | 131 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S05/attempt-009/state.json) |
| S06 | 图片灰度化 | 交付及程序检查通过 | 116 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S06/attempt-009/state.json) |
| S07 | 图片高斯模糊 | 交付及程序检查通过 | 150 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S07/attempt-009/state.json) |
| S08 | 图标抠透明背景 | 交付及程序检查通过 | 143 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S08/attempt-009/state.json) |
| S09 | SAM3人像分割 | 异常处理通过；未交付 | 364 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S09/attempt-012/state.json) |
| S10 | 图片文字识别 | 交付及程序检查通过 | 367 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S10/attempt-009/state.json) |
| S11 | PNG转JPEG | 异常处理通过；未交付 | 147 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S11/attempt-007/state.json) |
| S12 | WebP缩略图 | 交付及程序检查通过 | 130 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S12/attempt-007/state.json) |
| S13 | 音频精确截取 | 交付及程序检查通过 | 103 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S13/attempt-008/state.json) |
| S14 | 音量降低6dB | 交付及程序检查通过 | 102 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S14/attempt-008/state.json) |
| S15 | 音频首尾渐变 | 交付及程序检查通过 | 138 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S15/attempt-008/state.json) |
| S16 | 视频提取音频 | 交付及程序检查通过 | 122 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S16/attempt-008/state.json) |
| S17 | 视频精确剪切 | 交付及程序检查通过 | 145 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S17/attempt-008/state.json) |
| S18 | 视频等比缩小 | 交付及程序检查通过 | 129 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S18/attempt-008/state.json) |
| S19 | 视频顺时针旋转 | 交付及程序检查通过 | 215 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S19/attempt-007/state.json) |
| S20 | 两倍速保音高 | 交付及程序检查通过 | 160 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S20/attempt-007/state.json) |
| S21 | 移除视频音轨 | 交付及程序检查通过 | 97 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S21/attempt-007/state.json) |
| S22 | 按时刻抽帧 | 交付及程序检查通过 | 136 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S22/attempt-007/state.json) |
| S23 | 视频帧率转换 | 交付及程序检查通过 | 148 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S23/attempt-007/state.json) |
| S24 | 视频黑白效果 | 交付及程序检查通过 | 137 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S24/attempt-007/state.json) |
| S25 | 右上角透明图标 | 交付及程序检查通过 | 186 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S25/attempt-007/state.json) |
| S26 | 真实语音转写 | 交付及程序检查通过 | 143 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S26/attempt-010/state.json) |
| S27 | 真实文生图 | 交付及程序检查通过 | 276 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S27/attempt-008/state.json) |
| S28 | 真实文生视频 | 交付及程序检查通过 | 407 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S28/attempt-008/state.json) |
| S29 | 真实图生视频 | 交付及程序检查通过 | 921 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S29/attempt-015/state.json) |
| S30 | 字幕整理为文字 | 交付及程序检查通过 | 91 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/S30/attempt-007/state.json) |
| C01 | 三段精剪与配乐 | 交付及程序检查通过 | 417 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C01/attempt-008/state.json) |
| C02 | 双片段交叉淡化 | 交付及程序检查通过 | 445 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C02/attempt-007/state.json) |
| C03 | 竖屏字幕与角标 | 交付及程序检查通过 | 1035 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C03/attempt-008/state.json) |
| C04 | 抠图后视频合成 | 交付及程序检查通过 | 1143 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C04/attempt-007/state.json) |
| C05 | 人像分割精修海报 | 异常处理通过；未交付 | 288 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C05/attempt-011/state.json) |
| C06 | 跨图调色批量输出 | 交付及程序检查通过 | 816 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C06/attempt-009/state.json) |
| C07 | OCR驱动文字替换 | 交付及程序检查通过 | 558 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C07/attempt-010/state.json) |
| C08 | 透视校正打印卡片 | 交付及程序检查通过 | 335 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C08/attempt-007/state.json) |
| C09 | 真实图片分层与ORA | 交付及程序检查通过 | 858 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C09/attempt-011/state.json) |
| C10 | 异构素材竖版混剪 | 交付及程序检查通过 | 533 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C10/attempt-007/state.json) |
| C11 | 四宫格视频 | 交付及程序检查通过 | 501 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C11/attempt-007/state.json) |
| C12 | 图片动态幻灯片 | 交付及程序检查通过 | 539 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C12/attempt-007/state.json) |
| C13 | 分段变速与定格 | 交付及程序检查通过 | 645 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C13/attempt-009/state.json) |
| C14 | 倒放混剪 | 交付及程序检查通过 | 737 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C14/attempt-007/state.json) |
| C15 | 旁白配乐压低与字幕 | 交付及程序检查通过 | 1016 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C15/attempt-007/state.json) |
| C16 | 降噪与双格式播客 | 交付及程序检查通过 | 594 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C16/attempt-008/state.json) |
| C17 | 中英字幕品牌短片 | 交付及程序检查通过 | 615 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C17/attempt-007/state.json) |
| C18 | 音画分离后重组 | 异常处理通过；未交付 | 447 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C18/attempt-007/state.json) |
| C19 | 海报动画与透明角标 | 交付及程序检查通过 | 666 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C19/attempt-008/state.json) |
| C20 | 精修人像双背景对照 | 异常处理通过；未交付 | 470 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C20/attempt-010/state.json) |
| C21 | 并行预处理后汇合 | 交付及程序检查通过 | 345 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C21/attempt-007/state.json) |
| C22 | 24片段长程批处理与暂停续跑 | 交付及程序检查通过 | 930 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C22/attempt-008/state.json) |
| C23 | 局部模糊遮挡与合成 | 交付及程序检查通过 | 616 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C23/attempt-007/state.json) |
| C24 | 静音替换配乐与封面 | 交付及程序检查通过 | 487 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C24/attempt-007/state.json) |
| C25 | 多画幅并行交付 | 交付及程序检查通过 | 694 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C25/attempt-008/state.json) |
| C26 | OCR定位后真实AI消除 | 交付及程序检查通过 | 769 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C26/attempt-009/state.json) |
| C27 | 文生图再编辑品牌海报 | 异常处理通过；未交付 | 744 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C27/attempt-007/state.json) |
| C28 | 文生视频竖版包装 | 交付及程序检查通过 | 770 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C28/attempt-007/state.json) |
| C29 | 1080p长片与节点接管 | 交付及程序检查通过 | 1153 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C29/attempt-007/state.json) |
| C30 | 女孩图片到15秒海滩跑步完整视频 | 异常处理通过；未交付 | 783 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/C30/attempt-007/state.json) |
| R01 | 待确认时改交付名称 | 交付及程序检查通过 | 103.81 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/R01/attempt-005/state.json) |
| R02 | 待确认时改尺寸与格式 | 交付及程序检查通过 | 104.98 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/R02/attempt-005/state.json) |
| R03 | 待确认时改素材选择与顺序 | 交付及程序检查通过 | 99.79 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/R03/attempt-004/state.json) |
| R04 | 部分完成时追加输出格式 | 交付及程序检查通过 | 99.67 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/R04/attempt-003/state.json) |
| R05 | 部分完成时重做一个分支参数 | 交付及程序检查通过 | 95.89 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/R05/attempt-003/state.json) |
| R06 | 部分完成时替换一个分支素材 | 交付及程序检查通过 | 90.77 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/R06/attempt-003/state.json) |
| R07 | 导出过程中更换封面 | 交付及程序检查通过 | 161.23 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/R07/attempt-003/state.json) |
| R08 | 导出过程中改变字幕样式 | 交付及程序检查通过 | 138.6 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/R08/attempt-003/state.json) |
| R09 | 导出过程中改画幅和时长 | 交付及程序检查通过 | 183.2 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/R09/attempt-003/state.json) |
| R10 | 交付后追加缩略图 | 交付及程序检查通过 | 150.75 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/R10/attempt-003/state.json) |
| R11 | 验收时拒绝局部效果并重做 | 交付及程序检查通过 | 118.64 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/R11/attempt-003/state.json) |
| R12 | 交付后改变整体交付目标 | 交付及程序检查通过 | 314.61 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/R12/attempt-003/state.json) |
| R13 | 暂停讨论后停止旧版并重做 | 交付及程序检查通过 | 102.76 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/R13/attempt-003/state.json) |
| R14 | 导出完成附近取消并重做 | 交付及程序检查通过 | 101.71 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/R14/attempt-003/state.json) |
| R15 | 重做期间刷新与重新进入项目 | 交付及程序检查通过 | 108.42 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/R15/attempt-003/state.json) |
| R16 | 连续重做V1到V2到V3 | 交付及程序检查通过 | 216.92 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/R16/attempt-003/state.json) |
| R17 | 真实 Jev 执行中修改并重新规划 | 交付及程序检查通过 | 336.4470000267029 | [本轮证据](../.cluster/campaigns/20261031-real60/runs/R17/attempt-003/state.json) |

完整结构化结果见[验收证据](post-merge-real-validation-evidence.json)。原始证据位于数据盘 campaign 目录，服务凭据和浏览器 Cookie 不随报告提交。剩余未开放/未验证能力见[Harness TODO](harness-adapters-todo.md)，不因本次回归通过自动关闭。
