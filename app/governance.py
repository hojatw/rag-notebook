import hashlib
import logging
import math
import re
from typing import Any

from .config import config
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
    "policy",
    "answer_policy",
    "answer_note",
    "domain_hints",
    "definition",
    "synonyms",
    "query_expansions",
}
SAFE_AGGREGATE_METADATA_KEYS = {
    "hint_count",
    "enabled_hint_count",
    "matched_hint_count",
    "hint_payload_tokens_est",
    "policy_present",
    "policy_chars",
    "policy_tokens_est",
}
SAFETY_DETECTOR_VERSION = "local.rules.v3"
SAFETY_MAX_INPUT_CHARS = 12000
SAFETY_BLOCK_CANDIDATE_CHARS = 24000
CONTROL_TEXT_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b\u200c\u200d\ufeff]")
CJK_CHAR_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\u31f0-\u31ff\uac00-\ud7af]"
)
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("generic_api_key_assignment", re.compile(r"(?i)\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[^'\"\s]{12,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
)
PROMPT_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ignore_previous_instructions", re.compile(r"(?i)\bignore (?:all )?(?:previous|prior|above) instructions\b")),
    ("reveal_system_prompt", re.compile(r"(?i)\b(?:reveal|show|print|dump|repeat).{0,40}\b(?:system prompt|developer message|hidden instructions)\b")),
    ("bypass_rules", re.compile(r"(?i)\b(?:bypass|override|disable).{0,30}\b(?:rules|guardrails|safety|policy)\b")),
    (
        "zh_ignore_previous_instructions",
        re.compile(
            r"(?<!不要)(?<!不得)(?<!請勿)(?:忽略|無視|无视|忘記|忘掉|不理會|不理会)"
            r".{0,20}(?:先前|之前|前面|以上|上述|前述).{0,12}(?:指令|指示|規則|规则|要求|提示)"
        ),
    ),
    (
        "zh_reveal_system_prompt",
        re.compile(
            r"(?:(?:顯示|显示|輸出|输出|列出|洩漏|泄露|透露|重複|重复).{0,40}"
            r"(?:系統提示|系统提示|系統指令|系统指令|開發者訊息|开发者消息|隱藏指令|隐藏指令)"
            r"|(?:系統提示|系统提示|系統指令|系统指令|開發者訊息|开发者消息|隱藏指令|隐藏指令)"
            r".{0,40}(?:顯示|显示|輸出|输出|列出|洩漏|泄露|透露|重複|重复))"
        ),
    ),
    (
        "zh_bypass_rules",
        re.compile(
            r"(?:繞過|绕过|覆寫|覆盖|停用|禁用|解除|關閉|关闭).{0,30}"
            r"(?:規則|规则|限制|安全|防護|防护|政策|護欄|护栏)"
        ),
    ),
)


def count_cjk_chars(text: str | None) -> int:
    """Count Han, kana, and Hangul characters without retaining the input."""
    return len(CJK_CHAR_RE.findall(text or ""))


def estimate_tokens(chars: int, *, cjk_chars: int = 0) -> int:
    """Estimate tokens with separate CJK and non-CJK character ratios."""
    chars = max(0, int(chars or 0))
    cjk_chars = min(chars, max(0, int(cjk_chars or 0)))
    if not chars:
        return 0
    cjk_ratio = max(0.01, float(config.diagnostics.cjk_chars_per_token))
    latin_ratio = max(0.01, float(config.diagnostics.latin_chars_per_token))
    return math.ceil(cjk_chars / cjk_ratio + (chars - cjk_chars) / latin_ratio)


def normalize_usage(
    usage: dict[str, Any] | None,
    *,
    input_chars: int,
    output_chars: int = 0,
    input_cjk_chars: int = 0,
    output_cjk_chars: int = 0,
) -> dict[str, Any]:
    """Normalize provider usage, falling back to explicit estimates.

    OpenAI-compatible and Azure OpenAI responses commonly use
    prompt_tokens/completion_tokens/total_tokens. Some gateways use
    input_tokens/output_tokens. The returned shape is stable for reporting and
    marks estimates so governance reports do not imply billing precision.
    """
    input_chars = max(0, int(input_chars or 0))
    output_chars = max(0, int(output_chars or 0))
    input_cjk_chars = min(input_chars, max(0, int(input_cjk_chars or 0)))
    output_cjk_chars = min(output_chars, max(0, int(output_cjk_chars or 0)))
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

    any_estimated = False
    if prompt_tokens is None:
        if (
            total_tokens is not None
            and completion_tokens is not None
            and total_tokens >= completion_tokens
        ):
            prompt_tokens = total_tokens - completion_tokens
        else:
            prompt_tokens = estimate_tokens(input_chars, cjk_chars=input_cjk_chars)
            any_estimated = True
    if completion_tokens is None:
        if (
            total_tokens is not None
            and prompt_tokens is not None
            and total_tokens >= prompt_tokens
        ):
            completion_tokens = total_tokens - prompt_tokens
        else:
            completion_tokens = estimate_tokens(output_chars, cjk_chars=output_cjk_chars)
            any_estimated = True
    if total_tokens is None:
        total_tokens = prompt_tokens + completion_tokens

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "input_chars": input_chars,
        "output_chars": output_chars,
        "is_estimated": int(any_estimated),
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
    _insert_safety_findings(findings, event_type=event_type, surface=surface, context=context, metadata=metadata)
    return findings


