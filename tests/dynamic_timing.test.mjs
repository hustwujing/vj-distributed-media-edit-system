import test from 'node:test';
import assert from 'node:assert/strict';
import {duration,timingLabel,executionHistory,executionTimes,runTiming} from '../webapp/static/dynamic_timing.js';

test('live clocks count from persisted start; completed and interrupted clocks freeze',()=>{
 const a={started:100,finished:null,status:'running'};
 assert.equal(timingLabel(a,162),'已执行 1分2秒');
 assert.equal(timingLabel(a,99),'已执行 0秒');
 for(const status of ['finished','expired','interrupted']){
  const closed={...a,finished:165,status};assert.equal(timingLabel(closed,200),timingLabel(closed,900));
  assert.match(timingLabel(closed,200),status==='finished'?/结束/:/中断/);
 }
 assert.equal(timingLabel({...a,status:'finished'},200),'结束回执未记录');
 assert.equal(duration(3662),'1小时1分2秒');
});

test('retry history keeps the latest three actual attempts of the current revision only',()=>{
 const run={state:'ACTIVE',activity_attempts:Array.from({length:5},(_,i)=>({id:'a'+i,step_id:'s',task_revision:1,generation:i,started:100+i,status:'running'}))};
 run.activity_attempts.push({id:'old',step_id:'s',task_revision:0,generation:99,started:50});
 const item={id:'s',revision:1};assert.equal(executionHistory(item,run).length,5);
 const html=executionTimes(item,run);assert.doesNotMatch(html,/第1次|第2次|old/);assert.match(html,/第3次/);assert.match(html,/第5次/);
 run.state='FAILED';assert.equal(executionHistory(item,run)[0].status,'unconfirmed');
 assert.deepEqual(executionHistory({id:'unstarted'},run),[]);
 assert.equal(runTiming({state:'SUCCEEDED',created_at:10,updated_at:90,delivered_at:80}).finished,80);
});
