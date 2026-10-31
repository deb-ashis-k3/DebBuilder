import html
import secrets
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import desc, select

from .. import cluster
from ..credential_tests import test_credential
from ..credentials import (
    CREDENTIAL_TYPES, MASK, OAUTH_TOKEN_KEY, categories, decrypt, encrypt, mask, oauth2_authorize_url,
    oauth2_exchange_code, oauth2_redirect_uri, public_types, with_defaults,
)
from ..access import check, owned, owner_names, principal
from ..db import AsyncSession, Credential, SessionLocal, get_session
from ..model_lists import list_models
from ..security import Principal

router = APIRouter(prefix="/api/credentials", tags=["credentials"])
oauth_router = APIRouter(prefix="/api/oauth2", tags=["oauth2"])


class CredentialIn(BaseModel):
    name: str | None = None
    type: str | None = None
    data: dict | None = None


class TestIn(BaseModel):
    type: str
    data: dict = {}
    id: str | None = None  # merge stored secrets for masked fields


def serialize(c: Credential, with_data: bool = False, owner_name: str | None = None) -> dict:
    t = CREDENTIAL_TYPES.get(c.type, {})
    d = {"id": str(c.id), "name": c.name, "type": c.type, "created_at": c.created_at, "updated_at": c.updated_at,
         "owner_id": str(c.owner_id) if c.owner_id else None, "owner_name": owner_name,
         "type_label": t.get("label", c.type), "category": t.get("category"), "color": t.get("color"),
         "testable": bool(t.get("test")), "oauth2": bool(t.get("auth", {}).get("oauth2"))}
    if with_data or d["oauth2"]:
        data = mask(c.type, decrypt(c.data))
        if with_data:
            d["data"] = data
        d["oauth_connected"] = data.get("oauth_connected", False)
    return d


def _clean(cred_type: str, data: dict) -> dict:
    """Keep only declared fields (drops UI-only keys like oauth_connected)."""
    allowed = {f["name"] for f in CREDENTIAL_TYPES[cred_type]["fields"]}
    return {k: v for k, v in data.items() if k in allowed}


@router.get("/types")
async def types():
    return {"categories": categories(), "types": public_types(), "oauth_redirect_uri": oauth2_redirect_uri()}


@router.get("")
async def list_credentials(type: str | None = None, session: AsyncSession = Depends(get_session),
                           p: Principal = Depends(principal)):
    q = select(Credential).where(*owned(p, Credential.owner_id)).order_by(desc(Credential.updated_at))
    if type:
        q = q.where(Credential.type.in_(type.split(",")))
    rows = (await session.execute(q)).scalars().all()
    names = await owner_names(session, [c.owner_id for c in rows])
    return [serialize(c, owner_name=names.get(c.owner_id)) for c in rows]


@router.post("")
async def create_credential(body: CredentialIn, session: AsyncSession = Depends(get_session),
                            p: Principal = Depends(principal)):
    if body.type not in CREDENTIAL_TYPES:
        raise HTTPException(400, "Unknown credential type")
    c = Credential(name=body.name or CREDENTIAL_TYPES[body.type]["label"], type=body.type,
                   data=encrypt(_clean(body.type, body.data or {})), owner_id=p.user.id)
    session.add(c)
    await session.commit()
    await session.refresh(c)
    return serialize(c)


@router.post("/test")
async def test_unsaved(body: TestIn, p: Principal = Depends(principal)):
    """Test form values before/without saving. Masked secrets are taken from the stored credential,
    so only someone who may edit it can do that (the form could point its URL anywhere)."""
    if body.type not in CREDENTIAL_TYPES:
        raise HTTPException(400, "Unknown credential type")
    data = {k: v for k, v in body.data.items() if v != MASK}
    if body.id:
        async with SessionLocal() as s:
            stored = await _get(s, body.id, p, write=True)
            data = {**decrypt(stored.data), **data}
    return await test_credential(body.type, _clean(body.type, data) | ({OAUTH_TOKEN_KEY: data[OAUTH_TOKEN_KEY]} if OAUTH_TOKEN_KEY in data else {}), body.id)


async def _get(session: AsyncSession, cred_id: str, p: Principal, write: bool = False) -> Credential:
    try:
        c = await session.get(Credential, uuid.UUID(cred_id))
    except ValueError:
        c = None
    if not c:
        raise HTTPException(404, "Credential not found")
    check(p, c.owner_id, "Credential", write)
    return c


async def _serialize_one(session: AsyncSession, c: Credential, with_data: bool = False) -> dict:
    return serialize(c, with_data, (await owner_names(session, [c.owner_id])).get(c.owner_id))


@router.post("/{cred_id}/test")
async def test_saved(cred_id: str, session: AsyncSession = Depends(get_session), p: Principal = Depends(principal)):
    c = await _get(session, cred_id, p, write=True)
    return await test_credential(c.type, decrypt(c.data), cred_id)


