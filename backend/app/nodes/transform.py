"""Data utilities (n8n parity): Compare Datasets, Summarize, Rename Keys, Crypto, JWT, TOTP, XML,
HTML, Markdown, iCalendar."""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import re
import secrets
import struct
import time
import uuid
from datetime import datetime, timedelta, timezone

from .base import NodeContext, NodeError, NodeType, prop, register
from .core import DATA, get_path, set_path


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\"}


def unescape_sep(text: str) -> str:
    """Interpret only the escapes \\n, \\t, \\r, \\0 and \\\\ typed in a form field; every other character (é, →, 中) stays as is."""
    return re.sub(r"\\([ntr0\\])", lambda m: _ESCAPES[m.group(1)], str(text))


CMP = dict(group="data", color="#14b8a6")


# ---------------------------------------------------------------------------
# Compare Datasets
# ---------------------------------------------------------------------------


def _key(item: dict, fields: list[str]):
    return json.dumps([get_path(item, f) for f in fields], default=str, sort_keys=True)


async def _compare(ctx: NodeContext):
    a_items = ctx.inputs_by_handle.get("input1") or []
    b_items = ctx.inputs_by_handle.get("input2") or []
    pairs = [r for r in ctx.raw_params.get("fields") or [] if r.get("a") and r.get("b")]
    if not pairs:
        raise NodeError("Add at least one pair of fields to match on", ctx.name)
    skip = {f.strip() for f in str(ctx.param("skip_fields") or "").split(",") if f.strip()}
    prefer = ctx.param("on_difference") or "both"
    b_index: dict[str, list] = {}
    for b in b_items:
        b_index.setdefault(_key(b, [p["b"] for p in pairs]), []).append(b)
    out = {"a_only": [], "same": [], "different": [], "b_only": []}
    matched: set[int] = set()
    for a in a_items:
        hits = b_index.get(_key(a, [p["a"] for p in pairs]))
        if not hits:
            out["a_only"].append(a)
            continue
        b = hits.pop(0)
        matched.add(id(b))
        keys = (set(a) | set(b)) - skip
        diff = {k: {"inputA": a.get(k), "inputB": b.get(k)} for k in sorted(keys) if a.get(k) != b.get(k)}
        if not diff:
            out["same"].append(a)
        elif prefer == "a":
            out["different"].append(a)
        elif prefer == "b":
            out["different"].append(b)
        else:
            out["different"].append({"keys": {p["a"]: get_path(a, p["a"]) for p in pairs}, "same": {k: a.get(k) for k in keys if k not in diff},
                                     "different": diff})
    out["b_only"] = [b for b in b_items if id(b) not in matched]
    return out


register(NodeType(
    type="data.compare", label="Compare Datasets", icon="git-compare",
    description="Compare two inputs by key: items only in A, the same in both, different, or only in B.",
    properties=[
        prop("fields", "Fields to match", "collection", [{"a": "id", "b": "id"}],
             fields=[prop("a", "Input A field"), prop("b", "Input B field")]),
        prop("on_difference", "When items differ, output", "options", "both", options=[
            {"label": "Both versions (with the differences)", "value": "both"},
            {"label": "Input A version", "value": "a"}, {"label": "Input B version", "value": "b"}]),
        prop("skip_fields", "Fields to ignore when comparing", "string", "", placeholder="updated_at, etag"),
    ],
    inputs=["input1", "input2"], outputs=["a_only", "same", "different", "b_only"], defer=True,
    execute=_compare, **CMP,
))

# ---------------------------------------------------------------------------
# Summarize (group by + aggregations, like a pivot table)
# ---------------------------------------------------------------------------

AGGS = ["count", "count_unique", "sum", "average", "min", "max", "append", "append_unique", "concatenate"]


def _agg(op: str, values: list, sep: str):
    present = [v for v in values if v is not None and v != ""]
    nums = []
    for v in present:
        try:
            nums.append(float(v))
        except (TypeError, ValueError):
            pass
    if op == "count":
        return len(present)
    if op == "count_unique":
        return len({json.dumps(v, default=str) for v in present})
    if op == "sum":
        return _tidy(sum(nums))
    if op == "average":
        return _tidy(sum(nums) / len(nums)) if nums else None
    if op == "min":
        return _tidy(min(nums)) if nums else (min(present) if present else None)
    if op == "max":
        return _tidy(max(nums)) if nums else (max(present) if present else None)
    if op == "append":
        return present
    if op == "append_unique":
        seen, out = set(), []
        for v in present:
            k = json.dumps(v, default=str)
            if k not in seen:
                seen.add(k)
                out.append(v)
        return out
    if op == "concatenate":
        return sep.join(str(v) for v in present)
    raise NodeError(f"Unknown aggregation {op}")


