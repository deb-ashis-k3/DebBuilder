"""Compile a DebBuilder workflow (n8n-style nodes + edges) into a LangGraph
StateGraph and run it.

Semantics:
- Data flows as lists of JSON items. Each node returns {port: items}.
- After a node runs, every outgoing edge whose source port produced items
  dispatches a LangGraph `Send` to the target with exactly those items.
- Join nodes (`defer`: Merge, Compare Datasets) collect what reaches them in graph state
  (`joins`) and run once no task still in flight can deliver more to them (see _ready_joins),
  then read everything that arrived since their last run.
- Cycles are allowed (loops). A run stops after MAX_NODE_EXECUTIONS node executions (at least twice
  the node count; every Loop Over Items batch adds the node count, up to 100x): see Run.exec_limit.
- Nodes calling `interrupt()` pause the execution (status "waiting"); a later
  `resume` continues from the Postgres checkpoint.
- Sub-nodes (models, memory, tools) never run in the flow; their parent node
  instantiates them via `ctx.supply()`.
- Graph state (`$state`, see engine/state.py) is a LangGraph channel with per-key reducers:
  checkpointed with the run, combined across parallel branches at each step.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
import logging
import operator
import time
import traceback
import uuid
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from typing import Annotated, Any, TypedDict

from langchain_core.callbacks import AsyncCallbackHandler
from langgraph._internal._constants import CONFIG_KEY_CHECKPOINTER, TASKS
from langgraph.errors import GraphBubbleUp, GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send
from sqlalchemy import update

from ..config import get_settings
from ..db import Execution, SessionLocal, Workflow
from ..nodes import get_type
from ..nodes.base import NodeContext, NodeError
from . import state as gstate
from .bus import bus
from .errors import Overloaded
from .store import store, workflow_snapshot

log = logging.getLogger("debbuilder.runtime")

MAX_SAVED_ITEMS = 200
MAX_EVENT_ITEMS = 50
# A node record's state snapshot (the log's State pane): longer values (JSON chars) are replaced by a preview.
STATE_VALUE_MAX = 4000
STATE_RECORD_MAX = 32_000

_checkpointer = None
RUNNING: dict[str, asyncio.Task] = {}


def set_checkpointer(cp) -> None:
    global _checkpointer
    _checkpointer = cp


def _merge(a: dict, b: dict) -> dict:
    return {**(a or {}), **(b or {})}


def _joins(a: dict, b: dict) -> dict:
    """Reducer of the `joins` channel: {join node: [[handle, source, port, items], ...]} waiting to be read.
    Updates are {node: {"drop": n, "add": [...]}}: a join drops the n entries it read, sources add theirs."""
    out = dict(a or {})
    for k, u in (b or {}).items():
        cur = out.get(k, [])[u.get("drop", 0):] + list(u.get("add") or ())
        if cur:
            out[k] = cur
        else:
            out.pop(k, None)
    return out


class WFState(TypedDict, total=False):
    outputs: Annotated[dict, _merge]
    trigger_items: list
    steps: Annotated[int, operator.add]
    # The workflow's graph state; nodes write {key: [reducer, value]} updates.
    state: Annotated[dict, gstate.apply_updates]
    joins: Annotated[dict, _joins]


def _now() -> datetime:
    return datetime.now(timezone.utc)


_SCALARS = (str, int, bool, float, type(None))


def _plain(v: Any) -> bool:
    """Already plain JSON (dicts with str keys, lists, scalars)? ~5x cheaper than a dump + load round trip."""
    t = type(v)
    if t is dict:
        for k, x in v.items():
            if type(k) is not str:
                return False
            tx = type(x)
            if tx is dict or tx is list:
                if not _plain(x):
                    return False
            elif tx not in _SCALARS:
                return False
        return True
    if t is list:
        for x in v:
            tx = type(x)
            if tx is dict or tx is list:
                if not _plain(x):
                    return False
            elif tx not in _SCALARS:
                return False
        return True
    return t in _SCALARS


def _jsonable(v: Any) -> Any:
    if _plain(v):
        return v
    try:
        return json.loads(json.dumps(v, default=str))
    except Exception:  # noqa: BLE001
        return str(v)


def _normalize_items(items: Any, passed: set[int] | None = None) -> list[dict]:
    """Node output as a list of JSON items. Items handed on unchanged (`passed`: ids of the node's input
    items, already normalized) and items that are plain JSON are kept as they are."""
    if items is None:
        return []
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        items = [items]
    out = []
    for it in items:
        if type(it) is dict and ((passed is not None and id(it) in passed) or _plain(it)):
            out.append(it)
        else:
            out.append(_jsonable(it) if isinstance(it, dict) else {"value": _jsonable(it)})
    return out


def _fingerprint(workflow: dict) -> tuple[str, str]:
    """(content hash, JSON) of a workflow: serialized once per execution, then reused as the snapshot key,
    the compiled-graph cache key and the topology cache key."""
    raw = json.dumps(workflow, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:40], raw

def _clip(v: Any, n: int = 4000) -> Any:
    s = v if isinstance(v, str) else json.dumps(_jsonable(v), default=str)
    return s if len(s) <= n else s[:n] + "…"


# Sub-run payload limits: generous enough to show whole prompts (n8n shows them in full) without
# letting one runaway tool result bloat the execution row.
SUB_TEXT_MAX = 30_000
SUB_MESSAGES_MAX = 200


def _text(content: Any) -> str:
    """Message content as text (providers may return a list of content parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p if isinstance(p, str) else str(p.get("text") or p.get("content") or "") if isinstance(p, dict) else str(p)
                         for p in content)
    return "" if content is None else str(content)


def _message(m: Any) -> dict:
    out = {"role": getattr(m, "type", "?"), "content": _clip(_text(getattr(m, "content", m)), SUB_TEXT_MAX)}
    if getattr(m, "tool_calls", None):
        out["tool_calls"] = [{"name": t.get("name"), "args": _jsonable(t.get("args"))} for t in m.tool_calls]
    if getattr(m, "type", None) == "tool":
        out["name"] = getattr(m, "name", None)
    return out


def _payload(v: Any) -> Any:
    """Structured tool arguments stay structured (shown as JSON); anything huge becomes clipped text."""
    v = _jsonable(v)
    if isinstance(v, str):
        return _clip(v, SUB_TEXT_MAX)
    return v if len(json.dumps(v, default=str)) <= SUB_TEXT_MAX else _clip(v, SUB_TEXT_MAX)


def public_sub_run(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if not k.startswith("_")}


