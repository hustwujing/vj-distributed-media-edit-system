# Dynamic workflow operation reference

This is a server-owned technical reference. The authorization envelope, registered
profiles, tool allowlist and frozen Task contracts remain authoritative. The user
has already confirmed that envelope; do not ask again for the same scope. Submit
proposals only; never execute business tools as the Manager.

## Task and acceptance contracts

- `script`: complete argv including executable, such as `["python","-c", "actual code"]`;
  declare output filenames and output_roles. Do not set profile, operation,
  implementation or parameters. `/workspace` is read-only: explicitly write to
  `VIDEO_USE_STEP_OUTPUT`, or chdir there in the script.
- `operation`: use the advertised ID, implementation, parameter schema and input
  roles. Output roles/filenames are owned by the operation; omit argv, outputs and
  output_roles. Bind downstream inputs with step + the advertised role.
- `agent`: choose a registered profile, declared inputs and output_roles; do not
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
- Proposal IDs are unique throughout a workflow, including rejected proposals.
  The supplied planning_request_id is a suitable unique proposal_id.
- Acceptance declares `source` (`user`, `technical`, `product_default`, `suggestion`) and
  `stage` (`current`, `final`). Suggestions are signals, never blockers. Final rules belong only
  on `delivery_stage=true` tasks. Text rules select contains/token_sequence/line_sequence/
  normalized_text/exact_text. Media rules distinguish audio_track_present from audio_silent.
- Repair triggers may use `revise_tasks` for the failed logical Task or its dependency ancestors.
  Preserve every frozen field except local script argv or retry-safe local operation parameters.
  Never revise an unrelated branch or a task with active execution/cleanup.

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
For transitions, set transition.type to cut/fade/dissolve/wipeleft/wiperight/
slideleft/slideright/fadeblack and duration to at most 3 seconds and half the
shortest clip. Transitions overlap clips: subtract overlaps from final duration.

The optional top-level audio object uses asset, mode (mix/replace), audio_track,
start/end, offset, loop, volume, duck, denoise, highpass, fade_in and fade_out.
Without audio.asset, volume/fades process original audio. Clip boundaries receive
30ms fades. Peak limiting is not loudness normalization.

Titles use text, position (top/center/bottom), size, color (#RRGGBB), background (boolean),
start/end. Masks use even x/y/width/height, start/end, mode pixelate/blur/black.
Uploaded subtitles use subtitles.asset bound to an input role, with size, color,
position and outline. Do not pass a subtitle file path.

For large sequential assemblies use `media.concat_sequence`. Its `roles` list must exactly match
declared inputs. Compatible streams use concat demuxer; incompatible streams normalize one at a
time before concat. Decoder/encoder threads, temporary disk and input count are bounded and a
compiler report is produced. Do not create an arbitrary many-input filter graph in a script.

Use `audio.process` for bounded gain, fades, high-pass, denoise and optional two-pass loudness.
It writes `audio-process-report.json` atomically and reports objective output measurements.

## Image operations and transcription

`image.local_adjust` parameters are nested, e.g.
`{"parameters":{"brightness":1.1}}`. Other supported adjustments include
contrast, saturation, temperature, gamma, black_point, white_point, sharpen,
blur, auto_white_balance and auto_levels. Its PNG output may be opaque RGBA.

SAM3 text must be short English (at most 120 characters). Text, points and box
are mutually exclusive. Translate intent before submitting. ZIM takes the actual
source image and an existing mask. Use the output_contract names/roles; do not
invent filenames. OCR returns ocr.json and ocr-preview.png, not a finished text
transcript. A separate script can extract lines into TXT.

Transcription returns transcript.json. Reuse its real text/word timestamps to
format TXT/SRT in a script; do not call transcription again just to change format.
No provider HTTP loops, service credentials or GPU model invocations belong in
arbitrary scripts. Registered external operations own these calls and receipts.
