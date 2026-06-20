# The build: process, experiments, and lessons

This document is the story of *how* the system was built, not just what it is.
It keeps the dead ends, the experiments, and the evidence — because the
reasoning is the real deliverable. Read it as: (a) evidence of the process for
the review conversation, and (b) a playbook for the next project.

The clean summaries live elsewhere; this is the narrative that ties them
together:
- [`decisions.md`](decisions.md) — the final decisions, one record each.
- [`stateless-delta-design.md`](stateless-delta-design.md) — the deep dive on
  the hardest one.
- [`lessons-learned.md`](lessons-learned.md) — the reusable lessons, distilled.

---

## How we worked (the method that kept paying off)

A few habits did most of the heavy lifting. They show up again and again below.

1. **Evidence over assertion.** Every "it's probably X" was treated as a
   hypothesis to test, never a conclusion. The first wrong turn (below) came
   from skipping this; nothing went wrong once we stopped.
2. **Isolate one variable at a time.** When output was wrong, change exactly
   one thing, observe, repeat — until the cause is the only thing left.
3. **Verify the external API before designing on it.** A five-minute probe
   beats a confident assumption that collapses mid-implementation.
4. **Measure, don't argue.** When a choice is empirical, build a small harness
   that scores it. Opinions become numbers, and the harness is reusable.
5. **Write the acceptance test first.** Name the invariant as an executable
   check before implementing; "done" stops being subjective.
6. **Reproduce the production constraint in the test.** A test that passes in an
   environment the code won't run in proves little.
7. **Verify before anything destructive.** Look at what you're about to clear or
   overwrite first.

---

## Episode 1 — A baseline that worked

The straightforward part. `support.optisigns.com` is a Zendesk Guide help
center, so we pulled articles from the public Help Center API (clean `body`
HTML, `updated_at`, `html_url` for free) instead of scraping rendered pages —
402 articles, cursor pagination, no auth. Convert `body` → Markdown
(markdownify) with YAML frontmatter, upload via the Files API, attach to a
Vector Store, answer with an Assistant (`file_search`, gpt-4o). This came up
quickly and mostly stayed put. The interesting work was everywhere the obvious
approach quietly failed.

---

## Episode 2 — The citation that wouldn't print (the longest detour)

**Symptom.** The verbatim system prompt says *"Cite up to 3 'Article URL:'
lines."* The bot never printed them — it rendered markdown hyperlinks instead.

**The wrong turn.** The first explanation offered was "that's just GPT-4
behavior." That was an assertion with no evidence, and it was challenged. Good —
because it was wrong. The real method:

| Experiment | Result | Rules out |
| --- | --- | --- |
| Put the URL in every chunk (interleave through the body) | still 0 literal lines | retrieval |
| Sweep temperature 1.0 → 0.2 → 0 | still 0 | sampling |
| Switch model to gpt-4.1 | literal-ish, but **hallucinated URLs** (`optisigns.com/app/tutorial/...`) | (gpt-4.1 unusable) |
| gpt-4-turbo via `file_search` | reverts to hyperlinks | model identity alone |
| gpt-4-turbo / gpt-4o via plain `chat.completions` (no tool) | literal line appears | proves the model *can* |

**Root cause, proven.** The `file_search` tool injects its *own* citation
convention (`【n†source】` annotations) that overrides the prompt's literal
request. Same model: literal without the tool, hyperlinks with it. You cannot
reliably force the literal text through `file_search`.

**Resolution.** Keep gpt-4o (it grounds correctly; gpt-4.1 fabricates), and set
each file's **filename to the bare article URL** so the annotation resolves to
the real URL — visible in the Playground citation panel. Two gotchas surfaced,
each caught by testing: a trailing `.md` leaks into the citation (dropped it,
the explicit `text/markdown` MIME is enough), and content that *starts with*
`Article URL:` is MIME-sniffed as `message/news` and rejected (put frontmatter
first).

**Reframe that saved time.** The "Article URL:" screenshot is spec requirement
#8 — a *sanity check*, not a scored bucket. The citation showing the real URL
satisfies it. Chasing the exact literal prefix would have traded away grounding
quality for nothing.

**Lessons.** #2 (evidence over assertion), #7 (know which layer wins — a managed
tool overrode the prompt). And: re-read what's actually being graded before
optimizing.

---

