"""DebBuilder API.

Run with:  uv run python -m app
(On Windows this uses a selector event loop, required by psycopg async.)
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from . import listeners, scheduler
from .api import admin, auth, credentials, executions, folders, hooks, voice, workflows, ws
from .config import get_settings
from .db import init_db
from .engine import runtime
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
    scheduler.scheduler.start()
    await scheduler.load_all()
    await listeners.load_all()
    try:
        yield
    finally:
        await listeners.stop_all()
        scheduler.scheduler.shutdown(wait=False)
        await pool.close()


app = FastAPI(title="DebBuilder", version="0.1.0", lifespan=lifespan)
# Every request is authenticated + authorized against security.POLICY (default deny).
# Added before CORS so CORS stays outermost and 401/403 responses still carry CORS headers.
app.add_middleware(AuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in get_settings().cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (auth.router, admin.router, workflows.router, folders.router, executions.router, credentials.router, credentials.oauth_router, hooks.router, voice.router, ws.router):
    app.include_router(r)


# Bump with REQUIRED_API_VERSION in frontend/src/App.tsx when the editor starts using new endpoints
# (3 = folders, workflow import/export, batch execution delete/stop; 4 = authentication).
API_VERSION = 4


@app.get("/api/health")
async def health():
    return {"ok": True, "api_version": API_VERSION}


# Serve the built frontend (frontend/dist) when present.
DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        # Resolve and confine to dist/: "..%2f" segments must not reach files like backend/.env.
        f = (DIST / full_path).resolve()
        inside = f.is_relative_to(DIST.resolve())
        return FileResponse(f if full_path and inside and f.is_file() else DIST / "index.html")
