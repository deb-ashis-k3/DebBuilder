"""Core / flow-control / data-transformation nodes."""

import asyncio
import copy
import json
import re
from datetime import datetime, timedelta, timezone

import httpx
from langgraph.types import interrupt

from ..netguard import BlockedURL, guarded_client
from .base import NodeContext, NodeError, NodeType, prop, register

CORE = dict(group="core", color="#3b82f6")
FLOW = dict(group="flow", color="#8b5cf6")
DATA = dict(group="data", color="#14b8a6")

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

OPERATORS = [
    "equals", "not_equals", "contains", "not_contains", "starts_with", "ends_with",
    "gt", "gte", "lt", "lte", "is_empty", "is_not_empty", "is_true", "is_false", "regex", "exists",
]

CONDITION_FIELDS = [
    prop("left", "Value", "string", "", placeholder="{{ $json.status }}"),
    prop("operator", "Operator", "options", "equals", options=OPERATORS),
    prop("right", "Compare to", "string", ""),
]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "false"):
        return v.strip().lower() == "true"
    return None


def _maybe_json(v):
    if isinstance(v, str) and v.strip()[:1] in ("[", "{"):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _loose_eq(a, b) -> bool:
    """n8n-style loose equality: 1 == "1", True == "true", [1] == "[1]"."""
    if isinstance(a, bool) or isinstance(b, bool):
        ba, bb = _bool(a), _bool(b)
        if ba is not None and bb is not None:
            return ba == bb
    if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return _maybe_json(a) == _maybe_json(b)
    if _num(a) is not None and _num(b) is not None and not isinstance(a, bool) and not isinstance(b, bool):
        return _num(a) == _num(b)
    return str("" if a is None else a) == str("" if b is None else b)


def check(left, op, right) -> bool:
    if op == "equals":
        return _loose_eq(left, right)
    if op == "not_equals":
        return not check(left, "equals", right)
    if op == "contains":
        left = _maybe_json(left) if isinstance(left, str) and isinstance(right, (str, int, float)) and left.strip()[:1] == "[" else left
        if isinstance(left, list):
            return any(_loose_eq(x, right) for x in left)
        if isinstance(left, dict):
            return str(right) in left
        return str("" if right is None else right) in str("" if left is None else left)
    if op == "not_contains":
        return not check(left, "contains", right)
    if op == "starts_with":
        return str(left or "").startswith(str(right))
    if op == "ends_with":
        return str(left or "").endswith(str(right))
    if op in ("gt", "gte", "lt", "lte"):
        a, b = _num(left), _num(right)
        if a is None or b is None:
            a, b = str(left), str(right)
        return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
    if op == "is_empty":
        return left in (None, "", [], {})
    if op == "is_not_empty":
        return left not in (None, "", [], {})
    if op == "is_true":
        return left is True or str(left).lower() in ("true", "1", "yes")
    if op == "is_false":
        return not check(left, "is_true", None)
    if op == "regex":
        return re.search(str(right), str(left or "")) is not None
    if op == "exists":
        return left is not None
    raise NodeError(f"Unknown operator {op}")


def eval_conditions(ctx: NodeContext, i: int, conditions: list, combine: str = "and") -> bool:
    results = []
    for c in conditions or []:
        resolved = ctx.eval(c, i)
        results.append(check(resolved.get("left"), resolved.get("operator", "equals"), resolved.get("right")))
    if not results:
        return True
    return all(results) if combine == "and" else any(results)


