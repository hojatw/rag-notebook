import json
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient as FastAPITestClient

from test_ui import TestClient, _fresh_app, _login, _seed_notebook


def _hint_form(**overrides):
    values = {
        "term": "ACME-42",
        "synonyms": "ACME\n專案四二",
        "definition": "客戶內部專案代號",
        "query_expansions": "ACME-42 部署狀態",
        "answer_note": "請使用專案代號作為小節標題。",
        "enabled": "1",
    }
    values.update(overrides)
    return values


def test_domain_settings_owner_crud_audit_and_menu_entry(monkeypatch, tmp_path):
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        _user, notebook_id = _seed_notebook(db)

        page = client.get(f"/notebooks/{notebook_id}/domain-settings")
        assert page.status_code == 200
        assert "領域提示與回答政策" in page.text
        assert 'name="answer_policy"' in page.text

        saved = client.post(
            f"/notebooks/{notebook_id}/domain-settings",
            data={
                "hints_enabled": "1",
                "answer_policy_enabled": "1",
                "answer_policy": "請以繁體中文摘要，並保留來源引用。",
            },
            follow_redirects=False,
        )
        assert saved.status_code == 303

        created = client.post(
            f"/notebooks/{notebook_id}/domain-settings/hints",
            data=_hint_form(),
            follow_redirects=False,
        )
        assert created.status_code == 303
        with db.connect() as conn:
            config = conn.execute(
                "SELECT hints_enabled, answer_policy_enabled, answer_policy, revision FROM notebook_domain_config WHERE notebook_id = ?",
                (notebook_id,),
            ).fetchone()
            hint = conn.execute(
                "SELECT id, term, enabled FROM notebook_domain_hints WHERE notebook_id = ?",
                (notebook_id,),
            ).fetchone()
        assert tuple(config[:3]) == (1, 1, "請以繁體中文摘要，並保留來源引用。")
        assert config["revision"] == 2
        assert tuple(hint[1:]) == ("ACME-42", 1)

        updated = client.post(
            f"/notebooks/{notebook_id}/domain-settings/hints/{hint['id']}/edit",
            data=_hint_form(term="ACME-43", enabled=""),
            follow_redirects=False,
        )
        assert updated.status_code == 303
        removed = client.post(
            f"/notebooks/{notebook_id}/domain-settings/hints/{hint['id']}/delete",
            follow_redirects=False,
        )
        assert removed.status_code == 303

        workspace = client.get(f"/notebooks/{notebook_id}")
        assert workspace.status_code == 200
        assert f'/notebooks/{notebook_id}/domain-settings' in workspace.text

        with db.connect() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM notebook_domain_hints WHERE notebook_id = ?", (notebook_id,)
            ).fetchone()[0]
            audit_rows = conn.execute(
                "SELECT action, metadata_json FROM audit_events WHERE target_id = ? ORDER BY id",
                (notebook_id,),
            ).fetchall()
        assert remaining == 0
        actions = [row["action"] for row in audit_rows]
        assert {
            "notebook_domain_config_updated",
            "notebook_domain_hint_created",
            "notebook_domain_hint_updated",
            "notebook_domain_hint_deleted",
        }.issubset(actions)
        audit_blob = "\n".join(row["metadata_json"] for row in audit_rows)
        assert "ACME-42" not in audit_blob
        assert "客戶內部專案代號" not in audit_blob
        assert "請以繁體中文" not in audit_blob
        assert all("changed_kind" in json.loads(row["metadata_json"]) for row in audit_rows)


