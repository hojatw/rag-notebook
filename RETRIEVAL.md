# Retrieval — strategy & methodology

This doc explains how the NotebookLM-style POC turns a user question into the chunks the answer LLM sees. It is the canonical retrieval reference; [`README.md`](README.md) describes the user-facing feature set and operational workflow.

Last updated: 2026-05-16. Pipeline lives in [app/main.py](app/main.py) (`retrieve()`, `ask()`), [app/llm.py](app/llm.py) (rewrite / rerank / embedding), [app/ingest.py](app/ingest.py) (chunking), [app/vector_store.py](app/vector_store.py) (Chroma).

## Pipeline at a glance

```text
question
  │
  ▼ (1) query rewrite          app/llm.py:rewrite_search_queries
  ├─► [original, rewrite_1, rewrite_2, ...]   (up to 5 strings)
  │
  ▼ (2) embed all queries      app/llm.py:embed_texts
  │
  ├─► (3a) vector search       app/vector_store.py:query (top-20 per query)
  │       Chroma cosine, scoped by user_id + optional source_ids
  │
  └─► (3b) keyword search      app/main.py:keyword_candidates_from_sqlite (top-20)
          SQLite LIKE on Latin tokens + CJK 2/3-grams
  │
  ▼ (4) hybrid merge           app/main.py:merge_candidates
  │     score = 0.7·vector + 0.3·keyword     (per chunk, dedup)
  │     keep top-20 by score
  │
  ▼ (5) LLM rerank             app/llm.py:rerank_chunks
  │     score = 0.8·rerank + 0.2·hybrid      (top-6)
  │     graceful fallback to hybrid order if rerank fails / not configured
  │
  ▼ (6) low-confidence gate    app/main.py:ask  (NOT retrieve)
  │     if top.score < 0.25  →  "I cannot determine that..."
  │
  ▼ (7) answer generation      app/llm.py:generate_answer
        SYSTEM_PROMPT enforces grounding + language match + [N] citations
  │
  ▼ (8) citation filtering     app/main.py:ask
        Only chunks the answer actually cited with [N] are shown to the user
```

`retrieve()` returns the chunks but does **not** abstain — that lives in `ask()` so the eval harness can still measure raw retrieval scores below the threshold.

## Stage details

### 1. Chunking (offline, at ingest)

See [`app/ingest.py:chunk_text`](app/ingest.py:139). Sentence-aware splitter with CJK awareness:

- **Sentence boundaries** — `[。！？]+ | [.!?](?=\s|$) | \n+`. CJK terminators stand alone; Latin period/!/? require trailing whitespace so decimals (`3.14`), URLs, and most abbreviations are not split.
- **Auto-sized targets** — `is_mostly_cjk(text, threshold=0.30)` picks `CJK_TARGET_CHARS = 400` vs `LATIN_TARGET_CHARS = 800`. CJK characters carry ~2× the information density per char of English so chunk-char budgets differ accordingly.
- **Sentence-level overlap** — `DEFAULT_OVERLAP_SENTENCES = 1`. Carry the last *sentence*, not the last *N characters*, so chunk boundaries never split a grammatical clause.
- **Fallbacks for long sentences** — soft punctuation (`[，、；,;]`) first, then a hard char cut as a last resort to keep every output chunk `<= target_chars`.

Known limitation: "Mr. Smith" splits at "Mr." — acceptable for a POC.

### 2. Query rewriting

[`app/llm.py:rewrite_search_queries`](app/llm.py:138). Sends the question + last 6 history turns to the chat model with `QUERY_REWRITE_PROMPT`; the model returns 1–4 retrieval-focused rewrites as a JSON array. Cleaned and deduped via `unique_nonempty`, then prepended with the original question and capped at 5.

Skipped (`return [question]`) when no chat key/model is configured, or on any parse/HTTP failure — degrades to single-query retrieval rather than failing the request.

### 3a. Vector search

[`app/vector_store.py:query`](app/vector_store.py:101). All rewritten queries are embedded in one batch ([`embed_texts`](app/llm.py:82), batch size 64) and Chroma is queried with the full list of vectors. Chroma collection uses cosine space (`metadata={"hnsw:space": "cosine"}`). Per-chunk `vector_score = max(0, 1 - distance)`; for chunks that match multiple queries we keep the best score.

