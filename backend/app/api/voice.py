"""Voice endpoints: browser sessions, telephony management (LiveKit SIP + Plivo), and the internal API
the LiveKit worker uses (config, routing, turns, call events)."""

import asyncio
import hmac
import json
import secrets
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select

from ..access import can_manage, principal
from ..config import get_settings
from ..credentials import load_credential
from ..db import SessionLocal, Workflow
from ..engine.bus import bus
from ..engine.runtime import execute_workflow, load_workflow, output_text
from ..security import Principal
from ..services import livekit as lk
from ..services import plivo

router = APIRouter(prefix="/api/voice", tags=["voice"])


async def _voice_trigger(wf_id: str) -> tuple[dict, dict]:
    try:
        wf = await load_workflow(wf_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    node = next((n for n in wf["nodes"] if n["type"] == "trigger.voice" and not n.get("disabled")), None)
    if not node:
        raise HTTPException(400, "Workflow has no Voice Agent trigger")
    return wf, node


def _check_internal(token: str | None) -> None:
    if not token or not hmac.compare_digest(token, get_settings().internal_token):
        raise HTTPException(401, "Invalid internal token")


def voice_params(node: dict) -> dict:
    """The trigger's parameters with every default filled in (nodes saved before a setting existed)."""
    from ..nodes import get_type

    out = {p["name"]: p.get("default") for p in get_type("trigger.voice").properties if p["type"] != "section"}
    out.update({k: v for k, v in (node.get("parameters") or {}).items() if v is not None})
    return out


def _numbers(v) -> set[str]:
    return {"".join(c for c in x if c.isdigit()) for x in str(v or "").split(",") if any(c.isdigit() for c in x)}


class SessionIn(BaseModel):
    workflow_id: str
    identity: str = "web-user"


@router.post("/session")
async def web_session(body: SessionIn, p: Principal = Depends(principal)):
    wf, node = await _voice_trigger(body.workflow_id)
    if not can_manage(p, uuid.UUID(wf["owner_id"]) if wf["owner_id"] else None):
        raise HTTPException(404, "Workflow not found")
    params = voice_params(node)
    async with SessionLocal() as s:
        try:
            _, cred = await load_credential(s, params.get("credential"), wf["owner_id"])
        except ValueError as e:
            raise HTTPException(400, f"Voice trigger: {e}") from e
    try:
        return await lk.start_web_session(cred, body.workflow_id, body.identity, agent_name=params.get("agent_name") or lk.AGENT_NAME)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"LiveKit error: {e}") from e


# ---------------------------------------------------------------------------
# Internal API for the worker (INTERNAL_TOKEN)
# ---------------------------------------------------------------------------

@router.get("/config/{wf_id}")
async def worker_config(wf_id: str, x_internal_token: str | None = Header(None)):
    """Resolved voice settings, including decrypted provider keys (worker only)."""
    _check_internal(x_internal_token)
    wf, node = await _voice_trigger(wf_id)
    p = voice_params(node)
    async with SessionLocal() as s:
        for key in ("stt_credential", "tts_credential", "amd_credential", "recording_credential"):
            if p.get(key):
                try:
                    ctype, data = await load_credential(s, p[key], wf["owner_id"])
                except ValueError as e:
                    raise HTTPException(400, f"Voice trigger '{key}': {e}") from e
                p[key] = {"type": ctype, **data}
    p.pop("credential", None)
    p["workflow_name"] = wf.get("name")
    return p


@router.get("/resolve")
async def resolve_workflow(agent_name: str = "", number: str = "", x_internal_token: str | None = Header(None)):
    """Inbound call without a workflow in its dispatch metadata: pick the active voice workflow for this
    agent name whose phone numbers include the dialled number (or that lists none)."""
    _check_internal(x_internal_token)
    dialled = "".join(c for c in number if c.isdigit())
    best = None
    async with SessionLocal() as s:
        rows = (await s.execute(select(Workflow).where(Workflow.active.is_(True)))).scalars().all()
    for wf in rows:
        node = next((n for n in wf.nodes or [] if n["type"] == "trigger.voice" and not n.get("disabled")), None)
        if not node:
            continue
        p = voice_params(node)
        if (p.get("agent_name") or lk.AGENT_NAME) != (agent_name or lk.AGENT_NAME):
            continue
        nums = _numbers(p.get("phone_numbers"))
        if dialled and dialled in nums:
            return {"workflow_id": str(wf.id), "match": "number"}
        if not nums and best is None:
            best = str(wf.id)
    if best:
        return {"workflow_id": best, "match": "agent"}
    raise HTTPException(404, f"No active voice workflow for agent '{agent_name}' and number '{number}'")


class TurnIn(BaseModel):
    workflow_id: str
    session_id: str
    text: str
    room: str | None = None
    metadata: dict = {}


