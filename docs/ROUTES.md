# Route reference

Full HTTP route reference for the FastAPI app. The high-level onboarding path
lives in [`README.md`](../README.md).

```text
GET  /                                                    redirect to /notebooks (or /login)
GET  /login                                               sign-in page
POST /login                                               authenticate
POST /logout                                              clear session

GET  /notebooks                                           notebook grid
GET  /search                                              search notebooks, sources, conversations, and notes
POST /notebooks/new                                       create a notebook
GET  /notebooks/{id}                                      three-pane workspace
POST /notebooks/{id}/rename                               rename / change emoji
POST /notebooks/{id}/delete                               delete (cascades sources + chats + notes)

POST /notebooks/{id}/sources/upload                       upload + queue ingest
POST /notebooks/{id}/sources/{sid}/reindex                requeue ingest
POST /notebooks/{id}/sources/{sid}/delete                 delete source + vectors + file
GET  /notebooks/{id}/sources/{sid}/_partial               HTMX polling: source row
GET  /notebooks/{id}/sources/{sid}/preview                source preview drawer (chunk list)
GET  /notebooks/{id}/_chat-empty                          HTMX swap: chat empty-state after indexing changes

POST /notebooks/{id}/chat/new                             new conversation
POST /notebooks/{id}/chat/ask                             ask a question (HTMX: returns messages partial; no-JS: 303)
POST /notebooks/{id}/chat/ask-stream                      ask a question with streamed answer events
POST /notebooks/{id}/chat/{cid}/rename                    rename a conversation
POST /notebooks/{id}/chat/{cid}/delete                    delete a conversation
GET  /notebooks/{id}/chat/{cid}/_followups?message_id=N   lazy-load follow-up question chips (cached in metadata)
GET  /notebooks/{id}/chat/{cid}/export                    download conversation as Markdown

POST /notebooks/{id}/suggestions                          generate 4 starter questions (chat empty-state)
GET  /notebooks/{id}/_briefing                            HTMX swap: briefing strip (dedupes concurrent generation)
POST /notebooks/{id}/briefing[?force=1]                   generate / regenerate notebook briefing
GET  /notebooks/{id}/_tools                               HTMX swap: Studio tools launcher (tile grid)
GET  /notebooks/{id}/tools/{kind}                         tool config panel for the preview-modal (compare|minutes|study_guide|faq|timeline|translate)
POST /notebooks/{id}/compare                              compare 2+ sources (returns result fragment + save button)
POST /notebooks/{id}/minutes                              structured meeting minutes from one source (result + save button)
POST /notebooks/{id}/artifacts/{kind}                     A4 artifact: study_guide | faq | timeline (result + save button)
POST /notebooks/{id}/translate                            A5 translate one source's summary into a target language (result + save button)

POST /notebooks/{id}/notes/pin                            pin assistant message into notes
POST /notebooks/{id}/notes/add                            save a raw note (title + content)
POST /notebooks/{id}/notes/{note_id}/edit                 edit a note's title/content in place (U8)
POST /notebooks/{id}/notes/{note_id}/delete               remove pinned note (also broadcasts pin-cleared)
GET  /notebooks/{id}/_notes                               HTMX swap: notes section (notes-changed event)
GET  /notebooks/{id}/notes/export                         download all notes as Markdown
GET  /notebooks/{id}/notes/{note_id}/export               download one note as Markdown

GET  /account                                             change own password
POST /account/password                                    save new password

GET  /admin/users                                         user list (admin only)
POST /admin/users/new                                     create user
POST /admin/users/{uid}/reset-password                    set a new password
POST /admin/users/{uid}/toggle-admin                      promote / demote
POST /admin/users/{uid}/delete                            cascade-delete a user

GET  /admin/index                                         Chroma index health page (admin only)
POST /admin/index/rebuild                                 full re-upsert of every SQLite chunk
POST /admin/index/clear                                   delete every Chroma vector
GET  /admin/evals                                         admin eval workbench: active profile, eval sets, run history
GET  /admin/evals/help                                    in-product eval tuning guide for retrieval profiles
POST /admin/evals/sets                                    create an eval set for an existing notebook
POST /admin/evals/sets/{eval_set_id}/delete               delete an eval set and its items/runs/results
GET  /admin/evals/sets/{eval_set_id}                      eval-set detail, manual question authoring, run list
POST /admin/evals/sets/{eval_set_id}/generate             generate draft eval-item candidates from indexed chunks
POST /admin/evals/sets/{eval_set_id}/generate/llm         generate LLM-assisted draft eval candidates from selected sources
POST /admin/evals/sets/{eval_set_id}/items                add an approved manual retrieval-eval item
POST /admin/evals/sets/{eval_set_id}/items/{item_id}/approve approve a draft eval item
POST /admin/evals/sets/{eval_set_id}/items/{item_id}/delete delete an eval item
POST /admin/evals/sets/{eval_set_id}/run                  queue a retrieval-only eval run (optional profile_id)
GET  /admin/evals/runs/{run_id}                           eval-run detail with metrics and per-question results
GET  /admin/evals/runs/{run_id}/_status                   HTMX polling: eval-run progress and summary metrics
GET  /admin/evals/runs/{run_id}/_results                  HTMX polling: eval-run per-question results
GET  /admin/evals/runs/{run_id}/export/sanitized          JSON report without questions/evidence/retrieved snippets
GET  /admin/evals/runs/{run_id}/export/full?confirm=1     full internal JSON report; audited as high sensitivity
GET  /admin/evals/profiles                                retrieval profiles page (list, create, apply, delete)
GET  /admin/evals/profiles/{profile_id}/export            sanitized retrieval-profile JSON export
POST /admin/evals/profiles                                create a candidate retrieval profile (runtime-safe params)
POST /admin/evals/profiles/{profile_id}/apply             apply a profile to live retrieval (rollback = apply a previous one)
POST /admin/evals/profiles/{profile_id}/delete            delete a candidate profile (active profile is protected)
GET  /admin/evals/compare?base&candidate                  compare two succeeded runs: param/metric/per-question diff

GET  /admin/audit                                         admin audit trail with filters

GET  /settings                                            admin LLM settings (admin only)
POST /settings                                            save LLM settings (API key is encrypted on write)
```
