---
name: Broken run / bug report
about: A run failed, produced wrong output, or the tool misbehaved
title: "[bug] "
labels: bug
---

<!--
The manifest deliberately records everything needed to reproduce a run —
cost included. The more of it you paste, the faster this gets fixed.
-->

## What happened

<!-- One or two sentences. What did you expect, what did you get? -->

## Target repo

<!-- The repo URL you ran readme2demo against, and the flags you used. -->

- Repo URL:
- Command: `readme2demo run ... `

## Which stage failed

<!-- From manifest.json: ingest / agent / normalize / distill / verify / render / tutorial -->

## Artifacts (please attach or paste)

<!-- These live in runs/<run-id>/. They're the fastest path to a fix. -->

- [ ] `manifest.json` (stage statuses + cost)
- [ ] tail of `verify.log` **or** `transcript.ndjson`
- [ ] `commands.sh` if the issue is a wrong/unverified command

```
<paste the relevant tail here>
```

## Environment

- readme2demo version / commit:
- OS + arch:
- Docker version:
- Backend: `--llm-backend` value (auto / api / claude-cli / gemini / openai)
- Engine: `--engine` value (claude-code / openhands)

## Anything else

<!-- Screenshots of the demo.gif, a link to the run, etc. -->
