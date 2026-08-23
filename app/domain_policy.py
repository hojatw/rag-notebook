"""Notebook-scoped domain hints and answer-policy helpers for E2.

Domain configuration is owner-authored, untrusted operational guidance. It may
shape query generation and answer presentation, but it is never evidence and
never changes the persisted vector index.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
import uuid
from typing import Any, Iterable

from .config import config
from .db import dumps
from .governance import count_cjk_chars, estimate_tokens


SNAPSHOT_SCHEMA_VERSION = 1
PROMPT_VERSION = "notebook-domain-policy.v1"
MAX_SNAPSHOT_JSON_CHARS = 160_000
OPAQUE_VERSION_TOKEN_RE = re.compile(r"[0-9a-f]{32}")
SNAPSHOT_LIMIT_MAXIMA = {
    "max_hints": 100,
    "max_term_chars": 500,
    "max_synonyms": 40,
    "max_synonym_chars": 500,
    "max_definition_chars": 3_000,
    "max_query_expansions": 20,
    "max_query_expansion_chars": 1_000,
    "max_answer_note_chars": 3_000,
    "max_policy_chars": 10_000,
    "max_policy_tokens": 8_000,
    "max_matched_hints": 40,
    "max_hint_tokens": 8_000,
    "max_rewrite_queries": 20,
}


class DomainPolicyValidationError(ValueError):
    """A bounded domain-config field failed validation."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def limits() -> dict[str, int]:
    cfg = config.domain_policy
    return {
        "max_hints": cfg.max_hints,
        "max_term_chars": cfg.max_term_chars,
        "max_synonyms": cfg.max_synonyms,
        "max_synonym_chars": cfg.max_synonym_chars,
        "max_definition_chars": cfg.max_definition_chars,
        "max_query_expansions": cfg.max_query_expansions,
        "max_query_expansion_chars": cfg.max_query_expansion_chars,
        "max_answer_note_chars": cfg.max_answer_note_chars,
        "max_policy_chars": cfg.max_policy_chars,
        "max_policy_tokens": cfg.max_policy_tokens,
        "max_matched_hints": cfg.max_matched_hints,
        "max_hint_tokens": cfg.max_hint_tokens,
        "max_rewrite_queries": cfg.max_rewrite_queries,
    }


def parse_lines(
    value: str | Iterable[str] | None,
    *,
    max_items: int | None = None,
    max_item_chars: int | None = None,
    too_many_code: str = "too_many_items",
    item_too_long_code: str = "item_too_long",
) -> list[str]:
    """Normalize a newline-separated form field into unique non-empty values."""
    raw_values: Iterable[Any]
    if isinstance(value, (list, tuple)):
        raw_values = value
    else:
        # StringIO iterates incrementally, so an oversized single form field is
        # rejected without first materialising every line into a second list.
        raw_values = io.StringIO(str(value or ""))
    result: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        item = str(raw or "").strip()
        key = item.casefold()
        if not item or key in seen:
            continue
        if max_item_chars is not None and len(item) > max_item_chars:
            raise DomainPolicyValidationError(item_too_long_code)
        seen.add(key)
        result.append(item)
        if max_items is not None and len(result) > max_items:
            raise DomainPolicyValidationError(too_many_code)
    return result


def validate_answer_policy(
    value: str | None,
    runtime_limits: dict[str, int] | None = None,
) -> str:
    policy = str(value or "").strip()
    if len(policy) > _runtime_limit(runtime_limits, "max_policy_chars"):
        raise DomainPolicyValidationError("policy_too_long")
    if _token_estimate(policy) > _runtime_limit(runtime_limits, "max_policy_tokens"):
        raise DomainPolicyValidationError("policy_token_budget")
    return policy


