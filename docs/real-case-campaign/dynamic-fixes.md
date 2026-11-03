# 真机与部署问题修复记录

本文件保留问题及修复依据；是否通过以逐例报告和复测部署版本为准。

| 编号 | 首次证据 | 问题与归因 | 修复 | 复测 |
| --- | --- | --- | --- | --- |
| F01 | 冻结版本 `3160c42f798c` 完整 Python 门禁 | 测试虚拟环境链接穿过私有发布目录，测试降权账号无法读取；另有页面模板占位符未接线 | 创建独立可读测试源码与依赖副本；使用既有 BASE/PROJECT 模板占位符 | 后续多轮完整门禁通过，详见冻结版本报告 |
| F02 | `202dbcace19c` 首次健康检查、独立 journal 的 API PermissionError | 新 API 账号没有 helpers 源码只读权限 | 部署准备为 API 账号显式授权 helpers/references，只开放代码读取 | 原候选健康检查通过；脚本改动纳入后续冻结版本 |
| F03 | S01 attempt-001 | 测试驱动等待折叠范围面板内部元素可见，未进入执行；属于驱动问题 | 等待元素 attached，再按真实表单展开和操作 | S01 attempt-002 已创建真实工作流 |
| F04 | S01 attempt-002 的 workflow.execution_failed / ValidationError | 模型提案不符合 schema，程序正确暂停但未提供有界纠正反馈 | 提交前 schema 校验，反馈有界字段错误，仍受原轮次和 Token 限制；无效提案不写库 | 新增回归验证先拒绝、再修正、仅一次暂存；真实复测待执行 |
| F05 | S01 attempt-002 final-browser.png | 最终截图早于页面下一次轮询，截图状态与最终 DB 状态不同；属于证据驱动问题 | 截图前等待 DOM workflow ID/status 与实际快照一致 | 后续真实尝试验证 |
| F06 | S01 attempt-002 final-browser.png | 动态页面页头遗漏 app-header 样式类 | 复用已有页头样式 | 后续部署截图验证 |
| F07 | S01 attempt-002 journal 的 command_processed | 成功命令的空 error_code 被日志 identifier 清洗为 invalid，造成误导 | 图事件省略空错误码，补充 new_state 和稳定 reason | 后续部署日志验证 |
| F08 | S01 attempt-003 的 inspect 拒绝 | 专业 Agent 把角色/文件名当成冻结引用；拒绝正确，但缺少有界纠正 | 拒绝读取并解释仅可使用 inspectable_refs；测试保证纠正前没有读文件 | 后续真机复测 |
| F09 | D20 attempt-001 的 workflow spec 与 StepFailure | Manager 提交了只有 flags 的 argv，缺少实际程序 | Task 契约拒绝此命令，并在字段说明与固定提示中明确完整 argv | 后续真机复测 |
| F10 | S01/S05 多次 proposal 校验拒绝 | 字段级校验问题未落入持久化诊断，且 operation 输出角色说明容易误解 | 保存有界 schema 错误，不保存模型原文；明确固定 operation 输出角色由目录提供 | 后续真机复测 |
| F11 | S01 attempt-004、S30 attempt-001 的视觉报告；S05 attempt-002 的最终验收 | Reviewer 已实际 inspect，但结论使用简化引用或未逐字匹配交付 criterion；严格绑定正确拒绝，缺少纠正反馈 | 保持证据绑定，纯校验失败反馈精确已检查引用和 schema 错误，预算内纠正；无效结果不落候选/成功状态 | 新回归测试覆盖错误引用不发布、纠正后才进入候选；待真机复测 |

D20 attempt-002 在 `731b29b3f343` 完成真实专业 Agent 交付；实际浏览器及全维度审计通过。独立 Lanczos 像素参考 MAE=0，真实模型用量与预算结算均为 62,724 tokens。后续发布变更后仍需以新版本复测，不能当作最新源码永久通过。

## 并行 main 测试的影响（保留原基线）

