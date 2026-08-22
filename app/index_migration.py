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
from dataclasses import dataclass

from . import db


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
