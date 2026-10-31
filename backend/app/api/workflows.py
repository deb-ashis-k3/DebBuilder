import asyncio
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, StringConstraints
from sqlalchemy import and_, desc, func, literal_column, or_, select, update
from sqlalchemy.exc import IntegrityError

from .. import cluster, listeners, voice_workers
from ..access import check, owned, owner_names, principal
from ..config import get_settings
from ..db import AsyncSession, Folder, User, Workflow, WorkflowVersion, get_session, graph_hash
from ..engine.runtime import execute_workflow, workflow_to_dict
from ..netguard import guarded_client
from ..nodes import REGISTRY
from ..security import Principal
from ..templates import TEMPLATES

router = APIRouter(prefix="/api", tags=["workflows"])


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class WorkflowIn(BaseModel):
    name: Name | None = None
    description: str | None = None
    nodes: list[dict] | None = None
    edges: list[dict] | None = None
    settings: dict | None = None
    tags: list[Annotated[str, StringConstraints(max_length=100)]] | None = None
    active: bool | None = None
    folder_id: str | None = None
    # The revision the editor loaded: if another tab / session saved since, the save is refused with 409.
    revision: int | None = None


class IdsIn(BaseModel):
    ids: list[str]
    folder_id: str | None = None


class DuplicateIn(BaseModel):
    name: Name | None = None
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


def serialize(wf: Workflow, full: bool = True, owner_name: str | None = None, node_count: int | None = None,
              trigger_types: list[str] | None = None) -> dict[str, Any]:
    """full=False with node_count / trigger_types given (computed by the database) never touches the graph, so list
    queries can leave nodes / edges unloaded."""
    if node_count is None:
        node_count = len(wf.nodes or [])
        trigger_types = sorted({n["type"] for n in wf.nodes or [] if n["type"].startswith("trigger.")})
    d = {
        "id": str(wf.id), "name": wf.name, "description": wf.description, "active": wf.active,
        "owner_id": str(wf.owner_id) if wf.owner_id else None, "owner_name": owner_name,
        "tags": wf.tags or [], "created_at": wf.created_at, "updated_at": wf.updated_at,
        "folder_id": str(wf.folder_id) if wf.folder_id else None,
        "node_count": node_count,
        "trigger_types": sorted(trigger_types or []),
        # Draft / publish (n8n): production runs the published version; edits stay in the draft until published.
        "published_version_id": str(wf.published_version_id) if wf.published_version_id else None,
        "published_version": wf.published_version_number,
        "has_unpublished_changes": draft_changed(wf, node_count, fresh=full),
        "revision": wf.revision or 0,
    }
    if full:
        d.update(nodes=wf.nodes or [], edges=wf.edges or [], settings=wf.settings or {})
    return d


def draft_changed(wf: Workflow, node_count: int | None = None, fresh: bool = True) -> bool:
    """The draft differs from what production runs (never published counts as changed when it has nodes).
    fresh=False uses the stored draft_hash instead of hashing the graph (lists)."""
    if not wf.published_version_id:
        return bool(node_count if node_count is not None else wf.nodes)
    h = graph_hash(wf.nodes or [], wf.edges or []) if fresh or not wf.draft_hash else wf.draft_hash
    return h != wf.published_hash


def _version_dict(v: WorkflowVersion, author: str | None = None, published_id=None, full: bool = False) -> dict[str, Any]:
    d = {"id": str(v.id), "number": v.number, "note": v.note, "created_at": v.created_at, "author": author,
         "node_count": len(v.nodes or []), "published": v.id == published_id}
    if full:
        d.update(nodes=v.nodes or [], edges=v.edges or [])
    return d


def _publish(wf: Workflow, v: WorkflowVersion) -> None:
    wf.published_version_id, wf.published_version_number, wf.published_hash = v.id, v.number, v.content_hash
    wf.published_nodes, wf.published_edges = v.nodes or [], v.edges or []


