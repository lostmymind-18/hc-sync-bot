# Lessons learned

Reusable engineering lessons from building this project — written to be useful
on the *next* RAG / external-API / scheduled-job project, not just this one.
The concrete decisions they came from are in `decisions.md` and
`stateless-delta-design.md`.

---

## 1. Verify external-API assumptions empirically before designing on them

The whole stateless-delta design hinges on "OpenAI returns file `attributes` on
the `list` call." It would have been easy to *assume* that and write the design
around it. Instead: a 20-line probe against a throwaway vector store confirmed
(a) attributes are settable on attach, (b) returned on `list` (so reconstruction
is one paginated call, not N retrieves), and (c) mutable via `update` without
re-embedding (so migration is cheap).

**Takeaway.** Before a design depends on an API behavior, spend five minutes
proving the behavior. A cheap probe beats a confident assumption that collapses
mid-implementation.

---

## 2. Reject unsubstantiated root causes; isolate one variable at a time

The "why won't it print `Article URL:`" investigation went wrong first by
blaming "GPT-4 behavior" with no evidence. The real method that worked:

- interleave the URL through chunks → no change (rules out *retrieval*),
- sweep temperature 1.0/0.2/0 → no change (rules out *sampling*),
- drop the tool and use plain `chat.completions` → literal format appears
  (proves the *model is capable*),
- so the remaining difference — the `file_search` tool — is the cause.

Each step changed exactly one thing and was checked against output. The
conclusion ("the tool's citation convention overrides the prompt") was then
*proven*, not asserted.

**Takeaway.** "It's probably X" is a hypothesis, not a finding. Change one
variable, observe, repeat, until the cause is the only thing left.

---

## 3. Vector stores (and most cloud stores) are eventually consistent

Two test failures looked like logic bugs but were timing:

- after `files.update(attributes=...)`, both `list` *and* `retrieve` returned
  the **old** value for ~3 seconds;
- after deleting a file, `list` still showed it for a few seconds.

This never affects production here, because the only place that writes an
attribute and then reads it back is a *test* — real reads happen on a later run,
hours after the write. But it does affect how you test.

**Takeaway.** Test an eventually-consistent system by **polling to convergence**
with a timeout, not with a single-shot assertion right after a write. A genuine
bug still fails (it never converges); transient lag doesn't produce flakes. And
when an immediate read-after-write is unavoidable in a test, wait for the value
to propagate first.

---

## 4. Build a measurement harness instead of arguing from intuition

"Which chunking strategy is best?" was answered not by debate but by
`eval/chunking_eval.py`: throwaway store per strategy, ground-truth Q&A, scored
on what the model *actually retrieved*. The result — chunk sizing barely moves
quality for short articles — was a fact with numbers behind it, defensible in a
review. The harness also outlived the question: it's the template for trying any
future strategy by adding one registry entry.

**Takeaway.** When a choice is empirical (retrieval quality, latency, cost),
the highest-leverage move is a small harness that *measures* it. It converts
opinions into evidence and becomes reusable infrastructure.

---

## 5. Write the acceptance test before the implementation (TDD)

For the stateless rework, the invariant was named first —

> every live article maps to exactly one file in the store, and nothing else —

