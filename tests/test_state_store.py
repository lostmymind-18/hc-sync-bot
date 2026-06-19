"""
Bonus tests (+5 pts). state_store.diff_articles() is a pure function —
no mocking required, just hand-built Article fixtures.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from scraper import Article
from state_store import ChangeType, diff_articles, update_state_entry


def make_article(id=1001, title="Test Article", updated_at="2025-01-01T00:00:00Z"):
    return Article(
        id=id,
        title=title,
        body_html="<p>body</p>",
        html_url=f"https://support.optisigns.com/hc/en-us/articles/{id}",
        updated_at=updated_at,
        section_id=42,
    )


# ── diff_articles tests ──────────────────────────────────────────────────────

def test_new_article_is_added():
    article = make_article(id=1)
    diffs = diff_articles([article], state={})
    assert len(diffs) == 1
    assert diffs[0].change_type == ChangeType.ADDED
    assert diffs[0].article is article


def test_unchanged_article_is_skipped():
    article = make_article(id=2, updated_at="2025-06-01T00:00:00Z")
    state = {
        "2": {
            "updated_at": "2025-06-01T00:00:00Z",
            "openai_file_id": "file_old",
            "vector_store_file_id": "vsf_old",
        }
    }
    diffs = diff_articles([article], state)
    assert len(diffs) == 1
    assert diffs[0].change_type == ChangeType.SKIPPED


def test_changed_article_is_updated():
    article = make_article(id=3, updated_at="2025-07-01T00:00:00Z")
    state = {
        "3": {
            "updated_at": "2025-06-01T00:00:00Z",  # older
            "openai_file_id": "file_stale",
            "vector_store_file_id": "vsf_stale",
        }
    }
    diffs = diff_articles([article], state)
    assert len(diffs) == 1
    d = diffs[0]
    assert d.change_type == ChangeType.UPDATED
    assert d.stale_openai_file_id == "file_stale"
    assert d.stale_vector_store_file_id == "vsf_stale"


def test_empty_state_first_run():
    articles = [make_article(id=i) for i in range(5)]
    diffs = diff_articles(articles, state={})
    assert len(diffs) == 5
    assert all(d.change_type == ChangeType.ADDED for d in diffs)


def test_diff_is_pure_and_does_not_mutate_state():
    article = make_article(id=10)
    state = {}
    original_state = dict(state)
    diff_articles([article], state)
    assert state == original_state, "diff_articles must not mutate state"


def test_mixed_articles():
    articles = [
        make_article(id=100, updated_at="2025-01-01T00:00:00Z"),  # new
        make_article(id=200, updated_at="2025-03-01T00:00:00Z"),  # updated
        make_article(id=300, updated_at="2025-05-01T00:00:00Z"),  # skipped
    ]
    state = {
        "200": {"updated_at": "2025-02-01T00:00:00Z", "openai_file_id": "f1", "vector_store_file_id": "v1"},
        "300": {"updated_at": "2025-05-01T00:00:00Z", "openai_file_id": "f2", "vector_store_file_id": "v2"},
    }
    diffs = diff_articles(articles, state)
    by_id = {d.article.id: d for d in diffs}
    assert by_id[100].change_type == ChangeType.ADDED
    assert by_id[200].change_type == ChangeType.UPDATED
    assert by_id[300].change_type == ChangeType.SKIPPED


# ── update_state_entry tests ─────────────────────────────────────────────────

def test_update_state_entry_writes_correct_fields():
    state = {}
    article = make_article(id=999, title="Hello World", updated_at="2026-01-01T00:00:00Z")
    update_state_entry(state, article, "file_abc", "vsf_xyz")
    entry = state["999"]
    assert entry["title"] == "Hello World"
    assert entry["slug"] == "hello-world"
    assert entry["openai_file_id"] == "file_abc"
    assert entry["vector_store_file_id"] == "vsf_xyz"
    assert entry["updated_at"] == "2026-01-01T00:00:00Z"


def test_update_state_entry_overwrites_existing():
    state = {
        "999": {
            "title": "Old Title",
            "openai_file_id": "old_file",
            "vector_store_file_id": "old_vsf",
            "updated_at": "2025-01-01T00:00:00Z",
        }
    }
    article = make_article(id=999, title="New Title", updated_at="2026-06-01T00:00:00Z")
    update_state_entry(state, article, "new_file", "new_vsf")
    entry = state["999"]
    assert entry["title"] == "New Title"
    assert entry["openai_file_id"] == "new_file"
    assert entry["vector_store_file_id"] == "new_vsf"