async def _snapshot_draft(session: AsyncSession, wf: Workflow, p: Principal, note: str | None = None) -> WorkflowVersion:
    """A version holding the current draft. Publishing an unchanged draft again reuses its latest version.
    Version numbers are unique per workflow (index): if a concurrent publish took the next number, take the one after."""
    h = graph_hash(wf.nodes or [], wf.edges or [])
    for _ in range(5):
        latest = (await session.execute(select(WorkflowVersion).where(WorkflowVersion.workflow_id == wf.id)
                                        .order_by(desc(WorkflowVersion.number)).limit(1))).scalar_one_or_none()
        if latest and latest.content_hash == h and not note:
            return latest
        v = WorkflowVersion(workflow_id=wf.id, number=(latest.number if latest else 0) + 1, nodes=wf.nodes or [],
                            edges=wf.edges or [], content_hash=h, note=(note or "").strip()[:500] or None,
                            created_by=p.user.id)
        try:
            async with session.begin_nested():
                session.add(v)
                await session.flush()
            return v
        except IntegrityError:
            continue
    raise HTTPException(409, "This workflow is being published from somewhere else right now. Try again.")


def _webhook_keys(nodes: list) -> set[tuple[str, str]]:
    """(METHOD, path) of the enabled production webhook triggers in a graph, as the router matches them."""
    out = set()
    for n in nodes or []:
        if isinstance(n, dict) and n.get("type") == "trigger.webhook" and not n.get("disabled"):
            prm = n.get("parameters") or {}
            out.add((str(prm.get("method") or "POST").upper(), str(prm.get("path") or "").strip("/")))
    return out


async def _check_webhook_paths(session: AsyncSession, wf: Workflow) -> None:
    """Before a workflow goes live: no other active workflow may already serve one of its webhook paths
    (production webhooks would otherwise silently keep going to whichever was found first)."""
    mine = _webhook_keys(wf.live_nodes)
    if not mine:
        return
    # Held until commit, so two workflows taking the same path at the same moment can't both pass.
    await session.execute(select(func.pg_advisory_xact_lock(233812)))
    hook = [{"type": "trigger.webhook"}]
    rows = (await session.execute(
        select(Workflow.id, Workflow.name, Workflow.owner_id, Workflow.published_version_id, Workflow.published_nodes,
               Workflow.nodes)
        .where(Workflow.active.is_(True), Workflow.id != wf.id,
               or_(and_(Workflow.published_version_id.is_not(None), Workflow.published_nodes.contains(hook)),
                   and_(Workflow.published_version_id.is_(None), Workflow.nodes.contains(hook)))))).all()
    for _, name, owner_id, published_id, published_nodes, nodes in rows:
        taken = mine & _webhook_keys(published_nodes if published_id else nodes)
        if taken:
            method, path = sorted(taken)[0]
            # Workspaces are private: name the other workflow only to its own owner.
            who = f"Your workflow '{name}'" if owner_id == wf.owner_id else "Another active workflow"
            raise HTTPException(409, f"{who} already listens on {method} /webhook/{path}. Use a different path in "
                                     "this workflow's Webhook trigger, or deactivate the other workflow first.")


async def _get(session: AsyncSession, wf_id: str, p: Principal, write: bool = False, lock: bool = False) -> Workflow:
    """lock=True: SELECT ... FOR UPDATE, so concurrent saves / publishes of one workflow take turns."""
    try:
        wf = await session.get(Workflow, uuid.UUID(wf_id), with_for_update=lock or None)
    except ValueError:
        wf = None
    if not wf:
        raise HTTPException(404, "Workflow not found")
    check(p, wf.owner_id, "Workflow", write)
    return wf


async def _serialize_one(session: AsyncSession, wf: Workflow) -> dict[str, Any]:
    return serialize(wf, owner_name=(await owner_names(session, [wf.owner_id])).get(wf.owner_id))


def _validate(nodes: Any, edges: Any = None) -> None:
    """Shape of a graph: malformed nodes / edges are refused (422) instead of breaking the editor or a run later."""
    if not isinstance(nodes, list):
        raise HTTPException(422, "'nodes' must be a list")
    seen = set()
    for i, n in enumerate(nodes):
        if not isinstance(n, dict):
            raise HTTPException(422, f"Node {i + 1} must be an object")
        if not isinstance(n.get("id"), str) or not n["id"]:
            raise HTTPException(422, f"Node {i + 1} needs a string 'id'")
        if n["id"] in seen:
            raise HTTPException(422, f"Two nodes share the id '{n['id'][:100]}'")
        seen.add(n["id"])
        if not isinstance(n.get("type"), str):
            raise HTTPException(422, f"Node '{n['id'][:100]}' needs a string 'type'")
        if n["type"] not in REGISTRY:
            raise HTTPException(400, f"Unknown node type {n.get('type')}")
        if "name" in n and not isinstance(n["name"], str):
            raise HTTPException(422, f"Node '{n['id'][:100]}': 'name' must be a string")
        for key in ("parameters", "position"):
            if n.get(key) is not None and not isinstance(n[key], dict):
                raise HTTPException(422, f"Node '{n['id'][:100]}': '{key}' must be an object")
    if edges is None:
        return
    if not isinstance(edges, list):
        raise HTTPException(422, "'edges' must be a list")
    for i, e in enumerate(edges):
        if not isinstance(e, dict) or not isinstance(e.get("source"), str) or not isinstance(e.get("target"), str):
            raise HTTPException(422, f"Edge {i + 1} must be an object with string 'source' and 'target'")
        missing = next((x for x in (e["source"], e["target"]) if x not in seen), None)
        if missing is not None:
            raise HTTPException(422, f"Edge {i + 1} connects a node that doesn't exist ('{missing[:100]}')")


