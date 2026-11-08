export const terminalRun = state => ['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(state);

export function canApproveDecision(decision, run) {
  if (['plan_change', 'successor_authorization', 'new_run'].includes(decision.kind)) return true;
  if (decision.kind !== 'quality') return false;
  const task = run.tasks.find(task => task.id === decision.request.task_id);
  return Boolean(task?.quality.technical_valid && task.spec.acceptance.some(rule => rule.kind === 'review') &&
    (!task.quality.requires_agent || task.quality.agent_review_passed));
}

export const acceptsFeedback = decision => ['quality', 'quality_feedback', 'input_question'].includes(decision.kind);

export function inputStatus(run) {
  if (run.scope_confirmed === false || run.scope_confirmed === 0) return '正在整理新的执行范围，确认后才开始处理。';
  if (run.terminal_reason === 'workers_unavailable') return '补充要求已保存，等待执行器恢复后继续规划。';
  if (run.terminal_reason === 'input_waiting_external_unknown') return '补充要求已保存，正在核实已有外部请求；不会重复提交。';
  if (run.terminal_reason === 'input_waiting_user_decision') return '补充要求已保存，等待处理当前确认事项。';
  if (run.terminal_reason === 'review_contract_exhausted') return 'Reviewer 未能在限定纠错轮次内提交合法选择；候选与检查证据已保留。';
  if (run.terminal_reason === 'planning_contract_exhausted') return 'Manager 未能在限定纠错轮次内提交合法计划；草稿与校验错误已保留。';
  if (run.terminal_reason === 'planning_budget_exhausted') return '本轮规划轮次已耗尽，请停止本轮并新建一轮。';
  if (run.terminal_reason === 'model_token_budget_exhausted') return '本轮模型 Token 预算已耗尽，请停止本轮并新建一轮。';
  if (run.terminal_reason === 'budget_exhausted') return '本轮执行预算已耗尽，请停止本轮并新建一轮。';
  if (run.terminal_reason === 'delivery_scope_change') return '交付范围需要确认；已有成果会保留，不会自动重新生成。';
  if (run.terminal_reason === 'delivery_targets_incomplete') return '当前成果与冻结交付范围不一致，请确认保留的交付内容。';
  if (run.terminal_reason === 'user_decision_pending') return '当前任务等待你的确认，确认后会继续处理。';
  if (terminalRun(run.state) && run.terminal_notice) return run.terminal_notice;
  if (run.state === 'FAILED') return '任务已失败；请查看下方失败原因和诊断信息。';
  if (run.input_barrier_sequence !== null) return run.activity === 'PLANNING'
    ? '正在根据已接收的要求重新规划；此时追加的要求会进入下一批。'
    : '补充要求已接收，等待当前任务及验收结束后重新规划。';
  return run.state === 'SUCCEEDED' ? '继续补充要求将创建下一轮执行，并重新确认范围。' : '可以持续补充要求；正在处理的任务会正常完成。';
}
