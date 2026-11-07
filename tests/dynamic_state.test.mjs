import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {canApproveDecision,acceptsFeedback,inputStatus,terminalRun} from '../webapp/static/dynamic_state.js';

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
 assert.match(inputStatus({input_barrier_sequence:2,activity:'PLANNING'}),/下一批/);
 assert.match(inputStatus({input_barrier_sequence:null,state:'SUCCEEDED'}),/下一轮/);
 assert.equal(terminalRun('WAITING_USER'),false);
 assert.equal(terminalRun('CANCELLING'),false);
 assert.equal(terminalRun('SUCCEEDED'),true);
});

test('accepted artifacts expose reuse controls outside the hidden task audit container',()=>{
 const html=readFileSync(new URL('../webapp/static/dynamic.html',import.meta.url),'utf8');
 const source=readFileSync(new URL('../webapp/static/dynamic.js',import.meta.url),'utf8');
 assert.match(html,/<section id="dynamic-reusable"><\/section>/);
 assert.match(html,/<div id="dynamic-tasks" hidden><\/div>/);
 assert.match(source,/function renderReusable\(run\)/);
 assert.match(source,/renderDecisions\(run\);renderReusable\(run\)/);
 const legacy=source.slice(source.indexOf("$('dynamic-tasks').innerHTML"),source.indexOf('renderDecisions(run)'));
 assert.doesNotMatch(legacy,/data-reuse=/);
});
