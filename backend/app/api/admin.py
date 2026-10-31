"""User management (admins / owner) and the audit log."""

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import delete, desc, func, select, update

from ..db import AuditLog, Credential, Folder, SessionLocal, User, Workflow
from ..security import (ROLES, audit, check_password_policy, clean_username, current, hash_password_async, revoke_sessions, role_rank,
                        temp_password)
from .auth import ensure_username_free, user_json
from .. import cluster

router = APIRouter(prefix="/api", tags=["admin"])


def _uid(v: str) -> uuid.UUID:
    try:
        return uuid.UUID(v)
    except ValueError as e:
        raise HTTPException(404, "User not found") from e


def _can_manage(actor_role: str, target_role: str) -> bool:
    """Admins manage editors/viewers/admins but never owners; owners manage everyone."""
    return actor_role == "owner" or (role_rank(target_role) < role_rank("owner"))


async def _owner_count(s) -> int:
    return (await s.execute(select(func.count(User.id)).where(User.role == "owner", User.disabled.is_(False)))).scalar_one()


@router.get("/users")
async def list_users():
    async with SessionLocal() as s:
        rows = (await s.execute(select(User).order_by(User.created_at))).scalars().all()
        counts = dict((await s.execute(select(Workflow.owner_id, func.count()).group_by(Workflow.owner_id))).all())
    return [{**user_json(u), "workflow_count": counts.get(u.id, 0)} for u in rows]


class NewUser(BaseModel):
    email: str
    name: str = ""
    username: str = ""
    role: str = "editor"
    password: str | None = None


@router.post("/users")
async def create_user(body: NewUser, request: Request):
    p = current(request)
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(400, "Enter a valid email address")
    username = clean_username(body.username)
    if body.role not in ROLES:
        raise HTTPException(400, f"Role must be one of {', '.join(ROLES)}")
    if not _can_manage(p.role, body.role):
        raise HTTPException(403, "Only the owner can create owners")
    # Without a password we generate a strong temporary one that must be changed at first sign-in.
    pw = body.password or temp_password()
    if body.password:
        check_password_policy(pw, email, body.name)
    async with SessionLocal() as s:
        if (await s.execute(select(User).where(User.email == email))).scalar_one_or_none():
            raise HTTPException(409, "A user with that email already exists")
        await ensure_username_free(s, username)
        u = User(email=email, username=username, name=body.name.strip()[:200], role=body.role, password_hash=await hash_password_async(pw),
                 must_change_password=True)
        s.add(u)
        await s.commit()
        await s.refresh(u)
    await audit("user.created", request, p, status=200, target=email, role=body.role)
    return {"user": user_json(u), "temporary_password": None if body.password else pw}


class UserPatch(BaseModel):
    name: str | None = None
    username: str | None = None  # "" removes it
    role: str | None = None
    disabled: bool | None = None


@router.put("/users/{user_id}")
async def update_user(user_id: str, body: UserPatch, request: Request):
    p = current(request)
    async with SessionLocal() as s:
        u = await s.get(User, _uid(user_id))
        if not u:
            raise HTTPException(404, "User not found")
        if not _can_manage(p.role, u.role):
            raise HTTPException(403, "Only the owner can change an owner")
        if u.id == p.user.id and (body.role or body.disabled):
            raise HTTPException(400, "You can't change your own role or disable yourself")
        if body.role is not None:
            if body.role not in ROLES:
                raise HTTPException(400, "Unknown role")
            if not _can_manage(p.role, body.role):
                raise HTTPException(403, "Only the owner can make someone an owner")
            if u.role == "owner" and body.role != "owner" and await _owner_count(s) <= 1:
                raise HTTPException(400, "There must always be at least one owner")
            u.role = body.role
        if body.disabled is not None:
            if u.role == "owner" and body.disabled and await _owner_count(s) <= 1:
                raise HTTPException(400, "The last owner can't be disabled")
            u.disabled = body.disabled
        if body.name is not None:
            u.name = body.name.strip()[:200]
        if body.username is not None:
            username = clean_username(body.username)
            await ensure_username_free(s, username, u.id)  # before assigning, or autoflush hits the unique index
            u.username = username
        await s.commit()
        await s.refresh(u)
    if body.disabled or body.role:
        await revoke_sessions(u.id)  # new rights apply immediately
    await audit("user.updated", request, p, status=200, target=u.email, **body.model_dump(exclude_none=True))
    return {"user": user_json(u)}


