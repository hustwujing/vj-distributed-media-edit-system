"""Versioned public example assets using the existing shared artifact store."""
import hashlib
import json
from pathlib import Path
import re
import uuid

OWNER='system:demo-library'


def conversation_prompt(demo):
    """Preserve the card's visible parameters in the editable imported request."""
    prompt=demo['prompt'].strip()
    options=[item.strip() for item in demo.get('options',[]) if isinstance(item,str) and item.strip()]
    return prompt+('\n\n以下示例要求按原值执行；如需变更，请修改后提交：\n'+'\n'.join('- '+item for item in options) if options else '')


def component(value):
    if not isinstance(value,str) or not value or value.startswith('.') or Path(value).name!=value or '\\' in value:
        raise ValueError('Invalid example file name')
    return value


def references(catalog):
    if not isinstance(catalog,list) or len(catalog)>1000:raise ValueError('Invalid example catalogue')
    files=set();identifiers=set()
    for demo in catalog:
        name=demo['id']
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',name) or name in identifiers:raise ValueError('Invalid example ID')
        identifiers.add(name)
        for file in demo.get('files',[]):files.add(name+'/'+component(file))
        if demo.get('renderer')=='edit_media':files.add(name+'/plan.json')
        for source in demo.get('sources',[]):
            files.add('sources/'+component(source['file']))
            files.update('sources/'+component(t) for t in source.get('transcripts',{}).values())
    if len(files)>10000:raise ValueError('Example catalogue too large')
    return files


def local_file(root,relative):
    from .artifact_store import logical_path
    logical_path(relative);path=root/relative
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):raise FileNotFoundError(relative)
    return path


class Edition:
    def __init__(self,store,record=None):
        self.store=store;self.record=record;self.root=store.root/'demos'
        self.catalog=json.loads(record['catalog']) if record else (json.loads((self.root/'catalog.json').read_text()) if (self.root/'catalog.json').is_file() else [])
        self.files=references(self.catalog)
    def file(self,relative):
        if relative not in self.files:raise FileNotFoundError(relative)
        if self.record:return self.store.artifacts.load_file(self.record['project'],'demo-library/'+self.record['version']+'/'+relative)
        return local_file(self.root,relative)


class DemoLibrary:
    def __init__(self,store):
        self.store=store
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS demo_library(
              id TEXT PRIMARY KEY,project TEXT NOT NULL,version TEXT NOT NULL,catalog TEXT NOT NULL,
              manifest TEXT NOT NULL,updated REAL NOT NULL)''')
    def snapshot(self):
        rows=self.store.query("SELECT * FROM demo_library WHERE id='current'")
        return Edition(self.store,rows[0] if rows else None)
    def publish(self,source):
        source=Path(source);catalog=json.loads((source/'catalog.json').read_text());files=references(catalog)
        if not catalog:raise ValueError('Cannot publish an empty example library')
        manifest={}
        for name in sorted(files):
            path=local_file(source,name)
            with path.open('rb') as stream:sha=hashlib.file_digest(stream,'sha256').hexdigest()
            manifest[name]={'sha256':sha,'size':path.stat().st_size}
        version=hashlib.sha256(json.dumps([catalog,manifest],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        current=self.snapshot()
        if current.record and current.record['version']==version:return {'examples':len(catalog),'files':len(files),'version':version,'changed':False}
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            project_row=db.execute('SELECT id FROM projects WHERE owner=? ORDER BY created LIMIT 1',(OWNER,)).fetchone()
            project=project_row['id'] if project_row else uuid.uuid4().hex
            if not project_row:db.execute('INSERT INTO projects(id,owner,name,created) VALUES (?,?,?,?)',(project,OWNER,'Public example library',self.store.now()))
        # Do not create root-owned project cache directories here. Each API
        # materializes its own verified edition under its service identity.
        for name,expected in manifest.items():
            logical='demo-library/'+version+'/'+name;path=local_file(source,name)
            try:existing=self.store.artifacts.resolve(project,logical)
            except FileNotFoundError:existing=None
            if existing and existing['sha256']==expected['sha256'] and existing['size']==expected['size']:continue
            shared=self.store.query("SELECT id,sha256,size FROM artifact_objects WHERE project=? AND sha256=? AND size=? AND state='published' LIMIT 1",(project,expected['sha256'],expected['size']))
            artifact=shared[0] if shared else self.store.artifacts.upload(project,path)
            if any(artifact[k]!=expected[k] for k in ('sha256','size')):raise ValueError('Example changed during publication')
            self.store.artifacts.publish(project,logical,artifact['id'],expected_revision=0)
        # Every logical path is immutable within this edition. Existing requests
        # keep their pinned edition; failed imports never replace the pointer.
        self.store.execute("""INSERT INTO demo_library VALUES ('current',?,?,?,?,?)
          ON CONFLICT(id) DO UPDATE SET project=excluded.project,version=excluded.version,catalog=excluded.catalog,
          manifest=excluded.manifest,updated=excluded.updated""",
          (project,version,json.dumps(catalog,ensure_ascii=False),json.dumps(manifest),self.store.now()))
        return {'examples':len(catalog),'files':len(files),'version':version,'changed':True}


if __name__=='__main__':
    import argparse
    from .store import Store
    from .paths import data_root
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,required=True);args=parser.parse_args()
    library=DemoLibrary(Store(data_root(),initialize_schema=False))
    if (args.source/'catalog.json').is_file():result=library.publish(args.source)
    else:
        edition=library.snapshot()
        if not edition.record or not edition.catalog:raise RuntimeError('No published shared example library or local source catalogue')
        result={'examples':len(edition.catalog),'files':len(edition.files),'version':edition.record['version'],'changed':False}
    print(json.dumps(result))
