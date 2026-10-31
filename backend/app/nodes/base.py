"""Node type registry and the runtime context passed to every node."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from ..credentials import load_credential
from ..db import SessionLocal
from ..engine.expressions import build_context, evaluate

if TYPE_CHECKING:
    from ..engine.runtime import Run

Items = list[dict]
Outputs = dict[str, Items]

# Handle types for cluster (sub-)nodes, mirroring n8n's ai_* connections.
AI_HANDLES = {
    "ai_model": "Model",
    "ai_memory": "Memory",
    "ai_tool": "Tool",
    "ai_embedding": "Embedding",
    "ai_document": "Document",
    "ai_text_splitter": "Text Splitter",
    "ai_vector_store": "Vector Store",
    "ai_retriever": "Retriever",
    "ai_reranker": "Reranker",
}


def prop(
    name: str,
    label: str,
    type: str = "string",
    default: Any = None,
    *,
    description: str = "",
    placeholder: str = "",
    options: list | None = None,
    required: bool = False,
    show_if: dict | None = None,
    credential_types: list[str] | None = None,
    fields: list[dict] | None = None,
    rows: int | None = None,
    language: str | None = None,
    load_options: str | None = None,
) -> dict:
    """Declarative property spec; the UI builds forms from these."""
    if options and not isinstance(options[0], dict):
        options = [{"label": str(o), "value": o} for o in options]
    return {
        k: v
        for k, v in {
            "name": name,
            "label": label,
            "type": type,
            "default": default,
            "description": description,
            "placeholder": placeholder,
            "options": options,
            "required": required,
            "show_if": show_if,
            "credential_types": credential_types,
            "fields": fields,
            "rows": rows,
            "language": language,
            # "models": the UI fetches choices from the provider with the node's credential
            # (GET /api/credentials/{id}/models); `options` stay as the offline fallback.
            "load_options": load_options,
        }.items()
        if v not in (None, "", False) or k in ("name", "type", "default")
    }


@dataclass
class NodeType:
    type: str
    label: str
    group: str
    description: str
    icon: str = "box"
    color: str = "#64748b"
    properties: list[dict] = field(default_factory=list)
    # main inputs (0 for triggers / sub-nodes); sub-node handles accepted.
    inputs: list[str] = field(default_factory=lambda: ["main"])
    ai_inputs: list[dict] = field(default_factory=list)
    outputs: list[str] = field(default_factory=lambda: ["main"])
    # Property name whose list entries each become an output port.
    dynamic_outputs: str | None = None
    trigger: bool = False
    # Long-running listener trigger (Kafka, Redis, …) started while the workflow is active.
    listener: bool = False
    # Calls interrupt() to pause the run (Human Approval): the execution needs a checkpointer.
    interrupts: bool = False
    # For sub-nodes: which ai_* handle they provide.
    provides: str | None = None
    defer: bool = False
    execute: Callable[[NodeContext], Awaitable[Outputs]] | None = None
    supply: Callable[[NodeContext], Awaitable[Any]] | None = None

    def schema(self) -> dict:
        return {
            "type": self.type,
            "label": self.label,
            "group": self.group,
            "description": self.description,
            "icon": self.icon,
            "color": self.color,
            "properties": self.properties,
            "inputs": self.inputs,
            "ai_inputs": self.ai_inputs,
            "outputs": self.outputs,
            "dynamic_outputs": self.dynamic_outputs,
            "trigger": self.trigger,
            "listener": self.listener,
            "provides": self.provides,
        }


REGISTRY: dict[str, NodeType] = {}


def register(nt: NodeType) -> NodeType:
    REGISTRY[nt.type] = nt
    return nt


def ai_input(handle: str, required: bool = False, max: int | None = None) -> dict:
    return {"name": handle, "label": AI_HANDLES[handle], "required": required, "max": max}


class NodeError(Exception):
    def __init__(self, message: str, node_name: str | None = None):
        super().__init__(message)
        self.node_name = node_name


def _has_expression(value: Any) -> bool:
    if isinstance(value, str):
        return "{{" in value
    if isinstance(value, dict):
        return any(_has_expression(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_expression(v) for v in value)
    return False


@dataclass
class NodeContext:
    run: Run
    node: dict
    node_type: NodeType
    items: Items
    inputs_by_handle: dict[str, Items] = field(default_factory=dict)
    # For sub-nodes: the parent's item used to resolve expressions.
    parent_item: dict | None = None
    parent_items: Items | None = None
    # LLM / tool / memory calls made while this node ran: the log tree under the node (n8n's AI logs).
    sub_runs: list = field(default_factory=list)
    # The top-level node a sub-node runs under (whose run record holds the sub-runs); None for that node.
    root: "NodeContext | None" = None
    streamer: Any = None  # the root's TokenStreamer, created on first use
    # Graph state (engine/state.py) as this node received it, and the reducer updates it makes.
    graph_state: dict = field(default_factory=dict)
    state_updates: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.node.get("name") or self.node["id"]

    @property
    def raw_params(self) -> dict:
        params = {p["name"]: p.get("default") for p in self.node_type.properties}
        params.update(self.node.get("parameters") or {})
        return params

    def expr_context(self, i: int = 0) -> dict:
        items = self.parent_items if self.parent_items is not None else self.items
        item = self.parent_item if self.parent_item is not None else (items[i] if i < len(items) else {})
        return build_context(item, items, i, self.run.outputs_by_name(), self.run.meta, state=self.state)

    def param(self, name: str, i: int = 0, default: Any = None) -> Any:
        raw = self.raw_params.get(name, default)
        if raw is None:
            return default
        return evaluate(raw, self.expr_context(i))

    def params(self, i: int = 0) -> dict:
        raw = self.raw_params
        # No {{ }} anywhere: the values are the same for every item, so evaluate once (a node handling
        # thousands of items otherwise rebuilt the expression context per item for nothing).
        static = getattr(self, "_static_params", None)
        if static is None and not _has_expression(raw):
            ctx = self.expr_context(0)
            static = self._static_params = {k: evaluate(v, ctx) for k, v in raw.items()}
        if static is not None:
            return dict(static)
        ctx = self.expr_context(i)
        return {k: evaluate(v, ctx) for k, v in raw.items()}

    def eval(self, value: Any, i: int = 0) -> Any:
        return evaluate(value, self.expr_context(i))

    async def credential(self, param: str = "credential", i: int = 0) -> tuple[str, dict]:
        async with SessionLocal() as s:
            try:
                return await load_credential(s, self.param(param, i), self.run.meta.get("owner_id"))
            except ValueError as e:
                # Name the node that needs fixing (often a sub-node, not the one that ran).
                raise NodeError(f"'{self.name}': {e}", self.name) from e

    # ---- graph state ($state) ----------------------------------------------
    @property
    def state(self) -> dict:
        """The graph state this node sees (internal keys hidden). Writes made by this node show up
        for the nodes after it, like LangGraph: a node reads the state as of its own start."""
        from ..engine.state import public

        return public(self.graph_state)

    def set_state(self, key: str, value: Any, reducer: str | None = None) -> None:
        """Write a state key. `reducer` defaults to the key's reducer from Settings → State
        (replace for keys not declared there): replace | append | add | merge | messages."""
        from ..engine.state import REDUCERS, combine, schema

        key = str(key).strip()
        if not key:
            raise NodeError("State key is empty", self.name)
        op = reducer or schema(self.run.workflow.get("settings")).get(key, {}).get("reducer", "replace")
        if op not in REDUCERS:
            raise NodeError(f"Unknown reducer '{op}' (use {', '.join(REDUCERS)})", self.name)
        target = self.root or self  # sub-nodes write through their AI node
        target.state_updates[key] = combine(target.state_updates.get(key), op, value)

    # ---- files (binary data, see app/binary.py) ------------------------------
    def put_file(self, data: bytes, file_name: str = "file", mime_type: str | None = None) -> dict:
        """Store bytes for this workflow's workspace; returns the reference to put in item["binary"]."""
        from .. import binary

        try:
            return binary.put(self.run.meta.get("owner_id"), data, file_name, mime_type)
        except ValueError as e:
            raise NodeError(str(e), self.name) from e

    def file(self, item: dict, prop: str = "data") -> tuple[bytes, dict]:
        """(bytes, reference) of the file in `item["binary"][prop]`."""
        from .. import binary

        ref = ((item or {}).get("binary") or {}).get(prop or "data")
        if not ref:
            have = ", ".join((item or {}).get("binary") or {}) or "none"
            raise NodeError(f"No file in binary property '{prop}' (this item has: {have})", self.name)
        try:
            return binary.get(self.run.meta.get("owner_id"), ref), ref
        except ValueError as e:
            raise NodeError(str(e), self.name) from e

    def publish(self, event: dict) -> None:
        self.run.publish({"node_id": self.node["id"], **event})

    def log(self, message: str) -> None:
        self.publish({"type": "log", "message": message})

    # ---- cluster sub-nodes -------------------------------------------------
    def sub_nodes(self, handle: str) -> list[dict]:
        return self.run.sub_nodes(self.node["id"], handle)

    async def supply(self, handle: str, i: int = 0) -> list[Any]:
        """Instantiate attached sub-nodes (models, memory, tools)."""
        out = []
        items = self.parent_items if self.parent_items is not None else self.items
        item = self.parent_item if self.parent_item is not None else (items[i] if i < len(items) else {})
        for sub in self.sub_nodes(handle):
            if sub.get("disabled"):
                continue
            from . import get_type

            st = get_type(sub["type"])
            if not st.supply:
                continue
            sub_ctx = NodeContext(self.run, sub, st, items, parent_item=item, parent_items=items, root=self.root or self,
                                  graph_state=self.graph_state)
            obj = await st.supply(sub_ctx)
            for o in obj if isinstance(obj, list) else [obj]:
                _tag(o, sub)
            out.append(obj)
        return out

    async def require_model(self, i: int = 0):
        models = await self.supply("ai_model", i)
        if not models:
            raise NodeError("Connect a Chat Model sub-node to the 'Model' input", self.name)
        return models[0]

    def callbacks(self, stream: bool = True) -> list:
        # Sub-nodes (e.g. an Agent Tool) run inside their parent's callbacks already.
        if self.parent_item is not None:
            return []
        st = self._streamer()
        st.stream = stream
        return [st]

    def _streamer(self):
        from ..engine.runtime import TokenStreamer

        root = self.root or self
        if root.streamer is None:
            root.streamer = TokenStreamer(root)
        return root.streamer

    def start_sub_run(self, obj: Any, kind: str, action: str, input: Any) -> dict:
        """Log a step LangChain doesn't report (e.g. memory load/save) under this node in the log tree.
        `obj` is the sub-node object (tagged by supply) the step belongs to."""
        node = getattr(obj, "db_node", None) or {}
        return self._streamer().manual_start({"type": kind, "action": action, "input": input, **node},
                                             inside_node=self.node["id"] if self.root else None)

    def end_sub_run(self, entry: dict, **fields) -> None:
        self._streamer().manual_end(entry, **fields)


def _tag(obj: Any, node: dict) -> None:
    """Remember which canvas node a supplied model / tool / memory came from. LangChain passes a
    model's or tool's `metadata` to every callback, so the log can name the node that ran."""
    from langchain_core.runnables import Runnable

    info = {"db_node_id": node["id"], "db_node_name": node.get("name") or node["id"], "db_node_type": node["type"]}
    try:
        if isinstance(obj, Runnable) and hasattr(obj, "metadata"):  # LangChain chat model / tool / retriever
            obj.metadata = {**(obj.metadata or {}), **info}
        obj.db_node = info
    except (AttributeError, TypeError, ValueError):
        pass  # objects that refuse attributes just aren't named in the log


def new_id() -> str:
    return "n_" + uuid.uuid4().hex[:10]
