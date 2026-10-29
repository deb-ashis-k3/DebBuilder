"""Compile a DebBuilder workflow (n8n-style nodes + edges) into a LangGraph
StateGraph and run it.

Semantics:
- Data flows as lists of JSON items. Each node returns {port: items}.
- After a node runs, every outgoing edge whose source port produced items
  dispatches a LangGraph `Send` to the target with exactly those items.
- Nodes flagged `defer` (Merge) wait until all other branches finish, then
  read every incoming edge from graph state.
- Cycles are allowed (loops); `recursion_limit` caps total supersteps.
- Nodes calling `interrupt()` pause the execution (status "waiting"); a later
  `resume` continues from the Postgres checkpoint.
- Sub-nodes (models, memory, tools) never run in the flow; their parent node
  instantiates them via `ctx.supply()`.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import operator
import time
import traceback
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Annotated, Any, TypedDict

from langchain_core.callbacks import AsyncCallbackHandler
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send

from ..config import get_settings
from ..db import Execution, SessionLocal, Workflow
from ..nodes import get_type
from ..nodes.base import NodeContext, NodeError
from .bus import bus

log = logging.getLogger("debbuilder.runtime")

MAX_SAVED_ITEMS = 200
MAX_EVENT_ITEMS = 50

_checkpointer = None
RUNNING: dict[str, asyncio.Task] = {}


def set_checkpointer(cp) -> None:
    global _checkpointer
    _checkpointer = cp


def _merge(a: dict, b: dict) -> dict:
    return {**(a or {}), **(b or {})}


class WFState(TypedDict, total=False):
    outputs: Annotated[dict, _merge]
    trigger_items: list
    steps: Annotated[int, operator.add]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _jsonable(v: Any) -> Any:
    try:
        return json.loads(json.dumps(v, default=str))
    except Exception:  # noqa: BLE001
        return str(v)


def _normalize_items(items: Any) -> list[dict]:
    if items is None:
        return []
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        items = [items]
    out = []
    for it in items:
        out.append(_jsonable(it) if isinstance(it, dict) else {"value": _jsonable(it)})
    return out


def _clip(v: Any, n: int = 4000) -> Any:
    s = v if isinstance(v, str) else json.dumps(_jsonable(v), default=str)
    return s if len(s) <= n else s[:n] + "…"


class TokenStreamer(AsyncCallbackHandler):
    """Streams LLM tokens to the execution channel and records every LLM / tool
    call as a 'sub-run' (n8n's AI logs view)."""

    def __init__(self, ctx: NodeContext, stream: bool = True):
        self.ctx = ctx
        self.stream = stream
        self.parents: dict[str, str | None] = {}
        self.open: dict[str, dict] = {}

    def _depth(self, run_id, parent_run_id) -> int:
        self.parents[str(run_id)] = str(parent_run_id) if parent_run_id else None
        depth, cur = 0, self.parents.get(str(run_id))
        while cur:
            if cur in self.open and self.open[cur]["type"] == "tool":
                depth += 1
            cur = self.parents.get(cur)
        return depth

    def _start(self, run_id, parent_run_id, entry: dict) -> None:
        entry.update(depth=self._depth(run_id, parent_run_id), started_at=_now().isoformat(), status="running",
                     _t0=time.perf_counter())
        self.open[str(run_id)] = entry
        self.ctx.sub_runs.append(entry)

    def _end(self, run_id, **fields) -> None:
        entry = self.open.get(str(run_id))
        if entry:
            entry.update(fields, duration_ms=int((time.perf_counter() - entry.pop("_t0", time.perf_counter())) * 1000))

    async def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kw) -> None:
        self.parents[str(run_id)] = str(parent_run_id) if parent_run_id else None

    async def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kw) -> None:
        params = kw.get("invocation_params") or {}
        model = params.get("model") or params.get("model_name") or (serialized or {}).get("kwargs", {}).get("model") or "LLM"
        msgs = [{"role": getattr(m, "type", "?"), "content": _clip(getattr(m, "content", m), 1500)} for m in (messages[0] if messages else [])]
        self._start(run_id, parent_run_id, {"type": "llm", "name": str(model), "input": msgs[-12:]})

    async def on_llm_new_token(self, token: str, **kwargs) -> None:
        if self.stream and isinstance(token, str) and token:
            self.ctx.publish({"type": "token", "token": token})

    async def on_llm_end(self, response, *, run_id, **kw) -> None:
        gen = response.generations[0][0] if response.generations and response.generations[0] else None
        msg = getattr(gen, "message", None)
        usage = getattr(msg, "usage_metadata", None) or (response.llm_output or {}).get("token_usage") or {}
        out: dict = {"text": _clip(getattr(gen, "text", "") or "")}
        if msg is not None and getattr(msg, "tool_calls", None):
            out["tool_calls"] = [{"name": t["name"], "args": t.get("args")} for t in msg.tool_calls]
        self._end(run_id, status="success", output=out, tokens={
            "input": usage.get("input_tokens") or usage.get("prompt_tokens"),
            "output": usage.get("output_tokens") or usage.get("completion_tokens"),
            "total": usage.get("total_tokens"),
        })

    async def on_llm_error(self, error, *, run_id, **kw) -> None:
        self._end(run_id, status="error", error=str(error))

    async def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, **kw) -> None:
        self._start(run_id, parent_run_id, {"type": "tool", "name": (serialized or {}).get("name", "tool"),
                                            "input": _clip(kw.get("inputs") or input_str, 2000)})

    async def on_tool_end(self, output, *, run_id, **kw) -> None:
        self._end(run_id, status="success", output=_clip(getattr(output, "content", output), 3000))

    async def on_tool_error(self, error, *, run_id, **kw) -> None:
        self._end(run_id, status="error", error=str(error))


