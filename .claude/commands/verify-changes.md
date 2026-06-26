---
description: Run the project's standard verification (py_compile + pytest)
---

Verify the current changes with the project's standard checks, in order:

1. `.venv/bin/python -m py_compile app/*.py tests/*.py`
2. `.venv/bin/pytest`

Report pass/fail plainly. If py_compile fails, fix the syntax error before running pytest. If a test fails, show the failing output — do not claim success unless both steps pass.

$ARGUMENTS
