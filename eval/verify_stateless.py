"""
Integration acceptance test for stateless delta detection
(docs/stateless-delta-design.md).

Proves the job is idempotent WITHOUT any local state.json — i.e. it survives
DigitalOcean's ephemeral container — because every sync rebuilds its prior
state from the vector store itself. Run against a throwaway vector store seeded
with the 5 eval articles (fast, isolated; the production store is never
touched).

Every scenario asserts the P2 invariant — the whole design in one check:

    every live article maps to exactly ONE file in the store, and nothing else.

Scenarios:
  A  empty store, no local state          -> added=5,  P2
  B  run again (ephemeral: state rebuilt)  -> skipped=5, store still 5 (no dup)
  C  one stored updated_at forced older    -> updated=1, replaces not appends
  D  inject a duplicate file               -> reconcile self-heals back to 5

    python eval/verify_stateless.py

Exits non-zero on the first failed assertion.
"""

import collections
import pathlib
import sys
import tempfile
import time

from dotenv import load_dotenv
from openai import OpenAI

ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from config import load_config                       # noqa: E402
from scraper import fetch_all_articles                # noqa: E402
from main import sync                                 # noqa: E402  (implemented for this design)
from vector_store_client import attach_files_batch    # noqa: E402

TEST_IDS = {28598173096723, 48241081473043, 48626115821459, 52069065128723, 52412502456083}
CHUNK, OVERLAP = 800, 400


def list_files(client, vs_id):
    out, after = [], None
    while True:
        page = client.vector_stores.files.list(vector_store_id=vs_id, limit=100, after=after)
        out.extend(page.data)
        if not page.has_more:
            return out
        after = page.data[-1].id


def assert_p2(client, vs_id, articles, label, timeout=25):
    """
    P2 invariant: every live article maps to exactly one file, nothing extra.

    The vector store is eventually consistent — a list right after a sync that
    deleted/added files can briefly still show the old set. The fixed point is
    a *converged* state, so we poll until P2 holds and only fail if it never
    converges (which would be a real bug, not lag).
    """
    live = {str(a.id) for a in articles}
    last = None
    for _ in range(timeout):
        files = list_files(client, vs_id)
        groups = collections.defaultdict(list)
        for f in files:
            groups[(f.attributes or {}).get("article_id")].append(f.id)
        dups = {a: ids for a, ids in groups.items() if len(ids) != 1}
        if set(groups) == live and not dups and len(files) == len(live):
            print(f"   P2 OK [{label}]: {len(files)} files, exactly one per article")
            return
        last = f"ids={set(groups)} dups={dups} total={len(files)}"
        time.sleep(1)
    raise AssertionError(f"[{label}] P2 not reached within {timeout}s: {last} (live={live})")


def expect(label, counts, **kw):
    for k, v in kw.items():
        assert counts.get(k) == v, f"[{label}] expected {k}={v}, got {counts.get(k)} (full={counts})"
    print(f"   counts OK [{label}]: {counts}")


def wait_for_attr(client, vs_id, file_id, key, value, timeout=20):
    """
    Vector-store attribute writes are eventually consistent — a list/retrieve
    right after files.update() can still return the old value for a few
    seconds. This only matters when a test mutates an attribute and reads it
    back immediately; production never does (attributes are read on a later
    run). Poll until the change is visible so the test is deterministic.
    """
    for _ in range(timeout):
        for f in list_files(client, vs_id):
            if f.id == file_id and (f.attributes or {}).get(key) == value:
                return
        time.sleep(1)
    raise AssertionError(f"[setup] attribute {key}={value!r} did not propagate for {file_id}")


def run():
    load_dotenv()
    cfg = load_config()
    client = OpenAI(api_key=cfg.openai_api_key)

    articles = [a for a in fetch_all_articles(cfg.zendesk_subdomain) if a.id in TEST_IDS]
    assert len(articles) == 5, f"expected 5 test articles, got {len(articles)}"
    md_dir = tempfile.mkdtemp(prefix="verify_stateless_")

    vs = client.vector_stores.create(name="verify-stateless-temp")
    print("temp vector store:", vs.id)
    try:
        # A — empty store, no local state -> everything added
        print("\nScenario A: first sync against empty store")
        expect("A", sync(client, vs.id, articles, CHUNK, OVERLAP, md_dir), added=5, updated=0, skipped=0)
        assert_p2(client, vs.id, articles, "A")

        # B — run again with NO local state. This is the DO ephemeral case:
        # prior is rebuilt purely from the store, so nothing must be re-uploaded.
        print("\nScenario B: second sync (state rebuilt from store, no state.json)")
        expect("B", sync(client, vs.id, articles, CHUNK, OVERLAP, md_dir), added=0, updated=0, skipped=5)
        assert_p2(client, vs.id, articles, "B")

        # C — force one article's stored updated_at older -> UPDATED, replaced not appended
        print("\nScenario C: stale one file -> UPDATED replaces (no growth)")
        target = articles[0]
        tf = next(f for f in list_files(client, vs.id)
                  if (f.attributes or {}).get("article_id") == str(target.id))
        client.vector_stores.files.update(
            vector_store_id=vs.id, file_id=tf.id,
            attributes={"article_id": str(target.id), "updated_at": "2000-01-01T00:00:00Z", "url": target.html_url},
        )
        wait_for_attr(client, vs.id, tf.id, "updated_at", "2000-01-01T00:00:00Z")
        expect("C", sync(client, vs.id, articles, CHUNK, OVERLAP, md_dir), added=0, updated=1, skipped=4)
        assert_p2(client, vs.id, articles, "C")
        assert tf.id not in {f.id for f in list_files(client, vs.id)}, "[C] stale file was not removed"
        print("   stale file removed OK")

        # D — inject a duplicate file for one article -> reconcile self-heals
        print("\nScenario D: inject duplicate -> self-healing reconcile")
        dup = articles[1]
        extra = client.files.create(file=(dup.html_url, b"stale duplicate body\n", "text/markdown"), purpose="assistants")
        attach_files_batch(
            client, vs.id,
            [(extra.id, {"article_id": str(dup.id), "updated_at": "2010-01-01T00:00:00Z", "url": dup.html_url})],
            CHUNK, OVERLAP,
        )
        assert len(list_files(client, vs.id)) == 6, "[D] setup: expected 6 files after injecting duplicate"
        expect("D", sync(client, vs.id, articles, CHUNK, OVERLAP, md_dir), added=0, updated=0, skipped=5)
        assert_p2(client, vs.id, articles, "D")
        assert extra.id not in {f.id for f in list_files(client, vs.id)}, "[D] duplicate file was not reconciled away"
        print("   duplicate reconciled OK")

        print("\nALL SCENARIOS PASSED ✅")
    finally:
        try:
            leftover = [f.id for f in list_files(client, vs.id)]
        except Exception:
            leftover = []
        try:
            client.vector_stores.delete(vs.id)
        except Exception:
            pass
        for fid in leftover:
            try:
                client.files.delete(fid)
            except Exception:
                pass
        print("cleaned up temp vector store + files")


if __name__ == "__main__":
    run()
