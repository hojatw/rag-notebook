"""E3a answer feedback: vocabulary, validation, and persistence helpers.

Why this module exists separately from the routes: the rating and reason
vocabularies are a **writer/reader contract** between the chat template, the
submit route, the admin page, and (later, `E3c`) the eval-item conversion. The
project has been bitten before by two sides of such a contract drifting apart
because each wrote its own string literal (see AGENTS.md, *Writing tests that
actually hold*), so both vocabularies live here as the single source and every
side imports them.

What feedback is for — and what it is not — is recorded in `ROADMAP.md` E3:
it identifies *which stage of the pipeline* is failing and supplies eval items.
It is **not** evidence that a parameter change helped; those comparisons run on
a fixed eval set through the Eval Workbench.
"""
from __future__ import annotations

import sqlite3
from json import dumps, loads
from typing import Any

from .config import config

#: Ratings, ordered best → worst. The vocabulary is defined by what the user
#: would *do* with the answer rather than how they felt about it: a neutral
#: "fine" collects clicks that carry no signal.
RATING_USABLE = "usable"
RATING_PARTIAL = "partial"
RATING_UNUSABLE = "unusable"
RATINGS: tuple[str, ...] = (RATING_USABLE, RATING_PARTIAL, RATING_UNUSABLE)

#: Reason tags. Each maps onto the pipeline stage that failed, which is what
#: makes a rating diagnosable; the mapping to `E1e-2`'s judge dimensions is in
#: `ROADMAP.md` E3. `REASON_OTHER` is the only one that carries free text.
REASON_RETRIEVAL = "retrieval"
REASON_GENERATION = "generation"
REASON_CITATION = "citation"
REASON_GROUNDING = "grounding"
REASON_OVER_ABSTAIN = "over_abstain"
REASON_NON_QUALITY = "non_quality"
REASON_OTHER = "other"
REASONS: tuple[str, ...] = (
    REASON_RETRIEVAL,
    REASON_GENERATION,
    REASON_CITATION,
    REASON_GROUNDING,
    REASON_OVER_ABSTAIN,
    REASON_NON_QUALITY,
    REASON_OTHER,
)

#: i18n keys for each vocabulary value, so a template never hardcodes copy.
RATING_LABEL_KEYS = {value: f"feedback.rating_{value}" for value in RATINGS}
REASON_LABEL_KEYS = {value: f"feedback.reason_{value}" for value in REASONS}


def normalize_rating(raw: str | None) -> str | None:
    """Return the rating if it is in the vocabulary, else ``None``."""
    value = (raw or "").strip()
    return value if value in RATINGS else None


def normalize_reasons(raw: list[str] | None, rating: str = "") -> list[str]:
    """Keep known reasons, drop unknown ones, de-duplicate, preserve order.

    A `usable` rating carries **no** reasons, whatever the form posted. The
    rating buttons and the reason checkboxes share one form, so a user who
    ticks reasons and then changes their mind to "可以直接採用" submits both —
    and the row would record a problem the user just said did not exist. The
    rule belongs here rather than in the template: it is what the vocabulary
    means, not how one page happens to be laid out.
    """
    if rating == RATING_USABLE:
        return []
    seen: list[str] = []
    for item in raw or []:
        value = (item or "").strip()
        if value in REASONS and value not in seen:
            seen.append(value)
    return seen


def normalize_other_reason(raw: str | None, reasons: list[str]) -> str:
    """Bounded free text, kept only when the user actually picked "other".

    Collapsing whitespace matters for the admin list: a pasted paragraph would
    otherwise stretch a table row even within the character cap.
    """
    if REASON_OTHER not in reasons:
        return ""
    text = " ".join((raw or "").split())
    return text[: max(0, int(config.feedback.other_reason_max_chars))]


def freeze_context(message_metadata: dict[str, Any], active_params: dict[str, Any], *, chat_model: str = "") -> dict[str, Any]:
    """Snapshot what produced the answer, for later attribution.

    The active retrieval profile can be replaced by an admin at any time
    (`ACTIVE_RETRIEVAL_PARAMS` in `app/retrieval.py`), so storing a profile id
    would not survive: a feedback row read six months later has to say what the
    parameters *were*. `eval_runs` freezes its snapshots for the same reason.

    Only identifiers, flags and numbers are copied — never the question, the
    answer, or retrieved text.
    """
    metadata = message_metadata or {}
    return {
        "retrieval_params": dict(active_params or {}),
        "chat_model": (chat_model or "")[:120],
        "outcome": str(metadata.get("outcome", ""))[:40],
        "domain_hints_enabled": bool(metadata.get("domain_hints_enabled", False)),
        "answer_policy_enabled": bool(metadata.get("answer_policy_enabled", False)),
        "retrieved_chunks": metadata.get("retrieved_chunks"),
        "top_score": metadata.get("top_score"),
    }


