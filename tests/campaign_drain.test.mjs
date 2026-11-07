import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {verifyDrainedAttempts,videoInterventionReady}=createRequire(import.meta.url)('./campaign_unified_hooks.cjs');

test('video intervention requires accepted sources while the current export is running',()=>{
 const run={tasks:['prepare_a','prepare_b','clip'].map(id=>({id,status:'succeeded',selected:1}))};
 run.tasks.push({id:'export',status:'running',selected:1});
 assert.equal(videoInterventionReady(run),true);
 for(const status of ['ready','running','reviewing','paused']){
  run.tasks[1].status=status;assert.equal(videoInterventionReady(run),false);
 }
 run.tasks[1].status='succeeded';run.tasks[1].selected=0;
 assert.equal(videoInterventionReady(run),false);
 run.tasks[1].selected=1;run.tasks[3].status='succeeded';
 assert.equal(videoInterventionReady(run),false);
});

function fixture(){
 return {
  boundary:{started:[{id:'work',revision:0,attempts:1}],input:{accepted_at:2}},
  run:{id:'run',tasks:[{id:'work',revision:0,attempts:1,status:'succeeded'}]},
  facts:{attempts:[{id:'original',run_id:'run',step_id:'work',task_revision:0,started:1,finished:3,status:'finished'}],
   business_results:[{run_id:'run',task_id:'work',task_revision:0,lease_id:'original',outcome:'succeeded',status:'committed',committed_at:4}],
   rejected_task_reviews:[]}
 };
}
function repaired(f){
 f.run.tasks[0].revision=1;
 f.facts.attempts.push({id:'repair',run_id:'run',step_id:'work',task_revision:1,started:5,finished:6,status:'finished'});
 return f;
}
const verify=f=>verifyDrainedAttempts(f.boundary,f.run,f.facts);

test('successful drained work keeps its revision and execution count',()=>{
 const f=fixture();assert.equal(verify(f)[0].id,'original');
 f.run.tasks[0].attempts=2;assert.throws(()=>verify(f));
});
test('a committed execution failure permits later recovery',()=>{
 const f=repaired(fixture());f.facts.business_results[0].outcome='failed';
 assert.equal(verify(f)[0].id,'original');
});
test('input alone cannot justify replacing a successful revision',()=>{
 assert.throws(()=>verify(repaired(fixture())));
});
test('cancelled, uncommitted or nonoverlapping attempts do not prove drain',()=>{
 for(const mutate of [f=>f.facts.attempts[0].status='cancelled',
  f=>f.facts.business_results[0].status='staged',
  f=>f.facts.business_results[0].outcome='cancelled',
  f=>f.facts.attempts[0].finished=1.5]){
  const f=fixture();mutate(f);assert.throws(()=>verify(f));
 }
});
test('only a rejected review of that same task revision permits recovery',()=>{
 const f=repaired(fixture());
 f.facts.rejected_task_reviews=[{run_id:'run',task_id:'work',revision:0}];
 assert.equal(verify(f)[0].id,'original');
 f.facts.rejected_task_reviews[0].revision=1;assert.throws(()=>verify(f));
});
test('recovery cannot start before the original failure is committed',()=>{
 const f=repaired(fixture());f.facts.business_results[0].outcome='failed';
 f.facts.attempts[1].started=3.5;assert.throws(()=>verify(f));
});
