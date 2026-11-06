# 119 条真机 case 最终测试报告

本报告给出 `feature/unified-run` 在隔离部署上的最终验收结论。逐尝试的浏览器截图、日志、数据库、Redis、BOSS、候选归档、用量、租约和清理明细见 [动态测试证据索引](dynamic-test-report.md)。历史失败不会被复测覆盖；下表使用每个 case 的最新尝试作为最终结果，并在限制章节保留未触发边界。

## 最终结论

| 指标 | 结果 |
| --- | ---: |
| case 总数 | 119 |
| 最新业务/协议成功 | 98 |
| 非程序失败且程序处理正确 | 21 |
| 程序错误 | 0 |
| 待归因 / 运行中 / 未执行 | 0 / 0 / 0 |
| 最新结果成功率 | 82.35% |
| 程序正确性通过率 | 100% |

完整冻结轮 `cb27fbdd8723-full119-risk-20261106T120305` 从零执行了全部 119 条，得到 93 条成功、26 条非程序失败、0 条程序错误。随后只复测失败项，并在修复不可达 evidence 依赖后复测 C15；按各 case 最新尝试收敛为上表结果。用户确认纯模型波动无需无限重试，因此 21 条非程序失败保留原始结论，不继续增加提示词复杂度。

最终源码摘要为 `1db761cad5176417ce3565d066864d789ad9a552b13605fc7ee4fe064fe44112`，部署门禁为 980 项 Python 全通过、6 项显式 GPU/OCR skip，59 项 Node 全通过。隔离实例 10 个服务健康，入口为 `/distributed-agent-system/`。

## 冻结配置

| 项目 | 配置 |
| --- | --- |
| 专家角色模型 | Claude Sonnet 5.5，high，由服务端配置并按 Run 冻结 |
| 沙盒 Implementer | Claude 4.6 Sonnet，high，可由页面选择的登记预设 |
| Profile harness | `structured-profile-v4` |
| 模型协议 | `plan_draft_v1_strict_preferred`，明确不支持时回退，始终执行本地校验 |
| 输入预算 | `claude_bpe_estimate_v1`；真机 case Token 预算乘数 2 |
| 执行容量 | 2 个执行节点、每节点 4 个沙箱，总准入 8；普通浏览器 case 并发 6 |
| 隔离 | 独立 PostgreSQL 数据库、Redis 前缀、BOSS 前缀、加密密钥、端口、服务 UID 与运行目录；main 未被操作 |

## 程序修复与验证

本轮最终修复集中在通用契约，不包含按 case ID 的产品分支：

- 模型不能声明 `acceptance.source=technical`；所有模型验收降为用户业务要求，宿主按真实输出角色逐文件生成技术验收。
- 自动修复阶段可以新增受限的 `media.evidence` Operation，不能借此新增无关业务 Agent 或改写冻结验收。
- 暂停/失败 Task 的候选取证必须使用带 task、revision、artifact、SHA 和 role 的 `repair_candidate`。普通 step 引用只允许等待可达的已验收输出；编译器和图校验器双重拒绝不可达 evidence 节点。
- 交付文件名、输入批次、successor 截止点、Control Job 角色、真实媒体 delivery stage 和中间格式要求由宿主编译。
- Reviewer、Manager 和 Diagnosis 的错误保持有界；无合法结论时停在安全出口，不允许用户覆盖程序或 Agent Review 失败。

C04 真机验证了 technical authority 修复；C14、C15、C17 验证了自动补证；最终 C15 在修复版上执行了 3 个 evidence Task，内容复审仍拒绝，但没有再出现永久 blocked evidence 与 ACTIVE 空转。相关定向回归 139 项通过，最终完整门禁 980/59 通过。

## 非程序失败

21 条失败均满足：内部审计 `program_passed=true`；错误或证据不足未被提升为正式交付；重试、修订、模型调用和外部副作用保持有界；浏览器状态与数据库一致；清理完成或按外部未知协议明确保留。

主要类型：

- Manager/Reviewer 未在有界轮次内提交合法计划或最终决定，包括错误 evidence ref、请求用户输入和 turn/contract quota 耗尽。
- editing agent 脚本或内容不满足其冻结验收，例如 OCR 文本格式、临时 WAV muxer、蒙版像素和局部 Alpha 修改。
- SAM3 对指定人像返回无匹配目标；系统未接受空蒙版。
- 外部调用结果未知或轮询失败；系统保留唯一 request key，禁止重复提交。

