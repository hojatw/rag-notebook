# Performance backlog

Prioritised list of known performance / scalability issues, kept so they can be worked through one at a time. Each item lists **Issue → Impact → Fix**, a **priority**, and a **status** box to tick when done.

**Deployment context:** the target is ~200 users (not all concurrent) on the customer's **borrowed, shared** Gemma 4 31B (chat) + multilingual-e5-large (embedding) endpoint — **the serving side is fixed, so every adaptation must happen app-side** — with hundreds-of-page research-report PDFs. See `../handover.md` for the pre-launch checklist and `RETRIEVAL.md` for the retrieval pipeline.

Status legend: `[ ]` todo · `[~]` in progress · `[x]` done

---

## P0 — quick wins / critical for this deployment

### [x] P0-1 · Concurrent embedding batches
- **Issue:** `embed_texts` (`app/llm.py`) sends batches sequentially — `for start in range(...): embeddings.extend(await embed_text_batch(...))`.
- **Impact:** Indexing a ~900-chunk PDF is ~15 back-to-back round-trips to the shared e5 endpoint; index time is dominated by serial network wait.
- **Fix:** Fire batches with **bounded** concurrency (`asyncio.gather` with a 2–4 in-flight cap so the shared endpoint isn't overloaded).

### [x] P0-2 · SQLite write/read pragmas
- **Issue:** `connect()` (`app/db.py`) enables WAL but leaves `synchronous` at the default (FULL) and the default ~2 MB page cache; `journal_mode=WAL` is also re-set on every connect (it is persistent).
- **Impact:** Extra fsyncs slow ingest writes; small cache slows the `LIKE` keyword scans on large corpora.
- **Fix:** Add `PRAGMA synchronous = NORMAL` (safe under WAL) + a larger `PRAGMA cache_size` / `PRAGMA mmap_size`. Move the one-time `journal_mode` set out of the per-connection path if convenient.

### [x] P0-3 · LLM / embedding retry + backoff + generous timeout
- **Issue:** No HTTP retry/backoff (architectural follow-up #14); each question makes 3 chat calls (rewrite, rerank, answer) + embeddings.
- **Impact:** On the shared/throttled endpoint, a single slow or `429` call fails the whole question.
- **Fix:** Wrap chat + embedding HTTP in exponential backoff (e.g. `tenacity`, ~3 attempts); set a generous `Timeout seconds` in `/settings`.

---

## P1 — important at launch scale

### [ ] P1-1 · Move ingest off the web process
- **Issue:** Background ingest runs in-process (FastAPI `BackgroundTasks`); `pdfplumber` extraction of hundreds-of-page PDFs is slow and CPU-heavy (architectural follow-up #13).
- **Impact:** A bulk upload blocks the single web process and degrades every user's requests; a restart drops the queue with no retry.
- **Fix:** Migrate ingest to a worker queue (RQ / Arq / Celery / Dramatiq) with retries.

### [ ] P1-2 · Keyword search → SQLite FTS5 + BM25
- **Issue:** `keyword_candidates_from_sqlite` (`app/main.py`) uses `WHERE chunks.text LIKE '%token%'`.
- **Impact:** Full-table scan per query; slows down past tens of thousands of chunks. See `RETRIEVAL.md` open follow-ups.
- **Fix:** FTS5 virtual table + BM25 ranking.

### [ ] P1-3 · Cap or remove the Chroma-failure fallback
- **Issue:** When Chroma is unavailable, `retrieve()` (`app/main.py` ~1377-1393) decodes **every** chunk's `embedding_json` and computes cosine in pure Python.
- **Impact:** `O(all_chunks × queries × dim)` per request — a transient Chroma hiccup at scale would hang / melt the process.
- **Fix:** Bound the fallback (row cap) or drop it entirely once Chroma sync is trusted.

---

## P2 — scale / cleanup

### [ ] P2-1 · Stop storing vectors in SQLite (`embedding_json`)
- **Issue:** Every chunk's vector is persisted as JSON in SQLite **and** in Chroma; only the (rare) fallback above reads the SQLite copy.
- **Impact:** Doubles vector storage (~12 KB/vector of JSON for 1024-dim e5; ~12 GB at 1M chunks) and adds `dumps`/`loads` CPU on every ingest.
- **Fix:** After P1-3, drop the column (or store raw `float32` bytes instead of JSON). Depends on P1-3.

### [ ] P2-2 · Run vector + keyword search concurrently
- **Issue:** `retrieve()` calls `query_vectors(...)` then `keyword_candidates_from_sqlite(...)` sequentially, though they are independent.
- **Impact:** Minor added latency per question (both are local/fast).
- **Fix:** Run them concurrently (`asyncio.gather`, keyword in a thread since it's sync SQLite).

### [ ] P2-3 · Resolve the in-process briefing lock → enable multi-worker
- **Issue:** Briefing concurrency lock is an in-process dict (`app/main.py`), forcing a single uvicorn worker.
- **Impact:** Cannot use multiple cores / horizontal scale; vertical scaling only.
- **Fix:** Move the lock to a shared store (SQLite row / Redis / filesystem lockfile), then run multiple workers.

---

## P3 — UX / product tradeoffs

### [ ] P3-1 · Streaming responses
- **Issue:** Answers are returned only after the full chat call completes (architectural follow-up #18-streaming).
- **Impact:** High perceived latency, worse with a large/slow model.
- **Fix:** SSE / chunked streaming; reshape `chat_completion` to an async generator and stream into the chat UI.

### [ ] P3-2 · Make query-rewrite / rerank optional or cached
- **Issue:** Each question runs 3 sequential LLM calls (rewrite → rerank → answer).
- **Impact:** End-to-end latency ≈ 3× a single call on the slow shared endpoint — the biggest perceived-latency lever after the network itself.
- **Fix:** Allow disabling rerank/rewrite per deployment, cache rewrites, or skip rewrite for short queries. Product tradeoff (quality vs latency) — measure with the eval harness.
