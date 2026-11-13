const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {targetText,initialPlanApprovalRequired,revisionDecisionKinds,nextRevisionDecision,deliveryAmendmentMatches}=require('./campaign_dynamic_targets.cjs');

test('image authorization includes every structured output class',()=>{
 const text=targetText({expect:{kind:'image',width:1600,height:600,min_images:2,extra_alpha:true,extra_extension:'.ora'}});
 assert.match(text,/1600×600/);assert.match(text,/全部要求的图片版本/);
 assert.match(text,/透明背景 PNG/);assert.match(text,/\.ora 分层文件/);
});

test('explicit target remains authoritative',()=>{
 assert.equal(targetText({delivery_target_text:'完整冻结清单',expect:{kind:'image',width:1,height:1}}),'完整冻结清单');
});

test('gated intervention cases approve the initial plan before observing work',()=>{
 for(const id of ['R01','R02','R03','R11','R15','R17'])assert.equal(initialPlanApprovalRequired({id}),true,id);
 for(const id of ['R04','R10','D01'])assert.equal(initialPlanApprovalRequired({id}),false,id);
});

test('same-run revision decisions follow structured delivery changes',()=>{
 assert.deepEqual([...revisionDecisionKinds({expect:{kind:'image'}})],['plan_confirmation']);
 assert.deepEqual([...revisionDecisionKinds({expect:{kind:'image',min_images:2}})],['plan_confirmation']);
});

test('delivery scope approval precedes confirmation of the amended plan',()=>{
 const decisions=[{id:'confirm',kind:'plan_confirmation'},{id:'scope',kind:'plan_change'}];
 assert.equal(nextRevisionDecision(decisions).id,'scope');
 assert.equal(nextRevisionDecision([{id:'confirm',kind:'plan_confirmation'}]).id,'confirm');
});

test('amended plan confirmation preserves old targets and adds only declared filenames',()=>{
 const existing=['128×64双图PNG'],request={delivery_scope_changed:true,previous_delivery_targets:existing,
  delivery_targets:[...existing,'128×64双图JPG final.jpg'],delivery_contract:{filenames:['final.png','final.jpg']}};
 assert.equal(deliveryAmendmentMatches(existing,request,['final.jpg']),true);
 assert.equal(deliveryAmendmentMatches(existing,{...request,delivery_targets:[...request.delivery_targets,'extra.mov']},['final.jpg']),false);
 assert.equal(deliveryAmendmentMatches(existing,{delivery_scope_changed:false,delivery_targets:existing},[]),true);
});

test('the executable R04 case declares its expected delivery addition',()=>{
 const manifest=JSON.parse(fs.readFileSync(path.join(__dirname,'../docs/real-case-campaign/dynamic-cases.json')));
 assert.deepEqual(manifest.cases.find(item=>item.id==='R04').intervention_expected_delivery_additions,['final.jpg']);
});
