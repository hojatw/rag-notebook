"""Admin LLM settings routes: the /settings page, connection diagnostics
(test-chat / test-embedding), and saving the chat + embedding connections.

Extracted from ``app/main.py`` to keep the HTTP layer thin. These routes live on
an APIRouter that ``app/main.py`` mounts; route paths and behaviour are
unchanged. Chat and embedding remain independent connections (see AGENTS.md).
"""

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from . import i18n
from .db import (
    connect,
    dumps,
    encrypt_for_storage,
    load_llm_settings,
    load_llm_settings_for_display,
    loads,
)
from .llm import (
    chat_settings,
    embedding_settings,
    probe_chat_diagnostics,
    probe_embedding_diagnostics,
    probe_embedding_dimension,
)
from .main import record_audit_event, render, require_admin
from .vector_store import current_dimension as vector_current_dimension

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: Annotated[dict, Depends(require_admin)]):
    """Render the admin LLM settings page without exposing the API key."""
    with connect() as conn:
        settings = load_llm_settings_for_display(conn)
    return render_settings(request, user, settings)


def render_settings(
    request: Request,
    user: dict,
    settings: dict[str, Any],
    *,
    saved: bool = False,
    diagnostic_notice: str = "",
    active_tab: str = "chat",
) -> HTMLResponse:
    return render(
        request,
        "settings.html",
        {
            "user": user,
            "settings": settings,
            "saved": saved,
            "diagnostic_notice": diagnostic_notice,
            "active_tab": active_tab,
            "diagnostic_status_labels": {
                "succeeded": i18n.t("settings.status_succeeded"),
                "failed": i18n.t("settings.status_failed"),
                "skipped": i18n.t("settings.status_skipped"),
                "not_tested": i18n.t("settings.status_not_tested"),
            },
        },
    )


def candidate_settings_from_form(
    existing_decrypted: dict[str, Any],
    *,
    provider: str,
    base_url: str,
    embedding_base_url: str,
    api_key: str,
    chat_model: str,
    embedding_model: str,
    embedding_query_prefix: str,
    embedding_passage_prefix: str,
    api_version: str,
    temperature: float,
    timeout_seconds: float,
    embedding_provider: str,
    embedding_api_key: str,
    embedding_api_version: str,
) -> dict[str, Any]:
    if provider not in {"openai_compatible", "azure_openai"}:
        raise HTTPException(status_code=400, detail="Unsupported LLM provider")
    if embedding_provider not in {"openai_compatible", "azure_openai"}:
        raise HTTPException(status_code=400, detail="Unsupported embedding provider")
    return {
        "provider": provider,
        "base_url": base_url.strip(),
        "embedding_base_url": embedding_base_url.strip(),
        "api_key": api_key.strip() or existing_decrypted.get("api_key", ""),
        "chat_model": chat_model.strip(),
        "embedding_model": embedding_model.strip(),
        "embedding_query_prefix": embedding_query_prefix,
        "embedding_passage_prefix": embedding_passage_prefix,
        "api_version": api_version.strip(),
        "temperature": temperature,
        "timeout_seconds": timeout_seconds,
        # Independent embedding connection. Blank key keeps the stored one.
        "embedding_provider": embedding_provider,
        "embedding_api_key": embedding_api_key.strip() or existing_decrypted.get("embedding_api_key", ""),
        "embedding_api_version": embedding_api_version.strip(),
    }


