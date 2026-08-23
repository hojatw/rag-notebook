# Retrieval / answer-quality backlog

Known **retrieval- and answer-quality** issues, kept separate from [`PERFORMANCE.md`](PERFORMANCE.md) (pure speed/scale). Each item lists **Issue → Impact → Fix**, a **priority**, and a **status** box. Read [`RETRIEVAL.md`](RETRIEVAL.md) first — it documents the pipeline and the tuning knobs referenced here.

**Deployment context:** the serving side is **borrowed and fixed** — Gemma 4 31B (chat) + multilingual-e5-large (embedding, 1024-dim). Moving off the earlier 1536-dim OpenAI models invalidates several empirically-tuned knobs, and every adaptation has to be **app-side** because the serving cannot be changed. The facts these items lean on, and why they lead to the trade-offs recorded here, are in [`DEPLOYMENT_CONTEXT.md`](DEPLOYMENT_CONTEXT.md).

Status legend: `[ ]` todo · `[~]` in progress · `[x]` done

---

## P0 — model-switch correctness (do before / at go-live; app-side only)

### [x] Q0-1 · e5 `query:` / `passage:` prefix
- **Issue:** `embed_texts` (`../app/llm.py`) sends raw text; multilingual-e5 expects `"passage: "` on indexed chunks and `"query: "` on search queries.
- **Impact:** Noticeably worse retrieval — the model was trained expecting those prefixes. **Highest-value quality item; only fixable in our code.**
- **Fix:** **Done.** `embed_texts(..., role="query"|"passage")` prepends a **settings-driven** prefix (`/settings` → *Embedding query/passage prefix*), default empty so OpenAI and other models are unaffected (the app stays embedding-model-agnostic). Ingest embeds with `role="passage"`, retrieve with `role="query"`; the prefix only changes the text sent to the API, never the stored chunk. For e5, set the prefixes to `query: ` / `passage: ` and re-index.

### [ ] Q0-2 · Re-tune the low-confidence abstain threshold
- **Issue:** `LOW_CONFIDENCE_THRESHOLD = 0.25` (`../app/retrieval.py`) was tuned against OpenAI-1536; e5-1024 cosine scores have a different distribution.
- **Impact:** Over-abstaining ("I cannot determine that…") or under-abstaining (hallucinating) on the new model.
- **Fix:** Re-measure the score distribution on a representative set (needs Q1-3) and re-set the threshold.
- **⚠️ Measured limit of threshold tuning (E1e-2 judged run, 2026-07-25, threshold 0.3):** the score gate **cannot** be tuned to catch every unanswerable question, because retrieval score measures *topical similarity*, not *answerability*. On a 22-item judged set, two unanswerable questions that were fully on-topic but whose specific fact was absent ("平台部署的伺服器硬體規格", "使用者教育訓練的排程") scored **0.845 / 0.858** — inside the answerable items' range (**0.839–0.991**, avg 0.952). The two distributions overlap, so no threshold separates them; raising it to catch these would falsely refuse real questions. Off-topic unanswerables behaved as expected (0.151 / 0.177 → correctly gated).
- **Consequence:** the score gate is only the *first* of two refusal layers; the second is the model declining per `SYSTEM_PROMPT`, which caught exactly the two the gate missed, with **zero** false refusals on 18 answerable items. Treat threshold tuning as a way to reduce cost/latency on clearly off-topic queries, **not** as the primary hallucination defense. `E1e-2` measures both layers (see `answer_is_refusal` in `../app/evals.py`).

### [ ] Q0-3 · Validate Gemma JSON for query-rewrite + rerank
- **Issue:** Rewrite (`parse_json_strings`) and rerank (`parse_rerank_scores`) need clean JSON; Gemma's formatting / optional `<|think|>` reasoning may differ from GPT.
- **Impact:** A parse failure **silently** falls back to single-query retrieval / hybrid order — degraded quality, invisibly.
- **Fix:** Test against real Gemma output; tighten prompts/parsing if needed; log the fallbacks more loudly so they're noticed.

### [ ] Q0-4 · Validate `[N]` citation formatting on Gemma
- **Issue:** Only chunks the answer cites with `[N]` are shown (citation filtering); this depends on Gemma following `SYSTEM_PROMPT`.
- **Impact:** A well-grounded answer can render with missing or wrong citations.
- **Fix:** Verify on Gemma; adjust the prompt; consider a fallback that shows the top chunks when no `[N]` is parsed.

