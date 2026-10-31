"""App integrations: WhatsApp, Slack, Telegram, Email, Postgres."""

import asyncio
import json
import smtplib
from typing import Any
from email.message import EmailMessage

import httpx
import psycopg
from psycopg.rows import dict_row

from ..services.whatsapp import WhatsApp
from .base import NodeContext, NodeError, NodeType, prop, register

COMM = dict(group="communication", color="#10b981")

# ---------------------------------------------------------------------------
# WhatsApp
# ---------------------------------------------------------------------------

WA_OPS = [
    {"label": "Send text", "value": "text"},
    {"label": "Send media (image/video/audio/document)", "value": "media"},
    {"label": "Send reply buttons", "value": "buttons"},
    {"label": "Send list menu", "value": "list"},
    {"label": "Send template", "value": "template"},
    {"label": "Send location", "value": "location"},
    {"label": "React to message", "value": "reaction"},
    {"label": "Mark as read (+typing)", "value": "mark_read"},
    {"label": "Get media URL", "value": "media_url"},
    {"label": "List templates", "value": "list_templates"},
    {"label": "Create template", "value": "create_template"},
    {"label": "List phone numbers", "value": "phone_numbers"},
    {"label": "Get business profile", "value": "business_profile"},
]

SEND_OPS = ["text", "media", "buttons", "list", "template", "location", "reaction"]


async def _whatsapp(ctx: NodeContext):
    out = []
    for i in range(len(ctx.items) or 1):
        _, cred = await ctx.credential("credential", i)
        wa = WhatsApp(cred)
        p = ctx.params(i)
        op = p.get("operation", "text")
        to = str(p.get("to") or "").lstrip("+")
        if op in SEND_OPS and not to:
            raise NodeError("Recipient 'to' is empty")
        if op == "text":
            res = await wa.text(to, str(p.get("text", "")), bool(p.get("preview_url")), p.get("reply_to") or None)
        elif op == "media":
            res = await wa.media(to, p.get("media_type", "image"), p["media_link"], p.get("caption"), p.get("filename"))
        elif op == "buttons":
            res = await wa.buttons(to, str(p.get("text", "")), p.get("buttons") or [], p.get("header"), p.get("footer"))
        elif op == "list":
            res = await wa.list_message(to, str(p.get("text", "")), p.get("list_button") or "Choose", p.get("rows") or [])
        elif op == "template":
            comps = p.get("components")
            comps = json.loads(comps) if isinstance(comps, str) and comps.strip() else (comps or None)
            res = await wa.template(to, p["template_name"], p.get("language") or "en_US", comps)
        elif op == "location":
            res = await wa.location(to, float(p["latitude"]), float(p["longitude"]), p.get("location_name", ""), p.get("address", ""))
        elif op == "reaction":
            res = await wa.reaction(to, p["message_id"], p.get("emoji") or "👍")
        elif op == "mark_read":
            res = await wa.mark_read(p["message_id"], bool(p.get("typing")))
        elif op == "media_url":
            res = await wa.media_url(p["media_id"])
        elif op == "list_templates":
            res = await wa.list_templates()
        elif op == "create_template":
            comps = p.get("components")
            comps = json.loads(comps) if isinstance(comps, str) else comps
            res = await wa.create_template(p["template_name"], p.get("category", "UTILITY"), p.get("language") or "en_US", comps or [])
        elif op == "phone_numbers":
            res = await wa.phone_numbers()
        elif op == "business_profile":
            res = await wa.business_profile()
        else:
            raise NodeError(f"Unknown operation {op}")
        out.append(res)
    return {"main": out}


def _ops(*ops):
    return {"operation": list(ops)}


