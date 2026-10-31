"""n8n-style `{{ ... }}` expressions evaluated with a sandboxed Jinja engine.

Inside braces you can use (the `$` prefix is optional):
  $json            current input item
  $binary          files on the current item ($binary.data.fileName / mimeType / fileSize)
  $input.all / $input.first / $input.item
  $('Node Name').json / .items             output of an earlier node (by name)
  $node['Node Name'].json                   same, n8n v0 syntax
  $now, $today, $execution.id, $workflow.id/name, $env.VAR (secrets hidden), $vars.key
  $state.key       the workflow's graph state (Settings → State, Update State node)
A string that is exactly one `{{ expr }}` returns the native value (dict, list,
number...); mixed text renders as a string.
"""

import json as _json
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from jinja2.sandbox import SandboxedEnvironment

class _Env(SandboxedEnvironment):
    def getattr(self, obj, attribute):
        # n8n semantics: `$json.items` / `.keys` / `.values` mean the *field*, not the dict
        # method Jinja would otherwise return (dict.items is a builtin, not data).
        if isinstance(obj, dict) and attribute in obj:
            return obj[attribute]
        return super().getattr(obj, attribute)


_env = _Env(autoescape=False)
_env.filters["json"] = lambda v: _json.dumps(v, default=str)
_env.filters["fromjson"] = lambda v: _json.loads(v) if isinstance(v, str) else v

_EXPR = re.compile(r"\{\{(.*?)\}\}", re.S)
_SEVERAL = re.compile(r"\}\}.*?\{\{", re.S)
_DOLLAR = re.compile(r"\$(?=[A-Za-z_(])")
_DOLLAR_CALL = re.compile(r"\$\(")


class ItemRef(dict):
    """One item, usable both ways: n8n's `.first().json.x` / `.item.json.x` and the short `.first.x`."""

    @property
    def json(self) -> "ItemRef":
        return self

    def __call__(self) -> "ItemRef":
        return self


class ItemsRef(list):
    """A list of items that is also callable, so `.all()` (n8n) and `.all` both work."""

    def __call__(self) -> "ItemsRef":
        return self


def _item(d: dict | None) -> ItemRef:
    return ItemRef(d or {})


class InputRef:
    """`$input.all()` / `.first()` / `.last()` / `.item`, built only when an expression uses them. Building them
    eagerly wrapped every input item for every item's context: quadratic for nodes handling thousands of items."""

    def __init__(self, items: list[dict], item: dict | None):
        self._items, self._item_d = items, item
        self._all: ItemsRef | None = None

    @property
    def all(self) -> ItemsRef:
        if self._all is None:
            self._all = ItemsRef(_item(i) for i in self._items)
        return self._all

    @property
    def first(self) -> ItemRef:
        return _item(self._items[0] if self._items else {})

    @property
    def last(self) -> ItemRef:
        return _item(self._items[-1] if self._items else {})

    @property
    def item(self) -> ItemRef:
        return _item(self._item_d)

    def __getitem__(self, key: str):
        return getattr(self, key)


class NodeRef:
    def __init__(self, items: list[dict]):
        self.items = ItemsRef(_item(i) for i in items)
        self.all = self.items
        self.json = _item(items[0] if items else {})
        self.first = self.json
        self.last = _item(items[-1] if items else {})
        self.item = self.json

    def __getitem__(self, key):
        return self.json.get(key)


class NodeAccessor:
    """Supports both `$('Name')` and `$node['Name']`."""

    def __init__(self, by_name: dict[str, list[dict]]):
        self._by_name = by_name

    def __call__(self, name: str) -> NodeRef:
        if name not in self._by_name:
            raise KeyError(f"Node '{name}' has not run (or does not exist)")
        return NodeRef(self._by_name[name])

    def __getitem__(self, name: str) -> NodeRef:
        return self(name)


# Server settings that must never reach a workflow (every user can write expressions).
_SECRET_ENV = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASSWD|PWD|CREDENTIAL|PRIVATE|DATABASE_URL|DSN|AUTH)", re.I)


def env_allowed(key: str) -> bool:
    return bool(key) and not _SECRET_ENV.search(key)


class _Env:
    """`$env.NAME`: the server's environment, minus anything that looks like a secret."""

    def __getattr__(self, key):
        return os.environ.get(key) if env_allowed(key) else None

    def __getitem__(self, key):
        return self.__getattr__(key)


