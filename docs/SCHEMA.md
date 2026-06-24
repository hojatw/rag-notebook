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

eval_sets ─< eval_items
          └─< eval_runs ─< eval_results
retrieval_profiles ─< eval_runs
users ─< audit_events (actor_user_id SET NULL)
users/notebooks/conversations/messages/sources/eval_* ─< llm_usage_events (all SET NULL)
users/notebooks/conversations/messages/sources/eval_* ─< ai_safety_events (all SET NULL)
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

Chat and embedding have **independent connections** so they can point at
different services (e.g. Gemma chat on one host, e5 embedding on another). The
top-level `provider` / `base_url` / `api_key` / `api_version` columns are the
**chat** connection; the `embedding_*` columns below are the **embedding**
connection. The API key is **optional** on both sides — local services (e5,
Ollama, vLLM, TEI) accept requests without one; when present it is sent as a
bearer / `api-key` header, when blank no auth header is sent.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK `CHECK (id = 1)` | always 1 |
| `provider` | TEXT NOT NULL DEFAULT `'openai_compatible'` | **chat** provider: `openai_compatible` \| `azure_openai` |
| `base_url` | TEXT DEFAULT `''` | chat endpoint base / Azure endpoint |
| `embedding_base_url` | TEXT DEFAULT `''` | embedding endpoint base / Azure endpoint (blank falls back to `base_url`) |
| `api_key` | TEXT DEFAULT `''` | **chat** key. Optional. **Fernet-encrypted at rest**; read only via `load_llm_settings()` (never `SELECT` directly) |
| `chat_model` | TEXT DEFAULT `''` | model / Azure deployment |
| `embedding_model` | TEXT DEFAULT `''` | model / Azure deployment |
| `api_version` | TEXT DEFAULT `'2024-02-15-preview'` | chat Azure only |
| `embedding_provider` | TEXT NOT NULL DEFAULT `'openai_compatible'` | **embedding** provider: `openai_compatible` \| `azure_openai` |
| `embedding_api_key` | TEXT DEFAULT `''` | **embedding** key. Optional. **Fernet-encrypted at rest**; read only via `load_llm_settings()` |
| `embedding_api_version` | TEXT DEFAULT `'2024-02-15-preview'` | embedding Azure only |
| `temperature` | REAL DEFAULT 0.2 | shared (chat) |
| `timeout_seconds` | REAL DEFAULT 60 | shared |
| `embedding_query_prefix` | TEXT DEFAULT `''` | e.g. `query: ` for e5; blank for OpenAI |
| `embedding_passage_prefix` | TEXT DEFAULT `''` | e.g. `passage: ` for e5 |
| `diagnostics_json` | TEXT DEFAULT `'{}'` | O1 Phase 1 compact admin test results for chat/embedding diagnostics: status, latency, provider/model summary, embedding dimension, capability statuses, timestamp, and error class only; no prompts, outputs, API keys, or raw provider payloads |

> **Migration note:** the `embedding_provider` / `embedding_api_key` /
> `embedding_api_version` columns are added idempotently in `app/db.py`. On first
> upgrade they are **backfilled once** from the previously-shared chat fields
> (`provider` / `api_key` / `api_version`), and a blank `embedding_base_url` is
> set to `base_url`, so existing single-connection deployments keep working.

