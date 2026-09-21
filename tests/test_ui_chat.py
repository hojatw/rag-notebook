"""問答與對話的 UI 測試。

涵蓋 /ask 的 HTMX 片段與串流路徑、對話改名與中繼資料、延伸問題的產生與快取、
起始問題、匯出，以及 async 路由不得在事件迴圈上做 SQLite I/O。
契約文件：docs/RETRIEVAL.md、docs/UI.md。

由 tests/test_ui.py 拆出；共用固件見 tests/ui_helpers.py。
"""

import asyncio
import time

import pytest

from starlette.requests import Request

from tests.ui_helpers import TestClient, _fresh_app, _login, _seed_indexed_source, _seed_notebook


def test_ask_returns_messages_partial_for_htmx(monkeypatch, tmp_path):
    """U1: HTMX asks swap only the messages pane; plain posts keep the redirect."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)

        # HTMX request -> 200 partial with the question echoed, URL pushed,
        # and the OOB conversation-field update present.
        resp = client.post(
            f"/notebooks/{notebook_id}/chat/ask",
            data={"question": "第一個問題", "conversation_id": ""},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert 'id="chat-messages"' in resp.text
        assert "第一個問題" in resp.text
        assert "conversation_id=" in resp.headers.get("HX-Push-Url", "")
        assert 'hx-swap-oob="true"' in resp.text

        # Plain form post (no-JS fallback) -> 303 redirect, unchanged behavior.
        resp2 = client.post(
            f"/notebooks/{notebook_id}/chat/ask",
            data={"question": "第二個問題", "conversation_id": ""},
            follow_redirects=False,
        )
        assert resp2.status_code == 303


def test_streaming_ask_saves_answer_and_returns_final_messages(monkeypatch, tmp_path):
    """U2: streaming ask emits chunks, saves the final assistant message, and returns refreshed HTML."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    async def fake_retrieve(question, conversation_id, settings, history, user_id, source_ids=None, **kwargs):
        return [{
            "id": 1,
            "source_id": 10,
            "filename": "source.md",
            "location": "section",
            "text": "答案依據",
            "score": 0.9,
        }]

    async def fake_stream(question, chunks, settings, **kwargs):
        yield "串流"
        yield "回答 [1]"

    monkeypatch.setattr(main, "retrieve", fake_retrieve)
    monkeypatch.setattr(main, "generate_answer_stream", fake_stream)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)
        # 範圍一律在伺服器端解析成本 notebook 的已索引來源；沒有來源就不會檢索。
        _seed_indexed_source(db, user["id"], notebook_id)
        with db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_settings (id, provider, base_url, api_key, chat_model, embedding_model) "
                "VALUES (1, 'openai_compatible', 'https://x/v1', ?, 'chat', 'embed')",
                (db.encrypt_for_storage("sk-test"),),
            )

        resp = client.post(
            f"/notebooks/{notebook_id}/chat/ask-stream",
            data={"question": "請回答", "conversation_id": ""},
        )
        assert resp.status_code == 200
        assert "event: init" in resp.text
        assert "event: chunk" in resp.text
        assert "串流" in resp.text
        assert "event: done" in resp.text

        with db.connect() as conn:
            messages = conn.execute(
                "SELECT role, content, citations_json, metadata_json FROM messages ORDER BY id"
            ).fetchall()
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert messages[1]["content"] == "串流回答 [1]"
        assert "source.md" in messages[1]["citations_json"]
        assert '"outcome": "answered"' in messages[1]["metadata_json"]


