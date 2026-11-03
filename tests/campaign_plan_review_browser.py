"""Isolated real browser/API/PG/Redis proof; semantic results are protocol fixtures."""
import argparse,json,os,subprocess,threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    from tests.isolation import isolate_environment,isolate_network
    isolate_environment(out/'environment');isolate_network()
    class Reviewer(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            body=json.dumps({'answers':{'decision':{'type':'choice','choice':'ready','confidence':.99,'probabilities':{'ready':.99,'needs_information':.005,'discussion':.005}}}}).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Reviewer);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    os.environ['VIDEO_USE_CONTROL_INTENT_URL']=f'http://127.0.0.1:{server.server_port}/v1/systemone'
    from tests.test_api_pressure import APIPressureTests
    from webapp import agent_store,plan_readiness
    from webapp.execution_intent import declare
    from webapp.semantic_review import SemanticUncertain
    case=APIPressureTests();stop=threading.Event();worker=None
    try:
        case.setUp();task=case.task();case.store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(task,))
        declare(case.store,task,{'route':'single','operations':['script'],'goal':'导出128像素PNG','estimated_seconds':20})
        with patch('webapp.semantic_review.classify',side_effect=SemanticUncertain()):plan_readiness.review(case.store,task,'将已上传图片等比缩小为128像素PNG，原图不变。')
        agent_store.finish(case.store,task,'succeeded','将已上传图片等比缩小为128像素PNG，原图不变。方案已提交检查。')
        case.store.execute('INSERT INTO execution_workers(id,node,release,model,heartbeat,draining) VALUES (?,?,?,?,?,0)',('review-fixture','review-node','test','fixture',case.store.now()+3600))
        failures=[]
        def run():
            while not stop.wait(.05):
                for row in case.store.query("SELECT id FROM agent_turns WHERE project=? AND mode='plan' AND status='queued'",(case.project,)):
                    try:
                        case.store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(row['id'],))
                        with patch('webapp.semantic_review.classify',return_value='confirm'):
                            plan_readiness.handle_confirmation(SimpleNamespace(store=case.store,notify=lambda *_:None),agent_store.turn(case.store,row['id']))
                    except Exception as e:failures.append(type(e).__name__)
        worker=threading.Thread(target=run);worker.start()
        config={'base':case.proxy,'project':case.project,'task':task,'output':str(out),'cookies':[{'name':c.name,'value':c.value,'domain':'127.0.0.1','path':'/'} for c in case.session.cookies]}
        path=out/'private-config.json';path.write_text(json.dumps(config));path.chmod(0o600)
        result=subprocess.run(['node','tests/campaign_plan_review_browser.cjs',str(path)],capture_output=True,text=True,timeout=90)
        print(result.stdout);print(result.stderr);assert result.returncode==0;assert not failures,failures
        state=case.store.query('SELECT state,consumed_by FROM plan_readiness WHERE turn_id=?',(task,))[0]
        assert state['state']=='consumed';assert len(case.store.query("SELECT id FROM agent_turns WHERE project=? AND mode='execute'",(case.project,)))==1
        (out/'database-proof.json').write_text(json.dumps({'passed':True,'readiness':state,'review_events':case.store.query("SELECT payload FROM agent_events WHERE turn_id=? AND kind='plan_review'",(task,))},ensure_ascii=False,indent=2))
    finally:
        stop.set()
        if worker:worker.join(5)
        case.doCleanups();server.shutdown();server.server_close();thread.join(5)


if __name__=='__main__':main()
