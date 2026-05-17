import hashlib
import importlib
import math
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("NOTEBOOKLM_SECRET", "test-secret-do-not-use-in-prod")


def local_embedding(text: str, dimensions: int = 384) -> list[float]:
    """Deterministic SHA-256-bag-of-tokens embedder for tests.

    Lives in conftest (not app.llm) because production code now refuses to
    operate without a real embedding API — there is no legitimate runtime
    use for a hash-based embedder. Tests still want a stable embedder that
    doesn't hit the network, so we keep one here.
    """
    vector = [0.0] * dimensions
    tokens = [t for t in "".join(c.lower() if c.isalnum() else " " for c in text).split() if t]
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] % 2 else -1.0
        vector[bucket] += sign
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


@pytest.fixture
def local_embed(monkeypatch):
    """Patch the embedding function that ingest and retrieve look up so both
    can run in tests without a configured LLM. Real production code raises
    when settings are missing — this stand-in returns a deterministic local
    hash embedding instead, suitable for asserting retrieval/indexing logic
    but NOT for asserting real model output."""
    async def fake_embed(texts, settings):
        return [local_embedding(t) for t in texts]
    # ingest.py and main.py each bind ``embed_texts`` at import time, so we
    # patch them where they look it up rather than at app.llm.
    import app.ingest
    import app.main
    monkeypatch.setattr(app.ingest, "embed_texts", fake_embed)
    monkeypatch.setattr(app.main, "embed_texts", fake_embed)
    return fake_embed


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
