"""
Unit tests for the pure state-reconstruction logic that lets the job rebuild
its delta state from the vector store itself — no local state.json — so it
stays idempotent on DigitalOcean's ephemeral container.
See docs/stateless-delta-design.md.

Covers vector_store_client._index_files: given the file records read back from
the vector store, it must
  (a) produce a state dict in the exact shape state_store.diff_articles
      consumes (keyed by str article id, with updated_at / openai_file_id /
      vector_store_file_id), keeping exactly ONE entry per article (newest
      updated_at wins), and
  (b) report the file_ids of any leftover duplicate copies to remove, so the
      store converges to one-file-per-article.

These are pure (no network) and run under pytest.
"""

from vector_store_client import _index_files


def rec(article_id, updated_at, file_id, url="https://support.optisigns.com/hc/en-us/articles/1-x"):
    return {
        "article_id": article_id,
        "updated_at": updated_at,
        "file_id": file_id,
        "url": url,
    }


def test_distinct_articles_no_duplicates():
    state, dups = _index_files([
        rec("1", "2026-01-01T00:00:00Z", "file-a"),
        rec("2", "2026-01-02T00:00:00Z", "file-b"),
    ])
    assert set(state) == {"1", "2"}
    assert dups == []
    # shape must match what state_store.diff_articles reads
    assert state["1"]["updated_at"] == "2026-01-01T00:00:00Z"
    assert state["1"]["openai_file_id"] == "file-a"
    assert state["1"]["vector_store_file_id"] == "file-a"


def test_duplicate_keeps_newest_reports_old():
    state, dups = _index_files([
        rec("1", "2026-01-01T00:00:00Z", "file-old"),
        rec("1", "2026-02-01T00:00:00Z", "file-new"),
    ])
    assert state["1"]["openai_file_id"] == "file-new"
    assert dups == ["file-old"]


def test_duplicate_order_independent():
    state, dups = _index_files([
        rec("1", "2026-02-01T00:00:00Z", "file-new"),
        rec("1", "2026-01-01T00:00:00Z", "file-old"),
    ])
    assert state["1"]["openai_file_id"] == "file-new"
    assert dups == ["file-old"]


def test_missing_updated_at_is_oldest():
    # a pre-migration file (no updated_at attribute) must lose to an attributed
    # one, and be reported for removal
    state, dups = _index_files([
        rec("1", None, "file-noattr"),
        rec("1", "2026-01-01T00:00:00Z", "file-attr"),
    ])
    assert state["1"]["openai_file_id"] == "file-attr"
    assert dups == ["file-noattr"]


def test_single_missing_updated_at_kept_for_backfill():
    # one attribute-less file with no duplicate: keep it (None signals the
    # caller to re-upload/backfill), do not drop it
    state, dups = _index_files([rec("1", None, "file-noattr")])
    assert "1" in state
    assert state["1"]["updated_at"] is None
    assert dups == []


def test_three_copies_keep_one_drop_two():
    state, dups = _index_files([
        rec("9", "2026-01-01T00:00:00Z", "file-1"),
        rec("9", "2026-03-01T00:00:00Z", "file-3"),
        rec("9", "2026-02-01T00:00:00Z", "file-2"),
    ])
    assert state["9"]["openai_file_id"] == "file-3"
    assert sorted(dups) == ["file-1", "file-2"]
