# Spreadsheet ingestion design notes

Reference notes for future `.xlsx` / `.csv` support. The product backlog item lives in
[`ROADMAP.md`](ROADMAP.md) as `A6c`; this file captures chunking and extraction rules
that are too detailed for the roadmap.

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

Recommended embedding text:

```text
Type: Q&A
Workbook: faq.xlsx
Sheet: FAQ
Row: 12
Category: 帳號登入
Tags: 密碼, 登入, email

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

For a headerless two-column Q&A sheet, the generated chunk can still use clear labels:

```text
Type: Q&A
Workbook: faq.xlsx
Sheet: Sheet1
Row: 3
Detection: auto-detected question/answer columns

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
Type: Records
Workbook: customers.xlsx
Sheet: 客戶資料
Table: A1:F240
Rows: 2-4
Columns: 客戶ID, 公司名稱, 產業, 區域, 合約狀態, 備註

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

## Numeric reports

Numeric/statistical sheets should be chunked by metric block, period, region, or table
section. They should not rely on RAG for precise arithmetic.

Example:

```text
Type: Metrics
Workbook: revenue_report.xlsx
Sheet: 2025 營收統計
Block: Taiwan quarterly revenue
Rows: 4-8
Metrics: 營收, 毛利率
Dimensions: 地區, 季度

2025Q1 台灣營收 = 1,200,000；毛利率 = 42%
2025Q2 台灣營收 = 1,350,000；毛利率 = 44%
2025Q3 台灣營收 = 1,410,000；毛利率 = 43%

Notes:
- Q2 成長原因：新增兩家代理。
```

RAG can explain or retrieve relevant metric blocks. Questions such as totals,
averages, ranking, year-over-year change, and "top N" should be handled by structured
calculation/query tooling when that exists.

## Diagnostics

Spreadsheet ingestion should integrate with `A6a` diagnostics:

- detected sheet type (`qa_pairs`, `records`, `metrics`, etc.);
- header row decision and generated column names;
- row/column counts, skipped rows, hidden sheets, formula-heavy warnings;
- chunk count and row ranges;
- preview of the first generated chunks;
- warnings when the sheet is too wide, too large, or mostly numeric.

Diagnostics are part of the product contract: users need to know whether the system
understood the sheet as Q&A, records, metrics, or something else.
