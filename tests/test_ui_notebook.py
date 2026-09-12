"""Notebook、來源與上傳的 UI 測試。

涵蓋 notebook 與搜尋頁的渲染與筆數上限、來源列的 HX-Trigger 事件分流、上傳一律
進佇列（不在請求內做 ingest）、重新索引必須把狀態改回排隊值否則輪詢會停、A6a
擷取診斷的呈現，以及 SEC-2 的上傳大小上限。
契約文件：docs/UI.md、docs/ROUTES.md。

由 tests/test_ui.py 拆出；共用固件見 tests/ui_helpers.py。
"""

import json

from tests.ui_helpers import TestClient, _fresh_app, _login, _make_notebook, _ready_notebook, _seed_notebook, _upload


def test_notebook_forms_render_preset_emoji_picker(monkeypatch, tmp_path):
    main, _db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)

        home = client.get("/notebooks")
        assert home.status_code == 200
        assert 'class="emoji-picker"' in home.text
        assert 'name="emoji"' in home.text
        assert "🧠" in home.text
        # The Alpine state must use SINGLE-quoted JS literals. Using tojson
        # (double quotes) collides with the double-quoted HTML attribute and
        # silently breaks selection — guard against that regression.
        assert "x-data=\"{ selected: '📓' }\"" in home.text
        assert "@click=\"selected = '🧠'\"" in home.text
        assert '{ selected: "' not in home.text

        created = client.post(
            "/notebooks/new",
            data={"title": "Research", "emoji": "🧠", "description": ""},
            follow_redirects=False,
        )
        assert created.status_code == 303

        notebook = client.get(created.headers["location"])
        assert notebook.status_code == 200
        assert notebook.text.count('class="emoji-picker"') >= 1
        assert "🧠" in notebook.text
        assert "⚙️" in notebook.text


def test_notebook_grid_caps_large_lists_with_hint(monkeypatch, tmp_path):
    """M4: the notebook landing page should not render an unbounded grid silently."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        with db.connect() as conn:
            user = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
            for i in range(101):
                conn.execute(
                    "INSERT INTO notebooks (user_id, title, updated_at) VALUES (?, ?, datetime('now', ?))",
                    (user["id"], f"Notebook {i:03d}", f"-{i} seconds"),
                )

        home = client.get("/notebooks")
        assert home.status_code == 200
        assert home.text.count('class="card notebook-card"') == 100
        assert "Notebook 000" in home.text
        assert "Notebook 100" not in home.text
        assert "僅顯示最近 100 筆" in home.text


def test_search_caps_each_result_section_with_hint(monkeypatch, tmp_path):
    """M4: search should tell users when a per-type result cap hides older rows."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        with db.connect() as conn:
            user = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
            notebook_id = conn.execute(
                "INSERT INTO notebooks (user_id, title) VALUES (?, 'Search container')",
                (user["id"],),
            ).lastrowid
            for i in range(13):
                conn.execute(
                    """
                    INSERT INTO notes (notebook_id, user_id, title, content, updated_at)
                    VALUES (?, ?, ?, 'content', datetime('now', ?))
                    """,
                    (notebook_id, user["id"], f"needle note {i:02d}", f"-{i} seconds"),
                )

        resp = client.get("/search?q=needle")
        assert resp.status_code == 200
        assert resp.text.count('<span class="result-type">筆記</span>') == 12
        assert "needle note 00" in resp.text
        assert "needle note 12" not in resp.text
        assert "僅顯示最近 12 筆" in resp.text


