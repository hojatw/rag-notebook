"""Ingest worker (P1-1).

Drains the ``ingest_jobs`` queue by running :func:`app.ingest.process_source`.
The same :func:`run_worker_loop` coroutine powers both deployment modes:

* **Standalone** — ``python -m app.worker`` runs a dedicated process so the
  CPU-heavy PDF extraction/embedding lives off the web process (the real P1-1
  win). Used in production with ``NOTEBOOKLM_INLINE_WORKER=0`` on the app.
* **Inline** — the FastAPI lifespan spins this loop as a background task when
  ``NOTEBOOKLM_INLINE_WORKER`` is enabled (the default), so a single
  ``uvicorn app.main:app`` keeps ingesting like before — just queue-backed and
  restart-durable.

``process_source`` swallows its own ingestion errors (marking the source
``failed``), so the ``except`` here only fires on a true crash/cancel; those
jobs are retried or failed via the queue's visibility timeout.
"""
import asyncio
import logging
import os
import signal

import httpx

from .db import init_db
from .ingest import process_source
from .jobs import claim_next_job, mark_done, requeue_or_fail
from .llm import close_http_client, set_http_client

logger = logging.getLogger("notebooklm")

DEFAULT_POLL_INTERVAL_S = 2.0


async def _wait(interval: float, stop_event: "asyncio.Event | None") -> None:
    """Sleep ``interval`` seconds, waking early if ``stop_event`` is set."""
    if stop_event is None:
        await asyncio.sleep(interval)
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=interval)
    except asyncio.TimeoutError:
        pass


async def run_worker_loop(
    *,
    poll_interval: float = DEFAULT_POLL_INTERVAL_S,
    stop_event: "asyncio.Event | None" = None,
) -> None:
    """Poll the queue and process jobs until ``stop_event`` is set or cancelled."""
    logger.info("ingest_worker_started poll_interval=%s", poll_interval)
    while stop_event is None or not stop_event.is_set():
        job = claim_next_job()
        if job is None:
            await _wait(poll_interval, stop_event)
            continue
        logger.info(
            "ingest_job_claimed job_id=%s source_id=%s attempt=%s",
            job["id"], job["source_id"], job["attempts"],
        )
        try:
            await process_source(job["source_id"])
            mark_done(job["id"])
            logger.info("ingest_job_done job_id=%s source_id=%s", job["id"], job["source_id"])
        except asyncio.CancelledError:
            # Shutdown mid-job: leave it 'running' so the visibility timeout
            # re-claims it on the next start.
            raise
        except Exception as exc:  # pragma: no cover - true worker crash path
            logger.exception("ingest_job_crashed job_id=%s source_id=%s", job["id"], job["source_id"])
            requeue_or_fail(job["id"], job["source_id"], job["attempts"], str(exc))
    logger.info("ingest_worker_stopped")


async def _run_standalone() -> None:
    init_db()
    set_http_client(httpx.AsyncClient(timeout=None))
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, ValueError):  # e.g. non-main thread / Windows
            pass
    try:
        await run_worker_loop(stop_event=stop_event)
    finally:
        await close_http_client()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("NOTEBOOKLM_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_run_standalone())


if __name__ == "__main__":
    main()