03:25 的 D19 隔离检查发现 main executor a 的 PID 与会话开始不同。只读核对确认：main 原有
`20261031-real60/runs/C29/attempt-007` 在 03:00:34 发出 SIGKILL，03:11:47 完成并恢复该服务；
对应任务 `6409011638e84e4a87aedbc29443088d` 的故障记录与 systemd 停止的 PID 和时间一致。
本分支没有运行该旧驱动，独立故障工具仅允许 dynamic-agent 单元。main 其余进程、配置哈希和
部署版本保持原状。证据保存于 `.cluster/main-concurrent-campaign-event.json`，没有重置原基线。
D19 后续同时记录“相对会话基线”及“本次检查前后”两种进程比较，避免把其他并行测试掩盖成无变化。

| 编号 | 首次证据 | 问题与归因 | 修复 | 复测 |
| --- | --- | --- | --- | --- |
| F12 | S05–S08 四浏览器批次的 HTTP 502；API journal 18 次 storage 503；Nginx no live upstreams | 代理把有界队列的 503 当成实例失活；浏览器原生 lazy 图片仍同时发起大量请求 | 独立代理不因 503 摘除 API；首页缩略图使用每页两请求的有界可取消队列及有限重试 | 真实临时 Nginx 回归、3项 JS 队列回归；7d9b920cb576 上四页实际加载均无5xx/页面错误 |
| F13 | S09 attempt-001 | SAM3 英文短提示限制未在规划契约中完整公开，中文参数直到运行时才被拒绝 | 发现目录公布英文/120字符约束；提案与运行时复用同一校验 | 契约回归通过；待真实 SAM3 复测 |
| F14 | S11 attempt-001；对真实JPEG执行 ffmpeg -ss 0 的独立复现 | 静态 JPEG 在 seek=0 时返回空帧，导致实际有效图片验收失败 | 静态图省略时间定位；固定解码线程上限；保留媒体检查错误码与退出码 | 真实 JPEG 回归通过；待完整真机复测 |
| F15 | S10 attempt-001 | Manager 看不到专业 Agent 固定 JSON Schema，要求了 profile 禁止的 verified_lines 字段 | 目录公布实际 result_schema；拒绝 JSON报告以外的文件与不可能的 required_keys | 图契约定向回归通过；待完整真机复测 |
| F16 | HTTP域名浏览器环境探测 | crypto.randomUUID 在非安全 HTTP origin 不可用，localhost 测试未覆盖该差异 | 复用图片工作台的 getRandomValues 生成32位请求ID方式 | 需在实际本机 HTTP IP 入口运行创建/控制用例复测 |

R01 预跑在两张已调整图的 alpha 验收处暂停：模型额外要求无alpha，但固定能力生成完全不透明RGBA。
这不属于存储或复用成功证据，目标干预边界尚未触发。驱动明确中间图不限制alpha通道存在性，保留原尺寸与真实像素复用检查；不改写失败图或跳过程序验收。

| 编号 | 首次证据 | 问题与归因 | 修复 | 验证 |
| --- | --- | --- | --- | --- |
| F17 | S09 attempt-002 的被拒绝内容验收 | 目标步骤仍显示 reviewing，继续后可能没有可推进工作；错误暂停原因也会被下一次扫描误写成 user_paused | 拒绝时标记目标步骤 paused/quality_failed；禁止直接重审来绕过拒绝；保留真实暂停原因 | 内容拒绝、人工拒绝、错误原因持久性、恢复待确认、并行人工确认及双重验收边界测试通过 |
| F18 | 全透明红色 PNG 的真实预览复现 | JPEG 预览直接丢弃 alpha 后显示隐藏的红色，不能作为可见内容的真实证据 | 固定沙盒内按透明度合成灰色底，再生成有界预览；保留预览底色信息 | 实际全透明 PNG 与 JPEG 解码回归通过 |
| F19 | D13 attempt-003 的冻结采集器试跑 | 日志归档器仍有一处按快照目录推算仓库，导致证据范围校验失败 | 冻结辅助程序显式接收仓库根目录；两处边界校验一致；保留失败审计，补归档日志后清理项目 | D13 attempt-004 全维度审计与清理通过，helper 代码按尝试冻结 |
| F20 | 执行器目录实际使用 image_sync@1，控制器不含供应商环境 | 扩权批准若从控制器环境计算默认实现，会与执行器公布的实现不同 | 扩权绑定从健康执行器目录取得实现；无目录时拒绝确认 | 无控制器凭据的实现冻结回归通过 |
| F21 | 审批命令输入边界审查 | 非法决策标识或缺失授权可能使待处理命令反复失败并阻塞取消 | API与控制器共用严格 DecisionAnswer；坏命令被拒绝后继续处理取消 | 坏审批后取消可完成的回归通过 |

