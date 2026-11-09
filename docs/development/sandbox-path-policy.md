# 沙箱路径管理规范

代码编辑师的所有执行后端共享同一份路径契约。提示词只负责告诉 Harness 如何操作，宿主和 Runtime 仍须独立校验、过滤与验收，不能把模型遵守提示词当作安全边界。

## 固定路径

| 名称 | 值 | 生命周期 | 权限 |
| --- | --- | --- | --- |
| Workspace | `/workspace` | 单次沙箱 | 稳定 Shell 工作目录，不是清理目标 |
| 输入 | `VIDEO_USE_STEP_INPUTS` 中的绝对路径 | 冻结 Task revision | 只读；不得猜测、移动、改名或删除 |
| 输出 | `VIDEO_USE_STEP_OUTPUT` | 当前 Task Attempt | 唯一可持久化的任务目录；不得删除目录本身 |
| 临时区 | `VIDEO_USE_STEP_SCRATCH`，固定为输出目录下 `.scratch` | 当前 Attempt | 仅放可丢弃中间文件，不下载、不检查点化、不跨 Harness 恢复 |
| Runtime 状态 | `/state` | 受白名单约束的原生会话 | 只由 Runtime 管理，任务脚本不得用作成果目录 |

## 执行规则

1. Shell 的稳定工作目录是 `/workspace`。命令需要局部切换目录时使用绝对路径或子 Shell，不能让后续工具继承一个可能被删除的 cwd。
2. 输入路径只从 `VIDEO_USE_STEP_INPUTS` 解析。文件名、用户文本和工具输出都不能授予额外路径权限。
3. 脚本、候选成果、自检和需要保留的中间文件只能写在 `VIDEO_USE_STEP_OUTPUT` 下；声明成果必须是非空普通文件。
4. 可丢弃文件只能写在 `VIDEO_USE_STEP_SCRATCH`。不得自行创建 `/tmp/work` 或把 `/workspace` 的其他目录当作任务工作区。
5. 清理只能作用于解析并确认仍位于临时区之下的非符号链接后代。禁止递归删除空变量、通配符、cwd、父目录、输入、输出根和环境根目录。
6. 需要保留的临时结果先校验，再以同文件系统原子替换写入输出目录。发布、验收和恢复只认输出目录中的声明文件。
7. 结构化 bwrap 与原生 OpenSandbox Harness 必须注入相同的三个环境变量和同一份规则；新增 Harness 不得另造路径语义。

## 宿主强制项

- 启动前校验输出必须位于 `/workspace/edit/workflows/<run>/<task>/<attempt>` 命名空间。
- 输入挂载只读，输出目录逐级拒绝符号链接，成果下载使用目录 FD、`O_NOFOLLOW` 和临时文件原子替换。
- `.scratch` 在结构化脚本退出后清理；原生 Harness 的导出、恢复和下载都过滤该目录。
- 检查点只保存当前 Task 输出和白名单会话状态；跨 Harness 恢复不恢复临时区、凭据或其他 Task 文件。
- 最终成功仍须经过声明文件检查、技术验收和独立 Reviewer；Harness 正常退出不等于成果成功。

修改路径规则时，至少运行 `test_sandbox_path_policy`、`test_native_implementer`、`test_runtime_bootstrap`、`test_native_sessions`、`test_runtime_workspace`、`test_opensandbox_command` 和 `test_workflow_input_paths`。
