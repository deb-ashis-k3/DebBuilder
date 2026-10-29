"""Listener triggers: Kafka, Redis (Pub/Sub, Streams, Lists), RabbitMQ, MQTT, NATS, AWS SQS,
Postgres LISTEN/NOTIFY, IMAP email, RSS, Server-Sent Events, local files, intervals and
workflow events.

Each node pairs a listener (registered with ``app.listeners``) that runs while the workflow
is active, with an ``execute`` that passes the event through, or — on an editor run — waits
for one live test event, like n8n's "Listen for test event"."""

import asyncio
import datetime as dt
import email
import email.policy
import fnmatch
import json
import os
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import httpx

from ..listeners import ListenCtx, csv, listen_once, listener, parse_payload
from ..services.brokers import (aws_json_request, kafka_kwargs, mqtt_kwargs, nats_kwargs, pg_kwargs, rabbitmq_url,
                                redis_client)
from .base import NodeContext, NodeError, NodeType, prop, register


def _iso(ts: float | None) -> str | None:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat() if ts else None


async def _execute(ctx: NodeContext):
    if ctx.items and ctx.items != [{}]:
        return {"main": ctx.items}  # real event (or pinned / provided test data)
    if ctx.run.mode != "manual":
        return {"main": ctx.items or [{}]}
    params = ctx.raw_params
    cred = (await ctx.credential())[1] if params.get("credential") else {}
    timeout = float(params.get("test_timeout") or 120)
    ctx.log(f"Listening for a test event (up to {timeout:.0f}s)…")
    try:
        items = await listen_once(ctx.run.meta.get("workflow_id", ""), ctx.node, params, cred, timeout)
    except TimeoutError as e:
        raise NodeError(str(e), ctx.name) from e
    except Exception as e:  # noqa: BLE001
        raise NodeError(f"{type(e).__name__}: {e}", ctx.name) from e
    return {"main": items}


LISTENER = dict(trigger=True, listener=True, inputs=[], group="trigger", execute=_execute)
JSON = lambda label="Parse JSON payloads": prop("json_parse", label, "boolean", True)  # noqa: E731
CONCURRENCY = prop("max_concurrency", "Max parallel executions", "number", 5,
                   description="Executions running at once; further events wait (backpressure)")
TEST_TIMEOUT = prop("test_timeout", "Test listen timeout (s)", "number", 120,
                    description="How long an editor run waits for a live event")
ACK = lambda default="after_execution": prop("ack", "Acknowledge", "options", default, options=[  # noqa: E731
    {"label": "After the workflow succeeds (at-least-once)", "value": "after_execution"},
    {"label": "Immediately on receipt (at-most-once)", "value": "immediately"},
])


def node(type_, label, icon, color, description, properties):
    register(NodeType(type=type_, label=label, icon=icon, color=color, description=description,
                      properties=properties, **LISTENER))


# ===========================================================================
# Kafka
# ===========================================================================

node("trigger.kafka", "Kafka Trigger", "layers", "#8f8f8f",
     "Consumes messages from Kafka topics. Emits {message, key, topic, partition, offset, headers, timestamp}.", [
         prop("credential", "Kafka credential", "credential", None, credential_types=["kafka"], required=True),
         prop("topics", "Topics", "string", "", required=True, placeholder="orders, payments", description="Comma separated"),
         prop("group_id", "Consumer group", "string", "debbuilder", required=True,
              description="Instances in the same group share partitions"),
         prop("from_beginning", "Read from beginning (new group)", "boolean", False),
         prop("commit", "Commit offsets", "options", "after_execution", options=[
             {"label": "After the workflow finishes (in order, at-least-once)", "value": "after_execution"},
             {"label": "Automatically (parallel, at-most-once)", "value": "auto"},
         ]),
         JSON("Parse message as JSON"), CONCURRENCY, TEST_TIMEOUT,
     ])


@listener("trigger.kafka")
async def _kafka(lc: ListenCtx):
    from aiokafka import AIOKafkaConsumer

    p = lc.params
    after = p.get("commit") == "after_execution" and not lc.test
    consumer = AIOKafkaConsumer(
        *csv(p["topics"]), **kafka_kwargs(lc.cred),
        # Test runs read only new messages without a group, so they never move production offsets.
        group_id=None if lc.test else (p.get("group_id") or "debbuilder"),
        auto_offset_reset="latest" if lc.test or not p.get("from_beginning") else "earliest",
        enable_auto_commit=not after and not lc.test,
    )
    await consumer.start()
    try:
        lc.ready()
        async for m in consumer:
            item = {
                "message": parse_payload(m.value, p.get("json_parse", True)),
                "key": parse_payload(m.key, False) if m.key is not None else None,
                "topic": m.topic, "partition": m.partition, "offset": m.offset,
                "timestamp": _iso(m.timestamp / 1000 if m.timestamp else None),
                "headers": {k: parse_payload(v, False) for k, v in (m.headers or [])},
            }
            await lc.emit([item], wait=after)
            if after:
                await consumer.commit()
    finally:
        await consumer.stop()


