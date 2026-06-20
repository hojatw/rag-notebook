import asyncio
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
from urllib.parse import quote

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import config
from .db import UPLOAD_DIR, connect, dumps, encrypt_for_storage, init_db, load_llm_settings, load_llm_settings_for_display, loads
from .ingest import supported
from .jobs import enqueue_source
from .worker import run_worker_loop
from . import i18n
import httpx

from .llm import ARTIFACT_PROMPTS, FOLLOWUPS_CACHE_VERSION, close_http_client, compare_sources, cosine, embed_texts, generate_answer, generate_answer_stream, generate_artifact, generate_briefing, generate_eval_candidates, generate_meeting_minutes, generate_starter_questions, probe_embedding_dimension, rerank_chunks, set_http_client, rewrite_search_queries, suggest_followup_questions, translate_summary
from .security import INSECURE_DEV_SECRET, get_app_secret, hash_password, sign_user_id, unsign_user_id, verify_password
from .vector_store import clear_all_vectors as clear_all_vectors
from .vector_store import current_dimension as vector_current_dimension
from .vector_store import delete_source as delete_source_vectors
from .vector_store import index_status as vector_index_status
from .vector_store import query as query_vectors
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
        {"request": request, "followups_cache_version": FOLLOWUPS_CACHE_VERSION, **context},
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
    query = " ".join((q or "").split())[:120]
    results: list[dict[str, str]] = []
    if query:
        like = f"%{sql_like_escape(query)}%"
        with connect() as conn:
            for row in conn.execute(
                """
                SELECT id, title, description, emoji, updated_at
                FROM notebooks
                WHERE user_id = ? AND (title LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\')
                ORDER BY updated_at DESC
                LIMIT 12
                """,
                (user["id"], like, like),
            ).fetchall():
                results.append({
                    "type": "筆記本",
                    "title": f"{row['emoji'] or '📓'} {row['title']}",
                    "snippet": row["description"] or "開啟筆記本",
                    "url": f"/notebooks/{row['id']}",
                    "time": relative_time(row["updated_at"]),
                })
            for row in conn.execute(
                """
                SELECT s.id, s.notebook_id, s.filename, s.summary, s.updated_at, n.title AS notebook_title
                FROM sources s JOIN notebooks n ON n.id = s.notebook_id
                WHERE s.user_id = ? AND s.notebook_id IS NOT NULL
                  AND (s.filename LIKE ? ESCAPE '\\' OR s.summary LIKE ? ESCAPE '\\')
                ORDER BY s.updated_at DESC
                LIMIT 12
                """,
                (user["id"], like, like),
            ).fetchall():
                results.append({
                    "type": "來源",
                    "title": row["filename"],
                    "snippet": row["summary"] or f"位於「{row['notebook_title']}」",
                    "url": f"/notebooks/{row['notebook_id']}",
                    "time": relative_time(row["updated_at"]),
                })
            for row in conn.execute(
                """
                SELECT c.id, c.notebook_id, c.title, c.updated_at, n.title AS notebook_title,
                    (SELECT COUNT(*) FROM messages WHERE conversation_id = c.id AND user_id = c.user_id) AS message_count
                FROM conversations c JOIN notebooks n ON n.id = c.notebook_id
                WHERE c.user_id = ? AND c.title LIKE ? ESCAPE '\\'
                ORDER BY c.updated_at DESC
                LIMIT 12
                """,
                (user["id"], like),
            ).fetchall():
                results.append({
                    "type": "對話",
                    "title": row["title"],
                    "snippet": f"位於「{row['notebook_title']}」 · {row['message_count']} 則訊息",
                    "url": f"/notebooks/{row['notebook_id']}?conversation_id={row['id']}",
                    "time": relative_time(row["updated_at"]),
                })
            for row in conn.execute(
                """
                SELECT notes.id, notes.notebook_id, notes.title, notes.content, notes.updated_at, notebooks.title AS notebook_title
                FROM notes JOIN notebooks ON notebooks.id = notes.notebook_id
                WHERE notes.user_id = ? AND (notes.title LIKE ? ESCAPE '\\' OR notes.content LIKE ? ESCAPE '\\')
                ORDER BY notes.updated_at DESC
                LIMIT 12
                """,
                (user["id"], like, like),
            ).fetchall():
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
        {"user": user, "query": query, "results": results},
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


