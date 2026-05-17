import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from .security import decrypt_secret, encrypt_secret, get_app_secret, hash_password


def _app_secret() -> str:
    """Read the app secret through the fail-closed security helper."""
    return get_app_secret()


DATA_DIR = Path(os.environ.get("NOTEBOOKLM_DATA_DIR", "data"))
DB_PATH = DATA_DIR / "app.sqlite3"
UPLOAD_DIR = DATA_DIR / "uploads"


def connect() -> sqlite3.Connection:
    """Open a SQLite connection and ensure data directories exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    """Create or migrate the database schema and seed default users."""
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS llm_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                provider TEXT NOT NULL DEFAULT 'openai_compatible',
                base_url TEXT NOT NULL DEFAULT '',
                embedding_base_url TEXT NOT NULL DEFAULT '',
                api_key TEXT NOT NULL DEFAULT '',
                chat_model TEXT NOT NULL DEFAULT '',
                embedding_model TEXT NOT NULL DEFAULT '',
                api_version TEXT NOT NULL DEFAULT '2024-02-15-preview',
                temperature REAL NOT NULL DEFAULT 0.2,
                timeout_seconds REAL NOT NULL DEFAULT 60
            );

            CREATE TABLE IF NOT EXISTS notebooks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL DEFAULT 'Untitled notebook',
                emoji TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                content_type TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'uploaded',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                chunk_index INTEGER NOT NULL,
                location TEXT NOT NULL,
                text TEXT NOT NULL,
                embedding_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL DEFAULT 'New conversation',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                citations_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                notebook_id INTEGER NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                source_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_sources_user_created
            ON sources(user_id, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_sources_user_status_filename
            ON sources(user_id, status, filename);

            CREATE INDEX IF NOT EXISTS idx_chunks_user_source
            ON chunks(user_id, source_id);

            CREATE INDEX IF NOT EXISTS idx_conversations_user_updated
            ON conversations(user_id, updated_at DESC);

            CREATE INDEX IF NOT EXISTS idx_messages_conversation_user_created
            ON messages(conversation_id, user_id, created_at, id);

            CREATE INDEX IF NOT EXISTS idx_notebooks_user_updated
            ON notebooks(user_id, updated_at DESC);
            """
        )
        _ensure_column(conn, "llm_settings", "provider", "TEXT NOT NULL DEFAULT 'openai_compatible'")
        _ensure_column(conn, "llm_settings", "api_version", "TEXT NOT NULL DEFAULT '2024-02-15-preview'")
        # Optional dedicated embedding endpoint — empty string falls back to
        # ``base_url``. Required when chat and embedding live on different
        # services (e.g. vLLM for chat + Ollama / TEI for embeddings).
        _ensure_column(conn, "llm_settings", "embedding_base_url", "TEXT NOT NULL DEFAULT ''")
        # Notebook foreign keys are nullable so existing rows can be migrated in place.
        # Phase 2 routes will populate these on insert; the migration below backfills legacy rows.
        _ensure_column(conn, "sources", "notebook_id", "INTEGER REFERENCES notebooks(id) ON DELETE CASCADE")
        _ensure_column(conn, "conversations", "notebook_id", "INTEGER REFERENCES notebooks(id) ON DELETE CASCADE")
        # Per-message debug metadata: retrieval/generation timings, prompt token
        # estimates, score of the top citation. Drives the chat cost badge.
        _ensure_column(conn, "messages", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(conn, "notebooks", "suggestions_json", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "notebooks", "suggestions_at", "TEXT NOT NULL DEFAULT ''")
        # Per-source TL;DR generated at ingest, shown in preview drawer and
        # reused as compact context for briefing / comparison prompts.
        _ensure_column(conn, "sources", "summary", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "sources", "summary_at", "TEXT NOT NULL DEFAULT ''")
        # Cross-source briefing cached on the notebook (same TTL pattern as
        # suggestions). Auto-generated on first notebook view when sources
        # are indexed.
        _ensure_column(conn, "notebooks", "briefing", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "notebooks", "briefing_at", "TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sources_notebook_created ON sources(notebook_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_conversations_notebook_updated ON conversations(notebook_id, updated_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_notes_notebook_created ON notes(notebook_id, created_at DESC)"
        )
        conn.execute("INSERT OR IGNORE INTO llm_settings (id) VALUES (1)")
        _ensure_user(conn, "admin", "admin123", True)
        _ensure_user(conn, "user", "user123", False)
        _migrate_default_notebooks(conn)


def _migrate_default_notebooks(conn: sqlite3.Connection) -> None:
    """Ensure every user with legacy sources or conversations owns a default notebook.

    Idempotent: safe to call on every startup. Only users with orphan rows
    (notebook_id IS NULL) get a default notebook created; those orphans are
    then backfilled to point at it. Users with no legacy data are left alone
    so that Phase 2's "create notebook" flow remains the natural entry point.
    """
    user_rows = conn.execute(
        """
        SELECT DISTINCT user_id FROM (
            SELECT user_id FROM sources WHERE notebook_id IS NULL
            UNION
            SELECT user_id FROM conversations WHERE notebook_id IS NULL
        )
        """
    ).fetchall()
    for row in user_rows:
        user_id = row["user_id"]
        existing = conn.execute(
            "SELECT id FROM notebooks WHERE user_id = ? ORDER BY id ASC LIMIT 1",
            (user_id,),
        ).fetchone()
        if existing is None:
            cursor = conn.execute(
                """
                INSERT INTO notebooks (user_id, title, emoji, description)
                VALUES (?, 'My Notebook', '📓', 'Migrated from legacy sources and conversations.')
                """,
                (user_id,),
            )
            notebook_id = cursor.lastrowid
        else:
            notebook_id = existing["id"]
        conn.execute(
            "UPDATE sources SET notebook_id = ? WHERE user_id = ? AND notebook_id IS NULL",
            (notebook_id, user_id),
        )
        conn.execute(
            "UPDATE conversations SET notebook_id = ? WHERE user_id = ? AND notebook_id IS NULL",
            (notebook_id, user_id),
        )


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """Add a column to an existing SQLite table when it is missing."""
    columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _ensure_user(conn: sqlite3.Connection, username: str, password: str, is_admin: bool) -> None:
    """Seed a user account without overwriting an existing username."""
    conn.execute(
        """
        INSERT OR IGNORE INTO users (username, password_hash, is_admin)
        VALUES (?, ?, ?)
        """,
        (username, hash_password(password), int(is_admin)),
    )


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Convert a SQLite row to a plain dictionary, preserving None."""
    return dict(row) if row is not None else None


def dumps(value: Any) -> str:
    """Serialize a Python value to JSON while preserving non-ASCII text."""
    return json.dumps(value, ensure_ascii=False)


def loads(value: str) -> Any:
    """Deserialize a JSON string stored in SQLite."""
    return json.loads(value)


def load_llm_settings(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Return the global LLM settings row with the API key decrypted in place.

    All callers that need to make API calls should go through this so the
    plaintext key never lives in the DB. New settings are written via
    ``save_llm_api_key`` which re-encrypts before storage.
    """
    row = conn.execute("SELECT * FROM llm_settings WHERE id = 1").fetchone()
    if row is None:
        return None
    settings = dict(row)
    settings["api_key"] = decrypt_secret(settings.get("api_key") or "", _app_secret())
    return settings


def encrypt_for_storage(plaintext: str) -> str:
    """Encrypt a value for the ``llm_settings.api_key`` column."""
    return encrypt_secret(plaintext, _app_secret())


def load_llm_settings_for_display(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return the settings row with the API key blanked, for the admin UI.

    Includes a boolean ``api_key_masked`` so the form can show "saved" hint
    when there's a key configured. The plaintext key is never sent back.
    """
    row = dict(conn.execute("SELECT * FROM llm_settings WHERE id = 1").fetchone())
    row["api_key_masked"] = bool(row["api_key"])
    row["api_key"] = ""
    return row
