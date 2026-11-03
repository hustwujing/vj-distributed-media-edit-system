# Frozen script runtime contract

This contract applies when proposing, implementing or diagnosing a script. It
does not grant permission to execute scripts or inspect additional inputs.

`VIDEO_USE_STEP_INPUTS` is JSON with type `dict[str, list[str]]`. Keys are the
Task's declared input role names. Each value is a nonempty **list of absolute
file paths**, including when a role has only one input. It is never a bare path,
an asset metadata object, or a list of metadata objects. Do not guess alternate
shapes or fall back to filenames from a user's message.

For a Task with one declared `source` input, use this pattern:

```python
import json
import os
from pathlib import Path

inputs = json.loads(os.environ['VIDEO_USE_STEP_INPUTS'])
paths = inputs['source']
if len(paths) != 1:
    raise ValueError('Expected exactly one source file')
source = Path(paths[0])
output = Path(os.environ['VIDEO_USE_STEP_OUTPUT'])
output.mkdir(parents=True, exist_ok=True)
```

Pass `source` to Pillow, FFmpeg or another declared local tool. For a multi-file
role, iterate the path list explicitly. Write only under `VIDEO_USE_STEP_OUTPUT`;
do not infer writable locations from the process working directory. Declare
actual output filenames and their roles, and atomically finish each output.

For model-authored scripts, choose `on_failure: "diagnose"` when
`media_failure_diagnostician` is already authorized. A syntax, type, missing-file
or command error should produce a bounded Diagnosis and Manager revision within
the original goal, inputs, authorization and acceptance. Do not choose an identical
retry for deterministic code errors. Use `pause` only when stopping for explicit
user intervention is intended, or diagnostic authority is absent; do not silently
add that authority. Diagnosis cannot approve a failed candidate or repeat an
unknown external submission.
