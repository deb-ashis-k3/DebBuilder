"""Plivo REST helpers for wiring a Plivo number to a LiveKit voice agent.

The Python SDK (plivo-python) has no Zentrunk (SIP trunking) API, so this talks to the REST API
directly: https://api.plivo.com/v1/Account/{auth_id}/… with HTTP basic auth.

  inbound:  Plivo number → Zentrunk inbound trunk → origination URI = the LiveKit project's SIP URI
            → LiveKit inbound trunk (+ dispatch rule) → the agent
  outbound: LiveKit outbound trunk → Plivo termination domain (<id>.zt.plivo.com) with a Zentrunk credential
"""

from __future__ import annotations

import httpx

BASE = "https://api.plivo.com/v1/Account"


class PlivoError(RuntimeError):
    pass


async def call(cred: dict, method: str, path: str, **kw) -> dict:
    url = f"{BASE}/{cred['auth_id']}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.request(method, url, auth=(cred["auth_id"], cred["auth_token"]), **kw)
    if r.status_code >= 400:
        try:
            msg = r.json().get("error") or r.json()
        except ValueError:
            msg = r.text[:300]
        raise PlivoError(f"Plivo {method} {path} failed ({r.status_code}): {msg}")
    return r.json() if r.content else {}


async def create_uri(cred: dict, name: str, uri: str) -> str:
    res = await call(cred, "POST", "Zentrunk/URI/", json={"name": name, "uri": uri})
    return res.get("uri_uuid") or res.get("uuid") or res["id"]


async def create_credential(cred: dict, name: str, username: str, password: str) -> str:
    res = await call(cred, "POST", "Zentrunk/Credential/", json={"name": name, "username": username, "password": password})
    return res.get("credential_uuid") or res.get("uuid") or res["id"]


async def create_trunk(cred: dict, name: str, direction: str, **fields) -> dict:
    res = await call(cred, "POST", "Zentrunk/Trunk/", json={"name": name, "trunk_direction": direction, "trunk_status": "enabled",
                                                            "secure": False, **fields})
    trunk_id = res.get("trunk_id") or res.get("id")
    detail = await call(cred, "GET", f"Zentrunk/Trunk/{trunk_id}/")
    return {"trunk_id": trunk_id, **(detail.get("object") if isinstance(detail.get("object"), dict) else detail)}


async def attach_number(cred: dict, number: str, trunk_id: str) -> dict:
    """Route a Plivo number to a Zentrunk inbound trunk (the trunk id goes in the number's app_id)."""
    return await call(cred, "POST", f"Number/{number.lstrip('+')}/", json={"app_id": trunk_id})