def _cached_suggestions(notebook: dict) -> list[str]:
    """Return cached suggestions if within TTL, else empty list."""
    raw = (notebook.get("suggestions_json") or "").strip()
    saved_at = (notebook.get("suggestions_at") or "").strip()
    if not raw or not saved_at:
        return []
    try:
        cutoff = datetime.utcnow() - timedelta(hours=SUGGESTIONS_TTL_HOURS)
        if datetime.fromisoformat(saved_at.replace(" ", "T")) > cutoff:
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
        cutoff = datetime.utcnow() - timedelta(hours=BRIEFING_TTL_HOURS)
        if datetime.fromisoformat(saved_at.replace(" ", "T")) > cutoff:
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
    has_api_key = bool(settings.get("api_key"))
    has_chat_model = bool(settings.get("chat_model"))
    has_embedding_model = bool(settings.get("embedding_model"))
    return {
        "ready": has_api_key and has_chat_model and has_embedding_model,
        "has_api_key": has_api_key,
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
    with connect() as conn:
        notebooks = [
            dict(row)
            for row in conn.execute(
                """
                SELECT n.*,
                    (SELECT COUNT(*) FROM sources WHERE notebook_id = n.id) AS source_count,
                    (SELECT COUNT(*) FROM conversations WHERE notebook_id = n.id) AS conversation_count
                FROM notebooks n
                WHERE n.user_id = ?
                ORDER BY n.updated_at DESC, n.id DESC
                """,
                (user["id"],),
            ).fetchall()
        ]
    return render(request, "home.html", {"user": user, "notebooks": notebooks})


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
    elif not (settings or {}).get("api_key") or not (settings or {}).get("chat_model"):
        error = i18n.t("flow.suggestions_no_llm")
    else:
        try:
            questions = await generate_starter_questions(excerpts, settings or {})
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
        elif not settings.get("api_key") or not settings.get("chat_model"):
            error = i18n.t("flow.briefing_no_llm")
        else:
            try:
                briefing = await generate_briefing(summaries, settings)
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
    if not settings.get("api_key") or not settings.get("chat_model"):
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
        comparison = await compare_sources(summaries, focus, settings)
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
    source_ids: list[int],
) -> tuple[str, list[dict], dict[str, Any]]:
    """Run retrieval and non-streaming answer generation."""
    metadata: dict[str, Any] = {}
    retrieve_started = time.perf_counter()
    retrieved = await retrieve(question, None, settings, history, user_id, source_ids)
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
    answer = await generate_answer(question, retrieved, settings)
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
) -> None:
    with connect() as conn:
        conn.execute(
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
    try:
        conversation_id, history, settings = _prepare_question(notebook_id, user, question, conversation_id, source_ids)
        answer, citations, metadata = await _answer_question(question, settings, history, user["id"], source_ids)
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

    _save_assistant_message(notebook_id, user["id"], conversation_id, question, answer, citations, metadata)

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
        try:
            conversation, history, settings = _prepare_question(notebook_id, user, question, conversation, source_ids)
            yield sse_event("init", {"conversation_id": conversation, "url": f"/notebooks/{notebook_id}?conversation_id={conversation}"})
            yield sse_event("status", {"text": i18n.t("js.retrieving")})

            retrieve_started = time.perf_counter()
            retrieved = await retrieve(question, None, settings, history, user["id"], source_ids)
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
                async for piece in generate_answer_stream(question, retrieved, settings):
                    answer += piece
                    yield sse_event("chunk", {"text": piece})
                metadata["generation_ms"] = round((time.perf_counter() - generate_started) * 1000, 1)
                metadata["answer_chars"] = len(answer)
                citations = _referenced_citations(answer, retrieved)
                metadata["outcome"] = "answered"

            _save_assistant_message(notebook_id, user["id"], conversation, question, answer, citations, metadata)
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
            _save_assistant_message(notebook_id, user["id"], conversation, question, answer, [], metadata)
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
    questions = await suggest_followup_questions(prior_question["content"], message["content"], settings or {}, source_context)
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

    if not settings.get("api_key") or not settings.get("chat_model"):
        return render(
            request, "_minutes_result.html",
            {"minutes": "", "error": i18n.t("flow.minutes_no_llm"), "filename": source["filename"], "warning": ""},
            status_code=400,
        )

    minutes = await generate_meeting_minutes(chunks, settings)
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
    if not settings.get("api_key") or not settings.get("chat_model"):
        return render(
            request, "_artifact_result.html",
            {**base_ctx, "error": i18n.t("flow.artifact_no_llm")},
            status_code=400,
        )

    error = ""
    artifact = ""
    try:
        artifact = await generate_artifact(kind, summaries, settings)
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
    if not settings.get("api_key") or not settings.get("chat_model"):
        return render(
            request, "_translate_result.html",
            {**base_ctx, "error": i18n.t("flow.translate_no_llm")},
            status_code=400,
        )

    error = ""
    translated = ""
    try:
        translated = await translate_summary(source["summary"], target_language, settings)
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


# Per-question minimum top-score before we let the answer LLM run. Below this
# the model is asked to abstain. Lives on the ask() side, not retrieve(), so
# the eval harness can still observe raw retrieval scores.
LOW_CONFIDENCE_THRESHOLD = config.retrieval.low_confidence_threshold

# Upper bound on rows pulled into the degraded "Chroma is down" fallback in
# retrieve(). Without it, a transient Chroma failure would decode every chunk's
# embedding from SQLite and run Python-side cosine over the whole corpus —
# O(all_chunks) memory + CPU that melts the box at scale. The fallback has no
# real vector index, so it is a best-effort safety net, not the primary path.
FALLBACK_MAX_CHUNKS = config.retrieval.fallback_max_chunks

# Hybrid blend weights + candidate-pool / final-chunk sizes (config-driven).
VECTOR_WEIGHT = config.retrieval.vector_weight
KEYWORD_WEIGHT = config.retrieval.keyword_weight
CANDIDATE_POOL_SIZE = config.retrieval.candidate_pool_size
FINAL_CHUNK_COUNT = config.retrieval.final_chunk_count


async def retrieve(
    question: str,
    rows,
    settings: dict,
    history: list[dict[str, str]] | None = None,
    user_id: int | None = None,
    source_ids: list[int] | None = None,
    params: dict | None = None,
) -> list[dict]:
    """Retrieve chunks with query rewriting, hybrid search, and optional LLM reranking.

    ``params`` overrides the runtime-safe retrieval knobs for this call only
    (used by the eval workbench for isolated per-run experiments); None falls
    back to the active applied profile.
    """
    started = time.perf_counter()
    p = resolve_retrieval_params(params)
    pool_size = int(p["candidate_pool_size"])
    final_count = int(p["final_chunk_count"])
    vector_weight = float(p["vector_weight"])
    keyword_weight = float(p["keyword_weight"])
    rerank_weight = float(p["rerank_weight"])
    rerank_base_weight = float(p["rerank_base_weight"])
    queries = await rewrite_search_queries(question, history or [], settings)
    query_embeddings = await embed_texts(queries, settings, role="query")
    if user_id is not None:
        try:
            # Vector (Chroma) and keyword (SQLite) search are independent — run
            # them concurrently in threads so their I/O overlaps instead of
            # adding up (P2-2). Both are sync; to_thread releases the event loop.
            vector_candidates, keyword_candidates = await asyncio.gather(
                asyncio.to_thread(query_vectors, query_embeddings, user_id, source_ids, n_results=pool_size),
                asyncio.to_thread(keyword_candidates_from_sqlite, user_id, source_ids or [], queries, limit=pool_size),
            )
            candidates = merge_candidates(vector_candidates, keyword_candidates, queries, params=p)
            ranked = sorted(candidates.values(), key=lambda item: item["score"], reverse=True)[:pool_size]
            retrieved = await rerank_chunks(
                question, ranked, settings, limit=final_count,
                rerank_weight=rerank_weight, rerank_base_weight=rerank_base_weight,
            )
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.info(
                "retrieve_completed mode=chroma rewritten_queries=%s vector_candidates=%s keyword_candidates=%s candidates=%s reranked=%s elapsed_ms=%.1f",
                len(queries),
                len(vector_candidates),
                len(keyword_candidates),
                len(ranked),
                len(retrieved),
                elapsed_ms,
            )
            return retrieved
        except Exception:
            logger.warning(
                "retrieve_vector_failed user_id=%s — falling back to capped SQLite scan (max=%s chunks); "
                "results are degraded until Chroma recovers (try /admin/index Rebuild)",
                user_id, FALLBACK_MAX_CHUNKS, exc_info=True,
            )
            rows = fetch_candidate_rows(user_id, source_ids or [])
    if not rows:
        logger.info("retrieve_skipped reason=no_candidate_rows")
        return []
    candidates = {}
    for row in rows:
        embedding = loads(row["embedding_json"])
        vector_score = max(cosine(query_embedding, embedding) for query_embedding in query_embeddings)
        keyword = keyword_score(queries, row["text"])
        score = (vector_weight * max(0.0, vector_score)) + (keyword_weight * keyword)
        if score <= 0:
            continue
        candidates[row["id"]] = {
            "id": row["id"],
            "source_id": row["source_id"],
            "filename": row["filename"],
            "location": row["location"],
            "text": row["text"],
            "score": score,
            "vector_score": vector_score,
            "keyword_score": keyword,
        }
    ranked = sorted(candidates.values(), key=lambda item: item["score"], reverse=True)[:pool_size]
    retrieved = await rerank_chunks(
        question, ranked, settings, limit=final_count,
        rerank_weight=rerank_weight, rerank_base_weight=rerank_base_weight,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "retrieve_completed source_rows=%s rewritten_queries=%s candidates=%s reranked=%s elapsed_ms=%.1f",
        len(rows),
        len(queries),
        len(ranked),
        len(retrieved),
        elapsed_ms,
    )
    return retrieved


def fetch_candidate_rows(user_id: int, source_ids: list[int], limit: int = FALLBACK_MAX_CHUNKS) -> list:
    """Fetch SQLite chunks for the degraded fallback used when Chroma is down.

    Capped at ``limit`` rows (most recent first) so a Chroma outage degrades
    gracefully instead of decoding every chunk's embedding and melting the box
    at corpus scale. This path has no real vector index — best-effort only.
    """
    with connect() as conn:
        if source_ids:
            placeholders = ",".join("?" for _ in source_ids)
            return conn.execute(
                f"""
                SELECT chunks.*, sources.filename
                FROM chunks JOIN sources ON sources.id = chunks.source_id
                WHERE chunks.user_id = ? AND sources.status = 'indexed' AND chunks.source_id IN ({placeholders})
                ORDER BY chunks.id DESC
                LIMIT ?
                """,
                (user_id, *source_ids, limit),
            ).fetchall()
        return conn.execute(
            """
            SELECT chunks.*, sources.filename
            FROM chunks JOIN sources ON sources.id = chunks.source_id
            WHERE chunks.user_id = ? AND sources.status = 'indexed'
            ORDER BY chunks.id DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()


def keyword_candidates_from_sqlite(user_id: int, source_ids: list[int], queries: list[str], limit: int = 20) -> list[dict]:
    """Find keyword candidate chunks from SQLite without decoding all embeddings."""
    tokens = []
    for query in queries:
        tokens.extend(search_tokens(query))
    unique_tokens = list(dict.fromkeys(tokens))[:12]
    if not unique_tokens:
        return []
    like_clause = " OR ".join("chunks.text LIKE ?" for _ in unique_tokens)
    params: list = [user_id]
    source_clause = ""
    if source_ids:
        placeholders = ",".join("?" for _ in source_ids)
        source_clause = f"AND chunks.source_id IN ({placeholders})"
        params.extend(source_ids)
    params.extend(f"%{token}%" for token in unique_tokens)
    params.append(limit * 4)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT chunks.*, sources.filename
            FROM chunks JOIN sources ON sources.id = chunks.source_id
            WHERE chunks.user_id = ? AND sources.status = 'indexed'
              {source_clause}
              AND ({like_clause})
            ORDER BY chunks.id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    candidates = [
        {
            "id": row["id"],
            "source_id": row["source_id"],
            "filename": row["filename"],
            "location": row["location"],
            "text": row["text"],
            "vector_score": 0.0,
        }
        for row in rows
    ]
    return sorted(candidates, key=lambda item: keyword_score(queries, item["text"]), reverse=True)[:limit]


def merge_candidates(
    vector_candidates: list[dict],
    keyword_candidates: list[dict],
    queries: list[str],
    params: dict | None = None,
) -> dict[int, dict]:
    """Merge vector and keyword candidates into one hybrid-scored map."""
    p = resolve_retrieval_params(params)
    vector_weight = float(p["vector_weight"])
    keyword_weight = float(p["keyword_weight"])
    candidates: dict[int, dict] = {}
    for item in [*vector_candidates, *keyword_candidates]:
        chunk_id = int(item["id"])
        keyword = keyword_score(queries, item["text"])
        vector_score = max(0.0, float(item.get("vector_score") or 0.0))
        score = (vector_weight * vector_score) + (keyword_weight * keyword)
        existing = candidates.get(chunk_id)
        if existing and existing["score"] >= score:
            continue
        candidates[chunk_id] = {
            "id": chunk_id,
            "source_id": item["source_id"],
            "filename": item["filename"],
            "location": item["location"],
            "text": item["text"],
            "score": score,
            "vector_score": vector_score,
            "keyword_score": keyword,
        }
    return {chunk_id: item for chunk_id, item in candidates.items() if item["score"] > 0}


def keyword_score(queries: list[str], text: str) -> float:
    """Score lexical overlap between retrieval queries and a candidate chunk."""
    text_tokens = set(search_tokens(text))
    if not text_tokens:
        return 0.0
    best = 0.0
    lowered_text = text.lower()
    for query in queries:
        tokens = search_tokens(query)
        if not tokens:
            continue
        overlap = sum(1 for token in tokens if token in text_tokens) / len(tokens)
        phrase_boost = 0.15 if query.lower() in lowered_text else 0.0
        best = max(best, min(1.0, overlap + phrase_boost))
    return best


def search_tokens(text: str) -> list[str]:
    """Tokenize query text for lightweight keyword retrieval."""
    stopwords = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "to",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "要",
        "的",
        "是",
        "有",
        "和",
        "或",
        "在",
        "嗎",
        "呢",
    }
    latin_tokens = [
        token
        for token in re.findall(r"[\w.-]+", text.lower(), flags=re.UNICODE)
        if len(token) > 1 and token not in stopwords
    ]
    cjk_text = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    cjk_tokens = [token for token in cjk_ngrams(cjk_text) if token not in stopwords]
    return latin_tokens + cjk_tokens


def cjk_ngrams(text: str) -> list[str]:
    """Create short CJK character n-grams for keyword matching."""
    if len(text) < 2:
        return []
    grams = [text[index : index + 2] for index in range(len(text) - 1)]
    if len(text) > 2:
        grams.extend(text[index : index + 3] for index in range(len(text) - 2))
    return grams


def citation_payload(chunks: list[dict]) -> list[dict]:
    """Convert retrieved chunks into serializable citation metadata.

    Includes the hybrid / vector / keyword / rerank scores so the chat
    "Why these citations?" debug pane can show why each chunk was picked.
    Scores default to 0.0 — older messages stored before this field existed
    will simply render no debug numbers, which the template handles.
    """
    return [
        {
            "index": index,
            "source_id": chunk.get("source_id"),
            # U3: chunk row id → lets the citation chip open the source preview
            # scrolled to + highlighting this exact chunk (#preview-chunk-{id}).
            "chunk_id": chunk.get("id"),
            "filename": chunk["filename"],
            "location": chunk["location"],
            "snippet": chunk["text"][:260],
            "score": round(float(chunk.get("score", 0.0)), 3),
            "vector_score": round(float(chunk.get("vector_score", 0.0)), 3),
            "keyword_score": round(float(chunk.get("keyword_score", 0.0)), 3),
            "rerank_score": round(float(chunk["rerank_score"]), 3) if chunk.get("rerank_score") is not None else None,
        }
        for index, chunk in enumerate(chunks, start=1)
    ]


def message_with_citations(row) -> dict:
    """Attach decoded citation + per-message metadata to a row dictionary."""
    message = dict(row)
    message["citations"] = loads(message["citations_json"])
    raw_meta = message.get("metadata_json") or "{}"
    try:
        message["metadata"] = loads(raw_meta) if isinstance(raw_meta, str) else (raw_meta or {})
    except Exception:
        message["metadata"] = {}
    return message


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


@app.get("/admin/index", response_class=HTMLResponse)
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


@app.post("/admin/index/rebuild")
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


@app.post("/admin/index/clear")
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


def current_retrieval_profile_params() -> dict[str, Any]:
    """Snapshot runtime-safe retrieval knobs used by the E1 eval workbench."""
    return {
        "low_confidence_threshold": LOW_CONFIDENCE_THRESHOLD,
        "vector_weight": VECTOR_WEIGHT,
        "keyword_weight": KEYWORD_WEIGHT,
        "candidate_pool_size": CANDIDATE_POOL_SIZE,
        "final_chunk_count": FINAL_CHUNK_COUNT,
        "rerank_weight": config.retrieval.rerank_weight,
        "rerank_base_weight": config.retrieval.rerank_base_weight,
    }


PROFILE_PARAM_LABELS = {
    "low_confidence_threshold": "低信心閾值",
    "vector_weight": "Vector 權重",
    "keyword_weight": "Keyword 權重",
    "candidate_pool_size": "候選池大小",
    "final_chunk_count": "最終 chunk 數",
    "rerank_weight": "Rerank 權重",
    "rerank_base_weight": "Rerank base 權重",
}

# Type + range rules for the 7 runtime-safe profile params (E1c authoring form).
# Pool/chunk counts are positive ints; the rest are floats >= 0.
PROFILE_PARAM_INT_KEYS = {"candidate_pool_size", "final_chunk_count"}


def coerce_profile_params(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate/coerce admin-entered profile params; raise HTTP 400 on bad input."""
    params: dict[str, Any] = {}
    for key in PROFILE_PARAM_LABELS:
        if key not in raw or raw[key] in (None, ""):
            raise HTTPException(status_code=400, detail=f"缺少參數：{PROFILE_PARAM_LABELS[key]}")
        try:
            if key in PROFILE_PARAM_INT_KEYS:
                value: Any = int(raw[key])
                if value < 1:
                    raise ValueError
            else:
                value = float(raw[key])
                if value < 0:
                    raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"參數值無效：{PROFILE_PARAM_LABELS[key]}")
        params[key] = value
    return params


def profile_param_rows(params: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"key": key, "label": PROFILE_PARAM_LABELS.get(key, key), "value": params[key]}
        for key in PROFILE_PARAM_LABELS
        if key in params
    ]


