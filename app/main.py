import os
import re
import shutil
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
import time
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .db import UPLOAD_DIR, connect, dumps, encrypt_for_storage, init_db, load_llm_settings, load_llm_settings_for_display, loads
from .ingest import process_source, supported
import httpx

from .llm import close_http_client, compare_sources, cosine, embed_texts, generate_answer, generate_briefing, generate_starter_questions, probe_embedding_dimension, rerank_chunks, set_http_client, rewrite_search_queries
from .security import get_app_secret, hash_password, sign_user_id, unsign_user_id, verify_password
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
    set_http_client(httpx.AsyncClient(timeout=None))
    sync_from_sqlite()
    logger.info(
        "app_started log_level=%s log_file=%s data_dir=%s",
        LOG_LEVEL, LOG_FILE, os.environ.get("NOTEBOOKLM_DATA_DIR", "data"),
    )
    try:
        yield
    finally:
        await close_http_client()
        logger.info("app_stopped")


app = FastAPI(title="NotebookLM-like RAG POC", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


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
        raise HTTPException(status_code=403, detail="Admin only")
    return user


def render(request: Request, name: str, context: dict, status_code: int = 200) -> HTMLResponse:
    """Render a Jinja template with shared request context."""
    return templates.TemplateResponse(
        request,
        name,
        {"request": request, **context},
        status_code=status_code,
    )


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


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    """Render the login form."""
    return render(request, "login.html", {"error": ""})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    """Authenticate a username and password and issue a session cookie."""
    with connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not user or not verify_password(password, user["password_hash"]):
        logger.warning("login_failed username=%s", username)
        return render(request, "login.html", {"error": "Invalid username or password."}, 400)
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


SUGGESTIONS_TTL_HOURS = 24
BRIEFING_TTL_HOURS = 24

# In-process lock for briefing generation. Keyed by notebook_id, value is the
# unix timestamp when generation started. Used to dedupe concurrent POSTs that
# fire when multiple sources finish indexing within seconds of each other
# during a multi-file upload (each fires sources-changed → auto-fire POST).
# Stale entries past BRIEFING_LOCK_TIMEOUT_S are treated as released so a
# crashed request can't permanently block regeneration.
# NOTE: in-process dict, single-uvicorn-worker assumption — fine for POC.
# A multi-worker / multi-process deploy would need a SQLite row, Redis key,
# or filesystem lockfile instead.
_briefing_in_progress: dict[int, float] = {}
BRIEFING_LOCK_TIMEOUT_S = 90


def _briefing_locked(notebook_id: int) -> bool:
    """True if a non-stale briefing generation is currently in flight."""
    started = _briefing_in_progress.get(notebook_id)
    if started is None:
        return False
    if time.time() - started > BRIEFING_LOCK_TIMEOUT_S:
        _briefing_in_progress.pop(notebook_id, None)
        return False
    return True


def _acquire_briefing_lock(notebook_id: int) -> bool:
    """Try to take the briefing lock. Returns True if acquired, False if busy."""
    if _briefing_locked(notebook_id):
        return False
    _briefing_in_progress[notebook_id] = time.time()
    return True


def _release_briefing_lock(notebook_id: int) -> None:
    """Release the briefing lock. Safe to call when not held."""
    _briefing_in_progress.pop(notebook_id, None)


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
        raise HTTPException(status_code=404, detail="Notebook not found")
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
    user: Annotated[dict, Depends(require_login)],
    title: str = Form("Untitled notebook"),
    emoji: str = Form("📓"),
    description: str = Form(""),
):
    """Create a notebook and redirect into its workspace."""
    title = title.strip() or "Untitled notebook"
    description = description.strip()[:280]
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO notebooks (user_id, title, emoji, description) VALUES (?, ?, ?, ?)",
            (user["id"], title, (emoji or "📓").strip()[:8] or "📓", description),
        )
        notebook_id = cursor.lastrowid
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
            "SELECT * FROM conversations WHERE notebook_id = ? AND user_id = ? ORDER BY updated_at DESC, id DESC",
            (notebook_id, user["id"]), CONVERSATIONS_LIMIT,
        )
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
                raise HTTPException(status_code=404, detail="Conversation not found")
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
            "error": "",
            "wide": True,
            "breadcrumb": notebook["title"],
        },
    )


