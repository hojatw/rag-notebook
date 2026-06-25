"""Tests for the smart Chroma sync.

These touch the real Chroma persistent client by pointing it at a temp data
dir per test (via the fresh_modules fixture), so they exercise the
integration end-to-end.
"""
import asyncio


def seed_one_indexed_source(db, ingest, tmp_path, text="Alpha project revenue is 42 dollars."):
    """Helper: create a SQLite source row, ingest it, return the source_id."""
    source_path = tmp_path / "src.txt"
    source_path.write_text(text, encoding="utf-8")
    with db.connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE username = 'user'").fetchone()
        source_id = conn.execute(
            "INSERT INTO sources (user_id, filename, stored_path, content_type, status)"
            " VALUES (?, 'src.txt', ?, 'text/plain', 'uploaded')",
            (user["id"], str(source_path)),
        ).lastrowid
    asyncio.run(ingest.process_source(source_id))
    return source_id


def test_index_status_reports_in_sync_after_ingest(fresh_modules, local_embed, tmp_path):
    """After a clean ingest, index_status reports zero drift in both directions."""
    db, ingest, vs = fresh_modules.db, fresh_modules.ingest, fresh_modules.vector_store
    seed_one_indexed_source(db, ingest, tmp_path)

    status = vs.index_status()

    assert status["chroma_available"] is True
    assert status["sqlite_chunks"] > 0
    assert status["sqlite_chunks"] == status["chroma_chunks"]
    assert status["missing_in_chroma"] == 0
    assert status["orphan_in_chroma"] == 0
    assert status["in_sync"] is True


def test_index_status_detects_orphans(fresh_modules, local_embed, tmp_path):
    """A row deleted directly from SQLite (skipping the cascade) appears as an orphan."""
    db, ingest, vs = fresh_modules.db, fresh_modules.ingest, fresh_modules.vector_store
    source_id = seed_one_indexed_source(db, ingest, tmp_path)
    # Delete the chunks from SQLite WITHOUT removing the Chroma vectors —
    # the kind of drift that could happen if a manual DB edit slipped through.
    with db.connect() as conn:
        conn.execute("DELETE FROM chunks WHERE source_id = ?", (source_id,))

    status = vs.index_status()

    assert status["sqlite_chunks"] == 0
    assert status["chroma_chunks"] > 0
    assert status["orphan_in_chroma"] > 0
    assert status["in_sync"] is False


def test_index_status_detects_missing(fresh_modules, local_embed, tmp_path):
    """Wiping Chroma but keeping SQLite reports the entire delta as missing."""
    db, ingest, vs = fresh_modules.db, fresh_modules.ingest, fresh_modules.vector_store
    seed_one_indexed_source(db, ingest, tmp_path)
    vs.clear_all_vectors()

    status = vs.index_status()

    assert status["chroma_chunks"] == 0
    assert status["missing_in_chroma"] == status["sqlite_chunks"]
    assert status["in_sync"] is False


def test_diff_sync_is_no_op_when_aligned(fresh_modules, local_embed, tmp_path):
    """When everything matches, diff mode performs zero upserts and zero deletes."""
    db, ingest, vs = fresh_modules.db, fresh_modules.ingest, fresh_modules.vector_store
    seed_one_indexed_source(db, ingest, tmp_path)

    result = vs.sync_from_sqlite(mode="diff")

    assert result == {"upserted": 0, "deleted": 0}
    assert vs.index_status()["in_sync"] is True


def test_diff_sync_repairs_missing_and_orphan(fresh_modules, local_embed, tmp_path):
    """Diff mode upserts missing chunks and deletes orphan vectors in one pass."""
    db, ingest, vs = fresh_modules.db, fresh_modules.ingest, fresh_modules.vector_store
    source_id = seed_one_indexed_source(db, ingest, tmp_path)

    # Create drift in both directions:
    #   - inject an orphan into Chroma
    #   - remove a chunk from Chroma to simulate "missing"
    chunks_in_db = db.connect().execute("SELECT id FROM chunks WHERE source_id = ?", (source_id,)).fetchall()
    real_id = chunks_in_db[0]["id"]
    vs.collection().upsert(
        ids=["chunk:99999"],
        embeddings=[[0.0] * 384],
        documents=["orphan"],
        metadatas=[{"chunk_id": 99999, "user_id": 1, "source_id": 9999, "chunk_index": 0, "filename": "orphan.txt", "location": "document"}],
    )
    vs.collection().delete(ids=[vs.vector_id(real_id)])

    pre = vs.index_status()
    assert pre["missing_in_chroma"] >= 1
    assert pre["orphan_in_chroma"] >= 1

    result = vs.sync_from_sqlite(mode="diff")

    assert result["upserted"] >= 1
    assert result["deleted"] >= 1
    assert vs.index_status()["in_sync"] is True


def test_full_sync_reupserts_everything(fresh_modules, local_embed, tmp_path):
    """Full mode re-upserts every SQLite chunk regardless of current Chroma state."""
    db, ingest, vs = fresh_modules.db, fresh_modules.ingest, fresh_modules.vector_store
    seed_one_indexed_source(db, ingest, tmp_path)
    expected = vs.index_status()["sqlite_chunks"]

    result = vs.sync_from_sqlite(mode="full")

    assert result["upserted"] == expected
    assert result["deleted"] == 0


def test_clear_all_vectors_removes_everything(fresh_modules, local_embed, tmp_path):
    """clear_all_vectors empties Chroma but leaves SQLite intact."""
    db, ingest, vs = fresh_modules.db, fresh_modules.ingest, fresh_modules.vector_store
    seed_one_indexed_source(db, ingest, tmp_path)
    before = vs.index_status()["chroma_chunks"]

    removed = vs.clear_all_vectors()

    assert removed == before
    assert vs.index_status()["chroma_chunks"] == 0
    # SQLite chunks untouched
    assert vs.index_status()["sqlite_chunks"] == before


def test_probe_index_dimension_reports_dimension(fresh_modules, local_embed, tmp_path):
    """A healthy, populated index reports its locked-in dimension as readable."""
    db, ingest, vs = fresh_modules.db, fresh_modules.ingest, fresh_modules.vector_store
    seed_one_indexed_source(db, ingest, tmp_path)

    probe = vs.probe_index_dimension()

    assert probe["readable"] is True
    assert probe["dimension"] == 384  # local_embed stand-in is 384-dim
    assert vs.current_dimension() == 384


def test_probe_index_dimension_survives_unreadable_index(fresh_modules, local_embed, tmp_path, monkeypatch):
    """A corrupt on-disk index degrades gracefully instead of raising.

    Mirrors the customer 500: the embedding endpoint is fine, but reading one
    stored vector raises ``InternalError: ... hnsw segment reader: Nothing
    found on disk``. probe_index_dimension must catch it and report the index
    as unreadable so /settings and /admin/index don't 500.
    """
    vs = fresh_modules.vector_store

    class _BrokenCollection:
        def get(self, *args, **kwargs):
            raise RuntimeError("Error creating hnsw segment reader: Nothing found on disk")

    monkeypatch.setattr(vs, "collection", lambda: _BrokenCollection())

    probe = vs.probe_index_dimension()

    assert probe == {"dimension": None, "readable": False}
    # Back-compat wrapper must not raise either.
    assert vs.current_dimension() is None
