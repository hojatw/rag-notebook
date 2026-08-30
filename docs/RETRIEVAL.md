# Retrieval — strategy & methodology

This doc explains how the NotebookLM-style POC turns a user question into the chunks the answer LLM sees. It is the canonical retrieval reference; [`README.md`](../README.md) describes the user-facing feature set and operational workflow.

Last updated: 2026-08-24. Pipeline lives in [app/retrieval.py](../app/retrieval.py) (`retrieve()`, hybrid search, scoring, `ACTIVE_RETRIEVAL_PARAMS`), [app/domain_policy.py](../app/domain_policy.py) (E2 matching/expansion/snapshots), [app/main.py](../app/main.py) (`ask()` — low-confidence gate + citation filtering), [app/llm.py](../app/llm.py) (rewrite / rerank / embedding / grounded answer), [app/ingest.py](../app/ingest.py) (chunking), and [app/vector_store.py](../app/vector_store.py) (Chroma).

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
  └─► (3b) keyword search      app/retrieval.py:keyword_candidates_from_sqlite (top-20)
          SQLite LIKE on Latin tokens + CJK 2/3-grams
  │
  ▼ (4) hybrid merge           app/retrieval.py:merge_candidates
  │     score = 0.7·vector + 0.3·keyword     (per chunk, dedup)
  │     keep top-20 by score
  │
  ▼ (5) LLM rerank             app/llm.py:rerank_chunks
  │     score = 0.8·rerank + 0.2·hybrid      (default top-6)
  │     graceful fallback to hybrid order if rerank fails / not configured
  │
  ▼ (6) low-confidence gate    app/main.py:ask  (NOT retrieve)
  │     if top.score < 0.25  →  localized app-side abstention (default threshold)
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

Ingest chunks via [`app/ingest.py:chunk_sections`](../app/ingest.py); [`chunk_text`](../app/ingest.py) is a thin single-text wrapper over it. Sentence-aware splitter with CJK awareness:

- **Sentence boundaries** — `[。！？]+ | [.!?](?=\s|$) | \n+`. CJK terminators stand alone; Latin period/!/? require trailing whitespace so decimals (`3.14`), URLs, and most abbreviations are not split.
- **Auto-sized targets** — `is_mostly_cjk(text, threshold=0.30)` picks `CJK_TARGET_CHARS = 400` vs `LATIN_TARGET_CHARS = 800`. CJK characters carry ~2× the information density per char of English so chunk-char budgets differ accordingly.
- **Cross-section packing** — `chunk_sections` fills each chunk up to the target with sentences drawn **across consecutive extractor sections**, not resetting at every section boundary. This is what keeps formats apart-equal: the PDF extractor emits many small `page N paragraph K` blocks, and per-section chunking used to leave each short paragraph as its own tiny fragment (e.g. a 36 KB PDF → 559 chunks, median ~53 chars), while single-section TXT/MD filled to target (~106 chunks, median ~380). Packing across sections makes both produce comparable, well-sized chunks. Each sentence keeps its originating `location`; a chunk that merged several sections is labelled as a first-to-last span (`page 1 paragraph 1 – page 2 paragraph 3`) via `_span_label`.
- **Section-kind boundaries** — packing flushes when the extractor location changes kind (body/table/header/footer/footnote/text-box/transcript/slide-notes), so tables, DOCX meta-sections, and PPTX speaker notes do not get glued into body chunks.
- **Pre-chunked formats bypass this entirely** — spreadsheets (`A6c`) set `ExtractionResult.pre_chunked=True` and their sections are stored as-is. A sheet row is already the semantic unit, and sentence-packing across rows would glue unrelated records together and destroy the `sheet "FAQ" row 12` citation label. Their sizing comes from `[spreadsheet].embed_token_budget` instead of `[chunking]`. See [SPREADSHEET_INGESTION.md](SPREADSHEET_INGESTION.md).
- **Sentence-level overlap** — `DEFAULT_OVERLAP_SENTENCES = 1`. Carry the last *sentence*, not the last *N characters*, so chunk boundaries never split a grammatical clause. If carrying overlap would make the next chunk exceed the target, the overlap is dropped for that boundary.
- **Fallbacks for long sentences** — soft punctuation (`[，、；,;]`) first, then a hard char cut as a last resort to keep every output chunk `<= target_chars`.