def profile_params_for_display(profile: dict) -> list[dict[str, Any]]:
    return profile_param_rows(loads(profile.get("params_json") or "{}"))


# Runtime-safe retrieval params resolved at import as the literal defaults (equal
# to app/config.py), plus a mutable in-process "active" copy the live retrieval
# path reads. The eval workbench (E1c) can apply a saved profile to mutate the
# active copy (and persist it via retrieval_profiles.is_active), or run an eval
# with an isolated per-run override without touching the active copy. Defaults
# stay equal to today's behaviour, so an un-applied deployment is unchanged.
RETRIEVAL_PARAM_DEFAULTS: dict[str, Any] = current_retrieval_profile_params()
ACTIVE_RETRIEVAL_PARAMS: dict[str, Any] = dict(RETRIEVAL_PARAM_DEFAULTS)


def resolve_retrieval_params(params: dict | None) -> dict[str, Any]:
    """Merge a (possibly partial) override over defaults; None → active params."""
    if params is None:
        return dict(ACTIVE_RETRIEVAL_PARAMS)
    merged = dict(RETRIEVAL_PARAM_DEFAULTS)
    merged.update({k: v for k, v in params.items() if k in RETRIEVAL_PARAM_DEFAULTS})
    return merged


def active_retrieval_params() -> dict[str, Any]:
    return dict(ACTIVE_RETRIEVAL_PARAMS)


def active_low_confidence_threshold() -> float:
    return float(ACTIVE_RETRIEVAL_PARAMS.get("low_confidence_threshold", LOW_CONFIDENCE_THRESHOLD))


def set_active_retrieval_params(params: dict | None) -> None:
    """Replace the in-process active retrieval params (apply / startup load)."""
    resolved = resolve_retrieval_params(params)
    ACTIVE_RETRIEVAL_PARAMS.clear()
    ACTIVE_RETRIEVAL_PARAMS.update(resolved)


def load_active_retrieval_profile() -> None:
    """Seed the active retrieval params from the persisted active profile (if any).

    Called at startup so a previously applied profile survives a restart. Falls
    back to the import-time defaults when no profile has been applied yet.
    """
    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT params_json FROM retrieval_profiles WHERE is_active = 1 ORDER BY id ASC LIMIT 1"
            ).fetchone()
    except Exception:
        logger.exception("active_profile_load_failed")
        return
    if row is None:
        set_active_retrieval_params(None)
        return
    set_active_retrieval_params(loads(row["params_json"] or "{}"))
    logger.info("active_profile_loaded params=%s", dumps(ACTIVE_RETRIEVAL_PARAMS))


