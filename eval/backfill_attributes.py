"""
One-off migration: backfill {article_id, updated_at, url} attributes onto the
vector-store files that were uploaded before attributes existed.

Without this, the first run of the stateless job would see attribute-less files,
treat them all as needing an update, and re-upload the whole corpus (slow). This
sets the attributes in place via files.update — no re-embedding — so the next
run reconstructs cleanly and reports skipped=N.

Idempotent: files that already carry an article_id attribute are left alone.

    python eval/backfill_attributes.py
"""

import pathlib
import re
import sys

from dotenv import load_dotenv
from openai import OpenAI

ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from config import load_config            # noqa: E402
from scraper import fetch_all_articles     # noqa: E402

ARTICLE_ID_RE = re.compile(r"/articles/(\d+)")


def run():
    load_dotenv()
    cfg = load_config()
    client = OpenAI(api_key=cfg.openai_api_key)
    vs_id = cfg.openai_vector_store_id

    live = {a.id: a for a in fetch_all_articles(cfg.zendesk_subdomain)}
    print(f"{len(live)} live articles")

    done = skipped = orphan = 0
    after = None
    while True:
        page = client.vector_stores.files.list(vector_store_id=vs_id, limit=100, after=after)
        for f in page.data:
            if (f.attributes or {}).get("article_id"):
                skipped += 1
                continue
            fname = client.files.retrieve(f.id).filename or ""
            m = ARTICLE_ID_RE.search(fname)
            if not m:
                print(f"  ! unparseable filename for {f.id}: {fname!r}")
                orphan += 1
                continue
            aid = int(m.group(1))
            article = live.get(aid)
            if not article:
                print(f"  ! {aid} not in live set (orphan), leaving as-is")
                orphan += 1
                continue
            client.vector_stores.files.update(
                vector_store_id=vs_id, file_id=f.id,
                attributes={
                    "article_id": str(aid),
                    "updated_at": article.updated_at,
                    "url": article.html_url,
                },
            )
            done += 1
        if not page.has_more:
            break
        after = page.data[-1].id

    print(f"backfilled={done} already_had={skipped} orphan/unparseable={orphan}")


if __name__ == "__main__":
    run()
