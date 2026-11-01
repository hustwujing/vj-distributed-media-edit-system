"""Streaming authenticated encryption for private data in a shared/public bucket.

Only a verified plaintext temporary file is exposed to callers. Object identity
is authenticated so swapping two ciphertext objects cannot relabel their bytes.
"""
import base64
import hashlib
import os
import struct
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes

MAGIC=b'VUOBJ01\0'
OVERHEAD=len(MAGIC)+16+12+8+16


def configured_key():
    encoded=os.environ.get('VIDEO_USE_OBJECT_ENCRYPTION_KEY','')
    if not encoded:return None
    try:key=base64.b64decode(encoded,validate=True)
    except ValueError:raise ValueError('Object encryption key must be base64') from None
    if len(key)!=32:raise ValueError('Object encryption key must contain 32 bytes')
    return key


def encrypt(source,target,key,identity,check=lambda:None):
    start=source.tell();source.seek(0,2);size=source.tell()-start;source.seek(start)
    nonce=os.urandom(12);key_id=hashlib.sha256(key).digest()[:16]
    header=MAGIC+key_id+nonce+struct.pack('>Q',size)
    cipher=Cipher(algorithms.AES(key),modes.GCM(nonce)).encryptor()
    cipher.authenticate_additional_data(header+identity.encode())
    target.write(header)
    while chunk:=source.read(1024*1024):
        check();target.write(cipher.update(chunk))
    target.write(cipher.finalize());target.write(cipher.tag);target.seek(0)


def decrypt(source,target,key,identity,limit,check=lambda:None):
    header=source.read(OVERHEAD-16)
    if len(header)!=OVERHEAD-16 or not header.startswith(MAGIC):raise ValueError('Expected encrypted object envelope')
    if header[8:24]!=hashlib.sha256(key).digest()[:16]:raise ValueError('Object encryption key does not match')
    size=struct.unpack('>Q',header[36:44])[0]
    if size>limit:raise ValueError('Encrypted object exceeds declared plaintext size')
    cipher=Cipher(algorithms.AES(key),modes.GCM(header[24:36])).decryptor()
    cipher.authenticate_additional_data(header+identity.encode())
    remaining=size
    while remaining:
        check();chunk=source.read(min(1024*1024,remaining))
        if not chunk:raise ValueError('Truncated encrypted object')
        remaining-=len(chunk);target.write(cipher.update(chunk))
    tag=source.read(16)
    if len(tag)!=16 or source.read(1):raise ValueError('Invalid encrypted object length')
    try:target.write(cipher.finalize_with_tag(tag))
    except Exception:raise ValueError('Encrypted object authentication failed') from None
    target.seek(0)
