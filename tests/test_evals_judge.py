"""E1e-2 answer-judging tests: deterministic abstain, per-item isolation, metric
aggregation, run wiring, and full regression when judging is off."""

import asyncio
import importlib
import json


def _fresh_modules(monkeypatch, tmp_path):
    """Reload the app graph against an isolated temp DB and return (evals, db).

    Mirrors tests/test_ui._fresh_app: app.main is imported first because it is the
    package import root that the route modules import shared helpers back from.
    """
    monkeypatch.setenv("NOTEBOOKLM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOTEBOOKLM_SECRET", "eval-judge-secret")

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


def _answered_judge():
    return {
        "answer_quality": {"label": "correct", "score": 1.0, "rationale": "matches reference"},
        "groundedness": {"score": 1.0, "unsupported_claims": [], "rationale": "grounded"},
        "citation_correctness": {"score": 1.0, "wrong_citations": [], "rationale": "correct marker"},
        "judge_ok": True,
        "judge_model": "test-model",
    }


# --- deterministic helpers -------------------------------------------------


def test_judge_eval_item_abstains_and_skips_generation(monkeypatch, tmp_path):
    """Unanswerable item with no retrieval must abstain deterministically and never
    call the LLM — that saves a call and mirrors real ask() behaviour."""
    evals, _ = _fresh_modules(monkeypatch, tmp_path)
    calls = {"generate": 0, "judge": 0}

    async def fake_generate(*args, **kwargs):
        calls["generate"] += 1
        return "should never run"

    async def fake_judge(**kwargs):
        calls["judge"] += 1
        return _answered_judge()

    monkeypatch.setattr(evals, "generate_answer", fake_generate)
    monkeypatch.setattr(evals, "judge_answer", fake_judge)

    item = {"id": 1, "item_type": "unanswerable", "expected_substrings": [], "expected_answer": ""}
    out = asyncio.run(evals.judge_eval_item("q?", item, [], 0.0, 0.25, {"chat_model": "m"}, {}))

    assert out["answer_outcome"] == "abstained"
    assert calls == {"generate": 0, "judge": 0}
    assert out["judge"]["abstain"] == {
        "did_abstain": True,
        "retrieval_gated": True,
        "refused_at_generation": False,
        "expected_abstain": True,
        "correct": True,
    }
    assert out["judge"]["answer_quality"]["label"] == "not_applicable"


def test_judge_eval_item_generates_and_judges_answerable(monkeypatch, tmp_path):
    evals, _ = _fresh_modules(monkeypatch, tmp_path)

    async def fake_generate(question, chunks, settings, **kwargs):
        return "The value is 2024-02 [1]."

    async def fake_judge(**kwargs):
        return _answered_judge()

    monkeypatch.setattr(evals, "generate_answer", fake_generate)
    monkeypatch.setattr(evals, "judge_answer", fake_judge)

    item = {"id": 2, "item_type": "answerable", "expected_substrings": ["2024-02"], "expected_answer": "2024-02"}
    retrieved = [{"filename": "f", "location": "p1", "text": "api version 2024-02", "score": 0.9}]
    out = asyncio.run(evals.judge_eval_item("which version?", item, retrieved, 0.9, 0.25, {"chat_model": "m"}, {}))

    assert out["answer_outcome"] == "answered"
    assert out["judge"]["judge_ok"] is True
    assert out["judge"]["abstain"] == {
        "did_abstain": False,
        "retrieval_gated": False,
        "refused_at_generation": False,
        "expected_abstain": False,
        "correct": True,
    }


