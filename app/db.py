import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

from .security import (
    INSECURE_DEV_SECRET,
    decrypt_secret,
    encrypt_secret,
    get_app_secret,
    hash_password,
    verify_password,
)


logger = logging.getLogger(__name__)


def _app_secret() -> str:
    """Read the app secret through the fail-closed security helper."""
    return get_app_secret()


DATA_DIR = Path(os.environ.get("NOTEBOOKLM_DATA_DIR", "data"))
DB_PATH = DATA_DIR / "app.sqlite3"
UPLOAD_DIR = DATA_DIR / "uploads"
#: Opt in/out of seeding the demo accounts (see `_seed_default_users`). Read from
#: the environment rather than `app/config.py` because this is a bootstrap-time
#: decision made before any settings exist — the same reason `NOTEBOOKLM_DATA_DIR`
#: is read here. Unset means "decide from the app secret".
SEED_DEMO_USERS_ENV = "NOTEBOOKLM_SEED_DEMO_USERS"


def connect() -> sqlite3.Connection:
    """Open a SQLite connection and ensure data directories exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    # WAL makes synchronous=NORMAL safe (no corruption risk; at worst the last
    # committed transactions are lost on OS crash) and much cheaper on writes —
    # matters for ingest, which inserts thousands of chunk rows per source.
    conn.execute("PRAGMA synchronous = NORMAL")
    # Larger per-connection page cache + memory-mapped reads speed the keyword
    # LIKE scans and large reads. cache_size is negative => KiB (~16 MB here).
    conn.execute("PRAGMA cache_size = -16000")
    conn.execute("PRAGMA mmap_size = 268435456")  # 256 MB
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

            -- SEC-4 cross-process failed-login limiter. Bucket identifiers are
            -- HMACs, never raw usernames or client-controlled network headers.
            CREATE TABLE IF NOT EXISTS login_rate_limits (
                bucket_type TEXT NOT NULL,
                bucket_hash TEXT NOT NULL,
                failure_count INTEGER NOT NULL DEFAULT 0,
                window_started REAL NOT NULL,
                blocked_until REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL,
                PRIMARY KEY (bucket_type, bucket_hash)
            );

            CREATE INDEX IF NOT EXISTS idx_login_rate_limits_updated
            ON login_rate_limits(updated_at);

            -- Short cross-process leases bound concurrent PBKDF2 work and
            -- serialize checks for the same opaque account bucket. Expiry
            -- recovers capacity if a web process exits during verification.
            CREATE TABLE IF NOT EXISTS login_verification_leases (
                lease_id TEXT PRIMARY KEY,
                account_hash TEXT NOT NULL UNIQUE,
                expires_at REAL NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_login_verification_leases_expires
            ON login_verification_leases(expires_at);

            CREATE TABLE IF NOT EXISTS llm_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                provider TEXT NOT NULL DEFAULT 'openai_compatible',
                base_url TEXT NOT NULL DEFAULT '',
                embedding_base_url TEXT NOT NULL DEFAULT '',
                api_key TEXT NOT NULL DEFAULT '',
                chat_model TEXT NOT NULL DEFAULT '',
                embedding_model TEXT NOT NULL DEFAULT '',
                api_version TEXT NOT NULL DEFAULT '2024-02-15-preview',
                embedding_provider TEXT NOT NULL DEFAULT 'openai_compatible',
                embedding_api_key TEXT NOT NULL DEFAULT '',
                embedding_api_version TEXT NOT NULL DEFAULT '2024-02-15-preview',
                temperature REAL NOT NULL DEFAULT 0.2,
                reasoning_effort_mode TEXT NOT NULL DEFAULT 'auto',
                reasoning_effort TEXT NOT NULL DEFAULT 'medium',
                timeout_seconds REAL NOT NULL DEFAULT 60,
                diagnostics_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS external_identities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                provider TEXT NOT NULL,
                subject TEXT NOT NULL,
                email TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT '',
                groups_json TEXT NOT NULL DEFAULT '[]',
                last_login_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(provider, subject)
            );

            CREATE INDEX IF NOT EXISTS idx_external_identities_user
            ON external_identities(user_id);

            CREATE TABLE IF NOT EXISTS notebooks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL DEFAULT 'Untitled notebook',
                emoji TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                followups_enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS notebook_domain_config (
                notebook_id INTEGER PRIMARY KEY REFERENCES notebooks(id) ON DELETE CASCADE,
                hints_enabled INTEGER NOT NULL DEFAULT 0,
                answer_policy_enabled INTEGER NOT NULL DEFAULT 0,
                answer_policy TEXT NOT NULL DEFAULT '',
                revision INTEGER NOT NULL DEFAULT 0,
                version_token TEXT NOT NULL DEFAULT '',
                updated_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS notebook_domain_hints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                notebook_id INTEGER NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                term TEXT NOT NULL,
                synonyms_json TEXT NOT NULL DEFAULT '[]',
                definition TEXT NOT NULL DEFAULT '',
                query_expansions_json TEXT NOT NULL DEFAULT '[]',
                answer_note TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_notebook_domain_hints_notebook
            ON notebook_domain_hints(notebook_id, enabled, id);

            CREATE UNIQUE INDEX IF NOT EXISTS uq_notebook_domain_hints_term
            ON notebook_domain_hints(notebook_id, term COLLATE NOCASE);

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

            -- Cross-process briefing generation lock (P2-3). One row per
            -- notebook while a briefing is being generated; PRIMARY KEY gives
            -- at-most-one-holder, and a stale row past BRIEFING_LOCK_TIMEOUT_S
            -- is reclaimed by the acquirer. Replaces the old in-process dict so
            -- multiple uvicorn workers don't each hold an independent lock.
            CREATE TABLE IF NOT EXISTS briefing_locks (
                notebook_id INTEGER PRIMARY KEY REFERENCES notebooks(id) ON DELETE CASCADE,
                acquired_at REAL NOT NULL
            );

            -- DB-backed ingest job queue (P1-1). Replaces FastAPI
            -- BackgroundTasks so ingest survives a restart and can run in a
            -- separate worker process off the web process. UNIQUE(source_id)
            -- keeps at most one job per source; reindex upserts it back to
            -- 'queued'. claimed_at drives the crashed-worker visibility timeout.
            CREATE TABLE IF NOT EXISTS ingest_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL UNIQUE REFERENCES sources(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0,
                claimed_at REAL,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_ingest_jobs_status
            ON ingest_jobs(status, id);

            -- Cross-process coordination for the Chroma collection (O0).
            -- Chroma locks a collection's embedding dimension on first upsert
            -- and deleting every record does NOT release it, so a dimension
            -- migration has to replace the collection object itself. Each
            -- process caches its own collection handle, so the replacing
            -- process bumps `generation` and every other process notices the
            -- mismatch on its next collection() call and re-fetches. Single
            -- row; the CHECK keeps it that way.
            CREATE TABLE IF NOT EXISTS vector_index_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                generation INTEGER NOT NULL DEFAULT 0
            );

            INSERT OR IGNORE INTO vector_index_state (id, generation) VALUES (1, 0);

            -- Admin-only in-deployment eval workbench (E1). Eval data stays in
            -- the customer deployment; runs snapshot the profile and per-item
            -- results so tuning decisions can be audited and revisited.
            CREATE TABLE IF NOT EXISTS retrieval_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                params_json TEXT NOT NULL DEFAULT '{}',
                requires_reindex INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 0,
                is_default INTEGER NOT NULL DEFAULT 0,
                source_run_id INTEGER,
                created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS eval_sets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                target_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                notebook_id INTEGER NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS eval_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                eval_set_id INTEGER NOT NULL REFERENCES eval_sets(id) ON DELETE CASCADE,
                question TEXT NOT NULL,
                expected_source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
                expected_chunk_id INTEGER REFERENCES chunks(id) ON DELETE SET NULL,
                expected_substrings_json TEXT NOT NULL DEFAULT '[]',
                item_type TEXT NOT NULL DEFAULT 'answerable',
                expected_answer TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                notes TEXT NOT NULL DEFAULT '',
                approved INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS eval_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                eval_set_id INTEGER NOT NULL REFERENCES eval_sets(id) ON DELETE CASCADE,
                profile_id INTEGER REFERENCES retrieval_profiles(id) ON DELETE SET NULL,
                created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                progress_current INTEGER NOT NULL DEFAULT 0,
                progress_total INTEGER NOT NULL DEFAULT 0,
                current_step TEXT NOT NULL DEFAULT '',
                profile_snapshot_json TEXT NOT NULL DEFAULT '{}',
                domain_config_snapshot_json TEXT NOT NULL DEFAULT '{}',
                domain_hints_enabled INTEGER NOT NULL DEFAULT 0,
                answer_policy_enabled INTEGER NOT NULL DEFAULT 0,
                metrics_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                started_at TEXT,
                finished_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS eval_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
                eval_item_id INTEGER NOT NULL REFERENCES eval_items(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'pending',
                hit_rank INTEGER,
                top_score REAL NOT NULL DEFAULT 0,
                latency_ms REAL NOT NULL DEFAULT 0,
                retrieved_json TEXT NOT NULL DEFAULT '[]',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_eval_sets_notebook
            ON eval_sets(notebook_id, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_eval_items_set
            ON eval_items(eval_set_id, approved, id);

            CREATE INDEX IF NOT EXISTS idx_eval_runs_set
            ON eval_runs(eval_set_id, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_eval_results_run
            ON eval_results(run_id, eval_item_id);

            -- Durable, admin-visible audit trail for security / compliance
            -- relevant actions. Do not store copied source text, API keys, or
            -- full exported payloads here; metadata_json should contain only
            -- identifiers and compact summaries.
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                actor_username TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL,
                target_type TEXT NOT NULL DEFAULT '',
                target_id INTEGER,
                sensitivity TEXT NOT NULL DEFAULT 'normal',
                ip_address TEXT NOT NULL DEFAULT '',
                user_agent TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_audit_events_created
            ON audit_events(created_at DESC, id DESC);

            CREATE INDEX IF NOT EXISTS idx_audit_events_action_created
            ON audit_events(action, created_at DESC);

            -- High-volume AI governance telemetry (G1a/G1b). This is kept
            -- separate from audit_events because usage/cost events are much
            -- more frequent and should store only compact attribution and
            -- usage metrics, never prompts, source text, retrieved snippets,
            -- API keys, or full model outputs.
            CREATE TABLE IF NOT EXISTS llm_usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                notebook_id INTEGER REFERENCES notebooks(id) ON DELETE SET NULL,
                conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
                message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
                source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
                eval_run_id INTEGER REFERENCES eval_runs(id) ON DELETE SET NULL,
                eval_set_id INTEGER REFERENCES eval_sets(id) ON DELETE SET NULL,
                call_type TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'succeeded',
                latency_ms REAL NOT NULL DEFAULT 0,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                input_chars INTEGER NOT NULL DEFAULT 0,
                output_chars INTEGER NOT NULL DEFAULT 0,
                is_estimated INTEGER NOT NULL DEFAULT 1,
                error_class TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_llm_usage_events_created
            ON llm_usage_events(created_at DESC, id DESC);

            CREATE INDEX IF NOT EXISTS idx_llm_usage_events_call_created
            ON llm_usage_events(call_type, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_llm_usage_events_user_created
            ON llm_usage_events(user_id, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_llm_usage_events_notebook_created
            ON llm_usage_events(notebook_id, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_llm_usage_events_eval_run_created
            ON llm_usage_events(eval_run_id, created_at DESC);

            -- AI safety / guardrail signals (G1c). Kept separate from the
            -- formal audit trail and usage telemetry: this is high-volume,
            -- scanner-oriented data and must not copy prompts, source text,
            -- retrieved snippets, API keys, or model outputs.
            CREATE TABLE IF NOT EXISTS ai_safety_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                notebook_id INTEGER REFERENCES notebooks(id) ON DELETE SET NULL,
                conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
                message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
                source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
                eval_run_id INTEGER REFERENCES eval_runs(id) ON DELETE SET NULL,
                eval_set_id INTEGER REFERENCES eval_sets(id) ON DELETE SET NULL,
                event_type TEXT NOT NULL,
                surface TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL,
                severity TEXT NOT NULL,
                decision TEXT NOT NULL,
                detector_version TEXT NOT NULL DEFAULT '',
                rule_id TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL DEFAULT '',
                redacted_summary TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_ai_safety_events_created
            ON ai_safety_events(created_at DESC, id DESC);

            CREATE INDEX IF NOT EXISTS idx_ai_safety_events_category_created
            ON ai_safety_events(category, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_ai_safety_events_user_created
            ON ai_safety_events(user_id, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_ai_safety_events_notebook_created
            ON ai_safety_events(notebook_id, created_at DESC);
            """
        )
        _ensure_column(conn, "llm_settings", "provider", "TEXT NOT NULL DEFAULT 'openai_compatible'")
        _ensure_column(conn, "llm_settings", "api_version", "TEXT NOT NULL DEFAULT '2024-02-15-preview'")
        # Optional dedicated embedding endpoint — empty string falls back to
        # ``base_url``. Required when chat and embedding live on different
        # services (e.g. vLLM for chat + Ollama / TEI for embeddings).
        _ensure_column(conn, "llm_settings", "embedding_base_url", "TEXT NOT NULL DEFAULT ''")
        # Optional per-path embedding prefixes. Some embedding models (notably the
        # e5 family) need "query: " / "passage: " prefixes for best retrieval;
        # left empty for models that don't (OpenAI, etc.), so the default behaviour
        # is unchanged and the app stays embedding-model-agnostic.
        _ensure_column(conn, "llm_settings", "embedding_query_prefix", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "llm_settings", "embedding_passage_prefix", "TEXT NOT NULL DEFAULT ''")
        # Embedding now has its own independent connection (provider / key /
        # api-version), so chat and embedding can point at different services
        # (e.g. Gemma chat on one host, e5 embedding on another). Detect a fresh
        # add so we can backfill the embedding connection from the previously
        # shared chat fields once — preserving any already-working setup.
        _embedding_conn_is_new = "embedding_provider" not in {
            row["name"] for row in conn.execute("PRAGMA table_info(llm_settings)")
        }
        _ensure_column(conn, "llm_settings", "embedding_provider", "TEXT NOT NULL DEFAULT 'openai_compatible'")
        _ensure_column(conn, "llm_settings", "embedding_api_key", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "llm_settings", "embedding_api_version", "TEXT NOT NULL DEFAULT '2024-02-15-preview'")
        if _embedding_conn_is_new:
            # One-time backfill: copy the shared chat connection into the new
            # embedding connection so existing deployments keep working. Make the
            # embedding base URL explicit instead of relying on the old implicit
            # "empty embedding_base_url falls back to base_url" behaviour.
            conn.execute(
                """
                UPDATE llm_settings
                   SET embedding_provider = provider,
                       embedding_api_key = api_key,
                       embedding_api_version = api_version,
                       embedding_base_url = CASE
                           WHEN embedding_base_url = '' THEN base_url
                           ELSE embedding_base_url
                       END
                 WHERE id = 1
                """
            )
        # O1 Phase 1: compact admin diagnostics for the current single global
        # settings row. Stores only status/capability metadata, never prompts,
        # model outputs, API keys, or raw provider payloads.
        _ensure_column(conn, "llm_settings", "diagnostics_json", "TEXT NOT NULL DEFAULT '{}'")
        # LLM-5: provider-neutral reasoning policy. Auto preserves the existing
        # task-intent mapping; fixed values remain fail-closed until the current
        # endpoint probe accepts that exact value.
        _ensure_column(conn, "llm_settings", "reasoning_effort_mode", "TEXT NOT NULL DEFAULT 'auto'")
        _ensure_column(conn, "llm_settings", "reasoning_effort", "TEXT NOT NULL DEFAULT 'medium'")
        # U11: per-user colour theme — 'system' (follow the OS preference),
        # 'light', or 'dark'. Kept as a plain TEXT allowlist checked at the route
        # layer (see THEME_CHOICES in app/main.py) rather than a CHECK constraint,
        # so adding a theme later stays a code-only change.
        _ensure_column(conn, "users", "theme", "TEXT NOT NULL DEFAULT 'system'")
        # SEC-1: this account must change its password before it can use the app.
        # Set on the bootstrap `admin` seeded outside local development, and
        # back-filled by `_flag_default_passwords` onto any account still using a
        # seeded default. Defaults to 0 so every existing account is unaffected.
        _ensure_column(conn, "users", "must_change_password", "INTEGER NOT NULL DEFAULT 0")
        # SEC-3: bumped on every password change (self-service, forced bootstrap
        # change, or an admin reset). Session cookies embed the version they were
        # issued under, so a bump invalidates every session for that account
        # except the one that is re-issued to the actor. This is what makes
        # "changing a password signs the other sessions out" work without a
        # server-side session table.
        _ensure_column(conn, "users", "password_version", "INTEGER NOT NULL DEFAULT 1")
        # A6a: what the extractor actually produced for this source — counts,
        # the extractor path taken, warnings, and a bounded text preview. One
        # JSON blob (same pattern as llm_settings.diagnostics_json from O1a) so
        # new formats can add signals without a migration each time. Rewritten
        # on every (re)ingest, never appended to.
        _ensure_column(conn, "sources", "diagnostics_json", "TEXT NOT NULL DEFAULT '{}'")
        # O0 Phase C: the migration write barrier. While `locked_at` is set the
        # ingest worker refuses to claim jobs, so nothing can upsert between the
        # collection's delete and its recreate — an upsert landing in that window
        # rebuilds the collection at the OLD dimension and silently undoes the
        # migration. `locked_by` is diagnostics only (which process holds it),
        # never an authorization check. A lock older than the configured timeout
        # is treated as stale and reclaimed, the same contract as briefing_locks.
        _ensure_column(conn, "vector_index_state", "locked_at", "REAL")
        _ensure_column(conn, "vector_index_state", "locked_by", "TEXT NOT NULL DEFAULT ''")
        # U16 Phase 2: what produced an outputs-shelf entry ('pinned', 'note', or
        # a Studio tool kind — allowlist NOTE_KINDS in app/main.py). Drives the
        # type badge and the shelf filter. '' means "not yet classified" and is
        # consumed by the one-time backfill below.
        _ensure_column(conn, "notes", "kind", "TEXT NOT NULL DEFAULT ''")
        _backfill_note_kinds(conn)
        # Notebook foreign keys are nullable so existing rows can be migrated in place.
        # Phase 2 routes will populate these on insert; the migration below backfills legacy rows.
        _ensure_column(conn, "sources", "notebook_id", "INTEGER REFERENCES notebooks(id) ON DELETE CASCADE")
        _ensure_column(conn, "conversations", "notebook_id", "INTEGER REFERENCES notebooks(id) ON DELETE CASCADE")
        # Per-message debug metadata: retrieval/generation timings, prompt token
        # estimates, score of the top citation. Drives the chat cost badge.
        _ensure_column(conn, "messages", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(conn, "notebooks", "suggestions_json", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "notebooks", "suggestions_at", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "notebooks", "followups_enabled", "INTEGER NOT NULL DEFAULT 1")
        # Per-source TL;DR generated at ingest, shown in preview drawer and
        # reused as compact context for briefing / comparison prompts.
        _ensure_column(conn, "sources", "summary", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "sources", "summary_at", "TEXT NOT NULL DEFAULT ''")
        # Cross-source briefing cached on the notebook (same TTL pattern as
        # suggestions). Auto-generated on first notebook view when sources
        # are indexed.
        _ensure_column(conn, "notebooks", "briefing", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "notebooks", "briefing_at", "TEXT NOT NULL DEFAULT ''")
        # E1c: protect a non-deletable system-default retrieval profile so an admin
        # can always fall back to known-good config values. Backfill marks the
        # lowest-id profile as default when no row is flagged yet.
        _ensure_column(conn, "retrieval_profiles", "is_default", "INTEGER NOT NULL DEFAULT 0")
        # E1e-1: LLM-assisted eval authoring metadata. These fields are optional
        # for manual/deterministic items and do not change retrieval-only scoring.
        _ensure_column(conn, "eval_items", "item_type", "TEXT NOT NULL DEFAULT 'answerable'")
        _ensure_column(conn, "eval_items", "expected_answer", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "eval_items", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
        # E1e-2: optional answer-quality judging. `judge_enabled` gates the extra
        # generate+judge LLM calls per run (default off → full regression to
        # retrieval-only behavior). Per-result judge output is stored separately
        # from retrieval metrics so the two never mix. answer_text is only ever
        # surfaced in full internal exports (never sanitized ones).
        _ensure_column(conn, "eval_runs", "judge_enabled", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "eval_runs", "domain_config_snapshot_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(conn, "eval_runs", "domain_hints_enabled", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "eval_runs", "answer_policy_enabled", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "eval_results", "judge_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(conn, "eval_results", "answer_text", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "eval_results", "answer_outcome", "TEXT NOT NULL DEFAULT ''")
        if conn.execute("SELECT COUNT(*) FROM retrieval_profiles WHERE is_default = 1").fetchone()[0] == 0:
            conn.execute(
                "UPDATE retrieval_profiles SET is_default = 1 "
                "WHERE id = (SELECT id FROM retrieval_profiles ORDER BY id ASC LIMIT 1)"
            )
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
        _seed_default_users(conn)
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
    """Add a column to an existing SQLite table when it is missing.

    Idempotent under concurrency: the web app and the standalone ingest worker
    both run ``init_db()`` against the same SQLite file at startup, so two
    processes can observe the column missing and both issue the ALTER. The
    loser gets ``duplicate column name`` — swallow it, since the column now
    exists, which is all this function guarantees.
    """
    columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
    if column not in columns:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise


# Title prefixes the Studio generators used before `notes.kind` existed, mapped
# to the kind they imply. These literals are **historical and frozen**: they are
# a fingerprint of rows already on disk, not display copy, so they must not be
# swapped for the i18n labels (which may change, and in two cases already differ
# — the shelf entry said "會議整理"/"摘要翻譯" while the tool tile says
# "會議記錄"/"翻譯摘要"). Best-effort by design: a miss just means a row keeps
# the generic 'note' badge, never wrong data.
_LEGACY_NOTE_TITLE_PREFIXES = (
    ("來源比較：", "compare"),
    ("會議整理 — ", "minutes"),
    ("摘要翻譯（", "translate"),
    ("學習指南 — ", "study_guide"),
    ("常見問答 — ", "faq"),
    ("時間軸 — ", "timeline"),
)


def _backfill_note_kinds(conn: sqlite3.Connection) -> None:
    """Classify pre-U16-Phase-2 notes once (rows still carrying kind = '').

    Idempotent: every statement is scoped to ``kind = ''`` and the final sweep
    leaves no empty values behind, so later startups are no-ops.
    """
    # Exact: a note with a source message is a pinned chat answer.
    conn.execute(
        "UPDATE notes SET kind = 'pinned' WHERE kind = '' AND source_message_id IS NOT NULL"
    )
    # Best-effort: recover the generator from the title it wrote.
    for prefix, kind in _LEGACY_NOTE_TITLE_PREFIXES:
        conn.execute(
            "UPDATE notes SET kind = ? WHERE kind = '' AND title LIKE ?",
            (kind, prefix + "%"),
        )
    # Everything else is a plain saved note.
    conn.execute("UPDATE notes SET kind = 'note' WHERE kind = ''")


#: Username -> the password `_seed_default_users` would create it with. Used both
#: for seeding and for `_flag_default_passwords`, which is the half that protects
#: deployments seeded before this file learned to stop shipping standing defaults.
SEEDED_DEFAULT_PASSWORDS: dict[str, str] = {"admin": "admin123", "user": "user123"}


def seed_demo_users_enabled() -> bool:
    """Whether to seed the full demo account pair with standing passwords.

    Explicit ``NOTEBOOKLM_SEED_DEMO_USERS`` wins in both directions. Unset, this
    falls back to "are we running on the insecure dev secret?" — the same signal
    the login page already uses to decide whether printing the demo credentials
    is safe (``demo_hint`` in ``app/main.py``). So local development keeps working
    with no new configuration, while any deployment that sets a real
    ``NOTEBOOKLM_SECRET`` stops getting standing default passwords.
    """
    override = os.environ.get(SEED_DEMO_USERS_ENV, "").strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    return _app_secret() == INSECURE_DEV_SECRET


def _ensure_user(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    is_admin: bool,
    must_change_password: bool = False,
) -> None:
    """Seed a user account without overwriting an existing username."""
    conn.execute(
        """
        INSERT OR IGNORE INTO users (username, password_hash, is_admin, must_change_password)
        VALUES (?, ?, ?, ?)
        """,
        (username, hash_password(password), int(is_admin), int(must_change_password)),
    )


def _seed_default_users(conn: sqlite3.Connection) -> None:
    """Seed the bootstrap accounts, without leaving a standing default password.

    ``init_db()`` runs on **every** start of both the web app and the worker, so
    whatever this function creates is re-created after each restart. That used to
    mean an operator who followed SECURITY.md and *deleted* the demo accounts got
    them back — with their original passwords — on the next restart.

    Two different environments need two different answers, and the app already
    distinguishes them the same way the login page decides whether to print the
    demo credentials (``SECRET == INSECURE_DEV_SECRET``):

    * **Local development** (the insecure dev secret is explicitly allowed): seed
      both accounts exactly as before. The convenience is the point, the login
      page advertises them, and nothing is exposed.
    * **Anything else** (a real ``NOTEBOOKLM_SECRET`` is set): seed **only**
      ``admin``, and mark it ``must_change_password`` so the password is a
      one-time bootstrap credential rather than a standing one. ``user`` is not
      seeded at all, so deleting it now sticks.

    ``admin`` is still seeded in production on purpose: a deployment that restarts
    with no accounts must remain enterable. The forced change is what makes that
    safe.
    """
    if seed_demo_users_enabled():
        _ensure_user(conn, "admin", SEEDED_DEFAULT_PASSWORDS["admin"], True)
        _ensure_user(conn, "user", SEEDED_DEFAULT_PASSWORDS["user"], False)
        return
    _ensure_user(conn, "admin", SEEDED_DEFAULT_PASSWORDS["admin"], True, must_change_password=True)
    _flag_default_passwords(conn)


def _flag_default_passwords(conn: sqlite3.Connection) -> None:
    """Force a password change on any account still using its seeded default.

    Not seeding ``user`` any more only helps deployments created from here on.
    Every database seeded by an earlier version already carries ``admin/admin123``
    and ``user/user123``, and those accounts are exactly the ones an attacker
    would try first. Rather than silently deleting accounts that may own data, we
    verify each seeded username against the password it *would* have been created
    with, and flag the ones that never changed — the credential still works once,
    to change itself, and nothing else.

    Accounts whose password was already changed verify false and are left alone,
    so this is safe to run on every startup.

    **SSO-linked accounts are skipped, and that is a lockout guard, not a
    courtesy.** A flagged account may only change its password, but
    ``POST /account/password`` refuses accounts with an external identity (the
    same guardrail that blocks admin password resets for them). An account that
    was both flagged *and* SSO-linked would have no way out at all. Today the
    external-auth flow only ever creates fresh usernames with an unguessable
    ``sso:<uuid>`` hash, so it cannot produce one — this check makes that safe by
    construction rather than by coincidence, so a future "link SSO to an existing
    local account" feature cannot lock an operator out. Such an account keeps a
    working default password, which is worse than useless silently, so it is
    logged loudly instead: the fix there is to disable local login for the
    deployment or reset the password through admin tooling.
    """
    for username, default_password in SEEDED_DEFAULT_PASSWORDS.items():
        row = conn.execute(
            """
            SELECT u.id, u.password_hash, u.must_change_password,
                   (SELECT COUNT(*) FROM external_identities e WHERE e.user_id = u.id)
                       AS external_identity_count
              FROM users u
             WHERE u.username = ?
            """,
            (username,),
        ).fetchone()
        if row is None or row["must_change_password"]:
            continue
        if not verify_password(default_password, row["password_hash"]):
            continue
        if row["external_identity_count"]:
            logger.warning(
                "seeded_default_password_left_unflagged username=%s reason=sso_linked "
                "detail=account still accepts its seeded password over local login, but "
                "forcing a change would lock it out; disable local login or reset it via admin",
                username,
            )
            continue
        conn.execute("UPDATE users SET must_change_password = 1 WHERE id = ?", (row["id"],))


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Convert a SQLite row to a plain dictionary, preserving None."""
    return dict(row) if row is not None else None


def dumps(value: Any) -> str:
    """Serialize a Python value to JSON while preserving non-ASCII text."""
    return json.dumps(value, ensure_ascii=False)


def loads(value: str) -> Any:
    """Deserialize a JSON string stored in SQLite."""
    return json.loads(value)


def _llm_diagnostics(value: Any) -> dict[str, Any]:
    """Decode the untyped LLM diagnostics blob for runtime and display readers."""
    try:
        diagnostics = loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return diagnostics if isinstance(diagnostics, dict) else {}


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
    settings["embedding_api_key"] = decrypt_secret(settings.get("embedding_api_key") or "", _app_secret())
    settings["diagnostics"] = _llm_diagnostics(settings.get("diagnostics_json"))
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
    row["embedding_api_key_masked"] = bool(row["embedding_api_key"])
    row["embedding_api_key"] = ""
    row["diagnostics"] = _llm_diagnostics(row.get("diagnostics_json"))
    return row