def _uuid(v: str | None) -> uuid.UUID | None:
    if not v:
        return None
    try:
        return uuid.UUID(v)
    except ValueError as e:
        raise HTTPException(400, f"Invalid id '{v}'") from e


async def _folder(session: AsyncSession, folder_id: str | None, owner_id: uuid.UUID | None) -> uuid.UUID | None:
    """A workflow can only sit in a folder of the same owner."""
    fid = _uuid(folder_id)
    if fid:
        f = await session.get(Folder, fid)
        if not f:
            raise HTTPException(404, "Folder not found")
        if f.owner_id != owner_id:
            raise HTTPException(400, "That folder belongs to another user's workspace")
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
    from ..nodes.code_runtimes import runtimes_status

    return await asyncio.to_thread(runtimes_status)


_LIST_COLUMNS = (Workflow.id, Workflow.name, Workflow.description, Workflow.active, Workflow.owner_id, Workflow.tags,
                 Workflow.created_at, Workflow.updated_at, Workflow.folder_id, Workflow.published_version_id,
                 Workflow.published_version_number, Workflow.published_hash, Workflow.draft_hash, Workflow.revision)
# Counted by Postgres, so the list never ships graphs (nodes / edges / published copies) to Python.
_NODE_COUNT = func.jsonb_array_length(Workflow.nodes)
_TRIGGER_TYPES = literal_column(
    "ARRAY(SELECT DISTINCT e->>'type' FROM jsonb_array_elements(workflows.nodes) AS e WHERE e->>'type' LIKE 'trigger.%')")


def _json_default(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)  # UUID


@router.get("/workflows")
async def list_workflows(session: AsyncSession = Depends(get_session), p: Principal = Depends(principal)):
    # Plain rows (no ORM objects) of only the columns the list shows, encoded directly: with hundreds of workflows
    # the ORM identity map and FastAPI's generic encoder cost more than the query.
    q = (select(*_LIST_COLUMNS, _NODE_COUNT.label("node_count"), _TRIGGER_TYPES.label("trigger_types"))
         .where(*owned(p, Workflow.owner_id)).order_by(desc(Workflow.updated_at)))
    rows = (await session.execute(q)).all()
    hashes: dict = {}
    stale = [r.id for r in rows if r.draft_hash is None]
    if stale:  # written outside the ORM (no draft_hash yet): hash once and store
        graphs = (await session.execute(select(Workflow.id, Workflow.nodes, Workflow.edges).where(Workflow.id.in_(stale)))).all()
        hashes = {wid: graph_hash(n or [], e or []) for wid, n, e in graphs}
        await session.execute(update(Workflow), [{"id": wid, "draft_hash": h} for wid, h in hashes.items()])
        await session.commit()
    names = await owner_names(session, [r.owner_id for r in rows])
    out = []
    for r in rows:
        if r.id in hashes:
            r = SimpleNamespace(**{**r._asdict(), "draft_hash": hashes[r.id]})
        out.append(serialize(r, full=False, owner_name=names.get(r.owner_id), node_count=r.node_count or 0,
                             trigger_types=r.trigger_types or []))
    return Response(json.dumps(out, default=_json_default, separators=(",", ":")), media_type="application/json")


@router.post("/workflows/move")
async def move_workflows(body: IdsIn, session: AsyncSession = Depends(get_session), p: Principal = Depends(principal)):
    ids = [_uuid(i) for i in body.ids]
    q = select(Workflow).where(Workflow.id.in_(ids), *owned(p, Workflow.owner_id, write=True))
    rows = (await session.execute(q)).scalars().all()
    for wf in rows:
        wf.folder_id = await _folder(session, body.folder_id, wf.owner_id)
    await session.commit()
    return {"ok": True, "moved": len(rows)}


