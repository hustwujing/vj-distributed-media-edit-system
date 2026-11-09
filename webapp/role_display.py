"""Server-owned friendly names; internal role IDs remain audit-only."""

ACTORS={
    'orchestrator':'流程协调器','manager_agent':'任务规划师','reviewer_agent':'质量审查员',
    'diagnostician_agent':'故障诊断师','specialist_agent':'代码编辑师','executor':'媒体执行器',
    'sandbox_agent':'本机沙盒创作助手','remote_sandbox_agent':'远程沙盒创作助手',
    'light_agent':'需求助手',
}

CONTROL={'PLAN':'任务规划师','MESSAGE':'任务规划师 · 对话','REVIEW':'质量审查员','DIAGNOSE':'故障诊断师'}

def actor_name(actor,backend=''):
    if actor=='sandbox_agent' and backend in ('opensandbox','agent_runtime','command_runtime'):
        return ACTORS['remote_sandbox_agent']
    return ACTORS.get(actor,'创作助手')

def item_name(item):
    if item.get('control_kind'):return CONTROL.get(item['control_kind'],'流程控制助手')
    if item.get('type')=='agent':
        profile=(item.get('profile') or {}).get('id') or (item.get('spec') or {}).get('profile')
        return {'editing_implementer':'代码编辑师','visual_quality_reviewer':'质量审查员',
                'media_failure_diagnostician':'故障诊断师','media_planner':'任务规划师'}.get(profile,'专业编辑助手')
    if item.get('type') in ('operation','script'):return '媒体执行器'
    return '流程工作单元'