def _translate(expr: str) -> str:
    expr = _DOLLAR_CALL.sub("node(", expr)
    return _DOLLAR.sub("", expr)


def build_context(
    item: dict | None,
    items: list[dict],
    item_index: int,
    node_outputs: dict[str, list[dict]],
    meta: dict,
    state: dict | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "json": item or {},
        "binary": (item or {}).get("binary") or {},
        "input": InputRef(items, item),
        "items": items,
        "itemIndex": item_index,
        "node": NodeAccessor(node_outputs),
        "now": now,
        "today": now.date(),
        "timedelta": timedelta,
        "date": date,
        "datetime": datetime,
        "execution": {"id": meta.get("execution_id"), "mode": meta.get("mode")},
        "workflow": {"id": meta.get("workflow_id"), "name": meta.get("workflow_name")},
        "vars": meta.get("vars", {}),
        "state": state or {},
        "env": _Env(),
        "uuid": lambda: str(uuid.uuid4()),
        "len": len, "str": str, "int": int, "float": float, "bool": bool,
        "round": round, "min": min, "max": max, "sum": sum, "abs": abs,
        "sorted": sorted, "list": list, "dict": dict,
        "dumps": lambda v: _json.dumps(v, default=str),
        "loads": _json.loads,
    }


def evaluate(value: Any, ctx: dict[str, Any]) -> Any:
    """Recursively resolve expressions inside strings, lists and dicts."""
    if isinstance(value, str):
        return _eval_str(value, ctx)
    if isinstance(value, list):
        return [evaluate(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: evaluate(v, ctx) for k, v in value.items()}
    return value


def _eval_str(s: str, ctx: dict[str, Any]) -> Any:
    # n8n marks expression fields with a leading "="; accept and strip it.
    if s.startswith("={{"):
        s = s[1:]
    if "{{" not in s:
        return s
    rest = _EXPR.sub("", s)  # an opened {{ that is never closed is a mistake, not text
    if "{{" in rest:
        raise ValueError(f"Unclosed expression: '{{{{' without a matching '}}}}' in {rest[rest.find('{{'):][:60]!r}")
    stripped = s.strip()
    m = _EXPR.fullmatch(stripped)
    # One expression → its native value. "{{ a }} x {{ b }}" also starts with {{ and ends with }}, but it is
    # several blocks: a "}} … {{" inside means text between expressions (nested dict braces never reopen).
    if m and not _SEVERAL.search(m.group(1)):
        return _compile(m.group(1))(**ctx)

    def repl(match: re.Match) -> str:
        v = _compile(match.group(1))(**ctx)
        if isinstance(v, (dict, list)):
            return _json.dumps(v, default=str)
        return "" if v is None else str(v)

    return _EXPR.sub(repl, s)


_cache: dict[str, Any] = {}


def _compile(expr: str):
    fn = _cache.get(expr)
    if fn is None:
        try:
            fn = _env.compile_expression(_translate(expr.strip()), undefined_to_none=True)
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"Invalid expression '{{{{{expr}}}}}': {e}") from e
        if len(_cache) > 5000:
            _cache.clear()
        _cache[expr] = fn
    return fn


# ---------------------------------------------------------------------------
# Editor checks (n8n's inline expression states + field checks)
# ---------------------------------------------------------------------------

_ACCESSORS = {"json", "first", "last", "item", "all", "items"}


def _chain(n) -> tuple[object, list] | None:
    """Unwind `a.b['c'].d()` into (root, ['b', 'c', 'd()']) from a Jinja AST node."""
    from jinja2 import nodes as J

    path: list = []
    while True:
        if isinstance(n, J.Getattr):
            path.append(n.attr)
            n = n.node
        elif isinstance(n, J.Getitem) and isinstance(n.arg, J.Const):
            path.append(n.arg.value)
            n = n.node
        elif isinstance(n, J.Call) and not n.args and isinstance(n.node, J.Getattr) and n.node.attr in _ACCESSORS:
            n = n.node  # .first() / .all(): same as the attribute
        else:
            return n, list(reversed(path))