def llm_settings_fingerprint(settings: dict[str, Any]) -> str:
    """Hash non-secret settings so diagnostics can be matched without secrets."""
    summary = {
        "provider": settings.get("provider") or "openai_compatible",
        "base_url": settings.get("base_url") or "",
        "embedding_base_url": settings.get("embedding_base_url") or "",
        "api_key_set": bool(settings.get("api_key")),
        "chat_model": settings.get("chat_model") or "",
        "embedding_model": settings.get("embedding_model") or "",
        "embedding_query_prefix": settings.get("embedding_query_prefix") or "",
        "embedding_passage_prefix": settings.get("embedding_passage_prefix") or "",
        "api_version": settings.get("api_version") or "",
        "temperature": float(settings.get("temperature") or 0),
        "timeout_seconds": float(settings.get("timeout_seconds") or 0),
        "embedding_provider": settings.get("embedding_provider") or "openai_compatible",
        "embedding_api_key_set": bool(settings.get("embedding_api_key")),
        "embedding_api_version": settings.get("embedding_api_version") or "",
    }
    return hashlib.sha256(dumps(summary).encode("utf-8")).hexdigest()


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def compact_diagnostic_result(
    kind: str,
    result: dict[str, Any],
    *,
    settings_fingerprint: str,
    include_image: bool = False,
) -> dict[str, Any]:
    compact: dict[str, Any] = {
        "kind": kind,
        "status": str(result.get("status") or "failed")[:40],
        "tested_at": utc_timestamp(),
        "settings_fingerprint": settings_fingerprint,
        "provider": str(result.get("provider") or "")[:80],
        "model": str(result.get("model") or "")[:160],
        "latency_ms": round(float(result.get("latency_ms") or 0), 1),
        "error_class": str(result.get("error_class") or "")[:120],
    }
    if kind == "embedding":
        dimension = result.get("embedding_dimension")
        compact["embedding_dimension"] = int(dimension) if dimension is not None else None
        current_dimension = vector_current_dimension()
        compact["current_index_dimension"] = int(current_dimension) if current_dimension is not None else None
    if kind == "chat":
        compact["include_image_understanding"] = bool(include_image)
        compact["capabilities"] = compact_diagnostic_capabilities(result.get("capabilities") or {})
    return compact


