# NotebookLM-style RAG POC

A single-machine FastAPI proof of concept for a NotebookLM-style workspace: organise sources into **notebooks**, ground chats against the sources you select, and pin the answers worth keeping.

## What you get

- **Notebook home grid.** Each notebook owns its own sources, conversations, and pinned notes.
- **Three-pane workspace** per notebook:
  - **Sources** (left): drag-and-drop upload, automatic indexing-status polling, per-source reindex/delete, **click any indexed source to open a chunk-preview drawer**.
  - **Chat** (centre): grounded chat with conversation switcher (with per-row delete), Markdown-rendered answers, and inline `[1]` `[2]` citation chips that scroll the matching source into view.
  - **Studio** (right): one-click *Generate suggestions* (LLM-authored starter questions from your sources, auto-refreshing as sources finish indexing) and *Pin* assistant answers into collapsible notes (removing a note un-pins the source message automatically).
- **Hybrid retrieval**: query rewriting, Chroma vector search, SQLite keyword matching, and LLM reranking.
- **Multi-user** with hashed passwords and strict per-user/per-notebook isolation. Admin can manage user accounts at `/admin/users`; any signed-in user can change their own password at `/account`.
- **OpenAI-compatible** and **Azure OpenAI** chat + embedding providers, configured by an admin in `/settings`. **API keys are encrypted at rest** with Fernet (PBKDF2-SHA256 over `NOTEBOOKLM_SECRET`).
- **Admin vector-index console** at `/admin/index`: SQLite ↔ Chroma drift report, manual *Rebuild* and *Clear*.
- **Diff-only Chroma sync on startup**: only missing chunks are upserted and orphan vectors deleted; same-state restarts are near-instant.
- **Source formats**: PDF, TXT, Markdown, DOCX, HTML.
- **Persistence**: SQLite for metadata, the local filesystem for uploads, and Chroma for vectors.
- **Defensive list caps** (sources 200, conversations 50, messages 200, notes 50) with truncation hints in the UI.
- **Logging** to stdout and `logs/app.log` with rotation.

The frontend stays server-rendered (Jinja templates) and sprinkles in Alpine.js, HTMX, marked, and DOMPurify via CDN — no build step, no npm.

## Run

```bash
cd /Users/philip_1/Repos/My/side_projects/notebooklm-rag-poc
./setup.sh                                              # builds .venv and handles the chromadb caveat below
.venv/bin/uvicorn app.main:app --reload --port 8000
```

`setup.sh --force` wipes any existing `.venv` first. The script auto-detects whether `onnxruntime` has a wheel for the active Python version and falls back to the `chromadb --no-deps` install path if it does not.

Open `http://127.0.0.1:8000` and sign in:

- Admin: `admin` / `admin123`
- User: `user` / `user123`

On first launch any legacy data is migrated into a default notebook called *My Notebook*. New users start with an empty notebook grid and create their own with **+ New notebook**.

### Python 3.14 + ChromaDB caveat

`chromadb==1.5.9` depends on `onnxruntime`, which currently has no Python 3.14 wheel. The included [`setup.sh`](setup.sh) handles this — it installs Chroma without its embedding-function dependency and then adds the runtime extras Chroma actually needs. `requirements.txt` constrains `chromadb` to Python < 3.14 so a plain `pip install -r requirements.txt` does not break on 3.14; rely on `setup.sh` instead.

## LLM settings

Set the LLM connection at `/settings` while signed in as admin. Embeddings fall back to a deterministic local hash when no API key is configured (useful for offline demos); chat requires real LLM credentials.

OpenAI-compatible:

```text
Provider: OpenAI-compatible
Base URL: https://api.openai.com/v1
API key: sk-...
Chat model: gpt-4.1-mini
Embedding model: text-embedding-3-small
Temperature: 0.2
Timeout seconds: 60
```

Azure OpenAI:

```text
Provider: Azure OpenAI
Base URL / Azure endpoint: https://my-resource.openai.azure.com
API key: your Azure OpenAI key
Chat model / Azure chat deployment: my-gpt-4o-mini-deployment
Embedding model / Azure embedding deployment: my-text-embedding-3-small-deployment
Azure API version: 2024-02-15-preview
Temperature: 0.2
Timeout seconds: 60
```

## Logging

```bash
tail -f logs/app.log
NOTEBOOKLM_LOG_LEVEL=DEBUG .venv/bin/uvicorn app.main:app --reload --port 8000
```

Tunable environment variables:

```text
NOTEBOOKLM_LOG_LEVEL=INFO
NOTEBOOKLM_LOG_FILE=logs/app.log
NOTEBOOKLM_LOG_MAX_BYTES=5242880
NOTEBOOKLM_LOG_BACKUP_COUNT=5
NOTEBOOKLM_DATA_DIR=data
NOTEBOOKLM_SECRET=dev-secret-change-me
```

The app records: startup/shutdown, every HTTP request with status and elapsed time, login attempts, source upload/index/reindex/delete, embedding API calls, Chroma upsert/query, query rewriting, retrieval and rerank, chat success/failure, notebook and note CRUD, and exceptions with stack traces.

