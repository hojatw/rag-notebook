# Development and operations reference

Detailed setup, verification, tuning, logging, deployment, and repository-layout
notes. Start with [`README.md`](../README.md) for the short onboarding path.

## Runtime

Local development and Docker both use Python 3.12. Keeping them aligned avoids
platform-specific native-wheel gaps in dependencies such as `onnxruntime`, which
ChromaDB declares as a required dependency. The repo includes `.python-version`
as a hint for version managers, but `setup.sh` only requires a working
`python3.12` on `PATH`.

`setup.sh --force` wipes any existing `.venv` before rebuilding it. Use
`PYTHON_BIN=/path/to/python3.12 ./setup.sh` if your Python 3.12 binary has a
different name.

## Worker Mode

By default, the web process drains the ingest queue inline, so a single uvicorn
command is enough for local development:

```bash
NOTEBOOKLM_ALLOW_INSECURE_DEV_SECRET=1 .venv/bin/uvicorn app.main:app --reload --port 8000
```

For a production-style split that keeps extraction and embedding work off the
web process, set `NOTEBOOKLM_INLINE_WORKER=0` on the web app and run a dedicated
worker alongside it:

```bash
.venv/bin/python -m app.worker
```

Docker Compose does this automatically with separate `app` and `worker`
services. They share the same `./data` bind mount.

## Deployment Notes

- Build on the target host when possible so the image matches the host
  architecture. If you build on Apple Silicon and ship to linux/amd64, use
  `docker buildx build --platform linux/amd64`.
- The container must reach the configured chat and embedding endpoints. Confirm
  Docker network and firewall egress.
- The container runs as UID 1000. Ensure `./data` and `./logs` are writable by
  that UID, or set `user:` in `docker-compose.yml`.
- Use a stable, strong `NOTEBOOKLM_SECRET`. Changing it invalidates encrypted
  API keys and requires re-entering them at `/settings`.
- For customer SSO deployments, keep the app container on an internal network
  behind a reverse proxy or SSO gateway. The fronting service should own public
  ingress, TLS keys/certificates, and any enterprise auth integration; the app
  should receive only verified trusted-header or OIDC traffic. See
  [`AUTHENTICATION.md`](AUTHENTICATION.md) for Linux/container reverse-proxy
  topology notes.
- **Stamp the build so bug reports map to a commit.** The semantic version
  lives in the repo-root `VERSION` file; the commit is read at runtime. For
  Docker, pass the commit at build time so it survives into the image (the
  `.git` dir is not copied in):

  ```bash
  docker build --build-arg NOTEBOOKLM_GIT_SHA=$(git rev-parse --short HEAD) -t notebooklm .
  ```

  The build identifier (`vX.Y.Z (sha)`) shows in the page footer, the
  `app_started` log line, and `GET /healthz` (`{status, version, commit}`,
  unauthenticated). `NOTEBOOKLM_VERSION` / `NOTEBOOKLM_GIT_SHA` env vars
  override the file/git lookup when a release pipeline sets them.

Backup is one archive of `data/`:

```bash
tar czf data-$(date +%F).tar.gz data/
```

## Tuning

Retrieval and operational tunables live in [`app/config.py`](../app/config.py).
Values resolve in three layers:

1. dataclass defaults,
2. a TOML file such as `config.toml`,
3. environment variables `NOTEBOOKLM_<GROUP>_<FIELD>`.

Example:

```bash
NOTEBOOKLM_RETRIEVAL_VECTOR_WEIGHT=0.6 .venv/bin/python -m tests.eval_retrieval
```

Copy [`config.example.toml`](../config.example.toml) to `config.toml` for local
or deployment-specific overrides. Changing `[chunking]` — or the chunk-shaping
values in `[spreadsheet]` (`rows_per_chunk_max`, `embed_token_budget`) — requires
re-indexing existing sources. `[diagnostics]` thresholds only affect what the
source preview *displays*, so they never require a re-index. `[runtime].max_source_bytes`
is the hard size cap for eagerly-parsed sources (`.xlsx` / `.pptx` / `.csv`).

## Logging

```bash
tail -f logs/app.log
NOTEBOOKLM_LOG_LEVEL=DEBUG .venv/bin/uvicorn app.main:app --reload --port 8000
```

Common environment variables:

```text
NOTEBOOKLM_LOG_LEVEL=INFO
NOTEBOOKLM_LOG_FILE=logs/app.log
NOTEBOOKLM_LOG_MAX_BYTES=5242880
NOTEBOOKLM_LOG_BACKUP_COUNT=5
NOTEBOOKLM_DATA_DIR=data
NOTEBOOKLM_SECRET=replace-me-with-a-long-random-string
NOTEBOOKLM_ALLOW_INSECURE_DEV_SECRET=1  # local-only opt-in when NOTEBOOKLM_SECRET is unset
```

The app records startup/shutdown, HTTP requests, login attempts, source
upload/index/reindex/delete, embedding API calls, Chroma upsert/query, query
rewriting, retrieval/rerank, chat success/failure, notebook/note CRUD, and
exceptions with stack traces.

## Verification

