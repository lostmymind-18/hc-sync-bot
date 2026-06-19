# helpcenter-sync-bot

Scrapes the OptiSigns public Help Center (Zendesk API), converts articles to
Markdown, and loads them into an OpenAI Assistant's Vector Store via the API,
with a daily delta-only update job.

## Setup

```bash
cp .env.sample .env   # fill in OPENAI_API_KEY, OPENAI_ASSISTANT_ID, OPENAI_VECTOR_STORE_ID
pip install -r requirements.txt
```

**One-time manual step:** Create the Assistant in the OpenAI Playground,
paste the system prompt from `docs/system_prompt.txt` verbatim into
Instructions, enable File Search, create an empty Vector Store named
`optibot-docs`, attach it to the Assistant, and copy both IDs into `.env`.

## Run locally

```bash
python main.py
```

Or via Docker:

```bash
docker build -t helpcenter-sync-bot .
docker run --env-file .env helpcenter-sync-bot
```

Expected final log line:
`Run complete: added=X updated=Y skipped=Z total_live_articles=402 | vector_store files: completed=N ...`

## Delta detection

Each article's Zendesk `updated_at` timestamp is compared against the value
stored in `data/state.json` from the previous run. New articles → uploaded
and recorded. Newer `updated_at` → new file uploaded; the old vector store
file is removed and the old file object deleted before the next run, so the
vector store never accumulates stale duplicate content. Unchanged → skipped.
Running the job twice with no upstream changes produces `added=0 updated=0 skipped=402`.

## Chunking strategy

`max_chunk_size_tokens=800`, `chunk_overlap_tokens=400` (static chunking).
Help Center articles are short and single-topic with step-by-step
instructions; 800 tokens keeps a full instructional step intact rather than
splitting mid-procedure. 400-token overlap prevents context loss at chunk
boundaries. These values were chosen after inspecting real article lengths
(median ~300–600 tokens body text).

## Daily job

Deployed as a DigitalOcean App Platform Scheduled Job (daily cron) built
from this Docker image. Logs link: [TODO: add DO job logs URL after deploy]

## Playground sanity check

After upload, ask the Assistant **"How do I add a YouTube video?"** in the
Playground and confirm the answer includes `Article URL:` citation lines.

![Playground screenshot](docs/screenshot_playground.png)
