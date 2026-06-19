import hashlib
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
SAFETY_DETECTOR_VERSION = "local.rules.v1"
SAFETY_MAX_INPUT_CHARS = 12000
SAFETY_BLOCK_CANDIDATE_CHARS = 24000
CONTROL_TEXT_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b\u200c\u200d\ufeff]")
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("generic_api_key_assignment", re.compile(r"(?i)\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[^'\"\s]{12,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
)
PROMPT_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ignore_previous_instructions", re.compile(r"(?i)\bignore (?:all )?(?:previous|prior|above) instructions\b")),
    ("reveal_system_prompt", re.compile(r"(?i)\b(?:reveal|show|print|dump|repeat).{0,40}\b(?:system prompt|developer message|hidden instructions)\b")),
    ("bypass_rules", re.compile(r"(?i)\b(?:bypass|override|disable).{0,30}\b(?:rules|guardrails|safety|policy)\b")),
)


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


def scan_ai_safety(
    text: str | None,
    *,
    event_type: str,
    surface: str,
) -> list[dict[str, Any]]:
    """Run local G1c scanner rules and return compact, non-sensitive findings."""
    text = text or ""
    findings: list[dict[str, Any]] = []
    text_len = len(text)
    content_hash = _content_hash(text)

    if text_len > SAFETY_MAX_INPUT_CHARS:
        findings.append({
            "event_type": event_type,
            "surface": surface,
            "category": "input_length",
            "severity": "high" if text_len > SAFETY_BLOCK_CANDIDATE_CHARS else "medium",
            "decision": "block_candidate" if text_len > SAFETY_BLOCK_CANDIDATE_CHARS else "warn",
            "rule_id": "input_length.max_chars",
            "content_hash": content_hash,
            "redacted_summary": f"Input length {text_len} chars exceeds {SAFETY_MAX_INPUT_CHARS}.",
            "metadata": {"input_chars": text_len, "max_chars": SAFETY_MAX_INPUT_CHARS},
        })

    control_matches = CONTROL_TEXT_RE.findall(text)
    if control_matches:
        findings.append({
            "event_type": event_type,
            "surface": surface,
            "category": "invisible_or_control_text",
            "severity": "medium",
            "decision": "warn",
            "rule_id": "input_text.control_or_invisible_chars",
            "content_hash": content_hash,
            "redacted_summary": f"Input contains {len(control_matches)} control or invisible characters.",
            "metadata": {"match_count": len(control_matches)},
        })

    for rule_id, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            findings.append({
                "event_type": event_type,
                "surface": surface,
                "category": "secret_or_credential",
                "severity": "high",
                "decision": "warn",
                "rule_id": f"secret.{rule_id}",
                "content_hash": content_hash,
                "redacted_summary": "Input contains a possible credential or secret pattern.",
                "metadata": {"pattern": rule_id},
            })

    for rule_id, pattern in PROMPT_INJECTION_PATTERNS:
        if pattern.search(text):
            findings.append({
                "event_type": event_type,
                "surface": surface,
                "category": "prompt_injection",
                "severity": "medium",
                "decision": "warn",
                "rule_id": f"prompt_injection.{rule_id}",
                "content_hash": content_hash,
                "redacted_summary": "Input contains a prompt-injection style instruction.",
                "metadata": {"pattern": rule_id},
            })

    return findings


def record_ai_safety_events(
    *,
    text: str | None,
    event_type: str,
    surface: str,
    context: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Scan and persist local safety findings without blocking the caller."""
    findings = scan_ai_safety(text, event_type=event_type, surface=surface)
    if not findings:
        return []
    context = _clean_context(context or {})
    base_metadata = _clean_metadata(metadata or {})
    try:
        with connect() as conn:
            for finding in findings:
                finding_metadata = _clean_metadata({**base_metadata, **(finding.get("metadata") or {})})
                conn.execute(
                    """
                    INSERT INTO ai_safety_events (
                        user_id, notebook_id, conversation_id, message_id, source_id,
                        eval_run_id, eval_set_id, event_type, surface, category,
                        severity, decision, detector_version, rule_id, content_hash,
                        redacted_summary, metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        context.get("user_id"),
                        context.get("notebook_id"),
                        context.get("conversation_id"),
                        context.get("message_id"),
                        context.get("source_id"),
                        context.get("eval_run_id"),
                        context.get("eval_set_id"),
                        str(finding.get("event_type") or event_type)[:80],
                        str(finding.get("surface") or surface)[:120],
                        str(finding.get("category") or "unknown")[:80],
                        str(finding.get("severity") or "unknown")[:40],
                        str(finding.get("decision") or "warn")[:40],
                        SAFETY_DETECTOR_VERSION,
                        str(finding.get("rule_id") or "")[:120],
                        str(finding.get("content_hash") or "")[:80],
                        str(finding.get("redacted_summary") or "")[:300],
                        dumps(finding_metadata),
                    ),
                )
    except Exception:
        logger.warning("ai_safety_event_record_failed event_type=%s surface=%s", event_type, surface, exc_info=True)
    return findings


def _optional_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _content_hash(text: str) -> str:
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


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
