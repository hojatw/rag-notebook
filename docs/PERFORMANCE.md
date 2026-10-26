# Performance backlog

Prioritised list of known performance / scalability issues, kept so they can be worked through one at a time. Each item lists **Issue → Impact → Fix**, a **priority**, and a **status** box to tick when done.

**Deployment context:** ~200 users (not all concurrent) on a **borrowed, shared** Gemma 4 31B (chat) + multilingual-e5-large (embedding) endpoint — **the serving side cannot be changed, so every adaptation must happen app-side**, though it is not a single fixed host: chat runs on two switchable machines (2026-09-17 addendum in [`DEPLOYMENT_CONTEXT.md`](DEPLOYMENT_CONTEXT.md); every item below was re-checked against that on 2026-09-19, and the four whose text leaned on a single host now say so) — with hundreds-of-page research-report PDFs. Those facts, and the trade-offs they force, are written up in [`DEPLOYMENT_CONTEXT.md`](DEPLOYMENT_CONTEXT.md); the retrieval pipeline is in [`RETRIEVAL.md`](RETRIEVAL.md).

Status legend: `[ ]` todo · `[~]` in progress · `[x]` done · `[-]` deliberately not doing (reason + restart condition recorded)

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
- **Issue:** No HTTP retry/backoff; each question makes 3 chat calls (rewrite, rerank, answer) + embeddings.
- **Impact:** On the shared/throttled endpoint, a single slow or `429` call fails the whole question.
- **Fix:** **Done.** Chat + embedding HTTP go through `_post_json_with_retry` (`app/llm.py`, no third-party retry library): network/timeout errors and `429`/`5xx` are retried with exponential backoff + jitter, tuned via `[llm_retry]` (`max_attempts`, `backoff_base_s`). Set a generous `Timeout seconds` in `/settings`. **Per-host note (2026-09-19):** `Timeout seconds` and `[llm_retry]` are **one stored setting per connection**, not per machine — repointing the chat connection at the other host reuses values tuned for the first one, whose latency profile differs. Re-check them after a switch. The item's conclusion (retry + backoff is mandatory on a borrowed, throttled endpoint) holds on both hosts and is unchanged.

---

## P1 — important at launch scale

### [x] P1-1 · Move ingest off the web process
- **Issue:** Background ingest ran in-process (FastAPI `BackgroundTasks`); `pdfplumber` extraction of hundreds-of-page PDFs is slow and CPU-heavy.
- **Impact:** A bulk upload blocked the single web process and degraded every user's requests; a restart dropped the queue with no retry.
- **Fix:** **Done — DB-backed queue, no new infra (Redis deferred).** Uploads/reindex now `enqueue_source()` into an `ingest_jobs` SQLite table (`app/jobs.py`); a worker drains it via `process_source`. Two modes: a dedicated `python -m app.worker` process (compose `worker` service; `NOTEBOOKLM_INLINE_WORKER=0` on the web app) gives true off-process isolation, **or** an inline worker in the web lifespan (`NOTEBOOKLM_INLINE_WORKER=1`, the default) so a lone `uvicorn` still ingests. The queue also **fixes the restart-drops-the-queue gap** (queued jobs survive) and adds crash recovery via a visibility timeout + capped retries. `app/jobs.py` is the single swap-point for Redis + RQ later. Same single-machine constraint as P2-3: valid while all processes share one `data/app.sqlite3`.