def get_path(obj, path: str):
    cur = obj
    for part in [p for p in re.split(r"\.|\[(\d+)\]", path) if p]:
        if isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def set_path(obj: dict, path: str, value) -> None:
    parts = path.split(".")
    cur = obj
    for p in parts[:-1]:
        if not isinstance(cur.get(p), dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


def _cast(value, typ: str):
    if typ == "number":
        n = _num(value)
        return int(n) if n is not None and n.is_integer() else n
    if typ == "boolean":
        return value if isinstance(value, bool) else str(value).lower() in ("true", "1", "yes")
    if typ == "json" and isinstance(value, str):
        return json.loads(value) if value.strip() else None
    if typ == "string":
        return value if isinstance(value, str) else json.dumps(value, default=str)
    return value


# ---------------------------------------------------------------------------
# Set / Edit fields
# ---------------------------------------------------------------------------


async def _set(ctx: NodeContext):
    out = []
    keep = ctx.raw_params.get("include_other_fields", True)
    for i, item in enumerate(ctx.items):
        new = copy.deepcopy(item) if keep else {}
        for f in ctx.raw_params.get("fields") or []:
            name = ctx.eval(f.get("name", ""), i)
            if not name:
                continue
            set_path(new, name, _cast(ctx.eval(f.get("value"), i), f.get("type", "auto")))
        out.append(new)
    return {"main": out}


register(NodeType(
    type="core.set", label="Edit Fields (Set)", icon="pencil", description="Add, overwrite or build fields on each item.",
    properties=[
        prop("fields", "Fields", "collection", [{"name": "", "value": "", "type": "auto"}], fields=[
            prop("name", "Name (dot.path ok)"),
            prop("value", "Value", "string", ""),
            prop("type", "Type", "options", "auto", options=["auto", "string", "number", "boolean", "json"]),
        ]),
        prop("include_other_fields", "Keep existing fields", "boolean", True),
    ],
    execute=_set, **DATA,
))

# ---------------------------------------------------------------------------
# Update State (graph state, see engine/state.py)
# ---------------------------------------------------------------------------

STATE_REDUCER_OPTIONS = [
    {"label": "Key's reducer (Settings → State)", "value": ""},
    {"label": "Replace", "value": "replace"},
    {"label": "Append to list", "value": "append"},
    {"label": "Add (number)", "value": "add"},
    {"label": "Merge object", "value": "merge"},
    {"label": "Add chat messages", "value": "messages"},
]


async def _update_state(ctx: NodeContext):
    from ..engine.state import apply_updates, public

    rows = ctx.raw_params.get("updates") or []
    each = ctx.param("mode") == "each"
    for i in range(len(ctx.items) if each else 1):
        for row in rows:
            key = str(ctx.eval(row.get("key") or "", i) or "").strip()
            if not key:
                continue
            value = _cast(ctx.eval(row.get("value"), i), row.get("type") or "auto")
            ctx.set_state(key, value, row.get("reducer") or None)
    if ctx.param("output") == "state":
        return {"main": [public(apply_updates(ctx.graph_state, ctx.state_updates))]}
    return {"main": ctx.items}


register(NodeType(
    type="flow.state", label="Update State", icon="database-zap",
    description="Write the workflow's graph state ($state): counters, lists, drafts, chat messages. "
                "Each key combines writes with its reducer, like LangGraph.",
    properties=[
        prop("updates", "State updates", "collection", [{"key": "", "value": "", "reducer": "", "type": "auto"}], fields=[
            prop("key", "Key"),
            prop("value", "Value", "string", "", placeholder="{{ $json.output }}"),
            prop("reducer", "How to combine", "options", "", options=STATE_REDUCER_OPTIONS),
            prop("type", "Type", "options", "auto", options=["auto", "string", "number", "boolean", "json"]),
        ]),
        prop("mode", "Run", "options", "once", options=[
            {"label": "Once (first item)", "value": "once"},
            {"label": "For each item", "value": "each"},
        ]),
        prop("output", "Output", "options", "items", options=[
            {"label": "Input items (pass through)", "value": "items"},
            {"label": "The state after this update", "value": "state"},
        ]),
    ],
    execute=_update_state, **FLOW,
))

# ---------------------------------------------------------------------------
# Code (Python)
# ---------------------------------------------------------------------------

DEFAULT_CODE = """# `items` is a list of dicts (all input items).
# Graph state: read `state["key"]`, write `set_state("key", value)` (uses the key's reducer).
# Return a list of dicts (or a single dict).
for item in items:
    item["processed"] = True
return items
"""


async def _code(ctx: NodeContext):
    language = ctx.raw_params.get("language") or "python"
    mode = ctx.raw_params.get("mode", "all")
    if language != "python":
        return await _code_subprocess(ctx, language, mode)
    code = ctx.raw_params.get("code") or "return items"
    out = await run_python_sandboxed(ctx, code, {
        "kind": "code", "params": ["items", "item", "index"], "items": ctx.items, "mode": mode,
        "state": ctx.state, "nodes": ctx.run.outputs_by_name() if "node" in code else {},
    })
    return {"main": out}


async def run_python_sandboxed(ctx: NodeContext, code: str, req: dict):
    """Run Python user code in the process sandbox (see sandbox.py); applies set_state calls and logs."""
    from .sandbox import SandboxError, Timeout, code_timeout, run_python

    req = {**req, "code": code, "vars": ctx.run.meta.get("vars", {}), "execution_id": ctx.run.execution_id}
    try:
        resp = await run_python(ctx.run.meta.get("owner_id"), req, code_timeout(ctx.raw_params.get("timeout")))
    except (SandboxError, Timeout) as e:
        raise NodeError(str(e), ctx.name) from e
    for line in resp.get("logs") or []:
        ctx.log(line)
    if not resp.get("ok"):
        raise NodeError(resp.get("error") or "Code failed", ctx.name)
    for key, value, reducer in resp.get("state") or []:
        ctx.set_state(key, value, reducer)
    return resp.get("out")


async def _code_subprocess(ctx: NodeContext, language: str, mode: str):
    """JavaScript, TypeScript, Bash, PowerShell, Java, Go, … (see code_runtimes)."""
    from .code_runtimes import CodeError, run_code
    from .sandbox import code_timeout

    p = ctx.raw_params
    code = p.get(f"code_{language}") or ""
    try:
        out, logs = await asyncio.to_thread(
            run_code, language, code, copy.deepcopy(ctx.items), mode=mode, vars=ctx.run.meta.get("vars", {}),
            nodes=ctx.run.outputs_by_name(), execution_id=ctx.run.execution_id,
            timeout=code_timeout(p.get("timeout")), command=p.get("command"), ext=p.get("extension"),
        )
    except CodeError as e:
        raise NodeError(str(e), ctx.name) from e
    for line in logs:
        ctx.log(line)
    return {"main": out}


def _code_props() -> list[dict]:
    from .code_runtimes import LANGUAGES, TEMPLATES

    others = [*LANGUAGES, "custom"]
    return [
        prop("language", "Language", "options", "python", options=[
            {"label": "Python", "value": "python"},
            *({"label": lang.label, "value": key} for key, lang in LANGUAGES.items()),
            {"label": "Custom command (any language)", "value": "custom"},
        ]),
        prop("mode", "Mode", "options", "all", options=[
            {"label": "Run once for all items", "value": "all"},
            {"label": "Run once for each item (`item`, `index`)", "value": "each"},
        ]),
        prop("command", "Command", "string", "python {file}", show_if={"language": ["custom"]}, required=True,
             description="{file} is the script path, e.g. `deno run -A {file}`, `Rscript {file}`, `dotnet script {file}`"),
        prop("extension", "File extension", "string", ".py", show_if={"language": ["custom"]}),
        # One code field per language, so switching languages never loses code.
        prop("code", "Python code", "code", DEFAULT_CODE, language="python", rows=14, show_if={"language": ["python"]}),
        *(prop(f"code_{key}", f"{lang.label} code", "code", TEMPLATES[key], language=key, rows=14,
               show_if={"language": [key]}) for key, lang in LANGUAGES.items()),
        prop("code_custom", "Code", "code", TEMPLATES["custom"], rows=14, show_if={"language": ["custom"]}),
        prop("timeout", "Timeout (seconds)", "number", 60, show_if={"language": ["python", *others]},
             description="Capped by the server's CODE_TIMEOUT_SECONDS"),
    ]


register(NodeType(
    type="core.code", label="Code", icon="code",
    description="Run custom code: Python (sandboxed, async), JavaScript, TypeScript, Bash, PowerShell, Perl, Java, Ruby, PHP, Go, or any language via a custom command.",
    properties=_code_props(),
    execute=_code, **CORE,
))

# ---------------------------------------------------------------------------
# HTTP Request
# ---------------------------------------------------------------------------


async def apply_auth(ctx: NodeContext, i: int, headers: dict) -> tuple[httpx.Auth | None, dict]:
    """Authenticate with ANY credential type (n8n's 'predefined credential type').

    Mutates `headers`; returns (httpx auth, extra query params)."""
    from ..credentials import request_auth

    if ctx.param("auth", i) in (None, "none"):
        return None, {}
    ctype, cred = await ctx.credential("credential", i)
    h, params, auth = await request_auth(ctype, cred, ctx.param("credential", i))
    headers.update(h)
    return auth, params


def _kv(pairs) -> dict:
    return {p["key"]: p.get("value") for p in pairs or [] if p.get("key")}


def _ascii_headers(headers: dict) -> dict:
    for k, v in headers.items():
        for part, what in ((k, "name"), (v, "value")):
            try:
                str(part).encode("ascii")
            except UnicodeEncodeError:
                raise NodeError(f"Header {what} of '{k}' contains non-ASCII characters ({str(part)[:60]!r}). HTTP headers "
                                "must be ASCII: URL-encode the value, e.g. {{ encodeURIComponent($json.name) }}") from None
    return headers


def json_body(ctx: NodeContext, name: str, i: int = 0):
    """A JSON field with {{ }} expressions -> a value, always valid JSON.

    Each expression is filled in according to where it stands: inside a JSON string its result is escaped
    (quotes, newlines, backslashes stay valid), outside a string it becomes a JSON value, and a string that is
    nothing but one expression ("{{ $json.obj }}") takes the expression's value as is (object, number, ...)."""
    raw = ctx.raw_params.get(name)
    if not isinstance(raw, str):
        return ctx.eval(raw, i) if raw is not None else None
    if not raw.strip():
        return None
    if "{{" not in raw:
        try:
            return json.loads(raw)
        except ValueError as e:
            raise NodeError(f"Body is not valid JSON: {e}") from e
    out, k, n, in_str = [], 0, len(raw), False
    while k < n:
        c = raw[k]
        if raw.startswith("{{", k):
            end = raw.find("}}", k + 2)
            if end == -1:
                out.append(raw[k:])
                break
            expr = raw[k:end + 2]
            val = ctx.eval(expr, i)
            whole = in_str and out and out[-1].endswith('"') and raw[end + 2:end + 3] == '"' \
                and (len(out[-1]) < 2 or out[-1][-2] != "\\")
            if whole:  # "{{ expr }}": the value itself
                out[-1] = out[-1][:-1]
                out.append(json.dumps(val, default=str))
                k = end + 3
                in_str = False
                continue
            if in_str:
                text = val if isinstance(val, str) else json.dumps(val, default=str) if isinstance(val, (dict, list)) \
                    else ("" if val is None else str(val).lower() if isinstance(val, bool) else str(val))
                out.append(json.dumps(text)[1:-1])
            else:
                out.append(json.dumps(val, default=str))
            k = end + 2
            continue
        if c == "\\" and in_str:
            out.append(raw[k:k + 2])
            k += 2
            continue
        if c == '"':
            in_str = not in_str
        out.append(c)
        k += 1
    text = "".join(out)
    try:
        return json.loads(text)
    except ValueError as e:
        raise NodeError(f"Body is not valid JSON after filling in expressions: {e}. Got: {text[:300]}") from e


async def http_call(ctx: NodeContext, i: int) -> dict | list:
    p = ctx.params(i)
    headers = _ascii_headers({k: str(v) for k, v in _kv(p.get("headers")).items()})
    auth, auth_params = await apply_auth(ctx, i, headers)
    kwargs: dict = {"params": {**auth_params, **_kv(p.get("query"))} or None, "headers": headers, "auth": auth}
    body_type = p.get("body_type", "none")
    body = p.get("body")
    if body_type == "json":
        kwargs["json"] = json_body(ctx, "body", i)
    elif body_type == "form":
        kwargs["data"] = _kv(p.get("form"))
    elif body_type == "raw":
        kwargs["content"] = body if isinstance(body, str) else json.dumps(body)
    async with guarded_client(timeout=float(p.get("timeout") or 30), verify=not p.get("ignore_ssl"),
                              follow_redirects=True) as client:
        try:
            resp = await client.request(p.get("method", "GET"), p["url"], **kwargs)
        except BlockedURL as e:
            raise NodeError(str(e)) from e
    if resp.status_code >= 400 and not p.get("never_error"):
        raise NodeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
    fmt = p.get("response_format", "auto")
    if fmt == "file":
        import re as _re
        from urllib.parse import unquote, urlparse

        cd = resp.headers.get("content-disposition") or ""
        m = _re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
        name = unquote(m.group(1)) if m else (urlparse(str(resp.url)).path.rsplit("/", 1)[-1] or "file")
        ref = ctx.put_file(resp.content, name, (resp.headers.get("content-type") or "").split(";")[0] or None)
        out = {"binary": {p.get("binary_property") or "data": ref}}
        return {"status": resp.status_code, "headers": dict(resp.headers), **out} if p.get("full_response") else out
    data = None
    if fmt in ("auto", "json"):
        try:
            data = resp.json()
        except ValueError:
            if fmt == "json":
                raise NodeError("Response is not valid JSON")
    if data is None:
        data = {"data": resp.text}
    if p.get("full_response"):
        return {"status": resp.status_code, "headers": dict(resp.headers), "body": data}
    return data


async def _http(ctx: NodeContext):
    out = []
    for i in range(len(ctx.items) or 1):
        data = await http_call(ctx, i)
        if isinstance(data, list) and ctx.raw_params.get("split_list", True):
            out.extend(d if isinstance(d, dict) else {"value": d} for d in data)
        else:
            out.append(data if isinstance(data, dict) else {"value": data})
    return {"main": out}


KV = [prop("key", "Name"), prop("value", "Value")]
HTTP_PROPS = [
    prop("method", "Method", "options", "GET", options=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]),
    prop("url", "URL", "string", "", required=True, placeholder="https://api.example.com/items/{{ $json.id }}"),
    prop("auth", "Authentication", "options", "none", options=[
        {"label": "None", "value": "none"}, {"label": "Credential (any type, incl. OAuth2)", "value": "credential"}]),
    prop("credential", "Credential", "credential", None, show_if={"auth": ["credential"]},
         description="Any saved credential: its auth headers / query / OAuth2 token are applied automatically"),
    prop("query", "Query parameters", "collection", [], fields=KV),
    prop("headers", "Headers", "collection", [], fields=KV),
    prop("body_type", "Body", "options", "none", options=["none", "json", "form", "raw"]),
    prop("body", "Body content", "json", "", show_if={"body_type": ["json", "raw"]}),
    prop("form", "Form fields", "collection", [], fields=KV, show_if={"body_type": ["form"]}),
    prop("response_format", "Response format", "options", "auto", options=["auto", "json", "text", "file"]),
    prop("binary_property", "Put the file in binary property", "string", "data", show_if={"response_format": ["file"]}),
    prop("split_list", "Split array response into items", "boolean", True),
    prop("full_response", "Include status & headers", "boolean", False),
    prop("never_error", "Never error on 4xx/5xx", "boolean", False),
    prop("timeout", "Timeout (s)", "number", 30),
    prop("ignore_ssl", "Ignore SSL issues", "boolean", False),
]