@router.post("/turn")
async def voice_turn(body: TurnIn, x_internal_token: str | None = Header(None)):
    """Run the workflow for one caller turn. Streams NDJSON lines:
    {"type": "text", "text": "…"} pieces of the reply, and {"type": "action", "action": {…}} call actions
    (hang up, transfer, DTMF, say) queued by Voice Call Action nodes / the Call Control tool."""
    _check_internal(x_internal_token)
    wf, node = await _voice_trigger(body.workflow_id)
    stream_tokens = voice_params(node).get("stream_tokens", True)
    item = {"text": body.text, "message": body.text, "session_id": body.session_id, "room": body.room, **body.metadata}

    channel = f"wf:{body.workflow_id}"
    q, _ = bus.subscribe(channel)

    def line(obj: dict) -> str:
        return json.dumps(obj, default=str) + "\n"

    async def gen():
        try:
            started = await execute_workflow(wf, trigger_node_id=node["id"], items=[item], mode="voice", wait=False)
            exec_id = started["execution_id"]
            streamed = False
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=120)
                except TimeoutError:
                    yield line({"type": "text", "text": "Sorry, that took too long."})
                    return
                if ev.get("execution_id") != exec_id:
                    continue
                if ev["type"] == "token" and stream_tokens:
                    streamed = True
                    yield line({"type": "text", "text": ev["token"]})
                elif ev["type"] == "voice_action":
                    yield line({"type": "action", "action": ev.get("action") or {}})
                elif ev["type"] == "execution_finished":
                    if ev.get("status") == "error":
                        yield line({"type": "text", "text": "Sorry, something went wrong."})
                    elif not streamed:
                        yield line({"type": "text", "text": output_text(ev.get("output") or [])})
                    return
        finally:
            bus.unsubscribe(channel, q)

    return StreamingResponse(gen(), media_type="application/x-ndjson")


class EventIn(BaseModel):
    workflow_id: str
    session_id: str
    event: str
    data: dict = {}


