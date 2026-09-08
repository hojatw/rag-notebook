"""Inspect ONE file's chunks against the multilingual-e5 token limit — offline.

Companion to :mod:`tests.inspect_e5_chunk_tokens`, which scans chunks that are
already **indexed in the database**. That is useless for diagnosing an ingest
that *failed*: ``process_source`` deletes the source's chunks before it starts
and only writes them after the embedding call returns, so a file rejected by the
embedding endpoint leaves no chunk rows behind to inspect.

This script runs the same extraction and chunking the ingest worker runs, then
counts real tokens with the model's own tokenizer. It never touches the network,
the database, or the vector store — so it is safe to point at a customer file on
any machine:

    PYTHONPATH=. .venv/bin/python -m tests.inspect_file_tokens /path/to/file.pdf

Exit code is 1 when any chunk exceeds --max-tokens, so it can gate a check.

The tokenizer is loaded with ``local_files_only=True`` by default; see
:mod:`tests.inspect_e5_chunk_tokens` for how to cache it once.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.inspect_e5_chunk_tokens import (  # noqa: E402
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_PREFIX,
    chunk_category,
    load_tokenizer,
    print_summary,
    summarize,
    token_count,
)


def build_records(path: Path) -> tuple[Any, list[tuple[str, str]]]:
    """Reproduce exactly what `process_source` feeds to `embed_texts`."""
    from app.ingest import chunk_sections, extract_sections

    extraction = extract_sections(path)
    records = (
        list(extraction.sections)
        if extraction.pre_chunked
        else chunk_sections(extraction.sections)
    )
    return extraction, records


def run(args: argparse.Namespace) -> int:
    path = Path(args.path).expanduser()
    if not path.is_file():
        raise SystemExit(f"Not a file: {path}")

    tokenizer = load_tokenizer(args.model, local_files_only=not args.download_tokenizer)
    extraction, records = build_records(path)

    print(f"File:       {path}")
    print(f"Extractor:  {extraction.extractor}   pre_chunked={extraction.pre_chunked}")
    print(f"Notes:      {extraction.notes or '-'}")
    print(f"Sections:   {len(extraction.sections)}   Chunks: {len(records)}")
    for sheet in extraction.details.get("sheets", []):
        print(f"  sheet: {sheet}")
    print(f"Tokenizer:  {args.model}   prefix={args.prefix!r}   limit={args.max_tokens}")
    print()

    if not records:
        print("No chunks produced — extraction found no text. Token length is not the problem.")
        return 2

    from app.ingest import estimate_embedding_tokens

    rows: list[dict[str, Any]] = []
    by_category: dict[str, list[int]] = defaultdict(list)
    kinds: Counter[str] = Counter()
    for index, (location, text) in enumerate(records):
        count = token_count(tokenizer, args.prefix + text)
        category = chunk_category(text)
        by_category[category].append(count)
        kinds[category] += 1
        rows.append({
            "index": index,
            "location": location,
            "text": text,
            "tokens": count,
            "estimated": estimate_embedding_tokens(text),
            "chars": len(text),
            "category": category,
        })

    print_summary("all", summarize([row["tokens"] for row in rows]))
    for category in ("cjk", "latin", "mixed", "table"):
        if kinds[category]:
            print_summary(category, summarize(by_category[category]))
    print()

    over = [row for row in rows if row["tokens"] > args.max_tokens]
    print(f"Over limit: {len(over)} / {len(rows)} ({len(over) / len(rows):.1%})")

    # Always show the worst chunks: when nothing is over the limit, how close the
    # file got is the useful answer ("not the cause" vs "one edit away").
    shown = sorted(rows, key=lambda row: -row["tokens"])[: args.samples]
    print()
    print(f"Top {len(shown)} longest chunks:")
    print("-" * 100)
    for row in shown:
        flag = "  <-- OVER LIMIT" if row["tokens"] > args.max_tokens else ""
        print(
            f"tokens={row['tokens']:>5} (app estimate {row['estimated']:>5}) "
            f"chars={row['chars']:>5} category={row['category']:<5} "
            f"chunk_index={row['index']}{flag}"
        )
        print(f"  location={row['location']}")
        print("  " + " ".join((row["text"] or "").split())[: args.snippet])
    print()

    if over:
        print(
            f"=> {len(over)} chunk(s) exceed {args.max_tokens} tokens. A vLLM embedding\n"
            f"   endpoint rejects the whole request with HTTP 400 rather than truncating,\n"
            f"   so every batch containing one of these fails and the source ingest fails."
        )
    else:
        print(
            f"=> No chunk exceeds {args.max_tokens} tokens, so chunk length is NOT what this\n"
            f"   file's embedding failure came from. Look at the endpoint's own limit,\n"
            f"   the configured [chunking] values, and the app log's embedding_api_failed."
        )
    return 1 if over else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="file to extract, chunk, and measure")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"HF tokenizer name (default: {DEFAULT_MODEL})")
    parser.add_argument("--prefix", default=DEFAULT_PREFIX, help=f"passage prefix to prepend (default: {DEFAULT_PREFIX!r})")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS, help="model token limit (default: 512)")
    parser.add_argument("--samples", type=int, default=10, help="number of longest chunks to print")
    parser.add_argument("--snippet", type=int, default=200, help="characters of chunk text to print")
    parser.add_argument("--download-tokenizer", action="store_true", help="allow transformers to download tokenizer files")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
