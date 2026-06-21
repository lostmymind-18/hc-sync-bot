# helpcenter-sync-bot

Scrapes the OptiSigns public Help Center (Zendesk API), converts each article to
clean Markdown, and loads them into an OpenAI Assistant's Vector Store **via the
API** — as a daily job that uploads only the delta.

## Setup

```bash
cp .env.sample .env        # OPENAI_API_KEY, OPENAI_VECTOR_STORE_ID (+ OPENAI_ASSISTANT_ID for the manual step)
pip install -r requirements.txt
```

**One-time manual step:** in the OpenAI Playground, create an Assistant, paste
`docs/system_prompt.txt` **verbatim** into Instructions, enable File Search,
create an empty Vector Store, attach it, and copy the IDs into `.env`.

## Run

```bash
python main.py
# or:
docker build -t helpcenter-sync-bot .
docker run --env-file .env helpcenter-sync-bot
```

The container is **one-shot**: it runs `main.py` once and exits 0 on success
(non-zero on failure, so a bad scheduled run shows up as failed). No internal
loop — the daily cadence is the scheduler's job. Required env at runtime:
`OPENAI_API_KEY` and `OPENAI_VECTOR_STORE_ID` (the sync never queries the
Assistant, so `OPENAI_ASSISTANT_ID` is only for the manual setup above).

Final log line (greppable), reporting the delta, what was uploaded, and chunks:

```
Run complete: added=0 updated=0 skipped=402 files_uploaded=0 chunks_embedded=0 total_live_articles=402 | vector_store files: completed=402 in_progress=0 failed=0 total=402
```

## Delta detection (stateless, idempotent)

Each article's Zendesk `updated_at` is compared against the value last stored
for it: new → upload; newer → upload then remove the old file (no stale
duplicates); unchanged → skip. The "last stored" value is **reconstructed from
the vector store itself every run** (each file carries `{article_id,
updated_at, url}` as OpenAI file *attributes*), not from a local file — so the
job stays idempotent even on the deployed **ephemeral** container, where a
`state.json` would be wiped and cause a full daily re-upload. Reconstruction also
reconciles duplicates to one-per-article (self-healing). `data/state.json` is a
derived debug artifact only. Full rationale + idempotency argument:
[`docs/stateless-delta-design.md`](docs/stateless-delta-design.md).

Two runs with no change both give `added=0 updated=0 skipped=402` — so does a
run after `rm data/state.json` (simulating the ephemeral container). Acceptance
test: `python eval/verify_stateless.py`.

## Chunking strategy

Static chunking, `max_chunk_size_tokens=800`, `chunk_overlap_tokens=400`. Help
Center articles are short and step-by-step; 800 tokens keeps a full instructional
step intact, 400-token overlap prevents loss at boundaries. **Validated, not
guessed**: `eval/chunking_eval.py` scores strategies on what `file_search`
actually retrieves — for articles this short, chunk sizing isn't the bottleneck
(retrieval-hit 100%, deep-fact coverage identical across strategies), so we keep
the explicit static choice the spec asks for.

## Daily job & logs

A **DigitalOcean App Platform Scheduled Job** (app `opti-bot`, component
`hc-sync-bot`, cron `0 6 * * *` UTC), built from this Docker image.

Logs: DO dashboard → `opti-bot` → **Activity → Jobs** → a run → **Runtime Logs**.
A captured successful run is in [`docs/do_logs.png`](docs/do_logs.png).

## Playground sanity check

Ask **"How do I add a YouTube video?"** — the answer is grounded in the docs and
its citation resolves to the source `support.optisigns.com` article URL. (We set
each file's name to its article URL so `file_search` annotations carry the real
URL.)

![Playground screenshot](docs/screenshot_playground.png)

## Design notes

- [`docs/engineering-journey.md`](docs/engineering-journey.md) — **start here**:
  the full story — symptoms, experiments, evidence, dead ends, the production
  incident, and the lessons.
- [`docs/decisions.md`](docs/decisions.md) · [`docs/stateless-delta-design.md`](docs/stateless-delta-design.md)
  · [`docs/lessons-learned.md`](docs/lessons-learned.md) · [`docs/zendesk_api_notes.md`](docs/zendesk_api_notes.md)
