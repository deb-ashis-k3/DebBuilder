"""Connection builders for queues / streams / databases, shared by the listener
triggers and the credential testers so both connect exactly the same way."""

import datetime as dt
import hashlib
import hmac
import ssl as _ssl
from urllib.parse import quote


def _csv(v) -> list[str]:
    return [s.strip() for s in str(v or "").split(",") if s.strip()]


# ---- Kafka -----------------------------------------------------------------

def kafka_kwargs(cred: dict) -> dict:
    """aiokafka client kwargs (bootstrap servers + SASL/SSL)."""
    kw: dict = {"bootstrap_servers": _csv(cred.get("brokers")) or ["localhost:9092"],
                "client_id": cred.get("client_id") or "debbuilder"}
    mech = cred.get("sasl_mechanism") or "none"
    use_ssl = bool(cred.get("ssl"))
    if use_ssl:
        kw["ssl_context"] = _ssl.create_default_context()
    if mech != "none":
        kw.update(sasl_mechanism=mech, sasl_plain_username=cred.get("username"), sasl_plain_password=cred.get("password"),
                  security_protocol="SASL_SSL" if use_ssl else "SASL_PLAINTEXT")
    else:
        kw["security_protocol"] = "SSL" if use_ssl else "PLAINTEXT"
    return kw


# ---- RabbitMQ --------------------------------------------------------------

def rabbitmq_url(cred: dict) -> str:
    scheme = "amqps" if cred.get("ssl") else "amqp"
    vhost = quote(cred.get("vhost") or "/", safe="")
    user, pw = quote(cred.get("username") or "guest", safe=""), quote(cred.get("password") or "guest", safe="")
    return f"{scheme}://{user}:{pw}@{cred.get('host') or 'localhost'}:{int(cred.get('port') or 5672)}/{vhost}"


# ---- MQTT ------------------------------------------------------------------

def mqtt_kwargs(cred: dict) -> dict:
    kw: dict = {"hostname": cred.get("host") or "localhost", "port": int(cred.get("port") or 1883),
                "username": cred.get("username") or None, "password": cred.get("password") or None}
    if cred.get("client_id"):
        kw["identifier"] = cred["client_id"]
    if cred.get("ssl"):
        kw["tls_context"] = _ssl.create_default_context()
    return kw


# ---- NATS ------------------------------------------------------------------

def nats_kwargs(cred: dict) -> dict:
    kw: dict = {"servers": _csv(cred.get("servers")) or ["nats://localhost:4222"], "connect_timeout": 10,
                "allow_reconnect": False, "max_reconnect_attempts": 0}
    if cred.get("token"):
        kw["token"] = cred["token"]
    elif cred.get("username"):
        kw.update(user=cred["username"], password=cred.get("password") or "")
    return kw


# ---- Redis / Postgres ------------------------------------------------------

def redis_client(cred: dict, **extra):
    import redis.asyncio as aioredis
    from redis.asyncio.retry import Retry
    from redis.backoff import NoBackoff

    # No client-side retries: callers (listener supervisor, credential test) handle failures,
    # and redis-py's default retries made a wrong host take ~25s to report an error.
    return aioredis.Redis(host=cred.get("host") or "localhost", port=int(cred.get("port") or 6379), db=int(cred.get("db") or 0),
                          username=cred.get("username") or None, password=cred.get("password") or None,
                          ssl=bool(cred.get("ssl")), decode_responses=True,
                          **{"socket_connect_timeout": 10, "retry": Retry(NoBackoff(), 0), **extra})


def pg_kwargs(cred: dict) -> dict:
    return dict(host=cred.get("host") or "localhost", port=int(cred.get("port") or 5432), dbname=cred.get("database"),
                user=cred.get("user"), password=cred.get("password"), sslmode="require" if cred.get("ssl") else "prefer",
                connect_timeout=10)


# ---- AWS SigV4 (JSON protocol APIs such as SQS) ----------------------------

def aws_json_request(cred: dict, service: str, target: str, body: str, host: str | None = None) -> tuple[str, dict]:
    """Return (url, signed headers) for an AWS JSON-protocol POST (e.g. AmazonSQS.ReceiveMessage)."""
    region = cred.get("region") or "us-east-1"
    host = host or f"{service}.{region}.amazonaws.com"
    now = dt.datetime.now(dt.timezone.utc)
    amz_date, date = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    headers = {"content-type": "application/x-amz-json-1.0", "host": host, "x-amz-date": amz_date, "x-amz-target": target}
    if cred.get("session_token"):
        headers["x-amz-security-token"] = cred["session_token"]
    signed = ";".join(sorted(headers))
    canonical = "\n".join(["POST", "/", "", "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)), signed,
                           hashlib.sha256(body.encode()).hexdigest()])
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = ("AWS4" + cred["secret_access_key"]).encode()
    for part in (date, region, service, "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = f"AWS4-HMAC-SHA256 Credential={cred['access_key_id']}/{scope}, SignedHeaders={signed}, Signature={sig}"
    return f"https://{host}/", headers
