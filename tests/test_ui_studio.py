"""Studio 工具與產出架的 UI 測試。

涵蓋會議紀錄、來源比較、翻譯、產出物（artifact）與筆記編輯，以及產出架的分類
標籤、篩選與偽造 kind 的拒絕。
契約文件：docs/UI.md、docs/ROADMAP.md。

由 tests/test_ui.py 拆出；共用固件見 tests/ui_helpers.py。
"""

from html.parser import HTMLParser

import pytest

from tests.ui_helpers import TestClient, _fresh_app, _login, _make_notebook, _seed_indexed_source, _seed_notebook


def test_minutes_renders_with_save_button_no_autosave(monkeypatch, tmp_path):
    """A1: minutes generation renders the result + a save button but does NOT
    auto-save; the model only offers a savable result when it produces minutes."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    async def fake_minutes(chunks, settings, **kwargs):
        return "## 會議主題\n測試會議\n\n## 重要決議\n- 通過提案"

    monkeypatch.setattr(main, "generate_meeting_minutes", fake_minutes)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)
        with db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_settings (id, provider, base_url, api_key, chat_model, embedding_model) "
                "VALUES (1, 'openai_compatible', 'https://x/v1', ?, 'chat', 'embed')",
                (db.encrypt_for_storage("sk-test"),),
            )
            source_id = conn.execute(
                "INSERT INTO sources (user_id, notebook_id, filename, stored_path, status) "
                "VALUES (?, ?, 'meeting.txt', '/tmp/m.txt', 'indexed')",
                (user["id"], notebook_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO chunks (user_id, source_id, chunk_index, location, text, embedding_json) "
                "VALUES (?, ?, 0, 'document', '會議逐字稿內容', '[]')",
                (user["id"], source_id),
            )

        resp = client.post(f"/notebooks/{notebook_id}/minutes", data={"source_id": source_id})
        assert resp.status_code == 200
        assert "重要決議" in resp.text
        assert "存成筆記" in resp.text  # manual save offered
        assert resp.headers.get("HX-Trigger") is None  # not auto-saved
        with db.connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) c FROM notes WHERE notebook_id = ?", (notebook_id,)
            ).fetchone()["c"] == 0


def test_minutes_warns_before_non_meeting_source(monkeypatch, tmp_path):
    """Non-meeting sources show a warning first and do not spend an LLM call."""
    main, db = _fresh_app(monkeypatch, tmp_path)
    calls = {"n": 0}
    ambiguous = main.meeting_likelihood([{"text": "主持人：藥師\n這是一份藥品仿單，包含劑量資訊。"}])
    assert ambiguous["is_likely"] is False
    assert "只看到「主持人或講者欄位」" in ambiguous["reason"]
    assert "發言者標記" not in ambiguous["reason"]

    async def fake_minutes(chunks, settings, **kwargs):
        calls["n"] += 1
        return "## 會議主題\n不應先產生"

    monkeypatch.setattr(main, "generate_meeting_minutes", fake_minutes)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)
        with db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_settings (id, provider, base_url, api_key, chat_model, embedding_model) "
                "VALUES (1, 'openai_compatible', 'https://x/v1', ?, 'chat', 'embed')",
                (db.encrypt_for_storage("sk-test"),),
            )
            source_id = conn.execute(
                "INSERT INTO sources (user_id, notebook_id, filename, stored_path, status) "
                "VALUES (?, ?, 'label.txt', '/tmp/label.txt', 'indexed')",
                (user["id"], notebook_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO chunks (user_id, source_id, chunk_index, location, text, embedding_json) "
                "VALUES (?, ?, 0, 'document', '這是一份藥品仿單，包含劑量、禁忌症與副作用資訊。', '[]')",
                (user["id"], source_id),
            )

        resp = client.post(f"/notebooks/{notebook_id}/minutes", data={"source_id": source_id})
        assert resp.status_code == 200
        assert "不像會議逐字稿" in resp.text
        assert "仍然整理" in resp.text
        assert "發言者標記" not in resp.text
        assert calls["n"] == 0
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) c FROM notes WHERE notebook_id = ?", (notebook_id,)).fetchone()["c"] == 0

        forced = client.post(f"/notebooks/{notebook_id}/minutes", data={"source_id": source_id, "force": "1"})
        assert forced.status_code == 200
        assert "存成筆記" in forced.text  # forced result offers manual save
        assert calls["n"] == 1
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) c FROM notes WHERE notebook_id = ?", (notebook_id,)).fetchone()["c"] == 0


def test_minutes_decline_is_not_saved(monkeypatch, tmp_path):
    """If the model says the source is not meeting-like, show it but don't save."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    async def fake_minutes(chunks, settings, **kwargs):
        return "This does not look like a meeting record."

    monkeypatch.setattr(main, "generate_meeting_minutes", fake_minutes)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)
        with db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_settings (id, provider, base_url, api_key, chat_model, embedding_model) "
                "VALUES (1, 'openai_compatible', 'https://x/v1', ?, 'chat', 'embed')",
                (db.encrypt_for_storage("sk-test"),),
            )
            source_id = conn.execute(
                "INSERT INTO sources (user_id, notebook_id, filename, stored_path, status) "
                "VALUES (?, ?, 'meeting.txt', '/tmp/meeting.txt', 'indexed')",
                (user["id"], notebook_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO chunks (user_id, source_id, chunk_index, location, text, embedding_json) "
                "VALUES (?, ?, 0, 'document', '會議逐字稿\n主持人：今天討論專案進度。', '[]')",
                (user["id"], source_id),
            )

        resp = client.post(f"/notebooks/{notebook_id}/minutes", data={"source_id": source_id})
        assert resp.status_code == 200
        assert "不像會議記錄" in resp.text
        assert "存成筆記" not in resp.text  # non-meeting source offers no save
        assert resp.headers.get("HX-Trigger") is None
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) c FROM notes WHERE notebook_id = ?", (notebook_id,)).fetchone()["c"] == 0


