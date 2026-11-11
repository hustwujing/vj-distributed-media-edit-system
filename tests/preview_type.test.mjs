import test from 'node:test';import assert from 'node:assert/strict';
import {previewType} from '../webapp/static/preview_type.js';
test('generic or missing MIME falls back to the filename for preview',()=>{
 for(const [name,type] of [['CLIP.MP4','video/mp4'],['sound.wav','audio/wav'],['picture.png','image/png'],['page.html','text/html'],['caption.srt','text/plain'],['data.json','application/json'],['doc.pdf','application/pdf'],['model.bin','application/octet-stream']]){
  assert.equal(previewType({name,type:'application/octet-stream'}),type);assert.equal(previewType({file:name}),type);
 }
 assert.equal(previewType({name:'arbitrary.bin',mime:'Video/MP4; codec=avc1'}),'video/mp4');
});
