"""'Test connection' for every credential type."""

import asyncio
import base64
import datetime as dt
import hashlib
import hmac
import json
import time
from typing import Awaitable, Callable

import httpx

from .credential_catalog import TYPES
from .credentials import OAUTH_TOKEN_KEY, fmt, oauth2_access_token, request_auth, with_defaults

Result = dict  # {"ok": bool, "message": str}
TESTERS: dict[str, Callable[[dict, str | None], Awaitable[Result]]] = {}


def tester(name: str):
    def deco(fn):
        TESTERS[name] = fn
        return fn
    return deco


def ok(msg: str = "Connection tested successfully") -> Result:
    return {"ok": True, "message": msg}


def fail(msg: str) -> Result:
    return {"ok": False, "message": msg}


def _http_error(r: httpx.Response) -> str:
    try:
        body = r.json()
        detail = (body.get("error", {}).get("message") if isinstance(body.get("error"), dict) else body.get("error")) \
            or body.get("message") or body.get("detail") or body.get("description") or json.dumps(body)[:300]
    except ValueError:
        detail = r.text[:300]
        if "<Message>" in r.text:  # AWS-style XML errors
            detail = r.text.split("<Message>")[1].split("</Message>")[0]
    hint ={401: "Unauthorized: check the key / token", 403: "Forbidden: credential lacks permission",
            404: "Not found: check URL / IDs"}.get(r.status_code, f"HTTP {r.status_code}")
    return f"{hint}. {detail}".strip()


