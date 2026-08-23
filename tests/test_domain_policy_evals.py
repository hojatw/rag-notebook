"""E2d/E2e regression tests for immutable domain configuration in Eval runs."""

import asyncio
import copy
import importlib
import json
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException


def _fresh_modules(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTEBOOKLM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOTEBOOKLM_SECRET", "domain-policy-eval-test-secret")

    import app.main as main
    import app.config as app_config
    import app.security as security
    import app.db as db
    import app.vector_store as vector_store
    import app.ingest as ingest
    import app.retrieval as retrieval
    import app.admin as admin
    import app.evals as evals
    import app.settings as app_settings

    for module in (app_config, security, db, vector_store, ingest, retrieval, admin, evals, app_settings, main):
        importlib.reload(module)
    vector_store.reset_client()
    db.init_db()
    return evals, db


def _seed_eval_set(db):
    with db.connect() as conn:
        admin = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
        notebook_id = conn.execute(
            "INSERT INTO notebooks (user_id, title) VALUES (?, 'Eval notebook')", (admin["id"],)
        ).lastrowid
        source_id = conn.execute(
            "INSERT INTO sources (user_id, notebook_id, filename, stored_path, status) "
            "VALUES (?, ?, 'evidence.xlsx', '/tmp/evidence.xlsx', 'indexed')",
            (admin["id"], notebook_id),
        ).lastrowid
        eval_set_id = conn.execute(
            "INSERT INTO eval_sets (name, target_user_id, notebook_id, created_by) VALUES ('E2', ?, ?, ?)",
            (admin["id"], notebook_id, admin["id"]),
        ).lastrowid
        item_id = conn.execute(
            "INSERT INTO eval_items (eval_set_id, question, expected_source_id, expected_substrings_json, "
            "item_type, approved) VALUES (?, 'What is ACME?', ?, '[\"evidence\"]', 'answerable', 1)",
            (eval_set_id, source_id),
        ).lastrowid
    return dict(admin), notebook_id, source_id, eval_set_id, item_id


def _snapshot():
    from app.domain_policy import limits

    return {
        "schema_version": 1,
        "prompt_version": "notebook-domain-policy.v1",
        "revision": 7,
        "version_token": "a" * 32,
        "hints_enabled": True,
        "answer_policy_enabled": True,
        "hints": [{
            "id": 1,
            "term": "ACME",
            "synonyms": ["CustomerSecretAlias"],
            "definition": "TOP-SECRET-DEFINITION",
            "query_expansions": ["TOP-SECRET-EXPANSION"],
            "answer_note": "TOP-SECRET-NOTE",
            "enabled": True,
        }],
        "answer_policy": "TOP-SECRET-POLICY",
        "limits": limits(),
    }


def _seed_run(db, eval_set_id, admin_id, *, snapshot, hints=0, policy=0, judge=0):
    with db.connect() as conn:
        return conn.execute(
            "INSERT INTO eval_runs (eval_set_id, created_by, status, progress_total, profile_snapshot_json, "
            "judge_enabled, domain_config_snapshot_json, domain_hints_enabled, answer_policy_enabled) "
            "VALUES (?, ?, 'queued', 1, '{}', ?, ?, ?, ?)",
            (eval_set_id, admin_id, judge, json.dumps(snapshot), hints, policy),
        ).lastrowid


def test_eval_exports_keep_domain_content_out_of_sanitized_report(monkeypatch, tmp_path):
    evals, db = _fresh_modules(monkeypatch, tmp_path)
    admin, _, _, eval_set_id, _ = _seed_eval_set(db)
    run_id = _seed_run(db, eval_set_id, admin["id"], snapshot=_snapshot(), hints=1, policy=1)

    context = evals.eval_run_context(run_id)
    sanitized = evals.sanitized_run_export_payload(context)
    full = evals.full_run_export_payload(context)

    serialized_sanitized = json.dumps(sanitized)
    for secret in ("ACME", "CustomerSecretAlias", "TOP-SECRET-DEFINITION", "TOP-SECRET-EXPANSION", "TOP-SECRET-NOTE", "TOP-SECRET-POLICY"):
        assert secret not in serialized_sanitized
    summary = sanitized["run"]["domain_config"]
    assert summary["snapshot_available"] is True
    assert summary["snapshot_status"] == "available"
    assert summary["revision"] == 7
    assert "version_token" not in summary
    assert len(summary["version_fingerprint"]) == 12
    assert summary["hints_enabled"] is True
    assert summary["answer_policy_enabled"] is True
    assert summary["hint_count"] == summary["enabled_hint_count"] == 1
    assert summary["policy_present"] is True
    assert summary["policy_chars"] == len("TOP-SECRET-POLICY")
    assert summary["policy_tokens_est"] > 0
    assert full["run"]["domain_config_snapshot"] == _snapshot()


def test_invalid_or_old_snapshot_is_fail_closed_without_live_config(monkeypatch, tmp_path):
    evals, db = _fresh_modules(monkeypatch, tmp_path)
    admin, _, _, eval_set_id, _ = _seed_eval_set(db)
    run_id = _seed_run(db, eval_set_id, admin["id"], snapshot="not-a-dict", hints=1, policy=1)

    context = evals.eval_run_context(run_id)

    assert context["run"]["domain_config_snapshot"] == {}
    assert context["run"]["domain_config_snapshot_status"] == "invalid"
    assert context["run"]["domain_config_summary"]["hints_enabled"] is False
    assert context["run"]["domain_config_summary"]["answer_policy_enabled"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(prompt_version="unknown-version"),
        lambda value: value.update(version_token="TOP-SECRET-TOKEN"),
        lambda value: value["limits"].update(max_hint_tokens=999_999),
        lambda value: value.update(hints_enabled=False),
    ],
)
def test_frozen_snapshot_rejects_noncanonical_or_unsafe_values(monkeypatch, tmp_path, mutation):
    evals, _ = _fresh_modules(monkeypatch, tmp_path)
    snapshot = copy.deepcopy(_snapshot())
    mutation(snapshot)

    frozen, status = evals._frozen_domain_snapshot(json.dumps(snapshot))

    assert frozen == {}
    assert status == "invalid"


