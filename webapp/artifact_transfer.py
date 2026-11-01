"""Trusted child for bounded object transfers; never an Agent command endpoint."""
import ctypes
import json
import os
import signal
import sys
import boto3
from botocore.config import Config


def main():
    request=json.load(sys.stdin)
    # Die if the owning executor is SIGKILLed, including the startup race.
    if ctypes.CDLL(None,use_errno=True).prctl(1,signal.SIGKILL,0,0,0)!=0:
        raise OSError('Unable to install parent-death signal')
    if os.getppid()!=request['parent']:raise RuntimeError('Transfer owner exited')
    signal.signal(signal.SIGALRM,signal.SIG_DFL);signal.alarm(300)
    client=boto3.client('s3',endpoint_url=request['endpoint'],**request['credentials'],
                       config=Config(signature_version='s3v4',connect_timeout=5,read_timeout=30,retries={'max_attempts':1},s3={'addressing_style':'path'},request_checksum_calculation='when_required',response_checksum_validation='when_required'))
    if request['action']=='put':
        with os.fdopen(request['fd'],'rb',closefd=False) as source:
            client.put_object(Bucket=request['bucket'],Key=request['key'],Body=source,IfNoneMatch='*')
    elif request['action']=='get':
        response=client.get_object(Bucket=request['bucket'],Key=request['key'])
        limit=request['max_bytes'] if request['max_bytes'] is not None else 8*1024**3
        total=0
        try:
            with os.fdopen(request['fd'],'wb',closefd=False) as target:
                for chunk in response['Body'].iter_chunks(1024*1024):
                    total+=len(chunk)
                    if total>limit:raise ValueError('Remote object exceeds declared size')
                    target.write(chunk)
                target.flush()
        finally:response['Body'].close()
    else:raise ValueError('Unknown transfer action')


if __name__=='__main__':main()
