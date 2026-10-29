import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import desc, select, update

from .. import listeners, scheduler
from .hooks import invalidate_webhooks
from ..db import AsyncSession, Folder, Workflow, get_session
from ..engine.runtime import execute_workflow, workflow_to_dict
from ..nodes import REGISTRY
from ..templates import TEMPLATES

router = APIRouter(prefix="/api", tags=["workflows"])


class WorkflowIn(BaseModel):
    name: str | None = None
    description: str | None = None
    nodes: list[dict] | None = None
    edges: list[dict] | None = None
    settings: dict | None = None
    tags: list[str] | None = None
    active: bool | None = None
    folder_id: str | None = None


class IdsIn(BaseModel):
    ids: list[str]
    folder_id: str | None = None


class DuplicateIn(BaseModel):
    name: str | None = None
    folder_id: str | None = None


class ImportIn(BaseModel):
    # A single exported workflow, a list of them, or {"workflows": [...]} (bulk export file).
    data: Any = None
    url: str | None = None
    folder_id: str | None = None


class RunIn(BaseModel):
    trigger_node_id: str | None = None
    items: list[dict] | None = None
    # Run only up to (and including) this node — n8n's "Execute step".
    destination_node_id: str | None = None
    # Run the unsaved graph currently in the editor.
    workflow: dict | None = None


def serialize(wf: Workflow, full: bool = True) -> dict[str, Any]:
    d = {
        "id": str(wf.id), "name": wf.name, "description": wf.description, "active": wf.active,
        "tags": wf.tags or [], "created_at": wf.created_at, "updated_at": wf.updated_at,
        "folder_id": str(wf.folder_id) if wf.folder_id else None,
        "node_count": len(wf.nodes or []),
        "trigger_types": sorted({n["type"] for n in wf.nodes or [] if n["type"].startswith("trigger.")}),
    }
    if full:
        d.update(nodes=wf.nodes or [], edges=wf.edges or [], settings=wf.settings or {})
    return d


async def _get(session: AsyncSession, wf_id: str) -> Workflow:
    try:
        wf = await session.get(Workflow, uuid.UUID(wf_id))
    except ValueError:
        wf = None
    if not wf:
        raise HTTPException(404, "Workflow not found")
    return wf


def _validate(nodes: list[dict]) -> None:
    for n in nodes:
        if n.get("type") not in REGISTRY:
            raise HTTPException(400, f"Unknown node type {n.get('type')}")


def _uuid(v: str | None) -> uuid.UUID | None:
    if not v:
        return None
    try:
        return uuid.UUID(v)
    except ValueError as e:
        raise HTTPException(400, f"Invalid id '{v}'") from e


async def _folder(session: AsyncSession, folder_id: str | None) -> uuid.UUID | None:
    fid = _uuid(folder_id)
    if fid and not await session.get(Folder, fid):
        raise HTTPException(404, "Folder not found")
    return fid


def export_dict(wf: Workflow) -> dict:
    """Portable workflow file (no ids of this instance, no active flag)."""
    return {"name": wf.name, "description": wf.description, "nodes": wf.nodes or [], "edges": wf.edges or [],
            "settings": wf.settings or {}, "tags": wf.tags or [], "meta": {"exported_from": "DebBuilder",
            "exported_at": datetime.now(timezone.utc).isoformat()}}


@router.get("/node-types")
async def node_types():
    return [nt.schema() for nt in REGISTRY.values()]


@router.get("/code-runtimes")
async def code_runtimes():
    """Which Code node languages this server can run (and where their runtimes are)."""
    import asyncio

    from ..nodes.code_runtimes import runtimes_status

    return await asyncio.to_thread(runtimes_status)


@router.get("/workflows")
async def list_workflows(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(Workflow).order_by(desc(Workflow.updated_at)))).scalars().all()
    return [serialize(w, full=False) for w in rows]


@router.post("/workflows/move")
async def move_workflows(body: IdsIn, session: AsyncSession = Depends(get_session)):
    fid = await _folder(session, body.folder_id)
    ids = [_uuid(i) for i in body.ids]
    await session.execute(update(Workflow).where(Workflow.id.in_(ids)).values(folder_id=fid))
    await session.commit()
    return {"ok": True, "moved": len(ids)}


@router.post("/workflows/delete")
async def delete_workflows(body: IdsIn, session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(Workflow).where(Workflow.id.in_([_uuid(i) for i in body.ids])))).scalars().all()
    for wf in rows:
        scheduler.remove_workflow(str(wf.id))
        listeners.remove_workflow(str(wf.id))
        await session.delete(wf)
    await session.commit()
    invalidate_webhooks()
    return {"ok": True, "deleted": len(rows)}


@router.get("/workflows/export")
async def export_workflows(ids: str | None = None, folder_id: str | None = None, session: AsyncSession = Depends(get_session)):
    """Bulk export: comma separated ids, or every workflow in a folder (and its subfolders)."""
    q = select(Workflow).order_by(Workflow.name)
    if ids:
        q = q.where(Workflow.id.in_([_uuid(i) for i in ids.split(",") if i]))
    elif folder_id:
        from .folders import _subtree

        q = q.where(Workflow.folder_id.in_(await _subtree(session, _uuid(folder_id))))
    rows = (await session.execute(q)).scalars().all()
    return {"workflows": [export_dict(w) for w in rows]}