def test_frozen_snapshot_rejects_oversized_json_before_parsing(monkeypatch, tmp_path):
    evals, _ = _fresh_modules(monkeypatch, tmp_path)
    raw = "{" + (" " * evals.MAX_SNAPSHOT_JSON_CHARS) + "}"

    assert evals._frozen_domain_snapshot(raw) == ({}, "invalid")


def test_start_run_freezes_server_side_snapshot_and_flags(monkeypatch, tmp_path):
    evals, db = _fresh_modules(monkeypatch, tmp_path)
    admin, notebook_id, _, eval_set_id, _ = _seed_eval_set(db)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO notebook_domain_config (notebook_id, hints_enabled, answer_policy_enabled, "
            "answer_policy, revision, version_token, updated_by) VALUES (?, 1, 1, ?, 4, ?, ?)",
            (notebook_id, "INTERNAL-POLICY", "b" * 32, admin["id"]),
        )
        conn.execute(
            "INSERT INTO notebook_domain_hints (notebook_id, term, synonyms_json, definition, "
            "query_expansions_json, answer_note, enabled) VALUES (?, ?, '[]', '', '[]', ?, 1)",
            (notebook_id, "ACME", "INTERNAL-NOTE"),
        )

    tasks = BackgroundTasks()
    response = evals.admin_start_eval_run(
        eval_set_id,
        tasks,
        admin,
        None,
        profile_id=None,
        judge_enabled="on",
        domain_hints_enabled="on",
        answer_policy_enabled="on",
    )
    run_id = int(response.headers["location"].rstrip("/").split("/")[-1])
    with db.connect() as conn:
        run = dict(conn.execute("SELECT * FROM eval_runs WHERE id = ?", (run_id,)).fetchone())

    frozen = json.loads(run["domain_config_snapshot_json"])
    assert run["domain_hints_enabled"] == 1
    assert run["answer_policy_enabled"] == 1
    assert frozen["answer_policy"] == "INTERNAL-POLICY"
    assert frozen["hints"][0]["term"] == "ACME"
    assert len(tasks.tasks) == 1