class TokenStreamer(AsyncCallbackHandler):
    """Streams LLM tokens to the execution channel and records every LLM / tool call (and memory
    steps reported by nodes) as a 'sub-run': the n8n-style log tree under the node.

    Each sub-run has an `id` and a `parent` (the tool call it happened inside: a sub-agent's model
    calls sit under that agent's tool call) and names the canvas node it came from (supply() tags
    models and tools with `db_node_*` metadata)."""

    def __init__(self, ctx: NodeContext, stream: bool = True):
        self.ctx = ctx
        self.stream = stream
        self.parents: dict[str, str | None] = {}
        self.open: dict[str, dict] = {}

    def _parent(self, run_id, parent_run_id) -> str | None:
        self.parents[str(run_id)] = str(parent_run_id) if parent_run_id else None
        cur = self.parents.get(str(run_id))
        while cur and cur not in self.open:
            cur = self.parents.get(cur)
        return cur

    def _publish(self, entry: dict) -> None:
        self.ctx.publish({"type": "sub_run", "sub_run": _jsonable(public_sub_run(entry))})

    def _add(self, entry: dict, parent: str | None) -> dict:
        entry.update(parent=parent, depth=(self.open[parent]["depth"] + 1) if parent in self.open else 0,
                     started_at=_now().isoformat(), status="running", _t0=time.perf_counter())
        self.open[entry["id"]] = entry
        self.ctx.sub_runs.append(entry)
        self._publish(entry)
        return entry

    def _start(self, run_id, parent_run_id, entry: dict, kw: dict) -> None:
        md = kw.get("metadata") or {}
        node = {"node_id": md.get("db_node_id"), "node_name": md.get("db_node_name"), "node_type": md.get("db_node_type")}
        self._add({"id": str(run_id), **{k: v for k, v in node.items() if v}, **entry}, self._parent(run_id, parent_run_id))

    def _end(self, run_id, **fields) -> None:
        entry = self.open.get(str(run_id))
        if entry:
            entry.update(fields, duration_ms=int((time.perf_counter() - entry.pop("_t0", time.perf_counter())) * 1000))
            self._publish(entry)

    # ---- steps nodes report themselves (memory load / save …) -------------
    def manual_start(self, entry: dict, inside_node: str | None = None) -> dict:
        """`inside_node`: a sub-node (e.g. an Agent Tool) whose running tool call this step belongs under."""
        parent = None
        if inside_node:
            parent = next((e["id"] for e in reversed(self.ctx.sub_runs)
                           if e.get("node_id") == inside_node and e["type"] == "tool" and e["status"] == "running"), None)
        node = {k.removeprefix("db_"): entry.pop(k) for k in ("db_node_id", "db_node_name", "db_node_type") if k in entry}
        entry["input"] = _payload(entry.get("input"))
        entry.setdefault("name", node.get("node_name") or entry.get("action") or entry["type"])
        return self._add({"id": uuid.uuid4().hex, **node, **entry}, parent)

    def manual_end(self, entry: dict, **fields) -> None:
        if "output" in fields:
            fields["output"] = _payload(fields["output"])
        self._end(entry["id"], status=fields.pop("status", "success"), **fields)

    # ---- LangChain callbacks ----------------------------------------------
    async def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kw) -> None:
        self.parents[str(run_id)] = str(parent_run_id) if parent_run_id else None

    async def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kw) -> None:
        params = kw.get("invocation_params") or {}
        model = params.get("model") or params.get("model_name") or (serialized or {}).get("kwargs", {}).get("model") or "LLM"
        msgs = [_message(m) for m in (messages[0] if messages else [])][-SUB_MESSAGES_MAX:]
        name = (kw.get("metadata") or {}).get("db_node_name") or str(model)
        self._start(run_id, parent_run_id, {"type": "llm", "name": name, "model": str(model), "input": msgs}, kw)

    async def on_llm_new_token(self, token: str, **kwargs) -> None:
        if self.stream and isinstance(token, str) and token:
            self.ctx.publish({"type": "token", "token": token})

    async def on_llm_end(self, response, *, run_id, **kw) -> None:
        gen = response.generations[0][0] if response.generations and response.generations[0] else None
        msg = getattr(gen, "message", None)
        usage = getattr(msg, "usage_metadata", None) or (response.llm_output or {}).get("token_usage") or {}
        out: dict = {"text": _clip(getattr(gen, "text", "") or "", SUB_TEXT_MAX)}
        if msg is not None and getattr(msg, "tool_calls", None):
            out["tool_calls"] = [{"name": t["name"], "args": _jsonable(t.get("args"))} for t in msg.tool_calls]
        meta = getattr(msg, "response_metadata", None) or {}
        fields = {}
        if meta.get("model_name") or meta.get("model"):
            fields["model"] = str(meta.get("model_name") or meta.get("model"))  # the model that actually answered
        self._end(run_id, status="success", output=out, tokens={
            "input": usage.get("input_tokens") or usage.get("prompt_tokens"),
            "output": usage.get("output_tokens") or usage.get("completion_tokens"),
            "total": usage.get("total_tokens"),
        }, **fields)

    async def on_llm_error(self, error, *, run_id, **kw) -> None:
        self._end(run_id, status="error", error=str(error))

    async def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, **kw) -> None:
        tool = (serialized or {}).get("name", "tool")
        name = (kw.get("metadata") or {}).get("db_node_name") or tool
        self._start(run_id, parent_run_id, {"type": "tool", "name": name, "tool": tool,
                                            "input": _payload(kw.get("inputs") or input_str)}, kw)

    async def on_tool_end(self, output, *, run_id, **kw) -> None:
        self._end(run_id, status="success", output=_payload(_text(getattr(output, "content", output))))

    async def on_tool_error(self, error, *, run_id, **kw) -> None:
        self._end(run_id, status="error", error=str(error))

    async def on_retriever_start(self, serialized, query, *, run_id, parent_run_id=None, **kw) -> None:
        name = (kw.get("metadata") or {}).get("db_node_name") or (serialized or {}).get("name") or "Retriever"
        self._start(run_id, parent_run_id, {"type": "retriever", "name": name, "input": {"query": _clip(query, SUB_TEXT_MAX)}}, kw)

    async def on_retriever_end(self, documents, *, run_id, **kw) -> None:
        self._end(run_id, status="success", output=_payload(
            [{"pageContent": d.page_content, "metadata": d.metadata} for d in documents or []]))

    async def on_retriever_error(self, error, *, run_id, **kw) -> None:
        self._end(run_id, status="error", error=str(error))



