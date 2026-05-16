# NotebookLM RAG POC Handover

Last updated: 2026-05-15, Asia/Taipei.

## Project Location

```bash
/Users/philip_1/Repos/My/side_projects/notebooklm-rag-poc
```

This folder is currently not a git repository. The user said they will handle git separately.

## Current State

This is a single-machine FastAPI proof of concept for a NotebookLM-like RAG workflow:

1. Login with built-in users.
2. Upload sources.
3. Extract text and chunk documents.
4. Create embeddings.
5. Persist vectors in Chroma.
6. Ask questions against selected sources.
7. Retrieve relevant chunks with citations.
8. Generate source-grounded answers through configured LLM settings.

The development server was last restarted successfully at:

```text
http://127.0.0.1:8000
```

Default accounts:

```text
admin / admin123
user  / user123
```

Admin LLM settings are configured through `/settings`.

## Implemented Features

- FastAPI backend with Jinja templates and static assets.
- Multi-user login with hashed passwords and per-user isolation.
- Source upload, list, delete, and reindex.
- Supported source formats:
  - PDF
  - TXT
  - Markdown
  - DOCX
  - HTML
- Source status tracking:
  - uploaded
  - processing
  - indexed
  - failed
- SQLite persistence for users, settings, sources, chunks, conversations, and messages.
- Local filesystem storage for uploaded original files.
- Chroma persistent vector store under `data/chroma`.
- OpenAI-compatible and Azure OpenAI LLM settings.
- Shared reusable `httpx.AsyncClient` for LLM and embedding calls.
- Batched embeddings through `embed_texts()`.
- Query rewriting before retrieval.
- Hybrid retrieval:
  - Chroma vector search.
  - SQLite keyword candidates.
  - Candidate merge/scoring.
  - LLM reranking when chat settings exist.
- RAG prompt constrains answers to retrieved sources.
- Citations include filename, page or section, and text snippets.
- Conversation history per user.
- Logging to both stdout and `logs/app.log` with rotation.
- Function docstrings were added across the implementation.

## Important Files

```text
app/main.py          FastAPI routes, auth flow, chat flow, retrieval orchestration, logging setup.
app/db.py            SQLite schema, seed users/settings, DB helpers.
app/ingest.py        Text extraction, chunking, source processing, vector upsert.
app/llm.py           LLM calls, Azure/OpenAI-compatible request shaping, embeddings, query rewrite, rerank.
app/vector_store.py  Chroma PersistentClient wrapper, upsert/delete/query/sync helpers.
app/security.py      Password hashing and signed session helpers.
app/templates/       Jinja UI templates.
app/static/          CSS/JS UI assets.
tests/test_core.py   Core app, ingestion, isolation, retrieval tests.
tests/test_llm.py    LLM provider, client reuse, parsing tests.
README.md            Run/test/settings/logging instructions.
```

## Persistence

Local persistent data:

```text
data/app.sqlite3       SQLite app database.
data/uploads/          Uploaded source files.
data/chroma/           Chroma vector index.
logs/app.log           Rotating app log file.
```

Current `.gitignore` only ignores:

```text
logs/
```

If the user does not want to commit uploaded documents, SQLite data, or Chroma indexes, add `data/` to `.gitignore`.

## Run

```bash
cd /Users/philip_1/Repos/My/side_projects/notebooklm-rag-poc
source .venv/bin/activate
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open:

```text
http://127.0.0.1:8000
```

Set verbose logging:

```bash
NOTEBOOKLM_LOG_LEVEL=DEBUG .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Watch logs:

```bash
tail -f logs/app.log
tail -n 200 logs/app.log
```

## Test

```bash
cd /Users/philip_1/Repos/My/side_projects/notebooklm-rag-poc
.venv/bin/pytest
.venv/bin/python -m py_compile app/*.py tests/*.py
```

Last known verification:

```text
pytest: 10 passed
py_compile: passed
/login: HTTP 200
logs/app.log: confirmed writing startup and request logs
```

