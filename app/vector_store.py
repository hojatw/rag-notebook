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


def sync_from_sqlite(batch_size: int = 500) -> None:
    """Backfill Chroma from existing SQLite chunks."""
    if not chroma_available():
        logger.warning("vector_sync_skipped reason=chroma_unavailable")
        return
    started = time.perf_counter()
    synced = 0
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT chunks.*, sources.filename
            FROM chunks JOIN sources ON sources.id = chunks.source_id
            WHERE sources.status = 'indexed'
            ORDER BY chunks.id
            """
        ).fetchall()
    for start in range(0, len(rows), batch_size):
        batch = []
        for row in rows[start : start + batch_size]:
            batch.append(
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
            )
        upsert_chunks(batch)
        synced += len(batch)
    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info("vector_sync_completed chunks=%s elapsed_ms=%.1f", synced, elapsed_ms)