Filter is always `{user_id}` (multi-tenant isolation) and adds `{source_id: {$in: [...]}}` when the user picked specific sources in the chat form. `n_results=20`.

No-embedding-fallback policy: `embed_texts` raises when the embedding model isn't configured (previously fell back to a SHA-256 hash bag-of-tokens vector — removed because the resulting vectors are dim-incompatible with any real model and silent fallback masked misconfiguration as poor retrieval). The upload route refuses ingestion when LLM isn't configured ([`llm_settings_status`](app/main.py:232)), and `/settings` save probes the embedding endpoint to validate connectivity + dim consistency against the existing Chroma index.

### 3b. Keyword search

[`app/main.py:keyword_candidates_from_sqlite`](app/main.py:1002). Tokenises every rewritten query via [`search_tokens`](app/main.py:1086):

- Latin: `re.findall(r"[\w.-]+", text.lower())` minus a small EN+ZH stopword set; tokens shorter than 2 chars dropped.
- CJK: 2-grams plus 3-grams over the `[一-鿿]` characters of the text ([`cjk_ngrams`](app/main.py:1135)).

Unique tokens are capped at 12, fed into a single `WHERE chunks.text LIKE ? OR ...` query (with `LIMIT limit*4`), then re-ranked locally by [`keyword_score`](app/main.py:1069) — token overlap fraction with a `+0.15` phrase bonus when the full query string appears verbatim. Returns top-20.

This is the part most worth replacing with **SQLite FTS5 + BM25** when corpus size grows — see *Open follow-ups* below.

### 4. Hybrid merge

[`app/main.py:merge_candidates`](app/main.py:1046). Vector + keyword candidate lists are deduped by `chunk_id`, scored as

```
score = 0.7 · max(0, vector_score) + 0.3 · keyword_score
```

For chunks that show up in both lists we recompute against both feature scores and keep the higher final number. Anything with `score <= 0` is dropped. Top-20 by `score` is passed on.

The 0.7/0.3 weighting is empirical (recall@5 = 100%, MRR ≈ 0.88 against the demo notebook before rerank). For RRF-style merging see *Open follow-ups*.

### 5. LLM rerank

[`app/llm.py:rerank_chunks`](app/llm.py:161). Up to 20 candidates plus the question go to the chat model with `RERANK_PROMPT`; the model returns `[{"id": 1, "score": 0.92}, ...]`. Each scored candidate's final score is

```
combined = 0.8 · rerank_score + 0.2 · hybrid_score
```

Top-6 by `combined` is returned. Graceful fallbacks:

- No chat key/model → return hybrid top-6 directly.
- HTTP / parse / empty-scores failure → return hybrid top-6 directly.

Full chunk text is sent (no `text[:900]` truncation) because chunks are already bounded by `chunk_text()` and tail-of-chunk truncation previously dropped answer evidence. Cost stays controlled because chunks are capped at ~400 / ~800 chars.

### 6. Low-confidence abstain

[`app/main.py:ask`](app/main.py:776), threshold `LOW_CONFIDENCE_THRESHOLD = 0.25` ([app/main.py:909](app/main.py:909)). When `not retrieved` or `top.score < 0.25` we skip `generate_answer` entirely and return the canned "I cannot determine that from the selected sources." This avoids paying for a generation call that would either hallucinate or echo the same refusal back.

`metadata.outcome` is set to `low_confidence` / `no_retrieval` / `answered` / `error` so the per-message debug pane can render the reason.

### 7. Answer generation

[`app/llm.py:generate_answer`](app/llm.py:124) with `SYSTEM_PROMPT` ([app/llm.py:11](app/llm.py:11)):

- "Answer only from the provided source excerpts." (grounding)
- "Reply in the same language as the user's question (Traditional Chinese question → Traditional Chinese answer)." (stops the CJK-question / EN-answer regression)
- "If the excerpts do not contain enough information, say: 'I cannot determine that...'" (matches the abstain string)
- "Keep the answer concise and include bracket citations like [1], [2] for the excerpts you used."

User prompt is `"Source excerpts:\n{numbered chunks}\n\nQuestion: {question}"`.

### 8. Citation filtering & UI

