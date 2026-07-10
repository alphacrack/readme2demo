# readme2demo OpenHands image — extends the base sandbox with the OpenHands
# agent runtime. Required for `--engine openhands` and the `--gemini` preset
# (it is their default sandbox image; preflight probes for it and fails fast
# with this build command when missing):
#
#   docker build -t readme2demo/base:latest images/base
#   docker build -t readme2demo/openhands:latest images/openhands
#
# OpenHands is PINNED to 0.48.0 — the newest release that installs on BOTH
# amd64 and arm64 (py3-none-any wheel). Do not casually bump it:
#   * 0.49.0–0.62.0 ship cp312 manylinux_x86_64-only wheels (no arm64 — the
#     build fails on Apple Silicon), and 0.62.0 additionally pins a
#     pre-release (openhands-agent-server==1.0.0a6) that uv refuses.
#   * The V1 series (>= 1.0) replaced the headless interface entirely (new
#     `openhands` CLI, new event schema).
# The trajectory parser and headless invocation in engines/openhands.py are
# coupled to this version's interface (`python -m openhands.core.main`,
# SAVE_TRAJECTORY_PATH env — 0.48 has no --save-trajectory-path flag) — bump
# the pin and the engine adapter TOGETHER, exactly like the Claude Code pin in
# images/base/Dockerfile.

FROM readme2demo/base:latest

LABEL org.opencontainers.image.title="readme2demo-openhands" \
      org.opencontainers.image.description="readme2demo sandbox + pinned OpenHands runtime for --engine openhands / --gemini" \
      org.opencontainers.image.source="https://github.com/alphacrack/readme2demo" \
      org.opencontainers.image.base.name="readme2demo/base:latest"

USER root

# tmux: OpenHands' local runtime (RUNTIME=local) drives bash through tmux.
RUN apt-get update && apt-get install -y --no-install-recommends tmux \
    && rm -rf /var/lib/apt/lists/*

# openhands-ai 0.48 needs Python >=3.12,<3.14 — the distro python may be older
# or newer, so uv installs a self-contained CPython 3.13 plus the pinned
# OpenHands into /opt/openhands, independent of the distro.
ENV UV_INSTALL_DIR=/usr/local/bin \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python
# mkdir up front: uv only populates /opt/uv-python when the distro lacks a
# usable 3.13 (it prefers /usr/bin/python3.13 when present), and chmod on a
# nonexistent dir would fail the build at the last step.
# Alongside the pin: `deprecated` and `memory-profiler` are imported by 0.48
# but missing from its dependency metadata (they used to arrive transitively;
# 2026-era resolutions of its unpinned deps dropped them), and
# `jupyter-kernel-gateway` is what the jupyter plugin's
# `poetry run jupyter kernelgateway` actually launches.
RUN mkdir -p /opt/uv-python \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && uv venv --python 3.13 /opt/openhands \
    && uv pip install --python /opt/openhands/bin/python \
        openhands-ai==0.48.0 \
        deprecated \
        memory-profiler \
        jupyter-kernel-gateway \
    && chmod -R a+rX /opt/uv-python /opt/openhands

# The engine invokes `openhands-python -m openhands.core.main`. A wrapper
# SCRIPT in /usr/local/bin that execs the venv interpreter by its real path.
# Three rejected alternatives, each a bug we actually hit or verified:
#   * ENV PATH prepend — sandbox commands run via `bash -lc`, and the login
#     shell's /etc/profile RESETS PATH on Debian, silently erasing it;
#   * a symlink — CPython finds pyvenv.cfg relative to the path it was
#     invoked as, so a symlink from outside the venv resolves to the base
#     interpreter with SYSTEM site-packages (ModuleNotFoundError: openhands);
#   * shadowing python3 — verify/render run repo commands in this same image,
#     and the tutorial commands being verified would leak into the OpenHands
#     venv instead of the distro python.
RUN printf '#!/bin/sh\nexec /opt/openhands/bin/python3 "$@"\n' > /usr/local/bin/openhands-python \
    && chmod +x /usr/local/bin/openhands-python

# LocalRuntime spawns its action-execution server via a hardcoded
# `poetry run` prefix (it assumes OpenHands' own dev repo). This shim makes
# `poetry run CMD...` exec CMD directly — LocalRuntime prepends the venv's
# bin to that subprocess's PATH, so `python` and `jupyter` resolve inside the
# venv. It shadows the venv's real poetry deliberately; nothing else in this
# image uses poetry, and repo commands under verify never see this venv.
RUN printf '#!/bin/sh\nif [ "$1" = "run" ]; then shift; exec "$@"; fi\necho "poetry-shim: only \"poetry run\" is supported" >&2\nexit 1\n' \
        > /opt/openhands/bin/poetry \
    && chmod +x /opt/openhands/bin/poetry

# Surgical patch (stable against the ==0.48.0 pin; the grep fails the build
# if the string drifts): browser startup blocks the action server's lifespan
# for up to 200s when chromium can't launch, while LocalRuntime's client
# gives up at 120s — a guaranteed startup deadlock. 15s makes the (absent —
# no playwright browsers in this image) browser fail FAST and non-fatally;
# the engine runs the agent with AGENT_ENABLE_BROWSING=false anyway.
RUN sed -i 's/self.check_alive(timeout=200)/self.check_alive(timeout=15)/' \
        /opt/openhands/lib/python3.13/site-packages/openhands/runtime/browser/browser_env.py \
    && grep -q 'self.check_alive(timeout=15)' \
        /opt/openhands/lib/python3.13/site-packages/openhands/runtime/browser/browser_env.py

USER demo
WORKDIR /work