def _tidy(n: float):
    return int(n) if float(n).is_integer() else round(n, 10)


async def _summarize(ctx: NodeContext):
    by = [f.strip() for f in str(ctx.param("split_by") or "").split(",") if f.strip()]
    aggs = [r for r in ctx.raw_params.get("aggregations") or [] if r.get("field") or r.get("aggregation") == "count"]
    sep = unescape_sep(ctx.param("separator") or ", ")
    groups: dict[str, list] = {}
    keys: dict[str, dict] = {}
    for it in ctx.items:
        k = _key(it, by)
        groups.setdefault(k, []).append(it)
        keys.setdefault(k, {f: get_path(it, f) for f in by})
    out = []
    for k, items in groups.items():
        row = dict(keys[k])
        for a in aggs:
            op, field = a.get("aggregation") or "count", a.get("field") or ""
            vals = [get_path(it, field) if field else 1 for it in items]
            row[f"{op}_{field}".rstrip("_") if field else op] = _agg(op, vals, sep)
        out.append(row)
    return {"main": out}


register(NodeType(
    type="data.summarize", label="Summarize", icon="sigma",
    description="Group items by fields and aggregate (count, sum, average, min, max, append…), like a pivot table.",
    properties=[
        prop("split_by", "Group by fields", "string", "", placeholder="country, plan (empty = one group)"),
        prop("aggregations", "Aggregations", "collection", [{"aggregation": "count", "field": ""}], fields=[
            prop("aggregation", "Aggregation", "options", "count", options=AGGS),
            prop("field", "Field"),
        ]),
        prop("separator", "Separator (concatenate)", "string", ", "),
    ],
    execute=_summarize, **DATA,
))

# ---------------------------------------------------------------------------
# Rename Keys
# ---------------------------------------------------------------------------


def _pop_path(obj: dict, path: str):
    parts = path.split(".")
    cur = obj
    for p in parts[:-1]:
        if not isinstance(cur, dict) or p not in cur:
            return False, None
        cur = cur[p]
    if isinstance(cur, dict) and parts[-1] in cur:
        return True, cur.pop(parts[-1])
    return False, None


async def _rename(ctx: NodeContext):
    rows = [r for r in ctx.raw_params.get("keys") or [] if r.get("current") and r.get("new")]
    out = []
    for it in ctx.items:
        new = copy.deepcopy(it)
        for r in rows:
            found, v = _pop_path(new, r["current"])
            if found:
                set_path(new, r["new"], v)
        out.append(new)
    return {"main": out}


register(NodeType(
    type="data.rename_keys", label="Rename Keys", icon="replace",
    description="Rename fields (dot paths supported).",
    properties=[prop("keys", "Keys", "collection", [{"current": "", "new": ""}],
                     fields=[prop("current", "Current key"), prop("new", "New key")])],
    execute=_rename, **DATA,
))

# ---------------------------------------------------------------------------
# Crypto
# ---------------------------------------------------------------------------

HASHES = ["md5", "sha1", "sha256", "sha384", "sha512", "sha3_256", "sha3_512"]


def _encode(raw: bytes, enc: str) -> str:
    return base64.b64encode(raw).decode() if enc == "base64" else raw.hex()


def _sign(value: bytes, pem: str, algo: str) -> bytes:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

    key = serialization.load_pem_private_key(pem.encode(), password=None)
    h = {"sha256": hashes.SHA256(), "sha384": hashes.SHA384(), "sha512": hashes.SHA512()}.get(algo, hashes.SHA256())
    if isinstance(key, rsa.RSAPrivateKey):
        return key.sign(value, padding.PKCS1v15(), h)
    if isinstance(key, ec.EllipticCurvePrivateKey):
        return key.sign(value, ec.ECDSA(h))
    if isinstance(key, ed25519.Ed25519PrivateKey):
        return key.sign(value)
    raise NodeError("Unsupported private key type (use RSA, EC or Ed25519 PEM)")


