"""Runs the LiveKit voice workers for active workflows, so nobody has to start `voice/worker.py` by hand.

Like the scheduler (cron) and listeners (Kafka, …), this keeps the voice side in step with the
workflows' active state:

- every active workflow's Voice Agent trigger needs a worker registered under its agent name on its
  LiveKit project; one worker process per (LiveKit credential, agent name) serves all workflows that
  share them (the worker routes each call to its workflow via /api/voice/resolve);
- activating / saving / deactivating / deleting a workflow, or editing the LiveKit credential,
  starts, restarts or stops only the workers whose set changed;
- a worker that exits is restarted with backoff; its state and recent log lines are shown on the
  Voice Agent trigger (GET /api/workflows/{id}/listeners);
- VOICE_WORKERS=off turns this off (run `voice/worker.py start` yourself, e.g. on other machines).

The worker is a separate process (its own event loop, audio models and job processes) started with
subprocess.Popen: the backend's Windows event loop (Selector, for psycopg) can't spawn asyncio
subprocesses, and Popen behaves the same everywhere.
"""

import asyncio
import hashlib
import importlib.util
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from .config import get_settings
from .credentials import load_credential
from .db import SessionLocal, Workflow

log = logging.getLogger("debbuilder.voice_workers")

BACKEND_DIR = Path(__file__).resolve().parents[1]
WORKER_SCRIPT = BACKEND_DIR / "voice" / "worker.py"
DEFAULT_AGENT = "debbuilder-voice"
STOP_GRACE = 10       # seconds a stopping worker gets before it's killed
MAX_BACKOFF = 60
LOG_LINES = 200

Key = tuple[str, str]  # (LiveKit credential id or "" for backend/.env, agent name)


@dataclass
class Status:
    state: str = "starting"  # starting | running | error | stopped
    error: str | None = None
    pid: int | None = None
    restarts: int = 0
    since: float = field(default_factory=time.time)
    logs: deque = field(default_factory=lambda: deque(maxlen=LOG_LINES))

    def line(self, text: str) -> None:
        text = _message(text)
        self.logs.append(text)
        low = text.lower()
        if "registered worker" in low:
            self.state, self.error, self.since = "running", None, time.time()
        elif self.state != "running" and ("failed to connect" in low or "401" in low or "unauthorized" in low):
            self.state, self.error = "error", text


@dataclass
class Worker:
    key: Key
    env: dict
    signature: str
    status: Status = field(default_factory=Status)
    stopping: asyncio.Event = field(default_factory=asyncio.Event)
    proc: subprocess.Popen | None = None
    task: asyncio.Task | None = None


# workflow id → {trigger node id: Key}, for the active workflows; owners pick whose credential to load
_wanted: dict[str, dict[str, Key]] = {}
_owners: dict[str, str | None] = {}
_problems: dict[str, dict[str, str]] = {}  # workflow id → {node id: why no worker can run}
_workers: dict[Key, Worker] = {}
_lock = asyncio.Lock()
_files_ready = False


def enabled() -> bool:
    return get_settings().voice_workers.lower() != "off"


def _message(raw: str) -> str:
    """LiveKit logs JSON lines in production mode; keep them readable."""
    raw = raw.rstrip()
    if raw.startswith("{"):
        try:
            d = json.loads(raw)
            msg = d.get("message") or d.get("msg") or raw
            lvl = d.get("level") or d.get("levelname")
            return f"{lvl} {msg}" if lvl else msg
        except ValueError:
            pass
    return raw


def command() -> list[str] | None:
    """How to run the worker: this interpreter if it has the voice packages, else `uv run --extra voice`."""
    if importlib.util.find_spec("livekit.agents") is not None:
        return [sys.executable, str(WORKER_SCRIPT)]
    uv = shutil.which("uv")
    if uv:
        return [uv, "run", "--extra", "voice", "python", str(WORKER_SCRIPT)]
    return None


def _api_url() -> str:
    s = get_settings()
    return (s.internal_api_url or f"http://127.0.0.1:{os.environ.get('PORT', '8000')}").rstrip("/")


# ---------------------------------------------------------------------------
# what should run
# ---------------------------------------------------------------------------

def _voice_triggers(wf: Workflow) -> dict[str, dict]:
    if not wf.active:
        return {}
    return {n["id"]: n for n in wf.live_nodes if n.get("type") == "trigger.voice" and not n.get("disabled")}


def _set_workflow(wf: Workflow) -> None:
    wf_id = str(wf.id)
    nodes = _voice_triggers(wf)
    if not nodes:
        _wanted.pop(wf_id, None)
        _owners.pop(wf_id, None)
        _problems.pop(wf_id, None)
        return
    _wanted[wf_id] = {nid: (str((n.get("parameters") or {}).get("credential") or ""),
                            str((n.get("parameters") or {}).get("agent_name") or DEFAULT_AGENT).strip() or DEFAULT_AGENT)
                      for nid, n in nodes.items()}
    _owners[wf_id] = str(wf.owner_id) if wf.owner_id else None