def test_answer_quality_not_scored_without_a_reference_answer(monkeypatch, tmp_path):
    """Route A grades answer_quality by comparison. With no reference answer the judge
    still emits a label — discard it rather than manufacture a signal (QUALITY.md Q1-6).
    groundedness/citation must survive, since they only need the retrieved chunks.
    """
    evals, _ = _fresh_modules(monkeypatch, tmp_path)

    async def fake_generate(question, chunks, settings, **kwargs):
        return "some grounded answer [1]."

    async def fake_judge(**kwargs):
        return _answered_judge()  # judge insists on "correct"

    monkeypatch.setattr(evals, "generate_answer", fake_generate)
    monkeypatch.setattr(evals, "judge_answer", fake_judge)

    retrieved = [{"filename": "f", "location": "l", "text": "t", "score": 0.9}]
    no_ref = {"id": 4, "item_type": "answerable", "expected_substrings": [], "expected_answer": ""}
    with_ref = {"id": 5, "item_type": "answerable", "expected_substrings": [], "expected_answer": "alpha"}

    out_no_ref = asyncio.run(evals.judge_eval_item("q?", no_ref, retrieved, 0.9, 0.3, {"chat_model": "m"}, {}))
    out_with_ref = asyncio.run(evals.judge_eval_item("q?", with_ref, retrieved, 0.9, 0.3, {"chat_model": "m"}, {}))

    assert out_no_ref["judge"]["has_reference_answer"] is False
    assert out_no_ref["judge"]["answer_quality"]["label"] == "not_applicable"
    # the other two dimensions are unaffected — they never needed a reference
    assert out_no_ref["judge"]["groundedness"]["score"] == 1.0
    assert out_no_ref["judge"]["citation_correctness"]["score"] == 1.0

    assert out_with_ref["judge"]["has_reference_answer"] is True
    assert out_with_ref["judge"]["answer_quality"]["label"] == "correct"


def test_judge_metrics_exclude_unreferenced_items_from_quality_rate(monkeypatch, tmp_path):
    """correct_rate is a rate over items that *could* be graded, not over all answered."""
    evals, _ = _fresh_modules(monkeypatch, tmp_path)
    abstain_ok = {"did_abstain": False, "retrieval_gated": False, "refused_at_generation": False,
                  "expected_abstain": False, "correct": True}
    results = [
        {"answer_outcome": "answered", "judge": {
            "answer_quality": {"label": "correct"}, "groundedness": {"score": 1.0},
            "citation_correctness": {"score": 1.0}, "judge_ok": True,
            "has_reference_answer": True, "abstain": abstain_ok}},
        # three unreferenced items the judge would have rubber-stamped
        *[{"answer_outcome": "answered", "judge": {
            "answer_quality": {"label": "not_applicable"}, "groundedness": {"score": 0.5},
            "citation_correctness": {"score": 1.0}, "judge_ok": True,
            "has_reference_answer": False, "abstain": abstain_ok}} for _ in range(3)],
    ]

    metrics = evals.judge_metrics_from_results(results)

    quality = metrics["answer_quality"]
    assert quality["scored"] == 1
    assert quality["not_applicable"] == 3
    assert quality["correct"] == 1
    assert quality["correct_rate"] == 1.0  # 1/1, NOT 1/4 and not inflated to 4/4
    # groundedness still averages over all four answered items
    assert metrics["groundedness_avg"] == round((1.0 + 0.5 * 3) / 4, 4)


def test_answer_is_refusal_matches_pinned_wording(monkeypatch, tmp_path):
    """Deterministic detection of the refusal SYSTEM_PROMPT pins, tolerant of surrounding
    prose/whitespace/case but never firing on a real answer."""
    evals, _ = _fresh_modules(monkeypatch, tmp_path)

    assert evals.answer_is_refusal("I cannot determine that from the selected sources.") is True
    # tolerant of case, collapsed whitespace, and trailing prose
    assert evals.answer_is_refusal("  I Cannot Determine That From The\n Selected Sources. Sorry.") is True
    assert evals.answer_is_refusal("The API version is 2024-02 [1].") is False
    assert evals.answer_is_refusal("") is False


