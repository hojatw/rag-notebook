"""Single source of truth for the app version / build identifier.

Why this exists: when a customer reports a bug (e.g. a 500), we need to tie the
report back to an exact build. The semantic version lives in the repo-root
``VERSION`` file; the git commit is resolved at runtime so no build step is
required for local runs, while Docker images can bake the commit in via the
``NOTEBOOKLM_GIT_SHA`` build arg.

Resolution is cached (the answer can't change within a process) and never
raises — a missing VERSION file or absent ``.git`` degrades to a placeholder
rather than breaking startup.
"""
import os
import subprocess
from functools import lru_cache
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def app_version() -> str:
    """Return the semantic version string.

    ``NOTEBOOKLM_VERSION`` env wins (lets a release pipeline stamp it); else the
    repo-root ``VERSION`` file; else ``0+unknown``.
    """
    env = os.environ.get("NOTEBOOKLM_VERSION")
    if env and env.strip():
        return env.strip()
    try:
        text = (_ROOT / "VERSION").read_text(encoding="utf-8").strip()
        return text or "0+unknown"
    except OSError:
        return "0+unknown"


@lru_cache(maxsize=1)
def git_revision() -> str:
    """Return the short git commit, best-effort.

    Order: ``NOTEBOOKLM_GIT_SHA`` env (set at Docker build, where ``.git`` is
    absent) → ``git rev-parse`` against the working tree (local dev) →
    ``unknown``.
    """
    env = os.environ.get("NOTEBOOKLM_GIT_SHA")
    if env and env.strip():
        return env.strip()[:12]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


@lru_cache(maxsize=1)
def build_label() -> str:
    """Human-readable ``vX.Y.Z (sha)`` for the footer, health endpoint, logs."""
    return f"v{app_version()} ({git_revision()})"