## `llm_usage_events`
High-volume AI governance telemetry for LLM and embedding calls (G1a/G1b). This table is intentionally separate from `audit_events`: usage events are frequent, report-oriented, and must not copy prompts, source text, retrieved snippets, API keys, or model outputs. Context ids are nullable and use `ON DELETE SET NULL` so telemetry can remain useful after user data is deleted without preserving the deleted content.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `user_id` | INTEGER → `users(id)` SET NULL | user associated with the call, when known |
| `notebook_id` | INTEGER → `notebooks(id)` SET NULL | notebook associated with the call, when known |
| `conversation_id` | INTEGER → `conversations(id)` SET NULL | chat conversation, when known |
| `message_id` | INTEGER → `messages(id)` SET NULL | message associated with the call, when known |
| `source_id` | INTEGER → `sources(id)` SET NULL | source associated with ingest/source-summary/tool calls, when known |
| `eval_run_id` | INTEGER → `eval_runs(id)` SET NULL | eval run associated with retrieval calls, when known |
| `eval_set_id` | INTEGER → `eval_sets(id)` SET NULL | eval set associated with authoring/run calls, when known |
| `call_type` | TEXT NOT NULL | e.g. `answer`, `answer_stream`, `query_rewrite`, `embedding_query`, `embedding_passage`, `rerank`, `source_summary`, `briefing`, `compare`, `meeting_minutes`, `starter_questions`, `followups`, `eval_authoring`, `artifact_*`, `translate_summary` |
| `provider` / `model` | TEXT NOT NULL DEFAULT `''` | provider and chat/embedding model or deployment |
| `status` | TEXT NOT NULL DEFAULT `'succeeded'` | `succeeded` or `failed` |
| `latency_ms` | REAL NOT NULL DEFAULT 0 | end-to-end provider call latency |
| `prompt_tokens` / `completion_tokens` / `total_tokens` | INTEGER | normalized provider-reported usage when available, otherwise estimates |
| `input_chars` / `output_chars` | INTEGER NOT NULL DEFAULT 0 | character counts used for fallback estimates and sanity checks |
| `is_estimated` | INTEGER NOT NULL DEFAULT 1 | 0 = provider usage was present; 1 = char/4 estimate |
| `error_class` | TEXT NOT NULL DEFAULT `''` | compact exception class for failed calls |
| `metadata_json` | TEXT NOT NULL DEFAULT `'{}'` | compact scalar metadata only (e.g. text count, role, temperature, retry count, stream usage flags); prompt/source/output/API-key style keys are dropped |
| `created_at` | TEXT | |

Indexes: `idx_llm_usage_events_created`, `idx_llm_usage_events_call_created`, `idx_llm_usage_events_user_created`, `idx_llm_usage_events_notebook_created`, `idx_llm_usage_events_eval_run_created`.

Usage normalization accepts OpenAI-compatible / Azure-style `prompt_tokens`, `completion_tokens`, and `total_tokens`, common `input_tokens` / `output_tokens`, camelCase token-count fields, and nested `usage`, `token_usage`, or `tokens` objects. Streaming chat requests ask providers for usage; if an endpoint rejects `stream_options` before any output is emitted, the call is retried without stream usage and the row remains estimated.

## `ai_safety_events`
High-volume AI safety / guardrail telemetry for local scanner findings (G1c). This table is separate from both `audit_events` and `llm_usage_events`: it records review signals such as input length, invisible/control text, likely secrets, and prompt-injection-style phrases without copying raw prompts, source text, model output, retrieved snippets, or API keys. Context ids are nullable and use `ON DELETE SET NULL`.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `user_id` | INTEGER → `users(id)` SET NULL | user associated with the scanned input, when known |
| `notebook_id` | INTEGER → `notebooks(id)` SET NULL | notebook associated with the scanned input, when known |
| `conversation_id` | INTEGER → `conversations(id)` SET NULL | conversation associated with chat input, when known |
| `message_id` | INTEGER → `messages(id)` SET NULL | message associated with the event, when known |
| `source_id` | INTEGER → `sources(id)` SET NULL | source associated with the input, when known |
| `eval_run_id` | INTEGER → `eval_runs(id)` SET NULL | eval run associated with the input, when known |
| `eval_set_id` | INTEGER → `eval_sets(id)` SET NULL | eval set associated with eval-authoring input, when known |
| `event_type` | TEXT NOT NULL | e.g. `input_scan` |
| `surface` | TEXT NOT NULL DEFAULT `''` | route/workflow surface such as `chat.ask_stream`, `tool.compare_focus`, `eval_authoring.manual_question` |
| `category` | TEXT NOT NULL | `input_length`, `invisible_or_control_text`, `secret_or_credential`, `prompt_injection` |
| `severity` | TEXT NOT NULL | `medium` or `high` in the local rules MVP |
| `decision` | TEXT NOT NULL | `warn` or `block_candidate`; the MVP records signals but does not block the user flow |
| `detector_version` | TEXT NOT NULL DEFAULT `''` | local detector version, currently `local.rules.v1` |
| `rule_id` | TEXT NOT NULL DEFAULT `''` | stable local rule id |
| `content_hash` | TEXT NOT NULL DEFAULT `''` | SHA-256 of the scanned input for correlation without storing content |
| `redacted_summary` | TEXT NOT NULL DEFAULT `''` | short non-sensitive explanation |
| `metadata_json` | TEXT NOT NULL DEFAULT `'{}'` | compact scalar metadata only; prompt/source/output/API-key style keys are dropped |
| `created_at` | TEXT | |

