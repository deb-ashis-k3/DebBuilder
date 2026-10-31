"""Cron triggers for active workflows."""

import hashlib
import json
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from .db import SessionLocal, Workflow
from .engine.errors import Overloaded
from .engine.runtime import RUNNING, execute_workflow, load_workflow

log = logging.getLogger("debbuilder.scheduler")
scheduler = AsyncIOScheduler()

# workflow id → signature of its scheduled triggers: re-adding a job resets an interval's start, so jobs are
# only replaced when the schedule itself changed (not on every autosave of the draft).
_signatures: dict[str, str] = {}
# job id → the execution its last tick started (interval runs never overlap, see _fire).
_last_run: dict[str, str] = {}


async def _fire(workflow_id: str, node_id: str, exclusive: bool = False) -> None:
    job = f"{workflow_id}:{node_id}"
    if exclusive and _last_run.get(job) in RUNNING:
        # The run starts detached (wait=False), so APScheduler's max_instances can't see it: skip this tick here.
        log.info("schedule %s: previous run still in progress, tick skipped", job)
        return
    try:
        wf = await load_workflow(workflow_id, live=True)  # the published version
        res = await execute_workflow(wf, trigger_node_id=node_id, mode="schedule", items=[{"triggered_at": "schedule"}], wait=False)
        if exclusive:
            _last_run[job] = res["execution_id"]
    except Overloaded:
        log.warning("scheduled run of %s skipped: too many executions queued", workflow_id)
    except Exception:  # noqa: BLE001
        log.exception("scheduled run failed for %s", workflow_id)


def _signature(wf: Workflow) -> str:
    nodes = [[n.get("id"), n.get("parameters")] for n in (wf.live_nodes or [])
             if n.get("type") == "trigger.schedule" and not n.get("disabled")]
    return hashlib.sha1(json.dumps([bool(wf.active), nodes], sort_keys=True, default=str).encode()).hexdigest()


def sync_workflow(wf: Workflow) -> None:
    """Make this workflow's jobs match its published schedule triggers. A trigger that can't be scheduled
    (bad cron, unknown timezone, …) is logged and skipped; it never stops the others."""
    prefix = f"{wf.id}:"
    sig = _signature(wf)
    if _signatures.get(str(wf.id)) == sig:
        return  # schedule unchanged: keep the jobs (and their next fire times) as they are
    _signatures[str(wf.id)] = sig
    for job in scheduler.get_jobs():
        if job.id.startswith(prefix):
            job.remove()
    if not wf.active:
        return
    for node in wf.live_nodes:
        if node.get("type") != "trigger.schedule" or node.get("disabled"):
            continue
        params = node.get("parameters") or {}
        job_id = f"{prefix}{node['id']}"
        try:
            if params.get("mode") == "interval":
                try:
                    seconds = max(1.0, float(params.get("seconds") or 60))
                except (TypeError, ValueError):
                    log.warning("invalid interval %r on %s", params.get("seconds"), wf.id)
                    continue
                # Fast intervals: never overlap runs; a late tick is dropped instead of queued behind the last one.
                scheduler.add_job(_fire, IntervalTrigger(seconds=seconds), id=job_id, args=[str(wf.id), node["id"], True],
                                  replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=max(1, int(seconds)))
                continue
            cron = params.get("cron") or "*/15 * * * *"
            trigger = CronTrigger.from_crontab(cron, timezone=params.get("timezone") or "UTC")
            scheduler.add_job(_fire, trigger, id=job_id, args=[str(wf.id), node["id"]],
                              replace_existing=True, max_instances=3, coalesce=True)
        except Exception as e:  # noqa: BLE001 - e.g. ValueError (cron), ZoneInfoNotFoundError (a KeyError)
            log.warning("schedule %r on workflow %s skipped: %s: %s", node.get("name"), wf.id, type(e).__name__, e)


def remove_workflow(workflow_id: str) -> None:
    _signatures.pop(str(workflow_id), None)
    for job in scheduler.get_jobs():
        if job.id.startswith(f"{workflow_id}:"):
            _last_run.pop(job.id, None)
            job.remove()


def remove_all() -> None:
    scheduler.remove_all_jobs()
    _signatures.clear()
    _last_run.clear()


async def load_all() -> None:
    async with SessionLocal() as s:
        for wf in (await s.execute(select(Workflow).where(Workflow.active.is_(True)))).scalars():
            try:
                sync_workflow(wf)
            except Exception:  # noqa: BLE001 - one broken workflow never keeps the others from being scheduled
                log.exception("couldn't schedule workflow %s", wf.id)