# ===========================================================================
# Redis: Pub/Sub, Streams, Lists
# ===========================================================================

REDIS_CRED = prop("credential", "Redis credential", "credential", None, credential_types=["redis"], required=True)


def _redis_listener_client(cred: dict):
    # Blocking reads (XREADGROUP BLOCK, idle Pub/Sub) must not hit redis-py's default 5s read
    # timeout; periodic PINGs still detect dead connections so the supervisor can reconnect.
    return redis_client(cred, socket_timeout=None, health_check_interval=30)

node("trigger.redis", "Redis Trigger (Pub/Sub)", "radio-tower", "#dc382d",
     "Subscribes to Redis channels; glob patterns (news.*) use PSUBSCRIBE. Keyspace events work too, e.g. "
     "__keyevent@0__:expired. Emits {channel, pattern, message}.", [
         REDIS_CRED,
         prop("channels", "Channels", "string", "", required=True, placeholder="orders, alerts.*",
              description="Comma separated; * ? [ ] make it a pattern"),
         JSON(), CONCURRENCY, TEST_TIMEOUT,
     ])


@listener("trigger.redis")
async def _redis_pubsub(lc: ListenCtx):
    chans = csv(lc.params["channels"])
    patterns = [c for c in chans if any(ch in c for ch in "*?[")]
    plain = [c for c in chans if c not in patterns]
    r = _redis_listener_client(lc.cred)
    ps = r.pubsub(ignore_subscribe_messages=True)
    try:
        if plain:
            await ps.subscribe(*plain)
        if patterns:
            await ps.psubscribe(*patterns)
        lc.ready()
        async for msg in ps.listen():
            if msg.get("type") not in ("message", "pmessage"):
                continue
            await lc.emit([{"channel": msg["channel"], "pattern": msg.get("pattern"),
                            "message": parse_payload(msg["data"], lc.params.get("json_parse", True))}])
    finally:
        await ps.aclose()
        await r.aclose()


node("trigger.redis_stream", "Redis Stream Trigger", "audio-waveform", "#dc382d",
     "Reads Redis Streams (XREADGROUP with consumer groups, or plain XREAD). Unacked entries are "
     "re-delivered on restart. Emits {stream, id, fields}.", [
         REDIS_CRED,
         prop("streams", "Stream keys", "string", "", required=True, placeholder="events, orders",
              description="Comma separated"),
         prop("mode", "Mode", "options", "group", options=[
             {"label": "Consumer group (load-balanced, acknowledged)", "value": "group"},
             {"label": "Simple (every instance reads everything)", "value": "simple"},
         ]),
         prop("group", "Group name", "string", "debbuilder", show_if={"mode": ["group"]}),
         prop("consumer", "Consumer name", "string", "debbuilder-1", show_if={"mode": ["group"]}),
         prop("start", "Start from", "options", "new", options=[
             {"label": "New entries only ($)", "value": "new"}, {"label": "Beginning of stream (0)", "value": "all"}],
             description="For a new group, or for simple mode"),
         {**ACK(), "show_if": {"mode": ["group"]}},
         prop("count", "Batch size", "number", 10),
         JSON("Parse JSON field values"), CONCURRENCY, TEST_TIMEOUT,
     ])


def _stream_item(stream: str, eid: str, fields: dict | None, as_json: bool) -> dict:
    return {"stream": stream, "id": eid,
            "fields": {k: parse_payload(v, as_json) for k, v in (fields or {}).items()},
            "timestamp": _iso(int(eid.split("-")[0]) / 1000) if "-" in eid else None}


