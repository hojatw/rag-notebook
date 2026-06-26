# CLAUDE.md

NotebookLM-style RAG proof of concept: FastAPI + Jinja2 + HTMX + Alpine.js, SQLite for metadata, Chroma for vectors, local filesystem for uploads. Single machine. Treat it as a **POC, not production** — keep changes scoped and behavior-preserving unless explicitly asked for a redesign.

## Docs

**@AGENTS.md** is the engineering rulebook (runtime, verification, persistence safety, conventions, security, git hygiene) — follow it; this file only adds Claude-specific orientation on top. AGENTS.md's "Context To Read First" holds the full **task-gated** doc map: load a `docs/*.md` only when your change touches its area, don't read the whole tree up front. Quick index of the gates: retrieval/chunking/ranking → `docs/RETRIEVAL.md`; schema → `docs/SCHEMA.md`; routes → `docs/ROUTES.md`; setup/deploy/tuning → `docs/DEVELOPMENT.md`; product/admin surfaces → `docs/ROADMAP.md`; XLSX/CSV ingestion → `docs/SPREADSHEET_INGESTION.md`; page/component UI → `docs/UI.md`; user-facing copy → `docs/I18N.md` (never hardcode strings); perf/quality backlogs → `docs/PERFORMANCE.md` / `docs/QUALITY.md`; onboarding → `README.md`. `handover.md` (gitignored, when present) is cross-session work state — useful context, not a durable rule source.

## Commands

```bash
./setup.sh                                                        # build/refresh .venv (Python 3.12, matches Docker)
NOTEBOOKLM_ALLOW_INSECURE_DEV_SECRET=1 .venv/bin/uvicorn app.main:app --reload --port 8000
.venv/bin/pytest                                                  # full test suite
.venv/bin/python -m py_compile app/*.py tests/*.py
.venv/bin/python -m tests.eval_retrieval                          # retrieval eval (needs an LLM configured)
```

## Architecture map

- `app/main.py` — core routes (notebooks, sources, chat, notes, tools, account), auth + shared web helpers (`render`, `require_login`/`require_admin`, `record_audit_event`, CSRF middleware, `templates`), lifespan, logging; SQLite-backed briefing concurrency lock; optional inline ingest worker (`NOTEBOOKLM_INLINE_WORKER`). Mounts the route modules below via `app.include_router(...)` at the bottom of the file (after the shared helpers are defined, since those modules import them back from `main` — `app.main` is the package's import root and the only safe entry point).
- `app/retrieval.py` — the retrieval engine extracted from `main`: query rewrite → hybrid (vector + keyword) search → diversify → rerank (`retrieve`), candidate fetch/merge, keyword scoring/tokenization, `citation_payload`, plus the runtime-safe retrieval-parameter machinery (`ACTIVE_RETRIEVAL_PARAMS` state, `resolve/active/set_active_retrieval_params`, profile param coerce/display helpers). `main` re-exports several of these for back-compat. Read `docs/RETRIEVAL.md` first.
- `app/evals.py` — Admin Eval Workbench (E1) `APIRouter`: retrieval-profile management and eval set/item/run/compare endpoints (`/admin/evals/*`) plus their helpers (`run_eval_job`, metrics, export payloads).
- `app/admin.py` — Admin console `APIRouter`: vector-index management (`/admin/index*`), audit log (`/admin/audit`), user administration (`/admin/users*`).
- `app/settings.py` — Admin LLM settings `APIRouter`: the `/settings` page, connection diagnostics (`/settings/test-chat`, `/settings/test-embedding`), and saving the chat + embedding connections.
- `app/config.py` — centralized tunables (retrieval weights, top-k, chunking, retry, queue, TTLs). Defaults ← `config.toml` ← `NOTEBOOKLM_<GROUP>_<FIELD>` env. Constants in other modules now read from `config`; **keep dataclass defaults equal to current behavior** (guarded by `tests/test_config.py`).
- `app/db.py` — SQLite schema + idempotent migrations (`_ensure_column`); `load_llm_settings()` decrypts the API key — **always go through it**, never `SELECT` the key directly. Schema reference: `docs/SCHEMA.md` (**keep it in sync on any schema change**).
- `app/ingest.py` — text extraction, chunking, vector upsert, per-source summary (best-effort after indexing).
- `app/jobs.py` — DB-backed ingest queue (`ingest_jobs` table): `enqueue_source`, atomic `claim_next_job`, retry/visibility-timeout. The single swap-point if ingest ever moves to Redis/RQ.
- `app/worker.py` — ingest worker loop; runs standalone (`python -m app.worker`) or inline in the web lifespan.
- `app/llm.py` — LLM/embedding HTTP, query rewrite, rerank, starter questions, briefing, compare. Providers: `openai_compatible` and `azure_openai` only (Ollama/vLLM/TEI go through the OpenAI-compatible `/v1` path). Chat and embedding are **independent connections** (own provider/base-url/key/api-version) resolved via `chat_settings()` / `embedding_settings()`; API key is optional (blank → no auth header).
- `app/vector_store.py` — Chroma persistent client, diff/full sync, `index_status`, `clear_all_vectors`.
- `app/security.py` — password hashing, signed session cookies, Fernet API-key encryption (KDF over `NOTEBOOKLM_SECRET`).
- `app/templates/` — Jinja; HTMX partials are `_*.html`. `app/static/` — `style.css` + `app.js`; self-hosted vendor JS in `app/static/vendor/`.
- `tests/` — pytest suites + retrieval eval harness (`eval_retrieval.py`, `eval_questions.json`).

## Frontend model

Server-rendered Jinja with progressive enhancement via HTMX + Alpine — **no build step, no npm, no CDN** (vendor JS is self-hosted). HTMX partials live in `app/templates/_*.html`. Cross-fragment live updates are driven by custom `HX-Trigger` events broadcast from source-row polling:

- `source-status-changed` — every status change; the left source rows listen (fast row sync).
- `indexed-sources-changed` — only on `indexed`/`failed`; the Studio briefing strip + tools launcher (`_studio_tools.html`) and the center chat empty-state (which now hosts the relocated starter questions) listen, so they don't re-render on every 2s processing tick.

When adding a fragment that depends on indexed-source availability, listen for `indexed-sources-changed`, not the per-tick event.

## Guardrails (full list in AGENTS.md)

- Never modify or commit `data/` or `logs/` (user state); keep `.env` and real secrets uncommitted. Changing `NOTEBOOKLM_SECRET` invalidates encrypted API keys.
- Preserve per-user / per-notebook authorization checks on every route that reads or mutates notebook data.
- Keep password hashing and API-key encryption centralized in `app/security.py`.
- CSRF protection, streaming responses, LLM retry/backoff, and worker-backed ingest are implemented; keep them working when touching forms, HTMX requests, chat streaming, provider HTTP, or ingest flow. See `docs/SECURITY.md` and `docs/PERFORMANCE.md`.

## Verifying in the browser

The Claude Code preview sandbox has **no outbound network**, so embedding/upload/indexing fail there with `[Errno 8] nodename nor servname provided` (DNS). Pure client-side checks (emoji picker, HTMX swaps) work fine in preview, but to exercise anything that calls the LLM/embedding endpoint, run a real uvicorn server with network egress instead of the sandboxed preview.