def ensure_default_retrieval_profile(conn, admin_user_id: int | None = None) -> dict:
    """Ensure there is a baseline profile reflecting current app config."""
    row = conn.execute(
        "SELECT * FROM retrieval_profiles WHERE is_active = 1 ORDER BY id ASC LIMIT 1"
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM retrieval_profiles ORDER BY id ASC LIMIT 1"
        ).fetchone()
    if row is None:
        cursor = conn.execute(
            """
            INSERT INTO retrieval_profiles (name, description, params_json, is_active, is_default, created_by)
            VALUES (?, ?, ?, 1, 1, ?)
            """,
            (
                "目前系統預設",
                "從 app/config.py 目前 retrieval 設定建立的 baseline profile。",
                dumps(current_retrieval_profile_params()),
                admin_user_id,
            ),
        )
        row = conn.execute("SELECT * FROM retrieval_profiles WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row)


def load_eval_set(conn, eval_set_id: int) -> dict:
    row = conn.execute(
        """
        SELECT es.*, n.title AS notebook_title, n.emoji AS notebook_emoji, u.username AS target_username
        FROM eval_sets es
        JOIN notebooks n ON n.id = es.notebook_id
        JOIN users u ON u.id = es.target_user_id
        WHERE es.id = ?
        """,
        (eval_set_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="找不到 eval set")
    return dict(row)


def split_expected_substrings(value: str) -> list[str]:
    """Normalize newline-separated expected evidence snippets from the admin form."""
    return [line.strip() for line in (value or "").splitlines() if line.strip()]


def generated_eval_snippet(text: str, limit: int = 80) -> str:
    snippet = " ".join((text or "").split())
    if len(snippet) <= limit:
        return snippet
    return snippet[:limit].rsplit(" ", 1)[0] or snippet[:limit]


def generated_eval_question(filename: str, snippet: str) -> str:
    anchor = snippet[:72]
    suffix = "..." if len(snippet) > 72 else ""
    return f"「{anchor}{suffix}」這段內容的重點是什麼？來源：{filename}"


EVAL_ITEM_TYPE_LABELS = {
    "answerable": "可回答",
    "cross_lingual": "跨語言",
    "unanswerable": "不可回答",
}


def normalize_eval_item_type(value: str | None) -> str:
    item_type = (value or "answerable").strip().lower()
    return item_type if item_type in EVAL_ITEM_TYPE_LABELS else "answerable"


def eval_item_type_options() -> list[dict[str, str]]:
    return [
        {"value": "answerable", "label": "一般可回答"},
        {"value": "cross_lingual", "label": "跨語言"},
        {"value": "unanswerable", "label": "不可回答"},
    ]


def eval_authoring_chunks(
    conn,
    eval_set: dict,
    source_ids: list[int] | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Fetch bounded chunks for LLM eval authoring, spread across sources."""
    source_ids = [int(value) for value in (source_ids or []) if int(value) > 0]
    params: list[Any] = [eval_set["notebook_id"], eval_set["target_user_id"]]
    source_filter = ""
    if source_ids:
        placeholders = ",".join("?" for _ in source_ids)
        source_filter = f"AND s.id IN ({placeholders})"
        params.extend(source_ids)
    params.append(max(1, min(limit, 24)))
    rows = conn.execute(
        f"""
        WITH eligible AS (
            SELECT
                c.id AS chunk_id,
                c.text,
                c.location,
                c.chunk_index,
                s.id AS source_id,
                s.filename,
                ROW_NUMBER() OVER (
                    PARTITION BY s.id
                    ORDER BY c.chunk_index ASC, c.id ASC
                ) AS chunk_rank
            FROM chunks c
            JOIN sources s ON s.id = c.source_id
            WHERE s.notebook_id = ? AND s.user_id = ? AND s.status = 'indexed'
              AND TRIM(c.text) != ''
              {source_filter}
        )
        SELECT chunk_id, source_id, filename, location, text
        FROM eligible
        ORDER BY chunk_rank ASC, source_id DESC, chunk_id ASC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def eval_item_hit_rank(item: dict, retrieved: list[dict]) -> int | None:
    expected_source_id = item.get("expected_source_id")
    expected_chunk_id = item.get("expected_chunk_id")
    substrings = item.get("expected_substrings") or []
    has_criteria = bool(expected_source_id or expected_chunk_id or substrings)
    if not has_criteria:
        return None
    for rank, chunk in enumerate(retrieved, start=1):
        if expected_source_id and int(chunk.get("source_id") or 0) != int(expected_source_id):
            continue
        if expected_chunk_id and int(chunk.get("id") or 0) != int(expected_chunk_id):
            continue
        if substrings and not any(snippet in chunk.get("text", "") for snippet in substrings):
            continue
        return rank
    return 0


def eval_result_diagnosis(result: dict) -> str:
    if result.get("status") == "hit":
        rank = result.get("hit_rank")
        return f"命中預期依據，rank {rank}。" if rank else "命中預期依據。"
    if result.get("status") == "unscored":
        return "此題沒有設定 expected source/chunk/substrings，因此不納入 Recall/MRR。"
    if result.get("status") == "error":
        return result.get("error") or "執行此題時發生錯誤。"

    retrieved = result.get("retrieved") or []
    if not retrieved:
        return "沒有找回任何 chunk。"
    expected_source_id = result.get("expected_source_id")
    expected_chunk_id = result.get("expected_chunk_id")
    expected_substrings = result.get("expected_substrings") or []
    retrieved_source_ids = {int(chunk.get("source_id") or 0) for chunk in retrieved}
    retrieved_chunk_ids = {int(chunk.get("chunk_id") or 0) for chunk in retrieved}

    reasons = []
    if expected_source_id and int(expected_source_id) not in retrieved_source_ids:
        reasons.append("未找回預期來源。")
    elif expected_source_id:
        reasons.append("有找回同一來源，但不是預期 chunk/片段。")
    if expected_chunk_id and int(expected_chunk_id) not in retrieved_chunk_ids:
        reasons.append("預期 chunk 不在目前 top-k 結果中。")
    if expected_substrings:
        retrieved_text = " ".join(str(chunk.get("snippet") or "") for chunk in retrieved)
        missing = [snippet for snippet in expected_substrings if snippet not in retrieved_text]
        if missing:
            reasons.append("預期 substring 沒出現在 retrieved snippets。")
    return " ".join(reasons) or "找回結果未符合此題的 expected criteria。"


def compact_retrieved_chunks(retrieved: list[dict]) -> list[dict[str, Any]]:
    compact = []
    for rank, chunk in enumerate(retrieved, start=1):
        compact.append({
            "rank": rank,
            "chunk_id": chunk.get("id"),
            "source_id": chunk.get("source_id"),
            "filename": chunk.get("filename", ""),
            "location": chunk.get("location", ""),
            "score": round(float(chunk.get("score") or 0.0), 4),
            "vector_score": round(float(chunk.get("vector_score") or 0.0), 4),
            "keyword_score": round(float(chunk.get("keyword_score") or 0.0), 4),
            "snippet": " ".join((chunk.get("text") or "").split())[:220],
        })
    return compact


def run_metrics_from_results(
    results: list[dict],
    threshold: float | None = None,
    final_chunk_count: int | None = None,
) -> dict[str, Any]:
    if threshold is None:
        threshold = active_low_confidence_threshold()
    if final_chunk_count is None:
        final_chunk_count = int(ACTIVE_RETRIEVAL_PARAMS.get("final_chunk_count", FINAL_CHUNK_COUNT))
    scored = [r for r in results if r["status"] in {"hit", "miss"}]
    hits = [r for r in scored if r["status"] == "hit"]
    total = len(results)
    scored_count = len(scored)
    latencies = [float(r.get("latency_ms") or 0.0) for r in results]
    top_scores = [float(r.get("top_score") or 0.0) for r in results]
    mrr = sum(1 / int(r["hit_rank"]) for r in hits if r.get("hit_rank")) / scored_count if scored_count else 0.0
    recall = len(hits) / scored_count if scored_count else 0.0
    low_confidence = sum(1 for score in top_scores if score < threshold)
    return {
        "total": total,
        "scored": scored_count,
        "hits": len(hits),
        "misses": sum(1 for r in scored if r["status"] == "miss"),
        "unscored": sum(1 for r in results if r["status"] == "unscored"),
        "errors": sum(1 for r in results if r["status"] == "error"),
        "recall_at_k": round(recall, 4),
        "mrr": round(mrr, 4),
        "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
        "avg_top_score": round(sum(top_scores) / len(top_scores), 4) if top_scores else 0.0,
        "low_confidence_rate": round(low_confidence / total, 4) if total else 0.0,
        "threshold": threshold,
        "final_chunk_count": final_chunk_count,
    }


async def run_eval_job(run_id: int) -> None:
    """Background E1b retrieval-only eval runner."""
    try:
        with connect() as conn:
            run = conn.execute("SELECT * FROM eval_runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                return
            # Isolated per-run override: the run uses its frozen profile snapshot,
            # not the live applied profile, so candidate vs baseline comparisons
            # are meaningful without mutating real chat retrieval.
            run_params = resolve_retrieval_params(loads(run["profile_snapshot_json"] or "{}"))
            eval_set = load_eval_set(conn, run["eval_set_id"])
            items = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT * FROM eval_items
                    WHERE eval_set_id = ? AND approved = 1
                    ORDER BY id ASC
                    """,
                    (eval_set["id"],),
                ).fetchall()
            ]
            settings = load_llm_settings(conn) or {}
            source_ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM sources WHERE notebook_id = ? AND user_id = ? AND status = 'indexed'",
                    (eval_set["notebook_id"], eval_set["target_user_id"]),
                ).fetchall()
            ]
            conn.execute(
                """
                UPDATE eval_runs
                SET status = 'running', progress_total = ?, progress_current = 0,
                    current_step = ?, started_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (len(items), "準備 retrieval eval", run_id),
            )
        if not items:
            with connect() as conn:
                conn.execute(
                    """
                    UPDATE eval_runs
                    SET status = 'failed', error = ?, current_step = '',
                        finished_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    ("沒有 approved eval 題目可執行。", run_id),
                )
            return
        if not source_ids:
            with connect() as conn:
                conn.execute(
                    """
                    UPDATE eval_runs
                    SET status = 'failed', error = ?, current_step = '',
                        finished_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    ("這個 eval set 的 notebook 沒有已索引來源。", run_id),
                )
            return

        results: list[dict] = []
        for index, item in enumerate(items, start=1):
            question = item["question"]
            with connect() as conn:
                conn.execute(
                    """
                    UPDATE eval_runs
                    SET progress_current = ?, current_step = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (index - 1, f"檢索第 {index} / {len(items)} 題", run_id),
                )
            started = time.perf_counter()
            try:
                item["expected_substrings"] = loads(item["expected_substrings_json"] or "[]")
                retrieved = await retrieve(
                    question,
                    None,
                    settings,
                    [],
                    eval_set["target_user_id"],
                    source_ids,
                    params=run_params,
                )
                latency_ms = round((time.perf_counter() - started) * 1000, 1)
                top_score = float(retrieved[0].get("score", 0.0)) if retrieved else 0.0
                hit_rank = eval_item_hit_rank(item, retrieved)
                if hit_rank is None:
                    status = "unscored"
                    stored_rank = None
                elif hit_rank > 0:
                    status = "hit"
                    stored_rank = hit_rank
                else:
                    status = "miss"
                    stored_rank = None
                result = {
                    "eval_item_id": item["id"],
                    "status": status,
                    "hit_rank": stored_rank,
                    "top_score": top_score,
                    "latency_ms": latency_ms,
                    "retrieved_json": dumps(compact_retrieved_chunks(retrieved)),
                    "error": "",
                }
            except Exception as exc:
                logger.exception("eval_item_failed run_id=%s item_id=%s", run_id, item["id"])
                result = {
                    "eval_item_id": item["id"],
                    "status": "error",
                    "hit_rank": None,
                    "top_score": 0.0,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                    "retrieved_json": "[]",
                    "error": str(exc)[:300],
                }
            results.append(result)
            with connect() as conn:
                conn.execute(
                    """
                    INSERT INTO eval_results
                    (run_id, eval_item_id, status, hit_rank, top_score, latency_ms, retrieved_json, error)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        result["eval_item_id"],
                        result["status"],
                        result["hit_rank"],
                        result["top_score"],
                        result["latency_ms"],
                        result["retrieved_json"],
                        result["error"],
                    ),
                )
                conn.execute(
                    """
                    UPDATE eval_runs
                    SET progress_current = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (index, run_id),
                )
        metrics = run_metrics_from_results(
            results,
            threshold=float(run_params["low_confidence_threshold"]),
            final_chunk_count=int(run_params["final_chunk_count"]),
        )
        with connect() as conn:
            conn.execute(
                """
                UPDATE eval_runs
                SET status = 'succeeded', progress_current = progress_total,
                    current_step = '', metrics_json = ?, finished_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (dumps(metrics), run_id),
            )
        logger.info("eval_run_completed run_id=%s total=%s hits=%s", run_id, metrics["total"], metrics["hits"])
    except Exception as exc:
        logger.exception("eval_run_failed run_id=%s", run_id)
        with connect() as conn:
            conn.execute(
                """
                UPDATE eval_runs
                SET status = 'failed', error = ?, current_step = '',
                    finished_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (str(exc)[:500], run_id),
            )


def eval_run_context(run_id: int) -> dict[str, Any]:
    with connect() as conn:
        run = conn.execute(
            """
            SELECT er.*, es.name AS eval_set_name, es.notebook_id, n.title AS notebook_title
            FROM eval_runs er
            JOIN eval_sets es ON es.id = er.eval_set_id
            JOIN notebooks n ON n.id = es.notebook_id
            WHERE er.id = ?
            """,
            (run_id,),
        ).fetchone()
        if run is None:
            raise HTTPException(status_code=404, detail="找不到 eval run")
        results = [
            dict(row)
            for row in conn.execute(
                """
                SELECT r.*, i.question, i.expected_source_id, i.expected_chunk_id,
                       i.expected_substrings_json, s.filename AS expected_filename,
                       c.location AS expected_chunk_location,
                       c.text AS expected_chunk_text
                FROM eval_results r
                JOIN eval_items i ON i.id = r.eval_item_id
                LEFT JOIN sources s ON s.id = i.expected_source_id
                LEFT JOIN chunks c ON c.id = i.expected_chunk_id
                WHERE r.run_id = ?
                ORDER BY r.id ASC
                """,
                (run_id,),
            ).fetchall()
        ]
    run_dict = dict(run)
    run_dict["metrics"] = loads(run_dict.get("metrics_json") or "{}")
    run_dict["profile_snapshot"] = loads(run_dict.get("profile_snapshot_json") or "{}")
    run_dict["profile_params"] = profile_param_rows(run_dict["profile_snapshot"])
    for result in results:
        result["retrieved"] = loads(result.get("retrieved_json") or "[]")
        result["expected_substrings"] = loads(result.get("expected_substrings_json") or "[]")
        result["expected_chunk_snippet"] = " ".join((result.get("expected_chunk_text") or "").split())[:220]
        result["diagnosis"] = eval_result_diagnosis(result)
    return {"run": run_dict, "results": results}


def profile_export_payload(profile: dict) -> dict[str, Any]:
    params = loads(profile.get("params_json") or "{}")
    return {
        "export_schema": "retrieval_profile.v1",
        "export_type": "sanitized_profile",
        "profile": {
            "id": profile["id"],
            "name": profile["name"],
            "description": profile["description"],
            "params": params,
            "requires_reindex": bool(profile["requires_reindex"]),
            "is_active": bool(profile["is_active"]),
            "is_default": bool(profile["is_default"]),
            "source_run_id": profile["source_run_id"],
            "created_at": profile["created_at"],
            "updated_at": profile["updated_at"],
        },
    }


def sanitized_run_export_payload(context: dict[str, Any]) -> dict[str, Any]:
    run = context["run"]
    results = context["results"]
    return {
        "export_schema": "eval_run.v1",
        "export_type": "sanitized_run_report",
        "run": {
            "id": run["id"],
            "eval_set_id": run["eval_set_id"],
            "eval_set_name": run["eval_set_name"],
            "status": run["status"],
            "profile_id": run["profile_id"],
            "profile_snapshot": run["profile_snapshot"],
            "metrics": run["metrics"],
            "progress_current": run["progress_current"],
            "progress_total": run["progress_total"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "created_at": run["created_at"],
        },
        "result_summary": {
            "total_results": len(results),
            "hits": sum(1 for item in results if item["status"] == "hit"),
            "misses": sum(1 for item in results if item["status"] == "miss"),
            "unscored": sum(1 for item in results if item["status"] == "unscored"),
            "errors": sum(1 for item in results if item["status"] == "error"),
        },
        "results": [
            {
                "eval_item_id": item["eval_item_id"],
                "status": item["status"],
                "hit_rank": item["hit_rank"],
                "top_score": item["top_score"],
                "latency_ms": item["latency_ms"],
                "retrieved_count": len(item.get("retrieved") or []),
                "has_error": bool(item.get("error")),
            }
            for item in results
        ],
    }


def full_run_export_payload(context: dict[str, Any]) -> dict[str, Any]:
    payload = sanitized_run_export_payload(context)
    payload["export_type"] = "full_internal_run_report"
    payload["warning"] = "Contains eval questions, expected evidence, and retrieved snippets. Keep inside the deployment unless explicitly approved."
    payload["results"] = [
        {
            "eval_item_id": item["eval_item_id"],
            "question": item["question"],
            "status": item["status"],
            "hit_rank": item["hit_rank"],
            "top_score": item["top_score"],
            "latency_ms": item["latency_ms"],
            "diagnosis": item["diagnosis"],
            "error": item.get("error") or "",
            "expected": {
                "source_id": item["expected_source_id"],
                "filename": item["expected_filename"],
                "chunk_id": item["expected_chunk_id"],
                "chunk_location": item["expected_chunk_location"],
                "substrings": item["expected_substrings"],
                "snippet": item["expected_chunk_snippet"],
            },
            "retrieved": item.get("retrieved") or [],
        }
        for item in context["results"]
    ]
    return payload


def eval_set_items_redirect(eval_set_id: int) -> RedirectResponse:
    return RedirectResponse(f"/admin/evals/sets/{eval_set_id}#eval-items", status_code=303)


def eval_set_detail_context(eval_set_id: int) -> dict[str, Any]:
    with connect() as conn:
        eval_set = load_eval_set(conn, eval_set_id)
        sources = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, filename, status
                FROM sources
                WHERE notebook_id = ? AND user_id = ?
                ORDER BY filename
                """,
                (eval_set["notebook_id"], eval_set["target_user_id"]),
            ).fetchall()
        ]
        items = [
            dict(row)
            for row in conn.execute(
                """
                SELECT i.*, s.filename AS expected_filename
                FROM eval_items i LEFT JOIN sources s ON s.id = i.expected_source_id
                WHERE i.eval_set_id = ?
                ORDER BY i.id ASC
                """,
                (eval_set_id,),
            ).fetchall()
        ]
        runs = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM eval_runs WHERE eval_set_id = ? ORDER BY created_at DESC, id DESC LIMIT 21",
                (eval_set_id,),
            ).fetchall()
        ]
        profiles = [
            dict(row)
            for row in conn.execute(
                "SELECT id, name, is_active FROM retrieval_profiles ORDER BY is_active DESC, id DESC LIMIT 50"
            ).fetchall()
        ]
    for item in items:
        item["expected_substrings"] = loads(item.get("expected_substrings_json") or "[]")
        item["metadata"] = loads(item.get("metadata_json") or "{}")
        item["item_type"] = normalize_eval_item_type(item.get("item_type"))
        item["item_type_label"] = EVAL_ITEM_TYPE_LABELS[item["item_type"]]
        origin = item["metadata"].get("origin") or ("manual" if item["approved"] else "draft")
        item["origin_label"] = {
            "llm_generated": "LLM",
            "deterministic": "自動",
            "manual": "手動",
        }.get(origin, origin)
    runs_truncated = len(runs) > 20  # LIMIT+1 → surface the cap (UX review M4)
    runs = runs[:20]
    for run in runs:
        run["metrics"] = loads(run.get("metrics_json") or "{}")
    return {
        "eval_set": eval_set,
        "sources": sources,
        "items": items,
        "approved_count": sum(1 for item in items if item["approved"]),
        "runs": runs,
        "runs_truncated": runs_truncated,
        "profiles": profiles,
        "item_type_options": eval_item_type_options(),
    }