def test_policy_run_requires_answer_judging(monkeypatch, tmp_path):
    evals, db = _fresh_modules(monkeypatch, tmp_path)
    admin, _, _, eval_set_id, _ = _seed_eval_set(db)

    with pytest.raises(HTTPException, match="Answer policy run"):
        evals.admin_start_eval_run(
            eval_set_id,
            BackgroundTasks(),
            admin,
            None,
            profile_id=None,
            judge_enabled=None,
            domain_hints_enabled=None,
            answer_policy_enabled="on",
        )


def test_eval_runner_uses_frozen_hints_and_structural_answer_result(monkeypatch, tmp_path):
    evals, db = _fresh_modules(monkeypatch, tmp_path)
    admin, _, source_id, eval_set_id, _ = _seed_eval_set(db)
    run_id = _seed_run(db, eval_set_id, admin["id"], snapshot=_snapshot(), hints=1, policy=1, judge=1)
    observed = {}

    async def fake_retrieve(question, *args, domain_hints=None, **kwargs):
        observed["hints"] = domain_hints
        return [{
            "id": 1,
            "source_id": source_id,
            "filename": "evidence.xlsx",
            "location": "sheet1",
            "text": "evidence",
            "score": 0.9,
        }]

    async def fake_result(question, chunks, settings, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(text="grounded answer [1]", abstained=False)

    async def fake_judge(**kwargs):
        return {
            "answer_quality": {"label": "not_applicable", "score": None, "rationale": ""},
            "groundedness": {"score": 1.0, "unsupported_claims": [], "rationale": ""},
            "citation_correctness": {"score": 1.0, "wrong_citations": [], "rationale": ""},
            "judge_ok": True,
            "judge_model": "test",
        }

    monkeypatch.setattr(evals, "retrieve", fake_retrieve)
    monkeypatch.setattr(evals.llm, "generate_answer_result", fake_result, raising=False)
    monkeypatch.setattr(evals, "judge_answer", fake_judge)

    asyncio.run(evals.run_eval_job(run_id))

    assert observed["hints"][0]["term"] == "ACME"
    assert observed["answer_policy"] == "TOP-SECRET-POLICY"
    assert observed["answer_notes"] == ["TOP-SECRET-NOTE"]
    assert observed["spreadsheet_guard"] is True
    assert observed["domain_limits"] == _snapshot()["limits"]
    with db.connect() as conn:
        result = dict(conn.execute("SELECT * FROM eval_results WHERE run_id = ?", (run_id,)).fetchone())
    assert result["answer_outcome"] == "answered"


@pytest.mark.parametrize(
    ("snapshot", "hints", "policy", "judge", "expect_hints", "expect_policy", "expect_notes"),
    [
        (_snapshot(), 0, 0, 0, False, False, False),
        (_snapshot(), 1, 0, 0, True, False, False),
        (_snapshot(), 0, 1, 1, False, True, False),
        (_snapshot(), 1, 1, 1, True, True, True),
        ({}, 1, 1, 1, False, False, False),
    ],
)
def test_eval_runtime_mode_matrix(
    monkeypatch,
    tmp_path,
    snapshot,
    hints,
    policy,
    judge,
    expect_hints,
    expect_policy,
    expect_notes,
):
    evals, db = _fresh_modules(monkeypatch, tmp_path)
    admin, _, source_id, eval_set_id, _ = _seed_eval_set(db)
    run_id = _seed_run(
        db,
        eval_set_id,
        admin["id"],
        snapshot=snapshot,
        hints=hints,
        policy=policy,
        judge=judge,
    )
    observed = {"generate_calls": 0}

    async def fake_retrieve(question, *args, domain_hints=None, domain_limits=None, **kwargs):
        observed["domain_hints"] = domain_hints
        observed["retrieval_limits"] = domain_limits
        return [{
            "id": 1,
            "source_id": source_id,
            "filename": "evidence.pdf",
            "location": "p1",
            "text": "evidence",
            "score": 0.9,
        }]

    async def fake_result(question, chunks, settings, **kwargs):
        observed["generate_calls"] += 1
        observed.update(kwargs)
        return SimpleNamespace(text="grounded [1]", abstained=False)

    async def fake_judge(**kwargs):
        return {
            "answer_quality": {"label": "correct", "score": 1.0, "rationale": ""},
            "groundedness": {"score": 1.0, "unsupported_claims": [], "rationale": ""},
            "citation_correctness": {"score": 1.0, "wrong_citations": [], "rationale": ""},
            "judge_ok": True,
            "judge_model": "test",
        }

    monkeypatch.setattr(evals, "retrieve", fake_retrieve)
    monkeypatch.setattr(evals.llm, "generate_answer_result", fake_result)
    monkeypatch.setattr(evals, "judge_answer", fake_judge)
    asyncio.run(evals.run_eval_job(run_id))

    assert bool(observed["domain_hints"]) is expect_hints
    assert bool(observed["retrieval_limits"]) is expect_hints
    assert observed["generate_calls"] == judge
    if judge:
        assert bool(observed["answer_policy"]) is expect_policy
        assert bool(observed["answer_notes"]) is expect_notes
        assert bool(observed["domain_limits"]) is (expect_hints or expect_policy)


def test_structural_abstention_does_not_depend_on_refusal_wording(monkeypatch, tmp_path):
    evals, _ = _fresh_modules(monkeypatch, tmp_path)
    calls = {"judge": 0}

    async def fake_result(*args, **kwargs):
        return SimpleNamespace(text="ordinary-looking provider text", abstained=True)

    async def fake_judge(**kwargs):
        calls["judge"] += 1
        return {}

    monkeypatch.setattr(evals.llm, "generate_answer_result", fake_result)
    monkeypatch.setattr(evals, "judge_answer", fake_judge)
    outcome = asyncio.run(
        evals.judge_eval_item(
            "q",
            {"id": 1, "item_type": "unanswerable"},
            [{"filename": "f.pdf", "location": "p1", "text": "evidence", "score": 0.9}],
            0.9,
            0.25,
            {"chat_model": "test"},
            {},
        )
    )

    assert outcome["answer_outcome"] == "abstained"
    assert outcome["judge"]["abstain"]["refused_at_generation"] is True
    assert calls["judge"] == 0


def test_compare_context_exposes_only_compact_domain_diff(monkeypatch, tmp_path):
    evals, db = _fresh_modules(monkeypatch, tmp_path)
    admin, _, _, eval_set_id, _ = _seed_eval_set(db)
    base_id = _seed_run(db, eval_set_id, admin["id"], snapshot={}, hints=0, policy=0)
    candidate_id = _seed_run(db, eval_set_id, admin["id"], snapshot=_snapshot(), hints=1, policy=1)
    with db.connect() as conn:
        conn.execute("UPDATE eval_runs SET status = 'succeeded' WHERE id IN (?, ?)", (base_id, candidate_id))

    context = evals.compare_runs_context(base_id, candidate_id)
    diff = context["domain_config_diff"]

    assert diff["hints_changed"] is True
    assert diff["answer_policy_changed"] is True
    assert "TOP-SECRET-POLICY" not in json.dumps(diff)
    assert "ACME" not in json.dumps(diff)
