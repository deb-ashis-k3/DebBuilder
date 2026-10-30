"""LiveKit server API helpers: tokens, rooms, agent dispatch, SIP telephony (trunks, dispatch rules,
outbound calls, transfers) and call recording (egress). Results are plain JSON dicts."""

from __future__ import annotations

import json
import time
import uuid
from contextlib import asynccontextmanager

from google.protobuf.duration_pb2 import Duration
from google.protobuf.json_format import MessageToDict
from livekit import api

AGENT_NAME = "debbuilder-voice"


def http_url(url: str) -> str:
    return url.replace("wss://", "https://").replace("ws://", "http://")


@asynccontextmanager
async def client(cred: dict):
    lk = api.LiveKitAPI(http_url(cred["url"]), cred["api_key"], cred["api_secret"])
    try:
        yield lk
    finally:
        await lk.aclose()


def to_dict(msg) -> dict:
    return MessageToDict(msg, preserving_proto_field_name=True)


def _dur(seconds) -> Duration | None:
    return Duration(seconds=int(seconds)) if seconds not in (None, "", 0) else None


def _list(v) -> list[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in str(v or "").split(",") if x.strip()]


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


async def dispatch_agent(cred: dict, room: str, metadata: dict, agent_name: str = AGENT_NAME) -> None:
    async with client(cred) as lk:
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(agent_name=agent_name or AGENT_NAME, room=room, metadata=json.dumps(metadata)))


async def start_web_session(cred: dict, workflow_id: str, user: str = "web-user", extra: dict | None = None,
                            agent_name: str = AGENT_NAME) -> dict:
    room = f"db-{workflow_id[:8]}-{uuid.uuid4().hex[:8]}"
    await dispatch_agent(cred, room, {"workflow_id": workflow_id, "session_id": room, "direction": "web", **(extra or {})}, agent_name)
    return {"url": cred["url"], "room": room, "token": participant_token(cred, room, user), "agent_name": agent_name}


async def outbound_call(cred: dict, workflow_id: str, phone: str, trunk_id: str | None = None,
                        from_number: str | None = None, extra: dict | None = None, wait_until_answered: bool = False, *,
                        agent_name: str = AGENT_NAME, ringing_timeout=None, max_call_duration=None, krisp: bool = True,
                        play_dialtone: bool = False, headers: dict | None = None, display_name: str | None = None) -> dict:
    trunk = trunk_id or cred.get("sip_trunk_id")
    if not trunk:
        raise ValueError("No outbound SIP trunk configured (set it on the LiveKit credential or node)")
    room = f"call-{uuid.uuid4().hex[:10]}"
    await dispatch_agent(cred, room, {"workflow_id": workflow_id, "session_id": room, "phone": phone,
                                      "direction": "outbound", **(extra or {})}, agent_name)
    req = api.CreateSIPParticipantRequest(
        sip_trunk_id=trunk, sip_call_to=phone, room_name=room, participant_identity=f"phone-{phone}", participant_name=phone,
        wait_until_answered=wait_until_answered, krisp_enabled=krisp, play_dialtone=play_dialtone, headers=headers or {})
    if from_number:
        req.sip_number = from_number
    if display_name:
        req.display_name = display_name
    if d := _dur(ringing_timeout):
        req.ringing_timeout.CopyFrom(d)
    if d := _dur(max_call_duration):
        req.max_call_duration.CopyFrom(d)
    async with client(cred) as lk:
        info = await lk.sip.create_sip_participant(req)
    return {"room": room, "phone": phone, "participant_id": info.participant_id, "participant_identity": info.participant_identity,
            "sip_call_id": info.sip_call_id, "agent_name": agent_name}


# ---------------------------------------------------------------------------
# SIP trunks and dispatch rules
# ---------------------------------------------------------------------------

TRANSPORTS = {"auto": api.SIPTransport.SIP_TRANSPORT_AUTO, "udp": api.SIPTransport.SIP_TRANSPORT_UDP,
              "tcp": api.SIPTransport.SIP_TRANSPORT_TCP, "tls": api.SIPTransport.SIP_TRANSPORT_TLS}
