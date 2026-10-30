"""Core / flow-control / data-transformation nodes."""

import asyncio
import copy
import json
import math
import re
import textwrap
from datetime import datetime, timedelta, timezone

import httpx
from langgraph.types import interrupt

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


def check(left, op, right) -> bool:
    if op == "equals":
        if _num(left) is not None and _num(right) is not None:
            return _num(left) == _num(right)
        return str(left) == str(right) if not isinstance(left, (dict, list)) else left == right
    if op == "not_equals":
        return not check(left, "equals", right)
    if op == "contains":
        return str(right) in (left if isinstance(left, (list, dict)) else str(left or ""))
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
    logs: list[str] = []

    def _print(*a):
        msg = " ".join(str(x) for x in a)
        logs.append(msg)
        ctx.log(msg)

    env = {
        "json": json, "re": re, "math": math, "datetime": datetime, "timedelta": timedelta, "timezone": timezone,
        "httpx": httpx, "asyncio": asyncio, "print": _print,
        "node": lambda name: ctx.run.outputs_by_name().get(name, []),
        "vars": ctx.run.meta.get("vars", {}), "execution_id": ctx.run.execution_id,
        "state": copy.deepcopy(ctx.state), "set_state": ctx.set_state,
    }
    src = "async def __user_fn(items, item, index):\n" + textwrap.indent(code, "    ")
    try:
        exec(compile(src, f"<code:{ctx.name}>", "exec"), env)  # noqa: S102 - user code by design
    except SyntaxError as e:
        raise NodeError(f"Syntax error line {e.lineno - 1}: {e.msg}") from e
    fn = env["__user_fn"]
    items = copy.deepcopy(ctx.items)
    if mode == "each":
        out = []
        for idx, item in enumerate(items):
            r = await fn(items, item, idx)
            if r is None:
                continue
            out.extend(r if isinstance(r, list) else [r])
    else:
        r = await fn(items, items[0] if items else {}, 0)
        out = [] if r is None else (r if isinstance(r, list) else [r])
    return {"main": out}


async def _code_subprocess(ctx: NodeContext, language: str, mode: str):
    """JavaScript, TypeScript, Bash, PowerShell, Java, Go, … (see code_runtimes)."""
    from .code_runtimes import CodeError, run_code

    p = ctx.raw_params
    code = p.get(f"code_{language}") or ""
    try:
        out, logs = await asyncio.to_thread(
            run_code, language, code, copy.deepcopy(ctx.items), mode=mode, vars=ctx.run.meta.get("vars", {}),
            nodes=ctx.run.outputs_by_name(), execution_id=ctx.run.execution_id,
            timeout=float(p.get("timeout") or 60), command=p.get("command"), ext=p.get("extension"),
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
        prop("timeout", "Timeout (seconds)", "number", 60, show_if={"language": others}),
    ]


register(NodeType(
    type="core.code", label="Code", icon="code",
    description="Run custom code: Python (in-process, async), JavaScript, TypeScript, Bash, PowerShell, Perl, Java, Ruby, PHP, Go, or any language via a custom command.",
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


async def http_call(ctx: NodeContext, i: int) -> dict | list:
    p = ctx.params(i)
    headers = {k: str(v) for k, v in _kv(p.get("headers")).items()}
    auth, auth_params = await apply_auth(ctx, i, headers)
    kwargs: dict = {"params": {**auth_params, **_kv(p.get("query"))} or None, "headers": headers, "auth": auth}
    body_type = p.get("body_type", "none")
    body = p.get("body")
    if body_type == "json":
        kwargs["json"] = json.loads(body) if isinstance(body, str) and body.strip() else (body or None)
    elif body_type == "form":
        kwargs["data"] = _kv(p.get("form"))
    elif body_type == "raw":
        kwargs["content"] = body if isinstance(body, str) else json.dumps(body)
    async with httpx.AsyncClient(timeout=float(p.get("timeout") or 30), verify=not p.get("ignore_ssl"),
                                 follow_redirects=True) as client:
        resp = await client.request(p.get("method", "GET"), p["url"], **kwargs)
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


async def _split_out(ctx: NodeContext):
    field = ctx.raw_params.get("field", "")
    out = []
    for item in ctx.items:
        val = get_path(item, field)
        for v in val if isinstance(val, list) else ([val] if val is not None else []):
            if ctx.raw_params.get("include", "none") == "all":
                base = {k: x for k, x in item.items() if k != field.split(".")[0]}
                out.append({**base, **(v if isinstance(v, dict) else {field: v})})
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
    n = int(ctx.param("max") or 1)
    return {"main": ctx.items[-n:] if ctx.param("keep") == "last" else ctx.items[:n]}


register(NodeType(
    type="data.limit", label="Limit", icon="scissors", description="Keep only the first/last N items.",
    properties=[prop("max", "Max items", "number", 1), prop("keep", "Keep", "options", "first", options=["first", "last"])],
    execute=_limit, **DATA,
))


async def _sort(ctx: NodeContext):
    field = ctx.raw_params.get("field", "")
    desc = ctx.raw_params.get("order") == "desc"
    return {"main": sorted(ctx.items, key=lambda x: (get_path(x, field) is None, get_path(x, field)), reverse=desc)}


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
        dt = datetime.now(timezone.utc) if not raw else datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
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
    elif isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            pass
    ctx.run.response = {"status": int(p.get("status") or 200), "body": body, "headers": _kv(p.get("headers"))}
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
        wf = await load_workflow(workflow_id)
    except ValueError as e:
        raise NodeError(str(e)) from e
    if wf["owner_id"] != ctx.run.meta.get("owner_id"):  # same answer as a missing id: don't reveal it exists
        raise NodeError(f"Workflow {workflow_id} not found")
    res = await execute_workflow(wf, trigger_type="trigger.subworkflow", items=items, mode="subworkflow") \
        if any(n["type"] == "trigger.subworkflow" for n in wf["nodes"]) \
        else await execute_workflow(wf, items=items, mode="subworkflow")
    if res["status"] == "error":
        raise NodeError(f"Sub-workflow failed: {res['error']}")
    return res["output"]


async def _exec_wf(ctx: NodeContext):
    wf_id = ctx.param("workflow_id")
    if not wf_id:
        raise NodeError("Select a workflow")
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

register(NodeType(
    type="ui.sticky", label="Sticky Note", icon="sticky-note", description="Canvas annotation (not executed).",
    group="core", color="#facc15", inputs=[], outputs=[], properties=[prop("content", "Content", "text", "## Note")],
))
