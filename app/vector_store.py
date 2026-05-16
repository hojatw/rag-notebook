import logging
import time
from typing import Any

from . import db


COLLECTION_NAME = "rag_chunks"
logger = logging.getLogger(__name__)
_client = None
_collection = None


def chroma_available() -> bool:
    """Return whether Chroma can be imported in the current environment."""
    try:
        import chromadb  # noqa: F401
    except Exception:
        return False
    return True


def reset_client() -> None:
    """Clear cached Chroma client objects for tests or data-dir changes."""
    global _client, _collection
    _client = None
    _collection = None


def collection():
    """Return the persistent Chroma collection for source chunks."""
    global _client, _collection
    if _collection is not None:
        return _collection
    import chromadb
    from chromadb.config import Settings

    vector_dir = db.DATA_DIR / "chroma"
    vector_dir.mkdir(parents=True, exist_ok=True)
    _client = chromadb.PersistentClient(path=str(vector_dir), settings=Settings(anonymized_telemetry=False))
    _collection = _client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )
    return _collection


def vector_id(chunk_id: int) -> str:
    """Build a stable Chroma id for a SQLite chunk id."""
    return f"chunk:{chunk_id}"


def upsert_chunks(chunks: list[dict[str, Any]]) -> None:
    """Upsert chunk embeddings and metadata into Chroma."""
    if not chunks:
        return
    started = time.perf_counter()
    col = collection()
    col.upsert(
        ids=[vector_id(int(chunk["id"])) for chunk in chunks],
        embeddings=[chunk["embedding"] for chunk in chunks],
        documents=[chunk["text"] for chunk in chunks],
        metadatas=[
            {
                "chunk_id": int(chunk["id"]),
                "user_id": int(chunk["user_id"]),
                "source_id": int(chunk["source_id"]),
                "chunk_index": int(chunk["chunk_index"]),
                "filename": chunk["filename"],
                "location": chunk["location"],
            }
            for chunk in chunks
        ],
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info("vector_upsert_completed chunks=%s elapsed_ms=%.1f", len(chunks), elapsed_ms)


def delete_source(source_id: int, user_id: int | None = None) -> None:
    """Delete all vectors for a source, optionally scoped by user."""
    col = collection()
    where: dict[str, Any] = {"source_id": int(source_id)}
    if user_id is not None:
        where = {"$and": [{"source_id": int(source_id)}, {"user_id": int(user_id)}]}
    col.delete(where=where)
    logger.info("vector_source_deleted source_id=%s user_id=%s", source_id, user_id)


def build_where(user_id: int, source_ids: list[int] | None = None) -> dict[str, Any]:
    """Build a Chroma metadata filter for user and optional sources."""
    if source_ids:
        return {
            "$and": [
                {"user_id": int(user_id)},
                {"source_id": {"$in": [int(source_id) for source_id in source_ids]}},
            ]
        }
    return {"user_id": int(user_id)}


def query(
    query_embeddings: list[list[float]],
    user_id: int,
    source_ids: list[int] | None = None,
    n_results: int = 20,
) -> list[dict[str, Any]]:
    """Query Chroma vectors and return de-duplicated candidate chunks."""
    if not query_embeddings:
        return []
    started = time.perf_counter()
    col = collection()
    results = col.query(
        query_embeddings=query_embeddings,
        n_results=n_results,
        where=build_where(user_id, source_ids),
        include=["documents", "metadatas", "distances"],
    )
    candidates: dict[int, dict[str, Any]] = {}
    for ids, documents, metadatas, distances in zip(
        results.get("ids", []),
        results.get("documents", []),
        results.get("metadatas", []),
        results.get("distances", []),
    ):
        for item_id, document, metadata, distance in zip(ids, documents, metadatas, distances):
            chunk_id = int(metadata["chunk_id"])
            vector_score = max(0.0, 1.0 - float(distance))
            existing = candidates.get(chunk_id)
            if existing and existing["vector_score"] >= vector_score:
                continue
            candidates[chunk_id] = {
                "id": chunk_id,
                "source_id": int(metadata["source_id"]),
                "filename": metadata["filename"],
                "location": metadata["location"],
                "text": document,
                "vector_score": vector_score,
                "vector_id": item_id,
            }
    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "vector_query_completed queries=%s candidates=%s elapsed_ms=%.1f",
        len(query_embeddings),
        len(candidates),
        elapsed_ms,
    )
    return sorted(candidates.values(), key=lambda item: item["vector_score"], reverse=True)


def _indexed_chunk_ids() -> set[int]:
    """Return SQLite chunk ids for every indexed source. Cheap: id-only scan."""
    with db.connect() as conn:
        return {row["id"] for row in conn.execute(
            "SELECT chunks.id FROM chunks JOIN sources ON sources.id = chunks.source_id"
            " WHERE sources.status = 'indexed'"
        ).fetchall()}


def _indexed_chunk_rows(chunk_ids: set[int] | None = None) -> list[Any]:
    """Fetch chunk rows joined with their source filename.

    When ``chunk_ids`` is provided, only those chunks are loaded — used by
    diff sync to skip pulling the embedding_json blob for already-synced
    rows. When None, every indexed chunk is returned (used by full sync).
    """
    with db.connect() as conn:
        if chunk_ids is None:
            return conn.execute(
                "SELECT chunks.*, sources.filename"
                " FROM chunks JOIN sources ON sources.id = chunks.source_id"
                " WHERE sources.status = 'indexed' ORDER BY chunks.id"
            ).fetchall()
        if not chunk_ids:
            return []
        placeholders = ",".join("?" for _ in chunk_ids)
        return conn.execute(
            f"SELECT chunks.*, sources.filename"
            f" FROM chunks JOIN sources ON sources.id = chunks.source_id"
            f" WHERE chunks.id IN ({placeholders}) ORDER BY chunks.id",
            tuple(chunk_ids),
        ).fetchall()


