# Product design notes

Non-scheduled product design exploration for the NotebookLM-style RAG POC. Use
this file for directional ideas that are useful to preserve but should not make
`ROADMAP.md` harder to scan as a backlog.

## Studio and research-output paradigms

These alternatives extend beyond the current NotebookLM tools + notes frame.
They are not scheduled backlog items unless promoted into `ROADMAP.md`.

1. **Chat-centric commands** — expose generators as `/` commands or a `+` menu in
   the chat input. Results appear inline as rich, saveable message cards. The
   right pane becomes a pure outputs/clipboard shelf. This fits the current
   HTMX/server-render stack and would reduce Studio clutter.
2. **Selection-driven inspector** — make the right pane show actions relevant to
   the currently focused object: source text, source row, answer, citation, or
   selected text. This is context-sensitive instead of a static tool list.
3. **Report/compose builder** — turn the right pane into a document assembled
   from briefing, comparisons, pinned answers, minutes, and notes, then edited
   and exported. This best fits a research-report workflow.
4. **Proactive insights feed** — surface insights without an explicit user ask,
   such as source contradictions or stale pinned notes. This is higher cost and
   should wait for stronger eval coverage.
5. **Spatial canvas** — use freeform cards for sources, artifacts, and notes.
   This is the heaviest option and conflicts with the current no-build/no-CDN
   constraint, so it should remain out of scope for the POC.

Recommended direction: keep `U16` tools tiles + outputs shelf as the near-term
base. If the product shifts toward long-form research reports, grow the outputs
shelf toward a report/compose builder and selectively add chat-centric commands
or a selection-driven inspector where they reduce repeated navigation.