新增证据规则：每次尝试冻结辅助采集器与实际加载的浏览器驱动源码；在删除项目前保存候选/已验收文件、完整验收决定和执行器版本记录。证据不完整时保留项目，不能用删除数据掩盖采样缺口。

| 编号 | 首次证据 | 问题与归因 | 修复 | 验证 |
| --- | --- | --- | --- | --- |
| F22 | C04/C06 的 UniqueViolation | 新规划请求重复使用此前被拒绝的 proposal_id，直接触发数据库唯一约束 | 暂存前验证身份冲突并反馈；快照提供 planning_request_id 和已用ID；旧提案不可改写 | 冲突不写库且不成为数据库异常的回归通过 |
| F23 | C02/C13 的 inspection_frame_empty | 请求精确结尾或超界时间不能得到视频帧，却终止了整个验收 Agent | 视频检查支持小数秒；越界/空帧作为输入反馈，不写检查证据；仍保留技术解码异常 | 实际时间边界回归通过 |
| F24 | C07 的固定文字替换 stderr | Python 3.10 子解释器继承 Python 3.13 的 PYTHONHOME，无法导入 encodings | 仅为固定外部解释器清除父进程 Python 路径环境，禁用用户 site；不修改共享环境 | 真实OpenCV解释器完成文字替换，选区外像素保持不变 |
| F25 | S10/S19/S22 等公开模型回复的 JSONDecodeError | 外层 JSON 格式错误未进入已有的有界纠正循环 | 只接受单个JSON对象，支持完整JSON围栏；非空格式错误在原预算内反馈，空/不完整上游响应仍单独失败 | 非法语法不执行工具、纠正后只暂存一次的回归通过 |
| F26 | Profile.skills 接线审查及 S17/S23 尺寸失败 | 媒体技能只有声明，未实际注入；media.render 的强制长边尺寸不在发现信息中 | 注册并加载动态操作/媒体实现技能，内容摘要冻结；公开render预设尺寸与media.edit准确接口 | 技能注入、内容变更改变策略、未知/越界技能拒绝的回归通过 |

| F27 | S26 attempt-003、S29 attempt-002 的授权快照额度为空 | 上传后的迟到能力响应把用户输入重置为0；素材刷新也可能重选已取消的素材 | 按响应返回时保留当前额度和勾选，忽略过时响应；上传期间禁止提交；驱动检查实际持久化额度 | 9e128b29e49f 的 D13 attempt-005 延迟真实能力响应，额度/勾选/提交禁用/DB额度均通过；其他业务随后复测 |

| F28 | D08/D09 attempt-001 的UI命令已接收但快照仍为旧状态 | 真机驱动把 HTTP202 当作控制完成，批准/恢复之后立即读取旧暂停状态并结束观察 | 驱动等待同一 command_id 的 Orchestrator applied 回执，再检查后续运行；拒绝明确报告 | 同版本重跑 D08/D09，不修改服务端状态 |

| F29 | D14 attempt-001 的错误断言及真实加密拒绝日志 | 驱动假定认证失败透出InvalidTag；既有解密器实际包装为ValueError，环境准备清理后租约拒绝提交诊断表，故障仍在结构化日志中 | 检查实际decrypt异常栈、真实GCM标签单bit破坏哈希、暂停且无交付；不修改解密防线或伪造诊断 | 同版本重跑D14，恢复原对象后完成全维度审计 |

| F30 | R07 attempt-001 的独立交付检查及实际浏览器默认封面 | Orchestrator把Reviewer返回列表首项当主交付；列表并未声明主交付语义，封面排前就抢占视频 | 复用现有artifact_key按媒体/正式文件规则排序，只标记一个主交付；保持审核选定集合与字节哈希。重复同名文件也只标记一个主交付 | 新增真实存储元数据发布回归：封面在前仍选视频、重复显示名不产生多个主交付；增加PNG/JPEG并存的无损主成果回归，共30项定向回归通过；588fe90652ba完整739项Python/46项JS门禁通过，R07 attempt-002及R04 attempt-002真机复测与逐例审查通过 |

