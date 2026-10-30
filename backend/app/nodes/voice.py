"""Voice AI agents on LiveKit (phone via SIP / Plivo, or web).

The LiveKit worker (voice/worker.py) owns the audio: noise cancellation, VAD, STT, turn detection,
interruptions, TTS, background audio, DTMF, voicemail detection, recording. The workflow owns the
business logic: every caller turn runs it ({text, session_id, phone, …}) and its output is spoken
(an AI Agent node there = LangChain model + tools, run by LangGraph).

From inside the workflow the call is controlled with the Voice Call Action node or the Call Control
tool (hang up, transfer, DTMF, say): actions stream back to the worker with the reply and run once it
has been spoken. Call lifecycle events (started, ended + transcript, voicemail, DTMF…) start the
"Voice Call Events" trigger.
"""

import json

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import Field, create_model

from ..services import livekit as lk
from .base import NodeContext, NodeError, NodeType, prop, register

VOICE = dict(group="voice", color="#ef4444")
DEFAULT_AGENT = lk.AGENT_NAME


def section(name: str, label: str, **kw) -> dict:
    return prop(name, label, "section", None, **kw)


def _stt_for(*providers):
    return {"stt_provider": list(providers)}


STT_PROVIDERS = [{"label": "OpenAI (gpt-4o-transcribe)", "value": "openai"}, {"label": "Deepgram Nova", "value": "deepgram"},
                 {"label": "Deepgram Flux (built-in turn detection)", "value": "deepgram_flux"}]
TTS_PROVIDERS = [{"label": "OpenAI", "value": "openai"}, {"label": "Cartesia", "value": "cartesia"},
                 {"label": "ElevenLabs", "value": "elevenlabs"}, {"label": "Deepgram Aura", "value": "deepgram"}]

