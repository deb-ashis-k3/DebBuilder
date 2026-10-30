"""Builds the LiveKit Agents pipeline from a workflow's Voice Agent settings (GET /api/voice/config).

Pure functions so they can be unit-tested without a call: STT, TTS, VAD, turn handling, noise
cancellation, background audio. Parameter names match livekit-agents 1.8 and its plugins.
"""

from __future__ import annotations

import logging
import os

from livekit import rtc
from livekit.agents import AudioConfig, BackgroundAudioPlayer, BuiltinAudioClip, inference
from livekit.plugins import cartesia, deepgram, elevenlabs, openai, silero

log = logging.getLogger("debbuilder.voice")

TTS_DEFAULT_MODEL = {"openai": "gpt-4o-mini-tts", "cartesia": "sonic-3", "elevenlabs": "eleven_flash_v2_5", "deepgram": "aura-2-thalia-en"}
TTS_MODEL_PREFIX = {"openai": ("gpt-", "tts-"), "cartesia": ("sonic",), "elevenlabs": ("eleven",), "deepgram": ("aura",)}
STT_DEFAULT_MODEL = {"openai": "gpt-4o-transcribe", "deepgram": "nova-3", "deepgram_flux": "flux-general-en"}
STT_MODEL_PREFIX = {"openai": ("gpt-", "whisper"), "deepgram": ("nova", "enhanced", "base"), "deepgram_flux": ("flux",)}
DTMF_CODES = {**{str(d): d for d in range(10)}, "*": 10, "#": 11, "A": 12, "B": 13, "C": 14, "D": 15}


def num(cfg: dict, key: str, default: float | None = None) -> float | None:
    v = cfg.get(key)
    if v in (None, ""):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def is_cloud(url: str | None = None) -> bool:
    """Noise cancellation, adaptive interruptions and hosted inference need LiveKit Cloud."""
    return ".livekit.cloud" in (url or os.environ.get("LIVEKIT_URL", ""))


def _model(cfg: dict, key: str, provider: str, defaults: dict, prefixes: dict) -> str:
    """The configured model, unless it obviously belongs to another provider (switched provider, kept model)."""
    m = str(cfg.get(key) or "").strip()
    if not m or not m.lower().startswith(prefixes.get(provider, ("",))):
        return defaults[provider]
    return m


def keyterms(cfg: dict) -> list[str]:
    return [k.strip() for k in str(cfg.get("keyterms") or "").split(",") if k.strip()]


def build_stt(cfg: dict):
    cred = cfg.get("stt_credential") or {}
    provider = cfg.get("stt_provider") or "openai"
    model = _model(cfg, "stt_model", provider, STT_DEFAULT_MODEL, STT_MODEL_PREFIX)
    lang = cfg.get("language") or "en"
    terms = keyterms(cfg)
    if provider == "deepgram_flux":
        return deepgram.STTv2(model=model, api_key=cred.get("api_key") or os.environ.get("DEEPGRAM_API_KEY"),
                              **({"keyterm": terms} if terms else {}))
    if provider == "deepgram":
        return deepgram.STT(model=model, language=lang, detect_language=bool(cfg.get("detect_language")),
                            smart_format=bool(cfg.get("stt_smart_format", True)), profanity_filter=bool(cfg.get("stt_profanity_filter")),
                            api_key=cred.get("api_key") or os.environ.get("DEEPGRAM_API_KEY"), **({"keyterm": terms} if terms else {}))
    kw: dict = {"api_key": cred.get("api_key") or os.environ.get("OPENAI_API_KEY")}
    if cred.get("base_url"):
        kw["base_url"] = cred["base_url"]
    if terms:  # OpenAI transcription takes vocabulary hints as a prompt
        kw["prompt"] = "The conversation may mention: " + ", ".join(terms) + "."
    if cfg.get("stt_noise_reduction"):
        kw["noise_reduction_type"] = cfg["stt_noise_reduction"]
    return openai.STT(model=model, language=lang, detect_language=bool(cfg.get("detect_language")), **kw)


def build_tts(cfg: dict):
    cred = cfg.get("tts_credential") or {}
    provider = cfg.get("tts_provider") or "openai"
    model = _model(cfg, "tts_model", provider, TTS_DEFAULT_MODEL, TTS_MODEL_PREFIX)
    voice = str(cfg.get("tts_voice") or "").strip()
    speed = num(cfg, "tts_speed")
    if provider == "cartesia":
        return cartesia.TTS(api_key=cred.get("api_key"), model=model, language=(cfg.get("language") or "en")[:2],
                            **({"voice": voice} if voice and voice != "alloy" else {}),
                            **({"speed": speed} if speed is not None else {}),
                            **({"emotion": cfg["tts_emotion"]} if cfg.get("tts_emotion") else {}))
    if provider == "elevenlabs":
        settings = elevenlabs.VoiceSettings(stability=num(cfg, "tts_stability", 0.5), similarity_boost=num(cfg, "tts_similarity", 0.75),
                                            style=num(cfg, "tts_style", 0.0), **({"speed": speed} if speed is not None else {}))
        return elevenlabs.TTS(api_key=cred.get("api_key"), model=model, voice_settings=settings,
                              **({"voice_id": voice} if voice and voice != "alloy" else {}))
    if provider == "deepgram":
        return deepgram.TTS(api_key=cred.get("api_key"), model=model, **({"speed": speed} if speed is not None else {}))
    kw: dict = {"api_key": cred.get("api_key") or os.environ.get("OPENAI_API_KEY")}
    if cred.get("base_url"):
        kw["base_url"] = cred["base_url"]
    if speed is not None:
        kw["speed"] = speed
    if cfg.get("tts_instructions"):
        kw["instructions"] = cfg["tts_instructions"]
    return openai.TTS(model=model, voice=voice or "alloy", **kw)


