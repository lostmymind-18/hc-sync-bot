# Zendesk Help Center API — Verified Live Notes

**Last verified:** 2026-06-19 against support.optisigns.com

## Endpoint behaviour (confirmed live)

```
GET https://support.optisigns.com/api/v2/help_center/articles.json
```

- Returns **HTTP 301** redirecting to the locale-scoped URL:
  `https://support.optisigns.com/api/v2/help_center/en-us/articles.json`
- **No auth required** — all 402 published articles are public.
- Only locale present: `en-us` (no multi-locale complexity).
- **Total articles: 402** across 5 pages (page[size]=100).

## Cursor pagination (confirmed)

Use `page[size]=100` and follow `links.next` while `meta.has_more == true`.

Response envelope:
```json
{
  "meta": {
    "has_more": true,
    "after_cursor": "...",
    "before_cursor": "..."
  },
  "links": {
    "first": "...",
    "next": "https://support.optisigns.com/api/v2/help_center/en-us/articles?page[after]=...&page[size]=100",
    "last": "..."
  },
  "articles": [...]
}
```

**Important**: `next_page` is always `null` at the root level — ignore it.
The correct pagination field is `links.next` (combined with `meta.has_more`).

## Article object — fields confirmed live

```json
{
  "id": 52523606879251,
  "title": "OptiSigns Digital Signage App for Zoom — Adding, Using...",
  "body": "<h2>...</h2><p>...</p>",
  "html_url": "https://support.optisigns.com/hc/en-us/articles/52523606879251-...",
  "updated_at": "2026-06-11T22:24:43Z",
  "section_id": 26324330971411,
  "locale": "en-us"
}
```

- `body` is pre-rendered article HTML, already stripped of site chrome.
- `html_url` is the canonical link used for "Article URL:" citations.
- `updated_at` drives delta detection in state_store.py.

## Rate limits

Standard Zendesk per-minute limits. Basic exponential backoff on 429s is
sufficient at this scale (402 articles, daily run).

## Incremental endpoint (not used by primary pipeline)

```
GET https://support.optisigns.com/api/v2/help_center/incremental/articles.json?start_time={epoch}
```

Available but not needed — the primary delta strategy compares each article's
`updated_at` against the value stored on its vector-store file (reconstructed
from the store each run; see docs/stateless-delta-design.md).
