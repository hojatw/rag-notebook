# NotebookLM RAG POC — Handover

Last updated: 2026-05-16, Asia/Taipei.

## Project location

```bash
/Users/philip_1/Repos/My/side_projects/notebooklm-rag-poc
```

Tracked in git from this commit forward (`main` branch).

## Current state

This POC has gone through a 5-phase NotebookLM-style UI/UX overhaul plus a follow-up cleanup round. See [`README.md`](README.md) for the user-facing description (features, routes, run/test, LLM settings, layout). What this file captures is the **engineering** view: what's done, what's not, what to watch out for.

### Architecture summary

- FastAPI + Jinja2 templates + Alpine.js + HTMX + marked + DOMPurify (all sprinkled via CDN — no build step).
- SQLite (`data/app.sqlite3`) for metadata; Chroma persistent store (`data/chroma/`) for vectors; local filesystem (`data/uploads/`) for original files.
- Multi-user with hashed passwords (PBKDF2-SHA256) and per-user/per-notebook scoping enforced at the route layer.
- API key encrypted at rest with Fernet (PBKDF2-SHA256 KDF over `NOTEBOOKLM_SECRET`).
- FastAPI lifespan context manager (no more `@app.on_event`).
- Defensive list caps (sources 200, conversations 50, messages 200, notes 50) with truncation hints.

### Run / test

```bash
./setup.sh                                          # one-shot env bootstrap (handles Py 3.14 + chromadb caveat)
.venv/bin/uvicorn app.main:app --reload --port 8000
.venv/bin/pytest
```

Last known verification: `pytest` 10 passed; smoke test on all routes including new account/admin/users + conversation/delete + suggestions/refresh + pin/remove cycle.

## What's done (this round)

| # | Item | Outcome |
|---|---|---|
| 1 | Pin button check `successful` | `@htmx:after-request` now guards on `$event.detail.successful` |
| 4 | Conversation delete | New `POST /notebooks/{nid}/chat/{cid}/delete` + per-row × button in conversation dropdown |
| 6 | Source preview drawer | Clicking an indexed source opens a modal listing its chunks; `GET /notebooks/{nid}/sources/{sid}/preview` returns the HTML fragment; ESC + backdrop both close. Alpine handles state, HTMX swaps content |
| 7 | `.venv` shebangs | Rebuilt via `setup.sh`; `.venv/bin/uvicorn` works directly |
| 8 | `.gitignore` | Adds `data/`, `.venv/`, `__pycache__/`, Claude per-user settings |
| 9 | Python 3.14 / chromadb dance | `setup.sh` auto-detects `onnxruntime` availability and applies the `--no-deps` fallback when needed |
| 11 | Git init | Initial commit on `main` |
| 12 | `@app.on_event` deprecation | Migrated to `@asynccontextmanager lifespan` |
| 16 | Pagination | Defensive caps + `*_truncated` flags + `.truncated-hint` chip in the UI |
| 17 | API key encryption at rest | `cryptography.Fernet`, derived from `NOTEBOOKLM_SECRET` via PBKDF2-SHA256; legacy plaintext keys pass through unchanged until next save |
| 20 | Smart Chroma sync | `sync_from_sqlite(mode='diff')` (now the startup default) computes the set diff between SQLite and Chroma, upserts only what's missing and deletes orphans. `mode='full'` re-upserts everything; admin uses it via Rebuild. Aligned-state startups now do zero work (~200ms vs ~600ms before) |
| 21 | Account management | `/account` (own password), `/admin/users` list + create + reset-password + toggle-admin + delete (refuses self-delete and last-admin demotion) |
| 22 | (deferred — explained) | No tests added; design lives in this handover. Concrete next step: build a `tests/eval_retrieval.py` with `{question, expected_source, expected_chunk_substring}` JSON fixtures, compute recall@5 + MRR |
| 23 | Admin vector-index page | `/admin/index` shows SQLite vs Chroma counts + missing/orphan deltas + in-sync verdict; *Rebuild* triggers full sync, *Clear* wipes the collection. New topbar entry "Index" for admins |
| 24 | Form lock-out | `data-loading-form` now also disables every non-hidden input/textarea/select on the form, not just the submit button |

## Deferred follow-ups (carried over from previous handovers)

### Known bugs / UX polish

- **#2 Legacy citation `source_id` backfill.** Old assistant messages stored before the `citation_payload` change have no `source_id`. Frontend falls back to filename match — works as long as filenames are unique within the notebook and not renamed. One-shot backfill script welcome.
- **#3 `<details>` dropdowns don't close on outside click.** Conversation switcher and Notebook ▾ menu need a manual second click to close. Replace native `<details>` with Alpine `x-data="{ open: false }" @click.outside="open = false"`.
- **#5 Suggestions caching.** Every "Generate suggestions" click hits the LLM. Cache the result in `notebooks` (new `suggestions_json` + `suggestions_at` columns) with a TTL; "Refresh" still forces regeneration.
- **#10 Pin CDN versions of Alpine/HTMX/marked/DOMPurify.** Already pinned in `base.html`; if the CDN ever ships a breaking patch on those exact paths, the app silently breaks. Document fallback or self-host.

