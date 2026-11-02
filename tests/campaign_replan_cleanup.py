"""Clean one R17 browser attempt using the same scoped cleanup as R01–R16."""
import argparse,json,sys
from pathlib import Path
from types import SimpleNamespace
import requests
sys.path.insert(0,str(Path(__file__).resolve().parent))
from campaign_interventions import Case,ROOT,configure,save

def cleanup(directory,base):
    directory=Path(directory).resolve()
    if directory.parent!= (ROOT/'runs/R17').resolve():raise ValueError('Not an R17 attempt')
    state=json.loads((directory/'state.json').read_text())
    if state['case']!='R17':raise ValueError('Not an R17 fixture')
    cluster,release=configure()
    from webapp.store import Store
    item=Case.__new__(Case);item.spec={'id':'R17'};item.args=SimpleNamespace(base=base)
    item.cluster=cluster;item.release=release;item.run_id=state['run_id'];item.directory=directory
    item.state=state;item.project=state.get('project');item.store=Store(cluster.data('node','a'),initialize_schema=False)
    item.session=requests.Session();item.session.trust_env=False
    auth=directory/'private-session.json'
    if auth.exists():item.session.cookies.update(json.loads(auth.read_text()))
    try:result=item.cleanup()
    finally:item.session.close()
    save(directory/'cleanup.json',result);return result

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('directory');p.add_argument('--base',required=True)
    a=p.parse_args();print(json.dumps(cleanup(a.directory,a.base)))