### [~] Q0-5 · Check chunk size vs e5-large 512-token limit
- **Issue:** A 400-char CJK chunk + a `"passage: "` prefix may approach/exceed e5's 512-token input and get **silently truncated**.
- **Impact:** Chunk tails dropped from the embedding → lost recall on long CJK / table chunks.
- **Fix:** **Tooling landed; measurement pending in the target data/runtime.** Run `python -m tests.inspect_e5_chunk_tokens` against indexed deployment data with the `intfloat/multilingual-e5-large` tokenizer cached/available. The script prepends the passage prefix, counts tokens including special tokens, reports all/CJK/Latin/mixed/table p50/p95/p99/max, and prints over-512 examples. If real customer chunks exceed 512, lower `CJK_TARGET_CHARS` / tune table chunking and re-index; otherwise tick this off.
- **Current local measurement (2026-06-19):** 6,406 indexed chunks scanned; p95 = 285 tokens, p99 = 359, max = 874; 10 chunks (0.2%) exceed 512, all classified CJK. The over-limit cases are concentrated in long extracted pages / dense medical-label style text, not ordinary Latin chunks. Keep this open until the target customer corpus is measured and a chunking/table strategy is chosen for over-limit CJK chunks.
- **Now observable in production (A6a, 2026-07-25):** each source's ingestion diagnostics carry a `chunk_over_token_budget` warning counting chunks whose *estimated* token length exceeds `[diagnostics].embedding_token_budget` (CJK ~1 token/char, Latin ~4 chars/token — `estimate_embedding_tokens` in `app/ingest.py`). That makes the over-limit case visible per source on the customer corpus without shipping the tokenizer, so this item no longer depends on someone remembering to run the script. The script stays the ground truth — the warning is an estimate and can over-report.
- **Local guard added:** `chunk_sections` now drops sentence overlap when carrying it would make the next chunk exceed the configured char target. This fixes the observed dense-CJK boundary case where two ~400-char sentences could combine into one ~800-char chunk. Re-chunking the 10 local over-limit examples with the new guard produced max 320 e5 tokens. Existing indexed sources need reindexing to benefit.

### [x] Q0-6 · Starter questions ignored the source language
- **Issue:** `STARTER_QUESTIONS_PROMPT` had only a weak one-line language rule with a single CJK example, so the chat model emitted **Chinese** starter questions for **English-only** sources (observed on a real notebook).
- **Impact:** A Chinese starter question against an English-only notebook then forced cross-lingual retrieval (see Q1-4) → thin answers/citations, confusingly worse than the same question once a same-language source existed.
- **Fix:** **Done.** Strengthened the prompt to the same explicit per-language `LANGUAGE RULE` + "Do NOT translate" block used by summary/briefing. Regression-guarded by `tests/test_llm.py::test_generation_prompts_carry_strong_language_rule`.

---

## P1 — retrieval-quality improvements

### [ ] Q1-1 · Hybrid merge → Reciprocal Rank Fusion
- **Issue:** Fixed `0.7·vector + 0.3·keyword` linear blend (`merge_candidates`).
- **Impact:** Crude across differing score scales; RRF is more robust (and reduces the need for Q0-2-style weight tuning).
- **Fix:** Replace the linear blend with RRF. Documented in `RETRIEVAL.md` open follow-ups.
- **Deferred (2026-07-25) — not a rejection, a sequencing decision.** The 20-line change in `merge_candidates` is the small part; the cost is that the resulting `score` **changes scale** and three downstream consumers are calibrated on the current one:
  1. the abstain gate — `low_confidence_threshold` (0.25) is compared directly against `top_score` in `app/main.py` (`ask` + the streaming path); RRF scores start around `1/(60+1)`, so the current threshold would abstain on everything;
  2. the rerank blend — `combined = weight·rerank_score + base_weight·chunk["score"]` in `app/llm.py` mixes a 0–1 judge score with the hybrid score;
  3. Retrieval Profiles — `vector_weight` / `keyword_weight` are stored, editable profile fields (`app/evals.py`, `app/retrieval.py`, `app/config.py`), so RRF changes their meaning and needs a migration story for existing profiles, plus updates to `tests/test_config.py` / `tests/test_ui.py`.
