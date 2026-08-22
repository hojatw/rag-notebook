# Security Policy

## Project status

This is a **single-machine proof of concept**, not a hardened production service. It is suitable for local experiments and small trusted single-machine deployments after you set a strong `NOTEBOOKLM_SECRET`. Do not expose it directly to the public internet without adding the hardening items listed below.

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

The app keeps a DB-backed admin audit trail in `audit_events`, viewable at `/admin/audit`. It currently records high-risk operations such as full/sanitized eval exports, retrieval profile create/apply/delete/export, LLM settings updates and diagnostic tests, vector index Clear/Rebuild, user-management changes, and notebook/source/chat/note lifecycle or Markdown-export actions. Full internal eval report exports are marked `high` sensitivity because they include questions, expected evidence, diagnostics, and retrieved snippets; chat/note Markdown exports are also marked `high` because they can carry user-entered content out of the deployment.

Audit metadata is intentionally compact: store action identifiers, target ids, flags, and parameter summaries only. Do **not** copy API keys, source text, retrieved snippets, prompts, or full exported payloads into `audit_events`; the audit trail should prove that an action happened, not duplicate sensitive content.

## Hardening status

A full review on **2026-08-22** surfaced hardening items beyond `SEC-1` (bootstrap accounts, fixed above). They are triaged and tracked in `docs/REVIEW_BACKLOG_2026-08-22.md` with priorities and locations, and are folded back into this file as each lands. The open ones at the time of writing: **no upload size limit** (a multipart body is also fully buffered in memory by the CSRF middleware — the highest-severity item still open), **session tokens that never expire and cannot be revoked** (changing a password does not end other sessions), **no login rate limiting**, **prompt-injection detection with English-only patterns** on a zh-TW deployment, and two low-severity hygiene items. Keep treating the app as a POC and re-audit before any untrusted-network exposure.

### Attack surface note: uploaded-file parsers (A6b / A6c, 2026-07-25)

Ingestion now parses `.pptx` (`python-pptx`, which pulls in **Pillow**) and `.xlsx` (`openpyxl`) in addition to PDF/DOCX/HTML. Every one of these parses a **user-supplied file**, so a malicious upload is the realistic attack vector, and Pillow in particular has a steady stream of image-decoding CVEs.

Current mitigations are structural rather than sandboxing:

- Uploads are authenticated — a user must already have an account, so this is not an anonymous-internet surface.
- Parsing runs in the ingest worker, not the request path; `[runtime].max_source_bytes` caps every eagerly-parsed format (`.xlsx` / `.pptx` / `.csv`) **before** a parser sees the file, `[spreadsheet].max_rows` / `max_cols` bound the per-sheet work, and a parser exception fails that one source (`status='failed'` with `failed_stage`) rather than the process.
- Phase 1 PPTX **never decodes image bytes** — images are counted, not opened — so Pillow is currently a transitive dependency that ingest does not actually exercise. That changes the day `A8`/`A9` add OCR or vision, which is when this note needs revisiting.

**Keep these parsers patched** (they are the highest-value dependency updates in this project), and re-evaluate isolation if the app is ever exposed to untrusted uploaders.

(Resolved: CSRF protection on unsafe routes, streaming responses, LLM/embedding HTTP retry/backoff, and worker-backed ingest — a DB-backed queue (`app/jobs.py`) with a dedicated/inline worker — are now implemented. See `docs/PERFORMANCE.md`.)

## Triaged dependency-audit findings

The items below are surfaced by Dependabot / `pip-audit`. Each records how it was assessed against *this* deployment, so the alerts are not repeatedly re-investigated. An advisory that turns out not to apply is still patched when the upgrade is cheap — the note explains why the alert existed, not why the upgrade was skipped.

### GHSA-g6cj-pr64-35w5 — `cryptography < 50.0.0` (high) — patched; the affected path was never used

A Bleichenbacher oracle in **PKCS#7 `EnvelopedData` decryption**. This app's only use of `cryptography` is [`app/security.py`](../app/security.py): Fernet (AES-CBC + HMAC) for API-key encryption at rest, plus PBKDF2HMAC/SHA-256 for key derivation. It never calls the PKCS#7 envelope APIs, so the oracle was unreachable — upgraded to `50.0.0` anyway because it is the advisory's fix and costs nothing.