### [x] P1-6 · One hung parse stalled the whole ingest queue (2026-10-25)
- **Issue:** `process_source` ran extraction through `asyncio.to_thread`. A thread cannot be cancelled, and `run_worker_loop` awaits one job at a time.
- **Impact:** A file that made a parser loop forever (crafted, or just pathological) never returned. Every later upload, from every user, sat at "processing" until the worker was restarted. With the inline worker it also burned a core inside the web process.
- **Fix:** **Done.** Extraction runs in a child process with a wall-clock timeout, `[jobs].extract_timeout_s` (300s by default, not yet measured against the corpus). A timed-out or crashed child fails only its own source, without retry, and the queue moves on. The cost is about 0.3–0.6s of interpreter start and imports per source, which is noise next to embedding. `extract_child_finished duration_s=` is logged for every file (in the worker's log; under Compose that is `docker compose logs worker`, see `ROADMAP.md` `O2`), so the timeout can be tuned from real data; `extract_duration_s` is also stored in `diagnostics_json`. Security rationale and limits: `SECURITY.md` → *Uploaded-file parsing runs in a killable child process*.
- **Memory (2026-10-25):** the child also runs under `[jobs].extract_memory_limit_mb` (default 2048, `RLIMIT_DATA`) with numeric thread pools pinned to one. Its own peak RSS is stored as `extract_peak_rss_mb`, and the 檔案處理 tab shows the max and p95 of recent completed parses. **Both defaults (300s, 2048 MB) are still to be measured against the real corpus**; tune them from those figures.

### [ ] P1-2 · Keyword search → SQLite FTS5 + BM25
- **Issue:** `keyword_candidates_from_sqlite` (`app/retrieval.py`) uses `WHERE chunks.text LIKE '%token%'`.
- **Impact:** Full-table scan per query; slows down past tens of thousands of chunks. See `RETRIEVAL.md` open follow-ups.
- **Fix:** FTS5 virtual table + BM25 ranking.
- **Blocked on:** a representative **CJK** corpus + eval set — **not** config. FTS5's `trigram` tokenizer can't match <3-char Chinese queries (2-char terms are the backbone of CN search), so a naive swap regresses Chinese recall, and BM25 ranking needs real-data validation. Segmentation options (jieba search-mode / custom bigram / ICU / neural) and the measured trigram limitation are written up in `docs/RETRIEVAL.md` → *P1-2 design note — CJK tokenization*.

### [x] P1-3 · Cap the Chroma-failure fallback
- **Issue:** When Chroma is unavailable, `retrieve()` decoded **every** chunk's `embedding_json` and computed cosine in pure Python.
- **Impact:** `O(all_chunks × queries × dim)` per request — a transient Chroma hiccup at scale would hang / melt the process.
- **Fix:** **Done.** `fetch_candidate_rows` now `ORDER BY chunks.id DESC LIMIT FALLBACK_MAX_CHUNKS` (2000), so the degraded fallback is bounded; the failure logs a warning pointing at `/admin/index` Rebuild. The explicit "score these rows" path (`user_id=None`, used by tests) is unaffected.

### [x] P1-4 · Upload ran its blocking I/O on the event loop
- **Issue:** `upload_source` (`app/main.py`) was an `async def` whose body was entirely **blocking**: copying each file to disk, several SQLite writes, and `enqueue_source()`. Nothing in it awaited.
- **Impact:** for the duration of an upload, the web process could serve nothing else — the event loop was held by a synchronous file copy. A bulk upload of large sources stalled every other user's requests.
- **Fix:** **Done (with SEC-2).** Changed to a sync `def`, which FastAPI runs in its threadpool. Copying is now chunked through `_store_upload` rather than one `shutil.copyfileobj`, so a single file is never held in memory either.

### [x] P1-5 · CSV ingest materialises the file ~5.7x over
- **Issue (historical):** `_read_csv_sheet` (`app/ingest.py`) did `path.read_bytes()` → full `str` decode → `text.splitlines()`, so the whole file existed three times before a single row was parsed. **Measured: 27.6 MB peak for a 4.8 MB Big5 export (~5.7x).** At the `extract_max_file_bytes` cap of 20 MB that was ~114 MB for one source.
- **Fix:** **Done.** Encoding/delimiter detection reads a bounded 64 KiB sample, then `io.TextIOWrapper` incrementally decodes the binary file with `newline=""` and feeds `csv.reader`; only the configured row/column caps are retained. `TextIOWrapper` is used instead of passing binary-line chunks through `codecs.iterdecode`, because byte-level line iteration breaks UTF-16 newline boundaries. The parser still consumes through EOF after the row cap, so a sample that looks like UTF-8 is verified against every later byte. A late `UnicodeDecodeError` re-detects from the bounded failing buffer, releases the failed pass before retrying, and re-reads the file incrementally; replacement decoding remains the last resort and its replacement count is preserved in diagnostics.
- **Correctness regression covered:** a mostly-ASCII file whose Big5 content starts after 64 KiB must take the detected fallback even when the retained row cap is reached, and the main path is pinned never to call `Path.read_bytes()`. Feeding physical lines to `csv.reader` also preserves embedded newlines inside quoted fields, which `text.splitlines()` previously deleted.
- **Local verification (2026-08-29):** a synthetic 4.80 MB Big5 parser run peaked at 8.58 MB under `tracemalloc` (~1.8x file size), versus the historical 27.6 MB / ~5.7x observation. This is a bounded local fixture, not customer-corpus measurement.
- **Cap retained:** `.csv` still uses `extract_max_file_bytes` (20 MB). Streaming removes the whole-file copies, but the cap continues to bound parse time and pathological single records/fields that can approach the file's full size.

## P2 — scale / cleanup

### [-] P2-1 · Reduce the SQLite vector copy (`embedding_json`) — *decided: keep as-is*
- **Issue:** Every chunk's vector is persisted as JSON in SQLite **and** in Chroma.
- **Correction (2026-06-09):** an earlier note here claimed only the fallback reads the SQLite copy — that was wrong. `vector_store.py:sync_from_sqlite` reads `embedding_json` to (re)build Chroma, so **SQLite is the durable source of truth and Chroma is a derived index** rebuilt from it without re-embedding (startup diff-sync, `/admin/index` Rebuild). Dropping the column means Chroma becomes the only vector copy; any rebuild/recovery would require **re-embedding the whole corpus** (cost + time + the endpoint being up).
- **Impact:** ~12 KB/vector of JSON (~12 GB at 1M chunks) + `dumps`/`loads` CPU on ingest — but it buys cheap, offline index rebuilds.
- **Options considered:** Don't simply delete. (a) keep as-is (durability > disk); (b) store raw `float32` bytes instead of JSON (~4 KB/vector, ~3× smaller, still rebuildable) — the better middle ground; (c) drop it and accept re-embedding on rebuild.
- **Decision (2026-07-25): (a) keep as-is — deliberately not doing.** At POC scale disk is not the bottleneck, and the JSON copy buys the property that matters most here: rebuilding Chroma is offline, free, and does not require the customer's shared embedding endpoint to be up. (c) is explicitly rejected — it would turn "rebuild the index" into an operation that costs GPU time on a borrowed endpoint.
- **Supporting evidence (2026-09-19):** the 2026-09 repair window is this decision's argument happening in real life — a chat host went away for maintenance, and the embedding endpoint is borrowed under the same terms. "Rebuilding the index must not require someone else's GPU to be up" is not a hypothetical property; keep (c) rejected.
- **Restart condition:** revisit and implement **(b) `float32` bytes** if either (1) a single deployment passes roughly **300k chunks**, or (2) disk pressure on the target host becomes a real operational concern. (b) keeps offline rebuildability, so it stays the preferred move whenever this reopens — no need to re-litigate (c).

### [x] P2-2 · Run vector + keyword search concurrently
- **Issue:** `retrieve()` called `query_vectors(...)` then `keyword_candidates_from_sqlite(...)` sequentially, though they are independent.
- **Impact:** Minor added latency per question (both are local/fast).
- **Fix:** **Done.** Both now run via `asyncio.gather(asyncio.to_thread(query_vectors, …), asyncio.to_thread(keyword_candidates_from_sqlite, …))` so their I/O overlaps; behavior-preserving (same merge/rank). Both stay inside the existing `try`, so a Chroma failure still trips the capped SQLite fallback. Covered by `test_retrieve_runs_vector_and_keyword_search_concurrently`.

### [x] P2-3 · Resolve the in-process briefing lock → enable multi-worker
- **Issue:** Briefing concurrency lock was an in-process dict (`app/main.py`), forcing a single uvicorn worker.
- **Impact:** Cannot use multiple cores / horizontal scale; vertical scaling only.
- **Fix:** **Done.** The lock now lives in a `briefing_locks` SQLite row (`app/db.py`), keyed by `notebook_id` with the same 90 s stale-timeout self-heal. `_acquire_briefing_lock` does the check-and-set inside a `BEGIN IMMEDIATE` write transaction so two workers can't both acquire (the dict couldn't guarantee this). Same single-machine constraint as P1-1: valid while all workers share one `data/app.sqlite3`. This *unblocks* multiple workers; **the actual uvicorn worker count is a deploy decision and is not changed here.**

