"""
Chunking experimentation harness.

Goal: stop guessing which chunking strategy is best — measure it. For each
strategy we spin up a throwaway vector store + assistant, run a fixed
ground-truth Q&A set through it, score retrieval + answer quality, print a
comparison, then tear everything down. The production vector store
(OPENAI_VECTOR_STORE_ID) is never touched.

What makes this more than "eyeball the answer": we read the chunks that
file_search *actually retrieved* (via run steps + the `include` param), so we
can tell a retrieval failure (wrong/garbled chunk pulled) apart from a
generation failure (right chunk, bad answer).

A "strategy" = an optional pre-chunker (how WE split a doc into files before
upload) + the OpenAI chunking_strategy applied at attach time. Add a new entry
to STRATEGIES to try a new tactic; nothing else changes.

Usage:
    python eval/chunking_eval.py                      # default strategy set
    python eval/chunking_eval.py static_800_400 auto  # pick strategies
    python eval/chunking_eval.py --detail static_800_400
"""

import json
import os
import pathlib
import re
import sys

from dotenv import load_dotenv
from openai import OpenAI, NotFoundError

HERE = pathlib.Path(__file__).parent
ROOT = HERE.parent
MD_DIR = ROOT / "data" / "markdown"            # produced by `python main.py`
MD_FALLBACK = ROOT / "data" / "markdown_test5"  # optional scratch subset
SYSTEM_PROMPT = (ROOT / "docs" / "system_prompt.txt").read_text().strip()
MODEL = "gpt-4o"


# --------------------------------------------------------------------------
# Pre-chunkers: how WE split one article into upload units before OpenAI's
# own chunker runs. Each returns (article_url, [piece_text, ...]).
# --------------------------------------------------------------------------
def _parse_md(content: str):
    m = re.search(r"^url:\s*(\S+)", content, re.M)
    url = m.group(1) if m else ""
    parts = content.split("---", 2)
    body = (parts[2] if len(parts) >= 3 else content).strip()
    # drop the leading "Article URL:" line from body; we re-add per piece
    body = re.sub(r"^Article URL:.*\n+", "", body, count=1)
    return url, body


def whole_doc(content: str):
    """One upload unit = the whole article (as written to disk)."""
    url, _ = _parse_md(content)
    return url, [content]


def by_section(content: str):
    """
    One upload unit per top-level (#/##) section, URL stamped on each.

    The URL line goes at the END of the piece, not the start: OpenAI sniffs
    file content to pick a MIME type, and a piece beginning with
    "Article URL: <url>" gets misdetected as message/news and rejected.
    Starting with the section's own heading/prose avoids that.
    """
    url, body = _parse_md(content)
    url_line = f"Article URL: {url}"
    raw = re.split(r"(?=^#{1,2}\s)", body, flags=re.M)
    pieces = []
    for chunk in raw:
        chunk = chunk.strip()
        if chunk:
            pieces.append(f"{chunk}\n\n{url_line}\n")
    return url, pieces or [f"{body}\n\n{url_line}\n"]


# --------------------------------------------------------------------------
# Strategy registry: pre-chunker + OpenAI attach-time chunking_strategy.
# --------------------------------------------------------------------------
STRATEGIES = {
    "static_800_400": dict(  # current production setting
        pre=whole_doc,
        chunk={"type": "static", "static": {"max_chunk_size_tokens": 800, "chunk_overlap_tokens": 400}},
    ),
    "static_1600_200": dict(  # bigger chunks → less mid-procedure fragmentation
        pre=whole_doc,
        chunk={"type": "static", "static": {"max_chunk_size_tokens": 1600, "chunk_overlap_tokens": 200}},
    ),
    "auto": dict(  # let OpenAI decide
        pre=whole_doc,
        chunk={"type": "auto"},
    ),
    "section_static_800_0": dict(  # structure-aware: split on headings ourselves
        pre=by_section,
        chunk={"type": "static", "static": {"max_chunk_size_tokens": 800, "chunk_overlap_tokens": 0}},
    ),
}

DEFAULT_STRATEGIES = ["static_800_400", "auto", "section_static_800_0"]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _article_id(name: str):
    m = re.search(r"articles/(\d+)", name) or re.search(r"(\d{6,})", name)
    return int(m.group(1)) if m else None


