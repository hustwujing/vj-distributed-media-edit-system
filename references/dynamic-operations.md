# Dynamic workflow operation reference

This is a server-owned technical reference. The authorization envelope, registered
profiles, tool allowlist and frozen Task contracts remain authoritative. The user
has already confirmed that envelope; do not ask again for the same scope. Submit
commands through submit_plan only; never execute business tools as the Manager.

## Manager results and runtime input

Use the native submit_plan tool schema. The trusted host binds snapshot_digest,
graph/authorization versions and input sequence from the original request;
do not repeat these fields. Exactly APPLY_PLAN/COMPLETE carry a `plan` command.
Use `kind=APPLY_PLAN` when the batch adds executable work and `kind=COMPLETE`
to seal the graph. The host derives wait leaves from changed Tasks and dependency
bindings; there is no model-authored continuation field. CONTINUE preserves the graph; REQUEST_AUTHORIZATION
proposes a full new authorization; REQUEST_USER_INPUT supplies a concrete question;
NEW_RUN_REQUIRED names the new goal for user confirmation. COMPLETE seals the
plan but cannot publish a result directly. New tasks go in `plan.new_tasks`;
changes use `{task_id,set:{only_allowed_fields}}` in `plan.changes`. The host
retains all other immutable Task fields. Read model_command_contract.task_changes.
Map fields use unique `{key,value}` entries according to the tool schema, not
JSON-encoded strings. These wire commands compile to the domain contracts below;
they never bypass authorization or lease/snapshot checks.

Process the complete frozen input_batch. Later inputs are deliberately absent and
will be handled in another round. Do not cancel or replay an external request due
to changed instructions. A waiting/unknown provider handle must be resolved first.

## Task and acceptance contracts

- Acceptance `target`, when supplied, is an output type: file/image/media/text/json/subtitle.
  It is not a filename or output role; put the filename in `file`. Audio and video
  both use media. `reviewer=program` uses that target as `kind`; reviewer agent/user
  uses kind agent/review and a nonempty criterion. Do not supply conflicting kind.
- `script`: complete argv including executable, such as `["python","-c", "actual code"]`;
  declare `files:[{path,roles:[...]}]` once; the host derives outputs/output_roles. Do not set profile, operation,
  implementation or parameters. `/workspace` is read-only: explicitly write to
  `VIDEO_USE_STEP_OUTPUT`, or chdir there in the script.
- `operation`: use the advertised ID, implementation, parameter schema and input
  roles. Output roles/filenames are owned by the operation; omit argv, outputs and
  output_roles or files. Bind downstream inputs with step + the advertised role.
- `agent`: choose a registered profile, declared inputs and files with roles; do not
  supply argv/parameters/implementation. JSON-report profiles must use their
  result_schema. They cannot produce arbitrary additional business fields.
- Image acceptance supports dimensions/alpha but not decode/fps. `has_alpha` only checks the channel;
  `transparent_subject` requires at least one transparent background pixel and one visible subject pixel,
  without a fixed 5% border. Use `media`
  for decoding, frame rate and stream checks. `criterion` belongs only to
  agent/review acceptance. Text acceptance needs nonempty `contains`; do not
  invent expected words not present in an inspected input.
- A fully opaque RGBA image is still opaque. Do not require `has_alpha=false`
  unless the actual deliverable contract forbids an alpha channel.
- Use program checks for deterministic geometry and byte transformations, and
  Agent review for unresolved semantic questions. Do not keep re-inspecting
  unchanged artifacts or create redundant reviews of factual reports.
- Proposal IDs are bound by the host to the current immutable planning request.
- Acceptance declares `source` (`user`, `technical`, `product_default`, `suggestion`) and
  `stage` (`current`, `final`). Suggestions are signals, never blockers. Final rules belong only
  on `delivery_stage=true` tasks. Text rules select contains/token_sequence/line_sequence/
  normalized_text/exact_text. Media rules distinguish audio_track_present from audio_silent.
- Repair triggers may use `plan.changes` for the failed logical Task or its dependency ancestors.
  Automatic repairs preserve every frozen field except local script argv or retry-safe local
  operation parameters; a business Agent can get a new revision of the same contract with
  diagnostic context. Never revise a task with active execution/cleanup or unresolved external calls.
