"""Long-running trigger listeners (Kafka, Redis, RabbitMQ, MQTT, Postgres, IMAP, …).

Like the scheduler handles cron triggers, this module keeps one supervised asyncio task
per listener trigger node of every *active* workflow:

- a listener is ``async def fn(lc: ListenCtx)`` that connects, calls ``lc.ready()`` and
  then loops, handing each event to ``await lc.emit(items)``;
- the supervisor reconnects with exponential backoff and keeps a status record the
  editor shows (listening / error, event count, last event);
- ``emit`` starts an execution; at most ``max_concurrency`` run at once per listener,
  so a flood of messages applies backpressure to the broker instead of piling up tasks;
- editor test runs reuse the same listener with ``lc.test = True`` and stop at the first
  event (listeners then avoid acking / consumer groups so production traffic is untouched).
"""

import asyncio
import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sqlalchemy import select

from .credentials import load_credential
from .db import SessionLocal, Workflow

log = logging.getLogger("debbuilder.listeners")

ListenerFn = Callable[["ListenCtx"], Awaitable[None]]
LISTENERS: dict[str, ListenerFn] = {}
# Listeners that watch ``lc.stopping`` and exit on their own: cancelling them mid-operation
# could lose data (e.g. a BLPOP that already removed a job from Redis).
GRACEFUL: set[str] = set()
GRACE_SECONDS = 5


def listener(node_type: str, *, graceful: bool = False):
    def deco(fn: ListenerFn) -> ListenerFn:
        LISTENERS[node_type] = fn
        if graceful:
            GRACEFUL.add(node_type)
        return fn
    return deco


@dataclass
class Status:
    state: str = "starting"  # starting | listening | error | stopped
    error: str | None = None
    events: int = 0
    last_event_at: float | None = None
    since: float = field(default_factory=time.time)

    def public(self) -> dict:
        return {"state": self.state, "error": self.error, "events": self.events,
                "last_event_at": self.last_event_at, "since": self.since}


@dataclass
class ListenCtx:
    workflow_id: str
    node: dict
    params: dict
    cred: dict
    emit: Callable[..., Awaitable[dict | None]]
    test: bool = False
    reason: str = "activated"  # why the listener (re)started: activated | started | updated
    status: Status = field(default_factory=Status)
    stopping: asyncio.Event = field(default_factory=asyncio.Event)
    _ready: Callable[[], None] = lambda: None

    def ready(self) -> None:
        """Call once connected/subscribed; resets the reconnect backoff."""
        self.status.state, self.status.error, self.status.since = "listening", None, time.time()
        self._ready()

    @property
    def node_id(self) -> str:
        return self.node["id"]


class _Captured(Exception):
    """Raised from emit() in test mode to stop the listener after the first event."""


# ---------------------------------------------------------------------------
# helpers shared by listener implementations
# ---------------------------------------------------------------------------

def parse_payload(value: Any, as_json: bool = True) -> Any:
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode()
        except UnicodeDecodeError:
            return {"base64": base64.b64encode(value).decode()}
    if as_json and isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def csv(v) -> list[str]:
    return [s.strip() for s in str(v or "").split(",") if s.strip()]


# ---------------------------------------------------------------------------
# params / credentials
# ---------------------------------------------------------------------------

def _params(node: dict) -> dict:
    from .nodes import get_type

    params = {p["name"]: p.get("default") for p in get_type(node["type"]).properties}
    params.update(node.get("parameters") or {})
    return params


async def _cred(params: dict) -> dict:
    if not params.get("credential"):
        return {}
    async with SessionLocal() as s:
        return (await load_credential(s, params["credential"]))[1]


# ---------------------------------------------------------------------------
# supervisor
# ---------------------------------------------------------------------------

@dataclass
class _Entry:
    task: asyncio.Task
    signature: str
    status: Status
    stopping: asyncio.Event
    graceful: bool


_running: dict[tuple[str, str], _Entry] = {}
_inflight: set[asyncio.Task] = set()  # strong refs: unreferenced tasks may be garbage-collected mid-run


def _signature(node: dict) -> str:
    return hashlib.sha1(json.dumps([node.get("type"), node.get("parameters"), node.get("disabled")], sort_keys=True, default=str).encode()).hexdigest()


def _emitter(workflow_id: str, node: dict, status: Status, limit: int):
    from .engine.runtime import execute_workflow, load_workflow

    sem = asyncio.Semaphore(max(1, limit))

    async def run(items: list[dict]) -> dict:
        try:
            wf = await load_workflow(workflow_id)
            return await execute_workflow(wf, trigger_node_id=node["id"], mode="trigger", items=items, wait=True)
        except Exception as e:  # noqa: BLE001
            log.exception("listener execution failed (%s / %s)", workflow_id, node.get("name"))
            return {"status": "error", "error": str(e)}
        finally:
            sem.release()

    async def emit(items: list[dict], wait: bool = False) -> dict | None:
        """Start an execution with these trigger items. With wait=True return its result
        (for listeners that ack only after the workflow finished)."""
        status.events += 1
        status.last_event_at = time.time()
        await sem.acquire()  # backpressure: blocks the listener while `limit` runs are in flight
        if wait:
            return await run(items)
        task = asyncio.get_running_loop().create_task(run(items))
        _inflight.add(task)
        task.add_done_callback(_inflight.discard)
        return None

    return emit


