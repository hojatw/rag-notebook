---
description: Start the local dev server the right way (insecure dev secret, real network for LLM/embedding)
---

Start the local dev server for manual or browser verification. Run it as a background process so this session stays interactive:

```
NOTEBOOKLM_ALLOW_INSECURE_DEV_SECRET=1 .venv/bin/uvicorn app.main:app --reload --port 8000
```

Notes:
- The Claude Code **preview sandbox has no outbound network**, so embedding/upload/indexing fail there with DNS errors (`[Errno 8] nodename nor servname provided`). To exercise anything that calls the LLM/embedding endpoint, run this real uvicorn (with network egress) — not the sandboxed preview.
- Worktrees reuse the main repo's `.venv` and `data/`, so no separate setup is needed.
- Local test login: `admin` / `admin123`.
- Never use the insecure dev secret for a network-exposed or production-like run.

$ARGUMENTS
