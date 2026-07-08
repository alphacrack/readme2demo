# readme2demo — grand release plan

A sequenced, multi-channel launch. Companion to `LAUNCH_CHECKLIST.md` (the
GitHub/settings work) and the drafts in `launch/`. Skips LinkedIn by choice.

## The one thing to get right

Developer audiences punish marketing and reward candor. Lead with the *proof*,
not the pitch: **the tutorial ran, twice — once in a sandbox, once in a fresh
clean-room container — before you saw it.** The demo GIF and the
`✓ R2D_VERIFY_OK` moment are the whole story; show them first, everywhere.

Anchor the launch on **two big-bang channels — Hacker News (Show HN) and
Product Hunt** — and use everything else to amplify. Don't fire every channel
in the same minute: you can only actively hold one comment thread at a time,
and simultaneous blasting reads as spam.

## Pre-flight gate (must be true before you launch)

- [ ] Repo is **public**, docs site is **live** (`https://alphacrack.github.io/readme2demo/`), demo GIF renders on the README.
- [ ] `v0.1.0` release cut; README, one-liner, and social preview are final.
- [ ] Landing/demo is cached/CDN-backed — a front-page Show HN can send 5k–30k visitors in 24h.
- [ ] You have a free 2–4 hour block to sit on the threads the moment they go live.

## Timeline (relative — pick a Tue/Wed/Thu as T-0)

**T-14 → T-7 — prep & seed**
- Finalize all copy (see Asset checklist). Restore/refresh `launch/` drafts.
- Stand up a Product Hunt **"Coming soon"** teaser page to collect notify-me's.
- Submit to newsletters that need lead time (Console.dev, PyCoders, Python Weekly).
- If your HN/Reddit accounts are new, spend two weeks genuinely commenting to build a little karma/history so you're not auto-filtered.
- Line up ~10–20 people who'll *genuinely* try it and comment (never "please upvote").

**T-1 — stage**
- Schedule the Product Hunt launch for **12:01 AM PT**.
- Pre-write the Show HN title + your own first comment (the "why I built this" context).
- Draft the dev.to article; queue Reddit posts (don't post yet).

**T-0 — launch day (hour-by-hour, ET)**
- **8:00–9:00 AM** — Post **Show HN**. First 60–90 min decide the front page; you need ~30–50 upvotes in the first hour from organic interest. Reply to every comment fast and candidly.
- **12:01 AM PT (same calendar day)** — Product Hunt goes live; post your maker first-comment immediately, notify your "coming soon" list.
- **Midday** — Publish the **dev.to article**; cross-post to Hashnode/Medium with a canonical link back.
- **Spread across the day, not all at once** — 1–2 Reddit posts where you'll actually sit and reply. Save the rest for T+1/T+2.

**T+1 → T+7 — amplify & sustain**
- Post to the remaining subreddits (one or two per day, each in its own voice).
- Submit to link-curated newsletters and aggregators (below).
- Submit to the awesome-lists (see the earlier awesome-list picks).
- Reply to every issue/PR/comment within hours — responsiveness at launch converts stars into contributors.
- Write a short "what launch day looked like (numbers included)" follow-up — HN/Reddit love postmortems.

## Channels

### Tier 1 — anchors

| Channel | Where | Notes |
|---|---|---|
| Hacker News | Submit as **Show HN** at news.ycombinator.com | Tue–Thu ~8–10 AM ET (Sunday eve is a quieter second option). Plain, specific title; no hype, no version number. Don't ask for upvotes — ring detection is real. |
| Product Hunt | producthunt.com/launch | Launch 12:01 AM PT, Tue–Thu (dev tools also do fine on weekends with less competition). Coming-soon teaser beforehand; strong first comment; gallery = the demo GIF. |

### Reddit (post natively, read each sub's self-promo rules first, engage in comments)

| Subreddit | Fit |
|---|---|
| r/programming | Broadest dev reach; a good thread can beat a PH launch. Lead with the technical idea, not the product. |
| r/opensource | MIT OSS tool — core audience. |
| r/Python | It's a Python CLI; check their showcase/self-promo rules and flair. |
| r/commandline | CLI-native crowd. |
| r/coolgithubprojects | Purpose-built for sharing repos. |
| r/devops | README-verification + Docker/CI angle. |
| r/selfhosted | Self-hosted, runs-on-your-machine angle. |
| r/LocalLLaMA (~266k) | AI-agent angle; very active, allergic to fluff — bring the technical detail. |
| r/ClaudeAI | Claude Code is your default engine — directly relevant. |
| r/artificial, r/machinelearningnews | AI-agent framing for a broader AI audience. |
| r/SideProject, r/EntrepreneurRideAlong | Builder communities for the launch story. |

Rule of thumb: **one subreddit at a time**, each with a title written for *that* community, and be present to answer. Blasting all of them in one hour gets you spam-filtered and mod-removed.

### Written content (owned narrative + backlinks)

- **dev.to** — primary launch article ("I made an AI that refuses to publish a tutorial it didn't run twice"). Dev-native, easy reach.
- **Hashnode** and **Medium** (dev publications like *Level Up Coding*, *ITNEXT*; *Towards Data Science* for the AI angle) — cross-post with a `rel=canonical` back to dev.to or your docs so you don't split SEO.
- **Your docs site** — a `/blog` or a "How it works" deep-dive doubles as the canonical, ranking-friendly version.

### Newsletters & aggregators (curated — submit, then wait for their cadence)

| Name | Why / link |
|---|---|
| Console.dev | Reviews 2–3 devtools weekly — squarely your fit. console.dev |
| Changelog (+ Changelog Nightly) | Nightly auto-features trending new GitHub repos; the weekly covers OSS. changelog.com |
| TLDR / TLDR AI | Huge dev + AI dailies. tldr.tech |
| Pointer | Curated reading for devs. pointer.io |
| PyCoder's Weekly | Python — submit at pycoders.com/submissions |
| Python Weekly | Python tool/newsletter feature. |
| Hacker Newsletter | Curates the week's best HN — a strong Show HN gets picked up. |
| Ben's Bites / Latent Space | AI-builder audiences for the agent angle. |
| jackbridger/developer-newsletters | A directory to find more niche devtool newsletters to pitch. |

### Directories & discovery

- **AlternativeTo**, **SaaSHub**, **StackShare** — list the project; steady long-tail discovery + backlinks.
- **GitHub Trending** — not submittable, but a good launch-day spike (stars velocity) can land you there; it compounds.
- **Lobsters** (lobste.rs) — high-signal HN alternative, but **invite-only**; only viable if a member invites you. Worth asking around for an invite pre-launch.
- **Awesome-lists** — the picks from the earlier step (awesome-ai-agents, awesome-claude-code, awesome-readme-tools, awesome-docs, etc.).

### Communities / chat

- **Anthropic/Claude developer Discord** and **r/ClaudeAI** — you're built on Claude Code; share in the show-and-tell channels.
- Relevant **Slack/Discord** dev communities you're already in (share where you're a real member, not cold).
- _LinkedIn: intentionally skipped._