register(NodeType(
    type="whatsapp.action", label="WhatsApp", icon="message-square",
    description="WhatsApp Business Cloud API: send messages, media, buttons, templates; manage templates & numbers.",
    properties=[
        prop("credential", "WhatsApp credential", "credential", None, credential_types=["whatsapp"], required=True),
        prop("operation", "Operation", "options", "text", options=WA_OPS),
        prop("to", "To (phone, intl format)", "string", "{{ $json['from'] }}", show_if=_ops(*SEND_OPS)),
        prop("text", "Message", "text", "{{ $json.output }}", show_if=_ops("text", "buttons", "list"), rows=3),
        prop("preview_url", "Link preview", "boolean", False, show_if=_ops("text")),
        prop("reply_to", "Reply to message id", "string", "", show_if=_ops("text")),
        prop("media_type", "Media type", "options", "image", options=["image", "video", "audio", "document", "sticker"], show_if=_ops("media")),
        prop("media_link", "Media URL", "string", "", show_if=_ops("media")),
        prop("caption", "Caption", "string", "", show_if=_ops("media")),
        prop("filename", "Filename (documents)", "string", "", show_if=_ops("media")),
        prop("header", "Header", "string", "", show_if=_ops("buttons")),
        prop("footer", "Footer", "string", "", show_if=_ops("buttons")),
        prop("buttons", "Buttons (max 3)", "collection", [{"id": "yes", "title": "Yes"}, {"id": "no", "title": "No"}],
             fields=[prop("id", "ID"), prop("title", "Title")], show_if=_ops("buttons")),
        prop("list_button", "List button text", "string", "Choose", show_if=_ops("list")),
        prop("rows", "List rows (max 10)", "collection", [{"id": "1", "title": "Option 1", "description": ""}],
             fields=[prop("id", "ID"), prop("title", "Title"), prop("description", "Description")], show_if=_ops("list")),
        prop("template_name", "Template name", "string", "hello_world", show_if=_ops("template", "create_template")),
        prop("language", "Language code", "string", "en_US", show_if=_ops("template", "create_template")),
        prop("category", "Category", "options", "UTILITY", options=["UTILITY", "MARKETING", "AUTHENTICATION"], show_if=_ops("create_template")),
        prop("components", "Components (JSON)", "json", "", show_if=_ops("template", "create_template")),
        prop("latitude", "Latitude", "number", 0, show_if=_ops("location")),
        prop("longitude", "Longitude", "number", 0, show_if=_ops("location")),
        prop("location_name", "Place name", "string", "", show_if=_ops("location")),
        prop("address", "Address", "string", "", show_if=_ops("location")),
        prop("message_id", "Message id", "string", "{{ $json.message_id }}", show_if=_ops("reaction", "mark_read")),
        prop("emoji", "Emoji", "string", "👍", show_if=_ops("reaction")),
        prop("typing", "Show typing indicator", "boolean", True, show_if=_ops("mark_read")),
        prop("media_id", "Media id", "string", "", show_if=_ops("media_url")),
    ],
    execute=_whatsapp, **COMM,
))

# ---------------------------------------------------------------------------
# Slack / Telegram
# ---------------------------------------------------------------------------


async def _slack(ctx: NodeContext):
    out = []
    for i in range(len(ctx.items) or 1):
        _, cred = await ctx.credential("credential", i)
        p = ctx.params(i)
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post("https://slack.com/api/chat.postMessage",
                             headers={"Authorization": f"Bearer {cred['bot_token']}"},
                             json={"channel": p["channel"], "text": str(p.get("text", "")), **({"thread_ts": p["thread_ts"]} if p.get("thread_ts") else {})})
        data = r.json()
        if not data.get("ok"):
            raise NodeError(f"Slack error: {data.get('error')}")
        out.append(data)
    return {"main": out}


register(NodeType(
    type="slack.send", label="Slack: Send Message", icon="hash", description="Post a message to a Slack channel.",
    properties=[
        prop("credential", "Slack credential", "credential", None, credential_types=["slack"], required=True),
        prop("channel", "Channel (ID or #name)", "string", "#general"),
        prop("text", "Text", "text", "{{ $json.output }}"),
        prop("thread_ts", "Thread ts (reply)", "string", ""),
    ],
    execute=_slack, **COMM,
))


async def _telegram(ctx: NodeContext):
    out = []
    for i in range(len(ctx.items) or 1):
        _, cred = await ctx.credential("credential", i)
        p = ctx.params(i)
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"https://api.telegram.org/bot{cred['bot_token']}/sendMessage",
                             json={"chat_id": p["chat_id"], "text": str(p.get("text", "")),
                                   **({"parse_mode": p["parse_mode"]} if p.get("parse_mode") not in (None, "none") else {})})
        data = r.json()
        if not data.get("ok"):
            raise NodeError(f"Telegram error: {data.get('description')}")
        out.append(data["result"])
    return {"main": out}


register(NodeType(
    type="telegram.send", label="Telegram: Send Message", icon="send", description="Send a Telegram bot message.",
    properties=[
        prop("credential", "Telegram credential", "credential", None, credential_types=["telegram"], required=True),
        prop("chat_id", "Chat ID", "string", ""),
        prop("text", "Text", "text", "{{ $json.output }}"),
        prop("parse_mode", "Parse mode", "options", "none", options=["none", "Markdown", "HTML"]),
    ],
    execute=_telegram, **COMM,
))