async def _crypto(ctx: NodeContext):
    out = []
    for i, it in enumerate(ctx.items or [{}]):
        p = ctx.params(i)
        action, enc = p.get("action") or "hash", p.get("encoding") or "hex"
        value = p.get("value")
        raw = (value if isinstance(value, str) else json.dumps(value, default=str)).encode() if value is not None else b""
        if p.get("binary_property") and action in ("hash", "hmac"):
            raw = ctx.file(it, p["binary_property"])[0]
        if action == "hash":
            res = _encode(hashlib.new(p.get("algorithm") or "sha256", raw).digest(), enc)
        elif action == "hmac":
            res = _encode(hmac.new(str(p.get("secret") or "").encode(), raw, p.get("algorithm") or "sha256").digest(), enc)
        elif action == "sign":
            res = _encode(_sign(raw, str(p.get("private_key") or ""), p.get("algorithm") or "sha256"), enc)
        elif action == "generate":
            kind = p.get("generate") or "uuid"
            length = max(1, int(p.get("length") or 32))
            res = (str(uuid.uuid4()) if kind == "uuid" else secrets.token_hex(length // 2 + 1)[:length] if kind == "hex"
                   else secrets.token_urlsafe(length)[:length] if kind == "base64" else
                   "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789") for _ in range(length)))
        else:
            raise NodeError(f"Unknown action {action}")
        new = copy.deepcopy(it)
        set_path(new, p.get("output_field") or "data", res)
        out.append(new)
    return {"main": out}


register(NodeType(
    type="data.crypto", label="Crypto", icon="lock",
    description="Hash, HMAC, sign with a private key, or generate random values (UUID, hex, tokens).",
    properties=[
        prop("action", "Action", "options", "hash", options=[
            {"label": "Hash", "value": "hash"}, {"label": "HMAC", "value": "hmac"},
            {"label": "Sign (private key)", "value": "sign"}, {"label": "Generate random value", "value": "generate"}]),
        prop("algorithm", "Algorithm", "options", "sha256", options=HASHES, show_if={"action": ["hash", "hmac", "sign"]}),
        prop("value", "Value", "string", "{{ $json.text }}", show_if={"action": ["hash", "hmac", "sign"]}),
        prop("binary_property", "Or hash this file (binary property)", "string", "", show_if={"action": ["hash", "hmac"]}),
        prop("secret", "Secret", "string", "", show_if={"action": ["hmac"]}),
        prop("private_key", "Private key (PEM)", "text", "", rows=4, show_if={"action": ["sign"]}),
        prop("generate", "Type", "options", "uuid", options=["uuid", "hex", "base64", "alphanumeric"], show_if={"action": ["generate"]}),
        prop("length", "Length", "number", 32, show_if={"generate": ["hex", "base64", "alphanumeric"]}),
        prop("encoding", "Encoding", "options", "hex", options=["hex", "base64"], show_if={"action": ["hash", "hmac", "sign"]}),
        prop("output_field", "Output field", "string", "data"),
    ],
    execute=_crypto, **DATA,
))

# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------


async def _jwt(ctx: NodeContext):
    import jwt

    out = []
    for i, it in enumerate(ctx.items or [{}]):
        p = ctx.params(i)
        op, alg = p.get("operation") or "sign", p.get("algorithm") or "HS256"
        key = str(p.get("key") or "")
        if not key and op != "decode":
            raise NodeError("Set the secret / key", ctx.name)
        try:
            if op == "sign":
                claims = p.get("payload")
                claims = json.loads(claims) if isinstance(claims, str) and claims.strip() else (claims or {})
                if p.get("expires_in"):
                    claims["exp"] = int(time.time()) + int(p["expires_in"])
                claims.setdefault("iat", int(time.time()))
                res: dict = {"token": jwt.encode(claims, key, algorithm=alg)}
            elif op == "verify":
                res = {"payload": jwt.decode(str(p.get("token") or ""), key, algorithms=[alg],
                                             options={"verify_aud": bool(p.get("audience"))}, audience=p.get("audience") or None),
                       "valid": True}
            else:
                tok = str(p.get("token") or "")
                res = {"header": jwt.get_unverified_header(tok), "payload": jwt.decode(tok, options={"verify_signature": False})}
        except jwt.PyJWTError as e:
            if op == "verify" and p.get("continue_invalid"):
                res = {"valid": False, "error": str(e)}
            else:
                raise NodeError(f"JWT {op} failed: {e}", ctx.name) from e
        out.append({**it, **res})
    return {"main": out}