- **Restart condition (updated 2026-08-24):** `A6b` Phase 1, `A6c`, and the E1e-2 judgeability guard have landed. The remaining evidence blocker is `Q1-3`: a customer-approved representative set with reference answers where answer-quality correctness is required. Recalibrating the abstain gate or migrating profile score semantics without that measuring stick is tuning blind.
- **Upgraded from "nice to have" to a prerequisite (2026-08-22):** `Q1-7` adds a **third** retrieval arm whose score scale is not comparable to the other two, and — because a notebook mixes structured and unstructured sources — a weighted sum would silently favour sources that happen to have headings. RRF is the fix for that, so `Q1-7` cannot be turned on until this lands. It also sharpens the "recalibrate once" argument above: if `Q1-7` is coming, design the fusion for **three** arms now rather than doing RRF twice.

### [ ] Q1-2 · Keyword search → SQLite FTS5 + BM25
- **Same item as [`PERFORMANCE.md` P1-2](PERFORMANCE.md)** — full description, the CJK-tokenization blocker, and segmentation options live there (and in `RETRIEVAL.md` → *P1-2 design note*). Tick both together when done.

### [ ] Q1-3 · A harder, representative eval set
- **Issue:** The eval is saturated (Recall@5 = 100%, MRR 0.933) and built on the **demo notebook**, not the customer's hundreds-of-page research reports.
- **Impact:** Can't measure any of the above — tuning is blind.
- **Fix:** Build an eval set from representative customer-style documents with harder questions. **Prerequisite for tuning Q0-2 / Q1-1 / Q1-2 / Q1-4.** If customer data cannot leave the deployment, use the admin-only in-deployment eval workbench tracked in `ROADMAP.md` E1 to create, run, compare, apply, and export eval/profile results without exporting source data by default.

### [ ] Q1-4 · Cross-lingual retrieval (e.g. Chinese question ↔ English sources)
- **Issue:** A query only retrieves cross-language content through the **vector** arm (multilingual embedding); the **keyword** arm (`LIKE` on tokens) is dead across scripts, and cross-lingual cosine scores run lower — so a Chinese question against English-only sources retrieves fewer/weaker chunks (some trimmed by the `0.25` abstain threshold), yielding thin answers. Confirmed on a real notebook: the same Chinese questions answered richly once a same-language source was added. Same drug in EN (FDA label) + zh (仿單) is a ready-made test case.
- **Impact:** Mixed-language notebooks under-serve questions asked in the "other" language — a likely real usage pattern for this deployment.
- **Fix (investigate, needs the Q1-3 eval set):** options to measure — (a) confirm/upgrade the embedding model's CN↔EN strength (prod e5-large > ada-002); (b) translate/expand the query into the corpus languages before retrieval; (c) language-aware abstain threshold or a small cross-lingual score boost; (d) a CJK-capable keyword arm (ties into Q1-2). Measure each against a bilingual eval set before adopting.

### [~] Q1-5 · Notebook-level domain hints and answer policy
- **Original issue:** Before E2, domain-specific terminology, abbreviations, internal product names, aliases, and required answer rules lived only in indexed source text and generic prompts. Query rewrite could miss the document's exact terminology, and final answers could drift from deployment-specific rules such as "prefer label text", "numbers must come from cited evidence", or "abstain when evidence is missing".
- **Impact:** Accuracy can look inconsistent even when the retrieval stack is healthy: synonym/alias questions miss relevant chunks, cross-lingual/domain-keyword questions underperform, and grounded answers may drift in style or evidentiary strictness. Conversely, putting a large unstructured "domain prompt" into every answer risks turning prompt text into an unofficial knowledge source and can increase hallucination/cost.
- **Fix (E2 product capability complete; customer validation pending):** structured notebook-level **domain hints** (terms, synonyms, definitions, query expansions, answer notes) plus a bounded **answer policy** are implemented with owner scoping, query-time-only expansion, independent Eval flags, immutable/canonical run snapshots, and sanitized/full export boundaries. The implementation is covered by the baseline/hints/policy/combined mode matrix, route-level authorization/CSRF/export tests, streaming marker tests, and snapshot hardening tests. Validate with the in-deployment Eval Workbench by comparing the same Eval Set with and without hints; success means synonym/domain questions improve without reducing Recall/MRR, increasing false positives, or leaking sensitive prompt/hint text into sanitized exports or audit metadata.
- **Prerequisite now in place:** the answer-quality measuring stick — **`E1e-2` answer/citation judging** (`ROADMAP.md` E1e-2, plan in [`E1E2_ANSWER_JUDGING_PLAN.md`](archive/E1E2_ANSWER_JUDGING_PLAN.md)) — is implemented. It scores answer quality, groundedness, citation correctness, and abstain correctness per judged run, separately from Recall/MRR, so E2's with/without-hints comparison can be judged on answer quality, not just retrieval hits.
- **Boundary:** Q1-3 remains **PENDING**. The representative customer-style Eval Set still requires customer-approved in-deployment data/workflow; draft generation is a suggestion path only and is not an approved substitute for customer evidence.

