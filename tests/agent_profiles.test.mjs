import test from 'node:test';
import assert from 'node:assert/strict';
globalThis.document={querySelector:()=>({href:'https://example.invalid/video-use/'})};
const {profileStatus}=await import('../webapp/static/agent_profiles.js');
test('switch display separates effective configuration from pending handoff',()=>{
 const view=profileStatus({agent_profile:{harness:'codex',model:'test-model',reasoning_effort:'high'},harness_switch:{state:'waiting',reason:'等待旧执行结束'}});
 assert.match(view.current,/codex/);assert.equal(view.state,'等待安全切换点');assert.equal(view.canCancel,true);
});
test('unknown switch state is not displayed as successful',()=>{
 assert.equal(profileStatus({harness_switch:{state:'future_state'}}).state,'未知切换状态');
 assert.equal(profileStatus({harness_switch:{state:'activated'}}).canCancel,false);
 assert.equal(profileStatus({}).current,'');
});
