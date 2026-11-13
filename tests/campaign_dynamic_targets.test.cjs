const test=require('node:test'),assert=require('node:assert/strict');
const {targetText}=require('./campaign_dynamic_targets.cjs');

test('image authorization includes every structured output class',()=>{
 const text=targetText({expect:{kind:'image',width:1600,height:600,min_images:2,extra_alpha:true,extra_extension:'.ora'}});
 assert.match(text,/1600×600/);assert.match(text,/全部要求的图片版本/);
 assert.match(text,/透明背景 PNG/);assert.match(text,/\.ora 分层文件/);
});

test('explicit target remains authoritative',()=>{
 assert.equal(targetText({delivery_target_text:'完整冻结清单',expect:{kind:'image',width:1,height:1}}),'完整冻结清单');
});