async def _env_for(key: Key) -> tuple[dict | None, str | None]:
    """Worker environment for a key, or (None, reason)."""
    cred_id, agent = key
    if cred_id:
        owners = [o for w, o in _owners.items() if any(k == key for k in _wanted.get(w, {}).values())]
        data, last_err = None, "credential not found"
        async with SessionLocal() as s:
            for owner in owners or [None]:
                try:
                    _, data = await load_credential(s, cred_id, owner)
                    break
                except ValueError as e:
                    last_err = str(e)
        if not data:
            return None, f"LiveKit credential: {last_err}"
        lk = {"LIVEKIT_URL": data.get("url") or "", "LIVEKIT_API_KEY": data.get("api_key") or "",
              "LIVEKIT_API_SECRET": data.get("api_secret") or ""}
    else:  # no credential picked: the worker falls back to LIVEKIT_* in backend/.env
        from dotenv import dotenv_values
        file_env = dotenv_values(BACKEND_DIR / ".env")
        lk = {k: os.environ.get(k) or file_env.get(k) or "" for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")}
    if not all(lk.values()):
        return None, "Pick a LiveKit credential (URL, API key and secret) on the Voice Agent trigger"
    s = get_settings()
    return {**lk, "AGENT_NAME": agent, "DEBBUILDER_API": _api_url(), "INTERNAL_TOKEN": s.internal_token,
            "VOICE_WORKER_PORT": "0", "VOICE_IDLE_PROCESSES": str(s.voice_worker_idle_processes),
            "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}, None


async def _reconcile() -> None:
    async with _lock:
        if not enabled():
            return
        wanted: set[Key] = {k for nodes in _wanted.values() for k in nodes.values()}
        for key in [k for k in _workers if k not in wanted]:
            await _stop(key)
        problems: dict[Key, str] = {}
        for key in wanted:
            env, why = await _env_for(key)
            if env is None:
                problems[key] = why
                if key in _workers:
                    await _stop(key)
                continue
            sig = hashlib.sha256(json.dumps(env, sort_keys=True).encode()).hexdigest()
            w = _workers.get(key)
            if w and w.signature == sig:
                continue
            if w:
                await _stop(key)
            _start(key, env, sig)
        _problems.clear()
        for wf_id, nodes in _wanted.items():
            for nid, key in nodes.items():
                if key in problems:
                    _problems.setdefault(wf_id, {})[nid] = problems[key]


def _kick() -> None:
    try:
        asyncio.get_running_loop().create_task(_reconcile())
    except RuntimeError:  # no loop (scripts / tests without the app)
        pass


# ---------------------------------------------------------------------------
# processes
# ---------------------------------------------------------------------------

def _popen(args: list[str], env: dict) -> subprocess.Popen:
    kw: dict = {}
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True  # own process group: stopping it also stops its job processes
    return subprocess.Popen(args, cwd=BACKEND_DIR, env={**os.environ, **env}, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", bufsize=1, **kw)


def _pump(proc: subprocess.Popen, status: Status, prefix: str) -> None:
    for raw in proc.stdout:  # type: ignore[union-attr]
        status.line(raw)
        log.debug("%s %s", prefix, raw.rstrip())


def _kill(proc: subprocess.Popen, force: bool) -> None:
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            # The worker runs each call in a child process: take the whole tree down. A windowless
            # process can't be asked to close on Windows (no SIGTERM), so this is always forced.
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=15)
        else:
            os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        proc.kill()


async def _wait(proc: subprocess.Popen, stopping: asyncio.Event | None = None) -> None:
    while proc.poll() is None:
        if stopping is not None and stopping.is_set():
            return
        await asyncio.sleep(0.5)


async def _download_files(base: list[str], env: dict, status: Status) -> None:
    """Fetch the turn-detector / VAD / noise models once per backend start (a no-op when cached)."""
    global _files_ready
    if _files_ready or os.environ.get("VOICE_MODELS_PREFETCHED") == "1":  # baked into the Docker image
        return
    status.line("downloading voice models (first start only)…")
    try:
        proc = await asyncio.to_thread(_popen, base + ["download-files"], env)
        threading.Thread(target=_pump, args=(proc, status, "[download]"), daemon=True).start()
        await asyncio.wait_for(_wait(proc), timeout=600)
        _files_ready = proc.returncode == 0
    except (OSError, asyncio.TimeoutError) as e:
        status.line(f"model download failed: {e}")


async def _supervise(w: Worker) -> None:
    st, backoff = w.status, 2.0
    name = f"[voice:{w.key[1]}]"
    while not w.stopping.is_set():
        base = command()
        if base is None:
            st.state, st.error = "error", ("The voice packages aren't installed. In backend/ run: uv sync --extra voice "
                                           "(or install uv), then save the workflow again.")
            return
        await _download_files(base, w.env, st)
        if w.stopping.is_set():
            break
        st.state, st.error, st.since = "starting", None, time.time()
        try:
            w.proc = await asyncio.to_thread(_popen, base + ["start"], w.env)
        except OSError as e:
            st.state, st.error = "error", f"Couldn't start the voice worker: {e}"
            return
        st.pid = w.proc.pid
        log.info("%s started (pid %s)", name, w.proc.pid)
        threading.Thread(target=_pump, args=(w.proc, st, name), daemon=True).start()
        started = time.time()
        await _wait(w.proc, w.stopping)
        if w.stopping.is_set():
            break
        code = w.proc.returncode
        tail = " | ".join(list(st.logs)[-3:])
        st.state, st.error, st.pid = "error", f"Worker exited (code {code}); restarting. {tail}"[:600], None
        st.restarts += 1
        log.warning("%s exited with %s; restarting", name, code)
        if time.time() - started > 120:
            backoff = 2.0
        try:
            await asyncio.wait_for(w.stopping.wait(), timeout=backoff)
        except asyncio.TimeoutError:
            pass
        backoff = min(backoff * 2, MAX_BACKOFF)
    st.state, st.pid = "stopped", None


def _start(key: Key, env: dict, signature: str) -> None:
    w = Worker(key=key, env=env, signature=signature)
    _workers[key] = w
    w.task = asyncio.get_running_loop().create_task(_supervise(w))


async def _stop(key: Key) -> None:
    w = _workers.pop(key, None)
    if not w:
        return
    w.stopping.set()
    proc = w.proc
    if proc and proc.poll() is None:
        await asyncio.to_thread(_kill, proc, False)
        try:
            await asyncio.wait_for(_wait(proc), timeout=STOP_GRACE)
        except asyncio.TimeoutError:
            await asyncio.to_thread(_kill, proc, True)
    if w.task:
        try:
            await asyncio.wait_for(w.task, timeout=STOP_GRACE)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            w.task.cancel()
    log.info("[voice:%s] stopped", key[1])


# ---------------------------------------------------------------------------
# public API (same shape as scheduler / listeners)
# ---------------------------------------------------------------------------

def sync_workflow(wf: Workflow) -> None:
    _set_workflow(wf)
    _kick()


def remove_workflow(workflow_id: str) -> None:
    _wanted.pop(workflow_id, None)
    _owners.pop(workflow_id, None)
    _problems.pop(workflow_id, None)
    _kick()


def credential_changed(cred_id: str) -> None:
    """A credential was edited or deleted: workers using it restart with the new keys (or stop)."""
    if any(k[0] == str(cred_id) for nodes in _wanted.values() for k in nodes.values()):
        _kick()


async def restart(workflow_id: str, node_id: str) -> bool:
    key = _wanted.get(workflow_id, {}).get(node_id)
    if not key:
        return False
    async with _lock:
        await _stop(key)
    await _reconcile()
    return True


def status(workflow_id: str) -> dict[str, dict]:
    """Per Voice Agent trigger node of this workflow: its worker's state."""
    out = {}
    for nid, key in _wanted.get(workflow_id, {}).items():
        shared = sum(1 for nodes in _wanted.values() if key in nodes.values())
        base = {"kind": "voice_worker", "agent_name": key[1], "workflows": shared, "events": 0, "last_event_at": None}
        if not enabled():
            out[nid] = {**base, "state": "external", "error": None, "since": time.time(), "logs": []}
        elif nid in _problems.get(workflow_id, {}):
            out[nid] = {**base, "state": "error", "error": _problems[workflow_id][nid], "since": time.time(), "logs": []}
        elif (w := _workers.get(key)):
            st = w.status
            out[nid] = {**base, "state": st.state, "error": st.error, "since": st.since, "pid": st.pid,
                        "restarts": st.restarts, "logs": list(st.logs)[-40:]}
        else:
            out[nid] = {**base, "state": "starting", "error": None, "since": time.time(), "logs": []}
    return out


async def load_all() -> None:
    if not enabled():
        log.info("VOICE_WORKERS=off: run voice/worker.py yourself for voice workflows")
        return
    async with SessionLocal() as s:
        for wf in (await s.execute(select(Workflow).where(Workflow.active.is_(True)))).scalars():
            _set_workflow(wf)
    await _reconcile()


async def stop_all() -> None:
    async with _lock:
        await asyncio.gather(*(_stop(k) for k in list(_workers)), return_exceptions=True)
