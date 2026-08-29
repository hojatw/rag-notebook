"""Measure when the chat provider actually delivers stream chunks.

Run before turning on `runtime.answer_stream_gate_chars`. Partial streaming can
only help if the provider hands us text *during* generation; several hosted
endpoints buffer the completion and release it in one burst at the end, in which
case a gate gives up part of the abstain guarantee and buys nothing.

    NOTEBOOKLM_ALLOW_INSECURE_DEV_SECRET=1 .venv/bin/python -m tests.probe_provider_stream

Reads the chat connection from the database (same as the app) and makes ONE
chat call. Read the verdict at the bottom of the output.
"""

import asyncio
import time

from app import db, llm

PROMPT = "請條列說明台灣的四季氣候特徵，每一項都要有兩三句說明。"


async def probe() -> None:
    with db.connect() as conn:
        settings = db.load_llm_settings(conn)
    if not settings.get("chat_model"):
        print("Chat model is not configured — set it under /settings first.")
        return

    started = time.perf_counter()
    marks: list[tuple[int, int, int]] = []
    total = 0
    async for chunk in llm.chat_completion_stream(
        settings, PROMPT, "You are a helpful assistant.", call_type="answer_stream"
    ):
        total += len(chunk)
        marks.append((round((time.perf_counter() - started) * 1000), len(chunk), total))

    if not marks:
        print("The provider returned no chunks.")
        return

    span = marks[-1][0]
    first = marks[0][0]
    print(f"provider={settings.get('provider')} model={settings.get('chat_model')}")
    print(f"chunks={len(marks)} chars={total} stream_span={span}ms")
    print()
    print("  arrival    chars   cumulative")
    for ms, n, cum in marks[:20]:
        print(f"  {ms:7}ms {n:8} {cum:12}")
    if len(marks) > 20:
        print(f"  ... {len(marks) - 20} more")
    print()

    # The first chunk's arrival, as a share of the whole stream, is the number
    # that decides this: text that all lands at the end cannot be streamed early
    # no matter how small the gate is.
    share = (first / span * 100) if span else 0.0
    print(f"first chunk at {first}ms = {share:.0f}% into the stream")
    gaps = [marks[i][0] - marks[i - 1][0] for i in range(1, len(marks))]
    if gaps:
        print(f"inter-chunk gap: median {sorted(gaps)[len(gaps) // 2]}ms, max {max(gaps)}ms")
    print()
    if share > 50:
        print("VERDICT: the provider buffers upstream and bursts at the end.")
        print("  Leave runtime.answer_stream_gate_chars at 0 — a gate would give up")
        print("  part of the abstain guarantee without making anything feel faster.")
    else:
        print("VERDICT: the provider streams during generation.")
        print("  A gate will help here. 120 is the value derived from the reference")
        print("  corpus (docs/DEVELOPMENT.md); re-derive it if answers differ in shape.")


if __name__ == "__main__":
    asyncio.run(probe())
