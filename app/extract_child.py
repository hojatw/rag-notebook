"""Child-process entry point for isolated file extraction.

``python -m app.extract_child <source path> <result path> <cpu limit s> <memory limit MB>``

`ingest._extract_isolated` runs this once per source so that a parser which
hangs, crashes or balloons can be killed without taking the ingest worker (or,
with the inline worker, the web process) down with it — a thread running
`extract_sections` cannot be stopped from outside.

The result goes to a file, not stdout, so parser chatter on stdout can never
corrupt it. Exactly one JSON object is written:

- ``{"ok": true, "result": {...ExtractionResult fields...}, "runtime": {...}}``
- ``{"ok": false, "error_type": "...", "error": "<str(exc)>", "runtime": {...}}``
  — the parent re-raises with the same message, because ``str(exc)`` is what
  `process_source` stores in ``sources.error`` and the user reads.

``runtime`` reports what this process actually ran under and used:
``peak_rss_mb``, ``data_limit_mb`` (0 = uncapped) and ``oom_score_adj``.

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


def _limit_memory(megabytes: int) -> None:
    """Cap the data segment so an allocation bomb fails here, not on the host.

    RLIMIT_DATA rather than RLIMIT_AS: address space also counts shared
    libraries and per-core allocator/BLAS reservations, measured at 4x the real
    RSS for an XLSX parse and growing with the core count, so an AS cap would
    reject ordinary files on bigger hosts. Linux only — other platforms do not
    enforce RLIMIT_DATA for mmap'd allocations, so the cap is skipped there.
    """
    if megabytes <= 0 or not sys.platform.startswith("linux"):
        if megabytes > 0:
            logging.getLogger(__name__).warning(
                "extract_memory_limit_skipped platform=%s detail=RLIMIT_DATA is enforced on Linux only",
                sys.platform,
            )
        return
    import resource

    limit = megabytes * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_DATA, (limit, limit))


def _prefer_oom_kill() -> None:
    """Volunteer this process as the OOM killer's first choice.

    If the host or container runs out despite the caps, the kernel then kills
    the parse that caused it rather than the ingest worker or the web app.
    Raising one's own score needs no privilege.
    """
    try:
        Path("/proc/self/oom_score_adj").write_text("1000")
    except OSError:
        pass


def _peak_rss_mb(resource) -> float:
    """This process's own peak RSS.

    Not ``ru_maxrss`` on Linux: it survives execve, so a child spawned by a
    600 MB worker reports 619 MB while its real peak (VmHWM, reset with the new
    address space) is 8 MB — measured. That would fake every parse's memory
    and make ordinary parse errors look like cap hits.
    """
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return round(int(line.split()[1]) / 1024, 1)
    except (OSError, ValueError, IndexError):
        pass
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Fallback (non-Linux): ru_maxrss is bytes on macOS, KiB elsewhere.
    return round(peak / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)


def _runtime_report() -> dict:
    report: dict = {"peak_rss_mb": None, "data_limit_mb": 0, "oom_score_adj": None}
    try:
        import resource
    except ImportError:  # pragma: no cover - non-Unix
        return report
    report["peak_rss_mb"] = _peak_rss_mb(resource)
    soft, _hard = resource.getrlimit(resource.RLIMIT_DATA)
    report["data_limit_mb"] = 0 if soft == resource.RLIM_INFINITY else soft // (1024 * 1024)
    try:
        report["oom_score_adj"] = int(Path("/proc/self/oom_score_adj").read_text())
    except (OSError, ValueError):
        pass
    return report


def main(argv: list[str]) -> int:
    source_path, result_path, cpu_limit = Path(argv[0]), Path(argv[1]), int(argv[2])
    memory_limit = int(argv[3]) if len(argv) > 3 else 0
    _limit_cpu(cpu_limit)
    _prefer_oom_kill()
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get("NOTEBOOKLM_LOG_LEVEL", "INFO").upper(),
        format="%(levelname)s %(name)s %(message)s",
    )
    _limit_memory(memory_limit)
    from .ingest import extract_sections

    try:
        result = extract_sections(source_path)
        payload = {"ok": True, "result": dataclasses.asdict(result)}
    except Exception as exc:
        logging.getLogger(__name__).exception("extract_child_failed path=%s", source_path.name)
        payload = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
    payload["runtime"] = _runtime_report()
    result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
