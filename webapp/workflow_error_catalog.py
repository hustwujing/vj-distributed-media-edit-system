"""Canonical public meaning and recovery policy for workflow failures."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Failure:
    kind:str
    title:str
    message:str
    retry_policy:str
    user_action:str
    recovery_action:str
    recovery_reason:str
    surface_detail:bool=True


def failure(kind,title,message,retry,user_action,recovery,reason,surface_detail=True):
    return Failure(kind,title,message,retry,user_action,recovery,reason,surface_detail)


CATALOG={
    'submission_unknown':failure('external_state','外部请求状态未知','外部请求结果尚未确认，系统已暂停以避免重复调用。','reconcile_only','resolve_external_state','external_reconcile','External side effect state must be reconciled before any replacement call'),
    'result_unavailable':failure('external_state','执行结果不可恢复','执行已中断，缺少可安全恢复的结果。请先核实已有请求与产物。','reconcile_only','resolve_external_state','external_reconcile','External side effect state must be reconciled before any replacement call'),
    'poll_failed':failure('provider_transport','外部状态查询失败','查询外部任务状态失败；只会重试查询已有任务，不会重新提交。','same_external_handle','retry','retry_same_revision','The existing external handle can be queried again'),
    'collect_failed':failure('provider_transport','外部结果收取失败','获取已有任务结果失败；只会重试收取已有结果，不会重新提交。','same_external_handle','retry','retry_same_revision','The existing external handle can be collected again'),
    'provider_failed':failure('provider','上游处理失败','上游任务失败，系统不会自动重复提交可能收费的请求。','never_same_request','replan','manager_revision','A provider failure requires an adjusted immutable task revision'),
    'provider_cancelled':failure('provider','上游任务已终止','外部任务已终止，未产生可验收结果。','never_same_request','replan','manager_revision','A cancelled provider task requires a new plan'),
    'pre_submit':failure('contract','提交前检查失败','提交前检查未通过，尚未发起外部请求。请修订输入或参数后重新规划。','after_revision','replan','manager_revision','Pre-submit contract errors require a corrected immutable task revision'),
    'technical_validation_failed':failure('validation','技术验收失败','候选文件未通过技术检查，已暂停且尚未正式交付。','after_revision','replan','manager_revision','Technical validation requires a corrected result'),
    'quality_failed':failure('validation','内容验收失败','候选成果未通过内容验收，已暂停且尚未正式交付。','after_revision','replan','manager_revision','Quality validation requires a corrected result'),
    'review_incomplete':failure('review','审查证据不足','审查没有取得充分证据或有效结论；候选成果尚未正式交付。','after_revision','replan','manager_revision','Review evidence must be completed in a new revision'),
    'review_contract_exhausted':failure('contract','审查未完成','审查师在限定纠错次数内未提交合法结论；候选成果、检查证据和字段错误均已保留。请查看诊断；这不是缺少用户确认或素材。','never_same_revision','inspect_or_stop','user_decision','The Reviewer exhausted its bounded contract-correction attempts'),
    'planning_contract_exhausted':failure('contract','规划未完成','规划师在限定纠错次数内未提交合法计划；草稿和精确校验错误均已保留。请查看诊断；这不是缺少用户确认或素材。','never_same_revision','inspect_or_stop','user_decision','The Manager exhausted its bounded contract-correction attempts'),
    'model_result':failure('model_result','模型未产生合格结果','模型未找到符合条件的结果，请调整输入或要求后重新规划。','after_revision','replan','manager_revision','The model result did not satisfy the frozen task contract'),
    'agent_failed':failure('agent','代码编辑执行失败','代码编辑师未提交有效结果，工具与模型错误回执已保留。','safe_transient_only','inspect_or_replan','manager_revision','An Agent failure requires diagnosis and a new immutable revision'),
    'agent_timeout':failure('timeout','代码编辑师运行超时','代码编辑师达到本 Task 的墙钟运行时限，未发布候选成果，也不会自动重放同一 revision。','never_same_revision','replan','manager_revision','The Agent wall-clock limit requires a smaller or revised task'),
    'agent_turn_budget_exhausted':failure('quota','代码编辑师轮次已用完','代码编辑师已用完本 Task 的模型/工具轮次，尚未提交有效结果。','never_same_revision','replan','manager_revision','The Agent turn budget requires a smaller or revised task'),
    'agent_finalize_budget_exhausted':failure('quota','代码编辑师提交次数已用完','代码编辑师已用完最终提交纠错次数，已有输出和校验错误均已保留。','never_same_revision','inspect_or_replan','manager_revision','The Agent finalization budget requires a corrected task revision'),
    'agent_contract_exhausted':failure('contract','代码编辑命令连续无效','代码编辑师连续返回不符合工具或结果契约的命令，系统已停止重复纠错。','never_same_revision','inspect_or_replan','manager_revision','Repeated invalid Agent commands require a corrected task revision'),
    'agent_runtime_unavailable':failure('infrastructure','沙箱运行环境故障','沙箱或原生 Harness 无法启动或继续；这不是缺少业务输入。','after_runtime_repair','fix_runtime','stop','The runtime must be repaired before the task can run'),
    'runtime_profile_changed':failure('runtime_contract','执行环境版本已变化','旧 Runtime Profile 绑定不能由当前执行器继续运行。','after_revision','replan','manager_revision','A new immutable revision must bind the current Runtime Profile'),
    'policy_changed':failure('runtime_contract','Agent 策略已变化','旧控制任务不能由当前 Agent 策略继续执行。','after_revision','replan','manager_revision','A new immutable revision must bind the current Agent policy'),
    'execution_failed':failure('execution','系统执行失败','系统执行失败，已保留经过脱敏的底层错误和诊断。','policy_controlled','retry_or_replan','manager_revision','Execution evidence determines whether retry or revision is safe'),
    'invalid_result_manifest':failure('contract','交付校验失败','结果清单未通过范围或完整性校验，已阻止发布。请查看诊断；这不是缺少素材。','after_revision','replan','manager_revision','The result manifest must be corrected before publication'),
    'invalid_delivery_manifest':failure('contract','交付校验失败','最终审查或交付清单未通过校验，系统已阻止发布。请查看诊断；这不是缺少用户确认或素材。','after_revision','inspect_or_stop','user_decision','The final delivery manifest must be corrected before publication'),
    'missing_proposal':failure('contract','缺少可执行计划','规划轮次没有提交可执行计划。','after_revision','replan','manager_revision','A valid executable proposal is required'),
    'model_token_budget_exhausted':failure('quota','模型 Token 预算耗尽','本轮模型 Token 预算已耗尽，系统未继续发起模型请求。','never','increase_budget_or_stop','user_decision','The frozen workflow model-token budget is exhausted'),
    'budget_exhausted':failure('quota','本轮执行预算耗尽','本轮操作次数或非时间类执行预算已耗尽。','never','increase_budget_or_stop','user_decision','The frozen workflow operation budget is exhausted'),
    'daily_model_quota_exhausted':failure('quota','今日模型调用额度已用完','每日模型额度跨 Run 共享；新建一轮不会绕过额度。','after_quota_reset','wait','stop','Daily model quota is shared across Runs and must recover before retry'),
}

TASK_FAILURE_CODES=frozenset(CATALOG)-{'invalid_delivery_manifest'}
UNKNOWN=failure('unknown','任务未完成','任务未完成，系统保留了可用执行证据。','never','inspect','stop','No automatic recovery policy is registered',False)


def get(code):
    return CATALOG.get(code,UNKNOWN)


def message(code,detail=''):
    item=get(code);detail=str(detail or '').strip()
    if item.surface_detail and detail:
        detail=detail[:1200] if detail.endswith(('。','！','？','.','!','?')) else detail[:1200]+'。'
        return detail if item.message in detail else detail+item.message
    return item.message


def public_metadata(code):
    item=get(code)
    return {'failure_kind':item.kind,'failure_title':item.title,'retry_policy':item.retry_policy,
            'user_action':item.user_action,'known_error_code':code in CATALOG}
