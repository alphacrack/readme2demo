---
name: vhs-video
description: VHS tape syntax and ffmpeg video handling in readme2demo — how the demo video is generated, the hard-won gotchas, and the duration gate. Use when touching templates/demo.tape.j2, render.py, distill.tape_from_guide, or when a video is short, blank, mis-paced, or fails to render.
---

# VHS tapes & video

The demo video is built from the FINAL `step_by_step.md` (render stage, after
tutorial finalizes the guide) so the video follows the published guide
step-for-step. `distill.tape_from_guide` parses the guide's fenced bash blocks
into `TapeCommand`s; `templates/demo.tape.j2` renders the tape; `render.py`
runs VHS in the base image.

## Non-negotiable tape rules (each is a scar)

- **Never use `Wait+Screen` on output text.** It matches only the VISIBLE
  buffer; any pattern in longer-than-one-screen output scrolls off and times
  the render out. Pace with `Wait` (blocks until the shell prompt returns, so
  the command — build included — has finished) then a short `Sleep` to read.
- **VHS string literals have NO backslash escapes.** `vhs_quote` picks a
  delimiter the text doesn't contain: `"` → `` ` `` → `'`. A string with all
  three can't be typed (rare; that step is skipped).
- **`Type` is single-line.** Multi-line commands (heredocs) are typed
  LINE-BY-LINE via `TapeCommand.lines` (Type/Enter per line) so file creation
  plays on camera — a real shell reads a heredoc until its terminator.
- **Framerate 24, `Output demo.mp4` only.** A GIF per-frame render of a
  multi-minute tape once filled the Docker VM disk. `demo.gif` is a short
  downscaled ffmpeg preview cut from the mp4 afterwards (best-effort, never
  gates the stage).
- Hidden preamble `cd /work && clear` so the guide's own `git clone ... .`
  starts from the empty workspace.

## Duration gate (`render.validate_outputs`)

`expected_min_duration_s` sums the tape's Sleeps + typing time; real command
execution (the Waits) only adds on top. A video shorter than ~0.8× that bound
means steps didn't play (stale image, aborted tape) → HARD error, never a
silently short clip. Bounds: 5s..1500s.

## Coverage

`render` writes `tape_coverage.json` (guide_steps / tape_steps / dropped) and
prints any dropped step. With heredocs now on camera, a dropped step means a
genuinely ungrounded guide command — investigate, don't ignore.

## ffmpeg

Used for the gif preview (`fps=10,scale=800`) and, on the host, `ffprobe` for
the duration check. Both run inside the base image for the preview; the host
ffprobe check is skipped when ffprobe isn't on PATH.
