"""Shared vocabulary. Persisted v1 names are retained at compatibility boundaries."""
from enum import StrEnum

class WorkspacePolicy(StrEnum):
    WORKSPACE_READ_ONLY='WORKSPACE_READ_ONLY'
    TASK_OUTPUT_ONLY='TASK_OUTPUT_ONLY'
    REVIEW_SCRATCH_ONLY='REVIEW_SCRATCH_ONLY'
    FULL_PROJECT_EDIT='FULL_PROJECT_EDIT'

class PermissionProfile(StrEnum):
    PLANNING='planning'
    EXECUTION='execution'
    REVIEW='review'
    STEP='step'
    FIXED_TOOL='fixed_tool'

TERMINAL=frozenset({'succeeded','failed','cancelled'})
WAIT_REASONS=frozenset({'dependency','resource','provider','retry','user','model_unconfigured','model_unavailable','project_busy','user_limit','node_limit','cleanup','paused'})


def dispatch_kind(row):
    if row.get('dynamic'):return 'workflow'
    if row.get('manual'):return 'manual'
    return 'workflow' if row.get('workflow_status') in ('ready','staged','cancelling') else 'agent'


def profile(mode,*,review=False,step=False,fixed=False):
    if fixed:return PermissionProfile.FIXED_TOOL
    if step:return PermissionProfile.STEP
    if review:return PermissionProfile.REVIEW
    return PermissionProfile.EXECUTION if mode=='execute' else PermissionProfile.PLANNING


def classify_error(detail,phase):
    if isinstance(detail,dict):
        if detail.get('code')=='pre_submit' and detail.get('request_sent') is False:return 'pre_submit'
        if phase=='local' and detail.get('error_type')=='ModelResultError':return 'model_result'
        if phase=='local' and detail.get('code')=='output_contract':return 'code'
        if detail.get('error_type') in ('ValueError','ValidationError'):return 'parameters'
        report=detail.get('report',{})
        issues=report.get('issues',[])
        if issues:return issues[0].get('code','execution')
        if detail.get('code') in ('code_version','preflight'):return 'code'
    # A helper validation failure may be wrapped by the Agent's subprocess call.
    # Read only bounded structured error records, never infer success or retry an
    # external submission from script-provided output.
    if phase=='local' and isinstance(detail,dict):
        import json
        for line in str(detail.get('log_tail',''))[-32768:].splitlines():
            # A script may raise RuntimeError(captured_stderr); Python prefixes
            # the first diagnostic line in the traceback with the exception name.
            if line.startswith('RuntimeError: '):line=line[len('RuntimeError: '):]
            try:record=json.loads(line)
            except (ValueError,RecursionError):continue
            if isinstance(record,dict) and (record.get('stage')=='error' or record.get('event')=='tool.failure'):
                if (record.get('exception_type') or record.get('error_type'))=='ModelResultError':return 'model_result'
                if record.get('code')=='parameters' or (record.get('exception_type') or record.get('error_type')) in ('PlanError','ValueError','ValidationError'):
                    return 'parameters'
    text=str(detail).lower()
    if phase=='submit':return 'submission_unknown'
    if 'valueerror:' in text:return 'parameters'
    if 'syntaxerror' in text or 'nameerror' in text or 'typeerror' in text or 'assertionerror' in text:return 'code'
    if 'permissionerror' in text or 'permission denied' in text or 'read-only file system' in text or 'modulenotfounderror' in text or 'no module named' in text or 'no such file' in text:return 'environment'
    if 'no space left' in text or 'out of memory' in text:return 'resource'
    if '401' in text or '403' in text:return 'authorization'
    if '429' in text or 'rate limit' in text:return 'rate_limit'
    if 'timeout' in text or 'timed out' in text or '超时' in text:return 'timeout'
    if phase=='validate':return 'validation'
    if phase in ('poll','collect'):return 'network'
    return 'execution'
