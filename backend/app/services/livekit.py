"""LiveKit helpers: tokens, rooms, agent dispatch and outbound SIP calls."""

import json
import uuid

from livekit import api

AGENT_NAME = "debbuilder-voice"


def http_url(url: str) -> str:
    return url.replace("wss://", "https://").replace("ws://", "http://")


def participant_token(cred: dict, room: str, identity: str, name: str | None = None, ttl_minutes: int = 60) -> str:
    from datetime import timedelta

    return (
        api.AccessToken(cred["api_key"], cred["api_secret"])
        .with_identity(identity)
        .with_name(name or identity)
        .with_ttl(timedelta(minutes=ttl_minutes))
        .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True,
                                     can_publish_data=True))
        .to_jwt()
    )


async def dispatch_agent(cred: dict, room: str, metadata: dict) -> None:
    lk = api.LiveKitAPI(http_url(cred["url"]), cred["api_key"], cred["api_secret"])
    try:
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(agent_name=AGENT_NAME, room=room, metadata=json.dumps(metadata))
        )
    finally:
        await lk.aclose()


async def start_web_session(cred: dict, workflow_id: str, user: str = "web-user", extra: dict | None = None) -> dict:
    room = f"db-{workflow_id[:8]}-{uuid.uuid4().hex[:8]}"
    await dispatch_agent(cred, room, {"workflow_id": workflow_id, "session_id": room, **(extra or {})})
    return {"url": cred["url"], "room": room, "token": participant_token(cred, room, user)}


async def outbound_call(cred: dict, workflow_id: str, phone: str, trunk_id: str | None = None,
                        from_number: str | None = None, extra: dict | None = None, wait_until_answered: bool = False) -> dict:
    trunk = trunk_id or cred.get("sip_trunk_id")
    if not trunk:
        raise ValueError("No outbound SIP trunk configured (set it on the LiveKit credential or node)")
    room = f"call-{uuid.uuid4().hex[:10]}"
    await dispatch_agent(cred, room, {"workflow_id": workflow_id, "session_id": room, "phone": phone,
                                      "direction": "outbound", **(extra or {})})
    lk = api.LiveKitAPI(http_url(cred["url"]), cred["api_key"], cred["api_secret"])
    try:
        req = api.CreateSIPParticipantRequest(
            sip_trunk_id=trunk, sip_call_to=phone, room_name=room,
            participant_identity=f"phone-{phone}", participant_name=phone,
            wait_until_answered=wait_until_answered,
        )
        if from_number:
            req.sip_number = from_number
        info = await lk.sip.create_sip_participant(req)
    finally:
        await lk.aclose()
    return {"room": room, "phone": phone, "participant_id": info.participant_id, "sip_call_id": info.sip_call_id}
