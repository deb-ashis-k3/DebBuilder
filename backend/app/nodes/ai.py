"""AI nodes built on LangChain + LangGraph.

Root nodes (Agent, LLM Chain, Classifier, Extractor) run in the flow and pull
their Chat Model / Memory / Tools from attached sub-nodes, like n8n's cluster
nodes.
"""

import ast
import json
import operator as op
import textwrap
from datetime import datetime, timezone
from typing import Any

import httpx
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import Field, create_model
from sqlalchemy import delete, select

from ..db import ChatMessage, SessionLocal
from .base import NodeContext, NodeError, NodeType, ai_input, prop, register

AI = dict(group="ai", color="#ec4899")
SUB_MODEL = dict(group="ai_model", color="#a855f7", inputs=[], outputs=[])
SUB_MEMORY = dict(group="ai_memory", color="#0ea5e9", inputs=[], outputs=[])
SUB_TOOL = dict(group="ai_tool", color="#22c55e", inputs=[], outputs=[])

DEFAULT_PROMPT = "{{ $json.message or $json.text or $json.chatInput or $json.query }}"


def message_text(msg: BaseMessage | Any) -> str:
    content = getattr(msg, "content", msg)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content
                       if not isinstance(b, dict) or b.get("type") in (None, "text"))
    return str(content)


# ---------------------------------------------------------------------------
# Chat model sub-nodes
# ---------------------------------------------------------------------------

MODEL_COMMON = [
    prop("temperature", "Temperature (empty = provider default)", "number", None),
    prop("max_tokens", "Max output tokens", "number", None),
]


def _opt(kwargs: dict, **extra) -> dict:
    return {**kwargs, **{k: v for k, v in extra.items() if v not in (None, "")}}


async def _openai(ctx: NodeContext):
    from langchain_openai import ChatOpenAI

    _, cred = await ctx.credential()
    p = ctx.params()
    return ChatOpenAI(**_opt(
        # Local OpenAI-compatible servers often need no key, but the client requires one.
        {"model": p["model"], "api_key": cred.get("api_key") or "not-needed", "streaming": True},
        base_url=cred.get("base_url"), organization=cred.get("organization"),
        temperature=p.get("temperature"), max_tokens=p.get("max_tokens"),
    ))


async def _anthropic(ctx: NodeContext):
    from langchain_anthropic import ChatAnthropic

    _, cred = await ctx.credential()
    p = ctx.params()
    return ChatAnthropic(**_opt(
        {"model": p["model"], "api_key": cred["api_key"], "streaming": True,
         "max_tokens": int(p.get("max_tokens") or 4096)},
        temperature=p.get("temperature"),
    ))


async def _gemini(ctx: NodeContext):
    from langchain_google_genai import ChatGoogleGenerativeAI

    _, cred = await ctx.credential()
    p = ctx.params()
    return ChatGoogleGenerativeAI(**_opt(
        {"model": p["model"], "google_api_key": cred["api_key"]},
        temperature=p.get("temperature"), max_output_tokens=p.get("max_tokens"),
    ))


async def _ollama(ctx: NodeContext):
    from langchain_ollama import ChatOllama

    _, cred = await ctx.credential()
    p = ctx.params()
    return ChatOllama(**_opt({"model": p["model"], "base_url": cred.get("base_url")},
                             temperature=p.get("temperature"), num_predict=p.get("max_tokens")))


