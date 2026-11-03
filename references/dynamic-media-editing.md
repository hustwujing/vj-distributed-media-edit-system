# Scoped media implementation

You implement one frozen Task, not the whole workflow. The Task's declared inputs,
output roles, allowed tools and budgets are authoritative. Skill text does not
expand them. Do not call external providers, install packages or publish delivery.

Read inputs with `json.loads(os.environ['VIDEO_USE_STEP_INPUTS'])`: each role maps
to a list of absolute paths. Never guess filenames from user labels. Write only
under `VIDEO_USE_STEP_OUTPUT`. The structured profile's script tool starts there;
an ordinary script Task must explicitly use that directory or chdir into it.

Python standard library, Pillow, NumPy, FFmpeg and ffprobe are available. Prefer
small deterministic code. Do not assume arbitrary extra packages or GPU access.
Pillow examples: ImageOps.fit for a requested cover crop, Image.Resampling.LANCZOS
for high quality scaling, Image.Transpose for exact flips/quarter turns. Preserve
alpha when required. Channel presence is different from actual transparency.

Use complete subprocess argv arrays, check return codes, and preserve errors.
Keep FFmpeg CPU concurrency bounded with `-threads 2`, `-filter_threads 1` and
`-filter_complex_threads 1` where appropriate. When concatenating independent
sources, normalize geometry, fps, pixel format, time base and `setsar=1` before
concat. Reset timestamps with setpts/asetpts. Do not assume equal pixel dimensions
imply equal sample aspect ratios.

Do not open an unbounded number of independent decoders in one FFmpeg command. Use the registered
`media.concat_sequence` operation for large sequential assemblies and `audio.process` for common
gain/fade/loudness work. A single `-threads` before the first input applies only to that decoder;
it does not constrain later inputs.
For heterogeneous clips, use independent `media.normalize_clip` Tasks with a common explicit
width/height/fps/audio profile, accept their video checkpoints, then concatenate those outputs.
Normalization is a business DAG step so a failed clip can be repaired without redoing accepted clips.

For speed changes preserving pitch, use atempo with the corresponding video
setpts transformation. For measurements, compute from actual decoded samples;
never invent a listening result, RMS, pitch, silence or clipping check. If the
contract needs evidence that cannot be obtained, report that limitation.

Use actual word timestamps for speech cuts/subtitles. Preserve source audio unless
the Task requests removal or replacement. Apply captions after timeline assembly.
Use a real installed font for required glyphs. Missing output, blank required text
and merely returning exit code zero do not establish acceptance.

Validate your created files in the script. Submit `action=result, files={}` for
already created declared binary outputs. The files map is only for UTF-8 text,
not base64 binary. Further inspection and final publication are independent.
