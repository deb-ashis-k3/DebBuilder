"""Telephony management as workflow nodes: LiveKit SIP (trunks, dispatch rules, calls, transfers,
recording) and Plivo (numbers, calls, applications, Zentrunk SIP trunks)."""

from ..services import livekit as lk
from .base import NodeContext, NodeError, NodeType, prop, register
from .declarative import F, Offset, op, service

VOICE = dict(group="voice", color="#ef4444")

LK_OPS = [
    {"label": "Inbound trunk: create", "value": "inbound.create"}, {"label": "Inbound trunk: list", "value": "inbound.list"},
    {"label": "Outbound trunk: create", "value": "outbound.create"}, {"label": "Outbound trunk: list", "value": "outbound.list"},
    {"label": "Trunk: delete", "value": "trunk.delete"},
    {"label": "Dispatch rule: create", "value": "rule.create"}, {"label": "Dispatch rule: list", "value": "rule.list"},
    {"label": "Dispatch rule: delete", "value": "rule.delete"},
    {"label": "Call: transfer (cold)", "value": "call.transfer"}, {"label": "Call: hang up", "value": "call.hangup"},
    {"label": "Recording: start", "value": "recording.start"}, {"label": "Recording: stop", "value": "recording.stop"},
    {"label": "Room: list", "value": "room.list"}, {"label": "Room: participants", "value": "room.participants"},
]


def _on(*ops):
    return {"operation": list(ops)}


async def _livekit_sip(ctx: NodeContext):
    out = []
    for i, item in enumerate(ctx.items or [{}]):
        _, cred = await ctx.credential("credential", i)
        p = ctx.params(i)
        o = p.get("operation") or "inbound.list"
        try:
            if o == "inbound.create":
                res = await lk.create_inbound_trunk(cred, name=p.get("name") or "Inbound", numbers=p.get("numbers"),
                                                    allowed_addresses=p.get("allowed_addresses"), allowed_numbers=p.get("allowed_numbers"),
                                                    auth_username=p.get("auth_username") or "", auth_password=p.get("auth_password") or "",
                                                    krisp=bool(p.get("krisp", True)), ringing_timeout=p.get("ringing_timeout"),
                                                    max_call_duration=p.get("max_call_duration"), encryption=p.get("encryption") or "allow")
            elif o == "outbound.create":
                res = await lk.create_outbound_trunk(cred, name=p.get("name") or "Outbound", address=p.get("address") or "",
                                                     numbers=p.get("numbers"), auth_username=p.get("auth_username") or "",
                                                     auth_password=p.get("auth_password") or "", transport=p.get("transport") or "auto",
                                                     destination_country=p.get("destination_country") or "", encryption=p.get("encryption") or "allow")
            elif o == "rule.create":
                res = await lk.create_dispatch_rule(cred, name=p.get("name") or "Rule", agent_name=p.get("agent_name") or lk.AGENT_NAME,
                                                    workflow_id=p.get("workflow_id") or None, rule_type=p.get("rule_type") or "individual",
                                                    room_prefix=p.get("room_prefix") or "call-", room_name=p.get("room_name") or "",
                                                    pin=p.get("pin") or "", trunk_ids=p.get("trunk_ids"), inbound_numbers=p.get("numbers"),
                                                    hide_phone_number=bool(p.get("hide_phone_number")), krisp=bool(p.get("krisp", True)))
            elif o in ("inbound.list", "outbound.list", "rule.list", "room.list"):
                fn = {"inbound.list": lk.list_inbound_trunks, "outbound.list": lk.list_outbound_trunks,
                      "rule.list": lk.list_dispatch_rules, "room.list": lk.list_rooms}[o]
                out += await fn(cred)
                continue
            elif o == "trunk.delete":
                res = await lk.delete_trunk(cred, str(p.get("trunk_id") or ""))
            elif o == "rule.delete":
                res = await lk.delete_dispatch_rule(cred, str(p.get("rule_id") or ""))
            elif o == "call.transfer":
                res = await lk.transfer_call(cred, str(p.get("room") or ""), str(p.get("participant_identity") or ""),
                                             str(p.get("transfer_to") or ""), bool(p.get("play_dialtone")))
            elif o == "call.hangup":
                res = await lk.hangup(cred, str(p.get("room") or ""))
            elif o == "recording.start":
                _, aws = await ctx.credential("aws_credential", i)
                res = await lk.start_recording(cred, str(p.get("room") or ""), aws, str(p.get("bucket") or ""),
                                               p.get("path") or "calls/{room}.ogg", bool(p.get("dual_channel", True)))
            elif o == "recording.stop":
                res = await lk.stop_recording(cred, str(p.get("egress_id") or ""))
            elif o == "room.participants":
                out += await lk.list_participants(cred, str(p.get("room") or ""))
                continue
            else:
                raise NodeError(f"Unknown operation {o}")
        except NodeError:
            raise
        except Exception as e:  # noqa: BLE001
            raise NodeError(f"LiveKit: {e}", ctx.name) from e
        out.append(res if isinstance(res, dict) else {"result": res})
    return {"main": out}


