#!/usr/bin/env python3
"""Temporary operator workaround for a Chroma collection dimension change.

The current admin Clear action deletes vector records but keeps the Chroma
collection and its locked embedding dimension. This script replaces that
collection safely while preserving SQLite, uploads, notebooks, and messages.

Dry-run is the default. A real change requires both ``--apply`` and
``--services-stopped`` because the web app and ingest worker cache Chroma
collection handles and must not write during the migration.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_COLLECTION = "rag_chunks"
DIMENSION_MISMATCH_FRAGMENT = "collection expecting embedding with dimension"


@dataclass(frozen=True)
class SourceState:
    source_id: int
    filename: str
    status: str
    error: str
    chunk_count: int
    min_dimension: int | None
    max_dimension: int | None

    @property
    def single_dimension(self) -> int | None:
        if self.chunk_count and self.min_dimension == self.max_dimension:
            return self.min_dimension
        return None


@dataclass(frozen=True)
class MigrationPlan:
    target_dimension: int
    reusable_source_ids: tuple[int, ...]
    recoverable_failure_ids: tuple[int, ...]
    mark_for_reindex_ids: tuple[int, ...]
    unchanged_source_ids: tuple[int, ...]
    sources: tuple[SourceState, ...]


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _readonly_connection(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def inspect_sources(db_path: Path, target_dimension: int) -> MigrationPlan:
    """Classify SQLite sources without changing any state."""
    with _readonly_connection(db_path) as connection:
        rows = connection.execute(
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
    mark_for_reindex: list[int] = []
    unchanged: list[int] = []
    for source in sources:
        is_target_dimension = source.single_dimension == target_dimension
        failed_on_dimension_mismatch = (
            source.status == "failed"
            and DIMENSION_MISMATCH_FRAGMENT in source.error.lower()
        )
        if source.status == "indexed" and is_target_dimension:
            reusable.append(source.source_id)
        elif failed_on_dimension_mismatch and is_target_dimension:
            reusable.append(source.source_id)
            recoverable.append(source.source_id)
        elif source.status == "indexed":
            # Indexed rows at another (or mixed/unknown) dimension would be
            # replayed by startup sync and immediately re-lock the collection.
            mark_for_reindex.append(source.source_id)
        else:
            unchanged.append(source.source_id)

    return MigrationPlan(
        target_dimension=target_dimension,
        reusable_source_ids=tuple(reusable),
        recoverable_failure_ids=tuple(recoverable),
        mark_for_reindex_ids=tuple(mark_for_reindex),
        unchanged_source_ids=tuple(unchanged),
        sources=sources,
    )


def _collection_names(client: Any) -> set[str]:
    return {
        item.name if hasattr(item, "name") else str(item)
        for item in client.list_collections()
    }


def inspect_collection(data_dir: Path, collection_name: str) -> tuple[bool, int, int | None]:
    """Return collection existence, vector count, and visible vector dimension."""
    import chromadb
    from chromadb.config import Settings

    vector_dir = data_dir / "chroma"
    if not vector_dir.is_dir():
        # Keep dry-run genuinely non-mutating on a deployment with no index yet.
        return False, 0, None
    client = chromadb.PersistentClient(
        path=str(vector_dir),
        settings=Settings(anonymized_telemetry=False),
    )
    if collection_name not in _collection_names(client):
        return False, 0, None
    collection = client.get_collection(name=collection_name)
    count = int(collection.count())
    if not count:
        # Chroma can retain a locked schema even though no embedding is visible.
        return True, 0, None
    result = collection.get(limit=1, include=["embeddings"])
    embeddings = result.get("embeddings")
    dimension = len(embeddings[0]) if embeddings is not None and len(embeddings) else None
    return True, count, dimension


def create_backup(data_dir: Path, backup_dir: Path) -> Path:
    """Archive SQLite and Chroma state before applying the workaround."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_dir / f"chroma-dimension-reset-{timestamp}.tar.gz"
    candidates = (
        data_dir / "app.sqlite3",
        data_dir / "app.sqlite3-wal",
        data_dir / "app.sqlite3-shm",
        data_dir / "chroma",
    )
    with tarfile.open(backup_path, "x:gz") as archive:
        for path in candidates:
            if path.exists():
                archive.add(path, arcname=path.name, recursive=True)
    return backup_path