## Episode 3 — "Which chunking is best?" → build a measuring stick

**Symptom.** A choice with no obvious answer, and a temptation to argue it from
intuition.

**What we built instead.** `eval/chunking_eval.py`: per strategy, spin up a
throwaway vector store, run a ground-truth Q&A set, and score **retrieval hit**
(by reading the chunks `file_search` *actually retrieved* via run steps),
**answer coverage** of required facts (including facts buried mid-document and in
tables), citation correctness, and bullet-count.

**The harness exposed its own flaw.** A single 8-question run of the same
strategy disagreed with itself by 1–2 questions — transient empty-answer runs.
So we added retry-on-empty and `--repeats=N` averaging before trusting any
ranking.

**Evidence (`--repeats=3`).**

```
strategy               files  coverage  deep_cov  retr_hit  citation  bullet_viol
static_800_400             5      0.96      0.89      1.00      1.00         3
auto                       5      0.96      0.89      1.00      1.00         3
section_static_800_0      42      0.96      0.89      1.00      0.88         4
```

**Finding.** For articles this short (~2k tokens), **chunk sizing is not the
bottleneck** — retrieval hit is 100% and deep-fact coverage is identical across
strategies. Kept the explicit `static 800/400` (the spec wants an explained
static choice; it ties the best and is fully controllable). The defensible
review answer became *"we measured it,"* not *"we picked 800."*

**Lessons.** #4 (measure, don't guess), #3 (handle the noise before ranking).

---

## Episode 4 — The hidden bug: a job that can't keep state

**The question that surfaced it.** "We changed how we parse/chunk — do we need
to fix the deployed bot?" Pulling that thread revealed something bigger than a
code-sync.

**The bug.** Delta detection compared each article's `updated_at` to a value in
a local `data/state.json`. Correct locally. But the deployed job is a
DigitalOcean **scheduled job on an ephemeral container** — `state.json` is wiped
between runs. So every run saw empty state → treated all 402 as new →
re-uploaded the whole corpus. The local idempotency self-check passed for the
*wrong reason*: the file happened to survive on the local disk.

**The decision.** Stop mirroring the store's truth in a second place that can
drift. **Reconstruct the prior state from the vector store itself on every run**:
store `{article_id, updated_at, url}` as OpenAI file *attributes*, list them to
rebuild state, and reconcile any duplicates to one-per-article (self-healing).
`state.json` becomes derived debug output. (Full argument, including a precise
definition of *idempotent* and why this version is *unconditionally* idempotent,
in [`stateless-delta-design.md`](stateless-delta-design.md).)

**Verified the API before building on it** (these were probed, not assumed):
- file attributes are settable on attach, **returned on `list`** (so
  reconstruction is one paginated call, not N retrieves), and
- mutable via `files.update` **without re-embedding** (so migration is cheap).

**Test-first.** The invariant was named and encoded before any code:

> every live article maps to exactly one file in the store, and nothing else.

`tests/test_reconstruct.py` (pure unit tests for the dedup/index logic) and
`eval/verify_stateless.py` (integration: add → skip → update → reconcile, each
asserting the invariant) went red for the right reason, then green.

**The eventual-consistency surprise.** Two test failures looked like logic bugs
but were timing: after `files.update`, both `list` and `retrieve` returned the
**old** value for ~3 seconds; after a delete, `list` still showed the file
briefly. Production never hits this (reads happen on a later run), but tests do —
so the harness now **polls to convergence** instead of asserting immediately
after a write.

**Lessons.** #1 (verify the API), #5 (test-first), #3 (eventual consistency), #6
(reproduce the production constraint — we delete `state.json` and re-run to
simulate the ephemeral container).

---

## Episode 5 — The production incident (timeout + duplication)

**What happened.** Before the fix was deployed, the old code's scheduled run
fired at 06:00 UTC. The DO dashboard showed **`Container Timeout` — FAILED**,
`1:00:12 → 1:30:07` (exactly 30 minutes). The production store had grown from
**402 → 585** (402 attributed + 183 attribute-less duplicates).

**Diagnosis (the chain).** DO scheduled jobs have a hard **30-minute** limit.
The old (stateless-bug) code re-uploaded all 402; each `create_and_poll` blocks
~12s on embedding → 402 × 12s ≈ 80 min ≫ 30 min → DO killed it at ~183 files,
leaving partial duplicates. The timeout was a *symptom* of the Episode-4 bug.