def eval_set_items_response(
    request: Request,
    user: dict,
    eval_set_id: int,
    notice: str = "",
    error: str = "",
):
    context = eval_set_detail_context(eval_set_id)
    context["eval_notice"] = notice
    context["eval_error"] = error
    if request.headers.get("HX-Request") == "true":
        return render(request, "_eval_items_section.html", {"user": user, **context})
    return eval_set_items_redirect(eval_set_id)


@app.get("/admin/evals", response_class=HTMLResponse)
def admin_evals(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
    notebook_q: str = "",
):
    """Admin E1 landing page: eval sets, run history, active profile."""
    notebook_q = notebook_q.strip()[:80]
    notebook_like = f"%{notebook_q}%"
    with connect() as conn:
        profile = ensure_default_retrieval_profile(conn, user["id"])
        notebooks = [
            dict(row)
            for row in conn.execute(
                """
                SELECT n.id, n.title, n.emoji, u.username,
                       COUNT(s.id) AS indexed_count
                FROM notebooks n
                JOIN users u ON u.id = n.user_id
                JOIN sources s ON s.notebook_id = n.id AND s.user_id = n.user_id AND s.status = 'indexed'
                WHERE (? = '' OR n.title LIKE ? OR u.username LIKE ?)
                GROUP BY n.id
                ORDER BY n.updated_at DESC, n.id DESC
                LIMIT 100
                """,
                (notebook_q, notebook_like, notebook_like),
            ).fetchall()
        ]
        eval_sets = [
            dict(row)
            for row in conn.execute(
                """
                SELECT es.*, n.title AS notebook_title, n.emoji AS notebook_emoji,
                       u.username AS target_username,
                       (SELECT COUNT(*) FROM eval_items WHERE eval_set_id = es.id) AS item_count,
                       (SELECT COUNT(*) FROM eval_items WHERE eval_set_id = es.id AND approved = 1) AS approved_count
                FROM eval_sets es
                JOIN notebooks n ON n.id = es.notebook_id
                JOIN users u ON u.id = es.target_user_id
                ORDER BY es.updated_at DESC, es.id DESC
                LIMIT 51
                """
            ).fetchall()
        ]
        runs = [
            dict(row)
            for row in conn.execute(
                """
                SELECT er.*, es.name AS eval_set_name, n.title AS notebook_title
                FROM eval_runs er
                JOIN eval_sets es ON es.id = er.eval_set_id
                JOIN notebooks n ON n.id = es.notebook_id
                ORDER BY er.created_at DESC, er.id DESC
                LIMIT 21
                """
            ).fetchall()
        ]
    # LIMIT+1 → tell the user the list was capped (no pagination; UX review M4).
    eval_sets_truncated = len(eval_sets) > 50
    eval_sets = eval_sets[:50]
    runs_truncated = len(runs) > 20
    runs = runs[:20]
    for run in runs:
        run["metrics"] = loads(run.get("metrics_json") or "{}")
    return render(
        request,
        "admin_evals.html",
        {
            "user": user,
            "active_tab": "sets",
            "profile": profile,
            "notebooks": notebooks,
            "notebook_q": notebook_q,
            "eval_sets": eval_sets,
            "eval_sets_truncated": eval_sets_truncated,
            "runs": runs,
            "runs_truncated": runs_truncated,
        },
    )


@app.get("/admin/evals/profiles", response_class=HTMLResponse)
def admin_profiles_page(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
):
    """E1c: dedicated Retrieval Profiles management page (separate from eval sets)."""
    with connect() as conn:
        ensure_default_retrieval_profile(conn, user["id"])
        profiles = [
            dict(row)
            for row in conn.execute(
                """
                SELECT p.*, u.username AS created_by_username
                FROM retrieval_profiles p
                LEFT JOIN users u ON u.id = p.created_by
                ORDER BY p.is_active DESC, p.id DESC
                LIMIT 50
                """
            ).fetchall()
        ]
        for prof in profiles:
            prof["params"] = profile_params_for_display(prof)
    return render(
        request,
        "admin_profiles.html",
        {
            "user": user,
            "active_tab": "profiles",
            "profiles": profiles,
            "new_profile_fields": profile_param_rows(active_retrieval_params()),
        },
    )


@app.get("/admin/evals/help", response_class=HTMLResponse)
def admin_eval_help(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
):
    """E1f: in-product tuning guide for retrieval profiles."""
    return render(
        request,
        "admin_eval_help.html",
        {
            "user": user,
            "active_tab": "help",
            "current_profile_fields": profile_param_rows(active_retrieval_params()),
        },
    )


@app.post("/admin/evals/sets")
def admin_create_eval_set(
    user: Annotated[dict, Depends(require_admin)],
    notebook_id: int = Form(...),
    name: str = Form(...),
    description: str = Form(""),
):
    clean_name = " ".join(name.split())[:120] or "未命名 Eval Set"
    description = description.strip()[:500]
    with connect() as conn:
        notebook = conn.execute("SELECT id, user_id FROM notebooks WHERE id = ?", (notebook_id,)).fetchone()
        if notebook is None:
            raise HTTPException(status_code=404, detail="找不到筆記本")
        cursor = conn.execute(
            """
            INSERT INTO eval_sets (name, description, target_user_id, notebook_id, created_by)
            VALUES (?, ?, ?, ?, ?)
            """,
            (clean_name, description, notebook["user_id"], notebook_id, user["id"]),
        )
    logger.info("eval_set_created admin_user_id=%s eval_set_id=%s", user["id"], cursor.lastrowid)
    return RedirectResponse(f"/admin/evals/sets/{cursor.lastrowid}", status_code=303)


