import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {verifyFeedbackRevision}=createRequire(import.meta.url)('./campaign_unified_hooks.cjs');
function baseline(){return {id:'run',tasks:[{id:'render',selected:true,revision:0,user_revision_count:0,status:'paused'},
 {id:'source',status:'succeeded',revision:0,attempts:1,quality:{files:[{name:'source.png',sha256:'unchanged'}]}}]};}
test('feedback accepts immutable same-task revision with user provenance',()=>{
 const before=baseline(),run=structuredClone(before);Object.assign(run.tasks[0],{status:'succeeded',revision:1,user_revision_count:1});
 assert.equal(verifyFeedbackRevision(before,run,'render').id,'render');
});
test('feedback follows explicit replacement without requiring the retired revision to change',()=>{
 const before=baseline(),run=structuredClone(before);run.tasks[0].selected=false;
 run.tasks.push({id:'repair',status:'succeeded',selected:true,revision:0,user_revision_count:1});
 run.replacements=[{old_task_id:'render',new_task_id:'repair'}];
 assert.equal(verifyFeedbackRevision(before,run,'render').id,'repair');
 for(const mutate of [r=>r.replacements=[],r=>r.tasks[2].user_revision_count=0,r=>r.tasks[0].revision=1,
  r=>r.tasks[1].attempts=2,r=>r.tasks[1].quality.files[0].sha256='changed']){
  const invalid=structuredClone(run);mutate(invalid);assert.throws(()=>verifyFeedbackRevision(before,invalid,'render'));
 }
});