@router.post("/users/{user_id}/reset-password")
async def reset_password(user_id: str, request: Request):
    p = current(request)
    pw = temp_password()
    async with SessionLocal() as s:
        u = await s.get(User, _uid(user_id))
        if not u:
            raise HTTPException(404, "User not found")
        if not _can_manage(p.role, u.role):
            raise HTTPException(403, "Only the owner can reset an owner's password")
        u.password_hash, u.must_change_password, u.failed_logins, u.locked_until = await hash_password_async(pw), True, 0, None
        await s.commit()
    await revoke_sessions(u.id)
    await audit("user.password_reset", request, p, status=200, target=u.email)
    return {"temporary_password": pw}


@router.post("/users/{user_id}/reset-mfa")
async def reset_mfa(user_id: str, request: Request):
    p = current(request)
    async with SessionLocal() as s:
        u = await s.get(User, _uid(user_id))
        if not u:
            raise HTTPException(404, "User not found")
        if not _can_manage(p.role, u.role) or u.id == p.user.id:
            raise HTTPException(403, "Not allowed")
        u.mfa_enabled, u.mfa_secret, u.recovery_codes = False, None, []
        await s.commit()
    await revoke_sessions(u.id)
    await audit("user.mfa_reset", request, p, status=200, target=u.email)
    return {"ok": True}


@router.post("/users/{user_id}/revoke-sessions")
async def revoke_user_sessions(user_id: str, request: Request):
    p = current(request)
    async with SessionLocal() as s:
        u = await s.get(User, _uid(user_id))
        if not u or not _can_manage(p.role, u.role):
            raise HTTPException(404, "User not found")
    await revoke_sessions(u.id)
    await audit("user.sessions_revoked", request, p, status=200, target=u.email)
    return {"ok": True}


@router.delete("/users/{user_id}")
async def delete_user(user_id: str, request: Request, transfer_to: str | None = None):
    """Delete an account. Every user's workspace is private (nobody else, admins included, can see it), so by
    default their workflows (with executions and versions), folders and credentials are deleted with them.
    `?transfer_to=<user id>` hands the whole workspace to that user instead, explicitly."""
    p = current(request)
    async with SessionLocal() as s:
        u = await s.get(User, _uid(user_id))
        if not u:
            raise HTTPException(404, "User not found")
        if u.id == p.user.id:
            raise HTTPException(400, "You can't delete yourself")
        if not _can_manage(p.role, u.role):
            raise HTTPException(403, "Only the owner can delete an owner")
        if u.role == "owner" and await _owner_count(s) <= 1:
            raise HTTPException(400, "The last owner can't be deleted")
        heir = None
        if transfer_to:
            try:
                heir = await s.get(User, uuid.UUID(transfer_to))
            except ValueError:
                heir = None
            if not heir or heir.id == u.id:
                raise HTTPException(400, "transfer_to must be another existing user")
            if heir.disabled:
                raise HTTPException(400, "Can't transfer a workspace to a disabled user")
        email = u.email
        counts = {}
        for model in (Workflow, Folder, Credential):
            if heir:
                res = await s.execute(update(model).where(model.owner_id == u.id).values(owner_id=heir.id))
            else:  # executions, versions, stats and static data go with their workflow (ON DELETE CASCADE)
                res = await s.execute(delete(model).where(model.owner_id == u.id))
            counts[model.__tablename__] = res.rowcount
        await s.delete(u)
        await s.commit()
    # Every replica drops cached webhook routes and credentials; the leader reloads triggers, schedules and
    # listeners from the database, so deleted workflows stop and transferred ones run under their new owner.
    cluster.users_changed()
    if heir:
        await audit("user.deleted", request, p, status=200, target=email, transferred_to=heir.email, **counts)
        return {"ok": True, "transferred_to": str(heir.id), "transferred": counts}
    await audit("user.deleted", request, p, status=200, target=email, data="deleted", **counts)
    return {"ok": True, "deleted": counts}


@router.get("/audit")
async def audit_log(user: str | None = None, action: str | None = None, since: datetime | None = None,
                    limit: int = Query(100, ge=1), offset: int = Query(0, ge=0)):
    q = select(AuditLog)
    cq = select(func.count(AuditLog.id))
    conds = []
    if user:
        conds.append(AuditLog.user_email.ilike(f"%{user}%"))
    if action:
        conds.append(AuditLog.action.ilike(f"{action}%"))
    if since:
        conds.append(AuditLog.at >= since)
    async with SessionLocal() as s:
        rows = (await s.execute(q.where(*conds).order_by(desc(AuditLog.id)).limit(min(limit, 500)).offset(offset))).scalars().all()
        total = (await s.execute(cq.where(*conds))).scalar_one()
    return {"total": total, "items": [{"id": r.id, "at": r.at, "user_email": r.user_email, "action": r.action,
                                       "method": r.method, "path": r.path, "status": r.status, "ip": r.ip,
                                       "detail": r.detail} for r in rows]}
