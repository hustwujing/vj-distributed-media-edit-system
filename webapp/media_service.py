"""Fixed FD-only media decoder broker for hosts without unprivileged user namespaces.

No path, command, project, provider credentials or network capability is accepted.
The decoder runs in the existing read-only namespace and drops to the API UID.
"""
import array
import base64
import json
import os
from pathlib import Path
import pwd
import socket
import socketserver
import stat
import struct
import subprocess
import threading

REPO=Path(__file__).resolve().parents[1]
class DecoderFailure(RuntimeError):
    def __init__(self,code):
        self.returncode=code;self.diagnostic={'code':'decoder_unavailable' if code in (126,127) else 'media_rejected'}
        super().__init__('Fixed media decoder failed')


PROBE=['ffprobe','-v','error','-show_streams','-show_format','-of','json','/input']


def command(operation,position='0'):
    if operation in ('image','normalize'):
        return [str(REPO/'.venv/bin/python'),'/inspect.py']+(['--normalize'] if operation=='normalize' else [])
    if operation=='probe':return PROBE
    if operation=='thumbnail' and position in ('0','0.5'):
        return ['ffmpeg','-v','error','-threads','1','-ss',position,'-i','/input','-frames:v','1','-filter_threads','1','-vf',
            'scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2:color=0x17221c',
            '-q:v','4','-c:v','mjpeg','-threads','1','-f','image2pipe','-']
    raise ValueError('Unknown fixed media operation')


def request(source,arguments,timeout):
    operation=None;position='0'
    for candidate in ('image','normalize','probe','thumbnail'):
        for offset in ('0','0.5') if candidate=='thumbnail' else ('0',):
            if arguments==command(candidate,offset):operation=candidate;position=offset
    if operation is None:raise ValueError('Decoder request is not a registered fixed operation')
    from .telemetry import context
    observed={k:v for k,v in context().items() if k in ('request_id','trace_id','span_id','parent_span_id')}
    payload=json.dumps({'operation':operation,'position':position,'observation':observed}).encode()
    fd=os.open(source,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        with socket.socket(socket.AF_UNIX) as client:
            from .api_execution import install_interrupt,check_cancelled
            def interrupt():
                try:client.shutdown(socket.SHUT_RDWR)
                except OSError:pass
                client.close()
            remove=install_interrupt(interrupt)
            try:
                check_cancelled();client.settimeout(timeout+10);client.connect(os.environ['VIDEO_USE_MEDIA_SOCKET'])
                client.sendmsg([struct.pack('!I',len(payload))+payload],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[fd]))])
                def exact(size):
                    output=bytearray()
                    while len(output)<size:
                        check_cancelled()
                        chunk=client.recv(min(size-len(output),65536))
                        if not chunk:raise OSError('Media decoder disconnected')
                        output.extend(chunk)
                    return bytes(output)
                size=struct.unpack('!I',exact(4))[0]
                if size>48*1024**2:raise ValueError('Media decoder response too large')
                value=json.loads(exact(size))
                return subprocess.CompletedProcess(arguments,value['returncode'],base64.b64decode(value.get('stdout',''),validate=True),base64.b64decode(value.get('stderr',''),validate=True))
            except (OSError,ValueError,KeyError,TypeError) as exc:
                check_cancelled()
                from .media_isolation import MediaDecoderUnavailable
                raise MediaDecoderUnavailable('媒体解码服务暂不可用，请凭查询编号排查。') from exc
            finally:remove()
    finally:os.close(fd)


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        descriptors=[];observed={}
        try:
            self.request.settimeout(55)
            _,uid,_=struct.unpack('3i',self.request.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            service=pwd.getpwnam('video-use')
            if uid not in (0,service.pw_uid):raise ValueError('Unauthorized media caller')
            raw,ancillary,flags,_=self.request.recvmsg(8192,socket.CMSG_SPACE(4))
            for level,kind,value in ancillary:
                if level==socket.SOL_SOCKET and kind==socket.SCM_RIGHTS:
                    numbers=array.array('i');numbers.frombytes(value[:len(value)//numbers.itemsize*numbers.itemsize]);descriptors.extend(numbers)
            if flags&socket.MSG_CTRUNC or len(descriptors)!=1 or len(raw)<4:raise ValueError('Expected one input descriptor')
            size=struct.unpack('!I',raw[:4])[0]
            if size>4096:raise ValueError('Media request too large')
            while len(raw)<size+4:
                more=self.request.recv(size+4-len(raw))
                if not more:raise ValueError('Truncated request')
                raw+=more
            value=json.loads(raw[4:])
            if set(value)-{'operation','position','observation'}:raise ValueError('Unexpected media request field')
            candidate=value.get('observation',{})
            import re
            if not isinstance(candidate,dict) or set(candidate)-{'request_id','trace_id','span_id','parent_span_id'}:raise ValueError('Invalid observation context')
            observed=candidate
            if any(not isinstance(v,str) or not re.fullmatch('[a-f0-9]{16}|[a-f0-9]{32}',v) for v in observed.values()):
                observed={};raise ValueError('Invalid observation identifier')
            fd=descriptors[0];info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=service.pw_uid or not 0<info.st_size<=2*1024**3:
                raise ValueError('Invalid input file')
            from .media_isolation import run
            from .telemetry import bind,span
            with bind(**observed),span('media_decode'):
                result=run(None,command(value['operation'],value.get('position','0')),45,input_fd=fd,decode_uid=service.pw_uid,decode_gid=service.pw_gid)
                if result.returncode:
                    from .telemetry import emit
                    dependency=re.search(rb'error while loading shared libraries: ([A-Za-z0-9_.+-]+):',result.stderr[:4096])
                    emit('media.process_failed','ERROR',returncode=result.returncode,native_dependency=dependency[1].decode() if dependency else None)
                    raise DecoderFailure(result.returncode)
            response={'returncode':result.returncode,'stdout':base64.b64encode(result.stdout).decode(),'stderr':base64.b64encode(result.stderr).decode()}
        except Exception as exc:
            from .telemetry import emit
            emit('media.decode_failed','ERROR',exc=exc,**observed)
            response={'returncode':getattr(exc,'returncode',1),'stderr':base64.b64encode(b'Fixed media decoding failed').decode()}
        finally:
            for fd in descriptors:os.close(fd)
        encoded=json.dumps(response).encode()
        try:self.request.sendall(struct.pack('!I',len(encoded))+encoded)
        except OSError:pass


class Server(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
    daemon_threads=True
    def __init__(self,path):
        self.slots=threading.BoundedSemaphore(2)
        super().__init__(str(path),Handler)
        service=pwd.getpwnam('video-use');os.chown(path,0,service.pw_gid);os.chmod(path,0o660)
    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False):self.shutdown_request(request);return
        super().process_request(request,address)
    def process_request_thread(self,request,address):
        try:super().process_request_thread(request,address)
        finally:self.slots.release()


if __name__=='__main__':
    from .telemetry import setup
    setup('media')
    if os.geteuid()!=0:raise SystemExit('Fixed decoder broker requires the service launcher identity')
    path=Path(os.environ['VIDEO_USE_MEDIA_SOCKET']);path.unlink(missing_ok=True)
    with Server(path) as server:server.serve_forever()
