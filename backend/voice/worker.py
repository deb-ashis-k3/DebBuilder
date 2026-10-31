"""DebBuilder LiveKit voice worker.

Joins rooms dispatched to its agent name, runs the audio pipeline (noise cancellation, VAD, STT,
turn detection, interruptions, TTS, background audio) and hands every caller turn to the workflow
(POST /api/voice/turn). The workflow's reply is spoken; call actions it queues (hang up, transfer,
DTMF, say, hold) run once the reply has been spoken. Call events (started, ended + transcript,
voicemail, keypad, transfer) go to POST /api/voice/event → the workflow's "Voice Call Events" trigger.

Run:   uv run --extra voice python voice/worker.py dev        (or: start)
Env:   LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET  (same project as the workflow's LiveKit credential)
       AGENT_NAME (default "debbuilder-voice": must match the Voice Agent trigger's agent name)
       DEBBUILDER_API (default http://localhost:8000), INTERNAL_TOKEN (from backend/.env)
       DEFAULT_VOICE_WORKFLOW_ID (optional fallback for calls nothing routes)
       VOICE_WORKER_PORT / VOICE_IDLE_PROCESSES (health-check port, warm call processes)
You don't need to run it yourself: with VOICE_WORKERS=auto (default) the backend starts one worker per
LiveKit credential + agent name of every active workflow (app/voice_workers.py). Run it by hand when
VOICE_WORKERS=off, e.g. on separate machines.
Routing: dispatch metadata {"workflow_id"} (dispatch rules / outbound calls / web sessions created by DebBuilder),
else the active workflow whose Voice Agent trigger has this agent name and lists the dialled number.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from livekit import api, rtc
from livekit.agents import AMD, Agent, AgentSession, JobContext, JobProcess, WorkerOptions, cli, llm
from livekit.agents.voice.room_io import AudioInputOptions, RoomOptions
from livekit.plugins import openai

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pipeline as P  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

AGENT_NAME = os.environ.get("AGENT_NAME", "debbuilder-voice")
API = os.environ.get("DEBBUILDER_API", "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("INTERNAL_TOKEN", "")
HEADERS = {"X-Internal-Token": TOKEN}
log = logging.getLogger("debbuilder.voice")


class WorkflowLLM(llm.LLM):
    """Placeholder so AgentSession runs the reply pipeline; replies come from WorkflowAgent.llm_node."""

    def chat(self, *args, **kwargs):  # pragma: no cover - never called
        raise NotImplementedError("WorkflowAgent.llm_node handles generation")


class Call:
    """Everything one call needs: settings, routing, transcript, queued actions."""

    def __init__(self, ctx: JobContext, cfg: dict, workflow_id: str, meta: dict, participant: rtc.RemoteParticipant):
        self.ctx, self.cfg, self.workflow_id, self.meta, self.participant = ctx, cfg, workflow_id, meta, participant
        self.session_id = meta.get("session_id") or ctx.room.name
        self.started = time.time()
        self.transcript: list[dict] = []
        self.actions: list[dict] = []
        self.turn_extra: dict = {}
        self.away_count = 0
        self.usage = None
        self.recording: dict | None = None
        self.session: AgentSession | None = None
        self.bg = None
        self.hold_handle = None
        self.ending = False
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10))

    @property
    def is_phone(self) -> bool:
        return self.participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP

    def base_meta(self) -> dict:
        return {k: v for k, v in self.meta.items() if k not in ("workflow_id", "session_id")}

    def event(self, name: str, data: dict | None = None) -> None:
        async def send():
            try:
                await self.http.post(f"{API}/api/voice/event", headers=HEADERS, json={
                    "workflow_id": self.workflow_id, "session_id": self.session_id, "event": name,
                    "data": {**self.base_meta(), "room": self.ctx.room.name, **(data or {})}})
            except Exception as e:  # noqa: BLE001
                log.warning("event %s not delivered: %s", name, e)
        asyncio.create_task(send())

    # ---- call actions (from the workflow) ---------------------------------
    async def run_actions(self) -> None:
        actions, self.actions = self.actions, []
        for a in actions:
            try:
                await self.run_action(a)
            except Exception as e:  # noqa: BLE001
                log.exception("action %s failed", a)
                self.event("error", {"action": a, "error": str(e)})

    async def say(self, text: str, interruptible: bool = False) -> None:
        if text and self.session:
            await self.session.say(text, allow_interruptions=interruptible).wait_for_playout()

    async def run_action(self, a: dict) -> None:
        kind = a.get("type")
        if kind == "hangup":
            await self.say(a.get("message") or "")
            await self.hangup("workflow")
        elif kind == "transfer":
            await self.say(a.get("message") or "")
            if not self.is_phone:
                await self.say("Transfers only work on phone calls.")
                return
            to = P.transfer_uri(a["to"])
            self.event("transfer", {"to": to})
            await self.ctx.api.sip.transfer_sip_participant(api.TransferSIPParticipantRequest(
                room_name=self.ctx.room.name, participant_identity=self.participant.identity, transfer_to=to,
                play_dialtone=bool(a.get("play_dialtone"))))
            self.ctx.shutdown("transferred")
        elif kind == "dtmf":
            for d in str(a.get("digits") or ""):
                if d in P.DTMF_CODES:
                    await self.ctx.room.local_participant.publish_dtmf(code=P.DTMF_CODES[d], digit=d)
                    await asyncio.sleep(0.15)
        elif kind == "say":
            await self.say(a.get("text") or "", bool(a.get("interruptible", True)))
        elif kind == "hold" and self.bg and not self.hold_handle:
            self.hold_handle = self.bg.play(P.hold_music(), loop=True)
        elif kind == "unhold" and self.hold_handle:
            self.hold_handle.stop()
            self.hold_handle = None

    async def hangup(self, reason: str) -> None:
        if self.ending:
            return
        self.ending = True
        try:
            await self.ctx.delete_room()  # disconnects the phone line too
        finally:
            self.ctx.shutdown(reason)

    # ---- keypad input -------------------------------------------------------
    def setup_dtmf(self) -> None:
        if self.cfg.get("dtmf_input", "forward") == "off":
            return
        term = str(self.cfg.get("dtmf_terminator") or "#")
        wait = P.num(self.cfg, "dtmf_timeout", 3.0)
        buf: list[str] = []
        timer: dict = {"task": None}

        def flush():
            digits = "".join(buf)
            buf.clear()
            if not digits or not self.session:
                return
            self.event("dtmf", {"digits": digits})
            self.turn_extra = {"input_type": "dtmf", "dtmf": digits}
            self.session.generate_reply(user_input=f"(Keypad: {digits})")

        async def flush_later():
            await asyncio.sleep(wait)
            flush()

        def on_dtmf(ev: rtc.SipDTMF):
            if timer["task"]:
                timer["task"].cancel()
            if ev.digit == term:
                flush()
                return
            buf.append(ev.digit)
            timer["task"] = asyncio.create_task(flush_later())

        self.ctx.room.on("sip_dtmf_received", on_dtmf)


class WorkflowAgent(Agent):
    def __init__(self, call: Call):
        super().__init__(instructions="Replies are produced by a DebBuilder workflow.")
        self.call = call

    async def llm_node(self, chat_ctx: llm.ChatContext, tools, model_settings):
        call = self.call
        text = ""
        for item in reversed(chat_ctx.items):
            if getattr(item, "type", None) == "message" and item.role == "user":
                text = item.text_content or ""
                break
        extra, call.turn_extra = call.turn_extra, {}
        if not text.strip():
            return
        body = {"workflow_id": call.workflow_id, "session_id": call.session_id, "text": text, "room": call.ctx.room.name,
                "metadata": {**call.base_meta(), "input_type": "speech", **extra}}
        fillers = P.filler_phrases(call.cfg)
        filler_after = P.num(call.cfg, "filler_after", 0) or 0
        try:
            async with call.http.stream("POST", f"{API}/api/voice/turn", json=body, headers=HEADERS) as r:
                if r.status_code != 200:
                    log.error("turn failed: %s %s", r.status_code, await r.aread())
                    yield "Sorry, I'm having trouble right now."
                    return
                lines = r.aiter_lines()
                nxt = asyncio.ensure_future(anext(lines, None))
                if filler_after > 0 and fillers:
                    done, _ = await asyncio.wait({nxt}, timeout=filler_after)
                    if not done:  # the workflow is slow (tools, lookups): keep the caller company
                        yield random.choice(fillers) + " "
                while True:
                    raw = await nxt
                    if raw is None:
                        break
                    nxt = asyncio.ensure_future(anext(lines, None))
                    if not raw.strip():
                        continue
                    msg = json.loads(raw)
                    if msg.get("type") == "text" and msg.get("text"):
                        yield msg["text"]
                    elif msg.get("type") == "action":
                        call.actions.append(msg["action"])
        except httpx.HTTPError as e:
            log.error("turn request failed: %s", e)
            yield "Sorry, I'm having trouble right now."


async def resolve_workflow(http: httpx.AsyncClient, meta: dict, dialled: str) -> str | None:
    if meta.get("workflow_id"):
        return meta["workflow_id"]
    r = await http.get(f"{API}/api/voice/resolve", headers=HEADERS, params={"agent_name": AGENT_NAME, "number": dialled})
    if r.status_code == 200:
        return r.json()["workflow_id"]
    return os.environ.get("DEFAULT_VOICE_WORKFLOW_ID")


def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = P.build_vad({})


async def entrypoint(ctx: JobContext) -> None:
    meta = json.loads(ctx.job.metadata or "{}")
    await ctx.connect()
    participant = await ctx.wait_for_participant()
    attrs = dict(participant.attributes or {})
    is_phone = participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
    if attrs.get("sip.phoneNumber"):
        meta.setdefault("phone", attrs["sip.phoneNumber"])
    if attrs.get("sip.trunkPhoneNumber"):
        meta.setdefault("dialed_number", attrs["sip.trunkPhoneNumber"])
    meta.setdefault("direction", "inbound" if is_phone else "web")
    meta.setdefault("participant", participant.identity)
    meta.setdefault("agent_name", AGENT_NAME)

    async with httpx.AsyncClient(timeout=20) as http:
        workflow_id = await resolve_workflow(http, meta, meta.get("dialed_number", ""))
        if not workflow_id:
            log.error("no workflow for agent %s / number %s; hanging up", AGENT_NAME, meta.get("dialed_number"))
            await ctx.delete_room()
            return
        # draft: a test call from the editor runs the draft; phone calls run the published version
        r = await http.get(f"{API}/api/voice/config/{workflow_id}", headers=HEADERS,
                           params={"draft": "true"} if meta.get("draft") else None)
        r.raise_for_status()
        cfg = r.json()

    call = Call(ctx, cfg, workflow_id, meta, participant)
    defaults = P.vad_options({})
    vad = ctx.proc.userdata["vad"] if P.vad_options(cfg) == defaults else P.build_vad(cfg)
    away = P.num(cfg, "user_away_timeout", 15) or None
    session = AgentSession(stt=P.build_stt(cfg), tts=P.build_tts(cfg), llm=WorkflowLLM(), vad=vad, turn_handling=P.turn_handling(cfg),
                           user_away_timeout=away, tts_text_transforms=P.tts_transforms(cfg))
    call.session = session

    # ---- transcript, usage, silence handling, end of call -------------------
    @session.on("conversation_item_added")
    def _item(ev):
        item = ev.item
        if getattr(item, "type", None) == "message" and item.text_content:
            call.transcript.append({"role": "agent" if item.role == "assistant" else "caller", "text": item.text_content,
                                    "at": round(time.time() - call.started, 1)})

    @session.on("session_usage_updated")
    def _usage(ev):
        usage = getattr(ev, "usage", None)
        call.usage = usage.model_dump() if hasattr(usage, "model_dump") else str(usage)

    @session.on("agent_state_changed")
    def _state(ev):
        if ev.new_state in ("listening", "idle") and call.actions:
            asyncio.create_task(call.run_actions())

    @session.on("user_state_changed")
    def _user(ev):
        if ev.new_state == "speaking":
            call.away_count = 0
        elif ev.new_state == "away" and not call.ending:
            call.away_count += 1
            limit = int(P.num(cfg, "away_hangup_after", 2) or 0)
            if limit and call.away_count > limit:
                async def bye():
                    await call.say(cfg.get("goodbye_message") or "Goodbye!")
                    await call.hangup("caller silent")
                asyncio.create_task(bye())
            else:
                session.say(cfg.get("away_prompt") or "Are you still there?")

    @session.on("close")
    def _close(ev):
        call.event("call_ended", {"duration_s": round(time.time() - call.started, 1), "reason": str(getattr(ev, "reason", "")),
                                  "transcript": call.transcript, "usage": call.usage, "recording": call.recording})

    nc = P.noise_cancellation(cfg)
    room_options = RoomOptions(audio_input=AudioInputOptions(noise_cancellation=nc) if nc else True,
                               close_on_disconnect=True, delete_room_on_close=True)
    await session.start(agent=WorkflowAgent(call), room=ctx.room, room_options=room_options)

    call.bg = P.background_audio(cfg)
    try:
        await call.bg.start(room=ctx.room, agent_session=session)
    except Exception as e:  # noqa: BLE001
        log.warning("background audio unavailable: %s", e)
        call.bg = None
    call.setup_dtmf()
    call.event("call_started", {"attributes": attrs})

    # ---- recording ---------------------------------------------------------------
    rec = cfg.get("recording_credential") or {}
    if cfg.get("record_call") and rec and cfg.get("recording_bucket"):
        from app.services.livekit import recording_request

        await call.say(cfg.get("recording_consent") or "")
        try:
            req, filepath = recording_request(ctx.room.name, rec, cfg["recording_bucket"], cfg.get("recording_path") or "calls/{room}.ogg",
                                              bool(cfg.get("recording_dual_channel", True)))
            info = await ctx.api.egress.start_room_composite_egress(req)
            call.recording = {"egress_id": info.egress_id, "file": f"s3://{cfg['recording_bucket']}/{filepath}"}
        except Exception as e:  # noqa: BLE001
            log.warning("recording failed to start: %s", e)
            call.event("error", {"error": f"recording: {e}"})

    # ---- voicemail detection (outbound) ---------------------------------------
    amd_mode = cfg.get("amd") or "off"
    if amd_mode != "off" and meta.get("direction") == "outbound" and is_phone:
        amd_cred = cfg.get("amd_credential") or {}
        kw = {"llm": openai.LLM(model="gpt-4.1-mini", api_key=amd_cred["api_key"])} if amd_cred.get("api_key") else {}
        try:
            async with AMD(session, stt=P.build_stt(cfg), participant_identity=participant.identity,
                           interrupt_on_machine=amd_mode != "report", **kw) as detector:
                result = await detector.execute()
            call.event("amd", {"category": str(result.category.value), "reason": result.reason, "transcript": result.transcript})
            if result.is_machine and amd_mode == "hangup":
                await call.hangup("voicemail")
                return
            if result.is_machine and amd_mode == "message":
                await call.say(cfg.get("amd_message") or "")
                await call.hangup("voicemail message left")
                return
        except Exception as e:  # noqa: BLE001
            log.warning("voicemail detection failed: %s", e)

    if (cfg.get("greeting_mode") or "agent") == "agent" and cfg.get("greeting"):
        session.say(cfg["greeting"], allow_interruptions=bool(cfg.get("greeting_interruptible", True)))

    max_minutes = P.num(cfg, "max_call_minutes", 30) or 30

    async def hangup_later():
        await asyncio.sleep(max_minutes * 60)
        await call.say(cfg.get("goodbye_message") or "We've reached the maximum call length. Goodbye!")
        await call.hangup("max call length")

    asyncio.create_task(hangup_later())
    ctx.add_shutdown_callback(call.http.aclose)


def worker_options() -> WorkerOptions:
    opts = WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm, agent_name=AGENT_NAME)
    # Set by the backend when it runs the worker for you (several workers on one machine):
    # its own health-check port (0 = any free one) and how many warm call processes to keep.
    if os.environ.get("VOICE_WORKER_PORT"):
        opts.port = int(os.environ["VOICE_WORKER_PORT"])
    if os.environ.get("VOICE_IDLE_PROCESSES"):
        opts.num_idle_processes = int(os.environ["VOICE_IDLE_PROCESSES"])
    return opts


if __name__ == "__main__":
    cli.run_app(worker_options())