def test_compare_panel_exposes_optional_topic_with_three_source_limit(monkeypatch, tmp_path):
    """U17: the compare tool explains when it uses summaries vs topic retrieval."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)
        for name in ("a.pdf", "b.pdf", "c.pdf"):
            _seed_indexed_source(db, user["id"], notebook_id, name)

        panel = client.get(f"/notebooks/{notebook_id}/tools/compare")
        other_panel = client.get(f"/notebooks/{notebook_id}/tools/minutes")

    assert panel.status_code == 200
    assert 'name="focus"' in panel.text
    assert "比較主題（選填）" in panel.text
    assert "最多選擇 3 份來源" in panel.text
    assert "留白時依來源摘要比較" in panel.text
    assert "最多選擇 10 份來源" in panel.text
    assert 'data-swap-validation-errors="400"' in panel.text
    assert 'data-swap-validation-errors' not in other_panel.text


@pytest.mark.parametrize("focus", ["", "保固期間"])
def test_comparison_legend_matches_real_prompt_and_survives_note_export(monkeypatch, tmp_path, focus):
    """Real route -> compare prompt -> rendered save form -> note -> export."""
    import app.llm as llm

    main, db = _fresh_app(monkeypatch, tmp_path)
    captured = {}
    generated = "## 差異\n- [1] a_[draft].pdf 與 [2] b.pdf 的保固期間不同。"

    async def fake_chat(settings, user_prompt, system_prompt, **kwargs):
        captured["prompt"] = user_prompt
        return generated

    async def fake_retrieve(question, rows, settings, history, user_id, source_ids, **kwargs):
        name = names_by_id[source_ids[0]]
        if name == "c.pdf":
            return []
        return [
            {"text": "保固期間", "location": "page 2", "score": 0.9},
            {"text": "保固範圍", "location": "page 3", "score": 0.8},
            {"text": "  ", "location": "page 4", "score": 0.7},
        ]

    class SaveFormParser(HTMLParser):
        content = None

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "input" and attrs.get("name") == "content":
                self.content = attrs["value"]

    monkeypatch.setattr(llm, "chat_completion", fake_chat)
    monkeypatch.setattr(main, "retrieve", fake_retrieve)
    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)
        with db.connect() as conn:
            conn.execute("UPDATE llm_settings SET chat_model = 'chat' WHERE id = 1")
        names_by_id = {
            _seed_indexed_source(db, user["id"], notebook_id, name, f"summary {name}"): name
            for name in ("c.pdf", "b.pdf", "a_[draft].pdf")
        }
        response = client.post(
            f"/notebooks/{notebook_id}/compare",
            data={"source_ids": list(names_by_id), "focus": focus},
        )
        assert response.status_code == 200
        parser = SaveFormParser()
        parser.feed(response.text)
        content = parser.content
        assert content is not None
        assert content.startswith("### 來源對照與證據狀態\n")
        for index, filename in enumerate(sorted(names_by_id.values()), 1):
            assert f"[{index}] {filename}\n" in captured["prompt"]
            safe_name = filename.replace("_", "\\_").replace("[", "\\[").replace("]", "\\]")
            assert f"- **[{index}] {safe_name}** — " in content
        if focus:
            assert content.count("已取得主題證據（2 段）") == 2
            assert "- **[3] c.pdf** — 未取得足夠主題證據" in content
            assert "[3] c.pdf\n[NO_RELEVANT_TOPIC_EVIDENCE]" in captured["prompt"]
        else:
            assert content.count("使用摘要／節錄") == 3
            assert "[NO_RELEVANT_TOPIC_EVIDENCE]" not in captured["prompt"]
        assert content.endswith(generated)
        assert "來源對照與證據狀態" in response.text.split('class="save-note-form"')[0]
        saved = client.post(
            f"/notebooks/{notebook_id}/notes/add",
            data={"title": "比較測試", "content": content, "kind": "compare"},
        )
        assert saved.status_code == 200
        with db.connect() as conn:
            row = conn.execute("SELECT content FROM notes WHERE notebook_id = ?", (notebook_id,)).fetchone()
        assert row["content"] == content
        exported = client.get(f"/notebooks/{notebook_id}/notes/export")
        assert exported.status_code == 200
        assert content in exported.text


def test_compare_topic_retrieves_each_source_but_blank_topic_keeps_summaries(monkeypatch, tmp_path):
    """U17: a topic scopes one real retrieval run per authorised source."""
    main, db = _fresh_app(monkeypatch, tmp_path)
    retrieval_calls = []
    compare_calls = []

    async def fake_retrieve(question, rows, settings, history, user_id, source_ids=None, **kwargs):
        source_id = source_ids[0]
        retrieval_calls.append((question, list(source_ids)))
        if len(retrieval_calls) == 3:
            return [{
                "id": source_id * 10,
                "source_id": source_id,
                "filename": f"source-{source_id}.pdf",
                "location": f"page {source_id}",
                "text": "off-topic result",
                "score": 0.1,
            }]
        return [{
            "id": source_id * 10,
            "source_id": source_id,
            "filename": f"source-{source_id}.pdf",
            "location": f"page {source_id}",
            "text": f"topic evidence {source_id}",
            "score": 0.9,
        }]

    async def fake_compare(items, focus, settings, **kwargs):
        compare_calls.append((items, focus))
        return "比較完成"

    monkeypatch.setattr(main, "retrieve", fake_retrieve)
    monkeypatch.setattr(main, "compare_sources", fake_compare)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)
        with db.connect() as conn:
            conn.execute(
                "UPDATE llm_settings SET chat_model = 'chat', embedding_model = 'embed' WHERE id = 1"
            )
        source_ids = [
            _seed_indexed_source(db, user["id"], notebook_id, name, f"summary {name}")
            for name in ("a.pdf", "b.pdf", "c.pdf")
        ]
        with db.connect() as conn:
            foreign_user_id = conn.execute(
                "SELECT id FROM users WHERE username = 'user'"
            ).fetchone()["id"]
            foreign_notebook_id = conn.execute(
                "INSERT INTO notebooks (user_id, title) VALUES (?, 'foreign')",
                (foreign_user_id,),
            ).lastrowid
        foreign_source_id = _seed_indexed_source(
            db, foreign_user_id, foreign_notebook_id, "foreign.pdf", "foreign summary"
        )

        focused = client.post(
            f"/notebooks/{notebook_id}/compare",
            data={
                "source_ids": [str(sid) for sid in [*source_ids, foreign_source_id]],
                "focus": "風險控管",
            },
        )
        summary_source_ids = [
            *source_ids,
            _seed_indexed_source(db, user["id"], notebook_id, "d.pdf", "summary d.pdf"),
        ]
        blank = client.post(
            f"/notebooks/{notebook_id}/compare",
            data={"source_ids": [str(sid) for sid in summary_source_ids], "focus": "   "},
        )

    assert focused.status_code == 200
    assert retrieval_calls == [("風險控管", [sid]) for sid in source_ids]
    focused_items, focused_topic = compare_calls[0]
    assert focused_topic == "風險控管"
    assert f"page {source_ids[0]}" in focused_items[0]["summary"]
    assert f"topic evidence {source_ids[0]}" in focused_items[0]["summary"]
    assert focused_items[2]["summary"] == main.NO_RELEVANT_TOPIC_EVIDENCE

    assert blank.status_code == 200
    assert len(retrieval_calls) == 3
    blank_items, blank_topic = compare_calls[1]
    assert blank_topic == ""
    assert [item["summary"] for item in blank_items] == [
        "summary a.pdf",
        "summary b.pdf",
        "summary c.pdf",
        "summary d.pdf",
    ]


@pytest.mark.parametrize("htmx_request", [False, True])
def test_compare_topic_rejects_four_sources_before_retrieval(monkeypatch, tmp_path, htmx_request):
    """U17: topic mode must not create an unbounded LLM/retrieval fan-out."""
    main, db = _fresh_app(monkeypatch, tmp_path)
    retrieval_calls = []
    compare_calls = []

    async def fake_retrieve(*args, **kwargs):
        retrieval_calls.append((args, kwargs))
        return []

    async def fake_compare(*_args, **_kwargs):
        compare_calls.append((_args, _kwargs))
        return "不應執行比較"

    monkeypatch.setattr(main, "retrieve", fake_retrieve)
    monkeypatch.setattr(main, "compare_sources", fake_compare)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)
        with db.connect() as conn:
            conn.execute(
                "UPDATE llm_settings SET chat_model = 'chat', embedding_model = 'embed' WHERE id = 1"
            )
        source_ids = [
            _seed_indexed_source(db, user["id"], notebook_id, f"{index}.pdf")
            for index in range(4)
        ]

        response = client.post(
            f"/notebooks/{notebook_id}/compare",
            data={"source_ids": [str(sid) for sid in source_ids], "focus": "成本"},
            headers={"HX-Request": "true"} if htmx_request else {},
        )

    assert response.status_code == 400
    assert "最多只能比較 3 份來源" in response.text
    assert response.headers["content-type"].startswith("text/html")
    assert 'role="alert"' in response.text
    assert retrieval_calls == []
    assert compare_calls == []


@pytest.mark.parametrize("focus", ["", "   "])
def test_compare_summaries_accepts_ten_but_rejects_eleven(monkeypatch, tmp_path, focus):
    """Summary comparison is bounded before generation and never retrieves."""
    main, db = _fresh_app(monkeypatch, tmp_path)
    compare_calls = []
    retrieval_calls = []

    async def fake_compare(items, topic, settings, **kwargs):
        compare_calls.append((items, topic))
        return "摘要比較完成"

    async def fake_retrieve(*args, **kwargs):
        retrieval_calls.append((args, kwargs))
        return []

    monkeypatch.setattr(main, "compare_sources", fake_compare)
    monkeypatch.setattr(main, "retrieve", fake_retrieve)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)
        with db.connect() as conn:
            conn.execute("UPDATE llm_settings SET chat_model = 'chat' WHERE id = 1")
        source_ids = [
            _seed_indexed_source(db, user["id"], notebook_id, f"{index:02}.pdf", f"summary {index}")
            for index in range(11)
        ]
        rejected = client.post(
            f"/notebooks/{notebook_id}/compare",
            data={"source_ids": [str(sid) for sid in source_ids], "focus": focus},
            headers={"HX-Request": "true"},
        )
        accepted = client.post(
            f"/notebooks/{notebook_id}/compare",
            data={"source_ids": [str(sid) for sid in source_ids[:10]], "focus": focus},
            headers={"HX-Request": "true"},
        )

    assert rejected.status_code == 400
    assert "未輸入比較主題時，最多只能比較 10 份來源" in rejected.text
    assert 'role="alert"' in rejected.text
    assert accepted.status_code == 200
    assert len(compare_calls) == 1
    assert len(compare_calls[0][0]) == 10
    assert compare_calls[0][1] == ""
    assert retrieval_calls == []


def test_studio_tools_tile_gating(monkeypatch, tmp_path):
    """U16: the tools launcher enables the compare tile only with >=2 indexed sources."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)

        # No indexed sources: grid present, compare disabled.
        resp = client.get(f"/notebooks/{notebook_id}/_tools")
        assert resp.status_code == 200
        assert 'class="tool-grid"' in resp.text
        assert "來源比較" in resp.text and "學習指南" in resp.text
        assert resp.text.count("disabled") >= 5  # every tile disabled at 0 indexed

        # One indexed source: artifact/minutes tiles enabled, compare still disabled.
        _seed_indexed_source(db, user["id"], notebook_id, "a.pdf")
        resp = client.get(f"/notebooks/{notebook_id}/_tools")
        assert "/notebooks/%d/tools/study_guide" % notebook_id in resp.text
        # compare tile (needs 2) is still disabled -> no compare tool link
        assert "/tools/compare" not in resp.text

        # Two indexed sources: compare enabled.
        _seed_indexed_source(db, user["id"], notebook_id, "b.pdf")
        resp = client.get(f"/notebooks/{notebook_id}/_tools")
        assert "/tools/compare" in resp.text