def vad_options(cfg: dict) -> dict:
    return {"activation_threshold": num(cfg, "vad_activation_threshold", 0.5), "min_silence_duration": num(cfg, "vad_min_silence", 0.55),
            "min_speech_duration": num(cfg, "vad_min_speech", 0.05)}


def build_vad(cfg: dict):
    return silero.VAD.load(**vad_options(cfg))


def turn_handling(cfg: dict, cloud: bool | None = None) -> dict:
    """TurnHandlingOptions. Preemptive generation stays off: every reply runs the workflow, so a speculative
    run that gets discarded could still have side effects (a booked appointment, a sent email…)."""
    cloud = is_cloud() if cloud is None else cloud
    if (cfg.get("stt_provider") == "deepgram_flux") or cfg.get("turn_detection") == "stt":
        detection = "stt"
    elif cfg.get("turn_detection") == "vad":
        detection = "vad"
    else:
        detection = inference.TurnDetector()  # LiveKit's end-of-turn model (local fallback off Cloud)
    interrupt_mode = cfg.get("interruption_mode") or "adaptive"
    if interrupt_mode == "adaptive" and not cloud:
        interrupt_mode = "vad"  # adaptive barge-in uses LiveKit Cloud inference
    return {
        "turn_detection": detection,
        "endpointing": {"mode": "dynamic" if cfg.get("endpointing") == "dynamic" else "fixed",
                        "min_delay": num(cfg, "min_endpointing_delay", 0.5), "max_delay": num(cfg, "max_endpointing_delay", 3.0)},
        "interruption": {"enabled": bool(cfg.get("allow_interruptions", True)), "mode": interrupt_mode,
                         "min_duration": num(cfg, "min_interruption_duration", 0.5), "min_words": int(num(cfg, "min_interruption_words", 0)),
                         "resume_false_interruption": bool(cfg.get("resume_false_interruption", True)),
                         "false_interruption_timeout": num(cfg, "false_interruption_timeout", 2.0)},
        "preemptive_generation": {"enabled": False},
    }


def noise_cancellation(cfg: dict, cloud: bool | None = None):
    """A per-participant selector: telephony-tuned BVC for phone callers, BVC for web (mode 'auto')."""
    mode = cfg.get("noise_cancellation") or "auto"
    if mode == "off":
        return None
    if not (is_cloud() if cloud is None else cloud):
        log.warning("noise cancellation needs LiveKit Cloud; continuing without it")
        return None
    try:
        from livekit.plugins import noise_cancellation as nc
    except ImportError:
        log.warning("livekit-plugins-noise-cancellation not installed (uv sync --extra voice); continuing without it")
        return None
    fixed = {"bvc": nc.BVC, "bvc_telephony": nc.BVCTelephony, "nc": nc.NC}.get(mode)

    def select(params):
        if fixed:
            return fixed()
        is_phone = params.participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
        return nc.BVCTelephony() if is_phone else nc.BVC()
    return select


AMBIENCE = {"office": BuiltinAudioClip.OFFICE_AMBIENCE, "city": BuiltinAudioClip.CITY_AMBIENCE,
            "forest": BuiltinAudioClip.FOREST_AMBIENCE, "crowded_room": BuiltinAudioClip.CROWDED_ROOM}
THINKING = {"keyboard": BuiltinAudioClip.KEYBOARD_TYPING, "keyboard2": BuiltinAudioClip.KEYBOARD_TYPING2}


def background_audio(cfg: dict) -> BackgroundAudioPlayer:
    """Always created: hold music (Voice Call Action 'hold') plays through it too."""
    kw: dict = {}
    if clip := AMBIENCE.get(cfg.get("ambient_sound") or "none"):
        kw["ambient_sound"] = AudioConfig(clip, volume=num(cfg, "ambient_volume", 0.3))
    if clip := THINKING.get(cfg.get("thinking_sound") or "none"):
        vol = num(cfg, "thinking_volume", 0.4)
        kw["thinking_sound"] = [AudioConfig(clip, volume=vol, probability=0.8), AudioConfig(clip, volume=vol * 0.7, probability=0.2)]
    return BackgroundAudioPlayer(**kw)


def hold_music() -> AudioConfig:
    return AudioConfig(BuiltinAudioClip.HOLD_MUSIC, volume=0.6)


def tts_transforms(cfg: dict):
    return ["filter_markdown", "filter_emoji"] if cfg.get("filter_markdown", True) else None


def filler_phrases(cfg: dict) -> list[str]:
    return [p.strip() for p in str(cfg.get("filler_phrases") or "").split("|") if p.strip()]


def transfer_uri(to: str) -> str:
    to = to.strip()
    return to if to.startswith(("sip:", "tel:")) else f"tel:{to}"
