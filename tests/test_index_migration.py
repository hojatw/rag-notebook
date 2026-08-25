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


# -------------------- write barrier (O0 criterion 2, D1) --------------------

def test_migration_is_refused_while_an_ingest_job_runs(fresh_modules):
    """D1: a running job is mid-flight with the OLD model, so refuse outright.

    Draining it safely is more machinery than a single-machine POC needs;
    telling the operator to wait is honest and explains itself in a sentence.
    """
    import pytest
    import app.index_migration as im

    db = fresh_modules.db
    source_id = _seed_source(db, filename="a.txt", status="processing", dimension=384)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO ingest_jobs (source_id, status, claimed_at) VALUES (?, 'running', ?)",
            (source_id, 1.0),
        )
        conn.commit()

    with pytest.raises(im.MigrationBusy, match="攝取工作"):
        im.acquire_migration_lock()

    assert im.migration_lock_state()["locked"] is False   # nothing was left held


def test_queued_jobs_do_not_block_a_migration(fresh_modules):
    """A queued job hasn't embedded anything yet — it runs after, on the new model."""
    import app.index_migration as im

    db = fresh_modules.db
    source_id = _seed_source(db, filename="a.txt", status="uploaded", dimension=384)
    with db.connect() as conn:
        conn.execute("INSERT INTO ingest_jobs (source_id, status) VALUES (?, 'queued')", (source_id,))
        conn.commit()

    im.acquire_migration_lock()

    assert im.migration_lock_state()["locked"] is True
    im.release_migration_lock()


def test_second_migration_is_refused_while_one_holds_the_lock(fresh_modules):
    import pytest
    import app.index_migration as im

    im.acquire_migration_lock()
    try:
        with pytest.raises(im.MigrationBusy, match="進行中"):
            im.acquire_migration_lock()
    finally:
        im.release_migration_lock()


def test_worker_will_not_claim_jobs_during_a_migration(fresh_modules):
    """**The barrier's whole point.** No upsert may land mid-swap.

    A claim here would ingest → upsert → recreate the collection at the old
    dimension between reset_collection's delete and its recreate, silently
    undoing the migration.
    """
    import app.index_migration as im
    import app.jobs as jobs

    db = fresh_modules.db
    source_id = _seed_source(db, filename="a.txt", status="uploaded", dimension=384)
    jobs.enqueue_source(source_id)
    assert jobs.claim_next_job() is not None      # claimable when unlocked

    with db.connect() as conn:                    # put it back
        conn.execute("UPDATE ingest_jobs SET status = 'queued', claimed_at = NULL")
        conn.commit()
    im.acquire_migration_lock()
    try:
        assert jobs.claim_next_job() is None      # paused
    finally:
        im.release_migration_lock()
    assert jobs.claim_next_job() is not None      # resumes afterwards


def test_a_stale_lock_does_not_wedge_the_queue_forever(fresh_modules, monkeypatch):
    """A process that died mid-migration must not stop ingest permanently."""
    import app.index_migration as im
    import app.jobs as jobs

    db = fresh_modules.db
    source_id = _seed_source(db, filename="a.txt", status="uploaded", dimension=384)
    jobs.enqueue_source(source_id)
    im.acquire_migration_lock()
    # Patch through the module's own `config` reference: other suites reload
    # the app graph, after which `app.config.config` can be a different object
    # than the one this module closed over.
    monkeypatch.setattr(im.config.runtime, "index_migration_lock_timeout_s", -1.0)

    assert im.migration_lock_state()["stale"] is True
    assert jobs.claim_next_job() is not None      # the queue moves again

    # Once that job finishes, a fresh migration takes the stale lock over
    # instead of being blocked by the dead process's leftovers.
    with db.connect() as conn:
        conn.execute("UPDATE ingest_jobs SET status = 'done'")
        conn.commit()
    im.acquire_migration_lock()
    im.release_migration_lock()


# -------------------- target dimension --------------------