class Topology:
    """A workflow's edges indexed once per version (cached by content hash): every node of every execution
    walks them, so linear scans over the edge list made large graphs quadratic."""

    def __init__(self, workflow: dict):
        self.nodes: dict[str, dict] = {n["id"]: n for n in workflow.get("nodes", [])}
        self.out: dict[str, list[dict]] = {}
        self.inp: dict[str, list[dict]] = {}
        self.subs: dict[tuple[str, str | None], list[dict]] = {}
        for e in workflow.get("edges", []):
            s, t = e.get("source"), e.get("target")
            if s not in self.nodes:
                continue
            self.subs.setdefault((t, e.get("targetHandle")), []).append(self.nodes[s])
            if t in self.nodes and not (e.get("targetHandle") or "main").startswith("ai_"):
                self.out.setdefault(s, []).append(e)
                self.inp.setdefault(t, []).append(e)
        types = {nid: _type_or_none(n.get("type")) for nid, n in self.nodes.items()}
        self.joins = {nid for nid, nt in types.items() if nt is not None and nt.defer}
        self.interrupts = {nid for nid, nt in types.items() if nt is not None and nt.interrupts}
        self._reach: dict[str, frozenset] = {}
        self._order: dict[str, list[str]] = {}

    def reachable(self, start: str) -> list[str]:
        """Nodes reachable from `start` (itself included), in discovery order."""
        got = self._order.get(start)
        if got is None:
            seen, order, stack = set(), [], [start]
            while stack:
                nid = stack.pop()
                if nid in seen:
                    continue
                seen.add(nid)
                order.append(nid)
                stack.extend(e["target"] for e in self.out.get(nid, ()))
            got = self._order[start] = order
        return got

    def reach(self, start: str) -> frozenset:
        got = self._reach.get(start)
        if got is None:
            got = self._reach[start] = frozenset(self.reachable(start))
        return got

    def ancestors(self, target: str) -> set[str]:
        seen, stack = {target}, [target]
        while stack:
            for e in self.inp.get(stack.pop(), ()):
                if e["source"] not in seen:
                    seen.add(e["source"])
                    stack.append(e["source"])
        return seen


def _type_or_none(type_name: str | None):
    try:
        return get_type(type_name)
    except ValueError:
        return None


_TOPOLOGIES: "OrderedDict[str, Topology]" = OrderedDict()


def _topology(workflow: dict, fp: str | None = None) -> Topology:
    if fp is None:
        return Topology(workflow)
    topo = _TOPOLOGIES.get(fp)
    if topo is None:
        topo = _TOPOLOGIES[fp] = Topology(workflow)
        while len(_TOPOLOGIES) > _GRAPH_CACHE_SIZE:
            _TOPOLOGIES.popitem(last=False)
    else:
        _TOPOLOGIES.move_to_end(fp)
    return topo


class Run:
    """Per-execution runtime shared by every node of one workflow run."""

    def __init__(self, workflow: dict, execution_id: str, mode: str, records: list | None = None,
                 fp: str | None = None, parent: "Run | None" = None):
        self.workflow = workflow
        self.execution_id = execution_id
        self.mode = mode
        self.fp = fp  # content hash of `workflow` (see _fingerprint)
        self.saver = None  # per-execution checkpointer (see _saver_for)
        self.topo = _topology(workflow, fp)
        self.nodes: dict[str, dict] = self.topo.nodes
        self.edges: list[dict] = workflow.get("edges", [])
        self.records: list[dict] = list(records or [])
        self.state_outputs: dict[str, dict] = {}
        self._seen_outputs: dict | None = None
        self._by_name: dict | None = None
        self.response: dict | None = None  # set by "Respond to Webhook"
        self.allowed: set[str] | None = None  # partial execution ("Execute step")
        self.destination: str | None = None
        self.prior_ms = 0  # time spent before a pause (resumed executions)
        self.parent = parent  # the run whose Execute Workflow node started this one
        # Records: how often each node ran (run_index) and its open ("waiting") record, without rescanning.
        self.run_counts: Counter = Counter(r.get("node_id") for r in self.records)
        self.open_records = {r.get("node_id"): i for i, r in enumerate(self.records) if r.get("status") in ("running", "waiting")}
        self.final: tuple[dict, dict] | None = None  # (record, untrimmed outputs) of the last finished node
        # Joins (see _ready_joins): tasks dispatched and not finished, per node; joins with input waiting.
        self.pending: dict[str, int] = {}
        self.join_wait: set[str] = set()
        # Node executions so far and the limit (MAX_NODE_EXECUTIONS, at least twice the node count); each Loop
        # Over Items batch raises it by the node count, up to 100x, so long loops work and runaway cycles stop.
        s = get_settings()
        self.executed = 0
        self.exec_limit = max(1, s.max_node_executions, 2 * len(self.nodes))
        self.exec_ceiling = self.exec_limit * 100
        # Concurrency slot (see _drive_limited / _idle).
        self.queued = self.holding = self.lent = False
        self.idle = 0
        self.wf_channel = f"wf:{workflow.get('id')}"
        # Keep a replay buffer only for runs someone may watch: editor runs, or a workflow open in an editor.
        if mode == "manual":
            bus.keep(execution_id)
        elif bus.watching(self.wf_channel):
            bus.keep(execution_id, group=self.wf_channel)
        settings = workflow.get("settings") or {}
        self.meta = {
            "execution_id": execution_id,
            "mode": mode,
            "workflow_id": str(workflow.get("id")),
            "workflow_name": workflow.get("name"),
            "owner_id": workflow.get("owner_id"),
            "vars": {v["key"]: v.get("value") for v in settings.get("variables", []) if v.get("key")},
            "depth": (parent.meta.get("depth", 0) + 1) if parent is not None else 0,  # sub-workflow nesting
        }

    # ---- graph topology ----------------------------------------------------
    def is_flow_edge(self, e: dict) -> bool:
        return not (e.get("targetHandle") or "main").startswith("ai_")

    def flow_out(self, nid: str) -> list[dict]:
        return self.topo.out.get(nid, [])

    def flow_in(self, nid: str) -> list[dict]:
        return self.topo.inp.get(nid, [])

    def sub_nodes(self, nid: str, handle: str) -> list[dict]:
        return self.topo.subs.get((nid, handle), [])

    def set_outputs(self, outputs: dict) -> None:
        self.state_outputs.update(outputs)
        self._by_name = None

    def outputs_by_name(self) -> dict[str, list[dict]]:
        if self._by_name is not None:
            return self._by_name
        out = {}
        for nid, ports in self.state_outputs.items():
            node = self.nodes.get(nid)
            if not node or not isinstance(ports, dict):
                continue
            items = ports.get("main")
            if items is None:
                items = next((v for v in ports.values() if v), [])
            out[node.get("name") or nid] = items
        self._by_name = out
        return out

    def watched(self) -> bool:
        return bus.wants(self.execution_id) or bus.wants(self.wf_channel)

    def publish(self, event: dict) -> None:
        if not self.watched():
            return  # nobody listens and nobody will replay it: the common production case
        event = {"execution_id": self.execution_id, **event}
        bus.publish(self.execution_id, event)
        # The editor listens per workflow so it sees webhook/WhatsApp/voice runs live.
        bus.publish(self.wf_channel, event)