@router.post("/workflows/delete")
async def delete_workflows(body: IdsIn, session: AsyncSession = Depends(get_session), p: Principal = Depends(principal)):
    q = select(Workflow).where(Workflow.id.in_([_uuid(i) for i in body.ids]), *owned(p, Workflow.owner_id, write=True))
    rows = (await session.execute(q)).scalars().all()
    ids = [str(wf.id) for wf in rows]
    for wf in rows:
        await session.delete(wf)
    await session.commit()
    for i in ids:
        cluster.workflow_changed(i)
    return {"ok": True, "deleted": len(rows)}


@router.get("/workflows/export")
async def export_workflows(ids: str | None = None, folder_id: str | None = None, session: AsyncSession = Depends(get_session),
                           p: Principal = Depends(principal)):
    """Bulk export: comma separated ids, or every workflow in a folder (and its subfolders)."""
    q = select(Workflow).where(*owned(p, Workflow.owner_id)).order_by(Workflow.name)
    if ids:
        q = q.where(Workflow.id.in_([_uuid(i) for i in ids.split(",") if i]))
    elif folder_id:
        from .folders import _subtree

        q = q.where(Workflow.folder_id.in_(await _subtree(session, _uuid(folder_id), p.user.id)))
    rows = (await session.execute(q)).scalars().all()
    return {"workflows": [export_dict(w) for w in rows]}


async def _fetch_json(url: str) -> Any:
    """A workflow file from a URL: only public http(s) destinations (redirects re-checked), at most the request
    body limit in size."""
    limit = get_settings().max_request_body_mb * 2**20
    try:
        async with guarded_client(timeout=20, follow_redirects=True) as c:
            async with c.stream("GET", url) as r:
                r.raise_for_status()
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) > limit:
                        raise HTTPException(413, f"The file at that URL is larger than {get_settings().max_request_body_mb} MB")
        return json.loads(bytes(buf))
    except (httpx.HTTPError, ValueError) as e:  # BlockedURL is a ValueError
        raise HTTPException(400, f"Could not load workflow JSON from URL: {e}") from e


@router.post("/workflows/import")
async def import_workflows(body: ImportIn, session: AsyncSession = Depends(get_session), p: Principal = Depends(principal)):
    data = body.data
    if body.url:
        data = await _fetch_json(body.url)
    if isinstance(data, dict) and isinstance(data.get("workflows"), list):
        data = data["workflows"]
    items = data if isinstance(data, list) else [data]
    if not items or not all(isinstance(w, dict) and isinstance(w.get("nodes"), list) for w in items):
        raise HTTPException(400, "Not a DebBuilder workflow file (expected an object with 'nodes' and 'edges')")
    if any("connections" in w and "edges" not in w for w in items):
        raise HTTPException(400, "This looks like an n8n export. n8n nodes aren't compatible; rebuild it with DebBuilder nodes.")
    for w in items:
        _validate(w["nodes"], w.get("edges") or [])
        for key, typ, what in (("settings", dict, "an object"), ("tags", list, "a list"), ("description", str, "text")):
            if w.get(key) is not None and not isinstance(w[key], typ):
                raise HTTPException(422, f"'{key}' must be {what}")
    fid = await _folder(session, body.folder_id, p.user.id)
    created = []
    for w in items:
        wf = Workflow(name=str(w.get("name") or "").strip()[:200] or "Imported workflow", description=w.get("description") or "",
                      nodes=w["nodes"], edges=w.get("edges") or [], settings=w.get("settings") or {},
                      tags=[str(t)[:100] for t in w.get("tags") or []], folder_id=fid, owner_id=p.user.id)
        session.add(wf)
        created.append(wf)
    await session.commit()
    for wf in created:
        await session.refresh(wf)
    return [serialize(wf, full=False) for wf in created]


@router.post("/workflows")
async def create_workflow(body: WorkflowIn, session: AsyncSession = Depends(get_session), p: Principal = Depends(principal)):
    nodes = body.nodes or []
    _validate(nodes, body.edges or [])
    wf = Workflow(name=body.name or "My workflow", description=body.description or "", nodes=nodes,
                  edges=body.edges or [], settings=body.settings or {}, tags=body.tags or [],
                  folder_id=await _folder(session, body.folder_id, p.user.id), owner_id=p.user.id)
    session.add(wf)
    await session.commit()
    await session.refresh(wf)
    return await _serialize_one(session, wf)


