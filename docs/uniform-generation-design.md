# 图片、音频与参考素材视频生成统一接口

## 上层契约

上层通过既有工作流提交 `kind: operation`，选择稳定业务 ID：

| 操作 | 输入 | 参数 | 输出角色 | 当前实现 |
| --- | --- | --- | --- | --- |
| `image.generate_from_text` | 无图片输入 | prompt、width、height、quality、fit | primary_image | Image API / gpt-image-2 |
| `video.generate_from_image` | 唯一 `source` 首帧 | prompt、width、height、duration、seed、generate_audio | video | Wan 2.6 / Seedance 2.0 |
| `video.generate_from_text` | 无参考素材 | prompt、width、height、duration、seed、generate_audio | video | Seedance 2.0 |
| `video.generate_from_frames` | `first_frame`、`last_frame` | prompt、width、height、duration、seed、generate_audio | video | Seedance 2.0 |
| `video.generate_from_references` | 编号的图片/视频/音频参考角色 | prompt、width、height、duration、seed、generate_audio | video | Seedance 2.0 |
| `video.edit` | `source` 视频，可选编号参考角色 | prompt、seed、generate_audio | video | Seedance 2.0 |
| `video.extend` | `source` 视频，可选编号参考角色 | prompt、duration、extend_direction、seed、generate_audio | video | Seedance 2.0 |
| `audio.generate_from_text` | 无参考素材 | text、voice_id、speed、audio_format | audio | MiniMax T2A |

不新增顶层调度类型，不要求 Agent 指定供应商 URL、模型环境、对象地址或密钥。`generation_service.py` 是服务端统一调用边界，提供 prepare、submit、poll、download、cancel；prepare 完成输入字节及参数校验，submit 在已保存调用意图后准备内容寻址参考并仅发起一次生成请求，返回统一状态及任务 ID 或同步产物。

## 参考 ai-tool 的范围

参考本机 ai-tool 的供应商 endpoint、请求/响应映射及已有配置。没有复制它的 TaskPool、积分系统、submit-and-poll 循环、后台线程或供应商失败后自动重新生成策略。本项目的工作流、租约、资源、调用配额、验收、修复及独立 reaper 继续负责生命周期。

文生图保持同步上游接口：请求期间占用执行槽，本地以网络等待为主。视频是异步上游接口：提交得到 ID 后结束本次尝试；调度器按 next_poll 重新领取步骤，执行一次查询后返回。多个任务独立推进，受现有用户、节点及外部并发限制。

## 持久化与恢复

先保存调用意图，再发起 POST。调用记录新增 implementation，保存 protocol、provider、model、operation、reference_sha256；不保存 API key 或参考图 base64。任务 ID 与原有步骤修订绑定。查询/收取/取消按已保存协议选择适配器，而不是用最新默认模型重新判断。配置地址变更后不向新地址发送旧任务凭据；要求恢复原配置或明确迁移。

参考素材从已验收并绑定修订的项目版本读取，重新核对摘要、格式、尺寸、时长、帧率、编码、单项和合计大小。Seedance 参考素材以 SHA-256 写入目标环境专属的 `aniforge/distributed-agent-sandbox/` BOSS 前缀，再以确定性名称查询或创建 Ark Asset；Provider 只收到 `asset://` URI。对象存储、Ark 和 Provider 凭据不进入计划、调用句柄或沙箱。创建回执丢失时按确定性名称查询已有 Asset，不重复创建；Asset 准备在生成 POST 之前失败时不产生视频生成调用。

`video.generate_from_references` 最多接收 9 张图片、3 段视频和 3 段音频；参考视频和音频各自合计不超过 15 秒，音频不能作为唯一模态。`video.edit` 与 `video.extend` 的 `source` 必须是唯一编辑/延长源，其他编号输入只作为参考。编辑使用源视频自适应画幅，延长方向必须显式冻结。