def test_target_dimension_requires_a_successful_embedding_test(fresh_modules):
    """The target must be a width the endpoint actually returned, not typed in."""
    import json as json_module
    import app.index_migration as im

    db = fresh_modules.db
    assert im.target_dimension_from_diagnostics() == (None, "尚未在「設定」頁測試 embedding 模型，無法得知目標維度。")

    def _store(payload):
        with db.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO llm_settings (id) VALUES (1)")
            conn.execute(
                "UPDATE llm_settings SET diagnostics_json = ? WHERE id = 1",
                (json_module.dumps({"embedding": payload}),),
            )
            conn.commit()

    # Spell the status through the constant the probe writes, not a literal.
    # These assertions used to hardcode "ok", a value nothing ever stored, so
    # they passed while the real gate was permanently shut.
    from app.llm import DIAGNOSTIC_STATUS_FAILED, DIAGNOSTIC_STATUS_SUCCEEDED

    _store({"status": DIAGNOSTIC_STATUS_FAILED, "embedding_dimension": 1536})
    assert im.target_dimension_from_diagnostics()[0] is None      # a failed probe is not a target

    _store({"status": DIAGNOSTIC_STATUS_SUCCEEDED, "embedding_dimension": None})
    assert im.target_dimension_from_diagnostics()[0] is None      # ok but no number

    _store({"status": DIAGNOSTIC_STATUS_SUCCEEDED, "embedding_dimension": 1536})
    assert im.target_dimension_from_diagnostics() == (1536, "")


def test_probe_output_actually_opens_the_migration_gate(fresh_modules, monkeypatch):
    """Pin the writer/reader contract end to end, not each side's assumption.

    The regression this guards was invisible to per-side tests: the probe wrote
    `"succeeded"`, the gate compared to `"ok"`, and the gate's own test
    fabricated `"ok"` — so every test passed while the migration flow could
    never be reached. Drive a real probe result through the real storage path
    and assert the gate opens.
    """
    import asyncio
    import json as json_module

    import app.index_migration as im
    from app import llm

    async def fake_post(url, headers, payload, timeout, retry_stats=None):
        return {"data": [{"embedding": [0.0] * 1024}], "usage": {"total_tokens": 3}}

    monkeypatch.setattr(llm, "_post_json_with_retry", fake_post)
    monkeypatch.setattr(llm, "_record_usage_event", lambda **kwargs: None)

    # The real probe decides the status string; nothing here spells it out.
    result = asyncio.run(llm.probe_embedding_diagnostics({
        "provider": "openai_compatible",
        "base_url": "https://example.invalid/v1",
        "embedding_model": "test-embed",
    }))
    assert result["embedding_dimension"] == 1024

    with fresh_modules.db.connect() as conn:
        conn.execute("INSERT OR IGNORE INTO llm_settings (id) VALUES (1)")
        conn.execute(
            "UPDATE llm_settings SET diagnostics_json = ? WHERE id = 1",
            (json_module.dumps({"embedding": result}),),
        )
        conn.commit()

    assert im.target_dimension_from_diagnostics() == (1024, "")


# -------------------- end-to-end --------------------

def test_run_migration_swaps_the_collection_and_reconciles_sources(fresh_modules):
    """384 → 1536 end to end, with a mix of reusable and stale sources."""
    import app.index_migration as im

    db, vs = fresh_modules.db, fresh_modules.vector_store
    old = _seed_source(db, filename="old.txt", status="indexed", dimension=384, chunks=2)
    new = _seed_source(db, filename="new.txt", status="indexed", dimension=1536, chunks=3)
    vs.sync_from_sqlite(mode="full")               # locks the collection at 384
    assert vs.probe_index_dimension()["dimension"] == 384

    result = im.run_migration(1536)

    assert result["stale"] == 1
    assert result["restored_vectors"] == 3         # only the 1536 source comes back
    assert vs.probe_index_dimension()["dimension"] == 1536
    assert _status_of(db, old)["status"] == im.STALE_STATUS
    assert _status_of(db, new)["status"] == "indexed"
    assert im.migration_lock_state()["locked"] is False   # lock always released

    # And the migrated index survives a routine startup sync unchanged.
    after = vs.sync_from_sqlite(mode="diff")
    assert after == {"upserted": 0, "deleted": 0, "skipped_dimension": 0}
    assert vs.probe_index_dimension()["dimension"] == 1536


def test_run_migration_releases_the_lock_when_it_fails(fresh_modules, monkeypatch):
    """A crash mid-migration must not leave the ingest queue paused."""
    import pytest
    import app.index_migration as im

    db = fresh_modules.db
    _seed_source(db, filename="a.txt", status="indexed", dimension=384)
    monkeypatch.setattr(
        fresh_modules.vector_store, "reset_collection",
        lambda: (_ for _ in ()).throw(RuntimeError("chroma exploded")),
    )

    with pytest.raises(RuntimeError, match="chroma exploded"):
        im.run_migration(1536)

    assert im.migration_lock_state()["locked"] is False