### [~] Q1-6 · Answer-quality judging needs *judgeable* eval items
- **Original issue:** Before the 2026-07-25 judgeability guard, `E1e-2` scored every answered item, including items that could not support the scoring. Route A graded `answer_quality` against `expected_answer`; when an item had none, the judge was asked to grade against "(none provided)" and still returned a label. Measured on a 22-item judged run: **10 of 18** answerable items came from the deterministic authoring fallback — a "quote a chunk, then ask 這段內容的重點是什麼？" template carrying **no reference answer**. Aggregate came back `answer_quality 20/20 correct, groundedness 1.0, citation 1.0` — a judge with no discrimination, on a set with no hard cases, judging itself (same model generated and graded).
- **Impact:** A perfect-looking score that means nothing. Worse, it is *confidently* meaningless — exactly the failure mode the eval exists to prevent.
- **Also found (fixed 2026-07-25):** the `substring_hit_rate` anchor was withdrawn. It measured how many `expected_substrings` appeared verbatim in the *generated answer*, but that field anchors **retrieval** evidence — `eval_item_hit_rank` matches it against chunk text, and items are populated with things like document headers. Summarising answers never repeat them, so it read ~0.28 while every answer was fine, and it also fed document headers into the judge prompt as "expected evidence". A genuine answer-side anchor needs its own field (`expected_answer_substrings`), not this one.
- **Fix:**
  1. **Done (2026-07-25).** Judgeability is explicit: with no `expected_answer` the judge's label is discarded and recorded as `not_applicable`, and `answer_quality.correct_rate` is a rate over `answer_quality.scored` (items that had a reference) with the rest counted in `answer_quality.not_applicable`. **`groundedness` and `citation_correctness` are unaffected** — they only compare the answer against the retrieved chunks, so they remain valid for every answered item. The per-item judge payload carries `has_reference_answer`.
  2. Treat the deterministic template items as **retrieval probes only**. They are sound for Recall/MRR (they test "can the engine find the passage I quoted?") but ill-posed for answer grading: "這段內容" is undefined unless retrieval returns the exact quoted chunk, so a refusal there can be *correct* behaviour. One such item flipped between answered (run #15) and refused (run #16) purely from LLM-driven rewrite/rerank variance.
  3. Build reference answers for a real answer-quality set — this is **Q1-3**'s job. Until then, read judged aggregates as smoke tests, not quality measurements, and rely on the human spot-check (`E1E2_ANSWER_JUDGING_PLAN.md` §7).

### [ ] Q1-7 · Section summaries as a third retrieval arm
- **Issue:** hybrid retrieval has two arms — vector similarity over chunks, and keyword `LIKE` over chunks. Both operate at **chunk** granularity, so a question whose answer lives in a *section* ("這份報告的研究方法是什麼", "總結第四章") can only be served if some individual chunk happens to look similar to it. Document-level structure is invisible to retrieval.
- **Impact:** section-scoped questions are served by luck. This is the class of question that motivated PageIndex-style reasoning retrieval, and it is a natural fit for hundreds-of-page research reports.
- **Fix:** add a **third arm** that retrieves over *section summaries* rather than chunks, and merge its result into the existing hybrid pool.
  1. At index time, generate one short summary per detected heading node (from `A12`). This is an **increment on an existing, already-accepted cost**: `summarize_source` already runs an LLM summary per source after indexing — this extends "one summary per source" to "one summary per section", not a new class of spend. Gate it (e.g. only sources with ≥N detected sections and ≥M pages) so short documents pay nothing.
  2. Embed `title + summary` per node and store the vector in SQLite alongside the node. **Decided 2026-08-22 — do not put node vectors in the chunk collection**: `query_vectors` would then return summaries competing with chunks and break citation (a citation must point at a chunk). **And do not open a second Chroma collection**: node counts are 2–3 orders of magnitude below chunk counts (a 200-page report yields tens of nodes), so an in-memory cosine pass over the SQLite copy is fast, and it avoids extending `O0`'s dimension-generation/lock machinery to a second collection — which would roughly double that machinery's complexity for no gain. This reuses the pattern `PERFORMANCE.md` P2-1 already settled on: SQLite holds the durable vector, Chroma is the derived index.
  3. At query time: score the query against node vectors → take top-N nodes → add the chunks those nodes cover into the candidate pool with a `tree_score`, merged in `merge_candidates` alongside `vector_score` / `keyword_score`.
  4. Weight it through the existing retrieval-parameter machinery (`ACTIVE_RETRIEVAL_PARAMS` / retrieval profiles), **defaulting to 0 (off)** so the arm ships dark and is turned on only by a profile — which makes it directly A/B-able in the Eval Workbench instead of shipping and then reverting.
- **Mixed-structure notebooks are the hard part, and they make `Q1-1` a prerequisite rather than a sibling.** A notebook will routinely hold both structured research reports and unstructured vendor documents (`A12` records the expected corpus). A source with no detected headings contributes no nodes, so it is excluded from the section arm **automatically** — no query-time flag needed. But that is exactly the problem: structured sources gain a third scoring channel that unstructured sources can never receive, so their chunks are lifted **independently of relevance**, and the bias is invisible to the user. Three options were considered — (a) only run the arm when every selected source is structured (one unstructured file disables the feature: too brittle), (b) run it always and accept the bias (unacceptable: silent and unmeasurable), (c) **fuse by rank instead of by weighted score sum**. (c) wins, and it is already in this backlog as **`Q1-1` (Reciprocal Rank Fusion)** — RRF exists precisely to merge arms whose score scales are not comparable, which a section-summary score and a chunk-similarity score are not. **Treat `Q1-1` as a prerequisite for turning this arm on**, not as unrelated work; the two items were filed separately but are coupled.
- **Why not PageIndex's own design:** PageIndex retrieves by having an **LLM navigate the tree**, i.e. several sequential LLM calls per query. `PERFORMANCE.md` P3-2 already records that today's 3 sequential calls (rewrite → rerank → answer) are the single largest perceived-latency lever on the shared, borrowed endpoint; an agentic tree walk would move latency from "slow" to "unusable". The design above keeps PageIndex's insight — *document structure is a retrieval signal* — while paying for it at **index time** instead of query time, and adds **zero query-time LLM calls**. It also slots into the `asyncio.gather` that already runs the vector and keyword arms concurrently (`PERFORMANCE.md` P2-2), so the third arm costs no added latency.
- **Calibration warning:** PageIndex's headline 98.7% is on **FinanceBench** — financial filings, which have unusually regular, tabulated section structure. Research reports sit between filings and free prose. Do not carry that number over as an expectation.
- **Blocked on:** `A12` Phase 2 (there is no node to summarise until headings are detected and chunk boundaries align), **`Q1-1`** (rank fusion, per the mixed-notebook argument above), and **`Q1-3`** to prove the arm helps rather than merely changes results. `A12` Phase 0/1 are *not* blocked by any of these and should run first regardless.
- **Related:** `Q2-1` already stops chunks merging across a *register* change (table/footnote/header); this item and `A12` extend the same idea to a *topic* change.


---

## P2 — minor / known nuances

### [x] Q2-1 · Cross-section chunking blends DOCX meta-sections / tables
- **Issue:** `chunk_sections` packs across all extractor sections, so DOCX header/footer/footnotes and PDF table blocks can merge into adjacent body chunks (noted in the chunking PR review).
- **Impact:** Minor citation-precision noise (`document – footnotes`-style span labels).
- **Fix:** **Done.** `chunk_sections` infers a section kind from extractor location labels (`table`, `header`, `footer`, `footnote`, `text boxes`, `transcript`, body) and flushes the packing buffer when the kind changes, with no overlap carried across that boundary.

### [x] Q2-2 · Near-duplicate chunks from sentence overlap
- **Issue:** `DEFAULT_OVERLAP_SENTENCES = 1` means adjacent chunks share a boundary sentence.
- **Impact:** Near-duplicate chunks can occupy multiple top-k slots, slightly reducing context diversity.
- **Fix:** **Done.** Retrieval now diversifies the hybrid candidate list before rerank, dropping lower-ranked chunks whose token-set Jaccard overlap with an already kept candidate is very high. Chunking also drops overlap when carrying it would exceed the target, avoiding pathological duplicated boundary chunks without turning overlap off globally.