@listener("trigger.redis_stream")
async def _redis_stream(lc: ListenCtx):
    from redis.exceptions import ResponseError

    p = lc.params
    keys = csv(p["streams"])
    as_json = p.get("json_parse", True)
    count = int(p.get("count") or 10)
    r = _redis_listener_client(lc.cred)
    try:
        if lc.test or p.get("mode") == "simple":
            # Plain XREAD: tests only peek at new entries, nothing is acknowledged.
            cursor = {k: "$" if lc.test or p.get("start") != "all" else "0" for k in keys}
            lc.ready()
            while True:
                res = await r.xread(cursor, count=count, block=5000) or []
                for stream, entries in res:
                    for eid, fields in entries:
                        cursor[stream] = eid
                        await lc.emit([_stream_item(stream, eid, fields, as_json)])
            return

        group, consumer = p.get("group") or "debbuilder", p.get("consumer") or "debbuilder-1"
        for k in keys:
            try:
                await r.xgroup_create(k, group, id="0" if p.get("start") == "all" else "$", mkstream=True)
            except ResponseError as e:
                if "BUSYGROUP" not in str(e):
                    raise
        lc.ready()
        after = p.get("ack") != "immediately"
        # First re-deliver this consumer's pending (unacked) entries, then switch to new ones.
        pending = {k: "0" for k in keys}
        while True:
            ids = pending or {k: ">" for k in keys}
            res = await r.xreadgroup(group, consumer, ids, count=count, block=None if pending else 5000) or []
            if pending and not any(entries for _, entries in res):
                pending = {}
                continue
            for stream, entries in res:
                for eid, fields in entries:
                    if pending:
                        pending[stream] = eid
                    if not fields:  # entry trimmed from the stream while pending
                        await r.xack(stream, group, eid)
                        continue
                    if not after:
                        await r.xack(stream, group, eid)
                    result = await lc.emit([_stream_item(stream, eid, fields, as_json)], wait=after)
                    if after and (result or {}).get("status") != "error":
                        await r.xack(stream, group, eid)
    finally:
        await r.aclose()


node("trigger.redis_list", "Redis Queue Trigger (List)", "list-ordered", "#dc382d",
     "Pops items from Redis lists used as work queues (BLPOP / BRPOP). Emits {key, value}.", [
         REDIS_CRED,
         prop("keys", "List keys", "string", "", required=True, placeholder="jobs", description="Comma separated"),
         prop("side", "Pop from", "options", "left", options=[
             {"label": "Left (BLPOP)", "value": "left"}, {"label": "Right (BRPOP)", "value": "right"}],
              description="Producers using RPUSH → pop Left for FIFO; LPUSH → pop Right"),
         JSON(), CONCURRENCY, TEST_TIMEOUT,
     ])


@listener("trigger.redis_list", graceful=True)
async def _redis_list(lc: ListenCtx):
    keys = csv(lc.params["keys"])
    left = lc.params.get("side") != "right"
    as_json = lc.params.get("json_parse", True)
    r = _redis_listener_client(lc.cred)
    try:
        lc.ready()
        while not lc.stopping.is_set():
            if lc.test:
                # Peek without removing, so a test run never steals a job.
                for k in keys:
                    v = await r.lindex(k, 0 if left else -1)
                    if v is not None:
                        await lc.emit([{"key": k, "value": parse_payload(v, as_json), "peeked": True}])
                await asyncio.sleep(1)
                continue
            # Short blocking pop, so a stop request is honoured within ~1s without ever
            # cancelling a pop whose job Redis has already handed over.
            res = await (r.blpop if left else r.brpop)(keys, timeout=1)
            if res:
                await lc.emit([{"key": res[0], "value": parse_payload(res[1], as_json)}])
    finally:
        await r.aclose()


# ===========================================================================
# RabbitMQ
# ===========================================================================

node("trigger.rabbitmq", "RabbitMQ Trigger", "rabbit", "#ff6600",
     "Consumes a RabbitMQ queue (optionally bound to an exchange). Emits {message, routing_key, exchange, headers, …}.", [
         prop("credential", "RabbitMQ credential", "credential", None, credential_types=["rabbitmq"], required=True),
         prop("queue", "Queue", "string", "", required=True, placeholder="orders"),
         prop("durable", "Durable queue", "boolean", True),
         prop("exchange", "Bind to exchange", "string", "", description="Optional existing exchange"),
         prop("routing_key", "Routing key", "string", "#"),
         ACK(), prop("requeue", "Requeue failed messages", "boolean", False, show_if={"ack": ["after_execution"]}),
         prop("prefetch", "Prefetch count", "number", 5),
         JSON("Parse body as JSON"), CONCURRENCY, TEST_TIMEOUT,
     ])


def _amqp_item(msg, as_json: bool) -> dict:
    return {"message": parse_payload(msg.body, as_json), "routing_key": msg.routing_key, "exchange": msg.exchange,
            "headers": {k: parse_payload(v, False) for k, v in (msg.headers or {}).items()},
            "content_type": msg.content_type, "message_id": msg.message_id, "correlation_id": msg.correlation_id,
            "redelivered": msg.redelivered, "timestamp": msg.timestamp.isoformat() if msg.timestamp else None}


