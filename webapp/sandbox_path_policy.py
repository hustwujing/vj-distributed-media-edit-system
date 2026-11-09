"""One path contract shared by every sandboxed code-editor harness."""
from pathlib import PurePosixPath


WORKSPACE_ROOT='/workspace'
TASK_OUTPUT_PREFIX='/workspace/edit/workflows'
SCRATCH_NAME='.scratch'


def _absolute_output(value):
    path=PurePosixPath(value)
    if not path.is_absolute():path=PurePosixPath(WORKSPACE_ROOT)/path
    prefix=PurePosixPath(TASK_OUTPUT_PREFIX)
    if ('..' in path.parts or not path.is_relative_to(prefix)
            or len(path.parts)<len(prefix.parts)+3):
        raise ValueError('Task output escaped the sandbox path policy')
    return path


def scratch_directory(output):
    return str(_absolute_output(output)/SCRATCH_NAME)


def is_scratch_path(path,output):
    candidate=PurePosixPath(path)
    if candidate.is_absolute():candidate=candidate.relative_to(WORKSPACE_ROOT)
    root=_absolute_output(output).relative_to(WORKSPACE_ROOT)/SCRATCH_NAME
    return candidate==root or candidate.is_relative_to(root)


def is_reserved_scratch_path(path):
    candidate=PurePosixPath(path)
    if candidate.is_absolute():candidate=candidate.relative_to(WORKSPACE_ROOT)
    return candidate.parts[:2]==('edit','workflows') and SCRATCH_NAME in candidate.parts[2:]


def contract(output=None):
    output_root=str(_absolute_output(output)) if output else '$VIDEO_USE_STEP_OUTPUT'
    scratch_root=scratch_directory(output) if output else '$VIDEO_USE_STEP_SCRATCH'
    return {
        'schema_version':1,
        'workspace_root':WORKSPACE_ROOT,
        'shell_working_directory':WORKSPACE_ROOT,
        'input_manifest_environment':'VIDEO_USE_STEP_INPUTS',
        'output_root':output_root,
        'scratch_root':scratch_root,
        'rules':[
            'inputs_are_read_only',
            'writes_stay_under_output_root',
            'temporary_files_stay_under_scratch_root',
            'shell_cwd_is_stable',
            'cleanup_is_scratch_descendants_only',
            'output_root_is_never_deleted',
            'scratch_is_not_checkpointed',
        ],
    }


def instructions():
    return (
        '\n路径契约：/workspace 是稳定Shell工作目录；不要把持久Shell切换到其他目录。需要在子目录运行命令时，'
        '使用绝对路径或子Shell `(cd "$dir" && command)`。输入只能从VIDEO_USE_STEP_INPUTS解析，均为只读；'
        '不得猜测、覆盖、移动、改名或删除输入及其父目录。所有脚本、候选成果、自检文件和中间文件只能写入'
        'VIDEO_USE_STEP_OUTPUT之下；不得删除、移动或改名VIDEO_USE_STEP_OUTPUT本身。临时工作文件只能放在'
        'VIDEO_USE_STEP_SCRATCH中，不得自行使用/tmp、/workspace下的其他目录或新的绝对路径作为任务工作区。'
        '清理前先回到/workspace；只可清理已确认resolve后仍位于VIDEO_USE_STEP_SCRATCH之下、且不是符号链接的后代，'
        '不得对空变量、通配符、输入路径、当前目录、父目录或任何环境根目录执行递归删除。'
        '临时区不会作为成果下载或跨Harness恢复；需要保留的内容必须在结束前原子写入VIDEO_USE_STEP_OUTPUT，'
        '并逐一验证声明成果是非空普通文件。'
    )
