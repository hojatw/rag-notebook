"""E3a 回答回饋的 UI 測試。

涵蓋評價與原因標籤的送出與更新（同一則回答只留一列）、送出當下凍結檢索設定、
未知評價/原因的拒絕、跨使用者的授權邊界、面板狀態由伺服器渲染而非只靠 Alpine，
以及管理員檢視頁的統計與稽核。
契約文件：docs/SCHEMA.md、docs/SECURITY.md。

由 tests/test_ui.py 拆出；共用固件見 tests/ui_helpers.py。
"""

import json

from tests.ui_helpers import TestClient, _fresh_app, _login


# --- E3a answer feedback ----------------------------------------------------


def _seed_answer(main, db, *, user_name="admin", metadata="{}"):
    """Create notebook + conversation + one assistant message for that user."""
    with db.connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE username = ?", (user_name,)).fetchone()
        notebook_id = conn.execute(
            "INSERT INTO notebooks (user_id, title) VALUES (?, '回饋測試')", (user["id"],)
        ).lastrowid
        convo_id = conn.execute(
            "INSERT INTO conversations (user_id, notebook_id, title) VALUES (?, ?, 'T')",
            (user["id"], notebook_id),
        ).lastrowid
        conn.execute(
            "INSERT INTO messages (conversation_id, user_id, role, content) VALUES (?, ?, 'user', '問題')",
            (convo_id, user["id"]),
        )
        message_id = conn.execute(
            "INSERT INTO messages (conversation_id, user_id, role, content, metadata_json) "
            "VALUES (?, ?, 'assistant', '答案', ?)",
            (convo_id, user["id"], metadata),
        ).lastrowid
    return user["id"], notebook_id, convo_id, message_id


def _feedback_rows(db):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM answer_feedback ORDER BY id").fetchall()]


def test_answer_feedback_submit_then_change_keeps_one_row(monkeypatch, tmp_path):
    """Changing your mind must update, not add a second row.

    The admin page counts rows; a duplicate would inflate every total silently
    and make the one number E3a exists to produce wrong.
    """
    main, db = _fresh_app(monkeypatch, tmp_path)
    with TestClient(main.app) as client:
        _login(client)
        _uid, nb, convo, msg = _seed_answer(main, db)
        url = f"/notebooks/{nb}/chat/{convo}/messages/{msg}/feedback"

        first = client.post(url, data={"rating": "usable"})
        assert first.status_code == 200
        assert "已記錄" in first.text
        rows = _feedback_rows(db)
        assert len(rows) == 1 and rows[0]["rating"] == "usable"

        second = client.post(url, data={"rating": "unusable", "reasons": ["retrieval", "citation"]})
        assert second.status_code == 200
        rows = _feedback_rows(db)
        assert len(rows) == 1
        assert rows[0]["rating"] == "unusable"
        assert json.loads(rows[0]["reasons_json"]) == ["retrieval", "citation"]


def test_answer_feedback_freezes_the_retrieval_configuration(monkeypatch, tmp_path):
    """Context must be frozen at submit time, not looked up later.

    An admin can apply a different retrieval profile at any moment, so a
    feedback row that stored only a profile id would describe the wrong
    configuration when it is read weeks later.
    """
    main, db = _fresh_app(monkeypatch, tmp_path)
    import app.retrieval as retrieval

    retrieval.set_active_retrieval_params({"vector_weight": 0.9, "final_chunk_count": 3})
    try:
        with TestClient(main.app) as client:
            _login(client)
            _uid, nb, convo, msg = _seed_answer(
                main, db,
                metadata='{"outcome": "answered", "domain_hints_enabled": true, "top_score": 0.71}',
            )
            client.post(
                f"/notebooks/{nb}/chat/{convo}/messages/{msg}/feedback",
                data={"rating": "partial"},
            )
        context = json.loads(_feedback_rows(db)[0]["context_json"])
        assert context["retrieval_params"]["vector_weight"] == 0.9
        assert context["outcome"] == "answered"
        assert context["domain_hints_enabled"] is True
        assert context["top_score"] == 0.71

        # Changing the active profile afterwards must not rewrite history.
        retrieval.set_active_retrieval_params({"vector_weight": 0.1})
        assert json.loads(_feedback_rows(db)[0]["context_json"])["retrieval_params"]["vector_weight"] == 0.9
    finally:
        retrieval.set_active_retrieval_params(None)


