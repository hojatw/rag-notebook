"""管理後台的 UI 測試。

涵蓋 E1 Eval Workbench（檢索設定檔、eval set/run/compare、匯出與稽核）、
E1e-2 答案品質評分層、/admin/index 的向量索引與 O0 維度遷移閘門，以及 /settings
的連線診斷與取樣能力探測。
契約文件：docs/RETRIEVAL.md、docs/archive/O0_DIMENSION_RESET_PLAN.md。

由 tests/test_ui.py 拆出；共用固件見 tests/ui_helpers.py。
"""

import json

from tests.ui_helpers import TestClient, _fresh_app, _login, _seed_indexed_source, _seed_notebook


def test_admin_eval_workbench_creates_default_profile(monkeypatch, tmp_path):
    """E1a: admin eval shell exposes the active profile and durable run history tables."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)

        resp = client.get("/admin/evals")
        assert resp.status_code == 200
        assert "評測工作台" in resp.text
        assert "目前系統預設" in resp.text           # active-profile line
        assert 'href="/admin/evals/profiles"' in resp.text
        assert "歷史執行紀錄" in resp.text
        assert '<pre class="config-preview">' not in resp.text
        assert "搜尋全站已索引筆記本" in resp.text

        # Profile params live on the dedicated profiles page now.
        profiles_resp = client.get("/admin/evals/profiles")
        assert profiles_resp.status_code == 200
        assert "最終 chunk 數" in profiles_resp.text

        with db.connect() as conn:
            profile = conn.execute("SELECT * FROM retrieval_profiles WHERE is_active = 1").fetchone()
            tables = conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name IN (
                    'retrieval_profiles', 'eval_sets', 'eval_items', 'eval_runs', 'eval_results'
                )
                """
            ).fetchall()

        assert profile is not None
        assert json.loads(profile["params_json"])["final_chunk_count"] == main.FINAL_CHUNK_COUNT
        assert {row["name"] for row in tables} == {
            "retrieval_profiles",
            "eval_sets",
            "eval_items",
            "eval_runs",
            "eval_results",
        }


def test_admin_eval_help_page_documents_tuning_workflow(monkeypatch, tmp_path):
    """E1f: the in-product help page exposes tuning guidance without using the PDF."""
    main, _db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)

        landing = client.get("/admin/evals")
        assert landing.status_code == 200
        assert 'href="/admin/evals/help"' in landing.text

        help_page = client.get("/admin/evals/help")
        assert help_page.status_code == 200
        assert "調參指南" in help_page.text
        assert "先分類錯誤" in help_page.text
        assert "調參方向速查" in help_page.text
        assert "領域提示與回答規則" in help_page.text
        assert "current_profile_fields" not in help_page.text
        assert "<code>vector_weight</code>" in help_page.text
        assert 'aria-current="page">調參指南</a>' in help_page.text
        # O0: Clear/Rebuild cannot change a locked dimension (and Rebuild
        # never re-embeds), so the "cannot apply directly" card must route
        # dimension changes to the migration flow instead.
        assert "Clear/Rebuild" not in help_page.text
        assert "「更換 embedding 維度」遷移流程" in help_page.text


