# 示例库44项真机验收报告（进行中）

截至2026-11-09，最近完成的完整轮final-005已执行并独立验收全部44项：40项业务成功，2项程序/交付失败，1项明确可优化的提示词问题，1项模型质量失败。该轮不是最终无缺陷轮。三项通用修复已提交并部署定向版本144ca27，round-018正在复测时间线、字幕和文字包；后续仍需最新统一版本完整44项。下文final-004逐例表为保留的历史基线，不冒称当前最终结果。

## 最近完整轮final-005

冻结8a4b2f3，源码摘要`407e30e7a126027bf2678f76abe237097139a757451bbf6164f1c4282afe2364`。完整门禁运行1438项Python（0失败、6跳过）、92项Node（全部通过）。真机平均304.599秒、中位245.914秒、最长1171.061秒。逐例内容、页面、清理及账本见`.sandbox-cluster/campaigns/demo-library/final-005/round-metrics.json`，版本比较原生页面补充`native-012/`。

| 失败项 | 归因 | 通用修复/处理 |
|---|---|---|
| timeline | 原生时间线为防拥挤跳过距离过近的词，实际漏词 | 90586d2：按真实字宽分行，保留全部有效词；密度/像素超限明确失败 |
| subtitles | 将Planner选定实现误当作用户指定方法，且当前操作缺少大写格式参数 | 90586d2：区分方法约束与普通实现选择；uppercase统一作用于烧录、高亮和SRT，源转写/时间不变 |
| image-text-effects | 渲染器依赖source.png但未交付，也不读取ORA内原图；实际复放FileNotFoundError | 144ca27：干净目录、仅交付文件复放，显式检查输入依赖闭合 |
| image-local | Qwen多次改变几何/光影；达到修复预算 | 保留模型质量/规划协议失败；未换方法、未发布不合格结果，具体错误/正常取消/重进/cleanup0已验证 |

相关模块初跑62项，仅新增字幕fixture截断单词引起1项错误；修正fixture后21项local_editing全部通过。时间线、边界、Profile、Runtime复核12项通过；交付依赖策略7项通过。所有产品改动均为通用规则/能力，没有按示例ID分支。

## 版本、环境与统计口径

- 程序目录：`/data2/wujing03/distributed-agent-sandbox-backend`；命名空间sandbox-agent；来源`distributed-agent-system`未改动。
- final-004冻结源码4d7e6a3，部署摘要`6ad96098d0f61b5c7902d767e60cfdd6ef0c36700935524bf90fd716c344c283`，full_gate：1435项Python测试通过、6项跳过，92项Node测试通过。
- 示例edition：`7a0386c715bb1de9803cd5b61949f0e49c7f1e414c1cc1a946dca409ac78c585`；目录摘要`29cababf99ec87f93c952f76656cf7d99b2f67dae6b44ef36c8ff2d167fa3db0`。
- 并发上限8。五个角色均claude55 / claude-sonnet-5-5 / high；控制角色buffered，Implementer stream。
- CPU受支持Command实际走OpenSandbox/gVisor，CPU Agent仍local_bwrap，GPU原生工具保留本机隔离；未声称所有任务均在OpenSandbox中运行。
- 每例耗时从真实页面导入到终态和清理检查，包含排队、模型调用、修复及测试操作者答复等待。平均354.825秒，中位232.625秒，最长1555.284秒。
- 先验收真实产物再计通过；不以系统SUCCEEDED或模型自评代替独立验收。已验证的相同来源/要求/产物字节可复用内容证明，当前运行、方法、浏览器与清理另行核对。

## 本轮发现和通用修复

| 问题 | 根因与修复 | 验证 |
|---|---|---|
| 字幕报告未发布，错误要求用户缩小范围 | 审查读取目标字段位置与发布不同，明确要求的report产物又被过滤；286ecf5统一目标读取、附属交付资格、真实范围变更识别及具体交付错误 | 190项相关测试；真实失败快照在新校验下明确拒绝缺少JSON的选择；真机复测中 |
| 最终产物未保持指定模型方法 | 历史调用与当前产物来源混淆；3e4d06e明确方法需作用于最终处理链，投影当前recipe/input/provider回执，并绑定审查身份 | 95项相关测试；真机复测中；不为Qwen/SAM2等某个case增加分支 |
| 响度用例审查模型非法回复 | 模型反复提交非法主产物枚举和遗漏必需任务，已有具体纠错、有界停止 | 不放宽契约，不把错误结果发布；保留模型协议失败 |
| 本地模型被要求提供外部回执 | d9e4568提供注册implementation/lifecycle/model_profile、外部副作用标识及上游来源；空外部回执不表示SAM2等本地模型未执行 | 92项相关测试；round-017验证中 |
| 步骤审查建议导致方法被替换 | 8a4b2f3限定审查/诊断建议的权限，保留用户要求的方法；取候选色相统计不等于合成其编辑像素 | 7项Profile/Runtime检查；round-016的错误替代结果已被最终审查拦截而未发布 |

round-017-delivery通过真实任务设置明确要求`subtitles_v2.srt`和`subtitle_check.json`。两者进入确认方案、最终授权和实际下载，SRT时间与缓存一致，JSON保留原转写SHA并区分阅读速度提示与实际修改；完成、重进、终态流及cleanup0均通过，耗时203.244秒。这是单独的契约变体，不能替代完整44项轮次。

## final-004逐例结果