@app.post("/notebooks/{notebook_id}/rename")
def rename_notebook(
    notebook_id: int,
    user: Annotated[dict, Depends(require_login)],
    title: str = Form(...),
    emoji: str = Form(""),
    description: str = Form(""),
):
    """Update a notebook's title, emoji, and description."""
    title = title.strip() or "Untitled notebook"
    emoji = (emoji or "").strip()[:8]
    description = description.strip()[:280]
    with connect() as conn:
        get_notebook(conn, notebook_id, user["id"])
        conn.execute(
            "UPDATE notebooks SET title = ?, emoji = ?, description = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
            (title, emoji, description, notebook_id, user["id"]),
        )
    logger.info("notebook_renamed user_id=%s notebook_id=%s", user["id"], notebook_id)
    return RedirectResponse(f"/notebooks/{notebook_id}", status_code=303)


@app.post("/notebooks/{notebook_id}/delete")
def delete_notebook(notebook_id: int, user: Annotated[dict, Depends(require_login)]):
    """Delete a notebook and cascade its sources, chunks, conversations, and notes."""
    with connect() as conn:
        get_notebook(conn, notebook_id, user["id"])
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
    logger.info("notebook_deleted user_id=%s notebook_id=%s sources=%s", user["id"], notebook_id, len(sources))
    return RedirectResponse("/notebooks", status_code=303)


UPLOAD_BATCH_LIMIT = 5


@app.post("/notebooks/{notebook_id}/sources/upload")
async def upload_source(
    notebook_id: int,
    background: BackgroundTasks,
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
            detail="LLM is not configured. An admin must set the embedding model and chat model at /settings before sources can be indexed.",
        )
    if not files:
        raise HTTPException(status_code=400, detail="No files selected.")
    if len(files) > UPLOAD_BATCH_LIMIT:
        raise HTTPException(status_code=400, detail=f"Upload at most {UPLOAD_BATCH_LIMIT} files at a time.")
    for upload in files:
        if not upload.filename or not supported(upload.filename):
            logger.warning("source_upload_rejected user_id=%s notebook_id=%s filename=%s", user["id"], notebook_id, upload.filename)
            raise HTTPException(status_code=400, detail=f"Unsupported file type: {upload.filename or '(unnamed)'}")

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
        background.add_task(process_source, source_id)
        queued_source_ids.append(source_id)
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
    notebook_id: int,
    source_id: int,
    background: BackgroundTasks,
    user: Annotated[dict, Depends(require_login)],
):
    """Schedule reindexing for a source within a specific notebook."""
    with connect() as conn:
        get_notebook(conn, notebook_id, user["id"])
        source = conn.execute(
            "SELECT id FROM sources WHERE id = ? AND notebook_id = ? AND user_id = ?",
            (source_id, notebook_id, user["id"]),
        ).fetchone()
        if source is None:
            logger.warning("source_reindex_missing user_id=%s notebook_id=%s source_id=%s", user["id"], notebook_id, source_id)
            raise HTTPException(status_code=404, detail="Source not found")
        touch_notebook(conn, notebook_id)
    background.add_task(process_source, source_id)
    logger.info("source_reindex_requested user_id=%s notebook_id=%s source_id=%s", user["id"], notebook_id, source_id)
    return RedirectResponse(f"/notebooks/{notebook_id}", status_code=303)