# ---------------------------------------------------------------------------
# Graph compilation
# ---------------------------------------------------------------------------


def _reachable(run: Run, start: str) -> list[str]:
    return run.topo.reachable(start)


# The execution being driven. Each execution runs in its own context (see _launch), so node
# functions look the run up here instead of closing over it, which lets compiled graphs be
# cached and shared between executions of the same workflow.
_RUN: contextvars.ContextVar[Run] = contextvars.ContextVar("debbuilder_run")
_GRAPHS: "OrderedDict[Any, Any]" = OrderedDict()
_GRAPH_CACHE_SIZE = 256


def _make_node_fn(nid: str, is_entry: bool):
    async def fn(payload: dict) -> dict:
        run = _RUN.get()
        node = run.nodes[nid]
        nt = get_type(node["type"])
        run.executed += 1
        if run.executed > run.exec_limit:
            raise NodeError(f"Stopped after {run.exec_limit} node executions: does the workflow loop without an exit? "
                            f"(limit: MAX_NODE_EXECUTIONS)", node.get("name"))
        outs = payload.get("outputs")
        if outs and outs is not run._seen_outputs:  # Sends of one step share the dict: merge it once
            run._seen_outputs = outs
            run.set_outputs(outs)
        state = payload.get("state") or {}  # Send payloads carry the state as of dispatch
        inputs_by_handle: dict[str, list] = {}
        sources: list[dict] = []
        consumed = None
        if "input" in payload:  # dispatched via Send
            items = payload["input"]
            handle = payload.get("input_handle", "main")
            inputs_by_handle[handle] = items
            if payload.get("from"):
                sources.append({"node_id": payload["from"], "port": payload.get("from_port", "main"), "handle": handle, "count": len(items)})
        elif is_entry:
            items = payload.get("trigger_items") or [{}]
            inputs_by_handle["main"] = items
        else:  # a join: everything that reached it since it last ran
            items = []
            consumed = (payload.get("joins") or {}).get(nid) or []
            for handle, src, port, got in consumed:
                inputs_by_handle.setdefault(handle, []).extend(got)
                items.extend(got)
                sources.append({"node_id": src, "port": port, "handle": handle, "count": len(got)})
        result = await _execute_node(run, node, nt, items, inputs_by_handle, sources, state)
        joins = _arrivals(run, nid, result["outputs"][nid]) if run.topo.joins else None
        if consumed:
            joins = joins or {}
            joins[nid] = {"drop": len(consumed), **joins.get(nid, {})}
        if joins:
            result["joins"] = joins
        return result

    return fn


def _arrivals(run: Run, nid: str, outputs: dict) -> dict | None:
    """What this node delivers to join nodes: stored in graph state (checkpointed) until the join runs."""
    if nid == run.destination:
        return None
    out = None
    for e in run.topo.out.get(nid, ()):
        t = e["target"]
        if t not in run.topo.joins or (run.allowed is not None and t not in run.allowed):
            continue
        items = outputs.get(e.get("sourceHandle") or "main")
        if not items:
            continue
        out = out or {}
        out.setdefault(t, {"add": []})["add"].append([e.get("targetHandle") or "main", nid, e.get("sourceHandle") or "main", items])
        run.join_wait.add(t)
    return out


def _ready_joins(run: Run) -> list[str]:
    """Joins with input waiting that nothing still in flight can reach any more: they run now, once, with
    everything that arrived. A branch that produced nothing (If's empty side) never blocks them."""
    pending, ready = run.pending, []
    for j in list(run.join_wait):
        others = [p for p in pending if p != j] + [w for w in run.join_wait if w != j and w not in ready]
        if j in pending or any(j in run.topo.reach(p) for p in others):
            continue
        ready.append(j)
    if not ready and not pending and run.join_wait:
        ready = list(run.join_wait)  # joins feeding each other in a cycle: run them rather than stall
    for j in ready:
        run.join_wait.discard(j)
        pending[j] = pending.get(j, 0) + 1
    return ready


def _make_router(nid: str):
    # async on purpose: LangGraph runs a sync router in a thread pool (a thread hop per routing step)
    async def route(state: WFState):
        run = _RUN.get()
        topo = run.topo
        targets: list = []
        if nid != run.destination:
            ports = (state.get("outputs") or {}).get(nid) or {}
            for e in topo.out.get(nid, ()):
                tgt = e["target"]
                items = ports.get(e.get("sourceHandle") or "main")
                if not items or tgt in topo.joins or (run.allowed is not None and tgt not in run.allowed):
                    continue  # joins get their input through graph state (_arrivals)
                targets.append(
                    Send(
                        tgt,
                        {
                            "outputs": state.get("outputs") or {},
                            "input": items,
                            "input_handle": e.get("targetHandle") or "main",
                            "from": nid,
                            "from_port": e.get("sourceHandle") or "main",
                            "state": state.get("state") or {},
                        },
                    )
                )
        if topo.joins:  # bookkeeping only joins need: which tasks are still in flight
            pending = run.pending
            for s in targets:
                pending[s.node] = pending.get(s.node, 0) + 1
            n = pending.get(nid, 0) - 1
            if n > 0:
                pending[nid] = n
            else:
                pending.pop(nid, None)
            if run.join_wait:
                targets.extend(_ready_joins(run))
        return targets or END

    return route


def _ancestors(run: Run, target: str) -> set[str]:
    return run.topo.ancestors(target)


def compile_graph(run: Run, trigger_id: str, destination: str | None = None):
    """Compile the graph from `trigger_id`. With `destination` (n8n "Execute step"),
    only nodes on a path to that node run and execution stops after it."""
    nodes = _reachable(run, trigger_id)
    if destination:
        keep = _ancestors(run, destination)
        if destination not in nodes:
            raise ValueError("That node isn't connected to the trigger being run")
        nodes = [n for n in nodes if n in keep]
        run.allowed = set(nodes)
    run.destination = destination
    run.saver = _saver_for(run, nodes)

    # Compiling costs ~20ms of CPU; reuse graphs for identical workflow versions. A pausable run's
    # checkpointer is lent to the shared graph per invocation (see _drive), not compiled in.
    key = (run.fp or _fingerprint(run.workflow)[0], trigger_id, destination)
    cached = _GRAPHS.get(key)
    if cached is not None:
        _GRAPHS.move_to_end(key)
        return cached

    g = StateGraph(WFState)
    for nid in nodes:
        g.add_node(nid, _make_node_fn(nid, nid == trigger_id))
    g.add_edge(START, trigger_id)
    for nid in nodes:
        # Terminal nodes need a router only for the joins' in-flight bookkeeping.
        if run.topo.joins or (nid != destination and any(e["target"] in nodes for e in run.topo.out.get(nid, ()))):
            g.add_conditional_edges(nid, _make_router(nid))
        else:
            g.add_edge(nid, END)
    graph = g.compile()
    _GRAPHS[key] = graph
    while len(_GRAPHS) > _GRAPH_CACHE_SIZE:
        _GRAPHS.popitem(last=False)
    return graph


