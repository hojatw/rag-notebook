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

### [x] P1-1 · Move ingest off the web process
- **Issue:** Background ingest ran in-process (FastAPI `BackgroundTasks`); `pdfplumber` extraction of hundreds-of-page PDFs is slow and CPU-heavy (architectural follow-up #13).
- **Impact:** A bulk upload blocked the single web process and degraded every user's requests; a restart dropped the queue with no retry.
- **Fix:** **Done — DB-backed queue, no new infra (Redis deferred).** Uploads/reindex now `enqueue_source()` into an `ingest_jobs` SQLite table (`app/jobs.py`); a worker drains it via `process_source`. Two modes: a dedicated `python -m app.worker` process (compose `worker` service; `NOTEBOOKLM_INLINE_WORKER=0` on the web app) gives true off-process isolation, **or** an inline worker in the web lifespan (`NOTEBOOKLM_INLINE_WORKER=1`, the default) so a lone `uvicorn` still ingests. The queue also **fixes the restart-drops-the-queue gap** (queued jobs survive) and adds crash recovery via a visibility timeout + capped retries. `app/jobs.py` is the single swap-point for Redis + RQ later. Same single-machine constraint as P2-3: valid while all processes share one `data/app.sqlite3`.

### [ ] P1-2 · Keyword search → SQLite FTS5 + BM25
- **Issue:** `keyword_candidates_from_sqlite` (`app/retrieval.py`) uses `WHERE chunks.text LIKE '%token%'`.
- **Impact:** Full-table scan per query; slows down past tens of thousands of chunks. See `RETRIEVAL.md` open follow-ups.
- **Fix:** FTS5 virtual table + BM25 ranking.
- **Blocked on:** a representative **CJK** corpus + eval set — **not** config. FTS5's `trigram` tokenizer can't match <3-char Chinese queries (2-char terms are the backbone of CN search), so a naive swap regresses Chinese recall, and BM25 ranking needs real-data validation. Segmentation options (jieba search-mode / custom bigram / ICU / neural) and the measured trigram limitation are written up in `docs/RETRIEVAL.md` → *P1-2 design note — CJK tokenization*.

### [x] P1-3 · Cap the Chroma-failure fallback
- **Issue:** When Chroma is unavailable, `retrieve()` decoded **every** chunk's `embedding_json` and computed cosine in pure Python.
- **Impact:** `O(all_chunks × queries × dim)` per request — a transient Chroma hiccup at scale would hang / melt the process.
- **Fix:** **Done.** `fetch_candidate_rows` now `ORDER BY chunks.id DESC LIMIT FALLBACK_MAX_CHUNKS` (2000), so the degraded fallback is bounded; the failure logs a warning pointing at `/admin/index` Rebuild. The explicit "score these rows" path (`user_id=None`, used by tests) is unaffected.

---

## P2 — scale / cleanup

### [ ] P2-1 · Reduce the SQLite vector copy (`embedding_json`) — *tradeoff, not a free cleanup*
- **Issue:** Every chunk's vector is persisted as JSON in SQLite **and** in Chroma.
- **Correction (2026-06-09):** an earlier note here claimed only the fallback reads the SQLite copy — that was wrong. `vector_store.py:sync_from_sqlite` reads `embedding_json` to (re)build Chroma, so **SQLite is the durable source of truth and Chroma is a derived index** rebuilt from it without re-embedding (startup diff-sync, `/admin/index` Rebuild). Dropping the column means Chroma becomes the only vector copy; any rebuild/recovery would require **re-embedding the whole corpus** (cost + time + the endpoint being up).
- **Impact:** ~12 KB/vector of JSON (~12 GB at 1M chunks) + `dumps`/`loads` CPU on ingest — but it buys cheap, offline index rebuilds.
- **Fix (decision needed):** Don't simply delete. Options: (a) keep as-is (durability > disk); (b) store raw `float32` bytes instead of JSON (~4 KB/vector, ~3× smaller, still rebuildable) — the better middle ground; (c) drop it and accept re-embedding on rebuild. Recommend **(b)** if disk matters, else leave it.

### [x] P2-2 · Run vector + keyword search concurrently
- **Issue:** `retrieve()` called `query_vectors(...)` then `keyword_candidates_from_sqlite(...)` sequentially, though they are independent.
- **Impact:** Minor added latency per question (both are local/fast).
- **Fix:** **Done.** Both now run via `asyncio.gather(asyncio.to_thread(query_vectors, …), asyncio.to_thread(keyword_candidates_from_sqlite, …))` so their I/O overlaps; behavior-preserving (same merge/rank). Both stay inside the existing `try`, so a Chroma failure still trips the capped SQLite fallback. Covered by `test_retrieve_runs_vector_and_keyword_search_concurrently`.

### [x] P2-3 · Resolve the in-process briefing lock → enable multi-worker
- **Issue:** Briefing concurrency lock was an in-process dict (`app/main.py`), forcing a single uvicorn worker.
- **Impact:** Cannot use multiple cores / horizontal scale; vertical scaling only.
- **Fix:** **Done.** The lock now lives in a `briefing_locks` SQLite row (`app/db.py`), keyed by `notebook_id` with the same 90 s stale-timeout self-heal. `_acquire_briefing_lock` does the check-and-set inside a `BEGIN IMMEDIATE` write transaction so two workers can't both acquire (the dict couldn't guarantee this). Same single-machine constraint as P1-1: valid while all workers share one `data/app.sqlite3`. This *unblocks* multiple workers; **the actual uvicorn worker count is a deploy decision and is not changed here.**

---

## P3 — UX / product tradeoffs

### [x] P3-1 · Streaming responses
- **Issue:** Answers were returned only after the full chat call completed (architectural follow-up #18-streaming).
- **Impact:** High perceived latency, worse with a large/slow model.
- **Fix:** **Done.** Added a streaming chat path that emits retrieval/generation status and answer chunks, then swaps in the saved Markdown/citation message when complete. Non-streaming chat helpers remain for Studio features.

### [ ] P3-2 · Make query-rewrite / rerank optional or cached
- **Issue:** Each question runs 3 sequential LLM calls (rewrite → rerank → answer).
- **Impact:** End-to-end latency ≈ 3× a single call on the slow shared endpoint — the biggest perceived-latency lever after the network itself.
- **Fix:** Allow disabling rerank/rewrite per deployment, cache rewrites, or skip rewrite for short queries. Product tradeoff (quality vs latency) — measure with the eval harness.