def test_admin_eval_workbench_search_generate_approve_and_delete(monkeypatch, tmp_path):
    """E1b follow-up: all-site notebook search, draft generation, approval, and set deletion."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        admin_user, admin_notebook_id = _seed_notebook(db, "Admin indexed")
        admin_source_id = _seed_indexed_source(db, admin_user["id"], admin_notebook_id, "admin.pdf")
        admin_source_id_2 = _seed_indexed_source(db, admin_user["id"], admin_notebook_id, "admin-2.pdf", summary="second evidence")
        with db.connect() as conn:
            other_user = conn.execute("SELECT * FROM users WHERE username = 'user'").fetchone()
            other_notebook_id = conn.execute(
                "INSERT INTO notebooks (user_id, title) VALUES (?, ?)",
                (other_user["id"], "Customer indexed"),
            ).lastrowid
        _seed_indexed_source(db, other_user["id"], other_notebook_id, "customer.pdf", summary="customer evidence")

        resp = client.get("/admin/evals", params={"notebook_q": "Customer"})
        assert resp.status_code == 200
        assert "Customer indexed" in resp.text
        assert "user · 1 個已索引來源" in resp.text
        assert "Admin indexed" not in resp.text

        created = client.post(
            "/admin/evals/sets",
            data={"notebook_id": admin_notebook_id, "name": "Generated Eval", "description": ""},
            follow_redirects=False,
        )
        assert created.status_code == 303
        eval_set_id = int(created.headers["location"].rstrip("/").split("/")[-1])

        generated = client.post(
            f"/admin/evals/sets/{eval_set_id}/generate",
            data={"count": "2"},
            follow_redirects=False,
        )
        assert generated.status_code == 303
        assert generated.headers["location"] == f"/admin/evals/sets/{eval_set_id}#eval-items"
        generated_again = client.post(
            f"/admin/evals/sets/{eval_set_id}/generate",
            data={"count": "2"},
            follow_redirects=False,
        )
        assert generated_again.status_code == 303

        detail = client.get(f"/admin/evals/sets/{eval_set_id}")
        assert detail.status_code == 200
        assert '<nav aria-label="麵包屑導覽" class="breadcrumb">' in detail.text
        assert 'href="/admin/evals">評測工作台</a>' in detail.text
        assert 'id="eval-items"' in detail.text
        assert "返回 Eval 工作台" not in detail.text
        assert "自動生成 draft 題目" in detail.text
        assert "draft" in detail.text
        assert "approve" in detail.text
        assert "執行 Eval run</button>" in detail.text
        assert "disabled" in detail.text

        with db.connect() as conn:
            generated_items = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM eval_items WHERE eval_set_id = ? ORDER BY id ASC", (eval_set_id,)
                ).fetchall()
            ]
        assert len(generated_items) == 2
        assert {item["expected_source_id"] for item in generated_items} == {admin_source_id, admin_source_id_2}
        generated_item = generated_items[0]
        assert generated_item["approved"] == 0
        assert generated_item["expected_chunk_id"] is not None
        assert generated_item["question"].startswith("「")
        assert "來源：admin" in generated_item["question"]
        assert json.loads(generated_item["expected_substrings_json"])

        approved = client.post(
            f"/admin/evals/sets/{eval_set_id}/items/{generated_item['id']}/approve",
            follow_redirects=False,
        )
        assert approved.status_code == 303
        assert approved.headers["location"] == f"/admin/evals/sets/{eval_set_id}#eval-items"
        with db.connect() as conn:
            approved_item = conn.execute("SELECT approved FROM eval_items WHERE id = ?", (generated_item["id"],)).fetchone()
        assert approved_item["approved"] == 1

        htmx_approved = client.post(
            f"/admin/evals/sets/{eval_set_id}/items/{generated_items[1]['id']}/approve",
            headers={"HX-Request": "true"},
        )
        assert htmx_approved.status_code == 200
        assert 'id="eval-items"' in htmx_approved.text
        assert '<!doctype html>' not in htmx_approved.text
        assert "執行 Eval run" in htmx_approved.text

        deleted = client.post(f"/admin/evals/sets/{eval_set_id}/delete", follow_redirects=False)
        assert deleted.status_code == 303
        with db.connect() as conn:
            eval_set = conn.execute("SELECT id FROM eval_sets WHERE id = ?", (eval_set_id,)).fetchone()
            item = conn.execute("SELECT id FROM eval_items WHERE eval_set_id = ?", (eval_set_id,)).fetchone()
        assert eval_set is None
        assert item is None


def test_admin_eval_set_llm_authoring_generates_draft_items(monkeypatch, tmp_path):
    """E1e-1: LLM-assisted authoring stores reviewed-only draft eval candidates."""
    main, db = _fresh_app(monkeypatch, tmp_path)
    import app.evals as evals

    async def fake_generate_eval_candidates(chunks, settings, count=5, item_types=None, target_language="", **kwargs):
        assert settings["chat_model"] == "chat"
        assert item_types == ["answerable", "cross_lingual", "unanswerable"]
        assert target_language == "Traditional Chinese"
        assert chunks
        chunk = chunks[0]
        return [
            {
                "question": "alpha 的關鍵數字是什麼？",
                "item_type": "answerable",
                "source_id": chunk["source_id"],
                "chunk_id": chunk["chunk_id"],
                "expected_answer": "alpha answer",
                "expected_substrings": ["alpha evidence"],
                "rationale": "covers exact evidence",
            },
            {
                "question": "What does alpha evidence describe?",
                "item_type": "cross_lingual",
                "source_id": chunk["source_id"],
                "chunk_id": chunk["chunk_id"],
                "expected_answer": "alpha answer",
                "expected_substrings": ["alpha evidence"],
                "rationale": "cross-language retrieval",
            },
            {
                "question": "這份文件是否提到 beta approval date?",
                "item_type": "unanswerable",
                "source_id": None,
                "chunk_id": None,
                "expected_answer": "",
                "expected_substrings": [],
                "rationale": "tests abstention later",
            },
        ]

    monkeypatch.setattr(evals, "generate_eval_candidates", fake_generate_eval_candidates)

    with TestClient(main.app) as client:
        _login(client)
        admin_user, notebook_id = _seed_notebook(db, "LLM Eval")
        source_id = _seed_indexed_source(db, admin_user["id"], notebook_id, "alpha.pdf", summary="alpha evidence")
        with db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_settings (id, provider, base_url, api_key, chat_model, embedding_model) "
                "VALUES (1, 'openai_compatible', 'https://x/v1', ?, 'chat', 'embed')",
                (db.encrypt_for_storage("sk-test"),),
            )

        created = client.post(
            "/admin/evals/sets",
            data={"notebook_id": notebook_id, "name": "LLM Generated Eval", "description": ""},
            follow_redirects=False,
        )
        eval_set_id = int(created.headers["location"].rstrip("/").split("/")[-1])

        generated = client.post(
            f"/admin/evals/sets/{eval_set_id}/generate/llm",
            data={
                "count": "3",
                "item_types": ["answerable", "cross_lingual", "unanswerable"],
                "source_ids": [str(source_id)],
                "target_language": "Traditional Chinese",
            },
            headers={"HX-Request": "true"},
        )

        assert generated.status_code == 200
        assert 'id="eval-items"' in generated.text
        assert '<!doctype html>' not in generated.text
        assert "LLM 已建立 3 題 draft 候選題" in generated.text
        assert "跨語言" in generated.text
        assert "不可回答" in generated.text
        with db.connect() as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM eval_items WHERE eval_set_id = ? ORDER BY id ASC",
                    (eval_set_id,),
                ).fetchall()
            ]
        assert [row["approved"] for row in rows] == [0, 0, 0]
        assert [row["item_type"] for row in rows] == ["answerable", "cross_lingual", "unanswerable"]
        assert rows[2]["expected_source_id"] is None
        assert rows[2]["expected_substrings_json"] == "[]"
        metadata = json.loads(rows[0]["metadata_json"])
        assert metadata["origin"] == "llm_generated"
        assert metadata["selected_source_ids"] == [source_id]
        assert "alpha evidence" not in rows[0]["metadata_json"]


def test_admin_eval_set_llm_authoring_requires_settings(monkeypatch, tmp_path):
    """E1e-1: missing LLM settings returns a partial error and creates no item."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        admin_user, notebook_id = _seed_notebook(db, "No Settings Eval")
        _seed_indexed_source(db, admin_user["id"], notebook_id, "alpha.pdf", summary="alpha evidence")
        created = client.post(
            "/admin/evals/sets",
            data={"notebook_id": notebook_id, "name": "No Settings", "description": ""},
            follow_redirects=False,
        )
        eval_set_id = int(created.headers["location"].rstrip("/").split("/")[-1])

        generated = client.post(
            f"/admin/evals/sets/{eval_set_id}/generate/llm",
            data={"count": "1", "item_types": "answerable"},
            headers={"HX-Request": "true"},
        )

        assert generated.status_code == 200
        assert "尚未完成 LLM 設定" in generated.text
        with db.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM eval_items WHERE eval_set_id = ?",
                (eval_set_id,),
            ).fetchone()["n"]
        assert count == 0


