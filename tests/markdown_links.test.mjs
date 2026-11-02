import test from 'node:test';
import assert from 'node:assert/strict';
globalThis.document={querySelector:()=>({href:'https://app.invalid/video-use/'})};
const {markdown}=await import('../webapp/static/common.js');

test('owned media links tolerate surrounding destination whitespace',()=>{
 for(const name of ['sample.png','检查图.png','frame-two.jpg']){
  const artifact={name,url:'api/files/owned',type:'image/png'};
  for(const padding of [' ','\t','   ']){
   const rendered=markdown(`[Preview](${padding}/workspace/edit/${name}${padding})`,[artifact]);
   assert.match(rendered,/class="inline-media-link"/);assert.match(rendered,/data-media-url="api\/files\/owned"/);
  }
 }
});
test('whitespace normalization never authorizes an unknown file or unsafe scheme',()=>{
 const known=[{name:'owned.png',url:'api/files/owned',type:'image/png'}];
 for(const destination of ['javascript:alert(1)','data:text/html,unsafe','/private/unknown.png']){
  const rendered=markdown(`[Link]( ${destination} )`,known);
  assert.doesNotMatch(rendered,/<a |<button|<img/);
 }
 assert.match(markdown('[Docs]( https://example.org/docs )'),/href="https:\/\/example.org\/docs"/);
});
test('angle destinations resolve owned escaped names without opening unsafe targets',()=>{
 const artifact={name:'reviews/odd name & mask.png',url:'api/files/owned?part=1&revision=2',type:'image/png'};
 for(const target of ['/workspace/edit/'+artifact.name,artifact.url]){
  const rendered=markdown(`[Preview]( <${target}> )`,[artifact]);
  assert.match(rendered,/class="inline-media-link"/);
  assert.match(rendered,/data-media-url="api\/files\/owned\?part=1&amp;revision=2"/);
 }
 assert.match(markdown(`![Preview](</workspace/edit/${artifact.name}>)`,[artifact]),/class="media-card compact"/);
 for(const target of ['javascript:alert(1)','data:text/html,unsafe','/private/unknown.png']){
  assert.doesNotMatch(markdown(`[Link](<${target}>)`,[artifact]),/<a |<button|<img/);
 }
 assert.match(markdown('[Docs](<https://example.org/docs?a=1&b=2>)'),/href="https:\/\/example.org\/docs\?a=1&amp;b=2"/);
 assert.doesNotMatch(markdown('[Unknown](</private/>)',[{url:'api/files/owned',type:'image/png'}]),/<button|<img/);
});
test('code blocks keep media-like text literal',()=>{
 const rendered=markdown('```text\n[Preview]( /workspace/edit/owned.png )\n```',[{name:'owned.png',url:'api/files/owned',type:'image/png'}]);
 assert.match(rendered,/<pre><code>/);assert.doesNotMatch(rendered,/inline-media-link/);
});
