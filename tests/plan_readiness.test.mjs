import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
const source=await readFile(new URL('../webapp/static/plan_readiness.js',import.meta.url),'utf8');
const {executablePlan,planNotice}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
test('successful greetings and unanswered clarification do not enable execution',()=>{
 for(const state of ['none','pending','needs_information','discussion','stale','consumed','unavailable'])assert.equal(executablePlan([{mode:'plan',status:'succeeded',plan_readiness:{state,can_execute:false}}]),null);
});
test('only a server-audited successful plan can be confirmed, even after later discussion',()=>{
 const ready={id:'plan',mode:'plan',status:'succeeded',plan_readiness:{state:'ready',can_execute:true}};
 assert.equal(executablePlan([ready,{mode:'plan',status:'succeeded',text:'你好'}]),ready);
 assert.equal(executablePlan([{...ready,status:'running'}]),null);assert.match(planNotice({plan_readiness:{state:'stale'}}),/变化/);
});
