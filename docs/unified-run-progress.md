# 统一 Run 重构实施记录

本记录只记录实际进展；历史部署与历史真机结果不代表当前分支通过。

## 基线

- 分支：`feature/unified-run`；完整工作区基线提交：`7e667d8`。
- GitHub：待代理地址，尚未推送。
- 本次真机：现有 99 条，尚未部署本轮改动，启动 0 条。
- 临时文件、测试和证据均放 `/data2/wujing03`；隔离部署只操作本系统专用资源。

## 实施顺序与验收关口

1. Provider 元数据拆分：注册契约、预算、并发、路由、冻结调用记录、取消/恢复、复用与页面；定向协议和竞争测试。
2. 单一 Run、独立 Control Job、真实 lifecycle phase、WorkspacePolicy 和角色/action capability；接通租约、执行器、控制器、API 和观测。
3. run_inputs 排空屏障、冻结输入批次、六类统一重规划结果、质量反馈 revision、合法替代和审查去重；事务竞争与重启测试。
4. 完成原 25 条失败对应的通用修复；审查所有旧 case，登记保留/修改/删除理由并增加新边界 case。
5. 全量单元及前端检查、隔离部署、逐例真机与内部/浏览器审计、修复复测、完整报告和开发规范刷新。

## 已执行验证

- Provider 第一批：74 项定向测试通过，无失败、无跳过。
- Provider 第二批：29 项定向测试通过，无失败、无跳过。
- 新元数据边界及相关回归：59 项通过，无失败、无跳过。
- 上述批次包含重复覆盖，不应相加作为唯一用例数；最终发布以冻结版本全量报告为准。

## 当前未完成

Provider 拆分正在实施，尚未全量验证或部署。其余工程阶段和真机验收未完成，不勾选完成项。

## 2026-11-03 后续进展

- 单一 Run 创建已不再创建 agent_turn；Session 只记录会话与消息，successor 使用独立用量根。
- PLAN/REVIEW/DIAGNOSE 移至 control_jobs，业务 run_tasks 不再保存 internal；控制任务有独立租约围栏。
- Run 正式状态收敛为六类；activity 从当前工作派生。动态入口改为 /runs。
- 类型化 workspace policy、角色/action capability、实际 lifecycle phase 已接入当前动态路径。
- 已接入 run_inputs、派发屏障、drain set、输入批次上界与六类 ManagerResult；反馈 revise 走同一输入流程，带候选身份和修订闭包。
- 决策和提案应用补事务 savepoint，拒绝时回滚局部状态及未提交通知。
- 当前定向集 75 项通过，0 失败/错误/跳过，源码摘要 bef5a2a54e74634f6000b1334e088bed6456553213ed008bcfcff39f69ee19e4。包含真实 PostgreSQL 和两节点沙盒执行测试，但不是全量发布检查或真机验证。
- Provider 冻结副本全量 784 项：7 failure、7 error、6 显式 GPU/OCR skip；未通过。已发现副本降权读取权限问题、旧夹具缺少提交意图/Provider 元数据，剩余问题待修复复测。原始记录在 /data2/wujing03/vu-tmp/unified-provider-full.log，不能将此轮记为通过。
- 仍待补齐：合法替代与下游、审查去重、统一问答与 successor 决策、浏览器交互、API 下载/诊断/观测统一身份、真实数据库竞争、外部恢复边界，以及全部真机 case 调整与部署验证。
