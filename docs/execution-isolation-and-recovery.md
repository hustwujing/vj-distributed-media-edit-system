# Execution isolation and restart recovery

Current development rules are in [distributed execution development](development/change-distributed-execution.md), configuration in [distributed execution configuration](distributed-execution-configuration.md), and deployment in [cluster operations](cluster-operations.md). The dated implementation and design sections below are historical context, not proof of current deployment or test results.

## Deployment boundary

Distributed mode uses a shared PostgreSQL database and S3-compatible encrypted
object storage. API and executor data directories are independent local caches;
Redis is optional realtime transport, not the authority for task ownership.
SQLite remains a local compatibility path. Do not put SQLite/WAL on NFS or use
separate databases and expect coordinated ownership. The built-in deployment is
single-host, multi-instance; it does not provide whole-host disaster recovery.

The API no longer launches manual jobs or changes running tasks at startup.
`python -m webapp.agent_host` handles both manual jobs and Agent workflows. It
can run manual jobs without model credentials; model availability is advertised
only when model credentials are configured.

## Identities and ownership

* Business user IDs continue to own projects and authorize every API operation.
* Every executor start registers a new UUID worker ID. Identical configuration
  across replicas is expected.
* A node ID is created once under `VIDEO_USE_EXECUTOR_ROOT`; this directory must
  be local to a machine and shared by its executor processes. Do not clone it
  into an image or share it between machines.
* Each claim creates an attempt ID and increments the task generation. Leases,
  attempts, call budgets and provider submission intents are durable DB records.
* Each local execution rents a numeric UID/GID. No Linux account per business
  user is needed. Live identity slots are protected by OS file locks.
* A separate read-only `video-use-runtime` group grants access to installed code
  and dependencies, never project data or credentials. This is the only extra
  group granted to isolated task processes.

The launcher rejects existing OS accounts/groups and subordinate-ID allocations
inside its reserved range. It reconciles abandoned slots at startup: kill the
recorded task cgroups, verify no UID processes remain, unmount scratch, remove
files, then free the slot. Cleanup errors quarantine the slot. DB lease expiry
alone never authorizes local UID reuse.

## Files, privileges and resources

Each execution receives a separate bounded workspace with a file-count limit.
The default is tmpfs; the optional disk backend uses a size-bounded ext4 image
and fails if its mount/quota prerequisites are unavailable. Input
assets, transcript cache and the latest committed workspace/state checkpoint are
copied into it. Staging copies run inside the task cgroup so tmpfs pages are not
charged to the long-lived coordinator. No mutable working directory is shared
between attempts or machines.

Bubblewrap provides private mount, PID, network, IPC and UTS namespaces. Sources,
transcripts, manifests, fixed tools and dependencies are mounted read-only.
Only attempt work/state and its preview output are writable. Generated code
runs as its rented UID/GID after no-new-privileges and capability removal.

`sandbox_limits.py` installs an inherited libseccomp policy after privilege
drop. It denies mounting, namespace creation/joining, ptrace/process-memory
operations, module loading, BPF/perf, keyring manipulation and similar operations.
Normal process/thread creation remains available. clone3 returns ENOSYS so libc
can fall back to the filtered clone call. This is a maintained deny policy, not
a claim that every Linux syscall is audited or that the host kernel is isolated.

Task CPU, memory/swap and process counts use cgroups (v1 and v2 supported).
Scratch has a hard byte/inode ceiling; `/tmp` is separately bounded. File-size,
file-descriptor and core-dump limits are applied before untrusted execution.
Task cancellation, lease loss and cleanup kill the entire task cgroup.

Background local workflow steps have a read-only workspace and only their new
output directory writable; they have no working model gateway. This enforces
the side-effect boundary required for safe retries, beyond an Agent-provided
`retry_safe` flag.

## Media and model tools

The unprivileged API probes media, generates video thumbnails and normalizes
imported images in a small read-only bwrap sandbox. It exposes one input file,
no project tree or host network, bounded temporary space and output descriptors.

Fixed image tools run in another namespace, with only their task inputs/output,
selected read-only model runtimes, and an assigned GPU where needed. GPU leases
are acquired by the trusted host. The generated-code sandbox never receives GPU
devices, model directories or the GPU lease registry.

A per-tool Unix socket relay exposes only a fixed provider's job API, only the
job created for that invocation. It ignores untrusted model parameter fields in
favor of the validated task, caps input/output sizes, and persists submission
intent before sending. An ambiguous submission is never silently repeated.
Provider URLs are configured by the operator, not returned by a model.

