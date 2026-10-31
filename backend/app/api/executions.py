import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import delete, desc, func, select
from sqlalchemy.orm import load_only

from ..access import check, principal, visible_workflow_ids
from ..engine.store import stats_for, store, workflow_snapshot
from ..db import AsyncSession, Execution, Workflow, get_session
from ..engine.runtime import cancel_execution, execute_workflow, resume_execution
from ..security import Principal

router = APIRouter(prefix="/api/executions", tags=["executions"])
binary_router = APIRouter(prefix="/api/binary", tags=["executions"])


@binary_router.get("/{file_id}")
async def download_file(file_id: str, name: str = "file", mime: str = "application/octet-stream", download: bool = False,
                        p: Principal = Depends(principal)):
    """A file an execution produced (item["binary"]): only from workspaces the caller may read."""
    from fastapi.responses import FileResponse

    from .. import binary

    path = binary.find(file_id, [p.user.id])
    if path is None:
        raise HTTPException(404, "File not found")
    safe_mime = mime if "/" in mime and len(mime) < 128 and "\n" not in mime else "application/octet-stream"
    # Inline only for types that can't run script in our origin; everything else downloads.
    inline = not download and safe_mime.split(";")[0] in {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf",
                                                         "text/plain", "audio/mpeg", "audio/wav", "video/mp4"}
    return FileResponse(path, media_type=safe_mime, filename=name[:200] or "file",
                        content_disposition_type="inline" if inline else "attachment",
                        headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"})


COUNT_CAP = 10_000


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


def _conditions(f: ExecFilter, p: Principal, write: bool = False) -> list:
    """Filter conditions, always limited to executions of workflows the caller may see (or change)."""
    conds = [Execution.workflow_id.in_(visible_workflow_ids(p, write))]
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


_LIST_COLUMNS = (Execution.id, Execution.workflow_id, Execution.status, Execution.mode, Execution.started_at,
                 Execution.finished_at, Execution.duration_ms, Execution.error, Execution.waiting_for)


@router.get("")
async def list_executions(workflow_id: str | None = None, status: str | None = None, mode: str | None = None,
                          started_after: datetime | None = None, started_before: datetime | None = None,
                          limit: int = Query(50, ge=1), offset: int = Query(0, ge=0),
                          session: AsyncSession = Depends(get_session),
                          p: Principal = Depends(principal)):
    """Filters accept comma separated values (e.g. status=error,canceled; several workflow ids)."""
    conds = _conditions(ExecFilter(workflow_id=workflow_id, status=status, mode=mode,
                                   started_after=started_after, started_before=started_before), p)
    # List rows never load the run data (input / output / node_runs / snapshot JSON): only what the list shows.
    q = (select(Execution, Workflow.name).options(load_only(*_LIST_COLUMNS))
         .join(Workflow, Workflow.id == Execution.workflow_id).where(*conds))
    rows = (await session.execute(q.order_by(desc(Execution.started_at)).limit(min(limit, 200)).offset(offset))).all()
    # Exact counts over millions of rows are slow; past COUNT_CAP the page says "10,000+".
    total = (await session.execute(select(func.count()).select_from(
        select(Execution.id).where(*conds).limit(COUNT_CAP + 1).subquery()))).scalar_one()
    return {"total": min(total, COUNT_CAP), "total_capped": total > COUNT_CAP,
            "items": [serialize(ex, wf_name=name) for ex, name in rows]}


def _bulk_conditions(body: BulkIn, p: Principal) -> list:
    if body.ids:
        try:
            return [Execution.id.in_([uuid.UUID(i) for i in body.ids]), *_conditions(ExecFilter(), p, write=True)]
        except ValueError as e:
            raise HTTPException(400, "Invalid execution id") from e
    if body.filter is not None:
        return _conditions(body.filter, p, write=True)
    raise HTTPException(400, "Pass 'ids' or a 'filter'")


@router.post("/delete")
async def bulk_delete(body: BulkIn, session: AsyncSession = Depends(get_session), p: Principal = Depends(principal)):
    """Delete selected executions (or all matching a filter). Running ones are stopped first."""
    conds = _bulk_conditions(body, p)
    running = (await session.execute(select(Execution.id).where(*conds, Execution.status.in_(["running", "waiting"])))).scalars().all()
    for ex_id in running:
        await cancel_execution(str(ex_id))
    res = await session.execute(delete(Execution).where(*conds).execution_options(synchronize_session=False))
    await session.commit()
    return {"ok": True, "deleted": res.rowcount}


@router.post("/stop")
async def bulk_stop(body: BulkIn, session: AsyncSession = Depends(get_session), p: Principal = Depends(principal)):
    conds = _bulk_conditions(body, p)
    ids = (await session.execute(select(Execution.id).where(*conds, Execution.status.in_(["running", "waiting"])))).scalars().all()
    stopped = [str(i) for i in ids if await cancel_execution(str(i))]
    return {"ok": True, "stopped": len(stopped)}


@router.get("/stats")
async def stats(session: AsyncSession = Depends(get_session), p: Principal = Depends(principal)):
    # From the per-day rollup the execution writer maintains, plus live running / waiting rows.
    return await stats_for(session, visible_workflow_ids(p))


async def _get(session: AsyncSession, ex_id: str, p: Principal, write: bool = False) -> tuple[Execution, Workflow | None]:
    try:
        key = uuid.UUID(ex_id)
    except ValueError:
        raise HTTPException(404, "Execution not found") from None
    # Not in the database yet? The batched writer may still hold it (written within EXECUTIONS_FLUSH_MS).
    ex = await session.get(Execution, key) or store.pending(key)
    if not ex:
        raise HTTPException(404, "Execution not found")
    wf = await session.get(Workflow, ex.workflow_id)
    check(p, wf.owner_id if wf else None, "Execution", write)
    return ex, wf


@router.get("/{ex_id}")
async def get_execution(ex_id: str, session: AsyncSession = Depends(get_session), p: Principal = Depends(principal)):
    ex, wf = await _get(session, ex_id, p)
    return {**serialize(ex, full=True, wf_name=wf.name if wf else None), "workflow": await workflow_snapshot(ex, session)}


@router.delete("/{ex_id}")
async def delete_execution(ex_id: str, session: AsyncSession = Depends(get_session), p: Principal = Depends(principal)):
    ex, _ = await _get(session, ex_id, p, write=True)
    await session.delete(ex)
    await session.commit()
    return {"ok": True}


@router.delete("")
async def prune(workflow_id: str | None = None, session: AsyncSession = Depends(get_session), p: Principal = Depends(principal)):
    q = delete(Execution).where(Execution.status.in_(["success", "error", "canceled"]),
                                *_conditions(ExecFilter(workflow_id=workflow_id), p, write=True))
    await session.execute(q.execution_options(synchronize_session=False))
    await session.commit()
    return {"ok": True}


class ResumeIn(BaseModel):
    approved: bool = True
    data: dict = {}


@router.post("/{ex_id}/resume")
async def resume(ex_id: str, body: ResumeIn, session: AsyncSession = Depends(get_session), p: Principal = Depends(principal)):
    await _get(session, ex_id, p, write=True)
    try:
        return await resume_execution(ex_id, {"approved": body.approved, **body.data}, wait=False)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/{ex_id}/cancel")
async def cancel(ex_id: str, session: AsyncSession = Depends(get_session), p: Principal = Depends(principal)):
    await _get(session, ex_id, p, write=True)
    return {"ok": await cancel_execution(ex_id)}


@router.post("/{ex_id}/retry")
async def retry(ex_id: str, session: AsyncSession = Depends(get_session), p: Principal = Depends(principal)):
    ex, wf = await _get(session, ex_id, p, write=True)
    if not wf:
        raise HTTPException(404, "Workflow deleted")
    from ..engine.runtime import workflow_to_dict

    return await execute_workflow(workflow_to_dict(wf, live=wf.active), trigger_node_id=ex.trigger_node, items=ex.input,
                                  mode=ex.mode, wait=False)