and encoded as `P2` in `eval/verify_stateless.py`, plus pure unit tests for the
index/dedup logic, *before* any code was written. The tests went red for the
right reason (functions didn't exist), then green. Defining "done" up front kept
the implementation honest and made the eventual-consistency surprises obvious
rather than ambiguous.

**Takeaway.** State the invariant as an executable check first. It is both the
spec and the proof.

---

## 6. Design for the deployment environment's real constraints

The local idempotency self-check passed for the wrong reason: `state.json`
happened to survive on the local disk. The deployed environment — an ephemeral
scheduled-job container — wipes it, so the "delta-only" job would have
re-uploaded the entire corpus every day and doubled the store. The local test
never exercised the property that mattered in production.

**Takeaway.** A test that passes in an environment your code won't run in proves
little. Reproduce the production constraint (here: "no persistent disk" →
delete `state.json` and re-run) in the test. Prefer deriving state from the
authoritative store over mirroring it in a second place that can drift.

---

## 7. Know which layer wins when behaviors conflict

A tool (`file_search`) silently overrode an explicit system-prompt instruction.
Frameworks and managed tools inject their own prompting/behavior that can beat
yours, and the layer that wins isn't always documented.

**Takeaway.** When output ignores your instruction, suspect an intervening layer
(a tool, a managed wrapper, a default) before concluding the model "can't." Test
with the layer removed to locate the real authority.

---

## 8. Separate pure logic from I/O so the hard part is unit-testable

The tricky delta logic (`_index_files`: newest-wins dedup, duplicate reporting,
missing-attribute handling) is a pure function over plain dicts, with the
network calls in a thin wrapper around it. That made the genuinely error-prone
part exhaustively testable with hand-built fixtures and zero API cost, leaving
only orchestration for the slower integration test.

**Takeaway.** Push decisions into pure functions and keep I/O at the edges. The
part most likely to be wrong becomes the part cheapest to test.

---

## 9. Bulk APIs trade per-item flexibility for throughput — split the two

The slow part of attaching files was server-side embedding; the batch endpoint
parallelizes it and polls once (402 files: ~12.5 min vs ~80 min one-by-one). But
the batch takes a *single shared* `attributes` dict, while we needed per-file
attributes. Rather than abandon batching, we split it: do the expensive shared
operation in bulk (batch attach + embed), then apply the cheap per-item
operation separately (`files.update` per file, no re-embedding).

Also worth naming: a *managed-environment limit* (the 30-min job timeout) turned
a slow-but-correct approach into a hard failure. Know your platform's caps and
**measure the worst case against them** — here, timing a full cold start
(12.5 min < 30 min) is what actually proves the job can't time out, not a
hand-wave that "batch is faster."

**Takeaway.** When a bulk API seems too rigid, look for a fast bulk path for the
expensive shared work plus a cheap per-item pass for the rest — and verify the
worst case fits the platform's limits with a real measurement.

---

## Deep dive: what a "harness" is, and how this project used it

Several lessons above ("measure, don't guess"; "write the acceptance test
first") lean on the same tool — a **harness** — so it's worth defining clearly,
because it was the single highest-leverage technique in this project.

### What it is

A **harness** is scaffolding code that wraps the thing you're testing so you can
run it **repeatedly, in a controlled and isolated environment, and judge the
result automatically** — instead of running it by hand and eyeballing the
output. "Test harness" and "evaluation (eval) harness" are the same idea pointed
at two questions:

- a **test harness** answers *"is it correct?"* with pass/fail assertions;
- an **eval harness** answers *"how good is it?"* with a score.

Either way the value is the same: it converts a vague judgment ("seems fine",
"probably the best chunking") into a **repeatable, automated verdict** you can
trust, re-run after every change, and show as evidence.

### The two harnesses we built

**1. `eval/chunking_eval.py` — an eval harness (measure quality).**
- *Question:* which chunking strategy retrieves best?
- *Setup:* for each strategy, spin up a **throwaway** vector store (never the
  production one), upload the docs, attach with that strategy.
- *Stimulus:* a fixed **ground-truth Q&A set** (`eval/dataset.json`) with the
  expected source article and key facts per question — including facts buried
  mid-document, to stress chunk boundaries.
- *Measurement:* it reads the **chunks `file_search` actually retrieved** (via
  run steps), not just the final answer, and scores retrieval-hit, fact
  coverage, citation correctness, bullet-count.
- *Noise control:* `--repeats=N` averaging + retry-on-empty, because a single
  run was flaky.
- *Output:* a comparison table. Verdict: chunk sizing isn't the bottleneck for
  short articles — a fact with numbers, not an opinion.
- *Reusable:* add one entry to the `STRATEGIES` dict to test a new tactic.

**2. `eval/verify_stateless.py` — an acceptance test harness (prove correctness).**
- *Invariant under test:* "every live article maps to exactly one file in the
  store, and nothing else" (called `P2`).
- *Setup:* a throwaway store seeded with a few articles.
- *Scenarios:* it drives `sync()` through a sequence — A (empty → all added),
  B (run again with no local state → all skipped, no duplication = the
  ephemeral-container proof), C (force a stale timestamp → updated, replaced not
  appended), D (inject a duplicate → reconciled away).
- *Judgment:* after each scenario it **asserts P2**, polling to convergence to
  tolerate the store's eventual consistency.
- *Output:* `ALL SCENARIOS PASSED` or a precise failure. This is the executable
  definition of "done" for the stateless rework.

(`tests/test_*.py` are harnesses too — pytest is a ready-made one for pure
functions. The two above are custom because they need live API setup/teardown.)

### The recipe (reusable on any project)

A good harness has the same five parts every time:
1. **Isolated environment** — throwaway resources, set up and torn down each run;
   never the production system.
2. **Fixed stimulus** — a dataset or scenario sequence that is the same every
   run, so results are comparable.
3. **Automated judgment** — assertions (correctness) or a score (quality), read
   from the **real internal signal** where possible, not just the surface
   output.
4. **Noise handling** — repeats/averaging, retries, or polling to convergence,
   so a flaky run doesn't read as a real failure.
5. **Reusability** — a registry or parameter so the next variant is one line to
   add.

### When to reach for one

When you're about to **argue** (which design is better?), **guess** (will it
scale / time out?), or **eyeball** (does it still work after this change?) — that
is exactly when to spend 30–60 minutes building a harness instead. It pays for
itself the second time you run it, and it becomes the evidence behind every claim
you later make in a review.