Main model/transcription credentials remain in the trusted gateway. Calls are
fenced and counted transactionally per task and per business user/day. These are
call budgets, not measured consumption. Token receipts and usage reporting are
handled separately by the [usage ledger](development/change-observability-usage.md);
neither is a financial billing system.

## Claim, heartbeat and fencing

`task_leases.Coordinator` is the ownership repository. PostgreSQL uses short
control transactions serialized by a transaction-scoped advisory lock; the local
SQLite path uses `BEGIN IMMEDIATE`. Both use the database clock. Claim and renewal verify
the owner, attempt, generation and unexpired lease. Multiple scanners may race;
only one transaction wins. Per-owner concurrency and project exclusion span
manual jobs and Agent tasks.

Each lease slot has at most one valid owner. Independent workflow steps have
separate slots; root Agent/review execution excludes step business execution.
Cancellation control has a separate revocation path. Task identity alone is not
a sufficient uniqueness key for concurrent workflow attempts.

Binding a Lease makes Store transactions validate that generation under the
same write lock as the mutation. Gateway handler threads explicitly bind the
lease as well. A stale executor cannot renew, overwrite status, mutate a canvas,
submit a new authorized tool call or publish a result. Attempts use distinct
filesystem locations, so stale writes cannot overwrite a successor's outputs.

This does not cancel requests already accepted by an external provider.
Submission intent, external IDs and provider idempotency guarantees remain
necessary. A lost DB connection fails execution closed; it is not permission to
continue locally or take ownership without a transaction.

When changing database transport or lock scope, preserve database time,
uniqueness and atomic publication. A read followed by an unconditional UPDATE
is not a valid replacement. Schema migrations must be serialized; shared
metadata and files must not depend on node-local paths.

## Checkpoints and recovery

Completed workspace/state checkpoints contain only regular, non-hardlinked
files, copied using directory descriptors without following symlinks. Files and
directories are flushed before the fenced DB pointer is committed. Checksums
and sizes are verified when restoring. The latest three checkpoint versions are retained by default (configurable
from two to ten); expired versions are removed only after a new pointer commits.
Deletion of prior edit files is blocked at checkpoint publication. The version
API supports explicit rollback after task resources have been reclaimed.
Eligible seed workspaces can be imported on first execution; this does not
migrate an old SQLite project's metadata into the deployed PostgreSQL cluster.

Local steps checkpoint before marking themselves succeeded. External downloads
are checkpointed before dependent steps can run. Paid jobs with known upstream
IDs resume polling; ambiguous submission windows pause. Interrupted unrestricted
Agent work cannot be assumed safe to replay. Structured Agent review requests
use their own bounded recovery path and revalidate snapshots before applying a
decision. Local steps retry only under their declared safety and attempt limits.
Synchronous calls reuse a saved result when available; an accepted call without
a durable result or a reliable upstream handle is not submitted again.

Recovery is at step boundaries, not at arbitrary Python instructions or video
frames. A partially encoded monolithic video may need to be rendered again.
Distributed checkpoints use shared objects and committed manifests. Parallel
steps save separate step/revision snapshots; they do not replace the project
snapshot. Losing a local disk without a durable checkpoint cannot be repaired
by the lease protocol.

Outputs are copied to service-owned publication directories outside task
mounts. Manual jobs publish an attempt-directory pointer and completion together.
Agent outputs remain accessible only through the owning turn's artifact list.
Cancelled tasks cannot be changed to successful by a late completion.

## 本轮实现与部署边界（2026-10-30）

从本节起至 “Release and shutdown” 之前保留 2026-10-30 实施与后续设计记录，包含当时的待办、限制和测试结果。当前修改按[专项开发索引](development/README.md)选择规则；这些历史条目不代表当前代码仍缺失相应能力，也不是当前验收通过的证明。

