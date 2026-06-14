# Product roadmap — UX & AI features

UX improvements and new AI capabilities for the NotebookLM-style personal AI assistant, prioritised with effort and prerequisites. Same tick-off format as `PERFORMANCE.md` / `QUALITY.md`. Performance and retrieval-quality work stay in their own backlogs — this file is about **product surface**.

**Capability constraint:** the inference side is borrowed and fixed (Gemma 4 31B chat + multilingual-e5-large embeddings — **chat + embedding only**). Features needing only chat completions are cheap; new extraction paths (web, OCR) are app-side work; new model capabilities (vision, speech) must be verified against the customer endpoint first.

Status legend: `[ ]` todo · `[~]` in progress · `[x]` done

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

> **Design exploration — alternative Studio paradigms (beyond the NotebookLM tools+notes frame).** Recorded for future direction; not committed:
> 1. **Chat-centric commands** — dissolve the Studio panel; expose generators as `/`-commands (or a `+` menu) in the chat input, results appear inline as rich, saveable message cards. The right pane shrinks to a pure outputs/clipboard shelf. Best declutter; fits HTMX/server-render.
> 2. **Selection-driven inspector** — the right pane shows actions relevant to whatever is focused (a source → summarize/translate/minutes; an answer → follow-ups/pin; selected text → explain/find-related). Context-sensitive, not a static list.
> 3. **Report/compose builder** — the pane becomes a document the user assembles from briefing + comparisons + pinned answers + minutes, then edits and exports. Output-centric; fits the research-report use case.
> 4. **Proactive insights feed** — the app surfaces insights unprompted ("these 3 sources disagree on X", "new source contradicts a pinned note"); pull → push. Most "assistant"-like; higher LLM cost, needs eval.
> 5. **Spatial canvas** — draggable cards (sources/artifacts/notes) on a freeform board for synthesis. Most divergent, but heavy and fights the no-build/no-CDN constraint — likely too heavy for the POC.
>
> **Direction (future design reference only — NOT committed, NOT a decision).** For the "personal AI research assistant + produces research reports" positioning, a plausible long-term shape is: **near-term** — U16's tools-tiles + outputs-shelf as the base, *plus* folding in chat-centric `/`-commands (paradigm 1), since both declutter and fit the existing HTMX/server-render stack; **mid-term** — grow the outputs shelf toward a report/compose builder (paradigm 3), which best fits the long-report use case; **accent** — selectively adopt the selection-driven inspector (paradigm 2), e.g. highlight source text → explain; **far-term / data-gated** — proactive insights feed (paradigm 4) once compute headroom + an eval harness exist; spatial canvas (paradigm 5) stays out (violates no-build). These are directional notes to revisit, not scheduled work — only U16's checkboxes above are tracked items.

### Medium priority

#### [x] U6 · Upload feedback & batch size
- **Fix:** **Done.** The compact upload card shows selected files, file sizes, total size, configured batch limit, and a clearer over-limit message before upload.

#### [x] U7 · Answer action row — copy
- **Fix:** **Done.** Copy button on every assistant message (copies the raw Markdown; transient ✓ feedback). Regenerate / expand-all-citations remain todo.

#### [x] U8 · Editable notes
- **Done.** Each note in the shelf has an inline **編輯** toggle (Alpine) revealing a title + content (Markdown) form; saving `POST /notes/{id}/edit` updates in place and re-renders the shelf. Add/pin/delete unchanged.

#### [x] U9 · Global search
- **Fix:** **Done.** `/search` searches the signed-in user's notebooks, source filenames/summaries, conversation titles, and notes using scoped SQLite `LIKE` queries.

#### [ ] U10 · Mobile / responsive pass
- The three-pane workspace is desktop-first. Decide: support tablets/phones properly, or state desktop-only.

### Polish

#### [ ] U11 · Dark mode (CSS variables are ready)
#### [x] U12 · Onboarding empty state (3-step "upload → wait → ask" guide)
- **Fix:** **Done.** Empty chat state now shows a compact three-step upload → index → ask guide.
#### [ ] U13 · Accessibility pass (focus rings, aria labels, Esc to close modals)
#### [x] U14 · Friendlier error messages (no raw exception strings in chat)
- **Fix:** **Done.** Chat and Studio generation errors now show user-facing messages while raw provider/exception details stay in logs and metadata.
#### [ ] U15 · Proper i18n layer (low priority, but eventually required)
- U4 hardcoded zh-TW strings directly in templates / `app.js` / server messages — fine for the current single-language deployment, but adding any second language means re-touching every string. When needed: extract to a message catalog (Jinja i18n extension or a simple `messages.py` dict + a JS strings object), keyed lookups, language picked per deployment (config) or per user. Prerequisite for an English/zh-TW toggle; also centralizes the exported-Markdown headings (引用來源/筆記) that currently live in `main.py`.

#### [ ] U17 · Meaningful "focus" for source comparison (deferred feature)
- The compare tool used to expose a **聚焦重點 (focus)** free-text input. The value *was* passed to the prompt (`Focus: …` + "prioritise points relevant to it"), but because the comparison runs on each source's thin 2–4-sentence **summary**, the focus had little material to differentiate and the output barely changed — so the input was **removed from the UI** to avoid implying an effect it can't deliver. `POST /compare` still accepts an (empty) `focus` param, so re-enabling is a template-only change.
- **To make it meaningful when revisited:** on a non-empty focus, retrieve the **focus-relevant chunks** from each selected source (a small per-source retrieval) and compare on those instead of the summaries. Heavier (an extra retrieval step) but gives the focus real material to work with. Measure against a representative set first.

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

#### [ ] A6 · Web page as a source
- Paste URL → server-side fetch → readability extraction (`beautifulsoup4` already a dep) → existing chunk/embed pipeline. **Must add SSRF guards (block private IPs) and respect the customer's egress policy.**

#### [x] A7 · Subtitle files as sources (.srt / .vtt)
- **Done.** `_extract_subtitles` (`app/ingest.py`) strips cue indices, timestamp lines, the WebVTT header + NOTE/STYLE/REGION blocks, and inline VTT tags, and collapses rolling-caption repeats — leaving the spoken text as one `transcript` section that flows through the existing chunk/embed pipeline. `.srt`/`.vtt` added to `ALLOWED_EXTENSIONS` + the upload accept list. No new deps. Pairs naturally with A1 meeting minutes. Verified end-to-end (upload → indexed → clean transcript chunk).

#### [ ] A8 · OCR for scanned PDFs / images
- `pytesseract` + tesseract in the Docker image (`chi_tra` model for Traditional Chinese). Decades-old scanned research reports are likely in the customer corpus — high practical value, no LLM dependency.

### Tier 3 — verify infrastructure first

#### [ ] A9 · Image understanding (vision QA)
- **Blocked on:** whether the customer's Gemma 4 31B deployment accepts image input on `/v1/chat/completions`. If yes: image upload → vision description → description text joins RAG. If no: A8 OCR is the fallback.

#### [ ] A10 · Audio transcription (meeting recordings)
- Needs a Whisper-class endpoint (customer serving has none). Local CPU whisper is slow. Mitigate with A7 (accept transcripts) until infrastructure exists.

#### [ ] A11 · Audio overview / TTS, mind map
- TTS not available on the serving side; mind map needs a self-hosted render lib (markmap/mermaid — no-CDN rule). Nice-to-haves, not this phase.