def test_chat_errors_are_friendly_in_ui(monkeypatch, tmp_path):
    """U14: raw exception text is logged/metadata only, not shown in chat UI."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    async def failing_answer(question, settings, history, user_id, source_ids):
        raise RuntimeError("SECRET_PROVIDER_STACKTRACE")

    monkeypatch.setattr(main, "_answer_question", failing_answer)

    with TestClient(main.app) as client:
        _login(client)
        _user, notebook_id = _seed_notebook(db)
        resp = client.post(
            f"/notebooks/{notebook_id}/chat/ask",
            data={"question": "會失敗", "conversation_id": ""},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "回答生成失敗" in resp.text
        assert "SECRET_PROVIDER_STACKTRACE" not in resp.text
        assert "技術細節已記錄" in resp.text


def test_conversation_rename_and_menu_metadata(monkeypatch, tmp_path):
    """U5: conversations can be renamed and the menu shows message count/time metadata."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)
        with db.connect() as conn:
            convo_id = conn.execute(
                "INSERT INTO conversations (user_id, notebook_id, title) VALUES (?, ?, '舊名稱')",
                (user["id"], notebook_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO messages (conversation_id, user_id, role, content) VALUES (?, ?, 'user', '問題')",
                (convo_id, user["id"]),
            )
            conn.execute(
                "INSERT INTO messages (conversation_id, user_id, role, content) VALUES (?, ?, 'assistant', '回答')",
                (convo_id, user["id"]),
            )

        renamed = client.post(
            f"/notebooks/{notebook_id}/chat/{convo_id}/rename",
            data={"title": " 新名稱  "},
            follow_redirects=False,
        )
        assert renamed.status_code == 303
        assert f"conversation_id={convo_id}" in renamed.headers["location"]

        page = client.get(f"/notebooks/{notebook_id}?conversation_id={convo_id}")
        assert "新名稱" in page.text
        assert "2 則訊息" in page.text
        assert "重新命名對話" in page.text


def test_followups_generate_once_and_cache(monkeypatch, tmp_path):
    """A2: follow-up chips are generated once, cached into message metadata."""
    main, db = _fresh_app(monkeypatch, tmp_path)
    calls = {"n": 0}

    async def fake_followups(question, answer, settings, source_context=None, **kwargs):
        calls["n"] += 1
        assert source_context == ["English source excerpt"]
        return ["追問一？", "追問二？"]

    monkeypatch.setattr(main, "suggest_followup_questions", fake_followups)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)
        with db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_settings (id, provider, base_url, api_key, chat_model, embedding_model) "
                "VALUES (1, 'openai_compatible', 'https://x/v1', ?, 'chat', 'embed')",
                (db.encrypt_for_storage("sk-test"),),
            )
            convo_id = conn.execute(
                "INSERT INTO conversations (user_id, notebook_id, title) VALUES (?, ?, 'T')",
                (user["id"], notebook_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO messages (conversation_id, user_id, role, content) VALUES (?, ?, 'user', '原始問題')",
                (convo_id, user["id"]),
            )
            msg_id = conn.execute(
                "INSERT INTO messages (conversation_id, user_id, role, content, citations_json, metadata_json) "
                "VALUES (?, ?, 'assistant', '答案', '[{\"index\": 1, \"snippet\": \"English source excerpt\"}]', "
                "'{\"outcome\": \"answered\", \"followups\": [\"舊追問\"]}')",
                (convo_id, user["id"]),
            ).lastrowid

        page = client.get(f"/notebooks/{notebook_id}?conversation_id={convo_id}")
        assert "舊追問" not in page.text
        assert "追問生成中" in page.text

        url = f"/notebooks/{notebook_id}/chat/{convo_id}/_followups?message_id={msg_id}"
        first = client.get(url)
        assert first.status_code == 200
        assert "追問一？" in first.text
        assert "舊追問" not in first.text
        assert "data-fill-question" in first.text
        assert calls["n"] == 1

        # Cached in metadata_json -> the second request must not regenerate.
        second = client.get(url)
        assert "追問一？" in second.text
        assert calls["n"] == 1
        with db.connect() as conn:
            meta = conn.execute("SELECT metadata_json FROM messages WHERE id = ?", (msg_id,)).fetchone()
        assert "追問一" in meta["metadata_json"]


def test_notebook_can_disable_followups(monkeypatch, tmp_path):
    """Notebook-level setting prevents lazy follow-up generation."""
    main, db = _fresh_app(monkeypatch, tmp_path)
    calls = {"n": 0}

    async def fake_followups(question, answer, settings, source_context=None, **kwargs):
        calls["n"] += 1
        return ["不應產生"]

    monkeypatch.setattr(main, "suggest_followup_questions", fake_followups)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db, title="追問設定")
        with db.connect() as conn:
            notebook = conn.execute(
                "SELECT followups_enabled FROM notebooks WHERE id = ?", (notebook_id,)
            ).fetchone()
        assert notebook["followups_enabled"] == 1

        legacy_rename = client.post(
            f"/notebooks/{notebook_id}/rename",
            data={"title": "追問設定", "emoji": "📓", "description": ""},
            follow_redirects=False,
        )
        assert legacy_rename.status_code == 303
        with db.connect() as conn:
            assert conn.execute(
                "SELECT followups_enabled FROM notebooks WHERE id = ?", (notebook_id,)
            ).fetchone()["followups_enabled"] == 1

        renamed = client.post(
            f"/notebooks/{notebook_id}/rename",
            data={"title": "追問設定", "emoji": "📓", "description": "", "followups_setting_present": "1"},
            follow_redirects=False,
        )
        assert renamed.status_code == 303
        with db.connect() as conn:
            assert conn.execute(
                "SELECT followups_enabled FROM notebooks WHERE id = ?", (notebook_id,)
            ).fetchone()["followups_enabled"] == 0
            convo_id = conn.execute(
                "INSERT INTO conversations (user_id, notebook_id, title) VALUES (?, ?, 'T')",
                (user["id"], notebook_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO messages (conversation_id, user_id, role, content) VALUES (?, ?, 'user', '原始問題')",
                (convo_id, user["id"]),
            )
            msg_id = conn.execute(
                "INSERT INTO messages (conversation_id, user_id, role, content, metadata_json) "
                "VALUES (?, ?, 'assistant', '答案', '{\"outcome\": \"answered\"}')",
                (convo_id, user["id"]),
            ).lastrowid

        resp = client.get(f"/notebooks/{notebook_id}/chat/{convo_id}/_followups?message_id={msg_id}")
        assert resp.status_code == 200
        assert resp.text == ""
        assert calls["n"] == 0


def test_export_conversation_and_notes_markdown(monkeypatch, tmp_path):
    """A3: conversation and notes export as downloadable Markdown."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db, title="匯出測試")
        with db.connect() as conn:
            convo_id = conn.execute(
                "INSERT INTO conversations (user_id, notebook_id, title) VALUES (?, ?, '對話A')",
                (user["id"], notebook_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO messages (conversation_id, user_id, role, content) VALUES (?, ?, 'user', '問題X')",
                (convo_id, user["id"]),
            )
            conn.execute(
                "INSERT INTO messages (conversation_id, user_id, role, content, citations_json) "
                "VALUES (?, ?, 'assistant', '回答Y [1]', '[{\"index\": 1, \"filename\": \"a.pdf\", \"location\": \"page 1\"}]')",
                (convo_id, user["id"]),
            )
            conn.execute(
                "INSERT INTO notes (notebook_id, user_id, title, content) VALUES (?, ?, '筆記1', '筆記內容')",
                (notebook_id, user["id"]),
            )
            note_id = conn.execute(
                "INSERT INTO notes (notebook_id, user_id, title, content) VALUES (?, ?, '筆記2', '單筆內容')",
                (notebook_id, user["id"]),
            ).lastrowid

        convo = client.get(f"/notebooks/{notebook_id}/chat/{convo_id}/export")
        assert convo.status_code == 200
        assert "text/markdown" in convo.headers["content-type"]
        assert "attachment" in convo.headers["content-disposition"]
        assert "問題X" in convo.text and "回答Y" in convo.text and "a.pdf" in convo.text

        notes = client.get(f"/notebooks/{notebook_id}/notes/export")
        assert notes.status_code == 200
        assert "筆記內容" in notes.text
        assert "attachment" in notes.headers["content-disposition"]

        note = client.get(f"/notebooks/{notebook_id}/notes/{note_id}/export")
        assert note.status_code == 200
        assert "單筆內容" in note.text
        assert "筆記內容" not in note.text
        assert "attachment" in note.headers["content-disposition"]

        with db.connect() as conn:
            events = [
                dict(row)
                for row in conn.execute(
                    "SELECT action, sensitivity, metadata_json FROM audit_events ORDER BY id"
                ).fetchall()
            ]
        actions = [event["action"] for event in events]
        assert "conversation_exported" in actions
        assert "notes_exported" in actions
        assert "note_exported" in actions
        assert all(event["sensitivity"] == "high" for event in events)
        metadata_blob = "\n".join(event["metadata_json"] for event in events)
        assert "問題X" not in metadata_blob
        assert "回答Y" not in metadata_blob
        assert "筆記內容" not in metadata_blob


def test_async_llm_route_sqlite_does_not_block_event_loop(monkeypatch, tmp_path):
    """A slow SQLite phase in an async LLM route must run off the event loop.

    Compare is representative of suggestions, briefing, chat, follow-ups,
    minutes, artifacts, and translation: each route does a bounded SQLite phase
    before awaiting an LLM call.  A delayed connection used to freeze every
    request on that worker until the synchronous query returned.
    """
    main, db = _fresh_app(monkeypatch, tmp_path)
    db.init_db()
    user, notebook_id = _seed_notebook(db)
    source_ids = [
        _seed_indexed_source(db, user["id"], notebook_id, "a.pdf", "摘要 A"),
        _seed_indexed_source(db, user["id"], notebook_id, "b.pdf", "摘要 B"),
    ]
    with db.connect() as conn:
        conn.execute("UPDATE llm_settings SET chat_model = 'chat' WHERE id = 1")

    real_connect = main.connect

    def slow_connect():
        time.sleep(0.5)
        return real_connect()

    async def fake_compare(*_args, **_kwargs):
        return "比較完成"

    monkeypatch.setattr(main, "connect", slow_connect)
    monkeypatch.setattr(main, "compare_sources", fake_compare)
    request = Request({
        "type": "http",
        "method": "POST",
        "path": f"/notebooks/{notebook_id}/compare",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
    })

    async def drive():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        beat = asyncio.create_task(ticker())
        response = await main.notebook_compare(
            request,
            notebook_id,
            user,
            source_ids=source_ids,
            focus="",
        )
        beat.cancel()
        return response, ticks

    response, ticks = asyncio.run(drive())
    assert response.status_code == 200
    assert ticks >= 3, f"event loop was starved during SQLite work (ticks={ticks})"


def test_chat_empty_shows_starter_questions(monkeypatch, tmp_path):
    """U16: starter questions render in the chat empty-state, not the Studio."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        user, notebook_id = _seed_notebook(db)
        _seed_indexed_source(db, user["id"], notebook_id, "a.pdf")
        with db.connect() as conn:
            conn.execute(
                "UPDATE notebooks SET suggestions_json = ?, suggestions_at = CURRENT_TIMESTAMP WHERE id = ?",
                ('["這份文件的重點是什麼？"]', notebook_id),
            )

        empty = client.get(f"/notebooks/{notebook_id}/_chat-empty")
        assert empty.status_code == 200
        assert 'class="suggestion-chip"' in empty.text
        assert "這份文件的重點是什麼？" in empty.text
        # The relocated section no longer self-polls indexed-sources-changed
        # (the empty-state owns that refresh).
        assert 'id="studio-suggestions"' in empty.text


def test_citation_payload_and_merge_carry_chunk_id(monkeypatch, tmp_path):
    """U3: citations carry the chunk row id so the chip can open the preview at it."""
    main, _db = _fresh_app(monkeypatch, tmp_path)

    chunks = [{"id": 7, "source_id": 3, "filename": "a.pdf", "location": "p1", "text": "hello", "score": 0.9}]
    cites = main.citation_payload(chunks)
    assert cites[0]["chunk_id"] == 7 and cites[0]["source_id"] == 3

    vec = [{"id": 5, "source_id": 2, "filename": "f", "location": "l", "text": "alpha beta", "vector_score": 0.8}]
    merged = main.merge_candidates(vec, [], ["alpha"])
    assert merged[5]["id"] == 5


# --- 問答檢索不得離開當前 notebook -------------------------------------------
#
# chunks 表與 Chroma metadata 都沒有 notebook_id，notebook 範圍只能靠 source_ids
# 表達。曾經發生：ask 沒帶 source_ids（左欄按「全不選」或無 JS 表單）時，檢索
# 退化成只篩 user_id，回答引用了同一使用者「另一個 notebook」的來源。
# 這組測試跑真的 ask / ask-stream 路由與真的 retrieve()（Chroma 向量＋SQLite
# 關鍵字兩條路都走），只替換掉需要網路的 embedding／改寫／rerank／生成。


def _two_notebooks_same_user(main, db):
    """同一使用者的兩個 notebook，各一個含相同關鍵字的已索引來源。

    另一本的 chunk 刻意是「更好」的命中（向量完全相同、文字也不同，不會被去重），
    所以範圍一旦外漏，它一定會排進結果、出現在引用裡。
    """
    import app.vector_store as vector_store

    user, nb_here = _seed_notebook(db, title="這一本")
    _user, nb_other = _seed_notebook(db, title="另一本")
    here_source = _seed_indexed_source(db, user["id"], nb_here, filename="here.md", summary="潮汐發電概述")
    other_source = _seed_indexed_source(
        db, user["id"], nb_other, filename="other.md", summary="潮汐發電的原理與成本"
    )
    embeddings = {here_source: [0.8, 0.6, 0.0], other_source: [1.0, 0.0, 0.0]}
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT chunks.*, sources.filename FROM chunks JOIN sources ON sources.id = chunks.source_id"
        ).fetchall()
        conn.execute(
            "INSERT OR REPLACE INTO llm_settings (id, provider, base_url, chat_model, embedding_model) "
            "VALUES (1, 'openai_compatible', 'https://x/v1', 'chat', 'embed')"
        )
    vector_store.upsert_chunks([{**dict(row), "embedding": embeddings[row["source_id"]]} for row in rows])
    return user, nb_here, here_source, other_source


