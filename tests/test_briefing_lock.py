"""Briefing lock helper unit tests."""
import time


def test_briefing_lock_acquires_and_releases():
    from app.main import _acquire_briefing_lock, _release_briefing_lock, _briefing_locked, _briefing_in_progress

    _briefing_in_progress.clear()
    nid = 12345
    assert not _briefing_locked(nid)
    assert _acquire_briefing_lock(nid)
    assert _briefing_locked(nid)
    # A second acquire while held must fail.
    assert not _acquire_briefing_lock(nid)
    _release_briefing_lock(nid)
    assert not _briefing_locked(nid)
    # Releasing an unheld lock is a no-op (idempotent).
    _release_briefing_lock(nid)


def test_briefing_lock_expires_after_timeout():
    """Stale lock past BRIEFING_LOCK_TIMEOUT_S must be treated as released."""
    from app import main
    from app.main import _briefing_locked, _briefing_in_progress, BRIEFING_LOCK_TIMEOUT_S

    _briefing_in_progress.clear()
    nid = 67890
    _briefing_in_progress[nid] = time.time() - (BRIEFING_LOCK_TIMEOUT_S + 1)
    assert not _briefing_locked(nid)
    assert nid not in _briefing_in_progress
