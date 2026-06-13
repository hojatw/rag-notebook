# Database schema

SQLite database at `data/app.sqlite3` (metadata; vectors also live in Chroma under `data/chroma/`, uploads under `data/uploads/`).

> **Source of truth is [`app/db.py`](../app/db.py)** — `init_db()` holds the `CREATE TABLE` statements and the `_ensure_column(...)` migrations below them (columns added after a table's original definition). This document is a hand-maintained reference; **keep it in sync whenever you change the schema in `app/db.py`** (see AGENTS.md).
>
> Regenerate the effective DDL from a live DB any time:
> ```bash
> sqlite3 data/app.sqlite3 ".schema"
> ```

## Conventions

- **Primary keys** are `INTEGER PRIMARY KEY AUTOINCREMENT` unless noted.
- **Timestamps** are `TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP` (SQLite ISO-8601 strings, UTC).
- **Foreign keys** use `ON DELETE CASCADE` (a deleted parent removes its children) unless noted. `PRAGMA foreign_keys = ON` is set per connection in `connect()`.
- **Migrations** are idempotent: base tables via `CREATE TABLE IF NOT EXISTS`, later columns via `_ensure_column()` (a guarded `ALTER TABLE ADD COLUMN`, safe under concurrent startup — see the app/worker race fix). There is no migration-version table; the column set *is* the version.
- WAL mode + tuning pragmas (`synchronous=NORMAL`, `cache_size`, `mmap_size`) are set in `connect()`.

## Relationships

```
users ─┬─< notebooks ─┬─< sources ─┬─< chunks
       │              │            └─1 ingest_jobs   (UNIQUE source_id)
       │              ├─< conversations ─< messages ─< notes  (notes.source_message_id, SET NULL)
       │              ├─< notes
       │              └─1 briefing_locks               (PK notebook_id)
       ├─< sources / chunks / conversations / messages / notes   (user_id on every row, per-user scoping)
llm_settings : single row (id = 1), global
```

Every user-owned table carries `user_id` so authorization is enforced per-row at the route layer (defence in depth alongside the notebook FK).

---

## `users`
Login accounts. Seeded with `admin` / `user` on first init.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `username` | TEXT NOT NULL **UNIQUE** | |
| `password_hash` | TEXT NOT NULL | PBKDF2-SHA256 via `app/security.py` |
| `is_admin` | INTEGER NOT NULL DEFAULT 0 | 1 = admin (can access `/settings`, `/admin/*`) |
| `created_at` | TEXT | |

## `llm_settings`
Global LLM/embedding configuration. **Exactly one row** (`CHECK (id = 1)`).

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK `CHECK (id = 1)` | always 1 |
| `provider` | TEXT NOT NULL DEFAULT `'openai_compatible'` | `openai_compatible` \| `azure_openai` |
| `base_url` | TEXT DEFAULT `''` | chat endpoint base / Azure endpoint |
| `embedding_base_url` | TEXT DEFAULT `''` | optional; blank = share `base_url` |
| `api_key` | TEXT DEFAULT `''` | **Fernet-encrypted at rest**; read only via `load_llm_settings()` (never `SELECT` directly) |
| `chat_model` | TEXT DEFAULT `''` | model / Azure deployment |
| `embedding_model` | TEXT DEFAULT `''` | model / Azure deployment |
| `api_version` | TEXT DEFAULT `'2024-02-15-preview'` | Azure only |
| `temperature` | REAL DEFAULT 0.2 | |
| `timeout_seconds` | REAL DEFAULT 60 | |
| `embedding_query_prefix` | TEXT DEFAULT `''` | e.g. `query: ` for e5; blank for OpenAI |
| `embedding_passage_prefix` | TEXT DEFAULT `''` | e.g. `passage: ` for e5 |

## `notebooks`
A workspace owned by a user. Holds cached Studio outputs.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `user_id` | INTEGER NOT NULL → `users(id)` CASCADE | |
| `title` | TEXT DEFAULT `'Untitled notebook'` | |
| `emoji` | TEXT DEFAULT `''` | |
| `description` | TEXT DEFAULT `''` | |
| `followups_enabled` | INTEGER NOT NULL DEFAULT 1 | 1 = show follow-up question suggestions after answered chat messages |
| `created_at` / `updated_at` | TEXT | `updated_at` bumped on activity (sorts the grid) |
| `suggestions_json` / `suggestions_at` | TEXT DEFAULT `''` | cached starter questions + timestamp (24 h TTL) |
| `briefing` / `briefing_at` | TEXT DEFAULT `''` | cached cross-source briefing + timestamp (24 h TTL) |

## `sources`
An uploaded document. `notebook_id` is nullable for legacy rows (backfilled by the default-notebook migration).

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `user_id` | INTEGER NOT NULL → `users(id)` CASCADE | |
| `notebook_id` | INTEGER → `notebooks(id)` CASCADE | nullable (migrated column) |
| `filename` | TEXT NOT NULL | original name |
| `stored_path` | TEXT NOT NULL | path under `data/uploads/<user_id>/` |
| `content_type` | TEXT DEFAULT `''` | |
| `status` | TEXT NOT NULL DEFAULT `'uploaded'` | `uploaded` → `processing` → `indexed` \| `failed` |
| `error` | TEXT DEFAULT `''` | failure message (truncated) |
| `summary` / `summary_at` | TEXT DEFAULT `''` | per-source TL;DR generated after indexing |
| `created_at` / `updated_at` | TEXT | |

## `chunks`
Indexed text chunks + their embeddings. **`embedding_json` is the durable source of truth** from which the Chroma index is rebuilt without re-embedding.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | also the Chroma vector id |
| `user_id` | INTEGER NOT NULL → `users(id)` CASCADE | |
| `source_id` | INTEGER NOT NULL → `sources(id)` CASCADE | |
| `chunk_index` | INTEGER NOT NULL | order within the source |
| `location` | TEXT NOT NULL | citation span label (e.g. `page 3 paragraph 1`) |
| `text` | TEXT NOT NULL | chunk text (also keyword-`LIKE`-searched) |
| `embedding_json` | TEXT NOT NULL | JSON float array of the chunk vector |

## `conversations`
A chat thread within a notebook.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `user_id` | INTEGER NOT NULL → `users(id)` CASCADE | |
| `notebook_id` | INTEGER → `notebooks(id)` CASCADE | nullable (migrated column) |
| `title` | TEXT DEFAULT `'New conversation'` | set from the first question |
| `created_at` / `updated_at` | TEXT | |

## `messages`
User and assistant turns.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `conversation_id` | INTEGER NOT NULL → `conversations(id)` CASCADE | |
| `user_id` | INTEGER NOT NULL → `users(id)` CASCADE | |
| `role` | TEXT NOT NULL `CHECK (role IN ('user','assistant'))` | |
| `content` | TEXT NOT NULL | answer text (with `[N]` citation markers) |
| `citations_json` | TEXT DEFAULT `'[]'` | referenced chunks (filename/location/scores) |
| `metadata_json` | TEXT DEFAULT `'{}'` | debug pane: timings, top_score, outcome, etc. |
| `created_at` | TEXT | |

## `notes`
Pinned answers / notes in a notebook's Studio.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `notebook_id` | INTEGER NOT NULL → `notebooks(id)` CASCADE | |
| `user_id` | INTEGER NOT NULL → `users(id)` CASCADE | |
| `title` | TEXT DEFAULT `''` | |
| `content` | TEXT DEFAULT `''` | |
| `source_message_id` | INTEGER → `messages(id)` **ON DELETE SET NULL** | the pinned message, if any |
| `created_at` / `updated_at` | TEXT | |

## `briefing_locks`
Cross-process lock for briefing generation (P2-3). One row per in-flight notebook; a row older than `BRIEFING_LOCK_TIMEOUT_S` is treated as stale.

| Column | Type | Notes |
|---|---|---|
| `notebook_id` | INTEGER **PK** → `notebooks(id)` CASCADE | one lock per notebook |
| `acquired_at` | REAL NOT NULL | unix timestamp when generation started |

## `ingest_jobs`
DB-backed ingest queue (P1-1). At most one job per source.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `source_id` | INTEGER NOT NULL **UNIQUE** → `sources(id)` CASCADE | reindex upserts this row |
| `status` | TEXT NOT NULL DEFAULT `'queued'` | `queued` → `running` → `done` \| `failed` |
| `attempts` | INTEGER NOT NULL DEFAULT 0 | incremented on each claim; capped by `JOB_MAX_ATTEMPTS` |
| `claimed_at` | REAL | unix timestamp of claim; drives the visibility timeout (nullable) |
| `error` | TEXT DEFAULT `''` | last failure message |
| `created_at` / `updated_at` | TEXT | |

---

## Indexes

| Index | Table | Columns |
|---|---|---|
| `idx_sources_user_created` | sources | `(user_id, created_at DESC)` |
| `idx_sources_user_status_filename` | sources | `(user_id, status, filename)` |
| `idx_sources_notebook_created` | sources | `(notebook_id, created_at DESC)` |
| `idx_chunks_user_source` | chunks | `(user_id, source_id)` |
| `idx_conversations_user_updated` | conversations | `(user_id, updated_at DESC)` |
| `idx_conversations_notebook_updated` | conversations | `(notebook_id, updated_at DESC)` |
| `idx_messages_conversation_user_created` | messages | `(conversation_id, user_id, created_at, id)` |
| `idx_notebooks_user_updated` | notebooks | `(user_id, updated_at DESC)` |
| `idx_notes_notebook_created` | notes | `(notebook_id, created_at DESC)` |
| `idx_ingest_jobs_status` | ingest_jobs | `(status, id)` |
