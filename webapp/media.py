"""Small cached posters for already-authorized local video files."""
import hashlib
import subprocess
import threading
from pathlib import Path

_thumbnail_slots = threading.BoundedSemaphore(1)


def thumbnail(source: Path, cache: Path) -> Path:
    from .media_isolation import run,ensure_available
    info = source.stat()
    key = hashlib.sha256(f'{source.name}:{info.st_mtime_ns}:{info.st_size}'.encode()).hexdigest()
    target = cache / (key + '.jpg')
    with _thumbnail_slots:
        if target.is_file():
            return target
        cache.mkdir(parents=True, exist_ok=True)
        for start in ('0.5', '0'):
            result = run(source, [
                'ffmpeg', '-v', 'error', '-threads', '1', '-ss', start, '-i', '/input',
                '-frames:v', '1', '-filter_threads', '1', '-vf',
                'scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2:color=0x17221c',
                '-q:v', '4', '-c:v', 'mjpeg', '-threads', '1', '-f', 'image2pipe', '-'],
                timeout=30)
            ensure_available(result)
            if result.returncode == 0 and result.stdout.startswith(b'\xff\xd8'):
                temporary = target.with_suffix('.tmp')
                temporary.write_bytes(result.stdout)
                temporary.replace(target)
                return target
    raise ValueError('暂时无法生成此视频的封面。')
