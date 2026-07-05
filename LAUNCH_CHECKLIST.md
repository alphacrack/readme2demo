# readme2demo — open-source launch checklist

Everything below is ordered. Items marked **[done]** were added to the repo
already; **[you]** must be done by you on GitHub.com (or with the `gh` CLI)
because they are account/repo settings, not files.

---

## 0. Pre-flight (local)

- [done] Tests green: `python -m pytest tests/ -q` → 175 passed
- [done] Lint gate green: `ruff check src/ tests/`
- [done] Docs build clean: `mkdocs build --strict` (build to a clean dir)

**Two things to fix FIRST (do these before any `git` command):**

1. **Remove the stale git lock.** A zero-byte `.git/index.lock` is present and
   will block every git command ("Unable to create index.lock: File exists").
   Delete it:

   ```bash
   rm -f .git/index.lock
   ```

2. **Restore `launch/` before you stage anything.** The tracked `launch/`
   directory (`SHOW_HN_DRAFT.md`, `DEVTO_POST_DRAFT.md`, `LANDING_COPY.md`,
   `LAUNCH_CHECKLIST.md`) is missing from the working tree, so a blind
   `git add -A` would **commit its deletion** and wipe your go-public drafts.
   Either restore them, or intentionally decide to drop them:

   ```bash
   git status                       # confirm the "deleted: launch/..." lines
   git checkout HEAD -- launch/     # restore the drafts (recommended)
   ```

- [ ] **[you]** Then commit and push:

```bash
rm -f .git/index.lock
git checkout HEAD -- launch/         # keep your existing launch drafts
git add -A
git status                            # sanity-check: no unexpected deletions
git commit -m "chore: OSS launch — governance, lint gate, docs site, SEO/GEO"
git push origin main
```

> The local `site/` directory (mkdocs output) is gitignored — safe to delete.
> Note: this file duplicates the older `launch/LAUNCH_CHECKLIST.md` (a
> marketing-focused version). Keep whichever you prefer, or move this one into
> `launch/` and merge them.

---

## 1. Repository "About" — the single biggest GitHub SEO lever

The About box (description + topics) is what GitHub search and Google index
first. Set it precisely.

- [ ] **[you]** Description (Settings → General, or the gear on the repo home):

  > Verified tutorials and demo videos from your README. An AI agent runs it in a hardened Docker sandbox and replays it in a fresh container before anything is published.

- [ ] **[you]** Website field → your docs site URL (after step 3):
  `https://alphacrack.github.io/readme2demo/`

- [ ] **[you]** Topics (add all — these are keyword tags people search):

```
ai-agents  developer-tools  documentation  docs-as-code  devops
tutorial-generator  demo-video  vhs  sandbox  verification
claude  llm  cli  python  readme
```

`gh` one-liner:

```bash
gh repo edit alphacrack/readme2demo \
  --description "Verified tutorials and demo videos from your README. An AI agent runs it in a hardened Docker sandbox and replays it in a fresh container before anything is published." \
  --homepage "https://alphacrack.github.io/readme2demo/" \
  --add-topic ai-agents --add-topic developer-tools --add-topic documentation \
  --add-topic docs-as-code --add-topic devops --add-topic tutorial-generator \
  --add-topic demo-video --add-topic vhs --add-topic sandbox --add-topic verification \
  --add-topic claude --add-topic llm --add-topic cli --add-topic python --add-topic readme
```

---

## 2. Repository features

- [ ] **[you]** Enable **Discussions** (Settings → General → Features). Your
  issue-template `config.yml` already links to it.
- [ ] **[you]** Enable **Issues** (on by default).
- [ ] **[you]** Turn **Wikis** off (docs live in the docs site instead).
- [ ] **[you]** Settings → Pull Requests → enable "Automatically delete head
  branches."

---

## 3. Publish the docs site (GitHub Pages)

The site is built by `.github/workflows/docs.yml` (already added).

- [ ] **[you]** Settings → **Pages** → Source = **GitHub Actions**.
- [ ] **[you]** Push to `main` (or run the `docs` workflow via Actions →
  "docs" → Run workflow). First deploy publishes to
  `https://alphacrack.github.io/readme2demo/`.
- [ ] **[you]** Verify `https://alphacrack.github.io/readme2demo/llms.txt`
  loads (the GEO file).
- [ ] Optional custom domain: add `docs/CNAME` with the domain, update
  `site_url` in `mkdocs.yml`, and set the DNS record.

---

## 4. Branch protection (protect `main`)

- [ ] **[you]** Settings → Branches → Add rule for `main`:
  - Require a pull request before merging (1 approval).
  - Require status checks to pass → select **lint** and the **test** matrix jobs.
  - Require branches to be up to date before merging.
  - (Optional) Require signed commits.

Use a typed JSON body via `--input` (the `-f` flag sends everything as strings,
which the API rejects with a 422). The status-check contexts must match the CI
job names (`lint`, and `test (3.x)` for each matrix Python version):