def test_tool_panel_renders_each_kind(monkeypatch, tmp_path):
    """U16: each tool kind returns a modal panel; unknown kind 404s."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)
        _seed_indexed_source(db, user["id"], notebook_id, "a.pdf")
        _seed_indexed_source(db, user["id"], notebook_id, "b.pdf")

        compare = client.get(f"/notebooks/{notebook_id}/tools/compare")
        assert compare.status_code == 200
        assert "開始比較" in compare.text and 'id="tool-result"' in compare.text

        guide = client.get(f"/notebooks/{notebook_id}/tools/study_guide")
        assert guide.status_code == 200
        assert "生成學習指南" in guide.text

        assert client.get(f"/notebooks/{notebook_id}/tools/nope").status_code == 404


def test_artifact_renders_with_save_button_no_autosave(monkeypatch, tmp_path):
    """A4: artifact generation shows the result + a save button but does NOT
    auto-save; the user saves manually via /notes/add."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    async def fake_artifact(kind, summaries, settings, **kwargs):
        assert kind == "study_guide"
        return "## 核心概念\n- 測試"

    monkeypatch.setattr(main, "generate_artifact", fake_artifact)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db, title="A4測試")
        with db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_settings (id, provider, base_url, api_key, chat_model, embedding_model) "
                "VALUES (1, 'openai_compatible', 'https://x/v1', ?, 'chat', 'embed')",
                (db.encrypt_for_storage("sk-test"),),
            )
        source_id = _seed_indexed_source(db, user["id"], notebook_id, "a.pdf", summary="一份關於主題的摘要。")

        # The panel now posts the picked sources (all checked by default).
        resp = client.post(
            f"/notebooks/{notebook_id}/artifacts/study_guide",
            data={"source_ids": [str(source_id)]},
        )
        assert resp.status_code == 200
        assert "核心概念" in resp.text
        # Manual save: a save button is offered, nothing auto-saved.
        assert "存成筆記" in resp.text
        # A single-source run names the file (more precise than the notebook
        # title, and it makes repeat runs tell themselves apart in the shelf).
        assert 'name="title" value="學習指南 — a.pdf"' in resp.text
        assert resp.headers.get("HX-Trigger") is None
        with db.connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) c FROM notes WHERE notebook_id = ?", (notebook_id,)
            ).fetchone()["c"] == 0

        # Saving manually via /notes/add persists it.
        saved = client.post(
            f"/notebooks/{notebook_id}/notes/add",
            data={"title": "學習指南 — A4測試", "content": "## 核心概念\n- 測試"},
        )
        assert saved.status_code == 200
        with db.connect() as conn:
            note = conn.execute(
                "SELECT title, content FROM notes WHERE notebook_id = ?", (notebook_id,)
            ).fetchone()
        assert note is not None and note["title"].startswith("學習指南")
        assert "核心概念" in note["content"]

        # Unknown artifact kind -> 404.
        assert client.post(f"/notebooks/{notebook_id}/artifacts/nope").status_code == 404


