# 新命令、模型选择与输入预算部署验证

本记录针对 2026-11-04 的隔离实例，不作为 119 条媒体 case 的完整验收报告。工作区和部署入口均为 `/data2/wujing03/distributed-agent-system`，测试暂停由原轮次记录控制。

## 配置错配及处置

旧部署 `deedda4df9cb` 使用 structured-profile-v2，其 AgentProfile 不接受 `input_token_policy`。提前将新增字段写入共享 `.agent.env` 会造成旧服务重启时解析失败；运行中的进程仍持有旧环境，不能证明重启安全。

发现后先保存候选配置，再恢复原配置，并用旧部署副本实际调用 `profiles()/selected()` 验证成功。随后发布 v3 时，在旧服务停止、服务启动命令切换到已验证候选之后才原子提升新配置。该次发布没有重置数据库。配置暂存和恢复证据保存在私有 `.cluster/config-backups/`，不公开凭据。

发布前出现的新 Run `e08f3a673d8140b596eb31129db6b17a` 按用户指令通过正式 cancel 命令停止；命令 applied，Run 为 CANCELLED/user_cancelled。保留项目与原成果，没有直接改写终态或删除该用户项目。

## 门禁与部署证据

候选 `8d39c6778eb6` 的工作区源码摘要、冻结源码和 Python 门禁报告一致：900 项、0 failure、0 error、6 项显式真实 GPU/OCR 跳过；Node 59 项通过。审查时只读取旧轮 active-round 中的门禁数字，会漏掉这份新候选证据。

该候选已完成 v3 隔离部署。10 个服务 active，两个 API 的 `/api/agent/health` 与带 `/distributed-agent-system` 前缀的代理健康接口均返回 200/available。main 的 8 个服务 PID/启动时间及 4 份配置哈希与发布前一致。

第一次部署浏览器 smoke 发现 390px 宽度下公共页头右侧按钮溢出约 39px，故该次完整浏览器验收不通过。失败记录和截图保留于 `.cluster/deployment-validation/token-estimation/attempt-001-mobile-overflow/`。通用修复在窄屏收起品牌文字和冗余首页链接，保留导航、品牌图标和新建按钮，不隐藏整页溢出来掩盖布局问题。

最终部署为 `320c25093f7b`（2026-11-04 17:05 CST）。该摘要重新完成完整门禁：Python 900 项，0 failure、0 error、6 项显式跳过；Node 59 项通过。直接访问最终部署的完整浏览器 smoke 已通过，没有注入候选样式或绕过窄屏断言。已检查实际截图；模型控件和页头在 390px 宽度下均正常显示。

最终证据位于 `.cluster/deployment-validation/token-estimation/`：`browser.json`、`default-model.png`、`model-selection-mobile.png`、`frozen-model-run.png` 和 `deployment-final.json`。10 个服务健康，main 的进程/配置比对不变，活动 Run 和租约均为 0，用户取消命令已 applied，测试项目清理返回 200。全量门禁日志为 `.cluster/verification/320c25093f7b.log`。

## 已完成的分项验证

- 服务默认选择 claude，实际型号 `claude-4.6-sonnet`，推理强度 high；专业角色实际引擎为 structured-profile-v3。
- 模型与强度选择、项目草稿刷新保留，以及 Run 创建请求中的显式选择均有浏览器证据。
- 用独立测试项目选择 DS4.1/low 创建低预算 Run，确认实际冻结配置与提交一致；修改项目默认值不改变该 Run。
- 1024 Token 预算在生成前拦截，模型生成调用为 0；取消和项目清理完成。此项不证明真实模型输出或媒体制作质量。
- 原生计数不可用时的明确本地估算策略，及 Claude 异常零输出回执的预算处理，另见[计数改造记录](model-token-counting-gap.md)。预估不是厂商精确用量，缺失或矛盾回执不填零。

桌面独立诊断保存在 `desktop-diagnostic/`；修复样式的浏览器预览保存在 `mobile-fix-preview/`。前者显式保留已知窄屏缺陷，后者注入候选样式用于开发预览，均不能替代最终部署版本的完整 smoke。

strict 优先策略已由能力接口、注册引擎和冻结源码核实；此部署 smoke 的低预算设计没有发起实际生成，因此不单独证明最终部署下每种网关的 strict 执行质量或回退效果。既有真实协议实验与预算链路记录单列，后续媒体 case 仍需逐次核对实际请求和回退证据。

## 历史数据与下一轮清单

旧轮 D39/attempt-005、D41/attempt-003、D36/attempt-005、D38/attempt-004 均保存了完整内部审计及数据库快照，随后 cleanup.json 明确记录 project_removed=true。前两条历史结果为 passed，后两条为 program_failed。当前在线库不能重新查询已删除的项目，但清理前归档仍可复查；本次证据不能支持“模型评测重置了数据库”的推断。

逐文件 SHA、归档存在性和清理结果写入 `.cluster/deployment-validation/token-estimation/historical-evidence-audit.json`。归档只证明原版本、原尝试，不证明缺陷已在新版本真机关闭。

原 `full-119-source-lock.json` 保留；工作区的 browser/hook 两个文件已变更，旧锁不适用于新执行。为候选另存 `prepared/v3-<摘要>/source-lock.json`、完整 case manifest 和 13 份驱动/报告文件副本，状态明确为 prepared_not_started。下一轮实际起跑前仍须核对最终部署摘要、模型配置与文件哈希，并获得解除既有暂停的指令。

119 条清单仍暂停：旧轮已启动 8，成功 2，程序失败 2，用户停止 4，未执行 111。部署 smoke 和模型协议实验不计入这些 case，也不将旧结果重标为 Claude 4.6/new/strict 的通过结果。