def load_docs(dataset):
    """
    Load just the articles the eval set references, from the real output dir
    (`data/markdown`, populated by `python main.py`). Falls back to the
    `data/markdown_test5` scratch subset if the full output isn't present.
    """
    ids = {q["expected_article_id"] for q in dataset}
    src = MD_DIR if any(MD_DIR.glob("*.md")) else MD_FALLBACK
    docs = []
    for p in sorted(src.glob("*.md")):
        text = p.read_text(encoding="utf-8")
        m = re.search(r"^article_id:\s*(\d+)", text, re.M)
        if m and int(m.group(1)) in ids:
            docs.append(text)
    return docs


def build_vector_store(client: OpenAI, strategy: dict, docs):
    vs = client.vector_stores.create(name="chunk-eval-temp")
    file_ids = []
    for content in docs:
        url, pieces = strategy["pre"](content)
        for piece in pieces:
            f = client.files.create(
                file=(url, piece.encode("utf-8"), "text/markdown"),
                purpose="assistants",
            )
            file_ids.append(f.id)
            client.vector_stores.files.create_and_poll(
                vector_store_id=vs.id, file_id=f.id, chunking_strategy=strategy["chunk"]
            )
    return vs.id, file_ids


def build_assistant(client: OpenAI, vs_id: str):
    a = client.beta.assistants.create(
        model=MODEL,
        instructions=SYSTEM_PROMPT,
        tools=[{"type": "file_search"}],
        tool_resources={"file_search": {"vector_store_ids": [vs_id]}},
    )
    return a.id


def _run_once(client: OpenAI, asst_id: str, question: str):
    th = client.beta.threads.create()
    client.beta.threads.messages.create(thread_id=th.id, role="user", content=question)
    run = client.beta.threads.runs.create_and_poll(thread_id=th.id, assistant_id=asst_id)

    retrieved = []
    try:
        steps = client.beta.threads.runs.steps.list(
            thread_id=th.id, run_id=run.id,
            include=["step_details.tool_calls[*].file_search.results[*].content"],
        )
        for s in steps.data:
            det = s.step_details
            if getattr(det, "type", None) != "tool_calls":
                continue
            for tc in det.tool_calls:
                if getattr(tc, "type", None) == "file_search":
                    for r in (getattr(tc.file_search, "results", None) or []):
                        retrieved.append(_article_id(getattr(r, "file_name", "") or ""))
    except Exception:
        pass  # older API / no include support → fall back to annotations only

    msgs = client.beta.threads.messages.list(thread_id=th.id).data
    asst = next((m for m in msgs if m.role == "assistant"), None)
    answer, ann = "", []
    if asst:
        c = asst.content[0]
        answer = c.text.value
        for a in c.text.annotations:
            fc = getattr(a, "file_citation", None)
            if fc:
                ann.append(_article_id(client.files.retrieve(fc.file_id).filename))
    return dict(
        status=run.status,
        answer=answer,
        retrieved=[x for x in retrieved if x],
        annotations=[x for x in ann if x],
    )


def run_question(client: OpenAI, asst_id: str, question: str, max_attempts: int = 3):
    """
    A run occasionally completes with an empty assistant message (transient
    API/timing). Treat that as noise, not a strategy failure: retry until we
    get a non-empty answer from a completed run, up to max_attempts.
    """
    raw = None
    for _ in range(max_attempts):
        raw = _run_once(client, asst_id, question)
        if raw["status"] == "completed" and raw["answer"].strip():
            return raw
    return raw  # give back the last attempt even if still empty


def score(q, raw):
    ans = raw["answer"].lower()
    cov = sum(1 for k in q["must_include"] if k in ans) / len(q["must_include"])
    exp = q["expected_article_id"]
    bullets = len(re.findall(r"^\s{0,3}(?:\d+\.|[-*])\s", raw["answer"], re.M))
    return dict(
        coverage=cov,
        retrieval_hit=(exp in raw["retrieved"]) if raw["retrieved"] else None,
        citation_ok=exp in raw["annotations"],
        bullets=bullets,
        bullet_ok=bullets <= q["max_bullets"],
    )