def test_answer_feedback_rejects_unknown_rating_and_drops_unknown_reasons(monkeypatch, tmp_path):
    main, db = _fresh_app(monkeypatch, tmp_path)
    with TestClient(main.app) as client:
        _login(client)
        _uid, nb, convo, msg = _seed_answer(main, db)
        url = f"/notebooks/{nb}/chat/{convo}/messages/{msg}/feedback"

        bad = client.post(url, data={"rating": "great"})
        assert bad.status_code == 400
        assert _feedback_rows(db) == []

        # A rating that keeps reasons — `usable` deliberately clears them, which
        # is a different rule with its own test.
        client.post(url, data={"rating": "partial", "reasons": ["retrieval", "made_up", "retrieval"]})
        assert json.loads(_feedback_rows(db)[0]["reasons_json"]) == ["retrieval"]


def test_answer_feedback_other_text_is_bounded_and_only_kept_when_chosen(monkeypatch, tmp_path):
    main, db = _fresh_app(monkeypatch, tmp_path)
    import app.config as app_config

    with TestClient(main.app) as client:
        _login(client)
        _uid, nb, convo, msg = _seed_answer(main, db)
        url = f"/notebooks/{nb}/chat/{convo}/messages/{msg}/feedback"

        # Not selected as a reason -> the text is discarded entirely.
        client.post(url, data={"rating": "unusable", "reasons": ["retrieval"], "other_reason": "沒選其他"})
        assert _feedback_rows(db)[0]["other_reason"] == ""

        cap = app_config.config.feedback.other_reason_max_chars
        client.post(url, data={"rating": "unusable", "reasons": ["other"], "other_reason": "字" * (cap + 50)})
        assert len(_feedback_rows(db)[0]["other_reason"]) == cap


def test_answer_feedback_cannot_rate_another_users_message(monkeypatch, tmp_path):
    """Per-user scoping: the feedback route resolves the notebook and message
    through the signed-in user, like every other notebook-data route."""
    main, db = _fresh_app(monkeypatch, tmp_path)
    with TestClient(main.app) as client:
        _login(client)  # admin
        _uid, nb, convo, msg = _seed_answer(main, db, user_name="user")  # belongs to `user`
        denied = client.post(
            f"/notebooks/{nb}/chat/{convo}/messages/{msg}/feedback",
            data={"rating": "usable"},
        )
        assert denied.status_code == 404
        assert _feedback_rows(db) == []


def test_answer_feedback_audit_records_shape_not_free_text(monkeypatch, tmp_path):
    """The audit trail proves feedback happened; it must not duplicate the
    user's words (AGENTS.md / SECURITY.md: identifiers and counts only)."""
    main, db = _fresh_app(monkeypatch, tmp_path)
    secret = "使用者寫的秘密補充"
    with TestClient(main.app) as client:
        _login(client)
        _uid, nb, convo, msg = _seed_answer(main, db)
        client.post(
            f"/notebooks/{nb}/chat/{convo}/messages/{msg}/feedback",
            data={"rating": "unusable", "reasons": ["other"], "other_reason": secret},
        )
    with db.connect() as conn:
        event = conn.execute(
            "SELECT * FROM audit_events WHERE action = 'answer_feedback_submitted'"
        ).fetchone()
    assert event is not None
    assert secret not in event["metadata_json"]
    metadata = json.loads(event["metadata_json"])
    assert metadata["rating"] == "unusable"
    assert metadata["other_reason_chars"] == len(secret)