F30验证补充：未部署的2a9e候选在完整门禁期间主动中止，以加入PNG/JPEG并存边界，未替换正在运行的9e128部署。中止日志保留，不计作通过门禁。

| F31 | S29 attempt-003 的pre_submit异常、无供应商账本，但诊断声称submission_unknown | 提交前验证失败被统一submit阶段分类覆盖；未发送的请求被误诊为可能已收费 | 固定提交适配器在无调用意图时记录request_sent=false与cause_type；分类/候选失败/控制器/页面保留pre_submit，禁止原样继续；未知提交保持原规则 | 实际小图提交前拒绝且HTTP session未创建的回归、可信标记与不可信日志区分、暂停及继续拒绝回归；41项定向及741项Python/46项JS完整门禁通过；8186e036b815的S29 attempt-004真实触发提交前拒绝，页面/诊断/账本复测通过（业务因模型未准备合法输入仍未交付） |

| F32 | C12 attempt-002 的media.timeline在7.967秒抽不到最后一帧 | 30fps末帧7.966666秒被毫秒格式向上舍入；音轨比视频长时还可能使用错误的末尾边界 | 以视频流时长限界，FFmpeg时间按微秒向下取整；固定解码/编码线程上限，避免读取旧输出 | 两个真实编码回归通过；对归档真机8秒30fps文件重现旧错误，修复后9帧齐全，末帧字节与独立按序号239解码完全一致。待统一部署复测 |

| F33 | C17 attempt-001 的中间暖色母版被要求提前具备后续字幕/Logo | task Reviewer快照和内部任务objective错误使用整轮goal/delivery_targets，未绑定当前步骤标准 | task快照只提供步骤objective和冻结review_criteria，最终验收仍使用全局交付目标；worker与控制器都拒绝越界criterion，pass必须覆盖当前步骤全部标准 | 32项动态图定向回归通过，包括中间步骤不要求未来字幕、越界拒绝不落库、缺少步骤标准不通过、最终目标不变；待统一部署真机复测 |

| F34 | C21/C22/C25 的workflow_agents图像预算ValueError | 固定8张上限未进入Profile发现/冻结契约，第9次检查直接中断整轮，模型无法预先安排检查 | Profile.max_images明确发现并冻结；每次结果报告剩余额度；耗尽时只做元数据探测，在像素解码前返回有界反馈，不伪造检查记录；保留原操作/Token上限 | 47项定向回归通过，覆盖第9次拒绝、最多8张上下文、不解码被拒图片、保留8条证据后允许合法结束；待部署真机复测 |

| F35 | R09 attempt-002 的plan_change：模型把两个素材ID拼成了不存在的ID | 不存在的素材引用被当作可审批扩权，而审批不能创建该素材，导致无效人工阻塞 | stage阶段反馈未知素材，允许模型在原预算内纠正；控制器再次校验项目内存在性；真实存在但未授权的素材仍要求审批 | 46项动态图/gateway定向回归通过，覆盖错误ID纠正后仅暂存一次、无审批、真实未授权素材仍等待确认；待部署复测 |

| F36 | ca80e46f94a7 / S10 attempt-006 独立检查缺少15 | 用例提示仅写全部英文文字，固定验收却含纯数字；真实OCR已识别15，Manager按字面过滤且Reviewer明确核对英文集合 | 明确case提示为全部文字，包括数字；不提供预期答案、不修改原terms/坐标/格式验收，不修改应用代码，保留该次失败 | 同一ca80部署S10 attempt-007重新实际执行并完成逐例审查：原6个terms全保留，JSON文字/坐标与原始OCR完全一致，TXT逐项一致；attempt-006仍保留失败 |