def test_domain_settings_owner_scope_and_csrf(monkeypatch, tmp_path):
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as owner_client:
        _login(owner_client)
        _user, notebook_id = _seed_notebook(db)
        assert owner_client.post(
            f"/notebooks/{notebook_id}/domain-settings/hints",
            data=_hint_form(),
            follow_redirects=False,
        ).status_code == 303

        with db.connect() as conn:
            conn.execute(
                "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, 0)",
                ("other-owner", main.hash_password("other-password")),
            )

        owner_client.post("/logout", follow_redirects=False)
        assert owner_client.post(
            "/login",
            data={"username": "other-owner", "password": "other-password"},
            follow_redirects=False,
        ).status_code == 303
        assert owner_client.get(f"/notebooks/{notebook_id}/domain-settings").status_code == 404
        assert owner_client.post(
            f"/notebooks/{notebook_id}/domain-settings/hints/1/delete",
            follow_redirects=False,
        ).status_code == 404

    with FastAPITestClient(main.app) as csrf_client:
        login_page = csrf_client.get("/login")
        token = csrf_client.cookies.get("csrf_token")
        assert login_page.status_code == 200
        assert csrf_client.post(
            "/login",
            data={"username": "admin", "password": "admin123", "csrf_token": token},
            follow_redirects=False,
        ).status_code == 303
        rejected = csrf_client.post(
            f"/notebooks/{notebook_id}/domain-settings",
            data={"answer_policy": "不應被寫入"},
            follow_redirects=False,
        )
        assert rejected.status_code == 403

        with db.connect() as conn:
            before = dict(conn.execute(
                "SELECT revision, answer_policy FROM notebook_domain_config WHERE notebook_id = ?",
                (notebook_id,),
            ).fetchone())
            hint_id = conn.execute(
                "SELECT id FROM notebook_domain_hints WHERE notebook_id = ?",
                (notebook_id,),
            ).fetchone()["id"]

        # Crafted multipart bodies bypass the middleware's body reader, so each
        # E2 mutation route must reject them through verify_multipart_csrf.
        forged_routes = [
            (f"/notebooks/{notebook_id}/domain-settings", {"answer_policy": "forged"}),
            (f"/notebooks/{notebook_id}/domain-settings/hints", _hint_form(term="FORGED")),
            (f"/notebooks/{notebook_id}/domain-settings/hints/{hint_id}/edit", _hint_form(term="FORGED")),
            (f"/notebooks/{notebook_id}/domain-settings/hints/{hint_id}/delete", {}),
        ]
        for path, data in forged_routes:
            response = csrf_client.post(
                path,
                data=data,
                files={"probe": ("probe.txt", b"x", "text/plain")},
                follow_redirects=False,
            )
            assert response.status_code == 403

        oversized = csrf_client.post(
            f"/notebooks/{notebook_id}/domain-settings",
            content=b"",
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "content-length": str(64 * 1024 + 1),
            },
        )
        assert oversized.status_code == 413

        no_length_request = csrf_client.build_request(
            "POST",
            f"/notebooks/{notebook_id}/domain-settings",
            content=b"answer_policy=forged",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        del no_length_request.headers["content-length"]
        assert csrf_client.send(no_length_request).status_code == 411

        with db.connect() as conn:
            after = dict(conn.execute(
                "SELECT revision, answer_policy FROM notebook_domain_config WHERE notebook_id = ?",
                (notebook_id,),
            ).fetchone())
            hint = conn.execute(
                "SELECT term FROM notebook_domain_hints WHERE id = ?",
                (hint_id,),
            ).fetchone()
        assert after == before
        assert hint["term"] == "ACME-42"


def test_domain_settings_validation_re_echoes_form_without_audit_content(monkeypatch, tmp_path):
    main, db = _fresh_app(monkeypatch, tmp_path)
    invalid_policy = "secret-domain-policy-should-not-be-audited" * 40

    with TestClient(main.app) as client:
        _login(client)
        _user, notebook_id = _seed_notebook(db)
        rejected = client.post(
            f"/notebooks/{notebook_id}/domain-settings",
            data={"answer_policy_enabled": "1", "answer_policy": invalid_policy},
        )
        assert rejected.status_code == 400
        assert "回答政策超過字元上限" in rejected.text
        assert invalid_policy[:1000] in rejected.text
        assert invalid_policy not in rejected.text
        with db.connect() as conn:
            audit_count = conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE target_id = ?", (notebook_id,)
            ).fetchone()[0]
        assert audit_count == 0


def test_domain_hint_validation_preserves_line_values_on_rerender(monkeypatch, tmp_path):
    main, db = _fresh_app(monkeypatch, tmp_path)
    submitted_synonyms = "\n".join(f"alias-{index}" for index in range(9))

    with TestClient(main.app) as client:
        _login(client)
        _user, notebook_id = _seed_notebook(db)
        rejected = client.post(
            f"/notebooks/{notebook_id}/domain-settings/hints",
            data=_hint_form(synonyms=submitted_synonyms),
        )

    assert rejected.status_code == 400
    assert "同義詞數量超過上限" in rejected.text
    assert "alias-0\nalias-1\nalias-2" in rejected.text
    assert "a\nl\ni\na\ns" not in rejected.text


def test_domain_writes_serialize_revision_and_db_enforces_unique_term(monkeypatch, tmp_path):
    _main, db = _fresh_app(monkeypatch, tmp_path)
    from app import domain_policy
    db.init_db()

    with db.connect() as seed_conn:
        admin = seed_conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()
        notebook_id = seed_conn.execute(
            "INSERT INTO notebooks (user_id, title) VALUES (?, 'Concurrency')",
            (admin["id"],),
        ).lastrowid

    first_conn = db.connect()
    completed = threading.Event()
    started = threading.Event()
    observed: dict[str, int] = {}
    errors: list[Exception] = []
    try:
        first = domain_policy.save_domain_config(
            first_conn,
            notebook_id,
            hints_enabled=True,
            answer_policy_enabled=False,
            answer_policy="",
            updated_by=admin["id"],
        )
        assert first["revision"] == 1

        def second_writer():
            started.set()
            try:
                with db.connect() as second_conn:
                    second = domain_policy.save_domain_config(
                        second_conn,
                        notebook_id,
                        hints_enabled=False,
                        answer_policy_enabled=True,
                        answer_policy="policy",
                        updated_by=admin["id"],
                    )
                    observed["revision"] = second["revision"]
            except Exception as exc:  # surfaced below in the main test thread
                errors.append(exc)
            finally:
                completed.set()

        worker = threading.Thread(target=second_writer)
        worker.start()
        assert started.wait(1)
        assert completed.wait(0.05) is False
        first_conn.commit()
        assert completed.wait(2)
        worker.join(timeout=1)
    finally:
        first_conn.close()

    assert errors == []
    assert observed["revision"] == 2
    with db.connect() as conn:
        assert conn.execute(
            "SELECT revision FROM notebook_domain_config WHERE notebook_id = ?",
            (notebook_id,),
        ).fetchone()["revision"] == 2
        conn.execute(
            "INSERT INTO notebook_domain_hints (notebook_id, term) VALUES (?, 'ACME')",
            (notebook_id,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO notebook_domain_hints (notebook_id, term) VALUES (?, 'acme')",
                (notebook_id,),
            )