def _saver_for(run: "Run", nodes: list[str]):
    """Checkpointing only matters when a node can pause the run (interrupt → resume later).
    Everything else runs without one: checkpoints cost several Postgres round-trips per step.

    Pausable runs get their own saver instance on the shared pool: AsyncPostgresSaver guards
    every operation with one asyncio.Lock per instance, so a single global saver serialised
    all executions behind each other."""
    if _checkpointer is None or not run.topo.interrupts.intersection(nodes):
        return None
    return type(_checkpointer)(_checkpointer.conn)


# ---------------------------------------------------------------------------
# Node execution
# ---------------------------------------------------------------------------


def _trim(ports: dict, limit: int) -> dict:
    return {p: items[:limit] for p, items in ports.items()}


def _clip_state(state: dict) -> dict:
    """A node record's state snapshot, size-limited: every record carries one, so a large state (a long
    conversation, a big list) repeated per node made execution rows huge."""
    out, total = {}, 0
    for k, v in state.items():
        s = json.dumps(v, default=str)
        if len(s) > STATE_VALUE_MAX or total + len(s) > STATE_RECORD_MAX:
            out[k] = {"_truncated": True, "size": len(s), "preview": s[:500]}
            total += 600
        else:
            out[k] = v if _plain(v) else json.loads(s)
            total += len(s)
    return out


# Nodes that only sleep: they give their execution slot back meanwhile (see _idle).
_IDLE_TYPES = {"flow.wait"}


@contextlib.asynccontextmanager
async def _idle(run: Run):
    """Let other executions use this run's concurrency slot while it sleeps (a long Wait must not starve
    traffic); take one again before continuing."""
    run.idle += 1
    if run.idle == 1 and run.holding:
        run.holding, run.lent = False, True
        _slots.release()
    try:
        yield
    except asyncio.CancelledError:
        run.idle -= 1  # canceled: nothing left to run, don't queue for a slot
        raise
    except BaseException:
        await _reclaim(run)
        raise
    await _reclaim(run)


async def _reclaim(run: Run) -> None:
    run.idle -= 1
    if run.idle == 0 and run.lent:
        run.lent = False
        await _slots.acquire()
        run.holding = True


def _int(v: Any, default: int) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _first_port(nt) -> str:
    """A node's main output: "main", else its first one (Loop Over Items: "done", Switch: "out0")."""
    outs = nt.outputs
    if not outs:
        return "out0" if nt.dynamic_outputs else "main"
    return "main" if "main" in outs else ("done" if "done" in outs else outs[0])


def _passthrough(run: Run, nid: str, nt, items: list, inputs_by_handle: dict) -> dict:
    """A disabled node hands its input on unchanged, like n8n: through its first output (Loop Over Items:
    'done'); a disabled join passes on what reached its first input, once."""
    if nt.defer and nt.inputs:
        first = nt.inputs[0]
        if any((e.get("targetHandle") or "main") == first for e in run.topo.inp.get(nid, ())):
            items = inputs_by_handle.get(first, [])
    return {_first_port(nt): items}


async def _execute_node(run: Run, node: dict, nt, items: list, inputs_by_handle: dict,
                        sources: list | None = None, state: dict | None = None) -> dict:
    nid = node["id"]
    settings = node.get("settings") or {}
    started = time.perf_counter()
    started_at = _now().isoformat()
    reopened = run.open_records.get(nid)  # a resumed node keeps the run index of its "waiting" record
    record = {
        "node_id": nid,
        "name": node.get("name"),
        "type": node["type"],
        "status": "running",
        "started_at": started_at,
        "input": items[:MAX_SAVED_ITEMS],
        "input_count": len(items),
        "sources": sources or [],
        "run_index": run.records[reopened].get("run_index", 0) if reopened is not None else run.run_counts[nid],
    }
    run.publish({"type": "node_started", "node_id": nid, "name": node.get("name"), "started_at": started_at,
                 "run_index": record["run_index"]})
    ctx = NodeContext(run, node, nt, items, inputs_by_handle, graph_state=state or {})

    on_error = settings.get("on_error", "stop")  # stop | continue | error_output
    # Retry settings come from the editor as numbers or text: anything unusable falls back to the default.
    tries = min(10, max(1, _int(settings.get("max_tries"), 3))) if settings.get("retry_on_fail") else 1
    wait_ms = min(300_000, max(0, _int(settings.get("wait_between_tries_ms"), 1000)))

    outputs: dict[str, list] = {}
    error: str | None = None
    pin = node.get("pin_data")
    if node.get("disabled"):
        outputs = _passthrough(run, nid, nt, items, inputs_by_handle)
    elif pin is not None and run.mode == "manual":
        # n8n semantics: pinned output replaces execution in editor (manual) runs.
        outputs = pin if isinstance(pin, dict) else {"main": pin}
        record["pinned"] = True
    else:
        for attempt in range(tries):
            try:
                if nt.type in _IDLE_TYPES and _slots is not None:
                    async with _idle(run):
                        outputs = await nt.execute(ctx) or {}
                else:
                    outputs = await nt.execute(ctx) or {}
                error = None
                break
            except GraphBubbleUp:  # interrupt() — let LangGraph pause the run
                record.update(status="waiting", duration_ms=int((time.perf_counter() - started) * 1000))
                _upsert_record(run, record)
                run.publish({"type": "node_waiting", "node_id": nid})
                raise
            except Exception as e:  # noqa: BLE001
                error = f"{type(e).__name__}: {e}"
                log.debug("node %s failed: %s", nid, traceback.format_exc())
                if attempt < tries - 1:
                    run.publish({"type": "log", "node_id": nid, "message": f"Retry {attempt + 1}: {error}"})
                    await asyncio.sleep(wait_ms / 1000)

    if error:
        if on_error == "continue":
            outputs = {"main": [{**(it if isinstance(it, dict) else {}), "error": error} for it in (items or [{}])]}
        elif on_error == "error_output":
            outputs = {"error": [{**(it if isinstance(it, dict) else {}), "error": error} for it in (items or [{}])]}
        else:
            record.update(status="error", error=error, duration_ms=int((time.perf_counter() - started) * 1000),
                          finished_at=_now().isoformat(), sub_runs=_jsonable([public_sub_run(e) for e in ctx.sub_runs]))
            _upsert_record(run, record)
            run.publish({"type": "node_finished", "node_id": nid, "status": "error", "error": error,
                         "duration_ms": record["duration_ms"], "finished_at": record["finished_at"]})
            raise NodeError(error, node.get("name"))

    for k in [k for k in ctx.state_updates if k.startswith("__") and k.split(":", 1)[-1] != nid]:
        del ctx.state_updates[k]  # engine-internal keys (`__loop:<node>`) belong to their node only
    passed = {id(it) for it in items} if items else None
    outputs = {port: _normalize_items(v, passed) for port, v in outputs.items()}
    first = _first_port(nt)
    if settings.get("always_output_data") and not outputs.get(first):  # e.g. a Filter keeping nothing
        outputs[first] = [{}]
    if outputs.get("loop") and nt.type == "flow.loop":  # another batch: budget for one more pass of the body
        run.exec_limit = min(run.exec_ceiling, run.exec_limit + len(run.nodes))
    record.update(
        status="error" if error else "success",
        error=error,
        output=_trim(outputs, MAX_SAVED_ITEMS),
        item_counts={p: len(v) for p, v in outputs.items()},
        duration_ms=int((time.perf_counter() - started) * 1000),
        finished_at=_now().isoformat(),
    )
    if ctx.sub_runs:
        record["sub_runs"] = _jsonable([public_sub_run(e) for e in ctx.sub_runs])
    if state or ctx.state_updates:  # the state after this node, for the log's State pane
        record["state"] = _clip_state(gstate.public(gstate.apply_updates(state, ctx.state_updates)))
        if ctx.state_updates:
            record["state_updates"] = sorted(k for k in ctx.state_updates if not k.startswith("__"))
    _upsert_record(run, record)
    if nt.provides is None:
        run.final = (record, outputs)
    if run.watched():
        run.publish(
            {
                "type": "node_finished",
                "node_id": nid,
                "status": record["status"],
                "error": error,
                "output": _trim(outputs, MAX_EVENT_ITEMS),
                "item_counts": record["item_counts"],
                "duration_ms": record["duration_ms"],
                "started_at": started_at,
                "finished_at": record["finished_at"],
                "run_index": record["run_index"],
                "pinned": record.get("pinned", False),
            }
        )
    result: dict = {"outputs": {nid: outputs}, "steps": 1}
    if ctx.state_updates:
        result["state"] = _jsonable(ctx.state_updates)
    return result


