"""Regression checks for round 1 API identity and resource boundaries."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from webapp.app import create_app


class RequestBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.app=create_app(Path(self.folder.name),False)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://test:8080')

    async def asyncTearDown(self):
        await self.client.aclose();self.app.state.api_execution.close();self.folder.cleanup()

    async def test_large_json_is_rejected_before_parsing_and_project_creation(self):
        async def chunks():
            yield b'{"name":"'
            for _ in range(17):yield b'a'*65536
            yield b'"}'
        for length in (None,'1',str(2*1024*1024)):
            headers={'Content-Type':'application/json'}
            if length is not None:headers['Content-Length']=length
            with self.subTest(length=length):
                response=await self.client.post('/api/projects',content=chunks(),headers=headers)
                self.assertEqual(response.status_code,413,response.text)
        self.assertEqual(self.app.state.store.query('SELECT id FROM projects'),[])
        response=await self.client.request('GET','/api/projects',content=chunks(),headers={'Content-Type':'application/json'})
        self.assertEqual(response.status_code,413)
        self.assertEqual((await self.client.get('/api/health')).status_code,200)

    async def test_large_multipart_cannot_bypass_json_limit_and_real_upload_still_works(self):
        response=await self.client.post('/api/projects',files={'file':('x',b'a'*(1024*1024+1))})
        self.assertEqual(response.status_code,413)
        project=(await self.client.post('/api/projects',json={'name':'upload'})).json()['id']
        response=await self.client.post(f'/api/projects/{project}/assets',
            files={'file':('brief.md',b'a'*(1024*1024+1),'text/markdown')})
        self.assertEqual(response.status_code,201,response.text)

    async def test_origin_keeps_non_default_port_and_rejects_other_port(self):
        response=await self.client.post('/api/projects',json={'name':'same origin'},headers={'Origin':'http://test:8080'})
        self.assertEqual(response.status_code,201)
        response=await self.client.post('/api/projects',json={'name':'different origin'},headers={'Origin':'http://test:8081'})
        self.assertEqual(response.status_code,403)

    async def test_caption_json_has_body_limit_despite_sharing_upload_admission(self):
        project=(await self.client.post('/api/projects',json={'name':'captions'})).json()['id']
        url=f'/api/projects/{project}/editorial/captions'
        async def chunks():
            yield b'{"name":"captions","cues":[{"start":0,"end":1,"text":"'
            for _ in range(17):yield b'a'*65536
            yield b'"}]}'
        for content_type in ('application/json','multipart/form-data; boundary=pretend'):
            response=await self.client.post(url,content=chunks(),headers={'Content-Type':content_type})
            self.assertEqual(response.status_code,413,response.text)
        self.assertEqual(self.app.state.store.assets(project),[])
        response=await self.client.post(url,json={'name':'captions','cues':[{'start':0,'end':1,'text':'hello'}]})
        self.assertEqual(response.status_code,201,response.text)

    async def test_checkpoint_restore_refuses_dynamic_run_without_a_lease(self):
        import json
        from webapp import workflow_graph as graph
        store=self.app.state.store
        project=(await self.client.post('/api/projects',json={'name':'checkpoint'})).json()['id']
        old='checkpoints/old';new='checkpoints/new'
        (store.root/old).mkdir(parents=True);(store.root/new).mkdir(parents=True)
        manifest=json.dumps({'workspace':{},'state':{}})
        store.execute('INSERT INTO execution_checkpoints VALUES (?,?,?,?,?)',(project,'new',new,manifest,store.now()))
        store.execute('INSERT INTO execution_checkpoint_versions VALUES (?,?,?,?,?,?,?,?,?)',
            ('a'*32,project,'old',old,manifest,'{}','old-profile',None,store.now()))
        run=graph.create(store,project,None,{'goal':'report','allowed_assets':[],
            'allowed_operations':['script'],'delivery_targets':['report'],
            'content_fingerprint':graph.content_fingerprint(store,project)})
        self.assertEqual(store.query('SELECT * FROM execution_leases'),[])
        for state in ('ACTIVE','WAITING_USER','CANCELLING'):
            store.execute('UPDATE runs SET status=? WHERE id=?',(state,run['id']))
            response=await self.client.post(f'/api/projects/{project}/checkpoints/restore',json={'version':'a'*32})
            self.assertEqual(response.status_code,409,response.text)
            self.assertEqual(store.query('SELECT path FROM execution_checkpoints')[0]['path'],new)
        store.execute("UPDATE runs SET status='CANCELLED' WHERE id=?",(run['id'],))
        response=await self.client.post(f'/api/projects/{project}/checkpoints/restore',json={'version':'a'*32})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(store.query('SELECT path FROM execution_checkpoints')[0]['path'],old)


class ProjectAdmissionTests(unittest.TestCase):
    def test_project_limit_is_atomic_between_two_postgres_api_instances(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        from fastapi.testclient import TestClient
        from tests.distributed_support import PostgresServer
        pg=PostgresServer()
        try:
            with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{
                    'VIDEO_USE_DATABASE_URL':pg.dsn,'VIDEO_USE_SESSION_SECRET':'b2'*32}),patch('webapp.app.MAX_PROJECTS',1):
                apps=[create_app(Path(directory)/node,False) for node in ('a','b')]
                with TestClient(apps[0]) as a,TestClient(apps[1]) as b:
                    a.get('/api/projects');b.cookies.update(a.cookies)
                    barrier=threading.Barrier(2)
                    def wrapped(store):
                        original=store.query
                        def query(sql,args=()):
                            unprotected=getattr(store._connections,'db',None) is None
                            rows=original(sql,args)
                            if unprotected and sql.startswith('SELECT count(*) AS n FROM projects'):
                                barrier.wait(timeout=5)
                            return rows
                        return query
                    with patch.object(apps[0].state.store,'query',side_effect=wrapped(apps[0].state.store)), \
                         patch.object(apps[1].state.store,'query',side_effect=wrapped(apps[1].state.store)), \
                         ThreadPoolExecutor(max_workers=2) as pool:
                        futures=[pool.submit(client.post,'/api/projects',json={'name':'race'}) for client in (a,b)]
                        results=[future.result(timeout=15) for future in futures]
                    self.assertEqual(sorted(result.status_code for result in results),[201,409])
                    self.assertEqual(len(a.get('/api/projects').json()),1)
        finally:pg.close()