| 范围 | 实现及可验证行为 |
|---|---|
| 公开运行环境 | `runtime/public` 仅包含可公开 Skill/能力说明；`runtime_image.py` 构建显式程序和库清单。Agent 不挂载宿主完整 `/usr`、私有服务代码或原始 Skill，稳定使用 `/workspace`、`/opt/venv`。兼容旧脚本的路径别名只映射受限工作区。 |
| 受控出口 | `egress_policy.py` 拒绝主动远程图片/文件输入、外部 schema、内建联网工具、跨会话引用和元数据目的地址；校验所有 DNS 地址并连接到已校验 IP，关闭环境代理和重定向。合法内网服务由运营方名单指定。图片经过固定解码器归一化。 |
| 网关权限 | 模型 HTTP 网关和图片代理以 `video-use` 身份运行；通过继承的私有 socket 请求固定解码、抽音轨和图片工具，父端重新检查租约、资产及操作范围。无任意宿主命令接口。工作流协调器仍属于可信控制面。 |
| 输出与审计 | 检测已知密钥明文（包括跨块输出）、限制输入/响应和连接时长；策略拒绝持久化，默认累计 8 次拒绝终止任务。脱敏无法保证识别任意编码、改写或隐写，因此私密内容从源头不进入执行环境。 |
| 时间与容量 | 入队持久化 24 小时总截止时间、2 小时本地累计预算。续租预留并结算时间片，接管保守结算旧时间片；外部纯等待不耗本地预算。原有单步时限继续生效。节点并发额度包含尚未回收的尝试。 |
| 独立回收 | `execution_reaper.py` 单独服务；先检查本地单调时钟许可，再访问 DB。启动或运行时 DB 故障都不延长本地执行许可。用 pidfd/启动时间识别进程，处理旧 mount namespace。默认 60 秒未清完则隔离并停止节点接单，继续退避重试，不伪称释放。 |
| 外部收尾 | 已知 ID 由非特权收尾进程取消/确认，最多 3 次操作，每进程最多 12 秒；不提交生成请求。未知提交和无法确认停止均单独展示。 |
| 工作区版本 | 默认保留 3 个已提交版本，记录新增/修改/被阻止的删除，计入存储配额。旧 `edit` 文件删除不进入新检查点。恢复接口校验所属项目、任务与资源状态。 |
| 质量与修复 | `quality_policy.py` 保存契约；固定检查器校验格式、尺寸、时长、音轨、文本并记录摘要。语义/风格使用人工门禁。每步最多 2 次修复，收费重做需显式确认；重做只使受影响依赖链失效。 |
| 交互 | 查询进度和取消走控制 API，不新开 Agent；页面显示剩余预算、回收状态、上游未确认、候选结果、质量反馈、停止重规划和版本恢复。 |

系统仍共享宿主内核；未接入 gVisor/microVM，未宣称可以完全隐藏内核/库版本，
也未进行完整渗透测试。宿主补丁、镜像更新及节点物理容量仍须运维维护。
工作流的主观验收依赖用户，没有新增收费的自动视觉评审。
当时使用 SQLite 并验证多进程接管；此历史限制已由当前 PostgreSQL 与共享对象存储方案替代，实际部署仍需区分单机多实例与跨机容灾。

## 安全策略补充与对抗性验证

设计记录及实现更新：2026-10-30。以下保留设计依据；当前实现映射见本节前的
“本轮实现与部署边界”。代码和测试完成不等于线上已启用。SSH 掉线不在范围内。

### 安全目标与现有边界

允许 Agent 为编辑任务编写和执行代码，同时限制其可访问的数据、可产生的
外部影响和资源消耗。提示注入可能改变模型行为，权限边界仍须由可信服务
和操作系统执行，不能仅依赖模型拒绝请求。

已有代码提供项目鉴权、挂载隔离、独立执行身份、网络隔离、seccomp、资源
限制和受控网关。但这不等于 Skill 保密、环境特征完全隐藏或已经通过完整
渗透测试。实施后还需核验线上实际生效状态。

### 1. Skill、提示词与私有实现的边界

- 将内容分为可以公开的使用说明与服务端私有实现。沙盒内仅放可公开说明，
  不放密钥、内部部署信息、敏感业务规则或必须保密的算法实现。
- Agent 能读取的 Skill、脚本及其他文本，都应视为可能通过回答、附件、
  编码内容或图片被用户获取。“不要输出 Skill”以及禁止某个读取命令都不
  构成可靠保密措施；Python 等其他执行路径仍能读取相同内容。
- 需要保密的能力移至服务端受控接口，仅暴露经过授权的输入和必要结果。
  也不能把敏感内容放进模型上下文后再假设它一定不会被输出。
- 补充输出检查和脱敏，覆盖回答、错误、日志及可下载产物；将其作为辅助
  检测，不承诺识别所有编码、改写或隐写形式。可信控制面决定哪些内容
  可以进入沙盒和模型上下文，输出检查不能替代源头隔离。

### 2. 工具、依赖库与运行环境的暴露范围

- 提供经过审核的能力清单，让 Agent 知道可用工具和库，保留正常编程能力。
  探测沙盒内可用依赖本身不等于攻击，不以隐藏版本信息作为安全前提。
- 将当前较宽的系统目录和 Python 目录挂载逐步收缩为专用、固定版本的
  运行镜像。仅保留实际需要的程序、库、字体和公开说明。
- 使用稳定的沙盒路径，例如 `/workspace` 和 `/opt/runtime`，减少宿主用户名、
  真实安装路径、内部主机名、服务地址及部署结构在文档、配置和错误中的暴露。
