import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {unusedAuthorizedAttachment,killedAttemptTakeover}=createRequire(import.meta.url)('./campaign_unified_hooks.cjs');

test('attachment fault cannot remove any original task input',()=>{
 const run={authorization:{allowed_assets:['original','supplement']},tasks:[{spec:{inputs:{source:{asset:'original'}}}}]};
 assert.equal(unusedAuthorizedAttachment(run),'supplement');
 run.tasks.push({spec:{inputs:{source:{asset:'supplement'}}}});
 assert.throws(()=>unusedAuthorizedAttachment(run),/unused/);
});
test('normal next poll does not establish a killed attempt takeover',()=>{
 const old={id:'old',run_id:'run',step_id:'generate',task_revision:0,generation:3,status:'finished'};
 const next={...old,id:'next',generation:4,status:'running'};
 const facts={attempts:[old,next]};
 assert.throws(()=>killedAttemptTakeover(old,facts),/normally completed/);
 old.status='running';assert.equal(killedAttemptTakeover(old,facts),null);
 old.status='expired';assert.equal(killedAttemptTakeover(old,facts),next);
 next.task_revision=1;assert.equal(killedAttemptTakeover(old,facts),undefined);
 next.task_revision=0;next.run_id='other';assert.equal(killedAttemptTakeover(old,facts),undefined);
});
