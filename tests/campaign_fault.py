"""Operator-only, durable at-most-once worker fault injection for real QA."""
import fcntl
import json
import os
from pathlib import Path
import re
import time
import uuid

UNITS={'video-use-executor@a.service','video-use-executor@b.service'}


def read(folder,task):
    if not re.fullmatch('[a-f0-9]{32}',task):raise ValueError('Invalid fault task')
    path=Path(folder)/(task+'.json')
    return json.loads(path.read_text()) if path.exists() else {'attempted':False,'stopped':False}


def write(path,value):
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex)
    with temporary.open('x') as stream:
        os.fchmod(stream.fileno(),0o600);json.dump(value,stream);stream.flush();os.fsync(stream.fileno())
    temporary.replace(path)
    fd=os.open(path.parent,os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def properties(run,unit):
    result=run(['systemctl','show',unit,'--property=MainPID,ActiveState,SubState,ExecMainCode,ExecMainStatus'],timeout=5)
    return dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)


def stop_once(folder,task,candidate,run):
    if not re.fullmatch('[a-f0-9]{32}',task) or candidate.get('unit') not in UNITS:raise ValueError('Invalid fault target')
    folder=Path(folder);folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    if folder.is_symlink():raise ValueError('Invalid fault evidence directory')
    path=folder/(task+'.json');fd=os.open(folder/(task+'.lock'),os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if path.exists():return read(folder,task)  # Even uncertain effects must never be replayed.
        unit=candidate['unit'];before=properties(run,unit)
        if before.get('ActiveState')!='active' or before.get('MainPID') in (None,'0'):raise ValueError('Worker is not active')
        record={**candidate,'attempted':True,'stopped':False,'fault_state':'prepared','prepared_at':time.time(),'before':before}
        write(path,record)
        try:
            try:
                killed=run(['systemctl','kill','--signal=SIGKILL','--kill-whom=all',unit],check=False,timeout=5)
                record['kill_returncode']=killed.returncode;record['kill_stderr']=killed.stderr[:2000]
                record['after_kill']=properties(run,unit)
            finally:
                # A nonzero kill exit can occur after a signal was delivered.
                # Always stop automatic restart, and inspect structured state.
                stopped=run(['systemctl','stop',unit],check=False,timeout=10)
                record['stop_returncode']=stopped.returncode
            record['after_stop']=properties(run,unit)
            signal_confirmed=any(p.get('ExecMainCode')=='2' and p.get('ExecMainStatus')=='9'
                for p in (record.get('after_kill',{}),record['after_stop']))
            stopped_confirmed=record['stop_returncode']==0 and record['after_stop'].get('ActiveState') in ('inactive','failed') and record['after_stop'].get('MainPID')=='0'
            record['stopped']=signal_confirmed and stopped_confirmed
            record['fault_state']='confirmed' if record['stopped'] else 'uncertain'
        except Exception as exc:
            record.update(fault_state='uncertain',error_type=type(exc).__name__)
        record['finished_at']=time.time();write(path,record)
        return record
