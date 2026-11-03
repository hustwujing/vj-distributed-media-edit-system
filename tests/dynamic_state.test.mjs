import test from 'node:test';
import assert from 'node:assert/strict';
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
 assert.match(inputStatus({input_barrier_sequence:2,activity:'PLANNING'}),/下一批/);
 assert.match(inputStatus({input_barrier_sequence:null,state:'SUCCEEDED'}),/下一轮/);
 assert.equal(terminalRun('WAITING_USER'),false);
 assert.equal(terminalRun('CANCELLING'),false);
 assert.equal(terminalRun('SUCCEEDED'),true);
});
