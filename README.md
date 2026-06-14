# NotebookLM-style RAG POC

A single-machine FastAPI proof of concept for a NotebookLM-style workspace: organise sources into **notebooks**, ground chats against the sources you select, and pin the answers worth keeping.

## Status

This is a proof of concept, not a production-ready service. It is suitable for local experiments and small single-machine deployments after you configure a real `NOTEBOOKLM_SECRET`, but several production hardening items are still open (CSRF protection). See [Known follow-ups](#known-follow-ups).

## What you get

- **Notebook home grid.** Each notebook owns its own sources, conversations, and pinned notes.
- **Three-pane workspace** per notebook:
  - **Sources** (left): drag-and-drop upload with selected-file/size feedback, automatic indexing-status polling, per-source reindex/delete, **click any indexed source to open a chunk-preview drawer**.
  - **Chat** (centre): grounded chat with streaming answers, conversation switcher (rename, message counts, relative timestamps, per-row delete, and **Markdown export**), Markdown-rendered answers (with a per-answer **copy** button), and inline `[1]` `[2]` citation chips that open the source preview drawer scrolled to and highlighting the exact cited chunk. Asking stays in place with progressive status updates; after each answered question 2–3 **follow-up question chips** are suggested (lazy-generated, cached per message). The empty state hosts **starter questions** — one-click LLM-authored questions from your sources, auto-refreshing as sources finish indexing (24 h cache). Enter sends, Shift+Enter inserts a newline, IME-safe for CJK input. UI strings are Traditional Chinese.
  - **Studio** (right): a NotebookLM-style work area split into ambient context, a tools launcher, and an outputs shelf (the "Studio IA" restructure — see `ROADMAP.md` U16).
      - *Briefing strip* — a slim, one-line expandable cross-source synthesis, auto-generated on first notebook view and cached for 24 h. *Regenerate* on demand. Concurrent generation across tabs / sibling source completions is deduped by a shared SQLite-backed lock so a 5-file upload only calls the LLM once, not five times (works across multiple workers).
      - *Tools* — a tile grid; each tile opens its config in the preview-modal, runs, and shows the result with a **manual save-to-notes** button (the user decides what lands in the shelf — no auto-save):
          - *Compare sources* — pick 2+ indexed sources (with an optional focus hint) → a Shared / Distinct / Contradictions markdown report.
          - *Meeting minutes* — pick one indexed source (a transcript) → structured minutes (topic / decisions / action items / follow-ups / open questions); a non-meeting source shows the model's reason and offers no save.
          - *Study guide / FAQ / Timeline* — generated across the notebook's source summaries (A4).
          - *Translate summary* — translate one source's summary into a target language (A5).
      - *Outputs & notes shelf* — *Pin* assistant answers into collapsible notes (removing a note un-pins the source message automatically); every tool result you save lands here too; notes are **inline-editable**; **export all notes as Markdown**.
  - **Per-source summary** — every uploaded source gets a 2–4 sentence TL;DR generated automatically right after indexing, shown at the top of the preview drawer and reused as compact context for Briefing / Compare / artifacts.
- **Hybrid retrieval**: query rewriting, Chroma vector search, SQLite keyword matching, and LLM reranking. Below a configurable confidence threshold the model is asked to abstain rather than hallucinate. See [`RETRIEVAL.md`](docs/RETRIEVAL.md) for the full pipeline, tuning knobs, and eval workflow.
- **Per-message debug pane**: chat answers ship with a collapsible "📊 N chunks · retrieved Xms · generated Yms · top score Z" badge that opens a table of vector / keyword / rerank / final scores per citation.
- **Retrieval eval harness** (`tests/eval_retrieval.py`) with starter questions for the demo notebook so changes to query rewrite / hybrid scoring / rerank can be measured (recall@k, MRR).
- **Multi-user** with hashed passwords and strict per-user/per-notebook isolation. Admin can manage user accounts at `/admin/users`; any signed-in user can change their own password at `/account`.
- **OpenAI-compatible** (including local Ollama / vLLM / TEI) and **Azure OpenAI** chat + embedding providers, configured by an admin in `/settings`. Chat and embedding endpoints can live on different services via the optional **Embedding base URL** field. **API keys are encrypted at rest** with Fernet (PBKDF2-SHA256 over `NOTEBOOKLM_SECRET`). On save the embedding endpoint is probed once; dim mismatches with the existing Chroma index are rejected with a clear "Clear at /admin/index first" message.
- **Admin vector-index console** at `/admin/index`: SQLite ↔ Chroma drift report, manual *Rebuild* and *Clear*.
- **Global search** at `/search`: searches the signed-in user's notebooks, source filenames/summaries, conversation titles, and notes.
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

By default the web process also drains the ingest queue inline, so the single `uvicorn` command above ingests uploads as before. For a production-style split that keeps PDF extraction/embedding off the web process, set `NOTEBOOKLM_INLINE_WORKER=0` on the web app and run a dedicated worker alongside it (Docker Compose does this automatically — see the `worker` service):

```bash
.venv/bin/python -m app.worker                         # dedicated ingest worker (shares data/app.sqlite3)
```

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

Compose runs **two services off one image**: `app` (web, `NOTEBOOKLM_INLINE_WORKER=0`) and `worker` (`python -m app.worker`), which drains the ingest queue off the web process. They share the same `./data` bind-mount (one SQLite DB + Chroma index). The worker waits for the app to be healthy before starting, so schema migrations run once.

**Deploying to a remote / customer host:**

- **Build on the target host** (`docker compose up --build` there) so the image matches the host architecture. If you build on Apple Silicon (arm64) and ship the image to a linux/amd64 server, build with `docker buildx build --platform linux/amd64`.
- **Outbound network:** the container must reach the customer's chat + embedding endpoints (set as the Base URL in `/settings`). Ensure the Docker network / firewall allows egress to them.
- **Bind-mount ownership:** the container runs as UID 1000. `./data` and `./logs` on the host must be writable by it — `chown -R 1000:1000 data logs`, or set `user:` in `docker-compose.yml` to your host UID.
- **Tuning in Docker:** override retrieval/runtime defaults either with env vars (`NOTEBOOKLM_<GROUP>_<FIELD>`, highest precedence) or by mounting a tuned config — `cp config.example.toml config.toml`, edit it, and uncomment the `./config.toml:/app/config.toml:ro` line in `docker-compose.yml`. See [Tuning / configuration](#tuning--configuration).

### Python Runtime

Local development and Docker both use Python 3.12. Keeping them aligned avoids platform-specific native-wheel gaps in dependencies such as `onnxruntime`, which ChromaDB declares as a required dependency. The repo includes `.python-version` as a hint for version managers, but `setup.sh` only requires a working `python3.12` on `PATH`.

## LLM settings

Set the LLM connection at `/settings` while signed in as admin. Both chat and embeddings require a configured OpenAI-compatible (or Azure OpenAI) endpoint — there is no longer an offline-hash fallback for embeddings. The upload form is disabled until the embedding model is configured. The save handler probes the embedding endpoint once to validate connectivity and detect dimension mismatches against the existing Chroma index.

Optional **Embedding query/passage prefix** fields support models that need them: the e5 family (e.g. `multilingual-e5-large`) expects `query: ` on search queries and `passage: ` on indexed text. Leave both blank for OpenAI and other models that don't use prefixes — the prefix only changes what is sent to the embedding endpoint, never the stored chunk, so the app stays model-agnostic.

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

## Tuning / configuration

Retrieval and operational tunables — hybrid weights, the abstain threshold, candidate pool / final-chunk sizes, chunking targets, embedding batching, retry policy, ingest-queue timeouts, TTLs — live in [`app/config.py`](app/config.py). Values resolve in three layers, each overriding the one before:

1. **dataclass defaults** (version-controlled, identical to prior hardcoded behavior),
2. a **TOML file** — copy [`config.example.toml`](config.example.toml) to `config.toml` (gitignored), or point `NOTEBOOKLM_CONFIG_FILE` at any path,
3. **environment variables** `NOTEBOOKLM_<GROUP>_<FIELD>` (these win — handy for eval sweeps and per-deployment overrides).

```bash
# Sweep a retrieval weight without touching code:
NOTEBOOKLM_RETRIEVAL_VECTOR_WEIGHT=0.6 .venv/bin/python -m tests.eval_retrieval
```

Keep a tuned file per corpus/language as a deliverable (e.g. `config.zh.toml`). Changing `[chunking]` requires re-indexing existing sources (it changes how chunks are stored).

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
GET  /search                                              search notebooks, sources, conversations, and notes
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
POST /notebooks/{id}/chat/ask                             ask a question (HTMX: returns messages partial; no-JS: 303)
POST /notebooks/{id}/chat/ask-stream                      ask a question with streamed answer events
POST /notebooks/{id}/chat/{cid}/rename                    rename a conversation
POST /notebooks/{id}/chat/{cid}/delete                    delete a conversation
GET  /notebooks/{id}/chat/{cid}/_followups?message_id=N   lazy-load follow-up question chips (cached in metadata)
GET  /notebooks/{id}/chat/{cid}/export                    download conversation as Markdown

POST /notebooks/{id}/suggestions                          generate 4 starter questions (chat empty-state)
GET  /notebooks/{id}/_briefing                            HTMX swap: briefing strip (dedupes concurrent generation)
POST /notebooks/{id}/briefing[?force=1]                   generate / regenerate notebook briefing
GET  /notebooks/{id}/_tools                               HTMX swap: Studio tools launcher (tile grid)
GET  /notebooks/{id}/tools/{kind}                         tool config panel for the preview-modal (compare|minutes|study_guide|faq|timeline|translate)
POST /notebooks/{id}/compare                              compare 2+ sources (returns result fragment + save button)
POST /notebooks/{id}/minutes                              structured meeting minutes from one source (result + save button)
POST /notebooks/{id}/artifacts/{kind}                     A4 artifact: study_guide | faq | timeline (result + save button)
POST /notebooks/{id}/translate                            A5 translate one source's summary into a target language (result + save button)

POST /notebooks/{id}/notes/pin                            pin assistant message into notes
POST /notebooks/{id}/notes/add                            save a raw note (title + content)
POST /notebooks/{id}/notes/{note_id}/edit                 edit a note's title/content in place (U8)
POST /notebooks/{id}/notes/{note_id}/delete               remove pinned note (also broadcasts pin-cleared)
GET  /notebooks/{id}/_notes                               HTMX swap: notes section (notes-changed event)
GET  /notebooks/{id}/notes/export                         download all notes as Markdown

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
app/config.py          Centralized tunables (defaults <- config.toml <- env vars).
app/db.py              SQLite schema, default-notebook migration, load_llm_settings (decrypts API key).
app/ingest.py          Text extraction, chunking, vector upsert.
app/jobs.py            DB-backed ingest queue (ingest_jobs): enqueue + atomic claim + retry.
app/worker.py          Ingest worker loop (standalone `python -m app.worker` or inline).
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
  _suggestions.html    Starter-questions section (rendered in the chat empty-state).
  _briefing.html       Studio briefing slim strip (HTMX swap target; auto-fires POST on first view).
  _studio_tools.html   Studio tools launcher — tile grid that opens each tool in the preview-modal (HTMX swap target).
  _tool_panel.html     One tool's config panel loaded into the preview-modal (compare/minutes/study_guide/faq/timeline).
  _compare_result.html Comparison result fragment (markdown body + shared save button).
  _minutes_result.html Meeting-minutes result fragment (markdown body + save button; non-meeting sources offer no save).
  _artifact_result.html A4 artifact result fragment (markdown body + shared save button).
  _translate_result.html A5 translate-summary result fragment (markdown body + shared save button).
  _save_note_button.html Shared "save to notes" control used by all tool results (one-shot saved state).
  _notes_section.html  Studio outputs & notes shelf (HTMX swap target; inline note edit = U8).
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
  test_briefing_lock.py SQLite briefing lock: acquire / release / stale timeout.
  test_jobs.py         Ingest queue: enqueue idempotency, atomic claim, stale reclaim, retry/fail.
  test_config.py       Config layering (defaults <- TOML <- env) + example-file sync.
  eval_questions.json  Ground-truth retrieval Qs for the demo notebook.
  eval_retrieval.py    Recall@k + MRR harness (see RETRIEVAL.md).
config.example.toml    Tunable-config template (copy to config.toml to override).

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

Performance/scalability and retrieval-quality work are tracked as prioritised, tick-off backlogs in [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md) and [`docs/QUALITY.md`](docs/QUALITY.md) (issue → impact → fix → priority); UX improvements and new AI features live in [`docs/ROADMAP.md`](docs/ROADMAP.md). Engineering deep-dives live in [`docs/`](docs/). Headline items still outstanding:

- No CSRF protection on POST routes.
- No offline embedding fallback — embedding model must be configured before uploads are accepted.
- Keyword search uses `LIKE '%token%'` over SQLite; FTS5 + BM25 is on deck (see [`RETRIEVAL.md`](docs/RETRIEVAL.md)).
- Hybrid merge uses a fixed `0.7·vector + 0.3·keyword` blend; Reciprocal Rank Fusion is on deck.
- Qdrant is a future vector-store evaluation candidate; do a bounded spike before replacing Chroma.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
