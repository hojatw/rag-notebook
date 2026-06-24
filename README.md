# NotebookLM-style RAG POC

A single-machine FastAPI proof of concept for a NotebookLM-style workspace:
organise sources into **notebooks**, ground chats against selected sources, and
pin useful answers or generated artifacts as notes.

The app is server-rendered with FastAPI, Jinja2, HTMX, Alpine.js, SQLite,
Chroma, and local file uploads. There is no frontend build step, npm, or CDN
dependency.

## Status

This is a proof of concept, not a production-ready service. It is suitable for
local experiments and small trusted single-machine deployments after you set a
real `NOTEBOOKLM_SECRET`, but it has not been hardened for direct public
internet exposure.

## Quick Start

```bash
cd notebooklm-rag-poc
./setup.sh
NOTEBOOKLM_ALLOW_INSECURE_DEV_SECRET=1 .venv/bin/uvicorn app.main:app --reload --port 8000
```

Open `http://127.0.0.1:8000` and sign in:

- Admin: `admin` / `admin123`
- User: `user` / `user123`

These demo accounts and the insecure development secret are for local
development only. Change or remove the demo accounts before any network-exposed
deployment.

## Docker

```bash
cp .env.example .env       # then fill NOTEBOOKLM_SECRET
docker compose up --build -d
docker compose logs -f
```

Docker Compose requires `NOTEBOOKLM_SECRET` in `.env`; the app fails closed when
it is missing. The compose file bind-mounts `./data` and `./logs` so user state
survives rebuilds.

Upgrade:

```bash
git pull
docker compose up --build -d
```

Reset, deleting users, notebooks, uploads, vectors, and logs:

```bash
docker compose down
rm -rf data/ logs/
```

For deployment details, worker mode, logging, tuning, and test commands, see
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md).

## Configure LLM

Sign in as admin and open `/settings`. Chat and embeddings both require a
configured OpenAI-compatible or Azure OpenAI endpoint. Uploads stay disabled
until the embedding model is configured.

On save, the app probes the embedding endpoint once and rejects settings that
would mismatch the existing Chroma index dimension. API keys are encrypted at
rest with Fernet using `NOTEBOOKLM_SECRET`.

The same page also provides admin-only diagnostics: test the chat model and
embedding model separately, inspect latency/status/model summaries and
embedding dimension, and probe optional capabilities such as streaming,
provider usage reporting, JSON-following, and opt-in image understanding.
Diagnostics store compact status metadata only, not raw prompts, model output,
API keys, or raw provider payloads.

The settings page has **two independent cards** — a **Chat model** card and an
**Embedding model** card — each with its own provider, base URL, API key, model,
and **Test** button. They can point at completely different services (e.g. a
hosted chat model plus a local e5 embedding server).

OpenAI-compatible example (chat card):

```text
Provider:           OpenAI-compatible
Base URL:           https://api.openai.com/v1
API key:            sk-...
Chat model:         gpt-4.1-mini
Temperature:        0.2
```

Embedding card (here a local e5 server, no key):

```text
Provider:           OpenAI-compatible
Base URL:           http://10.0.0.1:8001/v1
API key:            (blank — local services need no key)
Embedding model:    intfloat/multilingual-e5-large
Query prefix:       query:
Passage prefix:     passage:
```

Local OpenAI-compatible services such as Ollama, vLLM, and TEI are supported
through their `/v1` endpoints. **The API key is optional** — a blank key sends no
auth header, which is what local services expect.

Optional embedding query/passage prefixes support models such as
`multilingual-e5-large`, where search queries should use `query: ` and indexed
text should use `passage: `. Prefixes affect only the text sent to the embedding
endpoint, not stored chunks.

## What You Get

- **Notebook workspace:** each notebook owns sources, conversations, pinned
  notes, and generated artifacts.
- **Sources pane:** drag-and-drop upload, indexing-status polling, reindex and
  delete controls, source preview drawer, citation-to-chunk highlighting.
- **Grounded chat:** streaming answers, Markdown rendering, source citations,
  copy/export, follow-up chips, starter questions, IME-safe input, and
  Traditional Chinese UI strings.
- **Studio tools:** briefing strip, source comparison, meeting minutes, study
  guide, FAQ, timeline, translation, and manual save-to-notes flow.
- **Hybrid retrieval:** query rewrite, Chroma vector search, SQLite keyword
  search, LLM reranking, abstain threshold, and per-message retrieval debug
  details.
- **Admin surfaces:** user management, vector-index console, LLM settings,
  audit trail, and in-deployment Eval Workbench with retrieval profiles,
  comparisons, exports, and tuning guide.
- **Governance backend:** compact LLM usage and safety-event telemetry without
  copying prompts, source text, retrieved snippets, model output, or API keys
  into governance metadata.
- **Supported source formats:** PDF, TXT, Markdown, DOCX, HTML, and subtitles
  (`.srt` / `.vtt`).
- **Persistence:** SQLite metadata, local uploads, and Chroma vectors under
  `data/`; rotating logs under `logs/`.

## Documentation Map

- [`docs/ROADMAP.md`](docs/ROADMAP.md) - product/admin roadmap: UX, Eval
  Workbench, AI governance, LLM operations, source-format support, and new AI
  features.
- [`docs/PRODUCT_WHITEPAPER.zh-TW.md`](docs/PRODUCT_WHITEPAPER.zh-TW.md) -
  customer-facing product whitepaper in Traditional Chinese.
- [`docs/RETRIEVAL.md`](docs/RETRIEVAL.md) - retrieval pipeline, ranking,
  reranking, eval workflow, and tuning knobs.
- [`docs/QUALITY.md`](docs/QUALITY.md) - retrieval and answer-quality backlog.
- [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md) - performance and scalability
  backlog.
- [`docs/SECURITY.md`](docs/SECURITY.md) - security policy and dependency-audit
  triage.
- [`docs/SCHEMA.md`](docs/SCHEMA.md) - SQLite schema reference.
- [`docs/UI.md`](docs/UI.md) - frontend design contract and component
  conventions.
- [`docs/ROUTES.md`](docs/ROUTES.md) - full HTTP route reference.
- [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) - setup, testing, logging,
  tuning, deployment notes, and repository layout.
- [`docs/SPREADSHEET_INGESTION.md`](docs/SPREADSHEET_INGESTION.md) -
  spreadsheet ingestion design notes.

## Development Checks

```bash
.venv/bin/pytest
.venv/bin/python -m py_compile app/*.py tests/*.py
git diff --check
```

For retrieval changes, also run the eval harness when an LLM configuration is
available:

```bash
.venv/bin/python -m tests.eval_retrieval
.venv/bin/python -m tests.eval_retrieval --no-rerank
```

## Known Follow-Ups

- No offline embedding fallback: configure embeddings before uploads.
- UI copy is routed through a `zh-TW` message catalog (i18n foundation done,
  `ROADMAP.md` U15a); adding an `en` locale + admin/per-user language controls
  is U15b. See [`docs/I18N.md`](docs/I18N.md).
- Admin LLM settings still use one global configuration. O1 Phase 1 diagnostics
  are done; multi-profile management and safe activation remain O1 Phase 2.
- New source-format support should start with ingestion diagnostics, then
  Q&A-style spreadsheets, SSRF-safe Web URL ingestion, and PPTX text-first
  ingestion (`ROADMAP.md` A6a/A6c/A6/A6b).
- Keyword search still uses SQLite `LIKE`; FTS5 + BM25 is tracked in
  `docs/QUALITY.md` / `docs/PERFORMANCE.md`.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
