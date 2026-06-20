"""
Owns data/state.json — the single source of truth mapping article_id to
{title, slug, url, updated_at, openai_file_id, vector_store_file_id}.

This module is pure logic (no network calls), making it ideal for unit tests
— diff_articles() is fully testable with hand-built fixtures.
"""

import json
import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from scraper import Article

logger = logging.getLogger(__name__)


class ChangeType(str, Enum):
    ADDED = "added"
    UPDATED = "updated"
    SKIPPED = "skipped"


@dataclass
class ArticleDiff:
    article: Article
    change_type: ChangeType
    stale_openai_file_id: Optional[str] = None
    stale_vector_store_file_id: Optional[str] = None


def load_state(state_file_path: str) -> dict:
    """
    Load data/state.json. Returns {} on first run (file not present yet).
    """
    if not os.path.exists(state_file_path):
        logger.info("No state file found at %s — first run", state_file_path)
        return {}
    with open(state_file_path, encoding="utf-8") as f:
        return json.load(f)


def save_state(state_file_path: str, state: dict) -> None:
    """Persist state dict to disk as pretty-printed JSON."""
    os.makedirs(os.path.dirname(state_file_path) or ".", exist_ok=True)
    with open(state_file_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    logger.debug("State saved to %s (%d entries)", state_file_path, len(state))


def diff_articles(live_articles: list, state: dict) -> list:
    """
    Core delta logic. Pure function — no I/O, fully deterministic.

    Returns a list[ArticleDiff] where each entry is one of:
    - ADDED: article id not in state
    - UPDATED: article id in state but updated_at is newer
    - SKIPPED: article id in state and updated_at unchanged
    """
    diffs = []
    for article in live_articles:
        key = str(article.id)
        if key not in state:
            diffs.append(ArticleDiff(article=article, change_type=ChangeType.ADDED))
        else:
            stored = state[key]
            # `or ""` guards reconstructed entries whose updated_at is None
            # (a pre-migration, attribute-less file): treat as needing an
            # update so the next run re-uploads it and backfills attributes.
            if article.updated_at > (stored.get("updated_at") or ""):
                diffs.append(ArticleDiff(
                    article=article,
                    change_type=ChangeType.UPDATED,
                    stale_openai_file_id=stored.get("openai_file_id"),
                    stale_vector_store_file_id=stored.get("vector_store_file_id"),
                ))
            else:
                diffs.append(ArticleDiff(article=article, change_type=ChangeType.SKIPPED))
    return diffs


def update_state_entry(
    state: dict,
    article: Article,
    openai_file_id: str,
    vector_store_file_id: str,
) -> None:
    """
    Mutates state in place: upserts the entry for article.id with new file ids.
    """
    from converter import slugify
    state[str(article.id)] = {
        "title": article.title,
        "slug": slugify(article.title),
        "url": article.html_url,
        "updated_at": article.updated_at,
        "openai_file_id": openai_file_id,
        "vector_store_file_id": vector_store_file_id,
    }
