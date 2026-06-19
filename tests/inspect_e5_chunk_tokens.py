"""Inspect indexed chunk lengths against the multilingual-e5 token limit.

Manual diagnostic for QUALITY.md Q0-5:

    .venv/bin/python -m tests.inspect_e5_chunk_tokens

By default the script uses the exact Hugging Face tokenizer for
``intfloat/multilingual-e5-large`` with ``local_files_only=True`` so it never
downloads model files unexpectedly. Install/cache the optional tokenizer stack
before running it:

    .venv/bin/python -m pip install "transformers>=4.0"
    .venv/bin/python - <<'PY'
    from transformers import AutoTokenizer
    AutoTokenizer.from_pretrained("intfloat/multilingual-e5-large")
    PY

Use ``--download-tokenizer`` only on a machine where outbound access is allowed.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DEFAULT_MODEL = "intfloat/multilingual-e5-large"
DEFAULT_PREFIX = "passage: "
DEFAULT_MAX_TOKENS = 512


def _cjk_ratio(text: str) -> float:
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return 0.0
    cjk = sum(1 for char in letters if "\u4e00" <= char <= "\u9fff")
    return cjk / len(letters)


def is_table_like(text: str) -> bool:
    """Heuristic for table-ish chunks likely to have dense symbols/numbers."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) >= 3:
        pipe_rows = sum(1 for line in lines if "|" in line or "\t" in line)
        if pipe_rows / len(lines) >= 0.35:
            return True
    chars = [char for char in text if not char.isspace()]
    if not chars:
        return False
    numeric_or_delim = sum(1 for char in chars if char.isdigit() or char in "|,;:\t%()[]{}")
    return len(chars) >= 120 and (numeric_or_delim / len(chars)) >= 0.28


def chunk_category(text: str) -> str:
    if is_table_like(text):
        return "table"
    ratio = _cjk_ratio(text)
    if ratio >= 0.50:
        return "cjk"
    if ratio <= 0.05:
        return "latin"
    return "mixed"


def percentile(values: list[int], pct: float) -> float:
    """Return a nearest-rank-ish percentile without third-party deps."""
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    ordered = sorted(values)
    index = (len(ordered) - 1) * pct
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return float(ordered[lower])
    fraction = index - lower
    return ordered[lower] + ((ordered[upper] - ordered[lower]) * fraction)


def load_tokenizer(model: str, *, local_files_only: bool):
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise SystemExit(
            "Missing optional dependency: transformers.\n"
            "Install it in the project venv when you want to run this diagnostic:\n"
            '  .venv/bin/python -m pip install "transformers>=4.0"'
        ) from exc
    try:
        return AutoTokenizer.from_pretrained(model, local_files_only=local_files_only)
    except Exception as exc:
        mode = "local cache" if local_files_only else "Hugging Face"
        raise SystemExit(
            f"Could not load tokenizer {model!r} from {mode}: {exc}\n"
            "Cache it once or rerun with --download-tokenizer on a network-enabled machine."
        ) from exc


def token_count(tokenizer: Any, text: str) -> int:
    encoded = tokenizer(text, add_special_tokens=True, truncation=False)
    return len(encoded["input_ids"])


def fetch_chunks(*, notebook_id: int | None, source_id: int | None, limit: int | None) -> list[dict[str, Any]]:
    from app.db import connect

    clauses = ["sources.status = 'indexed'"]
    params: list[Any] = []
    if notebook_id is not None:
        clauses.append("sources.notebook_id = ?")
        params.append(notebook_id)
    if source_id is not None:
        clauses.append("chunks.source_id = ?")
        params.append(source_id)
    sql = f"""
        SELECT chunks.id, chunks.source_id, chunks.chunk_index, chunks.location, chunks.text,
               sources.filename, sources.notebook_id
        FROM chunks JOIN sources ON sources.id = chunks.source_id
        WHERE {" AND ".join(clauses)}
        ORDER BY chunks.id ASC
    """
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    with connect() as conn:
        return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]


def summarize(values: Iterable[int]) -> dict[str, float]:
    data = list(values)
    if not data:
        return {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "mean": 0.0}
    return {
        "count": len(data),
        "p50": percentile(data, 0.50),
        "p95": percentile(data, 0.95),
        "p99": percentile(data, 0.99),
        "max": float(max(data)),
        "mean": statistics.fmean(data),
    }


def print_summary(label: str, stats: dict[str, float]) -> None:
    print(
        f"{label:<10} count={int(stats['count']):>5} "
        f"p50={stats['p50']:>6.1f} p95={stats['p95']:>6.1f} "
        f"p99={stats['p99']:>6.1f} max={stats['max']:>6.0f} mean={stats['mean']:>6.1f}"
    )


def run(args: argparse.Namespace) -> int:
    tokenizer = load_tokenizer(args.model, local_files_only=not args.download_tokenizer)
    rows = fetch_chunks(notebook_id=args.notebook_id, source_id=args.source_id, limit=args.limit)
    if not rows:
        print("No indexed chunks found for the selected filters.")
        return 2

    all_counts: list[int] = []
    by_category: dict[str, list[int]] = defaultdict(list)
    category_counts: Counter[str] = Counter()
    over_limit: list[dict[str, Any]] = []
    prefix = args.prefix
    for row in rows:
        text = prefix + row["text"]
        count = token_count(tokenizer, text)
        category = chunk_category(row["text"])
        all_counts.append(count)
        by_category[category].append(count)
        category_counts[category] += 1
        if count > args.max_tokens:
            over_limit.append({**row, "token_count": count, "category": category})

    print(f"Tokenizer: {args.model}")
    print(f"Prefix: {prefix!r}")
    print(f"Limit: {args.max_tokens} tokens including special tokens")
    print(f"Chunks scanned: {len(rows)}")
    print()
    print_summary("all", summarize(all_counts))
    for category in ("cjk", "latin", "mixed", "table"):
        if category_counts[category]:
            print_summary(category, summarize(by_category[category]))
    print()

    over_pct = len(over_limit) / len(rows)
    print(f"Over limit: {len(over_limit)} / {len(rows)} ({over_pct:.1%})")
    if over_limit:
        print()
        print(f"Top {min(args.samples, len(over_limit))} over-limit chunks:")
        print("-" * 100)
        for item in sorted(over_limit, key=lambda row: row["token_count"], reverse=True)[: args.samples]:
            snippet = " ".join((item["text"] or "").split())[:160]
            print(
                f"tokens={item['token_count']:>5} category={item['category']:<5} "
                f"chunk_id={item['id']} source_id={item['source_id']} "
                f"chunk_index={item['chunk_index']} file={item['filename']}"
            )
            print(f"  location={item['location']}")
            print(f"  {snippet}")

    if args.fail_on_over_limit and over_limit:
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"HF tokenizer name (default: {DEFAULT_MODEL})")
    parser.add_argument("--prefix", default=DEFAULT_PREFIX, help=f"passage prefix to prepend (default: {DEFAULT_PREFIX!r})")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS, help="model token limit (default: 512)")
    parser.add_argument("--notebook-id", type=int, help="scan only one notebook id")
    parser.add_argument("--source-id", type=int, help="scan only one source id")
    parser.add_argument("--limit", type=int, help="scan at most N chunks")
    parser.add_argument("--samples", type=int, default=10, help="number of over-limit examples to print")
    parser.add_argument("--download-tokenizer", action="store_true", help="allow transformers to download tokenizer files")
    parser.add_argument("--fail-on-over-limit", action="store_true", help="exit 1 when any chunk exceeds --max-tokens")
    args = parser.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
