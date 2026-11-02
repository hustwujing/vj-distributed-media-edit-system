import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
const source=await readFile(new URL('../webapp/static/artifact_order.js',import.meta.url),'utf8');
const {orderedArtifacts,collectResults,resultSections}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const video=(name,turn='t')=>({name,type:'video/mp4',size:1,url:`${turn}/${name}`});

test('unaccepted candidates never become the primary delivery',()=>{
 const candidate={...video('final-v9.mp4'),purpose:'quality_candidate',quality_state:'pending',is_primary:true};
 const sections=resultSections(orderedArtifacts([candidate]));
 assert.equal(sections.primary,undefined);assert.equal(sections.process.length,1);
 const accepted={...video('final-v8.mp4'),quality_state:'approved'};
 assert.equal(resultSections(orderedArtifacts([candidate,accepted])).primary.name,'final-v8.mp4');
});

test('v5 cannot be hidden behind the first four old videos',()=>{
 const files=[video('final.mp4'),...Array.from({length:4},(_,i)=>video(`final-v${i+2}.mp4`))];
 const ordered=orderedArtifacts(files,'最新成片 final-v5.mp4');
 assert.equal(ordered[0].name,'final-v5.mp4');
 assert.deepEqual(ordered.filter(a=>!a.alternate_version).map(a=>a.name),['final-v5.mp4']);
});
test('numeric version and explicit delivery both take precedence',()=>{
 const files=[video('final-v9.mp4'),video('final.mp4'),video('final-v10.mp4')];
 assert.equal(orderedArtifacts(files)[0].name,'final-v10.mp4');
 assert.equal(orderedArtifacts(files,'交付 final-v9.mp4')[0].name,'final-v9.mp4');
});
test('re-exported workspace files appear once, with current final visible and history folded',()=>{
 const turns=Array.from({length:5},(_,i)=>({id:'t'+i,mode:'execute',status:'succeeded',created:i+1,updated:i+2,
  text:`已完成 final${i?'-v'+(i+1):''}.mp4`,artifacts:Array.from({length:i+1},(_,n)=>video('final'+(n?'-v'+(n+1):'')+'.mp4','t'+i))}));
 const items=collectResults({jobs:[]},turns,{},String),sections=resultSections(items);
 assert.equal(items.length,5);assert.equal(sections.primary.name,'final-v5.mp4');assert.equal(sections.history.length,4);
});
test('new style pictures stay visible without displacing the latest final shortcut',()=>{
 const items=collectResults({jobs:[]},[
  {id:'video',mode:'execute',status:'succeeded',created:1,artifacts:[video('final-v5.mp4')]},
  {id:'style',mode:'plan',status:'succeeded',created:2,artifacts:[{name:'A.png',url:'a',type:'image/png',purpose:'style_preview'},{name:'B.png',url:'b',type:'image/png',purpose:'style_preview'}]},
 ],{},String);
 const sections=resultSections(items);assert.equal(sections.primary.name,'final-v5.mp4');assert.equal(sections.current.length,2);
});
test('independent manual jobs with the same output filename keep their history',()=>{
 const items=collectResults({jobs:[{id:'a',kind:'render',status:'succeeded',created:1,artifacts:[video('final.mp4','a')]},{id:'b',kind:'render',status:'succeeded',created:2,artifacts:[video('final.mp4','b')]}]},[],{render:'render'},String);
 assert.equal(items.length,2);assert.equal(resultSections(items).primary.url,'b/final.mp4');
});
test('completed workflow checkpoints stay visible while waiting, paused or stopped',()=>{
 for(const status of ['waiting','paused','cancelled']){
  const items=collectResults({jobs:[]},[{id:'durable',mode:'execute',status,workflow:{id:'w'},created:1,artifacts:[video('checkpoint.mp4')]}],{},String);
  assert.equal(items.length,1);assert.equal(items[0].name,'checkpoint.mp4');
 }
});
test('explicit workflow delivery takes precedence over intermediate media',()=>{
 const intermediate={...video('final-intermediate.mp4'),workflow_delivery:false,is_primary:true};
 const delivery={...video('delivered.mp4'),workflow_delivery:true,role:'deliverable',is_primary:true};
 const sections=resultSections(orderedArtifacts([intermediate,delivery]));
 assert.equal(sections.primary.name,'delivered.mp4');
 assert.equal(sections.process.length,1);
});

test('explicit subtitle delivery is preserved ahead of its JSON manifest',()=>{
 const files=[{name:'workflow/deliverables.json',type:'application/json',role:'deliverable',source_modified:3},
  {name:'workflow/speech.srt',type:'text/plain',role:'deliverable',is_primary:true,source_modified:1},
  {name:'workflow/speech.txt',type:'text/plain',role:'deliverable',source_modified:2}];
 const sections=resultSections(orderedArtifacts(files));
 assert.equal(sections.primary.name,'workflow/speech.srt');assert.equal(sections.primary.is_primary,true);
});

test('approved workflow intermediates cannot become final delivery before review',()=>{
 const intermediate={name:'random-output.mp4',url:'one',type:'video/mp4',source_step:'first',quality_state:'approved'};
 const pending={...intermediate,name:'complete.mp4',url:'two',quality_state:'pending'};
 const result=resultSections(orderedArtifacts([intermediate,pending]));
 assert.equal(result.primary,undefined);assert.equal(result.process.length,2);
 const delivered={...intermediate,workflow_delivery:true,role:'deliverable'};
 assert.equal(resultSections(orderedArtifacts([delivered,pending])).primary.name,'random-output.mp4');
});

test('workflow build documents cannot be promoted by missing delivery metadata',()=>{
 const document={name:'arbitrary-construction.json',url:'definition',type:'application/json',is_primary:true};
 const turn={id:'workflow',mode:'execute',status:'paused',workflow:{status:'paused'},artifacts:[document],created:1,text:'review required'};
 const items=collectResults({jobs:[]},[turn],{},String);
 assert.equal(resultSections(items).primary,undefined);assert.equal(resultSections(items).process.length,1);
 turn.status='succeeded';turn.artifacts=[{...document,workflow_delivery:true}];
 assert.equal(resultSections(collectResults({jobs:[]},[turn],{},String)).primary.name,document.name);
});
