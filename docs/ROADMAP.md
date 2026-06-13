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

#### [ ] U3 · Citation → source highlight
- Clicking `[2]` should open the source preview at that chunk and highlight it (today: collapsible snippet only). The trust-building feature of NotebookLM.

#### [x] U4 · Traditional Chinese UI
- **Issue:** UI strings were English while the user base and content are Chinese.
- **Fix:** **Done.** Templates, JS strings (thinking bubble, confirms, loading texts), and user-facing server messages are now Traditional Chinese (POC: hardcoded zh-TW, no i18n framework).

#### [x] U5 · Conversation management
- **Fix:** **Done.** Conversation menu supports renaming the active conversation, shows message counts and relative update times, and keeps the active row visually distinct.

### Medium priority

#### [x] U6 · Upload feedback & batch size
- **Fix:** **Done.** The compact upload card shows selected files, file sizes, total size, configured batch limit, and a clearer over-limit message before upload.

#### [x] U7 · Answer action row — copy
- **Fix:** **Done.** Copy button on every assistant message (copies the raw Markdown; transient ✓ feedback). Regenerate / expand-all-citations remain todo.

#### [ ] U8 · Editable notes
- Notes are add/pin/delete only; pinned answers usually need trimming. Add inline edit.

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

---

## New AI features

### Tier 1 — chat-only, cheap, high value

#### [x] A1 · Meeting-minutes organizer
- **What:** pick an indexed source (transcript upload) → structured minutes (主題/決議/行動項目(負責人/期限)/待辦/未決事項) → saved as a note.
- **Fix:** **Done.** Studio card with source picker; `MEETING_MINUTES_PROMPT` (strong language rule); result lands in Notes.

#### [x] A2 · Follow-up question chips after each answer
- **What:** 2–3 suggested follow-ups under the latest assistant answer; click = ask.
- **Fix:** **Done.** Generated lazily after the answer renders (non-blocking separate request), cached in `messages.metadata_json.followups`, reuses the suggestion-chip fill+submit mechanism.

#### [x] A3 · Export to Markdown
- **What:** download a conversation (Q/A + citations) or all notes as `.md`.
- **Fix:** **Done.** Export buttons on the conversation menu and the Notes card; no LLM involved.

#### [ ] A4 · Study guide / FAQ / timeline artifacts
- More Studio cards generated from source summaries (siblings of briefing/compare). Each is a prompt + a card.

#### [ ] A5 · Explicit "translate this source's summary" action
- Language-rule prompts already make this implicitly possible; surface it as a button.

### Tier 2 — new extraction paths (app-side, no inference change)

#### [ ] A6 · Web page as a source
- Paste URL → server-side fetch → readability extraction (`beautifulsoup4` already a dep) → existing chunk/embed pipeline. **Must add SSRF guards (block private IPs) and respect the customer's egress policy.**

#### [ ] A7 · Subtitle files as sources (.srt / .vtt)
- Plain-text parse, no new deps; covers recorded-meeting transcripts from other tools.

#### [ ] A8 · OCR for scanned PDFs / images
- `pytesseract` + tesseract in the Docker image (`chi_tra` model for Traditional Chinese). Decades-old scanned research reports are likely in the customer corpus — high practical value, no LLM dependency.

### Tier 3 — verify infrastructure first

#### [ ] A9 · Image understanding (vision QA)
- **Blocked on:** whether the customer's Gemma 4 31B deployment accepts image input on `/v1/chat/completions`. If yes: image upload → vision description → description text joins RAG. If no: A8 OCR is the fallback.

#### [ ] A10 · Audio transcription (meeting recordings)
- Needs a Whisper-class endpoint (customer serving has none). Local CPU whisper is slow. Mitigate with A7 (accept transcripts) until infrastructure exists.

#### [ ] A11 · Audio overview / TTS, mind map
- TTS not available on the serving side; mind map needs a self-hosted render lib (markmap/mermaid — no-CDN rule). Nice-to-haves, not this phase.
