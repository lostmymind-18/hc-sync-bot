# Decision log

Short records of the load-bearing decisions in this project: the context, what
was chosen, what was rejected, and the evidence behind it. Deeper dives live in
their own docs where noted.

---

## 1. Data source — Zendesk Help Center API, not HTML scraping

**Context.** `support.optisigns.com` is a Zendesk Guide help center. Each
article is available via the public Help Center API as JSON with `id`, `title`,
`body` (already-clean article HTML, no site nav/chrome), `html_url`, and
`updated_at`.

**Decision.** Pull from the API and convert `body` to Markdown, rather than
rendering and scraping pages with a DOM scraper.

**Why / evidence.** The API `body` is the article content without boilerplate,
so "strip nav/ads" is free. `updated_at` comes for free and is the basis for
delta detection. Pagination is a stable cursor (`links.next` + `meta.has_more`).
Verified against the live subdomain: 402 articles, no auth required for GET on
published articles.

**Consequence.** Conversion is a pure HTML→Markdown transform (markdownify) with
YAML frontmatter (`article_id`, `title`, `url`, `updated_at`); filenames are
title slugs, the numeric id kept in frontmatter.

---

## 2. Chunking — static 800 / 400, *validated empirically*

**Context.** The vector store needs a chunking strategy, and the README must
justify it (not a number picked at random).

**Decision.** Static chunking, `max_chunk_size_tokens=800`,
`chunk_overlap_tokens=400`.

**Process.** Rather than argue from intuition, built a measurement harness
(`eval/chunking_eval.py`): for each strategy it spins up a throwaway vector
store, runs a ground-truth Q&A set, and scores **retrieval hit** (by reading the
chunks `file_search` actually retrieved via run steps), **answer coverage** of
required facts (including facts buried mid-document / in tables), citation
correctness, and bullet-count. Compared `static_800_400`, `auto`, and a
structure-aware `section` split, with `--repeats=3` to remove run noise.

**Evidence.** All strategies were essentially tied: retrieval-hit ≈ 1.00,
deep-fact coverage ≈ 0.89, identical. For articles this short (~2k tokens),
**chunk sizing is not the bottleneck** — overlap + retrieval compensate for any
mid-procedure split. Kept the explicit static setting (spec wants an explained
static choice; it ties the best and is fully controllable).

**Lesson.** Measure before optimizing — see `lessons-learned.md`.

---

## 3. Answer model — gpt-4o, not gpt-4.1 or gpt-4-turbo

**Context.** The Assistant answers from the docs via `file_search`. Choosing the
model affects grounding quality and citation behavior.

**Decision.** `gpt-4o`. Temperature is a Playground/runtime knob (it does not
affect the upload job, which never queries the model) and is left at the
default.

**Evidence.** Tested the verbatim prompt against the same docs:
- `gpt-4.1` — under `file_search` it **hallucinated URLs** (invented
  `optisigns.com/app/tutorial/...` paths) and degraded content. Rejected.
- `gpt-4-turbo` — emits literal text well *without* the tool, but reverts to
  hyperlink citations *with* `file_search`. No advantage here.
- `gpt-4o` — grounds correctly with the real `support.optisigns.com` URLs.

---

## 4. Citations — URL-as-filename, accept the tool's annotation system

**Context.** The verbatim system prompt asks the bot to "Cite up to 3
'Article URL:' lines". gpt-4o would not emit literal `Article URL:` lines; it
rendered markdown hyperlinks / `【n†source】` annotations instead.

**Process (one variable at a time).**
- Putting the URL only at the top of a doc → absent from the chunks retrieved
  for mid-article questions. Interleaving the URL through the body (so every
  chunk carries it) → **no effect** on the output format.
- Temperature 1.0 → 0.2 → 0 → **no effect**.
- Plain `chat.completions` (no tool) with the same doc → gpt-4o still
  hyperlinks; `gpt-4-turbo` produces the literal line. So the literal format is
  *achievable* by the model, but something suppresses it under the Assistant.

