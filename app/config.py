"""Centralized tunable configuration.

All the retrieval/quality knobs and operational parameters that used to be
hardcoded across `main.py` / `llm.py` / `ingest.py` / `jobs.py` live here as a
single, version-controlled source of truth. Values are resolved in three
layers, lowest precedence first:

1. dataclass **defaults** below (identical to the previous hardcoded values),
2. a **TOML file** (`NOTEBOOKLM_CONFIG_FILE`, or `config.toml` in the working
   directory if present — copy `config.example.toml` to start one),
3. **environment variables** `NOTEBOOKLM_<GROUP>_<FIELD>` (handy for eval
   sweeps and per-deployment overrides; they win over the file).

`load_config()` runs once at import into the module-level `config`. The eval
harness and the app read the same object, so a sweep is just: set env/file,
run. Changing `[chunking]` requires re-indexing (it changes stored chunks).
"""
import dataclasses
import logging
import os
import tomllib
from pathlib import Path
from typing import Any

logger = logging.getLogger("notebooklm")

ENV_PREFIX = "NOTEBOOKLM_"


@dataclasses.dataclass
class RetrievalConfig:
    vector_weight: float = 0.7          # hybrid blend: weight on the vector score
    keyword_weight: float = 0.3         # hybrid blend: weight on the keyword score
    rerank_weight: float = 0.8          # rerank blend: weight on the LLM rerank score
    rerank_base_weight: float = 0.2     # rerank blend: weight on the hybrid score
    low_confidence_threshold: float = 0.25  # below this top score the app abstains
    candidate_pool_size: int = 20       # vector n_results / keyword limit / pre-rerank cap
    final_chunk_count: int = 6          # chunks kept after rerank, fed to the answer
    fallback_max_chunks: int = 2000     # cap on the Chroma-down SQLite scan


@dataclasses.dataclass
class ChunkingConfig:  # changing these requires re-indexing
    latin_target_chars: int = 800
    cjk_target_chars: int = 400
    overlap_sentences: int = 1


@dataclasses.dataclass
class SpreadsheetConfig:
    """A6c spreadsheet ingestion. Design notes: docs/SPREADSHEET_INGESTION.md.

    Values marked *(chunk shape)* change what gets stored and therefore require
    re-indexing existing spreadsheet sources; the caps and synonym lists only
    affect what is read/detected on the next ingest.
    """
    max_rows: int = 5000                 # per sheet, after the header row
    max_cols: int = 60                   # per sheet
    rows_per_chunk_max: int = 20         # (chunk shape) upper bound on record packing
    embed_token_budget: int = 400        # (chunk shape) estimated tokens per record chunk
    header_sample_rows: int = 5          # rows inspected when inferring a header
    wide_sheet_cols: int = 25            # above this, warn that the sheet is wide
    # Comma-separated column-name lists for Q&A detection. Customer sheets use
    # house vocabulary (客戶提問 / 回覆內容), hence configurable.
    qa_question_synonyms: str = "question,q,問題,提問,問句,客戶提問"
    qa_answer_synonyms: str = "answer,a,答案,回覆,回答,解答,回覆內容"


@dataclasses.dataclass
class DiagnosticsConfig:
    """A6a ingestion diagnostics — thresholds for the per-source warnings.

    These only decide when a warning is *shown*; none of them changes what is
    extracted, chunked, or embedded, so tuning them never requires re-indexing.
    """
    low_text_chars: int = 200           # below this extracted total -> "almost no text" warning
    preview_chars: int = 500            # stored extracted-text preview length
    embedding_token_budget: int = 512   # model input window used for the over-budget warning
    cjk_chars_per_token: float = 1.0    # CJK-heavy text estimate (conservative: 1 token/char)
    latin_chars_per_token: float = 4.0  # Latin text estimate (the usual ~4 chars/token)


@dataclasses.dataclass
class EmbeddingConfig:
    batch_size: int = 64                # texts per embedding request (settings can override)
    max_concurrency: int = 4            # in-flight embedding batches (settings can override)