@app.post("/admin/evals/sets/{eval_set_id}/delete")
def admin_delete_eval_set(
    eval_set_id: int,
    user: Annotated[dict, Depends(require_admin)],
):
    with connect() as conn:
        load_eval_set(conn, eval_set_id)
        active_run = conn.execute(
            """
            SELECT id FROM eval_runs
            WHERE eval_set_id = ? AND status IN ('queued', 'running')
            LIMIT 1
            """,
            (eval_set_id,),
        ).fetchone()
        if active_run is not None:
            raise HTTPException(status_code=400, detail="Eval run 仍在執行，不能刪除此 eval set。")
        conn.execute("DELETE FROM eval_sets WHERE id = ?", (eval_set_id,))
    logger.info("eval_set_deleted admin_user_id=%s eval_set_id=%s", user["id"], eval_set_id)
    return RedirectResponse("/admin/evals", status_code=303)


@app.get("/admin/evals/sets/{eval_set_id}", response_class=HTMLResponse)
def admin_eval_set_detail(
    request: Request,
    eval_set_id: int,
    user: Annotated[dict, Depends(require_admin)],
):
    context = eval_set_detail_context(eval_set_id)
    eval_set = context["eval_set"]
    return render(
        request,
        "admin_eval_set.html",
        {
            "user": user,
            "breadcrumb_items": [
                {"label": i18n.t("eval.title"), "href": "/admin/evals"},
                {"label": eval_set["name"], "href": None},
            ],
            **context,
        },
    )