class Run:
    """Per-execution runtime shared by every node of one workflow run."""

    def __init__(self, workflow: dict, execution_id: str, mode: str, records: list | None = None):
        self.workflow = workflow
        self.execution_id = execution_id
        self.mode = mode
        self.saver = None  # per-execution checkpointer (see _saver_for)
        self.nodes: dict[str, dict] = {n["id"]: n for n in workflow.get("nodes", [])}
        self.edges: list[dict] = workflow.get("edges", [])
        self.records: list[dict] = list(records or [])
        self.state_outputs: dict[str, dict] = {}
        self.response: dict | None = None  # set by "Respond to Webhook"
        self.allowed: set[str] | None = None  # partial execution ("Execute step")
        settings = workflow.get("settings") or {}
        self.meta = {
            "execution_id": execution_id,
            "mode": mode,
            "workflow_id": str(workflow.get("id")),
            "workflow_name": workflow.get("name"),
            "vars": {v["key"]: v.get("value") for v in settings.get("variables", []) if v.get("key")},
        }

    # ---- graph topology ----------------------------------------------------
    def is_flow_edge(self, e: dict) -> bool:
        return not (e.get("targetHandle") or "main").startswith("ai_")

    def flow_out(self, nid: str) -> list[dict]:
        return [e for e in self.edges if e["source"] == nid and self.is_flow_edge(e) and e["target"] in self.nodes]

    def flow_in(self, nid: str) -> list[dict]:
        return [e for e in self.edges if e["target"] == nid and self.is_flow_edge(e) and e["source"] in self.nodes]

    def sub_nodes(self, nid: str, handle: str) -> list[dict]:
        return [
            self.nodes[e["source"]]
            for e in self.edges
            if e["target"] == nid and e.get("targetHandle") == handle and e["source"] in self.nodes
        ]

    def outputs_by_name(self) -> dict[str, list[dict]]:
        out = {}
        for nid, ports in self.state_outputs.items():
            node = self.nodes.get(nid)
            if not node or not isinstance(ports, dict):
                continue
            items = ports.get("main")
            if items is None:
                items = next((v for v in ports.values() if v), [])
            out[node.get("name") or nid] = items
        return out

    def publish(self, event: dict) -> None:
        event = {"execution_id": self.execution_id, **event}
        bus.publish(self.execution_id, event)
        # The editor listens per workflow so it sees webhook/WhatsApp/voice runs live.
        bus.publish(f"wf:{self.meta['workflow_id']}", event)


# ---------------------------------------------------------------------------
# Graph compilation
# ---------------------------------------------------------------------------


def _reachable(run: Run, start: str) -> list[str]:
    seen, stack = [], [start]
    while stack:
        nid = stack.pop()
        if nid in seen:
            continue
        seen.append(nid)
        for e in run.flow_out(nid):
            stack.append(e["target"])
    return seen


# The execution being driven. Each execution runs in its own context (see _launch), so node
# functions look the run up here instead of closing over it, which lets compiled graphs be
# cached and shared between executions of the same workflow.
_RUN: contextvars.ContextVar[Run] = contextvars.ContextVar("debbuilder_run")
_GRAPHS: "OrderedDict[str, Any]" = OrderedDict()
_GRAPH_CACHE_SIZE = 256