@listener("trigger.rabbitmq")
async def _rabbitmq(lc: ListenCtx):
    import aio_pika

    p = lc.params
    as_json = p.get("json_parse", True)
    conn = await aio_pika.connect(rabbitmq_url(lc.cred), timeout=15)
    try:
        ch = await conn.channel()
        await ch.set_qos(prefetch_count=int(p.get("prefetch") or 5))
        if lc.test and p.get("exchange"):
            # Private temporary queue gets a copy of matching messages.
            q = await ch.declare_queue("", exclusive=True, auto_delete=True)
        else:
            q = await ch.declare_queue(p["queue"], durable=bool(p.get("durable", True)))
        if p.get("exchange"):
            ex = await ch.get_exchange(p["exchange"])  # passive: must already exist
            await q.bind(ex, routing_key=p.get("routing_key") or "#")
        lc.ready()
        if lc.test and not p.get("exchange"):
            # Peek: take a message and put it straight back.
            while True:
                msg = await q.get(no_ack=False, fail=False)
                if msg:
                    await msg.nack(requeue=True)
                    await lc.emit([_amqp_item(msg, as_json)])
                await asyncio.sleep(1)
        after = p.get("ack") != "immediately" and not lc.test
        async with q.iterator() as it:
            async for msg in it:
                if not after:
                    await msg.ack()
                result = await lc.emit([_amqp_item(msg, as_json)], wait=after)
                if after:
                    if (result or {}).get("status") == "error":
                        await msg.nack(requeue=bool(p.get("requeue")))
                    else:
                        await msg.ack()
    finally:
        await conn.close()


# ===========================================================================
# MQTT
# ===========================================================================

node("trigger.mqtt", "MQTT Trigger", "antenna", "#a855a0",
     "Subscribes to MQTT topics (wildcards + and # allowed). Emits {topic, message, qos, retain}.", [
         prop("credential", "MQTT credential", "credential", None, credential_types=["mqtt"], required=True),
         prop("topics", "Topics", "string", "", required=True, placeholder="sensors/+/temperature, alerts/#",
              description="Comma separated"),
         prop("qos", "QoS", "options", "0", options=["0", "1", "2"]),
         JSON(), CONCURRENCY, TEST_TIMEOUT,
     ])


@listener("trigger.mqtt")
async def _mqtt(lc: ListenCtx):
    import aiomqtt

    kw = mqtt_kwargs(lc.cred)
    if lc.test:
        kw.pop("identifier", None)  # same client id would kick the production connection off the broker
    async with aiomqtt.Client(**kw, timeout=15) as client:
        for t in csv(lc.params["topics"]):
            await client.subscribe(t, qos=int(lc.params.get("qos") or 0))
        lc.ready()
        async for m in client.messages:
            await lc.emit([{"topic": str(m.topic), "message": parse_payload(m.payload, lc.params.get("json_parse", True)),
                            "qos": m.qos, "retain": m.retain}])


# ===========================================================================
# NATS
# ===========================================================================

node("trigger.nats", "NATS Trigger", "share-2", "#27aae1",
     "Subscribes to a NATS subject (wildcards * and > allowed). Emits {subject, message, reply, headers}.", [
         prop("credential", "NATS credential", "credential", None, credential_types=["nats"], required=True),
         prop("subject", "Subject", "string", "", required=True, placeholder="orders.>"),
         prop("queue_group", "Queue group", "string", "", description="Load-balance across subscribers in the group"),
         JSON(), CONCURRENCY, TEST_TIMEOUT,
     ])


@listener("trigger.nats")
async def _nats(lc: ListenCtx):
    import nats

    kw = {**nats_kwargs(lc.cred), "allow_reconnect": not lc.test, "max_reconnect_attempts": -1 if not lc.test else 0}
    nc = await nats.connect(**kw)
    try:
        # Tests subscribe outside the queue group so they get a copy instead of taking work.
        sub = await nc.subscribe(lc.params["subject"], queue="" if lc.test else (lc.params.get("queue_group") or ""))
        lc.ready()
        async for msg in sub.messages:
            await lc.emit([{"subject": msg.subject, "message": parse_payload(msg.data, lc.params.get("json_parse", True)),
                            "reply": msg.reply or None, "headers": dict(msg.headers or {})}])
    finally:
        await nc.close()