**Recovery — and a proof.** This was exactly the self-healing case the new code
was designed for. Running it once reconstructed state, found two files for each
of those 183 articles (one attributed, one not), kept the attributed one, and
reconciled the rest: *"Reconciled 183 duplicate file(s)"* → store back to
**402**. Self-healing, demonstrated on the real store, not just in a test.

**Lessons.** #6 (a managed-platform limit turned slow-but-correct into a hard
failure — know your caps), and the value of designing for self-healing: the
incident was *recoverable by the same mechanism that prevents it*.

---

## Episode 6 — Fast enough to never time out

**The remaining risk.** Even with the stateless fix (steady-state runs do ~0
uploads), a large delta day or a cold start would still hit 402 × 12s.

**Probe first.** `file_batches.create_and_poll` embeds many files in parallel
and polls once — but takes a *single shared* `attributes` dict, while we need
per-file attributes. So: do the expensive shared work in bulk (batch attach +
embed), then the cheap per-item work separately (`files.update` per file, no
re-embed).

**Evidence.** Measured a full cold start of all 402 via the batch path:

```
COLD-START added=402 in 751s (12.5 min) | 30-min limit = 1800s
```

12.5 min vs the old ~80 min / timeout — comfortably inside the limit, with
steady-state daily runs finishing in seconds.

**Lesson.** #9 (bulk APIs trade per-item flexibility for throughput — split the
fast bulk path from the cheap per-item pass; and *measure the worst case against
the platform's cap* rather than hand-waving "batch is faster").

---

## Episode 7 — Closing the loop: a real green run

Deployed via `deploy_on_push`. To prove the fix end-to-end without waiting a day,
we temporarily moved the cron a few minutes ahead, watched the deployment go
ACTIVE, and let the scheduled job fire on the real platform:

```
Jun 20 08:08:40  Run complete: added=0 updated=0 skipped=402
                 total_live_articles=402 | vector_store files: ... total=402
```

A successful DigitalOcean run — `skipped=402`, ~12 seconds, no timeout, no
duplication (`docs/do_logs.png`) — then reverted the cron. One last edge surfaced
and was closed: a single "ghost" vector-store entry whose underlying file object
had been deleted (a race during an earlier update). We removed it, then hardened
`reconstruct_state_from_store` to report such dead pointers for removal too, so
the *"one file per article, and nothing else"* invariant holds without manual
help.

---

## What the experiments proved (at a glance)

| Question | Experiment | Verdict |
| --- | --- | --- |
| Why no literal `Article URL:`? | interleave / temperature / model sweeps; tool-on vs tool-off | the `file_search` tool's citation convention overrides the prompt |
| Best chunking strategy? | scored harness, 3 strategies, repeats=3 | not the bottleneck for short articles; keep `static 800/400` |
| Can we rebuild state from the store? | probe attributes on attach/list/update | yes — list-only, mutable without re-embed |
| Is the job idempotent without local state? | delete `state.json`, run on the live 402-file store | `added=0 updated=0 skipped=402`, total unchanged |
| Does it self-heal pollution? | reconcile after the 585-file incident | 183 duplicates removed → 402 |
| Will it ever time out? | timed cold start of 402 via batch | 12.5 min < 30 min cap |
| Does it work on the real platform? | temporary cron, watch a live run | `skipped=402` in ~12s, no timeout |

---

## Final shape

```
scraper.py        Zendesk Help Center API client (paginate, fetch)
converter.py      HTML -> Markdown + frontmatter, URL-as-citation
state_store.py    pure delta logic (diff_articles) over a state dict
vector_store_client.py
                  Files/Vector-Store API: upload, BATCH attach (+attributes),
                  reconstruct_state_from_store (+ dedup/dead-entry reconcile)
main.py           sync(): reconstruct -> reconcile -> diff -> batch apply
                  run once, exit 0 (no internal loop)
eval/             chunking_eval, verify_stateless, backfill_attributes
tests/            pure unit tests (state_store, reconstruct)
docs/             decisions, stateless-delta-design, lessons-learned, this file
```

The system the spec asked for is the easy 30%. The 70% that mattered was the
work in the gaps the obvious approach skipped — and the discipline of proving,
not assuming, the way through each one.