def cleanup(client: OpenAI, vs_id, file_ids, asst_id):
    for fid in file_ids:
        try:
            client.files.delete(fid)
        except NotFoundError:
            pass
    try:
        client.vector_stores.delete(vs_id)
    except NotFoundError:
        pass
    try:
        client.beta.assistants.delete(asst_id)
    except NotFoundError:
        pass


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else float("nan")


def _agg_question(q, scores):
    """Average `repeats` score dicts for one question into a single dict."""
    hits = [1.0 if s["retrieval_hit"] else 0.0 for s in scores if s["retrieval_hit"] is not None]
    mean_bullets = _mean([s["bullets"] for s in scores])
    return dict(
        coverage=_mean([s["coverage"] for s in scores]),
        retrieval_hit=(_mean(hits) if hits else None),
        citation_ok=_mean([1.0 if s["citation_ok"] else 0.0 for s in scores]),
        bullets=mean_bullets,
        bullet_over=mean_bullets > q["max_bullets"],
    )


def evaluate(client, name, docs, dataset, repeats=1, detail=False):
    strat = STRATEGIES[name]
    vs_id, file_ids = build_vector_store(client, strat, docs)
    asst_id = build_assistant(client, vs_id)
    rows = []
    try:
        for q in dataset:
            scores = [score(q, run_question(client, asst_id, q["question"])) for _ in range(repeats)]
            agg = _agg_question(q, scores)
            rows.append((q, agg))
            if detail:
                rh = "n/a" if agg["retrieval_hit"] is None else f"{agg['retrieval_hit']:.2f}"
                print(f"   [{q['id']:<18}] cov={agg['coverage']:.2f} retr_hit={rh} "
                      f"cite={agg['citation_ok']:.2f} bullets={agg['bullets']:.1f}"
                      f"{' !OVER' if agg['bullet_over'] else ''}")
    finally:
        cleanup(client, vs_id, file_ids, asst_id)

    deep = [s for q, s in rows if q.get("deep")]
    return dict(
        n_files=len(file_ids),
        coverage=_mean([s["coverage"] for _, s in rows]),
        deep_coverage=_mean([s["coverage"] for s in deep]),
        retrieval_hit=_mean([s["retrieval_hit"] for _, s in rows if s["retrieval_hit"] is not None]),
        citation=_mean([s["citation_ok"] for _, s in rows]),
        bullet_violations=sum(1 for _, s in rows if s["bullet_over"]),
    )


def main(argv):
    detail = "--detail" in argv
    repeats = 1
    for a in list(argv):
        if a.startswith("--repeats="):
            repeats = int(a.split("=", 1)[1])
    argv = [a for a in argv if a != "--detail" and not a.startswith("--repeats=")]
    names = argv or DEFAULT_STRATEGIES
    for n in names:
        if n not in STRATEGIES:
            sys.exit(f"Unknown strategy '{n}'. Available: {', '.join(STRATEGIES)}")

    load_dotenv()
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    dataset = json.loads((HERE / "dataset.json").read_text())["questions"]
    docs = load_docs(dataset)
    if not docs:
        sys.exit(f"No matching markdown in {MD_DIR} or {MD_FALLBACK}. Run `python main.py` first.")

    results = {}
    for n in names:
        print(f"\n=== Strategy: {n} ({len(docs)} articles, repeats={repeats}) ===")
        results[n] = evaluate(client, n, docs, dataset, repeats=repeats, detail=detail)

    print("\n" + "=" * 92)
    print(f"{'strategy':<22}{'files':>6}{'coverage':>10}{'deep_cov':>10}"
          f"{'retr_hit':>10}{'citation':>10}{'bullet_viol':>13}")
    print("-" * 92)
    for n, r in results.items():
        print(f"{n:<22}{r['n_files']:>6}{r['coverage']:>10.2f}{r['deep_coverage']:>10.2f}"
              f"{r['retrieval_hit']:>10.2f}{r['citation']:>10.2f}{r['bullet_violations']:>13}")
    print("=" * 92)
    print("coverage/deep_cov/retr_hit/citation = higher better (1.0 max); "
          "bullet_viol = count of answers over max_bullets (lower better)")


if __name__ == "__main__":
    main(sys.argv[1:])
