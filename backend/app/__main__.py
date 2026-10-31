"""DebBuilder command line: run it locally or in production (Docker / Kubernetes / any Linux host).

    debbuilder                  same as `debbuilder start`
    debbuilder start            local: set up .env + database, build the editor if changed, run in this terminal
    debbuilder dev              local development: API + Vite dev server, both live-reloading
    debbuilder serve            production: settings from the environment only, never writes files or builds
    debbuilder build            build the editor into frontend/dist (CI, Docker)
    debbuilder check            verify settings and the database connection, exit 0/1 (CI, init containers)
    debbuilder voice-worker     run one LiveKit voice worker (scale voice separately; VOICE_WORKERS=off on the API)

    (`python -m app ...` is the same.)  Install the command once, from backend/:  uv tool install --editable ".[voice]"

start options:  --no-build  --no-browser  --reload
Environment:    HOST (0.0.0.0), PORT (8000) and everything in backend/.env.example.
Limits:         MAX_CONNECTIONS (open connections per process before 503; 0 = 400 on Windows, 2000 elsewhere),
                MAX_REQUEST_BODY_MB (16)
serve also:     FORWARDED_ALLOW_IPS (proxies trusted for X-Forwarded-*, default 127.0.0.1),
                GRACEFUL_TIMEOUT (seconds, default 30), DB_AUTO_CREATE (create the database if missing, default false)
"""

import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
FRONTEND = BACKEND.parent / "frontend"
DIST_INDEX = FRONTEND / "dist" / "index.html"
# psycopg async can't use the Proactor loop on Windows: a selector loop, whose select() is guarded (selector_loop).
LOOP = "app.__main__:selector_loop" if sys.platform == "win32" else "auto"
LOOPBACK = ("127.0.0.1", "::1", "localhost")


# ---------------------------------------------------------------------------
# 0. server limits (connections, Windows select())
# ---------------------------------------------------------------------------
# Windows' select() watches at most 512 sockets per call; past that the event loop raised
# "ValueError: too many file descriptors in select()" and the whole server died. Two layers keep it alive:
# connections beyond MAX_CONNECTIONS are answered 503 and closed before the loop ever watches them, and if the
# limit is still passed (outgoing connections of busy workflows) select() runs in chunks instead of raising.

def max_connections() -> int:
    from .config import get_settings

    n = get_settings().max_connections
    return n if n > 0 else (400 if sys.platform == "win32" else 2000)


def selector_loop():
    """Loop factory for uvicorn on Windows (`loop="app.__main__:selector_loop"`)."""
    import asyncio
    import select
    import selectors

    class _ChunkedSelectSelector(selectors.SelectSelector):
        CHUNK = 500

        def _select(self, r, w, _, timeout=None):
            try:
                return super()._select(r, w, _, timeout)
            except ValueError:  # more than FD_SETSIZE sockets: poll them in chunks (busy-waits briefly; rare)
                pass
            deadline = None if timeout is None else time.monotonic() + timeout
            while True:
                rr, ww = [], []
                for i in range(0, len(r), self.CHUNK):
                    rr += select.select(r[i:i + self.CHUNK], [], [], 0)[0]
                for i in range(0, len(w), self.CHUNK):
                    _, wr, wx = select.select([], w[i:i + self.CHUNK], w[i:i + self.CHUNK], 0)
                    ww += wr + wx
                if rr or ww or (deadline is not None and time.monotonic() >= deadline):
                    return rr, ww, []
                time.sleep(0.002 if deadline is None else max(0.0, min(0.002, deadline - time.monotonic())))

    return asyncio.SelectorEventLoop(_ChunkedSelectSelector())


_BUSY_TEXT = b"Server is busy: too many connections. Retry shortly."
_BUSY = (b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: text/plain; charset=utf-8\r\nRetry-After: 1\r\n"
         b"Connection: close\r\nContent-Length: " + str(len(_BUSY_TEXT)).encode() + b"\r\n\r\n" + _BUSY_TEXT)


def _capped_http_protocol():
    """uvicorn's HTTP protocol that answers 503 and closes a connection arriving while MAX_CONNECTIONS are open,
    before the event loop starts watching its socket (uvicorn's own limit_concurrency still keeps the socket)."""
    try:
        from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol as Base
    except ImportError:
        from uvicorn.protocols.http.h11_impl import H11Protocol as Base
    cap = max_connections()

    class Capped(Base):
        _rejected = False

        def connection_made(self, transport):
            if len(self.server_state.connections) >= cap:
                self._rejected = True
                try:
                    transport.write(_BUSY)
                finally:
                    transport.close()
                return
            super().connection_made(transport)

        def connection_lost(self, exc):
            if not self._rejected:
                super().connection_lost(exc)

        def data_received(self, data):
            if not self._rejected:
                super().data_received(data)

        def eof_received(self):
            if not self._rejected:
                return super().eof_received()

    return Capped


