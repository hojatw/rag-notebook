"""DB-backed ingest queue unit tests (P1-1)."""
import importlib
import time

import pytest


@pytest.fixture
def jobs_env(monkeypatch, tmp_path):
    """Fresh temp DB with one source; returns (jobs module, db module, source_id)."""
    monkeypatch.setenv("NOTEBOOKLM_DATA_DIR", str(tmp_path / "data"))
    import app.db as db
    import app.jobs as jobs

    for module in (db, jobs):
        importlib.reload(module)
    db.init_db()

    with db.connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
        source_id = conn.execute(
            "INSERT INTO sources (user_id, filename, stored_path, status) "
            "VALUES (?, 'a.txt', '/tmp/a.txt', 'uploaded')",
            (user["id"],),
        ).lastrowid
    return jobs, db, source_id


def _job_row(db, source_id):
    with db.connect() as conn:
        return conn.execute(
            "SELECT * FROM ingest_jobs WHERE source_id = ?", (source_id,)
        ).fetchone()


def test_enqueue_is_idempotent_per_source(jobs_env):
    jobs, db, source_id = jobs_env

    jobs.enqueue_source(source_id)
    jobs.enqueue_source(source_id)  # reindex — must not create a second row

    with db.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM ingest_jobs WHERE source_id = ?", (source_id,)
        ).fetchone()["n"]
    assert count == 1
    row = _job_row(db, source_id)
    assert row["status"] == "queued"
    assert row["attempts"] == 0


def test_claim_is_atomic_and_fifo(jobs_env):
    jobs, db, source_id = jobs_env
    jobs.enqueue_source(source_id)

    first = jobs.claim_next_job()
    assert first is not None
    assert first["source_id"] == source_id
    assert first["attempts"] == 1
    assert _job_row(db, source_id)["status"] == "running"

    # Nothing else runnable: a second worker gets None (no double-claim).
    assert jobs.claim_next_job() is None


def test_claim_reclaims_stale_running_job(jobs_env):
    jobs, db, source_id = jobs_env
    jobs.enqueue_source(source_id)
    claimed = jobs.claim_next_job()

    # Fresh running job is NOT re-claimable.
    assert jobs.claim_next_job() is None

    # Backdate the claim past the visibility timeout → crashed worker.
    with db.connect() as conn:
        conn.execute(
            "UPDATE ingest_jobs SET claimed_at = ? WHERE id = ?",
            (time.time() - jobs.JOB_VISIBILITY_TIMEOUT_S - 1, claimed["id"]),
        )
    reclaimed = jobs.claim_next_job()
    assert reclaimed is not None
    assert reclaimed["id"] == claimed["id"]
    assert reclaimed["attempts"] == 2  # attempt counter advanced


def test_mark_done_only_acts_on_running(jobs_env):
    jobs, db, source_id = jobs_env
    jobs.enqueue_source(source_id)
    claimed = jobs.claim_next_job()

    jobs.mark_done(claimed["id"])
    assert _job_row(db, source_id)["status"] == "done"

    # A reindex flips it back to queued while a stale worker later calls
    # mark_done — the guard must not clobber the new queued job.
    jobs.enqueue_source(source_id)
    assert _job_row(db, source_id)["status"] == "queued"
    jobs.mark_done(claimed["id"])
    assert _job_row(db, source_id)["status"] == "queued"


def test_requeue_then_permanent_fail_marks_source(jobs_env):
    jobs, db, source_id = jobs_env
    with db.connect() as conn:
        conn.execute("UPDATE sources SET status = 'processing' WHERE id = ?", (source_id,))
    jobs.enqueue_source(source_id)

    # attempts below the cap → retried (re-queued).
    claimed = jobs.claim_next_job()  # attempts = 1
    assert jobs.requeue_or_fail(claimed["id"], source_id, claimed["attempts"], "boom") is True
    assert _job_row(db, source_id)["status"] == "queued"

    # Drive attempts to the cap → permanent failure flips job and source.
    last = None
    for _ in range(jobs.JOB_MAX_ATTEMPTS):
        last = jobs.claim_next_job()
        if last["attempts"] >= jobs.JOB_MAX_ATTEMPTS:
            break
        jobs.requeue_or_fail(last["id"], source_id, last["attempts"], "boom")

    assert jobs.requeue_or_fail(last["id"], source_id, last["attempts"], "boom again") is False
    job = _job_row(db, source_id)
    assert job["status"] == "failed"
    with db.connect() as conn:
        src = conn.execute("SELECT status, error FROM sources WHERE id = ?", (source_id,)).fetchone()
    assert src["status"] == "failed"
    assert "boom again" in src["error"]