# ===========================================================================
# AWS SQS
# ===========================================================================

node("trigger.sqs", "AWS SQS Trigger", "inbox", "#ff9900",
     "Long-polls an SQS queue and deletes messages once handled. Emits {message, message_id, attributes, …}.", [
         prop("credential", "AWS credential", "credential", None, credential_types=["aws"], required=True),
         prop("queue_url", "Queue URL", "string", "", required=True,
              placeholder="https://sqs.us-east-1.amazonaws.com/123456789012/my-queue"),
         prop("batch", "Messages per poll (1-10)", "number", 10),
         {**ACK(), "label": "Delete message"},
         JSON("Parse body as JSON"), CONCURRENCY, TEST_TIMEOUT,
     ])


async def _sqs_call(client: httpx.AsyncClient, cred: dict, queue_url: str, action: str, payload: dict) -> dict:
    body = json.dumps({"QueueUrl": queue_url, **payload})
    url, headers = aws_json_request(cred, "sqs", f"AmazonSQS.{action}", body, host=urlparse(queue_url).netloc)
    r = await client.post(url, content=body, headers=headers)
    if r.status_code != 200:
        raise RuntimeError(f"SQS {action} failed ({r.status_code}): {r.text[:300]}")
    return r.json() if r.content else {}


@listener("trigger.sqs")
async def _sqs(lc: ListenCtx):
    p = lc.params
    qurl = p["queue_url"]
    after = p.get("ack") != "immediately"
    async with httpx.AsyncClient(timeout=40) as client:
        await _sqs_call(client, lc.cred, qurl, "GetQueueAttributes", {"AttributeNames": ["QueueArn"]})
        lc.ready()
        while True:
            res = await _sqs_call(client, lc.cred, qurl, "ReceiveMessage", {
                "MaxNumberOfMessages": max(1, min(10, int(p.get("batch") or 10))), "WaitTimeSeconds": 20,
                "AttributeNames": ["All"], "MessageAttributeNames": ["All"],
                # Tests make the message visible again immediately instead of consuming it.
                **({"VisibilityTimeout": 0} if lc.test else {}),
            })
            for m in res.get("Messages", []):
                item = {"message": parse_payload(m.get("Body"), p.get("json_parse", True)), "message_id": m.get("MessageId"),
                        "attributes": m.get("Attributes", {}),
                        "message_attributes": {k: v.get("StringValue", v.get("BinaryValue")) for k, v in (m.get("MessageAttributes") or {}).items()}}
                delete = {"ReceiptHandle": m["ReceiptHandle"]}
                if lc.test:
                    await lc.emit([item])  # stops the test run; the message stays in the queue
                    continue
                if not after:
                    await _sqs_call(client, lc.cred, qurl, "DeleteMessage", delete)
                result = await lc.emit([item], wait=after)
                if after and (result or {}).get("status") != "error":
                    await _sqs_call(client, lc.cred, qurl, "DeleteMessage", delete)


# ===========================================================================
# Postgres LISTEN / NOTIFY
# ===========================================================================

node("trigger.postgres", "Postgres Trigger", "database-zap", "#336791",
     "Listens for NOTIFY on a channel, or for row changes on a table (creates a notify trigger while active). "
     "Emits {channel, payload} / {operation, schema, table, new, old}.", [
         prop("credential", "Postgres credential", "credential", None, credential_types=["postgres"], required=True),
         prop("mode", "Listen to", "options", "table", options=[
             {"label": "Table row changes", "value": "table"}, {"label": "A NOTIFY channel", "value": "channel"}]),
         prop("schema", "Schema", "string", "public", show_if={"mode": ["table"]}),
         prop("table", "Table", "string", "", show_if={"mode": ["table"]}),
         prop("events", "Events", "options", "INSERT OR UPDATE OR DELETE", show_if={"mode": ["table"]}, options=[
             "INSERT OR UPDATE OR DELETE", "INSERT", "UPDATE", "DELETE", "INSERT OR UPDATE"]),
         prop("channel", "Channel", "string", "", show_if={"mode": ["channel"]}),
         JSON("Parse payload as JSON"), CONCURRENCY, TEST_TIMEOUT,
     ])