def test_admin_eval_set_runner_records_results(monkeypatch, tmp_path):
    """E1b: admin can create a manual eval item, run it, and inspect stored metrics."""
    main, db = _fresh_app(monkeypatch, tmp_path)
    import app.evals as evals

    async def fake_retrieve(question, conversation_id, settings, history, user_id, source_ids=None, params=None, **kwargs):
        assert question == "alpha?"
        assert conversation_id is None
        assert user_id == admin_user["id"]
        assert source_ids == [source_id]
        assert params is not None and "vector_weight" in params
        return [
            {
                "id": 42,
                "source_id": source_id,
                "filename": "a.pdf",
                "location": "document",
                "text": "alpha evidence is here",
                "score": 0.91,
                "vector_score": 0.8,
                "keyword_score": 0.6,
            }
        ]

    monkeypatch.setattr(evals, "retrieve", fake_retrieve)

    with TestClient(main.app) as client:
        _login(client)
        admin_user, notebook_id = _seed_notebook(db)
        source_id = _seed_indexed_source(db, admin_user["id"], notebook_id, "a.pdf", summary="alpha evidence")

        created = client.post(
            "/admin/evals/sets",
            data={"notebook_id": notebook_id, "name": "Alpha Eval", "description": "Manual smoke"},
            follow_redirects=False,
        )
        assert created.status_code == 303
        eval_set_id = int(created.headers["location"].rstrip("/").split("/")[-1])

        bad_item = client.post(
            f"/admin/evals/sets/{eval_set_id}/items",
            data={"question": "bad?", "expected_source_id": "not-a-number", "expected_substrings": ""},
        )
        assert bad_item.status_code == 400

        item = client.post(
            f"/admin/evals/sets/{eval_set_id}/items",
            data={
                "question": "alpha?",
                "expected_source_id": str(source_id),
                "expected_substrings": "alpha evidence",
                "notes": "ground truth",
            },
            follow_redirects=False,
        )
        assert item.status_code == 303

        run = client.post(f"/admin/evals/sets/{eval_set_id}/run", follow_redirects=False)
        assert run.status_code == 303
        run_id = int(run.headers["location"].rstrip("/").split("/")[-1])

        detail = client.get(f"/admin/evals/runs/{run_id}")
        assert detail.status_code == 200
        assert "Alpha Eval" in detail.text
        assert '<nav aria-label="麵包屑導覽" class="breadcrumb">' in detail.text
        assert 'href="/admin/evals">評測工作台</a>' in detail.text
        assert f'href="/admin/evals/sets/{eval_set_id}">Alpha Eval</a>' in detail.text
        assert "返回 Eval Set" not in detail.text
        assert "hit" in detail.text
        assert "alpha evidence is here" in detail.text
        assert "預期依據" in detail.text
        assert "substrings: alpha evidence" in detail.text
        assert "診斷：命中預期依據" in detail.text
        assert '<pre class="config-preview">' not in detail.text
        assert "低信心閾值" in detail.text

        with db.connect() as conn:
            row = conn.execute("SELECT * FROM eval_runs WHERE id = ?", (run_id,)).fetchone()
            result = conn.execute("SELECT * FROM eval_results WHERE run_id = ?", (run_id,)).fetchone()

        metrics = json.loads(row["metrics_json"])
        retrieved = json.loads(result["retrieved_json"])
        assert row["status"] == "succeeded"
        assert metrics["recall_at_k"] == 1.0
        assert metrics["mrr"] == 1.0
        assert metrics["hits"] == 1
        assert result["status"] == "hit"
        assert result["hit_rank"] == 1
        assert result["top_score"] == 0.91
        assert retrieved[0]["chunk_id"] == 42


def test_create_apply_and_rollback_retrieval_profile(monkeypatch, tmp_path):
    """E1c: create a candidate profile, apply it to live retrieval, then roll back."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        # Eval-sets page links to the dedicated profiles page, not inlines it.
        landing = client.get("/admin/evals")
        assert landing.status_code == 200
        assert 'href="/admin/evals/profiles"' in landing.text
        assert "目前作用中的檢索 Profile" in landing.text

        profiles_page = client.get("/admin/evals/profiles")  # creates the baseline profile
        assert profiles_page.status_code == 200
        assert "檢索 Profile" in profiles_page.text
        assert "建立候選 Profile" in profiles_page.text
        assert "系統預設" in profiles_page.text

        defaults = main.current_retrieval_profile_params()
        form = {"name": "keyword-heavy", "description": "raise keyword weight",
                **{k: str(v) for k, v in defaults.items()}}
        form["keyword_weight"] = "0.9"
        form["vector_weight"] = "0.1"
        created = client.post("/admin/evals/profiles", data=form, follow_redirects=False)
        assert created.status_code == 303

        with db.connect() as conn:
            prof = conn.execute("SELECT * FROM retrieval_profiles WHERE name = 'keyword-heavy'").fetchone()
        assert prof["is_active"] == 0
        assert main.active_retrieval_params()["keyword_weight"] == defaults["keyword_weight"]

        applied = client.post(f"/admin/evals/profiles/{prof['id']}/apply", follow_redirects=False)
        assert applied.status_code == 303
        assert main.active_retrieval_params()["keyword_weight"] == 0.9
        assert main.active_retrieval_params()["vector_weight"] == 0.1
        with db.connect() as conn:
            assert conn.execute(
                "SELECT is_active FROM retrieval_profiles WHERE id = ?", (prof["id"],)
            ).fetchone()["is_active"] == 1

        # The system-default profile is now inactive but must still be undeletable.
        with db.connect() as conn:
            baseline = conn.execute("SELECT id FROM retrieval_profiles WHERE is_default = 1").fetchone()
        assert baseline is not None
        refused_default = client.post(f"/admin/evals/profiles/{baseline['id']}/delete", follow_redirects=False)
        assert refused_default.status_code == 400

        # Rollback = apply the default profile again.
        client.post(f"/admin/evals/profiles/{baseline['id']}/apply", follow_redirects=False)
        assert main.active_retrieval_params()["keyword_weight"] == defaults["keyword_weight"]

        # The active profile also cannot be deleted.
        with db.connect() as conn:
            active_id = conn.execute("SELECT id FROM retrieval_profiles WHERE is_active = 1").fetchone()["id"]
        refused = client.post(f"/admin/evals/profiles/{active_id}/delete", follow_redirects=False)
        assert refused.status_code == 400


def test_apply_profile_refuses_requires_reindex(monkeypatch, tmp_path):
    """E1c: index-affecting profiles must not be silently applied to live retrieval."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        client.get("/admin/evals")
        with db.connect() as conn:
            pid = conn.execute(
                "INSERT INTO retrieval_profiles (name, params_json, requires_reindex, is_active) "
                "VALUES ('needs-reindex', '{}', 1, 0)"
            ).lastrowid
        resp = client.post(f"/admin/evals/profiles/{pid}/apply", follow_redirects=False)
        assert resp.status_code == 400

        invalid = client.post("/admin/evals/profiles", data={
            "name": "bad", "description": "",
            "low_confidence_threshold": "x", "vector_weight": "0.7", "keyword_weight": "0.3",
            "candidate_pool_size": "20", "final_chunk_count": "6",
            "rerank_weight": "0.6", "rerank_base_weight": "0.4",
        }, follow_redirects=False)
        assert invalid.status_code == 400