@app.post("/notebooks/{notebook_id}/sources/{source_id}/delete")
def delete_source(notebook_id: int, source_id: int, user: Annotated[dict, Depends(require_login)]):
    """Delete a source within a specific notebook and clean up vectors and the file."""
    with connect() as conn:
        get_notebook(conn, notebook_id, user["id"])
        source = conn.execute(
            "SELECT * FROM sources WHERE id = ? AND notebook_id = ? AND user_id = ?",
            (source_id, notebook_id, user["id"]),
        ).fetchone()
        if source is None:
            logger.warning("source_delete_missing user_id=%s notebook_id=%s source_id=%s", user["id"], notebook_id, source_id)
            raise HTTPException(status_code=404, detail="Source not found")
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        touch_notebook(conn, notebook_id)
    cleanup_source_artifacts({"id": source_id, "stored_path": source["stored_path"], "user_id": user["id"]})
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
            raise HTTPException(status_code=404, detail="Source not found")
    response = render(request, "_source_item.html", {"notebook": notebook, "source": dict(row)})
    # Broadcast sources-changed on EVERY non-uploaded poll so sibling rows
    # immediately catch up (they listen to the same event in their hx-trigger
    # list). Without this, a 4-file upload could show "1 indexed, 3 uploaded"
    # for up to 2s after the first finishes, just due to staggered poll ticks.
    if dict(row).get("status") in ("processing", "indexed", "failed"):
        response.headers["HX-Trigger"] = "sources-changed"
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
            raise HTTPException(status_code=404, detail="Source not found")
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


@app.get("/notebooks/{notebook_id}/_source-picker", response_class=HTMLResponse)
def source_picker_partial(
    request: Request,
    notebook_id: int,
    user: Annotated[dict, Depends(require_login)],
):
    """Return the chat-form source-picker fieldset, refreshed after indexing."""
    with connect() as conn:
        notebook = get_notebook(conn, notebook_id, user["id"])
        indexed_sources = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM sources WHERE notebook_id = ? AND user_id = ? AND status = 'indexed' ORDER BY filename",
                (notebook_id, user["id"]),
            ).fetchall()
        ]
    return render(request, "_source_picker.html", {"notebook": notebook, "indexed_sources": indexed_sources})