register(NodeType(
    type="voice.livekit_sip", label="LiveKit Telephony", icon="phone",
    description="Manage LiveKit SIP: inbound/outbound trunks, dispatch rules (route calls to an agent), transfers, hang-up, "
                "call recording to S3, rooms.",
    properties=[
        prop("credential", "LiveKit credential", "credential", None, credential_types=["livekit"], required=True),
        prop("operation", "Operation", "options", "inbound.list", options=LK_OPS),
        prop("name", "Name", "string", "", show_if=_on("inbound.create", "outbound.create", "rule.create")),
        prop("numbers", "Phone numbers (comma separated)", "string", "", show_if=_on("inbound.create", "outbound.create", "rule.create")),
        prop("address", "Carrier SIP address", "string", "", placeholder="xxxx.zt.plivo.com / example.pstn.twilio.com",
             show_if=_on("outbound.create")),
        prop("transport", "Transport", "options", "auto", options=["auto", "udp", "tcp", "tls"], show_if=_on("outbound.create")),
        prop("destination_country", "Destination country (ISO, for region pinning)", "string", "", show_if=_on("outbound.create")),
        prop("allowed_addresses", "Allowed carrier IPs / CIDRs", "string", "", show_if=_on("inbound.create")),
        prop("allowed_numbers", "Allowed caller numbers", "string", "", show_if=_on("inbound.create")),
        prop("auth_username", "Auth username", "string", "", show_if=_on("inbound.create", "outbound.create")),
        prop("auth_password", "Auth password", "string", "", show_if=_on("inbound.create", "outbound.create")),
        prop("encryption", "Media encryption (SRTP)", "options", "allow", options=["disable", "allow", "require"],
             show_if=_on("inbound.create", "outbound.create")),
        prop("ringing_timeout", "Ringing timeout (s)", "number", None, show_if=_on("inbound.create")),
        prop("max_call_duration", "Max call duration (s)", "number", None, show_if=_on("inbound.create")),
        prop("krisp", "Krisp noise cancellation", "boolean", True, show_if=_on("inbound.create", "rule.create")),
        prop("agent_name", "Agent name", "string", lk.AGENT_NAME, show_if=_on("rule.create")),
        prop("workflow_id", "Voice workflow (empty = routed by agent name + number)", "workflow", None, show_if=_on("rule.create")),
        prop("rule_type", "Rooms", "options", "individual", show_if=_on("rule.create"), options=[
            {"label": "New room per caller (agents)", "value": "individual"}, {"label": "Room per dialled number", "value": "callee"},
            {"label": "One shared room", "value": "direct"}]),
        prop("room_prefix", "Room prefix", "string", "call-", show_if=_on("rule.create")),
        prop("room_name", "Room name", "string", "", show_if={"rule_type": ["direct"]}),
        prop("pin", "PIN", "string", "", show_if=_on("rule.create")),
        prop("trunk_ids", "Only for trunk ids", "string", "", show_if=_on("rule.create")),
        prop("hide_phone_number", "Hide caller number from the room", "boolean", False, show_if=_on("rule.create")),
        prop("trunk_id", "Trunk id", "string", "", show_if=_on("trunk.delete")),
        prop("rule_id", "Dispatch rule id", "string", "", show_if=_on("rule.delete")),
        prop("room", "Room", "string", "{{ $json.room }}", show_if=_on("call.transfer", "call.hangup", "recording.start", "room.participants")),
        prop("participant_identity", "Phone participant identity", "string", "{{ $json.participant }}", show_if=_on("call.transfer")),
        prop("transfer_to", "Transfer to", "string", "", placeholder="+14155550100 or sip:desk@pbx.example.com", show_if=_on("call.transfer")),
        prop("play_dialtone", "Play dial tone", "boolean", False, show_if=_on("call.transfer")),
        prop("aws_credential", "AWS S3 credential", "credential", None, credential_types=["aws"], show_if=_on("recording.start")),
        prop("bucket", "S3 bucket", "string", "", show_if=_on("recording.start")),
        prop("path", "File path", "string", "calls/{room}.ogg", show_if=_on("recording.start")),
        prop("dual_channel", "Caller / agent on separate channels", "boolean", True, show_if=_on("recording.start")),
        prop("egress_id", "Egress (recording) id", "string", "", show_if=_on("recording.stop")),
    ],
    execute=_livekit_sip, **VOICE,
))

# ---- Plivo (REST: numbers, calls, applications, endpoints, recordings, Zentrunk SIP trunking) --------