@router.get("/{cred_id}/models")
async def credential_models(cred_id: str, refresh: bool = False, session: AsyncSession = Depends(get_session),
                            p: Principal = Depends(principal)):
    """Models this credential can use, for the chat model nodes' picker. Uses the stored key, so
    the same rule as testing applies: only someone who may edit the credential."""
    c = await _get(session, cred_id, p, write=True)
    try:
        models = await list_models(c.type, decrypt(c.data), cred_id, c.updated_at.isoformat(), refresh)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"models": models}


@router.get("/{cred_id}")
async def get_credential(cred_id: str, session: AsyncSession = Depends(get_session), p: Principal = Depends(principal)):
    return await _serialize_one(session, await _get(session, cred_id, p), with_data=True)


@router.put("/{cred_id}")
async def update_credential(cred_id: str, body: CredentialIn, session: AsyncSession = Depends(get_session),
                            p: Principal = Depends(principal)):
    c = await _get(session, cred_id, p, write=True)
    if body.name:
        c.name = body.name
    if body.data is not None:
        current = decrypt(c.data)
        # Masked secrets coming back from the form mean "unchanged".
        changed = {k: v for k, v in _clean(c.type, body.data).items() if v != MASK}
        oauth_keys = {"client_id", "client_secret", "token_url", "auth_url", "scope", "grant_type"}
        if OAUTH_TOKEN_KEY in current and any(current.get(k) != v for k, v in changed.items() if k in oauth_keys):
            current.pop(OAUTH_TOKEN_KEY)  # app settings changed → reconnect
        current.update(changed)
        c.data = encrypt(current)
    await session.commit()
    await session.refresh(c)
    cluster.credential_changed(cred_id)
    return await _serialize_one(session, c)


@router.delete("/{cred_id}")
async def delete_credential(cred_id: str, session: AsyncSession = Depends(get_session), p: Principal = Depends(principal)):
    await session.delete(await _get(session, cred_id, p, write=True))
    await session.commit()
    cluster.credential_changed(cred_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# OAuth2 "Connect my account"
# ---------------------------------------------------------------------------

_STATES: dict[str, tuple[str, float]] = {}


@oauth_router.get("/auth-url")
async def auth_url(credential_id: str, p: Principal = Depends(principal)):
    async with SessionLocal() as s:
        c = await _get(s, credential_id, p, write=True)
        if not CREDENTIAL_TYPES.get(c.type, {}).get("auth", {}).get("oauth2"):
            raise HTTPException(404, "OAuth2 credential not found")
        data = with_defaults(c.type, decrypt(c.data))
    if not data.get("auth_url") or not data.get("client_id"):
        raise HTTPException(400, "Set the Authorization URL and Client ID first")
    now = time.time()
    for k, (_, t) in list(_STATES.items()):
        if now - t > 900:
            _STATES.pop(k, None)
    state = secrets.token_urlsafe(24)
    _STATES[state] = (credential_id, now)
    return {"url": oauth2_authorize_url(data, state), "redirect_uri": oauth2_redirect_uri()}


def _popup(ok: bool, message: str) -> HTMLResponse:
    color = "#10b981" if ok else "#f43f5e"
    msg = html.escape(message)
    return HTMLResponse(f"""<!doctype html><html><head><meta charset=utf-8><title>DebBuilder</title></head>
<body style="font-family:system-ui;display:grid;place-items:center;height:100vh;margin:0;background:#0d1030;color:#fff">
<div style="text-align:center"><div style="font-size:42px;color:{color}">{'✓' if ok else '✕'}</div><p>{msg}</p>
<p style="opacity:.6;font-size:13px">You can close this window.</p></div>
<script>try{{window.opener&&window.opener.postMessage({{type:'debbuilder-oauth2',ok:{str(ok).lower()}}},'*')}}catch(e){{}};
{'setTimeout(()=>window.close(),1200)' if ok else ''}</script></body></html>""")


@oauth_router.get("/callback")
async def callback(state: str = "", code: str | None = None, error: str | None = None, error_description: str | None = None):
    entry = _STATES.pop(state, None)
    if not entry:
        return _popup(False, "Invalid or expired state. Start the connection again.")
    if error or not code:
        return _popup(False, f"Authorization failed: {error_description or error or 'no code returned'}")
    cred_id = entry[0]
    async with SessionLocal() as s:
        c = await s.get(Credential, uuid.UUID(cred_id))
        if not c:
            return _popup(False, "Credential no longer exists")
        stored = decrypt(c.data)
        try:
            token = await oauth2_exchange_code(with_defaults(c.type, stored), code)
        except Exception as e:  # noqa: BLE001
            return _popup(False, str(e))
        stored[OAUTH_TOKEN_KEY] = token
        c.data = encrypt(stored)
        await s.commit()
    return _popup(True, "Account connected")
