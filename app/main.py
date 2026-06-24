import asyncio
import hmac
import json
import os
import re
import shutil
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
import time
import uuid
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs, quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from markupsafe import Markup, escape

from .config import config
from .db import UPLOAD_DIR, connect, dumps, init_db, load_llm_settings, loads
from .governance import record_ai_safety_events
from .ingest import supported
from .jobs import enqueue_source
from .worker import run_worker_loop
from . import i18n
import httpx

from .llm import ARTIFACT_PROMPTS, FOLLOWUPS_CACHE_VERSION, close_http_client, compare_sources, generate_answer, generate_answer_stream, generate_artifact, generate_briefing, generate_meeting_minutes, generate_starter_questions, set_http_client, suggest_followup_questions, translate_summary
# Used by main itself (chat/ask flow, lifespan, message rendering):
from .retrieval import (
    active_low_confidence_threshold,
    citation_payload,
    load_active_retrieval_profile,
    message_with_citations,
    retrieve,
)
# Re-exported only so the test suite can reach them as ``app.main.<name>`` (the
# retrieval engine now lives in app/retrieval.py); not used by main itself.
from .retrieval import (  # noqa: F401
    FINAL_CHUNK_COUNT,
    RETRIEVAL_PARAM_DEFAULTS,
    active_retrieval_params,
    current_retrieval_profile_params,
    diversify_candidates,
    fetch_candidate_rows,
    keyword_score,
    merge_candidates,
)
from .security import INSECURE_DEV_SECRET, get_app_secret, hash_password, new_csrf_token, sign_user_id, unsign_user_id, valid_csrf_token, verify_password
from .vector_store import delete_source as delete_source_vectors
from .vector_store import sync_from_sqlite


BASE_DIR = Path(__file__).resolve().parent
SECRET = get_app_secret()
LOG_LEVEL = os.environ.get("NOTEBOOKLM_LOG_LEVEL", "INFO").upper()
LOG_FILE = Path(os.environ.get("NOTEBOOKLM_LOG_FILE", BASE_DIR.parent / "logs" / "app.log"))
LOG_MAX_BYTES = int(os.environ.get("NOTEBOOKLM_LOG_MAX_BYTES", str(5 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.environ.get("NOTEBOOKLM_LOG_BACKUP_COUNT", "5"))
LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
# Run the ingest worker inside the web process (P1-1). On by default so a lone
# `uvicorn app.main:app` ingests as before; set to 0 in deployments that run a
# dedicated `python -m app.worker` to keep ingest off the web process.
INLINE_WORKER = os.environ.get("NOTEBOOKLM_INLINE_WORKER", "1").strip().lower() in {"1", "true", "yes", "on"}
CSRF_COOKIE_NAME = "csrf_token"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER_NAME = "x-csrf-token"
CSRF_COOKIE_MAX_AGE = 60 * 60 * 24 * 7
CSRF_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def configure_logging() -> None:
    """Configure console and rotating file logging for the application."""
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    formatter = logging.Formatter(LOG_FORMAT)
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    if not any(getattr(handler, "_notebooklm_console", False) for handler in root_logger.handlers):
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(level)
        console_handler._notebooklm_console = True
        root_logger.addHandler(console_handler)

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_file = LOG_FILE.resolve()
    if not any(getattr(handler, "_notebooklm_log_file", None) == log_file for handler in root_logger.handlers):
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        file_handler._notebooklm_log_file = log_file
        root_logger.addHandler(file_handler)


configure_logging()
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialise shared resources on startup and tear them down on shutdown.

    Replaces the deprecated @app.on_event hooks with the modern lifespan
    context manager (FastAPI 0.93+).
    """
    init_db()
    load_active_retrieval_profile()
    set_http_client(httpx.AsyncClient(timeout=None))
    sync_from_sqlite()
    # Inline ingest worker (P1-1): on by default so a single `uvicorn app.main:app`
    # keeps draining the ingest queue like before. Production sets
    # NOTEBOOKLM_INLINE_WORKER=0 and runs a dedicated `python -m app.worker`
    # process so PDF extraction/embedding stays off the web process.
    worker_task: asyncio.Task | None = None
    worker_stop: asyncio.Event | None = None
    if INLINE_WORKER:
        worker_stop = asyncio.Event()
        worker_task = asyncio.create_task(run_worker_loop(stop_event=worker_stop))
    logger.info(
        "app_started log_level=%s log_file=%s data_dir=%s inline_worker=%s",
        LOG_LEVEL, LOG_FILE, os.environ.get("NOTEBOOKLM_DATA_DIR", "data"), INLINE_WORKER,
    )
    try:
        yield
    finally:
        if worker_task is not None and worker_stop is not None:
            worker_stop.set()
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass
        await close_http_client()
        logger.info("app_stopped")


app = FastAPI(title="NotebookLM-like RAG POC", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# Human-readable (zh-Hant) labels for the source lifecycle status pill, shared
# by the left source list and the preview header so the two never drift.
templates.env.globals["source_status_labels"] = {
    "uploaded": "已上傳",
    "processing": "處理中",
    "indexed": "已索引",
    "failed": "失敗",
}

# i18n (Phase 0): `t()` resolves UI copy from the message catalog; `i18n_js()`
# feeds window.I18N in base.html so app.js shares the same source of truth.
templates.env.globals["t"] = i18n.t
templates.env.globals["i18n_js"] = i18n.js_messages
# Localised display for the audit sensitivity classification (raw value kept).
templates.env.globals["audit_sensitivity_labels"] = i18n.SENSITIVITY_LABELS
# Localised display for the eval run lifecycle status (raw value kept).
templates.env.globals["run_status_labels"] = i18n.RUN_STATUS_LABELS
# Localised display for per-question eval result status (raw value kept).
templates.env.globals["eval_result_status_labels"] = i18n.EVAL_RESULT_STATUS_LABELS


def csrf_token_for_request(request: Request) -> str:
    """Return the signed CSRF token for this request, creating one if needed."""
    token = getattr(request.state, "csrf_token", None)
    if token:
        return token
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    token = cookie_token if valid_csrf_token(cookie_token, SECRET) else new_csrf_token(SECRET)
    request.state.csrf_token = token
    return token


@pass_context
def csrf_input(context) -> Markup:
    """Render the hidden CSRF field used by non-JavaScript form submits."""
    request = context.get("request")
    token = csrf_token_for_request(request) if request is not None else ""
    return Markup(
        f'<input type="hidden" name="{CSRF_FORM_FIELD}" value="{escape(token)}">'
    )


templates.env.globals["csrf_input"] = csrf_input


def _csrf_response_token(request: Request) -> str:
    """Return the token that should be persisted to the CSRF cookie."""
    return csrf_token_for_request(request)


def _set_csrf_cookie(request: Request, response: Response) -> None:
    response.set_cookie(
        CSRF_COOKIE_NAME,
        _csrf_response_token(request),
        max_age=CSRF_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )


async def _submitted_csrf_token(request: Request) -> str | None:
    token = request.headers.get(CSRF_HEADER_NAME)
    if token:
        return token
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type == "application/x-www-form-urlencoded":
        body = await request.body()
        values = parse_qs(body.decode("utf-8", errors="ignore"), keep_blank_values=True)
        token_values = values.get(CSRF_FORM_FIELD)
        return token_values[0] if token_values else None
    if content_type == "multipart/form-data":
        body = await request.body()
        match = re.search(
            rb'name="' + re.escape(CSRF_FORM_FIELD.encode("ascii")) + rb'"\r?\n\r?\n([^\r\n]+)',
            body,
        )
        if match:
            return match.group(1).decode("ascii", errors="ignore")
    return None


@app.middleware("http")
async def csrf_protection(request: Request, call_next):
    """Reject unsafe requests unless they carry the page's signed CSRF token."""
    if request.method.upper() in CSRF_UNSAFE_METHODS:
        cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
        submitted_token = await _submitted_csrf_token(request)
        valid = (
            valid_csrf_token(cookie_token, SECRET)
            and submitted_token is not None
            and hmac.compare_digest(cookie_token, submitted_token)
        )
        if not valid:
            logger.warning("csrf_rejected method=%s path=%s", request.method, request.url.path)
            return HTMLResponse("CSRF token invalid", status_code=403)
        request.state.csrf_token = cookie_token
    else:
        csrf_token_for_request(request)

    response = await call_next(request)
    _set_csrf_cookie(request, response)
    return response


@app.middleware("http")
async def request_logger(request: Request, call_next):
    """Log each HTTP request with status and elapsed time."""
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.exception(
            "request_failed method=%s path=%s elapsed_ms=%.1f",
            request.method,
            request.url.path,
            elapsed_ms,
        )
        raise
    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "request_completed method=%s path=%s status=%s elapsed_ms=%.1f",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


def current_user(request: Request) -> dict:
    """Resolve the currently signed-in user from the session cookie."""
    user_id = unsign_user_id(request.cookies.get("session"), SECRET)
    if not user_id:
        raise HTTPException(status_code=401)
    with connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        raise HTTPException(status_code=401)
    return dict(user)


def require_login(request: Request) -> dict:
    """Require authentication and redirect anonymous users to login."""
    try:
        return current_user(request)
    except HTTPException:
        raise HTTPException(status_code=303, headers={"Location": "/login"})


def require_admin(user: Annotated[dict, Depends(require_login)]) -> dict:
    """Require the authenticated user to have administrator privileges."""
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="僅限管理員")
    return user


def render(request: Request, name: str, context: dict, status_code: int = 200) -> HTMLResponse:
    """Render a Jinja template with shared request context."""
    return templates.TemplateResponse(
        request,
        name,
        {
            "request": request,
            "csrf_token": csrf_token_for_request(request),
            "followups_cache_version": FOLLOWUPS_CACHE_VERSION,
            **context,
        },
        status_code=status_code,
    )


def friendly_error_message(exc: Exception | str, action: str | None = None) -> str:
    """Return a user-facing error without leaking provider/raw exception text."""
    action = action if action is not None else i18n.t("error.action_default")
    text = str(exc)
    if isinstance(exc, httpx.TimeoutException) or "timeout" in text.lower():
        return i18n.t("error.timeout", action=action)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 401:
            return i18n.t("error.auth")
        if status == 429:
            return i18n.t("error.ratelimit", action=action)
        if status >= 500:
            return i18n.t("error.unavailable")
        return i18n.t("error.generic_check", action=action)
    if isinstance(exc, RuntimeError) and "settings" in text.lower():
        return i18n.t("error.no_llm")
    return i18n.t("error.generic_retry", action=action)


def relative_time(value: str | None) -> str:
    """Small zh-TW relative timestamp for compact conversation lists."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value[:10]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return "剛剛"
    if seconds < 3600:
        return f"{seconds // 60} 分鐘前"
    if seconds < 86400:
        return f"{seconds // 3600} 小時前"
    if seconds < 604800:
        return f"{seconds // 86400} 天前"
    return value[:10]


def sql_like_escape(value: str) -> str:
    """Escape user text for a LIKE query using backslash as ESCAPE."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Convert application HTTP exceptions into redirects or error pages."""
    if exc.status_code == 303:
        return RedirectResponse(str(exc.headers["Location"]), status_code=303)
    if exc.status_code == 401:
        return RedirectResponse("/login", status_code=303)
    return render(request, "error.html", {"status_code": exc.status_code, "detail": exc.detail}, exc.status_code)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    """Send signed-in users to the notebook grid; everyone else to login."""
    user_id = unsign_user_id(request.cookies.get("session"), SECRET)
    return RedirectResponse("/notebooks" if user_id else "/login", status_code=303)


@app.get("/sources")
@app.get("/chat")
def legacy_redirect():
    """Redirect legacy bookmarks to the notebook home."""
    return RedirectResponse("/notebooks", status_code=303)


@app.get("/search", response_class=HTMLResponse)
def global_search(
    request: Request,
    user: Annotated[dict, Depends(require_login)],
    q: str = "",
):
    """Search the current user's notebooks, sources, notes, and conversation titles."""
    SEARCH_SECTION_LIMIT = 12
    query = " ".join((q or "").split())[:120]
    results: list[dict[str, str]] = []
    results_truncated = False
    if query:
        like = f"%{sql_like_escape(query)}%"
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT id, title, description, emoji, updated_at
                FROM notebooks
                WHERE user_id = ? AND (title LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\')
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (user["id"], like, like, SEARCH_SECTION_LIMIT + 1),
            ).fetchall()
            results_truncated = results_truncated or len(rows) > SEARCH_SECTION_LIMIT
            for row in rows[:SEARCH_SECTION_LIMIT]:
                results.append({
                    "type": "筆記本",
                    "title": f"{row['emoji'] or '📓'} {row['title']}",
                    "snippet": row["description"] or "開啟筆記本",
                    "url": f"/notebooks/{row['id']}",
                    "time": relative_time(row["updated_at"]),
                })
            rows = conn.execute(
                """
                SELECT s.id, s.notebook_id, s.filename, s.summary, s.updated_at, n.title AS notebook_title
                FROM sources s JOIN notebooks n ON n.id = s.notebook_id
                WHERE s.user_id = ? AND s.notebook_id IS NOT NULL
                  AND (s.filename LIKE ? ESCAPE '\\' OR s.summary LIKE ? ESCAPE '\\')
                ORDER BY s.updated_at DESC
                LIMIT ?
                """,
                (user["id"], like, like, SEARCH_SECTION_LIMIT + 1),
            ).fetchall()
            results_truncated = results_truncated or len(rows) > SEARCH_SECTION_LIMIT
            for row in rows[:SEARCH_SECTION_LIMIT]:
                results.append({
                    "type": "來源",
                    "title": row["filename"],
                    "snippet": row["summary"] or f"位於「{row['notebook_title']}」",
                    "url": f"/notebooks/{row['notebook_id']}",
                    "time": relative_time(row["updated_at"]),
                })
            rows = conn.execute(
                """
                SELECT c.id, c.notebook_id, c.title, c.updated_at, n.title AS notebook_title,
                    (SELECT COUNT(*) FROM messages WHERE conversation_id = c.id AND user_id = c.user_id) AS message_count
                FROM conversations c JOIN notebooks n ON n.id = c.notebook_id
                WHERE c.user_id = ? AND c.title LIKE ? ESCAPE '\\'
                ORDER BY c.updated_at DESC
                LIMIT ?
                """,
                (user["id"], like, SEARCH_SECTION_LIMIT + 1),
            ).fetchall()
            results_truncated = results_truncated or len(rows) > SEARCH_SECTION_LIMIT
            for row in rows[:SEARCH_SECTION_LIMIT]:
                results.append({
                    "type": "對話",
                    "title": row["title"],
                    "snippet": f"位於「{row['notebook_title']}」 · {row['message_count']} 則訊息",
                    "url": f"/notebooks/{row['notebook_id']}?conversation_id={row['id']}",
                    "time": relative_time(row["updated_at"]),
                })
            rows = conn.execute(
                """
                SELECT notes.id, notes.notebook_id, notes.title, notes.content, notes.updated_at, notebooks.title AS notebook_title
                FROM notes JOIN notebooks ON notebooks.id = notes.notebook_id
                WHERE notes.user_id = ? AND (notes.title LIKE ? ESCAPE '\\' OR notes.content LIKE ? ESCAPE '\\')
                ORDER BY notes.updated_at DESC
                LIMIT ?
                """,
                (user["id"], like, like, SEARCH_SECTION_LIMIT + 1),
            ).fetchall()
            results_truncated = results_truncated or len(rows) > SEARCH_SECTION_LIMIT
            for row in rows[:SEARCH_SECTION_LIMIT]:
                content = " ".join((row["content"] or "").split())
                results.append({
                    "type": "筆記",
                    "title": row["title"] or content[:40] or "未命名筆記",
                    "snippet": content[:140] or f"位於「{row['notebook_title']}」",
                    "url": f"/notebooks/{row['notebook_id']}",
                    "time": relative_time(row["updated_at"]),
                })
    return render(
        request,
        "search.html",
        {
            "user": user,
            "query": query,
            "results": results,
            "results_truncated": results_truncated,
            "search_section_limit": SEARCH_SECTION_LIMIT,
        },
    )


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    """Render the login form.

    The demo-account hint is shown only when running with the insecure dev
    secret, so a real (network-exposed) deployment never prints credentials.
    """
    return render(request, "login.html", {"error": "", "demo_hint": SECRET == INSECURE_DEV_SECRET})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    """Authenticate a username and password and issue a session cookie."""
    with connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not user or not verify_password(password, user["password_hash"]):
        logger.warning("login_failed username=%s", username)
        return render(request, "login.html", {"error": "帳號或密碼錯誤。"}, 400)
    redirect = RedirectResponse("/notebooks", status_code=303)
    redirect.set_cookie("session", sign_user_id(user["id"], SECRET), httponly=True, samesite="lax")
    logger.info("login_succeeded user_id=%s username=%s", user["id"], username)
    return redirect


@app.post("/logout")
def logout():
    """Clear the session cookie and return to the login page."""
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("session")
    logger.info("logout")
    return response


SUGGESTIONS_TTL_HOURS = config.runtime.suggestions_ttl_hours
BRIEFING_TTL_HOURS = config.runtime.briefing_ttl_hours

# Cross-process lock for briefing generation, backed by the ``briefing_locks``
# SQLite table (one row per in-flight notebook, value is the unix timestamp when
# generation started). Used to dedupe concurrent POSTs that fire when multiple
# sources finish indexing within seconds of each other during a multi-file
# upload (each fires indexed-sources-changed → auto-fire POST). A row older than
# BRIEFING_LOCK_TIMEOUT_S is treated as released so a crashed request can't
# permanently block regeneration.
# Unlike the old in-process dict, the SQLite row is shared across uvicorn
# workers, so the app can run multiple workers without two of them each holding
# an independent lock (P2-3).
BRIEFING_LOCK_TIMEOUT_S = config.runtime.briefing_lock_timeout_s


def _briefing_locked(notebook_id: int) -> bool:
    """True if a non-stale briefing generation is currently in flight."""
    with connect() as conn:
        row = conn.execute(
            "SELECT acquired_at FROM briefing_locks WHERE notebook_id = ?",
            (notebook_id,),
        ).fetchone()
        if row is None:
            return False
        if time.time() - row["acquired_at"] > BRIEFING_LOCK_TIMEOUT_S:
            # Stale (crashed holder) — reclaim opportunistically and report free.
            conn.execute("DELETE FROM briefing_locks WHERE notebook_id = ?", (notebook_id,))
            return False
        return True


def _acquire_briefing_lock(notebook_id: int) -> bool:
    """Try to take the briefing lock. Returns True if acquired, False if busy.

    The check-and-set runs inside a ``BEGIN IMMEDIATE`` write transaction so two
    workers can't both observe "free" and both acquire. ``busy_timeout`` (set in
    ``connect()``) makes a competing writer wait rather than error.
    """
    now = time.time()
    conn = connect()
    try:
        conn.isolation_level = None  # take manual control of the transaction
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT acquired_at FROM briefing_locks WHERE notebook_id = ?",
            (notebook_id,),
        ).fetchone()
        if row is not None and now - row["acquired_at"] <= BRIEFING_LOCK_TIMEOUT_S:
            conn.execute("ROLLBACK")
            return False
        conn.execute(
            "INSERT INTO briefing_locks (notebook_id, acquired_at) VALUES (?, ?) "
            "ON CONFLICT(notebook_id) DO UPDATE SET acquired_at = excluded.acquired_at",
            (notebook_id, now),
        )
        conn.execute("COMMIT")
        return True
    finally:
        conn.close()