_PG_FN = """
CREATE OR REPLACE FUNCTION {fn}() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE payload text;
BEGIN
  payload := json_build_object('operation', TG_OP, 'schema', TG_TABLE_SCHEMA, 'table', TG_TABLE_NAME,
    'new', CASE WHEN TG_OP <> 'DELETE' THEN row_to_json(NEW) END,
    'old', CASE WHEN TG_OP <> 'INSERT' THEN row_to_json(OLD) END)::text;
  IF octet_length(payload) > 7900 THEN  -- NOTIFY payload limit; never fail the user's write
    payload := json_build_object('operation', TG_OP, 'schema', TG_TABLE_SCHEMA, 'table', TG_TABLE_NAME,
      'truncated', true)::text;
  END IF;
  PERFORM pg_notify({chan}, payload);
  RETURN NULL;
END $$;
"""


@listener("trigger.postgres")
async def _postgres(lc: ListenCtx):
    import psycopg
    from psycopg import sql

    p = lc.params
    suffix = "".join(ch for ch in lc.node_id if ch.isalnum())[-12:].lower() + ("_test" if lc.test else "")
    table_mode = p.get("mode", "table") == "table"
    if table_mode and not p.get("table"):
        raise ValueError("Table is required")
    chan = f"debbuilder_{suffix}" if table_mode else p.get("channel")
    if not chan:
        raise ValueError("Channel is required")
    schema = p.get("schema") or "public"
    fn = sql.Identifier(schema, f"debbuilder_notify_{suffix}")
    trg = sql.Identifier(f"debbuilder_{suffix}")
    target = sql.Identifier(schema, p.get("table") or "")
    events = p.get("events") or "INSERT OR UPDATE OR DELETE"
    if events not in ("INSERT OR UPDATE OR DELETE", "INSERT", "UPDATE", "DELETE", "INSERT OR UPDATE"):
        raise ValueError(f"Invalid events '{events}'")
    async with await psycopg.AsyncConnection.connect(**pg_kwargs(lc.cred), autocommit=True) as conn:
        if table_mode:
            await conn.execute(sql.SQL(_PG_FN).format(fn=fn, chan=sql.Literal(chan)))
            await conn.execute(sql.SQL("DROP TRIGGER IF EXISTS {} ON {}").format(trg, target))
            await conn.execute(sql.SQL("CREATE TRIGGER {} AFTER " + events + " ON {} FOR EACH ROW EXECUTE FUNCTION {}()")
                               .format(trg, target, fn))
        try:
            await conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(chan)))
            lc.ready()
            async for n in conn.notifies():
                payload = parse_payload(n.payload, p.get("json_parse", True))
                item = payload if table_mode and isinstance(payload, dict) else {"channel": n.channel, "payload": payload}
                await lc.emit([item])
        finally:
            if table_mode:
                # Shielded so a second cancel (shutdown) can't leave the trigger behind.
                await asyncio.shield(_pg_cleanup(lc.cred, trg, target, fn))


async def _pg_cleanup(cred, trg, target, fn) -> None:
    """Remove the notify trigger with a fresh connection (the listening one may be gone)."""
    import psycopg
    from psycopg import sql

    try:
        async with await psycopg.AsyncConnection.connect(**pg_kwargs(cred), autocommit=True) as c:
            await c.execute(sql.SQL("DROP TRIGGER IF EXISTS {} ON {}").format(trg, target))
            await c.execute(sql.SQL("DROP FUNCTION IF EXISTS {}()").format(fn))
    except Exception:  # noqa: BLE001
        pass


# ===========================================================================
# Email (IMAP)
# ===========================================================================

node("trigger.email", "Email Trigger (IMAP)", "mail", "#64748b",
     "Polls a mailbox for unread emails. Emits {from, to, subject, date, text, html, attachments}.", [
         prop("credential", "IMAP credential", "credential", None, credential_types=["imap"], required=True),
         prop("mailbox", "Mailbox", "string", "INBOX"),
         prop("poll_seconds", "Check every (seconds)", "number", 60),
         prop("mark_read", "Mark as read", "boolean", True),
         CONCURRENCY, TEST_TIMEOUT,
     ])


def _parse_email(raw: bytes) -> dict:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    text = html = None
    attachments = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        if part.get_filename():
            attachments.append({"filename": part.get_filename(), "content_type": part.get_content_type(),
                                "size": len(part.get_payload(decode=True) or b"")})
        elif part.get_content_type() == "text/plain" and text is None:
            text = part.get_content()
        elif part.get_content_type() == "text/html" and html is None:
            html = part.get_content()
    return {"from": str(msg["from"] or ""), "to": str(msg["to"] or ""), "cc": str(msg["cc"] or ""),
            "subject": str(msg["subject"] or ""), "date": str(msg["date"] or ""), "message_id": str(msg["message-id"] or ""),
            "text": text, "html": html, "attachments": attachments}