ENCRYPTION = {"disable": api.SIPMediaEncryption.SIP_MEDIA_ENCRYPT_DISABLE, "allow": api.SIPMediaEncryption.SIP_MEDIA_ENCRYPT_ALLOW,
              "require": api.SIPMediaEncryption.SIP_MEDIA_ENCRYPT_REQUIRE}


async def list_inbound_trunks(cred: dict) -> list[dict]:
    async with client(cred) as lk:
        res = await lk.sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest())
    return [to_dict(t) for t in res.items]


async def list_outbound_trunks(cred: dict) -> list[dict]:
    async with client(cred) as lk:
        res = await lk.sip.list_outbound_trunk(api.ListSIPOutboundTrunkRequest())
    return [to_dict(t) for t in res.items]


async def list_dispatch_rules(cred: dict) -> list[dict]:
    async with client(cred) as lk:
        res = await lk.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())
    return [to_dict(r) for r in res.items]


async def create_inbound_trunk(cred: dict, *, name: str, numbers, allowed_addresses=None, allowed_numbers=None,
                               auth_username: str = "", auth_password: str = "", krisp: bool = True, ringing_timeout=None,
                               max_call_duration=None, encryption: str = "allow", metadata: str = "") -> dict:
    info = api.SIPInboundTrunkInfo(name=name, numbers=_list(numbers), allowed_addresses=_list(allowed_addresses),
                                   allowed_numbers=_list(allowed_numbers), auth_username=auth_username or "",
                                   auth_password=auth_password or "", krisp_enabled=krisp, metadata=metadata or "",
                                   media_encryption=ENCRYPTION.get(encryption, ENCRYPTION["allow"]))
    if d := _dur(ringing_timeout):
        info.ringing_timeout.CopyFrom(d)
    if d := _dur(max_call_duration):
        info.max_call_duration.CopyFrom(d)
    async with client(cred) as lk:
        return to_dict(await lk.sip.create_inbound_trunk(api.CreateSIPInboundTrunkRequest(trunk=info)))


async def create_outbound_trunk(cred: dict, *, name: str, address: str, numbers, auth_username: str = "", auth_password: str = "",
                                transport: str = "auto", destination_country: str = "", encryption: str = "allow",
                                metadata: str = "") -> dict:
    info = api.SIPOutboundTrunkInfo(name=name, address=address, numbers=_list(numbers), auth_username=auth_username or "",
                                    auth_password=auth_password or "", transport=TRANSPORTS.get(transport, TRANSPORTS["auto"]),
                                    destination_country=destination_country or "", metadata=metadata or "",
                                    media_encryption=ENCRYPTION.get(encryption, ENCRYPTION["allow"]))
    async with client(cred) as lk:
        return to_dict(await lk.sip.create_outbound_trunk(api.CreateSIPOutboundTrunkRequest(trunk=info)))


async def delete_trunk(cred: dict, trunk_id: str) -> dict:
    async with client(cred) as lk:
        return to_dict(await lk.sip.delete_trunk(api.DeleteSIPTrunkRequest(sip_trunk_id=trunk_id)))


async def create_dispatch_rule(cred: dict, *, name: str, agent_name: str, workflow_id: str | None = None, rule_type: str = "individual",
                               room_prefix: str = "call-", room_name: str = "", pin: str = "", trunk_ids=None, inbound_numbers=None,
                               hide_phone_number: bool = False, krisp: bool = True, metadata: dict | None = None) -> dict:
    """Route inbound calls to an agent. 'individual' = a new room per caller (what voice agents want)."""
    if rule_type == "direct":
        rule = api.SIPDispatchRule(dispatch_rule_direct=api.SIPDispatchRuleDirect(room_name=room_name or "call", pin=pin or ""))
    elif rule_type == "callee":
        rule = api.SIPDispatchRule(dispatch_rule_callee=api.SIPDispatchRuleCallee(room_prefix=room_prefix or "call-", pin=pin or "",
                                                                                  randomize=True))
    else:
        rule = api.SIPDispatchRule(dispatch_rule_individual=api.SIPDispatchRuleIndividual(room_prefix=room_prefix or "call-", pin=pin or ""))
    agent_meta = json.dumps({**({"workflow_id": workflow_id} if workflow_id else {}), "direction": "inbound", **(metadata or {})})
    info = api.SIPDispatchRuleInfo(
        name=name, rule=rule, trunk_ids=_list(trunk_ids), inbound_numbers=_list(inbound_numbers), hide_phone_number=hide_phone_number,
        krisp_enabled=krisp,  # (only settable on the Info form, not the flat request)
        room_config=api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=agent_name or AGENT_NAME, metadata=agent_meta)]))
    async with client(cred) as lk:
        return to_dict(await lk.sip.create_dispatch_rule(api.CreateSIPDispatchRuleRequest(dispatch_rule=info)))


