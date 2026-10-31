"""Public entry points: webhooks, hosted forms, chat, WhatsApp webhook."""

import asyncio
import html
import json
import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel
from sqlalchemy import select

from ..access import can_manage
from ..credentials import decrypt
from ..db import Credential, SessionLocal, Workflow
from ..engine.errors import Overloaded
from ..engine.runtime import execute_workflow, output_text, workflow_to_dict
from ..services.whatsapp import parse_webhook, verify_signature

log = logging.getLogger("debbuilder.hooks")
router = APIRouter(tags=["hooks"])


def busy(e: Overloaded) -> JSONResponse:
    """Admission control refused the execution (MAX_QUEUED_EXECUTIONS): 503, and when to retry."""
    return JSONResponse({"detail": str(e)}, status_code=503, headers={"Retry-After": str(e.retry_after)})


class _Routes:
    """Production entry points of active workflows, rebuilt lazily after any change (invalidate_webhooks) instead
    of scanning every workflow per request:
      webhooks: (METHOD, path) -> (workflow dict, webhook node)
      whatsapp: phone_number_id -> [(workflow dict, trigger node, decrypted credential)]: a WhatsApp message
                then decrypts nothing and looks at only the workflows of the number it was sent to."""

    def __init__(self, webhooks: dict, whatsapp: dict):
        self.webhooks, self.whatsapp = webhooks, whatsapp


_routes: _Routes | None = None
_routes_loading: asyncio.Task | None = None
_routes_version = 0


def invalidate_webhooks() -> None:
    """Called on every workflow, credential or user change (cluster._apply), on every replica."""
    global _routes, _routes_version
    _routes = None
    _routes_version += 1  # a rebuild already running started before this change: don't keep its result


async def _load_routes() -> _Routes:
    webhooks: dict[tuple[str, str], tuple[dict, dict]] = {}
    for wf, n in await _workflows_with("trigger.webhook", active_only=True):
        p = n.get("parameters") or {}
        key = (str(p.get("method") or "POST").upper(), str(p.get("path") or "").strip("/"))
        if key in webhooks:  # publishing refuses this now; older data may still have it
            log.warning("Workflows %s and %s both listen on %s /webhook/%s; %s gets the requests",
                        webhooks[key][0]["id"], wf["id"], *key, webhooks[key][0]["id"])
            continue
        webhooks[key] = (wf, n)
    whatsapp: dict[str, list[tuple[dict, dict, dict]]] = {}
    hits = await _workflows_with("trigger.whatsapp", active_only=True)
    cred_ids = set()
    for _, n in hits:
        try:
            cred_ids.add(uuid.UUID(str((n.get("parameters") or {}).get("credential"))))
        except ValueError:
            pass
    creds: dict[str, dict] = {}
    if cred_ids:
        async with SessionLocal() as s:
            rows = (await s.execute(select(Credential).where(Credential.id.in_(cred_ids),
                                                             Credential.type == "whatsapp"))).scalars().all()
        for c in rows:
            try:
                creds[str(c.id)] = {**decrypt(c.data), "_owner_id": str(c.owner_id) if c.owner_id else None}
            except Exception:  # noqa: BLE001 - one unreadable credential must not break every WhatsApp trigger
                log.warning("WhatsApp credential %s can't be decrypted", c.id)
    for wf, n in hits:
        cred = creds.get(str((n.get("parameters") or {}).get("credential")))
        if cred and cred["_owner_id"] == wf.get("owner_id"):  # a trigger only uses its workflow owner's credential
            whatsapp.setdefault(str(cred.get("phone_number_id")), []).append((wf, n, cred))
    return _Routes(webhooks, whatsapp)