PG = Offset(param="offset", size_param="limit", size=20)
CALL = F("call_uuid", req=True)
service("plivo", "Plivo", ["plivo"], "https://api.plivo.com/v1/Account/{auth_id}", color="#43b049", icon="phone", group="voice", ops=[
    op("number", "search", "Search numbers to buy", "GET", "/PhoneNumber/", F("country_iso", req=True, default="US"),
       F("type", type="options", options=["", "local", "tollfree", "mobile", "national", "fixed"]), F("pattern"), F("region"),
       F("services", desc="voice, sms…"), items="objects", paginate=PG),
    op("number", "buy", "Buy number", "POST", "/PhoneNumber/{number}/", F("number", req=True), F("app_id", desc="Application or Zentrunk inbound trunk id")),
    op("number", "list", "List my numbers", "GET", "/Number/", items="objects", paginate=PG),
    op("number", "update", "Route number (app / trunk)", "POST", "/Number/{number}/", F("number", req=True),
       F("app_id", "Application or inbound trunk id", req=True), F("alias")),
    op("number", "delete", "Release number", "DELETE", "/Number/{number}/", F("number", req=True)),
    op("call", "create", "Make call", "POST", "/Call/", F("from", req=True), F("to", req=True), F("answer_url", req=True),
       F("answer_method", default="POST"), F("hangup_url"), F("ring_timeout", type="number"), F("time_limit", type="number"),
       F("machine_detection", type="options", options=["", "true", "hangup"]), F("caller_name"), F("sip_headers", desc="k1=v1,k2=v2")),
    op("call", "get", "Get call (CDR)", "GET", "/Call/{call_uuid}/", CALL),
    op("call", "list", "List calls", "GET", "/Call/", F("call_direction", type="options", options=["", "inbound", "outbound"]),
       F("from_number"), F("to_number"), items="objects", paginate=PG),
    op("call", "live", "List live calls", "GET", "/Call/", F("status", default="live", where="query")),
    op("call", "hangup", "Hang up live call", "DELETE", "/Call/{call_uuid}/", CALL),
    op("call", "transfer", "Transfer live call", "POST", "/Call/{call_uuid}/", CALL, F("legs", type="options", options=["aleg", "bleg", "both"], default="aleg"),
       F("aleg_url", "New answer URL (A leg)")),
    op("call", "speak", "Speak text into call", "POST", "/Call/{call_uuid}/Speak/", CALL, F("text", type="text", req=True),
       F("voice", default="WOMAN"), F("language", default="en-US"), F("legs", default="aleg")),
    op("call", "play", "Play audio into call", "POST", "/Call/{call_uuid}/Play/", CALL, F("urls", "Audio URLs", req=True), F("loop", type="boolean")),
    op("call", "record", "Start recording", "POST", "/Call/{call_uuid}/Record/", CALL, F("time_limit", type="number"),
       F("file_format", type="options", options=["mp3", "wav"], default="mp3")),
    op("call", "record_stop", "Stop recording", "DELETE", "/Call/{call_uuid}/Record/", CALL),
    op("call", "dtmf", "Send DTMF", "POST", "/Call/{call_uuid}/DTMF/", CALL, F("digits", req=True), F("leg", default="aleg")),
    op("application", "create", "Create application", "POST", "/Application/", F("app_name", req=True), F("answer_url", req=True),
       F("answer_method", default="POST"), F("hangup_url")),
    op("application", "list", "List applications", "GET", "/Application/", items="objects", paginate=PG),
    op("application", "delete", "Delete application", "DELETE", "/Application/{app_id}/", F("app_id", req=True)),
    op("endpoint", "create", "Create SIP endpoint", "POST", "/Endpoint/", F("username", req=True), F("password", req=True), F("alias", req=True),
       F("app_id")),
    op("endpoint", "list", "List SIP endpoints", "GET", "/Endpoint/", items="objects", paginate=PG),
    op("recording", "list", "List recordings", "GET", "/Recording/", F("call_uuid"), items="objects", paginate=PG),
    op("zentrunk", "trunk_create", "SIP trunk: create", "POST", "/Zentrunk/Trunk/", F("name", req=True),
       F("trunk_direction", type="options", options=["inbound", "outbound"], default="inbound", req=True),
       F("primary_uri_uuid", "Origination URI id (inbound)"), F("credential_uuid", "Credential id (outbound)"),
       F("ipacl_uuid", "IP access list id (outbound)"), F("secure", type="boolean")),
    op("zentrunk", "trunk_list", "SIP trunk: list", "GET", "/Zentrunk/Trunk/", items="objects"),
    op("zentrunk", "trunk_get", "SIP trunk: get", "GET", "/Zentrunk/Trunk/{trunk_id}/", F("trunk_id", req=True)),
    op("zentrunk", "trunk_delete", "SIP trunk: delete", "DELETE", "/Zentrunk/Trunk/{trunk_id}/", F("trunk_id", req=True)),
    op("zentrunk", "uri_create", "Origination URI: create", "POST", "/Zentrunk/URI/", F("name", req=True),
       F("uri", req=True, desc="e.g. xxxx.sip.livekit.cloud")),
    op("zentrunk", "credential_create", "Trunk credential: create", "POST", "/Zentrunk/Credential/", F("name", req=True),
       F("username", req=True), F("password", req=True)),
    op("zentrunk", "ipacl_create", "IP access list: create", "POST", "/Zentrunk/IPAccessControlList/", F("name", req=True),
       F("ip_addresses", type="list", req=True)),
])