| ID | 示例 | 结果 | 秒 |
|---|---|---|---:|
| image-adjustment-layers | 用调整图层控制整体色调 | 通过 | 590 |
| image-text-effects | 自动换行、描边与阴影 | 通过 | 949 |
| image-erase | 移除文字，补全背景 | 通过 | 312 |
| image-layers | 拆成可编辑的透明图层 | 通过 | 697 |
| image-product-layout | 统一商品留白与自然投影 | 通过 | 359 |
| video-shot-notes | 按时间点批注并局部返工 | 通过 | 291 |
| local-word-edit | 按文字剪辑，让字幕逐词亮起 | 通过 | 185 |
| video-brand-batch | 按品牌规则批量输出视频 | 通过 | 211 |
| image-replace-text | 擦除原字并替换为新文字 | 通过 | 125 |
| image-layout-guides | 对齐、吸附与等距排版 | 通过 | 261 |
| video-version-compare | 同步比较两个视频版本 | 通过 | 686 |
| video-chapter-shorts | 从章节生成独立短片 | 通过 | 464 |
| video-caption-refine | 逐句修正字幕时间与文本 | 程序失败 | 975 |
| image-perspective | 四点校正文档透视 | 通过 | 320 |
| image-local | 只改杯子的颜色 | 方法契约/提示词问题 | 501 |
| image-sam3-zim | SAM3 选择主体，ZIM 精修边缘 | 通过 | 298 |
| local-music | 配乐遇到对白，自动轻一点 | 通过 | 181 |
| local-audio-replace | 换掉原声，安排新的声音 | 通过 | 184 |
| local-audio-clean | 提取对白，减轻持续底噪 | 通过 | 90 |
| loudness | 把过小的声音调整清楚 | 非程序失败 | 481 |
| agent-portrait | 一句话，定制竖屏与中文标题 | 通过 | 223 |
| subtitles | 让字幕跟上每一句对白 | 通过 | 483 |
| cuts | 去掉停顿，让对白更紧凑 | 通过 | 159 |
| local-reverse | 倒放，再循环一次 | 通过 | 242 |
| transcribe | 把对白变成逐词文字稿 | 通过 | 242 |
| batch | 多段素材，整理成一份笔记 | 通过 | 145 |
| grade | 给森林加一点电影感 | 通过 | 159 |
| overlay | 把第二个画面叠加进来 | 通过 | 189 |
| timeline | 把画面、波形和词放在一起 | 通过 | 578 |
| analysis | 先分析曝光，再决定调色 | 通过 | 286 |
| multitrack | 选对音轨，只转写人声 | 通过 | 153 |
| local-speed | 快慢切换，让节奏有变化 | 通过 | 138 |
| local-transition | 用叠化连接两个片段 | 通过 | 181 |
| local-geometry | 裁剪镜像，重新安排画幅 | 通过 | 193 |
| local-split | 左右分屏，对比两种色彩 | 通过 | 199 |
| local-mask | 按时段打码，添加中文说明 | 通过 | 169 |
| image-cutout | 抠出主体，换一个背景 | 通过 | 1555 |
| image-select | 框选杯子，精确选择主体 | 方法契约/提示词问题 | 1264 |
| image-mask-refine | 清理蒙版边缘与孔洞 | 通过 | 92 |
| image-auto-adjust | 自动白平衡、色阶与曲线 | 通过 | 87 |
| image-color-match | 参考另一张图统一色彩 | 通过 | 110 |
| image-watermark-batch | 统一尺寸、格式与水印 | 通过 | 111 |
| image-ocr | 识别图片文字和位置 | 通过 | 271 |
| video-quality-review | 检查黑帧、静音和轨道结构 | 通过 | 221 |

## 角色调用耗时

以下为该轮已结束的LLM调用账本，不等于整个case耗时；请求成功也不代表模型输出符合协议。

| 角色 | 调用数 | 平均秒 | 最大秒 |
|---|---:|---:|---:|
| delivery_reviewer | 125 | 12.61 | 36.17 |
| editing_implementer | 91 | 47.76 | 386.56 |
| media_planner | 236 | 10.51 | 35.59 |
| visual_quality_reviewer | 135 | 10.92 | 37.86 |

## 用户体验与边界

真实页面检查覆盖：导入全部可见要求、空交付选项由方案补充、确认前没有业务Task、具体问题/错误可见、实际成果预览下载及SHA、完成后重进、终态流稳定和cleanup=0。失败取消项另补查取消后的重进终态。

内容验收包含独立媒体探测与全解码、时间/像素对比、原文时间戳比对、分析数值重算、实际JSON+渲染器参数重放。三个编辑包在断网、只读、非root容器验证；不把自定义ORA字段称为原生参数能力。批量示例当前只有一份演示输入，不能据此宣称大批量容量通过。生成分层是模型重建图层，不保证与原图逐像素重合。SVG运行时不支持部分表现特性时明确返回能力不足，不报告虚假空白内容。

独立环境证据：`.sandbox-cluster/campaigns/demo-library/final-004/`，每例`review.json`、`state.json`、`final-run.json`、截图、实际下载产物与`independent/`；全轮账本`round-metrics.json`。原生版本比较补充证据`native-011/`。Cookie状态文件是私有凭据，不进入报告或提交。

## 尚未完成

关联复测、四张同版本镜像统一验证及最终full_gate部署、最新冻结完整44项轮次和原生页面回归。最终结案将保留此轮及全部首次失败，不把跨版本成功拼成一个完整通过轮。

按用户追加顺序，完成上述验收与报告后再实现：运行图在容器边界折行、左右交替的蛇形布局；聊天中的Agent执行步骤默认折叠并保留手动展开。方案确认及必需用户介入内容仍须可见。这两项尚未混入当前测试版本。