def test_chat_page_renders_the_feedback_control_for_answers_only(monkeypatch, tmp_path):
    main, db = _fresh_app(monkeypatch, tmp_path)
    with TestClient(main.app) as client:
        _login(client)
        _uid, nb, convo, msg = _seed_answer(main, db)
        page = client.get(f"/notebooks/{nb}?conversation_id={convo}")
        assert page.status_code == 200
        assert f'id="feedback-msg-{msg}"' in page.text
        assert "這個回答有用嗎？" in page.text
        # The notice is a promise made to the user, not decoration.
        assert "你的回饋會提供給系統管理員作為參考。" in page.text
        # One control per assistant message: the user's own message has none.
        assert page.text.count('class="answer-feedback"') == 1


def test_admin_feedback_page_lists_entries_counts_and_audits_the_read(monkeypatch, tmp_path):
    main, db = _fresh_app(monkeypatch, tmp_path)
    with TestClient(main.app) as client:
        _login(client)
        _uid, nb, convo, msg = _seed_answer(main, db)
        client.post(
            f"/notebooks/{nb}/chat/{convo}/messages/{msg}/feedback",
            data={"rating": "unusable", "reasons": ["over_abstain", "other"], "other_reason": "應該找得到"},
        )

        page = client.get("/admin/feedback")
        assert page.status_code == 200
        assert "明明有資料卻說找不到" in page.text
        assert "應該找得到" in page.text
        assert "回饋總數" in page.text
        # The page must warn against reading these counts as a quality trend.
        assert "不是品質指標" in page.text

        # Exact reason matching: filtering "over_abstain" must not be a
        # substring match, and an unrelated reason must exclude the row.
        assert "應該找得到" in client.get("/admin/feedback?reason=over_abstain").text
        assert "應該找得到" not in client.get("/admin/feedback?reason=citation").text
        assert "應該找得到" not in client.get("/admin/feedback?rating=usable").text

    with db.connect() as conn:
        read_events = conn.execute(
            "SELECT * FROM audit_events WHERE action = 'answer_feedback_viewed'"
        ).fetchall()
    assert len(read_events) == 4  # one per admin page view above


def test_admin_feedback_page_is_admin_only(monkeypatch, tmp_path):
    main, db = _fresh_app(monkeypatch, tmp_path)
    with TestClient(main.app) as client:
        response = client.post(
            "/login", data={"username": "user", "password": "user123"}, follow_redirects=False
        )
        assert response.status_code == 303
        denied = client.get("/admin/feedback")
        assert denied.status_code == 403


def test_feedback_panel_state_is_server_rendered_not_alpine_only(monkeypatch, tmp_path):
    """The reasons panel must be correct in the returned HTML, not after Alpine runs.

    HTMX inserts the fragment and Alpine initialises a tick later, so a panel
    whose initial visibility depends on `x-show` alone is painted first and
    hidden afterwards — it flashed on every click. Anything that starts hidden
    therefore carries an inline `display: none`, and anything that should not
    exist at all (reasons for a usable answer) is simply not rendered.
    """
    main, db = _fresh_app(monkeypatch, tmp_path)
    with TestClient(main.app) as client:
        _login(client)
        _uid, nb, convo, msg = _seed_answer(main, db)
        url = f"/notebooks/{nb}/chat/{convo}/messages/{msg}/feedback"

        # Rating it usable ends the interaction: no reasons panel, no toggle.
        usable = client.post(url, data={"rating": "usable"})
        assert "feedback-reasons" not in usable.text
        assert "feedback-toggle" not in usable.text

        # A problem rating asks why, and the panel is open in the markup itself.
        unusable = client.post(url, data={"rating": "unusable"})
        assert "feedback-reasons" in unusable.text
        panel = unusable.text.split('class="feedback-reasons"')[1][:120]
        assert "display: none" not in panel

        # With reasons recorded the panel starts collapsed — and is rendered
        # collapsed, rather than being shown and then hidden by Alpine.
        with_reasons = client.post(url, data={"rating": "unusable", "reasons": ["citation"]})
        panel = with_reasons.text.split('class="feedback-reasons"')[1][:120]
        assert "display: none" in panel