register(NodeType(
    type="data.jwt", label="JWT", icon="key-round",
    description="Sign, verify or decode JSON Web Tokens (HS*/RS*/ES*).",
    properties=[
        prop("operation", "Operation", "options", "sign", options=["sign", "verify", "decode"]),
        prop("algorithm", "Algorithm", "options", "HS256", options=["HS256", "HS384", "HS512", "RS256", "RS384", "RS512", "ES256", "ES384", "EdDSA"],
             show_if={"operation": ["sign", "verify"]}),
        prop("key", "Secret or key (PEM: private to sign, public to verify)", "text", "", rows=3, show_if={"operation": ["sign", "verify"]}),
        prop("payload", "Claims (JSON)", "json", '{ "sub": "{{ $json.id }}" }', show_if={"operation": ["sign"]}),
        prop("expires_in", "Expires in (seconds)", "number", 3600, show_if={"operation": ["sign"]}),
        prop("token", "Token", "string", "{{ $json.token }}", show_if={"operation": ["verify", "decode"]}),
        prop("audience", "Expected audience", "string", "", show_if={"operation": ["verify"]}),
        prop("continue_invalid", "Output {valid: false} instead of failing", "boolean", False, show_if={"operation": ["verify"]}),
    ],
    execute=_jwt, **DATA,
))

# ---------------------------------------------------------------------------
# TOTP
# ---------------------------------------------------------------------------


