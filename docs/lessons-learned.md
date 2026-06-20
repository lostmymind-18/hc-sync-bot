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
