# Spreadsheet ingestion design notes

Design reference for `.xlsx` / `.csv` support. The product backlog item lives in
[`ROADMAP.md`](ROADMAP.md) as `A6c`; this file captures chunking and extraction rules
that are too detailed for the roadmap.

> **Status (2026-07-25): the MVP described here is implemented.** Code lives in
> `app/ingest.py` (`_extract_spreadsheet` and the `_read_xlsx_sheets` /
> `_read_csv_sheet` access helpers), tunables in `[spreadsheet]`
> (`app/config.py`), tests in `tests/test_spreadsheet.py`. What shipped: Q&A
> detection (incl. headerless two-column), generic-record fallback with
> token-aware row packing and identifier-repeating splits, hidden-sheet
> skipping, row/column/file caps, CSV encoding + delimiter detection, and the
> `A6a` diagnostics integration. What did **not** ship, by design: detection
> rules for records/metrics/other sheet types beyond the generic fallback (the
> taxonomy below stays a reference, not a queue), and the table-query tool
> (`A6d`). Sections below describing those remain forward-looking.

## Product direction

Spreadsheet support should not use one universal chunking strategy. Different sheet
shapes answer different user needs:

| Sheet type | Examples | First implementation stance |
|---|---|---|
| Q&A pairs | FAQ sheets, helpdesk knowledge bases, policy Q&A | Do first. Best fit for RAG. |
| General records | CRM exports, inventory, customer lists, database-like tables | RAG for semantic lookup; SQL/table tooling later for exact filters. |
| Numeric reports | Sales reports, budgets, KPIs, financial statements | RAG for explanation; structured query/calculation tool for totals and ranking. |
| Survey/forms | Survey responses, applications, inspection forms | Record chunks plus field summaries. |
| Glossary/dictionary | Terms, abbreviations, code definitions | Strong RAG fit; similar to Q&A. |
| Lookup/mapping tables | Product codes, department mappings, country codes | Key-value or SQL-style lookup works better than freeform RAG. |
| Checklist/audit lists | Test cases, compliance checks, risk registers | Record chunks; preserve status/owner/severity fields. |
| Schedule/timeline | Project plans, event schedules | Record chunks; dates need structured handling. |
| Matrix/rubric | Scoring matrices, capability maps, permission matrices | Preserve row/column intersection semantics. |
| Key-value forms | Single-sheet settings, profile forms | Key-value chunks. |
| Logs/transactions | Event logs, payment rows, activity history | Structured storage first; RAG only for notes/descriptions. |

The table is a **reference taxonomy, not an implementation queue**. The MVP
implements Q&A detection only; every other sheet shape falls back to the same
bounded generic-records chunking, with diagnostics stating that the sheet was
ingested as generic records. General records and numeric reports are the most
likely next targets — enterprise demand for both is high — but they ship as
their own roadmap items with their own detection rules, not silently inside
the MVP.

## MVP priority: Q&A sheets first

Q&A-style sheets are the safest first target because each row is already a retrieval
unit. They do not require numeric recomputation, sorting, grouping, or joining.

Detection should support:

- explicit columns named like `question`, `q`, `問題`, `提問`;
- explicit columns named like `answer`, `a`, `答案`, `回覆`;
- optional `category`, `tags`, `keywords`, `source`, `updated_at`;
- headerless two-column sheets, with diagnostics marking the mapping as auto-detected.

Each Q&A row should become one chunk unless the answer is very long. If the answer
must be split, every child chunk should repeat the original question and row metadata.

Recommended chunk text (decision: one trimmed-preamble text per chunk — see
"Decision: single trimmed-preamble embedding text" below. Constant fields such
as workbook filename, row number, and detected type live in chunk metadata,
not in the embedded text):

```text
Sheet: FAQ · Category: 帳號登入 · Tags: 密碼, 登入, email

Question:
忘記密碼怎麼辦？

Answer:
請在登入頁點選「忘記密碼」，輸入註冊 email 後依照信件指示重設。
```

Recommended location label:

```text
sheet "FAQ" row 12
```

## Storage format

Use two representations:

- `chunks.text`: human-readable labeled plain text for embedding and source preview.
- chunk/source metadata JSON: machine-readable fields such as workbook, sheet, row
  range, detected type, columns, category, tags, and extraction warnings.

Do not use JSON/TOML/YAML as the primary text sent to embeddings. Labeled plain text is
more readable in citations, easier to debug, and less brittle when values contain
quotes, line breaks, or punctuation. JSON is still the right format for internal
metadata.

## Headerless sheets

Do not blindly treat the first row as column names. The importer should:

1. inspect the first few rows;
2. infer whether the first row looks like a header;
3. fall back to generated labels such as `Column A`, `Column B`;
4. for Q&A candidates, infer likely question/answer columns from text shape;
5. show the decision in ingestion diagnostics and allow a future UI override.

For a headerless two-column Q&A sheet, the generated chunk can still use clear
labels (the auto-detection note goes to metadata/diagnostics, not the embedded
text):

```text
Sheet: Sheet1

Question:
忘記密碼怎麼辦？

Answer:
請在登入頁點選「忘記密碼」...
```

## General record sheets

Database-like sheets should be chunked as structured records, not whole-sheet text.
Small/narrow sheets can group several rows per chunk; wide sheets should use fewer rows
per chunk, sometimes one row per chunk.

Example:

```text
Sheet: 客戶資料 · Columns: 客戶ID, 公司名稱, 產業, 區域, 合約狀態, 備註

Row 2:
客戶ID = C001
公司名稱 = 台灣大成製造
產業 = 鋼鐵
區域 = 台中
合約狀態 = 有效
備註 = 2025 年續約

Row 3:
...
```

RAG is useful for semantic lookup over text fields and notes. Exact filtering,
counting, sorting, and joins should eventually use structured table storage and a
table-query tool rather than vector search alone.

### Token budgeting for record chunks (anti-silent-truncation)

e5 truncates past 512 tokens without any error, so an over-long record chunk
looks indexed while its tail rows are unreachable through the vector path —
the worst spreadsheet failure mode. Layered strategy:

1. **Prevent (primary).** Estimate tokens at chunk-build time with a
   conservative character heuristic (CJK ≈ 1 token/char, ASCII ≈ 1 token per
   ~4 chars, plus a safety margin) and pack rows adaptively:
   budget = 512 − passage prefix − preamble − margin, targeting
   `embed_token_budget` (default ≈ 400 estimated tokens, aligned with
   `[chunking].cjk_target_chars`). Wide rows naturally degrade to one row per
   chunk.
2. **Split single over-budget rows.** When one row alone exceeds the budget,
   split it into column-group child chunks that each repeat the identifier
   columns (the same rule as long Q&A answers repeating the question), with
   location labels like `Row 7 · part 2/3 · columns G–T`. A pathological
   giant cell (e.g. a memo pasted into 備註) routes through the normal text
   chunker as its own sections.
3. **Detect (backstop).** Store the token estimate in chunk metadata; `A6a`
   diagnostics warn on any chunk whose estimate exceeds the window. Calibrate
   the heuristic against the real tokenizer with the existing
   `python -m tests.inspect_e5_chunk_tokens` harness (`QUALITY.md` Q0-4).
4. **Mitigation nuance — do not rely on it.** Keyword search scans the full
   `chunks.text` in SQLite, so a truncated tail still matches exact terms
   through the hybrid keyword channel. That shrinks the blast radius but does
   nothing for semantic queries; prevention above is the real fix.

Grouping several rows per chunk is a vector-count economy, not a quality
feature — at POC scale vectors are cheap, so when in doubt use fewer rows per
chunk.

## Numeric reports

Numeric/statistical sheets should be chunked by metric block, period, region, or table
section. They should not rely on RAG for precise arithmetic.

Example:

```text
Sheet: 2025 營收統計 · Block: Taiwan quarterly revenue
Metrics: 營收, 毛利率 · Dimensions: 地區, 季度

2025Q1 台灣營收 = 1,200,000；毛利率 = 42%
2025Q2 台灣營收 = 1,350,000；毛利率 = 44%
2025Q3 台灣營收 = 1,410,000；毛利率 = 43%

Notes:
- Q2 成長原因：新增兩家代理。
```

