import asyncio
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..access import can_read
from ..db import Execution, SessionLocal, Workflow
from ..engine.bus import bus
from ..security import _allowed_origin, resolve

router = APIRouter()


async def _owner(workflow_id: str | None = None, execution_id: str | None = None) -> tuple[bool, uuid.UUID | None]:
    """(exists, owner id) of the workflow a channel belongs to."""
    try:
        async with SessionLocal() as s:
            if execution_id:
                ex = await s.get(Execution, uuid.UUID(execution_id))
                if not ex:
                    return False, None
                workflow_id = str(ex.workflow_id)
            wf = await s.get(Workflow, uuid.UUID(workflow_id))
    except ValueError:
        return False, None
    return (wf is not None, wf.owner_id if wf else None)


async def _pump(ws: WebSocket, channel: str, replay: bool, workflow_id: str | None = None,
                execution_id: str | None = None) -> None:
    # The HTTP middleware doesn't see WebSockets: authenticate the handshake here
    # (session cookie or API key), refuse cross-site origins (CSWSH) and other users' workflows.
    p = await resolve(ws)
    if p is None or not p.can("workflow:read") or not _allowed_origin(ws, ws.headers.get("origin") or ""):
        await ws.close(code=4401)
        return
    exists, owner = await _owner(workflow_id, execution_id)
    if not exists or not can_read(p, owner):
        await ws.close(code=4404)
        return
    await ws.accept()
    q, buffered = bus.subscribe(channel)
    try:
        if replay:
            for ev in buffered:
                await ws.send_json(ev)
        while True:
            get = asyncio.ensure_future(q.get())
            recv = asyncio.ensure_future(ws.receive_text())
            done, pending = await asyncio.wait({get, recv}, return_when=asyncio.FIRST_COMPLETED)
            for p in pending:
                p.cancel()
            if recv in done:
                recv.result()  # raises WebSocketDisconnect on close
            if get in done:
                await ws.send_json(get.result())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        bus.unsubscribe(channel, q)


@router.websocket("/ws/executions/{execution_id}")
async def ws_execution(ws: WebSocket, execution_id: str):
    await _pump(ws, execution_id, replay=True, execution_id=execution_id)


@router.websocket("/ws/workflows/{workflow_id}")
async def ws_workflow(ws: WebSocket, workflow_id: str):
    await _pump(ws, f"wf:{workflow_id}", replay=False, workflow_id=workflow_id)
