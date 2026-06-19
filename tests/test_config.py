"""Config layering tests: defaults <- TOML file <- env vars."""
import app.config as cfg


def test_defaults_match_the_previously_hardcoded_values(tmp_path, monkeypatch):
    """The dataclass defaults must equal the old hardcoded constants exactly —
    this is the behavior-preserving contract for the config refactor."""
    # Point at a non-existent file so a stray cwd config.toml can't pollute.
    monkeypatch.setenv("NOTEBOOKLM_CONFIG_FILE", str(tmp_path / "absent.toml"))
    c = cfg.load_config()

    assert c.retrieval.vector_weight == 0.7
    assert c.retrieval.keyword_weight == 0.3
    assert c.retrieval.rerank_weight == 0.8
    assert c.retrieval.rerank_base_weight == 0.2
    assert c.retrieval.low_confidence_threshold == 0.25
    assert c.retrieval.candidate_pool_size == 20
    assert c.retrieval.final_chunk_count == 6
    assert c.retrieval.fallback_max_chunks == 2000

    assert c.chunking.latin_target_chars == 800
    assert c.chunking.cjk_target_chars == 400
    assert c.chunking.overlap_sentences == 1

    assert c.embedding.batch_size == 64
    assert c.embedding.max_concurrency == 4

    assert c.llm_retry.max_attempts == 3
    assert c.llm_retry.backoff_base_s == 0.5

    assert c.jobs.visibility_timeout_s == 1800.0
    assert c.jobs.max_attempts == 3
    assert c.jobs.poll_interval_s == 2.0

    assert c.runtime.briefing_lock_timeout_s == 90.0
    assert c.runtime.upload_batch_limit == 5
    assert c.runtime.suggestions_ttl_hours == 24
    assert c.runtime.briefing_ttl_hours == 24

    assert c.ui.language == "zh-TW"


def test_toml_file_overrides_defaults(tmp_path, monkeypatch):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "[retrieval]\nvector_weight = 0.6\nfinal_chunk_count = 8\n"
        "[chunking]\nlatin_target_chars = 1000\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("NOTEBOOKLM_CONFIG_FILE", str(config_file))
    c = cfg.load_config()

    assert c.retrieval.vector_weight == 0.6        # overridden
    assert c.retrieval.final_chunk_count == 8      # overridden
    assert c.retrieval.keyword_weight == 0.3       # untouched default
    assert c.chunking.latin_target_chars == 1000   # overridden in another group


def test_env_beats_toml_and_coerces_types(tmp_path, monkeypatch):
    config_file = tmp_path / "config.toml"
    config_file.write_text("[retrieval]\nvector_weight = 0.6\n", encoding="utf-8")
    monkeypatch.setenv("NOTEBOOKLM_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("NOTEBOOKLM_RETRIEVAL_VECTOR_WEIGHT", "0.55")
    monkeypatch.setenv("NOTEBOOKLM_RETRIEVAL_CANDIDATE_POOL_SIZE", "30")
    c = cfg.load_config()

    assert c.retrieval.vector_weight == 0.55       # env wins over TOML, coerced to float
    assert isinstance(c.retrieval.vector_weight, float)
    assert c.retrieval.candidate_pool_size == 30   # env string coerced to int
    assert isinstance(c.retrieval.candidate_pool_size, int)


def test_missing_file_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_CONFIG_FILE", str(tmp_path / "nope.toml"))
    c = cfg.load_config()
    assert c.retrieval.vector_weight == 0.7  # falls back to defaults cleanly


def test_example_toml_matches_code_defaults(tmp_path, monkeypatch):
    """The shipped config.example.toml must equal the dataclass defaults, so the
    template never silently drifts from the real defaults."""
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    monkeypatch.setenv("NOTEBOOKLM_CONFIG_FILE", str(example))
    from_example = cfg.load_config()
    monkeypatch.setenv("NOTEBOOKLM_CONFIG_FILE", str(tmp_path / "absent.toml"))
    defaults = cfg.load_config()
    assert from_example == defaults