VOICE_AGENT_PROPS = [
    prop("credential", "LiveKit credential", "credential", None, credential_types=["livekit"], required=True),

    section("s_agent", "Agent"),
    prop("agent_name", "Agent name", "string", DEFAULT_AGENT,
         description="The LiveKit agent this workflow answers as. Dispatch rules and outbound calls name it; run a worker with "
                     "AGENT_NAME=<this> (the default worker serves 'debbuilder-voice')."),
    prop("phone_numbers", "Inbound phone numbers", "string", "", placeholder="+14155550100, +4930…",
         description="Calls to these numbers (via a dispatch rule to this agent) run this workflow. Empty = any number for this agent."),
    prop("greeting", "Greeting", "text", "Hello! How can I help you today?", rows=2),
    prop("greeting_mode", "Who speaks first", "options", "agent", options=[
        {"label": "Agent greets the caller", "value": "agent"},
        {"label": "Wait for the caller (good for outbound / voicemail)", "value": "user"}]),
    prop("greeting_interruptible", "Caller can interrupt the greeting", "boolean", True),

    section("s_stt", "Speech-to-text"),
    prop("stt_provider", "Provider", "options", "openai", options=STT_PROVIDERS),
    prop("stt_credential", "STT credential", "credential", None, credential_types=["openai", "deepgram"]),
    prop("stt_model", "Model", "string", "gpt-4o-transcribe", description="gpt-4o-transcribe, gpt-4o-mini-transcribe, nova-3, flux-general-en…"),
    prop("language", "Language", "string", "en", description="BCP-47, e.g. en, en-US, hi, es, de"),
    prop("detect_language", "Detect the caller's language", "boolean", False, show_if={"stt_provider": ["openai", "deepgram"]}),
    prop("keyterms", "Key terms to recognise", "string", "", placeholder="Acme, Zentrunk, order number",
         description="Names / jargon the recogniser should favour (comma separated)."),
    prop("stt_noise_reduction", "Input noise reduction", "options", "", show_if=_stt_for("openai"), options=[
        {"label": "Off", "value": ""}, {"label": "Near field (headset, phone)", "value": "near_field"},
        {"label": "Far field (speakerphone, room)", "value": "far_field"}]),
    prop("stt_smart_format", "Smart formatting (numbers, dates)", "boolean", True, show_if=_stt_for("deepgram")),
    prop("stt_profanity_filter", "Profanity filter", "boolean", False, show_if=_stt_for("deepgram")),

    section("s_tts", "Text-to-speech"),
    prop("tts_provider", "Provider", "options", "openai", options=TTS_PROVIDERS),
    prop("tts_credential", "TTS credential", "credential", None, credential_types=["openai", "cartesia", "elevenlabs", "deepgram"]),
    prop("tts_model", "Model", "string", "gpt-4o-mini-tts", description="gpt-4o-mini-tts, sonic-3, eleven_flash_v2_5, aura-2-thalia-en…"),
    prop("tts_voice", "Voice", "string", "alloy", description="OpenAI: alloy, ash, coral, sage…; Cartesia / ElevenLabs: voice id"),
    prop("tts_speed", "Speaking speed", "number", None, description="OpenAI 0.25–4, ElevenLabs 0.8–1.2, Cartesia sonic-3 0.6–2.0 (empty = default)"),
    prop("tts_instructions", "Voice style", "text", "", rows=2, show_if={"tts_provider": ["openai"]},
         placeholder="Warm, upbeat customer-service tone. Speak clearly."),
    prop("tts_emotion", "Emotion", "string", "", show_if={"tts_provider": ["cartesia"]}, placeholder="Content, Calm, Excited…"),
    prop("tts_stability", "Stability (0–1)", "number", 0.5, show_if={"tts_provider": ["elevenlabs"]}),
    prop("tts_similarity", "Similarity boost (0–1)", "number", 0.75, show_if={"tts_provider": ["elevenlabs"]}),
    prop("tts_style", "Style exaggeration (0–1)", "number", 0, show_if={"tts_provider": ["elevenlabs"]}),
    prop("filter_markdown", "Strip markdown / emoji before speaking", "boolean", True),

    section("s_turns", "Turn-taking"),
    prop("turn_detection", "End-of-turn detection", "options", "model", options=[
        {"label": "LiveKit turn detector model (recommended)", "value": "model"},
        {"label": "Silence only (VAD)", "value": "vad"},
        {"label": "Speech-to-text endpointing", "value": "stt"}]),
    prop("endpointing", "Endpointing", "options", "fixed", options=[
        {"label": "Fixed delays", "value": "fixed"}, {"label": "Dynamic (adapts to the caller's pace)", "value": "dynamic"}]),
    prop("min_endpointing_delay", "Min wait after speech (s)", "number", 0.5),
    prop("max_endpointing_delay", "Max wait when unsure (s)", "number", 3.0),
    prop("vad_activation_threshold", "Voice activity threshold (0–1)", "number", 0.5,
         description="Raise it in noisy places so background noise isn't taken as speech."),
    prop("vad_min_silence", "Silence that ends speech (s)", "number", 0.55),
    prop("vad_min_speech", "Shortest speech (s)", "number", 0.05),

    section("s_interrupt", "Interruptions (barge-in)"),
    prop("allow_interruptions", "Caller can interrupt the agent", "boolean", True),
    prop("interruption_mode", "Detection", "options", "adaptive", show_if={"allow_interruptions": [True]}, options=[
        {"label": "Adaptive (ignores 'mm-hm', coughs)", "value": "adaptive"}, {"label": "Any voice activity", "value": "vad"}]),
    prop("min_interruption_duration", "Min speech to interrupt (s)", "number", 0.5, show_if={"allow_interruptions": [True]}),
    prop("min_interruption_words", "Min words to interrupt", "number", 0, show_if={"allow_interruptions": [True]}),
    prop("resume_false_interruption", "Resume after a false interruption", "boolean", True, show_if={"allow_interruptions": [True]}),
    prop("false_interruption_timeout", "False interruption timeout (s)", "number", 2.0, show_if={"allow_interruptions": [True]}),

    section("s_audio", "Audio quality & atmosphere"),
    prop("noise_cancellation", "Noise cancellation", "options", "auto", options=[
        {"label": "Auto (telephony model for phone calls, BVC for web)", "value": "auto"},
        {"label": "Background voice cancellation (BVC)", "value": "bvc"},
        {"label": "BVC tuned for telephony", "value": "bvc_telephony"},
        {"label": "Noise only (NC)", "value": "nc"}, {"label": "Off", "value": "off"}],
         description="LiveKit enhanced noise cancellation (needs LiveKit Cloud). Removes background noise and other voices."),
    prop("ambient_sound", "Background ambience", "options", "none", options=[
        {"label": "None", "value": "none"}, {"label": "Office", "value": "office"}, {"label": "City", "value": "city"},
        {"label": "Forest", "value": "forest"}, {"label": "Crowded room", "value": "crowded_room"}]),
    prop("ambient_volume", "Ambience volume (0–1)", "number", 0.3, show_if={"ambient_sound": ["office", "city", "forest", "crowded_room"]}),
    prop("thinking_sound", "While thinking", "options", "keyboard", options=[
        {"label": "Silence", "value": "none"}, {"label": "Keyboard typing", "value": "keyboard"}, {"label": "Keyboard typing 2", "value": "keyboard2"}]),
    prop("thinking_volume", "Thinking sound volume (0–1)", "number", 0.4, show_if={"thinking_sound": ["keyboard", "keyboard2"]}),

    section("s_behaviour", "Conversation behaviour"),
    prop("stream_tokens", "Speak while the model is still writing (lower latency)", "boolean", True),
    prop("filler_after", "Say a filler if the workflow takes longer than (s)", "number", 2.5, description="0 = never"),
    prop("filler_phrases", "Filler phrases", "string", "One moment please.|Let me check that for you.", description="Separate with |"),
    prop("user_away_timeout", "Caller silent for (s) before checking in", "number", 15, description="0 = never"),
    prop("away_prompt", "Check-in phrase", "string", "Are you still there?"),
    prop("away_hangup_after", "Hang up after this many unanswered check-ins", "number", 2),
    prop("goodbye_message", "Goodbye (on automatic hang-up)", "string", "Thanks for calling. Goodbye!"),
    prop("max_call_minutes", "Max call length (minutes)", "number", 30),

    section("s_phone", "Phone features"),
    prop("dtmf_input", "Keypad (DTMF) input", "options", "forward", options=[
        {"label": "Send keypresses to the workflow", "value": "forward"}, {"label": "Ignore", "value": "off"}]),
    prop("dtmf_terminator", "Keypad entry ends with", "string", "#", show_if={"dtmf_input": ["forward"]}),
    prop("dtmf_timeout", "…or after a pause of (s)", "number", 3, show_if={"dtmf_input": ["forward"]}),
    prop("amd", "Voicemail detection (outbound calls)", "options", "off", options=[
        {"label": "Off", "value": "off"}, {"label": "Hang up on voicemail / IVR", "value": "hangup"},
        {"label": "Leave a voicemail message, then hang up", "value": "message"},
        {"label": "Only report it (Voice Call Events)", "value": "report"}]),
    prop("amd_message", "Voicemail message", "text", "Hi, this is Acme calling. Please call us back. Thanks!", rows=2,
         show_if={"amd": ["message"]}),
    prop("amd_credential", "Detection model credential (empty = LiveKit Cloud inference)", "credential", None,
         credential_types=["openai"], show_if={"amd": ["hangup", "message", "report"]}),

    section("s_recording", "Recording"),
    prop("record_call", "Record calls", "boolean", False),
    prop("recording_credential", "AWS S3 credential", "credential", None, credential_types=["aws"], show_if={"record_call": [True]}),
    prop("recording_bucket", "S3 bucket", "string", "", show_if={"record_call": [True]}),
    prop("recording_path", "File path", "string", "calls/{room}.ogg", show_if={"record_call": [True]},
         description="{room}, {time} placeholders; extension picks the format (.ogg, .mp3, .mp4)"),
    prop("recording_dual_channel", "Caller and agent on separate channels", "boolean", True, show_if={"record_call": [True]}),
    prop("recording_consent", "Announce recording", "string", "This call may be recorded for quality purposes.", show_if={"record_call": [True]}),
]