def test_eval_compare_view_and_validation(monkeypatch, tmp_path):
    """E1c: comparison renders param/metric/per-question diffs; rejects bad pairs."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        admin_user, notebook_id = _seed_notebook(db)
        source_id = _seed_indexed_source(db, admin_user["id"], notebook_id, "a.pdf", summary="alpha")
        with db.connect() as conn:
            chunk_id = conn.execute("SELECT id FROM chunks WHERE source_id = ?", (source_id,)).fetchone()["id"]
            set_id = conn.execute(
                "INSERT INTO eval_sets (name, target_user_id, notebook_id, created_by) VALUES ('Cmp', ?, ?, ?)",
                (admin_user["id"], notebook_id, admin_user["id"]),
            ).lastrowid
            item_id = conn.execute(
                "INSERT INTO eval_items (eval_set_id, question, expected_chunk_id, approved) VALUES (?, 'alpha?', ?, 1)",
                (set_id, chunk_id),
            ).lastrowid
            base_params = main.current_retrieval_profile_params()
            cand_params = {**base_params, "keyword_weight": 0.9}

            def mk_run(params, metrics, status="succeeded"):
                return conn.execute(
                    "INSERT INTO eval_runs (eval_set_id, created_by, status, progress_total, progress_current, "
                    "profile_snapshot_json, metrics_json) VALUES (?, ?, ?, 1, 1, ?, ?)",
                    (set_id, admin_user["id"], status, db.dumps(params), db.dumps(metrics)),
                ).lastrowid

            base_id = mk_run(base_params, {"recall_at_k": 0.5, "mrr": 0.5, "hits": 1,
                                           "avg_latency_ms": 10, "avg_top_score": 0.4, "low_confidence_rate": 0.2})
            cand_id = mk_run(cand_params, {"recall_at_k": 1.0, "mrr": 1.0, "hits": 2,
                                           "avg_latency_ms": 8, "avg_top_score": 0.6, "low_confidence_rate": 0.1})
            running_id = mk_run(base_params, {}, status="running")
            conn.execute("INSERT INTO eval_results (run_id, eval_item_id, status, hit_rank, top_score) VALUES (?, ?, 'miss', NULL, 0.4)", (base_id, item_id))
            conn.execute("INSERT INTO eval_results (run_id, eval_item_id, status, hit_rank, top_score) VALUES (?, ?, 'hit', 1, 0.6)", (cand_id, item_id))

        page = client.get(f"/admin/evals/compare?base={base_id}&candidate={cand_id}")
        assert page.status_code == 200
        assert "參數差異" in page.text
        assert "Keyword 權重" in page.text
        assert "進步 1 題" in page.text       # the item went miss -> hit
        assert "指標差異" in page.text

        # A non-succeeded run in the pair is rejected.
        rejected = client.get(f"/admin/evals/compare?base={base_id}&candidate={running_id}")
        assert rejected.status_code == 400


def test_admin_eval_run_results_partial_polls_while_running(monkeypatch, tmp_path):
    """Eval run pages loaded mid-run must refresh results, not only the status card."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        admin_user, notebook_id = _seed_notebook(db)
        source_id = _seed_indexed_source(db, admin_user["id"], notebook_id, "a.pdf", summary="alpha evidence")
        with db.connect() as conn:
            chunk_id = conn.execute("SELECT id FROM chunks WHERE source_id = ?", (source_id,)).fetchone()["id"]
            eval_set_id = conn.execute(
                """
                INSERT INTO eval_sets (name, target_user_id, notebook_id, created_by)
                VALUES ('Polling Eval', ?, ?, ?)
                """,
                (admin_user["id"], notebook_id, admin_user["id"]),
            ).lastrowid
            item_id = conn.execute(
                """
                INSERT INTO eval_items
                (eval_set_id, question, expected_source_id, expected_chunk_id, expected_substrings_json, approved)
                VALUES (?, 'alpha?', ?, ?, ?, 1)
                """,
                (eval_set_id, source_id, chunk_id, db.dumps(["alpha evidence"])),
            ).lastrowid
            run_id = conn.execute(
                """
                INSERT INTO eval_runs
                (eval_set_id, created_by, status, progress_total, profile_snapshot_json, current_step)
                VALUES (?, ?, 'running', 1, ?, '檢索第 1 / 1 題')
                """,
                (eval_set_id, admin_user["id"], db.dumps(main.current_retrieval_profile_params())),
            ).lastrowid

        page = client.get(f"/admin/evals/runs/{run_id}")
        assert page.status_code == 200
        assert f'hx-get="/admin/evals/runs/{run_id}/_status"' in page.text
        assert f'hx-get="/admin/evals/runs/{run_id}/_results"' in page.text
        assert "尚未產生逐題結果" in page.text
        assert "低信心閾值" in page.text

        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO eval_results
                (run_id, eval_item_id, status, hit_rank, top_score, latency_ms, retrieved_json)
                VALUES (?, ?, 'hit', 1, 0.91, 12.5, ?)
                """,
                (
                    run_id,
                    item_id,
                    db.dumps([
                        {
                            "rank": 1,
                            "chunk_id": chunk_id,
                            "source_id": source_id,
                            "filename": "a.pdf",
                            "location": "document",
                            "score": 0.91,
                            "snippet": "alpha evidence",
                        }
                    ]),
                ),
            )
            conn.execute(
                "UPDATE eval_runs SET status = 'succeeded', metrics_json = ? WHERE id = ?",
                (db.dumps(_single_hit_metrics(main)), run_id),
            )

        results = client.get(f"/admin/evals/runs/{run_id}/_results")
        assert results.status_code == 200
        assert "alpha evidence" in results.text
        assert "預期依據" in results.text
        assert f'hx-get="/admin/evals/runs/{run_id}/_results"' not in results.text


def test_admin_eval_run_results_explain_miss(monkeypatch, tmp_path):
    """Miss rows show expected evidence and why the retrieved chunks did not score."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        admin_user, notebook_id = _seed_notebook(db)
        source_id = _seed_indexed_source(db, admin_user["id"], notebook_id, "a.pdf", summary="expected alpha")
        with db.connect() as conn:
            expected_chunk_id = conn.execute("SELECT id FROM chunks WHERE source_id = ?", (source_id,)).fetchone()["id"]
            other_chunk_id = conn.execute(
                """
                INSERT INTO chunks (user_id, source_id, chunk_index, location, text, embedding_json)
                VALUES (?, ?, 1, 'document p2', 'retrieved beta', '[]')
                """,
                (admin_user["id"], source_id),
            ).lastrowid
            eval_set_id = conn.execute(
                """
                INSERT INTO eval_sets (name, target_user_id, notebook_id, created_by)
                VALUES ('Miss Eval', ?, ?, ?)
                """,
                (admin_user["id"], notebook_id, admin_user["id"]),
            ).lastrowid
            item_id = conn.execute(
                """
                INSERT INTO eval_items
                (eval_set_id, question, expected_source_id, expected_chunk_id, expected_substrings_json, approved)
                VALUES (?, 'why alpha?', ?, ?, ?, 1)
                """,
                (eval_set_id, source_id, expected_chunk_id, db.dumps(["expected alpha"])),
            ).lastrowid
            run_id = conn.execute(
                """
                INSERT INTO eval_runs
                (eval_set_id, created_by, status, progress_total, profile_snapshot_json)
                VALUES (?, ?, 'succeeded', 1, ?)
                """,
                (eval_set_id, admin_user["id"], db.dumps(main.current_retrieval_profile_params())),
            ).lastrowid
            conn.execute(
                """
                INSERT INTO eval_results
                (run_id, eval_item_id, status, hit_rank, top_score, latency_ms, retrieved_json)
                VALUES (?, ?, 'miss', NULL, 0.77, 9.0, ?)
                """,
                (
                    run_id,
                    item_id,
                    db.dumps([
                        {
                            "rank": 1,
                            "chunk_id": other_chunk_id,
                            "source_id": source_id,
                            "filename": "a.pdf",
                            "location": "document p2",
                            "score": 0.77,
                            "snippet": "retrieved beta",
                        }
                    ]),
                ),
            )

        results = client.get(f"/admin/evals/runs/{run_id}/_results")
        assert results.status_code == 200
        assert "why alpha?" in results.text
        assert f"chunk #{expected_chunk_id}" in results.text
        assert "expected alpha" in results.text
        assert "retrieved beta" in results.text
        assert "診斷：有找回同一來源，但不是預期 chunk/片段。" in results.text
        assert "預期 chunk 不在目前 top-k 結果中。" in results.text