ca80e46f94a7 定向复测：C20 attempt-002、C17 attempt-002 均业务与全维度审查通过，实际中间验收快照仅含对应步骤目标。C12 attempt-003业务通过，但本次模型选择脚本路径，未调用media.timeline；F32旧路径依据真实末帧回归和旧候选复现，不将这次业务成功冒充相同路径触发。S10 attempt-006越过F33旧失败点后触发F36用例范围歧义，保留失败并以同版本明确提示重跑。

| F37 | ca80e46f94a7 / C25 attempt-002 Wrong primary video in UI | 发布阶段按final.mp4命名优先排序，覆盖用户明确的横版主成果，F30的通用媒体排序不足以表达主交付意图 | ReviewDecision新增primary不可变引用；最终多文件通过必须明确选择且属于已检查交付集合，worker/控制器共同验证；发布按引用标记唯一主成果，缺失选择拒绝交付，文件名只排列其余附件 | 49项定向与751项Python（6跳过）/46项JS完整门禁通过，ece94c89373f已部署；尚未执行该版真机回归，原C25程序失败保留 |

| F38 | 2026-11-03 11:49:12共享PostgreSQL重启；独立API/worker/watchdog同时被systemd停止且DB恢复后仍inactive | 独立unit从旧模板继承Requires=video-use-db.service，造成共享依赖停止向独立业务服务传播 | 共享DB改为Wants并保留After；独立media/watchdog强依赖保持。D19增加已安装依赖配置检查及两项专用临时systemd服务的真实停止/恢复验证，不操作main数据库 | 10项部署/身份定向测试通过；两项独立临时systemd服务实测依赖停止/恢复时调用方PID保持3980384，模拟依赖恢复成功，未触及main数据库；333d487bb1f6完整752项Python（6跳过）/46项JS通过并已部署，实际六项服务依赖检查通过；D19 attempt-004在333d487bb1f6实际执行并审查通过：安装依赖配置正确，模拟依赖停止/恢复时调用方PID18581保持不变，真实浏览器交付和全链路状态正常 |

F38现场来源：main在11:49完成独立部署至104943e9e307，数据库与main单元重启时间吻合；本分支没有操作main单元，部署单元测试的stop调用全部被mock。main四份私有配置哈希与原基线相同，版本/PID变化按外部并行部署记录，证据`.cluster/main-concurrent-deployment-event.json`。当时ca80批次已结束，没有把中断期间的运行结果标作通过。此前的全量门禁通过不代表该systemd部署关系正确。

| F39 | 333d487bb1f6 / C22 attempt-003 inspection_probe_failed；留存候选独立复现 | ZIP分段包被当作媒体交给ffprobe，合法归档返回1；旧失败诊断缺少目标引用，无法直接定位是哪次inspect | ZIP按有界目录元数据检查，不解包且明确未验证成员内容/CRC；不支持格式与超限目录受控反馈。媒体故障及输入拒绝保存引用SHA256，便于关联候选 | 留存26项ZIP目录修复复现成功；同次全部MP4/JPEG可正常探测。54项定向中修正测试夹具location后，18项检查器/网关回归通过；完整757项Python（6跳过）/46项JS通过。9446e2e10695已部署，带ZIP目录断言的C22真机复测进行中；旧失败保留 |

| F40 | 9446e2e10695 / C22 attempt-004 的目录统计为0，但完整DB审计有两次24成员检查 | 浏览器驱动从probe返回的简要状态读取inspections，完整检查记录实际保存在internal-final.json | 从完整审计文件读取固定检查证据；原state的错误计数保留，另写archive-boundary-review.json独立校正证据，不改写原尝试 | 实际完整证据由Python及修正后JavaScript分别核对，均为两次archive_directory检查、24个不同MP4、contents_verified=false；后续驱动使用修正逻辑 |

9446e2e10695实机修复闭环：F37的C25 attempt-004已完整通过，branded_master_640x360.mp4明确为唯一primary，另两份同名final.mp4不能抢占；F39的C22 attempt-004由Manager和诊断Agent实际检查24个ZIP视频成员，coverage=archive_directory且contents_verified=false，原媒体探测崩溃未复现。该C22后续在模型生成的FFmpeg命令打开编码器时失败，程序正常暂停并清理，业务失败仍单列。F40驱动摘要读取问题有独立校正证据；两个必测回归项已在open-regressions.json记为resolved。

