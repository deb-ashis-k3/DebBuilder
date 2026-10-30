"""Model lists for the chat model nodes' "From list" picker: ask the provider which models this
credential can use. Auth comes from the credential catalog (same headers as "Test credential").

Filtering and ordering follow n8n's model discovery (@n8n/ai-utilities/src/model-discovery/providers)
so both show the same models."""

import time
from datetime import datetime
from urllib.parse import urlparse

import httpx

from .credentials import request_auth, with_defaults

_OFFICIAL_OPENAI_HOSTS = {"api.openai.com"}


def _openai_chat_model(model_id: str, custom_host: bool) -> bool:
    """n8n's shouldIncludeOpenAiModel: on the official API drop families that work on neither
    chat/completions nor responses (gpt-audio-* stays: it works on chat). Custom hosts (Groq, LM Studio,
    vLLM, OpenRouter…) don't follow OpenAI's naming, so everything is listed there."""
    if custom_host:
        return True
    i = model_id
    return not (
        i.startswith(("babbage", "davinci", "computer-use", "dall-e", "text-embedding", "tts", "whisper",
                      "omni-moderation", "sora"))
        or any(x in i for x in ("-image", "-transcribe", "-diarize", "-tts", "-realtime"))
        or (i.startswith("gpt-") and "instruct" in i)
    )


def _google_chat_model(m: dict) -> bool:
    """n8n's shouldIncludeGoogleModel: needs generateContent, and image/TTS/embedding models are
    dropped by name too (proxied lists report generateContent for everything)."""
    name = m.get("name") or ""
    return ("generateContent" in (m.get("supportedGenerationMethods") or [])
            and not any(x in name for x in ("image", "tts", "embedding")))

_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_TTL = 600  # seconds; the picker's refresh button bypasses it


def _error(r: httpx.Response) -> str:
    try:
        body = r.json()
        err = body.get("error") if isinstance(body, dict) else None
        msg = (err.get("message") if isinstance(err, dict) else err) or body.get("message") or body.get("detail")
    except ValueError:
        msg = r.text[:200]
    return f"{r.status_code}: {msg or r.reason_phrase}"


async def _get(client: httpx.AsyncClient, url: str, cred_type: str, data: dict, cred_id: str, **params) -> dict:
    headers, query, auth = await request_auth(cred_type, data, cred_id)
    r = await client.get(url, headers=headers, params={**query, **params}, auth=auth)
    if r.status_code >= 400:
        raise ValueError(f"The provider refused the request ({_error(r)})")
    return r.json()


async def _openai(c, data, cred_id) -> list[dict]:
    base = (data.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    rows = (await _get(c, f"{base}/models", "openai", data, cred_id)).get("data") or []
    custom = (urlparse(base).hostname or "") not in _OFFICIAL_OPENAI_HOSTS
    ids = sorted({m["id"] for m in rows if isinstance(m, dict) and m.get("id")})
    return [{"value": i, "label": i} for i in ids if _openai_chat_model(i, custom)]


async def _anthropic(c, data, cred_id) -> list[dict]:
    out, after = [], None
    while True:
        body = await _get(c, "https://api.anthropic.com/v1/models", "anthropic", data, cred_id,
                          limit=1000, **({"after_id": after} if after else {}))
        out += body.get("data") or []
        if not body.get("has_more") or not body.get("last_id"):
            break
        after = body["last_id"]

    def created(m: dict) -> float:
        try:
            return datetime.fromisoformat(str(m.get("created_at")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return [{"value": m["id"], "label": m.get("display_name") or m["id"], "hint": m["id"]}
            for m in sorted(out, key=created, reverse=True) if m.get("id")]  # newest first


async def _gemini(c, data, cred_id) -> list[dict]:
    # Key in the x-goog-api-key header (not ?key=) so it can't leak through access logs or proxies.
    out, token = [], None
    while True:
        r = await c.get("https://generativelanguage.googleapis.com/v1beta/models", headers={"x-goog-api-key": data.get("api_key") or ""},
                        params={"pageSize": 1000, **({"pageToken": token} if token else {})})
        if r.status_code >= 400:
            raise ValueError(f"The provider refused the request ({_error(r)})")
        body = r.json()
        for m in body.get("models") or []:
            if isinstance(m, dict) and m.get("name") and _google_chat_model(m):
                mid = m["name"].removeprefix("models/")  # LangChain takes the bare id
                out.append({"value": mid, "label": (m.get("displayName") or "").strip() or mid, "hint": mid})
        token = body.get("nextPageToken")
        if not token:
            return sorted(out, key=lambda m: m["label"].lower())


async def _ollama(c, data, cred_id) -> list[dict]:
    base = (data.get("base_url") or "http://localhost:11434").rstrip("/")
    r = await c.get(f"{base}/api/tags")
    if r.status_code >= 400:
        raise ValueError(f"Ollama answered {_error(r)}")
    rows = r.json().get("models") or []
    return sorted(({"value": m["name"], "label": m["name"], "hint": (m.get("details") or {}).get("parameter_size", "")}
                   for m in rows if m.get("name")), key=lambda m: m["value"])


def _compatible(cred_type: str, base: str):
    """OpenAI-compatible providers (Groq, Mistral, DeepSeek, OpenRouter, xAI, Together): everything they list."""
    async def fetch(c, data, cred_id) -> list[dict]:
        body = await _get(c, f"{base}/models", cred_type, data, cred_id)
        rows = body if isinstance(body, list) else body.get("data") or []  # Together returns a bare list
        ids = sorted({m["id"] for m in rows if isinstance(m, dict) and m.get("id")})
        return [{"value": i, "label": i} for i in ids]
    return fetch


_PROVIDERS = {"openai": _openai, "anthropic": _anthropic, "google_genai": _gemini, "ollama": _ollama,
              **{t: _compatible(t, url) for t, url in {
                  "groq": "https://api.groq.com/openai/v1", "mistral": "https://api.mistral.ai/v1",
                  "deepseek": "https://api.deepseek.com/v1", "openrouter": "https://openrouter.ai/api/v1",
                  "xai": "https://api.x.ai/v1", "together": "https://api.together.xyz/v1"}.items()}}


def supports(cred_type: str) -> bool:
    return cred_type in _PROVIDERS


async def list_models(cred_type: str, data: dict, cred_id: str, version: str, refresh: bool = False) -> list[dict]:
    """[{value, label, hint?}] or ValueError with a message for the user. Cached per credential version."""
    if cred_type not in _PROVIDERS:
        raise ValueError("This credential type has no model list")
    key = (cred_id, version)
    hit = _cache.get(key)
    if hit and not refresh and time.monotonic() - hit[0] < _TTL:
        return hit[1]
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            models = await _PROVIDERS[cred_type](c, with_defaults(cred_type, data), cred_id)
    except httpx.HTTPError as e:
        raise ValueError(f"Couldn't reach the provider: {e.__class__.__name__}: {e}") from e
    for k in [k for k in _cache if k[0] == cred_id]:
        _cache.pop(k, None)  # drop lists of older credential versions
    _cache[key] = (time.monotonic(), models)
    return models