def test_eval_run_exports_sanitized_and_full_report_with_audit(monkeypatch, tmp_path):
    """E1d: sanitized export omits evidence text; full export is explicit and audited."""
    main, db = _fresh_app(monkeypatch, tmp_path)
    from app.domain_policy import limits as domain_limits

    domain_snapshot = {
        "schema_version": 1,
        "prompt_version": "notebook-domain-policy.v1",
        "revision": 3,
        "version_token": "c" * 32,
        "hints_enabled": True,
        "answer_policy_enabled": True,
        "hints": [{
            "id": 1,
            "term": "PRIVATE-TERM",
            "synonyms": ["PRIVATE-ALIAS"],
            "definition": "PRIVATE-DEFINITION",
            "query_expansions": ["PRIVATE-QUERY"],
            "answer_note": "PRIVATE-NOTE",
            "enabled": True,
        }],
        "answer_policy": "PRIVATE-POLICY",
        "limits": domain_limits(),
    }

    with TestClient(main.app) as client:
        _login(client)
        admin_user, notebook_id = _seed_notebook(db)
        source_id = _seed_indexed_source(db, admin_user["id"], notebook_id, "secret.pdf", summary="expected alpha")
        with db.connect() as conn:
            expected_chunk_id = conn.execute("SELECT id FROM chunks WHERE source_id = ?", (source_id,)).fetchone()["id"]
            eval_set_id = conn.execute(
                """
                INSERT INTO eval_sets (name, target_user_id, notebook_id, created_by)
                VALUES ('Exportable', ?, ?, ?)
                """,
                (admin_user["id"], notebook_id, admin_user["id"]),
            ).lastrowid
            item_id = conn.execute(
                """
                INSERT INTO eval_items
                (eval_set_id, question, expected_source_id, expected_chunk_id, expected_substrings_json, approved)
                VALUES (?, 'where is secret alpha?', ?, ?, ?, 1)
                """,
                (eval_set_id, source_id, expected_chunk_id, db.dumps(["expected alpha"])),
            ).lastrowid
            run_id = conn.execute(
                """
                INSERT INTO eval_runs
                (eval_set_id, created_by, status, progress_total, progress_current,
                 profile_snapshot_json, metrics_json, domain_config_snapshot_json,
                 domain_hints_enabled, answer_policy_enabled)
                VALUES (?, ?, 'succeeded', 1, 1, ?, ?, ?, 1, 1)
                """,
                (
                    eval_set_id,
                    admin_user["id"],
                    db.dumps(main.current_retrieval_profile_params()),
                    db.dumps(_single_hit_metrics(main)),
                    db.dumps(domain_snapshot),
                ),
            ).lastrowid
            conn.execute(
                """
                INSERT INTO eval_results
                (run_id, eval_item_id, status, hit_rank, top_score, latency_ms, retrieved_json)
                VALUES (?, ?, 'hit', 1, 0.91, 12.3, ?)
                """,
                (
                    run_id,
                    item_id,
                    db.dumps([
                        {
                            "rank": 1,
                            "chunk_id": expected_chunk_id,
                            "source_id": source_id,
                            "filename": "secret.pdf",
                            "location": "document",
                            "score": 0.91,
                            "snippet": "retrieved customer secret alpha",
                        }
                    ]),
                ),
            )

        page = client.get(f"/admin/evals/runs/{run_id}")
        assert page.status_code == 200
        assert f"/admin/evals/runs/{run_id}/export/sanitized" in page.text
        assert f'action="/admin/evals/runs/{run_id}/export/full"' in page.text
        assert "完整 domain 設定快照" in page.text

        forged_run = client.post(
            f"/admin/evals/sets/{eval_set_id}/run",
            data={"judge_enabled": "on"},
            files={"probe": ("probe.txt", b"x", "text/plain")},
            headers={"X-CSRF-Token": ""},
            follow_redirects=False,
        )
        forged_export = client.post(
            f"/admin/evals/runs/{run_id}/export/full",
            data={"confirm": "1"},
            files={"probe": ("probe.txt", b"x", "text/plain")},
            headers={"X-CSRF-Token": ""},
        )
        assert forged_run.status_code == 403
        assert forged_export.status_code == 403

        sanitized = client.get(f"/admin/evals/runs/{run_id}/export/sanitized")
        assert sanitized.status_code == 200
        assert "attachment" in sanitized.headers["content-disposition"]
        assert sanitized.json()["export_type"] == "sanitized_run_report"
        assert "where is secret alpha?" not in sanitized.text
        assert "expected alpha" not in sanitized.text
        assert "retrieved customer secret alpha" not in sanitized.text
        assert "PRIVATE-TERM" not in sanitized.text
        assert "PRIVATE-POLICY" not in sanitized.text
        assert "c" * 32 not in sanitized.text

        refused = client.get(f"/admin/evals/runs/{run_id}/export/full")
        assert refused.status_code == 405

        missing_confirmation = client.post(
            f"/admin/evals/runs/{run_id}/export/full",
            data={"confirm": ""},
        )
        assert missing_confirmation.status_code == 400

        full = client.post(
            f"/admin/evals/runs/{run_id}/export/full",
            data={"confirm": "1"},
        )
        assert full.status_code == 200
        full_json = full.json()
        assert full_json["export_type"] == "full_internal_run_report"
        assert full_json["results"][0]["question"] == "where is secret alpha?"
        assert full_json["results"][0]["expected"]["substrings"] == ["expected alpha"]
        assert full_json["results"][0]["retrieved"][0]["snippet"] == "retrieved customer secret alpha"
        assert full_json["run"]["domain_config_snapshot"]["answer_policy"] == "PRIVATE-POLICY"

        with db.connect() as conn:
            events = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM audit_events WHERE target_type = 'eval_run' ORDER BY id"
                ).fetchall()
            ]
        assert [event["action"] for event in events] == ["eval_run_export_sanitized", "eval_run_export_full"]
        assert events[1]["sensitivity"] == "high"
        assert json.loads(events[1]["metadata_json"])["contains_retrieved_snippets"] is True
        audit_blob = "\n".join(event["metadata_json"] for event in events)
        assert "PRIVATE-TERM" not in audit_blob
        assert "PRIVATE-POLICY" not in audit_blob
        assert "c" * 32 not in audit_blob