def server_options(host: str, reload: bool = False) -> dict:
    """uvicorn settings every command shares: connection limits and the startup warnings."""
    from .config import get_settings

    cap = max_connections()
    base = get_settings().public_base_url
    if host not in LOOPBACK and not base.startswith("https://"):
        step(f"warning: listening on {host}, reachable from the network, but PUBLIC_BASE_URL ({base}) is not https: "
             "sessions travel unencrypted and cookies aren't marked Secure. Put DebBuilder behind HTTPS and set "
             "PUBLIC_BASE_URL to the https address (or set HOST=127.0.0.1 for local use only).")
    opts = {"loop": LOOP, "limit_concurrency": cap + 1, "backlog": min(2048, max(128, cap * 2))}
    if not reload:  # the reloader pickles its config for the child process; a class made here can't be (dev only)
        opts["http"] = _capped_http_protocol()
    return opts


def step(msg: str) -> None:
    print(f"\033[36m›\033[0m {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"\033[31m✗ {msg}\033[0m", file=sys.stderr, flush=True)
    sys.exit(1)


def port_owner(port: int) -> str | None:
    """None if the port is free, "debbuilder" if DebBuilder already answers there, else "other"."""
    import socket

    with socket.socket() as s:
        s.settimeout(1)
        if s.connect_ex(("127.0.0.1", port)) != 0:
            return None
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as r:
            return "debbuilder" if b"api_version" in r.read() else "other"
    except OSError:
        return "other"


def open_browser_when_ready(api: str, url: str) -> None:
    def wait() -> None:
        for _ in range(240):
            try:
                with urllib.request.urlopen(f"{api}/api/health", timeout=2):
                    break
            except OSError:
                time.sleep(0.5)
        else:
            return
        webbrowser.open(url)
    threading.Thread(target=wait, daemon=True).start()


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
# 4. commands
# ---------------------------------------------------------------------------

def _settings():
    from pydantic import ValidationError

    from .config import get_settings
    try:
        return get_settings()
    except ValidationError as e:
        missing = ", ".join(str(err["loc"][0]).upper() for err in e.errors())
        fail(f"Configuration incomplete: {missing}. Set them as environment variables (or in backend/.env).")


def _port() -> tuple[str, int]:
    return os.environ.get("HOST", "0.0.0.0"), int(os.environ.get("PORT", "8000"))


def cmd_start(args: set[str]) -> None:
    import uvicorn

    host, port = _port()
    owner = port_owner(port)
    if owner == "debbuilder":
        step(f"DebBuilder is already running on port {port}: opening it")
        if "--no-browser" not in args:
            webbrowser.open(f"http://localhost:{port}")
        return
    if owner == "other":
        fail(f"Port {port} is used by another program. Stop it, or use another port (PORT=8001).")
    configure()
    _settings()
    ensure_database()
    prepare_frontend(build="--no-build" not in args)
    shown = "localhost" if host in ("0.0.0.0", "::") else host
    if "--no-browser" not in args:
        open_browser_when_ready(f"http://127.0.0.1:{port}", f"http://{shown}:{port}")
    print(f"\n  \033[1mDebBuilder\033[0m  →  \033[32mhttp://{shown}:{port}\033[0m   (Ctrl+C to stop)\n", flush=True)
    reload = "--reload" in args
    uvicorn.run("app.main:app", host=host, port=port, reload=reload,
                reload_dirs=[str(BACKEND / "app")] if reload else None, **server_options(host, reload))


def cmd_dev(args: set[str]) -> None:
    import uvicorn

    host, port = _port()
    configure()
    _settings()
    ensure_database()
    prepare_frontend(build=False)
    step("starting the Vite dev server…")
    vite = subprocess.Popen([shutil.which("npm") or "npm", "run", "dev"], cwd=FRONTEND,
                            env={**os.environ, "BACKEND_URL": f"http://127.0.0.1:{port}"})
    if "--no-browser" not in args:
        open_browser_when_ready(f"http://127.0.0.1:{port}", "http://localhost:5173")
    print(f"\n  \033[1mDebBuilder (dev)\033[0m  →  \033[32mhttp://localhost:5173\033[0m   (API on :{port}, live reload)\n",
          flush=True)
    try:
        uvicorn.run("app.main:app", host=host, port=port, reload=True, reload_dirs=[str(BACKEND / "app")],
                    **server_options(host, reload=True))
    finally:
        if vite.poll() is None:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(vite.pid), "/T", "/F"], capture_output=True)
            else:
                vite.terminate()


def cmd_serve(args: set[str]) -> None:
    """Production (12-factor): configuration comes from the environment (Secrets / ConfigMaps), nothing is
    generated or built at runtime, and SIGTERM drains requests and stops background work cleanly."""
    import uvicorn

    s = _settings()
    if "localhost" in s.public_base_url or "127.0.0.1" in s.public_base_url:
        step(f"warning: PUBLIC_BASE_URL is {s.public_base_url}; set it to the public https URL "
             "(webhook, OAuth and form links, secure cookies)")
    if os.environ.get("DB_AUTO_CREATE", "").lower() in ("1", "true", "yes"):
        ensure_database()
    if not DIST_INDEX.exists():
        step("warning: frontend/dist is missing; serving the API only (run `debbuilder build`)")
    host, port = _port()
    uvicorn.run("app.main:app", host=host, port=port, **server_options(host),
                proxy_headers=True, forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
                timeout_graceful_shutdown=int(os.environ.get("GRACEFUL_TIMEOUT", "30")),
                # Longer than any load balancer's idle timeout (ALB 60 s, nginx 75 s): with uvicorn's 5 s default a
                # proxy reuses a connection just as the server closes it, and the request fails (random 502s).
                timeout_keep_alive=int(os.environ.get("KEEPALIVE_TIMEOUT", "90")),
                access_log=os.environ.get("ACCESS_LOG", "true").lower() != "false",
                server_header=False)


def cmd_build(args: set[str]) -> None:
    prepare_frontend(build=True)


def cmd_check(args: set[str]) -> None:
    import psycopg

    s = _settings()
    try:
        with psycopg.connect(s.psycopg_url, connect_timeout=5) as c:
            version = c.execute("SHOW server_version").fetchone()[0]
    except psycopg.OperationalError as e:
        fail(f"database: {str(e).strip().splitlines()[0] if str(e).strip() else e}")
    step(f"settings OK · PostgreSQL {version} reachable · editor build {'present' if DIST_INDEX.exists() else 'MISSING'}")


def cmd_voice_worker(args: list[str]) -> None:
    """One LiveKit worker for an agent name, to run voice as its own deployment."""
    s = _settings()
    agent = next((a.split("=", 1)[1] for a in args if a.startswith("--agent=")), None) or os.environ.get("AGENT_NAME")
    if not agent:
        fail("Give the agent name: debbuilder voice-worker --agent=<name> (or AGENT_NAME)")
    for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        if not os.environ.get(k):
            fail(f"{k} is not set")
    env = {**os.environ, "AGENT_NAME": agent, "INTERNAL_TOKEN": s.internal_token,
           "DEBBUILDER_API": s.internal_api_url or os.environ.get("DEBBUILDER_API", "http://127.0.0.1:8000")}
    argv = [sys.executable, str(BACKEND / "voice" / "worker.py"), "start"]
    if sys.platform == "win32":
        sys.exit(subprocess.call(argv, env=env))
    os.execve(sys.executable, argv, env)  # replace this process so signals reach the worker directly


COMMANDS = {"start": cmd_start, "dev": cmd_dev, "serve": cmd_serve, "build": cmd_build, "check": cmd_check}


def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles may default to cp1252
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    os.chdir(BACKEND)  # .env, data/ and voice/ are relative to backend/
    argv = sys.argv[1:]
    named = bool(argv) and not argv[0].startswith("-")
    name, rest = (argv[0], argv[1:]) if named else ("start", argv)
    if name in ("help", "-h", "--help") or "--help" in rest:
        print(__doc__)
        return
    if "--dev" in rest:  # earlier spelling of `debbuilder dev`
        name = "dev"
    if name == "voice-worker":
        cmd_voice_worker(rest)
    elif name in COMMANDS:
        COMMANDS[name](set(rest))
    else:
        fail(f"Unknown command {name!r}. Run `debbuilder help`.")


if __name__ == "__main__":
    main()
