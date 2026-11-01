"""Trusted fixed worker for Agent image tasks, after UID/capability drop."""
import json
import sys
from pathlib import Path
try:
    from webapp.image_tasks import execute_image
    request=json.loads(Path(sys.argv[1]).read_text())
    files=execute_image(request['task'],request['assets'],request['project'],request['output'])
    Path(request['output'],'provider-result.json').write_text(json.dumps(files))
except Exception as exc:
    from webapp.telemetry import write_child_error
    write_child_error(exc)
    raise