# ---------------------------------------------------------------------------
# Email (SMTP)
# ---------------------------------------------------------------------------


def _send_mail(cred: dict, msg: EmailMessage) -> None:
    port = int(cred.get("port") or 587)
    sec = cred.get("security", "starttls")
    smtp = smtplib.SMTP_SSL(cred["host"], port, timeout=30) if sec == "ssl" else smtplib.SMTP(cred["host"], port, timeout=30)
    try:
        if sec == "starttls":
            smtp.starttls()
        if cred.get("username"):
            smtp.login(cred["username"], cred.get("password", ""))
        smtp.send_message(msg)
    finally:
        smtp.quit()


async def _email(ctx: NodeContext):
    out = []
    for i, item in enumerate(ctx.items or [{}]):
        _, cred = await ctx.credential("credential", i)
        p = ctx.params(i)
        msg = EmailMessage()
        msg["From"] = p.get("from_email") or cred.get("from_email") or cred.get("username")
        msg["To"] = p["to"]
        if p.get("cc"):
            msg["Cc"] = p["cc"]
        msg["Subject"] = p.get("subject", "")
        if p.get("format") == "html":
            msg.set_content("This email requires an HTML client.")
            msg.add_alternative(str(p.get("body", "")), subtype="html")
        else:
            msg.set_content(str(p.get("body", "")))
        await asyncio.to_thread(_send_mail, cred, msg)
        out.append({**item, "email_sent": True, "to": p["to"]})
    return {"main": out}


register(NodeType(
    type="email.send", label="Send Email", icon="mail", description="Send an email via SMTP.",
    properties=[
        prop("credential", "SMTP credential", "credential", None, credential_types=["smtp"], required=True),
        prop("from_email", "From (optional)", "string", ""),
        prop("to", "To", "string", ""),
        prop("cc", "CC", "string", ""),
        prop("subject", "Subject", "string", ""),
        prop("format", "Format", "options", "text", options=["text", "html"]),
        prop("body", "Body", "text", "{{ $json.output }}", rows=6),
    ],
    execute=_email, **COMM,
))

# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------


async def _postgres(ctx: NodeContext):
    out = []
    mode = ctx.raw_params.get("mode", "once")
    rng = range(len(ctx.items) or 1) if mode == "each" else range(1)
    _, cred = await ctx.credential()
    async with await psycopg.AsyncConnection.connect(
        host=cred["host"], port=int(cred.get("port") or 5432), dbname=cred["database"],
        user=cred["user"], password=cred.get("password"), row_factory=dict_row, autocommit=True,
    ) as conn:
        for i in rng:
            q = ctx.raw_params.get("query", "")  # SQL is not templated; use $1-style params below.
            params = [ctx.eval(v.get("value"), i) for v in ctx.raw_params.get("params") or []]
            q = _dollar_to_psycopg(q)
            cur = await conn.execute(q, params or None)
            if cur.description:
                rows = await cur.fetchall()
                out.extend(rows)
            else:
                out.append({"rowcount": cur.rowcount})
    return {"main": out}


def _dollar_to_psycopg(q: str) -> str:
    import re

    return re.sub(r"\$(\d+)", "%s", q.replace("%", "%%"))


register(NodeType(
    type="db.postgres", label="Postgres", icon="database",
    description="Run SQL. Use $1, $2... placeholders bound to the parameter list (safe from injection).",
    properties=[
        prop("credential", "Postgres credential", "credential", None, credential_types=["postgres"], required=True),
        prop("query", "SQL", "code", "SELECT now() AS time", language="sql", rows=6),
        prop("params", "Parameters ($1, $2, ...)", "collection", [], fields=[prop("value", "Value", "string", "")]),
        prop("mode", "Run", "options", "once", options=[
            {"label": "Once", "value": "once"}, {"label": "Once per input item", "value": "each"}]),
    ],
    group="data", color="#336791", execute=_postgres,
))


# ---------------------------------------------------------------------------
# MySQL / Redis / MongoDB
# ---------------------------------------------------------------------------


