import test from 'node:test';
import assert from 'node:assert/strict';
import {chooseModel,choiceIsCurrent,configurationText} from '../webapp/static/dynamic_models.js';

const choices={default_profile:'claude',default_effort:'high',profiles:[
 {id:'claude',model:'claude-4.6-sonnet',available:true,default_effort:'high',reasoning_efforts:['low','medium','high'],profile_digest:'a'.repeat(64)},
 {id:'ds',model:'deepseek-v4.1-flash',available:true,default_effort:'high',reasoning_efforts:['low','high','max'],profile_digest:'b'.repeat(64)}]};

test('server default and a valid user choice survive ordinary refresh',()=>{
 assert.equal(chooseModel(choices).profile,'claude');assert.equal(chooseModel(choices).reasoning_effort,'high');
 const saved={profile:'ds',reasoning_effort:'low',profile_digest:'b'.repeat(64)};
 assert.deepEqual(chooseModel(choices,saved),saved);
});
test('stale, unavailable and invalid effort settings cannot pass confirmation',()=>{
 const selected=chooseModel(choices);assert.ok(choiceIsCurrent(choices,selected));
 assert.equal(choiceIsCurrent(choices,{...selected,profile_digest:'stale'}),false);
 assert.equal(choiceIsCurrent(choices,{...selected,reasoning_effort:'unlimited'}),false);
 assert.equal(choiceIsCurrent({...choices,profiles:choices.profiles.map(p=>({...p,available:false}))},selected),false);
 assert.equal(chooseModel({...choices,profiles:[]}),null);
});
test('historical run badge uses its frozen settings and leaves missing history unknown',()=>{
 assert.match(configurationText({model:'deepseek-v4.1-flash',reasoning_effort:'medium'}),/deepseek-v4.1-flash.*中/);
 assert.match(configurationText({model:'claude-4.6-sonnet',reasoning_effort:'high',roles:{editing_implementer:{model:'deepseek-v4.1-flash',reasoning_effort:'low'}}}),/Implementer=deepseek-v4.1-flash\/低/);
 assert.equal(configurationText(null),'本轮模型配置未记录');
});