RAG can explain or retrieve relevant metric blocks. Questions such as totals,
averages, ranking, year-over-year change, and "top N" should be handled by structured
calculation/query tooling when that exists.

## Division of labor: RAG vs. table queries

Records/metrics sheets serve two different question classes, and they need
different machinery:

- **RAG (A6c records chunks):** single-row point lookups (the hybrid keyword
  channel matches exact names/IDs), semantic/fuzzy lookup over text columns
  (the sheet says 鋼鐵, the user asks 金屬加工), free-text columns (備註),
  and cross-source answers alongside PDFs/DOCX in the same notebook.
- **Table queries (`ROADMAP.md` `A6d`, customer-driven):** exact filtering,
  counting, sorting, aggregation, joins. Top-k vector retrieval structurally
  cannot guarantee completeness, and LLM arithmetic over retrieved fragments
  is unreliable — do not tune RAG toward these question types; route them.

RAG and table-query results do **not** rank-fuse the way the keyword/vector
hybrid does. Hybrid works because both channels score the same objects (text
chunks) over the same corpus, so scores can be normalized and blended. A table
result (a count, a filtered row set) is a deterministic answer, not a ranked
candidate — there is no shared score space. Combination therefore happens at
**context assembly** (both results placed into the generation prompt), never
at ranking.

Recommended interaction shape (recorded with `A6d`):

1. **Deterministic pre-filter:** if the notebook contains no table sources,
   skip everything below — the pure-RAG path pays zero added cost.
2. **One-shot LLM routing** folded into the existing query-rewrite call
   (rewrite → hybrid → diversify → rerank): the same call also emits
   `route: rag | table | both`, plus a table query spec when applicable. No
   extra LLM round-trip, no agent loop.
3. **Verify + fall back:** table-path failures are detectable (invalid spec,
   missing table, unexpected empty result); on failure fall back to RAG with
   an explicit caveat in the answer. RAG failures are silent — which is why
   the table path, not the RAG path, carries the verification burden.
4. **`route: both` covers mixed questions** (「台中的客戶有幾家？他們主要的
   合約問題是什麼？」): the count comes from the table tool, the themes from
   records chunks, merged in the prompt.

Until `A6d` exists, the transition guard is prompt-side: the default answer
policy (`ROADMAP.md` `E2`) tells the model that spreadsheet sources support
lookup and semantic search but not reliable aggregation.

## Parser and dependencies

- **XLSX:** `openpyxl` in read-only mode
  (`load_workbook(path, read_only=True, data_only=True)`). Pure Python, no
  native build step, exposes sheet visibility (`ws.sheet_state`:
  `visible`/`hidden`/`veryHidden`) and the structural metadata (merged cells,
  number formats) that the records/metrics phase will need. `data_only=True`
  returns cached formula results; formula cells without a cached value read as
  empty and must be counted toward the `formula-heavy` diagnostic warning.
- **CSV:** stdlib `csv` — no new dependency.
- **Encoding detection:** `charset-normalizer` — already present in the venv
  as a transitive dependency; pin it explicitly in `requirements.txt` the
  moment code imports it.
- **Out of scope:** legacy `.xls`, `.xlsb`, ODS, and `pandas` (heavy, wrong
  altitude for row-level chunk shaping).
- **Documented swap-point:** `python-calamine` (Rust-backed, prebuilt wheels
  for CPython 3.10–3.14 on macOS/Linux/Windows) reads the same files
  dramatically faster, detects hidden/very-hidden sheets
  (`SheetMetadata.visible` — verified locally, including CJK content), and
  would add `.xls`/`.xlsb`/ODS support for free. It does not expose number
  formats or merged-cell structure, which the metrics/matrix phases may want.
  Keep all workbook access behind a single extraction helper in
  `app/ingest.py` so the reader can be swapped without touching chunking —
  the same pattern as the `app/jobs.py` Redis/RQ swap-point.

## CSV encoding and dialect detection

zh-TW corpora make encoding the biggest practical failure mode for CSV — Big5
(CP950) exports are still common and "successfully" decode into mojibake when
assumed UTF-8. Ingestion must:

