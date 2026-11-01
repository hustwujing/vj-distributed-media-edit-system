import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
let source=await readFile(new URL('../webapp/static/task_usage.js',import.meta.url),'utf8');
source=source.replace(/^import.*\n/,'const esc=s=>String(s).replaceAll("<","&lt;").replaceAll(">","&gt;");\n');
const {tokenText,usageHtml}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
test('missing tokens are unknown and partial totals stay partial',()=>{
 assert.equal(tokenText({known:null,missing_calls:2}),'未知');
 assert.equal(tokenText({known:0,missing_calls:0}),'0');
 assert.match(tokenText({known:150,missing_calls:2}),/150.*2 次未回报/);
});
test('usage panel escapes provider metadata and labels subset tokens and gaps',()=>{
 const field={known:150,missing_calls:0};const s={scope:'run',members:[{},{}],counts:{llm:{calls:1,states:{unknown:1}}},workflow_retries:2,repairs:1,
 provider:{submissions:1,polls:3,reported_cost:0,details:[{operation:'<img src=x onerror=alert(1)>'}]},tokens:Object.fromEntries(['input_tokens','output_tokens','total_tokens','cached_tokens','reasoning_tokens'].map(k=>[k,field])),
 coverage:{historical_tasks:['old'],historical_tools_from_events:2},unfinished_calls_on_finished_attempts:1,quota_consumption:[],phase_wall_time:[]};
 const html=usageHtml(s);assert.match(html,/输入子集/);assert.match(html,/输出子集/);assert.match(html,/历史任务/);assert.match(html,/结果未知/);assert.doesNotMatch(html,/<img/);
});

test('project scope and calculated totals are explicit while raw missing fields remain visible',()=>{
 const field={known:10,missing_calls:0};const s={scope:'project',members:[{},{},{}],counts:{llm:{calls:3,states:{succeeded:3}}},workflow_retries:0,repairs:0,
 provider:{submissions:0,polls:0,reported_cost:0,details:[]},tokens:Object.fromEntries(['input_tokens','output_tokens','total_tokens','cached_tokens','reasoning_tokens'].map(k=>[k,{...field}])),
 computed_total:{known:25,derived_calls:1,missing_calls:1,inconsistent_calls:0},coverage:{historical_tasks:[]},quota_consumption:[],phase_wall_time:[]};
 s.tokens.total_tokens={known:10,missing_calls:2};const html=usageHtml(s);
 assert.match(html,/整个项目 · 3 个任务/);assert.match(html,/25.*1 次未回报/);assert.match(html,/1 次由已回报输入 \+ 输出计算/);assert.match(html,/原始回报合计 10.*2 次未回报/);
});
