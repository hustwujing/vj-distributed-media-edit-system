"""Trusted staging helper, charged to the task cgroup before copying input data."""
import json
from pathlib import Path
import sys
from webapp.execution_runtime import copy_tree

if __name__ == '__main__':
    request=json.loads(Path(sys.argv[1]).read_text())
    result=copy_tree(Path(request['source']),Path(request['destination']),request['uid'],request['gid'],request['limit'],request.get('manifest'),request.get('include'))
    Path(request['result']).write_text(json.dumps(result))