def save_feedback(
    conn: sqlite3.Connection,
    *,
    message_id: int,
    user_id: int,
    notebook_id: int,
    conversation_id: int,
    rating: str,
    reasons: list[str],
    other_reason: str,
    context: dict[str, Any],
) -> None:
    """Insert or replace this user's feedback for one message.

    A user changing their mind must not create a second row — the admin page
    counts rows, so duplicates would silently inflate every total. The UNIQUE
    (message_id, user_id) constraint plus this upsert is what holds that.
    """
    conn.execute(
        """
        INSERT INTO answer_feedback
            (message_id, user_id, notebook_id, conversation_id,
             rating, reasons_json, other_reason, context_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_id, user_id) DO UPDATE SET
            rating = excluded.rating,
            reasons_json = excluded.reasons_json,
            other_reason = excluded.other_reason,
            context_json = excluded.context_json,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            message_id,
            user_id,
            notebook_id,
            conversation_id,
            rating,
            dumps(reasons),
            other_reason,
            dumps(context),
        ),
    )


def get_feedback(conn: sqlite3.Connection, message_id: int, user_id: int) -> dict[str, Any] | None:
    """This user's own feedback for one message, decoded for rendering."""
    row = conn.execute(
        "SELECT * FROM answer_feedback WHERE message_id = ? AND user_id = ?",
        (message_id, user_id),
    ).fetchone()
    return _decode(row) if row else None


def feedback_for_messages(conn: sqlite3.Connection, message_ids: list[int], user_id: int) -> dict[int, dict[str, Any]]:
    """Feedback this user already gave, keyed by message id (one query)."""
    if not message_ids:
        return {}
    placeholders = ",".join("?" for _ in message_ids)
    rows = conn.execute(
        f"SELECT * FROM answer_feedback WHERE user_id = ? AND message_id IN ({placeholders})",
        (user_id, *message_ids),
    ).fetchall()
    return {row["message_id"]: _decode(row) for row in rows}


def admin_list(
    conn: sqlite3.Connection,
    *,
    rating: str = "",
    reason: str = "",
    notebook_id: int | None = None,
    since: str = "",
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Feedback across all users, newest first, for the admin review page.

    Admin-only by route. This is the one surface that shows other users' words
    (the free-text "other" reason), which is why reading it is audited.
    """
    where: list[str] = []
    params: list[Any] = []
    if rating in RATINGS:
        where.append("f.rating = ?")
        params.append(rating)
    if reason in REASONS:
        # reasons_json is a JSON array of short tokens; EXISTS over json_each
        # keeps the match exact instead of a LIKE that would also hit
        # "over_abstain" when filtering "abstain".
        where.append("EXISTS (SELECT 1 FROM json_each(f.reasons_json) WHERE json_each.value = ?)")
        params.append(reason)
    if notebook_id:
        where.append("f.notebook_id = ?")
        params.append(int(notebook_id))
    if since:
        where.append("f.created_at >= ?")
        params.append(since)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"""
        SELECT f.*, u.username AS username, n.title AS notebook_title,
               m.content AS answer_content, m.created_at AS answered_at
        FROM answer_feedback f
        JOIN users u ON u.id = f.user_id
        JOIN notebooks n ON n.id = f.notebook_id
        JOIN messages m ON m.id = f.message_id
        {where_sql}
        ORDER BY f.created_at DESC, f.id DESC
        LIMIT ?
        """,
        (*params, int(limit)),
    ).fetchall()
    return [_decode(row) for row in rows]


def admin_counts(conn: sqlite3.Connection) -> dict[str, Any]:
    """Totals by rating and by reason — the whole point of phase E3a.

    Deliberately not a time series: "bad ratings went down this week" is an
    uncontrolled comparison (different questions, different users) and must not
    be read as a quality trend. See `ROADMAP.md` E3.
    """
    by_rating = {value: 0 for value in RATINGS}
    for row in conn.execute("SELECT rating, COUNT(*) AS n FROM answer_feedback GROUP BY rating").fetchall():
        if row["rating"] in by_rating:
            by_rating[row["rating"]] = row["n"]
    by_reason = {value: 0 for value in REASONS}
    for row in conn.execute(
        """
        SELECT json_each.value AS reason, COUNT(*) AS n
        FROM answer_feedback, json_each(answer_feedback.reasons_json)
        GROUP BY json_each.value
        """
    ).fetchall():
        if row["reason"] in by_reason:
            by_reason[row["reason"]] = row["n"]
    total = sum(by_rating.values())
    return {"total": total, "by_rating": by_rating, "by_reason": by_reason}


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["reasons"] = loads(data.get("reasons_json") or "[]")
    except ValueError:
        data["reasons"] = []
    try:
        data["context"] = loads(data.get("context_json") or "{}")
    except ValueError:
        data["context"] = {}
    return data