- Explicit user feedback in input_batch permits changes to objective, parameters/argv,
  authorized inputs, acceptance and semantics inside its allowed revision closure. Changing
  type, operation/Profile, implementation, output roles or delivery stage requires a new logical Task.
- `replacements` maps an old failed requirement to a newly added Task that satisfies the same
  goal and acceptance. The server rewires affected dependencies with new revisions. Do not
  duplicate automatically redirected edges. Merely appending a similar Task does not unblock
  delivery. Original failure history and cumulative repair/provider budgets remain effective.

The model-facing automatic repair command is `plan.repair_replacements`, with
`task_id` and a new `implementation` (new ID/type/name, inputs, files, argv or
registered operation/profile). Omit objective, acceptance, semantics and
delivery_stage: the host inherits these exact original requirements. For ordinary
new Tasks, the host derives delivery_stage from user acceptance with `stage=final`. User feedback
instead uses plan.replacements and plan.new_tasks inside its explicit allowed scope.
Acceptance uses one `kind` discriminator; native tool parameters expose only fields
valid for that kind. Do not repeat target/reviewer aliases. Each local file declares
at least one role so it can be bound and reviewed without a second filename list.

## media.render versus media.edit

`media.render` is a preset montage renderer. It always resizes the long edge:
`draft` uses 1280; `preview` and `final` use 1920. It does NOT preserve a 640x360
source. For exact other dimensions, rotation, speed, layouts and audio editing,
use `media.edit` with an explicit edit_plan, or a local script.

For `media.edit`, use input role names in `asset`, never host paths or local
`sources`, `source`, `file` or `transcripts` mappings. The server binds paths.
For example, with inputs.source referring to an owned video:

```json
{"edit_plan":{"clips":[{"asset":"source","start":5,"end":12}],
 "output":{"width":640,"height":360,"fps":25,"crf":20}}}
```

Each clip supports: asset, start/end (seconds), audio_track (zero based),
speed (0.25..4, preserves pitch), reverse, reverse_audio, freeze_end (0..30),
repeat (1..20), crop [x,y,width,height] (even source pixels), rotate
(0/90/180/270 clockwise), flip (none/horizontal/vertical), fit
(contain/cover/blur), mute, volume (0..4), and grade.

Output width/height are even values in 16..4096; default is the first source's
visible size. fps is an integer 1..60, default 30. crf is 12..35, default 20.
Video format is mp4. For audio, use output.audio_only=true and format wav/mp3/m4a.
Output audio is 48kHz stereo. Only audio/video assets are accepted as clips;
convert still images to video with a script when needed.

Top-level layout is sequence, horizontal (2 clips), vertical (2), or grid (4).
Split layouts use the shortest clip; split_audio selects the view's audio track.
For transitions, set transition.type to cut/fade/pixel_dissolve/wipeleft/wiperight/
slideleft/slideright/fadeblack and duration to at most 3 seconds and half the
shortest clip. `fade` is a linear crossfade. `pixel_dissolve` randomly replaces
pixels and must not be used for a crossfade request. Transitions overlap clips:
subtract overlaps from final duration.

The optional top-level audio object uses asset, mode (mix/replace), audio_track,
start/end, offset, loop, volume, duck, denoise, highpass, fade_in and fade_out.
Without audio.asset, volume/fades process original audio. Clip boundaries receive
30ms fades. Peak limiting is not loudness normalization.

