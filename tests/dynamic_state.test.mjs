import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {canApproveDecision,acceptsFeedback,inputStatus,terminalRun} from '../webapp/static/dynamic_state.js';
import {settleTool} from '../webapp/static/work_status.js';

test('user approval requires declared review, technical pass and required agent pass',()=>{
 const decision={kind:'quality',request:{task_id:'image'}};
 const task={id:'image',spec:{acceptance:[{kind:'review'}]},quality:{technical_valid:true,requires_agent:true,agent_review_passed:true}};
 assert.equal(canApproveDecision(decision,{tasks:[task]}),true);
 for(const modified of [{...task,spec:{acceptance:[{kind:'agent'}]}},{...task,quality:{...task.quality,technical_valid:false}},{...task,quality:{...task.quality,agent_review_passed:false}}])
  assert.equal(canApproveDecision(decision,{tasks:[modified]}),false);
 assert.equal(canApproveDecision({...decision,kind:'quality_feedback'},{tasks:[task]}),false);
 assert.equal(acceptsFeedback({...decision,kind:'quality_feedback'}),true);
});

test('unknown external submission and planning have distinct input waiting messages',()=>{
 assert.match(inputStatus({input_barrier_sequence:2,terminal_reason:'input_waiting_external_unknown'}),/核实.*不会重复提交/);
 assert.match(inputStatus({input_barrier_sequence:null,terminal_reason:'review_contract_exhausted'}),/Reviewer.*证据已保留/);
 assert.match(inputStatus({input_barrier_sequence:null,terminal_reason:'planning_contract_exhausted'}),/Manager.*校验错误已保留/);
 assert.match(inputStatus({input_barrier_sequence:null,terminal_reason:'model_token_budget_exhausted'}),/Token.*未继续/);
 assert.match(inputStatus({input_barrier_sequence:null,terminal_reason:'delivery_scope_change'}),/交付范围.*确认/);
 assert.match(inputStatus({input_barrier_sequence:null,terminal_reason:'delivery_targets_incomplete'}),/交付范围/);
 assert.match(inputStatus({input_barrier_sequence:null,terminal_reason:'user_decision_pending'}),/等待你的确认/);
 assert.match(inputStatus({input_barrier_sequence:null,state:'FAILED',terminal_reason:'review_contract_exhausted'}),/Reviewer.*证据已保留/);
 assert.match(inputStatus({input_barrier_sequence:2,activity:'PLANNING'}),/下一批/);
 assert.match(inputStatus({input_barrier_sequence:null,state:'SUCCEEDED'}),/下一轮/);
 assert.equal(terminalRun('WAITING_USER'),false);
 assert.equal(terminalRun('CANCELLING'),false);
 assert.equal(terminalRun('SUCCEEDED'),true);
});

test('terminal notices explain failure and cancellation after re-entry',()=>{
 assert.equal(inputStatus({state:'FAILED',terminal_notice:'任务预算已耗尽，系统停止了后续执行。'}),'任务预算已耗尽，系统停止了后续执行。');
 assert.equal(inputStatus({state:'CANCELLED',terminal_notice:'你已取消本次任务，已完成成果会保留。'}),'你已取消本次任务，已完成成果会保留。');
});

test('stale tool activity is visibly interrupted after worker loss or terminal run',()=>{
 const item={status:'running',execution:{attempt_id:'old'},sequence:1};
 assert.equal(settleTool({status:'running',activity_attempts:[{id:'old',status:'expired'}]},item).status,'interrupted');
 assert.equal(settleTool({status:'SUCCEEDED'},item).status,'interrupted');
 assert.equal(settleTool({status:'running',activity_attempts:[{id:'old',status:'running'}]},item),item);
});

test('dynamic activity renders an explicit interrupted state',()=>{
 const source=readFileSync(new URL('../webapp/static/dynamic.js',import.meta.url),'utf8');
 assert.match(source,/import \{settleTool\} from '\.\/work_status\.js'/);
 assert.match(source,/执行器中断，未收到结束回执/);
 assert.match(source,/state==='interrupted'\?'↯'/);
 assert.match(source,/error\('\'\);if\(!eventStream\)/);
});

test('accepted artifacts expose reuse controls outside the hidden task audit container',()=>{
 const html=readFileSync(new URL('../webapp/static/dynamic.html',import.meta.url),'utf8');
 const source=readFileSync(new URL('../webapp/static/dynamic.js',import.meta.url),'utf8');
 const css=readFileSync(new URL('../webapp/static/dynamic.css',import.meta.url),'utf8');
 assert.match(html,/<section id="dynamic-reusable"><\/section>/);
 assert.match(html,/<div id="dynamic-tasks" hidden><\/div>/);
 assert.match(source,/function renderReusable\(run\)/);
 assert.match(source,/renderDecisions\(run\);renderReusable\(run\)/);
 const legacy=source.slice(source.indexOf("$('dynamic-tasks').innerHTML"),source.indexOf('renderDecisions(run)'));
 assert.doesNotMatch(legacy,/data-reuse=/);
 assert.match(css,/:has\(#dynamic-context-settings\[open\]\)\{max-height:55vh;overflow-y:auto\}/);
});