@router.post("/workflows/import")
async def import_workflows(body: ImportIn, session: AsyncSession = Depends(get_session)):
    data = body.data
    if body.url:
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
                r = await c.get(body.url)
                r.raise_for_status()
                data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise HTTPException(400, f"Could not load workflow JSON from URL: {e}") from e
    if isinstance(data, dict) and isinstance(data.get("workflows"), list):
        data = data["workflows"]
    items = data if isinstance(data, list) else [data]
    if not items or not all(isinstance(w, dict) and isinstance(w.get("nodes"), list) for w in items):
        raise HTTPException(400, "Not a DebBuilder workflow file (expected an object with 'nodes' and 'edges')")
    if any("connections" in w and "edges" not in w for w in items):
        raise HTTPException(400, "This looks like an n8n export. n8n nodes aren't compatible; rebuild it with DebBuilder nodes.")
    for w in items:
        _validate(w["nodes"])
    fid = await _folder(session, body.folder_id)
    created = []
    for w in items:
        wf = Workflow(name=str(w.get("name") or "Imported workflow")[:200], description=w.get("description") or "",
                      nodes=w["nodes"], edges=w.get("edges") or [], settings=w.get("settings") or {},
                      tags=w.get("tags") or [], folder_id=fid)
        session.add(wf)
        created.append(wf)
    await session.commit()
    for wf in created:
        await session.refresh(wf)
    return [serialize(wf, full=False) for wf in created]


@router.post("/workflows")
async def create_workflow(body: WorkflowIn, session: AsyncSession = Depends(get_session)):
    nodes = body.nodes or []
    _validate(nodes)
    wf = Workflow(name=body.name or "My workflow", description=body.description or "", nodes=nodes,
                  edges=body.edges or [], settings=body.settings or {}, tags=body.tags or [],
                  folder_id=await _folder(session, body.folder_id))
    session.add(wf)
    await session.commit()
    await session.refresh(wf)
    return serialize(wf)


@router.get("/workflows/{wf_id}")
async def get_workflow(wf_id: str, session: AsyncSession = Depends(get_session)):
    return serialize(await _get(session, wf_id))


@router.put("/workflows/{wf_id}")
async def update_workflow(wf_id: str, body: WorkflowIn, session: AsyncSession = Depends(get_session)):
    wf = await _get(session, wf_id)
    data = body.model_dump(exclude_none=True)
    if "folder_id" in body.model_fields_set:  # explicit null = move to the top level
        data["folder_id"] = await _folder(session, body.folder_id)
    if "nodes" in data:
        _validate(data["nodes"])
    for k, v in data.items():
        setattr(wf, k, v)
    await session.commit()
    await session.refresh(wf)
    scheduler.sync_workflow(wf)
    listeners.sync_workflow(wf)
    invalidate_webhooks()
    return serialize(wf)


@router.delete("/workflows/{wf_id}")
async def delete_workflow(wf_id: str, session: AsyncSession = Depends(get_session)):
    wf = await _get(session, wf_id)
    await session.delete(wf)
    await session.commit()
    scheduler.remove_workflow(wf_id)
    listeners.remove_workflow(wf_id)
    invalidate_webhooks()
    return {"ok": True}


@router.get("/workflows/{wf_id}/listeners")
async def listener_status(wf_id: str):
    """Live state of this workflow's listener triggers (Kafka, Redis, …), keyed by node id."""
    return listeners.status(wf_id)


@router.get("/workflows/{wf_id}/export")
async def export_workflow(wf_id: str, session: AsyncSession = Depends(get_session)):
    return export_dict(await _get(session, wf_id))


@router.post("/workflows/{wf_id}/duplicate")
async def duplicate_workflow(wf_id: str, body: DuplicateIn | None = None, session: AsyncSession = Depends(get_session)):
    src = await _get(session, wf_id)
    body = body or DuplicateIn()
    folder = await _folder(session, body.folder_id) if "folder_id" in body.model_fields_set else src.folder_id
    wf = Workflow(name=(body.name or f"{src.name} (copy)")[:200], description=src.description, nodes=src.nodes,
                  edges=src.edges, settings=src.settings, tags=src.tags, folder_id=folder)
    session.add(wf)
    await session.commit()
    await session.refresh(wf)
    return serialize(wf)


@router.post("/workflows/{wf_id}/run")
async def run_workflow(wf_id: str, body: RunIn, session: AsyncSession = Depends(get_session)):
    wf = await _get(session, wf_id)
    graph = workflow_to_dict(wf)
    if body.workflow:
        _validate(body.workflow.get("nodes", []))
        graph.update({k: body.workflow[k] for k in ("nodes", "edges", "settings") if k in body.workflow})
    try:
        return await execute_workflow(graph, trigger_node_id=body.trigger_node_id, items=body.items,
                                      mode="manual", wait=False, destination_node_id=body.destination_node_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/templates")
async def list_templates():
    return [{"id": k, "name": t["name"], "description": t["description"], "tags": t.get("tags", [])}
            for k, t in TEMPLATES.items()]


@router.post("/templates/{template_id}")
async def create_from_template(template_id: str, session: AsyncSession = Depends(get_session)):
    t = TEMPLATES.get(template_id)
    if not t:
        raise HTTPException(404, "Template not found")
    wf = Workflow(name=t["name"], description=t["description"], nodes=t["nodes"], edges=t["edges"],
                  settings=t.get("settings", {}), tags=t.get("tags", []))
    session.add(wf)
    await session.commit()
    await session.refresh(wf)
    return serialize(wf)