register(NodeType(
    type="core.http", label="HTTP Request", icon="globe", description="Call any REST API.",
    properties=HTTP_PROPS, execute=_http, **CORE,
))

# ---------------------------------------------------------------------------
# IF / Filter / Switch
# ---------------------------------------------------------------------------

COND_PROPS = [
    prop("conditions", "Conditions", "collection", [{"left": "", "operator": "equals", "right": ""}], fields=CONDITION_FIELDS),
    prop("combine", "Combine", "options", "and", options=[{"label": "AND (all)", "value": "and"}, {"label": "OR (any)", "value": "or"}]),
]


async def _if(ctx: NodeContext):
    t, f = [], []
    for i, item in enumerate(ctx.items):
        (t if eval_conditions(ctx, i, ctx.raw_params.get("conditions"), ctx.raw_params.get("combine", "and")) else f).append(item)
    return {"true": t, "false": f}


register(NodeType(
    type="flow.if", label="If", icon="git-branch", description="Route items to true / false.",
    properties=COND_PROPS, outputs=["true", "false"], execute=_if, **FLOW,
))


async def _filter(ctx: NodeContext):
    kept, dropped = [], []
    for i, item in enumerate(ctx.items):
        (kept if eval_conditions(ctx, i, ctx.raw_params.get("conditions"), ctx.raw_params.get("combine", "and")) else dropped).append(item)
    return {"main": kept, "discarded": dropped}


