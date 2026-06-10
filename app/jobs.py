"""DB-backed ingest job queue (P1-1).

A deliberately small abstraction over the ``ingest_jobs`` SQLite table. The web
process calls :func:`enqueue_source` instead of scheduling a FastAPI
``BackgroundTask``; one or more workers (in-process or a standalone
``python -m app.worker``) drain the queue via :func:`claim_next_job`.

This module is the single swap-point: replacing the SQLite backend with Redis +
RQ later means rewriting these functions, not the call sites. The claim runs
inside a ``BEGIN IMMEDIATE`` write transaction so two workers can't claim the
same job; a ``running`` row whose ``claimed_at`` is older than
``JOB_VISIBILITY_TIMEOUT_S`` is treated as abandoned (crashed worker) and
re-claimed, capped by ``JOB_MAX_ATTEMPTS`` to defuse poison-pill jobs.
"""
import logging
import time
from typing import Optional

from .config import config
from .db import connect

logger = logging.getLogger("notebooklm")

# A claimed job whose worker hasn't finished within this window is assumed dead
# and becomes re-claimable. Generous: hundreds-of-page PDFs embed slowly.
JOB_VISIBILITY_TIMEOUT_S = config.jobs.visibility_timeout_s
# Hard cap on (re)claims so a job that keeps crashing the worker eventually
# fails instead of looping forever.
JOB_MAX_ATTEMPTS = config.jobs.max_attempts


def enqueue_source(source_id: int) -> None:
    """Queue (or re-queue) ingestion for a source.

    Idempotent per source: ``UNIQUE(source_id)`` means a reindex resets the
    existing row back to ``queued`` with a fresh attempt budget rather than
    piling up duplicate jobs.
    """
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO ingest_jobs (source_id, status, attempts, claimed_at, error, updated_at)
            VALUES (?, 'queued', 0, NULL, '', CURRENT_TIMESTAMP)
            ON CONFLICT(source_id) DO UPDATE SET
                status = 'queued',
                attempts = 0,
                claimed_at = NULL,
                error = '',
                updated_at = CURRENT_TIMESTAMP
            """,
            (source_id,),
        )
    logger.info("ingest_job_enqueued source_id=%s", source_id)


def claim_next_job() -> Optional[dict]:
    """Atomically claim the next runnable job, or return None if there are none.

    Picks the oldest ``queued`` job, or a ``running`` job abandoned past the
    visibility timeout. Returns a dict with ``id``, ``source_id`` and the
    post-claim ``attempts``; the worker passes ``attempts`` back to
    :func:`requeue_or_fail` on failure.
    """
    now = time.time()
    stale_before = now - JOB_VISIBILITY_TIMEOUT_S
    conn = connect()
    try:
        conn.isolation_level = None  # manual transaction control
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT id, source_id, attempts FROM ingest_jobs
            WHERE status = 'queued'
               OR (status = 'running' AND claimed_at IS NOT NULL AND claimed_at < ?)
            ORDER BY id
            LIMIT 1
            """,
            (stale_before,),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        attempts = row["attempts"] + 1
        conn.execute(
            "UPDATE ingest_jobs SET status = 'running', claimed_at = ?, attempts = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (now, attempts, row["id"]),
        )
        conn.execute("COMMIT")
        return {"id": row["id"], "source_id": row["source_id"], "attempts": attempts}
    finally:
        conn.close()


def mark_done(job_id: int) -> None:
    """Mark a job complete.

    Guarded by ``status = 'running'`` so a reindex that re-queued the row while
    the worker was busy is not clobbered — that fresh job is left to run again.
    """
    with connect() as conn:
        conn.execute(
            "UPDATE ingest_jobs SET status = 'done', error = '', "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'running'",
            (job_id,),
        )


def requeue_or_fail(job_id: int, source_id: int, attempts: int, error: str) -> bool:
    """Retry a crashed job, or give up once it has exhausted its attempts.

    Returns True if the job was re-queued for another attempt, False if it was
    marked failed. Only acts on rows still in ``running`` (a concurrent reindex
    that re-queued the row wins). On permanent failure the source row is also
    flipped to ``failed`` so the UI reflects it — a hard worker crash can leave
    the source stuck at ``processing`` where ``process_source``'s own handler
    never ran.
    """
    truncated = (error or "")[:500]
    with connect() as conn:
        if attempts < JOB_MAX_ATTEMPTS:
            updated = conn.execute(
                "UPDATE ingest_jobs SET status = 'queued', claimed_at = NULL, error = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'running'",
                (truncated, job_id),
            ).rowcount
            if updated:
                logger.warning("ingest_job_requeued job_id=%s attempts=%s", job_id, attempts)
                return True
            return False
        updated = conn.execute(
            "UPDATE ingest_jobs SET status = 'failed', error = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'running'",
            (truncated, job_id),
        ).rowcount
        if updated:
            conn.execute(
                "UPDATE sources SET status = 'failed', error = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status != 'indexed'",
                (truncated or "Ingestion failed.", source_id),
            )
    logger.error("ingest_job_failed job_id=%s attempts=%s error=%s", job_id, attempts, truncated)
    return False
