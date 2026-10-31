"""Several DebBuilder replicas on one Postgres (Kubernetes, Docker Swarm, a second VM, …).

Every replica serves the API, the editor, webhooks and runs executions. Background triggers must
run exactly once, so they belong to a single leader:

- **Leader election**: the replica holding a Postgres advisory lock (on its own connection) runs the
  cron scheduler, the listener triggers (Kafka, IMAP, …) and the voice workers. If it dies, its
  connection drops, Postgres frees the lock and another replica takes over within a few seconds.
- **Change fan-out**: saving / activating / deleting a workflow or editing a credential is broadcast
  with NOTIFY; every replica drops its cached webhook routes and the leader re-syncs that workflow's
  triggers from the database, whichever replica handled the request.

BACKGROUND_TASKS=off makes a replica never take the lead (e.g. pure web pods next to one worker pod).
A single process behaves exactly as before: it is simply always the leader.
"""

import asyncio
import json
import logging
import os
import socket
import uuid

import psycopg

from .config import get_settings

log = logging.getLogger("debbuilder.cluster")

CHANNEL = "debbuilder_changes"
LOCK_KEY = 0x0DEB_B11D  # advisory locks are per database, so separate databases never collide
RETRY_SECONDS = 5
INSTANCE = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"

_leader = False
_tasks: list[asyncio.Task] = []
_lock_conn: psycopg.AsyncConnection | None = None
_stopping = asyncio.Event()


def is_leader() -> bool:
    return _leader


def info() -> dict:
    from . import scheduler

    return {"instance": INSTANCE, "leader": _leader, "background_tasks": get_settings().background_tasks,
            "schedules": len(scheduler.scheduler.get_jobs())}


# ---------------------------------------------------------------------------
# background triggers (leader only)
# ---------------------------------------------------------------------------

_prune_task: asyncio.Task | None = None


async def _start_background() -> None:
    global _prune_task
    from . import listeners, scheduler, voice_workers
    from .engine.store import prune_loop

    await scheduler.load_all()
    await listeners.load_all()
    await voice_workers.load_all()
    _prune_task = asyncio.get_running_loop().create_task(prune_loop())  # execution retention


async def _stop_background() -> None:
    global _prune_task
    from . import listeners, scheduler, voice_workers

    scheduler.scheduler.remove_all_jobs()
    if _prune_task:
        _prune_task.cancel()
        _prune_task = None
    await listeners.stop_all()
    await voice_workers.stop_all()


async def _reload_all() -> None:
    await _stop_background()
    await _start_background()


async def _resync(workflow_id: str) -> None:
    """Leader: make this workflow's triggers match the database."""
    from . import listeners, scheduler, voice_workers
    from .db import SessionLocal, Workflow

    try:
        async with SessionLocal() as s:
            wf = await s.get(Workflow, uuid.UUID(workflow_id))
    except ValueError:
        return
    if wf is None:
        scheduler.remove_workflow(workflow_id)
        listeners.remove_workflow(workflow_id)
        voice_workers.remove_workflow(workflow_id)
    else:
        scheduler.sync_workflow(wf)
        listeners.sync_workflow(wf)
        voice_workers.sync_workflow(wf)


def _apply(kind: str, ident: str | None) -> None:
    """React to a change, made here or on another replica."""
    from . import voice_workers
    from .api.hooks import invalidate_webhooks
    from .credentials import forget_credential

    invalidate_webhooks()
    if kind == "credential":
        forget_credential(ident)  # on every replica, leader or not
    elif kind == "users":
        forget_credential()
    if not _leader or (not ident and kind != "users"):
        return
    if kind == "workflow":
        asyncio.get_running_loop().create_task(_resync(ident))
    elif kind == "credential":
        voice_workers.credential_changed(ident)
    elif kind == "users":  # a deleted user's workflows changed owner: reload everything
        asyncio.get_running_loop().create_task(_reload_all())


# ---------------------------------------------------------------------------
# public: call after committing a change
# ---------------------------------------------------------------------------

def workflow_changed(workflow_id: str) -> None:
    _publish("workflow", workflow_id)


def credential_changed(credential_id: str) -> None:
    _publish("credential", credential_id)


def users_changed() -> None:
    _publish("users", None)


def _publish(kind: str, ident: str | None) -> None:
    _apply(kind, ident)

    async def send() -> None:
        try:
            async with await psycopg.AsyncConnection.connect(get_settings().psycopg_url, autocommit=True) as c:
                await c.execute("SELECT pg_notify(%s, %s)", (CHANNEL, json.dumps({"kind": kind, "id": ident, "from": INSTANCE})))
        except Exception as e:  # noqa: BLE001 - other replicas catch up at their next restart / election
            log.warning("couldn't broadcast %s change: %s", kind, e)
    try:
        asyncio.get_running_loop().create_task(send())
    except RuntimeError:
        pass


# ---------------------------------------------------------------------------
# election + listening loops
# ---------------------------------------------------------------------------

async def _try_lead() -> bool:
    """Take the lock if it's free; keeps the connection that holds it."""
    global _lock_conn, _leader
    try:
        if _lock_conn is None or _lock_conn.closed:
            _lock_conn = await psycopg.AsyncConnection.connect(get_settings().psycopg_url, autocommit=True)
        got = (await (await _lock_conn.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,))).fetchone())[0]
    except Exception as e:  # noqa: BLE001
        log.warning("leader election: %s", e)
        return False
    if got:
        _leader = True
        log.info("this replica (%s) is the leader: running schedules, listeners and voice workers", INSTANCE)
        await _start_background()
    return got


async def _elect_loop() -> None:
    global _leader, _lock_conn
    while not _stopping.is_set():
        if _leader:
            try:  # still holding the lock? (connection alive)
                await _lock_conn.execute("SELECT 1")
            except Exception as e:  # noqa: BLE001
                log.error("lost the leader lock (%s): stopping background triggers", e)
                _leader = False
                _lock_conn = None
                await _stop_background()
        else:
            await _try_lead()
        try:
            await asyncio.wait_for(_stopping.wait(), timeout=RETRY_SECONDS)
        except asyncio.TimeoutError:
            pass


async def _listen_loop() -> None:
    while not _stopping.is_set():
        try:
            async with await psycopg.AsyncConnection.connect(get_settings().psycopg_url, autocommit=True) as c:
                await c.execute(f"LISTEN {CHANNEL}")
                async for n in c.notifies():
                    try:
                        msg = json.loads(n.payload)
                    except ValueError:
                        continue
                    if msg.get("from") != INSTANCE:
                        _apply(msg.get("kind", ""), msg.get("id"))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("change listener reconnecting: %s", e)
            await asyncio.sleep(RETRY_SECONDS)


async def start() -> None:
    from . import scheduler

    _stopping.clear()
    scheduler.scheduler.start()  # runs on every replica; only the leader adds jobs
    if get_settings().background_tasks.lower() != "off":
        await _try_lead()  # first attempt inline: a single replica is leader before serving requests
        _tasks.append(asyncio.create_task(_elect_loop()))
    else:
        log.info("BACKGROUND_TASKS=off: this replica only serves requests")
    _tasks.append(asyncio.create_task(_listen_loop()))


async def stop() -> None:
    global _leader, _lock_conn
    from . import scheduler

    _stopping.set()
    for t in _tasks:
        t.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks.clear()
    if _leader:
        await _stop_background()
    _leader = False
    if _lock_conn is not None:
        await _lock_conn.close()  # releases the lock at once so another replica can lead
        _lock_conn = None
    scheduler.scheduler.shutdown(wait=False)