register(NodeType(
    type="flow.filter", label="Filter", icon="filter", description="Keep only items matching conditions.",
    properties=COND_PROPS, outputs=["main", "discarded"], execute=_filter, **FLOW,
))


async def _switch(ctx: NodeContext):
    rules = ctx.raw_params.get("rules") or []
    all_matches = ctx.raw_params.get("all_matches", False)
    out: dict[str, list] = {f"out{i}": [] for i in range(len(rules))}
    out["fallback"] = []
    for i, item in enumerate(ctx.items):
        matched = False
        for r_i, rule in enumerate(rules):
            r = ctx.eval(rule, i)
            if check(r.get("left"), r.get("operator", "equals"), r.get("right")):
                out[f"out{r_i}"].append(item)
                matched = True
                if not all_matches:
                    break
        if not matched and ctx.raw_params.get("fallback", True):
            out["fallback"].append(item)
    return out


register(NodeType(
    type="flow.switch", label="Switch", icon="split", description="Route items to one of many outputs by rules.",
    properties=[
        prop("rules", "Routing rules (one output each)", "collection",
             [{"name": "A", "left": "", "operator": "equals", "right": ""}],
             fields=[prop("name", "Output name"), *CONDITION_FIELDS]),
        prop("fallback", "Fallback output for unmatched", "boolean", True),
        prop("all_matches", "Send to all matching outputs", "boolean", False),
    ],
    outputs=[], dynamic_outputs="rules", execute=_switch, **FLOW,
))