def test_judge_eval_item_counts_generation_stage_refusal(monkeypatch, tmp_path):
    """The abstain metric must count a model-side refusal, not just the score gate.

    Regression guard for the real finding: an on-topic question whose specific fact is
    absent still retrieves high-scoring chunks (no threshold can gate it), so only the
    model refuses. Previously that counted as a *failed* abstain.
    """
    evals, _ = _fresh_modules(monkeypatch, tmp_path)

    async def fake_generate(question, chunks, settings, **kwargs):
        return "I cannot determine that from the selected sources."

    async def fake_judge(**kwargs):
        return _answered_judge()

    monkeypatch.setattr(evals, "generate_answer", fake_generate)
    monkeypatch.setattr(evals, "judge_answer", fake_judge)

    item = {"id": 9, "item_type": "unanswerable", "expected_substrings": [], "expected_answer": ""}
    # top_score 0.86 is far above threshold — the score gate cannot catch this one.
    retrieved = [{"filename": "f", "location": "l", "text": "on-topic but silent", "score": 0.86}]
    out = asyncio.run(evals.judge_eval_item("q?", item, retrieved, 0.86, 0.3, {"chat_model": "m"}, {}))

    abstain = out["judge"]["abstain"]
    assert abstain["retrieval_gated"] is False       # score gate did NOT fire
    assert abstain["refused_at_generation"] is True  # the model refused instead
    assert abstain["did_abstain"] is True            # system-level refusal = ① or ②
    assert abstain["correct"] is True                # unanswerable + refused = correct


def test_judge_metrics_count_generation_stage_refusal_as_correct(monkeypatch, tmp_path):
    """End of the chain: a generation-stage refusal lands in the aggregate abstain stats."""
    evals, _ = _fresh_modules(monkeypatch, tmp_path)
    results = [
        {"answer_outcome": "answered", "judge": {
            "answer_quality": {"label": "correct"}, "groundedness": {"score": 1.0},
            "citation_correctness": {"score": 1.0}, "judge_ok": True,
            "abstain": {"did_abstain": True, "retrieval_gated": False, "refused_at_generation": True,
                        "expected_abstain": True, "correct": True}}},
        {"answer_outcome": "abstained", "judge": {
            **evals._not_applicable_judge(),
            "abstain": {"did_abstain": True, "retrieval_gated": True, "refused_at_generation": False,
                        "expected_abstain": True, "correct": True}}},
    ]

    metrics = evals.judge_metrics_from_results(results)

    assert metrics["abstain"]["unanswerable_total"] == 2
    assert metrics["abstain"]["unanswerable_correct_refusal"] == 2
    assert metrics["abstain"]["unanswerable_correct_refusal_rate"] == 1.0
    assert metrics["abstain"]["correct_rate"] == 1.0


