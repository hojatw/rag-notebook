"""Tests for the smart Chroma sync.

These touch the real Chroma persistent client by pointing it at a temp data
dir per test (via the fresh_modules fixture), so they exercise the
integration end-to-end.
"""
import asyncio
import pathlib

import pytest


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

    assert result == {"upserted": 0, "deleted": 0, "skipped_dimension": 0}
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


def _chunk(chunk_id: int, dimension: int) -> dict:
    """Build one upsert-shaped chunk with an embedding of the given dimension."""
    return {
        "id": chunk_id,
        "user_id": 1,
        "source_id": 1,
        "chunk_index": 0,
        "filename": "src.txt",
        "location": "p1",
        "text": f"chunk {chunk_id}",
        "embedding": [0.1] * dimension,
    }


def test_clear_all_vectors_leaves_the_dimension_locked(fresh_modules):
    """O0 regression witness: this is the defect, pinned so it can't be lost.

    Deleting every record empties the collection but Chroma keeps its schema,
    so a different dimension is still rejected. probe_index_dimension reports
    None (nothing to measure), which is exactly why /settings wrongly accepts
    the new model. If Chroma ever changes this, the test fails loudly and O0's
    premise needs re-checking.
    """
    vs = fresh_modules.vector_store
    vs.upsert_chunks([_chunk(1, 384)])

    assert vs.clear_all_vectors() == 1
    assert vs.probe_index_dimension()["dimension"] is None  # looks unlocked...

    with pytest.raises(Exception) as excinfo:  # ...but is not
        vs.upsert_chunks([_chunk(2, 1536)])
    assert "dimension" in str(excinfo.value).lower()


def test_reset_collection_releases_the_locked_dimension(fresh_modules):
    """O0 criterion 1: replacing the collection lets a new dimension in."""
    vs = fresh_modules.vector_store
    vs.upsert_chunks([_chunk(1, 384)])

    assert vs.reset_collection() == 1

    vs.upsert_chunks([_chunk(2, 1536)])
    assert vs.probe_index_dimension()["dimension"] == 1536


def test_reset_collection_recovers_an_already_broken_index(fresh_modules):
    """The realistic entry point: an operator already pressed Clear.

    The collection is empty but still locked, which is the state a deployment
    is actually found in. reset_collection has to repair it, not just prevent
    it.
    """
    vs = fresh_modules.vector_store
    vs.upsert_chunks([_chunk(1, 384)])
    vs.clear_all_vectors()

    assert vs.reset_collection() == 0  # nothing left to discard

    vs.upsert_chunks([_chunk(2, 1536)])
    assert vs.probe_index_dimension()["dimension"] == 1536


def test_reset_collection_bumps_the_shared_generation(fresh_modules):
    """The generation counter is what other processes watch."""
    vs = fresh_modules.vector_store
    before = vs.index_generation()

    vs.reset_collection()

    assert vs.index_generation() == before + 1


def test_stale_collection_handle_is_refreshed_after_another_process_resets(fresh_modules):
    """O0 criterion 2: a cached handle from before a reset must not be reused.

    Stands in for the split app/worker deployment: the web process replaces the
    collection while the ingest worker still holds a handle to the deleted one.
    We simulate the worker by restoring its stale handle and generation, then
    assert the next collection() call hands back a live object that can be
    written through.
    """
    vs = fresh_modules.vector_store
    vs.upsert_chunks([_chunk(1, 384)])
    worker_handle = vs.collection()
    worker_generation = vs._collection_generation

    # The "web process" migrates the collection to a new dimension.
    vs.reset_collection()
    vs.upsert_chunks([_chunk(2, 1536)])

    # The "worker process" still has its pre-reset cache.
    vs._collection = worker_handle
    vs._collection_generation = worker_generation

    refreshed = vs.collection()

    assert refreshed is not worker_handle
    assert vs._collection_generation != worker_generation
    # The refreshed handle writes at the new dimension without raising.
    vs.upsert_chunks([_chunk(3, 1536)])
    assert vs.probe_index_dimension()["dimension"] == 1536


def test_init_db_adds_the_state_table_and_preserves_the_generation(fresh_modules):
    """Upgrade path: existing deployments have no vector_index_state row yet.

    init_db() runs on every startup, so it must both create the table on an
    older database and leave an already-advanced generation alone — reseeding
    it to 0 each boot would make every restart look like a reset to the other
    processes watching it.
    """
    db, vs = fresh_modules.db, fresh_modules.vector_store

    # Simulate a database created before this table existed.
    with db.connect() as conn:
        conn.execute("DROP TABLE vector_index_state")
        conn.commit()
    assert vs.index_generation() == 0  # missing table degrades to "never reset"

    db.init_db()
    assert vs.index_generation() == 0

    vs.reset_collection()
    advanced = vs.index_generation()
    assert advanced == 1

    db.init_db()  # a later restart must not reseed
    assert vs.index_generation() == advanced


# -------------------- cross-process index coherence (the 2026-09-10 outage) --------------------

_WORKER_UPSERT = """
import os, sys
sys.path.insert(0, {repo!r})
os.environ["NOTEBOOKLM_DATA_DIR"] = {data_dir!r}
import app.db, app.vector_store as vs
app.db.init_db()
vs.upsert_chunks([
    {{"id": 900 + i, "user_id": 1, "source_id": 96, "chunk_index": i,
      "filename": "worker.txt", "location": "p1", "text": "worker chunk %d" % i,
      "embedding": [0.01 * (i % 7), 0.02, 0.03] + [0.0] * 381}}
    for i in range(5)
])
print("worker-upserted", vs.collection().count())
"""