# ---------------------------------------------------------------------------
# Merge (deferred join)
# ---------------------------------------------------------------------------


async def _merge(ctx: NodeContext):
    a = ctx.inputs_by_handle.get("input1", [])
    b = ctx.inputs_by_handle.get("input2", [])
    mode = ctx.raw_params.get("mode", "append")
    if mode == "append":
        return {"main": a + b + ctx.inputs_by_handle.get("main", [])}
    if mode == "by_position":
        return {"main": [{**x, **y} for x, y in zip(a, b)]}
    if mode == "by_field":
        f1, f2 = ctx.raw_params.get("field1", "id"), ctx.raw_params.get("field2", "id")
        index = {}
        for y in b:
            index.setdefault(str(get_path(y, f2)), []).append(y)
        out = []
        keep_unmatched = ctx.raw_params.get("join", "inner") == "left"
        for x in a:
            matches = index.get(str(get_path(x, f1)))
            if matches:
                out.extend({**x, **y} for y in matches)
            elif keep_unmatched:
                out.append(x)
        return {"main": out}
    if mode == "input1":
        return {"main": a}
    if mode == "input2":
        return {"main": b}
    raise NodeError(f"Unknown merge mode {mode}")


register(NodeType(
    type="flow.merge", label="Merge", icon="merge", description="Wait for both branches and combine their items.",
    properties=[
        prop("mode", "Mode", "options", "append", options=[
            {"label": "Append", "value": "append"},
            {"label": "Combine by position", "value": "by_position"},
            {"label": "Combine by matching field", "value": "by_field"},
            {"label": "Wait, output input 1", "value": "input1"},
            {"label": "Wait, output input 2", "value": "input2"},
        ]),
        prop("field1", "Input 1 field", "string", "id", show_if={"mode": ["by_field"]}),
        prop("field2", "Input 2 field", "string", "id", show_if={"mode": ["by_field"]}),
        prop("join", "Join", "options", "inner", options=[{"label": "Inner", "value": "inner"}, {"label": "Keep all from input 1", "value": "left"}],
             show_if={"mode": ["by_field"]}),
    ],
    inputs=["input1", "input2"], defer=True, execute=_merge, **FLOW,
))