def _upsert_record(run: Run, record: dict) -> None:
    # A resumed node re-runs from the top; replace its "waiting" record.
    nid = record["node_id"]
    i = run.open_records.get(nid)
    if i is not None and i < len(run.records):
        run.records[i] = record
    else:
        i = len(run.records)
        run.records.append(record)
        run.run_counts[nid] += 1
    if record.get("status") in ("running", "waiting"):
        run.open_records[nid] = i
    else:
        run.open_records.pop(nid, None)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def workflow_to_dict(wf: Workflow, live: bool = False) -> dict:
    """The graph to run. live=True: what production runs (the published version, or the draft if never published);
    False: the editor's draft (manual / test runs)."""
    return {
        "id": str(wf.id),
        "name": wf.name,
        "owner_id": str(wf.owner_id) if wf.owner_id else None,
        "nodes": wf.live_nodes if live else (wf.nodes or []),
        "edges": wf.live_edges if live else (wf.edges or []),
        "settings": wf.settings or {},
        "version": wf.published_version_number if live and wf.published_version_id else None,
    }


def find_trigger(workflow: dict, trigger_type: str | None = None, node_id: str | None = None) -> dict | None:
    nodes = workflow.get("nodes", [])
    if node_id:
        return next((n for n in nodes if n["id"] == node_id), None)
    triggers = [n for n in nodes if get_type(n["type"]).trigger and not n.get("disabled")]
    if trigger_type:
        return next((n for n in triggers if n["type"] == trigger_type), None)
    for preferred in ("trigger.manual", "trigger.chat", "trigger.subworkflow"):
        hit = next((n for n in triggers if n["type"] == preferred), None)
        if hit:
            return hit
    return triggers[0] if triggers else None


def final_output(run: Run) -> list[dict]:
    """The last finished node's items: what a caller gets back (sub-workflow, webhook 'last node', workflow
    tool). In full: only what is saved in the node records is trimmed."""
    for r in reversed(run.records):
        if r.get("status") == "success" and get_type(r["type"]).provides is None:
            out = run.final[1] if run.final is not None and run.final[0] is r else (r.get("output") or {})
            return out.get("main") or next((v for v in out.values() if v), [])
    return []


def _trigger_for(workflow: dict, node_id: str) -> str | None:
    """Pick the trigger that leads to `node_id` (preferring manual triggers)."""
    topo = Topology(workflow)
    hits = [n for n in workflow.get("nodes", []) if get_type(n["type"]).trigger and not n.get("disabled")
            and node_id in topo.reach(n["id"])]
    hits.sort(key=lambda n: n["type"] != "trigger.manual")
    return hits[0]["id"] if hits else None


def _timeout(workflow: dict) -> float | None:
    """EXECUTION_TIMEOUT_SECONDS, or the workflow's settings.execution_timeout when that is lower (0 = none)."""
    limits = []
    for v in (get_settings().execution_timeout_seconds, (workflow.get("settings") or {}).get("execution_timeout")):
        try:
            if v is not None and float(v) > 0:
                limits.append(float(v))
        except (TypeError, ValueError):
            pass
    return min(limits) if limits else None


# Modes that never wait for a slot / are never refused by admission control (see _launch).
_UNQUEUED = ("manual", "subworkflow")