def validate_hint(
    *,
    term: str | None,
    synonyms: str | Iterable[str] | None = None,
    definition: str | None = None,
    query_expansions: str | Iterable[str] | None = None,
    answer_note: str | None = None,
    enabled: bool = True,
    runtime_limits: dict[str, int] | None = None,
) -> dict[str, Any]:
    normalized_term = str(term or "").strip()
    normalized_definition = str(definition or "").strip()
    normalized_note = str(answer_note or "").strip()
    normalized_synonyms = parse_lines(
        synonyms,
        max_items=_runtime_limit(runtime_limits, "max_synonyms"),
        max_item_chars=_runtime_limit(runtime_limits, "max_synonym_chars"),
        too_many_code="too_many_synonyms",
        item_too_long_code="synonym_too_long",
    )
    normalized_expansions = parse_lines(
        query_expansions,
        max_items=_runtime_limit(runtime_limits, "max_query_expansions"),
        max_item_chars=_runtime_limit(runtime_limits, "max_query_expansion_chars"),
        too_many_code="too_many_query_expansions",
        item_too_long_code="query_expansion_too_long",
    )

    if not normalized_term:
        raise DomainPolicyValidationError("term_required")
    if len(normalized_term) > _runtime_limit(runtime_limits, "max_term_chars"):
        raise DomainPolicyValidationError("term_too_long")
    if len(normalized_definition) > _runtime_limit(runtime_limits, "max_definition_chars"):
        raise DomainPolicyValidationError("definition_too_long")
    if len(normalized_note) > _runtime_limit(runtime_limits, "max_answer_note_chars"):
        raise DomainPolicyValidationError("answer_note_too_long")

    return {
        "term": normalized_term,
        "synonyms": normalized_synonyms,
        "definition": normalized_definition,
        "query_expansions": normalized_expansions,
        "answer_note": normalized_note,
        "enabled": bool(enabled),
    }


def empty_domain_config(notebook_id: int) -> dict[str, Any]:
    return {
        "notebook_id": int(notebook_id),
        "hints_enabled": False,
        "answer_policy_enabled": False,
        "answer_policy": "",
        "revision": 0,
        "version_token": "",
        "updated_by": None,
        "hints": [],
    }


def load_domain_config(conn: sqlite3.Connection, notebook_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM notebook_domain_config WHERE notebook_id = ?",
        (notebook_id,),
    ).fetchone()
    result = empty_domain_config(notebook_id)
    if row is not None:
        result.update(dict(row))
        result["hints_enabled"] = bool(result.get("hints_enabled"))
        result["answer_policy_enabled"] = bool(result.get("answer_policy_enabled"))

    hint_rows = conn.execute(
        "SELECT * FROM notebook_domain_hints WHERE notebook_id = ? ORDER BY id",
        (notebook_id,),
    ).fetchall()
    result["hints"] = [_hint_from_row(hint_row) for hint_row in hint_rows]
    return result