```bash
gh api -X PUT repos/alphacrack/readme2demo/branches/main/protection \
  -H "Accept: application/vnd.github+json" \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["lint", "test (3.10)", "test (3.11)", "test (3.12)", "test (3.13)"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": { "required_approving_review_count": 1 },
  "restrictions": null
}
JSON
```

> **Solo-maintainer caveat:** `required_approving_review_count: 1` means you
> can't merge your own PRs without a second person approving. If you're the
> only maintainer, set it to `0`, or replace the whole
> `"required_pull_request_reviews"` value with `null`, so status checks still
> gate merges but you aren't locked out.

---

## 5. Security settings

- [ ] **[you]** Settings → Advanced Security / Code security:
  - Enable **Dependabot alerts** and **Dependabot security updates**
    (`.github/dependabot.yml` handles version bumps).
  - Enable **Private vulnerability reporting** (your `SECURITY.md` and
    issue-template `config.yml` already point here).
  - Enable **Secret scanning** (and push protection).

---

## 6. Labels for community contribution

Your `CONTRIBUTING.md` references `good first issue` and `help wanted`.

- [ ] **[you]** Create/confirm labels:

```bash
gh label create "good first issue" --color 7057ff --description "Good for newcomers" --force
gh label create "help wanted" --color 008672 --description "Extra attention is wanted" --force
gh label create "dependencies" --color 0366d6 --description "Dependency updates" --force
```

- [ ] **[you]** Open 3–5 starter issues (from `CONTRIBUTING.md`'s "help
  wanted" list: egress proxy with key injection, OpenHands hardening, GitHub
  Action wrapper, docs-site URL ingestion) and label them.

---

## 7. Social preview image (link unfurls / Open Graph)

- [ ] **[you]** Settings → General → **Social preview** → upload a 1280×640 PNG
  (project name + one-line tagline + the verified-demo idea). This is what
  shows on X, Slack, LinkedIn, Google. High impact for click-through.

---

## 8. SEO specifics (search engines)

- [done] Keyword-rich README H1 and description.
- [done] Docs site with `site_url` (canonical URLs) + auto sitemap.xml.
- [done] `CITATION.cff` → GitHub shows a "Cite this repository" button.
- [ ] **[you]** After Pages is live, submit the sitemap to Google Search
  Console: `https://alphacrack.github.io/readme2demo/sitemap.xml`.
- [ ] **[you]** Add the repo to relevant awesome-lists (awesome-devtools,
  awesome-ai-agents, awesome-documentation) — backlinks drive ranking.

---

## 9. GEO specifics (being cited by AI answer engines)

- [done] `docs/llms.txt` served at the site root — the emerging standard file
  AI crawlers read for a clean, quotable project summary.
- [done] FAQ page in question-and-answer form (LLMs quote clear Q&A).
- [done] `howto.jsonld` structured data shipped in every run's output.
- [done] Consistent entity naming ("readme2demo") across README, docs, and
  CITATION so models resolve you to one project.
- [ ] **[you]** Keep the one-line description identical everywhere (repo,
  docs, llms.txt, CITATION) — consistency is what makes a model confident
  enough to cite you.

---

## 10. Cut the first release

- [ ] **[you]** Tag and release `v0.1.0` (the `CHANGELOG.md` 0.1.0 section is
  ready):

```bash
git tag -a v0.1.0 -m "readme2demo v0.1.0"
git push origin v0.1.0
gh release create v0.1.0 --title "v0.1.0" --notes-from-tag --latest
```

> PyPI publishing is intentionally deferred for now (your choice). When you
> want it: reserve the name, add a `release.yml` that builds and publishes via
> PyPI Trusted Publishing on tag, and add a PyPI badge + `pip install
> readme2demo` line to the README.

---

## 11. Announce

- [ ] **[you]** Post the verified demo GIF + one-liner where developers are:
  Hacker News (Show HN), r/programming, r/devtools, X, relevant Discords/Slacks,
  and dev.to / a short blog post that links back to the docs site (backlinks).
  _(Per your earlier preference, skipping LinkedIn.)_ You already have drafts in
  `launch/` — `SHOW_HN_DRAFT.md`, `DEVTO_POST_DRAFT.md`, `LANDING_COPY.md`
  (restore them per step 0 if they're missing locally).
- [ ] **[you]** Pin a "Welcome / start here" Discussion.

---

### What's already in the repo vs. what needs GitHub.com

**Added for you (this session):** `CHANGELOG.md`, `CITATION.cff`,
`.github/dependabot.yml`, `.github/CODEOWNERS`, `.github/FUNDING.yml`,
`.github/workflows/docs.yml`, lint job in `.github/workflows/ci.yml`,
`readme2demo.toml.example`, `.env.example`, the `docs/` site + `mkdocs.yml` +
`docs/llms.txt`, README demo GIF + doc-count fix, and the `ruff` gate.

**Only you can do (settings live on GitHub):** About/topics, enable
Discussions & Pages, branch protection, security features, labels, social
preview image, the release, and the announcement.
