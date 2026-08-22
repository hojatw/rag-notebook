"""O0 Phase B — source classification and the startup-sync dimension guard.

The scenario under test is the one that makes O0 dangerous rather than merely
annoying: the collection has been reset and is unlocked, SQLite still holds
old-dimension vectors, and a routine startup sync would replay them and lock
the collection right back at the old width — with no error anywhere.
"""
import json


def _seed_source(db, *, filename, status, dimension, chunks=2, error=""):
    """Insert a source plus `chunks` chunk rows whose embeddings are `dimension` wide."""
    with db.connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE username = 'user'").fetchone()
        source_id = conn.execute(
            "INSERT INTO sources (user_id, filename, stored_path, content_type, status, error)"
            " VALUES (?, ?, '/tmp/x', 'text/plain', ?, ?)",
            (user["id"], filename, status, error),
        ).lastrowid
        for i in range(chunks):
            conn.execute(
                "INSERT INTO chunks (user_id, source_id, chunk_index, location, text, embedding_json)"
                " VALUES (?, ?, ?, 'p1', ?, ?)",
                (user["id"], source_id, i, f"{filename} chunk {i}", json.dumps([0.1] * dimension)),
            )
        conn.commit()
    return source_id


def _status_of(db, source_id):
    with db.connect() as conn:
        return conn.execute("SELECT status, error FROM sources WHERE id = ?", (source_id,)).fetchone()


# -------------------- classification --------------------

def test_classify_splits_reusable_from_stale(fresh_modules):
    """Only same-dimension indexed sources survive a migration untouched."""
    import app.index_migration as im

    db = fresh_modules.db
    keep = _seed_source(db, filename="keep.txt", status="indexed", dimension=1536)
    stale = _seed_source(db, filename="old.txt", status="indexed", dimension=384)
    pending = _seed_source(db, filename="new.txt", status="uploaded", dimension=1536)

    plan = im.classify_sources(1536)

    assert plan.reusable_source_ids == (keep,)
    assert plan.mark_stale_ids == (stale,)
    assert plan.unchanged_source_ids == (pending,)
    assert plan.summary() == {
        "target_dimension": 1536, "reusable": 1, "recovered": 0, "stale": 1, "unchanged": 1,
    }


def test_classify_recovers_sources_that_only_failed_on_the_lock(fresh_modules):
    """A source at the target width that failed *because* of the lock comes back.

    Its vectors were always correct — the collection just refused them. After
    the reset there is nothing left to fix, so re-embedding it would be pure
    wasted LLM spend.
    """
    import app.index_migration as im

    db = fresh_modules.db
    blocked = _seed_source(
        db, filename="blocked.txt", status="failed", dimension=1536,
        error="Collection expecting embedding with dimension of 384, got 1536",
    )
    unrelated = _seed_source(
        db, filename="broken.pdf", status="failed", dimension=1536, error="PDF has no extractable text",
    )

    plan = im.classify_sources(1536)

    assert plan.recoverable_failure_ids == (blocked,)
    assert blocked in plan.reusable_source_ids
    assert unrelated in plan.unchanged_source_ids   # a real failure stays failed


def test_classify_treats_mixed_dimension_sources_as_stale(fresh_modules):
    """A half-reindexed source has no single width and must not be trusted."""
    import app.index_migration as im

    db = fresh_modules.db
    mixed = _seed_source(db, filename="mixed.txt", status="indexed", dimension=1536, chunks=1)
    with db.connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE username = 'user'").fetchone()
        conn.execute(
            "INSERT INTO chunks (user_id, source_id, chunk_index, location, text, embedding_json)"
            " VALUES (?, ?, 9, 'p9', 'leftover', ?)",
            (user["id"], mixed, json.dumps([0.1] * 384)),
        )
        conn.commit()

    plan = im.classify_sources(1536)

    assert plan.mark_stale_ids == (mixed,)
    assert plan.reusable_source_ids == ()


