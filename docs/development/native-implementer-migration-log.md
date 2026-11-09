# 原生Harness迁移过程记录

本文件随2026-11-09迁移持续更新。已验证的稳定规则最终归纳到Harness/Runtime开发规范；
假设、未验证事项和真机失败保留原状态，不把源码实现当作部署成功。

## 基线与实施顺序

1. 起点为已部署1139d01，报告提交51e1c8f；CPU Command已经走OpenSandbox/gVisor，
   动态代码编辑师仍是宿主structured-profile-v4与bwrap工具。
2. 读取现有模型配置、SpecialistProfile、HarnessAdapter、Runtime协议、Gateway和检查点实现。
   保留并行未提交的页面/文档改动；只提交自己负责的文件。
3. 复用VIDEO_USE_CPU_AGENT_SANDBOX_BACKEND启用原生编辑师策略；模型预设内的harness决定
   Codex/Claude/DS。补齐按Harness选择不可变Runtime Profile的映射，并将其纳入策略摘要。
4. 按动态Task创建最小workspace，接入原生控制/事件、仅模型网关、技术验收与候选暂存。
   第一批90项关联测试通过，公开角色前缀再通过7项定向检查；提交2d725e5。
5. 增加Task/revision/config/input绑定的会话检查点、恢复、存储额度与项目删除集成。
   188项关联回归通过，0失败/错误/跳过，53.9秒；覆盖状态协议、恢复、输入计数、用量和Gateway。
   尚未切换运行环境或启动付费真机。

## 已发现的问题与处理

| 问题 | 根因/影响 | 处理与证据状态 |
|---|---|---|
| 配置显示Harness不代表实际执行它 | 动态专家冻结structured-profile-v4，旧AgentProfile的harness只描述用户预设 | 独立native-implementer-v1策略，实际调用记录原生Harness；仅编辑师切换；单测通过 |
| 旧远程工厂不能直接套动态Task | 从agent_turns取项目，恢复项目级workspace；动态Run没有对应旧聊天记录 | 复用Command Runtime最小Task输入恢复，限定Task/revision，未使用旧项目级工厂 |
| 多Harness共用单镜像配置会错配 | 旧Agent backend只有一组image/profile字段 | 按用户冻结Harness精确选择不可变镜像，缺失/可变tag拒绝，GPU路径保留；单测通过 |
| 控制端点只监听容器loopback | OpenSandbox代理需要访问容器端口，不能据离线TestClient推断可达 | 改为容器内监听，继续Token/identity/generation认证；实际代理可达性待真机 |
| 原生进程缺HOME、PATH和Codex路由 | Runtime桥仅提供/usr/bin；Codex缺受控gateway config | 注入可写state目录与固定路由；长期密钥仍留宿主，Codex配置不进入checkpoint |
| 原生模型调用旧预算按JSON字节估算 | Base64图片和长SDK工具Schema会严重偏离模型Token；旧动态路径与专业循环策略不同 | 复用校准输入估算/原生计数、输入输出分账和数据库预算；SDK usage事件不再作为第二次计费 |
| 跨线程撤销可能破坏上下文回收 | grant reaper调用close时不在创建usage ContextVar的请求线程 | 异线程只中断HTTP，原请求线程结束时结算并退出作用域；需补实际取消验证 |
| 执行位置被强制显示宿主 | 页面投影把所有workflow Agent都视作结构化宿主循环 | 仅structured专家保留宿主标记，原生Task按实际Sandbox记录显示 |
| 原生公开消息缺角色前缀 | UI按持久消息段识别角色，不应从模型配置猜测 | 统一加代码编辑师前缀并保留Attempt关联；定向检查通过 |
| 扩展路由破坏旧测试夹具 | 新逻辑对非动态workflow也读取Run表，旧隔离夹具无该Store | 仅dynamic工作流读取动态Task；首轮64项中2错误，修复后90项通过，首败保留日志 |
| 恢复清单含宿主location字段 | SandboxWorkspaceRestorer仅接受portable字段 | 恢复前投影artifact/size/hash/executable，先核对全部哈希再写沙箱；恢复正例和篡改拒绝通过 |
| 会话文件可能不计存储或漏清理 | 新表若只做保存不接通Store/project delete，会形成生命周期缺口 | 补存储额度、项目目录、对象引用发布及项目删除；存储额度及旧租约回滚通过，真实项目删除待验收 |
| API与Worker策略摘要不一致 | 旧OpenSandbox配置只注入node env，API/Orchestrator无法冻结相同native策略 | 只把backend及无密钥镜像注册表同步到三个角色；URL、API key和TLS私钥仍不进入Orchestrator |
| Docker internal不阻止访问宿主或同网沙箱 | internal只限制对外路由；任意原生代码仍可探测宿主监听端口和同网execd | 为专用bridge补仅模型TLS端口可达与跨沙箱隔离规则；不修改其他网络；真实探测待执行 |
| DS镜像发布元数据协议已漂移 | 实际固定SDK走Anthropic Messages，release-spec仍登记Chat Completions | 校正为/messages、/v1/messages；实际SDK模型交互待真机 |

## 待验证和最终规范提炼

- 三Harness真实模型调用、工具后续轮次、输入输出协议与实际用量；不能只验证SDK import。
- 同Task/revision原生会话续接、跨节点材料恢复、进程中断无可用会话时的明确有界处理。
- TLS/source IP、短期token撤销、lease失效、模型断流及Sandbox删除确认。
- 媒体产物与用户可见进度/错误/等待/终态；按专项矩阵留证，不重跑44项。
- 明确哪些变更必须重建镜像、如何只构建被测镜像、如何从干净源码部署，以及回滚配置的配对要求。

关联文档：[接入方案](native-implementer-rollout.md)、[真机矩阵](../real-case-campaign/native-implementer-cases.md)。
