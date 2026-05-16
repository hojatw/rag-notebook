# NotebookLM-style RAG POC

A single-machine FastAPI proof of concept for a NotebookLM-style workspace: organise sources into **notebooks**, ground chats against the sources you select, and pin the answers worth keeping.

## What you get

- **Notebook home grid.** Each notebook owns its own sources, conversations, and pinned notes.
- **Three-pane workspace** per notebook:
  - **Sources** (left): drag-and-drop upload, automatic indexing-status polling, per-source reindex/delete.
  - **Chat** (centre): grounded chat with conversation switcher, Markdown-rendered answers, and inline `[1]` `[2]` citation chips that scroll the matching source into view.
  - **Studio** (right): one-click *Generate suggestions* (LLM-authored starter questions from your sources) and *Pin* assistant answers into collapsible notes.
- **Hybrid retrieval**: query rewriting, Chroma vector search, SQLite keyword matching, and LLM reranking.
- **Multi-user** with hashed passwords and strict per-user/per-notebook isolation.
- **OpenAI-compatible** and **Azure OpenAI** chat + embedding providers, configured by an admin in `/settings`.
- **Source formats**: PDF, TXT, Markdown, DOCX, HTML.
- **Persistence**: SQLite for metadata, the local filesystem for uploads, and Chroma for vectors.
- **Logging** to stdout and `logs/app.log` with rotation.

The frontend stays server-rendered (Jinja templates) and sprinkles in Alpine.js, HTMX, marked, and DOMPurify via CDN — no build step, no npm.

## Run

```bash
cd /Users/philip_1/Repos/My/side_projects/notebooklm-rag-poc
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload --port 8000
```

Open `http://127.0.0.1:8000` and sign in:

- Admin: `admin` / `admin123`
- User: `user` / `user123`

On first launch any legacy data is migrated into a default notebook called *My Notebook*. New users start with an empty notebook grid and create their own with **+ New notebook**.

### Python 3.14 + ChromaDB caveat

`chromadb==1.5.9` depends on `onnxruntime`, which has no Python 3.14 wheel yet. Install Chroma without its embedding-function dependency, then add the runtime extras manually:

```bash
.venv/bin/python -m pip install chromadb==1.5.9 --no-deps
.venv/bin/python -m pip install numpy==2.4.4 pydantic-settings==2.14.1 pybase64==1.4.3
.venv/bin/python -m pip install overrides jsonschema mmh3 orjson pypika tenacity typer tqdm rich \
    importlib-resources build bcrypt grpcio tokenizers \
    opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc kubernetes
```

`requirements.txt` already constrains `chromadb` to Python < 3.14 to avoid breaking installs on other versions.

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
GET  /notebooks/{id}/_source-picker                       HTMX swap: chat-form picker

POST /notebooks/{id}/chat/new                             new conversation
POST /notebooks/{id}/chat/ask                             ask a question

GET  /notebooks/{id}/_suggestions                         HTMX swap: suggestions section
POST /notebooks/{id}/suggestions                          generate 4 starter questions

POST /notebooks/{id}/notes/pin                            pin assistant message into notes
POST /notebooks/{id}/notes/{note_id}/delete               remove pinned note

GET  /settings                                            admin LLM settings (admin only)
POST /settings                                            save LLM settings
```

## Test

```bash
.venv/bin/pytest
.venv/bin/python -m py_compile app/*.py tests/*.py
```

## Layout

```text
app/main.py            Routes, auth, retrieval orchestration, logging.
app/db.py              SQLite schema, default-notebook migration, helpers.
app/ingest.py          Text extraction, chunking, vector upsert.
app/llm.py             LLM/embedding HTTP, query rewrite, rerank, starter questions.
app/vector_store.py    Chroma persistent client wrapper.
app/security.py        Password hashing + signed session cookies.
app/templates/
  base.html            Topbar, breadcrumbs, CDN scripts (marked, DOMPurify, HTMX, Alpine).
  home.html            Notebook grid.
  notebook.html        Three-pane workspace shell.
  _source_item.html    Single source list item (HTMX polling target).
  _source_picker.html  Chat-form source-checkbox fieldset (HTMX swap target).
  _suggestions.html    Studio suggestions section (HTMX swap target).
  _notes_section.html  Studio notes section (HTMX swap target).
  login.html, settings.html, error.html
app/static/
  style.css            Design tokens + components.
  app.js               Markdown render, citation click, drag-drop, suggestion fill, pin reset.
tests/
  test_core.py         Hash, ingest, isolation, retrieval.
  test_llm.py          Provider request shapes, parsing.
data/
  app.sqlite3          SQLite metadata (users, notebooks, sources, chunks, conversations, messages, notes, settings).
  uploads/             Per-user original files.
  chroma/              Vector index.
logs/app.log           Rotating app log.
```

## Known follow-ups

- `@app.on_event` deprecation — migrate to FastAPI lifespan.
- Python 3.14 reproducibility depends on the manual ChromaDB install above; pin to 3.12/3.13 if you need a clean `pip install -r requirements.txt`.
- No streaming responses yet — answers arrive after the full LLM call returns.
- Background ingest uses FastAPI background tasks rather than a worker queue.
- No CSRF, no pagination on large source/conversation/message lists, no per-user secret storage.
