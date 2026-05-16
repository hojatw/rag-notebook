"""Retrieval quality eval harness.

Run against the live SQLite + Chroma data:

    .venv/bin/python -m tests.eval_retrieval
    .venv/bin/python -m tests.eval_retrieval --no-rerank
    .venv/bin/python -m tests.eval_retrieval --top-k 10

Reads ``tests/eval_questions.json`` (one fixture file per project), calls the
real ``retrieve()`` for each question against the configured notebook, then
prints per-question hits and overall recall@k + MRR.

A "hit" means at least one of the top-k retrieved chunks
  (a) comes from ``expected_filename`` AND
  (b) contains every substring listed in ``expected_substrings``.

Skips silently when LLM settings are not configured (the production
fallback embedding is too noisy to be worth measuring).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).parent / "eval_questions.json"


def chunk_matches(chunk: dict, expected_filename: str, expected_substrings: list[str]) -> bool:
    """A chunk hits iff filename matches AND every expected substring appears."""
    if chunk.get("filename") != expected_filename:
        return False
    text = chunk.get("text", "")
    return all(sub in text for sub in expected_substrings)


def reciprocal_rank(retrieved: list[dict], expected_filename: str, expected_substrings: list[str]) -> float:
    for rank, chunk in enumerate(retrieved, start=1):
        if chunk_matches(chunk, expected_filename, expected_substrings):
            return 1.0 / rank
    return 0.0


async def run(top_k: int, disable_rerank: bool) -> int:
    from app.db import connect, load_llm_settings
    from app.main import retrieve

    with connect() as conn:
        settings = load_llm_settings(conn) or {}

    if not settings.get("api_key") or not settings.get("chat_model"):
        print("LLM not configured — skipping eval (would only measure local-fallback embedding noise).")
        return 1

    if disable_rerank:
        # Strip chat model so rewrite/rerank both fall back to no-LLM paths.
        settings = {**settings, "chat_model": ""}

    fixtures = json.loads(FIXTURES.read_text(encoding="utf-8"))
    notebook_id = fixtures["notebook_id"]
    questions = fixtures["questions"]

    with connect() as conn:
        notebook = conn.execute("SELECT * FROM notebooks WHERE id = ?", (notebook_id,)).fetchone()
        if notebook is None:
            print(f"Notebook id={notebook_id} not found in this database. Edit tests/eval_questions.json.")
            return 2
        user_id = notebook["user_id"]
        source_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM sources WHERE notebook_id = ? AND status = 'indexed'",
                (notebook_id,),
            ).fetchall()
        ]

    if not source_ids:
        print(f"Notebook id={notebook_id} has no indexed sources.")
        return 3

    print(f"Running {len(questions)} questions against notebook id={notebook_id} ({len(source_ids)} indexed sources)")
    print(f"top_k={top_k}, rerank={'off' if disable_rerank else 'on'}")
    print("=" * 80)

    hits = 0
    rr_sum = 0.0
    rows = []
    for q in questions:
        retrieved = await retrieve(
            q["q"], None, settings, history=[], user_id=user_id, source_ids=source_ids,
        )
        # retrieve() already caps at 6 (rerank limit); for top_k > 6 we can
        # only score what came back. Note the truncation in the report.
        top = retrieved[:top_k]
        hit_rank = next(
            (r for r, c in enumerate(top, start=1)
             if chunk_matches(c, q["expected_filename"], q["expected_substrings"])),
            None,
        )
        rr = (1.0 / hit_rank) if hit_rank else 0.0
        if hit_rank:
            hits += 1
        rr_sum += rr
        rows.append({"q": q["q"][:48], "expected": q["expected_filename"][:32], "hit_rank": hit_rank, "rr": rr})

    print()
    print(f"{'Hit @':>5}  {'1/r':>5}  Question / expected file")
    print("-" * 80)
    for r in rows:
        rank_str = str(r["hit_rank"]) if r["hit_rank"] else "—"
        print(f"{rank_str:>5}  {r['rr']:>5.2f}  {r['q']}")
        print(f"{' ':>5}  {' ':>5}  └── expected: {r['expected']}")

    n = len(questions)
    recall_at_k = hits / n
    mrr = rr_sum / n
    print()
    print("=" * 80)
    print(f"Recall@{top_k}: {recall_at_k:.1%} ({hits}/{n})")
    print(f"MRR:        {mrr:.3f}")
    print("=" * 80)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top-k", type=int, default=5, help="rank cutoff for hit/MRR (default: 5)")
    parser.add_argument("--no-rerank", action="store_true", help="disable LLM rerank to measure raw hybrid recall")
    args = parser.parse_args()
    return asyncio.run(run(top_k=args.top_k, disable_rerank=args.no_rerank))


if __name__ == "__main__":
    raise SystemExit(main())