## Chroma / Python 3.14 Caveat

The workspace uses Python 3.14.x. `chromadb==1.5.9` has a dependency on `onnxruntime`, but an `onnxruntime` wheel is not available for this Python version in the current environment.

The installed workaround was:

```bash
.venv/bin/python -m pip install chromadb==1.5.9 --no-deps
.venv/bin/python -m pip install numpy==2.4.4 pydantic-settings==2.14.1 pybase64==1.4.3
.venv/bin/python -m pip install overrides jsonschema mmh3 orjson pypika tenacity typer tqdm rich importlib-resources build bcrypt grpcio tokenizers opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc kubernetes
```

`requirements.txt` includes:

```text
chromadb==1.5.9; python_version < "3.14"
```

This avoids breaking installs on Python 3.14, but a fresh Python 3.14 setup still needs the manual Chroma install commands above.

## LLM Settings Examples

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

Azure requests use:

```text
POST {endpoint}/openai/deployments/{deployment}/chat/completions?api-version={api_version}
POST {endpoint}/openai/deployments/{deployment}/embeddings?api-version={api_version}
api-key: <key>
```

OpenAI-compatible requests use:

```text
POST {base_url}/chat/completions
POST {base_url}/embeddings
Authorization: Bearer <key>
```

## Retrieval / Embedding Strategy

Ingestion path:

1. Extract text by file type.
2. Split into chunks.
3. Batch embedding requests through `embed_texts()`.
4. Store chunk metadata and embedding JSON in SQLite.
5. Upsert vectors/documents/metadata into Chroma.

Query path:

1. Rewrite the user question into search queries when LLM settings are present.
2. Embed rewritten queries in batch.
3. Query Chroma with metadata filters:
   - `user_id`
   - selected `source_ids`
4. Fetch keyword candidates from SQLite.
5. Merge vector and keyword candidates.
6. Rerank candidates with the LLM if available.
7. Generate an answer from the final retrieved chunks.
8. Save assistant answer and citations.

If Chroma query fails, retrieval logs `retrieve_vector_failed` and falls back to SQLite brute-force vector search.

## Logging

Logging is configured in `app/main.py`.

Defaults:

```text
NOTEBOOKLM_LOG_LEVEL=INFO
NOTEBOOKLM_LOG_FILE=logs/app.log
NOTEBOOKLM_LOG_MAX_BYTES=5242880
NOTEBOOKLM_LOG_BACKUP_COUNT=5
```

The app logs:

- startup and shutdown
- request method/path/status/elapsed time
- login success/failure
- source upload/delete/reindex
- extraction and ingestion
- embedding batch activity
- Chroma vector upsert/delete/query/sync
- query rewrite
- retrieval and rerank
- chat success/failure
- exceptions with stack traces

## Known Technical Debt / Follow-ups

- FastAPI `@app.on_event` deprecation warnings remain. Consider migrating to lifespan handlers.
- Chroma telemetry warnings appear in tests because of package internals, despite disabled anonymized telemetry.
- Python 3.14 dependency reproducibility is imperfect because Chroma requires manual no-deps installation.
- `.gitignore` currently does not ignore `data/`. Decide whether app data should be versioned.
- The POC uses FastAPI background tasks rather than a robust worker queue.
- Current local deterministic embedding fallback is for offline demo only; real accuracy requires configured embedding API.
- No production-grade secret management yet. Admin API key is stored in SQLite.
- No CSRF protection yet.
- No pagination for large source/conversation/message sets yet.

## Good First Next Steps

1. Decide whether to ignore `data/` in git.
2. Migrate startup/shutdown to FastAPI lifespan.
3. Add admin UI indicator for Chroma sync/index status.
4. Add retry/backoff around LLM and embedding HTTP calls.
5. Add a small evaluation set for retrieval quality.
6. Improve dependency setup for Python 3.14 or pin project to Python 3.12/3.13.
