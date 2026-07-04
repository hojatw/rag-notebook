"""Admin console routes: vector-index management, the audit log, and user
administration.

Extracted from ``app/main.py`` to keep the HTTP layer thin. These routes live on
an APIRouter that ``app/main.py`` mounts; route paths and behaviour are
unchanged.
"""

import json
import logging
from typing import Annotated, Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import i18n
from .config import config
from .db import connect, loads
from .main import record_audit_event, render, require_admin
from .security import hash_password
from .vector_store import clear_all_vectors, sync_from_sqlite
from .vector_store import index_status as vector_index_status

logger = logging.getLogger(__name__)

router = APIRouter()


def _admin_user_rows(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT u.id,
               u.username,
               u.is_admin,
               u.created_at,
               COUNT(ei.id) AS external_identity_count,
               GROUP_CONCAT(DISTINCT ei.provider) AS external_providers
          FROM users u
          LEFT JOIN external_identities ei ON ei.user_id = u.id
         GROUP BY u.id, u.username, u.is_admin, u.created_at
         ORDER BY u.id ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _auth_status(label: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"label": label, "ok": ok, "detail": detail}


def _is_local_auth_host(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "http" and (parsed.hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}


def _secure_url_ok(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" or _is_local_auth_host(url)


def _auth_diagnostics() -> dict[str, Any]:
    auth = config.auth
    tr = i18n.t
    local_enabled = bool(auth.local_login_enabled)
    trusted_enabled = bool(auth.trusted_header_enabled)
    oidc_enabled = bool(auth.oidc_enabled)
    discovery_url = (auth.oidc_discovery_url or "").strip()
    issuer = (auth.oidc_issuer or "").strip().rstrip("/")
    effective_discovery_url = discovery_url or (f"{issuer}/.well-known/openid-configuration" if issuer else "")
    oidc_endpoint_urls = [url for url in [effective_discovery_url, issuer] if url]
    oidc_urls_secure = all(_secure_url_ok(url) for url in oidc_endpoint_urls) if oidc_endpoint_urls else False
    oidc_scopes = {scope.strip().lower() for scope in (auth.oidc_scopes or "").split() if scope.strip()}
    token_method_ok = (auth.oidc_token_auth_method or "").strip().lower() in {"client_secret_basic", "client_secret_post"}

    checks = [
        _auth_status(
            tr("auth_diag.login_methods"),
            local_enabled or trusted_enabled or oidc_enabled,
            tr("auth_diag.login_methods_ok") if local_enabled or trusted_enabled or oidc_enabled else tr("auth_diag.login_methods_bad"),
        ),
        _auth_status(
            tr("auth_diag.trusted_secret"),
            (not trusted_enabled) or bool(auth.trusted_header_secret),
            tr("auth_diag.trusted_secret_ok") if trusted_enabled and auth.trusted_header_secret else tr("auth_diag.trusted_secret_bad"),
        ),
        _auth_status(
            tr("auth_diag.trusted_subject"),
            (not trusted_enabled) or bool((auth.trusted_header_user_header or "").strip()),
            tr("auth_diag.trusted_subject_detail", header=(auth.trusted_header_user_header or "").strip() or tr("auth_diag.not_set")),
        ),
        _auth_status(
            tr("auth_diag.trusted_admin_groups"),
            (not trusted_enabled) or bool((auth.trusted_header_admin_groups or "").strip()),
            tr("auth_diag.admin_groups_ok") if auth.trusted_header_admin_groups else tr("auth_diag.trusted_admin_groups_bad"),
        ),
        _auth_status(
            tr("auth_diag.oidc_issuer"),
            (not oidc_enabled) or bool(effective_discovery_url),
            effective_discovery_url or tr("auth_diag.oidc_issuer_bad"),
        ),
        _auth_status(
            tr("auth_diag.oidc_transport"),
            (not oidc_enabled) or oidc_urls_secure,
            tr("auth_diag.oidc_transport_ok") if oidc_urls_secure else tr("auth_diag.oidc_transport_bad"),
        ),
        _auth_status(
            tr("auth_diag.oidc_credentials"),
            (not oidc_enabled) or (bool(auth.oidc_client_id) and bool(auth.oidc_client_secret)),
            tr("auth_diag.oidc_credentials_ok") if auth.oidc_client_id and auth.oidc_client_secret else tr("auth_diag.oidc_credentials_bad"),
        ),
        _auth_status(
            tr("auth_diag.oidc_scopes"),
            (not oidc_enabled) or ("openid" in oidc_scopes),
            auth.oidc_scopes or tr("auth_diag.not_set"),
        ),
        _auth_status(
            tr("auth_diag.oidc_token_method"),
            (not oidc_enabled) or token_method_ok,
            auth.oidc_token_auth_method or tr("auth_diag.not_set"),
        ),
        _auth_status(
            tr("auth_diag.oidc_admin_groups"),
            (not oidc_enabled) or bool((auth.oidc_admin_groups or "").strip()),
            tr("auth_diag.admin_groups_ok") if auth.oidc_admin_groups else tr("auth_diag.oidc_admin_groups_bad"),
        ),
    ]

    modes = [
        {"label": tr("auth_diag.mode_local"), "enabled": local_enabled, "detail": tr("auth_diag.mode_local_detail")},
        {
            "label": tr("auth_diag.mode_trusted"),
            "enabled": trusted_enabled,
            "detail": tr("auth_diag.mode_trusted_detail"),
        },
        {
            "label": tr("auth_diag.mode_oidc"),
            "enabled": oidc_enabled,
            "detail": tr("auth_diag.mode_oidc_detail"),
        },
    ]
    return {
        "modes": modes,
        "checks": checks,
        "trusted": {
            "provider": auth.trusted_header_provider,
            "subject_header": auth.trusted_header_user_header,
            "groups_header": auth.trusted_header_groups_header,
            "auto_provision": bool(auth.trusted_header_auto_provision),
        },
        "oidc": {
            "provider": auth.oidc_provider,
            "issuer": auth.oidc_issuer,
            "discovery_url": auth.oidc_discovery_url,
            "redirect_path": auth.oidc_redirect_path,
            "scopes": auth.oidc_scopes,
            "email_claim": auth.oidc_email_claim,
            "name_claim": auth.oidc_name_claim,
            "groups_claim": auth.oidc_groups_claim,
            "auto_provision": bool(auth.oidc_auto_provision),
            "allowed_algorithms": auth.oidc_allowed_algorithms,
        },
    }


@router.get("/admin/index", response_class=HTMLResponse)
def admin_index(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
    msg: str | None = None,
):
    """Render the Chroma index health page (admin only).

    ``msg`` is a small flash code passed via query string after a POST so we
    can show "Rebuilt" / "Cleared" without introducing a session-flash store.
    """
    status = vector_index_status()
    return render(request, "admin_index.html", {"user": user, "status": status, "msg": msg or ""})


@router.post("/admin/index/rebuild")
def admin_index_rebuild(request: Request, user: Annotated[dict, Depends(require_admin)]):
    """Run a full SQLite -> Chroma re-upsert (admin only)."""
    result = sync_from_sqlite(mode="full")
    record_audit_event(
        request,
        user,
        "index_rebuilt",
        "vector_index",
        None,
        {"upserted": result["upserted"], "deleted": result["deleted"]},
        "high",
    )
    logger.info("admin_index_rebuilt admin_user_id=%s upserted=%s deleted=%s", user["id"], result["upserted"], result["deleted"])
    return RedirectResponse(f"/admin/index?msg=rebuilt-{result['upserted']}", status_code=303)


@router.post("/admin/index/clear")
def admin_index_clear(request: Request, user: Annotated[dict, Depends(require_admin)]):
    """Delete every vector from the Chroma collection (admin only).

    SQLite data is untouched — a subsequent "Rebuild index" re-populates Chroma.
    """
    count = clear_all_vectors()
    record_audit_event(
        request,
        user,
        "index_cleared",
        "vector_index",
        None,
        {"deleted_vectors": count},
        "high",
    )
    logger.info("admin_index_cleared admin_user_id=%s deleted=%s", user["id"], count)
    return RedirectResponse(f"/admin/index?msg=cleared-{count}", status_code=303)


@router.get("/admin/audit", response_class=HTMLResponse)
def admin_audit(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
    action: str = "",
    actor: str = "",
    target_type: str = "",
    sensitivity: str = "",
    limit: int = 100,
):
    """Admin-visible durable audit event viewer."""
    action = action.strip()[:120]
    actor = actor.strip()[:120]
    target_type = target_type.strip()[:80]
    sensitivity = sensitivity.strip()[:40]
    limit = max(1, min(int(limit), 300))
    where = []
    params: list[Any] = []
    if action:
        where.append("action LIKE ?")
        params.append(f"%{action}%")
    if actor:
        where.append("(actor_username LIKE ? OR CAST(actor_user_id AS TEXT) = ?)")
        params.extend((f"%{actor}%", actor))
    if target_type:
        where.append("target_type = ?")
        params.append(target_type)
    if sensitivity:
        where.append("sensitivity = ?")
        params.append(sensitivity)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    with connect() as conn:
        events = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT *
                FROM audit_events
                {where_sql}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        ]
        actions = [row["action"] for row in conn.execute("SELECT DISTINCT action FROM audit_events ORDER BY action").fetchall()]
        target_types = [
            row["target_type"]
            for row in conn.execute("SELECT DISTINCT target_type FROM audit_events WHERE target_type != '' ORDER BY target_type").fetchall()
        ]
        sensitivities = [
            row["sensitivity"]
            for row in conn.execute("SELECT DISTINCT sensitivity FROM audit_events ORDER BY sensitivity").fetchall()
        ]
    for event in events:
        try:
            event["metadata"] = json.dumps(loads(event.get("metadata_json") or "{}"), ensure_ascii=False, indent=2)
        except Exception:
            event["metadata"] = "{}"
    return render(
        request,
        "admin_audit.html",
        {
            "user": user,
            "events": events,
            "filters": {
                "action": action,
                "actor": actor,
                "target_type": target_type,
                "sensitivity": sensitivity,
                "limit": limit,
            },
            "actions": actions,
            "target_types": target_types,
            "sensitivities": sensitivities,
        },
    )


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, user: Annotated[dict, Depends(require_admin)]):
    """List all user accounts (admin only)."""
    with connect() as conn:
        rows = _admin_user_rows(conn)
    return render(request, "admin_users.html", {"user": user, "users": rows, "error": "", "saved": False})


@router.get("/admin/auth", response_class=HTMLResponse)
def admin_auth(request: Request, user: Annotated[dict, Depends(require_admin)]):
    """Show current authentication modes and static SSO configuration checks."""
    return render(request, "admin_auth.html", {"user": user, "diagnostics": _auth_diagnostics()})


@router.post("/admin/users/new")
def admin_create_user(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
    username: str = Form(...),
    password: str = Form(...),
    is_admin: str = Form(""),
):
    """Create a new user account."""
    username = username.strip()
    error = ""
    if not username or len(username) > 64:
        error = "帳號長度須為 1–64 個字元。"
    elif len(password) < 6:
        error = "密碼至少需要 6 個字元。"
    created_id = None
    if not error:
        try:
            with connect() as conn:
                cursor = conn.execute(
                    "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
                    (username, hash_password(password), 1 if is_admin else 0),
                )
                created_id = cursor.lastrowid
        except Exception as exc:
            error = f"建立使用者失敗：{exc}"
    with connect() as conn:
        rows = _admin_user_rows(conn)
    if error:
        return render(request, "admin_users.html", {"user": user, "users": rows, "error": error, "saved": False}, 400)
    record_audit_event(
        request,
        user,
        "user_created",
        "user",
        created_id,
        {"username": username, "is_admin": bool(is_admin)},
        "high",
    )
    logger.info("user_created admin_user_id=%s new_username=%s is_admin=%s", user["id"], username, bool(is_admin))
    return render(request, "admin_users.html", {"user": user, "users": rows, "error": "", "saved": True})


@router.post("/admin/users/{target_id}/reset-password")
def admin_reset_password(
    request: Request,
    target_id: int,
    user: Annotated[dict, Depends(require_admin)],
    new_password: str = Form(...),
):
    """Reset another user's password to a new value."""
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="密碼至少需要 6 個字元。")
    with connect() as conn:
        target = conn.execute(
            """
            SELECT u.username, COUNT(ei.id) AS external_identity_count
              FROM users u
              LEFT JOIN external_identities ei ON ei.user_id = u.id
             WHERE u.id = ?
             GROUP BY u.id, u.username
            """,
            (target_id,),
        ).fetchone()
        if target is None:
            raise HTTPException(status_code=404, detail="User not found")
        if target["external_identity_count"]:
            raise HTTPException(status_code=400, detail=i18n.t("auth.password_reset_sso_blocked"))
        result = conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), target_id),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="User not found")
    record_audit_event(
        request,
        user,
        "user_password_reset",
        "user",
        target_id,
        {"target_username": target["username"]},
        "high",
    )
    logger.info("password_reset admin_user_id=%s target_user_id=%s", user["id"], target_id)
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{target_id}/toggle-admin")
def admin_toggle_admin(
    request: Request,
    target_id: int,
    user: Annotated[dict, Depends(require_admin)],
):
    """Flip the is_admin flag for another user. Refuses to demote the last admin."""
    if target_id == user["id"]:
        raise HTTPException(status_code=400, detail="You cannot change your own admin flag.")
    with connect() as conn:
        target = conn.execute(
            """
            SELECT u.username, u.is_admin, COUNT(ei.id) AS external_identity_count
              FROM users u
              LEFT JOIN external_identities ei ON ei.user_id = u.id
             WHERE u.id = ?
             GROUP BY u.id, u.username, u.is_admin
            """,
            (target_id,),
        ).fetchone()
        if target is None:
            raise HTTPException(status_code=404, detail="User not found")
        if target["external_identity_count"]:
            raise HTTPException(status_code=400, detail=i18n.t("auth.sso_role_managed"))
        new_flag = 0 if target["is_admin"] else 1
        if new_flag == 0:
            other_admins = conn.execute(
                "SELECT COUNT(*) c FROM users WHERE is_admin = 1 AND id != ?",
                (target_id,),
            ).fetchone()["c"]
            if other_admins == 0:
                raise HTTPException(status_code=400, detail="Cannot remove the last admin.")
        conn.execute("UPDATE users SET is_admin = ? WHERE id = ?", (new_flag, target_id))
    record_audit_event(
        request,
        user,
        "user_admin_toggled",
        "user",
        target_id,
        {"target_username": target["username"], "new_is_admin": bool(new_flag)},
        "high",
    )
    logger.info("admin_toggled admin_user_id=%s target_user_id=%s new_is_admin=%s", user["id"], target_id, new_flag)
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{target_id}/delete")
def admin_delete_user(
    request: Request,
    target_id: int,
    user: Annotated[dict, Depends(require_admin)],
):
    """Delete a user account (cascades notebooks/sources/conversations/notes/chunks)."""
    if target_id == user["id"]:
        raise HTTPException(status_code=400, detail="You cannot delete your own account.")
    with connect() as conn:
        target = conn.execute("SELECT username, is_admin FROM users WHERE id = ?", (target_id,)).fetchone()
        if target is None:
            raise HTTPException(status_code=404, detail="User not found")
        if target["is_admin"]:
            other_admins = conn.execute(
                "SELECT COUNT(*) c FROM users WHERE is_admin = 1 AND id != ?",
                (target_id,),
            ).fetchone()["c"]
            if other_admins == 0:
                raise HTTPException(status_code=400, detail="Cannot delete the last admin.")
        conn.execute("DELETE FROM users WHERE id = ?", (target_id,))
    record_audit_event(
        request,
        user,
        "user_deleted",
        "user",
        target_id,
        {"target_username": target["username"], "was_admin": bool(target["is_admin"])},
        "high",
    )
    logger.info("user_deleted admin_user_id=%s target_user_id=%s", user["id"], target_id)
    return RedirectResponse("/admin/users", status_code=303)
