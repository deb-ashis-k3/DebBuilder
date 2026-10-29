import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, desc, func, select

from ..db import AsyncSession, Execution, Workflow, get_session
from ..engine.runtime import cancel_execution, execute_workflow, resume_execution

router = APIRouter(prefix="/api/executions", tags=["executions"])


def serialize(ex: Execution, full: bool = False, wf_name: str | None = None) -> dict[str, Any]:
    d = {
        "id": str(ex.id), "workflow_id": str(ex.workflow_id), "workflow_name": wf_name,
        "status": ex.status, "mode": ex.mode, "started_at": ex.started_at, "finished_at": ex.finished_at,
        "duration_ms": ex.duration_ms, "error": ex.error, "waiting_for": ex.waiting_for,
    }
    if full:
        d.update(input=ex.input, output=ex.output, node_runs=ex.node_runs, workflow=ex.workflow_snapshot,
                 trigger_node=ex.trigger_node)
    return d


class ExecFilter(BaseModel):
    """Same filters as the list query; used by "select all matching" bulk actions."""
    workflow_id: str | None = None
    status: str | None = None
    mode: str | None = None
    started_after: datetime | None = None
    started_before: datetime | None = None


class BulkIn(BaseModel):
    ids: list[str] | None = None
    filter: ExecFilter | None = None


def _conditions(f: ExecFilter) -> list:
    conds = []
    if f.workflow_id:
        try:
            conds.append(Execution.workflow_id.in_([uuid.UUID(w) for w in f.workflow_id.split(",") if w]))
        except ValueError as e:
            raise HTTPException(400, "Invalid workflow id") from e
    if f.status:
        conds.append(Execution.status.in_(f.status.split(",")))
    if f.mode:
        conds.append(Execution.mode.in_(f.mode.split(",")))
    if f.started_after:
        conds.append(Execution.started_at >= f.started_after)
    if f.started_before:
        conds.append(Execution.started_at <= f.started_before)
    return conds


@router.get("")
async def list_executions(workflow_id: str | None = None, status: str | None = None, mode: str | None = None,
                          started_after: datetime | None = None, started_before: datetime | None = None,
                          limit: int = 50, offset: int = 0, session: AsyncSession = Depends(get_session)):
    """Filters accept comma separated values (e.g. status=error,canceled; several workflow ids)."""
    conds = _conditions(ExecFilter(workflow_id=workflow_id, status=status, mode=mode,
                                   started_after=started_after, started_before=started_before))
    q = select(Execution, Workflow.name).join(Workflow, Workflow.id == Execution.workflow_id).where(*conds)
    rows = (await session.execute(q.order_by(desc(Execution.started_at)).limit(min(limit, 200)).offset(offset))).all()
    total = (await session.execute(select(func.count(Execution.id)).where(*conds))).scalar_one()
    return {"total": total, "items": [serialize(ex, wf_name=name) for ex, name in rows]}


def _bulk_conditions(body: BulkIn) -> list:
    if body.ids:
        try:
            return [Execution.id.in_([uuid.UUID(i) for i in body.ids])]
        except ValueError as e:
            raise HTTPException(400, "Invalid execution id") from e
    if body.filter is not None:
        return _conditions(body.filter)
    raise HTTPException(400, "Pass 'ids' or a 'filter'")


@router.post("/delete")
async def bulk_delete(body: BulkIn, session: AsyncSession = Depends(get_session)):
    """Delete selected executions (or all matching a filter). Running ones are stopped first."""
    conds = _bulk_conditions(body)
    running = (await session.execute(select(Execution.id).where(*conds, Execution.status.in_(["running", "waiting"])))).scalars().all()
    for ex_id in running:
        await cancel_execution(str(ex_id))
    res = await session.execute(delete(Execution).where(*conds))
    await session.commit()
    return {"ok": True, "deleted": res.rowcount}


@router.post("/stop")
async def bulk_stop(body: BulkIn, session: AsyncSession = Depends(get_session)):
    conds = _bulk_conditions(body)
    ids = (await session.execute(select(Execution.id).where(*conds, Execution.status.in_(["running", "waiting"])))).scalars().all()
    stopped = [str(i) for i in ids if await cancel_execution(str(i))]
    return {"ok": True, "stopped": len(stopped)}


@router.get("/stats")
async def stats(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(Execution.status, func.count()).group_by(Execution.status))).all()
    return {s: c for s, c in rows}


async def _get(session: AsyncSession, ex_id: str) -> Execution:
    ex = await session.get(Execution, uuid.UUID(ex_id))
    if not ex:
        raise HTTPException(404, "Execution not found")
    return ex


@router.get("/{ex_id}")
async def get_execution(ex_id: str, session: AsyncSession = Depends(get_session)):
    ex = await _get(session, ex_id)
    wf = await session.get(Workflow, ex.workflow_id)
    return serialize(ex, full=True, wf_name=wf.name if wf else None)


@router.delete("/{ex_id}")
async def delete_execution(ex_id: str, session: AsyncSession = Depends(get_session)):
    await session.execute(delete(Execution).where(Execution.id == uuid.UUID(ex_id)))
    await session.commit()
    return {"ok": True}


@router.delete("")
async def prune(workflow_id: str | None = None, session: AsyncSession = Depends(get_session)):
    q = delete(Execution).where(Execution.status.in_(["success", "error", "canceled"]))
    if workflow_id:
        q = q.where(Execution.workflow_id == uuid.UUID(workflow_id))
    await session.execute(q)
    await session.commit()
    return {"ok": True}


class ResumeIn(BaseModel):
    approved: bool = True
    data: dict = {}


@router.post("/{ex_id}/resume")
async def resume(ex_id: str, body: ResumeIn):
    try:
        return await resume_execution(ex_id, {"approved": body.approved, **body.data}, wait=False)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/{ex_id}/cancel")
async def cancel(ex_id: str):
    return {"ok": await cancel_execution(ex_id)}


@router.post("/{ex_id}/retry")
async def retry(ex_id: str, session: AsyncSession = Depends(get_session)):
    ex = await _get(session, ex_id)
    wf = await session.get(Workflow, ex.workflow_id)
    if not wf:
        raise HTTPException(404, "Workflow deleted")
    from ..engine.runtime import workflow_to_dict

    return await execute_workflow(workflow_to_dict(wf), trigger_node_id=ex.trigger_node, items=ex.input,
                                  mode=ex.mode, wait=False)
