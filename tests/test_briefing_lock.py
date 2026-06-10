"""Briefing lock helper unit tests (P2-3: SQLite-backed, cross-process)."""
import importlib
import time

import pytest


@pytest.fixture
def lock_env(monkeypatch, tmp_path):
    """Fresh temp DB with one notebook; returns (main module, notebook_id)."""
    monkeypatch.setenv("NOTEBOOKLM_DATA_DIR", str(tmp_path / "data"))
    import app.db as db
    import app.main as main

    for module in (db, main):
        importlib.reload(module)
    db.init_db()

    with db.connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
        notebook_id = conn.execute(
            "INSERT INTO notebooks (user_id, title) VALUES (?, 'NB')",
            (user["id"],),
        ).lastrowid
    return main, notebook_id


def test_briefing_lock_acquires_and_releases(lock_env):
    main, nid = lock_env

    assert not main._briefing_locked(nid)
    assert main._acquire_briefing_lock(nid)
    assert main._briefing_locked(nid)
    # A second acquire while held must fail (dedupe overlapping POSTs).
    assert not main._acquire_briefing_lock(nid)
    main._release_briefing_lock(nid)
    assert not main._briefing_locked(nid)
    # Releasing an unheld lock is a no-op (idempotent).
    main._release_briefing_lock(nid)
    # ...and it can be taken again afterwards.
    assert main._acquire_briefing_lock(nid)


def test_briefing_lock_expires_after_timeout(lock_env):
    """Stale lock past BRIEFING_LOCK_TIMEOUT_S must be treated as released."""
    main, nid = lock_env
    import app.db as db

    assert main._acquire_briefing_lock(nid)
    # Simulate a crashed holder by backdating the row past the timeout.
    with db.connect() as conn:
        conn.execute(
            "UPDATE briefing_locks SET acquired_at = ? WHERE notebook_id = ?",
            (time.time() - (main.BRIEFING_LOCK_TIMEOUT_S + 1), nid),
        )
    # Reads free and the stale row is reclaimed so generation can proceed.
    assert not main._briefing_locked(nid)
    assert main._acquire_briefing_lock(nid)
