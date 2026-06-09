# NotebookLM-style RAG POC

A single-machine FastAPI proof of concept for a NotebookLM-style workspace: organise sources into **notebooks**, ground chats against the sources you select, and pin the answers worth keeping.

## Status

This is a proof of concept, not a production-ready service. It is suitable for local experiments and small single-machine deployments after you configure a real `NOTEBOOKLM_SECRET`, but several production hardening items are still open (CSRF protection, worker-backed ingest, LLM retry/backoff, streaming responses). See [Known follow-ups](#known-follow-ups).

## What you get

- **Notebook home grid.** Each notebook owns its own sources, conversations, and pinned notes.
- **Three-pane workspace** per notebook:
  - **Sources** (left): drag-and-drop upload, automatic indexing-status polling, per-source reindex/delete, **click any indexed source to open a chunk-preview drawer**.
  - **Chat** (centre): grounded chat with conversation switcher (with per-row delete), Markdown-rendered answers, and inline `[1]` `[2]` citation chips that scroll the matching source into view.
  - **Studio** (right): four NotebookLM-style helpers in one column.
      - *Suggested questions* — one-click LLM-authored starter questions from your sources, auto-refreshing as sources finish indexing (24 h cache).
      - *Briefing* — one-paragraph cross-source synthesis, auto-generated on first notebook view and cached for 24 h. *Regenerate* on demand. Concurrent generation across tabs / sibling source completions is deduped by an in-process lock so a 5-file upload only calls the LLM once, not five times.
      - *Compare sources* — pick 2+ indexed sources (with an optional focus hint) and the model produces a Shared / Distinct / Contradictions markdown report. *Save to notes* keeps it for later.
      - *Notes* — *Pin* assistant answers into collapsible notes (removing a note un-pins the source message automatically); comparison results can be saved here too.
  - **Per-source summary** — every uploaded source gets a 2–4 sentence TL;DR generated automatically right after indexing, shown at the top of the preview drawer and reused as compact context for Briefing / Compare.
- **Hybrid retrieval**: query rewriting, Chroma vector search, SQLite keyword matching, and LLM reranking. Below a configurable confidence threshold the model is asked to abstain rather than hallucinate. See [`RETRIEVAL.md`](RETRIEVAL.md) for the full pipeline, tuning knobs, and eval workflow.
- **Per-message debug pane**: chat answers ship with a collapsible "📊 N chunks · retrieved Xms · generated Yms · top score Z" badge that opens a table of vector / keyword / rerank / final scores per citation.
- **Retrieval eval harness** (`tests/eval_retrieval.py`) with starter questions for the demo notebook so changes to query rewrite / hybrid scoring / rerank can be measured (recall@k, MRR).
- **Multi-user** with hashed passwords and strict per-user/per-notebook isolation. Admin can manage user accounts at `/admin/users`; any signed-in user can change their own password at `/account`.
- **OpenAI-compatible** (including local Ollama / vLLM / TEI) and **Azure OpenAI** chat + embedding providers, configured by an admin in `/settings`. Chat and embedding endpoints can live on different services via the optional **Embedding base URL** field. **API keys are encrypted at rest** with Fernet (PBKDF2-SHA256 over `NOTEBOOKLM_SECRET`). On save the embedding endpoint is probed once; dim mismatches with the existing Chroma index are rejected with a clear "Clear at /admin/index first" message.
- **Admin vector-index console** at `/admin/index`: SQLite ↔ Chroma drift report, manual *Rebuild* and *Clear*.
- **Diff-only Chroma sync on startup**: only missing chunks are upserted and orphan vectors deleted; same-state restarts are near-instant.
- **Source formats**: PDF, TXT, Markdown, DOCX, HTML.
- **Persistence**: SQLite for metadata, the local filesystem for uploads, and Chroma for vectors.
- **Defensive list caps** (sources 200, conversations 50, messages 200, notes 50) with truncation hints in the UI.
- **Logging** to stdout and `logs/app.log` with rotation.

The frontend stays server-rendered (Jinja templates) and sprinkles in Alpine.js, HTMX, marked, and DOMPurify (all self-hosted in `app/static/vendor/`) — no build step, no npm, no CDN dependency.

## Run

Local quickstart can opt into the built-in insecure development secret. Do not use this mode for a network-exposed deployment.

```bash
cd notebooklm-rag-poc
./setup.sh                                              # builds a Python 3.12 .venv, matching Docker
NOTEBOOKLM_ALLOW_INSECURE_DEV_SECRET=1 .venv/bin/uvicorn app.main:app --reload --port 8000
```

`setup.sh --force` wipes any existing `.venv` first. The script uses `python3.12` by default, or `PYTHON_BIN=/path/to/python3.12 ./setup.sh` if your Python 3.12 binary has a different name.

Open `http://127.0.0.1:8000` and sign in:

- Admin: `admin` / `admin123`
- User: `user` / `user123`

These demo accounts are for local development only. Change or remove them before exposing the app on a network.

On first launch any legacy data is migrated into a default notebook called *My Notebook*. New users start with an empty notebook grid and create their own with **+ New notebook**.

### Docker (recommended for deployment)

```bash
cp .env.example .env       # then fill NOTEBOOKLM_SECRET
docker compose up --build -d
docker compose logs -f
```

Docker Compose requires `NOTEBOOKLM_SECRET` in `.env`; the app fails closed when it is missing. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

The compose file bind-mounts `./data` (SQLite + uploads + Chroma index) and `./logs` (rotating app log) from the repo root so that `docker compose down && docker compose up --build` preserves all user state. Backup is one `tar czf data-$(date +%F).tar.gz data/` away.

**Upgrade flow** (zero data loss):

```bash
git pull
docker compose up --build -d
```

**Reset** (CAUTION — deletes users, notebooks, vectors, uploads):

```bash
docker compose down
rm -rf data/ logs/
```

The image uses the same Python 3.12 runtime as local development. Default port is 8000 (override via `HOST_PORT` in `.env`).

### Python Runtime

Local development and Docker both use Python 3.12. Keeping them aligned avoids platform-specific native-wheel gaps in dependencies such as `onnxruntime`, which ChromaDB declares as a required dependency. The repo includes `.python-version` as a hint for version managers, but `setup.sh` only requires a working `python3.12` on `PATH`.

## LLM settings

Set the LLM connection at `/settings` while signed in as admin. Both chat and embeddings require a configured OpenAI-compatible (or Azure OpenAI) endpoint — there is no longer an offline-hash fallback for embeddings. The upload form is disabled until the embedding model is configured. The save handler probes the embedding endpoint once to validate connectivity and detect dimension mismatches against the existing Chroma index.

OpenAI-compatible:

```text
Provider:           OpenAI-compatible
Base URL:           https://api.openai.com/v1
Embedding base URL: (blank — share the chat URL)
API key:            sk-...
Chat model:         gpt-4.1-mini
Embedding model:    text-embedding-3-small
Temperature:        0.2
Timeout seconds:    60
```

Local-model setup (vLLM for chat + Ollama for embeddings on different ports):

```text
Provider:           OpenAI-compatible
Base URL:           http://localhost:8000/v1     (vLLM chat)
Embedding base URL: http://localhost:11434/v1    (Ollama embeddings)
API key:            EMPTY                        (any non-empty string)
Chat model:         meta-llama/Meta-Llama-3.1-8B-Instruct
Embedding model:    nomic-embed-text
Timeout seconds:    120                          (cold-load can be slow)
```

Single-Ollama setup (chat + embeddings both via Ollama):

```text
Provider:           OpenAI-compatible
Base URL:           http://localhost:11434/v1
Embedding base URL: (blank)
API key:            ollama
Chat model:         llama3.1:8b
Embedding model:    nomic-embed-text
Timeout seconds:    120
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
NOTEBOOKLM_SECRET=replace-me-with-a-long-random-string
NOTEBOOKLM_ALLOW_INSECURE_DEV_SECRET=1  # local-only opt-in when NOTEBOOKLM_SECRET is unset
```

`NOTEBOOKLM_SECRET` is required by default. For local-only quick starts you can
set `NOTEBOOKLM_ALLOW_INSECURE_DEV_SECRET=1` to use the built-in development
secret explicitly; do not set that flag in production.

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
GET  /notebooks/{id}/_briefing                            HTMX swap: briefing section (dedupes concurrent generation)
POST /notebooks/{id}/briefing[?force=1]                   generate / regenerate notebook briefing
GET  /notebooks/{id}/_compare                             HTMX swap: compare-sources section
POST /notebooks/{id}/compare                              compare 2+ sources (returns result fragment)

POST /notebooks/{id}/notes/pin                            pin assistant message into notes
POST /notebooks/{id}/notes/add                            save a raw note (title + content)
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

### Retrieval eval

`tests/eval_questions.json` holds ~25 ground-truth questions targeting the demo notebook. Run the harness to score the live retrieval pipeline:

```bash
.venv/bin/python -m tests.eval_retrieval                # default: top-k=5, rerank on
.venv/bin/python -m tests.eval_retrieval --no-rerank    # skip LLM rerank for a hybrid-only baseline
.venv/bin/python -m tests.eval_retrieval --top-k 10
```

The harness reports per-question hit rank, **Recall@k**, and **MRR**. Edit `tests/eval_questions.json` to add questions about your own indexed sources. The harness skips when no LLM key is configured.

## Layout

```text
app/main.py            Routes, auth, retrieval orchestration, lifespan, logging.
app/db.py              SQLite schema, default-notebook migration, load_llm_settings (decrypts API key).
app/ingest.py          Text extraction, chunking, vector upsert.
app/llm.py             LLM/embedding HTTP, query rewrite, rerank, starter questions.
app/vector_store.py    Chroma persistent client + diff sync + index_status + clear_all_vectors.
app/security.py        Password hashing, signed session cookies, Fernet encryption for API keys.
app/templates/
  base.html            Topbar, breadcrumbs, self-hosted vendor scripts (marked, DOMPurify, HTMX, Alpine).
  home.html            Notebook grid.
  notebook.html        Three-pane workspace shell + source preview modal.
  _source_item.html    Single source list item (HTMX polling target + preview trigger).
  _source_preview.html Chunks list rendered inside the preview modal.
  _source_picker.html  Chat-form source-checkbox fieldset (HTMX swap target).
  _suggestions.html    Studio suggestions section (HTMX swap target).
  _briefing.html       Studio briefing section (HTMX swap target; auto-fires POST on first view).
  _compare.html        Studio compare-sources panel (checkbox list + focus input).
  _compare_result.html Comparison result fragment (markdown body + Save-to-notes form).
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
  test_chunking.py     Sentence-aware chunker: CJK detection, splitting, overlap, long-sentence fallback.
  test_llm.py          Provider request shapes, parsing, Studio helper short-circuits (summary / briefing / compare).
  test_security.py     Fernet round-trip + legacy plaintext + wrong-secret behaviour.
  test_vector_store.py Index status + diff/full sync + clear, all against a real Chroma temp dir.
  test_extract.py      Source extraction (PDF, DOCX, HTML edge cases).
  test_briefing_lock.py In-process briefing lock: acquire / release / stale timeout.
  eval_questions.json  Ground-truth retrieval Qs for the demo notebook.
  eval_retrieval.py    Recall@k + MRR harness (see RETRIEVAL.md).

Runtime-generated, gitignored:
data/
  app.sqlite3          SQLite metadata (users, notebooks, sources, chunks, conversations, messages, notes, llm_settings).
  uploads/             Per-user original files.
  chroma/              Vector index.
logs/app.log           Rotating app log.
setup.sh               One-shot Python 3.12 env bootstrap.
requirements.txt       Runtime dependencies used by Docker.
requirements-dev.txt   Local development/test dependencies layered on runtime.
```

## Known follow-ups

Performance / scalability work is tracked as a prioritised, tick-off backlog in [`PERFORMANCE.md`](PERFORMANCE.md) (issue → impact → fix → priority). Headline items still outstanding:

- No streaming responses yet — answers arrive after the full LLM call returns.
- Background ingest uses FastAPI background tasks rather than a worker queue.
- No CSRF protection on POST routes.
- No LLM/embedding HTTP retry / backoff.
- No offline embedding fallback — embedding model must be configured before uploads are accepted.
- Keyword search uses `LIKE '%token%'` over SQLite; FTS5 + BM25 is on deck (see [`RETRIEVAL.md`](RETRIEVAL.md)).
- Hybrid merge uses a fixed `0.7·vector + 0.3·keyword` blend; Reciprocal Rank Fusion is on deck.
- Qdrant is a future vector-store evaluation candidate; do a bounded spike before replacing Chroma.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