Known limitation: "Mr. Smith" splits at "Mr." — acceptable for a POC.

> **Re-index after chunking changes.** Chunk granularity is fixed at ingest. Sources indexed before a chunking change keep their old chunks until re-indexed — use `/admin/index` Rebuild (or per-source reindex) to apply it.

### 2. Query rewriting

[`rewrite_search_queries`](../app/llm.py) sends the question + last 6 history turns to the chat model with `QUERY_REWRITE_PROMPT`; the model returns 1–4 retrieval-focused rewrites as a JSON array. Cleaned and deduped via `unique_nonempty`, then prepended with the original question and capped at 5 by default (E2's `[domain_policy].max_rewrite_queries` controls the bounded hint-enabled path).

Skipped (`return [question]`) when no chat model is configured, or on any parse/HTTP failure — degrades to single-query retrieval rather than failing the request. API keys are optional; a local OpenAI-compatible chat service can run this stage with a blank key.

When E2 hints are enabled for a notebook, matching `term`/`synonyms` are used
at query time for bounded deterministic expansion. `definition` supplies only
rewrite/disambiguation context and `query_expansions` supplies rewrite
candidates; none is inserted into source excerpts, citations, scores, or the
vector index. Hints never require re-indexing or an extra LLM call. The
`[domain_policy]` limits are 50 hints, 8 synonyms, 4 expansions per hint, at
most 8 matched hints / 600 estimated hint tokens, and at most 5 final rewrite
queries. Invalid or over-budget data falls back to the baseline query pipeline.

### 3a. Vector search

[`query`](../app/vector_store.py). All rewritten queries go through one [`embed_texts`](../app/llm.py) invocation, which splits requests into bounded HTTP batches (default size 64), and Chroma is queried with the full list of vectors. Chroma collection uses cosine space (`metadata={"hnsw:space": "cosine"}`). Per-chunk `vector_score = max(0, 1 - distance)`; for chunks that match multiple queries we keep the best score.

Filter is always `{user_id}` (multi-tenant isolation) and adds `{source_id: {$in: [...]}}` when the user picked specific sources in the chat form. `n_results` follows the active `candidate_pool_size` (default 20).

No-embedding-fallback policy: `embed_texts` raises when the embedding model isn't configured (previously fell back to a SHA-256 hash bag-of-tokens vector — removed because the resulting vectors are dim-incompatible with any real model and silent fallback masked misconfiguration as poor retrieval). The upload route refuses ingestion when the embedding model is not ready ([`llm_settings_status`](../app/main.py)), and `/settings` save probes the embedding endpoint to validate connectivity + dimension consistency against the existing Chroma index. A blank API key remains valid for local services.

> **Dimension changes are a migration, not a Clear (O0).** Deleting every vector
> does not reset a Chroma collection's locked width, so Clear can leave an empty
> index that still rejects a new dimension. Use **Migrate embedding dimension**
> on `/admin/index`: it replaces the collection, keeps vectors already at the
> target width, moves the rest to `stale_embedding` for Reindex, and pauses the
> ingest queue for the duration. Startup sync also refuses to upsert chunks
> whose width disagrees with the collection, so a stray old-dimension row can no
> longer re-lock it. See [`DEVELOPMENT.md`](DEVELOPMENT.md#changing-the-embedding-dimension).

Model-specific prefixes: `embed_texts(..., role="query"|"passage")` prepends an optional, settings-driven prefix (`/settings` → *Embedding query/passage prefix*). Retrieve embeds queries with `role="query"`, ingest embeds chunks with `role="passage"`. The e5 family needs `query: ` / `passage: `; OpenAI and others leave them blank (default), so the prefix is opt-in and only changes the text sent to the endpoint, never the stored chunk. **Changing a prefix changes the vectors → re-index** (`/admin/index` Rebuild).

### 3b. Keyword search

[`keyword_candidates_from_sqlite`](../app/retrieval.py). Tokenises every rewritten query via `search_tokens`:

- Latin: `re.findall(r"[\w.-]+", text.lower())` minus a small EN+ZH stopword set; tokens shorter than 2 chars dropped.
- CJK: 2-grams plus 3-grams over the `[一-鿿]` characters of the text (`cjk_ngrams`).

Unique tokens are capped at 12, fed into a single `WHERE chunks.text LIKE ? OR ...` query (with `LIMIT limit*4`), then re-ranked locally by [`keyword_score`](../app/retrieval.py) — token overlap fraction with a `+0.15` phrase bonus when the full query string appears verbatim. Returns the active candidate-pool limit (default 20).

This is the part most worth replacing with **SQLite FTS5 + BM25** when corpus size grows — see *Open follow-ups* below.

### 4. Hybrid merge

[`merge_candidates`](../app/retrieval.py). Vector + keyword candidate lists are deduped by `chunk_id`, scored as

```
score = 0.7 · max(0, vector_score) + 0.3 · keyword_score
```

For chunks that show up in both lists we recompute against both feature scores and keep the higher final number. Anything with `score <= 0` is dropped. Top-20 by `score` is passed on.

Before rerank, the sorted hybrid candidates are diversified: a lower-ranked chunk is skipped when its token-set Jaccard overlap with an already-kept chunk is very high. This preserves the best-scoring representative while preventing sentence-overlap neighbours from occupying multiple rerank slots.

The 0.7/0.3 weighting is empirical (recall@5 = 100%, MRR ≈ 0.88 against the demo notebook before rerank). For RRF-style merging see *Open follow-ups*.

### 5. LLM rerank

[`rerank_chunks`](../app/llm.py). Up to `candidate_pool_size` candidates (default 20) plus the question go to the chat model with `RERANK_PROMPT`; the model returns `[{"id": 1, "score": 0.92}, ...]`. Each scored candidate's final score is

```
combined = 0.8 · rerank_score + 0.2 · hybrid_score
```

The active `final_chunk_count` (default 6) by `combined` is returned. Graceful fallbacks:

- No chat model → return hybrid top-6 directly (a blank API key is valid for local services).
- HTTP / parse / empty-scores failure → return hybrid top-6 directly.

Full chunk text is sent (no `text[:900]` truncation) because chunks are already bounded by `chunk_text()` and tail-of-chunk truncation previously dropped answer evidence. Cost stays controlled by the configured chunk-size targets (defaults: ~400 CJK / ~800 Latin chars).

### 6. Low-confidence abstain

[`ask`](../app/main.py) reads the active low-confidence threshold live through `active_low_confidence_threshold()` ([app/retrieval.py](../app/retrieval.py)); the config default is `0.25`, while an applied retrieval profile can override it. When `not retrieved` or `top.score < threshold` we skip `generate_answer` entirely and return the localized app-side abstention. This avoids paying for a generation call that would either hallucinate or echo the same refusal back; generation-stage abstention uses the structural marker described below.

`metadata.outcome` is set to `low_confidence` / `no_retrieval` / `abstained` / `answered` / `error` so the per-message debug pane can render the reason.

### Topic-focused source comparison (U17)

`POST /notebooks/{id}/compare` has two evidence paths. With a blank topic it
preserves the original behavior and compares each selected source's stored
summary (or the existing first-chunk excerpt fallback). It accepts 2–10 sources
in one comparison call; eleven or more are rejected before
generation. This count limit is a product guardrail, not a token/context-window
guarantee. With a topic it runs this same `retrieve()` pipeline **sequentially
once per authorised source**, passing `source_ids=[source_id]`, then compares
the returned chunks. Query rewrite, embedding, hybrid retrieval, active profile,
and rerank behavior are therefore shared with chat rather than reimplemented.

Topic mode accepts two or three sources. Four or more are rejected before any
retrieval call so one request cannot create unbounded LLM/embedding fan-out.
The active low-confidence threshold is applied per source; no results or a top
score below the threshold become the metadata marker
`[NO_RELEVANT_TOPIC_EVIDENCE]`. The compare prompt must state that no sufficient
topic evidence was retrieved for that source and must not infer its position.
The marker is not source evidence and is never stored as a chunk.

Every successful result starts with an application-generated source legend,
using the same `comparison_source_items()` numbering as the real model prompt.
Each `[n]` maps to a filename and its input evidence status: summary/excerpt,
the count of non-empty topic excerpts actually supplied after the source-level
confidence gate, or insufficient topic evidence. This is input coverage, not a
guarantee that the generated report discusses every source. The legend is part
of the comparison Markdown, so manual save-to-notes and note exports retain it.
Its UI copy uses the i18n catalog; filenames are escaped as literal Markdown.

Both modes use neutral sections: **共同點 / 差異 / 待釐清** (or equivalents in
the evidence language), omitting empty sections. Differences are organised by
comparison dimension, with each source's explicit terms side by side. Factual
comparisons identify source IDs and filenames on every bullet and cite excerpt
locations when available. Shared points must list their supporting sources;
points supported by only a subset must be labelled partial commonalities, not
implied to apply to all selected documents. Bare phrases like "both reports"
are insufficient. A shared point requires at least two supporting documents;
similarities between groups/products/treatments inside a single document are
not cross-document commonalities. Single-source points belong under Differences.
Sources lacking topic evidence cannot support shared claims.
These source IDs are not chunk numbers or the documents' internal references.
Clarifications state the claims, the unresolved condition, and what needs confirmation; they
are not definitive contradiction verdicts. A shared topic does not establish a
shared project or subject. Different projects or versions may legitimately have
different terms; filenames/version numbers alone do not establish authority or
supersession. Only evidence of incompatible claims about the same subject,
scope, conditions, and effective period, with no known revision/supersession
explanation, warrants flagging a possible inconsistency. Unknown relationships
remain explicit uncertainties, not inferred conflicts. Missing evidence does
not prove a document lacks a provision, and bounded summaries/retrieved excerpts
cannot establish an exhaustive full-document comparison. These are prompt-level
rules, not deterministic verification of document relationships or model output.
Existing saved comparison notes are unchanged; regenerate to use the new prompt.

### 7. Answer generation

[`generate_answer`](../app/llm.py) with `SYSTEM_PROMPT`:

- "Answer only from the provided source excerpts." (grounding)
- "Reply in the same language as the user's question (Traditional Chinese question → Traditional Chinese answer)." (stops the CJK-question / EN-answer regression)
- If the excerpts do not contain enough information, emit the provider/app
  protocol marker `[[RAG_ABSTAIN]]`. The app buffers the provider completion up
  to 100,000 characters, then parses the full marker or any truncated reserved
  prefix before SSE, database persistence, HTML, citations, or exports. It
  renders localized `chat.abstain` copy and records
  `answer_outcome=abstained`; neither earlier text nor the marker may leave the
  protocol boundary.
- "Keep the answer concise and include bracket citations like [1], [2] for the excerpts you used."

An enabled notebook answer policy and matched `answer_note` are untrusted,
non-evidence instructions. Precedence is immutable grounding, security, and
citation rules > the app's spreadsheet aggregation guard > notebook answer
policy > matched answer notes > user formatting preferences. Policy and notes
cannot remove citations, turn unsupported content into evidence, or weaken the
spreadsheet guard. Answer notes are included only for matched hints and remain
lower priority than the policy. The system role contains only immutable
application instructions and the spreadsheet guard; notebook guidance is
serialized as bounded JSON in the user-role prompt.

User prompt is `"Source excerpts:\n{numbered chunks}"`, followed when needed by
an explicitly labelled untrusted JSON object containing `answer_policy` and
`matched_answer_notes`, then `"Question: {question}"`.

Eval runs store an immutable domain snapshot. A snapshot is used only after
raw-size, exact schema/prompt-version, opaque-token, per-field, list-count, token
budget, and static hard-ceiling validation succeeds as one unit. The frozen
limits, rather than current deployment defaults, control that run's matching,
rewrite, and answer-guidance budgets. Invalid or pre-E2 snapshots fail closed to
the baseline and never fall back to the notebook's live configuration.

### 8. Citation filtering & UI

[`ask`](../app/main.py) parses `[N]` markers out of the answer with `re.finditer(r"\[(\d+)\]", answer)` and only persists citations the model actually referenced — same behaviour as NotebookLM. Falls back to all retrieved chunks if the answer contains no markers (defensive: lets the user still see what was retrieved).

[`citation_payload`](../app/retrieval.py) serialises each chunk with `score / vector_score / keyword_score / rerank_score` so the per-message debug pane (`📊 N chunks · retrieved Xms · generated Yms · top score Z`) can show the table.

Per-message `messages.metadata_json` row carries `{retrieval_ms, generation_ms, retrieved_chunks, top_score, outcome, threshold?, answer_chars?, error?}` for the debug pane. Legacy messages stored before the column existed default to `'{}'` and render with whatever data is available.

## Evaluation harness

[`tests/eval_retrieval.py`](../tests/eval_retrieval.py) + [`tests/eval_questions.json`](../tests/eval_questions.json) (25 ground-truth Qs against the demo notebook). Run:

```bash
.venv/bin/python -m tests.eval_retrieval                # default: top-k=5, rerank on
.venv/bin/python -m tests.eval_retrieval --no-rerank    # hybrid-only baseline (strips chat_model)
.venv/bin/python -m tests.eval_retrieval --top-k 10
```

Reports per-question hit rank, **Recall@k**, **MRR**. It skips when no embedding model is configured. API keys are optional, and a chat model is optional: without chat, rewrite/rerank use their production fallbacks; `--no-rerank` forces that path.

For customer/private data that should not leave the deployment, admins can use the in-app workbench at `/admin/evals`. It stores eval sets in SQLite, always runs retrieval metrics against already-indexed notebook sources, and can optionally generate/judge answers when `judge_enabled` is selected. Each run freezes its retrieval profile and E2 domain snapshot, with independent hints/policy flags; policy mode requires judging. The UI polls progress with HTMX and persists aggregate retrieval/judge metrics, compact retrieved snippets, latency, answer outcomes, and per-question status. Admins can add questions manually or generate draft candidates from indexed chunks/LLM-assisted authoring; candidates remain drafts until approved. Result rows show expected evidence, top retrieval, miss diagnosis, and—on judged runs—answer/citation review signals. This is the path for building the representative customer-approved set called out in `QUALITY.md` Q1-3; generated drafts are not a substitute for that evidence. The file-based harness remains the lightweight demo/regression harness.

Exports are intentionally split by data sensitivity. Sanitized profile/run exports are JSON downloads meant for the implementation team and omit questions, expected evidence, retrieved snippets, source/domain text, answers, and judge rationale; E2 state is represented only by flags/counts/revision/fingerprint summaries. Full internal reports are CSRF-protected confirmed POST downloads and can include questions, evidence, diagnostics, retrieved snippets, frozen domain snapshot, generated answers, and judge rationale. They are recorded as high-sensitivity `audit_events`; the audit row stores identifiers and content-free summaries only.

### Hit semantics — why ANY-of, not ALL-of

A chunk "hits" iff its `filename == expected_filename` **and** at least one substring from `expected_substrings` appears in `chunk.text`. The any-of rule keeps the metric chunk-size-agnostic: when CJK chunks shrunk from 1200 → 400 chars some `expected_substrings` ended up split across two chunks, and an all-of rule would have falsely penalised retrievals that were in fact correct.

The admin workbench uses the same ANY-of substring idea, but stores DB-native expected evidence instead of demo filenames: an item can require an expected source, expected chunk, one or more substrings, or a combination. Items with no scoring criteria are recorded as `unscored`, so admins can draft questions without polluting Recall/MRR.

Trade-off: a chunk that contains *only one* expected substring can match even if the user really wanted all the supporting context. Ground-truth substrings are chosen to be specific enough that the false-positive rate stays low; check with the diagnostic in the *Maintaining the eval* section below before adding new questions.

### Historical demo baseline (2026-06-25)

Against `tests/eval_questions.json` (25 questions, demo notebook, after CJK-aware chunking):

| Configuration | Recall@5 | MRR  |
|---|---:|---:|
| Hybrid only (no rerank) | 100 % | 0.883 |
| Hybrid + rerank         | 100 % | 0.933 |

This is historical demo evidence, not a current customer baseline. Recall@5 saturated at 100 %, so the next retrieval changes need a **harder, customer-approved** eval set (more disambiguation, more needle-in-haystack questions) before they can show measurable lift. Rerun the harness before using these numbers as release evidence.

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

Most of these are now **centralized in [`app/config.py`](../app/config.py)** and overridable at runtime without code edits — defaults ← `config.toml` ← `NOTEBOOKLM_<GROUP>_<FIELD>` env (see [`DEVELOPMENT.md`](DEVELOPMENT.md)). The mapping: hybrid weights → `[retrieval] vector_weight/keyword_weight`; rerank weights → `rerank_weight/rerank_base_weight`; vector/keyword/rerank candidate counts → `candidate_pool_size`; rerank limit → `final_chunk_count`; abstain → `low_confidence_threshold`; chunking → `[chunking] *`; embedding batch → `[embedding] batch_size`. The module constants below still exist (call sites read them) but their values come from config. `is_mostly_cjk` threshold and the rewrite-history count remain plain constants.

> **Runtime override (E1c eval workbench).** The seven runtime-safe retrieval knobs (`vector_weight`, `keyword_weight`, `candidate_pool_size`, `final_chunk_count`, `rerank_weight`, `rerank_base_weight`, `low_confidence_threshold`) are read at request time from `ACTIVE_RETRIEVAL_PARAMS` in `app/retrieval.py`, not the import-time module constants. The config/env values still define the **defaults**; an admin can override them live by **applying a retrieval profile** at `/admin/evals` (persisted via `retrieval_profiles.is_active`, reloaded on startup). `retrieve()` / `merge_candidates()` / `rerank_chunks()` also accept a per-call `params` override, which the eval runner uses to test a candidate profile in isolation without changing live chat retrieval. Index-affecting parameters (chunking, embedding model/prefix/dimension) are **not** part of this override path and require Reindex. For a dimension change, use the `/admin/index` migration flow above rather than Clear/Rebuild.

| Knob | Default | Location | What it controls |
|---|---:|---|---|
| `LATIN_TARGET_CHARS` | 800 | [app/ingest.py](../app/ingest.py) | Max chars per Latin-dominant chunk |
| `CJK_TARGET_CHARS` | 400 | [app/ingest.py](../app/ingest.py) | Max chars per CJK-dominant chunk |
| `DEFAULT_OVERLAP_SENTENCES` | 1 | [app/ingest.py](../app/ingest.py) | Sentences carried into the next chunk |
| `is_mostly_cjk` threshold | 0.30 | [app/ingest.py](../app/ingest.py) | CJK char ratio that flips to CJK chunk size |
| Vector `n_results` | 20 | [app/retrieval.py](../app/retrieval.py) | Per-query vector candidates pulled from Chroma |
| Keyword `limit` | 20 | [app/retrieval.py](../app/retrieval.py) | Top-N kept after SQLite LIKE search |
| Hybrid weights | 0.7 / 0.3 | [app/retrieval.py](../app/retrieval.py) | `vector` / `keyword` blend in `merge_candidates` |
| Rerank candidates | 20 | [app/llm.py](../app/llm.py) | How many candidates the LLM reranker sees |
| Rerank weights | 0.8 / 0.2 | [app/llm.py](../app/llm.py) | `rerank` / `hybrid` blend after LLM rerank |
| Rerank `limit` | 6 | [app/llm.py](../app/llm.py) | Chunks returned from `rerank_chunks` |
| `LOW_CONFIDENCE_THRESHOLD` | 0.25 | [app/retrieval.py](../app/retrieval.py) | Top-score under which `ask()` abstains |
| History turns for rewrite | 6 | [app/llm.py](../app/llm.py) | Trailing history passed to query rewriter |
| Embedding batch size | 64 | [app/llm.py](../app/llm.py) | Per-HTTP batch for `embed_texts` |

Change one knob, rerun `python -m tests.eval_retrieval`, compare numbers. For the config-driven knobs you can sweep without editing code, e.g. `NOTEBOOKLM_RETRIEVAL_VECTOR_WEIGHT=0.6 python -m tests.eval_retrieval`.

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

### P1-2 design note — CJK tokenization is the real blocker (not config)

Externalizing tunables (`app/config.py`) does **not** unblock P1-2. FTS5 is implementable today (bundled SQLite 3.53 has FTS5 + the `trigram` tokenizer), but a naive swap would **regress Chinese keyword recall** on the deployment's CJK corpus, because FTS5's only built-in CJK-capable tokenizer (`trigram`) **cannot match queries shorter than 3 characters** — and 2-char terms (端點, 設定, 部署, 報告…) are the backbone of Chinese search. Measured:

| query | chars | trigram FTS5 match |
|---|---|---|
| `端點設` | 3 | ✅ |
| `設定方式` | 4 | ✅ |
| `端點` / `設定` / `部署` | 2 | ❌ 0 hits |

The current `keyword_candidates_from_sqlite` (`LIKE '%token%'` + custom `keyword_score`) matches arbitrary substrings, so it already handles 2-char CJK; FTS5+trigram would lose exactly those.

Segmentation options if/when we do P1-2 (decision deferred until a representative CJK corpus + eval set exist):

| route | method | CJK quality | cost |
|---|---|---|---|
| FTS5 `trigram` | char 3-grams | 2-char queries fail ❌ | lowest (built-in) |
| FTS5 custom **bigram** | char 2-grams | 2-char OK, noisier | low–med |
| **jieba (`cut_for_search`) → space-joined → FTS5** | dictionary + HMM | good, custom terms ✅ | med (one dep, segment at ingest) |
| FTS5 `icu` | dictionary | medium, ambiguity | med (compile ICU into Docker) |
| neural (pkuseg / BERT) | deep model | best | high (model + latency) |

Sweet spot for "single-machine POC + Chinese reports + recall-first" is likely **jieba search-mode → FTS5**, or a **custom bigram** tokenizer; `trigram` is unsuitable (2-char hole) and neural is overkill for search. Either way, adopting **BM25** ranking (vs the current overlap score) also needs the customer eval set to confirm no regression. Net: P1-2 stays parked on *representative CJK data*, not on config.

Other ideas, in rough order of cost-benefit:

- Cache embeddings per (query, model) — repeated questions in the same conversation don't need a fresh embedding call.
- Preserve the current abstain-safe buffering unless a future protocol can classify refusal before emitting text. The route already streams retrieval/generation status, but E2 deliberately emits the final answer only after the bounded provider completion is classified; restoring token-by-token display without solving late-marker leakage would be a security regression.
- Tighten the eval set with disambiguation questions across similar files (the current set is saturated at 100 % Recall@5).
- Add a per-source "score cap" so a single dominant source can't crowd out cross-document evidence.
- Backfill `messages.citations_json.source_id` for legacy assistant messages if older local databases need richer citation metadata.

## Pointers

- Retrieval engine: [`retrieve`](../app/retrieval.py); answer orchestration: [`ask`](../app/main.py)
- Domain-hint matching and frozen snapshots: [`app/domain_policy.py`](../app/domain_policy.py)
- LLM helpers: [`app/llm.py`](../app/llm.py) (rewrite / embed / rerank / generate)
- Chunker: [`app/ingest.py`](../app/ingest.py)
- Vector store: [`app/vector_store.py`](../app/vector_store.py)
- Eval: [`tests/eval_retrieval.py`](../tests/eval_retrieval.py), [`tests/eval_questions.json`](../tests/eval_questions.json)
- Unit tests: [`tests/test_chunking.py`](../tests/test_chunking.py) (chunker), [`tests/test_core.py`](../tests/test_core.py) (retrieve end-to-end against fixtures)
