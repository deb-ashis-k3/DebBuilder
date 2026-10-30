"""Workflow graph state: LangGraph's shared `State` with per-key reducers.

A workflow declares its state in Settings → State, e.g.

    question   replace   ""
    draft      replace
    feedback   append    []
    iteration  add       0
    messages   messages  []

Every node reads it as `{{ $state.key }}` (Code node: `state`), and writes it with the Update
State node, `set_state()` in Code, or an AI Agent keeping its conversation in a `messages` key.
Writes are reducer updates on one LangGraph channel (`WFState.state`), so state is checkpointed
with the run: it survives Human Approval / Wait pauses and resumes, and parallel branches'
updates combine by reducer at the end of each step, exactly like LangGraph.

Keys starting with `__` are engine-internal (e.g. Loop Over Items' cursor): kept in the
checkpoint, hidden from `$state` and the logs.
"""

import json
from typing import Any

REDUCERS = ("replace", "append", "add", "merge", "messages")
_EMPTY = {"append": list, "messages": list, "add": lambda: 0, "merge": dict}


def _num(v: Any) -> float | int:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return v
    try:
        f = float(str(v).strip())
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return 0


def _as_list(v: Any) -> list:
    return list(v) if isinstance(v, list) else ([] if v is None else [v])


def _message(m: Any) -> dict:
    """Chat messages are stored as {role, content[, id, name]}; strings become user messages."""
    if isinstance(m, dict):
        role = m.get("role") or m.get("type") or "human"
        out = {"role": {"user": "human", "assistant": "ai"}.get(role, role), "content": m.get("content", "")}
        out.update({k: m[k] for k in ("id", "name") if m.get(k)})
        return out
    role = getattr(m, "type", None)
    if role:  # a LangChain message object
        return {"role": role, "content": getattr(m, "content", "")}
    return {"role": "human", "content": str(m)}


def reduce(op: str, old: Any, value: Any) -> Any:
    """One reducer step: the new value of a key given its old value and an update."""
    if op == "append":
        return _as_list(old) + _as_list(value)
    if op == "add":
        return _num(old) + _num(value)
    if op == "merge":
        return {**(old if isinstance(old, dict) else {}), **(value if isinstance(value, dict) else {})}
    if op == "messages":  # LangGraph add_messages: append, but a message with a known id replaces it
        out = [_message(m) for m in _as_list(old)]
        for m in (_message(x) for x in _as_list(value)):
            i = next((k for k, x in enumerate(out) if m.get("id") and x.get("id") == m["id"]), None)
            if i is None:
                out.append(m)
            else:
                out[i] = m
        return out
    return value  # replace


def apply_updates(current: dict | None, updates: dict | None) -> dict:
    """The LangGraph reducer of the `state` channel. `updates` maps key -> [op, value]."""
    out = dict(current or {})
    for key, upd in (updates or {}).items():
        op, value = upd if isinstance(upd, (list, tuple)) and len(upd) == 2 else ("replace", upd)
        out[key] = reduce(op, out.get(key), value)
    return out


def combine(pending: list | None, op: str, value: Any) -> list:
    """Two writes to one key from the same node become one update, with the same result as
    applying them in order: append + append → both, add + add → sum, replace then append →
    replace with the appended value."""
    if pending is None or op == "replace":
        return [op, value]
    p_op, p_val = pending
    if p_op == op:
        return [op, reduce(op, p_val, value)]
    if p_op == "replace":
        return ["replace", reduce(op, p_val, value)]
    return [op, value]  # mixing two different accumulating reducers on one key: the last one wins


def parse_default(raw: Any) -> Any:
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            return json.loads(s)
        except ValueError:
            return raw
    return raw


def schema(settings: dict | None) -> dict[str, dict]:
    """{key: {"reducer": ..., "default": ...}} from the workflow's Settings → State."""
    out = {}
    for row in (settings or {}).get("state") or []:
        key = str(row.get("key") or "").strip()
        if not key:
            continue
        reducer = row.get("reducer") if row.get("reducer") in REDUCERS else "replace"
        default = parse_default(row.get("default"))
        if default is None and reducer in _EMPTY:
            default = _EMPTY[reducer]()
        out[key] = {"reducer": reducer, "default": default}
    return out


def initial_updates(settings: dict | None) -> dict:
    return {k: ["replace", v["default"]] for k, v in schema(settings).items()}


def public(state: dict | None) -> dict:
    """State as nodes and logs see it (internal `__` keys hidden)."""
    return {k: v for k, v in (state or {}).items() if not k.startswith("__")}
