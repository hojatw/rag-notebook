---
description: Run a UX/UI review per the project's six-direction rubric and report format
---

Run a UX/UI review of this app following `docs/UX_REVIEW_GUIDE.md` exactly.

- Judge every surface against the six directions (專業 / 簡單 / 直覺 / 明確 / 一致 / 可預期) and `docs/UI.md`; UI copy must go through i18n (`docs/I18N.md`), never hardcoded.
- Use the walk method in the guide: start a local server, log in as admin, screenshot each major page (desktop + 375px mobile), and verify text/styles with snapshot/inspect — not screenshots alone.
- Produce the report in the guide's format (走查方法 / 總評 / 做得好的 / 嚴重度分表 with `問題 | 方向 | 修改建議 | backlog 對應 | 狀態` / 優先順序). Update `docs/UX_REVIEW.md` in place: add new findings, tick `[x]` items that are now fixed, keep rows for traceability.
- Don't fix anything yet unless I say so — review and report first, then we discuss priorities.

$ARGUMENTS
