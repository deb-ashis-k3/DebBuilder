"""DebBuilder API.

Run with:  uv run python -m app
(On Windows this uses a selector event loop, required by psycopg async.)
"""

import asyncio
import gzip
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from . import cluster
from .api import admin, auth, credentials, executions, folders, hooks, voice, workflows, ws
from .config import get_settings
from .db import init_db
from .engine import runtime
from .engine.store import store
from .security import AuthMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    await init_db()
    pool = AsyncConnectionPool(
        get_settings().psycopg_url, max_size=10, open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open()
    checkpointer = AsyncPostgresSaver(pool)
    await checkpointer.setup()
    runtime.set_checkpointer(checkpointer)
    store.start()  # batched execution history writer (every replica runs executions)
    await cluster.start()  # schedules, listeners, voice workers and retention run on the elected replica
    try:
        yield
    finally:
        await cluster.stop()
        await store.stop()  # write what is still buffered before the process exits
        await pool.close()


app = FastAPI(title="DebBuilder", version="0.1.0", lifespan=lifespan)
# Every request is authenticated + authorized against security.POLICY (default deny).
# Added before CORS so CORS stays outermost and 401/403 responses still carry CORS headers.
app.add_middleware(AuthMiddleware)


class _Compress:
    """gzip API / HTML responses (e.g. /api/node-types: ~400 KB → ~50 KB). Left alone: streams that must flush
    as they go (voice turns), file downloads, webhook / form responses and the already-compressed assets."""
    SKIP = ("/api/voice/", "/api/binary/", "/assets/", "/webhook", "/form/")

    def __init__(self, app):
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=1024, compresslevel=6)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not scope["path"].startswith(self.SKIP):
            return await self.gzip(scope, receive, send)
        return await self.app(scope, receive, send)


app.add_middleware(_Compress)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in get_settings().cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (auth.router, admin.router, workflows.router, folders.router, executions.router, executions.binary_router, credentials.router, credentials.oauth_router, hooks.router, voice.router, ws.router):
    app.include_router(r)


# Bump with REQUIRED_API_VERSION in frontend/src/App.tsx when the editor starts using new endpoints
# (3 = folders, workflow import/export, batch execution delete/stop; 4 = authentication).
API_VERSION = 4


@app.get("/api/health")
async def health():
    """Liveness: the process answers (no dependencies, so a database outage doesn't restart pods)."""
    return {"ok": True, "api_version": API_VERSION}


@app.get("/api/ready")
async def ready():
    """Readiness: the database answers, so this replica can take traffic."""
    from sqlalchemy import text

    from .db import engine
    try:
        async with engine.connect() as c:
            await c.execute(text("SELECT 1"))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"database: {type(e).__name__}"}, status_code=503)
    return {"ok": True, **cluster.info(), "execution_writer": {**store.stats, "pending": len(store._rows)}}


# Serve the built frontend (frontend/dist) when present.
DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


IMMUTABLE = "public, max-age=31536000, immutable"


def _looks_like_file(path: str) -> bool:
    last = path.rstrip("/").rsplit("/", 1)[-1]
    return path.startswith(".well-known/") or ("." in last and not last.startswith("."))
_COMPRESSIBLE = {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml",
                 ".json": "application/json", ".map": "application/json", ".txt": "text/plain"}
_gz_cache: dict[str, bytes] = {}


class _HashedAssets(StaticFiles):
    """Vite puts a content hash in every asset name, so browsers and CDNs may keep them forever.
    Text assets are gzipped once and kept in memory (a name never changes content), cutting the JS to a third."""
    async def get_response(self, path, scope):
        ext = Path(path).suffix.lower()
        accept = dict(scope.get("headers") or []).get(b"accept-encoding", b"")
        if ext in _COMPRESSIBLE and b"gzip" in accept:
            f = (Path(str(self.directory)) / path).resolve()
            if f.is_relative_to(Path(str(self.directory)).resolve()) and f.is_file():
                body = _gz_cache.get(str(f))
                if body is None:
                    body = await asyncio.to_thread(gzip.compress, f.read_bytes(), 9)
                    _gz_cache[str(f)] = body
                return Response(body, media_type=_COMPRESSIBLE[ext], headers={
                    "Content-Encoding": "gzip", "Vary": "Accept-Encoding", "Cache-Control": IMMUTABLE})
        resp = await super().get_response(path, scope)
        if resp.status_code == 200:
            resp.headers["Cache-Control"] = IMMUTABLE
        return resp


if DIST.exists():
    app.mount("/assets", _HashedAssets(directory=DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        # Resolve and confine to dist/: "..%2f" segments must not reach files like backend/.env.
        f = (DIST / full_path).resolve()
        inside = f.is_relative_to(DIST.resolve())
        if not (full_path and inside and f.is_file()) and _looks_like_file(full_path):
            # A missing /robots.txt, /llms.txt, /.well-known/... or /x.json answered with the editor's HTML
            # confuses crawlers and AI agents ("Unexpected token '<'"); only app routes get index.html.
            return Response("Not found", status_code=404, media_type="text/plain")
        target = f if full_path and inside and f.is_file() else DIST / "index.html"
        # index.html names the current asset hashes: always revalidate so a new release shows at once.
        return FileResponse(target, headers={"Cache-Control": "no-cache"} if target.name == "index.html" else None)