def _insert_safety_findings(
    findings: list[dict[str, Any]],
    *,
    event_type: str,
    surface: str,
    context: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
) -> None:
    """Persist findings as `ai_safety_events` rows; never raises."""
    if not findings:
        return
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


# --- Indirect prompt injection: ingested sources and model output -----------
#
# The attacker these two scanners watch for is the author of an uploaded
# document, not the user (docs/SECURITY.md -> "Rendering LLM output"). Both are
# warn-only telemetry: they never block ingest or an answer, and they persist
# counts, booleans and hashes only -- never document text, output text or URLs,
# because an exfiltration URL *is* the data it is carrying out.

SOURCE_SCAN_EVENT_TYPE = "source_ingest_scan"
SOURCE_SCAN_SURFACE = "ingest.source"
#: Ingest warning code (sources.diagnostics_json.warnings[].code). The copy is
#: `diag.warn_<code>` in app/i18n.py.
SOURCE_SCAN_WARNING_CODE = "instruction_like_text"
#: Rules strong enough to put a user-visible warning on a source row. The
#: "bypass" rules were written for short chat questions; over whole documents
#: they match ordinary prose ("覆寫變數值；元件規則", "繞過網站存取限制" -- 3 of
#: this repo's own docs, all false positives), and a `!` that is usually wrong
#: teaches people to ignore it. They still count in diagnostics and events.
SOURCE_SCAN_WARNING_RULES = frozenset({
    "prompt_injection.ignore_previous_instructions",
    "prompt_injection.reveal_system_prompt",
    "prompt_injection.zh_ignore_previous_instructions",
    "prompt_injection.zh_reveal_system_prompt",
})


def scan_source_chunks(texts: list[str]) -> dict[str, Any]:
    """Count chunks of one source matching the prompt-injection rules.

    Only `PROMPT_INJECTION_PATTERNS` apply to documents. The other input rules
    would drown the signal: PDF text routinely carries form feeds and BOMs (the
    control-text rule), credential-looking strings are a DLP question rather
    than an injection one, and chunks are already length-bounded.
    """
    rules: dict[str, int] = {}
    first_chunk: dict[str, int] = {}
    flagged: set[int] = set()
    warned: set[int] = set()
    for index, text in enumerate(texts):
        for rule_id, pattern in PROMPT_INJECTION_PATTERNS:
            if pattern.search(text or ""):
                key = f"prompt_injection.{rule_id}"
                rules[key] = rules.get(key, 0) + 1
                first_chunk.setdefault(key, index)
                flagged.add(index)
                if key in SOURCE_SCAN_WARNING_RULES:
                    warned.add(index)
    return {
        "detector_version": SAFETY_DETECTOR_VERSION,
        "chunks_scanned": len(texts),
        "chunks_flagged": len(flagged),
        "chunks_warned": len(warned),
        "rules": rules,
        "first_chunk": first_chunk,
    }


def record_source_scan_events(summary: dict[str, Any], *, context: dict[str, Any]) -> None:
    """One `ai_safety_events` row per matched rule per ingest (not per chunk:
    a long report would otherwise write hundreds of rows for one document)."""
    findings = [
        {
            "category": "prompt_injection",
            "severity": "medium",
            "decision": "warn",
            "rule_id": rule_id,
            "redacted_summary": f"Ingested source has {count} chunk(s) matching a prompt-injection pattern.",
            "metadata": {
                "chunks_matched": count,
                "chunks_scanned": summary.get("chunks_scanned", 0),
                "first_chunk_index": summary.get("first_chunk", {}).get(rule_id),
            },
        }
        for rule_id, count in sorted((summary.get("rules") or {}).items())
    ]
    _insert_safety_findings(
        findings,
        event_type=SOURCE_SCAN_EVENT_TYPE,
        surface=SOURCE_SCAN_SURFACE,
        context=context,
        metadata=None,
    )


OUTPUT_SCAN_EVENT_TYPE = "output_scan"
#: Call types whose output is never rendered or stored as user-facing text.
OUTPUT_SCAN_SKIP_CALL_TYPES = frozenset({"query_rewrite", "rerank", "eval_judge"})
_URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"'`]+", re.IGNORECASE)
_URL_TRAILING = ".,;:!?/。，；：！？、」』）】"
_MARKDOWN_REMOTE_IMAGE_RE = re.compile(r"!\[[^\]\n]*\]\(\s*<?https?://", re.IGNORECASE)
_ACTIVE_MARKUP_RE = re.compile(
    r"<\s*(?:img|picture|source|video|audio|iframe|frame|object|embed|form|input|button|"
    r"textarea|select|script|style|link|meta|base|svg)\b"
    r"|\b(?:data-)?hx-[a-z-]+\s*=",
    re.IGNORECASE,
)