提示词和协议已采用状态相关小命令、宿主派生字段、联合 Schema、PlanDraft、精确错误反馈、受限证据 Operation 和有界纠错。继续为单次模型错误追加规则会增加上下文和遵循负担，因此没有做 case 特例或无限提示词修补。

## 外部调用与清理

Wan 调用 `2c79a0ca-d92f-4507-ae11-4d3cfa046bcb` 通过官方 operator reconcile 查询原 external ID，确认上游 `succeeded`，未重复生成；随后删除保留的 D35 QA 项目，BOSS、Redis 与节点缓存清理通过。

D16、D27 和最新 C27 的同步图片调用停在 `submitting/submission_unknown`，没有 external ID。该 Provider 类型不支持查回；operator CLI 明确拒绝无 ID reconcile。系统保留唯一请求意图、未知 usage/cost 和专用 QA 项目，不声称上游已取消，也不重提请求。这是协议要求的审计保留，不是本地租约或清理泄漏。

## 未完成边界

以下 case 的最新尝试未完整触发业务模型负责的必测边界；程序均正确拒绝或停止，且没有程序错误：

- R09：初始模型在 export 干预点前进入等待；较早同版本尝试已覆盖 successor 画幅变更，但最终 Reviewer 未收敛。
- R14：模型两次未输出测试要求的完成 marker，无法证明取消发生在 durable checkpoint 边界。
- R16：V1/V2 执行，模型两次在 V3 前请求用户输入。
- D30：模型未生成带单像素孔洞的合法源蒙版，局部修复比例检查未运行。
- D36：同 Run 反馈与 revision provenance 正常，但模型输出未产生预期 59 个局部 Alpha 像素变化。
- D40：Planner 两次在创建 audio/evidence Task 前耗尽 turn quota。
- D41：候选与 alternate 视频均生成，最终模型未提交唯一 alternate 交付决定。

这些限制是模型内容或命令选择问题。现有程序行为均为安全停止、拒绝错误结果、保留审计并清理；根据最终验收口径，不再无限复跑。

## 逐 case 最终结果

