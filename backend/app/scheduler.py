"""Cron triggers for active workflows."""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from .db import SessionLocal, Workflow
from .engine.runtime import execute_workflow, load_workflow

log = logging.getLogger("debbuilder.scheduler")
scheduler = AsyncIOScheduler()


async def _fire(workflow_id: str, node_id: str) -> None:
    try:
        wf = await load_workflow(workflow_id)
        await execute_workflow(wf, trigger_node_id=node_id, mode="schedule", items=[{"triggered_at": "schedule"}], wait=False)
    except Exception:  # noqa: BLE001
        log.exception("scheduled run failed for %s", workflow_id)


def sync_workflow(wf: Workflow) -> None:
    prefix = f"{wf.id}:"
    for job in scheduler.get_jobs():
        if job.id.startswith(prefix):
            job.remove()
    if not wf.active:
        return
    for node in wf.nodes or []:
        if node.get("type") != "trigger.schedule" or node.get("disabled"):
            continue
        params = node.get("parameters") or {}
        if params.get("mode") == "interval":
            try:
                seconds = max(1.0, float(params.get("seconds") or 60))
            except (TypeError, ValueError):
                log.warning("invalid interval %r on %s", params.get("seconds"), wf.id)
                continue
            trigger = IntervalTrigger(seconds=seconds)
            # Fast intervals: never overlap runs; a late tick is dropped instead of queued behind the last one.
            scheduler.add_job(_fire, trigger, id=f"{prefix}{node['id']}", args=[str(wf.id), node["id"]],
                              replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=max(1, int(seconds)))
            continue
        cron = params.get("cron") or "*/15 * * * *"
        try:
            trigger = CronTrigger.from_crontab(cron, timezone=params.get("timezone") or "UTC")
        except ValueError as e:
            log.warning("invalid cron %r on %s: %s", cron, wf.id, e)
            continue
        scheduler.add_job(_fire, trigger, id=f"{prefix}{node['id']}", args=[str(wf.id), node["id"]],
                          replace_existing=True, max_instances=3, coalesce=True)


def remove_workflow(workflow_id: str) -> None:
    for job in scheduler.get_jobs():
        if job.id.startswith(f"{workflow_id}:"):
            job.remove()


async def load_all() -> None:
    async with SessionLocal() as s:
        for wf in (await s.execute(select(Workflow).where(Workflow.active.is_(True)))).scalars():
            sync_workflow(wf)