async def execute_workflow(
    workflow: dict,
    *,
    trigger_node_id: str | None = None,
    trigger_type: str | None = None,
    items: list[dict] | None = None,
    mode: str = "manual",
    wait: bool = True,
    destination_node_id: str | None = None,
    parent: Run | None = None,
) -> dict:
    """`parent`: the run whose node starts this one as a sub-workflow (nesting depth, cancellation)."""
    if mode not in _UNQUEUED and _waiting_for_slot() >= get_settings().max_queued_executions:
        raise Overloaded()  # webhooks answer 503 + Retry-After instead of queueing without limit
    if parent is not None and parent.meta.get("depth", 0) + 1 > get_settings().max_subworkflow_depth:
        raise ValueError(f"Sub-workflows are nested more than {get_settings().max_subworkflow_depth} levels deep "
                         f"(MAX_SUBWORKFLOW_DEPTH): does a workflow call itself?")
    if destination_node_id:
        # Stored in the snapshot so a resumed execution compiles the same partial graph.
        workflow = {**workflow, "_destination": destination_node_id}
        if not trigger_node_id and not trigger_type:
            trigger_node_id = _trigger_for(workflow, destination_node_id)
    trigger = find_trigger(workflow, trigger_type, trigger_node_id)
    if not trigger:
        raise ValueError("Workflow has no trigger node" + (f" of type {trigger_type}" if trigger_type else ""))
    fp, raw = _fingerprint(workflow)
    if destination_node_id and destination_node_id not in _topology(workflow, fp).reach(trigger["id"]):
        raise ValueError("That node isn't connected to the trigger being run")

    # Written by the batched execution store (engine/store.py), not a transaction per execution.
    ex_id = uuid.uuid4()
    # A trigger always runs, with one empty item when started without data (n8n's manual trigger); nodes
    # after it only run when an edge delivers items to them.
    trigger_items = _normalize_items(items) or [{}]
    await store.started({
        "id": ex_id, "workflow_id": uuid.UUID(workflow["id"]), "status": "running", "mode": mode,
        "trigger_node": trigger["id"], "input": _jsonable(items or []), "output": [], "node_runs": [],
        "workflow_snapshot": None, "waiting_for": None, "error": None, "started_at": _now(),
        "finished_at": None, "duration_ms": None,
    }, workflow, immediate=mode == "manual", digest=fp, raw=raw)
    exec_id = str(ex_id)

    run = Run(workflow, exec_id, mode, fp=fp, parent=parent)
    inp = {"outputs": {}, "trigger_items": trigger_items, "steps": 0,
           "state": gstate.initial_updates(workflow.get("settings"))}
    return await _launch(run, trigger["id"], inp, wait)


async def resume_execution(execution_id: str, value: Any, wait: bool = False) -> dict:
    # The "waiting" row may still sit in the batched writer: write it before claiming it in the database.
    await store.flush()
    async with SessionLocal() as s:
        # Claim it atomically: of concurrent resumes exactly one gets the row, the others are refused.
        claimed = (await s.execute(
            update(Execution).where(Execution.id == uuid.UUID(execution_id), Execution.status == "waiting")
            .values(status="running", waiting_for=None).returning(Execution.id)
            .execution_options(synchronize_session=False))).scalar_one_or_none()
        if claimed is None:
            raise ValueError("Execution is not waiting for input (finished, canceled or already resumed)")
        await s.commit()
        ex = await s.get(Execution, claimed)
        workflow, trigger_id, records, mode = await workflow_snapshot(ex, s), ex.trigger_node, ex.node_runs, ex.mode
        prior_ms = ex.duration_ms or 0
        if "owner_id" not in workflow:  # snapshot taken before per-user workspaces
            wf = await s.get(Workflow, ex.workflow_id)
            workflow = {**workflow, "owner_id": str(wf.owner_id) if wf and wf.owner_id else None}
    run = Run(workflow, execution_id, mode, records, fp=_fingerprint(workflow)[0])
    run.prior_ms = prior_ms
    return await _launch(run, trigger_id, Command(resume=value), wait)


_slots: asyncio.Semaphore | None = None
_queued = 0  # executions launched and not holding a slot yet (admission control, see execute_workflow)


def _waiting_for_slot() -> int:
    """Launched executions that will have to wait: those beyond the slots still free."""
    free = _slots._value if _slots is not None else max(1, get_settings().max_concurrent_executions)
    return max(0, _queued - free)


async def _drive_limited(run: Run, trigger_id: str, inp: Any) -> dict:
    """At most `max_concurrent_executions` run at once; the rest queue here. Without this a
    burst of async webhooks started hundreds of executions that exhausted the DB pool.
    Sub-workflows skip the queue: their parent already holds a slot and waits for them
    (their nesting is limited by MAX_SUBWORKFLOW_DEPTH)."""
    global _slots, _queued
    if run.mode == "subworkflow":
        return await _drive(run, trigger_id, inp)
    if _slots is None:
        _slots = asyncio.Semaphore(max(1, get_settings().max_concurrent_executions))
    await _slots.acquire()
    run.holding = True
    if run.queued:
        run.queued = False
        _queued -= 1
    try:
        return await _drive(run, trigger_id, inp)
    finally:
        if run.holding:  # not while a Wait gave it back (and was canceled before taking one again)
            run.holding = False
            _slots.release()


async def _launch(run: Run, trigger_id: str, inp: Any, wait: bool) -> dict:
    global _queued
    if run.mode != "subworkflow":
        run.queued = True
        _queued += 1

    def done(_) -> None:
        global _queued
        RUNNING.pop(run.execution_id, None)
        if run.queued:  # canceled before it got a slot
            run.queued = False
            _queued -= 1

    # Fresh context so a sub-workflow doesn't inherit the parent's LangGraph config.
    task = asyncio.get_running_loop().create_task(_drive_limited(run, trigger_id, inp), context=contextvars.Context())
    RUNNING[run.execution_id] = task
    task.add_done_callback(done)
    if not wait:
        return {"execution_id": run.execution_id, "status": "running"}
    if run.parent is None:
        # A caller going away (e.g. a webhook client disconnecting) doesn't stop the execution.
        return await asyncio.shield(task)
    # A sub-workflow is part of its parent: canceling the parent (or its timeout) cancels it too.
    res = await task
    cur = asyncio.current_task()
    if cur is not None and cur.cancelling():
        raise asyncio.CancelledError
    return res


async def _restore_progress(run: Run, graph, config: dict) -> None:
    """Resumed runs: rebuild what _ready_joins needs (tasks still to run, joins with input waiting) from
    the checkpoint: the paused step's unfinished tasks, and what its finished tasks dispatched."""
    snap = await graph.aget_state(config)
    run.join_wait.update((snap.values or {}).get("joins") or {})
    tup = await run.saver.aget_tuple(config)
    writes = list(getattr(tup, "pending_writes", None) or ())
    finished = {tid for tid, ch, _ in writes if not ch.startswith("__")}
    pending, pulled = run.pending, set()
    for t in snap.tasks or ():
        if t.id not in finished:
            pending[t.name] = pending.get(t.name, 0) + 1
    for tid, ch, val in writes:
        if ch == TASKS and isinstance(val, Send):
            pending[val.node] = pending.get(val.node, 0) + 1
        elif ch.startswith("branch:to:"):
            pulled.add(ch.removeprefix("branch:to:"))
        elif ch == "joins" and isinstance(val, dict):
            run.join_wait.update(k for k, u in val.items() if isinstance(u, dict) and u.get("add"))
    for nid in pulled:
        pending[nid] = pending.get(nid, 0) + 1


