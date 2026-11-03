"""Explicit live integration case; persistent private root enables restart/resume.

Usage: python -m tests.run_live_generation_case --config PRIVATE_JSON --root PRIVATE_ROOT
One invocation advances ready actions and returns when the provider poll is not due.
No silent resubmission of an existing case and no background provider poll loop.
"""
import argparse
import json
import os
from pathlib import Path
import shutil


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--root',required=True);parser.add_argument('--reuse-portrait')
    args=parser.parse_args();os.environ.update(json.loads(Path(args.config).read_text()))
    os.environ.update(VIDEO_USE_TASK_MEMORY_BYTES=str(8*1024**3),VIDEO_USE_TASK_DISK_BYTES=str(1024**3),
                      VIDEO_USE_TASK_BUDGET_SECONDS='1800',VIDEO_USE_TASK_MODEL_CALLS='40')
    from PIL import Image,ImageDraw
    from webapp import agent_store,agent_sandbox,workflows,execution_intent
    from webapp.store import Store
    from webapp.agent_host import AgentHost
    from webapp.execution_resources import IdentityPool
    from webapp.scheduling import next_step
    root=Path(args.root);root.mkdir(parents=True,exist_ok=True);root.chmod(0o700)
    store=Store(root/'data');agent_store.initialize(store);identity=root/'case.json'
    if not identity.exists():
        project=store.add_project('integration-owner','真实生成：海滩跑步与图标')['id']
        source=store.project_dir(project)/'sources/logo.png'
        logo=Image.new('RGB',(256,256),'white');draw=ImageDraw.Draw(logo)
        draw.rounded_rectangle((38,38,218,218),radius=35,fill='#145cff');draw.polygon([(70,80),(128,180),(186,80),(160,80),(128,138),(96,80)],fill='white')
        logo.save(source);store.add_asset(project,'logo','logo','logo.png',{'kind':'image','width':256,'height':256})
        if args.reuse_portrait:
            shutil.copyfile(args.reuse_portrait,store.project_dir(project)/'sources/portrait.png')
            store.add_asset(project,'portrait-source','portrait-source','portrait.png',{'kind':'image','width':1280,'height':720})
        plan=agent_store.add_turn(store,project,'plan','生成一位漂亮的成年女性图片，以其为首帧生成她在沙滩奔跑的15秒横屏视频，将测试图标抠为透明背景并贴在右上角，交付完整视频。')
        store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(plan,))
        execution_intent.declare(store,plan,{'route':'workflow','goal':'15秒海滩跑步图标合成','operations':['image.generate_from_text','video.generate_from_image','image.remove_background','script'],'estimated_seconds':1200})
        agent_store.finish(store,plan,'succeeded','确认：1次文生图和1次Seedance图生视频。成年女性穿运动服在海滩，1280×720，15秒静音；原图明确允许cover尺寸处理。测试图标去白底后宽96像素，距右/上各24像素，全程显示。先验收人物图再生成视频；最终检查身份、海滩与奔跑动作、透明图标布局以及全片技术有效性。按每秒1帧抽样覆盖0–14秒，报告抽样限制，不宣称逐帧语义无缺陷；不合格暂停，不自动额外生成。')
        turn=agent_store.add_turn(store,project,'execute','按确认方案执行',plan_id=plan)
        store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(turn,))
        env=agent_sandbox.prepare(store,project,'test','execute',turn)
        if args.reuse_portrait:
            reuse=env['workspace']/'edit/reuse.py'
            reuse.write_text("import os,shutil\nfrom pathlib import Path\nshutil.copyfile('sources/portrait.png',Path(os.environ['VIDEO_USE_STEP_OUTPUT'])/'image.png')\n")
            reuse.chmod(0o644)
        script=env['workspace']/'edit/compose.py'
        script.write_text('''import os,json,subprocess
from pathlib import Path
inputs=json.loads(os.environ['VIDEO_USE_STEP_INPUTS']);out=Path(os.environ['VIDEO_USE_STEP_OUTPUT'])
subprocess.run(['ffmpeg','-v','error','-i',inputs['movie'][0],'-i',inputs['logo'][0],'-filter_complex','[1:v]scale=96:-1[logo];[0:v][logo]overlay=W-w-24:24','-t','15','-an','-c:v','libx264','-pix_fmt','yuv420p','-movflags','+faststart',str(out/'final.mp4')],check=True)
subprocess.run(['ffmpeg','-v','error','-i',str(out/'final.mp4'),'-vf','fps=1,scale=320:180,tile=5x3','-frames:v','1',str(out/'coverage.png')],check=True)
(out/'report.json').write_text(json.dumps({'coverage':'One frame per second at 0 through 14 seconds; 15 frames; sampled semantic inspection, full video decode checked separately','logo':{'width':96,'top':24,'right':24,'duration':15},'audio':'silent'}))
''');script.chmod(0o644)
        steps=[{'id':'portrait','name':'生成人物参考图','kind':'operation','operation':'image.generate_from_text',
            'parameters':{'prompt':'Photorealistic cinematic full body image of one beautiful adult woman aged 25 wearing a modest blue athletic T-shirt and black running shorts and running shoes, standing ready to run on a sunny sandy beach by the sea. Entire body visible, natural anatomy, wide landscape shot, warm daylight, no text, no logo.','width':1280,'height':720,'quality':'low','fit':'cover'},
            'goal':'成年女性海滩跑步参考首帧','on_failure':'pause','timeout':300,
            'acceptance':[{'kind':'image','width':1280,'height':720},{'kind':'agent','criterion':'查看实际图像：单一成年女性、运动服、海滩；全身可见，无明显严重肢体异常，适合作为跑步视频首帧。'}]},
            {'id':'video','name':'生成15秒跑步视频','kind':'operation','operation':'video.generate_from_image',
             'parameters':{'prompt':'The same adult woman in the reference image starts running and continues jogging naturally along the sandy beach beside the ocean for the whole shot. Keep her identity, face, blue athletic T-shirt and black shorts consistent. Smooth continuous tracking shot, realistic alternating running strides, full body visible, natural anatomy, no scene cuts, no text or logos.','width':1280,'height':720,'duration':15,'seed':1},
             'inputs':{'source':{'step':'portrait','role':'primary_image'}},'depends_on':['portrait'],'goal':'同一女性海滩持续跑步，15秒静音视频','on_failure':'pause','timeout':1800,
             'acceptance':[{'kind':'media','width':1280,'height':720,'min_duration':14.95,'max_duration':15.05,'decode':True}]},
            {'id':'cutout','name':'图标背景透明','kind':'operation','operation':'image.remove_background','inputs':{'source':{'asset':'logo'}},
             'goal':'保留蓝色图标，移除外部白色背景','on_failure':'pause',
             'acceptance':[{'kind':'image','file':'foreground.png','width':256,'height':256,'has_alpha':True,'min_transparent_fraction':0.2},{'kind':'image','file':'mask.png','width':256}]},
            {'id':'compose','name':'合成并检查成片','kind':'script','argv':['python','edit/compose.py'],'preflight':['edit/compose.py'],
             'outputs':['final.mp4','coverage.png','report.json'],'output_roles':{'video':'final.mp4','preview':'coverage.png','report':'report.json'},
             'depends_on':['video','cutout'],'inputs':{'movie':{'step':'video','role':'video'},'logo':{'step':'cutout','role':'foreground'}},
             'goal':'交付15秒右上角透明图标成片','on_failure':'pause','retry_safe':True,
             'acceptance':[{'kind':'media','file':'final.mp4','width':1280,'height':720,'min_duration':14.95,'max_duration':15.05,'audio_track_present':False,'decode':True},
                 {'kind':'image','file':'coverage.png','width':1600,'height':540},{'kind':'json','file':'report.json','required_keys':['coverage','logo','audio']},
                 {'kind':'agent','criterion':'对照人物参考图、原始图标、透明图标和coverage.png的0–14秒逐秒抽样画面：人物身份/服装一致，海滩场景和跑步姿态连续变化，无明显严重异常；右上角图标透明且未遮挡人物。结合报告确认布局与时长。声明抽样覆盖范围与局限，最终仅交付final.mp4。'}]}]
        if args.reuse_portrait:
            steps[0]={k:v for k,v in steps[0].items() if k not in ('operation','parameters')}
            steps[0].update(kind='script',argv=['python','edit/reuse.py'],outputs=['image.png'],output_roles={'primary_image':'image.png'},
                            inputs={'source':{'asset':'portrait-source'}},retry_safe=True,name='复用已生成的人物参考图')
            store.execute("UPDATE agent_turns SET text=text||? WHERE id=?",('\n服务适配验证追加：原Seedance拒绝写实人像参考图，复用原已生成人物图，改为一次Wan图生视频；原视频允许含音轨，最终合成明确去音轨。时长按24fps一帧加容器误差容差±0.05秒验收。',plan))
        workflows.submit(store,turn,{'key':'live-case','name':'真实生成与合成','steps':steps})
        workflows.state(store,turn,'ready');identity.write_text(json.dumps({'project':project,'turn':turn}));identity.chmod(0o600)
    ids=json.loads(identity.read_text());turn=ids['turn'];project=ids['project']
    host=AgentHost(store.root);host.identities=IdentityPool(root/'node',start=610000,size=4)
    for _ in range(12):
        row=agent_store.turn(store,turn);w=row['workflow']
        if row['status'] in ('succeeded','failed','paused','cancelled'):break
        if w['status']=='ready' and any(s['status']!='succeeded' for s in w['steps']) and not next_step(workflows.get(store,turn),store.now()):break
        kind='workflow' if w['status'] in ('ready','staged','cancelling') else 'agent'
        lease=host.coordinator.claim(turn,kind)
        if not lease:break
        host.execute_claim(lease)
    row=agent_store.turn(store,turn);w=row['workflow']
    report={'status':row['status'],'workflow_status':w['status'],'steps':[{'id':s['id'],'status':s['status'],'diagnostics':s['diagnostics'],'quality':s['quality']} for s in w['steps']],
            'calls':store.query('SELECT operation,state,external_id,implementation,usage,cost,poll_count FROM workflow_provider_calls'),
            'reviews':store.query('SELECT purpose,status,runs FROM workflow_agent_reviews'),'text':row['text'],'error':row['error']}
    target=Path('docs/test-evidence/live-generation-case');target.mkdir(exist_ok=True)
    for artifact in row['artifacts']:
        source=store.project_dir(project)/'edit/agent_exports'/turn/artifact['file']
        if source.is_file():shutil.copyfile(source,target/source.name)
    (target/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'status':report['status'],'workflow_status':report['workflow_status'],'steps':[(s['id'],s['status']) for s in w['steps']]},ensure_ascii=False))


if __name__=='__main__':main()
