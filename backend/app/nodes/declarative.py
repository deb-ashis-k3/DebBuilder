"""Declarative integration nodes (n8n's "declarative / routing" style).

A service is data: credential types, a base URL, and operations that map form fields to an HTTP
request. One generic executor handles auth (via the credential catalog), path/query/body building,
form or JSON bodies, GraphQL, pagination ("Return all"), item splitting, file upload/download, and
API error messages. Every service also gets n8n's "Custom API Call".

    service("todoist", "Todoist", ["todoist"], "https://api.todoist.com/rest/v2", group="productivity",
        ops=[op("task", "create", "Create a task", "POST", "/tasks", F("content", req=True), F("due_string"))])

Field placement: `{name}` in the path → path; otherwise GET/DELETE → query and POST/PUT/PATCH → body
(override with where=...). `to="a.b"` nests a body value. Every create/update op also accepts
"Additional fields (JSON)" merged into the body (or query), for anything not modelled.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import quote

import httpx

from ..netguard import guarded_client
from ..credentials import fmt, request_auth, with_defaults
from .base import NodeContext, NodeError, NodeType, prop, register
from .core import set_path

# ---------------------------------------------------------------------------
# Spec DSL
# ---------------------------------------------------------------------------


@dataclass
class F:
    name: str
    label: str | None = None
    type: str = "string"  # string | text | number | boolean | json | list | options | date
    req: bool = False
    default: Any = None
    to: str | None = None  # body/query key (dot path), defaults to name
    where: str | None = None  # path | query | body | header | none (only for body()/headers() builders)
    options: list | None = None
    desc: str = ""
    transform: str | None = None  # md5_lower | base64 | int | adf | lower
    sep: str | None = ","  # GET list → "a,b" (None: repeat the key, e.g. statuses[]=a&statuses[]=b)


@dataclass
class Op:
    resource: str
    action: str
    label: str
    method: str
    path: str
    fields: list[F] = field(default_factory=list)
    items: str | None = None  # dot path of the list to split into items
    out: str | None = None  # dot path to unwrap a single object
    paginate: Any = None  # a Paginator; enables "Return all" + "Limit"
    wrap: str | None = None  # body becomes {wrap: body}
    body: Callable[[dict], Any] | None = None  # build the body from field values
    send: str | None = None  # where non-path fields go: query | body | form | multipart (default by method)
    extra: bool | None = None  # offer "Additional fields (JSON)" (default: POST/PUT/PATCH)
    headers: Callable[[dict], dict] | None = None
    graphql: str | None = None  # GraphQL document; variables = field values (or body(values))
    upload: bool = False  # request content = the item's file
    download: bool = False  # response bytes → item file
    base: str | None = None  # override the service base URL for this op
    derive: dict[str, Callable[[dict], Any]] | None = None  # extra path values computed from fields (e.g. Mailchimp's email hash)
    # Multi-request operations (Sheets append by header, Drive multipart upload…):
    # async (call, values, item) -> list[dict], where `call(method, url_or_path, **httpx_kwargs)` is authenticated.
    custom: Callable | None = None

    @property
    def key(self) -> str:
        return f"{self.resource}.{self.action}"


def op(resource: str, action: str, label: str, method: str, path: str, *fields: F, **kw) -> Op:
    return Op(resource, action, label, method, path, list(fields), **kw)


# ---- pagination strategies ---------------------------------------------------

@dataclass
class Page:
    """?page=N&per_page=size until a short page."""
    param: str = "page"
    size_param: str | None = "per_page"
    size: int = 100
    start: int = 1


@dataclass
class Offset:
    param: str = "offset"
    size_param: str | None = "limit"
    size: int = 100


@dataclass
class Cursor:
    """Send `param`=<value at `next` in the last response> until it's empty."""
    param: str
    next: str
    location: str = "query"  # query | body
    size_param: str | None = None
    size: int = 100


@dataclass
class NextUrl:
    """The response holds the full URL of the next page (Zendesk `next_page`, Bitbucket `next`)."""
    next: str


class Link:
    """RFC 5988 `Link: <…>; rel="next"` (GitHub, GitLab, Shopify, Sentry)."""


@dataclass
class AfterId:
    """Stripe-style: `starting_after=<last id>` while `has_more`."""
    param: str = "starting_after"
    has_more: str = "has_more"
    size_param: str = "limit"
    size: int = 100