def _imap_fetch(cred: dict, mailbox: str, latest_only: bool, mark_read: bool) -> list[dict]:
    import imaplib

    cls = imaplib.IMAP4_SSL if cred.get("ssl", True) else imaplib.IMAP4
    m = cls(cred["host"], int(cred.get("port") or 993), timeout=30)
    try:
        m.login(cred["username"], cred["password"])
        m.select(mailbox or "INBOX", readonly=latest_only or not mark_read)
        _, data = m.uid("search", None, "ALL" if latest_only else "UNSEEN")
        uids = (data[0] or b"").split()
        uids = uids[-1:] if latest_only else uids[:50]
        out = []
        for uid in uids:
            _, parts = m.uid("fetch", uid, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if raw:
                out.append({**_parse_email(raw), "uid": uid.decode()})
                if mark_read and not latest_only:
                    m.uid("store", uid, "+FLAGS", "(\\Seen)")
        return out
    finally:
        try:
            m.logout()
        except Exception:  # noqa: BLE001
            pass


@listener("trigger.email")
async def _email(lc: ListenCtx):
    p = lc.params
    mark_read = bool(p.get("mark_read", True))
    seen: set[str] = set()  # without "mark as read" the same unread mails come back every poll
    first = True
    while True:
        mails = await asyncio.to_thread(_imap_fetch, lc.cred, p.get("mailbox") or "INBOX", lc.test, mark_read)
        if first:
            lc.ready()
            first = False
        for mail in mails:
            if mail["uid"] in seen:
                continue
            if not mark_read:
                seen.add(mail["uid"])
            await lc.emit([mail])
        await asyncio.sleep(max(10, int(p.get("poll_seconds") or 60)))


# ===========================================================================
# RSS / Atom
# ===========================================================================

node("trigger.rss", "RSS Feed Trigger", "rss", "#f26522",
     "Polls an RSS or Atom feed and emits each new entry {title, link, id, published, summary, author}.", [
         prop("url", "Feed URL", "string", "", required=True, placeholder="https://example.com/feed.xml"),
         prop("poll_minutes", "Check every (minutes)", "number", 10),
         CONCURRENCY, TEST_TIMEOUT,
     ])

_ATOM = "{http://www.w3.org/2005/Atom}"


def _feed_entries(xml: bytes) -> list[dict]:
    root = ET.fromstring(xml)
    out = []
    for it in root.iter("item"):  # RSS 2.0
        g = lambda tag: (it.findtext(tag) or "").strip()  # noqa: E731
        out.append({"title": g("title"), "link": g("link"), "id": g("guid") or g("link"), "published": g("pubDate"),
                    "summary": g("description"), "author": g("author") or g("{http://purl.org/dc/elements/1.1/}creator"),
                    "categories": [c.text for c in it.findall("category") if c.text]})
    for it in root.iter(f"{_ATOM}entry"):  # Atom
        g = lambda tag: (it.findtext(f"{_ATOM}{tag}") or "").strip()  # noqa: E731
        link = it.find(f"{_ATOM}link")
        out.append({"title": g("title"), "link": link.get("href") if link is not None else "", "id": g("id"),
                    "published": g("published") or g("updated"), "summary": g("summary") or g("content"),
                    "author": (it.findtext(f"{_ATOM}author/{_ATOM}name") or "").strip(),
                    "categories": [c.get("term") for c in it.findall(f"{_ATOM}category") if c.get("term")]})
    return out


@listener("trigger.rss")
async def _rss(lc: ListenCtx):
    seen: set[str] = set()
    first = True
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": "DebBuilder/1.0"}) as client:
        while True:
            r = await client.get(lc.params["url"])
            r.raise_for_status()
            entries = _feed_entries(r.content)
            if lc.test:
                if not entries:
                    raise ValueError("The feed has no entries")
                await lc.emit([entries[0]])
            if first:
                seen.update(e["id"] for e in entries)  # only entries published after activation
                lc.ready()
                first = False
            else:
                for e in reversed([e for e in entries if e["id"] not in seen]):
                    seen.add(e["id"])
                    await lc.emit([e])
            await asyncio.sleep(max(1, float(lc.params.get("poll_minutes") or 10)) * 60)


# ===========================================================================
# Server-Sent Events
# ===========================================================================

node("trigger.sse", "SSE Trigger", "radio", "#0ea5e9",
     "Connects to a Server-Sent Events stream and emits each event {event, data, id}.", [
         prop("url", "Stream URL", "string", "", required=True),
         prop("headers", "Headers (JSON)", "json", "{}"),
         prop("events", "Only these event names", "string", "", description="Comma separated; empty = all"),
         JSON("Parse data as JSON"), CONCURRENCY, TEST_TIMEOUT,
     ])


@listener("trigger.sse")
async def _sse(lc: ListenCtx):
    from httpx_sse import aconnect_sse

    headers = lc.params.get("headers") or {}
    if isinstance(headers, str):
        headers = json.loads(headers or "{}")
    only = set(csv(lc.params.get("events")))
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=None)) as client:
        async with aconnect_sse(client, "GET", lc.params["url"], headers=headers) as src:
            src.response.raise_for_status()
            lc.ready()
            async for ev in src.aiter_sse():
                if only and ev.event not in only:
                    continue
                await lc.emit([{"event": ev.event, "data": parse_payload(ev.data, lc.params.get("json_parse", True)),
                                "id": ev.id or None}])


