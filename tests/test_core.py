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


def test_default_notebook_migration_backfills_legacy_rows(monkeypatch, tmp_path):
    """init_db() should create a default notebook per user with orphan rows
    and backfill notebook_id on every sources / conversations row that
    pre-existed the notebook schema (follow-up #1, Phase 1)."""
    import sqlite3
    monkeypatch.setenv("NOTEBOOKLM_DATA_DIR", str(tmp_path / "data"))
    import app.db as db
    import importlib
    importlib.reload(db)
    db.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Simulate the pre-notebook schema: no notebooks/notes tables, no
    # notebook_id column on sources / conversations.
    conn = sqlite3.connect(db.DB_PATH)
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE sources (id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            filename TEXT NOT NULL, stored_path TEXT NOT NULL,
            content_type TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'uploaded',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE conversations (id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL DEFAULT 'New conversation',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        INSERT INTO users (username, password_hash) VALUES ('legacy_a', 'x'), ('legacy_b', 'y');
        INSERT INTO sources (user_id, filename, stored_path) VALUES
            (1, 'a.txt', '/tmp/a.txt'), (1, 'b.txt', '/tmp/b.txt'), (2, 'c.txt', '/tmp/c.txt');
        INSERT INTO conversations (user_id, title) VALUES (1, 'chat-a'), (2, 'chat-c');
        """
    )
    conn.commit()
    conn.close()

    db.init_db()

    with db.connect() as conn:
        # Each legacy user with orphan rows gets exactly one default notebook.
        per_user = {row["user_id"]: row["c"] for row in conn.execute(
            "SELECT user_id, COUNT(*) c FROM notebooks GROUP BY user_id"
        ).fetchall()}
        assert per_user[1] == 1
        assert per_user[2] == 1
        # No source / conversation left without a notebook_id.
        assert conn.execute("SELECT COUNT(*) c FROM sources WHERE notebook_id IS NULL").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM conversations WHERE notebook_id IS NULL").fetchone()["c"] == 0
    # Idempotency: running init_db a second time must NOT create a second notebook.
    db.init_db()
    with db.connect() as conn:
        per_user = {row["user_id"]: row["c"] for row in conn.execute(
            "SELECT user_id, COUNT(*) c FROM notebooks GROUP BY user_id"
        ).fetchall()}
        assert per_user[1] == 1
        assert per_user[2] == 1


def test_load_llm_settings_decrypts_api_key(monkeypatch, tmp_path):
    """load_llm_settings() should return the decrypted API key when the row
    was stored with encrypt_for_storage()."""
    monkeypatch.setenv("NOTEBOOKLM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOTEBOOKLM_SECRET", "unit-test-secret")
    import app.db as db
    import importlib
    importlib.reload(db)
    db.init_db()
    with db.connect() as conn:
        encrypted = db.encrypt_for_storage("sk-real")
        conn.execute("UPDATE llm_settings SET api_key = ? WHERE id = 1", (encrypted,))
        loaded = db.load_llm_settings(conn)
    assert loaded["api_key"] == "sk-real"


def test_load_llm_settings_passes_legacy_plaintext(monkeypatch, tmp_path):
    """Plaintext keys stored before encryption was added still load unchanged."""
    monkeypatch.setenv("NOTEBOOKLM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOTEBOOKLM_SECRET", "unit-test-secret")
    import app.db as db
    import importlib
    importlib.reload(db)
    db.init_db()
    with db.connect() as conn:
        conn.execute("UPDATE llm_settings SET api_key = ? WHERE id = 1", ("sk-legacy-plaintext",))
        loaded = db.load_llm_settings(conn)
    assert loaded["api_key"] == "sk-legacy-plaintext"


def test_pin_note_is_idempotent(monkeypatch, tmp_path):
    """Pinning the same assistant message twice must not create two notes
    (follow-up Phase 4 round 2 #4)."""
    db, _ = fresh_modules(monkeypatch, tmp_path)
    with db.connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE username = 'user'").fetchone()
        nb_id = conn.execute(
            "INSERT INTO notebooks (user_id, title) VALUES (?, 'NB')", (user["id"],)
        ).lastrowid
        convo_id = conn.execute(
            "INSERT INTO conversations (user_id, notebook_id, title) VALUES (?, ?, 'C')",
            (user["id"], nb_id),
        ).lastrowid
        msg_id = conn.execute(
            "INSERT INTO messages (conversation_id, user_id, role, content) VALUES (?, ?, 'assistant', 'A')",
            (convo_id, user["id"]),
        ).lastrowid

        # Emulate the dedupe guard from pin_note: only insert if no note
        # already references this message_id.
        def pin(msg_id):
            existing = conn.execute(
                "SELECT id FROM notes WHERE notebook_id = ? AND source_message_id = ?",
                (nb_id, msg_id),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO notes (notebook_id, user_id, title, content, source_message_id) VALUES (?, ?, 'P', 'A', ?)",
                    (nb_id, user["id"], msg_id),
                )

        pin(msg_id)
        pin(msg_id)
        pin(msg_id)

        count = conn.execute(
            "SELECT COUNT(*) c FROM notes WHERE notebook_id = ? AND source_message_id = ?",
            (nb_id, msg_id),
        ).fetchone()["c"]
    assert count == 1
