# Design: stateless delta detection (reconstruct state from the vector store)

## Problem

Delta detection (`added` / `updated` / `skipped`) was tracked in a local file,
`data/state.json`, keyed by article id:

```json
{ "<id>": { "updated_at": "...", "openai_file_id": "...", "vector_store_file_id": "..." } }
```

This is correct **locally**, where the filesystem persists between runs. It is
**broken in production**. The job is deployed as a DigitalOcean App Platform
*Scheduled Job*, whose container is **ephemeral** — there is no persistent
volume, so anything written to `data/` is discarded when the container exits.

Consequence, every scheduled run:

1. `load_state()` finds no file → returns `{}`.
2. The diff therefore classifies **all** live articles as `added`.
3. The job re-uploads the entire corpus. OpenAI does **not** deduplicate by
   filename — each upload is a new file object — so the vector store grows by
   the full corpus every run: 402 → 804 → 1206 → …

So the daily job both **violates the "upload only the delta" requirement** and
**pollutes retrieval** with stale duplicates. The local idempotency self-check
(`second run = skipped=N`) only passed because `state.json` happened to survive
on the local disk; it never exercised the deployed environment.

## Goal

Delta detection must be correct and idempotent **in the deployed environment**,
where there is no durable local filesystem — without standing up extra
infrastructure just to hold a few hundred timestamps.

## What "idempotent" means here

Idempotent = running the job any number of times leaves the system in the same
state as running it **once**, for the same inputs. The fixed point we want is:

> the vector store holds **exactly one current file per live article — and
> nothing else.**

This matters because a daily scheduled job runs forever; "safe to run
repeatedly" is the property that makes that acceptable. Formally, we want a run
to be a function whose output depends **only on its inputs** — `(live Zendesk
corpus, current vector-store contents)` — so that `f(f(x)) = f(x)`:

- a second run with no upstream change must perform **no mutation**
  (`added=0, updated=0, skipped=N`); and
- a partially-failed or duplicated store must **converge back** to the fixed
  point rather than drift further from it.

Note what idempotency is **not**: it is not "the output is always identical"
(the counts legitimately differ when articles actually change). It is that the
*resulting store state* is a function of the inputs alone, independent of how
many times — or with what crash / partial-run history — the job has run.

The local-`state.json` design is only **conditionally** idempotent: it reaches
the fixed point only while `state.json` both *survives* and *stays in sync* with
the store. Lose persistence (ephemeral container) or sync (any out-of-band
change) and repeated runs **diverge** — each one appends another full copy of
the corpus. That is precisely the production failure above.

Option 1 is **unconditionally** idempotent because every run derives its notion
of "what already exists" from the store itself, then drives the store toward the
fixed point:

- no upstream change → reconstructed prior state `==` live corpus → every
  article is SKIPPED → **zero writes**;
- a duplicated or partially-written store → reconciliation collapses the extra
  copies and fills any gaps → **one** run reaches the fixed point, and the run
  after it is a no-op.

Because "what already exists" is read from live truth on every run rather than
trusted from a persisted file, there is no state that can survive incorrectly
across runs — so convergence does not depend on the environment keeping a disk.

## Options considered

1. **Reconstruct state from the vector store at the start of each run.** The
   vector store already knows which articles are uploaded; store the small
   amount of extra metadata we need (`article_id`, `updated_at`) as OpenAI file
   **attributes**, and rebuild the prior-run state from a single list call.
2. **External durable state store** (DO Spaces / S3 / a small DB). Load
   `state.json` at start, save at end.
3. **Accept the limitation.** Document it and pause the schedule, or live with
   daily duplication.

## Decision: Option 1

The vector store **is** the source of truth for "what is currently uploaded."
Deriving state from it — rather than mirroring it in a second place — is the
right call for four reasons:

- **No dual source of truth, no drift.** A separate `state.json` (local or in
  Spaces) can disagree with reality. We hit exactly this bug during development:
  clearing/uploading files outside `main.py` left `state.json` describing files
  that no longer existed. Reconstructing from the live store makes that class of
  bug impossible — the prior state is, by construction, the truth.
- **Self-healing.** Any out-of-band change — a manual delete, a crashed partial
  run, or *the duplication bug above* — is reconciled on the next run, because
  state is recomputed from what actually exists, not trusted from a stale file.
  The reconstruction step can collapse duplicate files for the same article down
  to one, so the job repairs a polluted store instead of compounding it.
- **No new infrastructure or failure modes.** Option 2 adds a network store,
  credentials, and its own consistency concerns — all to cache data OpenAI
  already holds. That is moving parts for negative value here.
- **Genuinely stateless container.** `docker run -e OPENAI_API_KEY=… <image>`
  stays a pure function of (live Zendesk, vector store) → exits 0, with no
  hidden dependence on a persisted file. That is the scheduled-job ideal and
  what the spec's "runs once and exits" intent implies.

### Trade-offs accepted

- **One extra paginated list call** at startup (list the vector store's files +
  their attributes). Cheap, and it replaces a local read.
- **`updated_at` must live somewhere readable.** `filename = <article URL>`
  already encodes article *identity*, but not whether content changed. We store
  `article_id` and `updated_at` in the file's `attributes` so the
  updated-vs-skipped comparison can be reconstructed without a local store.
  (OpenAI file attributes comfortably fit a few short string keys.)

## How it works

1. **Reconstruct prior state.** List every file attached to the vector store
   (paginated). Each carries `attributes = {article_id, updated_at, url}` plus
   its vector-store-file id and underlying file id. Build:

   ```
   prior[article_id] = { updated_at, file_id, vector_store_file_id, url }
   ```

   This replaces `load_state(state.json)` as the authoritative prior state.

2. **Diff** the live Zendesk articles against `prior` — unchanged pure logic in
   `state_store.diff_articles`:
   - id not in `prior` → **ADDED**
   - live `updated_at` newer than `prior[id].updated_at` → **UPDATED**
   - otherwise → **SKIPPED**

3. **Apply.** On ADDED/UPDATED, upload the new file (`filename = url`) and attach
   it to the vector store **with `attributes`** set. On UPDATED, remove the old
   vector-store file and delete the old file object afterward (unchanged
   replacement logic).

4. **Reconcile duplicates.** If reconstruction finds more than one live file for
   the same `article_id` (residue from a previous stateless run before this fix),
   keep the newest and remove the rest — the job heals a polluted store.

5. `data/state.json` is still written at the end as a convenience for local
   debugging, but it is **no longer the source of truth** — it is derived
   output, not input.

## Consequences

- After deploy, a scheduled run reconstructs `prior` from the vector store
  (already 402 files) → diff → all SKIPPED → logs
  `added=0 updated=0 skipped=402`, with no duplication. Idempotent in
  production, not just locally.
- The first run after this change, if the store contains pre-fix duplicates,
  reconciles them down to one-per-article.

## Affected modules

- `vector_store_client.py` — set `attributes` on attach; add
  `reconstruct_state_from_store(client, vs_id)` (paginated list → `prior` dict)
  and duplicate reconciliation.
- `state_store.py` — `diff_articles` is unchanged (it already takes a state
  dict); it is simply fed the reconstructed `prior`.
- `main.py` — build `prior` from the vector store instead of `load_state()`;
  keep `save_state()` as derived output.

## Testing

- Unit: `diff_articles` stays pure (existing tests hold); add tests for
  attribute parsing and duplicate reconciliation.
- Integration: run `main.py` twice against the live store — second run reports
  `skipped=402` and total file count is unchanged. This self-check is now valid
  for the deployed environment, not only locally.
