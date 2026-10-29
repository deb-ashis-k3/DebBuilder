"""Public entry points: webhooks, hosted forms, chat, WhatsApp webhook."""

import html
import json
import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel
from sqlalchemy import select

from ..credentials import decrypt
from ..db import Credential, SessionLocal, Workflow
from ..engine.runtime import execute_workflow, load_workflow, output_text, workflow_to_dict
from ..services.whatsapp import parse_webhook, verify_signature

log = logging.getLogger("debbuilder.hooks")
router = APIRouter(tags=["hooks"])


# (method, path) -> (workflow dict, webhook node) for active workflows. Rebuilt lazily after
# any workflow change (invalidate_webhooks) instead of scanning every workflow per request.
_webhook_routes: dict[tuple[str, str], tuple[dict, dict]] | None = None


def invalidate_webhooks() -> None:
    global _webhook_routes
    _webhook_routes = None


async def _active_webhook(method: str, path: str) -> tuple[dict, dict] | None:
    global _webhook_routes
    if _webhook_routes is None:
        routes: dict[tuple[str, str], tuple[dict, dict]] = {}
        for wf, n in await _workflows_with("trigger.webhook", active_only=True):
            p = n.get("parameters") or {}
            routes.setdefault((p.get("method", "POST").upper(), p.get("path", "").strip("/")), (wf, n))
        _webhook_routes = routes
    return _webhook_routes.get((method, path.strip("/")))


async def _workflows_with(trigger_type: str, active_only: bool = True) -> list[tuple[dict, dict]]:
    async with SessionLocal() as s:
        q = select(Workflow)
        if active_only:
            q = q.where(Workflow.active.is_(True))
        rows = (await s.execute(q)).scalars().all()
    hits = []
    for wf in rows:
        for n in wf.nodes or []:
            if n.get("type") == trigger_type and not n.get("disabled"):
                hits.append((workflow_to_dict(wf), n))
    return hits


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


async def _handle_webhook(path: str, request: Request, test: bool):
    method = request.method.upper()
    if test:
        matches = [
            (wf, n) for wf, n in await _workflows_with("trigger.webhook", active_only=False)
            if (n.get("parameters") or {}).get("path", "").strip("/") == path.strip("/")
            and (n.get("parameters") or {}).get("method", "POST").upper() == method
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

    raw = await request.body()
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        body = {"raw": raw.decode(errors="replace")}
    item = {"body": body, "query": dict(request.query_params), "headers": dict(request.headers),
            "method": method, "path": path}

    mode = params.get("respond", "last_node")
    res = await execute_workflow(wf, trigger_node_id=node["id"], items=[item], mode="webhook",
                                 wait=mode != "immediately")
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
    """Same as /webhook but also matches inactive workflows (for building)."""
    return await _handle_webhook(path, request, test=True)


# ---------------------------------------------------------------------------
# Hosted forms
# ---------------------------------------------------------------------------

FORM_CSS = """body{font-family:system-ui,sans-serif;background:#f4f4f7;display:flex;justify-content:center;padding:48px 16px;margin:0}
form,.card{background:#fff;padding:32px;border-radius:12px;max-width:480px;width:100%;box-shadow:0 2px 12px #0001}
h1{margin:0 0 8px;font-size:22px}p{color:#555}label{display:block;margin:16px 0 6px;font-weight:600;font-size:14px}
input,textarea{width:100%;box-sizing:border-box;padding:10px;border:1px solid #ccd;border-radius:8px;font:inherit}
button{margin-top:24px;width:100%;padding:12px;border:0;border-radius:8px;background:#ff6d5a;color:#fff;font-weight:600;font-size:15px;cursor:pointer}"""


async def _form_node(wf_id: str) -> tuple[dict, dict]:
    try:
        wf = await load_workflow(wf_id)
    except ValueError as e:
        raise HTTPException(404, "Form not found") from e
    node = next((n for n in wf["nodes"] if n["type"] == "trigger.form" and not n.get("disabled")), None)
    if not node:
        raise HTTPException(404, "Form not found")
    return wf, node


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
    await execute_workflow(wf, trigger_node_id=node["id"], items=[data], mode="webhook", wait=False)
    msg = html.escape((node.get("parameters") or {}).get("success_message") or "Thanks!")
    return f"<!doctype html><html><head><meta charset=utf-8><style>{FORM_CSS}</style></head><body><div class=card><h1>✓</h1><p>{msg}</p></div></body></html>"


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
    try:
        wf = await load_workflow(wf_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    node = next((n for n in wf["nodes"] if n["type"] == "trigger.chat" and not n.get("disabled")), None)
    if not node:
        raise HTTPException(400, "Workflow has no Chat Trigger")
    # Signed-in users who may run workflows can always chat (editor panel); anyone else only if the
    # Chat Trigger is marked public. (A client header used to bypass this check.)
    p = getattr(request.state, "principal", None)
    if not (p and p.can("workflow:execute")) and not (node.get("parameters") or {}).get("public"):
        raise HTTPException(403, "Public chat is disabled on this Chat Trigger")
    item = {"message": body.message, "chatInput": body.message, "session_id": body.session_id or str(uuid.uuid4()),
            **body.metadata}
    res = await execute_workflow(wf, trigger_node_id=node["id"], items=[item], mode="chat", wait=body.wait)
    if body.wait:
        res["reply"] = output_text(res.get("output") or [])
    return res


# ---------------------------------------------------------------------------
# WhatsApp Cloud API webhook
# ---------------------------------------------------------------------------


async def _whatsapp_creds() -> list[tuple[str, dict]]:
    async with SessionLocal() as s:
        rows = (await s.execute(select(Credential).where(Credential.type == "whatsapp"))).scalars().all()
    return [(str(c.id), decrypt(c.data)) for c in rows]


@router.get("/api/whatsapp/webhook")
async def whatsapp_verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe":
        token = q.get("hub.verify_token")
        if any(c.get("verify_token") == token for _, c in await _whatsapp_creds()):
            return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403, "Verification failed")


@router.post("/api/whatsapp/webhook")
async def whatsapp_receive(request: Request):
    raw = await request.body()
    payload = json.loads(raw or b"{}")
    events = parse_webhook(payload)
    if not events:
        return {"ok": True}
    creds = dict(await _whatsapp_creds())
    for wf, node in await _workflows_with("trigger.whatsapp"):
        params = node.get("parameters") or {}
        cred = creds.get(str(params.get("credential")))
        if not cred:
            continue
        if cred.get("app_secret") and not verify_signature(cred["app_secret"], raw, request.headers.get("x-hub-signature-256")):
            log.warning("WhatsApp signature mismatch for workflow %s", wf["id"])
            continue
        for ev in events:
            if ev.get("phone_number_id") != cred.get("phone_number_id"):
                continue
            if ev["event"] == "status" and params.get("events", "messages") != "all":
                continue
            await execute_workflow(wf, trigger_node_id=node["id"], items=[ev], mode="whatsapp", wait=False)
    # Meta retries unless we answer 200 quickly; executions run in the background.
    return {"ok": True}