async def _passthrough(ctx: NodeContext):
    return {"main": ctx.items or [{}]}


register(NodeType(
    type="trigger.voice", label="Voice Agent (LiveKit)", icon="mic",
    description="Real-time voice agent for phone (SIP, Plivo) and web. Each caller turn runs the workflow with "
                "{text, session_id, phone, dialed_number, direction, dtmf?}; its output is spoken.",
    properties=VOICE_AGENT_PROPS, trigger=True, inputs=[], execute=_passthrough, **VOICE,
))

CALL_EVENTS = ["call_started", "call_ended", "amd", "dtmf", "transfer", "error"]
register(NodeType(
    type="trigger.voice_event", label="Voice Call Events", icon="phone-call",
    description="Runs on voice call events of this workflow's agent: call started, call ended (transcript, duration, reason, "
                "recording), voicemail detected, keypad input, transfers. Put post-call logic here (CRM, summaries, tickets).",
    properties=[prop("events", "Events", "string", ",".join(CALL_EVENTS),
                     description=f"Comma separated: {', '.join(CALL_EVENTS)}")],
    trigger=True, inputs=[], execute=_passthrough, **VOICE,
))

# ---------------------------------------------------------------------------
# In-call actions (hang up, transfer, DTMF, say): run by the worker after the reply is spoken
# ---------------------------------------------------------------------------

ACTIONS = [{"label": "Hang up", "value": "hangup"}, {"label": "Transfer the call", "value": "transfer"},
           {"label": "Send keypad tones (DTMF)", "value": "dtmf"}, {"label": "Say something", "value": "say"},
           {"label": "Play hold music", "value": "hold"}, {"label": "Stop hold music", "value": "unhold"}]