for _type, _label, _cred, _default, _options, _fn in [
    ("model.openai", "OpenAI Chat Model", "openai", "gpt-5-mini",
     ["gpt-5.5", "gpt-5", "gpt-5-mini", "gpt-5-nano", "gpt-4.1", "gpt-4o-mini"], _openai),
    ("model.anthropic", "Anthropic Chat Model", "anthropic", "claude-sonnet-5-5",
     ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"], _anthropic),
    ("model.gemini", "Google Gemini Chat Model", "google_genai", "gemini-2.5-flash",
     ["gemini-3.1-pro", "gemini-2.5-pro", "gemini-2.5-flash"], _gemini),
    ("model.ollama", "Ollama Chat Model", "ollama", "llama3.2", ["llama3.2", "qwen3", "mistral"], _ollama),
]:
    register(NodeType(
        type=_type, label=_label, icon="brain", description=f"{_label} for AI nodes.",
        properties=[
            prop("credential", "Credential", "credential", None, credential_types=[_cred], required=True),
            prop("model", "Model", "combo", _default, options=_options, required=True),
            *MODEL_COMMON,
        ],
        provides="ai_model", supply=_fn, **SUB_MODEL,
    ))

# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


class PgMemory:
    def __init__(self, session_key: str, window: int):
        self.session_key = session_key
        self.window = window

    async def load(self) -> list[BaseMessage]:
        async with SessionLocal() as s:
            rows = (await s.execute(
                select(ChatMessage).where(ChatMessage.session_key == self.session_key)
                .order_by(ChatMessage.id.desc()).limit(self.window)
            )).scalars().all()
        msgs: list[BaseMessage] = []
        for r in reversed(rows):
            msgs.append(HumanMessage(r.content) if r.role == "human" else AIMessage(r.content))
        return msgs

    async def save(self, human: str, ai: str) -> None:
        async with SessionLocal() as s:
            s.add_all([ChatMessage(session_key=self.session_key, role="human", content=human),
                       ChatMessage(session_key=self.session_key, role="ai", content=ai)])
            await s.commit()

    async def clear(self) -> None:
        async with SessionLocal() as s:
            await s.execute(delete(ChatMessage).where(ChatMessage.session_key == self.session_key))
            await s.commit()


async def _memory(ctx: NodeContext):
    key = ctx.param("session_key") or "default"
    return PgMemory(f"{ctx.run.meta['workflow_id']}:{key}", int(ctx.param("window") or 20))


register(NodeType(
    type="memory.postgres", label="Postgres Chat Memory", icon="database",
    description="Stores conversation history per session in Postgres.",
    properties=[
        prop("session_key", "Session key", "string", "{{ $json.session_id or $json['from'] or 'default' }}"),
        prop("window", "Context window (messages)", "number", 20),
    ],
    provides="ai_memory", supply=_memory, **SUB_MEMORY,
))

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

_PY_TYPES = {"string": str, "number": float, "integer": int, "boolean": bool, "object": dict, "array": list}


def _args_model(name: str, fields: list[dict]):
    spec = {}
    for f in fields or []:
        if not f.get("name"):
            continue
        typ = _PY_TYPES.get(f.get("type", "string"), str)
        default = ... if f.get("required", True) else None
        spec[f["name"]] = (typ if default is ... else typ | None, Field(default, description=f.get("description", "")))
    if not spec:
        spec["query"] = (str, Field(..., description="Input for the tool"))
    return create_model(f"{name}_args", **spec)


def _tool_name(ctx: NodeContext) -> str:
    raw = ctx.param("tool_name") or ctx.name
    return "".join(c if c.isalnum() or c in "_-" else "_" for c in raw)[:64]


PARAM_FIELDS = [
    prop("name", "Name"),
    prop("type", "Type", "options", "string", options=list(_PY_TYPES)),
    prop("description", "Description"),
    prop("required", "Required", "boolean", True),
]
TOOL_COMMON = [
    prop("tool_name", "Tool name", "string", "", description="Defaults to the node name"),
    prop("tool_description", "Description (tells the AI when to use it)", "text", "", required=True),
]


async def _tool_http(ctx: NodeContext) -> BaseTool:
    from .core import apply_auth

    p = ctx.params()
    params = ctx.raw_params.get("parameters") or []

    async def call(**kwargs):
        url = p["url"]
        query, body = {}, {}
        for f in params:
            v = kwargs.get(f["name"])
            if v is None:
                continue
            loc = f.get("location", "query")
            if loc == "path":
                url = url.replace("{" + f["name"] + "}", str(v))
            elif loc == "body":
                body[f["name"]] = v
            else:
                query[f["name"]] = v
        headers: dict = {}
        auth, auth_params = await apply_auth(ctx, 0, headers)
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            r = await c.request(p.get("method", "GET"), url, params={**auth_params, **query} or None,
                                json=body or None, headers=headers, auth=auth)
        return r.text[:8000]

    return StructuredTool.from_function(coroutine=call, name=_tool_name(ctx), description=p["tool_description"],
                                        args_schema=_args_model(_tool_name(ctx), params))


register(NodeType(
    type="tool.http", label="HTTP Request Tool", icon="globe", description="Let the AI call an API.",
    properties=[
        *TOOL_COMMON,
        prop("method", "Method", "options", "GET", options=["GET", "POST", "PUT", "PATCH", "DELETE"]),
        prop("url", "URL (use {param} for path params)", "string", ""),
        prop("auth", "Authentication", "options", "none", options=["none", "credential"]),
        prop("credential", "Credential", "credential", None, show_if={"auth": ["credential"]}),
        prop("parameters", "Parameters the AI fills in", "collection", [], fields=[
            *PARAM_FIELDS, prop("location", "Location", "options", "query", options=["query", "path", "body"])]),
    ],
    provides="ai_tool", supply=_tool_http, **SUB_TOOL,
))


async def _tool_code(ctx: NodeContext) -> BaseTool:
    code = ctx.raw_params.get("code") or "return query"
    fields = ctx.raw_params.get("parameters") or []
    arg_names = [f["name"] for f in fields if f.get("name")] or ["query"]
    env: dict = {"json": json, "httpx": httpx, "datetime": datetime}
    src = f"async def __tool({', '.join(arg_names)}):\n" + textwrap.indent(code, "    ")
    exec(compile(src, f"<tool:{ctx.name}>", "exec"), env)  # noqa: S102 - user code by design

    async def call(**kwargs):
        r = await env["__tool"](**kwargs)
        return r if isinstance(r, str) else json.dumps(r, default=str)

    return StructuredTool.from_function(coroutine=call, name=_tool_name(ctx), description=ctx.param("tool_description"),
                                        args_schema=_args_model(_tool_name(ctx), fields))


register(NodeType(
    type="tool.code", label="Code Tool (Python)", icon="code", description="A Python function the AI can call.",
    properties=[
        *TOOL_COMMON,
        prop("parameters", "Arguments", "collection", [{"name": "query", "type": "string", "description": "input", "required": True}],
             fields=PARAM_FIELDS),
        prop("code", "Python (async body; args are variables)", "code", "return query.upper()", language="python", rows=10),
    ],
    provides="ai_tool", supply=_tool_code, **SUB_TOOL,
))


async def _tool_workflow(ctx: NodeContext) -> BaseTool:
    from .core import run_subworkflow

    wf_id = ctx.param("workflow_id")
    fields = ctx.raw_params.get("parameters") or []

    async def call(**kwargs):
        out = await run_subworkflow(ctx, wf_id, [kwargs])
        return json.dumps(out[:20], default=str)

    return StructuredTool.from_function(coroutine=call, name=_tool_name(ctx), description=ctx.param("tool_description"),
                                        args_schema=_args_model(_tool_name(ctx), fields))


register(NodeType(
    type="tool.workflow", label="Call Workflow Tool", icon="workflow",
    description="Expose another workflow as a tool. The AI's arguments become the sub-workflow's input item.",
    properties=[*TOOL_COMMON, prop("workflow_id", "Workflow", "workflow", None, required=True),
                prop("parameters", "Arguments", "collection", [], fields=PARAM_FIELDS)],
    provides="ai_tool", supply=_tool_workflow, **SUB_TOOL,
))

_BIN = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv, ast.Pow: op.pow,
        ast.Mod: op.mod, ast.FloorDiv: op.floordiv}