# ---------------------------------------------------------------------------
# Loop over items (split in batches)
# ---------------------------------------------------------------------------


async def _loop(ctx: NodeContext):
    # The cursor lives in graph state (an internal key), so it's checkpointed: a loop whose body
    # pauses for Human Approval / Wait continues where it was after the resume.
    key = f"__loop:{ctx.node['id']}"
    size = max(1, int(ctx.param("batch_size") or 1))
    st = copy.deepcopy(ctx.graph_state.get(key))
    if st is None:
        st = {"queue": list(ctx.items), "done": []}
    else:
        st["done"].extend(ctx.items)
    if st["queue"]:
        batch, st["queue"] = st["queue"][:size], st["queue"][size:]
        ctx.set_state(key, st, "replace")
        return {"loop": batch, "done": []}
    ctx.set_state(key, None, "replace")
    return {"loop": [], "done": st["done"]}


register(NodeType(
    type="flow.loop", label="Loop Over Items", icon="repeat",
    description="Emit items in batches on 'loop'; connect the loop body back here. When finished, emits all results on 'done'.",
    properties=[prop("batch_size", "Batch size", "number", 1)],
    outputs=["loop", "done"], execute=_loop, **FLOW,
))

# ---------------------------------------------------------------------------
# Wait / Approval (human in the loop)
# ---------------------------------------------------------------------------


async def _wait(ctx: NodeContext):
    amount = float(ctx.param("amount") or 0)
    unit = ctx.param("unit", default="seconds")
    secs = amount * {"seconds": 1, "minutes": 60, "hours": 3600}.get(unit, 1)
    await asyncio.sleep(secs)
    return {"main": ctx.items}


register(NodeType(
    type="flow.wait", label="Wait", icon="hourglass", description="Pause for a period of time.",
    properties=[prop("amount", "Amount", "number", 1), prop("unit", "Unit", "options", "seconds", options=["seconds", "minutes", "hours"])],
    execute=_wait, **FLOW,
))


async def _approval(ctx: NodeContext):
    message = ctx.param("message")
    decision = interrupt({
        "kind": "approval",
        "node_id": ctx.node["id"],
        "node_name": ctx.name,
        "message": message,
        "fields": ctx.raw_params.get("fields") or [],
        "items": ctx.items[:20],
    })
    if not isinstance(decision, dict):
        decision = {"approved": bool(decision)}
    approved = bool(decision.get("approved", True))
    merged = [{**it, "approval": decision} for it in (ctx.items or [{}])]
    return {"approved": merged if approved else [], "rejected": [] if approved else merged}


register(NodeType(
    type="flow.approval", label="Human Approval", icon="user-check",
    description="Pause the execution until a person approves / rejects (Executions page or POST /api/executions/<id>/resume).",
    properties=[
        prop("message", "Message to reviewer", "text", "Please review: {{ $json }}"),
        prop("fields", "Extra input fields", "collection", [], fields=[prop("name", "Key"), prop("label", "Label")]),
    ],
    outputs=["approved", "rejected"], execute=_approval, interrupts=True, **FLOW,
))

# ---------------------------------------------------------------------------
# Item list utilities
# ---------------------------------------------------------------------------


def _del_path(obj: dict, path: str) -> None:
    parts = path.split(".")
    cur = obj
    for p in parts[:-1]:
        cur = cur.get(p) if isinstance(cur, dict) else None
    if isinstance(cur, dict):
        cur.pop(parts[-1], None)


async def _split_out(ctx: NodeContext):
    out = []
    for i, item in enumerate(ctx.items):
        field = str(ctx.param("field", i) or "")
        val = get_path(item, field)
        for v in val if isinstance(val, list) else ([val] if val is not None else []):
            if ctx.raw_params.get("include", "none") == "all":
                base = copy.deepcopy(item)
                _del_path(base, field)  # only the split field: its siblings stay
                if isinstance(v, dict):
                    base.update(v)
                else:
                    set_path(base, field, v)
                out.append(base)
            else:
                out.append(v if isinstance(v, dict) else {field.split(".")[-1]: v})
    return {"main": out}


register(NodeType(
    type="data.split_out", label="Split Out", icon="list-tree", description="Turn a list field into separate items.",
    properties=[prop("field", "Field to split", "string", "data"),
                prop("include", "Include other fields", "options", "none", options=["none", "all"])],
    execute=_split_out, **DATA,
))