async def _mysql(ctx: NodeContext):
    import pymysql
    from pymysql.cursors import DictCursor

    _, cred = await ctx.credential()
    mode = ctx.raw_params.get("mode", "once")
    query = ctx.raw_params.get("query", "")  # use %s placeholders bound to the parameter list
    batches = [[ctx.eval(v.get("value"), i) for v in ctx.raw_params.get("params") or []]
               for i in (range(len(ctx.items) or 1) if mode == "each" else range(1))]

    def run():
        conn = pymysql.connect(host=cred["host"], port=int(cred.get("port") or 3306), user=cred["user"],
                               password=cred.get("password") or "", database=cred["database"], cursorclass=DictCursor,
                               autocommit=True, connect_timeout=15, ssl={} if cred.get("ssl") else None)
        out = []
        try:
            with conn.cursor() as cur:
                for params in batches:
                    cur.execute(query, params or None)
                    out.extend(cur.fetchall() if cur.description else [{"affected_rows": cur.rowcount, "insert_id": cur.lastrowid}])
        finally:
            conn.close()
        return out

    return {"main": [json.loads(json.dumps(r, default=str)) for r in await asyncio.to_thread(run)]}


register(NodeType(
    type="db.mysql", label="MySQL", icon="database", description="Run SQL on MySQL / MariaDB (%s placeholders, injection-safe).",
    properties=[
        prop("credential", "MySQL credential", "credential", None, credential_types=["mysql"], required=True),
        prop("query", "SQL", "code", "SELECT NOW() AS time", language="sql", rows=6),
        prop("params", "Parameters (%s in order)", "collection", [], fields=[prop("value", "Value", "string", "")]),
        prop("mode", "Run", "options", "once", options=[{"label": "Once", "value": "once"}, {"label": "Once per input item", "value": "each"}]),
    ],
    group="data", color="#00758f", execute=_mysql,
))


_redis_clients: dict[tuple, Any] = {}


def _redis_client(cred: dict):
    """One pooled client per Redis credential (and event loop), reused across runs. A new connection per run
    costs a TCP (+TLS) handshake every time; on Windows "localhost" can add ~2 s while IPv6 is tried first."""
    import asyncio

    import redis.asyncio as aioredis

    key = (id(asyncio.get_running_loop()), cred.get("host"), str(cred.get("port") or 6379), str(cred.get("db") or 0),
           cred.get("username") or "", cred.get("password") or "", bool(cred.get("ssl")))
    client = _redis_clients.get(key)
    if client is None:
        if len(_redis_clients) > 64:
            _redis_clients.clear()
        client = _redis_clients[key] = aioredis.Redis(
            host=cred["host"], port=int(cred.get("port") or 6379), db=int(cred.get("db") or 0),
            username=cred.get("username") or None, password=cred.get("password") or None, ssl=bool(cred.get("ssl")),
            decode_responses=True, max_connections=50, health_check_interval=30)
    return client


def _stream_fields(value, item: dict) -> dict:
    """Redis stream entries are flat string maps: nested values go in as JSON. No value = the whole item."""
    if value in (None, ""):
        value = item
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {"value": value}
    if not isinstance(value, dict):
        return {"value": json.dumps(value)}
    return {str(k): v if isinstance(v, str) else json.dumps(v, default=str) for k, v in value.items()}


async def _redis(ctx: NodeContext):
    _, cred = await ctx.credential()
    r = _redis_client(cred)
    out = []
    try:
        if ctx.raw_params.get("operation") == "xadd":
            # Every item in one pipeline: a single round trip for the whole batch (thousands per second).
            pipe = r.pipeline(transaction=False)
            for i, item in enumerate(ctx.items or [{}]):
                p = ctx.params(i)
                fields = _stream_fields(p.get("fields"), item)
                maxlen = int(p["maxlen"]) if p.get("maxlen") else None
                pipe.xadd(str(p.get("key") or ""), fields, maxlen=maxlen, approximate=True)
            ids = await pipe.execute()
            return {"main": [{**item, "stream_id": eid} for item, eid in zip(ctx.items or [{}], ids)]}
        for i, item in enumerate(ctx.items or [{}]):
            p = ctx.params(i)
            op, key = p.get("operation", "get"), str(p.get("key") or "")
            if op == "get":
                val = await r.get(key)
                try:
                    val = json.loads(val) if val else val
                except ValueError:
                    pass
                out.append({**item, p.get("property") or "value": val})
            elif op == "set":
                v = p.get("value")
                await r.set(key, v if isinstance(v, str) else json.dumps(v), ex=int(p["ttl"]) if p.get("ttl") else None)
                out.append({**item, "ok": True})
            elif op == "delete":
                out.append({**item, "deleted": await r.delete(key)})
            elif op == "incr":
                out.append({**item, "value": await r.incrby(key, int(p.get("amount") or 1))})
            elif op == "publish":
                v = p.get("value")
                out.append({**item, "receivers": await r.publish(key, v if isinstance(v, str) else json.dumps(v))})
            elif op == "push":
                v = p.get("value")
                out.append({**item, "length": await r.rpush(key, v if isinstance(v, str) else json.dumps(v))})
            elif op == "pop":
                out.append({**item, "value": await r.lpop(key)})
            elif op == "keys":
                out.append({"keys": await r.keys(key or "*")})
    finally:
        pass  # pooled client: connections stay open for the next run
    return {"main": out}