def _run_worker_process(tmp_path, data_dir):
    """Upsert from a genuinely separate OS process.

    Reloading the module in-process (what test_index_migration does) gives
    separate Python globals but the SAME cached chromadb ``System`` for the
    store path -- and that System owns the in-memory HNSW index. So an
    in-process stand-in shares the very state whose staleness is the bug, and
    cannot observe it. Only a real subprocess can.
    """
    import subprocess
    import sys

    script = _WORKER_UPSERT.format(repo=str(pathlib.Path(__file__).resolve().parents[1]),
                                   data_dir=str(data_dir))
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            cwd=str(tmp_path), timeout=180)
    assert result.returncode == 0, f"worker process failed:\n{result.stdout}\n{result.stderr}"
    return result.stdout


def test_query_sees_records_written_by_another_process(fresh_modules, tmp_path):
    """The 2026-09-10 production failure, reproduced end-to-end.

    Timeline from logs/app.log: the web container deleted a source's vectors,
    the standalone ingest worker then upserted a newly uploaded source, and
    every subsequent query in the web container died with
    ``InternalError: Error executing plan: Internal error: Error finding id``
    -- for two days, because nothing invalidated the web process's cached
    client. Six of the window's thirteen questions were served from the
    degraded SQLite fallback and three users never got a vector-backed answer.

    Asserting on the query result rather than on the counter is deliberate: an
    earlier version of this mechanism re-fetched the collection handle from the
    same client, which advances every counter you could assert on and still
    leaves the stale index in place.
    """
    db, vs = fresh_modules.db, fresh_modules.vector_store
    data_dir = db.DATA_DIR

    vs.upsert_chunks([
        {"id": i, "user_id": 1, "source_id": 95, "chunk_index": i,
         "filename": "web.txt", "location": "p1", "text": f"web chunk {i}",
         "embedding": [0.01 * i, 0.02, 0.03] + [0.0] * 381}
        for i in range(1, 6)
    ])
    # Warm this process's client, then delete -- exactly the production order.
    assert vs.query([[0.03, 0.02, 0.03] + [0.0] * 381], user_id=1)
    vs.delete_source(95, user_id=1)

    _run_worker_process(tmp_path, data_dir)

    # The web process must serve the worker's records, not raise on a stale index.
    hits = vs.query([[0.03, 0.02, 0.03] + [0.0] * 381], user_id=1, n_results=10)
    assert [hit["source_id"] for hit in hits] == [96] * len(hits)
    assert len(hits) == 5


def test_a_process_does_not_reopen_its_client_for_its_own_writes(fresh_modules):
    """Own writes must not churn the client, or every upsert pays a reopen.

    ``bump_write_seq`` advances this process's marker with the counter, so the
    handle stays valid. Without that, the reopen that fixes the cross-process
    bug would fire on the single-process path too -- on every single query.
    """
    vs = fresh_modules.vector_store

    handle = vs.collection()
    vs.upsert_chunks([{
        "id": 1, "user_id": 1, "source_id": 1, "chunk_index": 0,
        "filename": "a.txt", "location": "p1", "text": "a", "embedding": [0.1] * 384,
    }])
    assert vs.index_write_seq() == 1
    assert vs.collection() is handle

    vs.delete_source(1, user_id=1)
    assert vs.index_write_seq() == 2
    assert vs.collection() is handle


def test_every_chroma_mutation_advances_the_write_counter(fresh_modules):
    """A mutation the counter misses is a mutation other processes never see."""
    vs = fresh_modules.vector_store

    def _chunk(chunk_id):
        return {"id": chunk_id, "user_id": 1, "source_id": 1, "chunk_index": 0,
                "filename": "a.txt", "location": "p1", "text": "a", "embedding": [0.1] * 384}

    assert vs.index_write_seq() == 0
    vs.upsert_chunks([_chunk(1)])
    assert vs.index_write_seq() == 1
    vs.upsert_chunks([])                 # no-op writes nothing, so it must not bump
    assert vs.index_write_seq() == 1
    vs.delete_source(1, user_id=1)
    assert vs.index_write_seq() == 2
    vs.upsert_chunks([_chunk(2)])
    assert vs.index_write_seq() == 3
    vs.clear_all_vectors()
    assert vs.index_write_seq() == 4
    vs.clear_all_vectors()               # nothing left to delete, so no bump
    assert vs.index_write_seq() == 4


def test_init_db_adds_the_write_counter_to_an_older_database(fresh_modules):
    """Upgrade path: the column arrives on a database that predates it."""
    db, vs = fresh_modules.db, fresh_modules.vector_store

    with db.connect() as conn:
        conn.execute("DROP TABLE vector_index_state")
        conn.execute("CREATE TABLE vector_index_state ("
                     "id INTEGER PRIMARY KEY CHECK (id = 1), generation INTEGER NOT NULL DEFAULT 0)")
        conn.execute("INSERT INTO vector_index_state (id, generation) VALUES (1, 4)")
        conn.commit()
    assert vs.index_write_seq() == 0     # missing column degrades to "no writes yet"

    db.init_db()
    assert vs.index_write_seq() == 0
    assert vs.index_generation() == 4    # the migration must not disturb O0's counter
