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
class EmbeddingConfig:
    batch_size: int = 64                # texts per embedding request (settings can override)
    max_concurrency: int = 4            # in-flight embedding batches (settings can override)


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
    upload_batch_limit: int = 5
    suggestions_ttl_hours: int = 24
    briefing_ttl_hours: int = 24


@dataclasses.dataclass
class UIConfig:
    language: str = "zh-TW"  # default UI locale; the i18n catalog falls back to this


@dataclasses.dataclass
class AppConfig:
    retrieval: RetrievalConfig = dataclasses.field(default_factory=RetrievalConfig)
    chunking: ChunkingConfig = dataclasses.field(default_factory=ChunkingConfig)
    embedding: EmbeddingConfig = dataclasses.field(default_factory=EmbeddingConfig)
    llm_retry: LLMRetryConfig = dataclasses.field(default_factory=LLMRetryConfig)
    jobs: JobsConfig = dataclasses.field(default_factory=JobsConfig)
    runtime: RuntimeConfig = dataclasses.field(default_factory=RuntimeConfig)
    ui: UIConfig = dataclasses.field(default_factory=UIConfig)


def _coerce(raw: Any, field_type: type) -> Any:
    """Coerce a raw (env string or TOML scalar) value to the field's type."""
    if field_type is bool:
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    if field_type is int:
        return int(raw)
    if field_type is float:
        return float(raw)
    return raw


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
        embedding=_load_group(EmbeddingConfig, "embedding", toml_data),
        llm_retry=_load_group(LLMRetryConfig, "llm_retry", toml_data),
        jobs=_load_group(JobsConfig, "jobs", toml_data),
        runtime=_load_group(RuntimeConfig, "runtime", toml_data),
        ui=_load_group(UIConfig, "ui", toml_data),
    )


config = load_config()