**Root cause (proven).** The `file_search` tool injects its **own** citation
convention (`【n†source】` annotations) that overrides the system prompt's
literal-format request. Same model: literal *without* the tool, hyperlinks
*with* it. You cannot reliably force `Article URL:` text through `file_search`.

**Decision.** Keep gpt-4o (grounding wins), and set each uploaded file's
**filename to the bare article URL** so the annotation/citation resolves to the
real URL (visible in the Playground citation panel and hyperlinks). The URL is
also written into the doc body for traceability.

**Gotchas (each verified).**
- A trailing `.md` on the filename shows up inside the citation → omit it; an
  explicit `text/markdown` MIME type is enough for the Files API to accept it.
- Content that **starts with** `Article URL: <url>` is MIME-sniffed as
  `message/news` and rejected (400) → keep frontmatter / a heading first.

**Note on scope.** The "Article URL:" screenshot is spec requirement #8, a
*sanity check*, not a scored bucket. The citation showing the real URL satisfies
it; chasing the exact literal prefix would have cost grounding quality.

---

## 5. Delta detection — `updated_at`, reconstructed from the vector store

**Context.** The daily job must upload only the delta, and stay correct when run
repeatedly. The deployed container (DO scheduled job) is ephemeral — a local
`state.json` is wiped between runs.

**Decision.** Compare each article's Zendesk `updated_at` against the value
stored as an OpenAI **file attribute**, and **reconstruct prior state from the
vector store on every run** rather than trusting a local file. `state.json`
becomes a derived debug artifact; the store is the source of truth. Duplicate
copies are reconciled to one-per-article (self-healing).

**Why this over the alternatives, and the full mechanism + idempotency
argument:** see [`stateless-delta-design.md`](stateless-delta-design.md).

**Evidence.** Probed that file attributes are settable on attach, returned on
`list`, and mutable via `update` (no re-embed) before building on them. Unit
tests for the pure index/dedup logic (`tests/test_reconstruct.py`), an
integration acceptance harness asserting one-file-per-article across
add/skip/update/reconcile (`eval/verify_stateless.py`), and a real-environment
check: delete `state.json`, run against the live 402-file store →
`added=0 updated=0 skipped=402`, total unchanged.

---

## 6. Deployment — DO App Platform Scheduled Job, run-once container

**Context.** Need a daily cadence; the ~10h budget says don't over-build infra.

**Decision.** A DigitalOcean App Platform **Scheduled Job** (cron `0 6 * * *`
UTC, `deploy_on_push: true`) built from the Docker image. `ENTRYPOINT
["python", "main.py"]` runs once and exits 0 — no internal loop; the platform
owns the cadence.

**Consequence.** The container's ephemeral filesystem is exactly what drove
decision #5: a local-state delta design passes locally but silently duplicates
the whole corpus daily in production.

---

## 7. Batched attach, to fit the 30-minute job timeout

**Context.** Attaching files one-by-one with `create_and_poll` blocks on
server-side embedding (~12s/file). The first production run hit this: with the
stateless bug from #5 not yet fixed, the deployed (old) job tried to re-upload
all 402, ran past DigitalOcean's **30-minute** container limit, was killed at
~183 files, and left partial duplicates (which the #5 reconcile later
self-healed). Even with #5 fixed, a one-off large delta or a cold start would
have the same problem: 402 × 12s ≈ 80 min.

**Decision.** In `sync()`, upload all changed files, then attach them in a
single `file_batches.create_and_poll` (server embeds them in parallel, one
poll), and set per-file `attributes` afterward via `files.update`. The batch API
only accepts one shared `attributes` dict, so the per-file step can't be folded
in — but `files.update` is cheap (no re-embedding).

**Evidence.** Measured a cold start of all 402 articles via the batch path:
**~12.5 min** (751s) end-to-end, vs the ~80 min / timeout of the per-file path.
Comfortably inside the 30-min limit, and steady-state daily runs (a handful of
deltas, or none) finish in seconds. Acceptance tests (`eval/verify_stateless.py`)
pass unchanged on the batch path.

**Consequence.** The realistic daily job was already safe after #5 (it does ~0
uploads); this also bounds the pathological large-delta / cold-start case so the
job cannot time out in normal operation.