def _make_node_fn(nid: str, is_entry: bool):
    async def fn(payload: dict) -> dict:
        run = _RUN.get()
        node = run.nodes[nid]
        nt = get_type(node["type"])
        run.state_outputs.update(payload.get("outputs") or {})
        inputs_by_handle: dict[str, list] = {}
        sources: list[dict] = []
        if "input" in payload:  # dispatched via Send
            items = payload["input"]
            handle = payload.get("input_handle", "main")
            inputs_by_handle[handle] = items
            if payload.get("from"):
                sources.append({"node_id": payload["from"], "port": payload.get("from_port", "main"), "handle": handle, "count": len(items)})
        elif is_entry:
            items = payload.get("trigger_items") or [{}]
            inputs_by_handle["main"] = items
        else:  # deferred join: gather every incoming edge from state
            items = []
            for e in run.flow_in(nid):
                got = (payload.get("outputs") or {}).get(e["source"], {}).get(e.get("sourceHandle") or "main") or []
                inputs_by_handle.setdefault(e.get("targetHandle") or "main", []).extend(got)
                items.extend(got)
                if got:
                    sources.append({"node_id": e["source"], "port": e.get("sourceHandle") or "main",
                                    "handle": e.get("targetHandle") or "main", "count": len(got)})
        return await _execute_node(run, node, nt, items, inputs_by_handle, sources)

    return fn


def _make_router(nid: str):
    def route(state: WFState):
        run = _RUN.get()
        out_edges = run.flow_out(nid)
        ports = (state.get("outputs") or {}).get(nid, {})
        targets: list = []
        deferred = set()
        for e in out_edges:
            items = ports.get(e.get("sourceHandle") or "main") or []
            if not items or (run.allowed is not None and e["target"] not in run.allowed):
                continue
            tgt = run.nodes[e["target"]]
            if tgt.get("disabled") is not True and get_type(tgt["type"]).defer:
                if e["target"] not in deferred:
                    deferred.add(e["target"])
                    targets.append(e["target"])
            else:
                targets.append(
                    Send(
                        e["target"],
                        {
                            "outputs": state.get("outputs") or {},
                            "input": items,
                            "input_handle": e.get("targetHandle") or "main",
                            "from": nid,
                            "from_port": e.get("sourceHandle") or "main",
                        },
                    )
                )
        return targets or END

    return route


def _ancestors(run: Run, target: str) -> set[str]:
    seen, stack = {target}, [target]
    while stack:
        for e in run.flow_in(stack.pop()):
            if e["source"] not in seen:
                seen.add(e["source"])
                stack.append(e["source"])
    return seen


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
    run.saver = _saver_for(run, nodes)

    # Compiling costs ~20ms of CPU; reuse graphs for identical workflow versions. Runs with a
    # checkpointer get their own (the saver is part of the compiled graph).
    key = None
    if run.saver is None:
        key = hashlib.sha1(json.dumps([run.workflow.get("nodes"), run.workflow.get("edges"), trigger_id, destination],
                                      sort_keys=True, default=str).encode()).hexdigest()
        cached = _GRAPHS.get(key)
        if cached is not None:
            _GRAPHS.move_to_end(key)
            return cached

    g = StateGraph(WFState)
    for nid in nodes:
        nt = get_type(run.nodes[nid]["type"])
        g.add_node(nid, _make_node_fn(nid, nid == trigger_id), defer=nt.defer and nid != trigger_id)
    g.add_edge(START, trigger_id)
    for nid in nodes:
        if nid != destination and any(run.allowed is None or e["target"] in run.allowed for e in run.flow_out(nid)):
            g.add_conditional_edges(nid, _make_router(nid))
        else:
            g.add_edge(nid, END)
    graph = g.compile(checkpointer=run.saver)
    if key:
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
    if _checkpointer is None or not any(get_type(run.nodes[n]["type"]).interrupts for n in nodes):
        return None
    return type(_checkpointer)(_checkpointer.conn)


# ---------------------------------------------------------------------------
# Node execution
# ---------------------------------------------------------------------------


def _trim(ports: dict, limit: int) -> dict:
    return {p: items[:limit] for p, items in ports.items()}