| ID | Case | 最新结果 | 必测边界 | 最新证据 | 根因/说明 |
| --- | --- | --- | --- | --- | --- |
| S01 | 图片中心裁切 | 成功 | 是 | attempt-035 | 业务与程序审查通过 |
| S02 | 图片等比缩小 | 成功 | 是 | attempt-024 | 业务与程序审查通过 |
| S03 | 图片顺时针旋转 | 成功 | 是 | attempt-025 | 业务与程序审查通过 |
| S04 | 图片水平镜像 | 成功 | 是 | attempt-024 | 业务与程序审查通过 |
| S05 | 图片亮度调整 | 成功 | 是 | attempt-028 | 业务与程序审查通过 |
| S06 | 图片灰度化 | 成功 | 是 | attempt-023 | 业务与程序审查通过 |
| S07 | 图片高斯模糊 | 成功 | 是 | attempt-022 | 业务与程序审查通过 |
| S08 | 图标抠透明背景 | 成功 | 是 | attempt-022 | 业务与程序审查通过 |
| S09 | SAM3人像分割 | 非程序失败 | 是 | attempt-021 | sam3_text_selection_empty_after_specific_prompt |
| S10 | 图片文字识别 | 成功 | 是 | attempt-023 | 业务与程序审查通过 |
| S11 | PNG转JPEG | 成功 | 是 | attempt-021 | 业务与程序审查通过 |
| S12 | WebP缩略图 | 成功 | 是 | attempt-021 | 业务与程序审查通过 |
| S13 | 音频精确截取 | 非程序失败 | 是 | attempt-021 | reviewer_again_failed_to_close_trim_identity_with_completed_window_evidence |
| S14 | 音量降低6dB | 成功 | 是 | attempt-030 | 业务与程序审查通过 |
| S15 | 音频首尾渐变 | 成功 | 是 | attempt-025 | 业务与程序审查通过 |
| S16 | 视频提取音频 | 成功 | 是 | attempt-022 | 业务与程序审查通过 |
| S17 | 视频精确剪切 | 成功 | 是 | attempt-022 | 业务与程序审查通过 |
| S18 | 视频等比缩小 | 成功 | 是 | attempt-019 | 业务与程序审查通过 |
| S19 | 视频顺时针旋转 | 成功 | 是 | attempt-018 | 业务与程序审查通过 |
| S20 | 两倍速保音高 | 成功 | 是 | attempt-019 | 业务与程序审查通过 |
| S21 | 移除视频音轨 | 成功 | 是 | attempt-018 | 业务与程序审查通过 |
| S22 | 按时刻抽帧 | 成功 | 是 | attempt-019 | 业务与程序审查通过 |
| S23 | 视频帧率转换 | 成功 | 是 | attempt-017 | 业务与程序审查通过 |
| S24 | 视频黑白效果 | 成功 | 是 | attempt-018 | 业务与程序审查通过 |
| S25 | 右上角透明图标 | 成功 | 是 | attempt-018 | 业务与程序审查通过 |
| S26 | 真实语音转写 | 成功 | 是 | attempt-019 | 业务与程序审查通过 |
| S27 | 真实文生图 | 成功 | 是 | attempt-017 | 业务与程序审查通过 |
| S28 | 真实文生视频 | 成功 | 是 | attempt-019 | 业务与程序审查通过 |
| S29 | 真实图生视频 | 成功 | 是 | attempt-018 | 业务与程序审查通过 |
| S30 | 字幕整理为文字 | 成功 | 是 | attempt-019 | 业务与程序审查通过 |
| S31 | 保留静音音轨 | 成功 | 是 | attempt-013 | 业务与程序审查通过 |
| C01 | 三段精剪与配乐 | 成功 | 是 | attempt-043 | 业务与程序审查通过 |
| C02 | 双片段交叉淡化 | 成功 | 是 | attempt-029 | 业务与程序审查通过 |
| C03 | 竖屏字幕与角标 | 成功 | 是 | attempt-029 | 业务与程序审查通过 |
| C04 | 抠图后视频合成 | 成功 | 是 | attempt-027 | 业务与程序审查通过 |
| C05 | 人像分割精修海报 | 非程序失败 | 是 | attempt-036 | sam3_text_selection_empty_after_specific_prompt |
| C06 | 跨图调色批量输出 | 成功 | 是 | attempt-024 | 业务与程序审查通过 |
| C07 | OCR驱动文字替换 | 非程序失败 | 是 | attempt-035 | model_script_again_mismatched_declared_ocr_text_acceptance |
| C08 | 透视校正打印卡片 | 成功 | 是 | attempt-022 | 业务与程序审查通过 |
| C09 | 真实图片分层与ORA | 成功 | 是 | attempt-023 | 业务与程序审查通过 |
| C10 | 异构素材竖版混剪 | 成功 | 是 | attempt-020 | 业务与程序审查通过 |
| C11 | 四宫格视频 | 成功 | 是 | attempt-021 | 业务与程序审查通过 |
| C12 | 图片动态幻灯片 | 成功 | 是 | attempt-021 | 业务与程序审查通过 |
| C13 | 分段变速与定格 | 成功 | 是 | attempt-021 | 业务与程序审查通过 |
| C14 | 倒放混剪 | 成功 | 是 | attempt-021 | 业务与程序审查通过 |
| C15 | 旁白配乐压低与字幕 | 非程序失败 | 是 | attempt-023 | content_review_rejected_after_reachable_evidence_and_bounded_revision |
| C16 | 降噪与双格式播客 | 成功 | 是 | attempt-031 | 业务与程序审查通过 |
| C17 | 中英字幕品牌短片 | 成功 | 是 | attempt-022 | 业务与程序审查通过 |
| C18 | 音画分离后重组 | 成功 | 是 | attempt-022 | 业务与程序审查通过 |
| C19 | 海报动画与透明角标 | 成功 | 是 | attempt-022 | 业务与程序审查通过 |
| C20 | 精修人像双背景对照 | 非程序失败 | 是 | attempt-016 | sam3_text_selection_empty_after_specific_prompt |
| C21 | 并行预处理后汇合 | 成功 | 是 | attempt-017 | 业务与程序审查通过 |
| C22 | 24片段长程批处理与暂停续跑 | 非程序失败 | 是 | attempt-023 | manager_nonconvergence_after_sixteen_segments_completed_again |
| C23 | 局部模糊遮挡与合成 | 成功 | 是 | attempt-013 | 业务与程序审查通过 |
| C24 | 静音替换配乐与封面 | 成功 | 是 | attempt-012 | 业务与程序审查通过 |
| C25 | 多画幅并行交付 | 成功 | 是 | attempt-020 | 业务与程序审查通过 |
| C26 | OCR定位后真实AI消除 | 成功 | 是 | attempt-012 | 业务与程序审查通过 |
| C27 | 文生图再编辑品牌海报 | 非程序失败 | 是 | attempt-014 | external_image_generation_submission_unknown |
| C28 | 文生视频竖版包装 | 成功 | 是 | attempt-011 | 业务与程序审查通过 |
| C29 | 1080p长片与节点接管 | 非程序失败 | 是 | attempt-010 | editing_model_revisions_repeated_invalid_wav_tmp_muxer_path |
| C30 | 女孩图片到15秒海滩跑步完整视频 | 成功 | 是 | attempt-015 | 业务与程序审查通过 |
| R01 | 待确认时改交付名称 | 非程序失败 | 是 | attempt-015 | delivery_reviewer_nonconvergence_after_successful_rename_revision |
| R02 | 待确认时改尺寸与格式 | 成功 | 是 | attempt-011 | 业务与程序审查通过 |
| R03 | 待确认时改素材选择与顺序 | 成功 | 是 | attempt-010 | 业务与程序审查通过 |
| R04 | 部分完成时追加输出格式 | 成功 | 是 | attempt-021 | 业务与程序审查通过 |
| R05 | 部分完成时重做一个分支参数 | 成功 | 是 | attempt-013 | 业务与程序审查通过 |
| R06 | 部分完成时替换一个分支素材 | 成功 | 是 | attempt-011 | 业务与程序审查通过 |
| R07 | 导出过程中更换封面 | 成功 | 是 | attempt-024 | 业务与程序审查通过 |
| R08 | 导出过程中改变字幕样式 | 成功 | 是 | attempt-016 | 业务与程序审查通过 |
| R09 | 导出过程中改画幅和时长 | 非程序失败 | 否 | attempt-020 | initial_model_waited_for_user_before_export_intervention_boundary |
| R10 | 交付后追加缩略图 | 成功 | 是 | attempt-010 | 业务与程序审查通过 |
| R11 | 验收时拒绝局部效果并重做 | 成功 | 是 | attempt-010 | 业务与程序审查通过 |
| R12 | 交付后改变整体交付目标 | 非程序失败 | 是 | attempt-018 | successor_final_review_requested_input_after_video_and_cover_succeeded |
| R13 | 暂停讨论后停止旧版并重做 | 成功 | 是 | attempt-010 | 业务与程序审查通过 |
| R14 | 导出完成附近取消并重做 | 非程序失败 | 否 | attempt-010 | model_export_implementation_again_omitted_required_output_ready_marker_boundary |
| R15 | 重做期间刷新与重新进入项目 | 成功 | 是 | attempt-008 | 业务与程序审查通过 |
| R16 | 连续重做V1到V2到V3 | 非程序失败 | 否 | attempt-012 | second_version_model_again_requested_input_before_required_third_run |
| R17 | 真实浏览器执行中修改与重新授权 | 成功 | 是 | attempt-013 | 业务与程序审查通过 |
| D01 | 真实 Manager 多轮扩图 | 成功 | 是 | attempt-011 | 业务与程序审查通过 |
| D02 | 并行分支完成后触发汇合规划 | 成功 | 是 | attempt-008 | 业务与程序审查通过 |
| D03 | 未封口禁止提前交付 | 成功 | 是 | attempt-009 | 业务与程序审查通过 |
| D04 | Orchestrator 重启与提案去重 | 成功 | 是 | attempt-009 | 业务与程序审查通过 |
| D05 | 两个 Orchestrator 竞争推进 | 成功 | 是 | attempt-008 | 业务与程序审查通过 |
| D06 | 执行器退出后换节点恢复 | 成功 | 是 | attempt-009 | 业务与程序审查通过 |
| D07 | Manager 执行中取消 | 协议成功 | 是 | attempt-010 | 业务与程序审查通过 |
| D08 | 暂停边界不启动新任务 | 成功 | 是 | attempt-010 | 业务与程序审查通过 |
| D09 | 外部调用超范围审批 | 协议成功 | 是 | attempt-012 | 业务与程序审查通过 |
| D10 | 拒绝扩大授权后不调用供应商 | 协议成功 | 是 | attempt-008 | 业务与程序审查通过 |
| D11 | 过期审批与重复命令拒绝 | 协议成功 | 是 | attempt-008 | 业务与程序审查通过 |
| D12 | 跨项目读取与控制拒绝 | 成功 | 是 | attempt-008 | 业务与程序审查通过 |
| D13 | 模型预算耗尽有界暂停 | 协议成功 | 是 | attempt-013 | 业务与程序审查通过 |
| D14 | BOSS 内容校验与候选不放行 | 协议成功 | 是 | attempt-013 | 业务与程序审查通过 |
| D15 | 授权内容指纹失效拒绝 | 协议成功 | 是 | attempt-009 | 业务与程序审查通过 |
| D16 | 外部提交未知不重复生成 | 协议成功 | 是 | attempt-009 | 业务与程序审查通过 |
| D17 | 观测服务中断不影响业务状态 | 成功 | 是 | attempt-009 | 业务与程序审查通过 |
| D18 | API 切换与浏览器恢复 | 成功 | 是 | attempt-009 | 业务与程序审查通过 |
| D19 | 独立部署与 main 数据隔离 | 成功 | 是 | attempt-011 | 业务与程序审查通过 |
| D20 | 专业 Agent 权限与实际模型用量 | 成功 | 是 | attempt-014 | 业务与程序审查通过 |
| D21 | 诊断后不可变修订并衔接下游 | 成功 | 是 | attempt-009 | 业务与程序审查通过 |
| D22 | 单一Run身份与独立Control Job | 成功 | 是 | attempt-010 | 业务与程序审查通过 |
| D23 | 业务排空与全Run派发屏障 | 成功 | 是 | attempt-009 | 业务与程序审查通过 |
| D24 | 专业Agent在途输入排空 | 成功 | 是 | attempt-013 | 业务与程序审查通过 |
| D25 | 规划期间连续输入分批处理 | 成功 | 是 | attempt-009 | 业务与程序审查通过 |
| D26 | Wan在途输入保持原句柄 | 成功 | 是 | attempt-005 | 业务与程序审查通过 |
| D27 | 外部提交未知时输入屏障 | 协议成功 | 是 | attempt-006 | 业务与程序审查通过 |
| D28 | 质量反馈幂等与过期快照 | 成功 | 是 | attempt-010 | 业务与程序审查通过 |
| D29 | 拒绝不能被人工批准且可补证据 | 协议成功 | 是 | attempt-006 | 业务与程序审查通过 |
| D30 | 局部蒙版修复比例边界 | 非程序失败 | 否 | attempt-007 | editing_model_mask_candidate_still_failed_review_after_evidence_collection |
| D31 | 持续动作的真实时序证据 | 成功 | 是 | attempt-015 | 业务与程序审查通过 |
| D32 | 输入与最终发布竞争 | 成功 | 是 | attempt-006 | 业务与程序审查通过 |
| D33 | 交付后successor输入幂等 | 非程序失败 | 是 | attempt-014 | successor_delivery_review_nonconverged_after_idempotent_inputs |
| D34 | 同步外部请求在途输入 | 协议成功 | 是 | attempt-006 | 业务与程序审查通过 |
| D35 | Provider配置切换后冻结路由恢复 | 非程序失败 | 是 | attempt-010 | external_wan_poll_failed_after_frozen_route_submission |
| D36 | 抠图候选边缘反馈修订 | 非程序失败 | 否 | attempt-015 | model_feedback_revision_again_preserved_original_bytes_instead_of_local_alpha_patch |
| D37 | 双图构图反馈修订 | 成功 | 是 | attempt-006 | 业务与程序审查通过 |
| D38 | Wan提交前参考图反馈修订 | 成功 | 是 | attempt-013 | 业务与程序审查通过 |
| D39 | 合法替代失败节点与历史读取 | 非程序失败 | 是 | attempt-020 | delivery_reviewer_contract_and_turn_quota_after_legal_replacement_succeeded |
| D40 | 已验收音频下游Operation取证 | 非程序失败 | 否 | attempt-021 | planner_turn_quota_again_exhausted_before_audio_and_evidence_tasks_created |
| D41 | 最终交付选择与多视频时序证据 | 非程序失败 | 否 | attempt-010 | final_delivery_model_again_requested_input_before_selecting_alternate_video |

## 证据口径

每个最新尝试均保留 `state.json`、`internal-final.json`、浏览器截图和 `review.json`。业务成功另有 `artifact-verification.json`；涉及媒体内容的 case 保存关键帧、contact sheet、音频测量或候选归档。内部审计覆盖 Run、Task/Control Job、revision、Attempt/Lease、Provider call、模型用量、日志、可观测性、PostgreSQL、Redis、BOSS、节点缓存和清理。

本报告不把模型/依赖失败改称业务成功。`程序正确性通过率 100%` 表示 119 条最新结果中没有程序错误，失败分支的程序应对经审查符合设计。