def _iter_chunk_batches(
    db_path: Path,
    source_ids: tuple[int, ...],
    target_dimension: int,
    batch_size: int,
) -> Iterable[list[dict[str, Any]]]:
    if not source_ids:
        return
    placeholders = ",".join("?" for _ in source_ids)
    with _readonly_connection(db_path) as connection:
        cursor = connection.execute(
            f"""
            SELECT chunks.*, sources.filename
            FROM chunks
            JOIN sources ON sources.id = chunks.source_id
            WHERE chunks.source_id IN ({placeholders})
            ORDER BY chunks.id
            """,
            source_ids,
        )
        while rows := cursor.fetchmany(batch_size):
            payload: list[dict[str, Any]] = []
            for row in rows:
                embedding = json.loads(row["embedding_json"])
                if len(embedding) != target_dimension:
                    raise RuntimeError(
                        f"chunk {row['id']} changed during migration: "
                        f"expected {target_dimension} dimensions, got {len(embedding)}"
                    )
                payload.append(
                    {
                        "id": f"chunk:{int(row['id'])}",
                        "embedding": embedding,
                        "document": str(row["text"]),
                        "metadata": {
                            "chunk_id": int(row["id"]),
                            "user_id": int(row["user_id"]),
                            "source_id": int(row["source_id"]),
                            "chunk_index": int(row["chunk_index"]),
                            "filename": str(row["filename"]),
                            "location": str(row["location"]),
                        },
                    }
                )
            yield payload


def _update_source_states(db_path: Path, plan: MigrationPlan) -> None:
    with sqlite3.connect(db_path) as connection:
        if plan.recoverable_failure_ids:
            placeholders = ",".join("?" for _ in plan.recoverable_failure_ids)
            connection.execute(
                f"""
                UPDATE sources
                SET status = 'indexed', error = '', updated_at = CURRENT_TIMESTAMP
                WHERE id IN ({placeholders})
                """,
                plan.recoverable_failure_ids,
            )
        if plan.mark_for_reindex_ids:
            placeholders = ",".join("?" for _ in plan.mark_for_reindex_ids)
            message = (
                f"Embedding dimension migration to {plan.target_dimension} requires Reindex. "
                "The previous chunks were preserved for operator recovery."
            )
            connection.execute(
                f"""
                UPDATE sources
                SET status = 'failed', error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id IN ({placeholders})
                """,
                (message, *plan.mark_for_reindex_ids),
            )


def apply_migration(
    data_dir: Path,
    collection_name: str,
    plan: MigrationPlan,
    backup_dir: Path,
    batch_size: int,
) -> tuple[Path, int, int | None]:
    """Back up state, replace the collection, and restore safe target vectors."""
    import chromadb
    from chromadb.config import Settings

    backup_path = create_backup(data_dir, backup_dir)
    client = chromadb.PersistentClient(
        path=str(data_dir / "chroma"),
        settings=Settings(anonymized_telemetry=False),
    )
    if collection_name in _collection_names(client):
        client.delete_collection(name=collection_name)
    collection = client.get_or_create_collection(
        name=collection_name,
        metadata={"hnsw:space": "cosine"},
    )

    restored = 0
    for batch in _iter_chunk_batches(
        data_dir / "app.sqlite3",
        plan.reusable_source_ids,
        plan.target_dimension,
        batch_size,
    ):
        collection.upsert(
            ids=[item["id"] for item in batch],
            embeddings=[item["embedding"] for item in batch],
            documents=[item["document"] for item in batch],
            metadatas=[item["metadata"] for item in batch],
        )
        restored += len(batch)

    _update_source_states(data_dir / "app.sqlite3", plan)
    count = int(collection.count())
    if count != restored:
        raise RuntimeError(f"verification failed: restored {restored} vectors but collection reports {count}")
    dimension: int | None = None
    if count:
        probe = collection.get(limit=1, include=["embeddings"])
        embeddings = probe.get("embeddings")
        dimension = len(embeddings[0]) if embeddings is not None and len(embeddings) else None
        if dimension != plan.target_dimension:
            raise RuntimeError(
                f"verification failed: expected {plan.target_dimension} dimensions, got {dimension}"
            )
    return backup_path, restored, dimension