def test_profile_export_and_audit_page(monkeypatch, tmp_path):
    """E1d: profile exports are sanitized and the audit page can review events."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        profiles = client.get("/admin/evals/profiles")
        assert profiles.status_code == 200
        with db.connect() as conn:
            profile = conn.execute("SELECT * FROM retrieval_profiles WHERE is_default = 1").fetchone()

        exported = client.get(f"/admin/evals/profiles/{profile['id']}/export")
        assert exported.status_code == 200
        body = exported.json()
        assert body["export_type"] == "sanitized_profile"
        assert body["profile"]["id"] == profile["id"]
        assert "api_key" not in exported.text

        audit = client.get("/admin/audit", params={"action": "profile_export", "sensitivity": "normal"})
        assert audit.status_code == 200
        assert "retrieval_profile_export_sanitized" in audit.text
        assert "retrieval_profile" in audit.text
        assert "admin" in audit.text


def test_high_risk_admin_actions_are_audited(monkeypatch, tmp_path):
    """E1d: user-management and profile-apply changes are queryable audit events."""
    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        create_user = client.post(
            "/admin/users/new",
            data={"username": "audited-user", "password": "password1", "is_admin": "1"},
        )
        assert create_user.status_code == 200

        client.get("/admin/evals/profiles")
        defaults = main.current_retrieval_profile_params()
        created_profile = client.post(
            "/admin/evals/profiles",
            data={
                "name": "audited-profile",
                "description": "",
                **{key: str(value) for key, value in defaults.items()},
            },
            follow_redirects=False,
        )
        assert created_profile.status_code == 303
        with db.connect() as conn:
            profile_id = conn.execute(
                "SELECT id FROM retrieval_profiles WHERE name = 'audited-profile'"
            ).fetchone()["id"]
        applied = client.post(f"/admin/evals/profiles/{profile_id}/apply", follow_redirects=False)
        assert applied.status_code == 303

        audit = client.get("/admin/audit", params={"sensitivity": "high"})
        assert audit.status_code == 200
        assert "user_created" in audit.text
        assert "retrieval_profile_applied" in audit.text
        assert "audited-user" in audit.text
        assert "audited-profile" in audit.text


def test_admin_index_page_warns_clear_does_not_reset_dimension(monkeypatch, tmp_path):
    """O0: the index page must not sell Clear/Rebuild as a dimension migration.

    Chroma keeps a collection's locked dimension after every vector is deleted,
    so an admin who clears in order to switch embedding models ends up with an
    empty index that still rejects the new dimension. Until the permanent O0
    fix lands this page is the last thing they read before pressing Clear, so
    it has to say so and point at the operator script instead.
    """
    main, _db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        page = client.get("/admin/index")
        assert page.status_code == 200

        # The empty-collection hint must not claim the dimension is unlocked.
        assert "尚未鎖定維度" not in page.text
        # Clear's confirm dialog carries the warning, not just the page prose.
        assert "清除不會解除集合已鎖定的向量維度" in page.text
        # And the page routes dimension changes to the workaround script.
        assert "scripts/reset_chroma_dimension.py" in page.text


def test_admin_index_migration_needs_a_successful_embedding_test(monkeypatch, tmp_path):
    """O0 Phase C: the target width comes from a probe, never from the form.

    Without a successful /settings embedding test there is no trustworthy
    target, so the page must refuse rather than offer a box to type a number
    into — a typo there rebuilds the index at the wrong width.
    """
    main, _db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login(client)
        page = client.get("/admin/index")

        assert page.status_code == 200
        assert "尚無法遷移" in page.text
        assert "尚未在「設定」頁測試 embedding 模型" in page.text
        assert 'action="/admin/index/migrate"' not in page.text   # no form offered

        # And the endpoint itself refuses, not just the UI.
        posted = client.post("/admin/index/migrate", data={"confirm": "1536"}, follow_redirects=False)
        assert posted.status_code == 303
        assert "migrate-blocked" in posted.headers["location"]


def test_admin_index_migration_requires_typing_the_dimension(monkeypatch, tmp_path):
    """A destructive action needs more than one click."""
    import json as json_module

    def _diagnostic_status_succeeded():
        from app.llm import DIAGNOSTIC_STATUS_SUCCEEDED

        return DIAGNOSTIC_STATUS_SUCCEEDED

    main, db = _fresh_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:      # the schema is created by the lifespan
        _login(client)
        with db.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO llm_settings (id) VALUES (1)")
            conn.execute(
                "UPDATE llm_settings SET diagnostics_json = ? WHERE id = 1",
                # Must be the status a real probe writes. This said "ok" —
                # matching the gate's old comparison but nothing the app ever
                # stored — so the test passed while real admins were blocked.
                (json_module.dumps({
                    "embedding": {
                        "status": _diagnostic_status_succeeded(),
                        "embedding_dimension": 1536,
                    }
                }),),
            )
            conn.commit()

        page = client.get("/admin/index")
        assert 'action="/admin/index/migrate"' in page.text
        assert "輸入目標維度" in page.text

        wrong = client.post("/admin/index/migrate", data={"confirm": "384"}, follow_redirects=False)
        assert wrong.status_code == 303
        assert "migrate-confirm-mismatch" in wrong.headers["location"]

        right = client.post("/admin/index/migrate", data={"confirm": "1536"}, follow_redirects=False)
        assert right.status_code == 303
        assert "migrated-1536" in right.headers["location"]

        audit = client.get("/admin/audit", params={"sensitivity": "high"})
        assert "index_dimension_migrated" in audit.text


def test_settings_diagnostics_store_compact_results_and_audit(monkeypatch, tmp_path):
    """O1 Phase 1: admins can test chat/embedding without storing prompts or secrets."""
    main, db = _fresh_app(monkeypatch, tmp_path)
    import app.settings as app_settings

    async def fake_chat_probe(settings, include_image=False, usage_context=None):
        assert settings["api_key"] == "sk-stored"
        assert settings["chat_model"] == "chat-candidate"
        assert include_image is True
        assert usage_context["user_id"] == 1
        return {
            "status": "succeeded",
            "provider": settings["provider"],
            "model": settings["chat_model"],
            "latency_ms": 12.3,
            "capabilities": {
                "streaming": {"status": "succeeded", "latency_ms": 4.0, "usage_available": True},
                "usage_reporting": {"status": "succeeded", "usage_available": True},
                "json_following": {"status": "succeeded", "latency_ms": 8.0, "json_valid": True},
                "image_understanding": {
                    "status": "succeeded",
                    "latency_ms": 9.0,
                    "json_valid": True,
                    "semantic_match": True,
                },
                "reasoning_effort": {
                    "status": "succeeded",
                    "supported_values": ["low", "medium"],
                },
            },
        }

    async def fake_embedding_probe(settings, usage_context=None):
        # Embedding has its own connection/key now, independent of chat.
        assert settings["api_key"] == "sk-embed"
        assert settings["embedding_model"] == "embed-candidate"
        assert usage_context["user_id"] == 1
        return {
            "status": "succeeded",
            "provider": settings["provider"],
            "model": settings["embedding_model"],
            "latency_ms": 6.5,
            "embedding_dimension": 384,
        }

    monkeypatch.setattr(app_settings, "probe_chat_diagnostics", fake_chat_probe)
    monkeypatch.setattr(app_settings, "probe_embedding_diagnostics", fake_embedding_probe)

    form = {
        "provider": "openai_compatible",
        "base_url": "http://model/v1",
        "embedding_base_url": "",
        "api_key": "",
        "chat_model": "chat-candidate",
        "embedding_model": "embed-candidate",
        "embedding_query_prefix": "query:",
        "embedding_passage_prefix": "passage:",
        "api_version": "2024-02-15-preview",
        "temperature": "0.2",
        "reasoning_effort_mode": "auto",
        "reasoning_effort": "medium",
        "timeout_seconds": "60",
        "embedding_provider": "openai_compatible",
        "embedding_api_key": "",
        "embedding_api_version": "2024-02-15-preview",
    }

    with TestClient(main.app) as client:
        _login(client)
        with db.connect() as conn:
            encrypted = db.encrypt_for_storage("sk-stored")
            encrypted_embed = db.encrypt_for_storage("sk-embed")
            conn.execute(
                "UPDATE llm_settings SET api_key = ?, embedding_api_key = ? WHERE id = 1",
                (encrypted, encrypted_embed),
            )

        page = client.get("/settings")
        assert page.status_code == 200
        # The tests are HTMX swaps, not form submits — a full re-render would
        # discard the scroll position and any unsaved values the probe just ran
        # against. Pin the target ids too, since the swap silently no-ops if a
        # partial's wrapper id and its button's hx-target drift apart.
        assert 'hx-post="/settings/test-chat"' in page.text
        assert 'hx-target="#chat-diagnostics"' in page.text
        assert 'id="chat-diagnostics"' in page.text
        assert 'hx-post="/settings/test-embedding"' in page.text
        assert 'hx-target="#embedding-diagnostics"' in page.text
        assert 'id="embedding-diagnostics"' in page.text
        assert 'name="reasoning_effort_mode"' in page.text
        assert 'name="reasoning_effort"' in page.text

        chat = client.post(
            "/settings/test-chat",
            data={**form, "include_image_understanding": "1"},
        )
        assert chat.status_code == 200
        assert "聊天模型測試已完成" in chat.text
        assert "chat-candidate" in chat.text
        assert "Image understanding" in chat.text

        embedding = client.post("/settings/test-embedding", data=form)
        assert embedding.status_code == 200
        assert "Embedding 模型測試已完成" in embedding.text
        assert "embed-candidate" in embedding.text
        assert "384" in embedding.text

    with db.connect() as conn:
        row = conn.execute("SELECT diagnostics_json FROM llm_settings WHERE id = 1").fetchone()
        audit_rows = conn.execute("SELECT action, metadata_json FROM audit_events ORDER BY id").fetchall()
    diagnostics = json.loads(row["diagnostics_json"])
    assert diagnostics["chat"]["status"] == "succeeded"
    assert diagnostics["chat"]["include_image_understanding"] is True
    assert diagnostics["chat"]["capabilities"]["image_understanding"]["semantic_match"] is True
    assert diagnostics["chat"]["capabilities"]["reasoning_effort"]["supported_values"] == [
        "low",
        "medium",
    ]
    assert diagnostics["embedding"]["embedding_dimension"] == 384
    stored = row["diagnostics_json"] + "".join(item["metadata_json"] for item in audit_rows)
    assert "sk-stored" not in stored
    assert "prompt" not in stored
    assert "output" not in stored
    assert "content" not in stored
    assert [item["action"] for item in audit_rows] == [
        "llm_settings_test_chat",
        "llm_settings_test_embedding",
    ]


def test_settings_embedding_dimension_mismatch_points_at_migration_flow(monkeypatch, tmp_path):
    """O0: the /settings mismatch warning must not sell Clear/Rebuild as a fix.

    Chroma locks a collection's width on first write and Clear does not release
    it, so "Clear then Rebuild" leaves an index that still rejects the new
    dimension. This warning is what an admin reads right after the probe shows a
    different width — it has to route them to the /admin/index migration flow.
    Drives the real test-embedding route so the diagnostics the template reads
    are the ones the producer actually writes.
    """
    import re

    main, _db = _fresh_app(monkeypatch, tmp_path)
    import app.settings as app_settings

    async def fake_embedding_probe(settings, usage_context=None):
        return {
            "status": "succeeded",
            "provider": settings["provider"],
            "model": settings["embedding_model"],
            "latency_ms": 5.0,
            "embedding_dimension": 1536,
        }

    monkeypatch.setattr(app_settings, "probe_embedding_diagnostics", fake_embedding_probe)
    monkeypatch.setattr(
        app_settings,
        "vector_probe_index_dimension",
        lambda: {"dimension": 1024, "readable": True},
    )

    form = {
        "provider": "openai_compatible",
        "base_url": "http://model/v1",
        "embedding_base_url": "",
        "api_key": "",
        "chat_model": "chat-candidate",
        "embedding_model": "embed-wider",
        "embedding_query_prefix": "",
        "embedding_passage_prefix": "",
        "api_version": "2024-02-15-preview",
        "temperature": "0.2",
        "reasoning_effort_mode": "auto",
        "reasoning_effort": "medium",
        "timeout_seconds": "60",
        "embedding_provider": "openai_compatible",
        "embedding_api_key": "",
        "embedding_api_version": "2024-02-15-preview",
    }

    with TestClient(main.app) as client:
        _login(client)
        embedding = client.post("/settings/test-embedding", data=form)
        assert embedding.status_code == 200
        warnings = re.findall(r'<p class="notice failed">(.*?)</p>', embedding.text, re.S)
        assert len(warnings) == 1
        warning = warnings[0]
        # The quoted section name, not just the words: the old Clear/Rebuild
        # copy also said "更換 embedding 維度" in passing.
        assert "「更換 embedding 維度」" in warning
        assert "/admin/index" in warning
        for recommended_fix in ("Clear", "Rebuild", "清除", "重建"):
            assert recommended_fix not in warning

        # The full settings page renders the same stored diagnostic.
        page = client.get("/settings")
        assert warning in page.text


def test_settings_rejects_fixed_reasoning_effort_until_exact_value_is_probed(monkeypatch, tmp_path):
    """Saving fixed mode fails closed when the current diagnostics did not verify it."""
    main, _db = _fresh_app(monkeypatch, tmp_path)

    form = {
        "provider": "openai_compatible",
        "base_url": "http://model/v1",
        "api_key": "",
        "chat_model": "chat-candidate",
        "api_version": "2024-02-15-preview",
        "temperature": "0.2",
        "reasoning_effort_mode": "fixed",
        "reasoning_effort": "high",
        "embedding_provider": "openai_compatible",
        "embedding_base_url": "",
        "embedding_api_key": "",
        "embedding_model": "",
        "embedding_query_prefix": "",
        "embedding_passage_prefix": "",
        "embedding_api_version": "2024-02-15-preview",
        "timeout_seconds": "60",
    }

    with TestClient(main.app) as client:
        _login(client)
        response = client.post("/settings", data=form)

    assert response.status_code == 400
    assert "high" in response.text
    assert "測試聊天模型" in response.text


def test_settings_fixed_reasoning_effort_round_trips_real_probe_contract(monkeypatch, tmp_path):
    """The probe writer, compact JSON, save gate, and settings reader agree on values."""
    main, db = _fresh_app(monkeypatch, tmp_path)
    import app.settings as app_settings

    async def fake_chat_probe(settings, include_image=False, usage_context=None):
        assert settings["reasoning_effort_mode"] == "fixed"
        assert settings["reasoning_effort"] == "high"
        return {
            "status": "succeeded",
            "provider": settings["provider"],
            "model": settings["chat_model"],
            "latency_ms": 5.0,
            "capabilities": {
                "sampling_params": {
                    "status": "failed",
                    "temperature_accepted": False,
                },
                "max_tokens_field": {
                    "status": "succeeded",
                    "field": "max_completion_tokens",
                },
                "reasoning_effort": {
                    "status": "succeeded",
                    "supported_values": ["low", "medium", "high"],
                },
            },
        }

    monkeypatch.setattr(app_settings, "probe_chat_diagnostics", fake_chat_probe)
    form = {
        "provider": "openai_compatible",
        "base_url": "http://model/v1",
        "api_key": "",
        "chat_model": "chat-candidate",
        "api_version": "2024-02-15-preview",
        "temperature": "0.2",
        "reasoning_effort_mode": "fixed",
        "reasoning_effort": "high",
        "embedding_provider": "openai_compatible",
        "embedding_base_url": "",
        "embedding_api_key": "",
        "embedding_model": "",
        "embedding_query_prefix": "",
        "embedding_passage_prefix": "",
        "embedding_api_version": "2024-02-15-preview",
        "timeout_seconds": "60",
    }

    with TestClient(main.app) as client:
        _login(client)
        tested = client.post("/settings/test-chat", data=form)
        assert tested.status_code == 200
        saved = client.post("/settings", data=form)
        assert saved.status_code == 200

    with db.connect() as conn:
        loaded = db.load_llm_settings(conn)
    assert loaded["reasoning_effort_mode"] == "fixed"
    assert loaded["reasoning_effort"] == "high"
    assert loaded["diagnostics"]["chat"]["capabilities"]["reasoning_effort"]["supported_values"] == [
        "low",
        "medium",
        "high",
    ]


def _seed_eval_notebook(db):
    """Minimal notebook + indexed source + eval set owned by admin. Returns ids."""
    db.init_db()  # schema + default admin, since we seed before any request runs the lifespan
    with db.connect() as conn:
        admin_id = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()["id"]
        notebook_id = conn.execute(
            "INSERT INTO notebooks (user_id, title) VALUES (?, 'NB')", (admin_id,)
        ).lastrowid
        source_id = conn.execute(
            "INSERT INTO sources (user_id, notebook_id, filename, stored_path, status) "
            "VALUES (?, ?, 'f.txt', '/tmp/f.txt', 'indexed')",
            (admin_id, notebook_id),
        ).lastrowid
        set_id = conn.execute(
            "INSERT INTO eval_sets (name, target_user_id, notebook_id, created_by) VALUES ('S', ?, ?, ?)",
            (admin_id, notebook_id, admin_id),
        ).lastrowid
    return admin_id, notebook_id, source_id, set_id


def test_eval_set_run_form_offers_judge_checkbox(monkeypatch, tmp_path):
    """E1e-2: the run form exposes an opt-in answer-quality checkbox and its cost hint."""
    main, db = _fresh_app(monkeypatch, tmp_path)
    _admin, _nb, _src, set_id = _seed_eval_notebook(db)

    client = TestClient(main.app)
    _login(client)
    resp = client.get(f"/admin/evals/sets/{set_id}")

    assert resp.status_code == 200
    assert 'name="judge_enabled"' in resp.text
    assert "同時評測答案品質" in resp.text
    assert "2× LLM" in resp.text  # cost hint


def test_eval_run_page_shows_answer_quality_layer_when_judged(monkeypatch, tmp_path):
    """A judged run renders the separate answer-quality block + per-item judge detail,
    labelled as a reference signal — and never mixes it into Recall/MRR chrome."""
    main, db = _fresh_app(monkeypatch, tmp_path)
    admin_id, _nb, source_id, set_id = _seed_eval_notebook(db)
    metrics = {
        "total": 1, "recall_at_k": 1.0, "mrr": 1.0, "hits": 1, "scored": 1,
        "avg_latency_ms": 5, "final_chunk_count": 8,
        "judge": {
            "answered": 1, "abstained": 0, "judge_ok": 1, "judge_failed": 0,
            "answer_quality": {"correct": 1, "partial": 0, "incorrect": 0, "correct_rate": 1.0},
            "groundedness_avg": 1.0, "citation_correct_rate": 1.0,
            "abstain": {
                "correct_rate": 1.0, "unanswerable_total": 0, "unanswerable_correct_refusal": 0,
                "answerable_total": 1, "answerable_false_refusal": 0,
            },
        },
    }
    judge = {
        "answer_quality": {"label": "correct", "score": 1.0, "rationale": "matches reference"},
        "groundedness": {"score": 1.0, "unsupported_claims": [], "rationale": "grounded"},
        "citation_correctness": {"score": 1.0, "wrong_citations": [], "rationale": "correct"},
        "judge_ok": True, "judge_model": "m",
        "abstain": {"did_abstain": False, "expected_abstain": False, "correct": True},
    }
    with db.connect() as conn:
        item_id = conn.execute(
            "INSERT INTO eval_items (eval_set_id, question, expected_source_id, expected_substrings_json, "
            "item_type, expected_answer, approved) VALUES (?, 'qa', ?, '[\"alpha\"]', 'answerable', 'alpha', 1)",
            (set_id, source_id),
        ).lastrowid
        run_id = conn.execute(
            "INSERT INTO eval_runs (eval_set_id, created_by, status, progress_total, progress_current, "
            "profile_snapshot_json, judge_enabled, metrics_json) "
            "VALUES (?, ?, 'succeeded', 1, 1, '{}', 1, ?)",
            (set_id, admin_id, json.dumps(metrics)),
        ).lastrowid
        conn.execute(
            "INSERT INTO eval_results (run_id, eval_item_id, status, hit_rank, top_score, latency_ms, "
            "retrieved_json, answer_text, answer_outcome, judge_json) "
            "VALUES (?, ?, 'hit', 1, 0.9, 5, '[]', 'alpha is the answer [1]', 'answered', ?)",
            (run_id, item_id, json.dumps(judge)),
        )

    client = TestClient(main.app)
    _login(client)
    resp = client.get(f"/admin/evals/runs/{run_id}")

    assert resp.status_code == 200
    body = resp.text
    assert "答案品質" in body            # judge section heading
    assert "參考信號" in body            # reference-signal disclaimer
    assert "答案品質評分" in body         # per-item judge heading
    assert "matches reference" in body    # judge rationale surfaced inline
    assert "已作答" in body               # answer outcome label


def _single_hit_metrics(main=None):
    """Run metrics for one hit, from the real producer.

    Both call sites used to hand-write this blob — and disagreed with each other
    (one carried `scored`/`avg_latency_ms`, the other did not), which is the
    smell: two fabrications of one contract cannot both be right.
    """
    # Imported here, not at module scope: app.evals imports shared helpers back
    # from app.main, so it is only safe to reach once main is fully loaded.
    from app.evals import run_metrics_from_results

    return run_metrics_from_results(
        [{"status": "hit", "hit_rank": 1, "latency_ms": 12.5, "top_score": 0.91}]
    )