def test_feedback_shows_what_was_recorded_after_submitting_reasons(monkeypatch, tmp_path):
    """Submitting reasons collapses the panel; without a summary the swap would
    look like nothing happened at all."""
    main, db = _fresh_app(monkeypatch, tmp_path)
    with TestClient(main.app) as client:
        _login(client)
        _uid, nb, convo, msg = _seed_answer(main, db)
        response = client.post(
            f"/notebooks/{nb}/chat/{convo}/messages/{msg}/feedback",
            data={"rating": "partial", "reasons": ["generation", "other"], "other_reason": "少了劑量說明"},
        )
        assert "已記錄：" in response.text
        assert "方向對但不完整" in response.text
        assert "找到了但答錯或不完整" in response.text
        assert "少了劑量說明" in response.text


def test_switching_to_usable_clears_any_reasons_already_ticked(monkeypatch, tmp_path):
    """The rating buttons and the reason checkboxes share one form, so a user
    who ticks reasons and then picks 可以直接採用 posts both. Recording those
    reasons would store a problem the user just said did not exist — and the
    admin page counts reasons, so it would also inflate the failure tally.
    """
    main, db = _fresh_app(monkeypatch, tmp_path)
    with TestClient(main.app) as client:
        _login(client)
        _uid, nb, convo, msg = _seed_answer(main, db)
        url = f"/notebooks/{nb}/chat/{convo}/messages/{msg}/feedback"

        client.post(url, data={"rating": "unusable", "reasons": ["citation", "other"], "other_reason": "引用錯頁"})
        assert json.loads(_feedback_rows(db)[0]["reasons_json"]) == ["citation", "other"]

        # Change of mind: the answer was fine after all.
        response = client.post(
            url, data={"rating": "usable", "reasons": ["citation", "other"], "other_reason": "引用錯頁"}
        )
        row = _feedback_rows(db)[0]
        assert row["rating"] == "usable"
        assert json.loads(row["reasons_json"]) == []
        assert row["other_reason"] == ""
        # ...and the stale reasons are gone from the rendered fragment too.
        assert "引用跟內容對不上" not in response.text
        assert "引用錯頁" not in response.text


def test_admin_feedback_page_sends_the_whole_answer_not_a_truncated_one(monkeypatch, tmp_path):
    """The answer preview is clamped in CSS and expanded in place, so the full
    text has to be in the HTML. Truncating server-side would make the expand
    button reveal the same cut-off text."""
    main, db = _fresh_app(monkeypatch, tmp_path)
    tail = "這句話只出現在回答的最後一段"
    with TestClient(main.app) as client:
        _login(client)
        with db.connect() as conn:
            user = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
            notebook_id = conn.execute(
                "INSERT INTO notebooks (user_id, title) VALUES (?, '長回答')", (user["id"],)
            ).lastrowid
            convo_id = conn.execute(
                "INSERT INTO conversations (user_id, notebook_id, title) VALUES (?, ?, 'T')",
                (user["id"], notebook_id),
            ).lastrowid
            message_id = conn.execute(
                "INSERT INTO messages (conversation_id, user_id, role, content) VALUES (?, ?, 'assistant', ?)",
                (convo_id, user["id"], "前面很長的內容。" * 40 + tail),
            ).lastrowid

        client.post(
            f"/notebooks/{notebook_id}/chat/{convo_id}/messages/{message_id}/feedback",
            data={"rating": "partial"},
        )
        page = client.get("/admin/feedback")
        assert tail in page.text
        assert "feedback-answer-toggle" in page.text