### [x] P2-4 · Keep async LLM routes from blocking on SQLite (PERF-2)
- **Issue:** async routes that wait on LLM/network work also opened SQLite connections and ran their bounded query/write phases directly on the event loop. The affected surfaces included starter questions, briefing, comparison, chat and streaming chat, follow-ups, meeting minutes, artifacts, and summary translation.
- **Impact:** SQLite is normally fast, but lock waits or slow local storage could pause every request handled by that worker. The problem becomes visible under concurrent use even though each individual query is small.
- **Fix:** **Done.** Route-local SQLite phases now run through `asyncio.to_thread`; each synchronous helper opens and closes its own connection inside the worker thread, so no `sqlite3.Connection` crosses thread boundaries. Related queries stay grouped into one phase to avoid a thread handoff per SQL statement. LLM/network awaits and HTML rendering remain on the event loop. A route-level regression test delays the real connection boundary and confirms the event loop continues ticking; existing route behavior tests cover persistence, authorization, caching, streaming, and rendered results.

### [ ] P2-5 · Cache OIDC discovery + JWKS (low priority; from review item PERF-3, 2026-08-22)
- **Issue:** `_oidc_discover()` (`app/main.py`) fetches the IdP's `/.well-known/openid-configuration` on **every** OIDC login start and callback, and the callback then fetches `jwks_uri` as well — nothing is cached.
- **Impact:** Extra outbound HTTPS round-trips per login (discovery on `/auth/oidc/login`, discovery + JWKS on the callback), so login latency and availability track the IdP's metadata endpoints. At ~200 users logging in a few times a day this is small, which is why it is not scheduled.
- **Fix:** A short TTL cache (e.g. 10–60 min) for the discovery document and JWKS, keyed by discovery URL. On a signature failure with an unknown `kid`, refetch JWKS once before rejecting, so IdP key rotation still works.
- **Restart condition:** login latency complaints, IdP rate limiting, or OIDC becoming the default login path for a larger user base.

