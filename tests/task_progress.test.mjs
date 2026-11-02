import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
const source=await readFile(new URL('../webapp/static/task_progress.js',import.meta.url),'utf8');
const {qualityActions,lifecycleText}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
test('approval requires passed technical checks and a live task',()=>{
 const step={status:'awaiting_review',quality:{technical_valid:false,passed:true}};
 assert.deepEqual(qualityActions(step,{}),['reject']);
 step.quality.technical_valid=true;
 assert.deepEqual(qualityActions(step,{}),['approve','reject']);
 assert.deepEqual(qualityActions(step,{termination_reason:'deadline'}),[]);
});
test('local cleanup does not claim upstream cancellation succeeded',()=>{
 const text=lifecycleText({termination_reason:'cancelled',cleanup:{state:'complete'},external_cleanup:[{state:'unconfirmed'}]});
 assert.match(text,/资源已回收/);assert.match(text,/上游是否停止尚未确认/);assert.doesNotMatch(text,/预算停止/);
 assert.match(lifecycleText({termination_reason:'security',cleanup:{state:'quarantined'}}),/安全策略/);
});
test('Agent review and diagnosis have distinct progress labels',async()=>{
 const {stepLabels}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
 assert.equal(stepLabels.agent_review,'Agent 验收中');
 assert.equal(stepLabels.diagnosing,'Agent 诊断中');
 assert.equal(stepLabels.verifying,'程序验收中');
});
test('temporary model outage and unavailable compute retain truthful wait labels',()=>{
 assert.match(lifecycleText({scheduling:{wait_reason:'model_unavailable'}}),/等待模型服务恢复/);
 const waiting=lifecycleText({scheduling:{wait_reason:'resource'}});
 assert.match(waiting,/等待计算资源/);assert.doesNotMatch(waiting,/模型未配置/);
});
test('manual async jobs display provider wait rather than generic queue',async()=>{
 const {jobPhaseLabel}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
 assert.equal(jobPhaseLabel({status:'queued',phase:'waiting'}),'等待外部结果');
 assert.equal(jobPhaseLabel({status:'failed',phase:'failed'}),'未完成');
 assert.match(lifecycleText({cleanup:{state:'complete'},external_cleanup:[{state:'unsupported'}]}),/不支持取消/);
});

test('quota pause guidance uses structured scope and actual counts',async()=>{
 const {pauseReason}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
 assert.equal(pauseReason({error:'quota exhausted'}),'');
 const task=pauseReason({resume_block:{scope:'task',category:'model',used:3,limit:3}});
 assert.match(task,/3\/3/);assert.match(task,/已保留/);assert.match(task,/新的收尾方案/);assert.doesNotMatch(task,/继续安全步骤/);
 const daily=pauseReason({resume_block:{scope:'daily',category:'image',used:2,limit:2}});
 assert.match(daily,/今日工具/);assert.match(daily,/每日额度恢复/);assert.doesNotMatch(daily,/新的收尾方案/);
});