- 用户无权改变宿主运行环境；确需新增依赖时，由受控构建流程准备新版本。
  仍允许任务在自己的受限目录内写代码；不能把删除 pip/curl 等工具当作
  防止代码执行或联网的安全边界。
- 接受部分库和系统特征可以被推断的事实。共享内核的沙盒不能保证隐藏
  内核版本，也不能因更改主机名就宣称完全隐藏宿主环境。

### 3. 网关及上游的间接网络访问

- 在现有直接网络隔离之外，逐项审计所有可信服务的代访问能力，包括模型
  输入中的图片/文件 URL、远程素材、代理配置、请求头、回调地址及上游响应。
  固定服务的 base URL 不等于请求内容不会触发其他网络访问。
- 默认不接受任意远程 URL，优先使用已授权素材 ID，由服务端解析为固定资源。
  必须支持远程获取时，明确协议、目标、端口、响应大小和时间限制。
- 对确需支持的 URL 获取，校验 DNS 解析结果和实际连接目标，处理 IPv4/IPv6、
  DNS 重绑定、地址混淆及重定向；默认拒绝未授权的内网、环回、链路本地和
  云元数据地址。合法内部模型服务作为运营方配置的独立例外，不由用户指定。
- 检查模型或其他上游服务是否会代抓取用户提供的地址；不具备可验证约束时，
  禁用该输入形式，不能只依赖本地沙盒网络规则。
- 网关必须绑定用户、任务、执行代次、资产和允许操作；禁止跨任务查询上游 ID。
  调用预算、并发、输入输出大小限制与网络策略同时生效。
- 明确外部模型属于数据出口。根据数据授权决定可以提交的内容，不能将
  “不直接联网”描述为“数据不会离开本机”。

### 4. 文件破坏、持久状态与版本保护

- 保留素材、已发布成果和其他用户数据的既有隔离；继续验证路径穿越、
  符号链接、硬链接、特殊文件和文件替换竞争不能绕过边界。
- 可写工作区允许正常修改和删除，因此被诱导的代码仍可能破坏本轮内容。
  现已增加受配额约束的多版本保留、删除保护及显式恢复；不承诺无限历史。
- 提交前生成新增、修改、删除清单，并由可信服务依据任务范围决定可以推广
  到后续状态的变化。禁止依靠 Agent 自行声明“安全”来扩大写入范围。
- 对历史记录、计划和持久状态中的文本继续按不可信数据处理，避免一次提示
  注入通过后续恢复和自动续跑长期生效。
- 出现异常删除或污染时，隔离本次产物并从可信检查点恢复；保留已发布成果
  和必要审计记录，避免静默覆盖历史版本。

### 5. 控制面、资源滥用与逃逸防护

- 进一步缩小特权启动器职责，将普通业务和网关逻辑与特权资源操作分离；
  对特权接口执行身份认证和参数约束，不提供任意宿主命令执行入口。
- 在单任务限制之外核算节点总容量，为控制面保留资源；审计大量连接、输出、
  日志、文件和进程是否能把消耗转移到沙盒外的可信服务。
- 将时间预算、独立回收器和清理告警与下一节设计联动。发现越权尝试或异常
  消耗时支持终止任务、记录事件及限制来源，不能把普通依赖查询一概当作攻击。
- 维护运行镜像、依赖和内核安全更新，复核 seccomp 与实际工作负载兼容性。
  面向公网不可信代码时评估 gVisor/microVM；这是待评估的更强隔离方案，
  不表示当前已经具备独立内核或虚拟机边界。

### 对抗性验收

- 在用户输入、文件名、字幕、素材文本和持久记录中放入提示注入，验证不能
  扩大文件、网络、工具和项目权限。
- 尝试读取 Skill、列举工具及依赖：公开内容可被读取，私有实现和密钥必须
  不在可访问环境或模型上下文中；不能用“模型回答拒绝了”代替权限验证。
- 验证普通读取、Python 读取、编码输出及附件等路径不会暴露未授权数据。
- 对直接连接和上游代抓取分别测试内网/元数据地址、重定向、DNS 变化及
  跨任务上游 ID，验证网关策略无法被绕过。
- 验证越权文件访问失败，当前工作区破坏可回滚，未经允许的删除不会进入
  下一轮状态；测试链接、特殊文件及文件替换竞争。
- 在受控测试环境中验证进程、内存、磁盘、连接、日志和时间耗尽后的隔离与
  回收，确认其他任务及控制面仍可工作。记录实际测试范围与未覆盖的风险，
  不将功能测试通过等同于完整安全认证。