### [x] P2-6 · Cache the Fernet key derivation (noticed 2026-09-12 while profiling the test suite)
- **Issue:** `_fernet(secret)` (`app/security.py`) runs a full 200,000-iteration PBKDF2 on **every** call, and nothing caches the result. `load_llm_settings()` (`app/db.py`) calls `decrypt_secret` twice per invocation — once for the chat API key, once for the embedding key — and there are ~19 call sites across `app/main.py`, `app/ingest.py`, `app/evals.py` and `app/settings.py`, including the chat / ask / ingest hot paths.
- **Impact:** ~85 ms per derivation measured locally, so ~170 ms of pure CPU added to every request that touches an LLM setting — **but only when an API key is actually stored.** `decrypt_secret` returns early on an empty token, so a deployment whose connections carry no key pays nothing. **Corrected wording (2026-09-19):** "keyless" is a property of each **connection**, not of a model or of this deployment as a whole — the original text attached it to Gemma. As confirmed by the project lead, **both** chat hosts (Gemma 4 31B on the new machine, gpt-oss-120b on the older one) and the e5 embedding endpoint are keyless, so this deployment pays nothing on either host. This remains latent cost that appears the moment any one connection configures a key. The fix below is unconditional and was never affected.
- **Fix:** **Done.** The derivation moved into `_derive_fernet_key(secret)` under `functools.lru_cache`; `_fernet` just wraps the cached key in a `Fernet`. Measured on the two decryptions one `load_llm_settings()` performs: **163.8 ms → 0.056 ms** after the process has derived once. No new exposure: the derived key is a deterministic function of `NOTEBOOKLM_SECRET`, which the process already holds in its environment for the whole of its lifetime. The cache is keyed by the secret, so a rotated `NOTEBOOKLM_SECRET` cannot be served a stale key.
- **Held by:** `tests/test_security.py::test_fernet_key_is_derived_once_per_secret`, which counts actual `PBKDF2HMAC` constructions rather than `lru_cache`'s own hit/miss bookkeeping — so it goes red if someone later reinstates an inline derivation inside `_fernet`, which is the regression that matters.

---

## P3 — UX / product tradeoffs

**Execution order for the open latency items (2026-10-20):** `P3-3` → `P3-9` (after `QUALITY.md` `Q0-7` for the host in question) → `P3-4` → `P3-5` → `P3-2` → `P3-10` → `P3-8` → `P3-11`. `P3-9` is second because it needs no code and shortens time-to-first-text directly; `P3-10` precedes `P3-8` because precomputed answers are background load.

