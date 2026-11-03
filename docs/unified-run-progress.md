# 统一 Run 重构实施记录

本记录区分源码实现、开发验证、部署和真机验收；历史部署结果不代表当前版本通过。

## 基线与发布状态

- 分支：`feature/unified-run`；完整工作区基线 `7e667d8`，第一阶段架构提交 `2233fcb`，协议/真机驱动实现 `46ee178`，历史证据归档 `b558797`。
- GitHub：用户要求使用代理，当前未取得可用代理，尚未推送；继续本地实施，不改成直连推送。
- 本轮尚未部署、尚未启动真机；现有 99 条已重新审查并调整协议，新增 17 条，清单总计 116 条。
- 临时文件、构建、数据库测试、冻结回归和证据均在 `/data2/wujing03`；只操作本系统专用部署。

## 已接通的源码能力

| 目标 | 当前实现 | 验证入口 |
| --- | --- | --- |
| Provider 元数据 | 副作用、预算组、并发组、路由适配器、具体 Provider、清理类别分离；调用时冻结，无旧 Operation.provider 回退 | test_provider_metadata、生命周期/取消/用量测试 |
| 单一执行根 | Session → Run → Business Task / Control Job → Attempt / Lease；动态路径不创建 agent_turn；/runs API、下载、事件、调度、取消和观测统一身份 | test_runs、test_dynamic_execution、API/调度/租约测试 |
| 输入与反馈 | run_inputs、全 Run 派发屏障、drain set、冻结 cutoff、六类 ManagerResult、幂等与快照、用户修订闭包、授权历史、successor 完整确认 | test_run_inputs、test_run_input_races、test_run_feedback |
| 通用修复 | 不可变 revision、显式 replacement、保留旧失败与 plan selection、用户/自动修订分别有界、Review 去重 | test_run_replacements、test_workflow_contract_repairs |
| 证据补充 | 候选只读 RepairCandidateRef、media.evidence、SHA 绑定时序/结构/音频/蒙版测量、review_again 不重生成 | test_repair_inputs、test_temporal_evidence、test_media_evidence |
| 有界恢复 | 正常 poll 不消耗中断恢复次数；原句柄恢复；审查预算不足交回 Manager；输入与失败合并到一次安全点规划 | test_run_recovery |
| 媒体资源 | 独立 media.normalize_clip Task 接检查点，兼容流 media.concat_sequence 顺序拼接；audio.process、全流测量、受限解码线程 | test_media_plan_compiler、operation 契约/执行测试 |
| 页面与规范 | 运行中输入、待决反馈、素材/修改根选择、刷新保留草稿、successor 跳转；harness v2 与角色专用结果协议 | 前端单元测试；实际浏览器仍待部署验收 |
| 重置与部署 | --reset-database：冻结门禁、停止专用服务、备份、检查未知外部请求和未完成清理，再重建专用 public schema | test_dynamic_reset，真实临时 PostgreSQL |

## 开发验证证据

这些批次有重复覆盖，不能相加为唯一测试数，也不是部署后真机结果。

- 冻结源码 `973e38c0849b3a9a9e1ddaf987e104f0891b1050effae4c3fee57941f806c354`：Python **815 项，0 failure、0 error、6 skip**；Node **48 项通过**。证据 `/data2/wujing03/vu-tmp/unified-regression-973e38c0849b/`。6 项为显式 GPU/OCR 实测开关未开启，不能计为模型验证。
- 定向 63 项：统一身份、输入、反馈、替代、候选证据、时序与两节点实际沙盒执行通过。
- 定向 80 项：新增目标闭包、successor 完整范围确认、独立片段规范化及契约回归通过。
- 定向 66 项：重置保护、真实 PostgreSQL 竞争、审查身份、观测和 Reviewer 校验通过。
- 定向 50 项：补齐失败/输入合并规划、正常 poll 后恢复、Review 去重、输入竞争、API 和观测后通过。
- 后续源码仍需按最终发布摘要完整门禁；上述冻结结果不能替代更新版本的发布检查。

- 发布候选 `138cd0a71bd0`：完整 Python **823 项，0 failure / 0 error / 6 skip**，Node **48 项通过**；日志 `.cluster/verification/138cd0a71bd0.log`。随后补强外部清理重置保护、短窗口时序采样和音频编码时长，需以最终新摘要重新执行门禁。
- 新增定向 19 项：蒙版主体面积比例、主体关系覆盖默认值、实际视频结构/参考哈希测量，以及外部清理未确认时拒绝数据库重置，通过。
- 新增定向 9 项：独立规范化、24 段时长累积、真实短视频联系表覆盖与稀疏证据拒绝，通过。

## 保留的失败与修复记录

- 旧冻结回归 784 项曾有 7 failure / 7 error / 6 skip；夹具和降权权限问题已修复，原记录保留。
- 中间冻结回归 809 项曾有 6 failure / 10 error / 6 skip，原因集中于冻结副本根目录受 umask 影响变成 0750，降权进程无法读取源码；修正副本遍历权限后，相关 74 项通过（5 显式 GPU/OCR skip），随后完整 815 项回归通过。
- successor 新测试曾在错误请求后重新 claim 同一个已经 running 的控制请求，测试夹具已改为同 lease 内验证拒绝和正确响应。
- 恢复新测试曾遗漏 basic-only acceptance 的理由字段，补齐合法契约后通过；未放宽生产校验。
- 复核 Reviewer evidence 耗尽与在途输入的合并路径：底层已有屏障拦截，本次补入口检查和专门回归，确认保留失败事实并在统一安全点只创建一个 PLAN。

- 独立规范化实测发现 AAC priming 经 MKV → concat → MP4 每段累计约 21ms；24 段 2 秒视频变为 48.504 秒。改为直接编码规范化 MP4 后，每段恢复 2 秒，整段 48.022 秒；加入按总时长约束的回归，原始/修复证据保留在 `/data2/wujing03/vu-tmp/normalization-timing-t6abnfyk/`。
- 短视频窗口采样固定上限内使用 16 帧，避免 8 帧联系表覆盖只有 87.5% 而无法达到明确 90% 的验收；仍校验真实时间戳与最大间隔。

## 待完成关口

1. 最终源码冻结全量检查与真机驱动预检；源码文档同步检查。
2. 专用数据库备份/重置、隔离部署、浏览器入口与运行包实际服务身份检查。
3. 116 条逐例真机执行、逐例浏览器/日志/观测/数据库/缓存/BOSS/清理审查；程序缺陷修复后复测，明确区分非程序失败与未触发边界。
4. 完整真机报告、最终规范核对、本地最终提交，以及取得代理后推送 GitHub。

TODO 中的验收勾选包含最终部署证据，未到该关口不提前勾选为完成。
