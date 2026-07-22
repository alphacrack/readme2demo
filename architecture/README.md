# readme2demo — Architecture

Three views: the pipeline, the trust boundaries, and the grounding rule that
makes the output trustworthy. Diagrams are Mermaid — GitHub renders them
natively.

## 1. Pipeline

Seven stages over a crash-safe manifest (`manifest.json`); every stage is
resumable (`readme2demo resume <run> [--from-stage <s>]`).

```mermaid
flowchart TD
    U["repo URL<br/>(+ optional -s step_by_step.md)"] --> I

    subgraph M1["ingest"]
        I["clone repo · collect docs<br/>planner LLM → plan.json<br/>feasibility gate"]
    end

    subgraph M3["agent"]
        A["Claude Code headless / OpenHands<br/>runs the README inside the sandbox<br/>→ transcript.ndjson"]
    end

    subgraph M4["normalize"]
        N["pure-Python transcript parser<br/>→ command_log.json<br/>pattern reality-check · cheat detection<br/>unattempted-guide-steps warning"]
    end

    subgraph M5["distill"]
        D["grounded LLM distillation<br/>→ commands.sh (+ clone preamble)<br/>→ step_by_step.md (copied or generated)<br/>→ demo.tape (parsed FROM the guide)"]
    end

    subgraph M6["verify — the moat"]
        V["FRESH container replays commands.sh<br/>exit 0 + R2D_VERIFY_OK required<br/>fail → one distiller feedback loop"]
    end

    subgraph M7["tutorial"]
        T["LLM polish (commands code-locked)<br/>→ tutorial.md · step_by_step.md<br/>→ troubleshooting.md · howto.jsonld"]
    end

    subgraph M8["render"]
        R["VHS executes demo.tape for real<br/>→ demo.mp4 (+ 30s gif preview)<br/>duration gate: video ≥ tape lower bound"]
    end

    I -->|plan.json| A -->|transcript.ndjson| N -->|command_log.json| D
    D -->|commands.sh| V
    V -->|verified ✅| T --> R
    V -.->|"unverified → render SKIPPED,<br/>docs labeled ⚠ UNVERIFIED"| T
    I -.->|"infeasible (creds/GPU/GUI)<br/>→ blocked report, $0.01 spent"| X["stop"]
```

## 2. Containers & trust boundaries

The container is the security boundary; the LLM is never trusted.

```mermaid
flowchart LR
    subgraph HOST["host machine"]
        CLI["readme2demo CLI<br/>orchestrator + manifest"]
        RUNS[("runs/&lt;run-id&gt;/<br/>all artifacts")]
        SOCK["/var/run/docker.sock"]
    end

    subgraph IMG["base image = VHS image + toolchains<br/>(vhs·ttyd·ffmpeg · git·go·python·node · claude CLI)"]
        subgraph C1["agent container (hardened*)"]
            AG["AI agent follows README in /work"]
        end
        subgraph C2["verify container (fresh, hardened*)"]
            VF["bash commands.sh — from zero"]
        end
        subgraph C3["render container"]
            VH["vhs demo.tape — every step on camera"]
        end
    end

    CLI --> C1 & C2 & C3
    C1 -->|transcript out| RUNS
    RUNS -->|commands.sh in| C2
    RUNS -->|demo.tape in| C3
    C3 -->|demo.mp4 out| RUNS
    SOCK -. "--allow-docker-socket only<br/>(opt-in, pierces isolation,<br/>--group-add socket gid)" .-> C1 & C2 & C3
```

\* hardened: `--cap-drop ALL`, `no-new-privileges`, memory/cpu/pids limits,
non-root user, wall-clock timeouts, destroyed after every stage.

Known MVP tradeoff: the engine credential enters the agent container
(dedicated low-limit key recommended); a host-side key-injecting egress proxy
is the planned fix.

## 3. The grounding rule (why the output can be trusted)

**The LLM never publishes anything a fresh container didn't independently
execute.** Enforced in code at four points — prompts are suggestions,
parsers are law.

```mermaid
sequenceDiagram
    participant L as agent transcript<br/>(what actually ran)
    participant D as distiller LLM
    participant G as grounding validator<br/>(code, not prompt)
    participant V as verify container
    participant P as published artifacts

    D->>G: proposed commands.sh + tape
    G->>L: every command must match a<br/>SUCCESSFUL log entry (fuzzy: whitespace,<br/>2>&1, env-prefix, chains, pipe variants)
    alt ungrounded command
        G-->>D: rejected — one retry, then hard fail
    end
    G->>V: commands.sh replayed from zero
    alt replay fails
        V-->>D: verify.log feedback — one loop,<br/>then labeled ⚠ UNVERIFIED (never silent)
    end
    V->>P: verified commands + REPLAY outputs
    Note over P: tutorial polish pass is code-blocked from<br/>altering any command or output (enforce_commands)
```

## Module map

| Stage | Module | LLM? | Key invariant |
|---|---|---|---|
| ingest | `ingest.py` | planner call | infeasible repos fail for pennies, before any agent time |
| agent | `agent.py`, `engines/` | the agent itself | runs INSIDE the sandbox; guide mode = execute every step |
| normalize | `normalize.py` | no — pure | deterministic parsing; cheat/pattern/coverage checks live here |
| distill | `distill.py` | one call | grounding enforced in code; tape derived from step_by_step.md |
| verify | `verify.py` | no | fresh container, zero agent state; the only source of "✅ verified" |
| tutorial | `tutorial.py` | polish call | commands/outputs restored from verified data regardless of LLM output |
| render | `render.py` | no | video duration must cover the tape; incomplete video = hard fail |

Engines are plugins (`engines/base.py`): `claude-code` (default) and
`openhands` (experimental) both normalize to the same `command_log.json` —
nothing downstream knows which agent ran.