### [x] P3-1 · Streaming responses
- **Issue:** Answers were returned only after the full chat call completed.
- **Impact:** High perceived latency, worse with a large/slow model.
- **Fix:** **Done.** Added a streaming chat path that emits retrieval/generation status, then swaps in the saved Markdown/citation message when complete. E2's structural abstention protocol means the provider completion is buffered (hard cap: 100,000 characters) and classified before answer text is emitted, so a late or truncated `[[RAG_ABSTAIN]]` marker cannot leak earlier text. By default (`runtime.answer_stream_gate_chars = 0`) the whole classified answer is sent as one event; a positive gate streams incrementally after the first N characters are classified, and an SSE `discard` event clears any preamble if the model abstains later. The invariants and the residual exposure of a positive gate are specified in `docs/SECURITY.md` → *E2 notebook domain hints and answer policy*. **Adherence gap, tracked in `QUALITY.md` `Q0-7` (2026-09-19):** the buffering and classification here are model-agnostic and fail-closed, so nothing breaks on a host switch — but the marker only exists if the chat model **emits** it, and no check confirms that per host. A model that never emits `[[RAG_ABSTAIN]]` silently removes E2's structural refusal layer instead of tripping anything. That is a validation gap, not a performance one, so this item stays done and `Q0-7` carries it. Non-streaming chat helpers remain for Studio features.

### [ ] P3-2 · Make query-rewrite / rerank optional or cached
- **Issue:** Each question runs 3 sequential LLM calls (rewrite → rerank → answer).
- **Impact:** End-to-end latency ≈ 3× a single call on the slow shared endpoint — the biggest perceived-latency lever after the network itself.
- **Measurement provenance (2026-09-19):** the "3×" structure holds on both chat hosts — it is a property of the pipeline, not of a model — but any wall-clock number supporting it was taken on **one** host and is not transferable; a 120B and a 31B model do not share a latency profile. **待確認:** no side-by-side latency measurement of the two hosts exists in this repo. Record which host a number came from when one is added.
- **Fix:** Allow disabling rerank/rewrite per deployment, cache rewrites, or skip rewrite for short queries. Product tradeoff (quality vs latency) — measure with the eval harness.
- **Plan (2026-10-20, from the token / prompt & context compression review):** it follows `P3-3`, `P3-9`, `P3-4` and `P3-5` in the P3 execution order above. Skipping rerank when the hybrid ranking is already confident removes a whole sequential call, which beats any prompt-length saving below. Shape: a confidence threshold in `app/config.py`, **default off** (current behaviour). Gate: eval recall@k does not drop and abstain rate does not rise. Score: skip ratio × latency saved per skipped call.