def _calc(node):
    import math

    if isinstance(node, ast.Expression):
        return _calc(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        return _BIN[type(node.op)](_calc(node.left), _calc(node.right))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_calc(node.operand)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and hasattr(math, node.func.id):
        return getattr(math, node.func.id)(*[_calc(a) for a in node.args])
    if isinstance(node, ast.Name) and node.id in ("pi", "e"):
        return getattr(math, node.id)
    raise ValueError("Unsupported expression")


async def _tool_calc(ctx: NodeContext) -> BaseTool:
    def calculator(expression: str) -> str:
        """Evaluate a math expression, e.g. '2 * (3 + 4) / sqrt(16)'."""
        try:
            return str(_calc(ast.parse(expression, mode="eval")))
        except Exception as e:  # noqa: BLE001
            return f"Error: {e}"

    return StructuredTool.from_function(calculator)


register(NodeType(
    type="tool.calculator", label="Calculator", icon="calculator", description="Math evaluation tool.",
    properties=[], provides="ai_tool", supply=_tool_calc, **SUB_TOOL,
))


async def _tool_time(ctx: NodeContext) -> BaseTool:
    def current_datetime() -> str:
        """Get the current date and time (UTC, ISO 8601)."""
        return datetime.now(timezone.utc).isoformat()

    return StructuredTool.from_function(current_datetime)


register(NodeType(
    type="tool.datetime", label="Date & Time Tool", icon="clock", description="Gives the AI the current time.",
    properties=[], provides="ai_tool", supply=_tool_time, **SUB_TOOL,
))


async def _tool_tavily(ctx: NodeContext) -> BaseTool:
    _, cred = await ctx.credential()
    max_results = int(ctx.param("max_results") or 5)

    async def web_search(query: str) -> str:
        """Search the web for up-to-date information."""
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post("https://api.tavily.com/search",
                             json={"api_key": cred["api_key"], "query": query, "max_results": max_results})
        r.raise_for_status()
        return json.dumps([{k: x.get(k) for k in ("title", "url", "content")} for x in r.json().get("results", [])])

    return StructuredTool.from_function(coroutine=web_search, name="web_search",
                                        description="Search the web for up-to-date information.")


register(NodeType(
    type="tool.tavily", label="Web Search (Tavily)", icon="search", description="Web search tool.",
    properties=[prop("credential", "Credential", "credential", None, credential_types=["tavily"], required=True),
                prop("max_results", "Max results", "number", 5)],
    provides="ai_tool", supply=_tool_tavily, **SUB_TOOL,
))


async def _tool_wikipedia(ctx: NodeContext) -> BaseTool:
    async def wikipedia(query: str) -> str:
        """Look up a topic on Wikipedia and return a summary."""
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "DebBuilder/0.1"}) as c:
            s = await c.get("https://en.wikipedia.org/w/api.php", params={
                "action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": 1})
            hits = s.json().get("query", {}).get("search", [])
            if not hits:
                return "No results"
            title = hits[0]["title"]
            r = await c.get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{title.replace(' ', '_')}")
            return f"{title}: {r.json().get('extract', '')}"

    return StructuredTool.from_function(coroutine=wikipedia, name="wikipedia",
                                        description="Look up a topic on Wikipedia and return a summary.")