@router.get("/workflows/{wf_id}")
async def get_workflow(wf_id: str, session: AsyncSession = Depends(get_session), p: Principal = Depends(principal)):
    return await _serialize_one(session, await _get(session, wf_id, p))


@router.put("/workflows/{wf_id}")
async def update_workflow(wf_id: str, body: WorkflowIn, session: AsyncSession = Depends(get_session),
                          p: Principal = Depends(principal)):
    wf = await _get(session, wf_id, p, write=True, lock=True)
    if body.revision is not None and body.revision != (wf.revision or 0):
        raise HTTPException(409, {"code": "conflict", "message": "This workflow was changed in another tab or by another session.",
                                  "workflow": jsonable_encoder(await _serialize_one(session, wf))})
    data = body.model_dump(exclude_none=True)
    data.pop("revision", None)
    if "folder_id" in body.model_fields_set:  # explicit null = move to the top level
        data["folder_id"] = await _folder(session, body.folder_id, wf.owner_id)
    if "nodes" in data or "edges" in data:
        _validate(data.get("nodes", wf.nodes or []), data.get("edges", wf.edges or []))
    # What production depends on: only a change here needs triggers re-synced and webhook caches dropped.
    # (A draft autosave doesn't: production runs the published version.)
    before = (wf.active, wf.published_version_id, wf.name, wf.settings if wf.active else None)
    for k, v in data.items():
        setattr(wf, k, v)
    if data.get("active") and not before[0]:
        if not wf.published_version_id:
            # Activating a never-published workflow publishes its draft (production needs a version to run).
            _publish(wf, await _snapshot_draft(session, wf, p))
        await _check_webhook_paths(session, wf)
    wf.revision = (wf.revision or 0) + 1
    await session.commit()
    await session.refresh(wf)
    if (wf.active, wf.published_version_id, wf.name, wf.settings if wf.active else None) != before:
        cluster.workflow_changed(str(wf.id))  # triggers re-sync on the leader, webhook caches everywhere
    return await _serialize_one(session, wf)


class PublishIn(BaseModel):
    note: str | None = None
    activate: bool = True


@router.post("/workflows/{wf_id}/publish")
async def publish_workflow(wf_id: str, body: PublishIn | None = None, session: AsyncSession = Depends(get_session),
                           p: Principal = Depends(principal)):
    """Publish the draft: it becomes a new version and what production (triggers, webhooks, schedules) runs."""
    body = body or PublishIn()
    wf = await _get(session, wf_id, p, write=True, lock=True)
    if not wf.nodes:
        raise HTTPException(400, "Nothing to publish: the workflow has no nodes")
    _publish(wf, await _snapshot_draft(session, wf, p, body.note))
    if body.activate:
        wf.active = True
    if wf.active:
        await _check_webhook_paths(session, wf)
    wf.revision = (wf.revision or 0) + 1
    await session.commit()
    await session.refresh(wf)
    cluster.workflow_changed(str(wf.id))
    return await _serialize_one(session, wf)


@router.get("/workflows/{wf_id}/versions")
async def list_versions(wf_id: str, session: AsyncSession = Depends(get_session), p: Principal = Depends(principal)):
    wf = await _get(session, wf_id, p)
    rows = (await session.execute(
        select(WorkflowVersion, User.name).outerjoin(User, User.id == WorkflowVersion.created_by)
        .where(WorkflowVersion.workflow_id == wf.id).order_by(desc(WorkflowVersion.number)).limit(200))).all()
    return [_version_dict(v, author, wf.published_version_id) for v, author in rows]


async def _version(session: AsyncSession, wf: Workflow, version_id: str) -> WorkflowVersion:
    v = await session.get(WorkflowVersion, _uuid(version_id))
    if not v or v.workflow_id != wf.id:
        raise HTTPException(404, "Version not found")
    return v


@router.get("/workflows/{wf_id}/versions/{version_id}")
async def get_version(wf_id: str, version_id: str, session: AsyncSession = Depends(get_session),
                      p: Principal = Depends(principal)):
    wf = await _get(session, wf_id, p)
    return _version_dict(await _version(session, wf, version_id), published_id=wf.published_version_id, full=True)


