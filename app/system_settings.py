"""Admin overrides of operational tunables (`system_settings` table).

Precedence for a registered key: an admin override stored here, else the
deployment value from `app.config` (dataclass default ← config.toml ← env).
The deployment value is therefore the *default* the UI can reset to.

Values are read from SQLite at the point of use rather than cached, because the
reader is often a different process from the writer: the admin saves in the web
app, the standalone ingest worker parses. One small indexed read per use is the
price of "applies to the next source, no restart".

Only keys in `SETTINGS` can be stored — this is not a generic config editor.
"""
import dataclasses
import logging
import math
from typing import Any, Callable

from . import config as app_config
from . import i18n
from .db import connect, loads

logger = logging.getLogger(__name__)

#: Where an effective value came from, shown next to it on /settings.
SOURCE_ADMIN = "admin"
SOURCE_DEPLOYMENT = "deployment"

EXTRACT_TIMEOUT_KEY = "jobs.extract_timeout_s"


@dataclasses.dataclass(frozen=True)
class SettingSpec:
    default: Callable[[], float]
    minimum: Callable[[], float]
    maximum: Callable[[], float]


def _jobs():
    # Looked up on every call, not bound at import: the reader of these values
    # must see the same `config` object as `ingest`, including after a reload.
    return app_config.config.jobs


SETTINGS: dict[str, SettingSpec] = {
    # Floor: a lower value would fail ordinary documents en masse (the largest
    # measured parse is 32s). Ceiling: the same 90%-of-visibility-timeout bound
    # `ingest._extract_timeout_s` enforces, so a saved value is never clamped.
    EXTRACT_TIMEOUT_KEY: SettingSpec(
        default=lambda: float(_jobs().extract_timeout_s),
        minimum=lambda: 30.0,
        maximum=lambda: float(_jobs().visibility_timeout_s) * 0.9,
    ),
}


def _spec(key: str) -> SettingSpec:
    if key not in SETTINGS:
        raise KeyError(f"unregistered system setting: {key}")
    return SETTINGS[key]


def get_override(key: str) -> float | None:
    """The stored admin override, or None. Never raises: a read failure must
    not stop the caller (ingest) — it falls back to the deployment value."""
    _spec(key)
    try:
        with connect() as conn:
            row = conn.execute("SELECT value FROM system_settings WHERE key = ?", (key,)).fetchone()
    except Exception:
        logger.warning("system_setting_read_failed setting=%s fallback=deployment", key, exc_info=True)
        return None
    if row is None:
        return None
    try:
        return float(row["value"])
    except (TypeError, ValueError):
        logger.warning("system_setting_unparseable setting=%s fallback=deployment", key)
        return None


def effective(key: str) -> tuple[float, str]:
    """``(value, source)`` for a registered key."""
    override = get_override(key)
    if override is not None:
        return override, SOURCE_ADMIN
    return _spec(key).default(), SOURCE_DEPLOYMENT


def validate(key: str, raw: Any) -> float:
    """Parse and range-check a submitted value; raises ValueError with UI copy."""
    spec = _spec(key)
    low, high = spec.minimum(), spec.maximum()
    try:
        value = float(str(raw).strip())
    except ValueError:
        raise ValueError(i18n.t("settings.ingest_invalid_number")) from None
    if not math.isfinite(value) or value < low or value > high:
        raise ValueError(i18n.t("settings.ingest_out_of_range", low=int(low), high=int(high)))
    return value


def set_override(key: str, raw: Any, user_id: int | None) -> float:
    value = validate(key, raw)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO system_settings (key, value, updated_by, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value, updated_by = excluded.updated_by, updated_at = CURRENT_TIMESTAMP
            """,
            (key, repr(value), user_id),
        )
    return value


def clear_override(key: str) -> None:
    _spec(key)
    with connect() as conn:
        conn.execute("DELETE FROM system_settings WHERE key = ?", (key,))


def recent_extract_stats(limit: int = 200) -> dict[str, Any]:
    """Parse durations of the most recent sources, to choose a timeout from data.

    ``max_s`` / ``p95_s`` cover parses that completed; timed-out parses are
    counted separately in ``timeouts``.

    Reads `extract_duration_s` / `failed_reason` from `sources.diagnostics_json`
    (written by `ingest.process_source`). Sources ingested before that field
    existed simply don't count.
    """
    from .ingest import EXTRACT_FAILED_TIMEOUT  # local: ingest imports this module

    with connect() as conn:
        rows = conn.execute(
            "SELECT diagnostics_json FROM sources WHERE diagnostics_json LIKE '%extract_duration_s%' "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    durations: list[float] = []
    timeouts = 0
    for row in rows:
        try:
            diagnostics = loads(row["diagnostics_json"])
        except ValueError:
            continue
        if not isinstance(diagnostics, dict):
            continue
        if diagnostics.get("failed_reason") == EXTRACT_FAILED_TIMEOUT:
            # Its duration is just the timeout in force, not how long the file
            # needs — counting it would pass a lower bound off as a measurement.
            timeouts += 1
            continue
        duration = diagnostics.get("extract_duration_s")
        if isinstance(duration, (int, float)):
            durations.append(float(duration))
    durations.sort()
    p95 = durations[min(len(durations) - 1, math.ceil(0.95 * len(durations)) - 1)] if durations else None
    return {
        "count": len(durations),
        "max_s": durations[-1] if durations else None,
        "p95_s": p95,
        "timeouts": timeouts,
    }