register(NodeType(
    type="tool.wikipedia", label="Wikipedia", icon="book-open", description="Wikipedia lookup tool.",
    properties=[], provides="ai_tool", supply=_tool_wikipedia, **SUB_TOOL,
))


async def _tool_mcp(ctx: NodeContext) -> list[BaseTool]:
    from langchain_mcp_adapters.client import MultiServerMCPClient

    p = ctx.params()
    headers = {h["key"]: h.get("value", "") for h in p.get("headers") or [] if h.get("key")}
    conn: dict = {"url": p["url"], "transport": p.get("transport", "streamable_http")}
    if headers:
        conn["headers"] = headers
    tools = await MultiServerMCPClient({"server": conn}).get_tools()
    include = {t.strip() for t in (p.get("include") or "").split(",") if t.strip()}
    return [t for t in tools if not include or t.name in include]


register(NodeType(
    type="tool.mcp", label="MCP Client Tool", icon="plug",
    description="Use every tool from a remote MCP server (Streamable HTTP or SSE).",
    properties=[
        prop("url", "Server URL", "string", "", placeholder="https://example.com/mcp", required=True),
        prop("transport", "Transport", "options", "streamable_http", options=["streamable_http", "sse"]),
        prop("headers", "Headers", "collection", [], fields=[prop("key", "Name"), prop("value", "Value")]),
        prop("include", "Only these tools (comma separated, empty = all)", "string", ""),
    ],
    provides="ai_tool", supply=_tool_mcp, **SUB_TOOL,
))