## Routes

```text
GET  /                                                    redirect to /notebooks (or /login)
GET  /login                                               sign-in page
POST /login                                               authenticate
POST /logout                                              clear session

GET  /notebooks                                           notebook grid
POST /notebooks/new                                       create a notebook
GET  /notebooks/{id}                                      three-pane workspace
POST /notebooks/{id}/rename                               rename / change emoji
POST /notebooks/{id}/delete                               delete (cascades sources + chats + notes)

POST /notebooks/{id}/sources/upload                       upload + queue ingest
POST /notebooks/{id}/sources/{sid}/reindex                requeue ingest
POST /notebooks/{id}/sources/{sid}/delete                 delete source + vectors + file
GET  /notebooks/{id}/sources/{sid}/_partial               HTMX polling: source row
GET  /notebooks/{id}/sources/{sid}/preview                source preview drawer (chunk list)
GET  /notebooks/{id}/_source-picker                       HTMX swap: chat-form picker

POST /notebooks/{id}/chat/new                             new conversation
POST /notebooks/{id}/chat/ask                             ask a question
POST /notebooks/{id}/chat/{cid}/delete                    delete a conversation

GET  /notebooks/{id}/_suggestions                         HTMX swap: suggestions section
POST /notebooks/{id}/suggestions                          generate 4 starter questions

POST /notebooks/{id}/notes/pin                            pin assistant message into notes
POST /notebooks/{id}/notes/{note_id}/delete               remove pinned note (also broadcasts pin-cleared)

GET  /account                                             change own password
POST /account/password                                    save new password

GET  /admin/users                                         user list (admin only)
POST /admin/users/new                                     create user
POST /admin/users/{uid}/reset-password                    set a new password
POST /admin/users/{uid}/toggle-admin                      promote / demote
POST /admin/users/{uid}/delete                            cascade-delete a user

GET  /admin/index                                         Chroma index health page (admin only)
POST /admin/index/rebuild                                 full re-upsert of every SQLite chunk
POST /admin/index/clear                                   delete every Chroma vector

GET  /settings                                            admin LLM settings (admin only)
POST /settings                                            save LLM settings (API key is encrypted on write)
```

## Test

```bash
.venv/bin/pytest
.venv/bin/python -m py_compile app/*.py tests/*.py
```

## Layout

```text
app/main.py            Routes, auth, retrieval orchestration, lifespan, logging.
app/db.py              SQLite schema, default-notebook migration, load_llm_settings (decrypts API key).
app/ingest.py          Text extraction, chunking, vector upsert.
app/llm.py             LLM/embedding HTTP, query rewrite, rerank, starter questions.
app/vector_store.py    Chroma persistent client + diff sync + index_status + clear_all_vectors.
app/security.py        Password hashing, signed session cookies, Fernet encryption for API keys.
app/templates/
  base.html            Topbar, breadcrumbs, CDN scripts (marked, DOMPurify, HTMX, Alpine).
  home.html            Notebook grid.
  notebook.html        Three-pane workspace shell + source preview modal.
  _source_item.html    Single source list item (HTMX polling target + preview trigger).
  _source_preview.html Chunks list rendered inside the preview modal.
  _source_picker.html  Chat-form source-checkbox fieldset (HTMX swap target).
  _suggestions.html    Studio suggestions section (HTMX swap target).
  _notes_section.html  Studio notes section (HTMX swap target).
  account.html         Per-user password change page.
  admin_users.html     Admin user management page.
  admin_index.html     Admin vector-index health page.
  login.html, settings.html, error.html
app/static/
  style.css            Design tokens + components + modal + admin index stats.
  app.js               Bindings, Alpine dropzone, Markdown render, citation click, suggestion fill, pin reset.
tests/
  test_core.py         Hash, ingest, isolation, retrieval, notebook migration, pin idempotency, settings decryption.
  test_llm.py          Provider request shapes, parsing.
  test_security.py     Fernet round-trip + legacy plaintext + wrong-secret behaviour.
  test_vector_store.py Index status + diff/full sync + clear, all against a real Chroma temp dir.
data/
  app.sqlite3          SQLite metadata (users, notebooks, sources, chunks, conversations, messages, notes, llm_settings).
  uploads/             Per-user original files.
  chroma/              Vector index.
logs/app.log           Rotating app log.
setup.sh               One-shot env bootstrap (handles Py 3.14 chromadb caveat).
```

## Known follow-ups

See [handover.md](handover.md) for the full deferred-work list with context. Headline items still outstanding:

- No streaming responses yet — answers arrive after the full LLM call returns.
- Background ingest uses FastAPI background tasks rather than a worker queue.
- No CSRF protection on POST routes.
- No LLM/embedding HTTP retry / backoff.
- Local-embedding fallback (when no LLM key is configured) is a deterministic hash — usable for offline demos only, gives poor recall.
- No retrieval evaluation harness yet.
