"""Child-process entry point for isolated file extraction.

``python -m app.extract_child <source path> <result path> <cpu limit s>``

`ingest._extract_isolated` runs this once per source so that a parser which
hangs, crashes or balloons can be killed without taking the ingest worker (or,
with the inline worker, the web process) down with it — a thread running
`extract_sections` cannot be stopped from outside.

The result goes to a file, not stdout, so parser chatter on stdout can never
corrupt it. Exactly one JSON object is written:

- ``{"ok": true, "result": {...ExtractionResult fields...}}``
- ``{"ok": false, "error_type": "...", "error": "<str(exc)>"}`` — the parent
  re-raises with the same message, because ``str(exc)`` is what
  `process_source` stores in ``sources.error`` and the user reads.

No result file (or an unreadable one) means the child died; the parent treats
that as a crash. Logging goes to stderr, which the parent drains and relays, so
the app log keeps a single writer.
"""
import dataclasses
import json
import logging
import os
import sys
from pathlib import Path


def _limit_cpu(seconds: int) -> None:
    """Backstop for an orphaned child: the kernel ends it after ``seconds`` of CPU.

    The parent's wall-clock timeout is the real limit; this only matters if the
    parent itself is SIGKILLed mid-parse and can no longer kill us.
    """
    if seconds <= 0:
        return
    try:
        import resource
    except ImportError:  # pragma: no cover - non-Unix
        return
    resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds))


def main(argv: list[str]) -> int:
    source_path, result_path, cpu_limit = Path(argv[0]), Path(argv[1]), int(argv[2])
    _limit_cpu(cpu_limit)
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get("NOTEBOOKLM_LOG_LEVEL", "INFO").upper(),
        format="%(levelname)s %(name)s %(message)s",
    )
    from .ingest import extract_sections

    try:
        result = extract_sections(source_path)
        payload = {"ok": True, "result": dataclasses.asdict(result)}
    except Exception as exc:
        logging.getLogger(__name__).exception("extract_child_failed path=%s", source_path.name)
        payload = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
    result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