def test_edit_note_updates_in_place(monkeypatch, tmp_path):
    """U8: editing a note updates title/content and returns the refreshed shelf."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)
        with db.connect() as conn:
            note_id = conn.execute(
                "INSERT INTO notes (notebook_id, user_id, title, content) VALUES (?, ?, '舊標題', '舊內容')",
                (notebook_id, user["id"]),
            ).lastrowid

        # The notes shelf offers an edit affordance.
        shelf = client.get(f"/notebooks/{notebook_id}/_notes")
        assert "編輯" in shelf.text and "note-edit-form" in shelf.text

        resp = client.post(
            f"/notebooks/{notebook_id}/notes/{note_id}/edit",
            data={"title": "新標題", "content": "新內容"},
        )
        assert resp.status_code == 200
        assert "新標題" in resp.text and "新內容" in resp.text
        with db.connect() as conn:
            note = conn.execute("SELECT title, content FROM notes WHERE id = ?", (note_id,)).fetchone()
        assert note["title"] == "新標題" and note["content"] == "新內容"

        # Empty content is rejected; a foreign/missing note 404s.
        assert client.post(
            f"/notebooks/{notebook_id}/notes/{note_id}/edit", data={"title": "x", "content": "   "}
        ).status_code == 400
        assert client.post(
            f"/notebooks/{notebook_id}/notes/999999/edit", data={"title": "x", "content": "y"}
        ).status_code == 404


def test_translate_renders_with_save_button(monkeypatch, tmp_path):
    """A5: translate-summary shows the translation + a save button; bad language 400s."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    async def fake_translate(text, target_language, settings, **kwargs):
        assert target_language == "English"
        return "Translated summary."

    monkeypatch.setattr(main, "translate_summary", fake_translate)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)
        with db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_settings (id, provider, base_url, api_key, chat_model, embedding_model) "
                "VALUES (1, 'openai_compatible', 'https://x/v1', ?, 'chat', 'embed')",
                (db.encrypt_for_storage("sk-test"),),
            )
        source_id = _seed_indexed_source(db, user["id"], notebook_id, "a.pdf", summary="一份摘要。")

        # Tool panel offers a source + language picker.
        panel = client.get(f"/notebooks/{notebook_id}/tools/translate")
        assert panel.status_code == 200
        assert "翻譯摘要" in panel.text and 'name="target_language"' in panel.text

        resp = client.post(
            f"/notebooks/{notebook_id}/translate",
            data={"source_id": source_id, "target_language": "English"},
        )
        assert resp.status_code == 200
        assert "Translated summary." in resp.text
        assert "存成筆記" in resp.text
        assert resp.headers.get("HX-Trigger") is None

        # Non-allowlisted language is rejected (no arbitrary prompt input).
        assert client.post(
            f"/notebooks/{notebook_id}/translate",
            data={"source_id": source_id, "target_language": "Klingon"},
        ).status_code == 400