def voice_action(ctx: NodeContext, action: dict) -> dict:
    """Queue an action for the live call this execution is answering."""
    ctx.publish({"type": "voice_action", "action": action})
    return action


def _action_from(p: dict) -> dict:
    kind = p.get("action") or "hangup"
    if kind == "transfer":
        to = str(p.get("transfer_to") or "").strip()
        if not to:
            raise NodeError("Transfer needs a destination number or SIP URI")
        return {"type": "transfer", "to": to, "message": p.get("message") or "", "play_dialtone": bool(p.get("play_dialtone"))}
    if kind == "dtmf":
        digits = "".join(c for c in str(p.get("digits") or "") if c in "0123456789*#ABCD")
        if not digits:
            raise NodeError("No DTMF digits (0-9 * # A-D)")
        return {"type": "dtmf", "digits": digits}
    if kind == "say":
        return {"type": "say", "text": str(p.get("message") or ""), "interruptible": bool(p.get("interruptible", True))}
    if kind in ("hold", "unhold"):
        return {"type": kind}
    return {"type": "hangup", "message": p.get("message") or ""}


async def _voice_action(ctx: NodeContext):
    if ctx.run.mode != "voice":
        ctx.log("Voice Call Action only affects live calls (mode 'voice'); recorded, nothing sent.")
    out = []
    for i, item in enumerate(ctx.items or [{}]):
        action = _action_from(ctx.params(i))
        if ctx.run.mode == "voice":
            voice_action(ctx, action)
        # Empty reply: the action speaks its own message, so the caller's words aren't echoed back.
        out.append({**item, "voice_action": action, "output": ""})
    return {"main": out}


register(NodeType(
    type="voice.action", label="Voice Call Action", icon="phone-forwarded",
    description="Control the live call: hang up, transfer (cold, to a number or SIP URI), send DTMF, say something, hold music. "
                "Runs right after the agent finishes speaking this turn's reply.",
    properties=[
        prop("action", "Action", "options", "hangup", options=ACTIONS),
        prop("transfer_to", "Transfer to", "string", "", placeholder="+14155550100 or sip:agent@pbx.example.com",
             show_if={"action": ["transfer"]}),
        prop("play_dialtone", "Play dial tone while transferring", "boolean", False, show_if={"action": ["transfer"]}),
        prop("digits", "Digits", "string", "", placeholder="1234#", show_if={"action": ["dtmf"]}),
        prop("message", "Say first", "text", "", rows=2, show_if={"action": ["hangup", "transfer", "say"]}),
        prop("interruptible", "Caller can interrupt", "boolean", True, show_if={"action": ["say"]}),
    ],
    execute=_voice_action, **VOICE,
))


async def _call_control_tool(ctx: NodeContext) -> BaseTool:
    p = ctx.params()
    targets = {str(r.get("name") or "").strip().lower(): str(r.get("number") or "").strip()
               for r in (ctx.raw_params.get("transfer_targets") or []) if r.get("name") and r.get("number")}
    allow_any = bool(p.get("allow_any_number"))
    lines = [f"- {n}: {num}" for n, num in targets.items()]

    async def control(action: str, transfer_to: str = "", digits: str = "", message: str = "") -> str:
        action = action.lower().strip()
        if action == "transfer":
            dest = targets.get(transfer_to.strip().lower()) or (transfer_to if allow_any else "")
            if not dest:
                return f"Unknown transfer target '{transfer_to}'. Choose one of: {', '.join(targets) or 'none configured'}."
            voice_action(ctx, {"type": "transfer", "to": dest, "message": message, "play_dialtone": False})
            return f"Transfer to {transfer_to} queued; it happens after your reply. Tell the caller you're transferring them."
        if action in ("hangup", "end_call", "end"):
            voice_action(ctx, {"type": "hangup", "message": ""})
            return "The call will end after your reply. Say goodbye now."
        if action == "dtmf":
            voice_action(ctx, {"type": "dtmf", "digits": digits})
            return f"Sending keypad tones {digits} after your reply."
        return "Unknown action: use hangup, transfer or dtmf."

    name = (ctx.param("tool_name") or "call_control").strip()
    return StructuredTool.from_function(
        coroutine=control, name=name,
        description=(ctx.param("tool_description") or "Control the phone call: hang up when the conversation is finished, "
                     "transfer the caller to a person/department, or send keypad tones to an IVR.")
        + ("\nTransfer targets:\n" + "\n".join(lines) if lines else ""),
        args_schema=create_model(f"{name}_args",
                                 action=(str, Field(..., description="hangup | transfer | dtmf")),
                                 transfer_to=(str, Field("", description="Transfer target name (see list) or number")),
                                 digits=(str, Field("", description="DTMF digits for action dtmf")),
                                 message=(str, Field("", description="Optional words to say before transferring"))),
    )