async def test_credential(cred_type: str, data: dict, cred_id: str | None = None) -> Result:
    spec = TYPES.get(cred_type)
    if not spec:
        return fail(f"Unknown credential type '{cred_type}'")
    test = spec.get("test")
    if not test:
        return {"ok": True, "message": "Saved. This credential type has no automatic test.", "untested": True}
    data = with_defaults(cred_type, data)
    missing = [f["label"] for f in spec["fields"]
               if f["required"] and data.get(f["name"]) in (None, "") and f["type"] != "boolean"
               and all(data.get(k) in v for k, v in (f.get("show_if") or {}).items())]
    if missing:
        return fail(f"Missing: {', '.join(missing)}")
    try:
        if "custom" in test:
            return await asyncio.wait_for(TESTERS[test["custom"]](data, cred_id, cred_type=cred_type, test=test), timeout=25)
        return await _http_test(cred_type, data, cred_id, test)
    except TimeoutError:
        return fail("Timed out: host unreachable or firewall blocking the connection")
    except httpx.ConnectError as e:
        host = e.request.url.host if getattr(e, "_request", None) else ""
        if "getaddrinfo" in str(e) or "Name or service not known" in str(e):
            return fail(f"Cannot resolve host '{host}'. Check the URL, or that this server can reach it (DNS/VPN).")
        return fail(f"Cannot connect to '{host}': {e or 'connection refused'}. Is the service running and the port open?")
    except httpx.HTTPError as e:
        return fail(f"{type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001
        return fail(f"{type(e).__name__}: {e}")


async def _http_test(cred_type: str, data: dict, cred_id: str | None, test: dict) -> Result:
    url = fmt(test["url"], data)
    if not url or url.startswith("/"):
        return {"ok": True, "message": "Saved. Add a test URL to verify this credential.", "untested": True}
    headers, params, auth = await request_auth(cred_type, data, cred_id)
    headers.update({k: fmt(v, data) for k, v in test.get("headers", {}).items()})
    body = json.loads(fmt(json.dumps(test["json"]), data)) if "json" in test else None
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
        r = await c.request(test.get("method", "GET"), url, headers=headers, params=params or None, json=body, auth=auth)
    if r.status_code >= 400:
        return fail(_http_error(r))
    if test.get("ok_field"):
        try:
            payload = r.json()
        except ValueError:
            payload = {}
        if not payload.get(test["ok_field"]):
            return fail(f"Rejected by API: {payload.get('error') or payload.get('description') or payload}")
    return ok()


# ---------------------------------------------------------------------------
# Custom testers
# ---------------------------------------------------------------------------


@tester("postgres")
async def _pg(d, _id, **_):
    import psycopg

    async with await psycopg.AsyncConnection.connect(
        host=d["host"], port=int(d.get("port") or 5432), dbname=d["database"], user=d["user"],
        password=d.get("password"), sslmode="require" if d.get("ssl") else "prefer", connect_timeout=10,
    ) as conn:
        version = (await (await conn.execute("select version()")).fetchone())[0]
    return ok(f"Connected · {version.split(',')[0]}")


@tester("mysql")
async def _mysql(d, _id, **_):
    import pymysql

    def run():
        conn = pymysql.connect(host=d["host"], port=int(d.get("port") or 3306), user=d["user"], password=d.get("password") or "",
                               database=d["database"], connect_timeout=10, ssl={} if d.get("ssl") else None)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION()")
                return cur.fetchone()[0]
        finally:
            conn.close()

    return ok(f"Connected · MySQL {await asyncio.to_thread(run)}")


@tester("redis")
async def _redis(d, _id, **_):
    from .services.brokers import redis_client

    r = redis_client(d)
    try:
        await r.ping()
        info = await r.info("server")
    finally:
        await r.aclose()
    return ok(f"Connected · Redis {info.get('redis_version', '')}")


@tester("kafka")
async def _kafka(d, _id, **_):
    from aiokafka import AIOKafkaConsumer

    from .services.brokers import kafka_kwargs

    c = AIOKafkaConsumer(**kafka_kwargs(d), request_timeout_ms=15000)
    await c.start()
    try:
        topics = await c.topics()
    finally:
        await c.stop()
    return ok(f"Connected · {len(topics)} topic{'' if len(topics) == 1 else 's'}")


@tester("rabbitmq")
async def _rabbitmq(d, _id, **_):
    import aio_pika

    from .services.brokers import rabbitmq_url

    conn = await aio_pika.connect(rabbitmq_url(d), timeout=15)
    try:
        version = (conn.transport.connection.server_properties or {}).get("version", b"")
    finally:
        await conn.close()
    return ok(f"Connected · RabbitMQ {version.decode() if isinstance(version, bytes) else version}".strip())


@tester("mqtt")
async def _mqtt(d, _id, **_):
    import aiomqtt

    from .services.brokers import mqtt_kwargs

    async with aiomqtt.Client(**mqtt_kwargs(d), timeout=15):
        pass
    return ok("Connected to MQTT broker")


@tester("nats")
async def _nats(d, _id, **_):
    import nats

    from .services.brokers import nats_kwargs

    nc = await nats.connect(**nats_kwargs(d))
    try:
        info = nc._server_info or {}
    finally:
        await nc.close()
    return ok(f"Connected · NATS {info.get('version', '')}".strip())


@tester("mongodb")
async def _mongo(d, _id, **_):
    from pymongo import MongoClient

    def run():
        client = MongoClient(d["uri"], serverSelectionTimeoutMS=10000)
        try:
            return client.server_info().get("version")
        finally:
            client.close()

    return ok(f"Connected · MongoDB {await asyncio.to_thread(run)}")


@tester("smtp")
async def _smtp(d, _id, **_):
    import smtplib

    def run():
        port = int(d.get("port") or 587)
        sec = d.get("security", "starttls")
        s = smtplib.SMTP_SSL(d["host"], port, timeout=15) if sec == "ssl" else smtplib.SMTP(d["host"], port, timeout=15)
        try:
            if sec == "starttls":
                s.starttls()
            if d.get("username"):
                s.login(d["username"], d.get("password", ""))
        finally:
            s.quit()

    await asyncio.to_thread(run)
    return ok("SMTP login successful" if d.get("username") else "SMTP server reachable")


@tester("imap")
async def _imap(d, _id, **_):
    import imaplib

    def run():
        cls = imaplib.IMAP4_SSL if d.get("ssl", True) else imaplib.IMAP4
        m = cls(d["host"], int(d.get("port") or 993), timeout=15)
        try:
            m.login(d["username"], d["password"])
        finally:
            try:
                m.logout()
            except Exception:  # noqa: BLE001
                pass

    await asyncio.to_thread(run)
    return ok("IMAP login successful")


@tester("ftp")
async def _ftp(d, _id, **_):
    import ftplib

    def run():
        f = ftplib.FTP_TLS(timeout=15) if d.get("tls") else ftplib.FTP(timeout=15)
        f.connect(d["host"], int(d.get("port") or 21))
        f.login(d.get("username") or "anonymous", d.get("password") or "")
        f.quit()

    await asyncio.to_thread(run)
    return ok("FTP login successful")


@tester("livekit")
async def _livekit(d, _id, **_):
    from livekit import api

    from .services.livekit import http_url

    lk = api.LiveKitAPI(http_url(d["url"]), d["api_key"], d["api_secret"])
    try:
        rooms = await lk.room.list_rooms(api.ListRoomsRequest())
    finally:
        await lk.aclose()
    return ok(f"Connected · {len(rooms.rooms)} active room(s)")


@tester("paypal")
async def _paypal(d, _id, **_):
    base = "https://api-m.sandbox.paypal.com" if d.get("environment") == "sandbox" else "https://api-m.paypal.com"
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{base}/v1/oauth2/token", data={"grant_type": "client_credentials"},
                         auth=(d["client_id"], d["client_secret"]))
    return ok() if r.status_code == 200 else fail(_http_error(r))