@router.post("/workflows/{wf_id}/versions/{version_id}/publish")
async def publish_version(wf_id: str, version_id: str, session: AsyncSession = Depends(get_session),
                          p: Principal = Depends(principal)):
    """Roll production back (or forward) to this version. The draft is left as it is."""
    wf = await _get(session, wf_id, p, write=True, lock=True)
    _publish(wf, await _version(session, wf, version_id))
    if wf.active:
        await _check_webhook_paths(session, wf)
    wf.revision = (wf.revision or 0) + 1
    await session.commit()
    await session.refresh(wf)
    cluster.workflow_changed(str(wf.id))
    return await _serialize_one(session, wf)


@router.post("/workflows/{wf_id}/versions/{version_id}/restore")
async def restore_version(wf_id: str, version_id: str, session: AsyncSession = Depends(get_session),
                          p: Principal = Depends(principal)):
    """Copy this version into the draft (production is unchanged until it is published)."""
    wf = await _get(session, wf_id, p, write=True, lock=True)
    v = await _version(session, wf, version_id)
    wf.nodes, wf.edges = v.nodes or [], v.edges or []
    wf.revision = (wf.revision or 0) + 1
    await session.commit()
    await session.refresh(wf)
    return await _serialize_one(session, wf)


@router.delete("/workflows/{wf_id}")
async def delete_workflow(wf_id: str, session: AsyncSession = Depends(get_session), p: Principal = Depends(principal)):
    wf = await _get(session, wf_id, p, write=True)
    await session.delete(wf)
    await session.commit()
    cluster.workflow_changed(wf_id)
    return {"ok": True}


@router.get("/workflows/{wf_id}/listeners")
async def listener_status(wf_id: str, session: AsyncSession = Depends(get_session), p: Principal = Depends(principal)):
    """Live state of this workflow's listener triggers (Kafka, Redis, …) and voice workers, keyed by node id."""
    await _get(session, wf_id, p)
    return {**listeners.status(wf_id), **voice_workers.status(wf_id)}


@router.post("/workflows/{wf_id}/voice-worker/{node_id}/restart")
async def restart_voice_worker(wf_id: str, node_id: str, session: AsyncSession = Depends(get_session),
                               p: Principal = Depends(principal)):
    await _get(session, wf_id, p, write=True)
    if not await voice_workers.restart(wf_id, node_id):
        raise HTTPException(409, "No voice worker for this trigger: activate the workflow first")
    return voice_workers.status(wf_id).get(node_id)


@router.get("/workflows/{wf_id}/export")
async def export_workflow(wf_id: str, session: AsyncSession = Depends(get_session), p: Principal = Depends(principal)):
    return export_dict(await _get(session, wf_id, p))


@router.post("/workflows/{wf_id}/duplicate")
async def duplicate_workflow(wf_id: str, body: DuplicateIn | None = None, session: AsyncSession = Depends(get_session),
                             p: Principal = Depends(principal)):
    """The copy belongs to the caller. Credentials stay with their owner, so a copy of someone
    else's workflow needs the caller's own credentials picked before it runs."""
    src = await _get(session, wf_id, p)
    body = body or DuplicateIn()
    if "folder_id" in body.model_fields_set:
        folder = await _folder(session, body.folder_id, p.user.id)
    else:
        folder = src.folder_id if src.owner_id == p.user.id else None
    wf = Workflow(name=(body.name or f"{src.name} (copy)")[:200], description=src.description, nodes=src.nodes,
                  edges=src.edges, settings=src.settings, tags=src.tags, folder_id=folder, owner_id=p.user.id)
    session.add(wf)
    await session.commit()
    await session.refresh(wf)
    return await _serialize_one(session, wf)


class PreviewIn(BaseModel):
    value: Any
    items: list[dict] = []          # the node's input items (latest run in the editor)
    item_index: int = 0
    node_outputs: dict[str, list[dict]] = {}  # earlier nodes' output by name, for $('Name')
    node_names: list[str] = []  # every node in the editor (also unsaved ones), to tell "not run yet" from "no such node"
    state: dict | None = None  # graph state from the latest run in the editor, for $state.key (None = never ran)