async def _aggregate(ctx: NodeContext):
    field = ctx.raw_params.get("field", "")
    out_field = ctx.raw_params.get("output_field") or "data"
    if field:
        return {"main": [{out_field: [get_path(i, field) for i in ctx.items]}]}
    return {"main": [{out_field: ctx.items}]}


register(NodeType(
    type="data.aggregate", label="Aggregate", icon="layers", description="Combine all items into one.",
    properties=[prop("field", "Field (empty = whole items)", "string", ""), prop("output_field", "Output field", "string", "data")],
    execute=_aggregate, **DATA,
))


async def _limit(ctx: NodeContext):
    raw = ctx.param("max")
    try:
        n = int(float(raw)) if raw not in (None, "") else 1
    except (TypeError, ValueError) as e:
        raise NodeError(f"Max items must be a number (got {raw!r})") from e
    if n < 0:
        raise NodeError("Max items can't be negative")
    if n == 0:
        return {"main": []}
    return {"main": ctx.items[-n:] if ctx.param("keep") == "last" else ctx.items[:n]}


register(NodeType(
    type="data.limit", label="Limit", icon="scissors", description="Keep only the first/last N items.",
    properties=[prop("max", "Max items", "number", 1), prop("keep", "Keep", "options", "first", options=["first", "last"])],
    execute=_limit, **DATA,
))


def _sort_key(v):
    """Total order over mixed JSON values: numbers < strings < booleans < lists/objects."""
    if isinstance(v, bool):
        return (2, int(v), "")
    if isinstance(v, (int, float)):
        return (0, v, "")
    if isinstance(v, str):
        return (1, 0, v)
    return (3, 0, json.dumps(v, sort_keys=True, default=str))


async def _sort(ctx: NodeContext):
    field = (ctx.raw_params.get("field") or "").strip()
    desc = ctx.raw_params.get("order") == "desc"
    if not field:
        return {"main": list(ctx.items)}
    present = [x for x in ctx.items if get_path(x, field) not in (None, "")]
    missing = [x for x in ctx.items if get_path(x, field) in (None, "")]
    # missing / empty values go last in both directions
    return {"main": sorted(present, key=lambda x: _sort_key(get_path(x, field)), reverse=desc) + missing}


register(NodeType(
    type="data.sort", label="Sort", icon="arrow-up-down", description="Sort items by a field.",
    properties=[prop("field", "Field", "string", ""), prop("order", "Order", "options", "asc", options=["asc", "desc"])],
    execute=_sort, **DATA,
))