def _stub_network_calls(main, monkeypatch):
    """只換掉需要網路的呼叫；retrieve() 本身與兩條候選搜尋都是真的。"""
    import app.retrieval as retrieval
    from app.llm import AnswerResult

    async def fake_rewrite(question, history, settings, **kwargs):
        return [question]

    async def fake_embed(texts, settings, **kwargs):
        return [[1.0, 0.0, 0.0] for _ in texts]

    async def fake_rerank(question, candidates, settings, limit=6, **kwargs):
        return candidates[:limit]

    def cite_everything(chunks):
        return "回答 " + " ".join(f"[{i}]" for i in range(1, len(chunks) + 1))

    async def fake_answer(question, chunks, settings, **kwargs):
        return AnswerResult(text=cite_everything(chunks))

    async def fake_stream(question, chunks, settings, **kwargs):
        yield cite_everything(chunks)

    monkeypatch.setattr(retrieval, "rewrite_search_queries", fake_rewrite)
    monkeypatch.setattr(retrieval, "embed_texts", fake_embed)
    monkeypatch.setattr(retrieval, "rerank_chunks", fake_rerank)
    monkeypatch.setattr(main, "generate_answer_result", fake_answer)
    monkeypatch.setattr(main, "generate_answer_stream", fake_stream)