### Architectural debt (production hardening)

- **#13 Background ingest uses FastAPI BackgroundTasks.** Restart drops the queue; no retries; no cross-process. Migrate to Celery / RQ / Arq / Dramatiq.
- **#14 LLM / embedding HTTP calls lack retry / backoff.** One 5xx fails the user's whole ask. Wrap `embed_text_batch` and `chat_completion` with tenacity (already in deps via chromadb) — exponential backoff, max 3 attempts.
- **#15 No CSRF protection.** Cookie session + form POSTs are vulnerable to CSRF. Add either a per-session token in forms or switch sensitive endpoints to require an `Origin` header check.
- **#18 No streaming responses.** Long answers arrive in one chunk after the full LLM call returns. SSE / chunked-streaming would give a typing effect; requires re-shaping `chat_completion` to async generator and updating the chat form to read the stream.
- **#19 Local embedding fallback misleading.** When no LLM key is configured, `local_embedding()` (deterministic hash) produces vectors that "work" but give terrible recall. Either disallow uploads with no embedding model, or show a prominent banner in `/settings`.

### Discussed but not done

- **#22 No retrieval eval set.** Any change to query rewrite / hybrid scoring / rerank is unmeasurable. Build a small `tests/eval_retrieval.py` with `{question, expected_source, expected_chunk_substring}` JSON fixtures using the already-indexed sample documents, compute recall@5 + MRR. Becomes essential the moment anyone tunes retrieval.

## Important files

```text
app/main.py            Routes, auth, retrieval orchestration, lifespan, logging.
app/db.py              SQLite schema, default-notebook migration, load_llm_settings (decrypts).
app/ingest.py          Text extraction, chunking, vector upsert.
app/llm.py             LLM/embedding HTTP, query rewrite, rerank, starter questions.
app/vector_store.py    Chroma persistent client, diff sync, index_status, clear_all_vectors.
app/security.py        Password hashing, signed sessions, Fernet encryption helpers.
app/templates/         Jinja templates: base, home, notebook (with preview modal), login, settings,
                       account, admin_users, admin_index, error, plus _source_item / _source_picker /
                       _source_preview / _suggestions / _notes_section partials.
app/static/            style.css (design tokens + components + modal + admin-index stats),
                       app.js (binders, Alpine dropzone, Markdown render, citation click, suggestion fill, pin reset).
tests/                 test_core.py, test_llm.py, test_security.py, test_vector_store.py.
setup.sh               One-shot env bootstrap. New machines / fresh clones should use it.
data/                  app.sqlite3, uploads/, chroma/. Gitignored.
logs/app.log           Rotating app log. Gitignored.
.claude/launch.json    Claude Preview server config. Committed.
```

## Persistence

```text
data/app.sqlite3       Users, llm_settings, notebooks, sources, chunks, conversations, messages, notes.
data/uploads/{uid}/    Per-user original files.
data/chroma/           Persistent Chroma index.
logs/app.log           Rotating app log.
```

`data/` and `logs/` are gitignored. A wiped checkout regenerates everything from `init_db()` + Chroma startup.

## Default accounts

```text
admin / admin123
user  / user123
```

Admins can now create, rename, promote/demote, reset, and delete other users from `/admin/users`. Both can change their own password at `/account`.

## Environment variables

```text
NOTEBOOKLM_SECRET=dev-secret-change-me    # session cookie signing + API-key encryption KDF
NOTEBOOKLM_DATA_DIR=data
NOTEBOOKLM_LOG_LEVEL=INFO
NOTEBOOKLM_LOG_FILE=logs/app.log
NOTEBOOKLM_LOG_MAX_BYTES=5242880
NOTEBOOKLM_LOG_BACKUP_COUNT=5
```

Changing `NOTEBOOKLM_SECRET` invalidates every encrypted API key (you'll need to re-enter it from `/settings`).

## Caveats

- Python 3.14 + chromadb: `setup.sh` handles. Don't `pip install -r requirements.txt` blindly on 3.14 — `chromadb==1.5.9; python_version < "3.14"` in `requirements.txt` would skip Chroma entirely.
- Lifespan deprecation: handled (`@app.on_event` removed). FastAPI's own `asyncio.iscoroutinefunction` DeprecationWarning remains (their bug, not ours).
- API keys live encrypted in `llm_settings.api_key`. Decryption happens in `load_llm_settings()` (db.py) — call it instead of `SELECT * FROM llm_settings` if you need the plaintext.
- Chroma startup sync is now diff-based. If you suspect index drift, hit `/admin/index` and click *Rebuild*; if you want a clean slate, click *Clear* then *Rebuild*.
