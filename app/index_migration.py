"""Source classification for an embedding-dimension migration (ROADMAP O0, Phase B).

When the Chroma collection is replaced to release its locked dimension
(:func:`app.vector_store.reset_collection`), SQLite still holds every chunk's
old ``embedding_json``. Startup sync selects ``sources.status = 'indexed'`` and
would re-upsert those vectors straight back into the fresh collection —
**re-locking it at the old dimension** and undoing the migration. That is the
quietest way for O0 to appear fixed and not be.

So a migration has to decide, per source, whether its stored vectors can be
reused at the target dimension. Sources that cannot are moved to
``stale_embedding``: a status of its own rather than ``failed``, because
"needs re-embedding" and "this file broke" are different problems and an admin
reading a list of ``failed`` rows would go looking for a bad upload.

This module is the single implementation of that classification.
``scripts/reset_chroma_dimension.py`` predates it and keeps its own copy for
the break-glass path (it must run when the app cannot import), so the two are
deliberately parallel — change both, and the tests here are the reference.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import time
from dataclasses import dataclass

from . import db
from .config import config


logger = logging.getLogger(__name__)

# Sources whose stored vectors are at the wrong dimension. Excluded from
# `sources.status = 'indexed'` queries — which is what keeps startup sync from
# re-locking the collection — and re-enters the normal flow through Reindex.
STALE_STATUS = "stale_embedding"

# Chroma's message when an upsert's width disagrees with the collection.
DIMENSION_MISMATCH_FRAGMENT = "collection expecting embedding with dimension"

STALE_REASON = (
    "Embedding 維度已變更，此來源的既有向量無法沿用。請執行「重新索引」以重新產生 embedding。"
)


@dataclass(frozen=True)
class SourceState:
    """One source's stored-vector shape, as seen from SQLite."""

    source_id: int
    filename: str
    status: str
    error: str
    chunk_count: int
    min_dimension: int | None
    max_dimension: int | None

    @property
    def single_dimension(self) -> int | None:
        """The source's dimension, or None when it has none or several.

        A source with mixed widths (a half-finished reindex, a manual edit)
        has no single answer, so it is never treated as reusable.
        """
        if self.chunk_count and self.min_dimension == self.max_dimension:
            return self.min_dimension
        return None


@dataclass(frozen=True)
class MigrationPlan:
    """What a migration to ``target_dimension`` would do to each source."""

    target_dimension: int
    reusable_source_ids: tuple[int, ...]
    recoverable_failure_ids: tuple[int, ...]
    mark_stale_ids: tuple[int, ...]
    unchanged_source_ids: tuple[int, ...]
    sources: tuple[SourceState, ...]

    def summary(self) -> dict[str, int]:
        """Counts for the admin preview and audit metadata."""
        return {
            "target_dimension": self.target_dimension,
            "reusable": len(self.reusable_source_ids),
            "recovered": len(self.recoverable_failure_ids),
            "stale": len(self.mark_stale_ids),
            "unchanged": len(self.unchanged_source_ids),
        }


