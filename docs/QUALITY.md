# Retrieval / answer-quality backlog

Known **retrieval- and answer-quality** issues, kept separate from [`PERFORMANCE.md`](PERFORMANCE.md) (pure speed/scale). Each item lists **Issue → Impact → Fix**, a **priority**, and a **status** box. Read [`RETRIEVAL.md`](RETRIEVAL.md) first — it documents the pipeline and the tuning knobs referenced here.

**Deployment context:** serving the customer's **borrowed, fixed** Gemma 4 31B (chat) + multilingual-e5-large (embedding, 1024-dim). Switching off the old OpenAI models (1536-dim) invalidates several empirically-tuned knobs, and all adaptation must be **app-side** (we can't change their serving). See `../handover.md`.

Status legend: `[ ]` todo · `[~]` in progress · `[x]` done

---

## P0 — model-switch correctness (do before / at go-live; app-side only)

### [x] Q0-1 · e5 `query:` / `passage:` prefix
- **Issue:** `embed_texts` (`../app/llm.py`) sends raw text; multilingual-e5 expects `"passage: "` on indexed chunks and `"query: "` on search queries.
- **Impact:** Noticeably worse retrieval — the model was trained expecting those prefixes. **Highest-value quality item; only fixable in our code.**
- **Fix:** **Done.** `embed_texts(..., role="query"|"passage")` prepends a **settings-driven** prefix (`/settings` → *Embedding query/passage prefix*), default empty so OpenAI and other models are unaffected (the app stays embedding-model-agnostic). Ingest embeds with `role="passage"`, retrieve with `role="query"`; the prefix only changes the text sent to the API, never the stored chunk. For e5, set the prefixes to `query: ` / `passage: ` and re-index.

### [ ] Q0-2 · Re-tune the low-confidence abstain threshold
- **Issue:** `LOW_CONFIDENCE_THRESHOLD = 0.25` (`../app/main.py`) was tuned against OpenAI-1536; e5-1024 cosine scores have a different distribution.
- **Impact:** Over-abstaining ("I cannot determine that…") or under-abstaining (hallucinating) on the new model.
- **Fix:** Re-measure the score distribution on a representative set (needs Q1-3) and re-set the threshold.

### [ ] Q0-3 · Validate Gemma JSON for query-rewrite + rerank
- **Issue:** Rewrite (`parse_json_strings`) and rerank (`parse_rerank_scores`) need clean JSON; Gemma's formatting / optional `<|think|>` reasoning may differ from GPT.
- **Impact:** A parse failure **silently** falls back to single-query retrieval / hybrid order — degraded quality, invisibly.
- **Fix:** Test against real Gemma output; tighten prompts/parsing if needed; log the fallbacks more loudly so they're noticed.

### [ ] Q0-4 · Validate `[N]` citation formatting on Gemma
- **Issue:** Only chunks the answer cites with `[N]` are shown (citation filtering); this depends on Gemma following `SYSTEM_PROMPT`.
- **Impact:** A well-grounded answer can render with missing or wrong citations.
- **Fix:** Verify on Gemma; adjust the prompt; consider a fallback that shows the top chunks when no `[N]` is parsed.

### [ ] Q0-5 · Check chunk size vs e5-large 512-token limit
- **Issue:** A 400-char CJK chunk + a `"passage: "` prefix may approach/exceed e5's 512-token input and get **silently truncated**.
- **Impact:** Chunk tails dropped from the embedding → lost recall on long CJK / table chunks.
- **Fix:** Measure token counts for CJK / Latin / table chunks against the e5 tokenizer; lower `CJK_TARGET_CHARS` if needed.

---

## P1 — retrieval-quality improvements

### [ ] Q1-1 · Hybrid merge → Reciprocal Rank Fusion
- **Issue:** Fixed `0.7·vector + 0.3·keyword` linear blend (`merge_candidates`).
- **Impact:** Crude across differing score scales; RRF is more robust (and reduces the need for Q0-2-style weight tuning).
- **Fix:** Replace the linear blend with RRF. Documented in `RETRIEVAL.md` open follow-ups.

### [ ] Q1-2 · Keyword search → SQLite FTS5 + BM25
- **Issue:** `LIKE '%token%'` + a token-overlap heuristic has no real relevance ranking and matches substrings noisily.
- **Impact:** Weak keyword arm of the hybrid; BM25 ranks far better. (Same change as `PERFORMANCE.md` P1-2 — also a speed win.)
- **Fix:** FTS5 virtual table + BM25.

### [ ] Q1-3 · A harder, representative eval set
- **Issue:** The eval is saturated (Recall@5 = 100%, MRR 0.933) and built on the **demo notebook**, not the customer's hundreds-of-page research reports.
- **Impact:** Can't measure any of the above — tuning is blind.
- **Fix:** Build an eval set from representative customer-style documents with harder questions. **Prerequisite for tuning Q0-2 / Q1-1 / Q1-2.**

---

## P2 — minor / known nuances

### [ ] Q2-1 · Cross-section chunking blends DOCX meta-sections / tables
- **Issue:** `chunk_sections` packs across all extractor sections, so DOCX header/footer/footnotes and PDF table blocks can merge into adjacent body chunks (noted in the chunking PR review).
- **Impact:** Minor citation-precision noise (`document – footnotes`-style span labels).
- **Fix:** Flush the packing buffer when the section "kind" changes.

### [ ] Q2-2 · Near-duplicate chunks from sentence overlap
- **Issue:** `DEFAULT_OVERLAP_SENTENCES = 1` means adjacent chunks share a boundary sentence.
- **Impact:** Near-duplicate chunks can occupy multiple top-k slots, slightly reducing context diversity.
- **Fix:** De-duplicate highly-overlapping chunks during merge, or drop overlap once recall is otherwise solid.
