"""Voice nodes powered by LiveKit Agents.

`trigger.voice` turns a workflow into a real-time voice agent: the LiveKit
worker (voice/worker.py) handles audio (VAD, STT, TTS, turn detection,
interruptions) and calls the workflow once per user turn with
{text, session_id, room, phone?}. Whatever the workflow outputs is spoken.
"""

from ..services import livekit as lk
from .base import NodeContext, NodeError, NodeType, prop, register

VOICE = dict(group="voice", color="#ef4444")

STT_PROVIDERS = [{"label": "OpenAI", "value": "openai"}, {"label": "Deepgram", "value": "deepgram"}]
TTS_PROVIDERS = [{"label": "OpenAI", "value": "openai"}, {"label": "Cartesia", "value": "cartesia"},
                 {"label": "ElevenLabs", "value": "elevenlabs"}, {"label": "Deepgram", "value": "deepgram"}]


async def _passthrough(ctx: NodeContext):
    return {"main": ctx.items or [{}]}


register(NodeType(
    type="trigger.voice", label="Voice Agent (LiveKit)", icon="mic",
    description="Real-time voice conversations (web, phone via SIP). Each user turn runs the workflow; its output is spoken.",
    properties=[
        prop("credential", "LiveKit credential", "credential", None, credential_types=["livekit"], required=True),
        prop("greeting", "Greeting", "text", "Hello! How can I help you today?", rows=2),
        prop("stt_provider", "Speech-to-text", "options", "openai", options=STT_PROVIDERS),
        prop("stt_credential", "STT credential", "credential", None, credential_types=["openai", "deepgram"]),
        prop("stt_model", "STT model", "string", "gpt-4o-transcribe", description="e.g. gpt-4o-transcribe, nova-3"),
        prop("language", "Language", "string", "en"),
        prop("tts_provider", "Text-to-speech", "options", "openai", options=TTS_PROVIDERS),
        prop("tts_credential", "TTS credential", "credential", None,
             credential_types=["openai", "cartesia", "elevenlabs", "deepgram"]),
        prop("tts_model", "TTS model", "string", "gpt-4o-mini-tts"),
        prop("tts_voice", "Voice", "string", "alloy", description="Provider voice id / name"),
        prop("allow_interruptions", "Allow barge-in", "boolean", True),
        prop("stream_tokens", "Speak tokens as they stream (lower latency)", "boolean", True),
        prop("max_call_minutes", "Max call length (minutes)", "number", 30),
    ],
    trigger=True, inputs=[], execute=_passthrough, **VOICE,
))


async def _call(ctx: NodeContext):
    out = []
    for i, item in enumerate(ctx.items or [{}]):
        _, cred = await ctx.credential("credential", i)
        p = ctx.params(i)
        wf = p.get("workflow_id") or ctx.run.meta["workflow_id"]
        if not p.get("phone"):
            raise NodeError("Phone number is empty")
        res = await lk.outbound_call(cred, wf, str(p["phone"]), p.get("sip_trunk_id") or None,
                                     p.get("from_number") or None, {"context": p.get("context") or {}},
                                     bool(p.get("wait_until_answered")))
        out.append({**item, "call": res})
    return {"main": out}


register(NodeType(
    type="voice.call", label="Make Phone Call (LiveKit SIP)", icon="phone-outgoing",
    description="Place an outbound phone call handled by a voice-agent workflow (like Vapi outbound calls).",
    properties=[
        prop("credential", "LiveKit credential", "credential", None, credential_types=["livekit"], required=True),
        prop("phone", "Phone number (E.164)", "string", "{{ $json.phone }}"),
        prop("workflow_id", "Voice workflow (empty = this one)", "workflow", None),
        prop("sip_trunk_id", "SIP trunk ID (override)", "string", ""),
        prop("from_number", "Caller ID (optional)", "string", ""),
        prop("context", "Context for the agent (JSON)", "json", ""),
        prop("wait_until_answered", "Wait until answered", "boolean", False),
    ],
    execute=_call, **VOICE,
))


async def _room(ctx: NodeContext):
    out = []
    for i, item in enumerate(ctx.items or [{}]):
        _, cred = await ctx.credential("credential", i)
        p = ctx.params(i)
        wf = p.get("workflow_id") or ctx.run.meta["workflow_id"]
        session = await lk.start_web_session(cred, wf, p.get("identity") or "guest")
        out.append({**item, **session})
    return {"main": out}


register(NodeType(
    type="voice.web_session", label="Create Voice Session (web)", icon="radio",
    description="Create a LiveKit room + token with a voice agent dispatched; hand the token to any LiveKit client.",
    properties=[
        prop("credential", "LiveKit credential", "credential", None, credential_types=["livekit"], required=True),
        prop("workflow_id", "Voice workflow (empty = this one)", "workflow", None),
        prop("identity", "Participant identity", "string", "guest"),
    ],
    execute=_room, **VOICE,
))