def compact_diagnostic_capabilities(capabilities: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for name, value in capabilities.items():
        if not isinstance(value, dict):
            continue
        item = {
            "status": str(value.get("status") or "not_tested")[:40],
            "latency_ms": round(float(value.get("latency_ms") or 0), 1),
            "error_class": str(value.get("error_class") or "")[:120],
        }
        for key in ("usage_available", "stream_usage_fallback", "json_valid"):
            if key in value:
                item[key] = bool(value.get(key))
        compact[str(name)[:80]] = item
    return compact


def audit_diagnostic_metadata(result: dict[str, Any]) -> dict[str, Any]:
    metadata = {
        "kind": result.get("kind"),
        "status": result.get("status"),
        "provider": result.get("provider"),
        "model": result.get("model"),
        "latency_ms": result.get("latency_ms"),
        "error_class": result.get("error_class"),
        "embedding_dimension": result.get("embedding_dimension"),
        "include_image_understanding": result.get("include_image_understanding", False),
    }
    capabilities = result.get("capabilities")
    if isinstance(capabilities, dict):
        metadata["capabilities"] = {
            name: {"status": value.get("status"), "error_class": value.get("error_class", "")}
            for name, value in capabilities.items()
            if isinstance(value, dict)
        }
    return {key: value for key, value in metadata.items() if value not in (None, "")}


def store_llm_diagnostic(kind: str, result: dict[str, Any]) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT diagnostics_json FROM llm_settings WHERE id = 1").fetchone()
        try:
            diagnostics = loads(row["diagnostics_json"] or "{}") if row else {}
        except (TypeError, json.JSONDecodeError):
            diagnostics = {}
        if not isinstance(diagnostics, dict):
            diagnostics = {}
        diagnostics[kind] = result
        conn.execute("UPDATE llm_settings SET diagnostics_json = ? WHERE id = 1", (dumps(diagnostics),))
        return load_llm_settings_for_display(conn)


@router.post("/settings/test-chat", response_class=HTMLResponse)
async def test_chat_settings(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
    provider: str = Form("openai_compatible"),
    base_url: str = Form(""),
    embedding_base_url: str = Form(""),
    api_key: str = Form(""),
    chat_model: str = Form(""),
    embedding_model: str = Form(""),
    embedding_query_prefix: str = Form(""),
    embedding_passage_prefix: str = Form(""),
    api_version: str = Form("2024-02-15-preview"),
    temperature: float = Form(0.2),
    timeout_seconds: float = Form(60),
    embedding_provider: str = Form("openai_compatible"),
    embedding_api_key: str = Form(""),
    embedding_api_version: str = Form("2024-02-15-preview"),
    include_image_understanding: str | None = Form(None),
):
    with connect() as conn:
        existing_decrypted = load_llm_settings(conn) or {}
    candidate = candidate_settings_from_form(
        existing_decrypted,
        provider=provider,
        base_url=base_url,
        embedding_base_url=embedding_base_url,
        api_key=api_key,
        chat_model=chat_model,
        embedding_model=embedding_model,
        embedding_query_prefix=embedding_query_prefix,
        embedding_passage_prefix=embedding_passage_prefix,
        api_version=api_version,
        temperature=temperature,
        timeout_seconds=timeout_seconds,
        embedding_provider=embedding_provider,
        embedding_api_key=embedding_api_key,
        embedding_api_version=embedding_api_version,
    )
    include_image = bool(include_image_understanding)
    raw_result = await probe_chat_diagnostics(
        chat_settings(candidate),
        include_image=include_image,
        usage_context={"user_id": user["id"]},
    )
    compact = compact_diagnostic_result(
        "chat",
        raw_result,
        settings_fingerprint=llm_settings_fingerprint(candidate),
        include_image=include_image,
    )
    settings = store_llm_diagnostic("chat", compact)
    record_audit_event(
        request,
        user,
        "llm_settings_test_chat",
        "llm_settings",
        1,
        audit_diagnostic_metadata(compact),
        "normal",
    )
    logger.info(
        "settings_chat_tested admin_user_id=%s status=%s model=%s include_image=%s",
        user["id"],
        compact["status"],
        compact["model"],
        include_image,
    )
    return render_settings(request, user, settings, diagnostic_notice=i18n.t("settings.diag_notice_chat"))


@router.post("/settings/test-embedding", response_class=HTMLResponse)
async def test_embedding_settings(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
    provider: str = Form("openai_compatible"),
    base_url: str = Form(""),
    embedding_base_url: str = Form(""),
    api_key: str = Form(""),
    chat_model: str = Form(""),
    embedding_model: str = Form(""),
    embedding_query_prefix: str = Form(""),
    embedding_passage_prefix: str = Form(""),
    api_version: str = Form("2024-02-15-preview"),
    temperature: float = Form(0.2),
    timeout_seconds: float = Form(60),
    embedding_provider: str = Form("openai_compatible"),
    embedding_api_key: str = Form(""),
    embedding_api_version: str = Form("2024-02-15-preview"),
):
    with connect() as conn:
        existing_decrypted = load_llm_settings(conn) or {}
    candidate = candidate_settings_from_form(
        existing_decrypted,
        provider=provider,
        base_url=base_url,
        embedding_base_url=embedding_base_url,
        api_key=api_key,
        chat_model=chat_model,
        embedding_model=embedding_model,
        embedding_query_prefix=embedding_query_prefix,
        embedding_passage_prefix=embedding_passage_prefix,
        api_version=api_version,
        temperature=temperature,
        timeout_seconds=timeout_seconds,
        embedding_provider=embedding_provider,
        embedding_api_key=embedding_api_key,
        embedding_api_version=embedding_api_version,
    )
    raw_result = await probe_embedding_diagnostics(embedding_settings(candidate), usage_context={"user_id": user["id"]})
    compact = compact_diagnostic_result(
        "embedding",
        raw_result,
        settings_fingerprint=llm_settings_fingerprint(candidate),
    )
    settings = store_llm_diagnostic("embedding", compact)
    record_audit_event(
        request,
        user,
        "llm_settings_test_embedding",
        "llm_settings",
        1,
        audit_diagnostic_metadata(compact),
        "normal",
    )
    logger.info(
        "settings_embedding_tested admin_user_id=%s status=%s model=%s dimension=%s",
        user["id"],
        compact["status"],
        compact["model"],
        compact.get("embedding_dimension"),
    )
    return render_settings(request, user, settings, diagnostic_notice=i18n.t("settings.diag_notice_embedding"), active_tab="embedding")


@router.post("/settings")
async def update_settings(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
    provider: str = Form("openai_compatible"),
    base_url: str = Form(""),
    embedding_base_url: str = Form(""),
    api_key: str = Form(""),
    chat_model: str = Form(""),
    embedding_model: str = Form(""),
    embedding_query_prefix: str = Form(""),
    embedding_passage_prefix: str = Form(""),
    api_version: str = Form("2024-02-15-preview"),
    temperature: float = Form(0.2),
    timeout_seconds: float = Form(60),
    embedding_provider: str = Form("openai_compatible"),
    embedding_api_key: str = Form(""),
    embedding_api_version: str = Form("2024-02-15-preview"),
):
    """Validate and save global LLM provider settings.

    Chat and embedding have independent connections (provider / key /
    api-version), so they can point at different services. Probes the embedding
    endpoint before persisting when embedding-affecting fields changed, so
    connectivity errors and dim mismatches surface at save time instead of at
    first ingest.
    """
    if provider not in {"openai_compatible", "azure_openai"}:
        raise HTTPException(status_code=400, detail="Unsupported LLM provider")
    if embedding_provider not in {"openai_compatible", "azure_openai"}:
        raise HTTPException(status_code=400, detail="Unsupported embedding provider")

    with connect() as conn:
        existing_row = conn.execute("SELECT * FROM llm_settings WHERE id = 1").fetchone()
        existing = dict(existing_row) if existing_row else {}
        # The stored keys are either Fernet ciphertext or legacy plaintext.
        # Either way they are opaque to us here; we keep the stored value when
        # the form field was left blank (the "keep existing" UX).
        if api_key.strip():
            stored_key = encrypt_for_storage(api_key.strip())
        else:
            stored_key = existing.get("api_key", "")
        if embedding_api_key.strip():
            stored_embedding_key = encrypt_for_storage(embedding_api_key.strip())
        else:
            stored_embedding_key = existing.get("embedding_api_key", "")

        # Decide whether the embedding endpoint changed materially. If it
        # didn't, skip the probe — admins editing temperature shouldn't be
        # forced to be online with the LLM service to save settings.
        embedding_changed = (
            embedding_model.strip() != (existing.get("embedding_model") or "")
            or embedding_base_url.strip() != (existing.get("embedding_base_url") or "")
            or embedding_provider != (existing.get("embedding_provider") or "openai_compatible")
            or embedding_api_version.strip() != (existing.get("embedding_api_version") or "")
            or bool(embedding_api_key.strip())
        )

    if embedding_changed and embedding_model.strip():
        # Build a candidate settings dict with the plaintext key so we can
        # actually call the API. Falls back to the existing decrypted key
        # when the form field was left blank.
        with connect() as conn:
            existing_decrypted = load_llm_settings(conn) or {}
        probe_settings = embedding_settings({
            "embedding_provider": embedding_provider,
            "embedding_base_url": embedding_base_url.strip(),
            "embedding_api_key": embedding_api_key.strip() or existing_decrypted.get("embedding_api_key", ""),
            "embedding_model": embedding_model.strip(),
            "embedding_api_version": embedding_api_version.strip(),
            "timeout_seconds": timeout_seconds,
        })
        try:
            new_dim = await probe_embedding_dimension(probe_settings)
        except Exception as exc:
            logger.warning("settings_probe_failed user_id=%s err=%s", user["id"], exc)
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Could not reach the embedding endpoint: {exc}. "
                    "Verify base URL / embedding base URL, API key, and model name before saving."
                ),
            )
        current_dim = vector_current_dimension()
        if current_dim is not None and current_dim != new_dim:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Embedding dimension mismatch: existing index is {current_dim}-dim, "
                    f"new model returns {new_dim}-dim. Open /admin/index, click Clear, "
                    "then save these settings again and Rebuild."
                ),
            )

    with connect() as conn:
        conn.execute(
            """
            UPDATE llm_settings
            SET provider = ?, base_url = ?, embedding_base_url = ?, api_key = ?,
                chat_model = ?, embedding_model = ?,
                embedding_query_prefix = ?, embedding_passage_prefix = ?,
                api_version = ?, temperature = ?, timeout_seconds = ?,
                embedding_provider = ?, embedding_api_key = ?, embedding_api_version = ?
            WHERE id = 1
            """,
            (
                provider,
                base_url.strip(),
                embedding_base_url.strip(),
                stored_key,
                chat_model.strip(),
                embedding_model.strip(),
                # Prefixes are stored verbatim — the trailing space in "query: "
                # / "passage: " is significant, so they must NOT be stripped.
                embedding_query_prefix,
                embedding_passage_prefix,
                api_version.strip(),
                temperature,
                timeout_seconds,
                embedding_provider,
                stored_embedding_key,
                embedding_api_version.strip(),
            ),
        )
        settings = load_llm_settings_for_display(conn)
    audited_fields = {
        "provider": {"before": existing.get("provider"), "after": provider},
        "base_url_set": {"before": bool((existing.get("base_url") or "").strip()), "after": bool(base_url.strip())},
        "embedding_base_url_set": {
            "before": bool((existing.get("embedding_base_url") or "").strip()),
            "after": bool(embedding_base_url.strip()),
        },
        "chat_model": {"before": existing.get("chat_model") or "", "after": chat_model.strip()},
        "embedding_model": {"before": existing.get("embedding_model") or "", "after": embedding_model.strip()},
        "embedding_query_prefix_changed": embedding_query_prefix != (existing.get("embedding_query_prefix") or ""),
        "embedding_passage_prefix_changed": embedding_passage_prefix != (existing.get("embedding_passage_prefix") or ""),
        "api_version": {"before": existing.get("api_version") or "", "after": api_version.strip()},
        "temperature": {"before": existing.get("temperature"), "after": temperature},
        "timeout_seconds": {"before": existing.get("timeout_seconds"), "after": timeout_seconds},
        "api_key_changed": bool(api_key.strip()),
        "embedding_provider": {
            "before": existing.get("embedding_provider"),
            "after": embedding_provider,
        },
        "embedding_api_version": {
            "before": existing.get("embedding_api_version") or "",
            "after": embedding_api_version.strip(),
        },
        "embedding_api_key_changed": bool(embedding_api_key.strip()),
        "embedding_changed": embedding_changed,
    }
    record_audit_event(
        request,
        user,
        "llm_settings_updated",
        "llm_settings",
        1,
        audited_fields,
        "high" if embedding_changed or api_key.strip() or embedding_api_key.strip() else "normal",
    )
    logger.info(
        "settings_updated admin_user_id=%s provider=%s base_url_set=%s embedding_base_url_set=%s chat_model_set=%s embedding_model_set=%s api_version=%s temperature=%s timeout_seconds=%s api_key_changed=%s",
        user["id"],
        provider,
        bool(base_url.strip()),
        bool(embedding_base_url.strip()),
        bool(chat_model.strip()),
        bool(embedding_model.strip()),
        api_version.strip(),
        temperature,
        timeout_seconds,
        bool(api_key.strip()),
    )
    return render_settings(request, user, settings, saved=True)
