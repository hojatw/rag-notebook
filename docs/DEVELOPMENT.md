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

E2 notebook domain hints and answer policy limits are in the `[domain_policy]`
group in `config.example.toml` and resolve through the same layers. They bound
owner-authored input and query-time expansion; changing them never requires
re-indexing. Current defaults are: 50 hints; term 100 chars; 8 synonyms at 100
chars each; definition 300 chars; 4 query expansions at 120 chars each; answer
note 300 chars; policy 1,000 chars / 800 estimated tokens; at most 8 matched
hints / 600 estimated hint tokens; and at most 5 rewrite queries.

Example:

```bash
NOTEBOOKLM_RETRIEVAL_VECTOR_WEIGHT=0.6 .venv/bin/python -m tests.eval_retrieval
```

Copy [`config.example.toml`](../config.example.toml) to `config.toml` for local
or deployment-specific overrides. Changing `[chunking]` — or the chunk-shaping
values in `[spreadsheet]` (`rows_per_chunk_max`, `embed_token_budget`) — requires
re-indexing existing sources. `[diagnostics]` thresholds only affect what the
source preview *displays*, so they never require a re-index.

### LLM output caps

`[max_tokens]` sets a per-`call_type` cap on the response. These are **truncation
limits, not length targets** — too low does not make the model write less, it cuts
the response off. For the call types that return JSON that is worse than it
sounds: the caller parses with a `try/except` and degrades silently (rerank falls
back to hybrid order, query rewrite to the original question), so an over-tight
cap shows up as *"retrieval quietly got worse"*, not as an error.

Defaults are sized for **Traditional Chinese** at roughly 1 token per character;
the same content in English needs about a quarter. Replace them with measured
numbers once a deployment has traffic:

```bash
sqlite3 data/app.sqlite3 "WITH ranked AS (SELECT call_type, completion_tokens, NTILE(20) OVER (PARTITION BY call_type ORDER BY completion_tokens) AS b FROM llm_usage_events WHERE status='succeeded' AND completion_tokens IS NOT NULL AND is_estimated=0) SELECT call_type, COUNT(*) n, MAX(completion_tokens) p95 FROM ranked WHERE b<=19 GROUP BY call_type ORDER BY p95 DESC;"
```

Take p95 × 1.5. The `is_estimated = 0` filter still matters: fallback counts are
now CJK-aware (`[diagnostics].cjk_chars_per_token` and
`latin_chars_per_token`) but remain approximations rather than provider/tokenizer
measurements. A row is also marked estimated when a provider returns only part of
the usage shape and the missing prompt/completion component must be synthesized;
prompt + total or completion + total can be completed exactly by subtraction.

### Session lifetime

`[auth].session_max_age_hours` (default 12) is the **absolute** lifetime of a
signed-in session, counted from login and **not extended by activity**. Raising it
trades exposure for fewer re-logins; the effective value is printed in the
`app_started` log line so it is visible per deployment. Changing a password (or an
admin resetting one) revokes that account's other sessions immediately, regardless
of this setting — see [`SECURITY.md`](SECURITY.md) → *Sessions*.

### Local-login rate limiting

`[auth].login_*` controls the SEC-4 account failure bucket and password-check
leases. Defaults are 5 failures per username in 15 minutes, at most 4 concurrent
PBKDF2 checks deployment-wide, one active check per account, and a 30-second
crash-recovery lease. State lives in SQLite and is shared across web workers;
account identifiers are HMACs, not usernames. There is no deployment-wide
failure cooldown because arbitrary unknown usernames could turn it into a
persistent login denial of service. A network deployment must still enforce a
verified client-IP limit at the reverse proxy; do not derive an IP bucket from
untrusted forwarding headers in the app.

### File size caps

There are **two**, named for the pipeline stage that enforces them, because they
protect different things:

| Setting | Stage | Formats | Protects |
|---|---|---|---|
| `[runtime].upload_max_file_bytes` (50 MB) | web request, while streaming to disk | **all** | the host — disk fill, request memory |
| `[runtime].extract_max_file_bytes` (20 MB) | ingest worker, file already stored | `.xlsx` / `.pptx` / `.csv` only | the **parser** |

The second exists because those three formats' on-disk size says nothing about
their parse cost: `.xlsx` / `.pptx` are zip containers (a small archive can
decompress to gigabytes) and `.csv` is read whole into memory for encoding
detection. PDF and DOCX stream, so they are exempt.

**The upload path applies whichever is stricter for the file's format**
(`ingest.upload_limit_for`), so a 30 MB spreadsheet is refused at upload with an
explanation rather than uploading fine and failing in the worker minutes later.
`extract_max_file_bytes` remains enforced in the worker as the backstop for files
that did not arrive through an upload — a reindex of something stored before the
cap existed, or a file dropped into `data/uploads/` by hand.

Raising the upload cap does **not** raise the parse cap. A whole request is
additionally bounded at `upload_max_file_bytes * upload_batch_limit` (plus 1 MB of
framing), refused from `Content-Length` before any body is read.

