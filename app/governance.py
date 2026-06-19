import logging
import re
from typing import Any

from .db import connect, dumps


logger = logging.getLogger(__name__)

USAGE_CONTEXT_KEYS = {
    "user_id",
    "notebook_id",
    "conversation_id",
    "message_id",
    "source_id",
    "eval_run_id",
    "eval_set_id",
}
SENSITIVE_METADATA_KEYS = {
    "prompt",
    "user_prompt",
    "system_prompt",
    "source_text",
    "source_content",
    "retrieved_text",
    "snippet",
    "answer",
    "output",
    "content",
    "api_key",
    "secret",
}


def estimate_tokens(chars: int) -> int:
    """Cheap token estimate for providers that omit usage details."""
    chars = max(0, int(chars or 0))
    return (chars + 3) // 4 if chars else 0


def normalize_usage(
    usage: dict[str, Any] | None,
    *,
    input_chars: int,
    output_chars: int = 0,
) -> dict[str, Any]:
    """Normalize provider usage, falling back to explicit estimates.

    OpenAI-compatible and Azure OpenAI responses commonly use
    prompt_tokens/completion_tokens/total_tokens. Some gateways use
    input_tokens/output_tokens. The returned shape is stable for reporting and
    marks estimates so governance reports do not imply billing precision.
    """
    input_chars = max(0, int(input_chars or 0))
    output_chars = max(0, int(output_chars or 0))
    usage = usage if isinstance(usage, dict) else {}

    prompt_tokens = _usage_int(
        usage,
        (
            "prompt_tokens",
            "input_tokens",
            "promptTokens",
            "inputTokens",
            "promptTokenCount",
            "inputTokenCount",
            "prompt_token_count",
            "input_token_count",
            "input",
            "prompt",
        ),
    )
    completion_tokens = _usage_int(
        usage,
        (
            "completion_tokens",
            "output_tokens",
            "completionTokens",
            "outputTokens",
            "candidatesTokenCount",
            "outputTokenCount",
            "completion_token_count",
            "output_token_count",
            "output",
            "completion",
        ),
    )
    total_tokens = _usage_int(
        usage,
        (
            "total_tokens",
            "totalTokens",
            "totalTokenCount",
            "total_token_count",
            "total",
        ),
    )

    if prompt_tokens is None and completion_tokens is None and total_tokens is None:
        prompt_tokens = estimate_tokens(input_chars)
        completion_tokens = estimate_tokens(output_chars)
        total_tokens = prompt_tokens + completion_tokens
        is_estimated = 1
    else:
        prompt_tokens = prompt_tokens if prompt_tokens is not None else estimate_tokens(input_chars)
        completion_tokens = completion_tokens if completion_tokens is not None else estimate_tokens(output_chars)
        total_tokens = total_tokens if total_tokens is not None else prompt_tokens + completion_tokens
        is_estimated = 0

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "input_chars": input_chars,
        "output_chars": output_chars,
        "is_estimated": is_estimated,
    }


def record_llm_usage_event(
    *,
    call_type: str,
    provider: str,
    model: str,
    status: str,
    latency_ms: float,
    usage: dict[str, Any],
    context: dict[str, Any] | None = None,
    error_class: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Persist one AI usage event without risking the user-facing flow."""
    context = _clean_context(context or {})
    metadata = _clean_metadata(metadata or {})
    try:
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO llm_usage_events (
                    user_id, notebook_id, conversation_id, message_id, source_id,
                    eval_run_id, eval_set_id, call_type, provider, model, status,
                    latency_ms, prompt_tokens, completion_tokens, total_tokens,
                    input_chars, output_chars, is_estimated, error_class,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    context.get("user_id"),
                    context.get("notebook_id"),
                    context.get("conversation_id"),
                    context.get("message_id"),
                    context.get("source_id"),
                    context.get("eval_run_id"),
                    context.get("eval_set_id"),
                    str(call_type or "unknown")[:80],
                    str(provider or "")[:80],
                    str(model or "")[:160],
                    str(status or "unknown")[:40],
                    float(latency_ms or 0),
                    usage.get("prompt_tokens"),
                    usage.get("completion_tokens"),
                    usage.get("total_tokens"),
                    int(usage.get("input_chars") or 0),
                    int(usage.get("output_chars") or 0),
                    int(usage.get("is_estimated") or 0),
                    str(error_class or "")[:120],
                    dumps(metadata),
                ),
            )
    except Exception:
        logger.warning("llm_usage_event_record_failed call_type=%s status=%s", call_type, status, exc_info=True)


def _optional_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _usage_int(usage: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = _optional_int(usage.get(key))
        if value is not None:
            return value
    for nested_key in ("usage", "token_usage", "tokens"):
        nested = usage.get(nested_key)
        if not isinstance(nested, dict):
            continue
        for key in keys:
            value = _optional_int(nested.get(key))
            if value is not None:
                return value
    return None


def _clean_context(context: dict[str, Any]) -> dict[str, int]:
    cleaned: dict[str, int] = {}
    for key in USAGE_CONTEXT_KEYS:
        value = _optional_int(context.get(key))
        if value is not None:
            cleaned[key] = value
    return cleaned


def _clean_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in metadata.items():
        safe_key = str(key)[:80]
        if _metadata_key_is_sensitive(str(key)):
            continue
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            cleaned[safe_key] = value if not isinstance(value, str) else value[:300]
    return cleaned


def _metadata_key_is_sensitive(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")
    compact = normalized.replace("_", "")
    compact_sensitive_keys = {sensitive.replace("_", "") for sensitive in SENSITIVE_METADATA_KEYS}
    return normalized in SENSITIVE_METADATA_KEYS or any(sensitive in compact for sensitive in compact_sensitive_keys)