def _print_plan(
    data_dir: Path,
    collection_name: str,
    plan: MigrationPlan,
    collection_state: tuple[bool, int, int | None],
) -> None:
    exists, count, visible_dimension = collection_state
    print(f"Data directory: {data_dir}")
    print(f"Collection: {collection_name} (exists={exists}, vectors={count}, visible_dimension={visible_dimension})")
    if exists and count == 0:
        print("Note: an empty Chroma collection may still retain its previous locked dimension.")
    print(f"Target dimension: {plan.target_dimension}")
    print(f"Reusable source ids: {list(plan.reusable_source_ids)}")
    print(f"Recoverable dimension-failure ids: {list(plan.recoverable_failure_ids)}")
    print(f"Sources that will require Reindex: {list(plan.mark_for_reindex_ids)}")
    print(f"Unchanged source ids: {list(plan.unchanged_source_ids)}")
    for source in plan.sources:
        dimension = source.single_dimension
        if source.chunk_count and dimension is None:
            dimension_text = f"mixed:{source.min_dimension}-{source.max_dimension}"
        else:
            dimension_text = str(dimension) if dimension is not None else "none"
        print(
            f"  source={source.source_id} status={source.status} chunks={source.chunk_count} "
            f"dimension={dimension_text} filename={source.filename!r}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("NOTEBOOKLM_DATA_DIR", "data")),
        help="runtime data directory containing app.sqlite3 and chroma/ (default: NOTEBOOKLM_DATA_DIR or data)",
    )
    parser.add_argument("--target-dimension", required=True, type=_positive_int)
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--backup-dir", type=Path, help="backup destination (default: DATA_DIR/backups)")
    parser.add_argument("--batch-size", type=_positive_int, default=500)
    parser.add_argument("--apply", action="store_true", help="perform the migration; otherwise report dry-run only")
    parser.add_argument(
        "--services-stopped",
        action="store_true",
        help="required acknowledgement that every app and worker process using this data directory is stopped",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    data_dir = args.data_dir.expanduser().resolve()
    db_path = data_dir / "app.sqlite3"
    if not db_path.is_file():
        parser.error(f"SQLite database not found: {db_path}")
    if args.apply and not args.services_stopped:
        parser.error("--apply requires --services-stopped")

    plan = inspect_sources(db_path, args.target_dimension)
    collection_state = inspect_collection(data_dir, args.collection)
    _print_plan(data_dir, args.collection, plan, collection_state)
    if not args.apply:
        print("DRY RUN only. Stop app/worker, then add --apply --services-stopped to make changes.")
        return 0
    if collection_state[1] and collection_state[2] == args.target_dimension:
        parser.error(
            "the populated collection already has the target dimension; this tool does not "
            "re-embed same-dimension model or prefix changes. Use source Reindex instead."
        )

    backup_dir = (args.backup_dir or data_dir / "backups").expanduser().resolve()
    backup_path, restored, dimension = apply_migration(
        data_dir,
        args.collection,
        plan,
        backup_dir,
        args.batch_size,
    )
    print(f"Backup: {backup_path}")
    print(f"Restored vectors: {restored}")
    print(f"New visible dimension: {dimension}")
    print("Migration complete. Restart app/worker and Reindex every source listed above as requiring Reindex.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted; services remain stopped until the operator restarts them.", file=sys.stderr)
        raise SystemExit(130)