# -------------------- split-worker operating model (O0 criterion 4) --------------------

def _second_process_vector_store():
    """Load an independent copy of app.vector_store, standing in for the worker.

    A standalone ``python -m app.worker`` has its own ``_client`` /
    ``_collection`` / ``_collection_generation`` globals while sharing the same
    SQLite file and Chroma directory. Executing the module a second time
    reproduces exactly that: separate module state, shared on-disk state. (Its
    ``from . import db`` still resolves to the one already-imported ``app.db``,
    so both halves agree on the data dir — same as two real processes reading
    the same ``NOTEBOOKLM_DATA_DIR``.)
    """
    import importlib.util

    spec = importlib.util.find_spec("app.vector_store")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_split_worker_does_not_write_through_a_handle_from_before_the_migration(fresh_modules):
    """O0 criterion 4, split-worker model: 384 → migrate → 1536 across processes.

    The web process migrates while the worker process is holding a collection
    handle it fetched earlier. Without the generation counter the worker would
    keep upserting into the deleted collection — the failure the single-process
    tests cannot reproduce.
    """
    import app.index_migration as im

    db, web = fresh_modules.db, fresh_modules.vector_store
    worker = _second_process_vector_store()

    _seed_source(db, filename="old.txt", status="indexed", dimension=384, chunks=2)
    web.sync_from_sqlite(mode="full")
    stale_handle = worker.collection()                 # worker warms its cache at 384
    assert worker.probe_index_dimension()["dimension"] == 384

    im.run_migration(1536)                             # web process migrates

    # The worker must notice and re-fetch before its next write.
    assert worker.collection() is not stale_handle
    worker.upsert_chunks([{
        "id": 501, "user_id": 1, "source_id": 1, "chunk_index": 0,
        "filename": "new.txt", "location": "p1", "text": "new", "embedding": [0.1] * 1536,
    }])

    # Both halves agree on the migrated index.
    assert worker.probe_index_dimension()["dimension"] == 1536
    assert web.probe_index_dimension()["dimension"] == 1536
    assert web.collection().count() == 1


def test_split_worker_and_web_share_one_generation_counter(fresh_modules):
    """Either process resetting invalidates the other — the barrier is symmetric."""
    db, web = fresh_modules.db, fresh_modules.vector_store
    worker = _second_process_vector_store()

    _seed_source(db, filename="a.txt", status="indexed", dimension=384, chunks=1)
    web.sync_from_sqlite(mode="full")
    web_handle, worker_handle = web.collection(), worker.collection()
    assert web.index_generation() == worker.index_generation()

    worker.reset_collection()                          # this time the *worker* resets

    assert web.index_generation() == worker.index_generation()
    assert web.collection() is not web_handle          # the web process refreshes too
    assert worker.collection() is not worker_handle


def test_inline_worker_model_migrates_end_to_end(fresh_modules):
    """O0 criterion 4, inline-worker model: one process, ingest re-runs after.

    The default single-machine deployment. After the migration the queued
    source re-ingests through the normal path and lands at the new width.
    """
    import app.index_migration as im
    import app.jobs as jobs

    db, vs = fresh_modules.db, fresh_modules.vector_store
    old = _seed_source(db, filename="old.txt", status="indexed", dimension=384, chunks=2)
    vs.sync_from_sqlite(mode="full")
    assert vs.probe_index_dimension()["dimension"] == 384

    im.run_migration(1536)

    assert _status_of(db, old)["status"] == im.STALE_STATUS
    assert vs.probe_index_dimension()["dimension"] is None   # empty, and unlocked

    # Reindex is the recovery path: the queue is claimable again and the
    # re-ingested source writes at the new width.
    jobs.enqueue_source(old)
    assert jobs.claim_next_job() is not None
    vs.upsert_chunks([{
        "id": 601, "user_id": 1, "source_id": old, "chunk_index": 0,
        "filename": "old.txt", "location": "p1", "text": "re-embedded", "embedding": [0.1] * 1536,
    }])
    assert vs.probe_index_dimension()["dimension"] == 1536