def save_domain_config(
    conn: sqlite3.Connection,
    notebook_id: int,
    *,
    hints_enabled: bool,
    answer_policy_enabled: bool,
    answer_policy: str | None,
    updated_by: int,
) -> dict[str, Any]:
    _begin_immediate_if_needed(conn)
    policy = validate_answer_policy(answer_policy)
    revision, version_token = _next_revision(conn, notebook_id)
    conn.execute(
        """
        INSERT INTO notebook_domain_config (
            notebook_id, hints_enabled, answer_policy_enabled, answer_policy,
            revision, version_token, updated_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(notebook_id) DO UPDATE SET
            hints_enabled = excluded.hints_enabled,
            answer_policy_enabled = excluded.answer_policy_enabled,
            answer_policy = excluded.answer_policy,
            revision = excluded.revision,
            version_token = excluded.version_token,
            updated_by = excluded.updated_by,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            notebook_id,
            int(bool(hints_enabled)),
            int(bool(answer_policy_enabled)),
            policy,
            revision,
            version_token,
            updated_by,
        ),
    )
    return load_domain_config(conn, notebook_id)


def create_domain_hint(
    conn: sqlite3.Connection,
    notebook_id: int,
    *,
    updated_by: int,
    **values: Any,
) -> dict[str, Any]:
    _begin_immediate_if_needed(conn)
    current_count = conn.execute(
        "SELECT COUNT(*) FROM notebook_domain_hints WHERE notebook_id = ?",
        (notebook_id,),
    ).fetchone()[0]
    if current_count >= config.domain_policy.max_hints:
        raise DomainPolicyValidationError("too_many_hints")
    hint = validate_hint(**values)
    _assert_unique_term(conn, notebook_id, hint["term"])
    try:
        cursor = conn.execute(
            """
            INSERT INTO notebook_domain_hints (
                notebook_id, term, synonyms_json, definition,
                query_expansions_json, answer_note, enabled
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                notebook_id,
                hint["term"],
                dumps(hint["synonyms"]),
                hint["definition"],
                dumps(hint["query_expansions"]),
                hint["answer_note"],
                int(hint["enabled"]),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise DomainPolicyValidationError("duplicate_term") from exc
    _touch_config(conn, notebook_id, updated_by)
    return _load_hint(conn, notebook_id, cursor.lastrowid)


def update_domain_hint(
    conn: sqlite3.Connection,
    notebook_id: int,
    hint_id: int,
    *,
    updated_by: int,
    **values: Any,
) -> dict[str, Any]:
    _begin_immediate_if_needed(conn)
    _load_hint(conn, notebook_id, hint_id)
    hint = validate_hint(**values)
    _assert_unique_term(conn, notebook_id, hint["term"], exclude_id=hint_id)
    try:
        conn.execute(
            """
            UPDATE notebook_domain_hints
            SET term = ?, synonyms_json = ?, definition = ?,
                query_expansions_json = ?, answer_note = ?, enabled = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND notebook_id = ?
            """,
            (
                hint["term"],
                dumps(hint["synonyms"]),
                hint["definition"],
                dumps(hint["query_expansions"]),
                hint["answer_note"],
                int(hint["enabled"]),
                hint_id,
                notebook_id,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise DomainPolicyValidationError("duplicate_term") from exc
    _touch_config(conn, notebook_id, updated_by)
    return _load_hint(conn, notebook_id, hint_id)


def delete_domain_hint(
    conn: sqlite3.Connection,
    notebook_id: int,
    hint_id: int,
    *,
    updated_by: int,
) -> None:
    _begin_immediate_if_needed(conn)
    cursor = conn.execute(
        "DELETE FROM notebook_domain_hints WHERE id = ? AND notebook_id = ?",
        (hint_id, notebook_id),
    )
    if cursor.rowcount != 1:
        raise KeyError(hint_id)
    _touch_config(conn, notebook_id, updated_by)


def snapshot_domain_config(
    domain_config: dict[str, Any],
    *,
    use_hints: bool,
    use_answer_policy: bool,
) -> dict[str, Any]:
    hints = [
        normalized
        for hint in domain_config.get("hints", [])
        if isinstance(hint, dict) and hint.get("enabled")
        for normalized in [_snapshot_hint(hint)]
        if normalized is not None
    ]
    effective_hints = bool(use_hints and domain_config.get("hints_enabled") and hints)
    try:
        policy = validate_answer_policy(domain_config.get("answer_policy"))
    except DomainPolicyValidationError:
        policy = ""
    effective_policy = bool(
        use_answer_policy
        and domain_config.get("answer_policy_enabled")
        and policy
    )
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "revision": int(domain_config.get("revision") or 0),
        "version_token": str(domain_config.get("version_token") or ""),
        "hints_enabled": effective_hints,
        "answer_policy_enabled": effective_policy,
        "hints": hints if effective_hints else [],
        "answer_policy": policy if effective_policy else "",
        "limits": limits(),
    }


def domain_config_summary(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    value = snapshot if isinstance(snapshot, dict) else {}
    hints = value.get("hints") if isinstance(value.get("hints"), list) else []
    enabled_hints = [hint for hint in hints if isinstance(hint, dict) and hint.get("enabled", True)]
    policy = str(value.get("answer_policy") or "")
    raw_revision = value.get("revision")
    revision = raw_revision if isinstance(raw_revision, int) and not isinstance(raw_revision, bool) and raw_revision >= 0 else 0
    token = _safe_version_token(value.get("version_token"), revision=revision)
    return {
        "snapshot_available": bool(value),
        "revision": revision,
        # Sanitized summaries and audit metadata never expose the raw opaque
        # token. The fingerprint is sufficient for change comparison.
        "version_fingerprint": hashlib.sha256(token.encode("ascii")).hexdigest()[:12] if token else "",
        "hints_enabled": bool(value.get("hints_enabled")),
        "answer_policy_enabled": bool(value.get("answer_policy_enabled")),
        "hint_count": len(hints),
        "enabled_hint_count": len(enabled_hints),
        "policy_present": bool(policy),
        "policy_chars": len(policy),
        "policy_tokens_est": _token_estimate(policy),
    }


def validate_domain_snapshot(snapshot: Any) -> dict[str, Any]:
    """Return a canonical, bounded E2 snapshot or raise fail-closed validation."""
    if not isinstance(snapshot, dict):
        raise DomainPolicyValidationError("snapshot_invalid")
    if snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise DomainPolicyValidationError("snapshot_invalid")
    if snapshot.get("prompt_version") != PROMPT_VERSION:
        raise DomainPolicyValidationError("snapshot_invalid")
    revision = snapshot.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise DomainPolicyValidationError("snapshot_invalid")
    version_token = _safe_version_token(snapshot.get("version_token"), revision=revision)
    if version_token != str(snapshot.get("version_token") or ""):
        raise DomainPolicyValidationError("snapshot_invalid")
    if type(snapshot.get("hints_enabled")) is not bool:
        raise DomainPolicyValidationError("snapshot_invalid")
    if type(snapshot.get("answer_policy_enabled")) is not bool:
        raise DomainPolicyValidationError("snapshot_invalid")
    raw_limits = snapshot.get("limits")
    if not isinstance(raw_limits, dict) or set(raw_limits) != set(limits()):
        raise DomainPolicyValidationError("snapshot_invalid")
    canonical_limits: dict[str, int] = {}
    for key in limits():
        value = raw_limits.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
            or value > SNAPSHOT_LIMIT_MAXIMA[key]
        ):
            raise DomainPolicyValidationError("snapshot_invalid")
        canonical_limits[key] = value
    raw_hints = snapshot.get("hints")
    if not isinstance(raw_hints, list) or len(raw_hints) > canonical_limits["max_hints"]:
        raise DomainPolicyValidationError("snapshot_invalid")
    canonical_hints: list[dict[str, Any]] = []
    for raw_hint in raw_hints:
        if not isinstance(raw_hint, dict) or type(raw_hint.get("enabled")) is not bool:
            raise DomainPolicyValidationError("snapshot_invalid")
        hint_id = raw_hint.get("id", 0)
        if isinstance(hint_id, bool) or not isinstance(hint_id, int) or hint_id < 0:
            raise DomainPolicyValidationError("snapshot_invalid")
        try:
            normalized = validate_hint(
                term=raw_hint.get("term"),
                synonyms=raw_hint.get("synonyms"),
                definition=raw_hint.get("definition"),
                query_expansions=raw_hint.get("query_expansions"),
                answer_note=raw_hint.get("answer_note"),
                enabled=raw_hint["enabled"],
                runtime_limits=canonical_limits,
            )
        except DomainPolicyValidationError as exc:
            raise DomainPolicyValidationError("snapshot_invalid") from exc
        canonical_hints.append({"id": hint_id, **normalized})
    try:
        policy = validate_answer_policy(snapshot.get("answer_policy"), canonical_limits)
    except DomainPolicyValidationError as exc:
        raise DomainPolicyValidationError("snapshot_invalid") from exc
    if not snapshot["hints_enabled"] and canonical_hints:
        raise DomainPolicyValidationError("snapshot_invalid")
    if not snapshot["answer_policy_enabled"] and policy:
        raise DomainPolicyValidationError("snapshot_invalid")
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "revision": revision,
        "version_token": version_token,
        "hints_enabled": snapshot["hints_enabled"],
        "answer_policy_enabled": snapshot["answer_policy_enabled"],
        "hints": canonical_hints,
        "answer_policy": policy,
        "limits": canonical_limits,
    }


def match_domain_hints(
    question: str,
    hints: Iterable[dict[str, Any]],
    runtime_limits: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Return bounded enabled hints whose term or synonym occurs in the query."""
    matched: list[dict[str, Any]] = []
    used_tokens = 0
    for hint in hints:
        if not isinstance(hint, dict) or not hint.get("enabled", True):
            continue
        needles = [str(hint.get("term") or ""), *_json_or_list(hint.get("synonyms"))]
        if not any(_contains_term(question, needle) for needle in needles if needle):
            continue
        hint_tokens = _token_estimate(_hint_budget_text(hint))
        if used_tokens + hint_tokens > _runtime_limit(runtime_limits, "max_hint_tokens"):
            continue
        matched.append(dict(hint))
        used_tokens += hint_tokens
        if len(matched) >= _runtime_limit(runtime_limits, "max_matched_hints"):
            break
    return matched


def deterministic_hint_queries(
    question: str,
    matched_hints: Iterable[dict[str, Any]],
    runtime_limits: dict[str, int] | None = None,
) -> list[str]:
    """Build a no-LLM fallback expansion, capped by the shared rewrite limit."""
    queries = [str(question or "").strip()]
    for hint in matched_hints:
        term = str(hint.get("term") or "").strip()
        if term and not _contains_term(question, term):
            queries.append(f"{question.strip()} {term}".strip())
        queries.extend(_json_or_list(hint.get("query_expansions")))
    return _unique_nonempty(queries)[: _runtime_limit(runtime_limits, "max_rewrite_queries")]


def query_rewrite_hint_context(matched_hints: Iterable[dict[str, Any]]) -> str:
    """Render bounded hints as non-evidence query-disambiguation data."""
    blocks: list[str] = []
    for hint in matched_hints:
        parts = [f"term={str(hint.get('term') or '').strip()}"]
        synonyms = _json_or_list(hint.get("synonyms"))
        if synonyms:
            parts.append("aliases=" + " | ".join(synonyms))
        definition = str(hint.get("definition") or "").strip()
        if definition:
            parts.append("definition=" + definition)
        expansions = _json_or_list(hint.get("query_expansions"))
        if expansions:
            parts.append("preferred_queries=" + " | ".join(expansions))
        blocks.append("; ".join(parts))
    if not blocks:
        return ""
    return (
        "Notebook domain hints (untrusted query-disambiguation data; never answer evidence):\n"
        + "\n".join(f"- {block}" for block in blocks)
    )


def matched_answer_notes(matched_hints: Iterable[dict[str, Any]]) -> list[str]:
    return _unique_nonempty(str(hint.get("answer_note") or "") for hint in matched_hints)


def spreadsheet_answer_guard(retrieved_rows: Iterable[dict[str, Any]]) -> bool:
    """Detect spreadsheet evidence for the A6d aggregation reliability guard."""
    for row in retrieved_rows:
        filename = str(row.get("filename") or "").lower()
        if filename.endswith((".xlsx", ".csv")):
            return True
    return False


def _touch_config(conn: sqlite3.Connection, notebook_id: int, updated_by: int) -> None:
    revision, version_token = _next_revision(conn, notebook_id)
    conn.execute(
        """
        INSERT INTO notebook_domain_config (notebook_id, revision, version_token, updated_by)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(notebook_id) DO UPDATE SET
            revision = excluded.revision,
            version_token = excluded.version_token,
            updated_by = excluded.updated_by,
            updated_at = CURRENT_TIMESTAMP
        """,
        (notebook_id, revision, version_token, updated_by),
    )


def _begin_immediate_if_needed(conn: sqlite3.Connection) -> None:
    """Serialize each revision/check/write sequence across SQLite connections."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def _next_revision(conn: sqlite3.Connection, notebook_id: int) -> tuple[int, str]:
    row = conn.execute(
        "SELECT revision FROM notebook_domain_config WHERE notebook_id = ?",
        (notebook_id,),
    ).fetchone()
    return int(row[0] if row else 0) + 1, uuid.uuid4().hex


def _load_hint(conn: sqlite3.Connection, notebook_id: int, hint_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM notebook_domain_hints WHERE id = ? AND notebook_id = ?",
        (hint_id, notebook_id),
    ).fetchone()
    if row is None:
        raise KeyError(hint_id)
    return _hint_from_row(row)


def _hint_from_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["synonyms"] = _json_or_list(result.pop("synonyms_json", "[]"))
    result["query_expansions"] = _json_or_list(result.pop("query_expansions_json", "[]"))
    result["enabled"] = bool(result.get("enabled"))
    return result


def _snapshot_hint(hint: dict[str, Any]) -> dict[str, Any] | None:
    """Project one DB row to the stable semantic snapshot schema."""
    try:
        normalized = validate_hint(
            term=hint.get("term"),
            synonyms=hint.get("synonyms"),
            definition=hint.get("definition"),
            query_expansions=hint.get("query_expansions"),
            answer_note=hint.get("answer_note"),
            enabled=bool(hint.get("enabled", True)),
        )
    except DomainPolicyValidationError:
        return None
    return {
        "id": int(hint.get("id") or 0),
        **normalized,
    }


def _json_or_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return parse_lines(value)
    if not isinstance(value, str):
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parse_lines(parsed) if isinstance(parsed, list) else []


def _safe_version_token(value: Any, *, revision: int) -> str:
    token = str(value or "")
    if revision == 0 and not token:
        return ""
    return token if OPAQUE_VERSION_TOKEN_RE.fullmatch(token) else ""


def _runtime_limit(runtime_limits: dict[str, int] | None, key: str) -> int:
    fallback = int(getattr(config.domain_policy, key))
    if not isinstance(runtime_limits, dict):
        return fallback
    value = runtime_limits.get(key)
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else fallback


def _assert_unique_term(
    conn: sqlite3.Connection,
    notebook_id: int,
    term: str,
    *,
    exclude_id: int | None = None,
) -> None:
    rows = conn.execute(
        "SELECT id, term FROM notebook_domain_hints WHERE notebook_id = ?",
        (notebook_id,),
    ).fetchall()
    if any(row["id"] != exclude_id and str(row["term"]).casefold() == term.casefold() for row in rows):
        raise DomainPolicyValidationError("duplicate_term")


def _contains_term(text: str, term: str) -> bool:
    haystack = str(text or "")
    needle = str(term or "").strip()
    if not needle:
        return False
    if count_cjk_chars(needle):
        return needle.casefold() in haystack.casefold()
    return bool(re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack, flags=re.IGNORECASE))


def _hint_budget_text(hint: dict[str, Any]) -> str:
    return "\n".join(
        [
            str(hint.get("term") or ""),
            *_json_or_list(hint.get("synonyms")),
            str(hint.get("definition") or ""),
            *_json_or_list(hint.get("query_expansions")),
            str(hint.get("answer_note") or ""),
        ]
    )


def _token_estimate(text: str) -> int:
    return estimate_tokens(len(text), cjk_chars=count_cjk_chars(text))


def _unique_nonempty(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value or "").strip()
        key = normalized.casefold()
        if normalized and key not in seen:
            seen.add(key)
            result.append(normalized)
    return result