```bash
.venv/bin/pytest
.venv/bin/python -m py_compile app/*.py tests/*.py
git diff --check
```

Current expected test-tooling warning: `fastapi.testclient` may emit
`StarletteDeprecationWarning` about its underlying `httpx` integration. This is
not an application runtime warning; revisit it when upgrading FastAPI,
Starlette, or httpx.

For retrieval changes, run the eval harness when an LLM configuration is
available:

```bash
.venv/bin/python -m tests.eval_retrieval
.venv/bin/python -m tests.eval_retrieval --no-rerank
.venv/bin/python -m tests.eval_retrieval --top-k 10
```

The harness reports per-question hit rank, Recall@k, and MRR. It skips when no
LLM key is configured.

## Layout

```text
app/main.py            Core routes (notebooks/sources/chat/notes/tools/account), auth + shared
                       web helpers, lifespan, logging; mounts the route modules below.
app/retrieval.py       Retrieval engine: hybrid search, scoring, ACTIVE_RETRIEVAL_PARAMS state.
app/evals.py           Admin Eval Workbench router (/admin/evals/*).
app/admin.py           Admin console router (/admin/index*, /admin/audit, /admin/users*).
app/settings.py        Admin LLM settings router (/settings, connection diagnostics).
app/config.py          Centralized tunables (defaults <- config.toml <- env vars).
app/db.py              SQLite schema, default-notebook migration, load_llm_settings.
app/ingest.py          Text extraction (PDF/DOCX/HTML/subtitles/PPTX/XLSX/CSV), chunking,
                       vector upsert, per-source summary, A6a ingestion diagnostics.
app/jobs.py            DB-backed ingest queue (ingest_jobs): enqueue + atomic claim + retry.
app/worker.py          Ingest worker loop (standalone or inline).
app/llm.py             LLM/embedding HTTP, query rewrite, rerank, starter questions.
app/governance.py      AI usage/safety telemetry normalization + sanitized recorders.
app/vector_store.py    Chroma persistent client + diff sync + index_status + clear_all_vectors.
app/security.py        Password hashing, signed session cookies, Fernet API-key encryption.
app/templates/         Jinja pages and HTMX partials.
app/static/            CSS, app JS, and self-hosted vendor assets.
tests/                 Pytest suites and retrieval eval harness.
config.example.toml    Tunable-config template.

Runtime-generated, gitignored:
data/                  SQLite metadata, uploads, and Chroma index.
logs/app.log           Rotating app log.
```

## Temporary workaround: embedding dimension migration (P0)

The current `/admin/index` Clear action removes vector records but does **not**
replace the Chroma collection schema. An empty collection may therefore remain
locked to the previous embedding dimension. This is tracked as critical-priority
`ROADMAP.md` O0; Clear/Rebuild is not a safe dimension-migration procedure until
the permanent fix lands.

Use `scripts/reset_chroma_dimension.py` only when `/settings` diagnostics or the
provider response has confirmed the new target dimension. Dry-run is the
default. Apply mode requires every web app and ingest worker using the same
`data/` directory to be stopped. This tool does not migrate between two models
or prefixes that happen to return the same dimension; those changes require a
normal source Reindex so the stored embeddings are regenerated.

Local/venv dry-run and apply (replace `1536` with the confirmed target):

```bash
.venv/bin/python scripts/reset_chroma_dimension.py --target-dimension 1536
.venv/bin/python scripts/reset_chroma_dimension.py \
  --target-dimension 1536 --apply --services-stopped
```

Docker Compose:

```bash
docker compose stop worker app
docker compose run --rm --no-deps \
  -v ./scripts:/app/scripts:ro \
  app python scripts/reset_chroma_dimension.py \
  --data-dir /app/data --target-dimension 1536
docker compose run --rm --no-deps \
  -v ./scripts:/app/scripts:ro \
  app python scripts/reset_chroma_dimension.py \
  --data-dir /app/data --target-dimension 1536 \
  --apply --services-stopped
docker compose up -d
```

The apply run:

- writes `data/backups/chroma-dimension-reset-*.tar.gz` before mutation;
- deletes/recreates only the `rag_chunks` Chroma collection;
- preserves SQLite, uploads, notebooks, messages, notes, and source files;
- restores indexed target-dimension chunks and target-dimension chunks left by
  this exact Chroma mismatch failure;
- changes old/mixed/unknown-dimension `indexed` sources to `failed` so startup
  sync cannot silently re-lock the collection, and lists them for Reindex;
- leaves unrelated failed/uploaded/processing sources unchanged.

After restart, verify `/admin/index`, then Reindex every source listed by the
tool. Keep the backup until retrieval and at least one fresh Reindex succeed.
The script does not call the embedding endpoint itself, so dry-run and recovery
of reusable vectors have no model cost; subsequent Reindex calls do.

## Persistence Safety

Do not commit runtime state under `data/` or `logs/`, and do not commit `.env`
or real secrets. For same-dimension vector drift, prefer the `/admin/index`
Clear/Rebuild flows over manual filesystem edits. For an embedding-dimension
change, the current Clear action is unsafe; use the stopped-service O0
workaround above until the permanent fix lands.
