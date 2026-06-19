"""
Entrypoint. Wires scraper -> converter -> state_store diff -> vector_store_client
upload, then prints a summary log line. Runs once and exits — no internal
scheduling loop (the daily cadence is handled externally by the deployment
platform; see Dockerfile + README).

Exit code 0 on success. Non-zero on any unhandled failure, so a scheduled
run that fails is visible as a failed job, not a silent no-op.
"""

import logging
import os
import pathlib
import sys

from config import load_config
from converter import build_markdown_file
from openai import OpenAI
from scraper import fetch_all_articles
from state_store import (
    ChangeType,
    diff_articles,
    load_state,
    save_state,
    update_state_entry,
)
from vector_store_client import (
    attach_file_to_vector_store,
    get_vector_store_stats,
    remove_stale_file,
    upload_markdown_file,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


def run() -> int:
    cfg = load_config()
    client = OpenAI(api_key=cfg.openai_api_key)

    # 1. Fetch all live articles
    logger.info("Fetching articles from Zendesk Help Center…")
    live_articles = fetch_all_articles(cfg.zendesk_subdomain)
    if not live_articles:
        logger.error("No articles returned from Zendesk — aborting to avoid false skips")
        return 1
    logger.info("Fetched %d live articles", len(live_articles))

    # 2. Load previous run state
    state = load_state(cfg.state_file_path)

    # 3. Diff
    diffs = diff_articles(live_articles, state)

    # 4. Process each diff
    out_dir = pathlib.Path(cfg.markdown_output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    added = updated = skipped = 0

    for d in diffs:
        if d.change_type == ChangeType.SKIPPED:
            skipped += 1
            continue

        # Convert article to Markdown and write to disk
        filename, contents = build_markdown_file(d.article)
        md_path = out_dir / filename
        md_path.write_text(contents, encoding="utf-8")

        # Upload to OpenAI Files API
        file_id = upload_markdown_file(client, str(md_path))

        # Attach to Vector Store with static chunking
        vsf_id = attach_file_to_vector_store(
            client,
            cfg.openai_vector_store_id,
            file_id,
            cfg.chunk_size_tokens,
            cfg.chunk_overlap_tokens,
        )

        if d.change_type == ChangeType.UPDATED:
            # Remove stale file from vector store AFTER attaching new one
            # so the article is never missing from retrieval, even briefly.
            remove_stale_file(
                client,
                cfg.openai_vector_store_id,
                d.stale_vector_store_file_id,
                d.stale_openai_file_id,
            )
            updated += 1
            logger.info("UPDATED: %s", d.article.title)
        else:
            added += 1
            logger.info("ADDED: %s", d.article.title)

        update_state_entry(state, d.article, file_id, vsf_id)

    # 5. Persist updated state
    save_state(cfg.state_file_path, state)

    # 6. Fetch vector store stats for logging
    stats = get_vector_store_stats(client, cfg.openai_vector_store_id)
    fc = stats["file_counts"]

    # 7. Summary log — greppable format for DO job logs
    print(
        f"Run complete: added={added} updated={updated} skipped={skipped} "
        f"total_live_articles={len(live_articles)} | "
        f"vector_store files: completed={fc['completed']} "
        f"in_progress={fc['in_progress']} failed={fc['failed']} total={fc['total']}"
    )

    return 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except Exception as exc:
        logger.exception("Fatal error: %s", exc)
        sys.exit(1)
