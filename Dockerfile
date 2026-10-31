# DebBuilder production image: the editor is built here, never at runtime.
#   docker build -t debbuilder .                      (add --build-arg VOICE=false for a smaller image without voice)
#   docker run --env-file .env -p 8000:8000 debbuilder
# The container runs `debbuilder serve`: settings come only from the environment (see backend/.env.example).

# ---- 1. editor -----------------------------------------------------------------------------
FROM node:22-alpine AS editor
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# the build bundles only the icons named in the editor and in the backend's node catalog (vite.config.ts)
COPY backend/app /src/backend/app
RUN npm run build

# ---- 2. API + engine -------------------------------------------------------------------------
FROM python:3.12-slim AS app
ARG VOICE=true
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    HF_HOME=/opt/models \
    HOST=0.0.0.0 \
    PORT=8000
WORKDIR /app/backend

# Dependencies first (cached layer), then the code.
COPY backend/pyproject.toml backend/uv.lock backend/README.md ./
RUN uv sync --frozen --no-install-project --no-dev $([ "$VOICE" = "true" ] && echo --extra voice)
COPY backend/app ./app
COPY backend/voice ./voice
COPY backend/.env.example ./
RUN uv sync --frozen --no-dev $([ "$VOICE" = "true" ] && echo --extra voice)
# Voice models (turn detector, VAD, noise cancellation) baked in: no downloads when a call starts.
RUN if [ "$VOICE" = "true" ]; then python voice/worker.py download-files; fi
ENV VOICE_MODELS_PREFETCHED=1

COPY --from=editor /src/frontend/dist /app/frontend/dist

# Unprivileged user; data/ (execution files, Read/Write Files sandbox) is the only writable path.
RUN useradd --uid 10001 --create-home debbuilder \
 && mkdir -p /app/backend/data \
 && chown -R debbuilder:debbuilder /app/backend/data
USER 10001
VOLUME ["/app/backend/data"]
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/api/health', timeout=4)"

ENTRYPOINT ["debbuilder"]
CMD ["serve"]