- **Verified on upgrade:** a Fernet token produced under `49.0.0` still decrypts correctly under `50.0.0` with the same `NOTEBOOKLM_SECRET`, and a wrong secret still yields `""` rather than garbage. Existing encrypted API keys in deployed databases are unaffected — no re-entry needed.

### GHSA-fp3f-mc75-235c / GHSA-fwg2-594c-jp42 — `pypdf < 6.15.0` (medium) — applicable, patched

Unbounded memory/CPU on crafted `/ToUnicode` streams and CID font width ranges. Unlike the other two entries here this **is** reachable: the app parses user-uploaded PDFs. It is denial-of-service only (no code execution, no disclosure), and ingest runs in the worker behind `[runtime].max_source_bytes` with per-source failure isolation, so the blast radius is one stuck ingest job rather than the web process. Patched to `6.15.0` — a direct application of the "keep these parsers patched" rule above.

### CVE-2026-45829 — `chromadb==1.5.9` (critical) — not applicable

The vulnerability is triggered through ChromaDB's **FastAPI server endpoint**. This application uses ChromaDB only as an **embedded library** via `chromadb.PersistentClient` (`app/vector_store.py`): it does **not** run a Chroma server, does not use `chromadb.HttpClient`, does not enable `allow_reset`, and exposes no Chroma HTTP endpoint. The only network port the app exposes is its own FastAPI/uvicorn server (`8000`).

**Second, independent reason it is unreachable (added on the 2026-08-22 re-check).** The advisory's mechanism is specific: reaching collection creation with a *malicious model repository* plus `trust_remote_code=true`, which makes Chroma load attacker-controlled code while instantiating an embedding function. This app **never lets Chroma instantiate an embedding function at all** — `get_or_create_collection` is called with only `name` and `metadata={"hnsw:space": "cosine"}`, and every vector is computed by `app/llm.py` and handed over pre-computed via `upsert(embeddings=...)`. `trust_remote_code` appears nowhere in the codebase. So the code path that loads a model repository is never entered, independently of whether a server is exposed. The same reasoning covers the still-open client-side variant, [chroma-core/chroma#7439](https://github.com/chroma-core/chroma/issues/7439) ("Harden client EF poisoning"), which would otherwise be the one relevant to embedded users.

**Status re-checked 2026-08-22 — still no patched release, but the reason changed.**

- **Upstream is fixed; the fix is unreleased.** [chroma-core/chroma#7237](https://github.com/chroma-core/chroma/pull/7237) ("block `trust_remote_code` in SentenceTransformer embedding function kwargs") **merged 2026-07-07**, closing tracking issue #7226 as completed. `main` now carries `chromadb/utils/embedding_functions/config_validation.py` and `chromadb/test/server/test_fastapi_security.py`.
- **But PyPI's newest release is still `1.5.9` (published 2026-05-05)** — no release has shipped since the fix landed. The GitHub Advisory therefore still lists patched versions as `None` (last updated 2026-05-29), and Dependabot reports `first_patched: null`. Note the advisory's affected range is `>= 1.0.0, <= 1.5.9`, i.e. bounded — the next release is expected to fall outside it.
- **Accepted risk** for the embedded-only usage pattern, unchanged.
- **Action:** watch for the next `chromadb` release and bump `requirements.txt` when it ships; Dependabot will flag it. This is a *pending upgrade*, not an unfixable condition — do not re-investigate whether a fix exists, only whether it has been **released**.
- **Re-evaluate sooner if** the app ever switches to `chromadb.HttpClient` / runs a Chroma server, or starts passing an `embedding_function` to Chroma instead of pre-computed vectors — either change removes one of the two reasons above.
- References: [GitHub Advisory GHSA-f4j7-r4q5-qw2c](https://github.com/advisories/GHSA-f4j7-r4q5-qw2c), [NVD CVE-2026-45829](https://nvd.nist.gov/vuln/detail/CVE-2026-45829).
