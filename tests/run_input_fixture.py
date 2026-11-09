"""Fixture for an already classified business change, before barrier tests.

Message intent decisions are exercised separately in test_workflow_messages.
These protocol tests intentionally supply the known replan disposition so they
continue to isolate claim/publication/authorization races without model calls.
"""
from webapp import run_inputs,workflow_graph as graph


def accept_change(store,identifier,actor,raw,**kwargs):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=run_inputs.accept(store,identifier,actor,raw,**kwargs)
        if row['status']=='ROUTING':
            record=store.query('SELECT * FROM run_inputs WHERE id=?',(row['id'],))[0]
            run_inputs.activate(store,graph.get(store,row['run_id']),record)
        return run_inputs.public(store.query('SELECT * FROM run_inputs WHERE id=?',(row['id'],))[0])