def test_outputs_shelf_records_and_badges_the_entry_type(monkeypatch, tmp_path):
    """U16 Phase 2: a saved tool result keeps the kind that produced it."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        nb_id, _msg_id = _make_notebook(db)

        shelf = client.post(
            f"/notebooks/{nb_id}/notes/add",
            data={"title": "學習指南 — 產出架測試", "content": "# 重點", "kind": "study_guide"},
        )

        assert shelf.status_code == 200
        with db.connect() as conn:
            row = conn.execute("SELECT kind FROM notes WHERE notebook_id = ?", (nb_id,)).fetchone()
        assert row["kind"] == "study_guide"
        assert "學習指南" in shelf.text          # type badge rendered on the entry
        # A single kind needs no filter row.
        assert "note-filters" not in shelf.text


def test_outputs_shelf_filter_appears_once_two_kinds_exist(monkeypatch, tmp_path):
    """The client-side type filter shows up only when it has something to do."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        nb_id, msg_id = _make_notebook(db)

        client.post(
            f"/notebooks/{nb_id}/notes/add",
            data={"title": "來源比較：a vs b", "content": "比較", "kind": "compare"},
        )
        shelf = client.post(f"/notebooks/{nb_id}/notes/pin", data={"message_id": str(msg_id)})

        assert shelf.status_code == 200
        assert "note-filters" in shelf.text
        assert "釘選回答" in shelf.text
        assert "來源比較" in shelf.text
        with db.connect() as conn:
            kinds = sorted(r["kind"] for r in conn.execute(
                "SELECT kind FROM notes WHERE notebook_id = ?", (nb_id,)
            ))
        assert kinds == ["compare", "pinned"]