### [ ] P3-3 · Measure where prompt tokens and latency actually go (prerequisite for P3-4/P3-5/P3-2)
- **Issue:** The compression review (2026-10-20) found that the token *guards* are complete (`[max_tokens]` per `call_type`, probed chat/embedding windows, `fit_answer_chunks`, `fit_rewrite_inputs`) but no item has measured which call dominates input size or latency. The estimates below are arithmetic, not measurements: rerank sends `candidate_pool_size` (20) full chunks ≈ 20 × 400 CJK chars × 0.6–0.75 tokens/char ≈ **4–6k tokens (待確認)**; the answer prompt holds 6 chunks ≈ 2–3k (待確認); the fixed instructions sent on every question (answer + rewrite + rerank prompts) are ≈ 1.5k chars ≈ 400–500 tokens (待確認).
- **Fix:** No new table. Query `llm_usage_events` per `call_type` (`query_rewrite`, `rerank`, `answer_stream`) for p50/p95 `prompt_tokens`, filtering `is_estimated = 0`, plus end-to-end latency; record **which chat host** each number came from (see `P3-2` provenance note). `llm_usage_events.latency_ms` covers LLM calls only, so also time the **non-LLM stages** per question — query embedding, Chroma query, keyword `LIKE`, merge/diversify — which `P3-11` needs to decide whether it is worth doing (a timing log line per stage is enough; no new table).
- **Gate:** `P3-5` proceeds only if rerank `prompt_tokens` is clearly larger than the answer's (threshold: see the P3-5 建議 below). `P3-4` is cheap enough to proceed regardless.
- **Tooling landed (2026-10-26)，正式數字待進客戶環境實跑：**
  - **`scripts/usage_percentiles.py`** — `.venv/bin/python -m scripts.usage_percentiles --db data/app.sqlite3 [--since YYYY-MM-DD] [--json]`。以 SQLite `mode=ro` 開啟、不寫入；只讀 `call_type`／`provider`／`model`／`status` 與數值欄位，不讀 `metadata_json` 或任何含內容的表，輸出只有彙總數字，可帶出客戶環境。依 `(call_type, model)` 分組：`llm_usage_events` 沒有記錄 base URL，**model 名就是 chat 主機的識別**（Gemma 4 31B／gpt-oss-120b）；若兩台日後跑同一模型就分不出主機，屆時需加欄位。tokens 只取 `is_estimated = 0`，延遲只取 `status = 'succeeded'`，預設排除 Eval Workbench 流量（`--include-evals` 可納入）。每台主機另輸出 rerank／answer `prompt_tokens` p50 比值與「rewrite＋query embedding＋rerank＋answer 各段 p50 相加」。
  - **端到端延遲的限制：** 只有 `answer`／`answer_stream` 事件會回填 `message_id`，rewrite／rerank／embedding 事件連不回同一題，所以腳本給的是各段 p50 相加的**估計值**，不是逐題實測。逐題的檢索端時間看下面的 log 行（`retrieve_completed elapsed_ms` 含 rewrite＋rerank）。
  - **非 LLM 階段計時 log** — `app/retrieval.py` 每題一行 `retrieve_stage_timings mode=… rewrite_ms embed_ms vector_ms keyword_ms search_ms merge_ms rerank_ms`（INFO，只有時間與 mode，不含題目）。vector 與 keyword 在 thread 內各自計時，兩者並行執行，`search_ms` 是實際等待的重疊牆鐘時間；fallback 路徑沒跑的階段記 `-`。`P3-11` 要的「embedding＋search 幾百 ms」就是 `embed_ms + search_ms`。沒有新表、沒有新設定，預設行為不變。
  - **本機示範（2026-10-26，⚠️ 不具代表性）：** 開發機 DB 的唯讀複本，chat 是 `gpt-4.1-mini`（雲端 API，不是兩台借用主機之一）、embedding 是 `text-embedding-ada-002`、語料是開發用測試文件，n≈106 題。rerank `prompt_tokens` p50/p95 = 5819/10474，answer_stream 1868/3264，比值 **3.1×**；query_rewrite 132/654。延遲 p50：rewrite 1445 ms、query embedding 367 ms、rerank 1743 ms、answer_stream 2513 ms。只證明工具能跑、估算方向（rerank 輸入 4–6k）大致對；**不得**拿來決定 P3-5。
  - **P3-5 門檻建議（待 Philip 確認）：** 在**每台** chat 主機上分別看，rerank／answer `prompt_tokens` p50 比值 **≥ 1.5×**、且該主機 rerank 的 `latency_ms` p50 佔各段 p50 相加的 **≥ 25%**，才做 P3-5。只看 token 比值不夠：本部署沒有 per-token 帳單（`DEPLOYMENT_CONTEXT.md`），P3-5 的唯一收益是延遲，若 rerank 在某台主機上本來就快（prefill 便宜），縮輸入也省不了多少。兩台主機結論不同時，以使用者實際較常用的那台為準。每筆數字都要附主機（model）與量測日期、`--since` 範圍。
  - **剩餘：** 有人進客戶環境時，對兩台主機各跑一次腳本（`--since` 設在切換主機之後）並收集一段時間的 `retrieve_stage_timings`，把數字填回這裡，才能勾掉本項。

### [ ] P3-4 · Cap per-message length of history sent to query rewrite
- **Issue:** `_question_context` (`app/main.py`) loads the last 6 messages **including full assistant answers**, and `fit_rewrite_inputs` bounds them only against the chat window (drops whole messages, and only when the window was probed). Rewrite only needs the topic of the conversation, not multi-thousand-character answers.
- **Fix:** Truncate each assistant message in the rewrite context to N chars, N in `app/config.py` with **default 0 = unlimited** (current behaviour; keep `tests/test_config.py` in sync). Not an LLM summary — that would add a sequential call. The answer prompt does not carry history and is unaffected.
- **Why it matters more than first thought (2026-10-20 measurement, see `P3-7`):** only 24.4% of logged questions are first turns, so ~3 in 4 questions reach rewrite **with** history.
- **Risk:** a follow-up's referent ("它的副作用呢？") can sit late in a long assistant answer, so a head-only cut may drop it. Keep user messages whole; for assistant messages keep head + tail rather than head only.
- **Gate:** `tests.eval_retrieval` always runs with `history=[]` and `app/evals.py` rejects items with history, so **the eval harness cannot measure this change**. Instead replay logged follow-ups offline: run `retrieve` with full vs capped history and require the top-`final_chunk_count` chunk sets to overlap (≥ 5/6, threshold 待訂) on the large majority (待訂). The new unit test is seen failing with the cap removed. **Score:** `query_rewrite` `prompt_tokens` p95.

