import asyncio
import importlib


def fresh_modules(monkeypatch, tmp_path):
    """Reload database-related modules against an isolated temporary data dir."""
    monkeypatch.setenv("NOTEBOOKLM_DATA_DIR", str(tmp_path / "data"))
    import app.db as db
    import app.ingest as ingest
    import app.vector_store as vector_store

    importlib.reload(db)
    importlib.reload(vector_store)
    importlib.reload(ingest)
    vector_store.reset_client()
    db.init_db()
    return db, ingest


def test_passwords_are_hashed():
    """Password hashes should not expose plaintext and should verify exactly."""
    from app.security import hash_password, verify_password

    encoded = hash_password("secret")
    assert "secret" not in encoded
    assert verify_password("secret", encoded)
    assert not verify_password("wrong", encoded)


def test_txt_source_ingestion_and_delete_cascades(monkeypatch, tmp_path):
    """TXT ingestion should index chunks and source deletion should remove them."""
    db, ingest = fresh_modules(monkeypatch, tmp_path)
    source_path = tmp_path / "source.txt"
    source_path.write_text("Alpha project revenue is 42 dollars. Beta is unrelated.", encoding="utf-8")

    with db.connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE username = 'user'").fetchone()
        cursor = conn.execute(
            """
            INSERT INTO sources (user_id, filename, stored_path, content_type, status)
            VALUES (?, 'source.txt', ?, 'text/plain', 'uploaded')
            """,
            (user["id"], str(source_path)),
        )
        source_id = cursor.lastrowid

    asyncio.run(ingest.process_source(source_id))

    with db.connect() as conn:
        source = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        chunks = conn.execute("SELECT * FROM chunks WHERE source_id = ?", (source_id,)).fetchall()
        assert source["status"] == "indexed"
        assert len(chunks) == 1
        assert "Alpha project" in chunks[0]["text"]

        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        remaining = conn.execute("SELECT COUNT(*) AS count FROM chunks WHERE source_id = ?", (source_id,)).fetchone()
        assert remaining["count"] == 0


def test_txt_source_ingestion_updates_chroma(monkeypatch, tmp_path):
    """TXT ingestion should write indexed chunks into Chroma for vector search."""
    db, ingest = fresh_modules(monkeypatch, tmp_path)
    from app.llm import local_embedding
    from app.vector_store import query

    source_path = tmp_path / "source.txt"
    source_path.write_text("Azure endpoint setup requires a deployment name.", encoding="utf-8")

    with db.connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE username = 'user'").fetchone()
        source_id = conn.execute(
            """
            INSERT INTO sources (user_id, filename, stored_path, content_type, status)
            VALUES (?, 'source.txt', ?, 'text/plain', 'uploaded')
            """,
            (user["id"], str(source_path)),
        ).lastrowid

    asyncio.run(ingest.process_source(source_id))

    results = query([local_embedding("Azure deployment name")], user["id"], [source_id], n_results=3)

    assert results
    assert results[0]["source_id"] == source_id


def test_user_source_queries_are_isolated(monkeypatch, tmp_path):
    """Chunk queries scoped by user id should not return another user's data."""
    db, _ = fresh_modules(monkeypatch, tmp_path)
    from app.llm import local_embedding

    with db.connect() as conn:
        user_a = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
        user_b = conn.execute("SELECT * FROM users WHERE username = 'user'").fetchone()
        source_a = conn.execute(
            "INSERT INTO sources (user_id, filename, stored_path, status) VALUES (?, 'a.txt', '/tmp/a.txt', 'indexed')",
            (user_a["id"],),
        ).lastrowid
        source_b = conn.execute(
            "INSERT INTO sources (user_id, filename, stored_path, status) VALUES (?, 'b.txt', '/tmp/b.txt', 'indexed')",
            (user_b["id"],),
        ).lastrowid
        conn.execute(
            "INSERT INTO chunks (user_id, source_id, chunk_index, location, text, embedding_json) VALUES (?, ?, 0, 'document', 'admin secret', ?)",
            (user_a["id"], source_a, db.dumps(local_embedding("admin secret"))),
        )
        conn.execute(
            "INSERT INTO chunks (user_id, source_id, chunk_index, location, text, embedding_json) VALUES (?, ?, 0, 'document', 'user secret', ?)",
            (user_b["id"], source_b, db.dumps(local_embedding("user secret"))),
        )

        rows = conn.execute(
            """
            SELECT chunks.*, sources.filename
            FROM chunks JOIN sources ON sources.id = chunks.source_id
            WHERE chunks.user_id = ? AND sources.status = 'indexed'
            """,
            (user_a["id"],),
        ).fetchall()

    assert len(rows) == 1
    assert rows[0]["text"] == "admin secret"


def test_retrieve_prefers_keyword_and_vector_relevant_chunks():
    """Hybrid retrieval should surface exact terms before unrelated chunks."""
    from app.db import dumps
    from app.llm import local_embedding
    from app.main import retrieve

    rows = [
        {
            "id": 1,
            "source_id": 1,
            "filename": "azure.md",
            "location": "document",
            "text": "The api_version parameter controls the Azure OpenAI API version.",
            "embedding_json": dumps(local_embedding("The api_version parameter controls the Azure OpenAI API version.")),
        },
        {
            "id": 2,
            "source_id": 2,
            "filename": "finance.md",
            "location": "document",
            "text": "Quarterly revenue and margin are reported in the finance appendix.",
            "embedding_json": dumps(local_embedding("Quarterly revenue and margin are reported in the finance appendix.")),
        },
    ]

    chunks = asyncio.run(retrieve("api_version setting", rows, {}))

    assert chunks
    assert chunks[0]["filename"] == "azure.md"


def test_keyword_score_supports_cjk_terms():
    """Keyword scoring should handle Chinese text without whitespace."""
    from app.main import keyword_score

    score = keyword_score(["Azure OpenAI 端點設定"], "這份文件說明 Azure OpenAI endpoint 的端點設定方式。")

    assert score > 0