@app.post("/admin/evals/sets/{eval_set_id}/generate")
def admin_generate_eval_items(
    request: Request,
    eval_set_id: int,
    user: Annotated[dict, Depends(require_admin)],
    count: int = Form(5),
):
    count = max(1, min(int(count), 20))
    created = 0
    with connect() as conn:
        eval_set = load_eval_set(conn, eval_set_id)
        rows = conn.execute(
            """
            WITH eligible AS (
                SELECT
                    c.id AS chunk_id,
                    c.text,
                    s.id AS source_id,
                    s.filename,
                    s.updated_at AS source_updated_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY s.id
                        ORDER BY c.chunk_index ASC, c.id ASC
                    ) AS chunk_rank
                FROM chunks c
                JOIN sources s ON s.id = c.source_id
                WHERE s.notebook_id = ? AND s.user_id = ? AND s.status = 'indexed'
                  AND TRIM(c.text) != ''
                  AND NOT EXISTS (
                      SELECT 1 FROM eval_items i
                      WHERE i.eval_set_id = ? AND i.expected_chunk_id = c.id
                  )
            )
            SELECT chunk_id, text, source_id, filename
            FROM eligible
            ORDER BY chunk_rank ASC, source_updated_at DESC, source_id DESC, chunk_id ASC
            LIMIT ?
            """,
            (eval_set["notebook_id"], eval_set["target_user_id"], eval_set_id, count),
        ).fetchall()
        for row in rows:
            snippet = generated_eval_snippet(row["text"])
            if not snippet:
                continue
            conn.execute(
                """
                INSERT INTO eval_items
                (eval_set_id, question, expected_source_id, expected_chunk_id,
                 expected_substrings_json, item_type, expected_answer, metadata_json, notes, approved)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    eval_set_id,
                    generated_eval_question(row["filename"], snippet),
                    row["source_id"],
                    row["chunk_id"],
                    dumps([snippet]),
                    "answerable",
                    "",
                    dumps({
                        "origin": "deterministic",
                        "prompt_version": "eval_authoring.deterministic.v1",
                        "source_id": row["source_id"],
                        "chunk_id": row["chunk_id"],
                    }),
                    "自動生成候選題；請人工確認後 approve。",
                ),
            )
            created += 1
        if created:
            conn.execute("UPDATE eval_sets SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (eval_set_id,))
    logger.info(
        "eval_items_generated admin_user_id=%s eval_set_id=%s created=%s",
        user["id"],
        eval_set_id,
        created,
    )
    return eval_set_items_response(request, user, eval_set_id)


@app.post("/admin/evals/sets/{eval_set_id}/generate/llm")
async def admin_generate_eval_items_llm(
    request: Request,
    eval_set_id: int,
    user: Annotated[dict, Depends(require_admin)],
    count: int = Form(5),
    item_types: list[str] | None = Form(None),
    source_ids: list[int] | None = Form(None),
    target_language: str = Form("Traditional Chinese"),
):
    count = max(1, min(int(count), 20))
    requested_types = [normalize_eval_item_type(value) for value in (item_types or [])]
    requested_types = list(dict.fromkeys(requested_types)) or ["answerable"]
    target_language = target_language.strip()[:60] or "Traditional Chinese"
    selected_source_ids = [int(value) for value in (source_ids or []) if int(value) > 0]
    with connect() as conn:
        eval_set = load_eval_set(conn, eval_set_id)
        settings = load_llm_settings(conn) or {}
        chunks = eval_authoring_chunks(conn, eval_set, selected_source_ids, limit=max(count * 3, 8))
    if not settings.get("api_key") or not settings.get("chat_model"):
        return eval_set_items_response(
            request,
            user,
            eval_set_id,
            error="尚未完成 LLM 設定，無法使用 LLM 生成候選題；可先使用 deterministic 自動生成或手動新增。",
        )
    if not chunks:
        return eval_set_items_response(
            request,
            user,
            eval_set_id,
            error="找不到可用的 indexed chunks；請確認來源已完成索引，或調整來源選擇。",
        )
    candidates = await generate_eval_candidates(
        chunks,
        settings,
        count=count,
        item_types=requested_types,
        target_language=target_language,
    )
    if not candidates:
        return eval_set_items_response(
            request,
            user,
            eval_set_id,
            error="LLM 沒有產生可用候選題；請減少來源範圍、調整題型，或稍後再試。",
        )

    chunk_by_id = {int(chunk["chunk_id"]): chunk for chunk in chunks}
    source_ids_available = {int(chunk["source_id"]) for chunk in chunks}
    created = 0
    skipped = 0
    with connect() as conn:
        load_eval_set(conn, eval_set_id)
        for candidate in candidates:
            item_type = normalize_eval_item_type(candidate.get("item_type"))
            question = " ".join(str(candidate.get("question") or "").split())[:500]
            if not question:
                skipped += 1
                continue

            chunk = chunk_by_id.get(int(candidate.get("chunk_id") or 0))
            expected_source_id = candidate.get("source_id")
            expected_chunk_id = None
            expected_substrings: list[str] = []
            if item_type != "unanswerable":
                if chunk is None and expected_source_id not in source_ids_available:
                    skipped += 1
                    continue
                if chunk is not None:
                    expected_chunk_id = chunk["chunk_id"]
                    expected_source_id = chunk["source_id"]
                    chunk_text = chunk.get("text") or ""
                    expected_substrings = [
                        value
                        for value in (candidate.get("expected_substrings") or [])
                        if value and value in chunk_text
                    ][:3]
                    if not expected_substrings:
                        snippet = generated_eval_snippet(chunk_text)
                        expected_substrings = [snippet] if snippet else []
                elif expected_source_id not in source_ids_available:
                    skipped += 1
                    continue
            else:
                expected_source_id = None
                expected_chunk_id = None

            duplicate = conn.execute(
                """
                SELECT id FROM eval_items
                WHERE eval_set_id = ? AND question = ?
                LIMIT 1
                """,
                (eval_set_id, question),
            ).fetchone()
            if duplicate is not None:
                skipped += 1
                continue

            metadata = {
                "origin": "llm_generated",
                "prompt_version": "eval_authoring.llm.v1",
                "model": settings.get("chat_model") or "",
                "target_language": target_language,
                "requested_item_types": requested_types,
                "selected_source_ids": selected_source_ids,
                "generated_source_id": expected_source_id,
                "generated_chunk_id": expected_chunk_id,
            }
            conn.execute(
                """
                INSERT INTO eval_items
                (eval_set_id, question, expected_source_id, expected_chunk_id,
                 expected_substrings_json, item_type, expected_answer, metadata_json, notes, approved)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    eval_set_id,
                    question,
                    expected_source_id,
                    expected_chunk_id,
                    dumps(expected_substrings),
                    item_type,
                    str(candidate.get("expected_answer") or "")[:800],
                    dumps(metadata),
                    (str(candidate.get("rationale") or "LLM 生成候選題；請人工確認後 approve。"))[:500],
                ),
            )
            created += 1
        if created:
            conn.execute("UPDATE eval_sets SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (eval_set_id,))
    logger.info(
        "eval_items_generated_llm admin_user_id=%s eval_set_id=%s created=%s skipped=%s",
        user["id"],
        eval_set_id,
        created,
        skipped,
    )
    return eval_set_items_response(
        request,
        user,
        eval_set_id,
        notice=f"LLM 已建立 {created} 題 draft 候選題；略過 {skipped} 題無效或重複輸出。",
    )


@app.post("/admin/evals/sets/{eval_set_id}/items")
def admin_add_eval_item(
    request: Request,
    eval_set_id: int,
    user: Annotated[dict, Depends(require_admin)],
    question: str = Form(...),
    expected_source_id: str = Form(""),
    expected_substrings: str = Form(""),
    notes: str = Form(""),
):
    question = " ".join(question.split())[:500]
    if not question:
        raise HTTPException(status_code=400, detail="問題不可為空。")
    try:
        source_id = int(expected_source_id) if str(expected_source_id).strip() else None
    except ValueError:
        raise HTTPException(status_code=400, detail="預期來源格式不正確。") from None
    snippets = split_expected_substrings(expected_substrings)
    with connect() as conn:
        eval_set = load_eval_set(conn, eval_set_id)
        if source_id is not None:
            row = conn.execute(
                "SELECT id FROM sources WHERE id = ? AND notebook_id = ? AND user_id = ?",
                (source_id, eval_set["notebook_id"], eval_set["target_user_id"]),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=400, detail="預期來源不屬於此 eval set。")
        conn.execute(
            """
            INSERT INTO eval_items
            (eval_set_id, question, expected_source_id, expected_substrings_json, notes, approved)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (eval_set_id, question, source_id, dumps(snippets), notes.strip()[:500]),
        )
        conn.execute("UPDATE eval_sets SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (eval_set_id,))
    logger.info("eval_item_created admin_user_id=%s eval_set_id=%s", user["id"], eval_set_id)
    return eval_set_items_response(request, user, eval_set_id)


@app.post("/admin/evals/sets/{eval_set_id}/items/{item_id}/approve")
def admin_approve_eval_item(
    request: Request,
    eval_set_id: int,
    item_id: int,
    user: Annotated[dict, Depends(require_admin)],
):
    with connect() as conn:
        load_eval_set(conn, eval_set_id)
        updated = conn.execute(
            """
            UPDATE eval_items
            SET approved = 1, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND eval_set_id = ?
            RETURNING id
            """,
            (item_id, eval_set_id),
        ).fetchone()
        if updated is None:
            raise HTTPException(status_code=404, detail="找不到 eval item")
        conn.execute("UPDATE eval_sets SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (eval_set_id,))
    logger.info("eval_item_approved admin_user_id=%s eval_set_id=%s item_id=%s", user["id"], eval_set_id, item_id)
    return eval_set_items_response(request, user, eval_set_id)


@app.post("/admin/evals/sets/{eval_set_id}/items/{item_id}/delete")
def admin_delete_eval_item(
    request: Request,
    eval_set_id: int,
    item_id: int,
    user: Annotated[dict, Depends(require_admin)],
):
    with connect() as conn:
        load_eval_set(conn, eval_set_id)
        used_in_run = conn.execute(
            "SELECT id FROM eval_results WHERE eval_item_id = ? LIMIT 1",
            (item_id,),
        ).fetchone()
        if used_in_run is not None:
            raise HTTPException(status_code=400, detail="此題目已有 run result，不能刪除以免破壞歷史紀錄。")
        deleted = conn.execute(
            "DELETE FROM eval_items WHERE id = ? AND eval_set_id = ? RETURNING id",
            (item_id, eval_set_id),
        ).fetchone()
        if deleted is None:
            raise HTTPException(status_code=404, detail="找不到 eval item")
        conn.execute("UPDATE eval_sets SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (eval_set_id,))
    logger.info("eval_item_deleted admin_user_id=%s eval_set_id=%s item_id=%s", user["id"], eval_set_id, item_id)
    return eval_set_items_response(request, user, eval_set_id)


@app.post("/admin/evals/sets/{eval_set_id}/run")
def admin_start_eval_run(
    eval_set_id: int,
    background_tasks: BackgroundTasks,
    user: Annotated[dict, Depends(require_admin)],
    profile_id: int | None = Form(None),
):
    with connect() as conn:
        load_eval_set(conn, eval_set_id)
        profile = ensure_default_retrieval_profile(conn, user["id"])
        if profile_id is not None:
            chosen = conn.execute(
                "SELECT * FROM retrieval_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
            if chosen is None:
                raise HTTPException(status_code=404, detail="找不到 retrieval profile")
            profile = dict(chosen)
        approved_count = conn.execute(
            "SELECT COUNT(*) AS n FROM eval_items WHERE eval_set_id = ? AND approved = 1",
            (eval_set_id,),
        ).fetchone()["n"]
        if approved_count == 0:
            raise HTTPException(status_code=400, detail="至少需要一題 approved eval item。")
        cursor = conn.execute(
            """
            INSERT INTO eval_runs
            (eval_set_id, profile_id, created_by, status, progress_total, profile_snapshot_json, current_step)
            VALUES (?, ?, ?, 'queued', ?, ?, '等待背景執行')
            """,
            (eval_set_id, profile["id"], user["id"], approved_count, profile["params_json"]),
        )
        run_id = cursor.lastrowid
    background_tasks.add_task(run_eval_job, run_id)
    logger.info("eval_run_queued admin_user_id=%s eval_set_id=%s run_id=%s", user["id"], eval_set_id, run_id)
    return RedirectResponse(f"/admin/evals/runs/{run_id}", status_code=303)


@app.get("/admin/evals/runs/{run_id}", response_class=HTMLResponse)
def admin_eval_run_detail(
    request: Request,
    run_id: int,
    user: Annotated[dict, Depends(require_admin)],
):
    context = eval_run_context(run_id)
    run = context["run"]
    return render(
        request,
        "admin_eval_run.html",
        {
            "user": user,
            "breadcrumb_items": [
                {"label": i18n.t("eval.title"), "href": "/admin/evals"},
                {"label": run["eval_set_name"], "href": f"/admin/evals/sets/{run['eval_set_id']}"},
                {"label": f"Run #{run['id']}", "href": None},
            ],
            **context,
        },
    )


@app.get("/admin/evals/runs/{run_id}/_status", response_class=HTMLResponse)
def admin_eval_run_status(
    request: Request,
    run_id: int,
    user: Annotated[dict, Depends(require_admin)],
):
    return render(request, "_eval_run_status.html", {"user": user, **eval_run_context(run_id)})


@app.get("/admin/evals/runs/{run_id}/_results", response_class=HTMLResponse)
def admin_eval_run_results(
    request: Request,
    run_id: int,
    user: Annotated[dict, Depends(require_admin)],
):
    return render(request, "_eval_run_results.html", {"user": user, **eval_run_context(run_id)})


@app.get("/admin/evals/runs/{run_id}/export/sanitized")
def admin_export_eval_run_sanitized(
    request: Request,
    run_id: int,
    user: Annotated[dict, Depends(require_admin)],
):
    context = eval_run_context(run_id)
    payload = sanitized_run_export_payload(context)
    record_audit_event(
        request,
        user,
        "eval_run_export_sanitized",
        "eval_run",
        run_id,
        {
            "eval_set_id": context["run"]["eval_set_id"],
            "status": context["run"]["status"],
            "result_count": len(context["results"]),
        },
        "normal",
    )
    logger.info("eval_run_exported_sanitized admin_user_id=%s run_id=%s", user["id"], run_id)
    return _json_download(payload, f"eval-run-{run_id}-sanitized.json")


@app.get("/admin/evals/runs/{run_id}/export/full")
def admin_export_eval_run_full(
    request: Request,
    run_id: int,
    user: Annotated[dict, Depends(require_admin)],
    confirm: int = 0,
):
    if confirm != 1:
        raise HTTPException(status_code=400, detail="Full internal report export requires explicit confirmation.")
    context = eval_run_context(run_id)
    payload = full_run_export_payload(context)
    record_audit_event(
        request,
        user,
        "eval_run_export_full",
        "eval_run",
        run_id,
        {
            "eval_set_id": context["run"]["eval_set_id"],
            "status": context["run"]["status"],
            "result_count": len(context["results"]),
            "contains_questions": True,
            "contains_expected_evidence": True,
            "contains_retrieved_snippets": True,
        },
        "high",
    )
    logger.info("eval_run_exported_full admin_user_id=%s run_id=%s", user["id"], run_id)
    return _json_download(payload, f"eval-run-{run_id}-full-internal.json")


# --- E1c: retrieval profile authoring + apply/rollback + run comparison ---

@app.get("/admin/evals/profiles/{profile_id}/export")
def admin_export_profile(
    request: Request,
    profile_id: int,
    user: Annotated[dict, Depends(require_admin)],
):
    with connect() as conn:
        profile = conn.execute("SELECT * FROM retrieval_profiles WHERE id = ?", (profile_id,)).fetchone()
    if profile is None:
        raise HTTPException(status_code=404, detail="找不到 retrieval profile")
    payload = profile_export_payload(dict(profile))
    record_audit_event(
        request,
        user,
        "retrieval_profile_export_sanitized",
        "retrieval_profile",
        profile_id,
        {
            "name": profile["name"],
            "requires_reindex": bool(profile["requires_reindex"]),
            "is_active": bool(profile["is_active"]),
        },
        "normal",
    )
    logger.info("eval_profile_exported admin_user_id=%s profile_id=%s", user["id"], profile_id)
    return _json_download(payload, f"retrieval-profile-{profile_id}.json")

@app.post("/admin/evals/profiles")
def admin_create_profile(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
    name: str = Form(...),
    description: str = Form(""),
    low_confidence_threshold: str = Form(...),
    vector_weight: str = Form(...),
    keyword_weight: str = Form(...),
    candidate_pool_size: str = Form(...),
    final_chunk_count: str = Form(...),
    rerank_weight: str = Form(...),
    rerank_base_weight: str = Form(...),
):
    """Create a candidate retrieval profile (runtime-safe params only, inactive)."""
    name = name.strip()[:120]
    if not name:
        raise HTTPException(status_code=400, detail="Profile 名稱不可為空。")
    params = coerce_profile_params({
        "low_confidence_threshold": low_confidence_threshold,
        "vector_weight": vector_weight,
        "keyword_weight": keyword_weight,
        "candidate_pool_size": candidate_pool_size,
        "final_chunk_count": final_chunk_count,
        "rerank_weight": rerank_weight,
        "rerank_base_weight": rerank_base_weight,
    })
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO retrieval_profiles
            (name, description, params_json, requires_reindex, is_active, created_by)
            VALUES (?, ?, ?, 0, 0, ?)
            """,
            (name, description.strip()[:500], dumps(params), user["id"]),
        )
    profile_id = cursor.lastrowid
    record_audit_event(
        request,
        user,
        "retrieval_profile_created",
        "retrieval_profile",
        profile_id,
        {"name": name, "params": params, "requires_reindex": False},
        "normal",
    )
    logger.info("eval_profile_created admin_user_id=%s profile_id=%s", user["id"], profile_id)
    return RedirectResponse("/admin/evals/profiles", status_code=303)


@app.post("/admin/evals/profiles/{profile_id}/delete")
def admin_delete_profile(
    request: Request,
    profile_id: int,
    user: Annotated[dict, Depends(require_admin)],
):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM retrieval_profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="找不到 retrieval profile")
        if row["is_default"]:
            raise HTTPException(status_code=400, detail="系統預設 profile 不能刪除，它是回復預設值的保底。")
        if row["is_active"]:
            raise HTTPException(status_code=400, detail="作用中的 profile 不能刪除，請先套用其他 profile。")
        conn.execute("DELETE FROM retrieval_profiles WHERE id = ?", (profile_id,))
    record_audit_event(
        request,
        user,
        "retrieval_profile_deleted",
        "retrieval_profile",
        profile_id,
        {"name": row["name"], "requires_reindex": bool(row["requires_reindex"])},
        "normal",
    )
    logger.info("eval_profile_deleted admin_user_id=%s profile_id=%s", user["id"], profile_id)
    return RedirectResponse("/admin/evals/profiles", status_code=303)