def _normalized_urls(text: str) -> list[tuple[str, str]]:
    """(normalized url, lower-cased host) for each http(s) URL in `text`."""
    found = []
    for match in _URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(_URL_TRAILING)
        scheme, _, rest = url.partition("://")
        host, sep, path = rest.partition("/")
        host = host.lower()
        found.append((f"{scheme.lower()}://{host}{sep}{path}", host))
    return found


def scan_model_output(output: str, prompt: str) -> list[dict[str, Any]]:
    """Find the shapes an injected instruction gives model output.

    `prompt` is everything the model was shown, so it contains every piece of
    evidence. A URL the model emits that is not *exactly* one of the prompt's
    URLs did not come from the evidence. The sharpest form is a URL on a host
    the evidence does mention but with a different path or query: that is how
    an injected "append the other excerpts to https://host/?d=" comes back out.
    """
    output = output or ""
    findings: list[dict[str, Any]] = []

    images = len(_MARKDOWN_REMOTE_IMAGE_RE.findall(output))
    if images:
        findings.append({
            "category": "output_exfiltration",
            "severity": "high",
            "decision": "warn",
            "rule_id": "output.markdown_remote_image",
            "redacted_summary": "Model output contains Markdown image syntax pointing at a remote URL.",
            "metadata": {"match_count": images},
        })

    output_urls = _normalized_urls(output)
    if output_urls:
        prompt_urls = _normalized_urls(prompt)
        known_urls = {url for url, _host in prompt_urls}
        known_hosts = {host for _url, host in prompt_urls}
        unsourced = [(url, host) for url, host in output_urls if url not in known_urls]
        if unsourced:
            on_known_host = sum(1 for _url, host in unsourced if host in known_hosts)
            findings.append({
                "category": "output_unsourced_url",
                "severity": "high" if on_known_host else "medium",
                "decision": "warn",
                "rule_id": "output.unsourced_url",
                "redacted_summary": "Model output contains a URL that does not appear in its input.",
                "metadata": {
                    "url_count": len(output_urls),
                    "unsourced_count": len(unsourced),
                    "unsourced_on_known_host_count": on_known_host,
                },
            })

    markup = len(_ACTIVE_MARKUP_RE.findall(output))
    if markup:
        findings.append({
            "category": "output_active_markup",
            "severity": "medium",
            "decision": "warn",
            "rule_id": "output.active_markup",
            "redacted_summary": "Model output contains HTML that fetches, submits, styles or binds behaviour.",
            "metadata": {"match_count": markup},
        })
    return findings


def record_output_scan_events(
    output: str,
    prompt: str,
    *,
    call_type: str,
    usage_context: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Scan one completion against its prompt and persist findings. Never raises."""
    if call_type in OUTPUT_SCAN_SKIP_CALL_TYPES or call_type.startswith("settings_"):
        return []
    try:
        findings = scan_model_output(output, prompt)
    except Exception:
        logger.warning("output_scan_failed call_type=%s", call_type, exc_info=True)
        return []
    if findings:
        content_hash = _content_hash(output)
        for finding in findings:
            finding["content_hash"] = content_hash
        logger.warning(
            "output_scan_findings call_type=%s rules=%s",
            call_type, ",".join(finding["rule_id"] for finding in findings),
        )
        _insert_safety_findings(
            findings,
            event_type=OUTPUT_SCAN_EVENT_TYPE,
            surface=f"llm.{call_type}"[:120],
            context=usage_context,
            metadata=None,
        )
    return findings


RERANK_OUTLIER_EVENT_TYPE = "rerank_outlier"


def record_rerank_outliers(
    outliers: list[dict[str, Any]],
    *,
    candidate_count: int,
    usage_context: dict[str, Any] | None,
) -> None:
    """One row per promoted chunk's source; see `llm.rerank_outliers`."""
    for outlier in outliers:
        _insert_safety_findings(
            [{
                "category": "retrieval_anomaly",
                "severity": "low",
                "decision": "warn",
                "rule_id": "rerank.promoted_from_low_hybrid_rank",
                "redacted_summary": "Reranker gave a near-maximal score to a chunk hybrid search ranked low.",
                "metadata": {
                    "rerank_score": outlier["rerank_score"],
                    "hybrid_rank": outlier["hybrid_rank"],
                    "returned_rank": outlier["returned_rank"],
                    "candidate_count": candidate_count,
                },
            }],
            event_type=RERANK_OUTLIER_EVENT_TYPE,
            surface="retrieval.rerank",
            context={**(usage_context or {}), "source_id": outlier.get("source_id")},
            metadata=None,
        )


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
    if normalized in SAFE_AGGREGATE_METADATA_KEYS:
        return False
    compact = normalized.replace("_", "")
    compact_sensitive_keys = {sensitive.replace("_", "") for sensitive in SENSITIVE_METADATA_KEYS}
    return normalized in SENSITIVE_METADATA_KEYS or any(sensitive in compact for sensitive in compact_sensitive_keys)
