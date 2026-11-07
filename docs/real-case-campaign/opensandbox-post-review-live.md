# FINAL 后独立环境真机测试记录

2026-11-07，Claude 的 `/data2/wujing03/bug/FINAL` 确认 28 项程序缺陷及 1 项规范漏项闭环后开始。此记录持续更新，不代替逐例证据或最终验收报告。

## 首批版本及隔离

- 源码 `d84c02cfbbed5e0fd37a5cd4aabd326fafa1a87a81e5183103cf60b3e442cb00`，Git `66ddf8a`；Python 1216 项（1210通过、6显式真机skip），前端64项通过。
- 实例 sandbox-agent，数据库 distributed_agent_sandbox，端口20765–20768，BOSS前缀 distributed-agent-sandbox，Redis前缀 distributed-agent-sandbox:events:。
- 工作目录 `/data2/wujing03/distributed-agent-sandbox-backend`；执行目录 `/data2/wujing03/das-sandbox-exec`；Docker数据 `/data2/wujing03/opensandbox-stack/data/docker`。未操作来源实例。
- 四镜像同源重建并推送到127.0.0.1:25001/sandbox-agent；证据在 `/data2/wujing03/das-final-runtime-build/runtime/build/`，每次重建前归档上批证据。镜像用于隔离QA，未签名，不是生产安全验收。
- CPU Command 使用 OpenSandbox，Agent 使用本地 bwrap。必须按实际后端分别报告，不能宣称全部 Agent 已在远端镜像运行。
- 浏览器显式使用 `PLAYWRIGHT_BROWSERS_PATH=/data2/wujing03/tool-cache/ms-playwright`；TMP/cache均在data2。首次驱动启动因缓存路径未配置而退出，未创建业务Run，不计case失败。

## 首批8个case及修复

C01–C07、C13以最高8并发执行；已全部结束，平均441.6秒。系统报告C03/C04业务成功，其余6条未成功；最终人工/独立审查与系统状态分开记录。

| Case | 已核实问题 | 修复/证据 |
| --- | --- | --- |
| C01 | 已生成的质量报告角色为quality_report，最终要求report；角色契约不一致 | `1f35d96`：用户明确要求报告时，唯一 *_report 路径加canonical report别名；多义情况要求显式指明，不猜文件 |
| C02 | 最终审查要求后半段音频证据；补证后Manager反复提交空APPLY_PLAN | `03c0629`：新增证据成功后提供明确complete_run入口再次独立审查；不得复用旧证据无限重审，任务级缺陷不走此捷径 |
| C05 | SAM3对合法长标签语义匹配分数过低 | 同图原标签0.35/0.175无结果，0.01得0.030029；woman astronaut在0.35得0.835938，woman in orange suit仍无结果。`a7069c9` 调整通用类别标签/空间提示指导，不降低全局阈值。证据 `/data2/wujing03/vu-tmp/c05-sam3-contrast/results.json` |
| C06 | 内容调色被拒后，修订上游的计划被旧paused下游阻断 | `1f35d96`：校验与实际提交统一按修订后等待集合；保留无关暂停分支，原有有声/外部副作用等约束不放宽 |
| C07 | OCR/TXT部分批次完成后撤掉继续规划工具；非法回复详情又被丢失 | `5302f5b` 保留新增任务入口；`4dd92b3`细分原生工具错误、保留安全阶段信息、不同错误不再都哈希空数组。旧回复未保存，不能还原其原始结构 |
| C13 | 多轮音频证据不足，修复替换还被互斥audio/video角色全部必需的校验阻断 | `1f35d96` 规划验收和替换校验共用实际激活的输出角色，缺失必需角色仍拒绝并列出名字 |

C04独立内容测量：实际蓝色Logo为64×64，距上/右边均16；透明PNG角点alpha=0；成片音频拟合原声增益0.996、音乐0.150。C03可见Logo为40×40、边距28（原素材留白），已作为内容尺寸疑点保留，不能仅凭系统SUCCEEDED视为完整通过。测量文件位于各attempt的 `independent-content-measurements.json`。

## 定向验证

- 工具异常分类：首轮106项为105通过、1条新夹具预期错误；修正成功提交后计数应清零的断言后，相关2项通过。
- 成功部分批次继续规划：107/107通过。
- SAM3指导及Bundle：14/14通过，另有真实GPU同图对照，未修改模型权重或业务配置。
- 报告角色/修复校验：158项首次157通过、1条新增夹具错误。夹具改用完整授权绑定及权威校验入口后，关联3/3通过；原始失败和各次复测日志均保留。
- 最终补证重审入口：134/134通过，26.210秒。上述变更尚待新版本真实case复测，不能当作已部署验收。

## 下一批模型

用户要求将原Claude5.5角色改为DS4.1 Flash。候选配置已通过当前代码加载：Manager、Diagnostician、Task Reviewer、Final Reviewer均为deepseek41预设，模型deepseek-v4.1-flash/high，Anthropic协议，deepseek_bpe_estimate_v1。Editing Implementer保持claude-4.6-sonnet/high。

新配置在新部署/新Run冻结，旧Claude结果不改写。代码修复与模型切换同时发生，前后结果不能全部归功于模型；报告分别列实际模型、角色耗时、成功率与错误原因。