def _check_path(data, path: list, label: str) -> tuple[str, bool] | None:
    """None if `path` exists in `data`; else (message naming the missing key, whether a close match exists)."""
    import difflib

    cur, walked = data, label
    for key in path:
        if isinstance(cur, list):
            if isinstance(key, int) and -len(cur) <= key < len(cur):
                cur, walked = cur[key], f"{walked}[{key}]"
                continue
            return None  # list methods / slicing: not ours to judge
        if not isinstance(cur, dict):
            return None  # str / number methods etc.
        if key not in cur:
            # 0.75: real typos (messag→message 0.92, nmae→name 0.75), not unrelated words (query→user 0.67)
            close = difflib.get_close_matches(str(key), [str(k) for k in cur], n=1, cutoff=0.75)
            hint = f" — did you mean '{close[0]}'?" if close else (f" (it has: {', '.join(list(map(str, cur))[:8])})" if cur else "")
            return f"No field '{key}' in {walked}{hint}", bool(close)
        cur, walked = cur[key], f"{walked}.{key}"
    return None


def _field_warnings(expr: str, item: dict, node_outputs: dict[str, list[dict]]) -> list[str]:
    from jinja2 import nodes as J

    try:
        tree = _env.parse("{{ " + _translate(expr.strip()) + " }}")
    except Exception:  # noqa: BLE001 - syntax errors are reported separately
        return []
    # Fields with a fallback (`a or b`, `x if c else y`, `| default`) are often optional on purpose: flag those
    # only when they look like a typo of a real field. Anything else that's missing is flagged.
    soft: set[int] = set()
    for f in tree.find_all((J.Or, J.CondExpr, J.Filter, J.Test)):
        if isinstance(f, J.Filter) and f.name != "default" or isinstance(f, J.Test) and f.name != "defined":
            continue
        soft.update(id(x) for x in f.find_all((J.Getattr, J.Getitem)))
    found: list[str] = []
    for n in tree.find_all((J.Getattr, J.Getitem)):
        root, path = _chain(n)
        if not path:
            continue
        if isinstance(root, J.Name) and root.name == "json" and item:
            msg = _check_path(item, path, "the input item")
        elif (isinstance(root, J.Call) and isinstance(root.node, J.Name) and root.node.name == "node"
              and root.args and isinstance(root.args[0], J.Const) and root.args[0].value in node_outputs):
            rest = list(path)
            while rest and (rest[0] in _ACCESSORS or isinstance(rest[0], int)):  # .first().json / .all()[0].json
                rest = rest[1:]
            out = node_outputs[root.args[0].value]
            msg = _check_path(out[0] if out else {}, rest, f"$('{root.args[0].value}')") if out else None
        else:
            continue
        if msg and (msg[1] or id(n) not in soft) and msg[0] not in found:
            found.append(msg[0])
    return found[:3]


def inspect(value: str, ctx: dict[str, Any], node_names: set[str], node_outputs: dict[str, list[dict]]) -> list[dict]:
    """Per {{ }} segment, like n8n's inline expression editor: state valid | pending | invalid, its value or
    error, plus warnings for fields that don't exist in the data the editor has (did you mean …)."""
    segments = []
    item = ctx.get("json") or {}
    for m in _EXPR.finditer(value or ""):
        expr = m.group(1)
        seg = {"from": m.start(), "to": m.end(), "state": "valid", "warnings": []}
        try:
            _compile(expr)
        except ValueError as e:
            seg.update(state="invalid", error=str(e).split(": ", 1)[-1])
            segments.append(seg)
            continue
        refs = re.findall(r"\$\(\s*['\"]([^'\"]+)['\"]\s*\)|\$node\[\s*['\"]([^'\"]+)['\"]\s*\]", expr)
        names = {a or b for a, b in refs}
        unknown = sorted(n for n in names if n not in node_names)
        not_run = sorted(n for n in names if n in node_names and n not in node_outputs)
        if unknown:
            seg.update(state="invalid", error=f"No node named '{unknown[0]}' in this workflow")
        elif not_run:
            seg.update(state="pending", error=f"Execute '{not_run[0]}' to see this value")
        else:
            try:
                v = _compile(expr)(**ctx)
                seg["value"] = v if isinstance(v, str) else _json.dumps(v, default=str)
            except Exception as e:  # noqa: BLE001
                seg.update(state="invalid", error=f"{type(e).__name__}: {e}"[:300])
            seg["warnings"] = _field_warnings(expr, item, node_outputs)
        segments.append(seg)
    return segments