def classify_sources(target_dimension: int) -> MigrationPlan:
    """Group every source by whether its vectors survive the migration.

    Pure read — safe to call for a dry-run preview.
    """
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT
                sources.id,
                sources.filename,
                sources.status,
                sources.error,
                COUNT(chunks.id) AS chunk_count,
                MIN(json_array_length(chunks.embedding_json)) AS min_dimension,
                MAX(json_array_length(chunks.embedding_json)) AS max_dimension
            FROM sources
            LEFT JOIN chunks ON chunks.source_id = sources.id
            GROUP BY sources.id, sources.filename, sources.status, sources.error
            ORDER BY sources.id
            """
        ).fetchall()

    sources = tuple(
        SourceState(
            source_id=int(row["id"]),
            filename=str(row["filename"]),
            status=str(row["status"]),
            error=str(row["error"] or ""),
            chunk_count=int(row["chunk_count"]),
            min_dimension=int(row["min_dimension"]) if row["min_dimension"] is not None else None,
            max_dimension=int(row["max_dimension"]) if row["max_dimension"] is not None else None,
        )
        for row in rows
    )

    reusable: list[int] = []
    recoverable: list[int] = []
    stale: list[int] = []
    unchanged: list[int] = []
    for source in sources:
        at_target = source.single_dimension == target_dimension
        failed_on_mismatch = (
            source.status == "failed"
            and DIMENSION_MISMATCH_FRAGMENT in source.error.lower()
        )
        if source.status == "indexed" and at_target:
            reusable.append(source.source_id)
        elif failed_on_mismatch and at_target:
            # Already-correct vectors that only failed because the collection
            # was locked. The migration unlocks it, so these come back.
            reusable.append(source.source_id)
            recoverable.append(source.source_id)
        elif source.status in ("indexed", STALE_STATUS):
            # Indexed at another (or mixed/unknown) width — startup sync would
            # replay these and re-lock the collection.
            stale.append(source.source_id)
        else:
            # uploaded / processing / unrelated failures re-embed on their own.
            unchanged.append(source.source_id)

    return MigrationPlan(
        target_dimension=target_dimension,
        reusable_source_ids=tuple(reusable),
        recoverable_failure_ids=tuple(recoverable),
        mark_stale_ids=tuple(stale),
        unchanged_source_ids=tuple(unchanged),
        sources=sources,
    )


def apply_source_states(plan: MigrationPlan) -> dict[str, int]:
    """Write the plan's status changes. Returns how many rows moved each way.

    Call this **after** the collection has been replaced: until then the old
    statuses are what a rollback would restore.
    """
    with db.connect() as conn:
        if plan.recoverable_failure_ids:
            marks = ",".join("?" for _ in plan.recoverable_failure_ids)
            conn.execute(
                f"UPDATE sources SET status = 'indexed', error = '',"
                f" updated_at = CURRENT_TIMESTAMP WHERE id IN ({marks})",
                plan.recoverable_failure_ids,
            )
        if plan.mark_stale_ids:
            marks = ",".join("?" for _ in plan.mark_stale_ids)
            conn.execute(
                f"UPDATE sources SET status = ?, error = ?,"
                f" updated_at = CURRENT_TIMESTAMP WHERE id IN ({marks})",
                (STALE_STATUS, STALE_REASON, *plan.mark_stale_ids),
            )
        conn.commit()

    result = {"recovered": len(plan.recoverable_failure_ids), "stale": len(plan.mark_stale_ids)}
    logger.info(
        "index_migration_states_applied target_dimension=%s recovered=%s stale=%s",
        plan.target_dimension, result["recovered"], result["stale"],
    )
    return result


def stale_source_count() -> int:
    """How many sources are currently waiting for a Reindex."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM sources WHERE status = ?", (STALE_STATUS,)
        ).fetchone()
    return int(row["n"]) if row else 0


def chunk_dimension(embedding_json: str) -> int | None:
    """Width of a stored embedding, or None when it can't be read."""
    try:
        value = json.loads(embedding_json)
    except (TypeError, ValueError):
        return None
    return len(value) if isinstance(value, list) else None


# --------------------------------------------------------------------------
# Write barrier (O0 criterion 2, D1: refuse rather than drain)
# --------------------------------------------------------------------------

class MigrationBusy(RuntimeError):
    """A migration cannot start right now. The message is admin-facing."""


def _owner_tag() -> str:
    return f"{socket.gethostname()}/{os.getpid()}"


def _lock_timeout() -> float:
    return float(config.runtime.index_migration_lock_timeout_s)


def migration_lock_is_live(conn, now: float | None = None) -> bool:
    """Whether a migration lock is held and has not aged out.

    The single definition of that rule: the admin route, the preview, and the
    ingest worker's claim all go through here, so the barrier cannot end up
    meaning one thing to the migration and another to the queue it pauses.
    Takes an open connection because ``claim_next_job`` calls it from inside
    its ``BEGIN IMMEDIATE`` transaction.
    """
    row = conn.execute("SELECT locked_at FROM vector_index_state WHERE id = 1").fetchone()
    if row is None or row["locked_at"] is None:
        return False
    return float(row["locked_at"]) > (now or time.time()) - _lock_timeout()


