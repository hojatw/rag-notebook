import asyncio
import sqlite3

import pytest

from app import llm
from app.domain_policy import (
    DomainPolicyValidationError,
    create_domain_hint,
    deterministic_hint_queries,
    domain_config_summary,
    load_domain_config,
    match_domain_hints,
    save_domain_config,
    snapshot_domain_config,
    spreadsheet_answer_guard,
    update_domain_hint,
    validate_answer_policy,
)
from app.governance import _clean_metadata


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE notebooks (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id));
        CREATE TABLE notebook_domain_config (
            notebook_id INTEGER PRIMARY KEY REFERENCES notebooks(id) ON DELETE CASCADE,
            hints_enabled INTEGER NOT NULL DEFAULT 0,
            answer_policy_enabled INTEGER NOT NULL DEFAULT 0,
            answer_policy TEXT NOT NULL DEFAULT '',
            revision INTEGER NOT NULL DEFAULT 0,
            version_token TEXT NOT NULL DEFAULT '',
            updated_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE notebook_domain_hints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            notebook_id INTEGER NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
            term TEXT NOT NULL,
            synonyms_json TEXT NOT NULL DEFAULT '[]',
            definition TEXT NOT NULL DEFAULT '',
            query_expansions_json TEXT NOT NULL DEFAULT '[]',
            answer_note TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO users (id) VALUES (1);
        INSERT INTO notebooks (id, user_id) VALUES (10, 1);
        """
    )
    yield connection
    connection.close()


def test_domain_config_crud_revisions_and_opaque_tokens(conn):
    assert load_domain_config(conn, 10)["revision"] == 0

    first = save_domain_config(
        conn,
        10,
        hints_enabled=True,
        answer_policy_enabled=True,
        answer_policy="請先給結論。",
        updated_by=1,
    )
    hint = create_domain_hint(
        conn,
        10,
        updated_by=1,
        term="Ecal Set",
        synonyms="Eval Set\n評測集\nEval Set",
        definition="部署內評測資料集",
        query_expansions="evaluation dataset\n評測資料集",
        answer_note="沿用客戶用語。",
        enabled=True,
    )
    loaded = load_domain_config(conn, 10)

    assert first["revision"] == 1
    assert loaded["revision"] == 2
    assert first["version_token"] != loaded["version_token"]
    assert len(loaded["version_token"]) == 32
    assert hint["synonyms"] == ["Eval Set", "評測集"]
    assert loaded["hints"][0]["query_expansions"] == ["evaluation dataset", "評測資料集"]

    updated = update_domain_hint(
        conn,
        10,
        hint["id"],
        updated_by=1,
        term="Eval Set",
        synonyms="評測集",
        definition="",
        query_expansions="",
        answer_note="",
        enabled=False,
    )
    assert updated["enabled"] is False
    assert load_domain_config(conn, 10)["revision"] == 3


def test_duplicate_terms_and_policy_limits_fail_closed(conn):
    create_domain_hint(conn, 10, updated_by=1, term="RAG")
    with pytest.raises(DomainPolicyValidationError, match="duplicate_term"):
        create_domain_hint(conn, 10, updated_by=1, term="rag")
    with pytest.raises(DomainPolicyValidationError, match="policy_too_long"):
        validate_answer_policy("x" * 1001)


def test_hint_matching_is_bounded_and_does_not_match_latin_substrings():
    hints = [
        {
            "id": 1,
            "term": "AI",
            "synonyms": ["人工智慧"],
            "definition": "",
            "query_expansions": ["artificial intelligence governance"],
            "answer_note": "",
            "enabled": True,
        },
        {
            "id": 2,
            "term": "評測集",
            "synonyms": [],
            "definition": "",
            "query_expansions": ["Eval Set"],
            "answer_note": "",
            "enabled": True,
        },
    ]
    assert match_domain_hints("This was said yesterday", hints) == []
    matched = match_domain_hints("AI 與評測集如何治理？", hints)
    assert [item["id"] for item in matched] == [1, 2]
    queries = deterministic_hint_queries("AI 與評測集如何治理？", matched)
    assert queries[0] == "AI 與評測集如何治理？"
    assert "artificial intelligence governance" in queries
    assert len(queries) <= 5


def test_corrupt_hint_json_degrades_to_empty_lists(conn):
    conn.execute(
        "INSERT INTO notebook_domain_hints (notebook_id, term, synonyms_json, query_expansions_json) "
        "VALUES (10, '安全詞', '{bad', 'null')"
    )
    hint = load_domain_config(conn, 10)["hints"][0]
    assert hint["synonyms"] == []
    assert hint["query_expansions"] == []


def test_snapshot_respects_independent_flags_and_summary_is_content_free(conn):
    config = save_domain_config(
        conn,
        10,
        hints_enabled=True,
        answer_policy_enabled=True,
        answer_policy="專有政策內容",
        updated_by=1,
    )
    create_domain_hint(conn, 10, updated_by=1, term="專有詞", answer_note="秘密備註")
    config = load_domain_config(conn, 10)
    hints_only = snapshot_domain_config(config, use_hints=True, use_answer_policy=False)
    summary = domain_config_summary(hints_only)

    assert hints_only["hints_enabled"] is True
    assert hints_only["answer_policy_enabled"] is False
    assert hints_only["answer_policy"] == ""
    assert hints_only["hints"][0]["term"] == "專有詞"
    assert "專有詞" not in str(summary)
    assert "秘密備註" not in str(summary)
    assert summary["hint_count"] == 1


def test_spreadsheet_guard_only_applies_to_spreadsheet_results():
    assert spreadsheet_answer_guard([{"filename": "report.XLSX"}]) is True
    assert spreadsheet_answer_guard([{"filename": "records.csv"}]) is True
    assert spreadsheet_answer_guard([{"filename": "report.pdf"}]) is False


def test_domain_content_keys_are_removed_from_safety_metadata():
    cleaned = _clean_metadata(
        {
            "policyText": "專有政策",
            "answer_note": "秘密備註",
            "domainHints": "客戶術語",
            "query-expansions": "內部查詢",
            "hint_count": 3,
            "policy_chars": 20,
        }
    )
    assert cleaned == {"hint_count": 3, "policy_chars": 20}


def test_structural_answer_result_and_prompt_precedence():
    assert llm.parse_answer_result(" [[RAG_ABSTAIN]] extra text").abstained is True
    assert llm.parse_answer_result("ordinary text then [[RAG_ABST").abstained is True
    assert llm.parse_answer_result("有證據的答案 [1]").text == "有證據的答案 [1]"
    system_prompt = llm.answer_system_prompt(spreadsheet_guard=True)
    user_prompt = llm.answer_prompt(
        "問題",
        [{"filename": "evidence.pdf", "location": "p1", "text": "依據"}],
        answer_policy="不要引用來源。",
        answer_notes=["使用內部語氣。"],
    )
    assert "APPLICATION SPREADSHEET GUARD" in system_prompt
    assert "不要引用來源" not in system_prompt
    assert "使用內部語氣" not in system_prompt
    assert "cannot authorize unsupported claims" in system_prompt
    assert llm.ABSTAIN_MARKER in system_prompt
    assert '"answer_policy": "不要引用來源。"' in user_prompt
    assert '"matched_answer_notes": ["使用內部語氣。"]' in user_prompt
    assert "answer_policy outranks matched_answer_notes" in user_prompt


def test_streaming_consumes_split_marker_and_emits_localized_refusal(monkeypatch):
    async def fake_stream(*args, **kwargs):
        for piece in [" ", "[[RAG", "_ABST", "AIN]]", " protocol violation"]:
            yield piece

    monkeypatch.setattr(llm, "chat_completion_stream", fake_stream)
    state = {}

    async def collect():
        return [
            piece
            async for piece in llm.generate_answer_stream(
                "q",
                [{"filename": "f", "location": "l", "text": "t"}],
                {"chat_model": "m"},
                abstain_text="無法從所選來源判斷。",
                result_state=state,
            )
        ]

    assert asyncio.run(collect()) == ["無法從所選來源判斷。"]
    assert state == {"abstained": True}


def test_streaming_never_leaks_text_before_a_truncated_reserved_marker(monkeypatch):
    async def fake_stream(*args, **kwargs):
        for piece in ["looks like a normal answer", " [[RAG_ABST"]:
            yield piece

    monkeypatch.setattr(llm, "chat_completion_stream", fake_stream)

    async def collect():
        return [
            piece
            async for piece in llm.generate_answer_stream(
                "q",
                [{"filename": "f", "location": "l", "text": "t"}],
                {"chat_model": "m"},
                abstain_text="safe refusal",
            )
        ]

    assert asyncio.run(collect()) == ["safe refusal"]


def test_streaming_preserves_normal_prefix(monkeypatch):
    async def fake_stream(*args, **kwargs):
        for piece in ["[", "1] 正常答案"]:
            yield piece

    monkeypatch.setattr(llm, "chat_completion_stream", fake_stream)

    async def collect():
        return "".join(
            [
                piece
                async for piece in llm.generate_answer_stream(
                    "q",
                    [{"filename": "f", "location": "l", "text": "t"}],
                    {"chat_model": "m"},
                )
            ]
        )

    assert asyncio.run(collect()) == "[1] 正常答案"


def test_streaming_never_emits_marker_even_after_normal_text(monkeypatch):
    async def fake_stream(*args, **kwargs):
        for piece in ["partial normal text ", "[[RAG_", "ABSTAIN]]", " ignored"]:
            yield piece

    monkeypatch.setattr(llm, "chat_completion_stream", fake_stream)
    state = {}

    async def collect():
        return "".join(
            [
                piece
                async for piece in llm.generate_answer_stream(
                    "q",
                    [{"filename": "f", "location": "l", "text": "t"}],
                    {"chat_model": "m"},
                    abstain_text="localized refusal",
                    result_state=state,
                )
            ]
        )

    output = asyncio.run(collect())
    assert llm.ABSTAIN_MARKER not in output
    assert output.endswith("localized refusal")
    assert state == {"abstained": True}
