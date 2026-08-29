# Security Policy

## Project status

This is a **single-machine proof of concept**, not a hardened production service. It is suitable for local experiments and small trusted single-machine deployments after you set a strong `NOTEBOOKLM_SECRET`. Do not expose it directly to the public internet without adding the hardening items listed below.

### What "proof of concept" is actually claiming — and when to drop it

The label currently carries **two different statements**, and only one of them is about immaturity:

- **Architectural scope** — single machine, SQLite, local files, embedded Chroma, no HA, no horizontal scale. At the scale this is built for (see [`DEPLOYMENT_CONTEXT.md`](DEPLOYMENT_CONTEXT.md)) that is a **deliberate, defensible architecture**, not a placeholder. Calling it "POC" undersells it.
- **Hardening and measurement maturity** — this is what the label is honestly signalling, and it is the half that can be closed.

So the question is not "when does this become production", it is "when can the scope be stated as a scope, and the maturity gap be declared closed". Three conditions, all checkable — **not a judgement call**:

1. **Login rate limiting — satisfied 2026-08-23 (`SEC-4`).** `POST /login` now uses a shared SQLite account failure bucket plus short cross-process password-verification leases, returns a generic HTTP 429 with `Retry-After`, and stores HMAC account ids instead of usernames. A reverse proxy must still provide verified client-IP limiting for network deployments.
2. **A representative eval set exists (`Q1-3` in [`QUALITY.md`](QUALITY.md)).** This is the important one. The Eval Workbench can run retrieval and judged comparisons, but **there is still no customer-approved representative set that can establish whether a retrieval change helps the target deployment**. Six items say so in their own text — `Q0-2` and `Q1-4` ("needs Q1-3"), `Q1-6` ("this is Q1-3's job"), `Q1-7` ("to prove the arm helps rather than merely changes results"), `QLT-1` in the review backlog, and `Q1-2`/`P1-2` (blocked on a representative CJK corpus) — plus `Q1-1` indirectly. A retrieval system whose target quality has not been measured should not claim to be past proof-of-concept; that is a factual statement about what is known, not modesty.
3. **Someone other than the author has completed an upgrade using only the documentation.** `RELEASE.md` and the CHANGELOG upgrade notes exist, but have never been executed by a second person. Until they have, "it is documented" is untested.

Deliberately **not** on this list: backup/restore drills, monitoring, and uptime targets. Those belong to whoever operates a given deployment, and their absence says nothing about this repository's maturity.

When all three hold, rewrite this section as a **scope statement** ("single machine by design; not HA, not multi-tenant-isolated, not horizontally scalable") plus the honest remaining limits, and drop "proof of concept" from `README.md`, `README.zh-TW.md`, `AGENTS.md`, and `CLAUDE.md` in the same change. Do not drop it piecemeal — a repo that calls itself production in one file and a POC in another has answered the question for nobody.

## Reporting a vulnerability

Please report security issues **privately** rather than opening a public issue:

- Open a [GitHub security advisory](https://github.com/hojatw/rag-notebook/security/advisories/new) for this repository, or
- Contact the maintainer directly.

Include reproduction steps and the affected version/commit. We aim to acknowledge reports within a few days. As a POC maintained on a best-effort basis, fixes are not guaranteed to ship on a fixed timeline.

## Secrets and data

- `NOTEBOOKLM_SECRET` signs session cookies and is the KDF input for Fernet encryption of stored LLM API keys (`app/security.py`). Keep it secret and stable — **changing it invalidates every encrypted API key**, which must then be re-entered at `/settings`.
- Never commit `.env`, real secrets, or runtime state under `data/` or `logs/`.
- Do not use `NOTEBOOKLM_ALLOW_INSECURE_DEV_SECRET=1` for any network-exposed or production-like run; it is a local-development convenience only.
- **Bootstrap accounts.** The demo pair (`admin/admin123`, `user/user123`) is seeded **only** when demo seeding is on: explicitly via `NOTEBOOKLM_SEED_DEMO_USERS=1`, or by default when running on the insecure dev secret. Any deployment with a real `NOTEBOOKLM_SECRET` seeds **only `admin`**, and that account must change its password at first login before it can reach anything else — the password is a one-time bootstrap credential, not a standing one. `user` is not seeded there at all.
  - **Upgrading an existing deployment:** on the next start, any account still using its seeded default password is flagged for a forced change. Nothing is deleted, so no data is lost, but `admin123` / `user123` stop being usable for anything except setting a new password. Plan to be at the keyboard for the first login after the upgrade.
    - Scope is deliberately narrow: **only the two seeded usernames are examined**, and only to check whether they still hold the password this project shipped them with. An account you created that happens to use `admin123` is not touched — this is not a weak-password audit.
    - **SSO-linked accounts are skipped**, because `POST /account/password` refuses external identities and a flagged one would have no way out. If such an account still accepts its seeded password over local login, startup logs `seeded_default_password_left_unflagged`; remediate by disabling local login for the deployment (`[auth].local_login_enabled = false`) or resetting the password through admin tooling.
  - **Historical note (fixed 2026-08-22):** this section previously told operators to "change or remove" the demo accounts. *Removing* them did not work: `init_db()` runs on every start of both the web app and the worker, and re-created both accounts — with their original passwords — on the next restart. If you are auditing a deployment that has not been upgraded, assume `user/user123` exists regardless of whether it was deleted.
- Enterprise SSO / AD integration is tracked as high-priority roadmap item `I1`
  and designed in [`AUTHENTICATION.md`](AUTHENTICATION.md). Trusted
  reverse-proxy header mode (`I1a`) and OIDC (`I1b`) are implemented but
  disabled by default. Header mode must use a proxy shared-secret check, not
  topology trust alone. OIDC endpoints must use HTTPS except localhost
  development endpoints, discovery issuer is bound to configured issuer, OIDC
  client secrets belong in env/gitignored config only, and tokens/codes must
  never be copied into audit metadata. SAML remains a customer-driven follow-up.
  Do not implement browser silent SSO as raw LDAP login; LDAP bind does not
  provide Integrated Windows Authentication.

## Audit trail

The app keeps a DB-backed admin audit trail in `audit_events`, viewable at `/admin/audit`. It currently records high-risk operations such as full/sanitized eval exports, retrieval profile create/apply/delete/export, LLM settings updates and diagnostic tests, vector index Clear/Rebuild, user-management changes, and notebook/source/chat/note lifecycle or Markdown-export actions. Full internal eval report exports are marked `high` sensitivity because they include questions, expected evidence, frozen domain-policy snapshots, retrieval diagnostics and snippets, generated answers, and judge details; chat/note Markdown exports are also marked `high` because they can carry user-entered content out of the deployment.

Audit metadata is intentionally compact: store action identifiers, target ids, flags, and parameter summaries only. Do **not** copy API keys, source text, retrieved snippets, prompts, or full exported payloads into `audit_events`; the audit trail should prove that an action happened, not duplicate sensitive content.

### E2 notebook domain hints and answer policy

Domain settings are owner-scoped notebook data. `GET` and every mutation under
`/notebooks/{id}/domain-settings` must resolve the notebook through the signed-in
owner; an admin role does not grant the ability to edit another user's notebook.
All mutations remain CSRF-protected POST requests, including crafted multipart
bodies. E2 text forms require a valid `Content-Length` and reject bodies over
64 KiB before parsing. Hints and policy are
untrusted operational instructions, not source evidence: they cannot override
grounding, authorization, citation requirements, or the spreadsheet aggregation
guard. Query-oriented hint fields are query-time only and do not alter Chroma
or source data.

The structural `[[RAG_ABSTAIN]]` value is an internal provider/app protocol
marker. Both streaming and non-streaming paths convert it to localized
`chat.abstain` before SSE or persistence, and a truncated reserved prefix is
treated as abstention. Two invariants hold in every configuration:

- **The marker never escapes.** It must not appear in SSE output, messages,
  citation payloads, HTML, or either export. In the streaming path this needs
  more than a substring check: providers split `[[RAG_ABSTAIN]]` across chunks,
  so `_split_emittable` holds back any tail that is still a prefix of the
  marker. That hold-back is derived from the marker length and is deliberately
  **not** configurable — one character less reopens the leak.
- **Persistence is always classified on the full completion.** The saved
  message, its citations, and `metadata.outcome` come from a final
  `parse_answer_result` over the whole text, never from what was streamed.

What `runtime.answer_stream_gate_chars` changes is only **what the user's screen
saw in the meantime**:

- `0` (default) — the whole completion is buffered before anything is emitted.
  A model that writes normal-looking text and abstains afterwards puts nothing
  on screen at all. This is the strongest form of the guarantee and the setting
  to keep unless partial streaming has been shown to help; see
  `docs/DEVELOPMENT.md`, since a provider that bursts its stream at the end
  makes the trade below buy nothing.
- `> N` — nothing is emitted until N characters have been buffered and
  classified. A compliant abstention is the marker alone at position 0 and never
  survives that gate; a reserved prefix appearing later stops emission for the
  rest of the stream. The residual exposure is bounded and specific: a model
  that writes a preamble **longer than N** and only then abstains will have had
  that preamble displayed. The server then emits an SSE `discard` event so the
  client clears it before the refusal is appended, rather than leaving it until
  the final `done` swap. Streamed text is written with `textContent`, so this
  path adds no markup-injection surface; the final render still goes through the
  server-side sanitizer.

Notebook answer policy and matched answer notes are serialized as bounded JSON
inside the user-role prompt. They are never placed in the privileged system
message. Immutable application grounding/citation rules and the spreadsheet
guard remain system-role instructions; explicit answer policy outranks matched
answer notes, but neither can weaken those application rules.

Eval runs freeze the selected notebook domain configuration and store hints and
policy only in the run snapshot. Before use, the snapshot is raw-size bounded,
canonicalized, checked against the exact schema/prompt version and static hard
ceilings, and rejected as a whole on any invalid field; old runs safely use the
baseline. The sanitized export exposes flags, counts, character summaries,
revision, and a short one-way version fingerprint; it does not expose terms,
policy text, or the raw token. Only the explicitly confirmed, CSRF-protected
POST full internal export may include the snapshot, and it remains admin-only
and high-sensitivity. Domain
audit metadata is allowlisted to identifiers, flags, counts, and character
lengths/fingerprints; it must never contain domain text or snapshots.

## Hardening status

A full review on **2026-08-22** surfaced a set of hardening items. They are triaged and tracked in `docs/REVIEW_BACKLOG_2026-08-22.md` with priorities and locations, and are folded back into this file as each lands.

**Fixed:** `SEC-1` bootstrap accounts (above), `SEC-2` upload size limits / multipart buffering (see the parser note below), `SEC-3` session lifetime + revocation (below), `SEC-4` shared login rate limiting, `SEC-5` Traditional/Simplified Chinese prompt-injection telemetry patterns, and `SEC-6` explicit session-user field projection that excludes `password_hash`.

**Still open:** two low-severity hygiene items: a raw exception string echoed to an admin (`SEC-7`) and logout-cookie deletion attributes that are not explicitly aligned with issuance (`SEC-8`).

### Local-login rate limiting (SEC-4, 2026-08-23)

The limiter has one configurable account failure bucket (default 5 attempts per
15 minutes, followed by a 15-minute cooldown). State is persisted in
`login_rate_limits` and updates run under a short SQLite `BEGIN IMMEDIATE`
transaction, so multiple uvicorn workers cannot silently maintain independent
counters. Successful authentication clears that account's bucket.

Password hashing is protected separately by short SQLite leases in
`login_verification_leases`: by default no more than 4 PBKDF2 checks run across
the deployment, and the `account_hash` uniqueness constraint serializes checks
for the same username so parallel requests cannot all pass the failure check.
Leases expire after 30 seconds to recover from a crashed web process and are
released in `finally` during normal operation. Missing accounts use a fixed
dummy hash on this same bounded path, but do not create persistent failure rows.

Bucket ids are HMAC-SHA256 values derived from `NOTEBOOKLM_SECRET`; usernames,
passwords, IP addresses, and forwarded headers are not stored. The app does not
trust `X-Forwarded-For`. Operators should keep the app behind a reverse proxy
that applies a verified client-IP limit as the outer layer.

**Review follow-up (2026-08-23):** the original implementation also had a
deployment-wide failure cooldown. It was removed before release because an
anonymous actor could fill it with arbitrary unknown usernames and repeatedly
deny every valid local login. Concurrency leases retain the CPU-protection goal
without leaving a cooldown active after the abusive requests finish. See
[`AUTHENTICATION.md`](AUTHENTICATION.md) and `config.example.toml` for the
`[auth].login_*` controls.

### Local prompt-injection telemetry (SEC-5, 2026-08-23)

`local.rules.v2` adds Traditional and Simplified Chinese patterns for ignoring
prior instructions, revealing system/developer instructions, and bypassing
safety rules. Findings remain **warn-only telemetry**: they are redacted and
hashed before persistence and do not block the user flow. This heuristic is a
review signal, not a security boundary, and its positive/negative regression
examples live in `tests/test_governance.py`.

### Sessions (SEC-3, 2026-08-22)

Sessions are stateless signed cookies — there is no server-side session table — so both expiry and revocation have to be carried by the token itself.

- **Absolute lifetime.** `[auth].session_max_age_hours` (default 12) is enforced on the **token**, not just the cookie: a `max_age` cookie attribute is only advice to a browser, and anything that copies the token value ignores it. The lifetime is counted from issue and is deliberately **not extended by activity** — a rolling window would keep a stolen cookie alive for as long as it kept being used, which is the case this bounds. The effective value is logged in `app_started`, so a deployment that sets it absurdly high cannot silently undo this.
- **Revocation via `users.password_version`.** The token embeds the version it was issued under; `current_user` compares it against the column on every request. Every password change bumps it. Self-service changes re-issue the actor's cookie, so you sign out your *other* devices; an admin reset re-issues nothing, so the target is signed out everywhere and the audit event records `sessions_revoked`.
- **Known limits, accepted for a POC:** there is still no "sign out all my devices" control that does not involve changing a password, no per-session listing, and no way to revoke one specific device. SSO logins issue local sessions and are covered by expiry, but the app does not consume IdP-side logout or token revocation — that limitation is recorded in [`AUTHENTICATION.md`](AUTHENTICATION.md).
- **Upgrading:** pre-SEC-3 tokens carry no timestamp and fail validation, so **every user is signed out once** on deploy. That is intended — those are exactly the never-expiring cookies being retired.

Keep treating the app as a POC and re-audit before any untrusted-network exposure.

### Attack surface note: uploaded-file parsers (A6b / A6c, 2026-07-25)

Ingestion now parses `.pptx` (`python-pptx`, which pulls in **Pillow**) and `.xlsx` (`openpyxl`) in addition to PDF/DOCX/HTML. Every one of these parses a **user-supplied file**, so a malicious upload is the realistic attack vector, and Pillow in particular has a steady stream of image-decoding CVEs.

Current mitigations are structural rather than sandboxing:

- Uploads are authenticated — a user must already have an account, so this is not an anonymous-internet surface.
- **Bounded before anything is stored or parsed (SEC-2, 2026-08-22).** `[runtime].upload_max_file_bytes` caps each file *at upload time*, for every format, enforced while streaming to disk (and drops to the stricter `extract_max_file_bytes` for `.xlsx`/`.pptx`/`.csv`, so those fail up front rather than in the worker) — an oversized file is refused with 413 and its partial write removed. A whole request is bounded at `upload_max_file_bytes * upload_batch_limit` and refused from `Content-Length` **before any body is read**. Previously nothing capped upload size at all: only file *count* was limited, and `extract_max_file_bytes` (then named `max_source_bytes`) applied at parse time, once the file was already on disk. Worse, the CSRF middleware called `await request.body()` on multipart requests to regex the token out of the raw body — and the upload form is a plain HTML form that carries no header to short-circuit that — so every upload was materialised in memory in full. A handful of large files was enough to exhaust it, from an ordinary authenticated account and with no vulnerability involved. The middleware no longer reads multipart bodies; upload routes validate CSRF from their own parsed form via `verify_multipart_csrf`, and a startup assertion fails the boot if a multipart route omits it, so the check cannot be dropped silently.
- Parsing runs in the ingest worker, not the request path; `[runtime].extract_max_file_bytes` caps every format with stricter parser-cost bounds (`.xlsx` / `.pptx` / `.csv`) **before** a parser sees the file. CSV rows now stream incrementally, but the cap still bounds total parse work and pathological single records/fields; `[spreadsheet].max_rows` / `max_cols` bound retained per-sheet data, and a parser exception fails that one source (`status='failed'` with `failed_stage`) rather than the process.
- Phase 1 PPTX **never decodes image bytes** — images are counted, not opened — so Pillow is currently a transitive dependency that ingest does not actually exercise. That changes the day `A8`/`A9` add OCR or vision, which is when this note needs revisiting.

**Keep these parsers patched** (they are the highest-value dependency updates in this project), and re-evaluate isolation if the app is ever exposed to untrusted uploaders.

(Resolved: CSRF protection on unsafe routes, streaming responses, LLM/embedding HTTP retry/backoff, and worker-backed ingest — a DB-backed queue (`app/jobs.py`) with a dedicated/inline worker — are now implemented. See `docs/PERFORMANCE.md`.)

## Triaged dependency-audit findings

The items below are surfaced by Dependabot / `pip-audit`. Each records how it was assessed against *this* deployment, so the alerts are not repeatedly re-investigated. An advisory that turns out not to apply is still patched when the upgrade is cheap — the note explains why the alert existed, not why the upgrade was skipped.

### GHSA-g6cj-pr64-35w5 — `cryptography < 50.0.0` (high) — patched; the affected path was never used

A Bleichenbacher oracle in **PKCS#7 `EnvelopedData` decryption**. This app's only use of `cryptography` is [`app/security.py`](../app/security.py): Fernet (AES-CBC + HMAC) for API-key encryption at rest, plus PBKDF2HMAC/SHA-256 for key derivation. It never calls the PKCS#7 envelope APIs, so the oracle was unreachable — upgraded to `50.0.0` anyway because it is the advisory's fix and costs nothing.

- **Verified on upgrade:** a Fernet token produced under `49.0.0` still decrypts correctly under `50.0.0` with the same `NOTEBOOKLM_SECRET`, and a wrong secret still yields `""` rather than garbage. Existing encrypted API keys in deployed databases are unaffected — no re-entry needed.

### GHSA-fp3f-mc75-235c / GHSA-fwg2-594c-jp42 — `pypdf < 6.15.0` (medium) — applicable, patched

Unbounded memory/CPU on crafted `/ToUnicode` streams and CID font width ranges. Unlike the other two entries here this **is** reachable: the app parses user-uploaded PDFs. It is denial-of-service only (no code execution, no disclosure), and ingest runs in the worker behind `[runtime].extract_max_file_bytes` with per-source failure isolation, so the blast radius is one stuck ingest job rather than the web process. Patched to `6.15.0` — a direct application of the "keep these parsers patched" rule above.

### ChromaDB `1.5.9` advisory cluster — current application paths not affected; patched release pending

As of 2026-08-29, GitHub reports seven open Dependabot alerts for four unique
ChromaDB advisories. The duplicate count comes from the same runtime dependency
being represented in both `requirements.txt` and `requirements-dev.txt` (the
latter includes the former with `-r`). The affected package is still pinned at
`chromadb==1.5.9` in [`requirements.txt`](../requirements.txt).

The four advisories are:

- **CVE-2026-45829 / GHSA-f4j7-r4q5-qw2c** (critical): pre-authentication code
  injection through the ChromaDB FastAPI collection endpoint, using a malicious
  model repository and `trust_remote_code=true`.
- **CVE-2026-45833 / GHSA-36p7-vc44-83pf** (critical): authenticated code
  injection through the ChromaDB FastAPI collection endpoint when the attacker
  has `UPDATE_COLLECTION` permission.
- **CVE-2026-45830 / GHSA-2wm9-hf6c-p5cr** (high): authenticated users can read,
  write, update, or delete collections across tenant boundaries.
- **CVE-2026-45831 / GHSA-xph7-9rjv-w5fr** (high): `SimpleRBAC` does not verify
  that a permission applies to the requested tenant, database, or collection.

All four advisories affect ChromaDB's HTTP server/authentication paths. This
application uses ChromaDB only as an embedded library through
`chromadb.PersistentClient` (`app/vector_store.py`): it does not run a Chroma
server, use `chromadb.HttpClient`, enable `allow_reset`, pass an
`embedding_function`, or accept `trust_remote_code`. Collections are created
with only a name and metadata, while embeddings are calculated by the
application and supplied to Chroma. The application also applies its own
per-user metadata filters before vector queries and source deletion. Therefore
these four advisories are **not reachable in the current repository's supported
deployment path**.

This is a deployment-scope assessment, not a claim that `chromadb==1.5.9` is
clean. `pip-audit` reports all four advisories and GitHub lists no
`first_patched_version`; PyPI's latest ChromaDB release remains `1.5.9`.
Do not expose a Chroma HTTP server, switch to `HttpClient`, or allow untrusted
users to control Chroma embedding-function configuration before a patched
release is available. A future release must be tested before changing the pin;
do not downgrade because the affected ranges include older ChromaDB versions.

The ChromaDB fix for CVE-2026-45829 was merged in
[chroma-core/chroma#7237](https://github.com/chroma-core/chroma/pull/7237), but
it has not shipped in a PyPI release. The tenant-scope fix remains under review
in [chroma-core/chroma#7602](https://github.com/chroma-core/chroma/pull/7602).
Re-evaluate this assessment immediately if the application adopts a Chroma
server or changes how embeddings are configured.

References: [GHSA-f4j7-r4q5-qw2c](https://github.com/advisories/GHSA-f4j7-r4q5-qw2c),
[GHSA-36p7-vc44-83pf](https://github.com/advisories/GHSA-36p7-vc44-83pf),
[GHSA-2wm9-hf6c-p5cr](https://github.com/advisories/GHSA-2wm9-hf6c-p5cr),
[GHSA-xph7-9rjv-w5fr](https://github.com/advisories/GHSA-xph7-9rjv-w5fr).