Indexes: `idx_ai_safety_events_created`, `idx_ai_safety_events_category_created`, `idx_ai_safety_events_user_created`, `idx_ai_safety_events_notebook_created`.

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

## `retrieval_profiles`
Admin-created retrieval parameter snapshots for the in-deployment eval workbench (E1). Profiles are audit/history records **and** applyable: the `is_active` row seeds the live `ACTIVE_RETRIEVAL_PARAMS` in `app/retrieval.py` at startup (E1c apply/rollback).

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `name` | TEXT NOT NULL | display name |
| `description` | TEXT DEFAULT `''` | |
| `params_json` | TEXT DEFAULT `'{}'` | JSON snapshot of retrieval/runtime-safe parameters |
| `requires_reindex` | INTEGER DEFAULT 0 | 1 = profile contains index-affecting changes; refused at apply |
| `is_active` | INTEGER DEFAULT 0 | 1 = applied to live retrieval (at most one row); loaded into `ACTIVE_RETRIEVAL_PARAMS` on startup |
| `is_default` | INTEGER DEFAULT 0 | 1 = the protected system-default baseline; cannot be deleted (fallback to known-good config) |
| `source_run_id` | INTEGER | optional run that produced this profile |
| `created_by` | INTEGER → `users(id)` SET NULL | admin who created it |
| `created_at` / `updated_at` | TEXT | |

## `eval_sets`
Admin-managed eval set scoped to an existing notebook. `target_user_id` is the notebook owner used when running retrieval, preserving the app's per-user Chroma/SQLite filters.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `name` | TEXT NOT NULL | |
| `description` | TEXT DEFAULT `''` | |
| `target_user_id` | INTEGER NOT NULL → `users(id)` CASCADE | notebook owner / retrieval user |
| `notebook_id` | INTEGER NOT NULL → `notebooks(id)` CASCADE | |
| `created_by` | INTEGER → `users(id)` SET NULL | admin who created it |
| `created_at` / `updated_at` | TEXT | |

## `eval_items`
One approved/manual eval question. Expected evidence can be a source, a chunk, substrings, or a combination; retrieval-only metrics score against the available fields.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `eval_set_id` | INTEGER NOT NULL → `eval_sets(id)` CASCADE | |
| `question` | TEXT NOT NULL | |
| `expected_source_id` | INTEGER → `sources(id)` SET NULL | optional expected source |
| `expected_chunk_id` | INTEGER → `chunks(id)` SET NULL | optional expected chunk |
| `expected_substrings_json` | TEXT DEFAULT `'[]'` | JSON list; any matching substring counts as evidence hit |
| `item_type` | TEXT DEFAULT `'answerable'` | E1e authoring type: `answerable`, `cross_lingual`, or `unanswerable` |
| `expected_answer` | TEXT DEFAULT `''` | optional reference answer for future answer-quality judging |
| `metadata_json` | TEXT DEFAULT `'{}'` | compact authoring metadata such as origin, prompt version, model, language, source ids; no copied prompts/source text |
| `notes` | TEXT DEFAULT `''` | admin notes / rationale |
| `approved` | INTEGER DEFAULT 1 | only approved items run |
| `created_at` / `updated_at` | TEXT | |