Titles use text, position (top/center/bottom), size, color (#RRGGBB), background (boolean),
start/end. Masks use even x/y/width/height, start/end, mode pixelate/blur/black.
Uploaded subtitles use subtitles.asset bound to an input role, with size, color,
position and outline. Do not pass a subtitle file path.

For large sequential assemblies use `media.concat_sequence`. Its `roles` list must exactly match
declared inputs. Compatible streams use concat demuxer. For incompatible streams, plan one
`media.normalize_clip` Task per source with the SAME explicit `width`, `height`, `fps` and
`audio_mode`. Each has a single `source` input and returns `video` (`final.mp4`) plus `report`
(`compiler-report.json`). Keep these intermediate Tasks on current-stage acceptance; their independently
accepted checkpoints become the ordered inputs to the final concat Task. Do not put normalization
inside one giant script or re-run unrelated accepted clips after a failure. A concat compatibility
failure requires this dependency repair, not an identical retry. Decoder/encoder threads, temporary
disk and input count are bounded. Do not create an arbitrary many-input filter graph in a script.

Use `audio.process` for bounded gain, fades, high-pass, denoise and optional two-pass loudness.
It writes `audio-process-report.json` atomically and reports objective output measurements.
It preserves the source channel count and sample rate. Gain/fades do not implicitly remix mono to stereo
or normalize loudness. The report records the exact filters, normalization flag, source format,
full-stream source/output levels and gain-only mean/peak deltas; these measurements are evidence,
not permission to override a failed review.

## Image operations and transcription

`image.local_adjust` parameters are nested, e.g.
`{"parameters":{"brightness":1.1}}`. Other supported adjustments include
contrast, saturation, temperature, gamma, black_point, white_point, sharpen,
blur, auto_white_balance and auto_levels. Its PNG output may be opaque RGBA.

SAM3 text must be short English (at most 120 characters). Text, points and box
are mutually exclusive. Use a concise object category, adding only modifiers
needed to distinguish visible candidates; do not turn the label into a detailed
caption or a list of inclusion/exclusion requirements. A syntactically valid
label can still match poorly. After `no_selection`, prefer a verified box or
points from the inspected image; do not repeat the same label or lower global
confidence thresholds. Check the resulting mask against the subject requirement.
Translate intent before submitting. ZIM takes the actual
source image and an existing mask. Use the output_contract names/roles; do not
invent filenames. OCR returns ocr.json and ocr-preview.png, not a finished text
transcript. A separate script can extract lines into TXT.

`image.refine_mask` accepts one input role, `mask`: bind the actual grayscale or
alpha mask to refine. Do not pass the original photo as `source` or send a separate
photo alongside the mask. Local mask-repair semantics use `reference_role=mask`
and `output_role=mask`, so execution and changed-area measurement share the same
reference pixels. `image.refine_zim` remains a separate image-plus-mask operation.

Transcription returns transcript.json. Reuse its real text/word timestamps to
format TXT/SRT in a script; do not call transcription again just to change format.
No provider HTTP loops, service credentials or GPU model invocations belong in
arbitrary scripts. Registered external operations own these calls and receipts.

## Evidence-only recovery and candidate inputs

When media is already program-valid but a content review lacks evidence, preserve
its bytes and external request. Create a `purpose=evidence` Task using registered
`media.evidence`; this operation is local, bounded, and produces evidence-report.json
plus preview.jpg. Modes are auto/image/video_window/audio; video windows are at most
8 seconds with up to 16 timestamped frames. Optional reference input is an image.
Image/video crops use [x1,y1,x2,y2]. Reports include actual source/preview hashes,
full-stream audio measurements or bounded frame structure metrics. These metrics
are not identity or semantic verdicts.

A failed candidate is not an accepted dependency. Use only a frozen descriptor
from `repair_candidates`, as
`inputs.source = {"repair_candidate": {logical_task_id, revision, artifact_id, sha256, role}}`.
Copy every field exactly; never invent an artifact ID or hash. Candidate reads are
permitted only for `purpose=repair/evidence` within the frozen repair scope and
only after program integrity verification. They do not approve the original Task.

Plan the collector first with `kind=APPLY_PLAN`; the host derives it as the wait leaf.
After it succeeds, use plan.review_again (a map represented as key/value entries)
mapping the original task ID to completed evidence task IDs to request a bounded
new content review of the same candidate and original acceptance. No media revision
or new provider submission is needed. A final-review evidence gap can instead add
verified evidence Tasks before sealing again. Repeating the same evidence does not
permit another verdict. Technical acceptance failures require actual repair, not
review_again. Explicit user feedback can revise content criteria in its allowed
closure; automatic evidence recovery cannot lower them.