## 任务总时限、累计预算与清理期限

实现更新：2026-10-30。`task_lifecycle.py` 持久化截止时间、时间片和回收状态，
当时设计采用 `execution_reaper.py` 独立回收并保留 SQLite；当前共享数据库配置见本文开头，接管协议仍不依赖进程内状态。

### 现有边界

当前限制主要作用于阶段：普通 Agent 单轮 15 分钟，手动任务执行 2 小时，
后台本地步骤默认 15 分钟、可配置至 1 小时，外部异步任务等待超过 24 小时
暂停查询。输入准备、部分工具调用也有独立超时。

这些阶段时限之外，现增加总时限及累计预算，覆盖排队、步骤、重试和发布，
回收期限独立计算。45 秒执行租约仅用于所有权和失联检测，持续
续租不能被视为任务可以无限执行的授权。

### 1. 持久化总截止时间

- 入队时由服务端策略确定并持久化 `deadline_at`，使用数据库时间。
  覆盖排队、输入准备、执行、外部等待、重试及发布；清理另设期限。
- 重启、重新领取、跨节点接管和普通“继续”操作均不得重置截止时间。
  如支持延长，必须由可信服务授权、记录变更，并重新检查用户额度。
- 领取、续租、工具调用、步骤转换和最终发布都检查剩余时间。
  单次操作期限不得超出任务剩余时间；禁止截止后再开始新步骤或收费请求。
- 排队过期时直接结束，不分配 UID 或创建沙盒。执行中过期时禁止后续操作，
  进入终止和清理流程。已提交的成果、检查点和已知上游任务 ID 保留。
- 发布与截止判断必须在同一受保护事务中处理，旧执行器或迟到结果不能把
  超时任务改回成功。默认总时限 24 小时、本地累计预算 2 小时，均由服务端配置，不向任务暴露修改权。

### 2. 跨尝试的累计执行预算

- 持久化任务总执行预算、每次 attempt 的已计费运行时长及结算位置。
  初始定义为本地执行环境从准备到发布占用的墙钟时间；排队和纯外部等待
  不计入本地运行预算，但仍计入总截止时间。CPU 用量另作资源指标。
- 通过事务预留/结算时间片并限制续租，防止并发接管、重复心跳或崩溃导致
  重复扣减或遗漏消耗。失联尝试保守结算已预留时间，不能简单归零。
- 重试和接管沿用剩余预算；即使每一步都未超时，也不能无限重试。
  累计预算、单步超时、尝试次数和调用额度同时有效，先达到的限制先终止执行。
- 用户主动取消、任务总超时、单步超时、预算耗尽、发布关停和租约丢失须记录
  不同原因；不能把总超时当成可无条件自动重跑的执行器故障。

### 3. 清理期限与独立回收器

- 终止时保存 `cleanup_started_at`、`cleanup_deadline_at`、清理阶段、
  所属节点、attempt、UID/GID、cgroup、挂载及 GPU 分配等资源记录。
  实际表为 `execution_cleanup`，本地记录另保存单调时钟许可、启动身份和资源信息。
- 先阻止新调用和结果提交，发送 SIGTERM，给予有限退出时间；必要时对整个
  任务 cgroup 使用 SIGKILL。随后核实进程退出，再清理 socket、挂载和临时文件。
- UID、GPU 分配及其他可能被残留进程使用的独占资源，必须在确认退出和清理
  成功后才归还。不能仅因数据库状态已超时就释放资源；需审计现有释放顺序。
- 由独立于执行任务的节点回收器继续处理超期清理，包括执行器自身卡死或
  被杀的情况。失败记录保留、重试退避、告警，并可将节点停止接单。
- 无法终止的内核进程、失败的卸载或失联节点都不能视为已经释放。
  保持资源隔离，节点恢复后核对记录、实际进程与挂载，避免误杀复用的 PID
  或误删新任务数据。清理期限是升级处理的阈值，不是强行宣称回收成功。
- 外部任务超时后，对已知 ID 尝试取消并记录结果；取消失败或提交结果未知时
  保留待核实状态。后续查询/取消由有独立预算的收尾流程处理，不能无限续租原任务，
  也不能宣称上游已经停止或自动重新提交。

### 验收用例

- 排队过期不创建沙盒；重启和跨执行器接管不重置截止时间或累计预算。
- 多次安全重试触及总预算后停止；重复结算及并发接管不造成预算错误。
- 截止与发布竞争时只有合法状态能提交；已发布成果仍可访问。
- 模拟执行器卡死、SIGTERM 无响应、卸载失败和节点失联，独立回收器仍能
  记录、重试并告警；未经确认不得复用 UID/GPU。
