"""WhatsApp Business Cloud API client (Python port of the wapi.go feature set:
messaging, media, interactive, templates, phone numbers, webhook parsing)."""

import hashlib
import hmac
from typing import Any

import httpx

GRAPH = "https://graph.facebook.com"


class WhatsAppError(Exception):
    pass


class WhatsApp:
    def __init__(self, cred: dict):
        self.token = cred["access_token"]
        self.phone_number_id = cred.get("phone_number_id")
        self.waba_id = cred.get("business_account_id")
        self.version = cred.get("api_version") or "v21.0"

    async def _req(self, method: str, path: str, **kw) -> dict:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.request(method, f"{GRAPH}/{self.version}/{path}",
                                headers={"Authorization": f"Bearer {self.token}"}, **kw)
        data = r.json() if r.content else {}
        if r.status_code >= 400:
            raise WhatsAppError(data.get("error", {}).get("message") or r.text)
        return data

    async def send(self, to: str, msg_type: str, payload: dict, reply_to: str | None = None) -> dict:
        body: dict[str, Any] = {"messaging_product": "whatsapp", "recipient_type": "individual",
                                "to": to, "type": msg_type, msg_type: payload}
        if reply_to:
            body["context"] = {"message_id": reply_to}
        return await self._req("POST", f"{self.phone_number_id}/messages", json=body)

    async def text(self, to: str, body: str, preview_url: bool = False, reply_to: str | None = None) -> dict:
        return await self.send(to, "text", {"body": body, "preview_url": preview_url}, reply_to)

    async def media(self, to: str, kind: str, link: str, caption: str | None = None, filename: str | None = None) -> dict:
        payload: dict = {"link": link}
        if caption and kind in ("image", "video", "document"):
            payload["caption"] = caption
        if filename and kind == "document":
            payload["filename"] = filename
        return await self.send(to, kind, payload)

    async def buttons(self, to: str, body: str, buttons: list[dict], header: str | None = None,
                      footer: str | None = None) -> dict:
        interactive: dict = {
            "type": "button",
            "body": {"text": body},
            "action": {"buttons": [{"type": "reply", "reply": {"id": b.get("id") or b["title"][:20],
                                                                "title": b["title"][:20]}} for b in buttons[:3]]},
        }
        if header:
            interactive["header"] = {"type": "text", "text": header}
        if footer:
            interactive["footer"] = {"text": footer}
        return await self.send(to, "interactive", interactive)

    async def list_message(self, to: str, body: str, button: str, rows: list[dict], section_title: str = "Options") -> dict:
        interactive = {
            "type": "list",
            "body": {"text": body},
            "action": {"button": button[:20], "sections": [{"title": section_title[:24], "rows": [
                {"id": r.get("id") or r["title"][:200], "title": r["title"][:24], "description": r.get("description", "")[:72]}
                for r in rows[:10]]}]},
        }
        return await self.send(to, "interactive", interactive)

    async def template(self, to: str, name: str, language: str, components: list | None = None) -> dict:
        payload: dict = {"name": name, "language": {"code": language}}
        if components:
            payload["components"] = components
        return await self.send(to, "template", payload)

    async def location(self, to: str, lat: float, lng: float, name: str = "", address: str = "") -> dict:
        return await self.send(to, "location", {"latitude": lat, "longitude": lng, "name": name, "address": address})

    async def reaction(self, to: str, message_id: str, emoji: str) -> dict:
        return await self.send(to, "reaction", {"message_id": message_id, "emoji": emoji})

    async def mark_read(self, message_id: str, typing: bool = False) -> dict:
        body: dict = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
        if typing:
            body["typing_indicator"] = {"type": "text"}
        return await self._req("POST", f"{self.phone_number_id}/messages", json=body)

    async def media_url(self, media_id: str) -> dict:
        return await self._req("GET", media_id)

    async def list_templates(self) -> dict:
        return await self._req("GET", f"{self.waba_id}/message_templates", params={"limit": 100})

    async def create_template(self, name: str, category: str, language: str, components: list) -> dict:
        return await self._req("POST", f"{self.waba_id}/message_templates",
                               json={"name": name, "category": category, "language": language, "components": components})

    async def delete_template(self, name: str) -> dict:
        return await self._req("DELETE", f"{self.waba_id}/message_templates", params={"name": name})

    async def phone_numbers(self) -> dict:
        return await self._req("GET", f"{self.waba_id}/phone_numbers")

    async def business_profile(self) -> dict:
        return await self._req("GET", f"{self.phone_number_id}/whatsapp_business_profile",
                               params={"fields": "about,address,description,email,profile_picture_url,websites,vertical"})


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


def parse_webhook(payload: dict) -> list[dict]:
    """Flatten a Cloud API webhook into one event per message / status."""
    events = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            meta = value.get("metadata", {})
            contacts = {c.get("wa_id"): c.get("profile", {}).get("name") for c in value.get("contacts", [])}
            for m in value.get("messages", []):
                mtype = m.get("type")
                text = None
                if mtype == "text":
                    text = m["text"].get("body")
                elif mtype == "interactive":
                    inter = m.get("interactive", {})
                    reply = inter.get("button_reply") or inter.get("list_reply") or {}
                    text = reply.get("title")
                elif mtype == "button":
                    text = m.get("button", {}).get("text")
                elif mtype in ("image", "video", "document", "audio", "sticker"):
                    text = m.get(mtype, {}).get("caption")
                events.append({
                    "event": "message",
                    "from": m.get("from"),
                    "name": contacts.get(m.get("from")),
                    "message_id": m.get("id"),
                    "timestamp": m.get("timestamp"),
                    "type": mtype,
                    "text": text,
                    "message": text,
                    "session_id": m.get("from"),
                    "phone_number_id": meta.get("phone_number_id"),
                    "display_phone_number": meta.get("display_phone_number"),
                    "raw": m,
                })
            for s in value.get("statuses", []):
                events.append({
                    "event": "status",
                    "status": s.get("status"),
                    "message_id": s.get("id"),
                    "recipient": s.get("recipient_id"),
                    "timestamp": s.get("timestamp"),
                    "phone_number_id": meta.get("phone_number_id"),
                    "errors": s.get("errors"),
                    "raw": s,
                })
    return events