def _release_briefing_lock(notebook_id: int) -> None:
    """Release the briefing lock. Safe to call when not held."""
    with connect() as conn:
        conn.execute("DELETE FROM briefing_locks WHERE notebook_id = ?", (notebook_id,))


def _sqlite_utc_timestamp(value: str) -> datetime:
    """Parse SQLite CURRENT_TIMESTAMP strings as timezone-aware UTC datetimes."""
    dt = datetime.fromisoformat(value.replace(" ", "T"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _cached_suggestions(notebook: dict) -> list[str]:
    """Return cached suggestions if within TTL, else empty list."""
    raw = (notebook.get("suggestions_json") or "").strip()
    saved_at = (notebook.get("suggestions_at") or "").strip()
    if not raw or not saved_at:
        return []
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=SUGGESTIONS_TTL_HOURS)
        if _sqlite_utc_timestamp(saved_at) > cutoff:
            return loads(raw)
    except Exception:
        pass
    return []


def _cached_briefing(notebook: dict) -> str:
    """Return cached briefing markdown if within TTL, else empty string."""
    raw = (notebook.get("briefing") or "").strip()
    saved_at = (notebook.get("briefing_at") or "").strip()
    if not raw or not saved_at:
        return ""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=BRIEFING_TTL_HOURS)
        if _sqlite_utc_timestamp(saved_at) > cutoff:
            return raw
    except Exception:
        pass
    return ""


