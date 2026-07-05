# readme2demo base image — the "kitchen sink" sandbox for ALL stages (M2/M7).
#
# Built ON TOP of the official VHS image so one image serves the agent run,
# the verify replay, AND the video render: vhs/ttyd/ffmpeg (+ browser deps)
# come from the base layer, toolchains and Claude Code are added here. That is
# what lets the demo video execute every step_by_step.md step for real.
#
#   docker build -t readme2demo/base:latest images/base
#
# The container is the security boundary; it is always run with the hardening
# flags in sandbox.py (--cap-drop ALL, no-new-privileges, memory/cpu/pids
# limits) and destroyed after each stage.

FROM ghcr.io/charmbracelet/vhs:latest

LABEL org.opencontainers.image.title="readme2demo-base" \
      org.opencontainers.image.description="Sandbox for readme2demo: VHS + ttyd + ffmpeg + Python/Node/Go toolchains + Claude Code CLI" \
      org.opencontainers.image.source="https://github.com/alphacrack/readme2demo" \
      org.opencontainers.image.base.name="ghcr.io/charmbracelet/vhs:latest"

# The VHS image sets ENTRYPOINT ["vhs"]; reset it so sandbox.py can run
# arbitrary commands (docker run ... image sleep infinity). The renderer
# re-invokes vhs explicitly via --entrypoint.
ENTRYPOINT []
CMD ["bash"]

ENV DEBIAN_FRONTEND=noninteractive

# Toolchains for the common ecosystems, plus the utilities agents lean on.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        build-essential \
        python3 \
        python3-venv \
        python3-pip \
        nodejs \
        npm \
        jq \
        ripgrep \
    && rm -rf /var/lib/apt/lists/*

# Go: latest stable from the official tarball, resolved at build time.
# Distro golang packages lag badly (versions modern go.mod files reject),
# which strands agents on "go >= 1.2x required" errors — and agents with
# stale training data may wrongly conclude the newer Go "doesn't exist yet"
# and give up. Rebuild the image periodically.
RUN GO_VERSION="$(curl -fsSL 'https://go.dev/VERSION?m=text' | head -1)" \
    && ARCH="$(dpkg --print-architecture)" \
    && curl -fsSL "https://go.dev/dl/${GO_VERSION}.linux-${ARCH}.tar.gz" \
       | tar -C /usr/local -xz \
    && ln -sf /usr/local/go/bin/go /usr/local/bin/go \
    && ln -sf /usr/local/go/bin/gofmt /usr/local/bin/gofmt \
    && go version

# Claude Code CLI, installed via npm rather than the claude.ai install script
# so the version is pinned and reproducible.
# UPDATE THIS PIN deliberately: the M4 transcript parser depends on this
# version's stream-json output format (see IMPLEMENTATION_PLAN.md risk #3).
RUN npm install -g @anthropic-ai/claude-code@2.0.14

# Non-root agent user. Remove any distro default user occupying uid 1000
# first. /work is the agent's writable workspace; /task receives the rendered
# prompt; /repo is bind-mounted read-only at run time by sandbox.py.
RUN existing="$(getent passwd 1000 | cut -d: -f1 || true)" \
    && if [ -n "$existing" ]; then userdel -r "$existing" 2>/dev/null || true; fi \
    && useradd --uid 1000 --create-home --shell /bin/bash demo \
    && mkdir -p /work /task \
    && chown demo:demo /work /task

USER demo
WORKDIR /work