def test_apply_source_states_marks_stale_and_restores_recovered(fresh_modules):
    import app.index_migration as im

    db = fresh_modules.db
    stale = _seed_source(db, filename="old.txt", status="indexed", dimension=384)
    blocked = _seed_source(
        db, filename="blocked.txt", status="failed", dimension=1536,
        error="Collection expecting embedding with dimension of 384, got 1536",
    )

    result = im.apply_source_states(im.classify_sources(1536))

    assert result == {"recovered": 1, "stale": 1}
    assert _status_of(db, stale)["status"] == im.STALE_STATUS
    assert "重新索引" in _status_of(db, stale)["error"]      # the row explains itself
    assert _status_of(db, blocked)["status"] == "indexed"
    assert _status_of(db, blocked)["error"] == ""
    assert im.stale_source_count() == 1


# -------------------- the startup-sync guard (O0 criterion 3) --------------------

def test_startup_sync_does_not_relock_a_reset_collection(fresh_modules):
    """**The criterion-3 test.** A replay must not re-lock the fresh collection.

    Collection reset to unlocked, SQLite still full of 384-dim vectors, and the
    deployment has moved to a 1536-dim model. A plain sync would upsert the old
    vectors, Chroma would lock at 384, and the next real ingest would fail with
    the original O0 error — after the migration supposedly fixed it.
    """
    db, vs = fresh_modules.db, fresh_modules.vector_store
    _seed_source(db, filename="old.txt", status="indexed", dimension=384)
    vs.reset_collection()

    result = vs.sync_from_sqlite(mode="full", expected_dimension=1536)

    assert result["upserted"] == 0
    assert result["skipped_dimension"] == 2
    # The collection is still unlocked, so the new model can move in.
    assert vs.probe_index_dimension()["dimension"] is None
    vs.upsert_chunks([{
        "id": 999, "user_id": 1, "source_id": 1, "chunk_index": 0,
        "filename": "new.txt", "location": "p1", "text": "new",
        "embedding": [0.1] * 1536,
    }])
    assert vs.probe_index_dimension()["dimension"] == 1536


def test_sync_skips_mismatched_chunks_against_a_populated_collection(fresh_modules):
    """With vectors present the collection's own width is the target."""
    db, vs = fresh_modules.db, fresh_modules.vector_store
    _seed_source(db, filename="good.txt", status="indexed", dimension=384, chunks=2)
    vs.sync_from_sqlite(mode="full")           # locks the collection at 384
    _seed_source(db, filename="wrong.txt", status="indexed", dimension=1536, chunks=3)

    result = vs.sync_from_sqlite(mode="full")

    assert result["skipped_dimension"] == 3    # the 1536 source is refused
    assert result["upserted"] == 2             # the 384 source still syncs
    assert vs.probe_index_dimension()["dimension"] == 384


def test_stale_sources_are_excluded_from_sync_entirely(fresh_modules):
    """Marking a source stale takes it out of the `status = 'indexed'` set.

    This is the belt to the dimension guard's braces: the guard catches stray
    rows, but a migrated deployment should not be offering them to sync at all.
    """
    import app.index_migration as im

    db, vs = fresh_modules.db, fresh_modules.vector_store
    _seed_source(db, filename="old.txt", status="indexed", dimension=384)
    vs.reset_collection()
    im.apply_source_states(im.classify_sources(1536))

    result = vs.sync_from_sqlite(mode="full", expected_dimension=1536)

    assert result["upserted"] == 0
    assert result["skipped_dimension"] == 0    # never even offered
    assert vs.index_status()["sqlite_chunks"] == 0


def test_sync_without_a_target_dimension_keeps_old_behaviour(fresh_modules):
    """No collection dimension and no hint: filtering must not kick in."""
    db, vs = fresh_modules.db, fresh_modules.vector_store
    _seed_source(db, filename="a.txt", status="indexed", dimension=384, chunks=2)

    result = vs.sync_from_sqlite(mode="full")

    assert result["upserted"] == 2
    assert result["skipped_dimension"] == 0