@dataclasses.dataclass
class MaxTokensConfig:
    """LLM-3: per-call-type output caps.

    A cap is a **truncation limit, not a length target** — setting one too low
    does not make the model write less, it cuts the response off mid-way. For the
    call types that return JSON that is worse than it sounds: the caller parses
    with a try/except and degrades silently (rerank falls back to hybrid order,
    query rewrite falls back to the original question), so an over-tight cap
    shows up as "retrieval quietly got worse", not as an error. Hence the
    generous headroom on those.

    Sized for **Traditional Chinese** output at roughly 1 token per character
    (see `DiagnosticsConfig.cjk_chars_per_token`); the same content in English
    needs about a quarter of this. Values are ~2x the expected output.

    Replace these with measured numbers once a deployment has traffic:
    `llm_usage_events` records `completion_tokens` per `call_type` — take p95 and
    multiply by 1.5, filtering on `is_estimated = 0`.
    """
    # Names must match the `call_type` strings used at the call sites exactly —
    # `resolve_max_tokens` looks the field up by that name and silently falls back
    # to `default` on a miss. `tests/test_llm.py` pins that every call_type in the
    # codebase has a field here, so a new one cannot quietly inherit the default.
    settings_chat_probe: int = 128       # diagnostics: replies "ok"
    settings_stream_probe: int = 128
    settings_image_probe: int = 128
    query_rewrite: int = 512             # JSON array, 1-4 short queries
    followups: int = 512                 # JSON, 3 short questions
    source_summary: int = 512            # 2-4 sentence per-source summary
    rerank: int = 768                    # JSON, ~20 x {"id", "score"}
    starter_questions: int = 768         # JSON, 4 questions under 80 chars
    briefing: int = 768                  # one 80-110 word paragraph
    translate_summary: int = 768
    eval_judge: int = 1536               # JSON: 3 dimensions + rationales
    answer_stream: int = 2048            # a grounded answer with citations
    chat_completion: int = 2048          # the non-streaming default call_type
    eval_answer: int = 2048              # same shape as an answer
    meeting_minutes: int = 3072          # headed sections with bullets
    compare: int = 3072
    artifact_study_guide: int = 3072
    artifact_faq: int = 3072
    artifact_timeline: int = 3072
    eval_authoring: int = 3072           # several draft eval items
    default: int = 2048                  # any call type not listed above


@dataclasses.dataclass
class LLMRetryConfig:
    max_attempts: int = 3
    backoff_base_s: float = 0.5


@dataclasses.dataclass
class JobsConfig:
    visibility_timeout_s: float = 1800.0  # abandoned 'running' job becomes re-claimable
    max_attempts: int = 3
    poll_interval_s: float = 2.0


@dataclasses.dataclass
class RuntimeConfig:
    briefing_lock_timeout_s: float = 90.0
    # O0: how long a held embedding-dimension migration lock stays valid. The
    # migration is a collection swap plus a bounded SQLite update, so minutes
    # is generous; the timeout exists only so a process that died mid-migration
    # cannot wedge the ingest queue forever.
    index_migration_lock_timeout_s: float = 600.0
    upload_batch_limit: int = 5
    # Two file-size caps, named for the **pipeline stage** that enforces them.
    # They protect different things, which is why both exist:
    #
    #   upload_max_file_bytes   web request, while streaming to disk, ALL formats
    #                           -> protects the host: disk fill, request memory
    #   extract_max_file_bytes  ingest worker, file already stored, .xlsx/.pptx/.csv
    #                           -> protects the parser. On-disk size does not
    #                              bound these formats' parse cost, but for three
    #                              different reasons:
    #                                .xlsx  zip expansion only — openpyxl reads it
    #                                       with read_only=True, so rows stream
    #                                .pptx  zip expansion, and python-pptx builds
    #                                       the whole presentation object
    #                                .csv   not a zip: read_bytes -> decode ->
    #                                       splitlines each materialises the whole
    #                                       file, measured at ~5.7x its size for
    #                                       Big5. See QUALITY/PERFORMANCE backlog
    #                                       for the streaming rewrite.
    #                              PDF/DOCX stream and are exempt.
    #
    # The upload path applies whichever of the two is stricter for the file's
    # format (see `ingest.upload_limit_for`), so an oversized spreadsheet is
    # refused up front instead of failing later in the worker.
    #
    # 50 MB is ~2.4x the largest file in a real corpus (p95 was 6.6 MB), so it
    # blocks nothing legitimate while bounding what one request can write.
    upload_max_file_bytes: int = 50_000_000
    extract_max_file_bytes: int = 20_000_000
    suggestions_ttl_hours: int = 24
    briefing_ttl_hours: int = 24


@dataclasses.dataclass
class UIConfig:
    language: str = "zh-TW"  # default UI locale; the i18n catalog falls back to this


@dataclasses.dataclass
class AuthConfig:
    local_login_enabled: bool = True
    # SEC-3: absolute session lifetime. Counted from when the session was issued
    # and never extended by activity — a rolling window would keep a stolen
    # cookie alive for as long as it kept being used, which is the case this
    # exists to bound. 12 hours covers a working day without carrying a session
    # overnight; raise it per deployment if re-login friction outweighs that.
    session_max_age_hours: float = 12.0
    trusted_header_enabled: bool = False
    trusted_header_secret: str = ""
    trusted_header_secret_header: str = "X-NotebookLM-Auth-Secret"
    trusted_header_user_header: str = "X-Forwarded-User"
    trusted_header_email_header: str = "X-Forwarded-Email"
    trusted_header_name_header: str = "X-Forwarded-Name"
    trusted_header_groups_header: str = "X-Forwarded-Groups"
    trusted_header_admin_groups: str = ""
    trusted_header_auto_provision: bool = True
    trusted_header_provider: str = "trusted_header"
    trusted_header_allowed_ips: str = ""
    oidc_enabled: bool = False
    oidc_provider: str = "oidc"
    oidc_issuer: str = ""
    oidc_discovery_url: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_scopes: str = "openid profile email"
    oidc_redirect_path: str = "/auth/oidc/callback"
    oidc_token_auth_method: str = "client_secret_basic"
    oidc_email_claim: str = "email"
    oidc_name_claim: str = "name"
    oidc_groups_claim: str = "groups"
    oidc_admin_groups: str = ""
    oidc_auto_provision: bool = True
    oidc_allowed_algorithms: str = "RS256"