def totp(secret: str, digits: int = 6, period: int = 30, algo: str = "sha1", at: float | None = None) -> str:
    key = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret.replace(" ", "")) % 8))
    counter = int((at if at is not None else time.time()) // period)
    digest = hmac.new(key, struct.pack(">Q", counter), algo).digest()
    o = digest[-1] & 0x0F
    return str((struct.unpack(">I", digest[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** digits).zfill(digits)


async def _totp(ctx: NodeContext):
    out = []
    for i, it in enumerate(ctx.items or [{}]):
        p = ctx.params(i)
        if not str(p.get("secret") or "").strip():
            raise NodeError("TOTP secret is empty (base32, e.g. JBSWY3DPEHPK3PXP)", ctx.name)
        try:
            code = totp(str(p.get("secret") or ""), int(p.get("digits") or 6), int(p.get("period") or 30), p.get("algorithm") or "sha1")
        except (ValueError, TypeError) as e:
            raise NodeError(f"Invalid TOTP secret (base32 expected): {e}", ctx.name) from e
        period = int(p.get("period") or 30)
        out.append({**it, "token": code, "secondsRemaining": period - int(time.time()) % period})
    return {"main": out}


register(NodeType(
    type="data.totp", label="TOTP", icon="timer-reset",
    description="Generate a time-based one-time password (2FA code) from a base32 secret.",
    properties=[prop("secret", "Secret (base32)", "string", ""), prop("digits", "Digits", "number", 6),
                prop("period", "Period (seconds)", "number", 30),
                prop("algorithm", "Algorithm", "options", "sha1", options=["sha1", "sha256", "sha512"])],
    execute=_totp, **DATA,
))

# ---------------------------------------------------------------------------
# XML, HTML, Markdown
# ---------------------------------------------------------------------------


async def _xml(ctx: NodeContext):
    import xmltodict

    out = []
    for i, it in enumerate(ctx.items or [{}]):
        p = ctx.params(i)
        field = p.get("field") or "data"
        new = copy.deepcopy(it)
        try:
            if p.get("mode") == "to_json":
                src = get_path(it, field)
                set_path(new, p.get("output_field") or field, xmltodict.parse(str(src or ""), attr_prefix=p.get("attr_prefix") or "@"))
            else:
                data = get_path(it, field) if p.get("source") == "field" else {k: v for k, v in it.items() if k != "binary"}
                root = p.get("root") or "root"
                body = data if isinstance(data, dict) and len(data) == 1 and p.get("source") == "field" else {root: data}
                set_path(new, p.get("output_field") or "data",
                         xmltodict.unparse(body, pretty=bool(p.get("pretty", True)), attr_prefix=p.get("attr_prefix") or "@"))
        except Exception as e:  # noqa: BLE001
            raise NodeError(f"XML conversion failed: {e}", ctx.name) from e
        out.append(new)
    return {"main": out}


register(NodeType(
    type="data.xml", label="XML", icon="code-xml",
    description="Convert XML to JSON or JSON to XML.",
    properties=[
        prop("mode", "Mode", "options", "to_json", options=[{"label": "XML → JSON", "value": "to_json"}, {"label": "JSON → XML", "value": "to_xml"}]),
        prop("field", "Field", "string", "data"),
        prop("source", "Convert", "options", "item", options=[{"label": "The whole item", "value": "item"}, {"label": "One field", "value": "field"}],
             show_if={"mode": ["to_xml"]}),
        prop("root", "Root element", "string", "root", show_if={"mode": ["to_xml"]}),
        prop("pretty", "Pretty print", "boolean", True, show_if={"mode": ["to_xml"]}),
        prop("attr_prefix", "Attribute prefix", "string", "@"),
        prop("output_field", "Output field", "string", ""),
    ],
    execute=_xml, **DATA,
))


async def _html(ctx: NodeContext):
    from bs4 import BeautifulSoup

    out = []
    for i, it in enumerate(ctx.items or [{}]):
        p = ctx.params(i)
        new = copy.deepcopy(it)
        if p.get("operation") == "generate":
            set_path(new, p.get("output_field") or "html", str(p.get("template") or ""))
            out.append(new)
            continue
        html = ctx.file(it, p.get("binary_property") or "data")[0].decode("utf-8", "replace") if p.get("source") == "binary" \
            else str(get_path(it, p.get("field") or "data") or "")
        soup = BeautifulSoup(html, "html.parser")
        for row in ctx.raw_params.get("extract") or []:
            if not row.get("key") or not row.get("selector"):
                continue
            els = soup.select(row["selector"])
            how = row.get("return") or "text"

            def val(el, how=how, row=row):
                return (el.get_text(" ", strip=True) if how == "text" else str(el) if how == "html"
                        else el.get(row.get("attribute") or "href") if how == "attribute" else el.decode_contents())
            vals = [val(e) for e in els]
            set_path(new, row["key"], vals if row.get("array") else (vals[0] if vals else None))
        out.append(new)
    return {"main": out}


register(NodeType(
    type="data.html", label="HTML", icon="file-code",
    description="Extract content from HTML with CSS selectors, or generate HTML from a template.",
    properties=[
        prop("operation", "Operation", "options", "extract", options=[
            {"label": "Extract HTML content", "value": "extract"}, {"label": "Generate HTML template", "value": "generate"}]),
        prop("source", "HTML from", "options", "field", options=[{"label": "A field", "value": "field"}, {"label": "A file (binary)", "value": "binary"}],
             show_if={"operation": ["extract"]}),
        prop("field", "Field", "string", "data", show_if={"source": ["field"]}),
        prop("binary_property", "Binary property", "string", "data", show_if={"source": ["binary"]}),
        prop("extract", "Values to extract", "collection", [{"key": "title", "selector": "title", "return": "text", "array": False}], fields=[
            prop("key", "Key"), prop("selector", "CSS selector"),
            prop("return", "Return", "options", "text", options=["text", "html", "inner_html", "attribute"]),
            prop("attribute", "Attribute", "string", "href"), prop("array", "Return all matches", "boolean", False),
        ], show_if={"operation": ["extract"]}),
        prop("template", "HTML template", "code", "<h1>{{ $json.title }}</h1>", language="html", rows=8, show_if={"operation": ["generate"]}),
        prop("output_field", "Output field", "string", "html", show_if={"operation": ["generate"]}),
    ],
    execute=_html, **DATA,
))


async def _markdown(ctx: NodeContext):
    import markdown as md
    from markdownify import markdownify

    out = []
    for i, it in enumerate(ctx.items or [{}]):
        p = ctx.params(i)
        src = str(p.get("value") or "")
        res = md.markdown(src, extensions=["tables", "fenced_code"]) if p.get("mode") == "to_html" else markdownify(src, heading_style="ATX").strip()
        new = copy.deepcopy(it)
        set_path(new, p.get("output_field") or "data", res)
        out.append(new)
    return {"main": out}


register(NodeType(
    type="data.markdown", label="Markdown", icon="file-type",
    description="Convert Markdown to HTML or HTML to Markdown.",
    properties=[prop("mode", "Mode", "options", "to_html", options=[{"label": "Markdown → HTML", "value": "to_html"},
                                                                     {"label": "HTML → Markdown", "value": "to_markdown"}]),
                prop("value", "Value", "text", "{{ $json.text }}", rows=3), prop("output_field", "Output field", "string", "data")],
    execute=_markdown, **DATA,
))

# ---------------------------------------------------------------------------
# iCalendar
# ---------------------------------------------------------------------------


def _ics_text(s: str) -> str:
    s = str(s).replace("\r\n", "\n").replace("\r", "\n")
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_value(s: str) -> str:
    """A value that is not TEXT (URL, address): no CR/LF, so it can't start a new property."""
    return re.sub(r"[\r\n]+", "", str(s))


def _ics_fold(line: str) -> str:
    """RFC 5545 3.1: lines longer than 75 octets continue on the next line after CRLF + space (UTF-8 safe)."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, cur, size, limit = [], [], 0, 75
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > limit:
            parts.append("".join(cur))
            cur, size, limit = [], 0, 74  # continuation lines start with a space
        cur.append(ch)
        size += n
    parts.append("".join(cur))
    return "\r\n ".join(parts)


def _ics_time(v, all_day: bool) -> str:
    dt = v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    if all_day:
        return dt.strftime("%Y%m%d")
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


async def _ical(ctx: NodeContext):
    out = []
    for i, it in enumerate(ctx.items or [{}]):
        p = ctx.params(i)
        all_day = bool(p.get("all_day"))
        start = p.get("start") or datetime.now(timezone.utc).isoformat()
        try:
            start_dt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(str(p["end"]).replace("Z", "+00:00")) if p.get("end") else None
        except ValueError as e:
            raise NodeError(f"Start / end must be ISO dates, e.g. 2024-05-01T09:00:00Z ({e})", ctx.name) from e
        if all_day:
            # DTEND of an all-day event is exclusive (RFC 5545): the day after the last day
            last = end_dt.date() if end_dt and end_dt.date() >= start_dt.date() else start_dt.date()
            start, end = start_dt.date().isoformat(), (last + timedelta(days=1)).isoformat()
        else:
            end = (end_dt or start_dt + timedelta(hours=1)).isoformat()
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//DebBuilder//iCalendar//EN", "BEGIN:VEVENT",
                 f"UID:{uuid.uuid4()}@debbuilder", f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
                 f"DTSTART{';VALUE=DATE' if all_day else ''}:{_ics_time(start, all_day)}",
                 f"DTEND{';VALUE=DATE' if all_day else ''}:{_ics_time(end, all_day)}",
                 f"SUMMARY:{_ics_text(p.get('title') or 'Event')}"]
        for key, field in (("DESCRIPTION", "description"), ("LOCATION", "location"), ("URL", "url")):
            if p.get(field):
                lines.append(f"{key}:{_ics_value(p[field]) if key == 'URL' else _ics_text(p[field])}")
        for a in [re.sub(r"[\r\n;:,\"<>\s]+", "", x) for x in re.split(r"[,\r\n]+", str(p.get("attendees") or ""))]:
            if a:
                lines.append(f"ATTENDEE;RSVP=TRUE:mailto:{a}")
        lines += ["END:VEVENT", "END:VCALENDAR"]
        ref = ctx.put_file(("\r\n".join(_ics_fold(x) for x in lines) + "\r\n").encode(), p.get("file_name") or "invite.ics", "text/calendar")
        out.append({**{k: v for k, v in it.items() if k != "binary"}, "binary": {**(it.get("binary") or {}), "data": ref}})
    return {"main": out}


register(NodeType(
    type="data.ical", label="iCalendar", icon="calendar-plus",
    description="Create an .ics calendar invite file (attach it to an email, upload it…).",
    properties=[prop("title", "Title", "string", "{{ $json.title }}"), prop("start", "Start (ISO)", "string", "{{ $json.start }}"),
                prop("end", "End (ISO, empty = start + 1 h)", "string", ""), prop("all_day", "All-day (end = last day, inclusive)", "boolean", False),
                prop("description", "Description", "text", "", rows=2), prop("location", "Location", "string", ""),
                prop("url", "URL", "string", ""), prop("attendees", "Attendee emails (comma separated)", "string", ""),
                prop("file_name", "File name", "string", "invite.ics")],
    execute=_ical, **DATA,
))
