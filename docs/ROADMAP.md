# Product roadmap

Product-facing roadmap for the NotebookLM-style personal AI assistant: UX, admin workflows, Eval Workbench, AI governance, source-format support, and new AI-assisted surfaces. Same tick-off format as `PERFORMANCE.md` / `QUALITY.md`.

Performance and retrieval-quality engineering stay in their own backlogs (`PERFORMANCE.md`, `QUALITY.md`). Security policy and dependency triage stay in `SECURITY.md`. This file tracks the product/admin capability surface and points to those deeper references when needed.

**Current target-deployment constraint:** the known customer inference side is borrowed and fixed (Gemma 4 31B chat + multilingual-e5-large embeddings — **chat + embedding only**). Features needing only chat completions are cheap; new extraction paths (web, PPTX, spreadsheets, OCR) are app-side work; new model capabilities (vision, speech) must be verified against the active customer endpoint first. `O1` adds admin capability probes so this assumption can be tested per deployment before enabling vision-dependent work.

Status legend: `[ ]` todo · `[~]` in progress · `[x]` done

---

## Recommended next round

1. **Answer-quality loop:** implement `E1e-2` answer/citation judging together with `E2` notebook domain hints and answer policy, then validate changes through Eval Workbench comparisons.
2. **Admin LLM operations:** implement `O1` Phase 1 first — separate chat/embedding tests and capability probes, including the optional image-understanding checkbox.
3. **Format foundation:** implement `A6a` ingestion diagnostics before adding more source formats.
4. **Source-format MVP path:** after `A6a`, prioritize `A6c` Q&A-style spreadsheets, then `A6` Web URL with SSRF guards, then `A6b` PPTX text-first ingestion.
5. **Customer-driven later work:** keep `A9`/`A10`/`A11` low priority unless a customer requirement or verified serving capability changes the economics.

---

## UX improvements

### High priority

#### [x] U1 · Ask without a full page reload (HTMX partial swap)
- **Issue:** Ask was `POST → 303 → full page re-render`: screen flash, scroll reset, three panes re-rendered per question.
- **Fix:** **Done.** The ask form now `hx-post`s and swaps only the messages pane (`_messages.html`); the URL updates via `HX-Push-Url`; non-JS fallback keeps the redirect. Prerequisite for U2 (streaming).

#### [x] U2 · Streaming responses (SSE)
- **Fix:** **Done.** Asking now posts to a streaming endpoint from the enhanced UI: the user sees retrieval/generation status, answer chunks stream into the chat pane, and the final saved message swaps back into the normal Markdown/citation/follow-up rendering path. Same item as [`PERFORMANCE.md` P3-1](PERFORMANCE.md).

#### [x] U3 · Citation → source highlight
- **Done.** Clicking a `[N]` citation chip now opens the source preview drawer scrolled to and flashing the exact cited chunk (`#preview-chunk-{id}`), plus flashes the matching left-pane source row. Required carrying the chunk row id through `merge_candidates` → `citation_payload` (`chunk_id`); older messages stored before this field gracefully fall back to expanding the inline snippet.

#### [x] U4 · Traditional Chinese UI
- **Issue:** UI strings were English while the user base and content are Chinese.
- **Fix:** **Done.** Templates, JS strings (thinking bubble, confirms, loading texts), and user-facing server messages are now Traditional Chinese (POC: hardcoded zh-TW, no i18n framework).

#### [x] U5 · Conversation management
- **Fix:** **Done.** Conversation menu supports renaming the active conversation, shows message counts and relative update times, and keeps the active row visually distinct.