def test_judge_eval_item_generate_failure_is_isolated(monkeypatch, tmp_path):
    """A generation failure marks only this item as error and keeps the deterministic
    abstain signal; it must not propagate out of judge_eval_item."""
    evals, _ = _fresh_modules(monkeypatch, tmp_path)

    async def boom(*args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(evals, "generate_answer", boom)
    monkeypatch.setattr(evals, "judge_answer", boom)

    item = {"id": 3, "item_type": "answerable", "expected_substrings": [], "expected_answer": "a"}
    retrieved = [{"filename": "f", "location": "l", "text": "t", "score": 0.9}]
    out = asyncio.run(evals.judge_eval_item("q?", item, retrieved, 0.9, 0.25, {"chat_model": "m"}, {}))

    assert out["answer_outcome"] == "error"
    assert out["judge"]["abstain"]["did_abstain"] is False
    assert "provider down" in out["judge"]["error"]


def test_judge_metrics_splits_abstain_and_quality(monkeypatch, tmp_path):
    evals, _ = _fresh_modules(monkeypatch, tmp_path)
    na = evals._not_applicable_judge()
    results = [
        {"answer_outcome": "answered", "judge": {
            "answer_quality": {"label": "correct"}, "groundedness": {"score": 1.0},
            "citation_correctness": {"score": 1.0}, "judge_ok": True,
            "abstain": {"did_abstain": False, "expected_abstain": False, "correct": True}}},
        {"answer_outcome": "answered", "judge": {
            "answer_quality": {"label": "incorrect"}, "groundedness": {"score": 0.0},
            "citation_correctness": {"score": 0.5}, "judge_ok": True,
            "abstain": {"did_abstain": False, "expected_abstain": False, "correct": True}}},
        {"answer_outcome": "abstained", "judge": {
            **na,
            "abstain": {"did_abstain": True, "expected_abstain": True, "correct": True}}},
        {"answer_outcome": "abstained", "judge": {
            **na,
            "abstain": {"did_abstain": True, "expected_abstain": False, "correct": False}}},
        # Retrieval-only item without a judge payload must be ignored entirely.
        {"answer_outcome": "", "status": "hit"},
    ]

    metrics = evals.judge_metrics_from_results(results)

    assert metrics["answered"] == 2
    assert metrics["answer_quality"] == {
        "correct": 1, "partial": 0, "incorrect": 1, "correct_rate": 0.5,
        "scored": 2, "not_applicable": 0,
    }
    assert metrics["groundedness_avg"] == 0.5
    assert metrics["citation_correct_rate"] == 0.75
    abstain = metrics["abstain"]
    assert abstain["unanswerable_total"] == 1
    assert abstain["unanswerable_correct_refusal"] == 1
    assert abstain["unanswerable_correct_refusal_rate"] == 1.0
    # Two answered answerable + one falsely-refused answerable = 3 expected-answerable items.
    assert abstain["answerable_total"] == 3
    assert abstain["answerable_false_refusal"] == 1
    assert abstain["answerable_false_refusal_rate"] == round(1 / 3, 4)


# --- run_eval_job integration ---------------------------------------------


def _seed_run(db, judge_flag):
    """Seed a notebook + indexed source + eval set with one answerable and one
    unanswerable item, plus a queued run. Returns (run_id, source_id, item ids)."""
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
        answerable_id = conn.execute(
            "INSERT INTO eval_items (eval_set_id, question, expected_source_id, expected_substrings_json, "
            "item_type, expected_answer, approved) VALUES (?, 'qa', ?, ?, 'answerable', 'alpha', 1)",
            (set_id, source_id, json.dumps(["alpha"])),
        ).lastrowid
        unanswerable_id = conn.execute(
            "INSERT INTO eval_items (eval_set_id, question, expected_substrings_json, item_type, approved) "
            "VALUES (?, 'qb', '[]', 'unanswerable', 1)",
            (set_id,),
        ).lastrowid
        run_id = conn.execute(
            "INSERT INTO eval_runs (eval_set_id, created_by, status, progress_total, "
            "profile_snapshot_json, judge_enabled) VALUES (?, ?, 'queued', 2, '{}', ?)",
            (set_id, admin_id, judge_flag),
        ).lastrowid
    return run_id, source_id, answerable_id, unanswerable_id


def _patch_llm(monkeypatch, evals, source_id, calls):
    async def fake_retrieve(question, *args, **kwargs):
        if question == "qa":
            return [{
                "id": 1, "source_id": source_id, "filename": "f.txt", "location": "p1",
                "text": "alpha beta gamma", "score": 0.9, "vector_score": 0.9, "keyword_score": 0.2,
            }]
        return []  # unanswerable → no retrieval → deterministic abstain

    async def fake_generate(question, chunks, settings, **kwargs):
        calls["generate"] += 1
        return "alpha is the answer [1]."

    async def fake_judge(**kwargs):
        calls["judge"] += 1
        return _answered_judge()

    monkeypatch.setattr(evals, "retrieve", fake_retrieve)
    monkeypatch.setattr(evals, "generate_answer", fake_generate)
    monkeypatch.setattr(evals, "judge_answer", fake_judge)


def test_run_eval_job_with_judge_enabled_stores_layered_results(monkeypatch, tmp_path):
    evals, db = _fresh_modules(monkeypatch, tmp_path)
    run_id, source_id, answerable_id, unanswerable_id = _seed_run(db, judge_flag=1)
    calls = {"generate": 0, "judge": 0}
    _patch_llm(monkeypatch, evals, source_id, calls)

    asyncio.run(evals.run_eval_job(run_id))

    with db.connect() as conn:
        run = dict(conn.execute("SELECT * FROM eval_runs WHERE id = ?", (run_id,)).fetchone())
        results = {
            r["eval_item_id"]: dict(r)
            for r in conn.execute("SELECT * FROM eval_results WHERE run_id = ?", (run_id,)).fetchall()
        }

    assert run["status"] == "succeeded"
    # Answerable item: generated + judged; unanswerable: abstained without generation.
    assert calls == {"generate": 1, "judge": 1}

    answerable = results[answerable_id]
    assert answerable["status"] == "hit"
    assert answerable["answer_outcome"] == "answered"
    assert answerable["answer_text"] == "alpha is the answer [1]."
    assert json.loads(answerable["judge_json"])["judge_ok"] is True

    unanswerable = results[unanswerable_id]
    assert unanswerable["answer_outcome"] == "abstained"
    assert unanswerable["answer_text"]  # canned refusal copy, non-empty

    metrics = json.loads(run["metrics_json"])
    # Judge metrics are nested and never mixed into the retrieval layer.
    assert "recall_at_k" in metrics and "recall_at_k" not in metrics["judge"]
    assert metrics["judge"]["answered"] == 1
    assert metrics["judge"]["abstain"]["unanswerable_correct_refusal"] == 1
    assert metrics["judge"]["answer_quality"]["correct"] == 1


def test_run_eval_job_without_judge_is_full_regression(monkeypatch, tmp_path):
    """judge_enabled=0 must behave exactly like the retrieval-only runner: no
    generate/judge calls, empty answer columns, and no judge metrics block."""
    evals, db = _fresh_modules(monkeypatch, tmp_path)
    run_id, source_id, *_ = _seed_run(db, judge_flag=0)
    calls = {"generate": 0, "judge": 0}
    _patch_llm(monkeypatch, evals, source_id, calls)

    asyncio.run(evals.run_eval_job(run_id))

    with db.connect() as conn:
        run = dict(conn.execute("SELECT * FROM eval_runs WHERE id = ?", (run_id,)).fetchone())
        results = [dict(r) for r in conn.execute("SELECT * FROM eval_results WHERE run_id = ?", (run_id,)).fetchall()]

    assert run["status"] == "succeeded"
    assert calls == {"generate": 0, "judge": 0}
    assert "judge" not in json.loads(run["metrics_json"])
    for result in results:
        assert result["judge_json"] == "{}"
        assert result["answer_text"] == ""
        assert result["answer_outcome"] == ""


def test_full_export_carries_judge_detail_sanitized_only_aggregates(monkeypatch, tmp_path):
    """E1e-2 export layering: judge rationale + generated answer live only in the full
    internal report; the sanitized report keeps aggregate judge numbers but no per-item
    answer/rationale text."""
    evals, db = _fresh_modules(monkeypatch, tmp_path)
    run_id, source_id, answerable_id, _ = _seed_run(db, judge_flag=1)
    _patch_llm(monkeypatch, evals, source_id, {"generate": 0, "judge": 0})
    asyncio.run(evals.run_eval_job(run_id))

    context = evals.eval_run_context(run_id)
    sanitized = evals.sanitized_run_export_payload(context)
    full = evals.full_run_export_payload(context)

    # Aggregate judge numbers ride along in metrics — safe to share.
    assert sanitized["run"]["metrics"]["judge"]["answered"] == 1
    # ...but no per-item answer text or judge payload in the sanitized results.
    for item in sanitized["results"]:
        assert "answer_text" not in item
        assert "judge" not in item

    full_by_id = {item["eval_item_id"]: item for item in full["results"]}
    answered = full_by_id[answerable_id]
    assert answered["answer_outcome"] == "answered"
    assert answered["answer_text"] == "alpha is the answer [1]."
    assert answered["judge"]["judge_ok"] is True
