import array
import asyncio
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from webapp.api_execution import BoundedExecutor,APICapacityError
from webapp import media_service,media_isolation


class APIMediaCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_socket_is_closed_on_cancel_and_deadline_without_fd_leaks(self):
        for timeout in (False,True):
            with self.subTest(timeout=timeout),tempfile.TemporaryDirectory(prefix='media-cancel-') as folder:
                source=Path(folder)/'input';source.write_bytes(b'fixture')
                path=str(Path(folder)/'broker.sock');server=socket.socket(socket.AF_UNIX);server.bind(path);server.listen()
                connected=threading.Event();disconnected=threading.Event();failures=[]
                def serve():
                    try:
                        client,_=server.accept()
                        with client:
                            client.settimeout(3)
                            _,ancillary,_,_=client.recvmsg(8192,socket.CMSG_SPACE(4))
                            for level,kind,data in ancillary:
                                if level==socket.SOL_SOCKET and kind==socket.SCM_RIGHTS:
                                    fds=array.array('i');fds.frombytes(data[:len(data)//fds.itemsize*fds.itemsize])
                                    for fd in fds:os.close(fd)
                            connected.set()
                            if client.recv(1)==b'':disconnected.set()
                    except Exception as exc:failures.append(exc)
                thread=threading.Thread(target=serve,daemon=True);thread.start()
                pool=BoundedExecutor('media',1,0,execution_timeout=.15 if timeout else 3)
                baseline=len(list(Path('/proc/self/fd').iterdir()))
                try:
                    with patch.dict(os.environ,{'VIDEO_USE_MEDIA_SOCKET':path}):
                        task=asyncio.create_task(pool.run(media_service.request,source,media_service.command('image'),30))
                        async with asyncio.timeout(2):
                            while not connected.is_set():await asyncio.sleep(.005)
                        if timeout:
                            with self.assertRaises(APICapacityError) as error:await task
                            self.assertEqual(error.exception.reason,'execution_timeout')
                        else:
                            task.cancel()
                            with self.assertRaises(asyncio.CancelledError):await task
                        async with asyncio.timeout(1):
                            while pool.snapshot()['active'] or not disconnected.is_set():await asyncio.sleep(.005)
                        self.assertEqual(failures,[])
                        self.assertEqual(pool.snapshot()['completed'],1)
                        self.assertLessEqual(len(list(Path('/proc/self/fd').iterdir())),baseline)
                finally:pool.close();server.close();thread.join(timeout=3)

    async def test_oversized_broker_frame_is_rejected_before_body_read(self):
        with tempfile.TemporaryDirectory(prefix='media-frame-') as folder:
            source=Path(folder)/'input';source.write_bytes(b'fixture')
            path=str(Path(folder)/'broker.sock');server=socket.socket(socket.AF_UNIX);server.bind(path);server.listen()
            def serve():
                client,_=server.accept()
                with client:
                    client.recv(8192)  # Unreceived SCM_RIGHTS descriptors are closed by the kernel.
                    client.sendall(struct.pack('!I',49*1024**2))
            thread=threading.Thread(target=serve,daemon=True);thread.start()
            try:
                with patch.dict(os.environ,{'VIDEO_USE_MEDIA_SOCKET':path}):
                    with self.assertRaises(media_isolation.MediaDecoderUnavailable):
                        media_service.request(source,media_service.command('image'),1)
            finally:server.close();thread.join(timeout=3)

    async def test_decoder_never_returns_successful_truncated_output(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'input';source.write_bytes(b'fixture')
            def produce(args,stdout,stderr,**kwargs):
                stdout.write(b'\x89PNG\r\n\x1a\n');stdout.seek(32*1024**2);stdout.write(b'x')
                return subprocess.CompletedProcess(args,0)
            with patch('webapp.media_isolation.subprocess.run',side_effect=produce):
                result=media_isolation.run(source,media_service.command('normalize'))
            self.assertNotEqual(result.returncode,0);self.assertEqual(result.stdout,b'')
            with patch('webapp.media_isolation.run',return_value=subprocess.CompletedProcess([],0,b'\x89PNG\r\n\x1a\n')):
                with self.assertRaises(ValueError):media_isolation.normalize_image(source)