- 上游取消失败或提交结果未知时，界面显示真实状态，不重复生成或计费。

## 步骤质量验收与有限自动修复

实现更新：2026-10-30。验收条件、检查报告、步骤版本、修复计数和用户反馈均已持久化。

### 能力与边界

现有本地步骤检查命令退出码、指定输出是否存在且非空，以及常见图片和
音视频的基本有效性。失败会阻断依赖步骤；可安全重试的本地步骤有限重试，
不确定或可能产生外部费用的操作不会盲目重新提交。

脚本可以自行加入业务检查，Agent 也会在后台流程完成后检查成果并做必要
的本地修正。但“文件有效、命令成功”不等同于“内容和效果符合预期”。现通过
固定检查器校验可计算条件；语义、布局、风格等以明确的人工验收门禁处理，
不宣称已经自动理解所有业务质量。已声明本地修复可自动执行，其他调整需明确授权。

### 验收与步骤推进

- 区分技术有效性与业务质量。技术检查覆盖格式、文件完整性、尺寸、时长、
  音轨等；业务检查覆盖已确认的字幕、布局、内容、风格和其他交付要求。
- 在执行前保存验收条件及其版本，关联用户已确认的方案。可确定计算的条件
  使用固定检查器；需要视觉/语义判断的条件使用受限检查流程或用户验收。
  不以生成步骤自身的“已完成”声明作为独立验收证据。
- 步骤产物先保存为候选结果，再进行验收。只有通过规定验收的结果才允许
  推进依赖步骤或进入正式交付；不合格候选和诊断材料受权限、配额及保留期限约束。
- 验收记录包含所检查的产物版本/摘要、条件版本、检查器版本、结论和证据。
  模型判断应允许“不确定”，不能伪造客观分数或把模型认可视为绝对正确。

### 有限修复与重算

- 不通过时保存具体原因，例如字幕越界、时长不符或主体被裁切；区分临时
  执行故障与逻辑/质量问题，不能反复运行相同错误脚本冒充修复。
- 对已确认范围内、可验证且风险受限的本地修正，可提出并执行修复计划。
  保存调整的脚本/参数、修复原因和新产物版本，然后重新验收。
- 重做步骤后，标记其依赖链上受影响的结果失效，并重新计算这些后续步骤；
  不相关且仍有效的已验收步骤保留。不能将旧版本的下游结果与新版本输入混用。
- 任务中断或接管后，从持久化的验收/修复状态继续；沿用执行代次和发布校验，
  拒绝旧执行器提交的过期验收结论或修复结果。
- 修改原先不可替换的工作流计划，需要新增受控的修复版本协议，不能直接绕过
  当前的计划不可覆盖约束。原确认方案和历次修复依据均保留可追溯记录。

### 预算、授权与暂停

- 配置并持久化修复次数和修复预算；与任务总截止时间、跨重试累计运行预算、
  模型调用额度及存储配额共同约束，重启和接管不能重置。
- 外部重新生成必须满足已有授权和剩余预算，并沿用提交意图、幂等/未知结果
  处理规则；不能因质量未达标就无条件重新调用收费服务。
- 改变已确认方案、扩大素材或修改范围、超出获准预算，以及无法可靠判断如何
  修复的情况，暂停等待用户决定。达到修复次数或时间上限也应暂停，不无限循环。
- 用户否决成果或提供新要求时，保存反馈和对应产物版本，在明确的新授权下
  继续处理；保留已发布成果，不静默覆盖历史版本。

### 前台交互

- 展示“执行完成”“验收中”“验收未通过”“修复中”“等待用户确认”及
  “验收通过”的区别；步骤使用 `verifying`、`awaiting_review`、`succeeded` 等状态，
  修复由步骤版本和持久化 `repair_count` 标识。
- 显示不合格原因、可理解的证据、当前修复动作及次数，说明哪些后续步骤在等待。
  可展示候选结果和前后对比，但不得将未验收结果标成最终交付。
- 提供适用的继续修复、调整要求和停止入口。查询进展及取消沿用独立控制接口，
  不要求重新启动一次编辑对话才能查看验收状态。

### 验收用例

- 有效媒体但字幕越界、尺寸/时长不符或音轨缺失时，业务检查能阻断后续步骤。
- 临时执行重试与质量修复分别记录；修复有效时重新验收，无效时达到上限后暂停。
- 重做上游步骤只重算受影响的依赖链，拒绝旧产物或旧验收结论被再次发布。
- 在验收、修复、发布各阶段强制重启或接管，状态、授权和预算均不丢失或重置。
- 超出方案/预算、模型判断不确定、上游提交结果未知时，不能静默重新生成。
- 用户能查看原因与候选结果、取消修复，并继续访问之前已发布的成果。

