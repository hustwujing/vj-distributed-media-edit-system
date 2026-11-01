"""Load operator-installed capability modules, never caller-provided code."""
import importlib
import os
import re
import threading

_lock=threading.RLock()
_loaded=set()
_loading=set()


def load():
    with _lock:
        for name in filter(None,(part.strip() for part in os.environ.get('VIDEO_USE_OPERATION_MODULES','').split(','))):
            if not re.fullmatch(r'[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*',name):raise ValueError('Invalid installed operation module')
            if name in _loaded or name in _loading:continue
            _loading.add(name)
            try:
                importlib.import_module(name)
                _loaded.add(name)
            finally:_loading.remove(name)
