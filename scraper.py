"""
Zendesk Help Center API client.

Responsible only for fetching article data as plain Python objects — no
Markdown conversion, no OpenAI calls, no file I/O beyond the network call
itself. Keep this module a thin, testable wrapper around the API.

Verified live against support.optisigns.com 2026-06-19:
- Endpoint 301-redirects to locale-scoped /en-us/ variant automatically.
- No auth required; 402 published articles in en-us.
- Cursor pagination via links.next / meta.has_more (next_page is always null).
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)

# Locale-scoped path; the non-locale URL 301-redirects here (confirmed live).
ARTICLES_PATH = "/api/v2/help_center/en-us/articles.json"
PAGE_SIZE = 100


@dataclass
class Article:
    id: int
    title: str
    body_html: str
    html_url: str
    updated_at: str  # ISO 8601, e.g. "2026-06-11T22:24:43Z"
    section_id: Optional[int] = None


class ZendeskRateLimitError(Exception):
    pass


@retry(
    retry=retry_if_exception_type(ZendeskRateLimitError),
    wait=wait_exponential(multiplier=1, min=5, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _get_page(url: str) -> dict:
    resp = requests.get(url, timeout=30)
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", 10))
        logger.warning("Rate limited by Zendesk; waiting %ds", retry_after)
        time.sleep(retry_after)
        raise ZendeskRateLimitError("429 rate limit")
    resp.raise_for_status()
    return resp.json()


def fetch_all_articles(subdomain: str) -> list[Article]:
    """
    Paginate through the Help Center API and return every published article.

    Uses the locale-scoped en-us endpoint directly (the non-locale URL
    returns a 301 redirect to en-us, confirmed live). Cursor pagination via
    links.next + meta.has_more.
    """
    url = f"https://{subdomain}{ARTICLES_PATH}?page%5Bsize%5D={PAGE_SIZE}"
    articles: list[Article] = []

    while url:
        data = _get_page(url)
        for a in data.get("articles", []):
            articles.append(Article(
                id=a["id"],
                title=a["title"],
                body_html=a.get("body", ""),
                html_url=a["html_url"],
                updated_at=a["updated_at"],
                section_id=a.get("section_id"),
            ))
        meta = data.get("meta", {})
        links = data.get("links", {})
        url = links.get("next") if meta.get("has_more") else None
        logger.info("Fetched %d articles so far…", len(articles))

    logger.info("Total articles fetched: %d", len(articles))
    return articles