共享依赖现场事件（2026-11-03 13:18及13:34）：共享PostgreSQL再次被外部重启，应用GET正确返回API_DATABASE_UNAVAILABLE/outcome=unknown；API/watchdog未被依赖关系连带停止，worker异常退出后由systemd恢复。S14、S21已用冻结采集器补采并经认证API清理，原audit_error/cleanup_error保留，另存audit-recovery.json，随后同版本重跑；S22原尝试已完整清理。真机驱动对特定数据库503的GET增加60秒有界重试，只读inspect/final审计遇OperationalError最多3次；每次中断留证。实际本地HTTP验证GET恢复、POST不重发、其他503不盲目重试、到期拒绝，证据`.cluster/harness-read-retry-verification.json`。这属于依赖恢复与测试驱动调整，应用源码仍为9446e2e10695。

| F41 | 9446e2e10695 / R12 attempt-003 | QA驱动将首次工作流暂停也当成“交付后”干预的起点；新版产物通过不足以证明原定边界。首次Reviewer发生ModelServiceError，应用正确保留未知用量预留且未交付 | R10/R12/R16干预前要求succeeded及非空delivery，独立检查器再核对前序工作流；原始state保留，审查将该尝试改为QA程序错误；同应用版本重跑R12 | 实际JS守卫8种状态/交付组合验证通过；独立检查器用真实R10成功审计和R12失败审计分别接受/拒绝，证据`.cluster/harness-prior-delivery-verification.json`。R12 attempt-004同版本复测完整通过：首次正式交付早于新版创建，前置守卫与独立DB检查均通过，最终视频/封面参考MAE0且九维审查完成；F41-R12已关闭。当前R10/R16留存审计也证明前序正式交付成功 |

| F42 | main `4b8a3a8` 对比后，在9446运行包临时沙盒复现capabilities.json为空 | 子文件先挂载、父工作区后挂载将其遮蔽；原测试仅验证文件存在 | 将只读能力目录挂载移到父工作区之后，保留动态分支目录协议；测试实际解析operations、尝试写入并确认仍只读 | 7ef71c64bc34真实降权沙盒定向及完整757项Python（6跳过）/46项JS通过；实际部署命令顺序与JSON读取验证通过，额外回归关闭；97条新版本回归进行中 |
| F43 | main `eb86805` 对比后，在9446运行包实际执行file失败 | 运行包有file/libmagic但缺少公开magic数据文件 | 将magic.mgc只读挂载并纳入运行时内容指纹，测试实际识别PNG | 7ef71c64bc34真实降权沙盒定向及完整门禁通过；部署运行包实际file识别PNG成功，magic哈希验证通过，额外回归关闭；97条新版本回归进行中 |


| F44 | 统一 Run 发布 0f381948c129 / S01 attempt-013、D22 attempt-001 | worker 候选筛选仍查询 run_tasks，独立 PLAN control_jobs 被静默过滤；两个执行器有心跳，零 execution_attempts、零模型/Provider 调用，Run 停在 graph v0 | 筛选改为 run_work_items；新增不创建 agent_turn 的完整候选筛选→dispatch_kind→claim 回归 | 23 项统一身份/两节点/调度定向通过；两条首次失败已完整归档、正常取消并清理，待新部署真机复测，不计通过 |
| F45 | 统一 Run 首批报告生成 | 历史授权证据没有新 run_id 字段，报告渲染直接索引导致 KeyError | 当前授权使用 run_id；缺少该字段的历史记录指向原始证据，不改变应用模型或读取旧运行接口 | 原始证据保留，报告待重新生成 |

| F47 | 3917f259e246 / S01 attempt-014、D22 attempt-002 | Manager直接编写Script，却没有加载声明VIDEO_USE_STEP_INPUTS为dict[str,list[str]]的文档；实际脚本把列表当路径或自猜类型。两条都按pause/never策略停下，未进入诊断修复 | 新增共享script-runtime契约并挂到Manager、Implementer和Diagnosis；示例实际执行验证；明确已授权诊断时模型脚本使用diagnose、有界修订；Final Reviewer提示允许inconclusive，与Schema一致 | 40项技能注入、恢复、Reviewer及两节点回归通过；按工程契约缺漏记录为程序失败，待新部署真机复测 |


