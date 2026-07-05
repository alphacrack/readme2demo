# How it works

readme2demo is a pipeline of small, resumable stages orchestrated over a
crash-safe `manifest.json`. Each stage reads and writes the run directory, so a
failed run resumes exactly where it stopped.

```
repo URL / guide → ingest & plan → agent run (in Docker) → normalize transcript
        → distill minimal path → VERIFY replay in a fresh container
        → tutorial + step_by_step.md → render VHS demo video
```

## The stages

1. **ingest** — clone the repo (if any), collect the README and docs, and run a
   single planner pass that emits a machine-checkable plan with a feasibility
   verdict. Infeasible quickstarts (needs GPUs, credentials, a GUI) fail here
   for pennies, before any agent time is spent.
2. **agent** — the chosen agent engine runs *inside* a hardened sandbox until
   the quickstart works, blocks, or runs out of turns. When a step-by-step
   guide is present, the agent must execute every step.
3. **normalize** — pure, deterministic Python turns the messy transcript into a
   structured command log. No LLM calls.
4. **distill** — one LLM pass reduces the run to the minimal reproduction path
   and writes `commands.sh`. The grounding validator runs here.
5. **verify** — the moat. `commands.sh` is replayed in a brand-new container
   that the agent never touched. This is the only source of the "verified"
   verdict.
6. **tutorial** — finalizes `step_by_step.md` with the verified outputs.
7. **render** — builds the VHS tape *from* the finalized `step_by_step.md`, so
   the video provably follows the published guide, and records it as
   `demo.mp4` / `demo.gif`.

## The grounding invariant

> The LLM never publishes anything a fresh container did not independently
> execute.

This is the single most important rule in the system, and it is enforced in
**code, not prompts**. Every command the distiller emits must fuzzy-match a
command that actually succeeded in the agent's log; anything else is a
violation. The fresh-container replay in the verify stage is what earns the
verification badge that every tutorial carries:

```
✅ Verified on <date> · image <digest> · commit <sha>
```

If the replay does not pass, the output ships loudly labeled `⚠ UNVERIFIED` —
it is never silently published.

## Security model

READMEs are untrusted code, so the agent runs inside a container hardened with
`cap-drop ALL`, `no-new-privileges`, non-root execution, and memory/CPU/PID
limits. That container is the permission boundary. See the
[Security model](security.md) page for the threat model and known tradeoffs.
