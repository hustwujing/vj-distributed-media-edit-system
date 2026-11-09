# 原生代码编辑师接入与对比

2026-11-09用户批准开始。现有动态专家使用structured-profile-v4；本次只迁移editing_implementer。
模型、协议、推理强度继续来自Run冻结的agent_profiles/model_selection，不新增用户配置体系。

## 执行绑定

独立部署的VIDEO_USE_CPU_AGENT_SANDBOX_BACKEND=opensandbox选择native-implementer-v1策略，
其他专家保持structured-profile-v4。每个Run的用户模型预设确定codex、claude_code或deepseek_harness。
VIDEO_USE_OPEN_SANDBOX_AGENT_PROFILES_JSON使用已有Runtime Profile字段，按Harness登记
runtime_profile、image_digest、skill_digest；不允许缺失配置后回退宿主或使用另一Harness镜像。
注册表纳入策略摘要，更新镜像必须重新冻结新Task，不能静默替换在途Task。

复用Command Runtime的按Task输入恢复、独立工作目录、产物下载、技术校验、步骤检查点和cleanup。
原生进程结束不代表业务成功；Executor只暂存候选，独立Reviewer与Orchestrator继续审查和发布。
原生会话恢复不能使用旧聊天工厂的项目级全量workspace；恢复必须限定Task/revision及配置摘要。

## 模型入口

每个Attempt在宿主内部bridge地址创建TLS模型入口，短期随机Token绑定Attempt/generation/sandbox/source IP。
只开放冻结模型协议的messages或responses端点，不开放通用代理、媒体Provider或规划/发布接口。
授权随该监听器生命期存在，不能在新进程或新generation复用；调用预算和实际用量复用共享数据库账本。
cleanup先撤销Token、关闭连接，再删除sandbox并确认；不能把凭据放入镜像或会话检查点。
本机TLS配置通过VIDEO_USE_NODE_GATEWAY_BIND/SUBNET/CERT/KEY/CA提供，私钥只在宿主。

## 测试与当前状态

第一阶段关联90项测试通过：三Harness绑定、独立角色权限、失败/停止不提交、Runtime控制、Bundle与
镜像契约、网关共用业务处理。随后补公开角色前缀定向复核。此记录只证明开发回归，不表示已部署原生Harness。
后续补恢复、限额、真实Gateway、实际Harness和媒体案例证据，按用户要求先构建被测镜像；完整发布再补齐四镜像。
不重跑44项。对比矩阵见[专项用例](../real-case-campaign/native-implementer-cases.md)。