[`app/main.py:ask`](app/main.py:776) parses `[N]` markers out of the answer with `re.finditer(r"\[(\d+)\]", answer)` and only persists citations the model actually referenced — same behaviour as NotebookLM. Falls back to all retrieved chunks if the answer contains no markers (defensive: lets the user still see what was retrieved).

[`citation_payload`](app/main.py:1145) serialises each chunk with `score / vector_score / keyword_score / rerank_score` so the per-message debug pane (`📊 N chunks · retrieved Xms · generated Yms · top score Z`) can show the table.

Per-message `messages.metadata_json` row carries `{retrieval_ms, generation_ms, retrieved_chunks, top_score, outcome, threshold?, answer_chars?, error?}` for the debug pane. Legacy messages stored before the column existed default to `'{}'` and render with whatever data is available.

## Evaluation harness

[`tests/eval_retrieval.py`](tests/eval_retrieval.py) + [`tests/eval_questions.json`](tests/eval_questions.json) (25 ground-truth Qs against the demo notebook). Run:

```bash
.venv/bin/python -m tests.eval_retrieval                # default: top-k=5, rerank on
.venv/bin/python -m tests.eval_retrieval --no-rerank    # hybrid-only baseline (strips chat_model)
.venv/bin/python -m tests.eval_retrieval --top-k 10
```

Reports per-question hit rank, **Recall@k**, **MRR**. Skips silently when LLM is not configured (the fallback hash embedding is too noisy to be worth measuring).

### Hit semantics — why ANY-of, not ALL-of

A chunk "hits" iff its `filename == expected_filename` **and** at least one substring from `expected_substrings` appears in `chunk.text`. The any-of rule keeps the metric chunk-size-agnostic: when CJK chunks shrunk from 1200 → 400 chars some `expected_substrings` ended up split across two chunks, and an all-of rule would have falsely penalised retrievals that were in fact correct.

Trade-off: a chunk that contains *only one* expected substring can match even if the user really wanted all the supporting context. Ground-truth substrings are chosen to be specific enough that the false-positive rate stays low; check with the diagnostic in the *Maintaining the eval* section below before adding new questions.

### Current baseline

Against `tests/eval_questions.json` (25 questions, demo notebook, after CJK-aware chunking):

| Configuration | Recall@5 | MRR  |
|---|---:|---:|
| Hybrid only (no rerank) | 100 % | 0.883 |
| Hybrid + rerank         | 100 % | 0.933 |

Recall@5 has saturated at 100 %, so the next retrieval changes need a **harder** eval set (more disambiguation, more needle-in-haystack questions) before they can show measurable lift. See the *Maintaining the eval* section below.

### Maintaining the eval

When adding ground-truth questions:

1. Pick substrings that appear in **only 1–3 chunks** of the expected file. A substring that hits 20+ chunks (e.g. very common terms) is too generic and inflates MRR.
2. Avoid substrings that span sentence boundaries — they may end up split across two chunks, depending on the chunker.
3. After re-chunking (any change to `app/ingest.py:chunk_text` constants or regex), re-verify every question's substrings still appear in *some* chunk of the expected file.

A quick verification script lives in the conversation history (`/tmp/diag_eval_questions.py` style); inline equivalent:

```python
from app.db import connect
from app.ingest import chunk_text
for q in questions:
    rows = conn.execute("SELECT text FROM chunks JOIN sources ON ... WHERE filename = ?", (q["expected_filename"],)).fetchall()
    for sub in q["expected_substrings"]:
        hits = sum(1 for r in rows if sub in r["text"])
        assert hits >= 1, f"substring not found: {sub}"
```

## Tuning knobs (one place to change each)