def get_notebook(conn, notebook_id: int, user_id: int) -> dict:
    """Fetch a notebook owned by the user, raising 404 otherwise."""
    row = conn.execute(
        "SELECT * FROM notebooks WHERE id = ? AND user_id = ?",
        (notebook_id, user_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="找不到筆記本")
    return dict(row)


def touch_notebook(conn, notebook_id: int) -> None:
    """Bump a notebook's updated_at so it bubbles to the top of the home grid."""
    conn.execute(
        "UPDATE notebooks SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (notebook_id,),
    )


def llm_settings_status(conn) -> dict:
    """Return a small flag set describing whether the LLM provider is usable.

    Used by routes that should refuse to ingest (no embedding API => useless
    chunks via the local fallback hash) and by templates that warn the user.
    """
    settings = load_llm_settings(conn) or {}
    # API key is optional: local services (e5, Ollama, vLLM, TEI) accept
    # requests without one. Readiness now hinges on having both models
    # configured; the key, when present, is sent as a bearer/api-key header.
    has_api_key = bool(settings.get("api_key"))
    has_embedding_api_key = bool(settings.get("embedding_api_key"))
    has_chat_model = bool(settings.get("chat_model"))
    has_embedding_model = bool(settings.get("embedding_model"))
    return {
        "ready": has_chat_model and has_embedding_model,
        "has_api_key": has_api_key,
        "has_embedding_api_key": has_embedding_api_key,
        "has_chat_model": has_chat_model,
        "has_embedding_model": has_embedding_model,
    }


def cleanup_source_artifacts(source: dict) -> None:
    """Best-effort removal of a source's on-disk file and Chroma vectors.

    Used by both ``delete_source`` and ``delete_notebook``. Vector deletion
    is wrapped in try/except because failing here would orphan the SQLite
    state the caller has already committed.
    """
    Path(source["stored_path"]).unlink(missing_ok=True)
    try:
        delete_source_vectors(source["id"], source["user_id"])
    except Exception:
        logger.exception("vector_source_delete_failed source_id=%s user_id=%s", source["id"], source["user_id"])


@app.get("/notebooks", response_class=HTMLResponse)
def list_notebooks(request: Request, user: Annotated[dict, Depends(require_login)]):
    """Render the notebook grid for the current user."""
    NOTEBOOKS_LIMIT = 100
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT n.*,
                (SELECT COUNT(*) FROM sources WHERE notebook_id = n.id) AS source_count,
                (SELECT COUNT(*) FROM conversations WHERE notebook_id = n.id) AS conversation_count
            FROM notebooks n
            WHERE n.user_id = ?
            ORDER BY n.updated_at DESC, n.id DESC
            LIMIT ?
            """,
            (user["id"], NOTEBOOKS_LIMIT + 1),
        ).fetchall()
    notebooks_truncated = len(rows) > NOTEBOOKS_LIMIT
    notebooks = [dict(row) for row in rows[:NOTEBOOKS_LIMIT]]
    return render(
        request,
        "home.html",
        {
            "user": user,
            "notebooks": notebooks,
            "notebooks_truncated": notebooks_truncated,
            "notebooks_limit": NOTEBOOKS_LIMIT,
        },
    )


@app.post("/notebooks/new")
def create_notebook(
    request: Request,
    user: Annotated[dict, Depends(require_login)],
    title: str = Form("未命名筆記本"),
    emoji: str = Form("📓"),
    description: str = Form(""),
):
    """Create a notebook and redirect into its workspace."""
    title = title.strip() or "未命名筆記本"
    description = description.strip()[:280]
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO notebooks (user_id, title, emoji, description) VALUES (?, ?, ?, ?)",
            (user["id"], title, (emoji or "📓").strip()[:8] or "📓", description),
        )
        notebook_id = cursor.lastrowid
    record_audit_event(
        request,
        user,
        "notebook_created",
        "notebook",
        notebook_id,
        {"title": title, "has_description": bool(description)},
        "normal",
    )
    logger.info("notebook_created user_id=%s notebook_id=%s title=%r", user["id"], notebook_id, title)
    return RedirectResponse(f"/notebooks/{notebook_id}", status_code=303)


@app.get("/notebooks/{notebook_id}", response_class=HTMLResponse)
def notebook_view(
    request: Request,
    notebook_id: int,
    user: Annotated[dict, Depends(require_login)],
    conversation_id: int | None = None,
):
    """Render the three-pane notebook workspace: sources, chat, studio."""
    # Defensive POC caps: rather than paginate, we fetch LIMIT+1 rows and
    # treat overflow as "show a truncation hint". Avoids a separate COUNT(*).
    SOURCES_LIMIT = 200
    CONVERSATIONS_LIMIT = 50
    MESSAGES_LIMIT = 200
    NOTES_LIMIT = 50

    def fetch_capped(conn, sql, params, limit):
        """Run sql with LIMIT+1, return (rows[:limit], truncated_bool)."""
        rows = [dict(r) for r in conn.execute(sql + " LIMIT ?", (*params, limit + 1)).fetchall()]
        return rows[:limit], len(rows) > limit

    with connect() as conn:
        notebook = get_notebook(conn, notebook_id, user["id"])
        llm_status = llm_settings_status(conn)
        sources, sources_truncated = fetch_capped(
            conn,
            "SELECT * FROM sources WHERE notebook_id = ? AND user_id = ? ORDER BY created_at DESC",
            (notebook_id, user["id"]), SOURCES_LIMIT,
        )
        conversations, conversations_truncated = fetch_capped(
            conn,
            """
            SELECT c.*,
                (SELECT COUNT(*) FROM messages WHERE conversation_id = c.id AND user_id = c.user_id) AS message_count
            FROM conversations c
            WHERE c.notebook_id = ? AND c.user_id = ?
            ORDER BY c.updated_at DESC, c.id DESC
            """,
            (notebook_id, user["id"]), CONVERSATIONS_LIMIT,
        )
        for item in conversations:
            item["relative_updated_at"] = relative_time(item.get("updated_at"))
        if conversation_id is None and conversations:
            conversation_id = conversations[0]["id"]
        conversation = None
        messages: list[dict] = []
        messages_truncated = False
        if conversation_id is not None:
            convo_row = conn.execute(
                "SELECT * FROM conversations WHERE id = ? AND notebook_id = ? AND user_id = ?",
                (conversation_id, notebook_id, user["id"]),
            ).fetchone()
            if convo_row is None:
                raise HTTPException(status_code=404, detail="找不到對話")
            conversation = dict(convo_row)
            recent, messages_truncated = fetch_capped(
                conn,
                "SELECT * FROM messages WHERE conversation_id = ? AND user_id = ? ORDER BY created_at DESC, id DESC",
                (conversation_id, user["id"]), MESSAGES_LIMIT,
            )
            messages = [message_with_citations(row) for row in reversed(recent)]
        notes, notes_truncated = fetch_capped(
            conn,
            "SELECT * FROM notes WHERE notebook_id = ? AND user_id = ? ORDER BY created_at DESC",
            (notebook_id, user["id"]), NOTES_LIMIT,
        )
    pinned_message_ids = {n["source_message_id"] for n in notes if n["source_message_id"] is not None}
    indexed_sources = [s for s in sources if s["status"] == "indexed"]
    cached_suggestions = _cached_suggestions(notebook)
    cached_briefing = _cached_briefing(notebook)
    return render(
        request,
        "notebook.html",
        {
            "user": user,
            "notebook": notebook,
            "llm_status": llm_status,
            "sources": sources,
            "indexed_sources": indexed_sources,
            "conversations": conversations,
            "conversation": conversation,
            "messages": messages,
            "notes": notes,
            "pinned_message_ids": pinned_message_ids,
            "sources_truncated": sources_truncated,
            "conversations_truncated": conversations_truncated,
            "messages_truncated": messages_truncated,
            "notes_truncated": notes_truncated,
            "cached_suggestions": cached_suggestions,
            "cached_briefing": cached_briefing,
            "upload_batch_limit": UPLOAD_BATCH_LIMIT,
            "error": "",
            "wide": True,
            "breadcrumb": notebook["title"],
        },
    )


@app.post("/notebooks/{notebook_id}/rename")
def rename_notebook(
    request: Request,
    notebook_id: int,
    user: Annotated[dict, Depends(require_login)],
    title: str = Form(...),
    emoji: str = Form(""),
    description: str = Form(""),
    followups_enabled: str | None = Form(None),
    followups_setting_present: str | None = Form(None),
):
    """Update a notebook's title, emoji, description, and UI settings."""
    title = title.strip() or "未命名筆記本"
    emoji = (emoji or "").strip()[:8]
    description = description.strip()[:280]
    with connect() as conn:
        notebook = get_notebook(conn, notebook_id, user["id"])
        followups_flag = (
            1 if followups_enabled == "1" else 0
        ) if followups_setting_present is not None else notebook["followups_enabled"]
        conn.execute(
            "UPDATE notebooks SET title = ?, emoji = ?, description = ?, followups_enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
            (title, emoji, description, followups_flag, notebook_id, user["id"]),
        )
    record_audit_event(
        request,
        user,
        "notebook_renamed",
        "notebook",
        notebook_id,
        {"title": title, "has_description": bool(description), "followups_enabled": bool(followups_flag)},
        "normal",
    )
    logger.info("notebook_renamed user_id=%s notebook_id=%s", user["id"], notebook_id)
    return RedirectResponse(f"/notebooks/{notebook_id}", status_code=303)


@app.post("/notebooks/{notebook_id}/delete")
def delete_notebook(request: Request, notebook_id: int, user: Annotated[dict, Depends(require_login)]):
    """Delete a notebook and cascade its sources, chunks, conversations, and notes."""
    with connect() as conn:
        notebook = get_notebook(conn, notebook_id, user["id"])
        sources = [
            {"id": row["id"], "stored_path": row["stored_path"], "user_id": user["id"]}
            for row in conn.execute(
                "SELECT id, stored_path FROM sources WHERE notebook_id = ? AND user_id = ?",
                (notebook_id, user["id"]),
            ).fetchall()
        ]
        conn.execute("DELETE FROM notebooks WHERE id = ? AND user_id = ?", (notebook_id, user["id"]))
    for source in sources:
        cleanup_source_artifacts(source)
    record_audit_event(
        request,
        user,
        "notebook_deleted",
        "notebook",
        notebook_id,
        {"title": notebook["title"], "source_count": len(sources)},
        "high",
    )
    logger.info("notebook_deleted user_id=%s notebook_id=%s sources=%s", user["id"], notebook_id, len(sources))
    return RedirectResponse("/notebooks", status_code=303)


UPLOAD_BATCH_LIMIT = config.runtime.upload_batch_limit


@app.post("/notebooks/{notebook_id}/sources/upload")
async def upload_source(
    request: Request,
    notebook_id: int,
    user: Annotated[dict, Depends(require_login)],
    files: list[UploadFile] = File(...),
):
    """Store up to UPLOAD_BATCH_LIMIT uploaded sources and schedule background ingestion for each."""
    with connect() as conn:
        get_notebook(conn, notebook_id, user["id"])
        llm_status = llm_settings_status(conn)
    if not llm_status["ready"]:
        # The local hash-based embedding fallback "works" but produces useless
        # vectors — refusing here prevents users from indexing then wondering
        # why retrieval is terrible.
        logger.warning("source_upload_rejected user_id=%s notebook_id=%s reason=llm_not_configured", user["id"], notebook_id)
        raise HTTPException(
            status_code=400,
            detail="尚未完成 LLM 設定。請管理員先到 /settings 設定 embedding 模型與聊天模型，才能索引來源。",
        )
    if not files:
        raise HTTPException(status_code=400, detail="尚未選擇檔案。")
    if len(files) > UPLOAD_BATCH_LIMIT:
        raise HTTPException(status_code=400, detail=f"一次最多上傳 {UPLOAD_BATCH_LIMIT} 個檔案。")
    for upload in files:
        if not upload.filename or not supported(upload.filename):
            logger.warning("source_upload_rejected user_id=%s notebook_id=%s filename=%s", user["id"], notebook_id, upload.filename)
            raise HTTPException(status_code=400, detail=f"不支援的檔案格式：{upload.filename or '(未命名)'}")

    user_dir = UPLOAD_DIR / str(user["id"])
    user_dir.mkdir(parents=True, exist_ok=True)

    queued_source_ids: list[int] = []
    for upload in files:
        safe_name = Path(upload.filename).name
        stored_path = user_dir / f"{uuid.uuid4().hex}_{safe_name}"
        with stored_path.open("wb") as out:
            shutil.copyfileobj(upload.file, out)
        with connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO sources (user_id, notebook_id, filename, stored_path, content_type, status)
                VALUES (?, ?, ?, ?, ?, 'uploaded')
                """,
                (user["id"], notebook_id, safe_name, str(stored_path), upload.content_type or ""),
            )
            source_id = cursor.lastrowid
            touch_notebook(conn, notebook_id)
        enqueue_source(source_id)
        queued_source_ids.append(source_id)
        record_audit_event(
            request,
            user,
            "source_uploaded",
            "source",
            source_id,
            {
                "notebook_id": notebook_id,
                "filename": safe_name,
                "content_type": upload.content_type or "",
                "batch_size": len(files),
            },
            "normal",
        )
        logger.info(
            "source_uploaded user_id=%s notebook_id=%s source_id=%s filename=%s content_type=%s",
            user["id"], notebook_id, source_id, safe_name, upload.content_type or "",
        )
    logger.info(
        "source_upload_batch_completed user_id=%s notebook_id=%s files=%s",
        user["id"], notebook_id, len(queued_source_ids),
    )
    return RedirectResponse(f"/notebooks/{notebook_id}", status_code=303)


@app.post("/notebooks/{notebook_id}/sources/{source_id}/reindex")
def reindex_source(
    request: Request,
    notebook_id: int,
    source_id: int,
    user: Annotated[dict, Depends(require_login)],
):
    """Schedule reindexing for a source within a specific notebook."""
    with connect() as conn:
        get_notebook(conn, notebook_id, user["id"])
        source = conn.execute(
            "SELECT id, filename FROM sources WHERE id = ? AND notebook_id = ? AND user_id = ?",
            (source_id, notebook_id, user["id"]),
        ).fetchone()
        if source is None:
            logger.warning("source_reindex_missing user_id=%s notebook_id=%s source_id=%s", user["id"], notebook_id, source_id)
            raise HTTPException(status_code=404, detail="找不到來源")
        touch_notebook(conn, notebook_id)
    enqueue_source(source_id)
    record_audit_event(
        request,
        user,
        "source_reindex_requested",
        "source",
        source_id,
        {"notebook_id": notebook_id, "filename": source["filename"]},
        "high",
    )
    logger.info("source_reindex_requested user_id=%s notebook_id=%s source_id=%s", user["id"], notebook_id, source_id)
    return RedirectResponse(f"/notebooks/{notebook_id}", status_code=303)


@app.post("/notebooks/{notebook_id}/sources/{source_id}/delete")
def delete_source(request: Request, notebook_id: int, source_id: int, user: Annotated[dict, Depends(require_login)]):
    """Delete a source within a specific notebook and clean up vectors and the file."""
    with connect() as conn:
        get_notebook(conn, notebook_id, user["id"])
        source = conn.execute(
            "SELECT * FROM sources WHERE id = ? AND notebook_id = ? AND user_id = ?",
            (source_id, notebook_id, user["id"]),
        ).fetchone()
        if source is None:
            logger.warning("source_delete_missing user_id=%s notebook_id=%s source_id=%s", user["id"], notebook_id, source_id)
            raise HTTPException(status_code=404, detail="找不到來源")
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        touch_notebook(conn, notebook_id)
    cleanup_source_artifacts({"id": source_id, "stored_path": source["stored_path"], "user_id": user["id"]})
    record_audit_event(
        request,
        user,
        "source_deleted",
        "source",
        source_id,
        {"notebook_id": notebook_id, "filename": source["filename"], "status": source["status"]},
        "high",
    )
    logger.info("source_deleted user_id=%s notebook_id=%s source_id=%s filename=%s", user["id"], notebook_id, source_id, source["filename"])
    return RedirectResponse(f"/notebooks/{notebook_id}", status_code=303)


@app.get("/notebooks/{notebook_id}/sources/{source_id}/_partial", response_class=HTMLResponse)
def source_partial(
    request: Request,
    notebook_id: int,
    source_id: int,
    user: Annotated[dict, Depends(require_login)],
):
    """Return a single source list item HTML fragment (used by HTMX polling).

    When the source has reached a final state, sets HX-Trigger so the chat
    source picker auto-refreshes without the user needing to reload.
    """
    with connect() as conn:
        notebook = get_notebook(conn, notebook_id, user["id"])
        row = conn.execute(
            "SELECT * FROM sources WHERE id = ? AND notebook_id = ? AND user_id = ?",
            (source_id, notebook_id, user["id"]),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="找不到來源")
    response = render(request, "_source_item.html", {"notebook": notebook, "source": dict(row)})
    # Keep fast source-row synchronization separate from Studio refreshes.
    # Processing polls are frequent; if the Studio sections also listen to
    # them they re-render every 2s during embedding, even when no useful
    # Studio data changed.
    status = dict(row).get("status")
    if status == "processing":
        response.headers["HX-Trigger"] = "source-status-changed"
    elif status in ("indexed", "failed"):
        response.headers["HX-Trigger"] = "source-status-changed, indexed-sources-changed"
    return response


@app.get("/notebooks/{notebook_id}/sources/{source_id}/preview", response_class=HTMLResponse)
def source_preview(
    request: Request,
    notebook_id: int,
    source_id: int,
    user: Annotated[dict, Depends(require_login)],
):
    """Return an HTML fragment listing every chunk that belongs to one source.

    Used by the source preview drawer in the workspace: clicking a source in
    the left pane HTMX-loads this fragment into the modal panel and Alpine
    opens it. Chunks are sorted by ``chunk_index`` so they read in document
    order, regardless of how Chroma decides to lay them out.
    """
    with connect() as conn:
        notebook = get_notebook(conn, notebook_id, user["id"])
        source_row = conn.execute(
            "SELECT * FROM sources WHERE id = ? AND notebook_id = ? AND user_id = ?",
            (source_id, notebook_id, user["id"]),
        ).fetchone()
        if source_row is None:
            raise HTTPException(status_code=404, detail="找不到來源")
        chunks = [
            dict(r)
            for r in conn.execute(
                "SELECT id, chunk_index, location, text FROM chunks WHERE source_id = ? AND user_id = ? ORDER BY chunk_index ASC",
                (source_id, user["id"]),
            ).fetchall()
        ]
    logger.info(
        "source_preview_loaded user_id=%s notebook_id=%s source_id=%s chunks=%s",
        user["id"], notebook_id, source_id, len(chunks),
    )
    return render(
        request,
        "_source_preview.html",
        {"notebook": notebook, "source": dict(source_row), "chunks": chunks},
    )


@app.get("/notebooks/{notebook_id}/_chat-empty", response_class=HTMLResponse)
def chat_empty_partial(
    request: Request,
    notebook_id: int,
    user: Annotated[dict, Depends(require_login)],
):
    """Return the center chat empty-state, refreshed after indexing.

    Triggered by `indexed-sources-changed` so the center pane flips from
    "Indexing in progress" to "Ask anything" the moment the first source
    finishes indexing — matching the left source rows and right Studio
    sections that already update live, instead of waiting for a page reload.
    """
    with connect() as conn:
        notebook = get_notebook(conn, notebook_id, user["id"])
        sources = [
            dict(row)
            for row in conn.execute(
                "SELECT id, status FROM sources WHERE notebook_id = ? AND user_id = ?",
                (notebook_id, user["id"]),
            ).fetchall()
        ]
    indexed_sources = [s for s in sources if s["status"] == "indexed"]
    return render(
        request,
        "_chat_empty.html",
        {
            "notebook": notebook,
            "sources": sources,
            "indexed_sources": indexed_sources,
            # U16: starter questions now live in the chat empty-state, not Studio.
            "cached_suggestions": _cached_suggestions(notebook),
        },
    )


@app.post("/notebooks/{notebook_id}/suggestions", response_class=HTMLResponse)
async def notebook_suggestions(
    request: Request,
    notebook_id: int,
    user: Annotated[dict, Depends(require_login)],
):
    """Generate 4 starter questions from the notebook's indexed chunks."""
    with connect() as conn:
        notebook = get_notebook(conn, notebook_id, user["id"])
        rows = conn.execute(
            """
            SELECT chunks.text, chunks.location, sources.filename
            FROM chunks JOIN sources ON sources.id = chunks.source_id
            WHERE chunks.user_id = ? AND sources.notebook_id = ? AND sources.status = 'indexed'
            ORDER BY chunks.id DESC
            LIMIT 24
            """,
            (user["id"], notebook_id),
        ).fetchall()
        settings = load_llm_settings(conn)
        has_indexed = bool(rows)

    excerpts = [{"filename": r["filename"], "location": r["location"], "text": r["text"]} for r in rows]
    questions: list[str] = []
    error = ""
    if not has_indexed:
        error = ""
    elif not (settings or {}).get("chat_model"):
        error = i18n.t("flow.suggestions_no_llm")
    else:
        try:
            questions = await generate_starter_questions(
                excerpts,
                settings or {},
                usage_context={"user_id": user["id"], "notebook_id": notebook_id},
            )
            if not questions:
                error = i18n.t("flow.suggestions_empty")
        except Exception as exc:
            logger.exception("suggestions_failed user_id=%s notebook_id=%s", user["id"], notebook_id)
            error = friendly_error_message(exc, i18n.t("error.action_suggestions"))

    if questions:
        with connect() as conn:
            conn.execute(
                "UPDATE notebooks SET suggestions_json = ?, suggestions_at = CURRENT_TIMESTAMP WHERE id = ?",
                (dumps(questions), notebook_id),
            )

    return render(
        request,
        "_suggestions.html",
        {"notebook": notebook, "questions": questions, "error": error, "has_indexed": has_indexed},
    )


def _fetch_source_summaries(conn, notebook_id: int, user_id: int, source_ids: list[int] | None = None) -> list[dict]:
    """Return [{id, filename, summary}] for indexed sources in this notebook.

    When ``source_ids`` is given, restricts to those ids (intersection with
    the notebook's indexed sources). For sources whose ``summary`` is empty,
    falls back to a 400-char snippet from the first chunk so briefing and
    comparison can still cover the source meaningfully.
    """
    params: list = [notebook_id, user_id]
    where = "notebook_id = ? AND user_id = ? AND status = 'indexed'"
    if source_ids:
        placeholders = ",".join("?" for _ in source_ids)
        where += f" AND id IN ({placeholders})"
        params.extend(source_ids)
    rows = conn.execute(
        f"SELECT id, filename, summary FROM sources WHERE {where} ORDER BY filename",
        params,
    ).fetchall()
    results: list[dict] = []
    for row in rows:
        summary = (row["summary"] or "").strip()
        if not summary:
            fallback_row = conn.execute(
                "SELECT text FROM chunks WHERE source_id = ? ORDER BY chunk_index ASC LIMIT 1",
                (row["id"],),
            ).fetchone()
            if fallback_row and fallback_row["text"]:
                summary = (fallback_row["text"] or "").strip()[:400]
        if summary:
            results.append({"id": row["id"], "filename": row["filename"], "summary": summary})
    return results


def _render_briefing(
    request: Request,
    notebook: dict,
    *,
    briefing: str = "",
    error: str = "",
    has_indexed: bool = False,
    in_progress: bool = False,
):
    """Shared render helper so GET and POST always pass the same context shape."""
    return render(
        request,
        "_briefing.html",
        {
            "notebook": notebook,
            "briefing": briefing,
            "error": error,
            "has_indexed": has_indexed,
            "in_progress": in_progress,
        },
    )


@app.get("/notebooks/{notebook_id}/_briefing", response_class=HTMLResponse)
def briefing_partial(
    request: Request,
    notebook_id: int,
    user: Annotated[dict, Depends(require_login)],
):
    """Return the briefing section reflecting the current cache + indexed state.

    Triggered by `indexed-sources-changed` so add/delete updates the section without
    a page reload. Never calls the LLM — POST is for generation. If a POST
    is currently in flight (lock held), returns the in_progress placeholder
    so this GET response doesn't kick off a duplicate auto-fire.
    """
    with connect() as conn:
        notebook = get_notebook(conn, notebook_id, user["id"])
        has_indexed = bool(conn.execute(
            "SELECT 1 FROM sources WHERE notebook_id = ? AND user_id = ? AND status = 'indexed' LIMIT 1",
            (notebook_id, user["id"]),
        ).fetchone())
    cached = _cached_briefing(notebook)
    in_progress = (not cached) and _briefing_locked(notebook_id)
    return _render_briefing(
        request, notebook,
        briefing=cached, has_indexed=has_indexed, in_progress=in_progress,
    )


@app.post("/notebooks/{notebook_id}/briefing", response_class=HTMLResponse)
async def notebook_briefing(
    request: Request,
    notebook_id: int,
    user: Annotated[dict, Depends(require_login)],
    force: int = 0,
):
    """Generate (or return cached) one-paragraph briefing across indexed sources.

    Concurrency: an in-process lock dedupes overlapping POSTs that fire when
    multiple sources finish indexing in close succession during a multi-file
    upload (each fires indexed-sources-changed → auto-fire POST). A waiter that
    arrives while the lock is held returns immediately with whichever state
    fits — cached if already written, otherwise the in_progress placeholder
    that polls until the lock clears.

    `?force=1` (sent by the *Regenerate* button) bypasses the cache check
    but still respects the lock so two simultaneous Regenerate clicks don't
    double-bill.
    """
    with connect() as conn:
        notebook = get_notebook(conn, notebook_id, user["id"])
        has_indexed = bool(conn.execute(
            "SELECT 1 FROM sources WHERE notebook_id = ? AND user_id = ? AND status = 'indexed' LIMIT 1",
            (notebook_id, user["id"]),
        ).fetchone())
        cached = _cached_briefing(notebook)

    # Fast paths that should never call the LLM:
    # 1. Non-forced request and we already have a fresh cache — just return it.
    # 2. Lock held by another in-flight POST — return cache or in_progress.
    if not force and cached:
        return _render_briefing(request, notebook, briefing=cached, has_indexed=has_indexed)
    if not _acquire_briefing_lock(notebook_id):
        logger.info("briefing_skipped_locked notebook_id=%s", notebook_id)
        return _render_briefing(
            request, notebook,
            briefing=cached, has_indexed=has_indexed,
            in_progress=not cached,
        )

    try:
        with connect() as conn:
            summaries = _fetch_source_summaries(conn, notebook_id, user["id"])
            settings = load_llm_settings(conn) or {}
        has_indexed = bool(summaries) or has_indexed

        briefing = ""
        error = ""
        if not summaries:
            error = ""  # nothing to brief; template falls back on has_indexed
        elif not settings.get("chat_model"):
            error = i18n.t("flow.briefing_no_llm")
        else:
            try:
                briefing = await generate_briefing(
                    summaries,
                    settings,
                    usage_context={"user_id": user["id"], "notebook_id": notebook_id},
                )
                if not briefing:
                    error = i18n.t("flow.briefing_empty")
            except Exception as exc:
                logger.exception("briefing_failed user_id=%s notebook_id=%s", user["id"], notebook_id)
                error = friendly_error_message(exc, i18n.t("error.action_briefing"))

        if briefing:
            with connect() as conn:
                conn.execute(
                    "UPDATE notebooks SET briefing = ?, briefing_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (briefing, notebook_id),
                )

        return _render_briefing(
            request, notebook,
            briefing=briefing, error=error, has_indexed=has_indexed,
        )
    finally:
        _release_briefing_lock(notebook_id)


@app.post("/notebooks/{notebook_id}/compare", response_class=HTMLResponse)
async def notebook_compare(
    request: Request,
    notebook_id: int,
    user: Annotated[dict, Depends(require_login)],
    source_ids: list[int] = Form(default=[]),
    focus: str = Form(default=""),
):
    """Compare 2+ indexed sources using their summaries; returns a result fragment."""
    selected_ids = [sid for sid in source_ids if isinstance(sid, int)]
    if focus.strip():
        record_ai_safety_events(
            text=focus,
            event_type="input_scan",
            surface="tool.compare_focus",
            context={"user_id": user["id"], "notebook_id": notebook_id},
            metadata={"source_count": len(selected_ids)},
        )
    if len(selected_ids) < 2:
        return render(
            request,
            "_compare_result.html",
            {
                "notebook_id": notebook_id,
                "comparison": "",
                "error": i18n.t("flow.compare_need_2"),
                "filenames": [],
                "focus": focus,
            },
            status_code=400,
        )
    with connect() as conn:
        notebook = get_notebook(conn, notebook_id, user["id"])
        summaries = _fetch_source_summaries(conn, notebook_id, user["id"], selected_ids)
        settings = load_llm_settings(conn) or {}

    if len(summaries) < 2:
        return render(
            request,
            "_compare_result.html",
            {
                "notebook_id": notebook_id,
                "comparison": "",
                "error": i18n.t("flow.compare_need_2_content"),
                "filenames": [s["filename"] for s in summaries],
                "focus": focus,
            },
            status_code=400,
        )
    if not settings.get("chat_model"):
        return render(
            request,
            "_compare_result.html",
            {
                "notebook_id": notebook_id,
                "comparison": "",
                "error": i18n.t("flow.compare_no_llm"),
                "filenames": [s["filename"] for s in summaries],
                "focus": focus,
            },
            status_code=400,
        )

    error = ""
    comparison = ""
    try:
        comparison = await compare_sources(
            summaries,
            focus,
            settings,
            usage_context={"user_id": user["id"], "notebook_id": notebook_id},
        )
        if not comparison:
            error = i18n.t("flow.compare_empty")
    except Exception as exc:
        logger.exception("compare_failed user_id=%s notebook_id=%s sources=%s", user["id"], notebook_id, len(summaries))
        error = friendly_error_message(exc, i18n.t("error.action_compare"))

    logger.info(
        "compare_completed user_id=%s notebook_id=%s sources=%s focus_chars=%s chars=%s",
        user["id"], notebook_id, len(summaries), len(focus or ""), len(comparison),
    )
    return render(
        request,
        "_compare_result.html",
        {
            "notebook_id": notebook_id,
            "comparison": comparison,
            "error": error,
            "filenames": [s["filename"] for s in summaries],
            "focus": focus,
        },
    )


@app.post("/notebooks/{notebook_id}/notes/add", response_class=HTMLResponse)
def add_note(
    request: Request,
    notebook_id: int,
    user: Annotated[dict, Depends(require_login)],
    title: str = Form(""),
    content: str = Form(...),
):
    """Create a raw note (no source message). Used by Save-to-notes on comparison results."""
    cleaned_content = (content or "").strip()
    if not cleaned_content:
        raise HTTPException(status_code=400, detail="筆記內容不可為空。")
    cleaned_title = " ".join((title or "").split())[:80] or "已儲存筆記"
    with connect() as conn:
        notebook = get_notebook(conn, notebook_id, user["id"])
        cursor = conn.execute(
            "INSERT INTO notes (notebook_id, user_id, title, content) VALUES (?, ?, ?, ?)",
            (notebook_id, user["id"], cleaned_title, cleaned_content),
        )
        note_id = cursor.lastrowid
        touch_notebook(conn, notebook_id)
        notes = [dict(r) for r in conn.execute(
            "SELECT * FROM notes WHERE notebook_id = ? AND user_id = ? ORDER BY created_at DESC, id DESC",
            (notebook_id, user["id"]),
        ).fetchall()]
    record_audit_event(
        request,
        user,
        "note_added",
        "note",
        note_id,
        {"notebook_id": notebook_id, "title": cleaned_title, "content_chars": len(cleaned_content)},
        "normal",
    )
    logger.info("note_added user_id=%s notebook_id=%s chars=%s", user["id"], notebook_id, len(cleaned_content))
    return render(request, "_notes_section.html", {"notebook": notebook, "notes": notes})


@app.post("/notebooks/{notebook_id}/notes/pin", response_class=HTMLResponse)
def pin_note(
    request: Request,
    notebook_id: int,
    user: Annotated[dict, Depends(require_login)],
    message_id: int = Form(...),
):
    """Pin an assistant message into the notebook's notes section."""
    with connect() as conn:
        notebook = get_notebook(conn, notebook_id, user["id"])
        message = conn.execute(
            """
            SELECT m.id, m.content, m.conversation_id, c.title, c.notebook_id
            FROM messages m JOIN conversations c ON c.id = m.conversation_id
            WHERE m.id = ? AND m.user_id = ? AND m.role = 'assistant' AND c.notebook_id = ?
            """,
            (message_id, user["id"], notebook_id),
        ).fetchone()
        if message is None:
            raise HTTPException(status_code=404, detail="找不到訊息")
        # Idempotent: pinning the same message twice is a no-op.
        existing = conn.execute(
            "SELECT id FROM notes WHERE notebook_id = ? AND source_message_id = ?",
            (notebook_id, message_id),
        ).fetchone()
        note_id = existing["id"] if existing else None
        created = False
        if existing is None:
            # Prefer the user question that prompted this assistant reply as
            # the note title (matches NotebookLM). Falls back to the
            # conversation title if no preceding user message exists.
            prompting = conn.execute(
                """
                SELECT content FROM messages
                WHERE conversation_id = ? AND user_id = ? AND role = 'user' AND id < ?
                ORDER BY id DESC LIMIT 1
                """,
                (message["conversation_id"], user["id"], message_id),
            ).fetchone()
            raw_title = (prompting["content"] if prompting else None) or message["title"] or "Pinned note"
            title = " ".join(raw_title.split())[:80]
            cursor = conn.execute(
                "INSERT INTO notes (notebook_id, user_id, title, content, source_message_id) VALUES (?, ?, ?, ?, ?)",
                (notebook_id, user["id"], title, message["content"], message_id),
            )
            note_id = cursor.lastrowid
            created = True
            touch_notebook(conn, notebook_id)
        notes = [dict(r) for r in conn.execute(
            "SELECT * FROM notes WHERE notebook_id = ? AND user_id = ? ORDER BY created_at DESC, id DESC",
            (notebook_id, user["id"]),
        ).fetchall()]
    record_audit_event(
        request,
        user,
        "note_pinned",
        "note",
        note_id,
        {"notebook_id": notebook_id, "source_message_id": message_id, "created": created},
        "normal",
    )
    logger.info("note_pinned user_id=%s notebook_id=%s message_id=%s", user["id"], notebook_id, message_id)
    return render(request, "_notes_section.html", {"notebook": notebook, "notes": notes})


@app.post("/notebooks/{notebook_id}/notes/{note_id}/delete", response_class=HTMLResponse)
def delete_note(
    request: Request,
    notebook_id: int,
    note_id: int,
    user: Annotated[dict, Depends(require_login)],
):
    """Delete a pinned note and return the refreshed notes section.

    Also broadcasts a `pin-cleared` HTMX event with the source message id so
    the matching pin button in the chat resets from "Pinned" back to "Pin".
    """
    with connect() as conn:
        notebook = get_notebook(conn, notebook_id, user["id"])
        # SQLite 3.35+ RETURNING saves a separate SELECT round-trip.
        deleted = conn.execute(
            "DELETE FROM notes WHERE id = ? AND notebook_id = ? AND user_id = ? RETURNING source_message_id",
            (note_id, notebook_id, user["id"]),
        ).fetchone()
        if deleted is None:
            raise HTTPException(status_code=404, detail="找不到筆記")
        source_message_id = deleted["source_message_id"]
        notes = [dict(r) for r in conn.execute(
            "SELECT * FROM notes WHERE notebook_id = ? AND user_id = ? ORDER BY created_at DESC, id DESC",
            (notebook_id, user["id"]),
        ).fetchall()]
    record_audit_event(
        request,
        user,
        "note_deleted",
        "note",
        note_id,
        {"notebook_id": notebook_id, "source_message_id": source_message_id},
        "high",
    )
    logger.info("note_deleted user_id=%s notebook_id=%s note_id=%s message_id=%s", user["id"], notebook_id, note_id, source_message_id)
    response = render(request, "_notes_section.html", {"notebook": notebook, "notes": notes})
    if source_message_id is not None:
        response.headers["HX-Trigger"] = dumps({"pin-cleared": {"message_id": source_message_id}})
    return response


@app.post("/notebooks/{notebook_id}/notes/{note_id}/edit", response_class=HTMLResponse)
def edit_note(
    request: Request,
    notebook_id: int,
    note_id: int,
    user: Annotated[dict, Depends(require_login)],
    title: str = Form(""),
    content: str = Form(...),
):
    """Update a note's title/content in place (U8) and return the refreshed shelf."""
    cleaned_content = (content or "").strip()
    if not cleaned_content:
        raise HTTPException(status_code=400, detail="筆記內容不可為空。")
    cleaned_title = " ".join((title or "").split())[:80]
    with connect() as conn:
        notebook = get_notebook(conn, notebook_id, user["id"])
        updated = conn.execute(
            "UPDATE notes SET title = ?, content = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND notebook_id = ? AND user_id = ? RETURNING id",
            (cleaned_title, cleaned_content, note_id, notebook_id, user["id"]),
        ).fetchone()
        if updated is None:
            raise HTTPException(status_code=404, detail="找不到筆記")
        notes = [dict(r) for r in conn.execute(
            "SELECT * FROM notes WHERE notebook_id = ? AND user_id = ? ORDER BY created_at DESC, id DESC",
            (notebook_id, user["id"]),
        ).fetchall()]
    record_audit_event(
        request,
        user,
        "note_edited",
        "note",
        note_id,
        {"notebook_id": notebook_id, "title": cleaned_title, "content_chars": len(cleaned_content)},
        "normal",
    )
    logger.info("note_edited user_id=%s notebook_id=%s note_id=%s chars=%s", user["id"], notebook_id, note_id, len(cleaned_content))
    return render(request, "_notes_section.html", {"notebook": notebook, "notes": notes})


@app.post("/notebooks/{notebook_id}/chat/{conversation_id}/delete")
def delete_conversation(
    request: Request,
    notebook_id: int,
    conversation_id: int,
    user: Annotated[dict, Depends(require_login)],
):
    """Delete a conversation and its messages within a notebook."""
    with connect() as conn:
        get_notebook(conn, notebook_id, user["id"])
        convo = conn.execute(
            """
            SELECT c.title, COUNT(m.id) AS message_count
            FROM conversations c LEFT JOIN messages m ON m.conversation_id = c.id AND m.user_id = c.user_id
            WHERE c.id = ? AND c.notebook_id = ? AND c.user_id = ?
            GROUP BY c.id
            """,
            (conversation_id, notebook_id, user["id"]),
        ).fetchone()
        if convo is None:
            raise HTTPException(status_code=404, detail="找不到對話")
        result = conn.execute(
            "DELETE FROM conversations WHERE id = ? AND notebook_id = ? AND user_id = ?",
            (conversation_id, notebook_id, user["id"]),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="找不到對話")
        touch_notebook(conn, notebook_id)
    record_audit_event(
        request,
        user,
        "conversation_deleted",
        "conversation",
        conversation_id,
        {"notebook_id": notebook_id, "title": convo["title"], "message_count": convo["message_count"]},
        "high",
    )
    logger.info("conversation_deleted user_id=%s notebook_id=%s conversation_id=%s", user["id"], notebook_id, conversation_id)
    return RedirectResponse(f"/notebooks/{notebook_id}", status_code=303)


@app.post("/notebooks/{notebook_id}/chat/{conversation_id}/rename")
def rename_conversation(
    request: Request,
    notebook_id: int,
    conversation_id: int,
    user: Annotated[dict, Depends(require_login)],
    title: str = Form(...),
):
    """Rename one conversation within a notebook."""
    clean_title = " ".join(title.split())[:80] or "新對話"
    with connect() as conn:
        get_notebook(conn, notebook_id, user["id"])
        result = conn.execute(
            """
            UPDATE conversations
            SET title = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND notebook_id = ? AND user_id = ?
            """,
            (clean_title, conversation_id, notebook_id, user["id"]),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="找不到對話")
        touch_notebook(conn, notebook_id)
    record_audit_event(
        request,
        user,
        "conversation_renamed",
        "conversation",
        conversation_id,
        {"notebook_id": notebook_id, "title": clean_title},
        "normal",
    )
    logger.info("conversation_renamed user_id=%s notebook_id=%s conversation_id=%s", user["id"], notebook_id, conversation_id)
    return RedirectResponse(f"/notebooks/{notebook_id}?conversation_id={conversation_id}", status_code=303)


@app.post("/notebooks/{notebook_id}/chat/new")
def new_conversation(request: Request, notebook_id: int, user: Annotated[dict, Depends(require_login)]):
    """Create an empty conversation scoped to a notebook."""
    with connect() as conn:
        get_notebook(conn, notebook_id, user["id"])
        cursor = conn.execute(
            "INSERT INTO conversations (user_id, notebook_id, title) VALUES (?, ?, '新對話')",
            (user["id"], notebook_id),
        )
        conversation_id = cursor.lastrowid
        touch_notebook(conn, notebook_id)
    record_audit_event(
        request,
        user,
        "conversation_created",
        "conversation",
        conversation_id,
        {"notebook_id": notebook_id},
        "normal",
    )
    logger.info("conversation_created user_id=%s notebook_id=%s conversation_id=%s", user["id"], notebook_id, conversation_id)
    return RedirectResponse(f"/notebooks/{notebook_id}?conversation_id={conversation_id}", status_code=303)


def _normalize_conversation_id(value: str | int | None) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    return int(text) if text else None


def _prepare_question(
    notebook_id: int,
    user: dict,
    question: str,
    conversation_id: int | None,
    source_ids: list[int],
) -> tuple[int, list[dict[str, str]], dict[str, Any]]:
    """Persist the user question and return context needed to answer it."""
    with connect() as conn:
        get_notebook(conn, notebook_id, user["id"])
        if source_ids:
            placeholders = ",".join("?" for _ in source_ids)
            allowed = {
                row["id"]
                for row in conn.execute(
                    f"SELECT id FROM sources WHERE notebook_id = ? AND user_id = ? AND id IN ({placeholders})",
                    (notebook_id, user["id"], *source_ids),
                ).fetchall()
            }
            source_ids[:] = [sid for sid in source_ids if sid in allowed]

        logger.info(
            "chat_question_received user_id=%s notebook_id=%s conversation_id=%s selected_sources=%s question_chars=%s",
            user["id"], notebook_id, conversation_id, len(source_ids), len(question),
        )

        if conversation_id is None:
            cursor = conn.execute(
                "INSERT INTO conversations (user_id, notebook_id, title) VALUES (?, ?, ?)",
                (user["id"], notebook_id, question[:80]),
            )
            conversation_id = cursor.lastrowid
        else:
            convo = conn.execute(
                "SELECT id FROM conversations WHERE id = ? AND notebook_id = ? AND user_id = ?",
                (conversation_id, notebook_id, user["id"]),
            ).fetchone()
            if convo is None:
                raise HTTPException(status_code=404, detail="找不到對話")
        conn.execute(
            "INSERT INTO messages (conversation_id, user_id, role, content) VALUES (?, ?, 'user', ?)",
            (conversation_id, user["id"], question),
        )
        history = [
            {"role": row["role"], "content": row["content"]}
            for row in conn.execute(
                """
                SELECT role, content
                FROM messages
                WHERE conversation_id = ? AND user_id = ? AND id NOT IN (
                    SELECT MAX(id) FROM messages WHERE conversation_id = ? AND user_id = ?
                )
                ORDER BY created_at DESC, id DESC
                LIMIT 6
                """,
                (conversation_id, user["id"], conversation_id, user["id"]),
            ).fetchall()
        ]
        history.reverse()
        settings = load_llm_settings(conn)
    return conversation_id, history, settings or {}


async def _answer_question(
    question: str,
    settings: dict[str, Any],
    history: list[dict[str, str]],
    user_id: int,
    notebook_id: int,
    conversation_id: int,
    source_ids: list[int],
) -> tuple[str, list[dict], dict[str, Any]]:
    """Run retrieval and non-streaming answer generation."""
    metadata: dict[str, Any] = {}
    usage_context = {"user_id": user_id, "notebook_id": notebook_id, "conversation_id": conversation_id}
    retrieve_started = time.perf_counter()
    retrieved = await retrieve(question, None, settings, history, user_id, source_ids, usage_context=usage_context)
    metadata["retrieval_ms"] = round((time.perf_counter() - retrieve_started) * 1000, 1)
    metadata["retrieved_chunks"] = len(retrieved)
    top_score = float(retrieved[0].get("score", 0.0)) if retrieved else 0.0
    if retrieved:
        metadata["top_score"] = round(top_score, 3)

    threshold = active_low_confidence_threshold()
    if not retrieved or top_score < threshold:
        metadata["outcome"] = "low_confidence" if retrieved else "no_retrieval"
        metadata["threshold"] = threshold
        logger.info(
            "chat_no_retrieval_results user_id=%s top_score=%.3f threshold=%.2f",
            user_id, top_score, threshold,
        )
        return i18n.t("chat.abstain"), [], metadata

    generate_started = time.perf_counter()
    answer = await generate_answer(question, retrieved, settings, usage_context=usage_context)
    metadata["generation_ms"] = round((time.perf_counter() - generate_started) * 1000, 1)
    metadata["answer_chars"] = len(answer)
    citations = _referenced_citations(answer, retrieved)
    metadata["outcome"] = "answered"
    return answer, citations, metadata


def _referenced_citations(answer: str, retrieved: list[dict]) -> list[dict]:
    all_citations = citation_payload(retrieved)
    referenced = {int(m.group(1)) for m in re.finditer(r"\[(\d+)\]", answer)}
    return [c for c in all_citations if c["index"] in referenced] if referenced else all_citations


def _save_assistant_message(
    notebook_id: int,
    user_id: int,
    conversation_id: int,
    question: str,
    answer: str,
    citations: list[dict],
    metadata: dict[str, Any],
) -> int:
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO messages (conversation_id, user_id, role, content, citations_json, metadata_json)
            VALUES (?, ?, 'assistant', ?, ?, ?)
            """,
            (conversation_id, user_id, answer, dumps(citations), dumps(metadata)),
        )
        conn.execute(
            """
            UPDATE conversations
            SET title = CASE WHEN title IN ('New conversation', '新對話') THEN ? ELSE title END,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND user_id = ?
            """,
            (question[:80], conversation_id, user_id),
        )
        touch_notebook(conn, notebook_id)
        return int(cursor.lastrowid)


def _llm_usage_event_watermark() -> int:
    with connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) AS last_id FROM llm_usage_events").fetchone()
    return int(row["last_id"] if row else 0)


def _attach_usage_events_to_message(
    *,
    user_id: int,
    notebook_id: int,
    conversation_id: int,
    message_id: int,
    after_event_id: int,
    call_types: tuple[str, ...],
) -> None:
    placeholders = ",".join("?" for _ in call_types)
    with connect() as conn:
        conn.execute(
            f"""
            UPDATE llm_usage_events
            SET message_id = ?
            WHERE id > ?
              AND user_id = ?
              AND notebook_id = ?
              AND conversation_id = ?
              AND message_id IS NULL
              AND call_type IN ({placeholders})
            """,
            (message_id, after_event_id, user_id, notebook_id, conversation_id, *call_types),
        )


def _messages_context(notebook_id: int, user_id: int, conversation_id: int) -> dict[str, Any]:
    with connect() as conn:
        notebook = get_notebook(conn, notebook_id, user_id)
        llm_status = llm_settings_status(conn)
        convo_row = conn.execute(
            "SELECT * FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        ).fetchone()
        rows = conn.execute(
            "SELECT * FROM messages WHERE conversation_id = ? AND user_id = ? ORDER BY created_at DESC, id DESC LIMIT 201",
            (conversation_id, user_id),
        ).fetchall()
        pinned_message_ids = {
            r["source_message_id"]
            for r in conn.execute(
                "SELECT source_message_id FROM notes WHERE notebook_id = ? AND user_id = ? AND source_message_id IS NOT NULL",
                (notebook_id, user_id),
            ).fetchall()
        }
    recent = [dict(r) for r in rows]
    return {
        "notebook": notebook,
        "conversation": dict(convo_row) if convo_row else None,
        "messages": [message_with_citations(row) for row in reversed(recent[:200])],
        "messages_truncated": len(recent) > 200,
        "pinned_message_ids": pinned_message_ids,
        "llm_status": llm_status,
    }


def _render_messages_partial(request: Request, notebook_id: int, user_id: int, conversation_id: int, *, oob: bool) -> HTMLResponse:
    return render(request, "_messages.html", {**_messages_context(notebook_id, user_id, conversation_id), "oob": oob})


def sse_event(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {dumps(data)}\n\n"


@app.post("/notebooks/{notebook_id}/chat/ask")
async def ask(
    request: Request,
    notebook_id: int,
    user: Annotated[dict, Depends(require_login)],
    question: str = Form(...),
    conversation_id: str | None = Form(None),
    source_ids: list[int] = Form(default=[]),
):
    """Persist a user question within a notebook, run retrieval, and save the assistant answer.

    Responds two ways (U1): HTMX requests get just the messages partial (no
    full page reload, URL updated via HX-Push-Url); plain form posts keep the
    original 303 redirect as the no-JS fallback.
    """
    question = question.strip()
    conversation_id = _normalize_conversation_id(conversation_id)
    if not question:
        return RedirectResponse(f"/notebooks/{notebook_id}", status_code=303)

    metadata: dict[str, Any] = {}
    usage_watermark = _llm_usage_event_watermark()
    try:
        conversation_id, history, settings = _prepare_question(notebook_id, user, question, conversation_id, source_ids)
        record_ai_safety_events(
            text=question,
            event_type="input_scan",
            surface="chat.ask",
            context={"user_id": user["id"], "notebook_id": notebook_id, "conversation_id": conversation_id},
            metadata={"source_count": len(source_ids)},
        )
        answer, citations, metadata = await _answer_question(
            question, settings, history, user["id"], notebook_id, conversation_id, source_ids
        )
    except HTTPException:
        raise
    except Exception as exc:
        if conversation_id is None:
            conversation_id, _history, _settings = _prepare_question(notebook_id, user, question, None, source_ids)
        answer = friendly_error_message(exc, i18n.t("error.action_answer"))
        citations = []
        metadata["outcome"] = "error"
        metadata["error"] = str(exc)[:200]
        logger.exception("chat_failed user_id=%s notebook_id=%s conversation_id=%s", user["id"], notebook_id, conversation_id)

    assistant_message_id = _save_assistant_message(notebook_id, user["id"], conversation_id, question, answer, citations, metadata)
    _attach_usage_events_to_message(
        user_id=user["id"],
        notebook_id=notebook_id,
        conversation_id=conversation_id,
        message_id=assistant_message_id,
        after_event_id=usage_watermark,
        call_types=("answer",),
    )

    if request.headers.get("HX-Request") == "true":
        response = _render_messages_partial(request, notebook_id, user["id"], conversation_id, oob=True)
        response.headers["HX-Push-Url"] = f"/notebooks/{notebook_id}?conversation_id={conversation_id}"
        return response
    return RedirectResponse(f"/notebooks/{notebook_id}?conversation_id={conversation_id}", status_code=303)


@app.post("/notebooks/{notebook_id}/chat/ask-stream")
async def ask_stream(
    request: Request,
    notebook_id: int,
    user: Annotated[dict, Depends(require_login)],
    question: str = Form(...),
    conversation_id: str | None = Form(None),
    source_ids: list[int] = Form(default=[]),
):
    """Stream a chat answer while preserving the saved message/citation flow."""
    question = question.strip()
    normalized_conversation_id = _normalize_conversation_id(conversation_id)
    if not question:
        raise HTTPException(status_code=400, detail="請先輸入問題。")

    async def events():
        conversation = normalized_conversation_id
        metadata: dict[str, Any] = {}
        citations: list[dict] = []
        answer = ""
        usage_watermark = 0
        try:
            conversation, history, settings = _prepare_question(notebook_id, user, question, conversation, source_ids)
            usage_watermark = _llm_usage_event_watermark()
            usage_context = {"user_id": user["id"], "notebook_id": notebook_id, "conversation_id": conversation}
            record_ai_safety_events(
                text=question,
                event_type="input_scan",
                surface="chat.ask_stream",
                context=usage_context,
                metadata={"source_count": len(source_ids)},
            )
            yield sse_event("init", {"conversation_id": conversation, "url": f"/notebooks/{notebook_id}?conversation_id={conversation}"})
            yield sse_event("status", {"text": i18n.t("js.retrieving")})

            retrieve_started = time.perf_counter()
            retrieved = await retrieve(question, None, settings, history, user["id"], source_ids, usage_context=usage_context)
            metadata["retrieval_ms"] = round((time.perf_counter() - retrieve_started) * 1000, 1)
            metadata["retrieved_chunks"] = len(retrieved)
            top_score = float(retrieved[0].get("score", 0.0)) if retrieved else 0.0
            if retrieved:
                metadata["top_score"] = round(top_score, 3)

            if not retrieved or top_score < active_low_confidence_threshold():
                answer = i18n.t("chat.abstain")
                metadata["outcome"] = "low_confidence" if retrieved else "no_retrieval"
                metadata["threshold"] = active_low_confidence_threshold()
                yield sse_event("chunk", {"text": answer})
            else:
                yield sse_event("status", {"text": i18n.t("js.generating")})
                generate_started = time.perf_counter()
                async for piece in generate_answer_stream(question, retrieved, settings, usage_context=usage_context):
                    answer += piece
                    yield sse_event("chunk", {"text": piece})
                metadata["generation_ms"] = round((time.perf_counter() - generate_started) * 1000, 1)
                metadata["answer_chars"] = len(answer)
                citations = _referenced_citations(answer, retrieved)
                metadata["outcome"] = "answered"

            assistant_message_id = _save_assistant_message(notebook_id, user["id"], conversation, question, answer, citations, metadata)
            _attach_usage_events_to_message(
                user_id=user["id"],
                notebook_id=notebook_id,
                conversation_id=conversation,
                message_id=assistant_message_id,
                after_event_id=usage_watermark,
                call_types=("answer_stream",),
            )
        except Exception as exc:
            logger.exception("chat_stream_failed user_id=%s notebook_id=%s conversation_id=%s", user["id"], notebook_id, conversation)
            if conversation is None:
                # The initial _prepare_question failed, so there's no row to
                # attach the error to — try once more.
                try:
                    conversation, _history, _settings = _prepare_question(notebook_id, user, question, None, source_ids)
                    yield sse_event("init", {"conversation_id": conversation, "url": f"/notebooks/{notebook_id}?conversation_id={conversation}"})
                except Exception:
                    # Still can't establish a conversation. Report the error and
                    # end the stream cleanly rather than aborting the generator
                    # (which would leave the client with no error/done event).
                    yield sse_event("error", {"text": friendly_error_message(exc, i18n.t("error.action_answer"))})
                    return
            answer = friendly_error_message(exc, i18n.t("error.action_answer"))
            # Update (not replace) so retrieval metrics gathered before the
            # failure survive into the saved metadata.
            metadata.update({"outcome": "error", "error": str(exc)[:200]})
            assistant_message_id = _save_assistant_message(notebook_id, user["id"], conversation, question, answer, [], metadata)
            _attach_usage_events_to_message(
                user_id=user["id"],
                notebook_id=notebook_id,
                conversation_id=conversation,
                message_id=assistant_message_id,
                after_event_id=usage_watermark,
                call_types=("answer_stream",),
            )
            yield sse_event("error", {"text": answer})

        final = _render_messages_partial(request, notebook_id, user["id"], conversation, oob=False)
        yield sse_event("done", {"html": final.body.decode("utf-8"), "url": f"/notebooks/{notebook_id}?conversation_id={conversation}"})

    return StreamingResponse(events(), media_type="text/event-stream")


@app.get("/notebooks/{notebook_id}/chat/{conversation_id}/_followups", response_class=HTMLResponse)
async def followups_partial(
    request: Request,
    notebook_id: int,
    conversation_id: int,
    user: Annotated[dict, Depends(require_login)],
    message_id: int,
):
    """Lazy-load follow-up question chips for an answered assistant message (A2).

    Generated once per message and cached in messages.metadata_json.followups,
    so reloading the page does not re-call the LLM.
    """
    with connect() as conn:
        notebook = get_notebook(conn, notebook_id, user["id"])
        if not notebook["followups_enabled"]:
            return HTMLResponse("")
        convo = conn.execute(
            "SELECT id FROM conversations WHERE id = ? AND notebook_id = ? AND user_id = ?",
            (conversation_id, notebook_id, user["id"]),
        ).fetchone()
        message = conn.execute(
            "SELECT * FROM messages WHERE id = ? AND conversation_id = ? AND user_id = ? AND role = 'assistant'",
            (message_id, conversation_id, user["id"]),
        ).fetchone() if convo else None
        if message is None:
            return HTMLResponse("")
        message_data = message_with_citations(message)
        metadata = message_data["metadata"]
        if metadata.get("followups") and metadata.get("followups_version") == FOLLOWUPS_CACHE_VERSION:
            return render(request, "_followups.html", {"questions": metadata["followups"]})
        source_context = [c.get("snippet", "") for c in message_data.get("citations", [])]
        prior_question = conn.execute(
            "SELECT content FROM messages WHERE conversation_id = ? AND user_id = ? AND role = 'user' AND id < ? ORDER BY id DESC LIMIT 1",
            (conversation_id, user["id"], message_id),
        ).fetchone()
        settings = load_llm_settings(conn)
    if prior_question is None:
        return HTMLResponse("")
    questions = await suggest_followup_questions(
        prior_question["content"],
        message["content"],
        settings or {},
        source_context,
        usage_context={
            "user_id": user["id"],
            "notebook_id": notebook_id,
            "conversation_id": conversation_id,
            "message_id": message_id,
        },
    )
    if questions:
        # json_set patches only the followups key inside SQLite, so a slow LLM
        # call here can't clobber metadata written concurrently elsewhere.
        with connect() as conn:
            conn.execute(
                "UPDATE messages SET metadata_json = json_set(metadata_json, '$.followups', json(?), '$.followups_version', ?) "
                "WHERE id = ? AND user_id = ?",
                (dumps(questions), FOLLOWUPS_CACHE_VERSION, message_id, user["id"]),
            )
    return render(request, "_followups.html", {"questions": questions})


def _markdown_download(markdown: str, filename: str) -> Response:
    """Wrap markdown text as a UTF-8 file download (RFC 5987 filename)."""
    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename=\"export.md\"; filename*=UTF-8''{quote(filename)}",
        },
    )


def _json_download(payload: dict[str, Any], filename: str) -> Response:
    """Wrap a JSON payload as a UTF-8 file download."""
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename=\"export.json\"; filename*=UTF-8''{quote(filename)}",
        },
    )


def record_audit_event(
    request: Request | None,
    user: dict | None,
    action: str,
    target_type: str = "",
    target_id: int | None = None,
    metadata: dict[str, Any] | None = None,
    sensitivity: str = "normal",
) -> None:
    """Persist a compact audit event for admin-visible compliance review.

    Keep metadata identifiers-only: do not copy source text, API keys, full
    exports, prompts, or retrieved snippets into the audit table.
    """
    actor_id = int(user["id"]) if user and user.get("id") is not None else None
    actor_username = str(user.get("username") or "") if user else ""
    ip_address = request.client.host if request and request.client else ""
    user_agent = request.headers.get("user-agent", "")[:300] if request else ""
    # Auditing is best-effort: it runs AFTER the main action has already
    # committed (e.g. index clear/rebuild, profile apply), so an audit-write
    # failure must never turn a succeeded action into a 500.
    try:
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_events
                (actor_user_id, actor_username, action, target_type, target_id,
                 sensitivity, ip_address, user_agent, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    actor_id,
                    actor_username[:120],
                    action[:120],
                    target_type[:80],
                    target_id,
                    sensitivity[:40],
                    ip_address[:120],
                    user_agent,
                    dumps(metadata or {}),
                ),
            )
    except Exception:
        logger.exception("audit_event_failed action=%s target=%s/%s", action, target_type, target_id)


@app.get("/notebooks/{notebook_id}/chat/{conversation_id}/export")
def export_conversation(
    request: Request,
    notebook_id: int,
    conversation_id: int,
    user: Annotated[dict, Depends(require_login)],
):
    """Download one conversation (questions, answers, citations) as Markdown (A3)."""
    with connect() as conn:
        notebook = get_notebook(conn, notebook_id, user["id"])
        convo = conn.execute(
            "SELECT * FROM conversations WHERE id = ? AND notebook_id = ? AND user_id = ?",
            (conversation_id, notebook_id, user["id"]),
        ).fetchone()
        if convo is None:
            raise HTTPException(status_code=404, detail="找不到對話")
        rows = conn.execute(
            "SELECT * FROM messages WHERE conversation_id = ? AND user_id = ? ORDER BY created_at, id",
            (conversation_id, user["id"]),
        ).fetchall()
    lines = [f"# {notebook['title']} — {convo['title']}", ""]
    for row in rows:
        message = message_with_citations(row)
        if message["role"] == "user":
            lines += [f"## 🙋 {message['content']}", ""]
        else:
            lines += [message["content"], ""]
            if message["citations"]:
                lines.append("> " + i18n.t("export.citations"))
                for c in message["citations"]:
                    lines.append(f"> [{c.get('index')}] {c.get('filename')} · {c.get('location')}")
                lines.append("")
    record_audit_event(
        request,
        user,
        "conversation_exported",
        "conversation",
        conversation_id,
        {"notebook_id": notebook_id, "title": convo["title"], "message_count": len(rows), "format": "markdown"},
        "high",
    )
    logger.info("conversation_exported user_id=%s notebook_id=%s conversation_id=%s messages=%s", user["id"], notebook_id, conversation_id, len(rows))
    return _markdown_download("\n".join(lines), f"{notebook['title']}-{convo['title']}.md")


@app.get("/notebooks/{notebook_id}/notes/export")
def export_notes(
    request: Request,
    notebook_id: int,
    user: Annotated[dict, Depends(require_login)],
):
    """Download all of a notebook's notes as Markdown (A3)."""
    with connect() as conn:
        notebook = get_notebook(conn, notebook_id, user["id"])
        notes = conn.execute(
            "SELECT * FROM notes WHERE notebook_id = ? AND user_id = ? ORDER BY created_at DESC, id DESC",
            (notebook_id, user["id"]),
        ).fetchall()
    lines = [f"# {notebook['title']} — {i18n.t('export.notes_suffix')}", ""]
    for note in notes:
        title = note["title"] or note["content"][:40]
        lines += [f"## {title}", "", note["content"], "", f"_{note['created_at']}_", "", "---", ""]
    record_audit_event(
        request,
        user,
        "notes_exported",
        "notebook",
        notebook_id,
        {"note_count": len(notes), "format": "markdown"},
        "high",
    )
    logger.info("notes_exported user_id=%s notebook_id=%s notes=%s", user["id"], notebook_id, len(notes))
    return _markdown_download("\n".join(lines), f"{notebook['title']}-notes.md")


@app.get("/notebooks/{notebook_id}/notes/{note_id}/export")
def export_note(
    request: Request,
    notebook_id: int,
    note_id: int,
    user: Annotated[dict, Depends(require_login)],
):
    """Download one notebook note as Markdown."""
    with connect() as conn:
        notebook = get_notebook(conn, notebook_id, user["id"])
        note = conn.execute(
            "SELECT * FROM notes WHERE id = ? AND notebook_id = ? AND user_id = ?",
            (note_id, notebook_id, user["id"]),
        ).fetchone()
        if note is None:
            raise HTTPException(status_code=404, detail="找不到筆記")
    title = note["title"] or note["content"][:40] or i18n.t("export.notes_suffix")
    lines = [f"# {title}", "", note["content"], "", f"_{note['created_at']}_", ""]
    record_audit_event(
        request,
        user,
        "note_exported",
        "note",
        note_id,
        {"notebook_id": notebook_id, "title": title, "format": "markdown"},
        "high",
    )
    logger.info("note_exported user_id=%s notebook_id=%s note_id=%s", user["id"], notebook_id, note_id)
    return _markdown_download("\n".join(lines), f"{notebook['title']}-{title}.md")


@app.get("/notebooks/{notebook_id}/_notes", response_class=HTMLResponse)
def notes_partial(
    request: Request,
    notebook_id: int,
    user: Annotated[dict, Depends(require_login)],
):
    """Return the notes section fragment (refreshed via the notes-changed event)."""
    with connect() as conn:
        notebook = get_notebook(conn, notebook_id, user["id"])
        notes = [dict(r) for r in conn.execute(
            "SELECT * FROM notes WHERE notebook_id = ? AND user_id = ? ORDER BY created_at DESC, id DESC",
            (notebook_id, user["id"]),
        ).fetchall()]
    return render(request, "_notes_section.html", {"notebook": notebook, "notes": notes})


MEETING_LIKE_PATTERNS = [
    (r"會議(逐字稿|紀錄|記錄|主題|主席|主持|議程)", "會議相關標題"),
    (r"與會(者|人員)", "與會者資訊"),
    (r"出席(者|人員)", "出席者資訊"),
    (r"列席", "列席資訊"),
    (r"決議", "決議事項"),
    (r"行動項目", "行動項目"),
    (r"待辦", "待辦事項"),
    (r"未決事項", "未決事項"),
    (r"follow[- ]?up", "追蹤事項"),
    (r"action item", "行動項目"),
    (r"meeting (transcript|minutes|notes|agenda)", "會議相關標題"),
    (r"attendee[s]?", "與會者資訊"),
    (r"participant[s]?", "與會者資訊"),
    (r"decision[s]?", "決議事項"),
]
STRONG_MEETING_LIKE_PATTERNS = [
    (r"會議逐字稿", "會議逐字稿"),
    (r"會議(紀錄|記錄)", "會議紀錄"),
    (r"meeting (transcript|minutes|notes)", "會議逐字稿或會議紀錄"),
]

SPEAKER_LABEL_RE = re.compile(
    r"(?mi)^\s*(?:主持人|主席|與會者|參與者|講者|發言人|記錄|紀錄|Speaker|Host|Moderator|Participant|Attendee)\s*[：:]\s*\S"
)
TIMESTAMP_RE = re.compile(r"(?:^|\s)(?:\d{1,2}:){1,2}\d{2}(?:\s|$)|\[\s*\d{1,2}:\d{2}(?::\d{2})?\s*\]")


def meeting_likelihood(chunks: list[dict[str, str]]) -> dict[str, object]:
    """Cheaply detect whether a source looks like meeting notes/transcript."""
    sample = "\n\n".join((chunk.get("text") or "") for chunk in chunks[:20])[:12000]
    lowered = sample.lower()
    hits: list[str] = []
    score = 0

    for pattern, label in STRONG_MEETING_LIKE_PATTERNS:
        if re.search(pattern, lowered, re.IGNORECASE):
            score += 4
            hits.append(label)

    for pattern, label in MEETING_LIKE_PATTERNS:
        if re.search(pattern, lowered, re.IGNORECASE):
            score += 2
            hits.append(label)

    speaker_labels = len(SPEAKER_LABEL_RE.findall(sample))
    if speaker_labels >= 3:
        score += 4
        hits.append("多位主持人或講者欄位")
    elif speaker_labels:
        score += 2
        hits.append("主持人或講者欄位")

    timestamps = len(TIMESTAMP_RE.findall(sample))
    if timestamps >= 3:
        score += 3
        hits.append("多個時間戳記")
    elif timestamps:
        score += 1
        hits.append("時間戳記")

    bullet_actions = len(re.findall(r"(?im)^\s*(?:[-*]|\d+[.)])\s*(?:TODO|待辦|決議|action|follow)", sample))
    if bullet_actions:
        score += 2
        hits.append("條列行動項目")

    is_likely = score >= 4
    unique_hits = list(dict.fromkeys(hits))
    if not unique_hits:
        reason = "沒有看到明顯的會議逐字稿、與會者、決議或待辦資訊。"
    else:
        evidence = "、".join(unique_hits[:4])
        reason = (
            f"看到「{evidence}」。"
            if is_likely
            else f"只看到「{evidence}」，但不足以判定這是會議逐字稿或會議紀錄。"
        )

    return {
        "is_likely": is_likely,
        "score": score,
        "reason": reason,
    }


def minutes_declines_meeting(minutes: str) -> bool:
    """Detect the prompt's "not a meeting record" abstention response."""
    normalized = minutes.strip().lower()
    if not normalized:
        return False
    decline_markers = [
        "不像會議",
        "不是會議",
        "不屬於會議",
        "not look like a meeting",
        "does not look like a meeting",
        "not a meeting transcript",
        "not meeting notes",
    ]
    return any(marker in normalized for marker in decline_markers)


@app.post("/notebooks/{notebook_id}/minutes", response_class=HTMLResponse)
async def source_minutes(
    request: Request,
    notebook_id: int,
    user: Annotated[dict, Depends(require_login)],
    source_id: int = Form(...),
    force: int = Form(0),
):
    """Generate structured meeting minutes from one indexed source (A1).

    The result is rendered in the Studio card and saved as a note; an
    HX-Trigger refreshes the notes section.
    """
    with connect() as conn:
        get_notebook(conn, notebook_id, user["id"])
        source = conn.execute(
            "SELECT * FROM sources WHERE id = ? AND notebook_id = ? AND user_id = ? AND status = 'indexed'",
            (source_id, notebook_id, user["id"]),
        ).fetchone()
        if source is None:
            return render(
                request, "_minutes_result.html",
                {"minutes": "", "error": i18n.t("flow.minutes_no_source"), "filename": ""},
                status_code=404,
            )
        # Cap the fetch: generate_meeting_minutes uses ~16k chars and chunks are
        # ~300-800 chars each, so 100 rows is ample — don't read a 1000-chunk
        # transcript just to throw 95% of it away.
        chunks = [dict(r) for r in conn.execute(
            "SELECT location, text FROM chunks WHERE source_id = ? AND user_id = ? ORDER BY chunk_index LIMIT 100",
            (source_id, user["id"]),
        ).fetchall()]
        settings = load_llm_settings(conn) or {}

    likelihood = meeting_likelihood(chunks)
    if not force and not likelihood["is_likely"]:
        logger.info(
            "meeting_minutes_warning user_id=%s notebook_id=%s source_id=%s score=%s",
            user["id"], notebook_id, source_id, likelihood["score"],
        )
        return render(
            request,
            "_minutes_result.html",
            {
                "minutes": "",
                "error": "",
                "filename": source["filename"],
                "warning": i18n.t("flow.minutes_not_meeting"),
                "warning_detail": likelihood["reason"],
                "notebook_id": notebook_id,
                "source_id": source_id,
            },
        )

    if not settings.get("chat_model"):
        return render(
            request, "_minutes_result.html",
            {"minutes": "", "error": i18n.t("flow.minutes_no_llm"), "filename": source["filename"], "warning": ""},
            status_code=400,
        )

    minutes = await generate_meeting_minutes(
        chunks,
        settings,
        usage_context={"user_id": user["id"], "notebook_id": notebook_id, "source_id": source_id},
    )
    if not minutes:
        return render(
            request, "_minutes_result.html",
            {"minutes": "", "error": i18n.t("flow.minutes_empty"), "filename": source["filename"]},
            status_code=502,
        )

    if minutes_declines_meeting(minutes):
        logger.info(
            "meeting_minutes_declined user_id=%s notebook_id=%s source_id=%s chars=%s",
            user["id"], notebook_id, source_id, len(minutes),
        )
        # Model judged the source is not a meeting record: show its reply, no
        # save option (per the manual-save model — only meetings are savable).
        return render(
            request,
            "_minutes_result.html",
            {"minutes": minutes, "error": "", "filename": source["filename"],
             "declined": True, "warning": "", "notebook_id": notebook_id},
        )

    logger.info(
        "meeting_minutes_completed user_id=%s notebook_id=%s source_id=%s chars=%s",
        user["id"], notebook_id, source_id, len(minutes),
    )
    # Manual save: show the minutes with a save-to-notes button; do not persist
    # automatically (no notes-changed fired — the shelf refreshes on save).
    return render(
        request, "_minutes_result.html",
        {"minutes": minutes, "error": "", "filename": source["filename"],
         "declined": False, "warning": "", "notebook_id": notebook_id},
    )


# --- U16: Studio tools launcher + A4 artifact generators ------------------
# Tools (compare / minutes / A4 artifacts) are surfaced as a tile grid that
# opens each tool's config inside the shared preview-modal. Artifact results
# are saved to Notes (the outputs shelf), mirroring meeting minutes.

ARTIFACT_LABELS = {
    "study_guide": "學習指南",
    "faq": "常見問答",
    "timeline": "時間軸",
}
# Tools that operate on the whole notebook (need only ≥1 indexed source) vs
# compare which needs ≥2. Drives the tile enabled-state in the launcher.
TOOL_MIN_INDEXED = {"compare": 2, "minutes": 1, "study_guide": 1, "faq": 1, "timeline": 1, "translate": 1}
# A5: target languages offered by the translate-summary tool (allowlisted so the
# value going into the prompt can't be arbitrary user text).
TRANSLATE_LANGUAGES = ["繁體中文", "English", "日本語", "简体中文"]


@app.get("/notebooks/{notebook_id}/_tools", response_class=HTMLResponse)
def tools_partial(
    request: Request,
    notebook_id: int,
    user: Annotated[dict, Depends(require_login)],
):
    """Return the Studio tools tile grid, refreshed on indexed-sources-changed.

    Tiles enable/disable based on the indexed-source count so the grid stays in
    sync with the left pane after upload / delete without a page reload.
    """
    with connect() as conn:
        notebook = get_notebook(conn, notebook_id, user["id"])
        indexed_count = conn.execute(
            "SELECT COUNT(*) AS n FROM sources WHERE notebook_id = ? AND user_id = ? AND status = 'indexed'",
            (notebook_id, user["id"]),
        ).fetchone()["n"]
    return render(request, "_studio_tools.html", {"notebook": notebook, "indexed_count": indexed_count})


@app.get("/notebooks/{notebook_id}/tools/{kind}", response_class=HTMLResponse)
def tool_panel(
    request: Request,
    notebook_id: int,
    kind: str,
    user: Annotated[dict, Depends(require_login)],
):
    """Return one tool's config panel, loaded into the preview-modal by a tile."""
    if kind not in TOOL_MIN_INDEXED:
        raise HTTPException(status_code=404, detail="未知的工具")
    with connect() as conn:
        notebook = get_notebook(conn, notebook_id, user["id"])
        sources_indexed = [dict(r) for r in conn.execute(
            "SELECT id, filename, summary FROM sources WHERE notebook_id = ? AND user_id = ? AND status = 'indexed' ORDER BY filename",
            (notebook_id, user["id"]),
        ).fetchall()]
    return render(
        request,
        "_tool_panel.html",
        {
            "notebook": notebook,
            "kind": kind,
            "sources_indexed": sources_indexed,
            "artifact_label": ARTIFACT_LABELS.get(kind, ""),
            "translate_languages": TRANSLATE_LANGUAGES,
        },
    )


@app.post("/notebooks/{notebook_id}/artifacts/{kind}", response_class=HTMLResponse)
async def notebook_artifact(
    request: Request,
    notebook_id: int,
    kind: str,
    user: Annotated[dict, Depends(require_login)],
):
    """Generate an A4 artifact (study guide / FAQ / timeline) from the notebook's
    source summaries. The result is shown in the modal; the user decides whether
    to save it to the outputs shelf (no auto-save)."""
    if kind not in ARTIFACT_PROMPTS:
        raise HTTPException(status_code=404, detail="未知的產出類型")
    label = ARTIFACT_LABELS.get(kind, kind)
    with connect() as conn:
        notebook = get_notebook(conn, notebook_id, user["id"])
        summaries = _fetch_source_summaries(conn, notebook_id, user["id"])
        settings = load_llm_settings(conn) or {}

    base_ctx = {"notebook_id": notebook_id, "label": label, "notebook_title": notebook["title"], "artifact": ""}
    if not summaries:
        return render(
            request, "_artifact_result.html",
            {**base_ctx, "error": i18n.t("flow.artifact_need_index")},
            status_code=400,
        )
    if not settings.get("chat_model"):
        return render(
            request, "_artifact_result.html",
            {**base_ctx, "error": i18n.t("flow.artifact_no_llm")},
            status_code=400,
        )

    error = ""
    artifact = ""
    try:
        artifact = await generate_artifact(
            kind,
            summaries,
            settings,
            usage_context={"user_id": user["id"], "notebook_id": notebook_id},
        )
        if not artifact:
            error = i18n.t("flow.artifact_empty")
    except Exception as exc:
        logger.exception("artifact_failed user_id=%s notebook_id=%s kind=%s", user["id"], notebook_id, kind)
        error = friendly_error_message(exc, i18n.t("error.action_generate", label=label))

    logger.info(
        "artifact_completed user_id=%s notebook_id=%s kind=%s sources=%s chars=%s",
        user["id"], notebook_id, kind, len(summaries), len(artifact),
    )
    return render(
        request, "_artifact_result.html",
        {**base_ctx, "artifact": artifact, "error": error},
    )


@app.post("/notebooks/{notebook_id}/translate", response_class=HTMLResponse)
async def translate_source_summary(
    request: Request,
    notebook_id: int,
    user: Annotated[dict, Depends(require_login)],
    source_id: int = Form(...),
    target_language: str = Form(...),
):
    """Translate one indexed source's summary into a target language (A5).

    The result is shown in the modal with a manual save-to-notes button.
    """
    if target_language not in TRANSLATE_LANGUAGES:
        raise HTTPException(status_code=400, detail="不支援的目標語言")
    with connect() as conn:
        notebook = get_notebook(conn, notebook_id, user["id"])
        summaries = _fetch_source_summaries(conn, notebook_id, user["id"], [source_id])
        settings = load_llm_settings(conn) or {}

    base_ctx = {"notebook_id": notebook_id, "target_language": target_language, "translated": "", "filename": ""}
    if not summaries:
        return render(
            request, "_translate_result.html",
            {**base_ctx, "error": i18n.t("flow.translate_no_source")},
            status_code=404,
        )
    source = summaries[0]
    base_ctx["filename"] = source["filename"]
    if not settings.get("chat_model"):
        return render(
            request, "_translate_result.html",
            {**base_ctx, "error": i18n.t("flow.translate_no_llm")},
            status_code=400,
        )

    error = ""
    translated = ""
    try:
        translated = await translate_summary(
            source["summary"],
            target_language,
            settings,
            usage_context={"user_id": user["id"], "notebook_id": notebook_id, "source_id": source_id},
        )
        if not translated:
            error = i18n.t("flow.translate_empty")
    except Exception as exc:
        logger.exception("translate_failed user_id=%s notebook_id=%s source_id=%s", user["id"], notebook_id, source_id)
        error = friendly_error_message(exc, i18n.t("error.action_translate"))

    logger.info(
        "translate_summary_completed user_id=%s notebook_id=%s source_id=%s lang=%s chars=%s",
        user["id"], notebook_id, source_id, target_language, len(translated),
    )
    return render(
        request, "_translate_result.html",
        {**base_ctx, "translated": translated, "error": error},
    )


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request, user: Annotated[dict, Depends(require_login)]):
    """Render the per-user account page (currently: change own password)."""
    return render(request, "account.html", {"user": user, "saved": False, "error": ""})


@app.post("/account/password")
def change_own_password(
    request: Request,
    user: Annotated[dict, Depends(require_login)],
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    """Allow a signed-in user to change their own password."""
    error = ""
    with connect() as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
    if row is None or not verify_password(current_password, row["password_hash"]):
        error = "目前密碼不正確。"
    elif len(new_password) < 6:
        error = "新密碼至少需要 6 個字元。"
    elif new_password != confirm_password:
        error = "新密碼與確認密碼不一致。"
    if error:
        return render(request, "account.html", {"user": user, "saved": False, "error": error}, 400)
    with connect() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new_password), user["id"]))
    logger.info("password_changed user_id=%s", user["id"])
    return render(request, "account.html", {"user": user, "saved": True, "error": ""})


# Mounted last so the shared helpers above (render, require_admin,
# record_audit_event, _json_download) are defined before the router modules
# import them. Admin console (index/audit/users) → app/admin.py; Admin Eval
# Workbench → app/evals.py; LLM settings/diagnostics → app/settings.py.
from .admin import router as admin_router  # noqa: E402
from .evals import router as evals_router  # noqa: E402
from .settings import router as settings_router  # noqa: E402

app.include_router(admin_router)
app.include_router(evals_router)
app.include_router(settings_router)