## `eval_runs`
Background retrieval-only eval run. Progress fields drive the admin UI while the run is executing; profile/metrics JSON fields make the result immutable and reviewable later.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `eval_set_id` | INTEGER NOT NULL → `eval_sets(id)` CASCADE | |
| `profile_id` | INTEGER → `retrieval_profiles(id)` SET NULL | profile under test |
| `created_by` | INTEGER → `users(id)` SET NULL | admin who started the run |
| `status` | TEXT DEFAULT `'queued'` | `queued` → `running` → `succeeded` \| `failed` \| `cancelled` |
| `progress_current` / `progress_total` | INTEGER DEFAULT 0 | item progress |
| `current_step` | TEXT DEFAULT `''` | visible progress message |
| `profile_snapshot_json` | TEXT DEFAULT `'{}'` | immutable parameter snapshot used for this run |
| `metrics_json` | TEXT DEFAULT `'{}'` | aggregate metrics |
| `error` | TEXT DEFAULT `''` | failure summary |
| `started_at` / `finished_at` | TEXT | nullable |
| `created_at` / `updated_at` | TEXT | |

## `eval_results`
Per-question retrieval result for one eval run.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `run_id` | INTEGER NOT NULL → `eval_runs(id)` CASCADE | |
| `eval_item_id` | INTEGER NOT NULL → `eval_items(id)` CASCADE | |
| `status` | TEXT DEFAULT `'pending'` | `hit`, `miss`, `unscored`, or `error` after completion |
| `hit_rank` | INTEGER | 1-based rank when expected evidence is retrieved |
| `top_score` | REAL DEFAULT 0 | |
| `latency_ms` | REAL DEFAULT 0 | retrieval latency for this item |
| `retrieved_json` | TEXT DEFAULT `'[]'` | compact retrieved chunk summary |
| `error` | TEXT DEFAULT `''` | per-item failure |
| `created_at` | TEXT | |

## `audit_events`
Durable admin-visible audit trail for security/compliance-relevant operations. It is append-only by convention and backs `/admin/audit`. `metadata_json` must contain identifiers and compact summaries only; do **not** store API keys, full export payloads, prompts, retrieved snippets, or copied source text here.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `actor_user_id` | INTEGER → `users(id)` SET NULL | signed-in user who performed the action |
| `actor_username` | TEXT DEFAULT `''` | username snapshot, retained if the user is later deleted |
| `action` | TEXT NOT NULL | stable action key, e.g. `eval_run_export_full` |
| `target_type` | TEXT DEFAULT `''` | logical target, e.g. `eval_run`, `retrieval_profile`, `user` |
| `target_id` | INTEGER | target row id when applicable |
| `sensitivity` | TEXT DEFAULT `'normal'` | `normal` or `high` in the current UI |
| `ip_address` | TEXT DEFAULT `''` | request client host when available |
| `user_agent` | TEXT DEFAULT `''` | truncated request user-agent |
| `metadata_json` | TEXT DEFAULT `'{}'` | compact event metadata, no copied sensitive content |
| `created_at` | TEXT | |

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
| `idx_eval_sets_notebook` | eval_sets | `(notebook_id, created_at DESC)` |
| `idx_eval_items_set` | eval_items | `(eval_set_id, approved, id)` |
| `idx_eval_runs_set` | eval_runs | `(eval_set_id, created_at DESC)` |
| `idx_eval_results_run` | eval_results | `(run_id, eval_item_id)` |
| `idx_audit_events_created` | audit_events | `(created_at DESC, id DESC)` |
| `idx_audit_events_action_created` | audit_events | `(action, created_at DESC)` |