| Knob | Default | Location | What it controls |
|---|---:|---|---|
| `LATIN_TARGET_CHARS` | 800 | [app/ingest.py:71](app/ingest.py:71) | Max chars per Latin-dominant chunk |
| `CJK_TARGET_CHARS` | 400 | [app/ingest.py:72](app/ingest.py:72) | Max chars per CJK-dominant chunk |
| `DEFAULT_OVERLAP_SENTENCES` | 1 | [app/ingest.py:73](app/ingest.py:73) | Sentences carried into the next chunk |
| `is_mostly_cjk` threshold | 0.30 | [app/ingest.py:76](app/ingest.py:76) | CJK char ratio that flips to CJK chunk size |
| Vector `n_results` | 20 | [app/main.py:926](app/main.py:926) | Per-query vector candidates pulled from Chroma |
| Keyword `limit` | 20 | [app/main.py:927](app/main.py:927) | Top-N kept after SQLite LIKE search |
| Hybrid weights | 0.7 / 0.3 | [app/main.py:1053](app/main.py:1053) | `vector` / `keyword` blend in `merge_candidates` |
| Rerank candidates | 20 | [app/llm.py:176](app/llm.py:176) | How many candidates the LLM reranker sees |
| Rerank weights | 0.8 / 0.2 | [app/llm.py:191](app/llm.py:191) | `rerank` / `hybrid` blend after LLM rerank |
| Rerank `limit` | 6 | [app/llm.py:161](app/llm.py:161) | Chunks returned from `rerank_chunks` |
| `LOW_CONFIDENCE_THRESHOLD` | 0.25 | [app/main.py:909](app/main.py:909) | Top-score under which `ask()` abstains |
| History turns for rewrite | 6 | [app/llm.py:144](app/llm.py:144) | Trailing history passed to query rewriter |
| Embedding batch size | 64 | [app/llm.py:35](app/llm.py:35) | Per-HTTP batch for `embed_texts` |

Change one knob, rerun `python -m tests.eval_retrieval`, compare numbers.

## Observability

Every stage emits a structured log line (key=value pairs). Useful greps in `logs/app.log`:

```text
chat_question_received      user_id, notebook_id, selected_sources, question_chars
query_rewrite_completed     input_chars, output_queries
embedding_api_completed     model, batch_text_count, elapsed_ms
vector_query_completed      queries, candidates, elapsed_ms
retrieve_completed          rewritten_queries, vector_candidates, keyword_candidates, reranked, elapsed_ms
rerank_completed            candidates, scored, returned
chat_completion_completed   model, prompt_tokens_est, response_tokens_est, elapsed_ms
chat_answer_generated       retrieved_chunks, shown_citations, answer_chars
chat_no_retrieval_results   top_score, threshold      ← abstain path
```

In the UI: the "📊 N chunks · retrieved Xms · generated Yms · top score Z" badge under each assistant message opens a per-citation score table — the per-message `metadata_json` + `citations_json` columns drive it.

## Open follow-ups (retrieval-side only)

Status of the original "retrieval top 3":

| # | Item | Status | Expected lift |
|---|---|---|---|
| 1 | CJK-aware chunking | ✅ Landed (see *Chunking* above) | Roughly even MRR vs old hard-cut splitter; citations now respect sentence boundaries, dramatically better readability |
| 2 | SQLite **FTS5** for keyword search | Pending | Bigger notebooks (>5K chunks) get meaningful latency drop; smaller ones get better tokenisation quality (esp. CJK). Replaces the `LIKE '%token%'` scan + Python re-scoring in `keyword_candidates_from_sqlite` |
| 3 | **Reciprocal Rank Fusion** for hybrid merge | Pending | Less sensitive to score-scale drift between vector cosine and keyword overlap. Replaces the `0.7×v + 0.3×k` weighted sum in `merge_candidates` |

Other ideas, in rough order of cost-benefit:

- Cache embeddings per (query, model) — repeated questions in the same conversation don't need a fresh embedding call.
- Stream the answer generation (SSE) so users see the answer mid-response instead of waiting for the full call.
- Tighten the eval set with disambiguation questions across similar files (the current set is saturated at 100 % Recall@5).
- Add a per-source "score cap" so a single dominant source can't crowd out cross-document evidence.
- Backfill `messages.citations_json.source_id` for legacy assistant messages if older local databases need richer citation metadata.

## Pointers

- Retrieval orchestration: [`app/main.py:retrieve`](app/main.py:912), [`app/main.py:ask`](app/main.py:776)
- LLM helpers: [`app/llm.py`](app/llm.py) (rewrite / embed / rerank / generate)
- Chunker: [`app/ingest.py`](app/ingest.py)
- Vector store: [`app/vector_store.py`](app/vector_store.py)
- Eval: [`tests/eval_retrieval.py`](tests/eval_retrieval.py), [`tests/eval_questions.json`](tests/eval_questions.json)
- Unit tests: [`tests/test_chunking.py`](tests/test_chunking.py) (chunker), [`tests/test_core.py`](tests/test_core.py) (retrieve end-to-end against fixtures)