@tester("oauth2")
async def _oauth2(d, cred_id, cred_type="oauth2", test=None, **_):
    if d.get("grant_type") != "client_credentials" and not (d.get(OAUTH_TOKEN_KEY) or {}).get("access_token"):
        return fail("Not connected yet. Click 'Connect my account' to authorize.")
    token = await oauth2_access_token(cred_type, d, cred_id)
    if test and test.get("url"):
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(test["url"], headers={"Authorization": f"Bearer {token}"})
        if r.status_code >= 400:
            return fail(_http_error(r))
        try:
            who = r.json()
            name = who.get("email") or who.get("mail") or who.get("login") or who.get("name") or who.get("userPrincipalName")
        except ValueError:
            name = None
        return ok(f"Connected{f' as {name}' if name else ''}")
    return ok("Access token obtained")


@tester("google_service_account")
async def _google_sa(d, _id, **_):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    sa = json.loads(d["service_account_json"])
    now = int(time.time())
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
    header = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = b64(json.dumps({"iss": sa["client_email"], "scope": d.get("scopes") or "https://www.googleapis.com/auth/cloud-platform",
                             "aud": sa.get("token_uri", "https://oauth2.googleapis.com/token"), "iat": now, "exp": now + 600}).encode())
    key = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
    sig = b64(key.sign(f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256()))
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(sa.get("token_uri", "https://oauth2.googleapis.com/token"),
                         data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": f"{header}.{claims}.{sig}"})
    return ok(f"Authenticated as {sa['client_email']}") if r.status_code == 200 else fail(_http_error(r))


@tester("aws")
async def _aws(d, _id, **_):
    """STS GetCallerIdentity signed with SigV4 (no SDK needed)."""
    region = d.get("region") or "us-east-1"
    host = f"sts.{region}.amazonaws.com"
    body = "Action=GetCallerIdentity&Version=2011-06-15"
    now = dt.datetime.now(dt.timezone.utc)
    amz_date, date = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    headers = {"content-type": "application/x-www-form-urlencoded; charset=utf-8", "host": host, "x-amz-date": amz_date}
    if d.get("session_token"):
        headers["x-amz-security-token"] = d["session_token"]
    signed = ";".join(sorted(headers))
    canonical = "\n".join(["POST", "/", "", "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)), signed,
                           hashlib.sha256(body.encode()).hexdigest()])
    scope = f"{date}/{region}/sts/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = ("AWS4" + d["secret_access_key"]).encode()
    for part in (date, region, "sts", "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = f"AWS4-HMAC-SHA256 Credential={d['access_key_id']}/{scope}, SignedHeaders={signed}, Signature={sig}"
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"https://{host}/", content=body, headers=headers)
    if r.status_code != 200:
        return fail(_http_error(r))
    arn = r.text.split("<Arn>")[1].split("</Arn>")[0] if "<Arn>" in r.text else ""
    return ok(f"Authenticated · {arn}")
