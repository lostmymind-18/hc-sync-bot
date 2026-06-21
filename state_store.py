"""
Pure delta logic: diff live articles against prior state into
added / updated / skipped, plus a small helper to persist state to disk.

No network calls — diff_articles() is fully testable with hand-built fixtures.
Note: the prior state is reconstructed from the vector store at runtime (see
vector_store_client.reconstruct_state_from_store); data/state.json is only a
derived debug artifact, not the source of truth. See
docs/stateless-delta-design.md.
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