def test_notebook_renders_mobile_workspace_switcher(monkeypatch, tmp_path):
    """U10: narrow viewports get a pane switcher with chat selected by default."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        _user, notebook_id = _seed_notebook(db, title="Mobile")

        page = client.get(f"/notebooks/{notebook_id}")
        assert page.status_code == 200
        assert 'data-workspace-switcher' in page.text
        assert 'aria-label="工作區面板切換"' in page.text
        assert 'data-workspace-mobile-tabs' in page.text
        assert 'data-active-pane="chat"' in page.text
        assert 'data-workspace-tab="sources">來源</button>' in page.text
        assert 'data-workspace-tab="chat">對話</button>' in page.text
        assert 'data-workspace-tab="studio">工作台</button>' in page.text
        assert 'aria-controls="workspace-sources-pane"' in page.text
        assert 'aria-controls="chat-pane"' in page.text
        assert 'aria-controls="workspace-studio-pane"' in page.text
        assert 'id="workspace-sources-pane"' in page.text
        assert 'id="chat-pane"' in page.text
        assert 'id="workspace-studio-pane"' in page.text
        assert 'data-mobile-pane="sources"' in page.text
        assert 'data-mobile-pane="chat"' in page.text
        assert 'data-mobile-pane="studio"' in page.text
        assert 'class="workspace-mobile-tab is-active"' in page.text
        assert 'aria-selected="true"' in page.text


def test_source_partial_splits_row_and_studio_refresh_events(monkeypatch, tmp_path):
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        with db.connect() as conn:
            user = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
            notebook_id = conn.execute(
                "INSERT INTO notebooks (user_id, title) VALUES (?, 'Events')",
                (user["id"],),
            ).lastrowid
            processing_id = conn.execute(
                """
                INSERT INTO sources (user_id, notebook_id, filename, stored_path, status)
                VALUES (?, ?, 'processing.txt', '/tmp/processing.txt', 'processing')
                """,
                (user["id"], notebook_id),
            ).lastrowid
            indexed_id = conn.execute(
                """
                INSERT INTO sources (user_id, notebook_id, filename, stored_path, status)
                VALUES (?, ?, 'indexed.txt', '/tmp/indexed.txt', 'indexed')
                """,
                (user["id"], notebook_id),
            ).lastrowid

        processing = client.get(f"/notebooks/{notebook_id}/sources/{processing_id}/_partial")
        assert processing.status_code == 200
        assert processing.headers["HX-Trigger"] == "source-status-changed"

        indexed = client.get(f"/notebooks/{notebook_id}/sources/{indexed_id}/_partial")
        assert indexed.status_code == 200
        assert indexed.headers["HX-Trigger"] == "source-status-changed, indexed-sources-changed"


def test_chat_empty_partial_reflects_indexing_state(monkeypatch, tmp_path):
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        with db.connect() as conn:
            user = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
            notebook_id = conn.execute(
                "INSERT INTO notebooks (user_id, title) VALUES (?, 'Empty')",
                (user["id"],),
            ).lastrowid

        url = f"/notebooks/{notebook_id}/_chat-empty"

        # No sources at all.
        empty = client.get(url)
        assert empty.status_code == 200
        assert "先新增一個來源開始使用" in empty.text
        assert "上傳來源" in empty.text
        assert "等待索引" in empty.text
        assert "開始提問" in empty.text
        # The container must re-fetch itself when indexing state changes.
        assert 'hx-trigger="indexed-sources-changed from:body"' in empty.text

        # A source mid-indexing.
        with db.connect() as conn:
            source_id = conn.execute(
                """
                INSERT INTO sources (user_id, notebook_id, filename, stored_path, status)
                VALUES (?, ?, 'doc.txt', '/tmp/doc.txt', 'processing')
                """,
                (user["id"], notebook_id),
            ).lastrowid
        processing = client.get(url)
        assert "索引建立中" in processing.text

        # Once indexed, the center flips to the ask prompt.
        with db.connect() as conn:
            conn.execute("UPDATE sources SET status = 'indexed' WHERE id = ?", (source_id,))
        indexed = client.get(url)
        assert "問任何關於你來源文件的問題" in indexed.text


def test_upload_enqueues_ingest_job_instead_of_running_inline(monkeypatch, tmp_path):
    """P1-1: uploading queues an ingest_jobs row; the source waits for a worker."""
    # Disable the inline worker so nothing drains the queue during the test.
    monkeypatch.setenv("NOTEBOOKLM_INLINE_WORKER", "0")
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        # Seed only the embedding model: upload/indexing must not require chat,
        # because per-source summary generation is best-effort. The /settings
        # route does a live network probe that cannot run in this test.
        with db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_settings (id, provider, base_url, api_key, chat_model, embedding_model) "
                "VALUES (1, 'openai_compatible', 'https://api.example.com/v1', '', '', 'embed')",
            )
        with db.connect() as conn:
            user = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
            notebook_id = conn.execute(
                "INSERT INTO notebooks (user_id, title) VALUES (?, 'Q')",
                (user["id"],),
            ).lastrowid

        page = client.get(f"/notebooks/{notebook_id}")
        assert page.status_code == 200
        assert "llm-not-ready" not in page.text

        resp = client.post(
            f"/notebooks/{notebook_id}/sources/upload",
            files={"files": ("a.txt", b"hello world", "text/plain")},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        with db.connect() as conn:
            source = conn.execute(
                "SELECT id, status FROM sources WHERE notebook_id = ?", (notebook_id,)
            ).fetchone()
            job = conn.execute(
                "SELECT status FROM ingest_jobs WHERE source_id = ?", (source["id"],)
            ).fetchone()
            audit = conn.execute(
                "SELECT * FROM audit_events WHERE action = 'source_uploaded' AND target_id = ?",
                (source["id"],),
            ).fetchone()
        # Source is parked until a worker picks it up; a queued job exists.
        assert source["status"] == "uploaded"
        assert job is not None
        assert job["status"] == "queued"
        assert audit is not None
        assert json.loads(audit["metadata_json"])["filename"] == "a.txt"


def test_global_search_scopes_to_current_user(monkeypatch, tmp_path):
    """U9: global search covers owned content and does not leak other users' rows."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        with db.connect() as conn:
            admin = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
            other = conn.execute("SELECT * FROM users WHERE username = 'user'").fetchone()
            notebook_id = conn.execute(
                "INSERT INTO notebooks (user_id, title, description) VALUES (?, '搜尋測試', 'alpha 專案')",
                (admin["id"],),
            ).lastrowid
            conn.execute(
                "INSERT INTO sources (user_id, notebook_id, filename, stored_path, status, summary) "
                "VALUES (?, ?, 'alpha.txt', '/tmp/a.txt', 'indexed', 'alpha 摘要')",
                (admin["id"], notebook_id),
            )
            convo_id = conn.execute(
                "INSERT INTO conversations (user_id, notebook_id, title) VALUES (?, ?, 'alpha 對話')",
                (admin["id"], notebook_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO notes (notebook_id, user_id, title, content) VALUES (?, ?, 'alpha 筆記', '內容')",
                (notebook_id, admin["id"]),
            )
            other_nb = conn.execute(
                "INSERT INTO notebooks (user_id, title, description) VALUES (?, 'alpha 不可見', '')",
                (other["id"],),
            ).lastrowid
            conn.execute(
                "INSERT INTO notes (notebook_id, user_id, title, content) VALUES (?, ?, 'alpha 私人', '不可見')",
                (other_nb, other["id"]),
            )

        resp = client.get("/search?q=alpha")
        assert resp.status_code == 200
        assert "搜尋測試" in resp.text
        assert "alpha.txt" in resp.text
        assert "alpha 對話" in resp.text
        assert "alpha 筆記" in resp.text
        assert f"conversation_id={convo_id}" in resp.text
        assert "alpha 不可見" not in resp.text
        assert "alpha 私人" not in resp.text


def test_user_data_lifecycle_actions_are_audited(monkeypatch, tmp_path):
    """Audit round 2: notebook/source/chat/note lifecycle mutations are traceable."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        created = client.post(
            "/notebooks/new",
            data={"title": "Audit NB", "emoji": "A", "description": "private description"},
            follow_redirects=False,
        )
        assert created.status_code == 303
        notebook_id = int(created.headers["location"].rstrip("/").split("/")[-1])

        renamed = client.post(
            f"/notebooks/{notebook_id}/rename",
            data={
                "title": "Audit NB renamed",
                "emoji": "B",
                "description": "",
                "followups_setting_present": "1",
                "followups_enabled": "1",
            },
            follow_redirects=False,
        )
        assert renamed.status_code == 303

        with db.connect() as conn:
            user = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
            source_id = conn.execute(
                """
                INSERT INTO sources (user_id, notebook_id, filename, stored_path, status)
                VALUES (?, ?, 'audit-source.txt', '/tmp/missing-audit-source.txt', 'indexed')
                """,
                (user["id"], notebook_id),
            ).lastrowid

        reindexed = client.post(f"/notebooks/{notebook_id}/sources/{source_id}/reindex", follow_redirects=False)
        assert reindexed.status_code == 303
        deleted_source = client.post(f"/notebooks/{notebook_id}/sources/{source_id}/delete", follow_redirects=False)
        assert deleted_source.status_code == 303

        new_convo = client.post(f"/notebooks/{notebook_id}/chat/new", follow_redirects=False)
        assert new_convo.status_code == 303
        conversation_id = int(new_convo.headers["location"].split("conversation_id=")[-1])
        renamed_convo = client.post(
            f"/notebooks/{notebook_id}/chat/{conversation_id}/rename",
            data={"title": "Audit conversation"},
            follow_redirects=False,
        )
        assert renamed_convo.status_code == 303
        deleted_convo = client.post(f"/notebooks/{notebook_id}/chat/{conversation_id}/delete", follow_redirects=False)
        assert deleted_convo.status_code == 303

        added_note = client.post(
            f"/notebooks/{notebook_id}/notes/add",
            data={"title": "Audit note", "content": "Sensitive note content"},
        )
        assert added_note.status_code == 200
        with db.connect() as conn:
            note_id = conn.execute("SELECT id FROM notes WHERE notebook_id = ?", (notebook_id,)).fetchone()["id"]
        edited_note = client.post(
            f"/notebooks/{notebook_id}/notes/{note_id}/edit",
            data={"title": "Audit note edited", "content": "Updated sensitive note content"},
        )
        assert edited_note.status_code == 200
        deleted_note = client.post(f"/notebooks/{notebook_id}/notes/{note_id}/delete")
        assert deleted_note.status_code == 200

        deleted_notebook = client.post(f"/notebooks/{notebook_id}/delete", follow_redirects=False)
        assert deleted_notebook.status_code == 303

        with db.connect() as conn:
            events = [
                dict(row)
                for row in conn.execute(
                    "SELECT action, target_type, sensitivity, metadata_json FROM audit_events ORDER BY id"
                ).fetchall()
            ]
        actions = [event["action"] for event in events]
        for action in [
            "notebook_created",
            "notebook_renamed",
            "source_reindex_requested",
            "source_deleted",
            "conversation_created",
            "conversation_renamed",
            "conversation_deleted",
            "note_added",
            "note_edited",
            "note_deleted",
            "notebook_deleted",
        ]:
            assert action in actions
        high_actions = {event["action"] for event in events if event["sensitivity"] == "high"}
        assert {"source_reindex_requested", "source_deleted", "conversation_deleted", "note_deleted", "notebook_deleted"} <= high_actions
        metadata_blob = "\n".join(event["metadata_json"] for event in events)
        assert "Sensitive note content" not in metadata_blob
        assert "Updated sensitive note content" not in metadata_blob


def test_source_preview_shows_ingestion_diagnostics(monkeypatch, tmp_path):
    """A6a: the preview drawer explains what was extracted, with consequences."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        nb_id, _msg_id = _make_notebook(db)
        diagnostics = {
            "extractor": "pdf_pypdf",
            "chars": 12,
            "sections": 3,
            "chunks": 0,
            "section_kinds": {"body": 2, "table": 1},
            "warnings": [
                {"code": "low_text", "chars": 12, "threshold": 200},
                {"code": "pdf_structure_fallback"},
            ],
            "preview": "只有這幾個字",
        }
        with db.connect() as conn:
            user_id = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()["id"]
            source_id = conn.execute(
                "INSERT INTO sources (user_id, notebook_id, filename, stored_path, status, diagnostics_json) "
                "VALUES (?, ?, 'scan.pdf', '/tmp/scan.pdf', 'indexed', ?)",
                (user_id, nb_id, json.dumps(diagnostics)),
            ).lastrowid

        preview = client.get(f"/notebooks/{nb_id}/sources/{source_id}/preview")

        assert preview.status_code == 200
        assert "萃取診斷" in preview.text
        assert "PDF（僅整頁文字）" in preview.text     # extractor path, localised
        assert "掃描影像" in preview.text              # low_text warning explains the cause
        assert "只有這幾個字" in preview.text          # extracted-text preview
        assert "表格" in preview.text                  # section-kind breakdown


def _failed_extract_diagnostics() -> dict:
    """The diagnostics blob ingest persists when extraction yields nothing.

    Produced by `collect_ingest_diagnostics` so the drawer test is pinned to the
    real shape. Hand-writing this is how a renderer and its producer drift apart
    without any test noticing (AGENTS.md → Writing tests that actually hold).
    """
    from app import ingest as ingest_module

    extraction = ingest_module.ExtractionResult(
        sections=[], extractor="pdf_pypdf", notes=[]
    )
    diagnostics = ingest_module.collect_ingest_diagnostics(extraction, [])
    diagnostics["failed_stage"] = "extract"
    return diagnostics


def test_failed_source_row_is_openable_and_flags_warnings(monkeypatch, tmp_path):
    """A failed source must be inspectable — its diagnostics are the only clue."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        nb_id, _msg_id = _make_notebook(db)
        with db.connect() as conn:
            user_id = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()["id"]
            source_id = conn.execute(
                "INSERT INTO sources (user_id, notebook_id, filename, stored_path, status, error, diagnostics_json) "
                "VALUES (?, ?, 'broken.pdf', '/tmp/broken.pdf', 'failed', '解析失敗', ?)",
                # Built by the real producer, not hand-written: a fabricated blob
                # would keep this drawer test green even if the shape it renders
                # stopped matching what ingest actually persists.
                (user_id, nb_id, json.dumps(_failed_extract_diagnostics())),
            ).lastrowid

        row = client.get(f"/notebooks/{nb_id}/sources/{source_id}/_partial")

        assert row.status_code == 200
        assert "點擊查看萃取診斷" in row.text        # not disabled any more
        assert "source-warn" in row.text            # warning marker on the row

        preview = client.get(f"/notebooks/{nb_id}/sources/{source_id}/preview")
        assert preview.status_code == 200
        assert "讀取檔案內容" in preview.text        # which stage broke
        assert "沒有萃取到任何文字" in preview.text


def test_oversized_file_is_rejected_and_leaves_nothing_on_disk(monkeypatch, tmp_path):
    """A file past the per-file cap gets 413, and its partial write is cleaned up."""
    main, db = _fresh_app(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "UPLOAD_MAX_FILE_BYTES", 1024)
    monkeypatch.setattr(main.config.runtime, "upload_max_file_bytes", 1024)
    with TestClient(main.app) as client:
        _login(client)
        notebook_id = _ready_notebook(main, db, client)

        response = _upload(client, notebook_id, "big.txt", b"x" * 5000)
        assert response.status_code == 413

        # Nothing recorded, and nothing left behind.
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"] == 0
        upload_root = tmp_path / "data" / "uploads"
        leftovers = list(upload_root.rglob("*")) if upload_root.exists() else []
        assert [p for p in leftovers if p.is_file()] == []


def test_file_at_the_limit_is_accepted(monkeypatch, tmp_path):
    """The cap is an upper bound, not an off-by-one that rejects the boundary."""
    main, db = _fresh_app(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "UPLOAD_MAX_FILE_BYTES", 1024)
    monkeypatch.setattr(main.config.runtime, "upload_max_file_bytes", 1024)
    with TestClient(main.app) as client:
        _login(client)
        notebook_id = _ready_notebook(main, db, client)

        assert _upload(client, notebook_id, "ok.txt", b"x" * 1024).status_code == 303
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"] == 1


def test_oversized_request_is_refused_from_content_length(monkeypatch, tmp_path):
    """The whole request is refused up front, before any body is read.

    This is the memory guard: `Content-Length` is checked in the middleware, so
    an oversized upload never reaches the point of being buffered or spooled.
    """
    main, db = _fresh_app(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "MAX_REQUEST_BYTES", 512)
    with TestClient(main.app) as client:
        _login(client)
        notebook_id = _ready_notebook(main, db, client)

        response = _upload(client, notebook_id, "big.txt", b"x" * 4000)
        assert response.status_code == 413


def test_oversized_spreadsheet_is_refused_at_upload_not_in_the_worker(monkeypatch, tmp_path):
    """The two-stage-rejection fix: a big .xlsx fails immediately, with a reason.

    Before this, a spreadsheet under the upload cap but over the extract cap
    uploaded successfully and then failed in the worker minutes later. It is now
    one 413 at upload, worded so the stricter number does not contradict the
    limit the upload widget advertises.
    """
    import app.i18n as i18n

    main, db = _fresh_app(monkeypatch, tmp_path)
    monkeypatch.setattr(main.config.runtime, "upload_max_file_bytes", 8000)
    monkeypatch.setattr(main.config.runtime, "extract_max_file_bytes", 1000)
    monkeypatch.setattr(main, "UPLOAD_MAX_FILE_BYTES", 8000)
    with TestClient(main.app) as client:
        _login(client)
        notebook_id = _ready_notebook(main, db, client)

        # A PDF of the same size is fine: it only meets the general cap.
        assert _upload(client, notebook_id, "ok.pdf", b"x" * 5000).status_code == 303

        response = client.post(
            f"/notebooks/{notebook_id}/sources/upload",
            files={"files": ("rows.csv", b"x" * 5000, "text/csv")},
            follow_redirects=False,
        )
        assert response.status_code == 413
        # The wording must explain *why* this format's number is smaller.
        assert "試算表" in response.text or "壓縮檔" in response.text
        assert i18n.t("upload.file_too_large_eager_format", filename="rows.csv", limit_mb=1) != ""

        with db.connect() as conn:
            rows = conn.execute("SELECT filename FROM sources").fetchall()
        assert [r["filename"] for r in rows] == ["ok.pdf"]


def test_reindex_marks_the_source_queued_so_its_row_keeps_polling(monkeypatch, tmp_path):
    """A queued source must look queued, or the UI silently stops tracking it.

    `_source_item.html` only carries its HTMX polling attributes while the
    status is uploaded/processing. Reindex used to enqueue the job without
    touching `sources.status`, so the row re-rendered as `indexed` with no
    polling — the worker flipped the status seconds later and the page showed
    stale text until a manual reload.
    """
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        _, notebook_id = _seed_notebook(db)
        with db.connect() as conn:
            user = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
            source_id = conn.execute(
                """
                INSERT INTO sources (user_id, notebook_id, filename, stored_path, status, error)
                VALUES (?, ?, 'stale.txt', '/tmp/missing-stale.txt', 'indexed', 'previous failure')
                """,
                (user["id"], notebook_id),
            ).lastrowid
            conn.commit()

        row = client.get(f"/notebooks/{notebook_id}/sources/{source_id}/_partial")
        assert "hx-trigger" not in row.text, "an indexed source should not poll"

        assert client.post(
            f"/notebooks/{notebook_id}/sources/{source_id}/reindex", follow_redirects=False
        ).status_code == 303

        with db.connect() as conn:
            source = dict(conn.execute(
                "SELECT status, error FROM sources WHERE id = ?", (source_id,)
            ).fetchone())
        assert source["status"] == "uploaded"
        assert source["error"] == "", "the stale error must be cleared on retry"

        # The rendered row now carries the polling attributes, which is the part
        # the user actually sees fail.
        row = client.get(f"/notebooks/{notebook_id}/sources/{source_id}/_partial")
        assert 'hx-trigger="every 2s' in row.text
        assert f"/notebooks/{notebook_id}/sources/{source_id}/_partial" in row.text
