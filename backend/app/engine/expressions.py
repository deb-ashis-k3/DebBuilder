"""n8n-style `{{ ... }}` expressions evaluated with a sandboxed Jinja engine.

Inside braces you can use (the `$` prefix is optional):
  $json            current input item
  $input.all / $input.first / $input.item
  $('Node Name').json / .items             output of an earlier node (by name)
  $node['Node Name'].json                   same, n8n v0 syntax
  $now, $today, $execution.id, $workflow.id/name, $env.VAR, $vars.key
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
_DOLLAR = re.compile(r"\$(?=[A-Za-z_(])")
_DOLLAR_CALL = re.compile(r"\$\(")


class NodeRef:
    def __init__(self, items: list[dict]):
        self.items = items
        self.all = items
        self.json = items[0] if items else {}
        self.first = self.json
        self.last = items[-1] if items else {}
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


class _Env:
    def __getattr__(self, key):
        return os.environ.get(key)

    def __getitem__(self, key):
        return os.environ.get(key)


def _translate(expr: str) -> str:
    expr = _DOLLAR_CALL.sub("node(", expr)
    return _DOLLAR.sub("", expr)


def build_context(
    item: dict | None,
    items: list[dict],
    item_index: int,
    node_outputs: dict[str, list[dict]],
    meta: dict,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "json": item or {},
        "input": {"all": items, "first": items[0] if items else {}, "item": item or {}, "last": items[-1] if items else {}},
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
    stripped = s.strip()
    m = _EXPR.fullmatch(stripped)
    if m:
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
