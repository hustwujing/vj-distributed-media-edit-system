"""Real 15s media, sandbox scripts and HTTP protocol; provider generation is simulated."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch
from PIL import Image
from tests import test_workflows as fixtures
from webapp import workflows,workflow_review,agent_sandbox
from webapp.workflow_runtime import Adapter

@unittest.skipUnless(os.geteuid()==0 and shutil.which('bwrap'),'Linux sandbox required')
class GenerationChainTests(unittest.TestCase):
    def test_image_reference_video_logo_composition_and_final_delivery(self):
        f=fixtures.WorkflowTests();f.setUp();server=None
        try:
            root=f.store.root;png=io.BytesIO();Image.new('RGB',(96,96),'#66aacc').save(png,'PNG');image=png.getvalue()
            clip=root/'reference.mp4'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=96x96:rate=5','-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','15','-c:a','aac','-c:v','libx264','-pix_fmt','yuv420p',str(clip)],check=True)
            video=clip.read_bytes();submissions=[]
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*args):pass
                def send(self,status,data,cookie=False):
                    self.send_response(status)
                    if cookie:self.send_header('Set-Cookie','owner=chain-owner; Path=/')
                    self.end_headers();self.wfile.write(data)
                def do_POST(self):
                    raw=self.rfile.read(int(self.headers.get('Content-Length',0)))
                    if self.path.endswith('/cancel'):return self.send(200,b'{"status":"cancelled"}')
                    is_video=b'video.generate_from_image' in raw
                    if is_video and image not in raw:return self.send(422,b'{"error":"missing reference"}')
                    submissions.append('video' if is_video else 'image')
                    self.send(202,json.dumps({'id':'video1' if is_video else 'image1'}).encode(),True)
                def do_GET(self):
                    if self.path=='/api/capabilities':
                        return self.send(200,json.dumps({'protocol':'video-use-jobs-v1','operations':{
                            'image.generate_from_text':{'sizes':[[96,96]]},'video.generate_from_image':{'sizes':[[96,96]],'durations':[15],'reference_image':True}}}).encode())
                    if self.headers.get('Cookie')!='owner=chain-owner':return self.send(403,b'{}')
                    is_video='/video1' in self.path
                    if '/artifacts/' in self.path:return self.send(200,video if is_video else image)
                    self.send(200,json.dumps({'status':'succeeded','artifacts':[{'id':'artifact','name':'video.mp4' if is_video else 'portrait.png'}]}).encode())
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
            env=agent_sandbox.prepare(f.store,f.project,'test','execute',f.turn)
            Image.new('RGBA',(16,16),(255,0,0,180)).save(env['sources']/'source.png')
            (env['sources']/'source.png').chmod(0o644)
            env['sources'].chmod(0o755)
            script=env['workspace']/'edit/compose.py'
            script.write_text("""import json,os,subprocess
from pathlib import Path
inputs=json.loads(os.environ['VIDEO_USE_STEP_INPUTS']);out=Path(os.environ['VIDEO_USE_STEP_OUTPUT'])
subprocess.run(['ffmpeg','-v','error','-i',inputs['movie'][0],'-i',inputs['logo'][0],'-filter_complex','[0:v][1:v]overlay=W-w-4:4','-t','15','-c:v','libx264','-pix_fmt','yuv420p',str(out/'final.mp4')],check=True)
""");script.chmod(0o644)
            cut=env['workspace']/'edit/cut.py';cut.write_text("""import os
from pathlib import Path
from PIL import Image
with Image.open('sources/source.png') as im:im.convert('RGBA').save(Path(os.environ['VIDEO_USE_STEP_OUTPUT'])/'foreground.png')
""");cut.chmod(0o644)
            check=env['workspace']/'edit/check.py';check.write_text("""import os,json,subprocess
