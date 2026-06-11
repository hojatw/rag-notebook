# AGENTS.md

## Project Summary

This repository is a single-machine NotebookLM-style RAG proof of concept built with FastAPI, Jinja2 templates, HTMX, Alpine.js, SQLite, local uploads, and Chroma.

Treat it as a POC, not a production service. Keep changes scoped and behavior-preserving unless the user explicitly asks for a redesign.

## Context To Read First

- Read `README.md` for the current user-facing feature set, routes, setup, LLM settings, and known follow-ups.
- Read `docs/RETRIEVAL.md` before changing retrieval, chunking, ranking, reranking, evals, or vector-store behavior.
- Engineering deep-dives and the prioritised backlogs live in `docs/`: `docs/PERFORMANCE.md` (performance/scalability), `docs/QUALITY.md` (retrieval/answer quality), `docs/SECURITY.md` (security policy + triaged audit findings).
- Read `handover.md` when present for local cross-session work state. It is gitignored and may contain current priorities, but it is not a durable project rule source.

## Runtime And Dependencies

- Local development and Docker both use Python 3.12.
- Use `./setup.sh` to create or refresh `.venv`. Use `setup.sh --force` only when intentionally replacing the virtualenv.
- Runtime dependencies belong in `requirements.txt`.
- Development and test-only dependencies belong in `requirements-dev.txt`.
- Do not introduce npm, frontend build tooling, or CDN dependencies. Frontend vendor assets are self-hosted in `app/static/vendor/`.

## Run Commands

```bash
./setup.sh
NOTEBOOKLM_ALLOW_INSECURE_DEV_SECRET=1 .venv/bin/uvicorn app.main:app --reload --port 8000
```

For Docker:

```bash
docker compose up --build -d
docker compose logs -f
```

Docker requires `NOTEBOOKLM_SECRET` through `.env`. Do not use the insecure dev secret for network-exposed or production-like runs.

## Verification

Run the smallest checks that match the change. For general Python changes, prefer:

```bash
.venv/bin/pytest
.venv/bin/python -m py_compile app/*.py tests/*.py
```

For retrieval changes, also run the eval harness when an LLM configuration is available:

```bash
.venv/bin/python -m tests.eval_retrieval
.venv/bin/python -m tests.eval_retrieval --no-rerank
```

For Docker/runtime changes, build the image and smoke-test at least `/` and `/login`.

## Persistence And Safety

- Do not delete, rewrite, or commit runtime state from `data/` or `logs/`.
- `data/app.sqlite3`, `data/uploads/`, and `data/chroma/` are user state.
- `.env` and real secrets must stay uncommitted.
- Changing `NOTEBOOKLM_SECRET` invalidates encrypted API keys.
- If vector state may be inconsistent, prefer `/admin/index` Rebuild or Clear flows over manual filesystem edits.

## Implementation Conventions

- Keep the app server-rendered with Jinja templates and progressive enhancement through HTMX and Alpine.
- Prefer existing helper functions and route patterns in `app/main.py`, `app/db.py`, `app/llm.py`, `app/ingest.py`, and `app/vector_store.py`.
- Schema changes are currently handled through idempotent SQLite setup/migration helpers in `app/db.py`; add tests for new persistence behavior. **When you change the schema (new table, column, index, or constraint), update [`docs/SCHEMA.md`](docs/SCHEMA.md) in the same change** — it is the human-readable reference and must not drift from `app/db.py`.
- Keep generated UI fragments in `app/templates/_*.html` when they are HTMX partials.
- Keep Markdown rendering sanitized through the existing marked + DOMPurify path.
- Avoid broad refactors unless they directly reduce risk for the requested change.
- Tunable parameters (retrieval weights, top-k, chunking sizes, retry, queue timeouts, TTLs) live in `app/config.py`, resolved as defaults ← `config.toml` ← `NOTEBOOKLM_<GROUP>_<FIELD>` env. Add new tunables there rather than hardcoding; keep the dataclass defaults equal to current behavior and update `config.example.toml` (kept in sync by `tests/test_config.py`).

## LLM And Retrieval Notes

- Supported providers are `openai_compatible` and `azure_openai`.
- Ollama, vLLM, and TEI are supported only through OpenAI-compatible `/v1` endpoints, not native provider adapters.
- Local OpenAI-compatible services may still need a non-empty dummy API key in settings.
- Embedding responses must provide OpenAI-compatible `data[].embedding`; chat responses must provide `choices[0].message.content`.
- Changing embedding models can change vector dimensions. Preserve the existing dimension check and require clearing/rebuilding the Chroma index when needed.
- Before changing query rewrite, hybrid retrieval, reranking, chunking, or scoring, read `docs/RETRIEVAL.md` and update eval expectations where appropriate.

## Security Expectations

- Preserve per-user and per-notebook authorization checks on every route that reads or mutates notebook data.
- Use `load_llm_settings()` when plaintext API keys are needed; do not bypass decryption by reading `llm_settings` directly.
- Keep password hashing and API-key encryption centralized in `app/security.py`.
- Treat CSRF protection and streaming responses as known hardening follow-ups unless the user asks to implement them. (LLM retry/backoff and worker-backed ingest are implemented — see `docs/PERFORMANCE.md`.)

## Git Hygiene

- Do not revert user changes unless explicitly asked.
- Keep commits focused and include tests or verification notes when making code changes.
- `handover.md` is a local handoff document and should normally remain untracked.