## Release and shutdown

SIGTERM/SIGINT request draining. An executor stops claiming work, continues
heartbeats for active tasks, and exits when those tasks finish. At the configured
drain deadline it interrupts them and leaves durable recovery intent. Shutdown
is distinct from a user cancellation.

Use immutable release directories/images and set `VIDEO_USE_RELEASE` explicitly
for production. This release label records provenance; it is not itself a hard
takeover compatibility check. Local/script steps bind a runtime fingerprint
during claim, and model operations retain their selected implementation. Check
those bindings and persisted contracts before recovery or migration. Do not
change installed Python/tools underneath active workers.

The first migration from the old launcher requires draining/stopping **all old
launchers and embedded API workers** before starting the new version. Old code
does not honor execution leases and cannot safely coexist with the new protocol.

The built-in deployment freezes and verifies source, backs up state, drains and
stops the old services, then updates and starts the new release as documented in
[cluster operations](cluster-operations.md). It is not an automatic rolling
upgrade. API restarts do not own or kill worker task processes.

## Installation and operations

Prerequisites: Linux namespaces, bubblewrap, socat, libseccomp, ACL tools, FFmpeg,
and writable task cgroups for the trusted launcher. The API may use the configured
fixed media service through `VIDEO_USE_MEDIA_SOCKET` when unprivileged user
namespaces are disabled; that service still decodes in a restricted sandbox.
Missing isolation prerequisites fail closed.

```sh
# Root: provision the node registry and read-only runtime ACLs.
bash deploy/prepare-executor.sh
# On nodes serving the configured local image models:
bash deploy/prepare-executor.sh --models

# Load the service's database/storage configuration and use its local data directory.
.venv/bin/python -m webapp.executor_admin status
.venv/bin/python -m webapp.executor_admin drain WORKER_UUID
```

Apply the updated systemd units when deploying. The launcher needs write access
to its local registry and task cgroups; these paths are not mounted into tasks.
The API remains unprivileged. Model HTTP gateways and image relays now run as
the service user, using inherited private channels for fixed privileged tools.
The launcher and workflow coordinator remain trusted control-plane code; this
is neither a remote privileged-launcher service nor a VM boundary.

Transcript caches restored from shared storage may be owned by root. The
unprivileged gateway reads these caches without changing their ownership or
mode; it sets the initial mode only for files it creates. The existing
`refresh_transcript` broker operation rechecks the lease, operation scope and
asset, rejects links and non-regular files, normalizes the cache mode to `0640`,
and publishes/stages the transcript using the existing storage and sandbox
paths. The sandbox copy remains owned by the task UID with mode `0600`.
Filesystem permission failures return HTTP 500 with
`filesystem_permission_denied`, without host paths or raw exception messages;
they are not reported as an upstream 502 or retried automatically.

Run the node reaper independently of the executor. The current independent
deployment uses `dynamic-agent-watchdog@a/b` with the corresponding
`dynamic-agent-executor@a/b` instances; use the units and directory layout from
`deploy/dynamic_cluster.py`. Preserve live runtime records until verified cleanup.
The preparation script rebuilds the curated runtime
manifest after dependency changes. Runtime prerequisites are fail-closed; do not
bypass the reaper or fall back to unsandboxed execution.

On cleanup quarantine, inspect the registry's `records/` and `runs/`, cgroup
membership and watchdog journal first. Restore connectivity/killability or
mount cleanup, then let the reaper reconcile. Only after resources are verified
gone may an administrator remove the node's `node-quarantined` marker and start
a fresh worker. Never clear identity records to force reuse of a live UID/GPU.
Use structured reaper/cleanup events and the existing collector as described in
[observability operations](observability-operations.md). External notification
recipients are not automatically configured.