async def _current_routes() -> _Routes:
    """Single-flight rebuild: under load hundreds of requests arrive while the cache is empty (startup, a publish),
    and each querying the database on its own exhausted the connection pool and Postgres' client limit."""
    global _routes_loading
    while _routes is None:
        if _routes_loading is None or _routes_loading.done():
            version = _routes_version

            async def build() -> None:
                global _routes
                routes = await _load_routes()
                if version == _routes_version:
                    _routes = routes
            _routes_loading = asyncio.get_running_loop().create_task(build())
        await asyncio.shield(_routes_loading)
    return _routes


async def _active_webhook(method: str, path: str) -> tuple[dict, dict] | None:
    return (await _current_routes()).webhooks.get((method, path.strip("/")))


async def _workflows_with(trigger_type: str, active_only: bool = True,
                          owner_id: uuid.UUID | None = None) -> list[tuple[dict, dict]]:
    """Workflows with an enabled trigger of this type. active_only: production (the published version) of active
    workflows; otherwise the drafts of `owner_id`'s workflows (test webhooks)."""
    async with SessionLocal() as s:
        q = select(Workflow)
        if active_only:
            q = q.where(Workflow.active.is_(True))
        else:
            q = q.where(Workflow.owner_id == owner_id, Workflow.nodes.contains([{"type": trigger_type}]))
        rows = (await s.execute(q)).scalars().all()
    hits = []
    for wf in rows:
        for n in (wf.live_nodes if active_only else wf.nodes or []):
            if n.get("type") == trigger_type and not n.get("disabled"):
                hits.append((workflow_to_dict(wf, live=active_only), n))
    return hits


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


async def _handle_webhook(path: str, request: Request, test: bool):
    method = request.method.upper()
    if test:
        # Test webhooks run a draft: only the signed-in owner of that workflow may trigger it (everyone else gets
        # the same 404 as for a path nobody listens on).
        p = getattr(request.state, "principal", None)
        matches = [] if p is None else [
            (wf, n) for wf, n in await _workflows_with("trigger.webhook", active_only=False, owner_id=p.user.id)
            if str((n.get("parameters") or {}).get("path") or "").strip("/") == path.strip("/")
            and str((n.get("parameters") or {}).get("method") or "POST").upper() == method
        ]
    else:
        hit = await _active_webhook(method, path)
        matches = [hit] if hit else []
    if not matches:
        raise HTTPException(404, f"No {'' if test else 'active '}workflow listens on {method} /{path}")
    wf, node = matches[0]
    params = node.get("parameters") or {}

    if params.get("auth_header"):
        name, _, value = params["auth_header"].partition(":")
        if request.headers.get(name.strip()) != value.strip():
            raise HTTPException(401, "Unauthorized")

    raw = await request.body()  # size capped by the request body limit (main._BodyLimit)
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        body = {"raw": raw.decode(errors="replace")}
    item = {"body": body, "query": dict(request.query_params), "headers": dict(request.headers),
            "method": method, "path": path}

    mode = params.get("respond", "last_node")
    # /webhook-test runs are test runs ("manual", as in n8n): the editor canvas follows them, not production ones.
    try:
        res = await execute_workflow(wf, trigger_node_id=node["id"], items=[item], mode="manual" if test else "webhook",
                                     wait=mode != "immediately")
    except Overloaded as e:
        return busy(e)
    if mode == "immediately":
        return JSONResponse({"message": "Workflow started", "execution_id": res["execution_id"]}, status_code=202)
    if res.get("response"):
        r = res["response"]
        body = r["body"]
        if isinstance(body, (dict, list)):
            return JSONResponse(body, status_code=r["status"], headers=r.get("headers") or None)
        return Response(str(body), status_code=r["status"], headers=r.get("headers") or None)
    if res["status"] == "error":
        return JSONResponse({"error": res["error"], "execution_id": res["execution_id"]}, status_code=500)
    out = res["output"]
    return JSONResponse(out[0] if len(out) == 1 else out)


