import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..engine.bus import bus
from ..security import _allowed_origin, resolve

router = APIRouter()


async def _pump(ws: WebSocket, channel: str, replay: bool) -> None:
    # The HTTP middleware doesn't see WebSockets: authenticate the handshake here
    # (session cookie or API key) and refuse cross-site origins (CSWSH).
    p = await resolve(ws)
    if p is None or not p.can("workflow:read") or not _allowed_origin(ws, ws.headers.get("origin") or ""):
        await ws.close(code=4401)
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
    await _pump(ws, execution_id, replay=True)


@router.websocket("/ws/workflows/{workflow_id}")
async def ws_workflow(ws: WebSocket, workflow_id: str):
    await _pump(ws, f"wf:{workflow_id}", replay=False)
