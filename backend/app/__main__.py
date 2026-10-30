"""Run DebBuilder with one command: configures, builds the editor and serves everything on one port.

    uv run --directory backend python -m app            # from the repo root (or: cd backend; uv run python -m app)

1. backend/.env: created from .env.example on first run; ENCRYPTION_KEY / INTERNAL_TOKEN are generated if empty.
2. PostgreSQL: the database in DATABASE_URL is created if it doesn't exist (tables are created on startup).
3. Editor: `npm install` when dependencies changed, `npm run build` when the sources are newer than frontend/dist.
4. API + editor on http://localhost:8000 (the API serves frontend/dist); voice workers start with active workflows.

Options:  --dev        live-reload API + Vite dev server (http://localhost:5173) instead of the built editor
          --no-build   skip the editor build (use the existing frontend/dist)
          --reload     reload the API when Python files change
Env:      HOST (0.0.0.0), PORT (8000)
"""

import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

import uvicorn

BACKEND = Path(__file__).resolve().parents[1]
FRONTEND = BACKEND.parent / "frontend"
DIST_INDEX = FRONTEND / "dist" / "index.html"


def step(msg: str) -> None:
    print(f"\033[36m›\033[0m {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"\033[31m✗ {msg}\033[0m", file=sys.stderr, flush=True)
    sys.exit(1)


# ---------------------------------------------------------------------------
# 1. configuration
# ---------------------------------------------------------------------------

def configure() -> None:
    from cryptography.fernet import Fernet

    env = BACKEND / ".env"
    example = BACKEND / ".env.example"
    if not env.exists():
        env.write_text(example.read_text(encoding="utf-8") if example.exists() else "", encoding="utf-8")
        step("created backend/.env from .env.example")
    lines = env.read_text(encoding="utf-8").splitlines()
    generate = {"ENCRYPTION_KEY": lambda: Fernet.generate_key().decode(), "INTERNAL_TOKEN": lambda: secrets.token_urlsafe(32)}
    changed = False
    for key, make in generate.items():
        idx = next((i for i, ln in enumerate(lines) if ln.split("=", 1)[0].strip() == key), None)
        if idx is not None and lines[idx].split("=", 1)[1].strip():
            continue
        if os.environ.get(key):  # set in the environment: leave it there
            continue
        value = f"{key}={make()}"
        if idx is None:
            lines.append(value)
        else:
            lines[idx] = value
        changed = True
        step(f"generated {key} in backend/.env")
    if changed:
        env.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 2. database
# ---------------------------------------------------------------------------

def ensure_database() -> None:
    import psycopg
    from psycopg import sql
    from sqlalchemy.engine import make_url

    from .config import get_settings

    url = make_url(get_settings().database_url)
    if not url.drivername.startswith("postgresql"):
        return
    params = dict(host=url.host or "localhost", port=url.port or 5432, user=url.username, password=url.password)
    try:
        with psycopg.connect(dbname="postgres", autocommit=True, connect_timeout=5, **params) as conn:
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (url.database,)).fetchone():
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(url.database)))
                step(f"created database {url.database}")
    except psycopg.OperationalError as e:
        first = str(e).strip().splitlines()[0] if str(e).strip() else repr(e)
        fail(f"Can't reach PostgreSQL at {params['host']}:{params['port']} as {params['user']} ({first}).\n"
             "  Start PostgreSQL, or set DATABASE_URL in backend/.env.")


# ---------------------------------------------------------------------------
# 3. editor build
# ---------------------------------------------------------------------------

def _newest(paths: list[Path]) -> float:
    newest = 0.0
    for p in paths:
        if p.is_file():
            newest = max(newest, p.stat().st_mtime)
        elif p.is_dir():
            for f in p.rglob("*"):
                if f.is_file():
                    newest = max(newest, f.stat().st_mtime)
    return newest


def _npm(*args: str) -> None:
    npm = shutil.which("npm")
    if subprocess.run([npm, *args], cwd=FRONTEND).returncode != 0:
        fail(f"npm {' '.join(args)} failed (see the output above)")


def prepare_frontend(build: bool) -> None:
    if not FRONTEND.exists():
        return
    if not shutil.which("npm"):
        if DIST_INDEX.exists():
            step("Node.js not found: serving the existing editor build")
            return
        fail("Node.js (npm) is needed to build the editor once: install Node.js 20+ and run again.")
    installed = FRONTEND / "node_modules" / ".package-lock.json"
    if not installed.exists() or _newest([FRONTEND / "package.json", FRONTEND / "package-lock.json"]) > installed.stat().st_mtime:
        step("installing editor dependencies (npm install)…")
        _npm("install", "--no-audit", "--no-fund")
    if not build:
        return
    sources = [FRONTEND / n for n in ("src", "public", "index.html", "package.json", "package-lock.json", "vite.config.ts",
                                      "tsconfig.json", "tsconfig.app.json", "tsconfig.node.json")]
    if DIST_INDEX.exists() and DIST_INDEX.stat().st_mtime >= _newest(sources):
        step("editor build is up to date")
        return
    step("building the editor (npm run build)…")
    _npm("run", "build")


# ---------------------------------------------------------------------------
# 4. run
# ---------------------------------------------------------------------------

def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles may default to cp1252
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    os.chdir(BACKEND)  # .env, data/ and voice/ are relative to backend/
    args = set(sys.argv[1:])
    dev = "--dev" in args
    host, port = os.environ.get("HOST", "0.0.0.0"), int(os.environ.get("PORT", "8000"))

    configure()
    ensure_database()
    prepare_frontend(build=not dev and "--no-build" not in args)

    vite = None
    if dev:
        step("starting the Vite dev server…")
        vite = subprocess.Popen([shutil.which("npm"), "run", "dev"], cwd=FRONTEND,
                                env={**os.environ, "BACKEND_URL": f"http://127.0.0.1:{port}"})
    shown = "localhost" if host in ("0.0.0.0", "::") else host
    print(f"\n  \033[1mDebBuilder\033[0m  →  \033[32mhttp://{shown}:{5173 if dev else port}\033[0m"
          + ("   (API on :%d, live reload)" % port if dev else "") + "\n", flush=True)
    try:
        uvicorn.run(
            "app.main:app",
            host=host,
            port=port,
            reload=dev or "--reload" in args,
            reload_dirs=[str(BACKEND / "app")] if dev or "--reload" in args else None,
            # psycopg's async driver cannot run on Windows' default Proactor loop.
            loop="asyncio:SelectorEventLoop" if sys.platform == "win32" else "auto",
        )
    finally:
        if vite and vite.poll() is None:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(vite.pid), "/T", "/F"], capture_output=True)
            else:
                vite.terminate()


if __name__ == "__main__":
    main()