register(NodeType(
    type="db.redis", label="Redis", icon="layers", description="Get/set keys, counters, lists, pub/sub and streams.",
    properties=[
        prop("credential", "Redis credential", "credential", None, credential_types=["redis"], required=True),
        prop("operation", "Operation", "options", "get", options=[
            {"label": "get", "value": "get"}, {"label": "set", "value": "set"}, {"label": "delete", "value": "delete"},
            {"label": "incr", "value": "incr"}, {"label": "publish", "value": "publish"}, {"label": "push", "value": "push"},
            {"label": "pop", "value": "pop"}, {"label": "keys", "value": "keys"},
            {"label": "Stream add (XADD)", "value": "xadd"}]),
        prop("key", "Key / channel / pattern / stream", "string", ""),
        prop("value", "Value", "string", "", show_if={"operation": ["set", "publish", "push"]}),
        prop("fields", "Fields (JSON object; empty = the whole item)", "string", "", show_if={"operation": ["xadd"]},
             description="Each item becomes one entry. All items are sent in one pipelined batch."),
        prop("maxlen", "Max stream length (approx.)", "number", 100000, show_if={"operation": ["xadd"]},
             description="Trims old entries (MAXLEN ~) so the stream can't grow without limit. Empty = never trim."),
        prop("ttl", "TTL seconds (optional)", "number", None, show_if={"operation": ["set"]}),
        prop("amount", "Increment by", "number", 1, show_if={"operation": ["incr"]}),
        prop("property", "Output property", "string", "value", show_if={"operation": ["get"]}),
    ],
    group="data", color="#dc382d", execute=_redis,
))


async def _mongodb(ctx: NodeContext):
    from pymongo import MongoClient

    _, cred = await ctx.credential()
    p = ctx.params(0)
    op = p.get("operation", "find")

    def parse(v, default):
        if isinstance(v, str):
            return json.loads(v) if v.strip() else default
        return v if v is not None else default

    def run():
        client = MongoClient(cred["uri"], serverSelectionTimeoutMS=15000)
        try:
            db = client[p.get("database") or cred.get("database") or client.get_default_database().name]
            col = db[p["collection"]]
            if op == "find":
                docs = list(col.find(parse(p.get("filter"), {})).limit(int(p.get("limit") or 50)))
                return [json.loads(json.dumps(d, default=str)) for d in docs]
            if op == "insert":
                docs = [{k: v for k, v in it.items()} for it in ctx.items] or [{}]
                res = col.insert_many(docs)
                return [{"inserted_id": str(i)} for i in res.inserted_ids]
            if op == "update":
                res = col.update_many(parse(p.get("filter"), {}), {"$set": parse(p.get("update"), {})})
                return [{"matched": res.matched_count, "modified": res.modified_count}]
            if op == "delete":
                return [{"deleted": col.delete_many(parse(p.get("filter"), {})).deleted_count}]
            if op == "aggregate":
                return [json.loads(json.dumps(d, default=str)) for d in col.aggregate(parse(p.get("pipeline"), []))]
            raise NodeError(f"Unknown operation {op}")
        finally:
            client.close()

    return {"main": await asyncio.to_thread(run)}


register(NodeType(
    type="db.mongodb", label="MongoDB", icon="leaf", description="Find, insert, update, delete and aggregate documents.",
    properties=[
        prop("credential", "MongoDB credential", "credential", None, credential_types=["mongodb"], required=True),
        prop("operation", "Operation", "options", "find", options=["find", "insert", "update", "delete", "aggregate"]),
        prop("database", "Database (empty = credential default)", "string", ""),
        prop("collection", "Collection", "string", "", required=True),
        prop("filter", "Filter (JSON)", "json", "{}", show_if={"operation": ["find", "update", "delete"]}),
        prop("update", "$set fields (JSON)", "json", "{}", show_if={"operation": ["update"]}),
        prop("pipeline", "Pipeline (JSON array)", "json", "[]", show_if={"operation": ["aggregate"]}),
        prop("limit", "Limit", "number", 50, show_if={"operation": ["find"]}),
    ],
    group="data", color="#47a248", execute=_mongodb,
))
