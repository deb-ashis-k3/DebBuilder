"""DebBuilder LiveKit voice worker.

Joins rooms it is dispatched to (agent name "debbuilder-voice"), handles the
audio pipeline (Silero VAD, STT, TTS, interruptions) and delegates every user
turn to the workflow named in the dispatch metadata via POST /api/voice/turn.

Run:   uv run --extra voice python voice/worker.py dev
Env:   LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET (same project as the
       LiveKit credential used in your workflows), DEBBUILDER_API (default
       http://localhost:8000), INTERNAL_TOKEN (from backend/.env).
For inbound phone calls, set the SIP dispatch rule's agent to
"debbuilder-voice" with metadata {"workflow_id": "<id>"}, or set
DEFAULT_VOICE_WORKFLOW_ID.
"""

import asyncio
import json
import logging
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from livekit.agents import Agent, AgentSession, JobContext, JobProcess, WorkerOptions, cli, llm
from livekit.plugins import cartesia, deepgram, elevenlabs, openai, silero

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

AGENT_NAME = "debbuilder-voice"
API = os.environ.get("DEBBUILDER_API", "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("INTERNAL_TOKEN", "")
log = logging.getLogger("debbuilder.voice")


class WorkflowLLM(llm.LLM):
    """Placeholder so AgentSession runs the reply pipeline; replies come from llm_node."""

    def chat(self, *args, **kwargs):  # pragma: no cover - never called
        raise NotImplementedError("WorkflowAgent.llm_node handles generation")


class WorkflowAgent(Agent):
    def __init__(self, workflow_id: str, session_id: str, room: str, metadata: dict):
        super().__init__(instructions="Replies are produced by a DebBuilder workflow.")
        self.workflow_id = workflow_id
        self.session_id = session_id
        self.room_name = room
        self.metadata = metadata

    async def llm_node(self, chat_ctx: llm.ChatContext, tools, model_settings):
        text = ""
        for item in reversed(chat_ctx.items):
            if getattr(item, "type", None) == "message" and item.role == "user":
                text = item.text_content or ""
                break
        if not text.strip():
            return
        body = {"workflow_id": self.workflow_id, "session_id": self.session_id, "text": text,
                "room": self.room_name, "metadata": self.metadata}
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10)) as c:
            async with c.stream("POST", f"{API}/api/voice/turn", json=body,
                                headers={"X-Internal-Token": TOKEN}) as r:
                if r.status_code != 200:
                    log.error("turn failed: %s %s", r.status_code, await r.aread())
                    yield "Sorry, I'm having trouble right now."
                    return
                async for chunk in r.aiter_text():
                    if chunk:
                        yield chunk


def _key(cfg: dict, field: str) -> dict:
    return cfg.get(field) or {}


def build_stt(cfg: dict):
    cred = _key(cfg, "stt_credential")
    lang = cfg.get("language") or "en"
    model = cfg.get("stt_model")
    if cfg.get("stt_provider") == "deepgram":
        return deepgram.STT(model=model or "nova-3", language=lang, api_key=cred.get("api_key") or os.environ.get("DEEPGRAM_API_KEY"))
    kw = {"api_key": cred.get("api_key") or os.environ.get("OPENAI_API_KEY")}
    if cred.get("base_url"):
        kw["base_url"] = cred["base_url"]
    return openai.STT(model=model or "gpt-4o-transcribe", language=lang, **kw)


def build_tts(cfg: dict):
    cred = _key(cfg, "tts_credential")
    provider = cfg.get("tts_provider") or "openai"
    voice = cfg.get("tts_voice")
    model = cfg.get("tts_model")
    if provider == "cartesia":
        return cartesia.TTS(api_key=cred.get("api_key"), **({"voice": voice} if voice else {}), **({"model": model} if model else {}))
    if provider == "elevenlabs":
        return elevenlabs.TTS(api_key=cred.get("api_key"), **({"voice_id": voice} if voice else {}), **({"model": model} if model else {}))
    if provider == "deepgram":
        return deepgram.TTS(api_key=cred.get("api_key"), model=model or "aura-2-thalia-en")
    kw = {"api_key": cred.get("api_key") or os.environ.get("OPENAI_API_KEY")}
    if cred.get("base_url"):
        kw["base_url"] = cred["base_url"]
    return openai.TTS(model=model or "gpt-4o-mini-tts", voice=voice or "alloy", **kw)


def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext) -> None:
    meta = json.loads(ctx.job.metadata or "{}")
    workflow_id = meta.get("workflow_id") or os.environ.get("DEFAULT_VOICE_WORKFLOW_ID")
    if not workflow_id:
        log.error("dispatch has no workflow_id; leaving room %s", ctx.room.name)
        return
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{API}/api/voice/config/{workflow_id}", headers={"X-Internal-Token": TOKEN})
        r.raise_for_status()
        cfg = r.json()

    await ctx.connect()
    participant = await ctx.wait_for_participant()
    attrs = dict(participant.attributes or {})
    if attrs.get("sip.phoneNumber"):
        meta.setdefault("phone", attrs["sip.phoneNumber"])
    meta.setdefault("participant", participant.identity)

    session = AgentSession(
        stt=build_stt(cfg),
        tts=build_tts(cfg),
        llm=WorkflowLLM(),
        vad=ctx.proc.userdata["vad"],
        allow_interruptions=bool(cfg.get("allow_interruptions", True)),
    )
    agent = WorkflowAgent(workflow_id, meta.get("session_id") or ctx.room.name, ctx.room.name,
                          {k: v for k, v in meta.items() if k not in ("workflow_id", "session_id")})
    await session.start(agent=agent, room=ctx.room)
    if cfg.get("greeting"):
        await session.say(cfg["greeting"], allow_interruptions=True)

    max_minutes = float(cfg.get("max_call_minutes") or 30)

    async def hangup_later():
        await asyncio.sleep(max_minutes * 60)
        await session.say("We've reached the maximum call length. Goodbye!")
        ctx.shutdown("max call length")

    asyncio.create_task(hangup_later())


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm, agent_name=AGENT_NAME))