@router.api_route("/webhook/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def webhook(path: str, request: Request):
    return await _handle_webhook(path, request, test=False)


@router.api_route("/webhook-test/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def webhook_test(path: str, request: Request):
    """Same as /webhook but runs the caller's own draft (inactive workflows too, for building)."""
    return await _handle_webhook(path, request, test=True)


# ---------------------------------------------------------------------------
# Hosted forms
# ---------------------------------------------------------------------------

FORM_CSS = """body{font-family:system-ui,sans-serif;background:#f4f4f7;display:flex;justify-content:center;padding:48px 16px;margin:0}
form,.card{background:#fff;padding:32px;border-radius:12px;max-width:480px;width:100%;box-shadow:0 2px 12px #0001}
h1{margin:0 0 8px;font-size:22px}p{color:#555}label{display:block;margin:16px 0 6px;font-weight:600;font-size:14px}
input,textarea{width:100%;box-sizing:border-box;padding:10px;border:1px solid #ccd;border-radius:8px;font:inherit}
button{margin-top:24px;width:100%;padding:12px;border:0;border-radius:8px;background:#ff6d5a;color:#fff;font-weight:600;font-size:15px;cursor:pointer}"""


async def _live_workflow(wf_id: str) -> dict | None:
    """What production runs, only for an active, published workflow (public entry points never reach drafts)."""
    try:
        key = uuid.UUID(wf_id)
    except ValueError:
        return None
    async with SessionLocal() as s:
        wf = await s.get(Workflow, key)
        if not wf or not wf.active or not wf.published_version_id:
            return None
        return workflow_to_dict(wf, live=True)


async def _form_node(wf_id: str) -> tuple[dict, dict]:
    wf = await _live_workflow(wf_id)
    node = next((n for n in (wf or {}).get("nodes", []) if n["type"] == "trigger.form" and not n.get("disabled")), None)
    if not node:
        raise HTTPException(404, "Form not found")
    return wf, node


def _card(title: str, message: str) -> str:
    return (f"<!doctype html><html><head><meta charset=utf-8><style>{FORM_CSS}</style></head><body><div class=card>"
            f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p></div></body></html>")


@router.get("/form/{wf_id}", response_class=HTMLResponse)
async def form_page(wf_id: str):
    _, node = await _form_node(wf_id)
    p = node.get("parameters") or {}
    fields = []
    for f in p.get("fields") or []:
        name, label = html.escape(f.get("name", "")), html.escape(f.get("label") or f.get("name", ""))
        req = " required" if f.get("required") else ""
        if f.get("type") == "textarea":
            fields.append(f"<label>{label}</label><textarea name='{name}' rows='4'{req}></textarea>")
        else:
            fields.append(f"<label>{label}</label><input type='{html.escape(f.get('type', 'text'))}' name='{name}'{req}>")
    return f"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
<title>{html.escape(p.get('title', 'Form'))}</title><style>{FORM_CSS}</style></head><body>
<form method=post><h1>{html.escape(p.get('title', 'Form'))}</h1><p>{html.escape(p.get('description', ''))}</p>
{''.join(fields)}<button type=submit>Submit</button></form></body></html>"""


@router.post("/form/{wf_id}", response_class=HTMLResponse)
async def form_submit(wf_id: str, request: Request, background: BackgroundTasks):
    wf, node = await _form_node(wf_id)
    data = dict(await request.form())
    try:
        await execute_workflow(wf, trigger_node_id=node["id"], items=[data], mode="webhook", wait=False)
    except Overloaded as e:
        return HTMLResponse(_card("Busy", "The server is busy right now. Go back and submit again in a moment."),
                            status_code=503, headers={"Retry-After": str(e.retry_after)})
    msg = (node.get("parameters") or {}).get("success_message") or "Thanks!"
    return _card("✓", msg)


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------


class ChatIn(BaseModel):
    message: str
    session_id: str | None = None
    wait: bool = False
    metadata: dict = {}


@router.post("/api/chat/{wf_id}")
async def chat(wf_id: str, body: ChatIn, request: Request):
    # Its owner (signed in, allowed to run workflows) may always chat: the editor's chat panel (header
    # x-debbuilder-editor: 1) tests the draft. Everyone else talks to the published version of an active workflow
    # whose Chat Trigger is marked public.
    p = getattr(request.state, "principal", None)
    try:
        key = uuid.UUID(wf_id)
    except ValueError:
        raise HTTPException(404, "Workflow not found") from None
    async with SessionLocal() as s:
        row = await s.get(Workflow, key)
        if row is None:
            raise HTTPException(404, "Workflow not found")
        owner = bool(p and p.can("workflow:execute") and can_manage(p, row.owner_id))
        if owner and request.headers.get("x-debbuilder-editor") == "1":
            wf = workflow_to_dict(row)
        elif owner or (row.active and row.published_version_id):
            wf = workflow_to_dict(row, live=True)
        else:
            raise HTTPException(404, "Workflow not found")  # unpublished / inactive: not reachable from outside
    node = next((n for n in wf["nodes"] if n["type"] == "trigger.chat" and not n.get("disabled")), None)
    if not node:
        raise HTTPException(400, "Workflow has no Chat Trigger")
    if not owner and not (node.get("parameters") or {}).get("public"):
        raise HTTPException(403, "Public chat is disabled on this Chat Trigger")
    item = {"message": body.message, "chatInput": body.message, "session_id": body.session_id or str(uuid.uuid4()),
            **body.metadata}
    try:
        res = await execute_workflow(wf, trigger_node_id=node["id"], items=[item], mode="chat", wait=body.wait)
    except Overloaded as e:
        return busy(e)
    if body.wait:
        res["reply"] = output_text(res.get("output") or [])
    return res


# ---------------------------------------------------------------------------
# WhatsApp Cloud API webhook
# ---------------------------------------------------------------------------


async def _whatsapp_creds() -> list[tuple[str, dict]]:
    """[(credential id, data incl. "_owner_id")] of every WhatsApp credential (the rare verification handshake only)."""
    async with SessionLocal() as s:
        rows = (await s.execute(select(Credential).where(Credential.type == "whatsapp"))).scalars().all()
    out = []
    for c in rows:
        try:
            out.append((str(c.id), {**decrypt(c.data), "_owner_id": str(c.owner_id) if c.owner_id else None}))
        except Exception:  # noqa: BLE001
            continue
    return out


@router.get("/api/whatsapp/webhook")
async def whatsapp_verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe":
        token = q.get("hub.verify_token")
        if token and any(c.get("verify_token") == token for _, c in await _whatsapp_creds()):
            return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403, "Verification failed")


@router.post("/api/whatsapp/webhook")
async def whatsapp_receive(request: Request):
    raw = await request.body()
    try:
        payload = json.loads(raw or b"{}")
        events = parse_webhook(payload) if isinstance(payload, dict) else None
    except (ValueError, TypeError, AttributeError, KeyError):
        events = None
    if events is None:
        raise HTTPException(400, "Expected a WhatsApp Cloud API webhook (JSON)")
    if not events:
        return {"ok": True}
    routes = await _current_routes()
    signature = request.headers.get("x-hub-signature-256")
    try:
        for number in {ev.get("phone_number_id") for ev in events}:
            for wf, node, cred in routes.whatsapp.get(str(number), []):
                params = node.get("parameters") or {}
                if cred.get("app_secret") and not verify_signature(cred["app_secret"], raw, signature):
                    log.warning("WhatsApp signature mismatch for workflow %s", wf["id"])
                    continue
                for ev in events:
                    if ev.get("phone_number_id") != cred.get("phone_number_id"):
                        continue
                    if ev["event"] == "status" and params.get("events", "messages") != "all":
                        continue
                    await execute_workflow(wf, trigger_node_id=node["id"], items=[ev], mode="whatsapp", wait=False)
    except Overloaded as e:
        return busy(e)  # Meta retries the delivery later
    # Meta retries unless we answer 200 quickly; executions run in the background.
    return {"ok": True}