async def _dedupe(ctx: NodeContext):
    field = ctx.raw_params.get("field", "")
    seen, out = set(), []
    for item in ctx.items:
        key = json.dumps(get_path(item, field) if field else item, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return {"main": out}


register(NodeType(
    type="data.dedupe", label="Remove Duplicates", icon="copy-minus", description="Drop duplicate items.",
    properties=[prop("field", "Compare field (empty = whole item)", "string", "")],
    execute=_dedupe, **DATA,
))


async def _datetime(ctx: NodeContext):
    out = []
    for i, item in enumerate(ctx.items):
        p = ctx.params(i)
        raw = p.get("value")
        if raw in (None, ""):
            dt = datetime.now(timezone.utc)
        elif isinstance(raw, (int, float)) or re.fullmatch(r"-?\d+(\.\d+)?", str(raw).strip()):
            ts = float(raw)
            dt = datetime.fromtimestamp(ts / 1000 if abs(ts) > 1e11 else ts, timezone.utc)  # unix seconds or ms
        else:
            try:
                dt = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
            except ValueError as e:
                raise NodeError(f"Can't read the date {raw!r}: use ISO 8601 (2024-05-01T09:00:00Z) or a unix timestamp") from e
        if p.get("add_amount"):
            dt += timedelta(**{p.get("add_unit", "days"): float(p["add_amount"])})
        val = dt.strftime(p["format"]) if p.get("format") else dt.isoformat()
        out.append({**item, p.get("output_field") or "date": val})
    return {"main": out}


register(NodeType(
    type="data.datetime", label="Date & Time", icon="calendar", description="Parse, shift and format dates.",
    properties=[
        prop("value", "Date (ISO, empty = now)", "string", ""),
        prop("add_amount", "Add amount (negative to subtract)", "number", 0),
        prop("add_unit", "Unit", "options", "days", options=["seconds", "minutes", "hours", "days", "weeks"]),
        prop("format", "strftime format (empty = ISO)", "string", ""),
        prop("output_field", "Output field", "string", "date"),
    ],
    execute=_datetime, **DATA,
))


async def _noop(ctx: NodeContext):
    return {"main": ctx.items}


register(NodeType(
    type="core.noop", label="No Operation", icon="circle-dashed", description="Pass items through unchanged.",
    properties=[], execute=_noop, **CORE,
))


async def _stop(ctx: NodeContext):
    raise NodeError(ctx.param("message") or "Workflow stopped")


register(NodeType(
    type="core.stop_error", label="Stop and Error", icon="octagon-x", description="Fail the execution with a message.",
    properties=[prop("message", "Error message", "string", "Something went wrong")], outputs=[], execute=_stop, **CORE,
))

# ---------------------------------------------------------------------------
# Webhook response / sub-workflows
# ---------------------------------------------------------------------------


async def _respond(ctx: NodeContext):
    p = ctx.params(0)
    body = p.get("body")
    if p.get("respond_with") == "first_item":
        body = ctx.items[0] if ctx.items else {}
    elif p.get("respond_with") == "all_items":
        body = ctx.items
    elif isinstance(ctx.raw_params.get("body"), str):
        try:
            body = json_body(ctx, "body", 0)  # expressions with quotes etc. keep the JSON valid
        except NodeError:
            pass  # not JSON at all: send the text as is
    try:
        status = int(float(p.get("status") or 200))
    except (TypeError, ValueError):
        raise NodeError(f"Status code must be a number (got {p.get('status')!r})") from None
    if not 100 <= status <= 599:
        raise NodeError(f"Status code must be between 100 and 599 (got {status})")
    ctx.run.response = {"status": status, "body": body, "headers": _kv(p.get("headers"))}
    return {"main": ctx.items}


register(NodeType(
    type="core.respond_webhook", label="Respond to Webhook", icon="reply", description="Set the HTTP response of a Webhook trigger.",
    properties=[
        prop("respond_with", "Respond with", "options", "first_item", options=[
            {"label": "First item", "value": "first_item"}, {"label": "All items", "value": "all_items"},
            {"label": "Custom body", "value": "custom"}]),
        prop("body", "Body", "json", "", show_if={"respond_with": ["custom"]}),
        prop("status", "Status code", "number", 200),
        prop("headers", "Headers", "collection", [], fields=KV),
    ],
    execute=_respond, **CORE,
))


async def run_subworkflow(ctx: NodeContext, workflow_id: str, items: list[dict]) -> list[dict]:
    from ..engine.runtime import execute_workflow, load_workflow

    try:
        wf = await load_workflow(workflow_id, live=True)  # sub-workflows run their published version
    except ValueError as e:
        raise NodeError(str(e)) from e
    if wf["owner_id"] != ctx.run.meta.get("owner_id"):  # same answer as a missing id: don't reveal it exists
        raise NodeError(f"Workflow {workflow_id} not found")
    # parent: limits nesting (MAX_SUBWORKFLOW_DEPTH) and cancels the child with this run
    trigger = "trigger.subworkflow" if any(n["type"] == "trigger.subworkflow" for n in wf["nodes"]) else None
    try:
        res = await execute_workflow(wf, trigger_type=trigger, items=items, mode="subworkflow", parent=ctx.run)
    except ValueError as e:
        raise NodeError(str(e)) from e
    if res["status"] == "error":  # nested sub-workflows report the innermost reason once, not once per level
        raise NodeError(f"Sub-workflow failed: {(res['error'] or '').rsplit('Sub-workflow failed: ', 1)[-1]}")
    if res["status"] == "canceled":
        raise NodeError("Sub-workflow was canceled")
    return res["output"]


async def _exec_wf(ctx: NodeContext):
    import uuid as _uuid

    wf_id = ctx.param("workflow_id")
    if not wf_id:
        raise NodeError("Select a workflow")
    try:
        _uuid.UUID(str(wf_id))
    except ValueError:
        raise NodeError(f"'{wf_id}' is not a workflow id: pick the workflow from the list (or map a workflow's id)") from None
    if ctx.raw_params.get("mode", "all") == "each":
        out = []
        for item in ctx.items:
            out.extend(await run_subworkflow(ctx, wf_id, [item]))
        return {"main": out}
    return {"main": await run_subworkflow(ctx, wf_id, ctx.items)}


register(NodeType(
    type="core.execute_workflow", label="Execute Workflow", icon="workflow", description="Run another workflow and return its output.",
    properties=[
        prop("workflow_id", "Workflow", "workflow", None, required=True),
        prop("mode", "Mode", "options", "all", options=[
            {"label": "Once with all items", "value": "all"}, {"label": "Once per item", "value": "each"}]),
    ],
    execute=_exec_wf, **CORE,
))

async def _sticky(ctx: NodeContext):
    return {"main": ctx.items}  # a note wired into the flow by mistake just passes items through


register(NodeType(
    type="ui.sticky", label="Sticky Note", icon="sticky-note", description="Canvas annotation (not executed).",
    group="core", color="#facc15", inputs=[], outputs=[], properties=[prop("content", "Content", "text", "## Note")],
    execute=_sticky,
))
