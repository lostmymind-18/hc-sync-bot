"""
Entrypoint. Wires scraper -> converter -> state_store diff -> vector_store_client
upload, then prints a summary log line. Runs once and exits — no internal
scheduling loop (the daily cadence is handled externally by the deployment
platform; see Dockerfile + README).

Exit code 0 on success. Non-zero on any unhandled failure, so a scheduled
run that fails is visible as a failed job, not a silent no-op.
"""

import logging
import pathlib
import sys

from config import load_config
from converter import build_markdown_file
from openai import OpenAI
from scraper import fetch_all_articles
from state_store import ChangeType, diff_articles, save_state
from vector_store_client import (
    attach_files_batch,
    get_vector_store_stats,
    reconstruct_state_from_store,
    remove_stale_file,
    upload_markdown_file,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


def sync(
    client: OpenAI,
    vector_store_id: str,
    live_articles: list,
    chunk_size_tokens: int,
    chunk_overlap_tokens: int,
    markdown_dir: str,
) -> dict:
    """
    One delta sync of the vector store against the live articles. Stateless:
    the prior state is rebuilt from the vector store itself, so the job is
    idempotent even with no local state.json (the DigitalOcean ephemeral
    container case). See docs/stateless-delta-design.md.

    Returns {"added", "updated", "skipped"}.
    """
    out_dir = pathlib.Path(markdown_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Rebuild prior state from the store; collect any duplicate copies.
    prior, duplicate_file_ids = reconstruct_state_from_store(client, vector_store_id)

    # 2. Reconcile: drop duplicate copies so the store has one file per article.
    for fid in duplicate_file_ids:
        remove_stale_file(client, vector_store_id, fid, fid)
    if duplicate_file_ids:
        logger.info("Reconciled %d duplicate file(s) from the vector store",
                    len(duplicate_file_ids))

    # 3. Diff live articles against the reconstructed prior state.
    diffs = diff_articles(live_articles, prior)

    # 4. Convert + upload every changed article, collecting file ids so they can
    #    be attached in one batched (parallel-embedding) operation — this keeps
    #    even a large re-sync under the platform's job timeout, where attaching
    #    one-by-one (~12s/file) would blow past it.
    pending = []  # (file_id, attributes, diff)
    skipped = 0
    for d in diffs:
        if d.change_type == ChangeType.SKIPPED:
            skipped += 1
            continue
        filename, contents = build_markdown_file(d.article)
        md_path = out_dir / filename
        md_path.write_text(contents, encoding="utf-8")
        file_id = upload_markdown_file(client, str(md_path), d.article.html_url)
        attributes = {
            "article_id": str(d.article.id),
            "updated_at": d.article.updated_at,
            "url": d.article.html_url,
        }
        pending.append((file_id, attributes, d))

    attach_files_batch(
        client,
        vector_store_id,
        [(fid, attrs) for fid, attrs, _ in pending],
        chunk_size_tokens,
        chunk_overlap_tokens,
    )

    # 5. Count, and for UPDATED articles remove the stale file now that the new
    #    one is attached (so the article is never missing from retrieval).
    added = updated = 0
    for _file_id, _attrs, d in pending:
        if d.change_type == ChangeType.UPDATED:
            remove_stale_file(
                client,
                vector_store_id,
                d.stale_vector_store_file_id,
                d.stale_openai_file_id,
            )
            updated += 1
            logger.info("UPDATED: %s", d.article.title)
        else:
            added += 1
            logger.info("ADDED: %s", d.article.title)

    return {"added": added, "updated": updated, "skipped": skipped}


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

    # 2. Delta sync (stateless — prior state comes from the vector store)
    counts = sync(
        client,
        cfg.openai_vector_store_id,
        live_articles,
        cfg.chunk_size_tokens,
        cfg.chunk_overlap_tokens,
        cfg.markdown_output_dir,
    )

    # 3. Persist state.json as a derived debug artifact (NOT the source of
    #    truth — the vector store is). Rebuilt from the store post-sync.
    final_state, _ = reconstruct_state_from_store(client, cfg.openai_vector_store_id)
    save_state(cfg.state_file_path, final_state)

    # 4. Summary log — greppable format for DO job logs
    stats = get_vector_store_stats(client, cfg.openai_vector_store_id)
    fc = stats["file_counts"]
    print(
        f"Run complete: added={counts['added']} updated={counts['updated']} "
        f"skipped={counts['skipped']} total_live_articles={len(live_articles)} | "
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
