# Product roadmap

Product-facing roadmap for the NotebookLM-style personal AI assistant: UX, admin workflows, Eval Workbench, AI governance, source-format support, and new AI-assisted surfaces. Same tick-off format as `PERFORMANCE.md` / `QUALITY.md`.

Performance and retrieval-quality engineering stay in their own backlogs (`PERFORMANCE.md`, `QUALITY.md`). Security policy and dependency triage stay in `SECURITY.md`. This file tracks the product/admin capability surface and points to those deeper references when needed.

**Current target-deployment constraint:** the known customer inference side is borrowed and fixed (Gemma 4 31B chat + multilingual-e5-large embeddings — **chat + embedding only unless the active chat endpoint passes the image-understanding probe**). Features needing only chat completions are cheap; new extraction paths (web, PPTX, spreadsheets, OCR) are app-side work; new model capabilities (vision, speech) must be verified against the active customer endpoint first. `O1` adds admin capability probes so this assumption can be tested per deployment before enabling vision-dependent work.

Status legend: `[ ]` todo · `[~]` in progress · `[x]` done

---

## Recommended next round

1. **P0 index safety:** complete `O0`. The current Clear action leaves Chroma's collection dimension locked and can break every ingest after an embedding-dimension change. The temporary operator script is not the product fix.
2. **Enterprise authentication:** `I1a` trusted reverse-proxy header mode, `I1b` OIDC, and `I1d` operator diagnostics are implemented; answer customer-discovery questions for each deployment, then add `I1c` SAML only when a customer IdP requires it.
3. **Answer-quality loop:** `E1e-2` answer/citation judging is implemented (the measuring stick); next, implement `E2` notebook domain hints and answer policy and validate it through Eval Workbench comparisons (judged runs with/without hints).
4. **Admin LLM operations:** `O1` Phase 1 is done; next LLM-ops work is Phase 2 profile management and safe activation once needed.
5. **Format foundation:** `A6a` ingestion diagnostics is implemented — every new extractor reports its own signals through it.
6. **Source-format MVP path:** `A6c` spreadsheets and `A6b` PPTX Phase 1 are implemented; next is `A6` Web URL with SSRF guards.
7. **Image/OCR path:** `A8` OCR and `A9` image search v1 depend on extraction diagnostics and capability checks; block image uploads unless `/settings` image understanding succeeds or a non-LLM OCR-only path is explicitly enabled.
8. **Customer-driven later work:** keep `A10`/`A11` low priority unless a customer requirement or verified serving capability changes the economics.

---

## Enterprise authentication

### High priority

#### [~] I1 · Enterprise SSO / AD integration
- **Issue:** Customers may expect the app to integrate with corporate identity and, in Microsoft intranet environments, to support "automatic Windows account" login behavior. That behavior is typically Integrated Windows Authentication (Kerberos/SPNEGO, sometimes NTLM fallback) behind AD/Entra/ADFS infrastructure, but implementing raw Kerberos in the FastAPI app would make the product depend on deployment-specific SPNs, keytabs, DNS, browser policy, and domain-join details.
- **Target model:** support standard enterprise SSO at the app boundary. OIDC is the primary path; SAML is the compatibility path; trusted reverse-proxy header mode lets a customer-owned gateway/IIS/Apache/Nginx/identity proxy perform Kerberos/IWA/SAML/OIDC authentication and pass only a verified identity to the app. Local login remains available for break-glass admin unless explicitly disabled per deployment.
- **Authority:** detailed product direction, security rules, and customer discovery questions live in [`AUTHENTICATION.md`](AUTHENTICATION.md).
- **Guardrails:**
  - Do not treat AD integration as plain LDAP login; LDAP bind does not provide browser silent SSO.
  - Never trust identity headers from arbitrary clients. Header auth is valid only behind a controlled reverse proxy that strips inbound identity headers and sets verified values, and needs a concrete app-side trust check (localhost/internal-network binding **plus** a proxy shared secret or mTLS), not a topology assumption alone.
  - External identities must map to local users before data access so existing per-user/per-notebook authorization remains intact.
  - Group-to-admin mapping must be explicit and auditable.
  - Store the OIDC client secret with the existing `app/security.py` Fernet pattern (or env/`config.toml`-only at first); never in audit metadata. Rotating `NOTEBOOKLM_SECRET` invalidates DB-stored encrypted secrets.
  - Disable or clearly flag `/admin/users` password reset for SSO-provisioned accounts — a reset password silently re-opens local login around IdP deprovisioning.
  - Repo sync: external-identity persistence updates `SCHEMA.md`; new `/auth/*` routes go into `ROUTES.md`; login/SSO copy goes through the i18n catalog (`I18N.md`).
  - Keep local break-glass admin available unless a deployment explicitly opts out.
  - Document the MVP lifecycle limits (login-time-only group mapping, no server-side session revocation, no IdP logout) as known limitations in `AUTHENTICATION.md` — they are accepted POC trade-offs, not security promises.
