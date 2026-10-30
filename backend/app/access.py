"""Per-user workspaces: who can see and change which workflows, folders and credentials.

- editor: only their own workflows, folders, credentials (and those workflows' executions).
- viewer: reads everything, owns nothing (route policy already blocks their writes).
- admin / owner: read and manage everyone's.

Someone else's resource answers 404 (not 403) so ids can't be probed. At run time a workflow
may only use its owner's credentials and call its owner's workflows (see credentials.load_credential
and nodes.core.run_subworkflow).
"""

import uuid

from fastapi import HTTPException, Request
from sqlalchemy import select

from .db import User, Workflow
from .security import Principal, current

_READ_ALL = {"viewer", "admin", "owner"}
_MANAGE_ALL = {"admin", "owner"}


def principal(request: Request) -> Principal:
    """FastAPI dependency: the signed-in caller (the middleware already authenticated them)."""
    return current(request)


def reads_all(p: Principal) -> bool:
    return p.role in _READ_ALL


def manages_all(p: Principal) -> bool:
    return p.role in _MANAGE_ALL


def can_read(p: Principal, owner_id: uuid.UUID | None) -> bool:
    return reads_all(p) or owner_id == p.user.id


def can_manage(p: Principal, owner_id: uuid.UUID | None) -> bool:
    return manages_all(p) or owner_id == p.user.id


def check(p: Principal, owner_id: uuid.UUID | None, what: str, write: bool = False) -> None:
    if not (can_manage if write else can_read)(p, owner_id):
        raise HTTPException(404, f"{what} not found")


def owned(p: Principal, column, write: bool = False) -> list:
    """WHERE conditions limiting a query to what the caller may see (or change)."""
    return [] if (manages_all(p) if write else reads_all(p)) else [column == p.user.id]


def visible_workflow_ids(p: Principal, write: bool = False):
    """Subquery of workflow ids the caller may see/change, for filtering executions."""
    return select(Workflow.id).where(*owned(p, Workflow.owner_id, write))


async def owner_names(session, ids) -> dict[uuid.UUID, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    rows = (await session.execute(select(User.id, User.name, User.email).where(User.id.in_(ids)))).all()
    return {uid: name or email for uid, name, email in rows}
