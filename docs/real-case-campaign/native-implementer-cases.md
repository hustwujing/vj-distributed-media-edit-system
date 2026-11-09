# 原生代码编辑师专项矩阵

只使用sandbox-agent独立环境，源项目不改动。并发上限8；取消/服务故障注入单独执行。
每批冻结源码、配置、Harness版本、镜像、Bundle及驱动；不同版本不混成同一成功率。
代码错误先修复并仅复测受影响项；模型/供应商质量失败保留首败，不无限重跑。

| Case | 内容 | 独立检查与状态边界 |
|---|---|---|
| NH01 | 生成小型代码/数据处理交付 | Claude/Codex按已验证镜像走OpenSandbox；当前DS SDK不满足终止契约，按冻结能力走structured+bwrap；输入归属、真实文件内容、模型回执与清理；页面显示控制循环及工具实际位置 |
| NH02 | 复杂媒体代码编辑 | 与structured-profile-v4使用相同目标/素材/模型对比；实际像素/时长/音轨和可复放文件，不以系统成功代替质量 |
| NH03 | 规划确认后组合修改契约 | 文件名、规格和附属文件同时变更；安全点/新确认/新revision，审查与发布同一契约；旧成果不复活 |
| NH04 | 运行中关闭页面再进入 | 流式/完成消息、真实位置和耗时、等待决策、终态交付可见；DS按实际能力显示完整消息而非虚构token delta |
| NH05 | 模型/工具失败与预算耗尽 | 明确错误及有界出口，预算数据库累计，未知用量保留；不伪装为需要补充业务信息 |
| NH06 | 取消、断连与执行器中断恢复 | Token撤销、旧generation拒绝、无重复发布、仅恢复同Task/revision兼容会话、sandbox删除确认；真实未触发边界不计通过 |

每个Harness必须有真实启动和工具后续轮次证据；只有镜像可启动不代表业务接入完成。
阶段报告分别列业务成功、程序失败、提示词/契约失败、非程序失败、未执行与平均总耗时。
## 迁移前基线

1139d01上的NH02已完成：Run c365632c1a6446fa8011ce2e83dca4f3，248.230秒，
实际editing_implementer使用structured-profile-v4、claude-sonnet-5-5，1次Attempt/4次模型调用。
交付portrait.mp4为720×1280、24fps、9秒；完整解码、桌面/窄屏播放、重进及cleanup=0通过。
独立抽帧与原素材比较确认人物完整保留、同步模糊背景和底部中文标题；中间黑场原素材也存在。
音频包SHA256与输入完全一致：a3488e7babe8ec73e280c2f12be83b772c3d534068bef4c1854bb967ceaccdf1。
视频215帧（源216帧），容器/音频9秒；保留这个帧数差异，不宣称逐帧字节等价。

驱动首轮业务完成后误读未公开的task.profile，结果保留needs_analysis；只读review脚本随后使用实际
usage.harness_runs完成核对，无业务重跑。证据：.sandbox-cluster/campaigns/native-implementer/baseline-NH02-001/。
先前native-baseline-001的agent-portrait实际走media.edit Operation，仅作媒体参考，不计Harness基线。
原生Harness业务案例尚未执行；真实网络/TLS探测另列，不计业务成功。

## 迁移后NH01结果

| 配置 | Run | 结果 | 秒 | 实际执行 | 结论 |
|---|---|---:|---:|---|---|
| Claude 5.5 / high | `3e910538375b4385849b0ef8176babbb` | 成功 | 315.188 | `claude_code`，OpenSandbox | CSV内容/SHA256、页面重进、终态交付、cleanup通过 |
| DeepSeek 4.1 / high | `32c81fd768e14297abf14ba1e23964cc` | 程序失败 | 323.408 | `deepseek_harness`，OpenSandbox | 4096 Token；无`message_stop`，明确`agent_runtime_unavailable`，无反复修订 |
| DeepSeek 4.1 / low | `80d45af5c9b446609d53f08f2e911584` | 程序失败 | 622.703 | `deepseek_harness`，OpenSandbox | bash成功后第二次模型流仍不结束；cleanup通过 |
| DeepSeek 4.1 / low fallback | `81cbdd6c45f041e6ac6a4c0d6923247b` | 成功 | 365.109 | `structured-profile-v4`，工具为bwrap | 4096 Token、最小600秒；产物与Claude案例SHA256相同，cleanup通过 |

DeepSeek原生失败属于当前SDK/模型Messages终止契约，不是业务输入或普通质量失败。当前发布配置不登记
DeepSeek原生Runtime Profile；模型仍可选择，但代码编辑Task在计划时冻结structured+bwrap，不能在执行时
静默改变。Codex已完成真实OpenSandbox控制探针，当前用户模型注册表没有Codex模型Profile，因此未伪造
真实付费业务案例。