- **Phased:**
  - [x] I1a — Customer discovery + trusted reverse-proxy header mode. **Done.** Deployment-disabled-by-default trusted header auth for customer-owned SSO gateways: configurable header names, proxy shared-secret check, external identity persistence (`provider + subject -> users.id`), local user auto-provisioning, group-to-role mapping at login, an optional source-IP allowlist (`trusted_header_allowed_ips`, defense-in-depth on top of the shared secret), audit events, and local-password reset/change blocking for SSO-linked accounts. This is the smallest piece that satisfies "automatic Windows account" login in every topology, including on-prem-AD-only deployments.
  - [x] I1b — OIDC. **Done.** Configurable issuer/discovery, client credentials, callback, token validation (`iss`/`aud`/`exp`/`nbf`/`iat`/`nonce`, JWKS signature verification), local user linking/provisioning by provider + `sub`, group-to-role mapping, local-password guardrail reuse, and audit events without tokens/codes. Requires an OIDC-capable IdP (Entra ID, ADFS, Keycloak). **Follow-up:** add PKCE — the app is a confidential client (state + nonce + client secret already cover authorization-code interception), so PKCE is defense-in-depth and OAuth 2.1 alignment, not a fix for an open hole.
  - [ ] I1c — SAML service-provider support when a customer IdP requires it: metadata, ACS endpoint, signed assertion validation, attribute mapping, group-to-role mapping, and audit events. Python SAML libraries pull in native `xmlsec` dependencies (a real Docker-image cost) — keep this strictly customer-driven.
  - [x] I1d — Admin/operator documentation and diagnostics. **Done.** `/admin/auth` shows current auth modes, static SSO configuration health checks, trusted-header/OIDC mapping summaries, and operator pointers to audit rejection reason codes. SSO-linked users now keep admin role authority with the IdP/proxy group mapping; local admin role toggles are blocked for external accounts.

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
- **Fix:** **Done.** Templates, JS strings (thinking bubble, confirms, loading texts), and user-facing server messages are Traditional Chinese. (Originally hardcoded zh-TW; since superseded by the U15a i18n catalog — copy now resolves through `t()`/`window.I18N`, see [`docs/I18N.md`](I18N.md).)

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
  - [x] Phase 2 — **Done.** Notes are now an **outputs shelf**: every entry carries a persisted type (`notes.kind`, allowlist `NOTE_KINDS` in `app/main.py`) rendered as a `.tag` badge, plus a client-side type filter (Alpine, `x-cloak`'d so a JS-less client just sees every entry). Saving from a tool passes its kind through `_save_note_button.html`; pinning writes `pinned` from its own route, and `SAVABLE_NOTE_KINDS` keeps a saved artifact from forging that badge. Pre-existing rows are classified once by `_backfill_note_kinds` (`app/db.py`) — exact for pinned via `source_message_id`, best-effort by the historical title prefixes for tool outputs. Exports carry the type label too. *(The other Phase 2 clauses — all generators saving here, inline edit — already landed in Phase 1 and U8; the type layer was the remaining gap.)*
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
- **Baseline done.** The app supports a usable narrow viewport without changing the desktop workspace model: the topbar/nav wraps cleanly, the three-pane workspace stacks at tablet widths, modals fit within mobile viewports, Eval tabs scroll instead of wrapping awkwardly, forms/actions collapse to full-width controls where needed, and table/card-heavy admin pages keep their existing `data-label` mobile presentation.
- **Follow-up done (2026-06-20, UX review H1):** mobile workspaces now show a segmented pane switcher (來源 / 對話 / 工作台, default 對話) at narrow widths. The desktop/tablet workspace model is unchanged, and no-JS fallback still shows the stacked panes.

### Polish

#### [x] U11 · Dark mode (CSS variables are ready)
- **Done.** Per-user theme (`users.theme`: `system` | `light` | `dark`) chosen on `/account` (`POST /account/theme`, allowlist `THEME_CHOICES` in `app/main.py`). `light`/`dark` render server-side as `<html data-theme>` so they work without JS; `system` is resolved before first paint by a synchronous inline script in `base.html` that also follows live OS changes.
- Implemented **purely as a token layer**: one `[data-theme="dark"]` block overrides the `:root` variables and no component rule branches on theme. Getting there meant tokenising the last hardcoded colours (`--on-accent`/`--on-danger`, `--accent-2`, `--accent-soft-2`, `--accent-wash`, `--glass`, `--scrim`, `--placeholder`, `--chip-neutral-*`, `--code-*`) — zero hardcoded hex remains outside the token blocks. Light-mode computed values are unchanged. The only theme-duplicated rule is the `select` chevron (a data-URI SVG whose stroke can't read a variable). Contract written up in [`UI.md`](UI.md) §1.1.
#### [x] U12 · Onboarding empty state (3-step "upload → wait → ask" guide)
- **Fix:** **Done.** Empty chat state now shows a compact three-step upload → index → ask guide.
#### [x] U13 · Accessibility pass (focus rings, aria labels, Esc to close modals)
- **Done.** Added skip navigation, active-nav `aria-current`, modal focus targets/return behavior for source/tool preview and audit metadata dialogs, explicit menu expanded state, Eval tab roles/selected state, accessible labels for compact chat actions, and reduced-motion handling. Existing focus-ring token remains the global focus treatment.
#### [x] U14 · Friendlier error messages (no raw exception strings in chat)
- **Fix:** **Done.** Chat and Studio generation errors now show user-facing messages while raw provider/exception details stay in logs and metadata.
#### [x] U15a · i18n foundation (deployment-level locale)
- U4 hardcoded zh-TW strings directly in templates / `app.js` / server messages — fine for the current single-language deployment, but every new feature adds more future extraction work. First pass should stay scoped: add a message catalog (`app/i18n.py` or equivalent), a template `t()` helper, a JS strings object emitted from `base.html`, and a config-driven locale (default `zh-TW`, optional `en`) via `app/config.py` / `config.example.toml` / `NOTEBOOKLM_UI_LANGUAGE`. Cover the high-churn surfaces first: nav, chat empty/stream states, Studio tool labels, user-facing server errors, and exported-Markdown headings (`引用來源` / `筆記`) that currently live in `main.py`.
- **Complete (2026-06-19).** Dependency-free catalog (`app/i18n.py`) + `t()` Jinja global + `window.I18N` (base.html) + `tr()` in `app.js` + `[ui].language` config (`NOTEBOOKLM_UI_LANGUAGE`, default `zh-TW`). Every user-facing and admin surface resolves copy through the catalog: nav, chat, Studio tools, server errors, upload widget + settings hints, the audit page, and the **whole Eval workbench** (landing, Profiles, eval-set detail / runs / compare / help + partials), plus status label maps (source / run / eval-result / sensitivity), global aria-labels (L3), and the settings bilingual label (L4). Hybrid eval naming: `評測` for the activity; `Eval Set`/`Eval run`/`Run`/`Baseline`/`Candidate` and Recall/MRR/Profile/chunk/embedding/RAG kept as domain terms. Only `zh-TW` is populated. **Full reference + how-to (add strings, add/switch locales, known exceptions): [`docs/I18N.md`](I18N.md).** Phase-by-phase landing log is in [`docs/UX_REVIEW.md`](UX_REVIEW.md).

#### [ ] U15b · `en` locale + admin system-locale and per-user locale controls
- U15a left the **catalog `zh-TW`-only** and the locale **deployment-level** (env/file, process-global). U15b makes language selectable in the product:
  - **Admin sets the system default locale from the UI** (System settings), persisted (not just `NOTEBOOKLM_UI_LANGUAGE`), applied to users who haven't chosen their own.
  - **Each user sets a personal locale** on their account page, applied to their own session and overriding the system default.
- Work required (full design in [`docs/I18N.md`](I18N.md) "Planned" section):
  1. Populate an actual **`en`** catalog (foundation ships only `zh-TW`).
  2. Make the **status label maps locale-aware** (`SENSITIVITY_LABELS` / `RUN_STATUS_LABELS` / `EVAL_RESULT_STATUS_LABELS` / `source_status_labels` are single-locale today).
  3. **Persistence**: per-user `locale` column on `users` + a system-locale setting (schema change → update [`SCHEMA.md`](SCHEMA.md)). Resolution: user → system → `DEFAULT_LOCALE`.
  4. **Request-scoped resolution**: `t()` reads global `config.ui.language` today; per-user needs locale resolved per request (e.g. a `contextvar` from the signed-in user) so `t()`/`js_messages()` use the request's locale.
  5. **UI affordances**: language control in System settings (admin) and the account page (per-user).
  6. **Tests** across HTMX partials (each fragment localises under the request's locale).
  7. Extract the documented inline exceptions in [`docs/I18N.md`](I18N.md) if they must translate.
- Keep LLM prompt-language rules separate from UI i18n; prompts control model output language and are not display copy.

#### [ ] U17 · Meaningful "focus" for source comparison (deferred feature)
- The compare tool used to expose a **聚焦重點 (focus)** free-text input. The value *was* passed to the prompt (`Focus: …` + "prioritise points relevant to it"), but because the comparison runs on each source's thin 2–4-sentence **summary**, the focus had little material to differentiate and the output barely changed — so the input was **removed from the UI** to avoid implying an effect it can't deliver. `POST /compare` still accepts an (empty) `focus` param, so re-enabling is a template-only change.
- **Future direction: topic-focused source comparison.** Re-introduce focus as a **topic** field, not a cosmetic prompt hint. On a non-empty topic, run a small source-scoped retrieval for each selected source, collect the topic-relevant chunks from each source, then compare on those chunks instead of the thin source summaries. The report should show Shared / Distinct / Contradictions plus which sources had weak or no topic evidence. This fits the current vector RAG design; the key change is using per-source retrieval before comparison. Measure against a representative set first.
- **Later extension:** section-focused comparison can follow once ingestion preserves stronger section metadata (DOCX/HTML headings, PPTX slide titles, PDF page ranges, spreadsheet sheet/table names). Until then, topic retrieval is the safer v1.

#### [ ] U18 · Timestamps in the viewer's local time
- **Issue:** every stored timestamp is UTC (SQLite `CURRENT_TIMESTAMP`) and is rendered **raw**, so a zh-TW user sees times 8 hours behind their clock. It was invisible while surfaces showed dates only; it became visible once the outputs shelf started showing minutes (2026-07-25) and it will get worse as more surfaces show times.
- **Affected surfaces today:** outputs shelf (`_notes_section.html`), audit log (`admin_audit.html` — date + time), eval run/set/list pages (`admin_eval_run.html`, `admin_eval_set.html`, `admin_evals.html` — full stamps), notebook cards (`home.html`), user admin (`admin_users.html`). `relative_time()` in `app/main.py` is **already correct** (it parses as UTC and computes a delta), so anything using it is unaffected — that is also the cheapest interim mitigation for a surface that only needs recency.
- **Options:**
  - **(a) Client-side conversion (recommended).** Emit machine-readable UTC in a `datetime` attribute (`<time datetime="2026-07-25T14:35:54Z">`) and let a small `app.js` pass rewrite the text via `toLocaleString()`. No schema, no config, correct for every viewer including DST, and one place to change. Degrades to the raw UTC stamp without JS — so keep the visible fallback explicitly labelled UTC.
  - **(b) Per-user timezone setting.** A `users.timezone` column plus an account-page control, resolved per request. `users.theme` (U11) is the working precedent for the persistence + account-control shape, and this pairs naturally with `U15b`'s per-user locale. Costs a schema change and request-scoped resolution; only worth it if server-rendered output must be correct without JS (e.g. exports).
  - **(c) Deployment-level timezone config.** One `[ui].timezone` value. Cheapest, wrong for any multi-region deployment; acceptable only for a single-office install.
- **Note:** exported Markdown (notes/chat) also carries raw stamps — decide whether exports follow the viewer's zone or stay UTC (UTC is defensible for an archival artifact, but say so in the file).

---

## Admin evaluation workbench

### High priority — unlocks blocked retrieval / tuning work

#### [~] E1 · In-deployment eval workbench (private customer data stays in place)
- **Issue:** Several quality/performance items are blocked on representative customer-style eval data (`QUALITY.md` Q0-2 / Q1-1 / Q1-2 / Q1-4, `PERFORMANCE.md` P3-2). Customer source data may be unable to leave the deployment, so the app needs an admin-only way to build and run evals **inside** the customer's environment.
- **Target model:** an admin creates eval sets from already-indexed DB data, runs retrieval/profile experiments with visible progress, compares every run historically, applies/rolls back approved runtime-safe parameters, and exports sanitized settings/reports for the implementation team.
- **Guardrails:**
  - Generated candidate questions are suggestions only; admins must review/approve/edit before they become ground truth.
  - Each run stores immutable snapshots: eval set version, active/candidate profile, LLM setting summary, app version/commit when available, aggregate metrics, and per-question results.
  - Runtime-safe parameters can be applied immediately; index-affecting parameters (chunk sizes, overlap, embedding model/prefix/dimension) must be shown with strong Reindex warnings and should not be silently applied. Dimension changes must stay blocked or use the temporary O0 migration tool until the permanent collection-reset workflow lands.
  - Export has two modes: sanitized profile/report (settings + aggregate metrics, no source text) and full internal report (questions, expected evidence, failures; for in-environment or explicitly approved sharing only). Full internal report exports must be recorded in the durable audit trail.
- **Phased (tick as done):**
  - [x] E1a — **Done.** Schema + admin shell landed: `eval_sets`, `eval_items`, `eval_runs`, `eval_results`, `retrieval_profiles`; `/admin/evals` shows the active baseline profile, eval sets, and historical run list. This creates the audit trail before any tuning UI exists.
  - [x] E1b — **Done.** Eval-set builder + retrieval-only runner landed: admins can add approved questions against existing notebooks/sources, generate draft candidates from indexed chunks, approve/delete items without losing scroll position, delete eval sets, queue background runs, watch progress (`queued/running/succeeded/failed`, current item/total/current step), and inspect persisted Recall@k, MRR, top score, low-confidence rate, latency, error counts, expected evidence, compact retrieved snippets, miss diagnosis, and per-question hit/miss/unscored/error status. LLM-assisted authoring/judging remains E1e.
  - [x] E1c — **Done.** Profile comparison + apply/rollback landed. A runtime active-params layer (`ACTIVE_RETRIEVAL_PARAMS` in `app/retrieval.py`) now backs the retrieval path: admins author candidate profiles (7 runtime-safe params), run an eval set against any profile via an isolated per-run override (the runner applies the run's frozen snapshot, not live config), compare two succeeded runs of the same set (param diff + metric diff + per-question improved/regressed), then **apply** a profile to live chat retrieval (persisted via `retrieval_profiles.is_active`, reloaded on startup) or **roll back** by applying a previous profile. Index-affecting profiles (`requires_reindex = 1`) are refused at apply. Routes: `/admin/evals/profiles` (create/delete/apply), `/admin/evals/compare`.
  - [x] E1d — **Done.** Export + audit foundation landed. Retrieval profiles can be exported as sanitized JSON, eval runs can be exported as sanitized JSON (no questions/evidence/retrieved snippets) or full internal JSON (questions, expected evidence, diagnostics, retrieved snippets) gated by explicit confirmation. A durable `audit_events` table and `/admin/audit` viewer now record export events plus high-risk admin actions: retrieval profile create/apply/delete/export, LLM settings updates, index Clear/Rebuild, user-management changes, and notebook/source/chat/note lifecycle or Markdown-export actions.
  - [x] E1e-1 — **Done.** LLM-assisted eval authoring landed as a draft-only flow: admins can generate candidate questions from selected indexed sources, request answerable / cross-lingual / unanswerable item types, review item type, reference answer, expected substrings, and source/chunk grounding, then approve manually. Generated metadata records compact origin/model/prompt-version/source ids without copying prompts or source text. The deterministic chunk-based generator remains available as a no-LLM fallback.
  - [x] E1e-2 — **Done.** Answer-quality and citation judging (route A, comparative/structured). Opt-in per run (`judge_enabled`, default off, ~2× LLM cost): each item runs a deterministic abstain decision, then — unless it abstained — generates an answer and LLM-judges answer quality, groundedness, and citation correctness; abstain correctness and a substring-hit-rate anchor are computed deterministically, not by the judge. Judge metrics are stored and shown **separately** from retrieval Recall/MRR (nested `metrics_json.judge`) and labelled a reference signal, not ground truth. A single item's generate/judge failure is isolated (`answer_outcome=error`) and never fails the run; `judge_enabled=0` fully regresses to retrieval-only behaviour. Generated answers + judge rationale + unsupported-claim text appear **only in full internal exports** (audited); the sanitized report carries aggregate judge numbers only. Telemetry separates `eval_answer` / `eval_judge` call types. **Implementation plan:** [`E1E2_ANSWER_JUDGING_PLAN.md`](E1E2_ANSWER_JUDGING_PLAN.md).
  - [x] E1f — **Done.** Eval tuning guide landed as `/admin/evals/help` and as a first-class tab in the Eval workbench. It converts the internal tuning PDF/discussion into HTML covering: when to tune parameters vs fix Eval items, symptom -> likely cause -> parameter guidance, profile experiment workflow, starter profiles, non-runtime-safe changes that require reindex, and the role of future domain hints / answer policy. The PDF remains optional/shareable, but the product source of truth is now HTML so labels stay aligned with the live profile UI.
- **Recommended next implementation round:** fix **O0** first because dimension migration can break all ingestion. With **E1e-2** providing the answer-quality measuring stick, then prioritize **E2** (notebook domain hints + answer policy) and validate it via judged with/without-hints run comparisons. Further audit expansion should wait for customer requirements, e.g. explicit read-access audit for source preview/result viewing. Defer index-affecting parameter application until O0 provides a safe reset/Reindex workflow and clear UX.

#### [ ] E2 · Notebook domain hints and answer policy — high priority for answer quality
- **Issue:** Some "inaccurate" answers are not fixed by retrieval-weight tuning alone. Domain-specific aliases, abbreviations, internal product names, and deployment-specific answer rules may need to be available at the notebook level so query rewrite can find the right evidence and final answers follow the customer's rules.
- **Target model:** each notebook can carry bounded, structured **domain hints** (term/synonyms/definition/query expansion/answer note) plus a concise **answer policy**. Hints improve retrieval/query rewrite; policy controls final answer behavior. Neither should become an unbounded extra knowledge base.
- **Default spreadsheet-aggregation policy (transition guard until `A6d`):** the baseline answer policy should state that spreadsheet sources support row lookup and semantic search but not reliable counting/summing/ranking — aggregate questions over sheet sources get an explicit caveat instead of a confidently computed number. Ships alongside `A6c` records support; per-notebook removable once `A6d` exists.
- **Guardrails:**
  - Keep hints structured and size-limited; avoid one giant free-form prompt pasted into every LLM call.
  - Query expansion fields can influence rewrite/retrieval; answer policy fields can influence final answer wording. Do not mix the two blindly.
  - Treat hints/policy as potentially sensitive. Sanitized exports and audit metadata should store identifiers/counts/summaries, not full prompt or proprietary keyword text.
  - Validate with the Eval Workbench: compare the same Eval Set with and without hints, and verify improvements do not come from false-positive evidence matches.
- **Phased:**
  - [ ] E2a — Schema + notebook admin/editor UI for domain hints and answer policy.
  - [ ] E2b — Feed domain hints into query rewrite / retrieval expansion with explicit limits.
  - [ ] E2c — Feed answer policy into final answer prompting without treating it as source evidence.
    - **Carried in from E1e-2 (deliberately deferred, 2026-07-25):** replace the literal refusal sentence in `SYSTEM_PROMPT` with a **structural refusal marker** the app renders as localized copy (`i18n.t("chat.abstain")`), in both the streaming and non-streaming answer paths.
      - **Why it belongs here:** answer policy is specified to influence *final answer wording* (see Guardrails above). Once per-notebook policy can reword or wrap a refusal, E1e-2's current exact-sentence detection (`REFUSAL_MARKERS` in `app/llm.py`) degrades to under-reporting. A structural marker is orthogonal to style rules and survives that.
      - **Why it was not done in E1e-2:** it changes the live chat answer path (streaming must buffer the first ~16 chars to decide before emitting, and the marker must never reach the user or the stored message) — disproportionate blast radius for a measurement fix, and it would be reworked by E2c anyway.
      - **It also fixes a live bug:** a Traditional Chinese question currently gets an **English** refusal, because the model copies the literal English sentence in `SYSTEM_PROMPT` line 4 over the "reply in the user's language" rule one line above. The score-gated path is already localized, so the two refusal paths are inconsistent today.
      - **Restart condition / guard:** `tests/test_llm.py::test_refusal_markers_stay_pinned_to_system_prompt` fails the moment the prompt's refusal wording changes, forcing detection and prompt to be updated together. Do this work when starting E2c, or sooner if a customer reports the English-refusal bug.
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

### Critical priority — vector-index safety

#### [~] O0 · Reset Chroma collection dimension safely
- **Live P0 defect:** `/admin/index` Clear calls `collection.delete(ids=...)`. Chroma keeps the collection schema after the last vector is deleted, so an empty collection can remain locked to (for example) 1024 dimensions. `probe_index_dimension()` sees no stored embedding and reports `None`; settings then accept a 1536-dimensional model, but the first query/upsert fails with `Collection expecting embedding with dimension of 1024, got 1536`.
- **Operational impact:** ingestion fails after the embedding model changes; repeated Clear/Rebuild does not repair it. In split app/worker deployments, both processes also cache collection handles, so replacing the collection in only the web process is insufficient.
- **Temporary mitigation landed:** `scripts/reset_chroma_dimension.py` provides dry-run classification, requires stopped services for apply, backs up SQLite + Chroma, replaces the collection, restores only safe target-dimension vectors, and marks old-dimension indexed sources for Reindex. This is an operator workaround, not completion of O0.
- **Admin copy corrected (interim):** `/admin/index` no longer presents Clear/Rebuild as a dimension-migration path. The empty-collection hint no longer claims the dimension is unlocked, the Clear confirm dialog carries the lock warning, the post-Clear flash separates same-model rebuild from a dimension change, and a dedicated section routes dimension changes to the workaround script. Guarded by `test_admin_index_page_warns_clear_does_not_reset_dimension`. Criterion 5 still needs the *post-fix* rewrite plus audit metadata.
- **Permanent-fix acceptance criteria:**
  1. Clear used for a dimension migration deletes/recreates the collection schema, not only its records.
  2. App and dedicated worker cannot keep stale cached collection handles after reset; concurrent ingest is blocked, drained, or safely retried.
  3. Startup sync cannot silently reinsert old-dimension SQLite embeddings and re-lock the new collection; affected sources receive an explicit Reindex state/path.
  4. Regression coverage proves `384 → Clear/migrate → 1536` succeeds in both inline-worker and split-worker operating models.
  5. Admin copy, audit metadata, README, and operator docs describe the same safe migration and rollback behavior; remove the temporary warning only after these checks pass.

### Medium priority — safer LLM configuration and deployment flexibility

#### [ ] O1 · Admin-only LLM settings diagnostics and profiles
- **Issue:** The current `/settings` page stores one global LLM configuration (`llm_settings`, `id = 1`). It probes embedding dimension on save, but admins cannot test chat connectivity separately, keep multiple candidate configurations, or switch between known-good endpoints safely.
- **Target model:** only admins manage LLM settings. Do not expose LLM profile selection or editing to normal users in this phase. Admins can test, save, compare, and activate configurations while the app protects existing indexes from incompatible embedding changes.
- **Phase 1 — diagnostics before profile management:**
  - [x] O1a — **Done.** `/settings` now has separate admin-only "Test chat model" and "Test embedding model" actions. Results persist as compact `llm_settings.diagnostics_json` metadata with status, latency, provider/model/deployment summary, embedding dimension, and last-tested timestamp. Raw prompts, outputs, API keys, and raw provider payloads are not stored; audit/governance rows keep compact status/error-class metadata only.
  - [x] O1b — **Done.** The settings diagnostics section probes streaming support, provider usage reporting, JSON-following sanity, and optional image understanding. The image-understanding checkbox is off by default and sends only a tiny built-in test image when explicitly enabled. The result records capability/status only and does not enable A9 automatically.
- **Phase 2 — multiple profiles + safe activation:**
  - [ ] O1c — Replace the single global settings row with admin-managed LLM profiles: name, provider, base URLs, encrypted API key, chat model, embedding model/prefixes, temperature, timeout, last test status, and active flag. Migrate the existing `llm_settings` row into the default active profile.
  - [ ] O1d — Add safe profile activation rules. Chat-only changes can activate directly after a successful chat test. Embedding-affecting changes (model/base URL/prefix/dimension) must be blocked or strongly gated when the existing Chroma index dimension/config is incompatible. Do not direct admins to Clear/Rebuild for dimension migration until O0 is complete; use the temporary O0 workaround and explicit Reindex guidance.
- **Future phase — task-specific routing:**
  - [ ] O1e — Allow admins to assign different profiles to answer generation, embeddings, eval judging, eval authoring, source summaries, Studio artifacts, and low-cost follow-up/starter questions. Keep this out of the MVP until global profile switching is stable.
- **Guardrails:**
  - Keep all profile management admin-only.
  - Never expose stored API keys back to the browser.
  - Record profile create/update/test/activate/delete actions in audit metadata without storing secrets, prompts, outputs, or full endpoint payloads.
  - Treat multimodal probing as capability detection only; the image-understanding probe does not enable image upload by itself. `A9` must explicitly gate upload/indexing on the latest successful probe result and show an admin-facing remediation path when the probe is missing or failed.

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

**Recommended implementation order:** `A6a` diagnostics, `A6c` spreadsheets, and `A6b` PPTX Phase 1 are **done** — a new extractor should add its own signals to `ExtractionResult.notes` / `details` (and a `_section_kind` branch when it introduces a genuinely different register) rather than failing silently. Next up is `A6` Web URL with SSRF protection. `A8` OCR and `A9` image search v1 should follow only when extraction diagnostics and model/OCR capability are ready. `A6b` Phase 2 visual extraction can reuse the same image-caption/OCR path once proven.

#### [x] A6a · Ingestion diagnostics for source quality
- **What:** show what the app actually extracted before/after indexing: extracted character count, section/page/table counts when available, chunk count, OCR/fallback flags, warnings, failure reason, and a small extracted-text preview, visible from the source row / preview drawer.
- **Why first:** adding Web URL, PPTX, XLSX/CSV, and OCR support increases the chance of "indexed but useless" sources. Diagnostics make format support trustworthy and give users a way to distinguish extraction failure from retrieval/answer failure.
- **Done.** `sources.diagnostics_json` is written by `process_source` on every (re)ingest and replaced, never appended. `extract_sections` now returns an `ExtractionResult` (sections + which extractor path ran + notes) so extractor-internal facts — above all a PDF falling back from pdfplumber to plain pypdf — are recordable; `collect_ingest_diagnostics` derives the counts, `_section_kind` breakdown, and warnings from data the pipeline already had. Four warnings, each stating the consequence: **low_text** (scanned/image-only — the A8 OCR signal), **pdf_structure_fallback** (citations degrade to page level), **chunk_over_token_budget** (silent embedding truncation — see below), **empty_sections**. Failures record `failed_stage` (`extract`/`chunk`/`embed`/`store`) so "the file had no text" reads differently from "the endpoint was down", and **failed sources can now open the preview drawer** — previously the button was disabled, leaving their diagnostics unreachable. Thresholds live in `[diagnostics]` (`app/config.py`); they only affect display, so tuning them never requires re-indexing.
- **`chunk_over_token_budget` is an estimate, not a measurement** (`estimate_embedding_tokens`): CJK-heavy text is charged ~1 token/char, Latin ~4 chars/token, reusing the chunker's `is_mostly_cjk` split. It exists to make [`QUALITY.md` Q0-5](QUALITY.md) observable per-source in a real deployment without shipping a tokenizer; ground truth stays `tests/inspect_e5_chunk_tokens.py`.
- **Guardrails:** do not duplicate full source text into audit/governance logs; keep diagnostics scoped to the owning user's source and avoid exposing extracted snippets outside normal source permissions. *(The original entry also asked for diagnostics "in admin troubleshooting surfaces" — that clause contradicted this guardrail, since the useful part is the extracted snippet. Scope here is deliberately the owning user only; the admin half is split out as `A6e` below.)*

#### [ ] A6e · Aggregate ingestion health for admins
- **What:** a deployment-level view of ingestion quality — how many sources carry each warning code, failure counts by `failed_stage`, and which formats/extractor paths fail most. **Aggregates only: no filenames, no extracted text, no cross-user source listing**, which is what keeps it compatible with A6a's guardrail.
- **Why deferred (2026-07-25):** with six mature formats and no cross-user browsing today, the charts would be empty. The value appears once `A6b`/`A6c` add formats whose extraction quality actually varies.
- **Restart condition:** revisit after `A6b` **and** `A6c` have landed, or earlier if an operator asks "is ingestion healthy across this deployment?" and the per-source drawer can't answer it. If a future need requires per-source admin access to another user's extracted text, that is a **separate privacy decision** — it needs an explicit policy call plus an audit event, not a quiet extension of this item.

#### [ ] A6 · Web page as a source
- Paste URL → server-side fetch → readability extraction (`beautifulsoup4` already a dep) → existing chunk/embed pipeline. **Must add SSRF guards (block private IPs) and respect the customer's egress policy.**
- **SSRF guardrail for implementation:** only allow `http`/`https`; resolve DNS and block loopback/private/link-local/multicast/reserved IP ranges; re-check every redirect target; cap redirects, response size, and request timeout; restrict accepted content types; optionally support deployment allow/block lists; record URL/status/diagnostics without copying full fetched content into audit/governance logs.

#### [~] A6b · PowerPoint decks as sources (.pptx)
- **Phase 1 done.** `_extract_pptx` (`app/ingest.py`) emits `slide N` (title first, then body text), `slide N table K`, and `slide N notes`, in that per-slide order — PPTX shape order does not follow visual reading order, so a predictable order beats a fake one. Sections flow through the normal chunker, so short slides pack together and citations become spans (`slide 1 – slide 2`) rather than one tiny vector per slide.
  - **Grouped shapes are walked recursively** (`_iter_pptx_shapes`). This is the PPTX form of the nested-table bug: a flat `slide.shapes` walk finds *nothing* on a slide whose author grouped its text boxes, and the slide would index as empty. Covered by a test that asserts the flat walk would miss it.
  - **Speaker notes get their own `_section_kind` (`slide_notes`)** so the chunker never merges presenter cues into slide-body chunks — they are a different register, and a citation should say which one an answer came from. Found by inspecting real output, not by spec.
  - **Visual-only slides are reported, not silently empty:** pictures/charts/SmartArt increment `slides_without_text`, raising the `pptx_visual_only_slides` A6a warning that names OCR/vision as the missing capability. A slide holding only a table does **not** count as visual-only.
  - Diagnostics also carry slide/table/notes/image counts. Dependency: `python-pptx` (pulls Pillow + XlsxWriter transitively).
- **Phase 1 — text-first ingestion (as specified):** extract slide titles, body text, tables, and speaker notes into slide-scoped sections (`slide N`, `slide N notes`, `slide N table K`) that flow through the existing chunk/embed pipeline. Keep slide order and location labels stable so citations can point back to a specific slide. Visual-only slide content should be surfaced in ingestion diagnostics as unsupported visual content.
- **Phase 2 — image understanding after OCR / vision support:** only revisit embedded images, screenshots, diagrams, and visual-only slides after A8 OCR and/or A9 vision support is available. OCR can extract text from screenshots; a vision-capable model is needed for chart/diagram/photo semantics. The resulting text should be stored as explicit sections such as `slide N image K OCR text` or `slide N image K visual description`, with diagnostics showing which method was used.
- **MVP guardrail:** do not block Phase 1 on image understanding; ship text-first PPTX support first, then add Phase 2 only when the required OCR/vision capability exists or a customer explicitly needs it.

#### [x] A6c · Spreadsheet sources (.xlsx / .csv)
- **Done (MVP).** `.xlsx` / `.csv` ingest through a single access helper (`_read_xlsx_sheets` / `_read_csv_sheet` in `app/ingest.py` — the documented python-calamine swap-point). Q&A sheets are detected by configurable column synonyms (`[spreadsheet].qa_question_synonyms` / `qa_answer_synonyms`, so 客戶提問 / 回覆內容 resolve) plus the headerless two-column case, which is ingested as Q&A **and flagged as auto-detected**. Everything else falls back to bounded generic-record chunks and says so via the `spreadsheet_generic_records` warning, which also states the MVP's limit (semantic lookup yes; exact filtering/counting/aggregation is `A6d`). Header inference is deliberately conservative — sentence-shaped cells and rows that aren't terser than the data below are treated as data, since mislabelling a row as a header silently deletes a record.
- **Chunking:** one chunk per Q&A row; records use token-aware adaptive packing against `[spreadsheet].embed_token_budget`, and a single over-budget row splits into column-group parts that each repeat the identifier column (`sheet "X" row 7 part 2/3`). Spreadsheet extraction returns `ExtractionResult.pre_chunked=True` so `chunk_sections` does **not** re-pack rows — sentence packing across rows would glue unrelated records together and destroy the `sheet "X" row N` citation labels.
- **Diagnostics (A6a):** per-sheet detected type, header decision, row/column/chunk counts, skipped hidden sheets, row/column truncation, uncached-formula count (a second bounded pass, run only when the first pass saw blanks — `data_only=True` makes uncached formulas read as empty), wide-sheet warning, and for CSV the encoding decision (BOM → strict UTF-8 → `charset-normalizer`, Big5/CP950 expected) with replacement-character count plus the sniffed delimiter.
- **Deferred to their own items:** records/metrics *detection* rules beyond the generic fallback, and the structured table-query tool (`A6d`). The sheet-type taxonomy in [`SPREADSHEET_INGESTION.md`](SPREADSHEET_INGESTION.md) stays the reference.
- **As-specified scope, kept for reference (all of it shipped):**
- Extract workbook/sheet metadata, detected header rows, bounded row groups, and compact table summaries into sheet/table-scoped sections. Chunk rows as structured records rather than flattening entire sheets into one blob; preserve sheet name, row range, and column names in chunk metadata/location.
- **MVP priority:** implement Q&A-style sheets first (`question` + `answer`, optional category/tags/keywords) because they are naturally aligned with RAG and require no numeric recomputation. Treat each Q&A pair as the minimum semantic unit.
- **MVP scope:** Q&A detection only; every other sheet shape falls back to bounded generic-records chunking, stated in diagnostics. General records and numeric reports are the likely next formats (high enterprise demand) but ship as their own items — keep the sheet-type taxonomy in `SPREADSHEET_INGESTION.md` as the reference.
- **MVP guardrail:** enforce row/column/file-size caps via a `[spreadsheet]` config group (`app/config.py` convention; chunk-shaping values require re-indexing). Skip hidden/very-hidden sheets by default and record them in diagnostics. Detect CSV encoding (BOM → UTF-8 → `charset-normalizer` fallback; Big5/CP950 expected in zh-TW corpora) and surface the decision plus replacement-character count. Warn on formula-heavy/very-wide sheets and on chunks exceeding the embedding-token window, and show a preview in ingestion diagnostics so users can see how tabular data was interpreted. Parser: `openpyxl` read-only + stdlib `csv` behind a single extraction helper (documented `python-calamine` swap-point). Full details in [`SPREADSHEET_INGESTION.md`](SPREADSHEET_INGESTION.md).
- **Embedding-text shape (decided):** one trimmed-preamble text per chunk — semantic context (sheet/category/question/columns) in the embedded text, constant fields (workbook filename, row numbers, detection notes) in chunk metadata. Record chunks use token-aware adaptive row packing with identifier-repeating splits for over-budget rows, so e5's silent 512-token truncation cannot hide indexed-but-unsearchable rows. Details and escalation path in [`SPREADSHEET_INGESTION.md`](SPREADSHEET_INGESTION.md).

#### [ ] A6d · Structured table-query tool for spreadsheet sources (customer-driven)
- **What:** exact filtering, counting, sorting, aggregation, and joins over ingested spreadsheet data — the other half of the spreadsheet division of labor (see `SPREADSHEET_INGESTION.md` "Division of labor"). RAG records chunks (`A6c`) handle point lookups and semantic/free-text search; this tool handles the COUNT/SUM/rank/filter questions that top-k vector retrieval structurally cannot answer completely and LLM arithmetic cannot answer reliably.
- **Storage:** normalize sheet rows into SQLite at ingest time (per-source rows keyed by source/sheet/row; `A6c` chunk/source metadata already records columns and row ranges). Reuses the existing DB — no new infrastructure.
- **Query interface:** the LLM emits a constrained filter/aggregate spec (a small JSON DSL) that the app validates and executes; never interpolate model output into SQL strings.
- **Interaction (recommended):** routed, not always-on. Deterministic pre-filter first (notebook has no table sources → pure RAG, zero added cost), then a one-shot route decision (`rag | table | both`) folded into the existing query-rewrite LLM call; `both` merges results at context assembly, not at ranking. Table-path failures (invalid spec, missing table, unexpected empty result) are detectable and fall back to RAG with an explicit caveat. Requires the chat model to pass the JSON-following diagnostic (O1 probe pattern).
- **Presentation:** table results need their own citation shape (workbook · sheet · matched-row count) linking back to the source preview.
- **Guardrails:** preserve per-user/per-notebook authorization on every query; cap result rows; audit query specs without logging cell contents; add a routing-accuracy eval type before trusting the router (the Eval Workbench currently measures retrieval only).
- **Why customer-driven:** a big lift (structured storage, query DSL, routing, result UX, new eval type); the transition risk is mitigated by the `E2` default spreadsheet-aggregation policy.

#### [x] A7 · Subtitle files as sources (.srt / .vtt)
- **Done.** `_extract_subtitles` (`app/ingest.py`) strips cue indices, timestamp lines, the WebVTT header + NOTE/STYLE/REGION blocks, and inline VTT tags, and collapses rolling-caption repeats — leaving the spoken text as one `transcript` section that flows through the existing chunk/embed pipeline. `.srt`/`.vtt` added to `ALLOWED_EXTENSIONS` + the upload accept list. No new deps. Pairs naturally with A1 meeting minutes. Verified end-to-end (upload → indexed → clean transcript chunk).

#### [ ] A8 · OCR for scanned PDFs / images
- `pytesseract` + tesseract in the Docker image (`chi_tra` model for Traditional Chinese). Decades-old scanned research reports are likely in the customer corpus — high practical value, no LLM dependency.

#### [ ] A9 · Image search v1 (OCR + vision captions + text embedding)
- **What:** accept image sources (`.png`, `.jpg`, `.jpeg`, `.webp` initially) and make them searchable through the existing text RAG stack. The first version does **not** require a true multimodal embedding model: it extracts OCR text when available, asks the active chat model for a concise visual caption / diagram description when image understanding is supported, embeds the combined text with the configured embedding model (e.g. e5), and returns the original image as the cited result.
- **Capability gate:** image upload must be blocked unless the latest `/settings` chat diagnostics include a successful `image_understanding` probe for the active chat configuration, or the deployment explicitly enables an OCR-only image mode. If the probe is missing, skipped, stale, or failed, the upload form should reject image files with a clear message asking an admin to run the image-understanding diagnostic or disable image-source support. Do not silently accept images that would index as empty text.
- **Indexing model:** store the original image under `data/uploads/` like other sources, generate a bounded preview/thumbnail for display, then create labeled text sections such as:
  - `OCR text:` text detected by A8 or another configured OCR engine;
  - `Visual description:` Gemma/vision-model caption of visible content, chart/diagram semantics, notable labels, and likely document role;
  - `Image metadata:` filename, dimensions, MIME type, and safety/diagnostic warnings.
- **Search behavior:** use the existing text embedding and retrieval path for `ocr_text + visual_caption`. This supports text-to-image search such as "找出登入流程圖", "哪張截圖提到向量資料庫", or "找含有某段文字的掃描圖". True image-to-image similarity and visual-style matching require a future multimodal embedding collection and are out of v1 scope.
- **Presentation:** image sources should appear in the left Sources pane with a compact thumbnail/format badge and normal indexing status. The source preview drawer should show the image first, then extraction diagnostics and generated OCR/caption chunks. Citation clicks and search results should open the same preview drawer anchored to the matching image section; chat answers should cite the image by filename + section, not paste large images into the message stream.
- **Guardrails:**
  - Keep full image bytes out of audit/governance metadata; log ids, counts, dimensions, and diagnostic statuses only.
  - Cap image file size, dimensions, generated thumbnail size, and caption/OCR text length.
  - Treat vision captions as derived, fallible text; show extraction diagnostics so users can see what the model/OCR actually indexed.
  - Preserve authorization: image files, thumbnails, previews, and extracted text must remain scoped to the owning user/notebook.
  - Record upload rejection reasons when image capability is missing, but do not include user images or model captions in audit metadata by default.
- **Future extension:** add a separate multimodal embedding backend/collection (e.g. Azure AI Vision, Google multimodal embeddings, CLIP/Jina/Nomic/self-hosted) only after v1 proves the product value. Do not mix e5 text vectors and image vectors in the same Chroma collection unless dimensions/model semantics are explicitly separated.

### Tier 3 — low priority / customer-driven only

#### [ ] A10 · Audio transcription (meeting recordings)
- **Low priority unless a customer explicitly needs it.** Needs a Whisper-class endpoint (customer serving has none). Local CPU whisper is slow. Mitigate with A7 (accept transcripts) until infrastructure exists.

#### [ ] A11 · Audio overview / TTS, mind map
- **Low priority unless a customer explicitly needs it.** TTS not available on the serving side; mind map needs a self-hosted render lib (markmap/mermaid — no-CDN rule). Nice-to-haves, not this phase.
