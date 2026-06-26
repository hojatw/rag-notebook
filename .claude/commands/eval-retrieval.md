---
description: Run the retrieval eval harness, reranked vs no-rerank (needs an LLM configured)
---

Run the retrieval evaluation. If you're about to change retrieval behavior, read `docs/RETRIEVAL.md` first.

Run both passes and compare:

1. `.venv/bin/python -m tests.eval_retrieval`
2. `.venv/bin/python -m tests.eval_retrieval --no-rerank`

Summarize the metrics and call out the delta the reranker makes. Note: this needs a working chat + embedding connection; in the no-network sandbox it fails with DNS errors, so run it where there is network egress.

$ARGUMENTS
