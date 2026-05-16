import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def fresh_modules(monkeypatch, tmp_path):
    """Reload db / vector_store / ingest against an isolated temp data dir.

    Returns a SimpleNamespace with .db, .ingest, .vector_store so callers
    can pick what they need. The Chroma client cache is reset and the schema
    is initialised before the fixture yields.
    """
    monkeypatch.setenv("NOTEBOOKLM_DATA_DIR", str(tmp_path / "data"))
    import app.db as db
    import app.ingest as ingest
    import app.vector_store as vector_store

    for module in (db, vector_store, ingest):
        importlib.reload(module)
    vector_store.reset_client()
    db.init_db()
    return SimpleNamespace(db=db, ingest=ingest, vector_store=vector_store)