### [ ] P3-5 · Decouple rerank input size from the retrieval candidate pool
- **Issue:** `RERANK_INPUT_SIZE = config.retrieval.candidate_pool_size` (`app/llm.py`), and the same value is also the vector `n_results` / keyword limit. Shrinking the rerank prompt by lowering it would also shrink recall.
- **Fix:** Add a separate `retrieval.rerank_input_size` (default = 20, current behaviour), then compare 20 / 12 / 8 with `tests.eval_retrieval`. Do **not** re-introduce per-candidate text truncation — the old 900-char cut lopped off answer evidence (see the comment in `rerank_chunks`).
- **Also evaluate the rerank output length.** ~20 × `{"id": n, "score": x}` is roughly 200–300 completion tokens (估算, 待確認), and on a self-hosted model decode is far slower per token than prefill. The output cannot become an id-only ranking — the final order blends rerank score × `rerank_weight` (0.8) with the hybrid score × `rerank_base_weight` (0.2), so scores are required — but a compact shape such as `[[1,0.9],…]` keeps them. Changing the shape touches `RERANK_PROMPT` and `parse_rerank_scores`; gate it with the same eval as above.
- **Gate:** recall@k and abstain rate hold. **Score:** rerank `prompt_tokens` and per-question latency p50.

### [-] P3-6 · Prompt-template compression and evidence compression — *decided: not doing*
- **Considered (2026-10-20):** shortening the instruction prompts; LLM summarisation of history or chunks; token-pruning compressors such as LLMLingua / LongLLMLingua / LLMLingua-2 on the answer evidence.
- **Why not:** on this deployment there is no per-token bill (both chat hosts and e5 are keyless — `DEPLOYMENT_CONTEXT.md`), so the only payoff is latency, and latency is dominated by the three sequential calls (`P3-2`), not prompt length. The fixed instructions are a small fraction of input and carry the grounding / abstain-marker / language rules whose adherence is already fragile per host (`QUALITY.md` `Q0-7`). Compressing evidence breaks the citation contract `fit_answer_chunks` exists to protect: `[N]` would point at text that differs from the source. Summarisation adds a sequential call. A pruning compressor adds a local model dependency, unvalidated on this Traditional-Chinese corpus.
- **Restart condition:** a deployment billed per token where `P3-3` shows input cost matters; or, for rerank candidates only (never answer evidence), `P3-5`/`P3-2` landed and rerank prefill is still the measured bottleneck.

