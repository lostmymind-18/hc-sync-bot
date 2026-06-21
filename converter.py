"""
Converts a single Article (see scraper.py) into clean Markdown with a small
YAML frontmatter header, and derives a filesystem-safe slug from its title.

No network calls here — pure transformation.
"""

import re

import markdownify

from scraper import Article


def slugify(title: str) -> str:
    """
    "How do I add a YouTube video?" -> "how-do-i-add-a-youtube-video"

    Lowercase, ASCII-only, hyphenated, strip punctuation. Collapses multiple
    hyphens and strips leading/trailing hyphens.
    """
    slug = title.lower()
    # Replace non-alphanumeric chars (except hyphens) with hyphens
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    # Collapse repeated hyphens and strip surrounding ones
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "untitled"


def html_to_markdown(body_html: str) -> str:
    """
    Convert article body HTML to clean Markdown.

    markdownify handles: headings (h1-h6 → #-######), code blocks
    (<pre><code> → fenced ```), links (relative and absolute), lists.
    The Zendesk `body` field is already stripped of nav/sidebar chrome,
    so we don't need to strip anything further — verified by eyeball on
    real articles.

    Options chosen:
    - heading_style=ATX: use # syntax instead of underline-style headings
    - bullets="-": consistent "-" list markers
    - strip=["script", "style"]: drop any stray script/style tags
    Excess blank lines are then collapsed to keep the Markdown tidy.
    """
    md = markdownify.markdownify(
        body_html,
        heading_style=markdownify.ATX,
        bullets="-",
        strip=["script", "style"],
    )
    # Collapse excessive blank lines (markdownify can produce 3+ blank lines)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def build_markdown_file(article: Article) -> tuple[str, str]:
    """
    Returns (filename, file_contents) for a single article.

    Frontmatter fields:
    - article_id: numeric id for delta-tracking traceability
    - title: human-readable title
    - url: canonical article URL (the source citation for this doc)
    - updated_at: timestamp (mirrors state.json for manual debugging)

    The article URL is also written as a plain "Article URL:" line at the top
    of the body so it travels with the content into the vector store. (Note:
    the Assistant's file_search tool cites sources via its own annotation
    system rather than echoing this line verbatim — see README — but keeping
    the URL in-document aids traceability and bare-context use.)
    """
    slug = slugify(article.title)
    filename = f"{slug}.md"

    # Escape double-quotes in title for YAML safety
    safe_title = article.title.replace('"', '\\"')

    frontmatter = (
        "---\n"
        f'article_id: {article.id}\n'
        f'title: "{safe_title}"\n'
        f'url: {article.html_url}\n'
        f'updated_at: {article.updated_at}\n'
        "---\n\n"
    )

    body = html_to_markdown(article.body_html)
    url_line = f"Article URL: {article.html_url}"
    contents = frontmatter + url_line + "\n\n" + body + "\n"
    return filename, contents
