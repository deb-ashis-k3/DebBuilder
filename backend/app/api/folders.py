"""Workflow folders (n8n-style): nested, with move / rename / delete-or-transfer."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select, text, update

from .. import cluster
from ..access import check, owned, owner_names, principal
from ..db import AsyncSession, Folder, Workflow, get_session
from ..security import Principal

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


def serialize(f: Folder, count: int = 0, owner_name: str | None = None) -> dict:
    return {"id": str(f.id), "name": f.name, "parent_id": str(f.parent_id) if f.parent_id else None,
            "owner_id": str(f.owner_id) if f.owner_id else None, "owner_name": owner_name,
            "workflow_count": count, "created_at": f.created_at, "updated_at": f.updated_at}


async def _get(session: AsyncSession, folder_id: str, p: Principal, write: bool = False) -> Folder:
    f = await session.get(Folder, _uuid(folder_id))
    if not f:
        raise HTTPException(404, "Folder not found")
    check(p, f.owner_id, "Folder", write)
    return f


async def _subtree(session: AsyncSession, root: uuid.UUID, owner_id: uuid.UUID | None) -> list[uuid.UUID]:
    """The folder and all its descendants, within one owner's workspace. UNION (not UNION ALL) drops rows already
    seen, so even a cycle in the data ends the recursion."""
    rows = await session.execute(text(
        "WITH RECURSIVE t(id) AS (SELECT id FROM folders WHERE id = :root AND owner_id = :owner "
        "UNION SELECT f.id FROM folders f JOIN t ON f.parent_id = t.id WHERE f.owner_id = :owner) SELECT id FROM t"),
        {"root": root, "owner": owner_id})
    return list(rows.scalars())


async def _lock_tree(session: AsyncSession, owner_id: uuid.UUID | None) -> None:
    """Serialize folder moves within a workspace (until commit): two concurrent moves (A into B, B into A) would
    each pass the cycle check against the other's old state."""
    key = (owner_id.int if owner_id else 0) & 0x7FFF_FFFF_FFFF_FFFF
    await session.execute(select(func.pg_advisory_xact_lock(key)))


async def _check_parent(session: AsyncSession, parent: uuid.UUID | None, owner_id: uuid.UUID | None,
                        moving: uuid.UUID | None = None) -> None:
    """Folders nest only inside the same owner's tree."""
    if parent is None:
        return
    pf = await session.get(Folder, parent)
    if not pf:
        raise HTTPException(404, "Parent folder not found")
    if pf.owner_id != owner_id:
        raise HTTPException(400, "That folder belongs to another user's workspace")
    if moving and parent in await _subtree(session, moving, owner_id):
        raise HTTPException(400, "A folder can't be moved into itself or one of its subfolders")


@router.get("")
async def list_folders(session: AsyncSession = Depends(get_session), p: Principal = Depends(principal)):
    counts = dict((await session.execute(
        select(Workflow.folder_id, func.count()).where(Workflow.folder_id.is_not(None)).group_by(Workflow.folder_id))).all())
    rows = (await session.execute(select(Folder).where(*owned(p, Folder.owner_id)).order_by(Folder.name))).scalars().all()
    names = await owner_names(session, [f.owner_id for f in rows])
    return [serialize(f, counts.get(f.id, 0), names.get(f.owner_id)) for f in rows]


@router.post("")
async def create_folder(body: FolderIn, session: AsyncSession = Depends(get_session), p: Principal = Depends(principal)):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "Folder name is required")
    parent = _uuid(body.parent_id)
    await _check_parent(session, parent, p.user.id)
    f = Folder(name=name[:200], parent_id=parent, owner_id=p.user.id)
    session.add(f)
    await session.commit()
    await session.refresh(f)
    return serialize(f)


@router.put("/{folder_id}")
async def update_folder(folder_id: str, body: FolderIn, session: AsyncSession = Depends(get_session),
                        p: Principal = Depends(principal)):
    f = await _get(session, folder_id, p, write=True)
    if body.name is not None:
        if not body.name.strip():
            raise HTTPException(400, "Folder name is required")
        f.name = body.name.strip()[:200]
    if "parent_id" in body.model_fields_set:
        parent = _uuid(body.parent_id)
        await _lock_tree(session, f.owner_id)
        await _check_parent(session, parent, f.owner_id, moving=f.id)
        f.parent_id = parent
    await session.commit()
    await session.refresh(f)
    return serialize(f)


@router.delete("/{folder_id}")
async def delete_folder(folder_id: str, mode: str = "move", session: AsyncSession = Depends(get_session),
                        p: Principal = Depends(principal)):
    """mode=move: workflows and subfolders go to the parent folder.
    mode=delete: everything inside (workflows included) is deleted."""
    f = await _get(session, folder_id, p, write=True)
    if mode == "move":
        await session.execute(update(Workflow).where(Workflow.folder_id == f.id).values(folder_id=f.parent_id))
        await session.execute(update(Folder).where(Folder.parent_id == f.id).values(parent_id=f.parent_id))
    elif mode == "delete":
        ids = await _subtree(session, f.id, f.owner_id)
        doomed = (await session.execute(select(Workflow).where(Workflow.folder_id.in_(ids)))).scalars().all()
        for wf in doomed:
            await session.delete(wf)
    else:
        raise HTTPException(400, "mode must be 'move' or 'delete'")
    gone = [str(wf.id) for wf in doomed] if mode == "delete" else []
    await session.delete(f)
    await session.commit()
    for i in gone:
        cluster.workflow_changed(i)
    return {"ok": True}