`extract_max_file_bytes` was called `max_source_bytes` before this rename; the old
key is still read, with a deprecation warning at startup.

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
NOTEBOOKLM_SEED_DEMO_USERS=0            # see below; defaults to the dev-secret answer
```

**`NOTEBOOKLM_SEED_DEMO_USERS`** controls whether the demo pair
(`admin/admin123` + `user/user123`) is seeded with standing passwords. Unset, it
follows the same signal the login page uses to decide whether printing those
credentials is safe: **on** with the insecure dev secret, **off** whenever a real
`NOTEBOOKLM_SECRET` is set. With it off, only `admin` is seeded and that account
must change its password at first login before it can reach any other page; `user`
is not seeded, so deleting it survives a restart. Set it to `1` only for local
development or a throwaway demo. See [`SECURITY.md`](SECURITY.md) for the
upgrade behaviour on an existing deployment.

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

The harness reports per-question hit rank, Recall@k, and MRR. It requires an
embedding model, but not an API key: local OpenAI-compatible embedding services
may be configured with a blank key. A chat model is optional; without one the
harness measures the single-query / hybrid-order fallback path, and
`--no-rerank` forces that path even when chat is configured.

## Layout

```text
app/main.py            Core routes (notebooks/sources/chat/notes/tools/account), auth + shared
                       web helpers, lifespan, logging; mounts the route modules below.
app/retrieval.py       Retrieval engine: hybrid search, scoring, ACTIVE_RETRIEVAL_PARAMS state.
app/domain_policy.py   E2 domain-hint/policy validation, matching, snapshots, export summaries.
app/evals.py           Admin Eval Workbench router (/admin/evals/*).
app/admin.py           Admin console router (/admin/index*, /admin/audit, /admin/users*).
app/settings.py        Admin LLM settings router (/settings, connection diagnostics).
app/config.py          Centralized tunables (defaults <- config.toml <- env vars).
app/db.py              SQLite schema, default-notebook migration, load_llm_settings.
app/ingest.py          Text extraction (PDF/DOCX/HTML/subtitles/PPTX/XLSX/CSV), chunking,
                       vector upsert, per-source summary, A6a ingestion diagnostics.
app/jobs.py            DB-backed ingest queue (ingest_jobs): enqueue + atomic claim + retry.
app/worker.py          Ingest worker loop (standalone or inline).
app/llm.py             LLM/embedding HTTP, query rewrite, rerank, grounded answers and tools.
app/governance.py      AI usage/safety telemetry normalization + sanitized recorders.
app/index_migration.py O0 embedding-dimension migration: source classification by
                       vector dimension, the /admin/index flow, and the
                       vector_index_state generation + lock.
app/i18n.py            UI message catalog (t() / window.I18N); see docs/I18N.md.
app/version.py         Build identity (VERSION + git sha) for footer/logs/healthz.
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

## Changing the embedding dimension

Chroma locks a collection's vector width on the first upsert, and deleting every
record does **not** release it — only replacing the collection object does. So
moving to an embedding model of a different dimension is its own flow, never
Clear/Rebuild.

**In the app (the normal path):**

1. `/settings` → **Test embedding model** against the new model. The migration
   reads its target width from that stored result, so it is always a number the
   endpoint actually returned rather than one typed into a form.
2. `/admin/index` → **更換 embedding 維度**. The page previews what would happen:
   how many sources keep their vectors, how many need re-embedding, and which
   filenames are affected.
3. Type the target dimension to confirm, then run it.
4. Reindex the sources it listed. That is the only step that costs embedding
   calls; the migration itself makes none.

The migration replaces the `rag_chunks` collection, restores the vectors already
at the target width, moves the rest to the `stale_embedding` status (excluded
from search and from startup sync, recovered by Reindex), and holds a lock that
pauses the ingest queue until it finishes. SQLite, uploads, notebooks, messages,
and notes are untouched throughout.

It refuses to start while an ingest job is **running** — that job is mid-flight
with the old model. Queued jobs are fine; they run afterwards against the new
one. It also cannot re-embed for you: two models that both return, say, 1536
dimensions are indistinguishable to a width check, so switching between them (or
changing an embedding prefix) needs a plain source Reindex instead.

**Break-glass: `scripts/reset_chroma_dimension.py`**

Use this only when the app will not start and `/admin/index` is unreachable. It
does the same job from outside the app: dry-run by default, `--apply` requires
`--services-stopped`, and it backs up SQLite + Chroma to
`data/backups/chroma-dimension-reset-*.tar.gz` before touching anything. Every
web and worker process sharing the `data/` directory must be stopped first.

```bash
.venv/bin/python scripts/reset_chroma_dimension.py --target-dimension 1536
.venv/bin/python scripts/reset_chroma_dimension.py \
  --target-dimension 1536 --apply --services-stopped
```

Docker Compose:

```bash
docker compose stop worker app
docker compose run --rm --no-deps -v ./scripts:/app/scripts:ro \
  app python scripts/reset_chroma_dimension.py \
  --data-dir /app/data --target-dimension 1536
docker compose run --rm --no-deps -v ./scripts:/app/scripts:ro \
  app python scripts/reset_chroma_dimension.py \
  --data-dir /app/data --target-dimension 1536 --apply --services-stopped
docker compose up -d
```

Keep the backup until retrieval and at least one fresh Reindex succeed. The
script marks affected sources `failed` rather than `stale_embedding`, so after
recovering this way, Reindex the sources it names.

## Persistence Safety

Do not commit runtime state under `data/` or `logs/`, and do not commit `.env`
or real secrets. For same-dimension vector drift, prefer the `/admin/index`
Clear/Rebuild flows over manual filesystem edits. For an embedding-dimension
change use the migration flow above — Clear does not release the collection's
locked width.