| Configuration | Default | Meaning |
|---|---:|---|
| `VIDEO_USE_DATA_DIR` | repository workspace | Local cache in distributed mode; local DB/files in compatibility mode |
| `VIDEO_USE_EXECUTOR_ROOT` | `/var/lib/video-use-executor` | Private node-local identity registry and scratch mounts |
| `VIDEO_USE_RELEASE` | `local-v1` | Release provenance label; runtime compatibility uses separate bindings |
| `VIDEO_USE_CONCURRENCY` | 2 | Executing actions per worker |
| `VIDEO_USE_NODE_CONCURRENCY` | worker concurrency | Shared active-action ceiling across replicas on one node |
| `VIDEO_USE_USER_CONCURRENCY` | 2 | Active actions per business user across workers |
| `VIDEO_USE_QUEUE_LIMIT` | 100 | Queue admission limit |
| `VIDEO_USE_USER_QUEUE_LIMIT` | 4 | User queue admission limit |
| `VIDEO_USE_LEASE_SECONDS` | 45 | Lease lifetime; renewal every one-third lifetime |
| `VIDEO_USE_TASK_TTL_SECONDS` | 86400 | Whole-task deadline from enqueue; persisted once |
| `VIDEO_USE_TASK_BUDGET_SECONDS` | 7200 | Cumulative local runtime across attempts |
| `VIDEO_USE_CLEANUP_SECONDS` | 60 | Escalate incomplete cleanup and quarantine node |
| `VIDEO_USE_CHECKPOINT_VERSIONS` | 3 | Retained workspace versions; clamped to 2–10 |
| `VIDEO_USE_MAX_REPAIRS` | 2 | Repair ceiling per step; never exceeds declared alternatives for automatic repair |
| `VIDEO_USE_SECURITY_DENIAL_LIMIT` | 8 | Repeated rejected model-input requests stop the task |
| `VIDEO_USE_INTERNAL_PROVIDER_HOSTS` | localhost,127.0.0.1,::1 | Explicit operator-approved private provider hosts; metadata/link-local remain forbidden |
| `VIDEO_USE_PROVIDER_REQUEST_SECONDS` | 240 | Provider connection lifetime cap, additionally bounded by task deadline |
| `VIDEO_USE_DRAIN_SECONDS` | 900 | Graceful release deadline |
| `VIDEO_USE_UID_START` / `VIDEO_USE_UID_COUNT` | 300000 / 128 | Reserved node-local identity pool |
| `VIDEO_USE_TASK_MEMORY_BYTES` | 12 GiB | Task cgroup memory limit |
| `VIDEO_USE_WORKSPACE_STORAGE` | tmpfs | Bounded tmpfs or disk workspace backend |
| `VIDEO_USE_TASK_DISK_BYTES` | 8 GiB | Workspace byte limit; tmpfs consumes memory, disk preallocates an ext4 image |
| `VIDEO_USE_TASK_CPUS` / `VIDEO_USE_TASK_PIDS` | 2 / 128 | CPU quota and process/thread ceiling |
| `VIDEO_USE_USER_STORAGE_BYTES` | 20 GiB | Application quota for registered assets, checkpoints and new published results |
| `VIDEO_USE_TASK_MODEL_CALLS` | 40 | Model calls per Agent turn |
| `VIDEO_USE_USER_MODEL_DAILY` | 200 | Model calls per user/UTC day |
| `VIDEO_USE_USER_IMAGE_DAILY` | 40 | Image-tool calls per user/UTC day |
| `VIDEO_USE_IMAGE_EDIT_URL` / `VIDEO_USE_IMAGE_DECOMPOSE_URL` | local providers | Operator-selected upstream services |

Size memory/scratch/concurrency together. tmpfs consumes memory; the disk backend
requires loop mounts, ext4 tools and sufficient provisioned space, with the same
cleanup contract. Deployment overrides can differ from the code defaults above.
Long-term storage lifecycle and cluster-wide physical capacity still require
operator provisioning. The application retention quota is checked transactionally
when committing uploads/checkpoints/results, not a filesystem reservation for
in-flight copies. Legacy manual outputs without recorded sizes and crash-orphaned
files require reconciliation before treating these counters as physical usage.

## Verification

Tests exercise separate processes competing for one task, expired renewal and
stale-write rejection, persistent call budgets, real UID/cgroup/seccomp execution,
scratch exhaustion, safe checkpoint copying, real worker SIGKILL and takeover,
draining, isolated image processing, and real FFmpeg rendering through the new
manual executor. Mock provider tests cover existing-job polling and ambiguous
submissions without invoking paid providers.

Additional checks exercise the real Codex app-server initialization inside the
curated runtime (no model request), public Skill/private implementation boundaries,
unprivileged HTTP-to-tool brokerage, deadline/budget persistence, independent
process termination during DB outage, retained versions and rollback, fixed
dimension checks versus human review, revision invalidation, bounded external
cancellation, owner-checked control APIs and physical-cleanup deletion guards.
Frontend tests keep unaccepted candidates out of primary delivery and distinguish
local cleanup completion from unconfirmed upstream cancellation.

```sh
.venv/bin/python -m tests.run_distributed_suite
node --test tests/*.test.mjs
```

Use the isolated runner and prerequisites in [development and testing](development.md).
The test descriptions above identify coverage areas, not results for the current
working tree.