async def _drive(run: Run, trigger_id: str, inp: Any) -> dict:
    t0 = time.perf_counter()
    status, error, waiting_for = "success", None, None
    config = {
        "configurable": {"thread_id": run.execution_id},
        # Node executions are limited by Run.exec_limit; LangGraph's step limit only needs to stay out of the way.
        "recursion_limit": run.exec_ceiling + 100,
    }
    run.publish({"type": "execution_started", "workflow_id": run.meta["workflow_id"], "mode": run.mode})
    graph = None
    _RUN.set(run)
    limit = _timeout(run.workflow)
    deadline = asyncio.timeout(limit)
    try:
        async with deadline:
            graph = compile_graph(run, trigger_id, run.workflow.get("_destination"))
            if run.saver is not None:
                config["configurable"][CONFIG_KEY_CHECKPOINTER] = run.saver  # lent to the cached graph
                if isinstance(inp, Command):
                    if run.topo.joins:
                        await _restore_progress(run, graph, config)
                else:
                    run.pending[trigger_id] = 1
                # "exit": persist state once, when the run finishes or pauses, not after every step.
                await graph.ainvoke(inp, config, durability="exit")
            else:
                run.pending[trigger_id] = 1
                await graph.ainvoke(inp, config)
            interrupts = []
            if run.saver is not None:
                snap = await graph.aget_state(config)
                interrupts = list(getattr(snap, "interrupts", ()) or [])
                if not interrupts:
                    for t in snap.tasks or ():
                        interrupts.extend(t.interrupts or ())
            if interrupts:
                status = "waiting"
                waiting_for = _jsonable(interrupts[0].value)
    except TimeoutError as e:
        if deadline.expired():
            status, error = "error", f"Execution timed out after {limit:g} s"
        else:
            status, error = "error", f"{type(e).__name__}: {e}"
            log.exception("execution %s failed", run.execution_id)
    except asyncio.CancelledError:
        status, error = "canceled", "Execution was canceled"
    except NodeError as e:
        status, error = "error", f"[{e.node_name}] {e}"
    except GraphRecursionError:
        status, error = "error", "Stopped after too many steps: does the workflow loop without an exit? (limit: MAX_NODE_EXECUTIONS)"
    except Exception as e:  # noqa: BLE001
        status, error = "error", f"{type(e).__name__}: {e}"
        log.exception("execution %s failed", run.execution_id)

    output = final_output(run)
    duration = int((time.perf_counter() - t0) * 1000)
    fields = {"status": status, "error": error, "node_runs": _jsonable(run.records), "output": output[:MAX_SAVED_ITEMS],
              "waiting_for": waiting_for}
    if status != "waiting":
        fields.update(finished_at=_now(), duration_ms=run.prior_ms + duration)
    await store.finished(uuid.UUID(run.execution_id), run.workflow, run.mode, fields, immediate=run.mode == "manual")

    if status != "waiting" and run.saver is not None:
        try:
            await run.saver.adelete_thread(run.execution_id)
        except Exception:  # noqa: BLE001
            pass

    result = {
        "execution_id": run.execution_id,
        "status": status,
        "error": error,
        "output": output,
        "waiting_for": waiting_for,
        "response": run.response,
    }
    run.publish({"type": "execution_finished", **result, "output": output[:MAX_SAVED_ITEMS]})

    err_wf = (run.workflow.get("settings") or {}).get("error_workflow_id")
    if status == "error" and err_wf and run.mode != "error":
        asyncio.get_running_loop().create_task(_run_error_workflow(err_wf, run, error))
    return result


async def _run_error_workflow(workflow_id: str, run: Run, error: str | None) -> None:
    try:
        async with SessionLocal() as s:
            wf = await s.get(Workflow, uuid.UUID(workflow_id))
            if not wf:
                return
            target = workflow_to_dict(wf, live=True)
        if target["owner_id"] != run.meta.get("owner_id"):
            log.warning("error workflow %s skipped: it belongs to another user", workflow_id)
            return
        failed = next((r for r in reversed(run.records) if r["status"] == "error"), {})
        await execute_workflow(
            target,
            trigger_type="trigger.error",
            items=[
                {
                    "execution": {"id": run.execution_id, "error": error, "last_node": failed.get("name")},
                    "workflow": {"id": run.meta["workflow_id"], "name": run.meta["workflow_name"]},
                }
            ],
            mode="error",
            wait=False,
        )
    except Exception:  # noqa: BLE001
        log.exception("error workflow failed")


async def cancel_execution(execution_id: str) -> bool:
    task = RUNNING.get(execution_id)
    if task:
        task.cancel()
        return True
    async with SessionLocal() as s:
        prev = (await s.execute(
            update(Execution).where(Execution.id == uuid.UUID(execution_id), Execution.status.in_(["waiting", "running"]))
            .values(status="canceled", finished_at=_now(), error="Canceled", waiting_for=None)
            .returning(Execution.id).execution_options(synchronize_session=False))).scalar_one_or_none()
        await s.commit()
    if prev is None:
        return False
    if _checkpointer is not None:  # a paused run's checkpoint is never resumed now
        try:
            await _checkpointer.adelete_thread(execution_id)
        except Exception:  # noqa: BLE001
            log.warning("couldn't delete the checkpoint of canceled execution %s", execution_id)
    return True


async def drain(grace: float = 10.0) -> None:
    """Process shutdown: give running executions `grace` seconds to finish, then cancel the rest so each
    records its outcome (canceled) before the execution writer's last flush, instead of being left 'running'."""
    tasks = list(RUNNING.values())
    if not tasks:
        return
    _, left = await asyncio.wait(tasks, timeout=grace)
    for t in left:
        t.cancel()
    if left:
        await asyncio.wait(left, timeout=5)


async def load_workflow(workflow_id: str, live: bool = False) -> dict:
    async with SessionLocal() as s:
        wf = await s.get(Workflow, uuid.UUID(str(workflow_id)))
        if not wf:
            raise ValueError(f"Workflow {workflow_id} not found")
        return workflow_to_dict(wf, live=live)


def output_text(items: list[dict]) -> str:
    """Best-effort extraction of a reply string (chat/voice/WhatsApp)."""
    if not items:
        return ""
    first = items[0]
    if isinstance(first.get("output"), str):  # an explicit reply, even empty (e.g. a Voice Call Action speaks its own message)
        return first["output"]
    for key in ("text", "response", "message", "answer", "content"):
        v = first.get(key)
        if isinstance(v, str) and v:
            return v
    return json.dumps(first, default=str)