def _last_assistant(db):
    from app.db import loads

    with db.connect() as conn:
        row = conn.execute(
            "SELECT content, citations_json, metadata_json FROM messages WHERE role = 'assistant' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return row["content"], loads(row["citations_json"] or "[]"), loads(row["metadata_json"] or "{}")


@pytest.mark.parametrize("route", ["ask", "ask-stream"])
def test_ask_without_source_ids_stays_inside_the_notebook(monkeypatch, tmp_path, route):
    """沒帶 source_ids（無 JS 表單、舊客戶端）= 這個 notebook 的全部已索引來源，絕不是整個語料。"""
    main, db = _fresh_app(monkeypatch, tmp_path)
    _stub_network_calls(main, monkeypatch)

    with TestClient(main.app) as client:
        _login(client)
        _user, nb_here, here_source, other_source = _two_notebooks_same_user(main, db)

        resp = client.post(
            f"/notebooks/{nb_here}/chat/{route}",
            data={"question": "潮汐發電", "conversation_id": ""},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    answer, citations, metadata = _last_assistant(db)
    assert metadata["outcome"] == "answered"
    # 只有本 notebook 的來源——用 == 而不是 in，另一本的引用混進來也要紅。
    assert {c["source_id"] for c in citations} == {here_source}
    assert "other.md" not in answer


@pytest.mark.parametrize("route", ["ask", "ask-stream"])
@pytest.mark.parametrize(
    "case, expected_key",
    [
        ("explicit_none", "chat.scope_none_selected"),
        ("all_invalid", "chat.scope_invalid"),
    ],
)
def test_ask_with_unusable_scope_is_refused_not_widened(monkeypatch, tmp_path, route, case, expected_key):
    """明確全不選、或送來的 id 全部不屬於本 notebook：拒答並說明，不退回任何更大的範圍。"""
    main, db = _fresh_app(monkeypatch, tmp_path)
    _stub_network_calls(main, monkeypatch)
    retrieve_calls = []
    real_retrieve = main.retrieve

    async def spy_retrieve(*args, **kwargs):
        retrieve_calls.append(args)
        return await real_retrieve(*args, **kwargs)

    monkeypatch.setattr(main, "retrieve", spy_retrieve)

    with TestClient(main.app) as client:
        _login(client)
        _user, nb_here, _here_source, other_source = _two_notebooks_same_user(main, db)
        data = {"question": "潮汐發電", "conversation_id": "", "source_scope": "selected"}
        if case == "all_invalid":
            # 另一個 notebook 的來源 id（同一使用者，舊版會照單全收地檢索）
            data["source_ids"] = [str(other_source), "999999"]

        resp = client.post(
            f"/notebooks/{nb_here}/chat/{route}", data=data, headers={"HX-Request": "true"}
        )
        assert resp.status_code == 200

    answer, citations, metadata = _last_assistant(db)
    assert answer == main.i18n.t(expected_key)
    assert citations == []
    assert metadata["outcome"] == "scope_rejected"
    assert retrieve_calls == []


def test_retrieve_fails_closed_without_a_source_scope(monkeypatch, tmp_path, caplog):
    """底層防線：有 user_id 卻沒有 source_ids 時回傳空結果，不搜尋使用者的整個語料。"""
    main, db = _fresh_app(monkeypatch, tmp_path)
    _stub_network_calls(main, monkeypatch)
    with TestClient(main.app) as client:
        _login(client)
        user, *_ = _two_notebooks_same_user(main, db)

    with caplog.at_level("WARNING", logger="app.retrieval"):
        out = asyncio.run(main.retrieve("潮汐發電", None, {}, [], user["id"], []))
    assert out == []
    assert any("retrieve_refused_unscoped" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("route", ["ask", "ask-stream"])
def test_ask_in_a_notebook_with_nothing_indexed_abstains_without_the_unscoped_warning(
    monkeypatch, tmp_path, caplog, route
):
    """尚無已索引來源的 notebook：照常拒答，而且不觸發 retrieve() 的「呼叫端有 bug」警告。"""
    main, db = _fresh_app(monkeypatch, tmp_path)
    _stub_network_calls(main, monkeypatch)
    with TestClient(main.app) as client:
        _login(client)
        _user, nb_here, *_ = _two_notebooks_same_user(main, db)
        _user, nb_empty = _seed_notebook(db, title="空的")
        with caplog.at_level("WARNING", logger="app.retrieval"):
            resp = client.post(
                f"/notebooks/{nb_empty}/chat/{route}",
                data={"question": "潮汐發電", "conversation_id": ""},
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200

    answer, citations, metadata = _last_assistant(db)
    assert answer == main.i18n.t("chat.abstain")
    assert citations == []
    assert metadata["outcome"] == "no_retrieval"
    assert not [r for r in caplog.records if "retrieve_refused_unscoped" in r.getMessage()]