# ---------------------------------------------------------------------------
# Agent core (shared by AI Agent node and Agent Tool sub-node)
# ---------------------------------------------------------------------------


async def collect_tools(ctx: NodeContext, i: int = 0) -> list[BaseTool]:
    tools: list[BaseTool] = []
    for t in await ctx.supply("ai_tool", i):
        tools.extend(t if isinstance(t, list) else [t])
    return tools


def _json_schema(fields: list[dict]) -> dict | None:
    props, required = {}, []
    for f in fields or []:
        if not f.get("name"):
            continue
        props[f["name"]] = {"type": f.get("type", "string"), "description": f.get("description", "")}
        if f.get("required", True):
            required.append(f["name"])
    if not props:
        return None
    return {"title": "Output", "type": "object", "properties": props, "required": required}


async def run_agent(ctx: NodeContext, i: int, prompt: str, system: str, *, max_iterations: int = 10,
                    output_fields: list | None = None, memory=None) -> dict:
    model = await ctx.require_model(i)
    tools = await collect_tools(ctx, i)
    schema = _json_schema(output_fields or [])
    agent = create_agent(model, tools, system_prompt=system or None, response_format=schema)
    history = await memory.load() if memory else []
    config: dict = {"recursion_limit": max_iterations * 2 + 1}
    if cbs := ctx.callbacks():  # empty for sub-agents: they inherit the parent's handlers
        config["callbacks"] = cbs
    result = await agent.ainvoke({"messages": [*history, HumanMessage(prompt)]}, config=config)
    messages = result["messages"]
    answer = message_text(messages[-1]) if messages else ""
    steps = []
    by_id = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    for m in messages:
        if isinstance(m, AIMessage):
            for tc in m.tool_calls or []:
                res = by_id.get(tc.get("id"))
                steps.append({"tool": tc["name"], "input": tc.get("args"),
                              "output": message_text(res)[:2000] if res else None})
    if memory:
        await memory.save(prompt, answer)
    out: dict = {"output": answer}
    if schema and result.get("structured_response") is not None:
        sr = result["structured_response"]
        out["output"] = sr if isinstance(sr, dict) else getattr(sr, "model_dump", lambda: sr)()
    if steps:
        out["steps"] = steps
    return out


async def _agent(ctx: NodeContext):
    out = []
    for i, item in enumerate(ctx.items or [{}]):
        p = ctx.params(i)
        prompt = p.get("prompt")
        if not prompt:
            raise NodeError("Prompt is empty. Map it from the input, e.g. {{ $json.message }}")
        memories = await ctx.supply("ai_memory", i)
        res = await run_agent(ctx, i, str(prompt), p.get("system_message") or "",
                              max_iterations=int(p.get("max_iterations") or 10),
                              output_fields=ctx.raw_params.get("output_fields") if p.get("structured") else None,
                              memory=memories[0] if memories else None)
        if not p.get("include_steps"):
            res.pop("steps", None)
        out.append({**({"input": item} if p.get("keep_input") else {}), **res})
    return {"main": out}


AGENT_PROPS = [
    prop("prompt", "Prompt (user message)", "text", DEFAULT_PROMPT, rows=3),
    prop("system_message", "System message", "text", "You are a helpful assistant.", rows=5),
    prop("max_iterations", "Max tool iterations", "number", 10),
    prop("structured", "Require structured output", "boolean", False),
    prop("output_fields", "Output fields", "collection", [], fields=PARAM_FIELDS, show_if={"structured": [True]}),
    prop("include_steps", "Include tool steps in output", "boolean", False),
    prop("keep_input", "Keep input item under 'input'", "boolean", False),
]