def test_outputs_shelf_rejects_forged_or_unknown_kinds(monkeypatch, tmp_path):
    """Only SAVABLE_NOTE_KINDS may be set through save-to-shelf.

    'pinned' is excluded on purpose: pinning has its own route, so a saved
    artifact cannot claim to be a pinned chat answer.
    """
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        nb_id, _msg_id = _make_notebook(db)

        for sent in ("pinned", "not_a_kind"):
            response = client.post(
                f"/notebooks/{nb_id}/notes/add",
                data={"title": f"t-{sent}", "content": "c", "kind": sent},
            )
            assert response.status_code == 200

        with db.connect() as conn:
            kinds = [r["kind"] for r in conn.execute(
                "SELECT kind FROM notes WHERE notebook_id = ? ORDER BY id", (nb_id,)
            )]
        assert kinds == ["note", "note"]


def test_note_export_carries_the_type_label(monkeypatch, tmp_path):
    """A downloaded shelf still says what produced each entry."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        nb_id, _msg_id = _make_notebook(db)
        client.post(
            f"/notebooks/{nb_id}/notes/add",
            data={"title": "時間軸 — 產出架測試", "content": "1999 年…", "kind": "timeline"},
        )

        export = client.get(f"/notebooks/{nb_id}/notes/export")

        assert export.status_code == 200
        assert "時間軸 ·" in export.text


def test_artifact_tools_honour_the_source_picker(monkeypatch, tmp_path):
    """A4 tools used the whole notebook with no way to narrow them; now the
    panel picks sources and the route respects the selection."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        nb_id, _msg_id = _make_notebook(db)
        with db.connect() as conn:
            user_id = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()["id"]
            ids = {}
            for name, summary in (("表格.xlsx", "異常現象與處理方法"), ("簡報.pptx", "EAP 導入專案時程")):
                ids[name] = conn.execute(
                    "INSERT INTO sources (user_id, notebook_id, filename, stored_path, status, summary) "
                    "VALUES (?, ?, ?, '/tmp/x', 'indexed', ?)",
                    (user_id, nb_id, name, summary),
                ).lastrowid

        panel = client.get(f"/notebooks/{nb_id}/tools/faq")
        assert panel.status_code == 200
        # Every indexed source is offered and pre-checked (previous default).
        assert panel.text.count('name="source_ids"') == 2
        assert panel.text.count("checked") == 2
        assert "表格.xlsx" in panel.text and "簡報.pptx" in panel.text

        captured = {}

        async def fake_generate_artifact(kind, summaries, settings, **kwargs):
            captured["filenames"] = [s["filename"] for s in summaries]
            return "# 產出"

        monkeypatch.setattr(main, "generate_artifact", fake_generate_artifact)
        with db.connect() as conn:
            conn.execute("UPDATE llm_settings SET chat_model = 'x' WHERE id = 1")

        result = client.post(
            f"/notebooks/{nb_id}/artifacts/faq",
            data={"source_ids": [str(ids["簡報.pptx"])]},
        )

        assert result.status_code == 200
        # The deselected spreadsheet must not reach the prompt.
        assert captured["filenames"] == ["簡報.pptx"]
        # A single-source run names the file, so two runs are distinguishable.
        assert 'value="常見問答 — 簡報.pptx"' in result.text