async def _execute_node(run: Run, node: dict, nt, items: list, inputs_by_handle: dict,
                        sources: list | None = None) -> dict:
    nid = node["id"]
    settings = node.get("settings") or {}
    started = time.perf_counter()
    started_at = _now().isoformat()
    record = {
        "node_id": nid,
        "name": node.get("name"),
        "type": node["type"],
        "status": "running",
        "started_at": started_at,
        "input": items[:MAX_SAVED_ITEMS],
        "input_count": len(items),
        "sources": sources or [],
        "run_index": sum(1 for r in run.records if r["node_id"] == nid),
    }
    run.publish({"type": "node_started", "node_id": nid, "name": node.get("name"), "started_at": started_at,
                 "run_index": record["run_index"]})
    ctx = NodeContext(run, node, nt, items, inputs_by_handle)

    on_error = settings.get("on_error", "stop")  # stop | continue | error_output
    tries = max(1, int(settings.get("max_tries", 3))) if settings.get("retry_on_fail") else 1
    wait_ms = int(settings.get("wait_between_tries_ms", 1000))

    outputs: dict[str, list] = {}
    error: str | None = None
    pin = node.get("pin_data")
    if node.get("disabled"):
        outputs = {"main": items}
    elif pin is not None and run.mode == "manual":
        # n8n semantics: pinned output replaces execution in editor (manual) runs.
        outputs = pin if isinstance(pin, dict) else {"main": pin}
        record["pinned"] = True
    else:
        for attempt in range(tries):
            try:
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
                          finished_at=_now().isoformat(), sub_runs=_jsonable(ctx.sub_runs))
            _upsert_record(run, record)
            run.publish({"type": "node_finished", "node_id": nid, "status": "error", "error": error,
                         "duration_ms": record["duration_ms"], "finished_at": record["finished_at"]})
            raise NodeError(error, node.get("name"))

    outputs = {port: _normalize_items(v) for port, v in outputs.items()}
    if settings.get("always_output_data") and not any(outputs.values()):
        outputs["main"] = [{}]
    record.update(
        status="error" if error else "success",
        error=error,
        output=_trim(outputs, MAX_SAVED_ITEMS),
        item_counts={p: len(v) for p, v in outputs.items()},
        duration_ms=int((time.perf_counter() - started) * 1000),
        finished_at=_now().isoformat(),
    )
    if ctx.sub_runs:
        record["sub_runs"] = _jsonable(ctx.sub_runs)
    _upsert_record(run, record)
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
    return {"outputs": {nid: outputs}, "steps": 1}


