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
- The demo accounts (`admin/admin123`, `user/user123`) are for local development. Change or remove them before exposing the app on a network.

## Known hardening follow-ups

These are deliberately deferred for the POC and tracked in `../README.md` / `../AGENTS.md`. Address them before any untrusted-network exposure:

- No CSRF protection on POST routes.

(Resolved: streaming responses, LLM/embedding HTTP retry/backoff, and worker-backed ingest — a DB-backed queue (`app/jobs.py`) with a dedicated/inline worker — are now implemented. See `docs/PERFORMANCE.md`.)

## Triaged dependency-audit findings

The items below are surfaced by Dependabot / `pip-audit` but have been assessed as **not exploitable in this deployment**. They are documented here so the alerts are not repeatedly re-investigated.

### CVE-2026-45829 — `chromadb==1.5.9` (critical) — not applicable

The vulnerability is triggered through ChromaDB's **FastAPI server endpoint**. This application uses ChromaDB only as an **embedded library** via `chromadb.PersistentClient` (`app/vector_store.py`): it does **not** run a Chroma server, does not use `chromadb.HttpClient`, does not enable `allow_reset`, and exposes no Chroma HTTP endpoint. The only network port the app exposes is its own FastAPI/uvicorn server (`8000`).

- No fixed release is available: the latest published `chromadb` on PyPI is also `1.5.9`, and `pip-audit` offers no remediation version.
- **Accepted risk** for the embedded-only usage pattern.
- **Re-evaluate if** the app ever switches to `chromadb.HttpClient` / runs a Chroma server, or a patched `chromadb` release becomes available — then bump `requirements.txt` accordingly.
- Reference: [NVD CVE-2026-45829](https://nvd.nist.gov/vuln/detail/CVE-2026-45829).