@dataclasses.dataclass
class AppConfig:
    retrieval: RetrievalConfig = dataclasses.field(default_factory=RetrievalConfig)
    chunking: ChunkingConfig = dataclasses.field(default_factory=ChunkingConfig)
    spreadsheet: SpreadsheetConfig = dataclasses.field(default_factory=SpreadsheetConfig)
    diagnostics: DiagnosticsConfig = dataclasses.field(default_factory=DiagnosticsConfig)
    embedding: EmbeddingConfig = dataclasses.field(default_factory=EmbeddingConfig)
    llm_retry: LLMRetryConfig = dataclasses.field(default_factory=LLMRetryConfig)
    max_tokens: MaxTokensConfig = dataclasses.field(default_factory=MaxTokensConfig)
    jobs: JobsConfig = dataclasses.field(default_factory=JobsConfig)
    runtime: RuntimeConfig = dataclasses.field(default_factory=RuntimeConfig)
    ui: UIConfig = dataclasses.field(default_factory=UIConfig)
    auth: AuthConfig = dataclasses.field(default_factory=AuthConfig)


def _coerce(raw: Any, field_type: type) -> Any:
    """Coerce a raw (env string or TOML scalar) value to the field's type."""
    if field_type is bool:
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    if field_type is int:
        return int(raw)
    if field_type is float:
        return float(raw)
    return raw


#: (group, current field) -> the field name it used to have. A deployment that
#: set the old key keeps working; it is read only when the new key is absent,
#: and each use is logged once so the setting can be migrated deliberately
#: rather than discovered later.
DEPRECATED_FIELD_ALIASES: dict[tuple[str, str], str] = {
    # Renamed because `max_upload_bytes` / `max_source_bytes` read as near
    # synonyms while meaning different pipeline stages. The names now lead with
    # the stage: upload-time vs extract-time.
    ("runtime", "extract_max_file_bytes"): "max_source_bytes",
}


def _load_group(group_cls, group_name: str, toml_data: dict):
    """Build one config group, applying TOML then env overrides over defaults."""
    toml_group = toml_data.get(group_name) or {}
    values: dict[str, Any] = {}
    for f in dataclasses.fields(group_cls):
        env_key = f"{ENV_PREFIX}{group_name.upper()}_{f.name.upper()}"
        if env_key in os.environ:
            values[f.name] = _coerce(os.environ[env_key], f.type)
        elif f.name in toml_group:
            values[f.name] = _coerce(toml_group[f.name], f.type)
            continue
        else:
            legacy = DEPRECATED_FIELD_ALIASES.get((group_name, f.name))
            if legacy is None:
                continue
            legacy_env = f"{ENV_PREFIX}{group_name.upper()}_{legacy.upper()}"
            if legacy_env in os.environ:
                values[f.name] = _coerce(os.environ[legacy_env], f.type)
            elif legacy in toml_group:
                values[f.name] = _coerce(toml_group[legacy], f.type)
            else:
                continue
            logger.warning(
                "config_deprecated_key group=%s old=%s new=%s "
                "detail=still honoured; rename it to silence this",
                group_name, legacy, f.name,
            )
    return group_cls(**values)


def _read_toml() -> dict:
    """Load the TOML config file if one is configured/present, else {}."""
    path = os.environ.get(f"{ENV_PREFIX}CONFIG_FILE")
    candidate = Path(path) if path else Path("config.toml")
    if not candidate.is_file():
        return {}
    try:
        with candidate.open("rb") as fh:
            data = tomllib.load(fh)
        logger.info("config_loaded_from_file path=%s", candidate)
        return data
    except (OSError, tomllib.TOMLDecodeError):
        logger.exception("config_file_unreadable path=%s — using defaults/env only", candidate)
        return {}


def load_config() -> AppConfig:
    toml_data = _read_toml()
    return AppConfig(
        retrieval=_load_group(RetrievalConfig, "retrieval", toml_data),
        chunking=_load_group(ChunkingConfig, "chunking", toml_data),
        spreadsheet=_load_group(SpreadsheetConfig, "spreadsheet", toml_data),
        diagnostics=_load_group(DiagnosticsConfig, "diagnostics", toml_data),
        embedding=_load_group(EmbeddingConfig, "embedding", toml_data),
        llm_retry=_load_group(LLMRetryConfig, "llm_retry", toml_data),
        max_tokens=_load_group(MaxTokensConfig, "max_tokens", toml_data),
        jobs=_load_group(JobsConfig, "jobs", toml_data),
        runtime=_load_group(RuntimeConfig, "runtime", toml_data),
        ui=_load_group(UIConfig, "ui", toml_data),
        auth=_load_group(AuthConfig, "auth", toml_data),
    )


config = load_config()
