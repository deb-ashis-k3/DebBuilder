"""Voice endpoints: browser sessions + internal API for the LiveKit worker."""

import asyncio
import hmac

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..config import get_settings
from ..credentials import load_credential
from ..db import SessionLocal
from ..engine.bus import bus
from ..engine.runtime import execute_workflow, load_workflow, output_text
from ..services import livekit as lk

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


class SessionIn(BaseModel):
    workflow_id: str
    identity: str = "web-user"


@router.post("/session")
async def web_session(body: SessionIn):
    _, node = await _voice_trigger(body.workflow_id)
    async with SessionLocal() as s:
        try:
            _, cred = await load_credential(s, (node.get("parameters") or {}).get("credential"))
        except ValueError as e:
            raise HTTPException(400, f"Voice trigger: {e}") from e
    try:
        return await lk.start_web_session(cred, body.workflow_id, body.identity)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"LiveKit error: {e}") from e


@router.get("/config/{wf_id}")
async def worker_config(wf_id: str, x_internal_token: str | None = Header(None)):
    """Resolved voice settings, including decrypted STT/TTS keys (worker only)."""
    _check_internal(x_internal_token)
    _, node = await _voice_trigger(wf_id)
    p = dict(node.get("parameters") or {})
    async with SessionLocal() as s:
        for key in ("stt_credential", "tts_credential"):
            if p.get(key):
                ctype, data = await load_credential(s, p[key])
                p[key] = {"type": ctype, **data}
    p.pop("credential", None)
    return p


class TurnIn(BaseModel):
    workflow_id: str
    session_id: str
    text: str
    room: str | None = None
    metadata: dict = {}


@router.post("/turn")
async def voice_turn(body: TurnIn, x_internal_token: str | None = Header(None)):
    """Run the workflow for one user utterance; stream the reply as plain text."""
    _check_internal(x_internal_token)
    wf, node = await _voice_trigger(body.workflow_id)
    stream_tokens = (node.get("parameters") or {}).get("stream_tokens", True)
    item = {"text": body.text, "message": body.text, "session_id": body.session_id, "room": body.room,
            **body.metadata}

    channel = f"wf:{body.workflow_id}"
    q, _ = bus.subscribe(channel)

    async def gen():
        try:
            started = await execute_workflow(wf, trigger_node_id=node["id"], items=[item], mode="voice", wait=False)
            exec_id = started["execution_id"]
            streamed = False
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=120)
                except TimeoutError:
                    yield "Sorry, that took too long."
                    return
                if ev.get("execution_id") != exec_id:
                    continue
                if ev["type"] == "token" and stream_tokens:
                    streamed = True
                    yield ev["token"]
                elif ev["type"] == "execution_finished":
                    if ev.get("status") == "error":
                        yield "Sorry, something went wrong."
                    elif not streamed:
                        yield output_text(ev.get("output") or [])
                    return
        finally:
            bus.unsubscribe(channel, q)

    return StreamingResponse(gen(), media_type="text/plain")