register(NodeType(
    type="ai.agent", label="AI Agent", icon="bot",
    description="Tool-calling agent (LangChain create_agent on LangGraph). Attach a model, optional memory and tools.",
    properties=AGENT_PROPS,
    ai_inputs=[ai_input("ai_model", True, 1), ai_input("ai_memory", max=1), ai_input("ai_tool")],
    execute=_agent, **AI,
))


async def _agent_tool(ctx: NodeContext) -> BaseTool:
    system = ctx.param("system_message") or ""
    max_iter = int(ctx.param("max_iterations") or 8)

    async def call(task: str) -> str:
        res = await run_agent(ctx, 0, task, system, max_iterations=max_iter)
        return res["output"] if isinstance(res["output"], str) else json.dumps(res["output"])

    return StructuredTool.from_function(
        coroutine=call, name=_tool_name(ctx), description=ctx.param("tool_description"),
        args_schema=create_model(f"{_tool_name(ctx)}_args", task=(str, Field(..., description="Task for the agent"))),
    )


register(NodeType(
    type="tool.agent", label="AI Agent Tool", icon="bot",
    description="A sub-agent the parent agent can delegate to (multi-agent). Has its own model and tools.",
    properties=[*TOOL_COMMON,
                prop("system_message", "System message", "text", "You are a specialist.", rows=5),
                prop("max_iterations", "Max tool iterations", "number", 8)],
    ai_inputs=[ai_input("ai_model", True, 1), ai_input("ai_tool")],
    provides="ai_tool", supply=_agent_tool, **SUB_TOOL,
))

# ---------------------------------------------------------------------------
# LLM chain / classifier / extractor / sentiment
# ---------------------------------------------------------------------------


async def _chain(ctx: NodeContext):
    out = []
    for i, item in enumerate(ctx.items or [{}]):
        p = ctx.params(i)
        model = await ctx.require_model(i)
        memories = await ctx.supply("ai_memory", i)
        memory = memories[0] if memories else None
        msgs: list[BaseMessage] = []
        if p.get("system_message"):
            msgs.append(SystemMessage(p["system_message"]))
        if memory:
            msgs.extend(await memory.load())
        msgs.append(HumanMessage(str(p.get("prompt") or "")))
        schema = _json_schema(ctx.raw_params.get("output_fields")) if p.get("structured") else None
        if schema:
            res = await model.with_structured_output(schema).ainvoke(msgs, config={"callbacks": ctx.callbacks()})
            answer: Any = res
        else:
            res = await model.ainvoke(msgs, config={"callbacks": ctx.callbacks()})
            answer = message_text(res)
        if memory:
            await memory.save(str(p.get("prompt")), answer if isinstance(answer, str) else json.dumps(answer))
        out.append({"output": answer})
    return {"main": out}


register(NodeType(
    type="ai.llm", label="Basic LLM Chain", icon="sparkles", description="Single prompt → completion (optionally structured JSON).",
    properties=[
        prop("prompt", "Prompt", "text", DEFAULT_PROMPT, rows=4),
        prop("system_message", "System message", "text", "", rows=3),
        prop("structured", "Structured output", "boolean", False),
        prop("output_fields", "Output fields", "collection", [], fields=PARAM_FIELDS, show_if={"structured": [True]}),
    ],
    ai_inputs=[ai_input("ai_model", True, 1), ai_input("ai_memory", max=1)],
    execute=_chain, **AI,
))


async def _classifier(ctx: NodeContext):
    cats = [c for c in ctx.raw_params.get("categories") or [] if c.get("name")]
    if not cats:
        raise NodeError("Add at least one category")
    names = [c["name"] for c in cats]
    fallback = ctx.raw_params.get("fallback", True)
    enum = names + (["other"] if fallback else [])
    schema = {"title": "Classification", "type": "object",
              "properties": {"category": {"type": "string", "enum": enum}}, "required": ["category"]}
    out: dict[str, list] = {f"out{k}": [] for k in range(len(cats))}
    out["fallback"] = []
    for i, item in enumerate(ctx.items):
        model = await ctx.require_model(i)
        text = ctx.param("text", i)
        listing = "\n".join(f"- {c['name']}: {c.get('description', '')}" for c in cats)
        sys = (ctx.param("system_message", i) or "Classify the text into exactly one category.") + \
            f"\nCategories:\n{listing}" + ("\n- other: none of the above" if fallback else "")
        res = await model.with_structured_output(schema).ainvoke([SystemMessage(sys), HumanMessage(str(text))], config={"callbacks": ctx.callbacks(stream=False)})
        cat = (res or {}).get("category")
        tagged = {**item, "category": cat}
        out[f"out{names.index(cat)}" if cat in names else "fallback"].append(tagged)
    return out