@router.post("/workflows/{wf_id}/expression-preview")
async def expression_preview(wf_id: str, body: PreviewIn, session: AsyncSession = Depends(get_session),
                             p: Principal = Depends(principal)):
    """Resolve a parameter's expression against sample data, like n8n's inline result under a field."""
    from ..engine.expressions import build_context, evaluate, inspect

    wf = await _get(session, wf_id, p)
    items = body.items or []
    idx = min(max(body.item_index, 0), max(len(items) - 1, 0))
    variables = {v["key"]: v.get("value") for v in (wf.settings or {}).get("variables", []) if v.get("key")}
    # $state: the latest run's state, or the declared defaults (Settings → State) before the first run.
    state = body.state
    if state is None:
        state = {}
        for row in (wf.settings or {}).get("state") or []:
            try:
                state[row["key"]] = json.loads(row.get("default") or "null")
            except (ValueError, KeyError, TypeError):
                pass
    ctx = build_context(items[idx] if items else {}, items, idx, body.node_outputs,
                        {"workflow_id": str(wf.id), "workflow_name": wf.name, "mode": "preview", "vars": variables}, state)
    # Each {{ }} on its own (valid / pending / invalid + field warnings), like n8n's inline expression editor.
    names = {n.get("name") for n in (wf.nodes or []) if n.get("name")} | set(body.node_outputs) | set(body.node_names)

    def run() -> tuple[list, Any, Exception | None]:  # CPU work (big inputs take seconds): off the event loop
        segs = inspect(body.value, ctx, names, body.node_outputs) if isinstance(body.value, str) else []
        try:
            return segs, evaluate(body.value, ctx), None
        except Exception as exc:  # noqa: BLE001
            return segs, None, exc
    segments, result, e = await asyncio.to_thread(run)
    if e is not None:  # show the author what went wrong, like n8n's red hint
        pending = next((sg for sg in segments if sg["state"] == "pending"), None)
        if pending:  # a node that hasn't run yet: not an error, the value just isn't known
            return {"ok": False, "pending": True, "error": pending["error"], "segments": segments}
        if body.state is None and "$state" in str(body.value):  # state is filled in by a run
            return {"ok": False, "pending": True, "segments": segments,
                    "error": "$state gets its values when the workflow runs: execute it once to preview this"}
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:500], "segments": segments}
    text = result if isinstance(result, str) else json.dumps(result, default=str, ensure_ascii=False)
    return {"ok": True, "type": type(result).__name__, "text": text[:5000], "truncated": len(text) > 5000,
            "segments": segments}


class CodeCheckIn(BaseModel):
    language: str = "python"
    code: str = ""


@router.post("/workflows/{wf_id}/code-check")
async def code_check(wf_id: str, body: CodeCheckIn, session: AsyncSession = Depends(get_session),
                     p: Principal = Depends(principal)):
    """Syntax check for the Code node while typing (n8n lints its code editor). Compiles only, never runs."""
    import textwrap

    await _get(session, wf_id, p)
    if body.language != "python":
        return {"ok": True, "checked": False}
    # Same wrapping as the Code node (core.py): the code is the body of an async function, so `return` works.
    src = "async def __user_fn(items, item, index):\n" + textwrap.indent(body.code or "pass", "    ")
    try:
        await asyncio.to_thread(compile, src, "<code>", "exec", dont_inherit=True)  # CPU-bound for long code
    except SyntaxError as e:
        line = max((e.lineno or 2) - 1, 1)
        return {"ok": False, "checked": True, "line": line, "column": max((e.offset or 5) - 4, 1),
                "message": f"{e.msg} (line {line})"}
    return {"ok": True, "checked": True}


@router.post("/workflows/{wf_id}/run")
async def run_workflow(wf_id: str, body: RunIn, session: AsyncSession = Depends(get_session), p: Principal = Depends(principal)):
    wf = await _get(session, wf_id, p, write=True)
    graph = workflow_to_dict(wf)
    if body.workflow:
        _validate(body.workflow.get("nodes", []), body.workflow.get("edges"))
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
async def create_from_template(template_id: str, session: AsyncSession = Depends(get_session), p: Principal = Depends(principal)):
    t = TEMPLATES.get(template_id)
    if not t:
        raise HTTPException(404, "Template not found")
    wf = Workflow(name=t["name"], description=t["description"], nodes=t["nodes"], edges=t["edges"],
                  settings=t.get("settings", {}), tags=t.get("tags", []), owner_id=p.user.id)
    session.add(wf)
    await session.commit()
    await session.refresh(wf)
    return await _serialize_one(session, wf)
