# CLAUDE.md

NotebookLM-style RAG proof of concept: FastAPI + Jinja2 + HTMX + Alpine.js, SQLite for metadata, Chroma for vectors, local filesystem for uploads. Single machine. Treat it as a **POC, not production** — keep changes scoped and behavior-preserving unless explicitly asked for a redesign.

## Authoritative docs — read before working

- **@AGENTS.md** — the engineering rulebook (runtime, verification, persistence safety, conventions, security, git hygiene). Follow it; this file only adds Claude-specific orientation on top.
- `README.md` — user-facing feature set, the full route list, LLM settings, run/test.
- `docs/RETRIEVAL.md` — read before changing retrieval, chunking, ranking, reranking, evals, or vector-store behavior.
- `docs/PERFORMANCE.md` / `docs/QUALITY.md` — prioritised, tick-off backlogs for performance and retrieval-quality work.
- `handover.md` (gitignored, when present) — cross-session work state; useful context, not a durable rule source.

## Commands

```bash
./setup.sh                                                        # build/refresh .venv (Python 3.12, matches Docker)
NOTEBOOKLM_ALLOW_INSECURE_DEV_SECRET=1 .venv/bin/uvicorn app.main:app --reload --port 8000
.venv/bin/pytest                                                  # full test suite
.venv/bin/python -m py_compile app/*.py tests/*.py
.venv/bin/python -m tests.eval_retrieval                          # retrieval eval (needs an LLM configured)
```

## Architecture map

- `app/main.py` — routes, auth, retrieval orchestration, lifespan, logging; SQLite-backed briefing concurrency lock; optional inline ingest worker (`NOTEBOOKLM_INLINE_WORKER`).
- `app/config.py` — centralized tunables (retrieval weights, top-k, chunking, retry, queue, TTLs). Defaults ← `config.toml` ← `NOTEBOOKLM_<GROUP>_<FIELD>` env. Constants in other modules now read from `config`; **keep dataclass defaults equal to current behavior** (guarded by `tests/test_config.py`).
- `app/db.py` — SQLite schema + idempotent migrations (`_ensure_column`); `load_llm_settings()` decrypts the API key — **always go through it**, never `SELECT` the key directly.
- `app/ingest.py` — text extraction, chunking, vector upsert, per-source summary (best-effort after indexing).
- `app/jobs.py` — DB-backed ingest queue (`ingest_jobs` table): `enqueue_source`, atomic `claim_next_job`, retry/visibility-timeout. The single swap-point if ingest ever moves to Redis/RQ.
- `app/worker.py` — ingest worker loop; runs standalone (`python -m app.worker`) or inline in the web lifespan.
- `app/llm.py` — LLM/embedding HTTP, query rewrite, rerank, starter questions, briefing, compare. Providers: `openai_compatible` and `azure_openai` only (Ollama/vLLM/TEI go through the OpenAI-compatible `/v1` path).
- `app/vector_store.py` — Chroma persistent client, diff/full sync, `index_status`, `clear_all_vectors`.
- `app/security.py` — password hashing, signed session cookies, Fernet API-key encryption (KDF over `NOTEBOOKLM_SECRET`).
- `app/templates/` — Jinja; HTMX partials are `_*.html`. `app/static/` — `style.css` + `app.js`; self-hosted vendor JS in `app/static/vendor/`.
- `tests/` — pytest suites + retrieval eval harness (`eval_retrieval.py`, `eval_questions.json`).

## Frontend model

Server-rendered Jinja with progressive enhancement via HTMX + Alpine — **no build step, no npm, no CDN** (vendor JS is self-hosted). HTMX partials live in `app/templates/_*.html`. Cross-fragment live updates are driven by custom `HX-Trigger` events broadcast from source-row polling:

- `source-status-changed` — every status change; the left source rows listen (fast row sync).
- `indexed-sources-changed` — only on `indexed`/`failed`; the Studio sections (suggestions/briefing/compare) and the center chat empty-state listen, so they don't re-render on every 2s processing tick.

When adding a fragment that depends on indexed-source availability, listen for `indexed-sources-changed`, not the per-tick event.

## Guardrails (full list in AGENTS.md)

- Never modify or commit `data/` or `logs/` (user state); keep `.env` and real secrets uncommitted. Changing `NOTEBOOKLM_SECRET` invalidates encrypted API keys.
- Preserve per-user / per-notebook authorization checks on every route that reads or mutates notebook data.
- Keep password hashing and API-key encryption centralized in `app/security.py`.
- CSRF protection and streaming responses are known hardening follow-ups — don't add them unless asked. (LLM retry/backoff and worker-backed ingest are now implemented — see `docs/PERFORMANCE.md`.)

## Verifying in the browser

The Claude Code preview sandbox has **no outbound network**, so embedding/upload/indexing fail there with `[Errno 8] nodename nor servname provided` (DNS). Pure client-side checks (emoji picker, HTMX swaps) work fine in preview, but to exercise anything that calls the LLM/embedding endpoint, run a real uvicorn server with network egress instead of the sandboxed preview.
