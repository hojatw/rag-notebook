# Single-stage image for the NotebookLM-style RAG POC.
#
# Why python:3.12-slim:
#   - chromadb 1.5.9 ships cp310-cp313 wheels but no cp314 yet (and
#     onnxruntime has no cp313/cp314 wheels at all). 3.12 avoids the
#     setup.sh --no-deps dance the host script does for Python 3.14.
#   - slim variant keeps the image around 250 MB while still carrying glibc
#     (so we don't fight musl quirks).
#
# Persistence:
#   /app/data and /app/logs are expected to be bind-mounted from the host
#   (see docker-compose.yml). Everything else inside the image is read-only
#   user code that gets replaced on the next `docker compose up --build`.
FROM python:3.12-slim AS runtime

# 1) System packages: only what python-docx / pypdf / chromadb need at
#    runtime. build-essential is for the rare wheel-less dep; we drop it
#    immediately after pip install to keep the image small.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential \
        libxml2 \
 && rm -rf /var/lib/apt/lists/*

# 2) Non-root user. UID 1000 lines up with the default Linux/macOS user so
#    bind-mounted host directories tend to be writable without chown
#    gymnastics. Override with `user:` in compose if your host UID differs.
RUN useradd --create-home --uid 1000 app

WORKDIR /app

# 3) Install Python deps before copying the app code so a code-only change
#    doesn't bust the wheel-install layer cache.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && apt-get purge -y --auto-remove build-essential \
 && rm -rf /var/lib/apt/lists/*

# 4) App code. Excludes from .dockerignore keep data/, logs/, .venv/, .git/,
#    __pycache__/ out of the image.
COPY app ./app
COPY tests ./tests
COPY README.md handover.md RETRIEVAL.md ./

# 5) Make sure mount targets exist with the right owner. Empty directories
#    here get replaced by the bind mount at container start; the chown
#    matters when the user runs without a mount (fresh-install demo).
RUN mkdir -p /app/data /app/logs \
 && chown -R app:app /app

USER app

# 6) Defaults the app reads via os.environ. Override in compose / `-e`.
ENV NOTEBOOKLM_DATA_DIR=/app/data \
    NOTEBOOKLM_LOG_FILE=/app/logs/app.log \
    NOTEBOOKLM_LOG_LEVEL=INFO \
    NOTEBOOKLM_LOG_MAX_BYTES=5242880 \
    NOTEBOOKLM_LOG_BACKUP_COUNT=5

# NOTEBOOKLM_SECRET intentionally NOT defaulted in the image. Without it the
# app fails closed unless NOTEBOOKLM_ALLOW_INSECURE_DEV_SECRET=1 is explicitly
# set for local development. Compose requires NOTEBOOKLM_SECRET via .env.
# Setting it AFTER first save invalidates previously-encrypted API keys
# (admin will need to re-enter at /settings).

EXPOSE 8000

# 7) Healthcheck without adding curl: ask Python to do the HTTP. The root
#    route returns 303 (redirect to /login or /notebooks) so we accept
#    anything < 400 as alive.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        r=urllib.request.urlopen('http://127.0.0.1:8000/', timeout=4); \
        sys.exit(0 if r.status < 400 else 1)" \
        || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
