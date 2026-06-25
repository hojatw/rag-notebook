# Single-stage image for the NotebookLM-style RAG POC.
#
# Why python:3.12-slim:
#   - Matches the local development runtime pinned by .python-version and
#     setup.sh.
#   - Keeps native dependency wheels such as onnxruntime available on the
#     supported local and container platforms.
#   - slim variant keeps the image around 250 MB while still carrying glibc
#     (so we don't fight musl quirks).
#
# Persistence:
#   /app/data and /app/logs are expected to be bind-mounted from the host
#   (see docker-compose.yml). Everything else inside the image is read-only
#   user code that gets replaced on the next `docker compose up --build`.
FROM python:3.12-slim AS runtime

# 1) Runtime defaults. build-essential is installed only inside the dependency
#    installation layer below, then purged in the same layer so it does not
#    remain in the final image.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 2) Non-root user. UID 1000 lines up with the default Linux/macOS user so
#    bind-mounted host directories tend to be writable without chown
#    gymnastics. Override with `user:` in compose if your host UID differs.
RUN useradd --create-home --uid 1000 app

WORKDIR /app

# 3) Install Python deps before copying the app code so a code-only change
#    doesn't bust the wheel-install layer cache.
COPY requirements.txt .
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential \
        libxml2 \
 && pip install --no-cache-dir --no-compile -r requirements.txt \
 && apt-get purge -y --auto-remove build-essential \
 && rm -rf /var/lib/apt/lists/*

# 4) App code. Excludes from .dockerignore keep data/, logs/, .venv/, .git/,
#    __pycache__/ out of the image.
COPY app ./app

# 4b) Tunable-config template, for reference inside the container. To actually
#     override defaults, bind-mount a config.toml over /app/config.toml (see
#     docker-compose.yml) or set NOTEBOOKLM_<GROUP>_<FIELD> env vars.
COPY config.example.toml ./

# 4c) Semantic version, read by app/version.py for the footer / /healthz. The
#     .git dir is not copied into the image, so the commit comes from the
#     NOTEBOOKLM_GIT_SHA build arg (step 6) instead.
COPY VERSION ./

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

# Build identifier: pass `--build-arg NOTEBOOKLM_GIT_SHA=$(git rev-parse --short HEAD)`
# so /healthz and the footer report the exact commit. Defaults to "unknown".
ARG NOTEBOOKLM_GIT_SHA=unknown
ENV NOTEBOOKLM_GIT_SHA=$NOTEBOOKLM_GIT_SHA

# NOTEBOOKLM_SECRET intentionally NOT defaulted in the image. Without it the
# app fails closed unless NOTEBOOKLM_ALLOW_INSECURE_DEV_SECRET=1 is explicitly
# set for local development. Compose requires NOTEBOOKLM_SECRET via .env.
# Setting it AFTER first save invalidates previously-encrypted API keys
# (admin will need to re-enter at /settings).

EXPOSE 8000

# 7) Healthcheck without adding curl: ask Python to do the HTTP. /healthz is the
#    dedicated liveness endpoint and returns 200 (no auth, no redirect).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        r=urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4); \
        sys.exit(0 if r.status == 200 else 1)" \
        || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