# ===========================================================================
# Local files
# ===========================================================================

node("trigger.file", "Local File Trigger", "folder-search", "#a16207",
     "Watches a folder on the server for added / changed / deleted files. Emits {event, path, name, size}.", [
         prop("path", "Folder or file", "string", "", required=True, placeholder="C:\\data\\incoming"),
         prop("recursive", "Include subfolders", "boolean", True),
         prop("pattern", "File name pattern", "string", "*", description="Glob, e.g. *.csv"),
         prop("events", "Events", "options", "added", options=[
             {"label": "File added", "value": "added"}, {"label": "File changed", "value": "modified"},
             {"label": "File deleted", "value": "deleted"}, {"label": "Any change", "value": "all"}]),
         CONCURRENCY, TEST_TIMEOUT,
     ])


@listener("trigger.file")
async def _file(lc: ListenCtx):
    from watchfiles import Change, awatch

    p = lc.params
    path = os.path.expandvars(os.path.expanduser(p["path"]))
    if not os.path.exists(path):
        raise FileNotFoundError(f"Path not found: {path}")
    wanted = {"added": {Change.added}, "modified": {Change.modified}, "deleted": {Change.deleted}}.get(
        p.get("events") or "added", {Change.added, Change.modified, Change.deleted})
    pattern = p.get("pattern") or "*"
    lc.ready()
    async for changes in awatch(path, recursive=bool(p.get("recursive", True)), step=200):
        for change, fp in sorted(changes, key=lambda c: c[1]):
            name = os.path.basename(fp)
            if change not in wanted or not fnmatch.fnmatch(name, pattern) or os.path.isdir(fp):
                continue
            st = os.stat(fp) if change != Change.deleted and os.path.exists(fp) else None
            await lc.emit([{"event": change.name, "path": fp, "name": name, "size": st.st_size if st else None,
                            "modified_at": _iso(st.st_mtime) if st else None}])


# ===========================================================================
# Interval & workflow events
# ===========================================================================

node("trigger.interval", "Interval Trigger", "timer", "#f97316",
     "Runs every N seconds / minutes / hours while the workflow is active (for sub-minute polling).", [
         prop("every", "Every", "number", 30, required=True),
         prop("unit", "Unit", "options", "seconds", options=["seconds", "minutes", "hours"]),
         prop("max_concurrency", "Max parallel executions", "number", 1,
              description="1 = skip ticks while the previous run is still going"),
     ])


@listener("trigger.interval")
async def _interval(lc: ListenCtx):
    seconds = float(lc.params.get("every") or 30) * {"seconds": 1, "minutes": 60, "hours": 3600}.get(lc.params.get("unit"), 1)
    seconds = max(1.0, seconds)
    lc.ready()
    n = 0
    while True:
        if not lc.test:
            await asyncio.sleep(seconds)
        n += 1
        await lc.emit([{"tick": n, "at": _iso(time.time())}])


node("trigger.workflow_event", "Workflow Event Trigger", "power", "#f97316",
     "Runs when this workflow is activated, or when the server starts with it active (n8n Trigger).", [
         prop("events", "Run on", "options", "both", options=[
             {"label": "Activation or server start", "value": "both"},
             {"label": "Workflow activated", "value": "activated"},
             {"label": "Server started", "value": "started"}]),
     ])


@listener("trigger.workflow_event")
async def _workflow_event(lc: ListenCtx):
    lc.ready()
    want = lc.params.get("events") or "both"
    if lc.test or (lc.reason in ("activated", "started") and want in ("both", lc.reason)):
        await lc.emit([{"event": "test" if lc.test else lc.reason, "at": _iso(time.time())}])