## Asset checklist (prepare before T-0)

- [ ] **Show HN**: plain title + your first comment (origin story, the grounding invariant, honest limitations). Draft in `launch/SHOW_HN_DRAFT.md`.
- [ ] **Product Hunt**: 60-char tagline, description, first maker comment, gallery (demo GIF + 2–3 shots), topics/tags, thumbnail.
- [ ] **Article**: `launch/DEVTO_POST_DRAFT.md` — publish on dev.to, cross-post elsewhere.
- [ ] **Reddit**: 3–4 per-community title/body variants (not one copy-paste).
- [ ] **Visuals**: the verified demo GIF (done), `assets/social-preview.png` (done), one clean terminal screenshot of `✓ R2D_VERIFY_OK`.
- [ ] **The "why"**: one crisp paragraph on the grounding invariant — it's your differentiator; repeat it verbatim everywhere.

## Suggested hooks (tune per channel)

- Show HN: `Show HN: readme2demo – runs your README in a sandbox, publishes only what re-runs`
- Product Hunt tagline: `Verified tutorials & demo videos, generated from your README`
- Reddit (r/Python): `I built a tool that runs your README in a Docker sandbox and only publishes the tutorial if a fresh-container replay passes`
- Article: `The AI that refuses to publish a tutorial it didn't run twice`

## What to measure

GitHub stars/day and traffic sources (Insights → Traffic), Show HN rank + comment sentiment, Product Hunt rank/upvotes, docs-site visitors, and — the real signal — issues and PRs opened. Capture it for the T+7 "how the launch went" writeup.

## Etiquette / risks (the stuff that sinks launches)

- **Never** ask for upvotes on HN or PH; both detect vote rings and will penalize you.
- **Don't** cross-post identical text to many subreddits at once — mods and spam filters remove it, and you can't engage everywhere.
- **Read each community's self-promotion rules** before posting; some require a participation ratio or a specific thread.
- **Be candid about limitations** (the API-key-in-sandbox tradeoff, experimental OpenHands engine). Admitting weaknesses earns more trust than hiding them.
- **Lobsters is invite-only** — don't count on it unless you have an invite.