#### [~] U16 · Studio information-architecture restructure (tools launcher + outputs shelf)
- **Phase 1 done** — tools tile grid + slim briefing strip + suggestions relocated to chat. Phases 2–3 remain.
- **Issue:** The Studio (right pane) stacks one always-on card per generator (Suggested questions, Briefing, Compare, Meeting minutes). It mixes two different kinds of thing — **generators (actions)** and **outputs (artifacts)** — in one vertical list, so every new AI feature (A4 study guide/FAQ/timeline, A5 translate, …) adds another card and the column grows endlessly cluttered.
- **Target model:** separate **tools** from **outputs**, so adding a feature = +1 tile, not +1 always-on card.
  - Briefing → a slim one-line expandable strip (it's ambient context, not an action).
  - **Tools** → a compact tile grid; each generator is a tile that opens its config in the existing `preview-modal` drawer (`open-preview`/`close-preview`), runs, and writes its result to the outputs shelf.
  - **Outputs / Notes** → one unified artifact shelf: every generated result (compare, minutes, study guide, …) + pinned answers, each with a type badge, collapsible, editable (U8), exportable.
  - Relocate **Suggested questions** out of Studio into the chat area (empty state / follow-up chips) — it's a chat-entry aid, not a Studio artifact.
- **Reuses existing infra:** the `notes/add` save path + `#studio-notes` refresh and the `preview-modal` Alpine pattern already exist. Mostly a template/CSS reshuffle plus a few thin routes. No build step.
- **Phased (tick as done):**
  - [x] Phase 1 — **Done.** The four generators collapsed into a **Tools** tile grid (`_studio_tools.html`); each tile opens its config in the `preview-modal` (`_tool_panel.html`); Briefing is now a slim expandable strip; Suggested questions moved to the chat empty-state. **All generators now use manual save** — the result is shown with a shared save-to-notes button (`_save_note_button.html`), the user decides what lands in the shelf (no auto-save). New routes: `GET /_tools`, `GET /tools/{kind}`, `POST /artifacts/{kind}`; dead `GET /_compare`/`/_minutes`/`/_suggestions` partial routes + `_compare.html`/`_minutes.html` removed.
  - [ ] Phase 2 — unify Notes into an **outputs shelf** (type badges; all generators save here; inline edit = U8).
  - [ ] Phase 3 *(optional)* — tabbed or fully-collapsible Studio if the tile grid itself grows large.
- **Note for new AI features:** A4/A5 and later generators should be implemented as **tools (tiles) writing to the outputs shelf**, not as new stacked cards.
- **Design reference:** broader Studio/reporting paradigms are kept in [`PRODUCT_DESIGN_NOTES.md`](PRODUCT_DESIGN_NOTES.md). Only the checkboxes above are scheduled `U16` work.

### Medium priority

#### [x] U6 · Upload feedback & batch size
- **Fix:** **Done.** The compact upload card shows selected files, file sizes, total size, configured batch limit, and a clearer over-limit message before upload.

#### [x] U7 · Answer action row — copy
- **Fix:** **Done.** Copy button on every assistant message (copies the raw Markdown; transient ✓ feedback). Regenerate / expand-all-citations remain todo.

#### [x] U8 · Editable notes
- **Done.** Each note in the shelf has an inline **編輯** toggle (Alpine) revealing a title + content (Markdown) form; saving `POST /notes/{id}/edit` updates in place and re-renders the shelf. Add/pin/delete unchanged.

#### [x] U9 · Global search
- **Fix:** **Done.** `/search` searches the signed-in user's notebooks, source filenames/summaries, conversation titles, and notes using scoped SQLite `LIKE` queries.

#### [x] U10 · Mobile / responsive pass
- **Done.** The app now supports a usable narrow viewport baseline without changing the desktop workspace model: the topbar/nav wraps cleanly, the three-pane workspace stacks at tablet widths, modals fit within mobile viewports, Eval tabs scroll instead of wrapping awkwardly, forms/actions collapse to full-width controls where needed, and table/card-heavy admin pages keep their existing `data-label` mobile presentation.

### Polish

#### [ ] U11 · Dark mode (CSS variables are ready)
#### [x] U12 · Onboarding empty state (3-step "upload → wait → ask" guide)
- **Fix:** **Done.** Empty chat state now shows a compact three-step upload → index → ask guide.
#### [x] U13 · Accessibility pass (focus rings, aria labels, Esc to close modals)
- **Done.** Added skip navigation, active-nav `aria-current`, modal focus targets/return behavior for source/tool preview and audit metadata dialogs, explicit menu expanded state, Eval tab roles/selected state, accessible labels for compact chat actions, and reduced-motion handling. Existing focus-ring token remains the global focus treatment.
#### [x] U14 · Friendlier error messages (no raw exception strings in chat)
- **Fix:** **Done.** Chat and Studio generation errors now show user-facing messages while raw provider/exception details stay in logs and metadata.
#### [ ] U15a · i18n foundation (deployment-level locale)
- U4 hardcoded zh-TW strings directly in templates / `app.js` / server messages — fine for the current single-language deployment, but every new feature adds more future extraction work. First pass should stay scoped: add a message catalog (`app/i18n.py` or equivalent), a template `t()` helper, a JS strings object emitted from `base.html`, and a config-driven locale (default `zh-TW`, optional `en`) via `app/config.py` / `config.example.toml` / `NOTEBOOKLM_UI_LANGUAGE`. Cover the high-churn surfaces first: nav, chat empty/stream states, Studio tool labels, user-facing server errors, and exported-Markdown headings (`引用來源` / `筆記`) that currently live in `main.py`.

#### [ ] U15b · Full i18n extraction + optional user toggle
- After U15a lands, finish extracting the remaining admin/settings/search/source/notebook-management strings and decide whether language stays deployment-level or becomes per-user. A per-user English/zh-TW toggle requires persistence, request-time locale resolution, UI affordance, and broader tests across HTMX partials. Keep LLM prompt language rules separate from UI i18n; prompts control model output language and should not be treated as display copy.

#### [ ] U17 · Meaningful "focus" for source comparison (deferred feature)
- The compare tool used to expose a **聚焦重點 (focus)** free-text input. The value *was* passed to the prompt (`Focus: …` + "prioritise points relevant to it"), but because the comparison runs on each source's thin 2–4-sentence **summary**, the focus had little material to differentiate and the output barely changed — so the input was **removed from the UI** to avoid implying an effect it can't deliver. `POST /compare` still accepts an (empty) `focus` param, so re-enabling is a template-only change.
- **Future direction: topic-focused source comparison.** Re-introduce focus as a **topic** field, not a cosmetic prompt hint. On a non-empty topic, run a small source-scoped retrieval for each selected source, collect the topic-relevant chunks from each source, then compare on those chunks instead of the thin source summaries. The report should show Shared / Distinct / Contradictions plus which sources had weak or no topic evidence. This fits the current vector RAG design; the key change is using per-source retrieval before comparison. Measure against a representative set first.
- **Later extension:** section-focused comparison can follow once ingestion preserves stronger section metadata (DOCX/HTML headings, PPTX slide titles, PDF page ranges, spreadsheet sheet/table names). Until then, topic retrieval is the safer v1.

---

## Admin evaluation workbench

### High priority — unlocks blocked retrieval / tuning work

#### [~] E1 · In-deployment eval workbench (private customer data stays in place)
- **Issue:** Several quality/performance items are blocked on representative customer-style eval data (`QUALITY.md` Q0-2 / Q1-1 / Q1-2 / Q1-4, `PERFORMANCE.md` P3-2). Customer source data may be unable to leave the deployment, so the app needs an admin-only way to build and run evals **inside** the customer's environment.
- **Target model:** an admin creates eval sets from already-indexed DB data, runs retrieval/profile experiments with visible progress, compares every run historically, applies/rolls back approved runtime-safe parameters, and exports sanitized settings/reports for the implementation team.
- **Guardrails:**
  - Generated candidate questions are suggestions only; admins must review/approve/edit before they become ground truth.
  - Each run stores immutable snapshots: eval set version, active/candidate profile, LLM setting summary, app version/commit when available, aggregate metrics, and per-question results.
  - Runtime-safe parameters can be applied immediately; index-affecting parameters (chunk sizes, overlap, embedding model/prefix/dimension) must be shown with strong "requires Clear/Rebuild or reindex" warnings and should not be silently applied.
  - Export has two modes: sanitized profile/report (settings + aggregate metrics, no source text) and full internal report (questions, expected evidence, failures; for in-environment or explicitly approved sharing only). Full internal report exports must be recorded in the durable audit trail.
- **Phased (tick as done):**
  - [x] E1a — **Done.** Schema + admin shell landed: `eval_sets`, `eval_items`, `eval_runs`, `eval_results`, `retrieval_profiles`; `/admin/evals` shows the active baseline profile, eval sets, and historical run list. This creates the audit trail before any tuning UI exists.
  - [x] E1b — **Done.** Eval-set builder + retrieval-only runner landed: admins can add approved questions against existing notebooks/sources, generate draft candidates from indexed chunks, approve/delete items without losing scroll position, delete eval sets, queue background runs, watch progress (`queued/running/succeeded/failed`, current item/total/current step), and inspect persisted Recall@k, MRR, top score, low-confidence rate, latency, error counts, expected evidence, compact retrieved snippets, miss diagnosis, and per-question hit/miss/unscored/error status. LLM-assisted authoring/judging remains E1e.
  - [x] E1c — **Done.** Profile comparison + apply/rollback landed. A runtime active-params layer (`ACTIVE_RETRIEVAL_PARAMS` in `app/main.py`) now backs the retrieval path: admins author candidate profiles (7 runtime-safe params), run an eval set against any profile via an isolated per-run override (the runner applies the run's frozen snapshot, not live config), compare two succeeded runs of the same set (param diff + metric diff + per-question improved/regressed), then **apply** a profile to live chat retrieval (persisted via `retrieval_profiles.is_active`, reloaded on startup) or **roll back** by applying a previous profile. Index-affecting profiles (`requires_reindex = 1`) are refused at apply. Routes: `/admin/evals/profiles` (create/delete/apply), `/admin/evals/compare`.
  - [x] E1d — **Done.** Export + audit foundation landed. Retrieval profiles can be exported as sanitized JSON, eval runs can be exported as sanitized JSON (no questions/evidence/retrieved snippets) or full internal JSON (questions, expected evidence, diagnostics, retrieved snippets) gated by explicit confirmation. A durable `audit_events` table and `/admin/audit` viewer now record export events plus high-risk admin actions: retrieval profile create/apply/delete/export, LLM settings updates, index Clear/Rebuild, user-management changes, and notebook/source/chat/note lifecycle or Markdown-export actions.
  - [x] E1e-1 — **Done.** LLM-assisted eval authoring landed as a draft-only flow: admins can generate candidate questions from selected indexed sources, request answerable / cross-lingual / unanswerable item types, review item type, reference answer, expected substrings, and source/chunk grounding, then approve manually. Generated metadata records compact origin/model/prompt-version/source ids without copying prompts or source text. The deterministic chunk-based generator remains available as a no-LLM fallback.
  - [ ] E1e-2 — **High priority for answer quality.** Answer-quality and citation judging: optionally generate an answer during eval runs and score answer quality, groundedness, citation correctness, and abstain correctness as secondary metrics. Keep these metrics separate from retrieval-only Recall/MRR, and include full judging detail only in full internal exports.
  - [x] E1f — **Done.** Eval tuning guide landed as `/admin/evals/help` and as a first-class tab in the Eval workbench. It converts the internal tuning PDF/discussion into HTML covering: when to tune parameters vs fix Eval items, symptom -> likely cause -> parameter guidance, profile experiment workflow, starter profiles, non-runtime-safe changes that require reindex, and the role of future domain hints / answer policy. The PDF remains optional/shareable, but the product source of truth is now HTML so labels stay aligned with the live profile UI.
- **Recommended next implementation round:** prioritize **E1e-2** together with **E2** because they directly improve and measure answer quality. Further audit expansion should wait for customer requirements, e.g. explicit read-access audit for source preview/result viewing. Defer index-affecting parameter application until there is a clear Clear/Rebuild UX.

#### [ ] E2 · Notebook domain hints and answer policy — high priority for answer quality
- **Issue:** Some "inaccurate" answers are not fixed by retrieval-weight tuning alone. Domain-specific aliases, abbreviations, internal product names, and deployment-specific answer rules may need to be available at the notebook level so query rewrite can find the right evidence and final answers follow the customer's rules.
- **Target model:** each notebook can carry bounded, structured **domain hints** (term/synonyms/definition/query expansion/answer note) plus a concise **answer policy**. Hints improve retrieval/query rewrite; policy controls final answer behavior. Neither should become an unbounded extra knowledge base.
- **Guardrails:**
  - Keep hints structured and size-limited; avoid one giant free-form prompt pasted into every LLM call.
  - Query expansion fields can influence rewrite/retrieval; answer policy fields can influence final answer wording. Do not mix the two blindly.
  - Treat hints/policy as potentially sensitive. Sanitized exports and audit metadata should store identifiers/counts/summaries, not full prompt or proprietary keyword text.
  - Validate with the Eval Workbench: compare the same Eval Set with and without hints, and verify improvements do not come from false-positive evidence matches.
- **Phased:**
  - [ ] E2a — Schema + notebook admin/editor UI for domain hints and answer policy.
  - [ ] E2b — Feed domain hints into query rewrite / retrieval expansion with explicit limits.
  - [ ] E2c — Feed answer policy into final answer prompting without treating it as source evidence.
  - [ ] E2d — Eval Workbench comparison path for "with hints" vs "without hints" runs.
  - [ ] E2e — Export/audit boundaries for hint/policy changes and exports.
- **Quality reference:** see `QUALITY.md` Q1-5.

---

## AI governance

### Medium priority — auditability, safety visibility, and cost control

#### [ ] G1 · AI governance telemetry and guardrail events
- **Issue:** The current audit trail records high-risk admin/data actions, but AI governance also needs usage visibility and safety-event traceability: token/cost usage, blocked or warned prompts, unsafe output attempts, prompt-injection signals, PII/secrets detection, and who exported or viewed sensitive AI reports. These signals are related to audit, but they have different volume, retention, and sensitivity than `audit_events`.
- **Target model:** keep the formal audit trail focused on low-volume authority/data-state actions, and add separate governance telemetry tables for high-volume AI events. Admins should see a unified governance dashboard, but the underlying data should stay separated:
  - `audit_events` for official actions such as settings changes, profile apply/rollback, exports, data lifecycle actions.
  - `llm_usage_events` for LLM/embedding calls, token/cost estimates, latency, status, and feature-level attribution.
  - `ai_safety_events` for guardrail decisions, categories, severity, detector version, and redacted summaries.
  - `messages` remains the canonical place for original conversation content; governance tables should store ids, hashes, redacted summaries, and compact metadata rather than copying full prompts/answers.
- **Guardrails:**
  - Do not store full prompts, source text, retrieved snippets, API keys, or full exported payloads in governance metadata by default.
  - Prefer provider-reported `usage` when available; fall back to explicit estimates and mark them as estimated.
  - Treat safety detections as review signals, not perfect truth. Keep detector version, rule version, category, severity, and decision so future audits can explain why something was allowed, warned, blocked, or redacted.
  - Keep customer data-residency constraints explicit: external moderation or gateway services are optional integration points, not baseline assumptions.
- **Phase 1 — low-cost in-app foundation (no new gateway required):**
  - [x] G1a — Add `llm_usage_events` and record per-call telemetry for rewrite, embedding, rerank, answer, summaries/artifacts, follow-ups, and eval runs. Store user/notebook/conversation/message ids where available, provider/model, call type, prompt/output token counts or estimates, latency, status, and error class. **Backend complete:** schema + sanitized recorder + core call-site instrumentation record compact usage events for chat completions, streaming answers, embeddings, query rewrite, rerank, starter/follow-up questions, source summaries, briefing/compare/artifacts/meeting-minutes/translation, and eval authoring/runs. Answer usage is backfilled with the saved assistant `message_id` when available, and retry/failure metadata is stored as compact scalar metadata.
  - [x] G1b — Normalize provider `usage` responses when present; retain `is_estimated` for char/token estimates so reports do not imply billing precision that the endpoint did not provide. **Backend complete:** OpenAI-compatible/Azure-style `prompt_tokens` / `completion_tokens` / `total_tokens`, common `input_tokens` / `output_tokens`, camelCase/gateway token-count fields, and nested `usage` / `token_usage` / `tokens` shapes normalize into `llm_usage_events`. Streaming chat requests ask for provider usage and safely retry without `stream_options` if the endpoint rejects it; missing usage falls back to char/4 estimates marked `is_estimated=1`.
  - [~] G1c — Add `ai_safety_events` plus a first local rules engine: input length limits, invisible/control text checks, obvious secret patterns, simple prompt-injection phrases, and deployment-specific deny/allow lists. Record redacted summary/hash, not raw sensitive content. **Backend MVP landed:** schema + sanitized recorder + local rules (`local.rules.v1`) now record findings for chat questions, streaming chat questions, compare focus text, eval authoring target language, and manual eval questions. The MVP records `warn` / `block_candidate` review signals without blocking user workflows. Remaining full-scope work: deployment-specific deny/allow configuration, broader surfaces/output scanning, and dashboard/reporting in G1d/G1e.
  - [ ] G1d — Add an admin governance dashboard and report surface with tabs for LLM usage, safety events, high-sensitivity exports, and settings/profile changes. Start with aggregate tables and filters before charts: daily usage, user/notebook/function breakdown, estimated/provider token totals, latency/error rates, eval-run cost summary, safety-event counts by category/severity/decision, and high-sensitivity action summaries.
  - [ ] G1e — Add governance report export and retention policy: CSV/JSON exports for usage and safety summaries first, optionally PDF later. Summarized usage can be retained longer; raw safety context should be short-lived or redacted, with full-content lookup going through existing message permissions.
- **Phase 2 — productized governance integrations:**
  - [ ] G1f — Evaluate LiteLLM Proxy for centralized spend tracking, virtual keys, budgets, rate limits, and cross-model gateway controls once the deployment needs more than in-app reporting. Note that LiteLLM key/spend management introduces a gateway and database dependency, so it should be a deliberate product/deploy decision.
  - [ ] G1g — Evaluate Presidio for PII/secrets detection and anonymization where local data processing is required.
  - [ ] G1h — Evaluate LLM Guard for input/output scanners such as prompt injection, secrets, toxicity, invisible text, token limits, and malicious URLs.
  - [ ] G1i — Evaluate NeMo Guardrails when policy flows need input, retrieval, dialog, execution, and output rails rather than only scanner-style detection.
  - [ ] G1j — Add connector abstraction so external guardrail/gateway decisions still write the same `ai_safety_events` / `llm_usage_events` records and remain auditable in the in-app governance dashboard.

---

## Admin operations

### Medium priority — safer LLM configuration and deployment flexibility

#### [ ] O1 · Admin-only LLM settings diagnostics and profiles
- **Issue:** The current `/settings` page stores one global LLM configuration (`llm_settings`, `id = 1`). It probes embedding dimension on save, but admins cannot test chat connectivity separately, keep multiple candidate configurations, or switch between known-good endpoints safely.
- **Target model:** only admins manage LLM settings. Do not expose LLM profile selection or editing to normal users in this phase. Admins can test, save, compare, and activate configurations while the app protects existing indexes from incompatible embedding changes.
- **Phase 1 — diagnostics before profile management:**
  - [ ] O1a — Add "Test chat model" and "Test embedding model" actions on `/settings`, with separate status, latency, provider/model/deployment summary, embedding dimension, and last-tested timestamp. Test results should avoid storing raw prompts/outputs; audit/governance metadata should keep compact status/error-class details only.
  - [ ] O1b — Add a capability probe section for optional model features: streaming support, provider usage reporting, JSON-following sanity check, and multimodal/vision support. The settings test UI should include an admin-controlled checkbox such as "also test image understanding"; it is off by default, and when checked sends a tiny built-in test image to the chat endpoint. Record capability/status only, without enabling A9 automatically.
- **Phase 2 — multiple profiles + safe activation:**
  - [ ] O1c — Replace the single global settings row with admin-managed LLM profiles: name, provider, base URLs, encrypted API key, chat model, embedding model/prefixes, temperature, timeout, last test status, and active flag. Migrate the existing `llm_settings` row into the default active profile.
  - [ ] O1d — Add safe profile activation rules. Chat-only changes can activate directly after a successful chat test. Embedding-affecting changes (model/base URL/prefix/dimension) must be blocked or strongly gated when the existing Chroma index dimension/config is incompatible, with clear Clear/Rebuild or reindex guidance.
- **Future phase — task-specific routing:**
  - [ ] O1e — Allow admins to assign different profiles to answer generation, embeddings, eval judging, eval authoring, source summaries, Studio artifacts, and low-cost follow-up/starter questions. Keep this out of the MVP until global profile switching is stable.
- **Guardrails:**
  - Keep all profile management admin-only.
  - Never expose stored API keys back to the browser.
  - Record profile create/update/test/activate/delete actions in audit metadata without storing secrets, prompts, outputs, or full endpoint payloads.
  - Treat multimodal probing as capability detection only; PPTX/image understanding still depends on A8/A9 roadmap items and customer need.

---

## New AI features

### Tier 1 — chat-only, cheap, high value

#### [x] A1 · Meeting-minutes organizer
- **What:** pick an indexed source (transcript upload) → structured minutes (主題/決議/行動項目(負責人/期限)/待辦/未決事項) → save to Notes.
- **Fix:** **Done.** A Studio **tool tile** (U16) with a source picker; `MEETING_MINUTES_PROMPT` (strong language rule). A non-meeting source shows the model's reason with no save option; a real transcript shows the minutes with a **manual** save-to-notes button.

#### [x] A2 · Follow-up question chips after each answer
- **What:** 2–3 suggested follow-ups under the latest assistant answer; click = ask.
- **Fix:** **Done.** Generated lazily after the answer renders (non-blocking separate request), cached in `messages.metadata_json.followups`, reuses the suggestion-chip fill+submit mechanism.

#### [x] A3 · Export to Markdown
- **What:** download a conversation (Q/A + citations) or all notes as `.md`.
- **Fix:** **Done.** Export buttons on the conversation menu and the Notes card; no LLM involved.

#### [x] A4 · Study guide / FAQ / timeline artifacts
- **Done.** Three generators (學習指南 / 常見問答 / 時間軸) built as **tools (tiles)** in the U16 Studio launcher. Each takes the notebook's source summaries (siblings of briefing/compare), runs a strong per-language prompt (`STUDY_GUIDE_PROMPT` / `FAQ_PROMPT` / `TIMELINE_PROMPT` in `app/llm.py`, dispatched via `ARTIFACT_PROMPTS` → `generate_artifact`), and shows the result with a **manual** save-to-notes button (no auto-save). Route: `POST /notebooks/{id}/artifacts/{kind}`.

#### [x] A5 · Explicit "translate this source's summary" action
- **Done.** A Studio **tool tile** (U16) — pick a source + a target language (繁中 / English / 日本語 / 简体中文, allowlisted) → `TRANSLATE_SUMMARY_PROMPT` / `translate_summary` translates that source's summary; result shown with a manual save-to-notes button. Route: `POST /notebooks/{id}/translate`.

### Tier 2 — new extraction paths (app-side, no inference change)

**Recommended implementation order:** start with `A6a` diagnostics so every later format can explain extraction quality; then ship `A6c` Q&A-style spreadsheets; then add `A6` Web URL with SSRF protection; then `A6b` PPTX Phase 1 text-first ingestion. `A8` OCR and `A6b` Phase 2 visual extraction should follow only when the extraction diagnostics and model/OCR capability are ready. `A9` vision remains customer-driven.

#### [ ] A6a · Ingestion diagnostics for source quality
- **What:** show what the app actually extracted before/after indexing: extracted character count, section/page/table counts when available, chunk count, OCR/fallback flags, warnings, failure reason, and a small extracted-text preview. This should be visible from the source row / preview drawer and included in admin troubleshooting surfaces.
- **Why first:** adding Web URL, PPTX, XLSX/CSV, and OCR support increases the chance of "indexed but useless" sources. Diagnostics make format support trustworthy and give users/admins a way to distinguish extraction failure from retrieval/answer failure.
- **Guardrails:** do not duplicate full source text into audit/governance logs; keep diagnostics scoped to the owning user's source and avoid exposing extracted snippets outside normal source permissions.

#### [ ] A6 · Web page as a source
- Paste URL → server-side fetch → readability extraction (`beautifulsoup4` already a dep) → existing chunk/embed pipeline. **Must add SSRF guards (block private IPs) and respect the customer's egress policy.**
- **SSRF guardrail for implementation:** only allow `http`/`https`; resolve DNS and block loopback/private/link-local/multicast/reserved IP ranges; re-check every redirect target; cap redirects, response size, and request timeout; restrict accepted content types; optionally support deployment allow/block lists; record URL/status/diagnostics without copying full fetched content into audit/governance logs.

#### [ ] A6b · PowerPoint decks as sources (.pptx)
- **Phase 1 — text-first ingestion:** extract slide titles, body text, tables, and speaker notes into slide-scoped sections (`slide N`, `slide N notes`, `slide N table K`) that flow through the existing chunk/embed pipeline. Keep slide order and location labels stable so citations can point back to a specific slide. Visual-only slide content should be surfaced in ingestion diagnostics as unsupported visual content.
- **Phase 2 — image understanding after OCR / vision support:** only revisit embedded images, screenshots, diagrams, and visual-only slides after A8 OCR and/or A9 vision support is available. OCR can extract text from screenshots; a vision-capable model is needed for chart/diagram/photo semantics. The resulting text should be stored as explicit sections such as `slide N image K OCR text` or `slide N image K visual description`, with diagnostics showing which method was used.
- **MVP guardrail:** do not block Phase 1 on image understanding; ship text-first PPTX support first, then add Phase 2 only when the required OCR/vision capability exists or a customer explicitly needs it.

#### [ ] A6c · Spreadsheet sources (.xlsx / .csv)
- Extract workbook/sheet metadata, detected header rows, bounded row groups, and compact table summaries into sheet/table-scoped sections. Chunk rows as structured records rather than flattening entire sheets into one blob; preserve sheet name, row range, and column names in chunk metadata/location.
- **MVP priority:** implement Q&A-style sheets first (`question` + `answer`, optional category/tags/keywords) because they are naturally aligned with RAG and require no numeric recomputation. Treat each Q&A pair as the minimum semantic unit.
- **MVP guardrail:** enforce row/column/file-size limits, skip or warn on formula-heavy/hidden/very wide sheets, and show a preview in ingestion diagnostics so users can see how tabular data was interpreted. Keep implementation details in [`SPREADSHEET_INGESTION.md`](SPREADSHEET_INGESTION.md).

#### [x] A7 · Subtitle files as sources (.srt / .vtt)
- **Done.** `_extract_subtitles` (`app/ingest.py`) strips cue indices, timestamp lines, the WebVTT header + NOTE/STYLE/REGION blocks, and inline VTT tags, and collapses rolling-caption repeats — leaving the spoken text as one `transcript` section that flows through the existing chunk/embed pipeline. `.srt`/`.vtt` added to `ALLOWED_EXTENSIONS` + the upload accept list. No new deps. Pairs naturally with A1 meeting minutes. Verified end-to-end (upload → indexed → clean transcript chunk).

#### [ ] A8 · OCR for scanned PDFs / images
- `pytesseract` + tesseract in the Docker image (`chi_tra` model for Traditional Chinese). Decades-old scanned research reports are likely in the customer corpus — high practical value, no LLM dependency.

### Tier 3 — low priority / customer-driven only

#### [ ] A9 · Image understanding (vision QA)
- **Low priority unless a customer explicitly needs it.** Blocked on whether the customer's Gemma 4 31B deployment accepts image input on `/v1/chat/completions`. If yes: image upload → vision description → description text joins RAG. If no: A8 OCR is the fallback.

#### [ ] A10 · Audio transcription (meeting recordings)
- **Low priority unless a customer explicitly needs it.** Needs a Whisper-class endpoint (customer serving has none). Local CPU whisper is slow. Mitigate with A7 (accept transcripts) until infrastructure exists.

#### [ ] A11 · Audio overview / TTS, mind map
- **Low priority unless a customer explicitly needs it.** TTS not available on the serving side; mind map needs a self-hosted render lib (markmap/mermaid — no-CDN rule). Nice-to-haves, not this phase.