def _upsert_record(run: Run, record: dict) -> None:
    # A resumed node re-runs from the top; replace its "waiting" record.
    for i in range(len(run.records) - 1, -1, -1):
        r = run.records[i]
        if r["node_id"] == record["node_id"] and r["status"] in ("running", "waiting"):
            run.records[i] = record
            return
    if record not in run.records:
        run.records.append(record)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def workflow_to_dict(wf: Workflow) -> dict:
    return {
        "id": str(wf.id),
        "name": wf.name,
        "nodes": wf.nodes or [],
        "edges": wf.edges or [],
        "settings": wf.settings or {},
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
    for r in reversed(run.records):
        if r.get("status") == "success" and get_type(r["type"]).provides is None:
            out = r.get("output") or {}
            return out.get("main") or next((v for v in out.values() if v), [])
    return []


def _trigger_for(workflow: dict, node_id: str) -> str | None:
    """Pick the trigger that leads to `node_id` (preferring manual triggers)."""
    run = Run(workflow, "probe", "manual")
    hits = [n for n in workflow.get("nodes", []) if get_type(n["type"]).trigger and not n.get("disabled")
            and node_id in _reachable(run, n["id"])]
    hits.sort(key=lambda n: n["type"] != "trigger.manual")
    return hits[0]["id"] if hits else None


async def execute_workflow(
    workflow: dict,
    *,
    trigger_node_id: str | None = None,
    trigger_type: str | None = None,
    items: list[dict] | None = None,
    mode: str = "manual",
    wait: bool = True,
    destination_node_id: str | None = None,
) -> dict:
    if destination_node_id:
        # Stored in the snapshot so a resumed execution compiles the same partial graph.
        workflow = {**workflow, "_destination": destination_node_id}
        if not trigger_node_id and not trigger_type:
            trigger_node_id = _trigger_for(workflow, destination_node_id)
    trigger = find_trigger(workflow, trigger_type, trigger_node_id)
    if not trigger:
        raise ValueError("Workflow has no trigger node" + (f" of type {trigger_type}" if trigger_type else ""))

    async with SessionLocal() as s:
        ex = Execution(
            workflow_id=uuid.UUID(workflow["id"]),
            status="running",
            mode=mode,
            trigger_node=trigger["id"],
            input=_jsonable(items or []),
            workflow_snapshot=_jsonable(workflow),
        )
        s.add(ex)
        await s.commit()
        exec_id = str(ex.id)

    run = Run(workflow, exec_id, mode)
    inp = {"outputs": {}, "trigger_items": _normalize_items(items) or [{}], "steps": 0}
    return await _launch(run, trigger["id"], inp, wait)


async def resume_execution(execution_id: str, value: Any, wait: bool = False) -> dict:
    async with SessionLocal() as s:
        ex = await s.get(Execution, uuid.UUID(execution_id))
        if not ex or ex.status != "waiting":
            raise ValueError("Execution is not waiting for input")
        ex.status, ex.waiting_for = "running", None
        await s.commit()
        workflow, trigger_id, records, mode = ex.workflow_snapshot, ex.trigger_node, ex.node_runs, ex.mode
    run = Run(workflow, execution_id, mode, records)
    return await _launch(run, trigger_id, Command(resume=value), wait)


_slots: asyncio.Semaphore | None = None


async def _drive_limited(run: Run, trigger_id: str, inp: Any) -> dict:
    """At most `max_concurrent_executions` run at once; the rest queue here. Without this a
    burst of async webhooks started hundreds of executions that exhausted the DB pool.
    Sub-workflows skip the queue: their parent already holds a slot and waits for them."""
    global _slots
    if run.mode == "subworkflow":
        return await _drive(run, trigger_id, inp)
    if _slots is None:
        _slots = asyncio.Semaphore(max(1, get_settings().max_concurrent_executions))
    async with _slots:
        return await _drive(run, trigger_id, inp)


async def _launch(run: Run, trigger_id: str, inp: Any, wait: bool) -> dict:
    # Fresh context so a sub-workflow doesn't inherit the parent's LangGraph config.
    task = asyncio.get_running_loop().create_task(_drive_limited(run, trigger_id, inp), context=contextvars.Context())
    RUNNING[run.execution_id] = task
    task.add_done_callback(lambda _: RUNNING.pop(run.execution_id, None))
    if not wait:
        return {"execution_id": run.execution_id, "status": "running"}
    return await asyncio.shield(task)


async def _drive(run: Run, trigger_id: str, inp: Any) -> dict:
    t0 = time.perf_counter()
    status, error, waiting_for = "success", None, None
    config = {
        "configurable": {"thread_id": run.execution_id},
        "recursion_limit": get_settings().max_node_executions,
    }
    run.publish({"type": "execution_started", "workflow_id": run.meta["workflow_id"], "mode": run.mode})
    graph = None
    _RUN.set(run)
    try:
        graph = compile_graph(run, trigger_id, run.workflow.get("_destination"))
        if run.saver is not None:
            # "exit": persist state once, when the run finishes or pauses, not after every step.
            await graph.ainvoke(inp, config, durability="exit")
        else:
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
    except asyncio.CancelledError:
        status, error = "canceled", "Execution was canceled"
    except NodeError as e:
        status, error = "error", f"[{e.node_name}] {e}"
    except Exception as e:  # noqa: BLE001
        status, error = "error", f"{type(e).__name__}: {e}"
        log.exception("execution %s failed", run.execution_id)

    output = final_output(run)
    duration = int((time.perf_counter() - t0) * 1000)
    async with SessionLocal() as s:
        ex = await s.get(Execution, uuid.UUID(run.execution_id))
        if ex:
            ex.status = status
            ex.error = error
            ex.node_runs = _jsonable(run.records)
            ex.output = output
            ex.waiting_for = waiting_for
            if status != "waiting":
                ex.finished_at = _now()
                ex.duration_ms = (ex.duration_ms or 0) + duration
            await s.commit()

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
    run.publish({"type": "execution_finished", **result})

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
            target = workflow_to_dict(wf)
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
        ex = await s.get(Execution, uuid.UUID(execution_id))
        if ex and ex.status in ("waiting", "running"):
            ex.status, ex.finished_at, ex.error = "canceled", _now(), "Canceled"
            await s.commit()
            return True
    return False


async def load_workflow(workflow_id: str) -> dict:
    async with SessionLocal() as s:
        wf = await s.get(Workflow, uuid.UUID(str(workflow_id)))
        if not wf:
            raise ValueError(f"Workflow {workflow_id} not found")
        return workflow_to_dict(wf)


def output_text(items: list[dict]) -> str:
    """Best-effort extraction of a reply string (chat/voice/WhatsApp)."""
    if not items:
        return ""
    first = items[0]
    for key in ("output", "text", "response", "message", "answer", "content"):
        v = first.get(key)
        if isinstance(v, str) and v:
            return v
    return json.dumps(first, default=str)