register(NodeType(
    type="ai.classifier", label="Text Classifier", icon="tags", description="Route items to an output per category using an LLM.",
    properties=[
        prop("text", "Text to classify", "text", DEFAULT_PROMPT, rows=2),
        prop("categories", "Categories (one output each)", "collection",
             [{"name": "support", "description": "Help with a problem"}, {"name": "sales", "description": "Buying intent"}],
             fields=[prop("name", "Category"), prop("description", "Description")]),
        prop("fallback", "'Other' output for no match", "boolean", True),
        prop("system_message", "System message", "text", "", rows=2),
    ],
    ai_inputs=[ai_input("ai_model", True, 1)], outputs=[], dynamic_outputs="categories",
    execute=_classifier, **AI,
))


async def _extractor(ctx: NodeContext):
    schema = _json_schema(ctx.raw_params.get("attributes"))
    if not schema:
        raise NodeError("Define at least one attribute")
    out = []
    for i, item in enumerate(ctx.items):
        model = await ctx.require_model(i)
        sys = ctx.param("system_message", i) or "Extract the requested attributes from the text. Use null when unknown."
        res = await model.with_structured_output(schema).ainvoke([SystemMessage(sys), HumanMessage(str(ctx.param("text", i)))], config={"callbacks": ctx.callbacks(stream=False)})
        out.append({"output": res} if ctx.raw_params.get("nest", True) else {**item, **(res or {})})
    return {"main": out}


register(NodeType(
    type="ai.extractor", label="Information Extractor", icon="scan-text", description="Pull structured fields out of free text.",
    properties=[
        prop("text", "Text", "text", DEFAULT_PROMPT, rows=2),
        prop("attributes", "Attributes", "collection",
             [{"name": "name", "type": "string", "description": "Person's name", "required": False}], fields=PARAM_FIELDS),
        prop("nest", "Put result under 'output'", "boolean", True),
        prop("system_message", "System message", "text", "", rows=2),
    ],
    ai_inputs=[ai_input("ai_model", True, 1)], execute=_extractor, **AI,
))


async def _sentiment(ctx: NodeContext):
    labels = [s.strip() for s in (ctx.param("labels") or "positive,neutral,negative").split(",") if s.strip()]
    schema = {"title": "Sentiment", "type": "object", "properties": {
        "sentiment": {"type": "string", "enum": labels}, "confidence": {"type": "number"}},
        "required": ["sentiment", "confidence"]}
    out: dict[str, list] = {lbl: [] for lbl in labels}
    for i, item in enumerate(ctx.items):
        model = await ctx.require_model(i)
        res = await model.with_structured_output(schema).ainvoke(
            [SystemMessage("Analyze the sentiment of the text."), HumanMessage(str(ctx.param("text", i)))],
            config={"callbacks": ctx.callbacks(stream=False)})
        lbl = res.get("sentiment") if res else labels[-1]
        out.setdefault(lbl, []).append({**item, "sentiment": res})
    return {"main": [x for v in out.values() for x in v]}


register(NodeType(
    type="ai.sentiment", label="Sentiment Analysis", icon="smile", description="Score sentiment with an LLM.",
    properties=[prop("text", "Text", "text", DEFAULT_PROMPT, rows=2),
                prop("labels", "Labels (comma separated)", "string", "positive,neutral,negative")],
    ai_inputs=[ai_input("ai_model", True, 1)], execute=_sentiment, **AI,
))