def test_artifact_tools_reject_an_empty_selection(monkeypatch, tmp_path):
    """Deselecting everything must be an error, never a silent whole-notebook run."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        nb_id, _msg_id = _make_notebook(db)
        with db.connect() as conn:
            user_id = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()["id"]
            conn.execute(
                "INSERT INTO sources (user_id, notebook_id, filename, stored_path, status, summary) "
                "VALUES (?, ?, 'a.pdf', '/tmp/a', 'indexed', '摘要')",
                (user_id, nb_id),
            )

        response = client.post(f"/notebooks/{nb_id}/artifacts/faq", data={})

        assert response.status_code == 400
        assert "請至少選擇一個來源" in response.text


def test_shelf_shows_the_time_so_same_day_entries_differ(monkeypatch, tmp_path):
    """Two runs of one tool on one day looked identical when only a date showed."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        nb_id, _msg_id = _make_notebook(db)
        with db.connect() as conn:
            user_id = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()["id"]
            for stamp in ("2026-07-25 14:35:16", "2026-07-25 14:35:54"):
                conn.execute(
                    "INSERT INTO notes (notebook_id, user_id, title, content, kind, created_at) "
                    "VALUES (?, ?, '常見問答 — NB', 'x', 'faq', ?)",
                    (nb_id, user_id, stamp),
                )

        shelf = client.get(f"/notebooks/{nb_id}/_notes")

        assert shelf.status_code == 200
        assert "07-25 14:35" in shelf.text
        assert 'title="2026-07-25 14:35:54"' in shelf.text   # full stamp on hover