def _chroma_ids() -> set[str]:
    """Return the set of vector ids currently stored in the Chroma collection."""
    if not chroma_available():
        return set()
    return set(collection().get(include=[])["ids"])


def current_dimension() -> int | None:
    """Return the dimensionality the Chroma collection is locked to, if any.

    Chroma fixes the vector dim on the first upsert; subsequent upserts
    with a different length raise. We peek one stored vector and report its
    length, which lets ``/settings`` reject embedding-model changes that
    would break the existing index instead of failing at first ingest.
    Returns ``None`` when the collection is empty (any dim is still legal)
    or when Chroma is unavailable.
    """
    if not chroma_available():
        return None
    first = collection().get(limit=1, include=["embeddings"])
    embeddings = first.get("embeddings")
    # Chroma sometimes returns a numpy array; len() works on both.
    if embeddings is None or len(embeddings) == 0:
        return None
    return len(embeddings[0])


def index_status() -> dict[str, Any]:
    """Compare SQLite indexed chunks against the Chroma collection.

    Returns a dict suitable for the admin index page:
        sqlite_chunks      total SQLite chunks belonging to indexed sources
        chroma_chunks      total vectors currently in Chroma
        missing_in_chroma  count of SQLite chunks not yet upserted
        orphan_in_chroma   count of Chroma vectors with no matching SQLite chunk
        in_sync            True iff both counts above are zero
        dimension          locked-in vector dim, or None when collection empty
        chroma_available   False when chromadb cannot be imported
    """
    if not chroma_available():
        return {
            "chroma_available": False,
            "sqlite_chunks": 0,
            "chroma_chunks": 0,
            "missing_in_chroma": 0,
            "orphan_in_chroma": 0,
            "in_sync": False,
            "dimension": None,
        }
    chroma_ids = _chroma_ids()
    sqlite_ids = {vector_id(cid) for cid in _indexed_chunk_ids()}
    missing = sqlite_ids - chroma_ids
    orphans = chroma_ids - sqlite_ids
    return {
        "chroma_available": True,
        "sqlite_chunks": len(sqlite_ids),
        "chroma_chunks": len(chroma_ids),
        "missing_in_chroma": len(missing),
        "orphan_in_chroma": len(orphans),
        "in_sync": not missing and not orphans,
        "dimension": current_dimension(),
    }


def clear_all_vectors() -> int:
    """Delete every vector from the collection. Returns the number removed."""
    if not chroma_available():
        return 0
    col = collection()
    ids = list(col.get(include=[])["ids"])
    if ids:
        col.delete(ids=ids)
    logger.info("vector_clear_all_completed count=%s", len(ids))
    return len(ids)


def sync_from_sqlite(batch_size: int = 500, mode: str = "diff") -> dict[str, int]:
    """Reconcile the Chroma collection with SQLite chunks.

    Modes:
        diff (default, fast)
            Upsert only SQLite chunks missing from Chroma; delete Chroma
            vectors whose chunk id is no longer in SQLite. This is what
            startup uses — it short-circuits to zero work when in sync.
        full (slow, repair)
            Re-upsert every SQLite chunk regardless of current Chroma state.
            Use from the admin "Rebuild index" button when drift is
            suspected or after restoring from a backup.

    Returns ``{"upserted": int, "deleted": int}``.
    """
    if not chroma_available():
        logger.warning("vector_sync_skipped reason=chroma_unavailable")
        return {"upserted": 0, "deleted": 0}
    started = time.perf_counter()
    col = collection()

    sqlite_chunk_ids = _indexed_chunk_ids()
    if mode == "diff":
        chroma_ids = _chroma_ids()
        # Pull full rows (with embeddings) only for chunks Chroma is missing.
        # Aligned-state startups skip the heavy SELECT entirely.
        pending_chunk_ids = {cid for cid in sqlite_chunk_ids if vector_id(cid) not in chroma_ids}
        sqlite_vector_ids = {vector_id(cid) for cid in sqlite_chunk_ids}
        orphan_ids = list(chroma_ids - sqlite_vector_ids)
        pending_rows = _indexed_chunk_rows(pending_chunk_ids)
    elif mode == "full":
        pending_rows = _indexed_chunk_rows()
        orphan_ids = []
    else:
        raise ValueError(f"Unknown sync mode: {mode!r} (expected 'diff' or 'full')")

    if orphan_ids:
        col.delete(ids=orphan_ids)
        logger.info("vector_orphans_deleted count=%s", len(orphan_ids))

    upserted = 0
    for start in range(0, len(pending_rows), batch_size):
        batch = [
            {
                "id": row["id"],
                "user_id": row["user_id"],
                "source_id": row["source_id"],
                "chunk_index": row["chunk_index"],
                "filename": row["filename"],
                "location": row["location"],
                "text": row["text"],
                "embedding": db.loads(row["embedding_json"]),
            }
            for row in pending_rows[start : start + batch_size]
        ]
        upsert_chunks(batch)
        upserted += len(batch)

    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "vector_sync_completed mode=%s upserted=%s deleted=%s sqlite_chunks=%s elapsed_ms=%.1f",
        mode, upserted, len(orphan_ids), len(sqlite_chunk_ids), elapsed_ms,
    )
    return {"upserted": upserted, "deleted": len(orphan_ids)}
