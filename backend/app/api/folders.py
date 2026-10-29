"""Workflow folders (n8n-style): nested, with move / rename / delete-or-transfer."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select, update

from .. import listeners, scheduler
from ..db import AsyncSession, Folder, Workflow, get_session

router = APIRouter(prefix="/api/folders", tags=["folders"])


class FolderIn(BaseModel):
    name: str | None = None
    parent_id: str | None = None


def _uuid(v: str | None) -> uuid.UUID | None:
    if not v:
        return None
    try:
        return uuid.UUID(v)
    except ValueError as e:
        raise HTTPException(400, f"Invalid id '{v}'") from e


def serialize(f: Folder, count: int = 0) -> dict:
    return {"id": str(f.id), "name": f.name, "parent_id": str(f.parent_id) if f.parent_id else None,
            "workflow_count": count, "created_at": f.created_at, "updated_at": f.updated_at}


async def _get(session: AsyncSession, folder_id: str) -> Folder:
    f = await session.get(Folder, _uuid(folder_id))
    if not f:
        raise HTTPException(404, "Folder not found")
    return f


async def _subtree(session: AsyncSession, root: uuid.UUID) -> list[uuid.UUID]:
    """The folder and all its descendants."""
    rows = (await session.execute(select(Folder.id, Folder.parent_id))).all()
    children: dict[uuid.UUID | None, list[uuid.UUID]] = {}
    for fid, pid in rows:
        children.setdefault(pid, []).append(fid)
    out, stack = [], [root]
    while stack:
        cur = stack.pop()
        out.append(cur)
        stack.extend(children.get(cur, []))
    return out


async def _check_parent(session: AsyncSession, parent: uuid.UUID | None, moving: uuid.UUID | None = None) -> None:
    if parent is None:
        return
    if not await session.get(Folder, parent):
        raise HTTPException(404, "Parent folder not found")
    if moving and parent in await _subtree(session, moving):
        raise HTTPException(400, "A folder can't be moved into itself or one of its subfolders")


@router.get("")
async def list_folders(session: AsyncSession = Depends(get_session)):
    counts = dict((await session.execute(
        select(Workflow.folder_id, func.count()).where(Workflow.folder_id.is_not(None)).group_by(Workflow.folder_id))).all())
    rows = (await session.execute(select(Folder).order_by(Folder.name))).scalars().all()
    return [serialize(f, counts.get(f.id, 0)) for f in rows]


@router.post("")
async def create_folder(body: FolderIn, session: AsyncSession = Depends(get_session)):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "Folder name is required")
    parent = _uuid(body.parent_id)
    await _check_parent(session, parent)
    f = Folder(name=name[:200], parent_id=parent)
    session.add(f)
    await session.commit()
    await session.refresh(f)
    return serialize(f)


@router.put("/{folder_id}")
async def update_folder(folder_id: str, body: FolderIn, session: AsyncSession = Depends(get_session)):
    f = await _get(session, folder_id)
    if body.name is not None:
        if not body.name.strip():
            raise HTTPException(400, "Folder name is required")
        f.name = body.name.strip()[:200]
    if "parent_id" in body.model_fields_set:
        parent = _uuid(body.parent_id)
        await _check_parent(session, parent, moving=f.id)
        f.parent_id = parent
    await session.commit()
    await session.refresh(f)
    return serialize(f)


@router.delete("/{folder_id}")
async def delete_folder(folder_id: str, mode: str = "move", session: AsyncSession = Depends(get_session)):
    """mode=move: workflows and subfolders go to the parent folder.
    mode=delete: everything inside (workflows included) is deleted."""
    f = await _get(session, folder_id)
    if mode == "move":
        await session.execute(update(Workflow).where(Workflow.folder_id == f.id).values(folder_id=f.parent_id))
        await session.execute(update(Folder).where(Folder.parent_id == f.id).values(parent_id=f.parent_id))
    elif mode == "delete":
        ids = await _subtree(session, f.id)
        doomed = (await session.execute(select(Workflow).where(Workflow.folder_id.in_(ids)))).scalars().all()
        for wf in doomed:
            scheduler.remove_workflow(str(wf.id))
            listeners.remove_workflow(str(wf.id))
            await session.delete(wf)
    else:
        raise HTTPException(400, "mode must be 'move' or 'delete'")
    await session.delete(f)
    await session.commit()
    from .hooks import invalidate_webhooks

    invalidate_webhooks()
    return {"ok": True}
