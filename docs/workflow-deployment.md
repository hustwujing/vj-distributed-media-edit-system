# 部署与恢复步骤

部署前运行 `deploy/release-workflow.sh --check`。必须全部检查项有证据且 overall-result.json 为 passed；任何 blocked/pending 项都会阻止部署。用户已授权部署，无需再次申请权限，但不能跳过其“全部完成并验收后再部署”的前置条件。

`--prepare` 生成仅 root 可读的源代码快照和 SQLite 一致备份，记录当前 unit。`--deploy` 在同样检查后停止 API 接收新写入、排空 Agent、更新模型/运行文件 ACL 和沙箱镜像、增量迁移数据库、安装三个 systemd unit，启动 reaper/API/Agent 并检查健康。现有内部模型主机按服务配置加入明确允许列表，不打印凭据。

部署后检查健康端点、三个服务、队列恢复与资源清理；另运行真实 Agent 和非收费本地工作流 smoke。模型 configured/unverified 与实际健康分开记录。

恢复时先停止 API/Agent 并确认执行资源已回收，再从指定备份恢复代码、重建对应 runtime、恢复 unit；如需回滚数据库，必须确认备份之后没有需要保留的用户写入，禁止直接覆盖新数据。备份是发布前磁盘版本，不能声称能还原更早、未保存的内存中服务版本。部署脚本健康失败会停止并报告，不自动丢弃用户数据。