### [-] P3-7 · Semantic answer cache (reuse an earlier answer for a similar question) — *decided: not doing*
- **Considered (2026-10-20):** exact / normalized and embedding-similarity answer caching (GPTCache, Redis LangCache style), including a variant that reuses an answer only when the new question's hybrid retrieval returns the same chunk set.
- **Measured (2026-10-20, one deployment's `data/app.sqlite3`, read-only count script):** 131 user questions over 17 notebooks (p50 7, max 23 per notebook). First turns (no history — the only turns a cache could serve safely): 32 (24.4%). Repeats of an earlier question in the same notebook: exact-normalized 8 (6.1%); char-bigram Jaccard ≥0.8: 8 (6.1%), **of which first turns 1/32 (3.1%)**; ≥0.6: 12 (9.2%), first turns 2/32 (6.2%). Bigram Jaccard is a lexical proxy and undercounts paraphrases, so read these as a lower bound — but n is small (1/32 has a wide interval).
- **Why not:** (1) the cacheable population is ~1 hit in 32 first turns, below the 5% bar set before measuring; (2) every exact repeat is **not** a first turn — they are re-asks inside a conversation, which most plausibly means the user was not satisfied (待確認), so a cache would hand back the very answer being rejected; (3) notebooks are per-user (no sharing in the schema), so hits cannot come from other users; (4) a correct key must include source set + index generation, retrieval params, domain policy, chat host and history — missing any one serves a confident stale answer.
- **Restart condition:** notebooks become shared across users (`FEATURE_PROPOSAL_ENTERPRISE_WORKSPACE`), **or** a re-measurement with ≥300 first-turn questions shows first-turn near-repeat ≥5%.
- **Side signal worth keeping:** in-conversation re-asks may be a dissatisfaction signal comparable to E3a feedback; if pursued, that belongs in `QUALITY.md`, not here.

### [ ] P3-8 · Precompute answers for starter questions (measure clicks first)
- **Idea (2026-10-20):** starter questions are a fixed, per-notebook list the user *chooses* from, so serving a precomputed answer has none of `P3-7`'s "similar but different question" risk, and a click becomes near-instant. Deriving an FAQ from other users' history is deferred with `P3-7` (per-user notebooks; it would also expose one user's questions to another).
- **Unknown:** how often starters are clicked. A click POSTs the question text like a typed one (`app/static/app.js`, `_suggestions.html`), so nothing records the origin. Ceiling: starters show only in the empty state, i.e. at most the 24.4% first-turn share.
- **Step 1 (measure):** tag the asked message with `origin = starter` in `messages.metadata_json` (update `docs/SCHEMA.md`'s JSON-blob contracts and add a round-trip test). **Gate:** proceed if starter clicks are a material share of first turns (threshold 待訂).
- **Step 2 (build):** generate the answers in a low-priority background job after the starter list is produced; key = (notebook, question text, source set + index generation, chat settings fingerprint); invalidate on `indexed-sources-changed`. **Depends on `P3-10`** landing first. Show "generated from sources as of <date>" and a regenerate action. Cap background calls — the serving side is shared and rate-limited (`DEPLOYMENT_CONTEXT.md` §2), so precompute must never compete with live questions.
- **Observability:** precomputed-answer click rate, regenerate-after-click rate, background `prompt_tokens`. **Failure mode:** an incomplete key or missed invalidation serves a stale answer.

### [ ] P3-9 · Measure provider streaming per chat host, then decide `answer_stream_gate_chars`
- **Issue:** `[runtime].answer_stream_gate_chars = 0` buffers the whole answer and classifies it before sending, so time-to-first-text equals the full generation time. The only arrival measurement recorded (`app/config.py` comment) is Azure OpenAI, which bursts the completion at the end — a gate there buys ~0.2s. Self-hosted vLLM streams token by token, the case where a gate pays off, and **no measurement exists for either self-hosted chat host** (Gemma 4 31B, gpt-oss-120b) (待確認).
- **Fix:** No code. Run `python -m tests.probe_provider_stream` against each host (procedure in `docs/DEVELOPMENT.md`). If chunk arrivals are spread across the generation, set the gate to 120 (the value the config comment derives from this corpus); if bunched, keep 0. Record which host each result came from.
- **Trade-off:** a positive gate accepts the residual exposure described in `docs/SECURITY.md` → *E2 notebook domain hints and answer policy*. Because the structural refusal only exists if the model emits `[[RAG_ABSTAIN]]`, **do `QUALITY.md` `Q0-7` for a host before enabling the gate on it.**
- **Score:** time-to-first-text p50 per host. **Failure mode:** an abstaining answer briefly shows a preamble before the SSE `discard` clears it.

### [ ] P3-10 · Give live questions priority over background LLM calls
- **Issue:** chat calls have no concurrency control (the only `asyncio.Semaphore` in `app/` bounds embedding batches in `embed_texts`). Post-index source summaries, starter questions, briefing and Studio jobs share the same borrowed, rate-limited endpoint (`DEPLOYMENT_CONTEXT.md` §2) as live questions, and `P3-8` would add more background load.
- **Fix:** one shared concurrency limit for background `call_type`s (size in `app/config.py`, default preserving current behaviour), live question calls (`query_rewrite`, `rerank`, `answer_stream`) unbounded. Must not deadlock the ingest worker or break LLM retry/backoff.
- **Gate:** first confirm from `llm_usage_events` timestamps that background and live calls actually overlap; if they rarely do, defer. **Score:** live answer latency p95 during ingest bursts.

### [ ] P3-11 · Start retrieval for the raw question while query rewrite runs
- **Issue:** `retrieve()` (`app/retrieval.py`) awaits `rewrite_search_queries` before embedding or searching anything, so embedding + vector/keyword search wait on an LLM call.
- **Fix:** embed and hybrid-search the raw question concurrently with rewrite; when rewrite returns, embed/search only the queries it added and merge. No extra LLM call; results must equal the sequential path's when rewrite returns only the raw question.
- **Gate:** `P3-3`'s non-LLM stage timings. If embedding + search is only a couple of hundred ms (待確認), the added complexity is not worth it — mark `[-]` with that number.
