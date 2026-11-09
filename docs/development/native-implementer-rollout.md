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

## 路由与能力契约

Manager不能从工具名、任务标题或模型名称猜执行地。Operation目录必须提供`execution_requirements`，
至少包含`requires_gpu / execution_mode / cpu / memory_mb / network`。代码编辑Task必须提交
`runtime_requirements`：`requires_gpu`、实际导入的`python_modules`、执行的`system_tools`及精确
`network_urls`。方案确认卡展示这些字段；宿主根据冻结目录再次校验。

- `requires_gpu=false`且依赖均在`opensandbox_cpu`目录中：优先OpenSandbox及用户冻结的
  Claude/Codex/DS Harness。缺镜像、缺依赖或网关不可用时明确失败，不静默回落bwrap。
- `requires_gpu=true`：使用原structured代码编辑师与bwrap，调度前取得GPU租约。缺库不能作为GPU理由；
  能拆成注册GPU Operation的工作优先使用Operation，只有确实需要动态代码时才使用GPU Agent。
- Control Job、Reviewer、Manager和Diagnostician不随业务代码编辑师迁移，继续使用服务端结构化引擎。
- 运行时目标、Harness、模型、镜像、Bundle、依赖目录和网络授权都进入冻结摘要。恢复时重新读取当前
  默认值、在不同Harness间复用私有会话或从CPU静默切GPU均不允许。

能力目录的承诺必须由`deploy/verify_agent_runtime_capabilities.py`实际检查。补齐OpenSandbox依赖以
原bwrap代码编辑师的非GPU媒体能力为基线；测试框架、云平台管理SDK和宿主运维依赖不属于业务承诺。
本轮明确补齐`networkx / PyYAML / sympy / xmltodict`和`file / ps / socat`，保留Pillow、NumPy、
OpenCV、SciPy、Librosa、Matplotlib、psd-tools、FFmpeg/FFprobe及公开helpers。不能笼统宣称任意
Python/Node/浏览器程序都可运行；未登记依赖必须在规划时拒绝，并按镜像发布规范新增。

## 受控公网读取

沙箱继续没有任意网络。需要公开数据时，Task先把完整HTTPS URL加入`allowed_network_urls`授权，
再把本Task实际使用的子集写入`runtime_requirements.network_urls`。用户在授权卡和方案卡中能看到URL。
代码统一调用`/opt/video-use/network_fetch.py`；Gateway只允许精确URL的GET，不携带用户或宿主凭据，
不跟随重定向，限制响应32MiB，并继续执行DNS解析后的公网地址校验、租约、Attempt/generation/source-IP、
调用次数、请求/响应字节和cleanup撤销。HTTP、内网/metadata地址、未授权URL、任意POST和沙箱直接联网
均拒绝。扩大URL集合需要新授权；输入文本出现URL本身不授予访问权。

## 开发与发布门禁

1. 修改Profile、Task资源字段、Gateway、Runtime或依赖清单时，先更新能力目录和迁移记录。
2. Python/系统依赖变化修改`pyproject.toml`/lock、离线wheel和foundation/base；按共享base规则重建四镜像。
   定向验证可只构建当前被测Harness，但必须标记`targeted_only`，最终发布补齐四镜像。
3. 对每个被测镜像运行只读根、断网、非root、Bundle、SDK子进程、模块/工具和代表性helper检查；
   仅顶层import成功不代表SDK清洗后的子进程环境正确。
4. 先运行无付费控制链路与fixture wire探测，再运行真实模型。实际调用记录必须显示Harness、模型、
   执行地和用量；案例名称或提示词不能作为执行方证据。
5. 至少验证正常产物、模型/协议错误、依赖缺失、预算耗尽、页面重进、取消、控制断线、Executor中断、
   会话恢复、Token撤销及Sandbox删除。程序性首败保留，修复后只复测受影响项。
