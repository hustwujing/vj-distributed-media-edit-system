---
name: video-use-install
description: Install video-use into the current agent (Claude Code, Codex, Hermes, Openclaw, etc.) and wire up ffmpeg + the ElevenLabs API key so the user can start editing immediately.
---

# vj-distributed-media-edit-system — Agent skill installation

Use this file for installing or reconnecting the **local Agent skill**. For daily editing, read [SKILL.md](SKILL.md); helper scripts live in `helpers/`. For the distributed Web service, use the [configuration guide](docs/distributed-execution-configuration.md) and the project [deployment entry](README.md#本机隔离部署). Registering the skill does not deploy the Web service.

## What you're doing

You're setting up a conversation-driven video editor for the user. After install, the user drops raw footage into any folder, runs their agent (`claude`, `codex`, etc.) there, and says "edit these into a launch video." You do the rest by reading `SKILL.md`.

Local editing requires:

1. The `video-use` repo cloned somewhere stable.
2. `ffmpeg` on `$PATH` (plus optional `yt-dlp` for online sources).
3. Python and the repository dependencies. An ElevenLabs API key is additionally required when creating new Scribe transcriptions; local edits and existing transcript inspection do not require it.

And one thing must be true about the current agent:

4. It can discover `SKILL.md` — either via a global skills directory (`~/.claude/skills/`, `~/.codex/skills/`) or via a `CLAUDE.md` / system-prompt import.

## Install prompt contract

- Do everything yourself. Only ask the user for things you cannot generate — the ElevenLabs API key, and confirmation before `brew install`.
- Prefer a stable clone path like `~/Developer/vj-distributed-media-edit-system` (not `/tmp`, not `~/Downloads`).
- The skill references helpers by bare name (`transcribe.py`, `render.py`). That works because SKILL.md and `helpers/` ship together — keep them as siblings when you register the skill.
- Verify the interpreter, helper entrypoints and FFmpeg. Report these as installation checks; claim media processing works only after processing and inspecting an actual output file.

## Steps

### 1. Clone to a stable path

```bash
test -d ~/Developer/vj-distributed-media-edit-system || git clone https://github.com/hustwujing/vj-distributed-media-edit-system ~/Developer/vj-distributed-media-edit-system
cd ~/Developer/vj-distributed-media-edit-system
```

If the repo is already there, inspect `git status --short` first and preserve local changes. Update with `git pull --ff-only` when the checkout is ready to update.

### 2. Install Python deps

```bash
# Preferred: use the locked dependencies.
uv sync --locked

# If uv is unavailable, use a dedicated virtual environment instead:
# python3 -m venv .venv
# .venv/bin/python -m pip install -e .
```

`pyproject.toml` lists `requests`, `librosa`, `matplotlib`, `pillow`, `numpy`. No console scripts — invoke helpers with `.venv/bin/python helpers/<name>.py`. If this checkout also serves Web development, preserve its extras with `uv sync --locked --extra web --extra web-test`; a base-only sync can remove optional Web dependencies.

### 3. Install ffmpeg (+ optional yt-dlp)

`ffmpeg` and `ffprobe` are hard requirements. `yt-dlp` is only needed if the user wants to pull sources from URLs. Animation engines such as HyperFrames, Remotion, and Manim are installed lazily the first time a project actually needs them.

```bash
# macOS
command -v ffmpeg >/dev/null || brew install ffmpeg
command -v yt-dlp >/dev/null || brew install yt-dlp     # optional

# Debian / Ubuntu
# sudo apt-get update && sudo apt-get install -y ffmpeg
# pip install yt-dlp

# Arch
# sudo pacman -S ffmpeg yt-dlp
```

If `brew` / `apt` / `pacman` requires a sudo prompt, tell the user the exact command and wait. Do not invent a password.

### 4. Register the skill with the current agent

Figure out which agent you are running under, and register once. A symlink of the whole repo directory is the right shape — helpers/ needs to sit next to SKILL.md.

- **Claude Code** (`~/.claude/` present):

    ```bash
    mkdir -p ~/.claude/skills
    ln -sfn ~/Developer/vj-distributed-media-edit-system ~/.claude/skills/video-use
    ```

- **Codex** (`$CODEX_HOME` set, or `~/.codex/` present):

    ```bash
    mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
    ln -sfn ~/Developer/vj-distributed-media-edit-system "${CODEX_HOME:-$HOME/.codex}/skills/video-use"
    ```

- **Hermes / Openclaw / another agent with a skills directory**: symlink `~/Developer/vj-distributed-media-edit-system` into that agent's skills directory under the name `video-use`. If the agent has no skills directory, add a line to its system prompt / config pointing at `~/Developer/vj-distributed-media-edit-system/SKILL.md` (e.g. an `@~/Developer/vj-distributed-media-edit-system/SKILL.md` import in a `CLAUDE.md`-equivalent).

If you can't tell which agent you're in, ask the user once: "which agent am I running under — Claude Code, Codex, or something else?" Then pick the right target.

### 5. Configure transcription only when needed

New Scribe transcription requires `ELEVENLABS_API_KEY`. Skip this step when the user only needs local media processing or existing transcripts.

The local helper currently checks the repository `.env` first, then the working-directory `.env`, then the environment. A matching empty entry in either file also takes precedence over an exported key. Check the actual files without printing values; if choosing an exported key, remove or update conflicting file entries first.

When a key is needed and unavailable, ask the user to configure one from their ElevenLabs account. Update only the `ELEVENLABS_API_KEY` entry in the repository `.env`, preserve other settings, and set permissions to `600`. Do not overwrite the entire file: it may also contain Web control or stream configuration. Never echo a key in tool output or commit `.env`.

A key's presence is not a credential validity check. Validate transcription when the user requests their first transcription task; it consumes the configured account's quota.

### 6. Verify installation

Use the installed interpreter, not an unrelated system Python:

```bash
cd ~/Developer/vj-distributed-media-edit-system
.venv/bin/python helpers/timeline_view.py --help
.venv/bin/python helpers/render.py --help
ffmpeg -version
ffprobe -version
```

These commands verify helper loading and executable availability. They do not render media, validate API credentials or prove the Web sandbox works. If the user provided a local clip, an additional local render and `ffprobe` inspection can verify actual processing without a transcription call. Do not run a paid transcription merely to finish installation.

### 7. Hand off

Tell the user, in one short message:

- Where the skill is installed (`~/Developer/vj-distributed-media-edit-system`).
- That they should `cd` into their footage folder and start their agent there (e.g. `claude`).
- That a good first message is: *"edit these into a launch video"* or *"inventory these takes and propose a strategy."*
- That all outputs land in `<videos_dir>/edit/` — the repo stays clean.

## Keeping the skill current

- `cd ~/Developer/vj-distributed-media-edit-system && git pull --ff-only` pulls the latest code. The symlink auto-picks it up on the next run.
- If `pyproject.toml` changed deps, re-run `uv sync --locked` (preserving any Web/animation extras used by this checkout) or `.venv/bin/python -m pip install -e .` after pulling.

## Cold-start reminders

- Symlink the **whole directory**, not just `SKILL.md`. The helpers need to sit next to it.
- If `.env` exists but the key is empty, treat it the same as missing — don't assume existence means validity.
- `ffmpeg` from static builds works fine. Any modern (≥ 4.x) build is enough.
- `yt-dlp` is optional. Don't block install on it; install lazily the first time a user asks to pull from a URL.
- Node.js/npm are only needed for HyperFrames or Remotion slots. HyperFrames currently requires Node.js 22+.
- HyperFrames, Remotion, and Manim are optional animation engines. Don't install or prefer one globally during setup; pick the engine per animation slot in `SKILL.md`. HyperFrames can run through `npx --yes hyperframes ...` in the slot directory. Remotion can be scaffolded with `npx create-video@latest` or installed inside the slot before rendering.
- Never run transcription as part of install verification unless the user explicitly asks — Scribe costs real money.
- If the user is on Linux without a package manager Claude recognizes, print the manual `ffmpeg` install URL and wait rather than guessing.