register(NodeType(
    type="tool.voice_control", label="Call Control Tool", icon="phone-forwarded",
    description="Lets an AI Agent in a voice workflow end the call, transfer it (to named departments), or send DTMF.",
    properties=[
        prop("tool_name", "Tool name", "string", "call_control"),
        prop("tool_description", "Description (tells the AI when to use it)", "text", "", rows=2),
        prop("transfer_targets", "Transfer targets", "collection", [{"name": "sales", "number": ""}],
             fields=[prop("name", "Name (what the AI says)"), prop("number", "Number or SIP URI")]),
        prop("allow_any_number", "Allow transferring to any number the AI chooses", "boolean", False),
    ],
    provides="ai_tool", supply=_call_control_tool, group="ai_tool", color="#22c55e", inputs=[], outputs=[],
))

# ---------------------------------------------------------------------------
# Outbound calls and web sessions
# ---------------------------------------------------------------------------


async def _call(ctx: NodeContext):
    out = []
    for i, item in enumerate(ctx.items or [{}]):
        _, cred = await ctx.credential("credential", i)
        p = ctx.params(i)
        wf = p.get("workflow_id") or ctx.run.meta["workflow_id"]
        if not p.get("phone"):
            raise NodeError("Phone number is empty")
        context = p.get("context") or {}
        if isinstance(context, str):  # JSON typed with {{ }} inside renders as text
            try:
                context = json.loads(context)
            except ValueError:
                context = {"text": context}
        try:
            res = await lk.outbound_call(
                cred, wf, str(p["phone"]), p.get("sip_trunk_id") or None, p.get("from_number") or None,
                {"context": context}, bool(p.get("wait_until_answered")),
                agent_name=p.get("agent_name") or DEFAULT_AGENT, ringing_timeout=p.get("ringing_timeout"),
                max_call_duration=p.get("max_call_duration"), krisp=bool(p.get("krisp")),
                play_dialtone=bool(p.get("play_dialtone")), headers={r["name"]: str(r.get("value") or "") for r in p.get("headers") or [] if r.get("name")},
                display_name=p.get("display_name") or None)
        except Exception as e:  # noqa: BLE001
            raise NodeError(f"Call failed: {e}", ctx.name) from e
        out.append({**item, "call": res})
    return {"main": out}


register(NodeType(
    type="voice.call", label="Make Phone Call (LiveKit SIP)", icon="phone-outgoing",
    description="Place an outbound phone call handled by a voice-agent workflow (via a LiveKit outbound trunk, e.g. Plivo / Twilio).",
    properties=[
        prop("credential", "LiveKit credential", "credential", None, credential_types=["livekit"], required=True),
        prop("phone", "Phone number (E.164)", "string", "{{ $json.phone }}"),
        prop("workflow_id", "Voice workflow (empty = this one)", "workflow", None),
        prop("agent_name", "Agent name", "string", DEFAULT_AGENT),
        prop("sip_trunk_id", "Outbound trunk ID (empty = the credential's)", "string", ""),
        prop("from_number", "Caller ID", "string", ""),
        prop("display_name", "Caller display name", "string", ""),
        prop("context", "Context for the agent (JSON)", "json", ""),
        prop("ringing_timeout", "Ring for (s)", "number", 30),
        prop("max_call_duration", "Max call duration (s)", "number", None),
        prop("krisp", "Krisp noise cancellation on the line", "boolean", True),
        prop("play_dialtone", "Play dial tone in the room while ringing", "boolean", False),
        prop("headers", "SIP headers", "collection", [], fields=[prop("name", "Name"), prop("value", "Value", "string", "")]),
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
        session = await lk.start_web_session(cred, wf, p.get("identity") or "guest", agent_name=p.get("agent_name") or DEFAULT_AGENT)
        out.append({**item, **session})
    return {"main": out}


register(NodeType(
    type="voice.web_session", label="Create Voice Session (web)", icon="radio",
    description="Create a LiveKit room + token with a voice agent dispatched; hand the token to any LiveKit client.",
    properties=[
        prop("credential", "LiveKit credential", "credential", None, credential_types=["livekit"], required=True),
        prop("workflow_id", "Voice workflow (empty = this one)", "workflow", None),
        prop("agent_name", "Agent name", "string", DEFAULT_AGENT),
        prop("identity", "Participant identity", "string", "guest"),
    ],
    execute=_room, **VOICE,
))