@app.get("/notebooks/{notebook_id}/_suggestions", response_class=HTMLResponse)
def suggestions_partial(
    request: Request,
    notebook_id: int,
    user: Annotated[dict, Depends(require_login)],
):
    """Return the suggestions section reflecting current indexed-source state.

    Triggered by `sources-changed` so the right pane swaps from the
    "Index a source first" placeholder to the "Generate suggestions" CTA the
    moment the first source finishes indexing — without page reload.
    """
    with connect() as conn:
        notebook = get_notebook(conn, notebook_id, user["id"])
        has_indexed = bool(conn.execute(
            "SELECT 1 FROM sources WHERE notebook_id = ? AND user_id = ? AND status = 'indexed' LIMIT 1",
            (notebook_id, user["id"]),
        ).fetchone())
    return render(
        request,
        "_suggestions.html",
        {"notebook": notebook, "questions": _cached_suggestions(notebook), "error": "", "has_indexed": has_indexed},
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
        error = "Configure LLM settings to generate suggestions."
    else:
        try:
            questions = await generate_starter_questions(excerpts, settings or {})
            if not questions:
                error = "The model returned no questions. Try again."
        except Exception as exc:
            logger.exception("suggestions_failed user_id=%s notebook_id=%s", user["id"], notebook_id)
            error = f"Could not generate suggestions: {exc}"

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

    Triggered by `sources-changed` so add/delete updates the section without
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
    upload (each fires sources-changed → auto-fire POST). A waiter that
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
            error = "Configure LLM settings to generate a briefing."
        else:
            try:
                briefing = await generate_briefing(summaries, settings)
                if not briefing:
                    error = "The model returned no briefing. Try again."
            except Exception as exc:
                logger.exception("briefing_failed user_id=%s notebook_id=%s", user["id"], notebook_id)
                error = f"Could not generate briefing: {exc}"

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


@app.get("/notebooks/{notebook_id}/_compare", response_class=HTMLResponse)
def compare_partial(
    request: Request,
    notebook_id: int,
    user: Annotated[dict, Depends(require_login)],
):
    """Return the compare-sources section reflecting current indexed sources.

    Triggered by `sources-changed` so the checkbox list stays in sync with
    the left pane after upload / delete without a page reload.
    """
    with connect() as conn:
        notebook = get_notebook(conn, notebook_id, user["id"])
        sources_indexed = [
            dict(r)
            for r in conn.execute(
                "SELECT id, filename, summary FROM sources WHERE notebook_id = ? AND user_id = ? AND status = 'indexed' ORDER BY filename",
                (notebook_id, user["id"]),
            ).fetchall()
        ]
    return render(
        request,
        "_compare.html",
        {"notebook": notebook, "sources_indexed": sources_indexed},
    )


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
                "error": "Pick at least 2 sources to compare.",
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
                "error": "Need at least 2 sources with content to compare.",
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
                "error": "Configure LLM settings to compare sources.",
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
            error = "The model returned an empty comparison. Try again."
    except Exception as exc:
        logger.exception("compare_failed user_id=%s notebook_id=%s sources=%s", user["id"], notebook_id, len(summaries))
        error = f"Could not generate comparison: {exc}"

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
        raise HTTPException(status_code=400, detail="Note content cannot be empty.")
    cleaned_title = " ".join((title or "").split())[:80] or "Saved note"
    with connect() as conn:
        notebook = get_notebook(conn, notebook_id, user["id"])
        conn.execute(
            "INSERT INTO notes (notebook_id, user_id, title, content) VALUES (?, ?, ?, ?)",
            (notebook_id, user["id"], cleaned_title, cleaned_content),
        )
        touch_notebook(conn, notebook_id)
        notes = [dict(r) for r in conn.execute(
            "SELECT * FROM notes WHERE notebook_id = ? AND user_id = ? ORDER BY created_at DESC, id DESC",
            (notebook_id, user["id"]),
        ).fetchall()]
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
            raise HTTPException(status_code=404, detail="Message not found")
        # Idempotent: pinning the same message twice is a no-op.
        existing = conn.execute(
            "SELECT id FROM notes WHERE notebook_id = ? AND source_message_id = ?",
            (notebook_id, message_id),
        ).fetchone()
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
            conn.execute(
                "INSERT INTO notes (notebook_id, user_id, title, content, source_message_id) VALUES (?, ?, ?, ?, ?)",
                (notebook_id, user["id"], title, message["content"], message_id),
            )
            touch_notebook(conn, notebook_id)
        notes = [dict(r) for r in conn.execute(
            "SELECT * FROM notes WHERE notebook_id = ? AND user_id = ? ORDER BY created_at DESC, id DESC",
            (notebook_id, user["id"]),
        ).fetchall()]
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
            raise HTTPException(status_code=404, detail="Note not found")
        source_message_id = deleted["source_message_id"]
        notes = [dict(r) for r in conn.execute(
            "SELECT * FROM notes WHERE notebook_id = ? AND user_id = ? ORDER BY created_at DESC, id DESC",
            (notebook_id, user["id"]),
        ).fetchall()]
    logger.info("note_deleted user_id=%s notebook_id=%s note_id=%s message_id=%s", user["id"], notebook_id, note_id, source_message_id)
    response = render(request, "_notes_section.html", {"notebook": notebook, "notes": notes})
    if source_message_id is not None:
        response.headers["HX-Trigger"] = dumps({"pin-cleared": {"message_id": source_message_id}})
    return response


@app.post("/notebooks/{notebook_id}/chat/{conversation_id}/delete")
def delete_conversation(
    notebook_id: int,
    conversation_id: int,
    user: Annotated[dict, Depends(require_login)],
):
    """Delete a conversation and its messages within a notebook."""
    with connect() as conn:
        get_notebook(conn, notebook_id, user["id"])
        result = conn.execute(
            "DELETE FROM conversations WHERE id = ? AND notebook_id = ? AND user_id = ?",
            (conversation_id, notebook_id, user["id"]),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Conversation not found")
        touch_notebook(conn, notebook_id)
    logger.info("conversation_deleted user_id=%s notebook_id=%s conversation_id=%s", user["id"], notebook_id, conversation_id)
    return RedirectResponse(f"/notebooks/{notebook_id}", status_code=303)


@app.post("/notebooks/{notebook_id}/chat/new")
def new_conversation(notebook_id: int, user: Annotated[dict, Depends(require_login)]):
    """Create an empty conversation scoped to a notebook."""
    with connect() as conn:
        get_notebook(conn, notebook_id, user["id"])
        cursor = conn.execute(
            "INSERT INTO conversations (user_id, notebook_id, title) VALUES (?, ?, 'New conversation')",
            (user["id"], notebook_id),
        )
        conversation_id = cursor.lastrowid
        touch_notebook(conn, notebook_id)
    logger.info("conversation_created user_id=%s notebook_id=%s conversation_id=%s", user["id"], notebook_id, conversation_id)
    return RedirectResponse(f"/notebooks/{notebook_id}?conversation_id={conversation_id}", status_code=303)


@app.post("/notebooks/{notebook_id}/chat/ask")
async def ask(
    notebook_id: int,
    user: Annotated[dict, Depends(require_login)],
    question: str = Form(...),
    conversation_id: int | None = Form(None),
    source_ids: list[int] = Form(default=[]),
):
    """Persist a user question within a notebook, run retrieval, and save the assistant answer."""
    question = question.strip()
    if not question:
        return RedirectResponse(f"/notebooks/{notebook_id}", status_code=303)

    with connect() as conn:
        get_notebook(conn, notebook_id, user["id"])
        # Restrict source_ids to those owned by this notebook to prevent cross-notebook leakage.
        if source_ids:
            placeholders = ",".join("?" for _ in source_ids)
            allowed = {
                row["id"]
                for row in conn.execute(
                    f"SELECT id FROM sources WHERE notebook_id = ? AND user_id = ? AND id IN ({placeholders})",
                    (notebook_id, user["id"], *source_ids),
                ).fetchall()
            }
            source_ids = [sid for sid in source_ids if sid in allowed]

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
                raise HTTPException(status_code=404, detail="Conversation not found")
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

    metadata: dict = {}
    try:
        retrieve_started = time.perf_counter()
        retrieved = await retrieve(question, None, settings or {}, history, user["id"], source_ids)
        metadata["retrieval_ms"] = round((time.perf_counter() - retrieve_started) * 1000, 1)
        metadata["retrieved_chunks"] = len(retrieved)
        top_score = float(retrieved[0].get("score", 0.0)) if retrieved else 0.0
        if retrieved:
            metadata["top_score"] = round(top_score, 3)

        if not retrieved or top_score < LOW_CONFIDENCE_THRESHOLD:
            answer = "I cannot determine that from the selected sources."
            citations = []
            metadata["outcome"] = "low_confidence" if retrieved else "no_retrieval"
            metadata["threshold"] = LOW_CONFIDENCE_THRESHOLD
            logger.info(
                "chat_no_retrieval_results user_id=%s notebook_id=%s conversation_id=%s top_score=%.3f threshold=%.2f",
                user["id"], notebook_id, conversation_id, top_score, LOW_CONFIDENCE_THRESHOLD,
            )
        else:
            generate_started = time.perf_counter()
            answer = await generate_answer(question, retrieved, settings or {})
            metadata["generation_ms"] = round((time.perf_counter() - generate_started) * 1000, 1)
            metadata["answer_chars"] = len(answer)
            all_citations = citation_payload(retrieved)
            # Only keep citations the model actually referenced as [N] in the
            # answer. Listing every retrieved chunk would mislead about what
            # was actually used. Match NotebookLM behaviour.
            referenced = {int(m.group(1)) for m in re.finditer(r"\[(\d+)\]", answer)}
            citations = [c for c in all_citations if c["index"] in referenced] if referenced else all_citations
            metadata["outcome"] = "answered"
            logger.info(
                "chat_answer_generated user_id=%s notebook_id=%s conversation_id=%s retrieved_chunks=%s shown_citations=%s of %s answer_chars=%s",
                user["id"], notebook_id, conversation_id, len(retrieved), len(citations), len(all_citations), len(answer),
            )
    except Exception as exc:
        answer = f"Chat error: {exc}"
        citations = []
        metadata["outcome"] = "error"
        metadata["error"] = str(exc)[:200]
        logger.exception("chat_failed user_id=%s notebook_id=%s conversation_id=%s", user["id"], notebook_id, conversation_id)

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO messages (conversation_id, user_id, role, content, citations_json, metadata_json)
            VALUES (?, ?, 'assistant', ?, ?, ?)
            """,
            (conversation_id, user["id"], answer, dumps(citations), dumps(metadata)),
        )
        conn.execute(
            """
            UPDATE conversations
            SET title = CASE WHEN title = 'New conversation' THEN ? ELSE title END,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND user_id = ?
            """,
            (question[:80], conversation_id, user["id"]),
        )
        touch_notebook(conn, notebook_id)
    return RedirectResponse(f"/notebooks/{notebook_id}?conversation_id={conversation_id}", status_code=303)


# Per-question minimum top-score before we let the answer LLM run. Below this
# the model is asked to abstain. Lives on the ask() side, not retrieve(), so
# the eval harness can still observe raw retrieval scores.
LOW_CONFIDENCE_THRESHOLD = 0.25


async def retrieve(
    question: str,
    rows,
    settings: dict,
    history: list[dict[str, str]] | None = None,
    user_id: int | None = None,
    source_ids: list[int] | None = None,
) -> list[dict]:
    """Retrieve chunks with query rewriting, hybrid search, and optional LLM reranking."""
    started = time.perf_counter()
    queries = await rewrite_search_queries(question, history or [], settings)
    query_embeddings = await embed_texts(queries, settings)
    if user_id is not None:
        try:
            vector_candidates = query_vectors(query_embeddings, user_id, source_ids, n_results=20)
            keyword_candidates = keyword_candidates_from_sqlite(user_id, source_ids or [], queries, limit=20)
            candidates = merge_candidates(vector_candidates, keyword_candidates, queries)
            ranked = sorted(candidates.values(), key=lambda item: item["score"], reverse=True)[:20]
            retrieved = await rerank_chunks(question, ranked, settings, limit=6)
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
            logger.exception("retrieve_vector_failed user_id=%s", user_id)
            rows = fetch_candidate_rows(user_id, source_ids or [])
    if not rows:
        logger.info("retrieve_skipped reason=no_candidate_rows")
        return []
    candidates = {}
    for row in rows:
        embedding = loads(row["embedding_json"])
        vector_score = max(cosine(query_embedding, embedding) for query_embedding in query_embeddings)
        keyword = keyword_score(queries, row["text"])
        score = (0.7 * max(0.0, vector_score)) + (0.3 * keyword)
        if score <= 0:
            continue
        candidates[row["id"]] = {
            "source_id": row["source_id"],
            "filename": row["filename"],
            "location": row["location"],
            "text": row["text"],
            "score": score,
            "vector_score": vector_score,
            "keyword_score": keyword,
        }
    ranked = sorted(candidates.values(), key=lambda item: item["score"], reverse=True)[:20]
    retrieved = await rerank_chunks(question, ranked, settings, limit=6)
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


def fetch_candidate_rows(user_id: int, source_ids: list[int]) -> list:
    """Fetch SQLite chunks for fallback retrieval when Chroma is unavailable."""
    with connect() as conn:
        if source_ids:
            placeholders = ",".join("?" for _ in source_ids)
            return conn.execute(
                f"""
                SELECT chunks.*, sources.filename
                FROM chunks JOIN sources ON sources.id = chunks.source_id
                WHERE chunks.user_id = ? AND sources.status = 'indexed' AND chunks.source_id IN ({placeholders})
                """,
                (user_id, *source_ids),
            ).fetchall()
        return conn.execute(
            """
            SELECT chunks.*, sources.filename
            FROM chunks JOIN sources ON sources.id = chunks.source_id
            WHERE chunks.user_id = ? AND sources.status = 'indexed'
            """,
            (user_id,),
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


def merge_candidates(vector_candidates: list[dict], keyword_candidates: list[dict], queries: list[str]) -> dict[int, dict]:
    """Merge vector and keyword candidates into one hybrid-scored map."""
    candidates: dict[int, dict] = {}
    for item in [*vector_candidates, *keyword_candidates]:
        chunk_id = int(item["id"])
        keyword = keyword_score(queries, item["text"])
        vector_score = max(0.0, float(item.get("vector_score") or 0.0))
        score = (0.7 * vector_score) + (0.3 * keyword)
        existing = candidates.get(chunk_id)
        if existing and existing["score"] >= score:
            continue
        candidates[chunk_id] = {
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
        error = "Current password is incorrect."
    elif len(new_password) < 6:
        error = "New password must be at least 6 characters."
    elif new_password != confirm_password:
        error = "New password and confirmation do not match."
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
def admin_index_rebuild(user: Annotated[dict, Depends(require_admin)]):
    """Run a full SQLite -> Chroma re-upsert (admin only)."""
    result = sync_from_sqlite(mode="full")
    logger.info("admin_index_rebuilt admin_user_id=%s upserted=%s deleted=%s", user["id"], result["upserted"], result["deleted"])
    return RedirectResponse(f"/admin/index?msg=rebuilt-{result['upserted']}", status_code=303)


@app.post("/admin/index/clear")
def admin_index_clear(user: Annotated[dict, Depends(require_admin)]):
    """Delete every vector from the Chroma collection (admin only).

    SQLite data is untouched — a subsequent "Rebuild index" re-populates Chroma.
    """
    count = clear_all_vectors()
    logger.info("admin_index_cleared admin_user_id=%s deleted=%s", user["id"], count)
    return RedirectResponse(f"/admin/index?msg=cleared-{count}", status_code=303)


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
        error = "Username must be 1–64 characters."
    elif len(password) < 6:
        error = "Password must be at least 6 characters."
    if not error:
        try:
            with connect() as conn:
                conn.execute(
                    "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
                    (username, hash_password(password), 1 if is_admin else 0),
                )
        except Exception as exc:
            error = f"Could not create user: {exc}"
    with connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT id, username, is_admin, created_at FROM users ORDER BY id ASC").fetchall()]
    if error:
        return render(request, "admin_users.html", {"user": user, "users": rows, "error": error, "saved": False}, 400)
    logger.info("user_created admin_user_id=%s new_username=%s is_admin=%s", user["id"], username, bool(is_admin))
    return render(request, "admin_users.html", {"user": user, "users": rows, "error": "", "saved": True})


@app.post("/admin/users/{target_id}/reset-password")
def admin_reset_password(
    target_id: int,
    user: Annotated[dict, Depends(require_admin)],
    new_password: str = Form(...),
):
    """Reset another user's password to a new value."""
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    with connect() as conn:
        result = conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), target_id),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="User not found")
    logger.info("password_reset admin_user_id=%s target_user_id=%s", user["id"], target_id)
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{target_id}/toggle-admin")
def admin_toggle_admin(
    target_id: int,
    user: Annotated[dict, Depends(require_admin)],
):
    """Flip the is_admin flag for another user. Refuses to demote the last admin."""
    if target_id == user["id"]:
        raise HTTPException(status_code=400, detail="You cannot change your own admin flag.")
    with connect() as conn:
        target = conn.execute("SELECT is_admin FROM users WHERE id = ?", (target_id,)).fetchone()
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
    logger.info("admin_toggled admin_user_id=%s target_user_id=%s new_is_admin=%s", user["id"], target_id, new_flag)
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{target_id}/delete")
def admin_delete_user(
    target_id: int,
    user: Annotated[dict, Depends(require_admin)],
):
    """Delete a user account (cascades notebooks/sources/conversations/notes/chunks)."""
    if target_id == user["id"]:
        raise HTTPException(status_code=400, detail="You cannot delete your own account.")
    with connect() as conn:
        target = conn.execute("SELECT is_admin FROM users WHERE id = ?", (target_id,)).fetchone()
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
                chat_model = ?, embedding_model = ?, api_version = ?,
                temperature = ?, timeout_seconds = ?
            WHERE id = 1
            """,
            (
                provider,
                base_url.strip(),
                embedding_base_url.strip(),
                stored_key,
                chat_model.strip(),
                embedding_model.strip(),
                api_version.strip(),
                temperature,
                timeout_seconds,
            ),
        )
        settings = load_llm_settings_for_display(conn)
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