async def delete_dispatch_rule(cred: dict, rule_id: str) -> dict:
    async with client(cred) as lk:
        return to_dict(await lk.sip.delete_dispatch_rule(api.DeleteSIPDispatchRuleRequest(sip_dispatch_rule_id=rule_id)))


async def transfer_call(cred: dict, room: str, participant_identity: str, transfer_to: str, play_dialtone: bool = False) -> dict:
    to = transfer_to if transfer_to.startswith(("sip:", "tel:")) else f"tel:{transfer_to}"
    async with client(cred) as lk:
        return to_dict(await lk.sip.transfer_sip_participant(api.TransferSIPParticipantRequest(
            room_name=room, participant_identity=participant_identity, transfer_to=to, play_dialtone=play_dialtone)))


async def hangup(cred: dict, room: str) -> dict:
    """End a call: deleting the room disconnects the phone line and the agent."""
    async with client(cred) as lk:
        await lk.room.delete_room(api.DeleteRoomRequest(room=room))
    return {"room": room, "ended": True}


async def list_participants(cred: dict, room: str) -> list[dict]:
    async with client(cred) as lk:
        res = await lk.room.list_participants(api.ListParticipantsRequest(room=room))
    return [to_dict(p) for p in res.participants]


async def list_rooms(cred: dict) -> list[dict]:
    async with client(cred) as lk:
        res = await lk.room.list_rooms(api.ListRoomsRequest())
    return [to_dict(r) for r in res.rooms]


# ---------------------------------------------------------------------------
# Recording (room composite egress, audio only) to S3
# ---------------------------------------------------------------------------

def recording_request(room: str, aws: dict, bucket: str, path: str = "calls/{room}.ogg", dual_channel: bool = True):
    filepath = (path or "calls/{room}.ogg").replace("{room}", room).replace("{time}", time.strftime("%Y%m%d-%H%M%S"))
    ext = filepath.rsplit(".", 1)[-1].lower()
    ftype = {"mp3": api.EncodedFileType.MP3, "mp4": api.EncodedFileType.MP4}.get(ext, api.EncodedFileType.OGG)
    out = api.EncodedFileOutput(file_type=ftype, filepath=filepath, s3=api.S3Upload(
        access_key=aws.get("access_key_id", ""), secret=aws.get("secret_access_key", ""), session_token=aws.get("session_token") or "",
        region=aws.get("region") or "us-east-1", bucket=bucket))
    return api.RoomCompositeEgressRequest(
        room_name=room, audio_only=True, file_outputs=[out],
        audio_mixing=api.AudioMixing.DUAL_CHANNEL_AGENT if dual_channel else api.AudioMixing.DEFAULT_MIXING), filepath


async def start_recording(cred: dict, room: str, aws: dict, bucket: str, path: str = "calls/{room}.ogg", dual_channel: bool = True) -> dict:
    req, filepath = recording_request(room, aws, bucket, path, dual_channel)
    async with client(cred) as lk:
        info = await lk.egress.start_room_composite_egress(req)
    return {"egress_id": info.egress_id, "file": f"s3://{bucket}/{filepath}", "status": api.EgressStatus.Name(info.status)}


async def stop_recording(cred: dict, egress_id: str) -> dict:
    async with client(cred) as lk:
        return to_dict(await lk.egress.stop_egress(api.StopEgressRequest(egress_id=egress_id)))