@dataclass
class Service:
    key: str
    label: str
    credentials: list[str]
    base: str | Callable[[dict], str]
    ops: list[Op]
    group: str = "integration"
    color: str = "#6366f1"
    icon: str = "plug"
    description: str = ""
    ok_field: str | None = None  # Slack/Cloudflare: 200 + {"ok": false}
    error_path: str | None = None
    auth: Callable | None = None  # async (cred) -> (headers, params) for non-catalog auth (PayPal)
    custom_call: bool = True


SERVICES: dict[str, Service] = {}


def _dig(obj: Any, path: str | None) -> Any:
    if not path:
        return obj
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.lstrip("-").isdigit() and -len(obj) <= int(part) < len(obj):
            obj = obj[int(part)]  # "-1" = last element
        else:
            return None
    return obj


def _label(name: str) -> str:
    s = re.sub(r"(?<!^)(?=[A-Z])", " ", name).replace("_", " ").strip()
    return s[:1].upper() + s[1:]


def adf(text: Any) -> dict:
    """Plain text → Atlassian Document Format (Jira/Confluence rich text)."""
    return {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": line}] if line else []} for line in str(text or "").split("\n")]}


_TRANSFORMS: dict[str, Callable[[Any], Any]] = {
    "md5_lower": lambda v: hashlib.md5(str(v).strip().lower().encode()).hexdigest(),  # noqa: S324 - Mailchimp subscriber hash
    "base64": lambda v: base64.b64encode(v if isinstance(v, bytes) else str(v).encode()).decode(),
    "int": lambda v: int(float(v)),
    "lower": lambda v: str(v).lower(),
    "adf": adf,
}

# ---------------------------------------------------------------------------
# Node generation
# ---------------------------------------------------------------------------

_PROP_TYPE = {"string": "string", "text": "text", "number": "number", "boolean": "boolean", "json": "json",
              "list": "string", "options": "options", "date": "string"}


def _props(svc: Service) -> list[dict]:
    ops = svc.ops
    choices = [{"label": f"{_label(o.resource)}: {o.label}", "value": o.key} for o in ops]
    if svc.custom_call:
        choices.append({"label": "Custom API call", "value": "custom.call"})
    props = [
        prop("credential", "Credential", "credential", None, credential_types=svc.credentials, required=True),
        prop("operation", "Operation", "options", ops[0].key, options=choices),
    ]
    by_name: dict[str, dict] = {}
    for o in ops:
        for f in o.fields:
            key = f.name
            prev = by_name.get(key)
            if prev and (prev["type"] != _PROP_TYPE[f.type] or prev.get("_label") != (f.label or _label(f.name))):
                key = f"{o.resource}_{f.name}"  # same name, different meaning: keep them apart
                prev = by_name.get(key)
            if prev:
                prev["show_if"]["operation"].append(o.key)
                prev["required"] = prev.get("required", False) or f.req
                continue
            label = f.label or _label(f.name)
            p = prop(key, label + (" (comma separated)" if f.type == "list" else ""), _PROP_TYPE[f.type], f.default,
                     options=f.options, required=f.req, description=f.desc, show_if={"operation": [o.key]},
                     rows=3 if f.type == "text" else None)
            p["_label"] = label
            by_name[key] = p
            f.__dict__["_key"] = key
    props += list(by_name.values())
    for p in props:
        p.pop("_label", None)
    extra_ops = [o.key for o in ops if (o.extra if o.extra is not None else o.method in ("POST", "PUT", "PATCH")) and not o.graphql]
    if extra_ops:
        props.append(prop("additional", "Additional fields (JSON)", "json", "{}", show_if={"operation": extra_ops},
                          description="Any other API fields, merged into the request body"))
    if file_ops := [o.key for o in ops if o.upload or o.download]:
        props.append(prop("binary_property", "Binary property", "string", "data", show_if={"operation": file_ops}))
    if paged := [o.key for o in ops if o.paginate is not None]:
        props.append(prop("return_all", "Return all", "boolean", False, show_if={"operation": paged}))
        props.append(prop("limit", "Limit", "number", 50, show_if={"operation": paged}))
    if svc.custom_call:
        c = {"operation": ["custom.call"]}
        props += [prop("custom_method", "Method", "options", "GET", options=["GET", "POST", "PUT", "PATCH", "DELETE"], show_if=c),
                  prop("custom_path", "Path (after the base URL, or a full URL)", "string", "/", show_if=c),
                  prop("custom_query", "Query parameters", "collection", [], fields=[prop("name", "Name"), prop("value", "Value", "string", "")],
                       show_if=c),
                  prop("custom_body", "Body (JSON)", "json", "", show_if=c)]
    seen: set[str] = set()
    for p in props:
        if p["name"] in seen:
            raise ValueError(f"{svc.key}: two form fields named '{p['name']}' (rename one in the spec)")
        seen.add(p["name"])
    return props


def service(key: str, label: str, credentials: list[str], base, *, ops: list[Op], **kw) -> Service:
    svc = Service(key, label, credentials, base, ops, **kw)
    SERVICES[key] = svc

    async def execute(ctx: NodeContext):
        return await _execute(ctx, svc)

    register(NodeType(
        type=f"app.{key}", label=label, icon=svc.icon,
        description=svc.description or f"{label}: {', '.join(sorted({_label(o.resource) for o in ops}))}, plus any API call.",
        properties=_props(svc), execute=execute, group=svc.group, color=svc.color,
    ))
    return svc


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _value(f: F, raw: Any) -> Any:
    if raw is None or raw == "":
        return None
    if f.type == "number":
        try:
            n = float(raw)
            return int(n) if n.is_integer() else n
        except (TypeError, ValueError):
            raise NodeError(f"'{f.label or _label(f.name)}' must be a number") from None
    if f.type == "boolean":
        return raw if isinstance(raw, bool) else str(raw).lower() in ("true", "1", "yes")
    if f.type == "json" and isinstance(raw, str):
        try:
            return json.loads(raw) if raw.strip() else None
        except ValueError as e:
            raise NodeError(f"'{f.label or _label(f.name)}' is not valid JSON: {e}") from e
    if f.type == "list":
        return raw if isinstance(raw, list) else [x.strip() for x in str(raw).split(",") if x.strip()]
    return raw


def _flatten_form(data: Any, prefix: str = "") -> list[tuple[str, str]]:
    """Nested dict/list → Stripe/Twilio-style form keys: metadata[a]=1, items[0][price]=x."""
    out: list[tuple[str, str]] = []
    if isinstance(data, dict):
        for k, v in data.items():
            out += _flatten_form(v, f"{prefix}[{k}]" if prefix else str(k))
    elif isinstance(data, list):
        for i, v in enumerate(data):
            out += _flatten_form(v, f"{prefix}[{i}]")
    elif data is not None:
        out.append((prefix, "true" if data is True else "false" if data is False else str(data)))
    return out


def _api_error(svc: Service, r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text[:300] or r.reason_phrase
    for path in [svc.error_path, "message", "error.message", "error_description", "error", "errors.0.message", "errors.0.detail",
                 "errors.0", "detail", "description", "title"]:
        v = _dig(body, path) if path else None
        if v:
            return v if isinstance(v, str) else json.dumps(v)[:300]
    return json.dumps(body)[:300]


def _base(svc: Service, o: Op | None, cred: dict) -> str:
    base = (o.base if o and o.base else None) or svc.base
    return (base(cred) if callable(base) else fmt(base, cred)).rstrip("/")


async def _execute(ctx: NodeContext, svc: Service):
    out: list[dict] = []
    ops = {o.key: o for o in svc.ops}
    async with guarded_client(timeout=60, follow_redirects=True) as client:
        for i, item in enumerate(ctx.items or [{}]):
            p = ctx.params(i)
            key = p.get("operation") or svc.ops[0].key
            o = ops.get(key)
            if o is None and key != "custom.call":  # checked before any request (incl. token requests)
                raise NodeError(f"Unknown operation '{key}'", ctx.name)
            ctype, cred = await ctx.credential("credential", i)
            cred = with_defaults(ctype, cred)
            if svc.auth:
                headers, params = await svc.auth(cred)
                auth = None
            else:
                headers, params, auth = await request_auth(ctype, cred, ctx.param("credential", i))
            if key == "custom.call":
                out += await _custom(ctx, svc, client, p, cred, headers, params, auth)
                continue
            out += await _run_op(ctx, svc, o, client, p, item, cred, dict(headers), dict(params), auth)
    return {"main": out}


async def _custom(ctx, svc, client, p, cred, headers, params, auth) -> list[dict]:
    path = str(p.get("custom_path") or "/")
    url = path if path.startswith("http") else f"{_base(svc, None, cred)}/{path.lstrip('/')}"
    q = {**params, **{r["name"]: r.get("value") for r in p.get("custom_query") or [] if r.get("name")}}
    body = p.get("custom_body")
    try:
        body = json.loads(body) if isinstance(body, str) and body.strip() else (body or None)
    except ValueError as e:
        raise NodeError(f"Custom body is not valid JSON: {e}", ctx.name) from e
    r = await client.request(p.get("custom_method") or "GET", url, params=q or None, json=body, headers=headers, auth=auth)
    return _items(svc, r, None, None)


def _items(svc: Service, r: httpx.Response, items_path: str | None, out_path: str | None) -> list[dict]:
    if r.status_code >= 400:
        raise NodeError(f"{svc.label} API error {r.status_code}: {_api_error(svc, r)}")
    if not r.content:
        return [{"success": True, "status": r.status_code}]
    try:
        data = r.json()
    except ValueError:
        return [{"data": r.text}]
    if svc.ok_field and isinstance(data, dict) and data.get(svc.ok_field) is False:
        raise NodeError(f"{svc.label} API error: {_api_error(svc, r)}")
    if items_path is not None:
        data = _dig(data, items_path) or []
    elif out_path:
        data = _dig(data, out_path) if _dig(data, out_path) is not None else data
    rows = data if isinstance(data, list) else [data]
    return [x if isinstance(x, dict) else {"value": x} for x in rows]


async def _run_op(ctx, svc: Service, o: Op, client: httpx.AsyncClient, p: dict, item: dict, cred: dict,
                  headers: dict, params: dict, auth) -> list[dict]:
    values: dict[str, Any] = {}
    path_vals: dict[str, str] = {}
    query: dict[str, Any] = dict(params)
    body: dict[str, Any] = {}
    placeholders = set(re.findall(r"{(\w+)}", o.path))
    send = o.send or ("query" if o.method in ("GET", "DELETE") else "body")
    for f in o.fields:
        v = _value(f, p.get(f.__dict__.get("_key", f.name)))
        if v is None:
            if f.req:
                raise NodeError(f"'{f.label or _label(f.name)}' is required for {_label(o.resource)}: {o.label}", ctx.name)
            continue
        if f.transform:
            v = _TRANSFORMS[f.transform](v)
        values[f.name] = v
        where = f.where or ("path" if f.name in placeholders else send if send not in ("form", "multipart") else "body")
        if where == "none":
            continue
        if where == "path":
            path_vals[f.name] = quote(str(v), safe="") if not str(v).startswith("/") else quote(str(v), safe="/")
        elif where == "query":
            query[f.to or f.name] = f.sep.join(map(str, v)) if isinstance(v, list) and f.type == "list" and f.sep else v
        elif where == "header":
            headers[f.to or f.name] = str(v)
        else:
            set_path(body, f.to or f.name, v)
    for name, fn in (o.derive or {}).items():
        path_vals[name] = quote(str(fn(values)), safe="")
    extra = p.get("additional")
    extra = json.loads(extra) if isinstance(extra, str) and extra.strip() else (extra or {})
    if not isinstance(extra, dict):
        raise NodeError("Additional fields must be a JSON object", ctx.name)
    if send == "query":
        query.update(extra)
    else:
        body.update(extra)
    if o.body:
        body = o.body({**values, **extra})
    if o.wrap and not o.body:
        body = {o.wrap: body}
    if o.headers:
        headers.update(o.headers(values))
    url = _base(svc, o, cred) + o.path.format(**{k: path_vals.get(k, "") for k in placeholders})
    kwargs: dict = {"headers": headers, "auth": auth}

    if o.custom:
        base_url = _base(svc, o, cred)

        async def call(method: str, target: str, **kw) -> Any:
            r = await client.request(method, target if target.startswith("http") else base_url + target,
                                     headers={**headers, **kw.pop("headers", {})}, auth=auth, params={**params, **kw.pop("params", {})} or None, **kw)
            if r.status_code >= 400:
                raise NodeError(f"{svc.label} API error {r.status_code}: {_api_error(svc, r)}", ctx.name)
            return r.json() if r.content and "json" in r.headers.get("content-type", "") else r
        return await o.custom(call, {**values, **extra}, item, ctx, p)  # raw values: handlers encode what they put in URLs

    if o.graphql:
        kwargs["json"] = {"query": o.graphql, "variables": o.body(values) if o.body else values}
    elif o.upload:
        data, ref = ctx.file(item, p.get("binary_property") or "data")
        kwargs["content"] = data
        headers.setdefault("Content-Type", ref.get("mimeType") or "application/octet-stream")
    elif (o.method not in ("GET", "DELETE") and (body or not o.download)) or (o.body and body):
        # (a download with nothing to send, e.g. Dropbox's content endpoints, gets no body at all)
        if send == "form":
            kwargs["data"] = dict(_flatten_form(body))
        elif send == "multipart":
            kwargs["files"] = {k: (None, v) for k, v in _flatten_form(body)}
        elif body or o.method in ("POST", "PUT", "PATCH"):
            kwargs["json"] = body

    if o.download:
        r = await client.request(o.method, url, params=query or None, **kwargs)
        if r.status_code >= 400:
            raise NodeError(f"{svc.label} API error {r.status_code}: {_api_error(svc, r)}", ctx.name)
        name = re.search(r'filename="?([^";]+)', r.headers.get("content-disposition", ""))
        ref = ctx.put_file(r.content, name.group(1) if name else f"{o.resource}.{(r.headers.get('content-type') or 'bin').split('/')[-1].split(';')[0]}",
                           (r.headers.get("content-type") or "").split(";")[0] or None)
        return [{**{k: v for k, v in item.items() if k != "binary"}, "binary": {**(item.get("binary") or {}), p.get("binary_property") or "data": ref}}]

    if o.paginate is None:
        r = await client.request(o.method, url, params=query or None, **kwargs)
        return _items(svc, r, o.items, o.out)
    return await _paginate(svc, o, client, url, query, kwargs, bool(p.get("return_all")), int(p.get("limit") or 50))


async def _paginate(svc: Service, o: Op, client, url: str, query: dict, kwargs: dict, return_all: bool, limit: int) -> list[dict]:
    pg = o.paginate
    want = None if return_all else max(1, limit)
    out: list[dict] = []
    q = dict(query)
    size = getattr(pg, "size", 100)
    if getattr(pg, "size_param", None):
        q[pg.size_param] = min(size, want) if want else size
    if isinstance(pg, Page):
        q[pg.param] = pg.start
    if isinstance(pg, Offset):
        q[pg.param] = 0
    next_url: str | None = url
    for _ in range(1000):  # hard stop
        if isinstance(pg, Cursor) and pg.location == "body" and "json" in kwargs:
            kwargs["json"] = {**(kwargs["json"] or {}), **{k: v for k, v in q.items() if k in (pg.param, pg.size_param)}}
            send_q = {k: v for k, v in q.items() if k not in (pg.param, pg.size_param)}
        else:
            send_q = q
        r = await client.request(o.method, next_url, params=send_q or None, **kwargs)
        page = _items(svc, r, o.items, o.out)
        data = r.json() if r.content else {}
        out += page
        if (want and len(out) >= want) or not page:
            break
        if isinstance(pg, Link):
            m = re.search(r'<([^>]+)>;\s*rel="?next"?', r.headers.get("link", ""))
            if not m:
                break
            next_url, q = m.group(1), {}  # the next link carries every parameter
        elif isinstance(pg, NextUrl):
            nxt = _dig(data, pg.next)
            if not nxt:
                break
            next_url, q = nxt, {}
        elif isinstance(pg, Cursor):
            nxt = _dig(data, pg.next)
            if not nxt:
                break
            q[pg.param] = nxt
        elif isinstance(pg, AfterId):
            if not _dig(data, pg.has_more):
                break
            q[pg.param] = page[-1].get("id")
        elif isinstance(pg, Page):
            full_page = q.get(pg.size_param, size) if pg.size_param else 1  # unknown page size: stop on an empty page
            if len(page) < full_page:
                break
            q[pg.param] += 1
        elif isinstance(pg, Offset):
            if pg.size_param and len(page) < q[pg.size_param]:
                break
            q[pg.param] += len(page)
    return out[:want] if want else out