def migration_lock_state() -> dict[str, object]:
    """Who holds the migration lock, and whether it has gone stale."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT locked_at, locked_by FROM vector_index_state WHERE id = 1"
        ).fetchone()
    if row is None or row["locked_at"] is None:
        return {"locked": False, "owner": "", "age_s": 0.0, "stale": False}
    age = time.time() - float(row["locked_at"])
    return {
        "locked": True,
        "owner": str(row["locked_by"] or ""),
        "age_s": round(age, 1),
        "stale": age > _lock_timeout(),
    }


def acquire_migration_lock() -> str:
    """Take the migration lock, or raise :class:`MigrationBusy`.

    The check-and-set runs inside ``BEGIN IMMEDIATE`` so two admins clicking
    at once cannot both proceed. A running ingest job blocks the migration
    outright (D1): that job is mid-flight with the *old* embedding model, and
    interrupting it safely is more machinery than a single-machine POC needs —
    telling the operator to wait is honest and takes one sentence to explain.

    Queued jobs are deliberately fine: they have not embedded anything yet, so
    once the migration finishes they run against the new model and produce
    correct-width vectors.
    """
    owner = _owner_tag()
    now = time.time()
    stale_before = now - _lock_timeout()
    conn = db.connect()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        held = conn.execute(
            "SELECT locked_at, locked_by FROM vector_index_state WHERE id = 1"
        ).fetchone()
        if held is not None and held["locked_at"] is not None and float(held["locked_at"]) > stale_before:
            conn.execute("ROLLBACK")
            raise MigrationBusy(
                f"另一個維度遷移正在進行中（{held['locked_by']}）。請等它完成後再試。"
            )
        running = conn.execute(
            "SELECT COUNT(*) AS n FROM ingest_jobs WHERE status = 'running'"
        ).fetchone()
        if running is not None and int(running["n"]) > 0:
            conn.execute("ROLLBACK")
            raise MigrationBusy(
                f"目前有 {running['n']} 個攝取工作正在執行，它們仍在使用舊的 embedding 模型。"
                "請等佇列清空（或停掉 worker）後再執行遷移。"
            )
        conn.execute(
            "UPDATE vector_index_state SET locked_at = ?, locked_by = ? WHERE id = 1",
            (now, owner),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    logger.info("index_migration_lock_acquired owner=%s", owner)
    return owner


def release_migration_lock() -> None:
    """Release the lock. Safe to call when not held."""
    with db.connect() as conn:
        conn.execute(
            "UPDATE vector_index_state SET locked_at = NULL, locked_by = '' WHERE id = 1"
        )
        conn.commit()
    logger.info("index_migration_lock_released")


def ingest_queue_snapshot() -> dict[str, int]:
    """Queued/running job counts, for the pre-migration preview."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM ingest_jobs"
            " WHERE status IN ('queued', 'running') GROUP BY status"
        ).fetchall()
    counts = {"queued": 0, "running": 0}
    for row in rows:
        counts[str(row["status"])] = int(row["n"])
    return counts


# --------------------------------------------------------------------------
# Target dimension
# --------------------------------------------------------------------------

def target_dimension_from_diagnostics() -> tuple[int | None, str]:
    """The configured embedding model's width, from the O1 settings probe.

    Deliberately *not* probed here: reading the stored diagnostic forces the
    admin to have run "Test embedding model" on `/settings` against the model
    they intend to migrate to, so the target is a number the endpoint actually
    returned rather than one typed into a form. Returns ``(None, reason)`` when
    that has not happened.
    """
    with db.connect() as conn:
        row = conn.execute("SELECT diagnostics_json FROM llm_settings WHERE id = 1").fetchone()
    try:
        diagnostics = json.loads(row["diagnostics_json"] or "{}") if row else {}
    except (TypeError, ValueError):
        diagnostics = {}
    embedding = diagnostics.get("embedding") if isinstance(diagnostics, dict) else None
    if not isinstance(embedding, dict):
        return None, "尚未在「設定」頁測試 embedding 模型，無法得知目標維度。"
    if embedding.get("status") != "ok":
        return None, "最近一次 embedding 測試未通過，請先在「設定」頁測試成功再遷移。"
    dimension = embedding.get("embedding_dimension")
    if not isinstance(dimension, int) or dimension <= 0:
        return None, "最近一次 embedding 測試沒有回報維度，請重新測試。"
    return dimension, ""


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def run_migration(target_dimension: int) -> dict[str, int]:
    """Replace the collection and reconcile source states. Admin-triggered.

    Ordering matters: the collection is replaced first, then source states are
    written, then reusable vectors are restored. If the process dies between
    the swap and the restore, startup sync repopulates from SQLite for the
    sources still marked ``indexed`` — which are exactly the target-dimension
    ones — so the crash window degrades to "slower", not "wrong".
    """
    from . import vector_store

    owner = acquire_migration_lock()
    try:
        plan = classify_sources(target_dimension)
        removed = vector_store.reset_collection()
        states = apply_source_states(plan)
        restored = vector_store.sync_from_sqlite(
            mode="full", expected_dimension=target_dimension
        )
        result = {
            **plan.summary(),
            "removed_vectors": removed,
            "restored_vectors": restored["upserted"],
            "skipped_vectors": restored["skipped_dimension"],
            **states,
        }
        logger.info("index_migration_completed owner=%s result=%s", owner, result)
        return result
    finally:
        release_migration_lock()