失败且明确拒绝与提交未知分开；没有证明上游幂等，就不能用同一个请求键自动重新提交。已有任务查询/下载重试不消耗新的生成授权。技术错误和上游错误代码记录在诊断中，错误正文、凭据和签名下载地址不进入用户诊断。

## 取消

取消信号先持久化，本地 HTTP 连接监视器约每秒检查任务与租约，shutdown/close 中断等待；执行环境由既有清理机制兜底。同步调用无法保证上游停止或未计费。异步调用已有 ID 时交由独立清理程序调用对应适配器。

Seedance DELETE 的明确 204 响应才视为确认；真实测试中任务立即进入运行并返回 InvalidAction.RunningTaskDeletion，适配器明确记录运行中不支持取消。其他结果仍需核实。当前 Wan 接入没有已验证的取消协议，明确返回 unsupported，不猜接口、不伪报取消成功。没有 ID 的提交未知状态不能假定已取消。

## 模型和真实服务验证

- 文生图网关模型目录成功响应；本次实际 gpt-image-2 生成人物图后经 Agent 内容验收。
- Seedance 文生视频真实返回任务 ID，查询成功并下载 1280×720 视频，完整解码通过。
- Seedance 图生视频对写实人物参考图明确返回 `InputImageSensitiveContentDetected.PrivacyInformation`；不作为人物图生视频默认实现。保留该失败记录，使用本次任务授权重新选用 ai-tool 已有 Wan 适配。
- Wan 图生视频及完整15秒案例测试结果见 [实施报告](workflow-implementation-report.md) 和 test-evidence/live-generation-case/result.json，不以提交成功代替生成成功。
- Seedance 测试5秒成片的容器时长为5.041667秒（24fps一帧量级），因此按±0.05秒记录测试容差；不循环、不定格、不拉伸视频凑时长。

服务配置存在不等于长期健康；能力清单保持 configured_unverified 的运行时语义，实际 smoke 证据只证明测试时服务可用。

## 资源、凭据和产物边界

凭据仅服务端私有配置，低权限网关及 reaper 按需接收，不进入 Agent 沙箱。下载只允许配置的 HTTPS 存储域名，禁止跳转；不会把 API Authorization 转发给对象存储。文件大小仍受统一收取上限，结果经隔离导入和技术验收，最终只交付明确选择的产物。

新增配置：`VIDEO_USE_GENERATION_BACKEND/URL/KEY/MODEL` 管理默认视频实现；`VIDEO_USE_IMAGE_VIDEO_BACKEND/URL/KEY/MODEL` 可覆盖图生视频；各自 DOWNLOAD_HOSTS 管理上游存储域名。参考模式另需宿主私有的 `VIDEO_USE_ARK_ACCESS_KEY/SECRET_KEY/PROJECT_NAME/ASSET_GROUP_ID`、Ark 控制地址及 `VIDEO_USE_MEDIA_REFERENCE_PUBLIC_ENDPOINT/PREFIX`。文生图使用既有 `VIDEO_USE_IMAGE_GENERATION_*`，文生音频使用 `VIDEO_USE_AUDIO_GENERATION_*`。配置文件不进入版本库。

## 验证清单

- 八个媒体生成入口的输入契约与上层能力清单一致；纯文本生成不能夹带参考素材。
- prepare 前置校验拒绝非法时长、尺寸、角色、类型、编码和参考总量；参数不能覆盖模型、地址或凭据。
- 一次 submit 只有一次 POST；返回 ID 后退出，不持有轮询循环。
- 持久化协议和模型，进程重启后查原 ID；明确拒绝和提交未知不混淆。
- 状态映射、产物缺失、错误代码、上游失败和未知状态均有处理。
- 下载验证域名/协议/大小且不转发 API 凭据；取消能力真实表达。
- 真实文生图、文生音频、文生视频、图生视频、首尾帧、多模态参考、视频编辑和视频延长案例；旧协议与整体回归。
