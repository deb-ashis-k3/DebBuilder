"""Credential storage (encrypted), request auth for any credential type, OAuth2."""

import base64
import json
import time
import uuid
from urllib.parse import parse_qsl, urlencode

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .credential_catalog import CATEGORY_ORDER, TYPES
from .db import Credential, SessionLocal

CREDENTIAL_TYPES = TYPES
LLM_CREDENTIAL_TYPES = ["openai", "anthropic", "google_genai", "ollama"]
MASK = "••••••••"
# Stored inside OAuth2 credentials; never shown in forms.
OAUTH_TOKEN_KEY = "oauth_token_data"


def _fernet() -> Fernet:
    return Fernet(get_settings().encryption_key.encode())


def encrypt(data: dict) -> str:
    return _fernet().encrypt(json.dumps(data).encode()).decode()


def decrypt(token: str) -> dict:
    return json.loads(_fernet().decrypt(token.encode()))


def mask(cred_type: str, data: dict) -> dict:
    secret = {f["name"] for f in CREDENTIAL_TYPES.get(cred_type, {}).get("fields", []) if f["secret"]}
    out = {k: (MASK if k in secret and v else v) for k, v in data.items() if k != OAUTH_TOKEN_KEY}
    if OAUTH_TOKEN_KEY in data:
        tok = data[OAUTH_TOKEN_KEY] or {}
        out["oauth_connected"] = bool(tok.get("access_token"))
        out["oauth_expires_at"] = tok.get("expires_at")
    return out


def public_types() -> dict:
    """Catalog for the UI (no test/auth internals beyond what the form needs)."""
    return {
        k: {
            "label": t["label"], "category": t["category"], "fields": t["fields"], "color": t["color"],
            "docs": t["docs"], "description": t["description"], "testable": bool(t.get("test")),
            "oauth2": bool(t["auth"].get("oauth2")),
        }
        for k, t in TYPES.items()
    }


def categories() -> list[str]:
    return CATEGORY_ORDER


def with_defaults(cred_type: str, data: dict) -> dict:
    out = {f["name"]: f["default"] for f in TYPES.get(cred_type, {}).get("fields", []) if f["default"] is not None}
    out.update({k: v for k, v in data.items() if v not in (None, "")})
    return out


class _Blank(dict):
    def __missing__(self, key):
        return ""


def fmt(template: str, data: dict) -> str:
    values = _Blank({k: (str(v).rstrip("/") if isinstance(v, str) and v.startswith("http") else v) for k, v in data.items()})
    return template.format_map(values)


async def load_credential(session: AsyncSession, cred_id: str | uuid.UUID | None) -> tuple[str, dict]:
    """Return (type, decrypted data with defaults) or raise ValueError."""
    if not cred_id:
        raise ValueError("No credential selected")
    cred = await session.get(Credential, uuid.UUID(str(cred_id)))
    if not cred:
        raise ValueError(f"Credential {cred_id} not found")
    return cred.type, with_defaults(cred.type, decrypt(cred.data))


async def find_credentials(session: AsyncSession, cred_type: str) -> list[tuple[Credential, dict]]:
    rows = (await session.execute(select(Credential).where(Credential.type == cred_type))).scalars().all()
    return [(c, decrypt(c.data)) for c in rows]


async def _save_data(cred_id: str, data: dict) -> None:
    async with SessionLocal() as s:
        cred = await s.get(Credential, uuid.UUID(str(cred_id)))
        if cred:
            cred.data = encrypt(data)
            await s.commit()


# ---------------------------------------------------------------------------
# Applying a credential to an HTTP request
# ---------------------------------------------------------------------------


async def request_auth(cred_type: str, data: dict, cred_id: str | None = None) -> tuple[dict, dict, httpx.Auth | None]:
    """Headers, query params and httpx auth for calling an API with this credential."""
    spec = TYPES.get(cred_type, {}).get("auth", {})
    data = with_defaults(cred_type, data)
    headers: dict = {}
    params: dict = {}
    auth = None
    if spec.get("oauth2"):
        headers["Authorization"] = f"Bearer {await oauth2_access_token(cred_type, data, cred_id)}"
    if spec.get("custom_json"):
        headers.update({k: str(v) for k, v in json.loads(data.get("headers") or "{}").items()})
        params.update(json.loads(data.get("query") or "{}"))
    for k, v in spec.get("headers", {}).items():
        val = fmt(v, data)
        if val.strip() and not val.endswith(" "):  # skip headers whose field is empty
            headers[fmt(k, data)] = val
    for k, v in spec.get("query", {}).items():
        params[fmt(k, data)] = fmt(v, data)
    if spec.get("basic"):
        user, pw = (fmt(x, data) for x in spec["basic"])
        auth = httpx.BasicAuth(user, pw)
    return headers, params, auth


# ---------------------------------------------------------------------------
# OAuth2
# ---------------------------------------------------------------------------


def oauth2_redirect_uri() -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/api/oauth2/callback"


def oauth2_authorize_url(data: dict, state: str) -> str:
    params = {"response_type": "code", "client_id": data["client_id"], "redirect_uri": oauth2_redirect_uri(), "state": state}
    if data.get("scope"):
        params["scope"] = data["scope"]
    params.update(dict(parse_qsl(data.get("auth_query") or "")))
    sep = "&" if "?" in data["auth_url"] else "?"
    return f"{data['auth_url']}{sep}{urlencode(params)}"


async def _token_request(data: dict, form: dict) -> dict:
    headers = {"Accept": "application/json"}
    if data.get("client_auth") == "header":
        raw = f"{data['client_id']}:{data['client_secret']}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
    else:
        form = {**form, "client_id": data["client_id"], "client_secret": data["client_secret"]}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(data["token_url"], data=form, headers=headers)
    try:
        body = r.json()
    except ValueError:
        body = dict(parse_qsl(r.text))
    if r.status_code >= 400 or "access_token" not in body:
        raise ValueError(f"Token endpoint error {r.status_code}: {body.get('error_description') or body.get('error') or r.text[:300]}")
    expires_in = body.get("expires_in")
    return {
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token"),
        "token_type": body.get("token_type", "Bearer"),
        "expires_at": time.time() + float(expires_in) - 60 if expires_in else None,
        "scope": body.get("scope"),
    }


async def oauth2_exchange_code(data: dict, code: str) -> dict:
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": oauth2_redirect_uri()}
    extra = dict(parse_qsl(data.get("auth_query") or ""))
    if extra.get("code_challenge_method") == "plain":  # simple PKCE (e.g. X)
        form["code_verifier"] = extra.get("code_challenge", "")
    return await _token_request(data, form)


async def oauth2_access_token(cred_type: str, data: dict, cred_id: str | None) -> str:
    tok = data.get(OAUTH_TOKEN_KEY) or {}
    fresh = tok.get("access_token") and (not tok.get("expires_at") or tok["expires_at"] > time.time())
    if fresh:
        return tok["access_token"]
    if data.get("grant_type") == "client_credentials":
        form = {"grant_type": "client_credentials", **({"scope": data["scope"]} if data.get("scope") else {})}
        tok = await _token_request(data, form)
    elif tok.get("refresh_token"):
        new = await _token_request(data, {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
        tok = {**new, "refresh_token": new.get("refresh_token") or tok["refresh_token"]}
    else:
        raise ValueError("Not connected. Open the credential and click 'Connect my account'.")
    if cred_id:
        stored = {k: v for k, v in data.items()}
        stored[OAUTH_TOKEN_KEY] = tok
        await _save_data(cred_id, stored)
    data[OAUTH_TOKEN_KEY] = tok
    return tok["access_token"]