@router.post("/event")
async def voice_event(body: EventIn, x_internal_token: str | None = Header(None)):
    """Call lifecycle event from the worker: runs this workflow's Voice Call Events triggers that listen for it."""
    _check_internal(x_internal_token)
    try:
        wf = await load_workflow(body.workflow_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    started = []
    for node in wf["nodes"]:
        if node["type"] != "trigger.voice_event" or node.get("disabled"):
            continue
        wanted = {e.strip() for e in str((node.get("parameters") or {}).get("events") or "").split(",") if e.strip()}
        if wanted and body.event not in wanted:
            continue
        item = {"event": body.event, "session_id": body.session_id, **body.data}
        res = await execute_workflow(wf, trigger_node_id=node["id"], items=[item], mode="voice_event", wait=False)
        started.append(res["execution_id"])
    return {"ok": True, "executions": started}


# ---------------------------------------------------------------------------
# Telephony management (Phone page): LiveKit trunks / dispatch rules, Plivo wiring
# ---------------------------------------------------------------------------

async def _cred(cred_id: str, p: Principal, expected: str) -> dict:
    async with SessionLocal() as s:
        try:
            ctype, data = await load_credential(s, cred_id, p.user.id)
        except ValueError as e:
            raise HTTPException(404, str(e)) from e
    if ctype != expected:
        raise HTTPException(400, f"Expected a {expected} credential, got {ctype}")
    return data


async def _lk_call(coro):
    try:
        return await coro
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 - surface LiveKit / Plivo errors to the UI
        raise HTTPException(502, str(e)) from e


@router.get("/telephony/{cred_id}")
async def telephony_overview(cred_id: str, p: Principal = Depends(principal)):
    cred = await _cred(cred_id, p, "livekit")
    inbound, outbound, rules = await _lk_call(asyncio.gather(lk.list_inbound_trunks(cred), lk.list_outbound_trunks(cred),
                                                             lk.list_dispatch_rules(cred)))
    return {"inbound": inbound, "outbound": outbound, "rules": rules, "sip_uri": cred.get("sip_uri") or ""}


class InboundIn(BaseModel):
    name: str
    numbers: str = ""
    allowed_addresses: str = ""
    allowed_numbers: str = ""
    auth_username: str = ""
    auth_password: str = ""
    krisp: bool = True
    ringing_timeout: int | None = None
    max_call_duration: int | None = None
    encryption: str = "allow"


@router.post("/telephony/{cred_id}/inbound")
async def add_inbound(cred_id: str, body: InboundIn, p: Principal = Depends(principal)):
    cred = await _cred(cred_id, p, "livekit")
    return await _lk_call(lk.create_inbound_trunk(cred, **body.model_dump()))


class OutboundIn(BaseModel):
    name: str
    address: str
    numbers: str = ""
    auth_username: str = ""
    auth_password: str = ""
    transport: str = "auto"
    destination_country: str = ""
    encryption: str = "allow"


@router.post("/telephony/{cred_id}/outbound")
async def add_outbound(cred_id: str, body: OutboundIn, p: Principal = Depends(principal)):
    cred = await _cred(cred_id, p, "livekit")
    return await _lk_call(lk.create_outbound_trunk(cred, **body.model_dump()))


class RuleIn(BaseModel):
    name: str
    agent_name: str = lk.AGENT_NAME
    workflow_id: str | None = None
    rule_type: str = "individual"
    room_prefix: str = "call-"
    room_name: str = ""
    pin: str = ""
    trunk_ids: str = ""
    inbound_numbers: str = ""
    hide_phone_number: bool = False
    krisp: bool = True


@router.post("/telephony/{cred_id}/rules")
async def add_rule(cred_id: str, body: RuleIn, p: Principal = Depends(principal)):
    cred = await _cred(cred_id, p, "livekit")
    return await _lk_call(lk.create_dispatch_rule(cred, **body.model_dump()))


@router.delete("/telephony/{cred_id}/trunks/{trunk_id}")
async def remove_trunk(cred_id: str, trunk_id: str, p: Principal = Depends(principal)):
    cred = await _cred(cred_id, p, "livekit")
    return await _lk_call(lk.delete_trunk(cred, trunk_id))


@router.delete("/telephony/{cred_id}/rules/{rule_id}")
async def remove_rule(cred_id: str, rule_id: str, p: Principal = Depends(principal)):
    cred = await _cred(cred_id, p, "livekit")
    return await _lk_call(lk.delete_dispatch_rule(cred, rule_id))


class PlivoConnectIn(BaseModel):
    livekit_credential: str
    plivo_credential: str
    phone_number: str
    agent_name: str = lk.AGENT_NAME
    workflow_id: str | None = None
    outbound: bool = True
    krisp: bool = True


@router.post("/telephony/plivo-connect")
async def plivo_connect(body: PlivoConnectIn, p: Principal = Depends(principal)):
    """One click: a Plivo number answers with a LiveKit voice agent (and the agent can call out through Plivo).

    1. Plivo origination URI → the LiveKit project's SIP URI; Zentrunk inbound trunk using it; number → that trunk.
    2. LiveKit inbound trunk for the number + dispatch rule (new room per caller) → the agent (+ workflow).
    3. (outbound) Zentrunk credential + outbound trunk; LiveKit outbound trunk → its termination domain.
    Every created id is returned; on failure the steps already done are listed so they can be cleaned up."""
    lkc = await _cred(body.livekit_credential, p, "livekit")
    pc = await _cred(body.plivo_credential, p, "plivo")
    sip_uri = (lkc.get("sip_uri") or "").strip().removeprefix("sip:")
    if not sip_uri:
        raise HTTPException(400, "Set 'SIP URI' on the LiveKit credential first (LiveKit Cloud → Settings → SIP URI)")
    number = "+" + "".join(c for c in body.phone_number if c.isdigit())
    tag = f"debbuilder-{number.lstrip('+')}"
    done: dict = {"number": number}
    try:
        uri_id = await plivo.create_uri(pc, f"{tag}-livekit", sip_uri)
        done["plivo_uri"] = uri_id
        inbound = await plivo.create_trunk(pc, f"{tag}-in", "inbound", primary_uri_uuid=uri_id)
        done["plivo_inbound_trunk"] = inbound["trunk_id"]
        await plivo.attach_number(pc, number, inbound["trunk_id"])
        done["plivo_number_routed"] = True
        lk_in = await lk.create_inbound_trunk(lkc, name=f"{tag} (Plivo)", numbers=[number], krisp=body.krisp)
        done["livekit_inbound_trunk"] = lk_in.get("sip_trunk_id")
        rule = await lk.create_dispatch_rule(lkc, name=f"{tag} → {body.agent_name}", agent_name=body.agent_name,
                                             workflow_id=body.workflow_id, trunk_ids=[lk_in.get("sip_trunk_id")], krisp=body.krisp)
        done["livekit_dispatch_rule"] = rule.get("sip_dispatch_rule_id")
        if body.outbound:
            username, password = f"lk{secrets.token_hex(5)}", secrets.token_urlsafe(18)
            cred_id = await plivo.create_credential(pc, f"{tag}-livekit", username, password)
            done["plivo_credential"] = cred_id
            outbound = await plivo.create_trunk(pc, f"{tag}-out", "outbound", credential_uuid=cred_id)
            done["plivo_outbound_trunk"] = outbound["trunk_id"]
            domain = outbound.get("trunk_domain") or outbound.get("termination_domain") or f"{outbound['trunk_id']}.zt.plivo.com"
            lk_out = await lk.create_outbound_trunk(lkc, name=f"{tag} (Plivo out)", address=domain, numbers=[number],
                                                    auth_username=username, auth_password=password)
            done["livekit_outbound_trunk"] = lk_out.get("sip_trunk_id")
    except (plivo.PlivoError, Exception) as e:  # noqa: BLE001
        raise HTTPException(502, {"message": str(e), "completed_steps": done}) from e
    return {"ok": True, **done}