@app.post("/admin/evals/profiles/{profile_id}/apply")
def admin_apply_profile(
    request: Request,
    profile_id: int,
    user: Annotated[dict, Depends(require_admin)],
):
    """Apply a runtime-safe profile to live retrieval; persisted via is_active."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM retrieval_profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="找不到 retrieval profile")
        if row["requires_reindex"]:
            raise HTTPException(
                status_code=400,
                detail="此 profile 含需重建索引的參數，不能直接套用；請改用 Clear/Rebuild 流程。",
            )
        previous = conn.execute(
            "SELECT id, name, params_json FROM retrieval_profiles WHERE is_active = 1 ORDER BY id ASC LIMIT 1"
        ).fetchone()
        conn.execute("UPDATE retrieval_profiles SET is_active = 0 WHERE is_active = 1")
        conn.execute(
            "UPDATE retrieval_profiles SET is_active = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (profile_id,),
        )
    set_active_retrieval_params(loads(row["params_json"] or "{}"))
    record_audit_event(
        request,
        user,
        "retrieval_profile_applied",
        "retrieval_profile",
        profile_id,
        {
            "name": row["name"],
            "previous_profile_id": previous["id"] if previous else None,
            "previous_profile_name": previous["name"] if previous else "",
            "params": loads(row["params_json"] or "{}"),
        },
        "high",
    )
    logger.info(
        "eval_profile_applied admin_user_id=%s profile_id=%s params=%s",
        user["id"], profile_id, dumps(ACTIVE_RETRIEVAL_PARAMS),
    )
    return RedirectResponse("/admin/evals/profiles", status_code=303)


def compare_runs_context(base_id: int, candidate_id: int) -> dict[str, Any]:
    """Build a side-by-side comparison of two succeeded runs in the same eval set."""
    base = eval_run_context(base_id)
    candidate = eval_run_context(candidate_id)
    base_run, candidate_run = base["run"], candidate["run"]
    if base_run["eval_set_id"] != candidate_run["eval_set_id"]:
        raise HTTPException(status_code=400, detail="只能比較同一個 eval set 的 run。")
    for run in (base_run, candidate_run):
        if run["status"] != "succeeded":
            raise HTTPException(status_code=400, detail="只能比較已成功完成的 run。")

    # Param diff: union of both snapshots, flag changed rows.
    base_params = base_run["profile_snapshot"]
    cand_params = candidate_run["profile_snapshot"]
    param_diff = []
    for key in PROFILE_PARAM_LABELS:
        if key not in base_params and key not in cand_params:
            continue
        bval, cval = base_params.get(key), cand_params.get(key)
        param_diff.append({
            "label": PROFILE_PARAM_LABELS.get(key, key),
            "base": bval,
            "candidate": cval,
            "changed": bval != cval,
        })

    # Metric diff: lower-is-better for latency + low-confidence rate.
    base_metrics = base_run["metrics"]
    cand_metrics = candidate_run["metrics"]
    metric_specs = [
        ("recall_at_k", "Recall@k", True),
        ("mrr", "MRR", True),
        ("avg_top_score", "平均 top score", True),
        ("avg_latency_ms", "平均延遲 (ms)", False),
        ("low_confidence_rate", "低信心率", False),
        ("hits", "命中數", True),
    ]
    metric_diff = []
    for key, label, higher_better in metric_specs:
        bval = float(base_metrics.get(key) or 0.0)
        cval = float(cand_metrics.get(key) or 0.0)
        delta = round(cval - bval, 4)
        if delta == 0:
            direction = "same"
        elif (delta > 0) == higher_better:
            direction = "better"
        else:
            direction = "worse"
        metric_diff.append({
            "label": label, "base": bval, "candidate": cval,
            "delta": delta, "direction": direction,
        })

    # Per-question delta keyed by eval_item_id.
    base_by_item = {r["eval_item_id"]: r for r in base["results"]}
    cand_by_item = {r["eval_item_id"]: r for r in candidate["results"]}
    rank_order = {"hit": 0, "miss": 1, "unscored": 2, "error": 3, "pending": 4}
    question_diff = []
    for item_id in sorted(set(base_by_item) | set(cand_by_item)):
        b = base_by_item.get(item_id)
        c = cand_by_item.get(item_id)
        bstatus = b["status"] if b else "—"
        cstatus = c["status"] if c else "—"
        brank = b.get("hit_rank") if b else None
        crank = c.get("hit_rank") if c else None
        if b and c:
            bscore, cscore = rank_order.get(bstatus, 9), rank_order.get(cstatus, 9)
            if cscore < bscore or (bstatus == cstatus == "hit" and crank and brank and crank < brank):
                trend = "improved"
            elif cscore > bscore or (bstatus == cstatus == "hit" and crank and brank and crank > brank):
                trend = "regressed"
            else:
                trend = "unchanged"
        else:
            trend = "unchanged"
        question_diff.append({
            "question": (b or c)["question"],
            "base_status": bstatus, "candidate_status": cstatus,
            "base_rank": brank, "candidate_rank": crank,
            "trend": trend,
        })

    return {
        "base_run": base_run,
        "candidate_run": candidate_run,
        "param_diff": param_diff,
        "metric_diff": metric_diff,
        "question_diff": question_diff,
        "improved": sum(1 for q in question_diff if q["trend"] == "improved"),
        "regressed": sum(1 for q in question_diff if q["trend"] == "regressed"),
    }


@app.get("/admin/evals/compare", response_class=HTMLResponse)
def admin_eval_compare(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
    base: int,
    candidate: int,
):
    context = compare_runs_context(base, candidate)
    base_run = context["base_run"]
    return render(
        request,
        "admin_eval_compare.html",
        {
            "user": user,
            "breadcrumb_items": [
                {"label": i18n.t("eval.title"), "href": "/admin/evals"},
                {"label": base_run["eval_set_name"], "href": f"/admin/evals/sets/{base_run['eval_set_id']}"},
                {"label": f"比較 #{context['base_run']['id']} ↔ #{context['candidate_run']['id']}", "href": None},
            ],
            **context,
        },
    )


@app.get("/admin/audit", response_class=HTMLResponse)
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


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, user: Annotated[dict, Depends(require_admin)]):
    """List all user accounts (admin only)."""
    with connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT id, username, is_admin, created_at FROM users ORDER BY id ASC"
            ).fetchall()
        ]
    return render(request, "admin_users.html", {"user": user, "users": rows, "error": "", "saved": False})


@app.post("/admin/users/new")
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
        rows = [dict(r) for r in conn.execute("SELECT id, username, is_admin, created_at FROM users ORDER BY id ASC").fetchall()]
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


@app.post("/admin/users/{target_id}/reset-password")
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
        target = conn.execute("SELECT username FROM users WHERE id = ?", (target_id,)).fetchone()
        if target is None:
            raise HTTPException(status_code=404, detail="User not found")
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


@app.post("/admin/users/{target_id}/toggle-admin")
def admin_toggle_admin(
    request: Request,
    target_id: int,
    user: Annotated[dict, Depends(require_admin)],
):
    """Flip the is_admin flag for another user. Refuses to demote the last admin."""
    if target_id == user["id"]:
        raise HTTPException(status_code=400, detail="You cannot change your own admin flag.")
    with connect() as conn:
        target = conn.execute("SELECT username, is_admin FROM users WHERE id = ?", (target_id,)).fetchone()
        if target is None:
            raise HTTPException(status_code=404, detail="User not found")
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


@app.post("/admin/users/{target_id}/delete")
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


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: Annotated[dict, Depends(require_admin)]):
    """Render the admin LLM settings page without exposing the API key."""
    with connect() as conn:
        settings = load_llm_settings_for_display(conn)
    return render(request, "settings.html", {"user": user, "settings": settings, "saved": False})


@app.post("/settings")
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
):
    """Validate and save global LLM provider settings.

    Probes the embedding endpoint before persisting when embedding-affecting
    fields changed, so connectivity errors and dim mismatches surface at
    save time instead of at first ingest.
    """
    if provider not in {"openai_compatible", "azure_openai"}:
        raise HTTPException(status_code=400, detail="Unsupported LLM provider")

    with connect() as conn:
        existing_row = conn.execute("SELECT * FROM llm_settings WHERE id = 1").fetchone()
        existing = dict(existing_row) if existing_row else {}
        # The stored api_key is either Fernet ciphertext or legacy plaintext.
        # Either way it is opaque to us here; we just keep it if the form
        # field was left blank (the "keep existing" UX).
        if api_key.strip():
            stored_key = encrypt_for_storage(api_key.strip())
        else:
            stored_key = existing.get("api_key", "")

        # Decide whether the embedding endpoint changed materially. If it
        # didn't, skip the probe — admins editing temperature shouldn't be
        # forced to be online with the LLM service to save settings.
        embedding_changed = (
            embedding_model.strip() != (existing.get("embedding_model") or "")
            or embedding_base_url.strip() != (existing.get("embedding_base_url") or "")
            or base_url.strip() != (existing.get("base_url") or "")
            or provider != (existing.get("provider") or "openai_compatible")
            or bool(api_key.strip())
        )

    if embedding_changed and embedding_model.strip():
        # Build a candidate settings dict with the plaintext key so we can
        # actually call the API. Falls back to the existing decrypted key
        # when the form field was left blank.
        with connect() as conn:
            existing_decrypted = load_llm_settings(conn) or {}
        probe_settings = {
            "provider": provider,
            "base_url": base_url.strip(),
            "embedding_base_url": embedding_base_url.strip(),
            "api_key": api_key.strip() or existing_decrypted.get("api_key", ""),
            "embedding_model": embedding_model.strip(),
            "api_version": api_version.strip(),
            "timeout_seconds": timeout_seconds,
        }
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
                api_version = ?, temperature = ?, timeout_seconds = ?
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
        "embedding_changed": embedding_changed,
    }
    record_audit_event(
        request,
        user,
        "llm_settings_updated",
        "llm_settings",
        1,
        audited_fields,
        "high" if embedding_changed or api_key.strip() else "normal",
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
    return render(request, "settings.html", {"user": user, "settings": settings, "saved": True})