| F48 | f0afcad996f7 / 历史revision归档复核 | QA归档器假设输出路径包含 `/output/`，实际固定工具使用attempt子目录，部分历史revision字节未进入离线归档 | 按受控Task路径前缀收集全部历史检查点，并保留每个revision的manifest；原证据不覆盖，故障现场补采明确标记只读采集源码 | D28/D38保留现场已用修正采集器补充检查并完成清理；新版用例重新验证，不用旧结果替代新验收 |
| F49 | f0afcad996f7 / D28 attempt-001、D38 attempt-001 | 合法replacement后的旧Task被work_items排除，graph.get也因此漏解析其JSON字段；公共详情遍历全部历史节点时KeyError/HTTP500 | get解析全部业务/控制记录，调度仍只选active；API显式返回replacement谱系，增加替代后的公共快照回归 | 16项定向及后续83项相关回归通过；原HTTP500和采集失败保留，internal-supplement.json为修复后只读补采，不改变原失败结论 |
| F50 | f0afcad996f7 / 多例74字符“非法JSON”及S05上下文重放 | 内部错误提示恰好74字符；Responses适配器把合法commentary+final_answer一律当多个文本块拒绝，消耗纠正预算。真实gpt-5.6探测复现两个commentary和一个合法inspect最终动作 | 按官方phase选择唯一最终消息，继续拒绝歧义/多个最终消息；分开响应结构与JSON错误，补阶段数量和剩余预算日志 | 两次真实协议探测仅保存脱敏结构/哈希/用量，修复后实际接受最终inspect；严格协议回归通过。S05原响应未保留phase，完整case仍需新版本复测 |
| F51 | f0afcad996f7 / 业务报告结果纠正复核 | 系统提示已要求typed report对象，但冻结business result_contract仍要求JSON字符串/files，产生冲突 | 两处复用同一result_instruction，角色及输出Schema一致；回归检查实际传入模型的冻结上下文 | 83项相关回归通过，等待新版本真机 |

| F52 | cd13667ad301 / D39 attempt-001 | editing_implementer生成了正确候选但反复执行Script直到轮数耗尽；harness未返回实际脚本回执/输出事实，且finalize仍允许script | 反馈实际动作、结构化回执和有界stdout；探索性脚本可分步完成，最终提交才严格验收；finalize禁止继续工具调用并限制剩余时间 | 真实两节点沙盒验证先探测缺失输出→实际写文件→读取哈希/大小/日志回执→提交；相关59项回归通过，待新部署真机 |
| F53 | cd13667ad301 / S05 attempt-011 | 模型给出格式合法但绑定值错误的ManagerResult，服务器正确拒绝，但回显错误未进入同请求内的有界纠正 | 精确区分模型快照字段回显错误与真实失效租约/过期请求；前者拒绝后纠正，后者仍中止 | 回归验证错误绑定从未暂存、同一冻结请求纠正后才暂存；无放宽版本或授权约束 |
| F54 | cd13667ad301 / D28 attempt-002及质量反馈用例复核 | 测试用反馈取消原人工Review，与最初必须本人确认的口径发生模型判断冲突；且驱动没有独立复核后批准新版候选的路径 | 测试保留原Review，先下载并绑定候选哈希、独立检查请求的像素变化，再操作真实approve；技术/Agent未通过时禁止批准。最终Reviewer获得匹配当前候选的用户审批事实 | 独立像素检查正反例通过；审批快照回归发现局部变量覆盖并已修正，59项相关回归通过。原失败保留不改判通过 |

| F55 | ecc1c9fe8290 / D28 attempt-003、D39 attempt-002 | 用户批准已落库但后续Manager未收到事实而重复询问；最终Reviewer只看到当前选中节点，无法核对已保存的原Task规格和替换前依赖，要求重做流程 | 统一提供匹配当前候选的user_reviews；Manager/Final Review提供有界权威history，包含规格、依赖历史、plan选择和replacement，明确退休节点不再作为必需交付；Final Review补失败事实 | 35项授权、替代、API/网关、真实两节点及竞争回归通过；待新版本实际复测，原等待用户结果保留 |