async def _supervise(workflow_id: str, node: dict, status: Status, reason: str, stopping: asyncio.Event) -> None:
    fn = LISTENERS[node["type"]]
    delay = 1.0
    first = True
    while not stopping.is_set():
        if status.state != "error":  # keep showing the last error while retrying
            status.state = "starting"
        try:
            params = _params(node)
            lc = ListenCtx(workflow_id=workflow_id, node=node, params=params, cred=await _cred(params),
                           emit=_emitter(workflow_id, node, status, int(params.get("max_concurrency") or 5)),
                           reason=reason if first else "reconnected", status=status, stopping=stopping)

            def reset() -> None:
                nonlocal delay
                delay = 1.0

            lc._ready = reset
            first = False
            await fn(lc)
            if stopping.is_set():
                break
            # A listener that returns (e.g. one-shot workflow events) just stays "listening".
            if status.state != "error":
                status.state = "listening"
            await stopping.wait()
        except asyncio.CancelledError:
            status.state = "stopped"
            raise
        except Exception as e:  # noqa: BLE001
            status.state, status.error = "error", f"{type(e).__name__}: {e}"
            log.warning("listener %s/%s failed: %s (retry in %.0fs)", workflow_id, node.get("name"), status.error, delay)
            try:
                await asyncio.wait_for(stopping.wait(), timeout=delay)
            except TimeoutError:
                pass
            delay = min(delay * 2, 60)
    status.state = "stopped"


def _start(workflow_id: str, node: dict, reason: str) -> None:
    status, stopping = Status(), asyncio.Event()
    task = asyncio.get_running_loop().create_task(_supervise(workflow_id, node, status, reason, stopping))
    _running[(workflow_id, node["id"])] = _Entry(task, _signature(node), status, stopping, node["type"] in GRACEFUL)


def _stop(key: tuple[str, str]) -> None:
    entry = _running.pop(key, None)
    if not entry:
        return
    entry.stopping.set()
    if entry.graceful:
        # Let it finish the current poll and exit by itself; cancel only if it hangs.
        asyncio.get_running_loop().call_later(GRACE_SECONDS, entry.task.cancel)
    else:
        entry.task.cancel()


def sync_workflow(wf: Workflow, reason: str | None = None) -> None:
    """Start/stop/restart listeners so they match the workflow's active state and nodes."""
    wf_id = str(wf.id)
    had_any = any(k[0] == wf_id for k in _running)
    wanted = {n["id"]: n for n in (wf.nodes or []) if wf.active and n.get("type") in LISTENERS and not n.get("disabled")}
    for key in [k for k in _running if k[0] == wf_id and k[1] not in wanted]:
        _stop(key)
    for nid, node in wanted.items():
        entry = _running.get((wf_id, nid))
        if entry and entry.signature == _signature(node):
            continue
        if entry:
            _stop((wf_id, nid))
        _start(wf_id, node, reason or ("updated" if had_any else "activated"))


def remove_workflow(workflow_id: str) -> None:
    for key in [k for k in _running if k[0] == workflow_id]:
        _stop(key)


def status(workflow_id: str) -> dict[str, dict]:
    return {k[1]: e.status.public() for k, e in _running.items() if k[0] == workflow_id}


async def load_all() -> None:
    async with SessionLocal() as s:
        for wf in (await s.execute(select(Workflow).where(Workflow.active.is_(True)))).scalars():
            sync_workflow(wf, reason="started")


async def stop_all() -> None:
    tasks = [e.task for e in _running.values()]
    for key in list(_running):
        _stop(key)
    if tasks:
        await asyncio.wait(tasks, timeout=GRACE_SECONDS + 1)


# ---------------------------------------------------------------------------
# editor test runs
# ---------------------------------------------------------------------------

async def listen_once(workflow_id: str, node: dict, params: dict, cred: dict, timeout: float) -> list[dict]:
    """Run the node's listener in test mode until it produces one event."""
    loop = asyncio.get_running_loop()
    got: asyncio.Future = loop.create_future()

    async def emit(items: list[dict], wait: bool = False) -> dict:
        if not got.done():
            got.set_result(items)
        raise _Captured

    lc = ListenCtx(workflow_id=workflow_id, node=node, params=params, cred=cred, emit=emit, test=True, reason="test")
    task = loop.create_task(LISTENERS[node["type"]](lc))
    try:
        await asyncio.wait({got, task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if got.done():
            return got.result()
        if task.done() and task.exception() and not isinstance(task.exception(), _Captured):
            raise task.exception()
        raise TimeoutError(f"No event received within {timeout:.0f}s. Send one now, or pin test data on this node.")
    finally:
        if got.done() and not task.done():
            # The listener is unwinding after the captured event; let its cleanup finish
            # (e.g. Postgres drops its temporary notify trigger) instead of cancelling it.
            await asyncio.wait({task}, timeout=10)
        if not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=5)
        if task.done() and not task.cancelled():
            task.exception()  # mark retrieved (normally our _Captured) so asyncio doesn't log it