1. check for a UTF-8/UTF-16 BOM first;
2. try strict UTF-8;
3. fall back to `charset-normalizer` detection (expect Big5/CP950 candidates);
4. decode with `errors="replace"` only as a last resort, counting replacement
   characters;
5. record the chosen encoding, detection confidence, and replacement-character
   count in `A6a` diagnostics — a file that decoded via fallback should be
   visibly flagged, never silently indexed.

Delimiter/dialect: run `csv.Sniffer` on a bounded sample (first ~64 KB) with a
comma fallback; record the detected delimiter in diagnostics.

## Hidden sheets

Hidden and very-hidden sheets are **skipped by default** and recorded in
diagnostics (`skipped hidden sheet "<name>"`). They are usually lookup scratch
pads, stale copies, or intentionally suppressed data; indexing them surprises
users. No MVP override — revisit only if a customer asks.

## Configuration

Tunables live in `app/config.py` as a `[spreadsheet]` group (defaults ←
`config.toml` ← `NOTEBOOKLM_SPREADSHEET_<FIELD>`), per repo convention:

- `max_file_bytes`, `max_rows`, `max_cols` — hard ingest caps;
- `rows_per_chunk_min` / `rows_per_chunk_max` — record-chunk grouping bounds;
- `embed_token_budget` — estimated-token cap per chunk for the adaptive row
  packing (see "Token budgeting for record chunks");
- `header_sample_rows` — rows inspected for header inference;
- `qa_question_synonyms` / `qa_answer_synonyms` — column-name lists for Q&A
  detection (`question`, `q`, `問題`, `提問` / `answer`, `a`, `答案`, `回覆`),
  configurable because customer sheets use house vocabulary such as
  `客戶提問` / `回覆內容`.

Like `[chunking]`, values that change chunk shape require re-indexing; mark
them the same way in `config.example.toml` and keep dataclass defaults equal
to shipped behavior (`tests/test_config.py`).

## Decision: single trimmed-preamble embedding text

`chunks.text` is embedded directly (with the settings-driven e5 `passage: `
prefix — see `RETRIEVAL.md`) **and** shown in previews/citations, and
multilingual-e5-large silently truncates past 512 tokens. A full labeled
preamble (`Type:`/`Workbook:`/`Row:`/…) would spend that budget on boilerplate
identical across chunks and dilute similarity scores.

**Decided (option B):** keep one text per chunk with a trimmed preamble.

- Embed only semantically useful context: sheet name, category/tags, question,
  column names — e.g. `Sheet: 帳號問題 · Category: 帳號登入` followed by the
  Q&A/record body (all examples above use this shape).
- Constant fields (workbook filename, row number/range, detected type,
  detection notes) live in chunk/source metadata; `A6a` diagnostics and the
  preview drawer render them, and the citation label shows
  `faq.xlsx · sheet "FAQ" row 12`.
- Rows-per-chunk is capped by the token budgeting below; diagnostics warn when
  a chunk still exceeds the window.
- No schema or pipeline change; what users see is exactly what was embedded.

Escalation path only if evals show B underperforming: first a derived embed
text (strip label lines deterministically at embed time — no schema change,
but embedded ≠ displayed and the stripper must track the label format), and
last a separate `embed_text` column (schema + ingest + rebuild +
keyword-scoring change; a spreadsheet-only special case in an otherwise
uniform pipeline).

## Diagnostics

Spreadsheet ingestion should integrate with `A6a` diagnostics:

- detected sheet type (`qa_pairs`, `records`, `metrics`, etc.), or the
  generic-records fallback note;
- header row decision and generated column names;
- row/column counts, skipped rows, skipped-by-default hidden sheets,
  formula-heavy warnings;
- CSV encoding decision (BOM / UTF-8 / detected fallback), detection
  confidence, and replacement-character count;
- chunk count and row ranges, plus warnings for chunks exceeding the
  embedding-token window;
- preview of the first generated chunks;
- warnings when the sheet is too wide, too large, or mostly numeric.

Diagnostics are part of the product contract: users need to know whether the system
understood the sheet as Q&A, records, metrics, or something else.