from pathlib import Path
source=json.loads(os.environ['VIDEO_USE_STEP_INPUTS'])['movie'][0];out=Path(os.environ['VIDEO_USE_STEP_OUTPUT'])
report=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_format','-of','json',source]))
(out/'report.json').write_text(json.dumps({'duration':float(report['format']['duration']),'coverage':'metadata and frame at 7s'}))
subprocess.run(['ffmpeg','-v','error','-ss','7','-i',source,'-frames:v','1',str(out/'preview.png')],check=True)
""");check.chmod(0o644)
            steps=[{'id':'portrait','name':'portrait','kind':'operation','operation':'image.generate_from_text','parameters':{'prompt':'adult woman','width':96,'height':96},'inputs':{},'goal':'reference','on_failure':'pause','acceptance':[{'kind':'image','width':96}]},
                {'id':'video','name':'video','kind':'operation','operation':'video.generate_from_image','parameters':{'prompt':'running on a beach','width':96,'height':96,'duration':15},'inputs':{'source':{'step':'portrait','role':'primary_image'}},'depends_on':['portrait'],'goal':'15s video','on_failure':'pause','acceptance':[{'kind':'media','min_duration':14.99,'max_duration':15.01,'audio_track_present':True}]},
                {'id':'cutout','name':'cutout','kind':'script','argv':['python','edit/cut.py'],'outputs':['foreground.png'],'output_roles':{'foreground':'foreground.png'},'goal':'transparent logo','on_failure':'pause','retry_safe':True,'acceptance':[{'kind':'image','has_alpha':True}]},
                {'id':'compose','name':'compose','kind':'script','argv':['python','edit/compose.py'],'outputs':['final.mp4'],'output_roles':{'video':'final.mp4'},'depends_on':['video','cutout'],'inputs':{'movie':{'step':'video','role':'video'},'logo':{'step':'cutout','role':'foreground'}},'goal':'overlay logo','on_failure':'diagnose','retry_safe':True,'acceptance':[{'kind':'media','min_duration':14.99,'max_duration':15.01,'audio_track_present':True,'decode':True}]},
                {'id':'inspect','name':'inspect','kind':'script','argv':['python','edit/check.py'],'outputs':['report.json','preview.png'],'depends_on':['compose'],'inputs':{'movie':{'step':'compose','role':'video'}},'goal':'check output','on_failure':'diagnose','retry_safe':True,'acceptance':[{'kind':'json','file':'report.json','required_keys':['duration','coverage']},{'kind':'image','file':'preview.png','width':96}]}]
            sockets=root/'sockets';sockets.mkdir();adapter=Adapter(f.store,'test',sockets);runner=workflows.Runner(f.store,adapter,threading.Event())
            with patch.dict(os.environ,{'VIDEO_USE_GENERATION_URL':f'http://127.0.0.1:{server.server_port}'}):
                f.submit(steps)
                for _ in range(12):
                    f.store.execute('UPDATE agent_steps SET next_poll=0')
                    runner.tick(f.turn)
                    # This protocol fixture runs the adapter directly, without
                    # TaskRuntime. Supply the same durable file boundary that
                    # step_checkpoints.save provides in executor integration tests.
                    import hashlib
                    current=f.get()
                    for completed in current['steps']:
                        if not completed['result']:continue
                        manifest={}
                        for relative in completed['result']:
                            path=env['workspace']/relative
                            manifest[relative]={'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                                'size':path.stat().st_size,'location':str(path.relative_to(f.store.root))}
                        f.store.execute('''INSERT INTO execution_step_checkpoints VALUES (?,?,?,?,?,?,?)
                          ON CONFLICT(workflow,step,revision) DO UPDATE SET manifest=excluded.manifest''',
                          (current['id'],completed['id'],completed['revision'],f.project,'protocol-fixture',json.dumps(manifest),f.store.now()))
                    if f.get()['status'] in ('resuming','paused','reviewing'):break
                w=f.get();self.assertEqual(w['status'],'resuming',json.dumps(workflows.public(w),ensure_ascii=False))
                self.assertEqual(submissions,['image','video'])
                self.assertEqual(w['steps'][1]['bindings']['source']['revision'],0)
                f.store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(f.turn,))
                request=workflow_review.begin(f.store,f.turn)
                workflow_review.stage(f.store,f.turn,{'request_id':request['id'],'action':'pass','reason':'Simulated reviewer checked test fixture','deliveries':w['steps'][3]['result'],
                    'evidence':[{'step_id':s['id'],'files':s['result'],'observation':'Test media inspected; semantic generation not tested','coverage':'Metadata and preview at 7 seconds; provider content is simulated'} for s in w['steps']]})
                workflow_review.commit(f.store,f.turn,adapter)
                self.assertEqual(f.get()['status'],'succeeded')
        finally:
            if server:server.shutdown();server.server_close()
            f.tearDown()
