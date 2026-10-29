"""Starter workflows. Credentials are left empty; pick them in the editor."""


def n(id, type, name, x, y, **params):
    return {"id": id, "type": type, "name": name, "position": {"x": x, "y": y}, "parameters": params}


def e(src, tgt, sh="main", th="main"):
    return {"id": f"e_{src}_{sh}_{tgt}_{th}", "source": src, "sourceHandle": sh, "target": tgt, "targetHandle": th}


MODEL = dict(model="gpt-5-mini")

TEMPLATES = {
    "ai-chat-agent": {
        "name": "AI Chat Agent with Memory & Tools",
        "description": "Chat trigger → LangGraph tool-calling agent with Postgres memory, calculator, clock and Wikipedia.",
        "tags": ["ai", "chat"],
        "nodes": [
            n("chat", "trigger.chat", "When chat message received", 0, 0),
            n("agent", "ai.agent", "AI Agent", 320, 0, system_message="You are a helpful assistant. Use tools when useful."),
            n("model", "model.openai", "OpenAI Chat Model", 200, 220, **MODEL),
            n("mem", "memory.postgres", "Postgres Memory", 360, 220),
            n("calc", "tool.calculator", "Calculator", 520, 220),
            n("clock", "tool.datetime", "Clock", 640, 220),
            n("wiki", "tool.wikipedia", "Wikipedia", 760, 220),
        ],
        "edges": [
            e("chat", "agent"),
            e("model", "agent", "ai_model", "ai_model"),
            e("mem", "agent", "ai_memory", "ai_memory"),
            e("calc", "agent", "ai_tool", "ai_tool"),
            e("clock", "agent", "ai_tool", "ai_tool"),
            e("wiki", "agent", "ai_tool", "ai_tool"),
        ],
    },
    "whatsapp-ai-assistant": {
        "name": "WhatsApp AI Assistant",
        "description": "Incoming WhatsApp message → mark read + typing → AI agent with per-number memory → reply.",
        "tags": ["whatsapp", "ai"],
        "nodes": [
            n("wa", "trigger.whatsapp", "WhatsApp Trigger", 0, 0),
            n("only_text", "flow.filter", "Text messages only", 260, 0,
              conditions=[{"left": "{{ $json.text }}", "operator": "is_not_empty", "right": ""}]),
            n("read", "whatsapp.action", "Mark read", 520, -120, operation="mark_read"),
            n("agent", "ai.agent", "AI Agent", 520, 60,
              system_message="You are a friendly WhatsApp assistant for our business. Keep replies short."),
            n("model", "model.openai", "OpenAI Chat Model", 420, 280, **MODEL),
            n("mem", "memory.postgres", "Memory (per phone)", 600, 280, session_key="{{ $json['from'] }}"),
            n("reply", "whatsapp.action", "Send reply", 820, 60, operation="text",
              to="{{ $('WhatsApp Trigger').json['from'] }}", text="{{ $json.output }}"),
        ],
        "edges": [
            e("wa", "only_text"), e("only_text", "read"), e("only_text", "agent"), e("agent", "reply"),
            e("model", "agent", "ai_model", "ai_model"), e("mem", "agent", "ai_memory", "ai_memory"),
        ],
    },
    "voice-receptionist": {
        "name": "Voice Receptionist (LiveKit)",
        "description": "Real-time voice agent: LiveKit handles STT/TTS/VAD, each turn runs an AI agent with memory.",
        "tags": ["voice", "livekit", "ai"],
        "nodes": [
            n("voice", "trigger.voice", "Voice Agent", 0, 0, greeting="Hi, thanks for calling Acme. How can I help?"),
            n("agent", "ai.agent", "Receptionist", 320, 0,
              system_message="You are a concise, warm phone receptionist for Acme Dental. Speak in short sentences; no markdown or lists.",
              prompt="{{ $json.text }}"),
            n("model", "model.openai", "OpenAI Chat Model", 220, 220, **MODEL),
            n("mem", "memory.postgres", "Call memory", 400, 220, session_key="{{ $json.session_id }}"),
            n("clock", "tool.datetime", "Clock", 560, 220),
        ],
        "edges": [
            e("voice", "agent"),
            e("model", "agent", "ai_model", "ai_model"), e("mem", "agent", "ai_memory", "ai_memory"),
            e("clock", "agent", "ai_tool", "ai_tool"),
        ],
    },
    "webhook-api": {
        "name": "Webhook → Branch → Respond",
        "description": "POST /webhook/orders, route by amount, call an API and respond with JSON.",
        "tags": ["webhook", "http"],
        "nodes": [
            n("hook", "trigger.webhook", "Webhook", 0, 0, path="orders", method="POST", respond="respond_node"),
            n("if", "flow.if", "Big order?", 260, 0,
              conditions=[{"left": "{{ $json.body.amount }}", "operator": "gt", "right": "100"}]),
            n("vip", "core.set", "Mark VIP", 520, -100,
              fields=[{"name": "tier", "value": "vip", "type": "string"},
                      {"name": "amount", "value": "{{ $json.body.amount }}", "type": "number"}],
              include_other_fields=False),
            n("std", "core.set", "Mark standard", 520, 100,
              fields=[{"name": "tier", "value": "standard", "type": "string"},
                      {"name": "amount", "value": "{{ $json.body.amount }}", "type": "number"}],
              include_other_fields=False),
            n("respond", "core.respond_webhook", "Respond", 800, 0, respond_with="first_item"),
        ],
        "edges": [e("hook", "if"), e("if", "vip", "true"), e("if", "std", "false"), e("vip", "respond"), e("std", "respond")],
    },
    "ticket-triage": {
        "name": "Support Ticket Triage",
        "description": "Hosted form → LLM classifier routes to billing / technical / other branches.",
        "tags": ["ai", "form"],
        "nodes": [
            n("form", "trigger.form", "Support form", 0, 0, title="Contact support", fields=[
                {"name": "email", "label": "Email", "type": "email", "required": True},
                {"name": "message", "label": "How can we help?", "type": "textarea", "required": True}]),
            n("cls", "ai.classifier", "Classify", 280, 0, text="{{ $json.message }}", categories=[
                {"name": "billing", "description": "Payments, invoices, refunds"},
                {"name": "technical", "description": "Bugs, errors, how-to"}]),
            n("model", "model.openai", "OpenAI Chat Model", 280, 220, **MODEL),
            n("billing", "core.set", "Billing queue", 580, -120, fields=[{"name": "queue", "value": "billing"}]),
            n("tech", "core.set", "Tech queue", 580, 0, fields=[{"name": "queue", "value": "technical"}]),
            n("other", "core.set", "General queue", 580, 120, fields=[{"name": "queue", "value": "general"}]),
        ],
        "edges": [e("form", "cls"), e("cls", "billing", "out0"), e("cls", "tech", "out1"), e("cls", "other", "fallback"),
                  e("model", "cls", "ai_model", "ai_model")],
    },
    "multi-agent-research": {
        "name": "Multi-Agent Research Team",
        "description": "A supervisor agent delegates to researcher and writer sub-agents (agents as tools).",
        "tags": ["ai", "multi-agent"],
        "nodes": [
            n("chat", "trigger.chat", "Chat", 0, 0),
            n("sup", "ai.agent", "Supervisor", 300, 0,
              system_message="You coordinate a team. Delegate research to `researcher` and drafting to `writer`, then return the final answer."),
            n("m1", "model.openai", "Supervisor model", 160, 220, **MODEL),
            n("res", "tool.agent", "researcher", 380, 220, tool_description="Finds facts about a topic.",
              system_message="You research topics using Wikipedia and report concise facts with sources."),
            n("m2", "model.openai", "Researcher model", 380, 440, **MODEL),
            n("wiki", "tool.wikipedia", "Wikipedia", 540, 440),
            n("wri", "tool.agent", "writer", 720, 220, tool_description="Writes polished prose from notes.",
              system_message="You turn notes into a clear, well-structured answer."),
            n("m3", "model.openai", "Writer model", 790, 440, **MODEL),
        ],
        "edges": [
            e("chat", "sup"), e("m1", "sup", "ai_model", "ai_model"),
            e("res", "sup", "ai_tool", "ai_tool"), e("wri", "sup", "ai_tool", "ai_tool"),
            e("m2", "res", "ai_model", "ai_model"), e("wiki", "res", "ai_tool", "ai_tool"),
            e("m3", "wri", "ai_model", "ai_model"),
        ],
    },
    "human-approval": {
        "name": "Human-in-the-Loop Approval",
        "description": "Pause with LangGraph interrupt() until a reviewer approves or rejects (no credentials needed).",
        "tags": ["hitl"],
        "nodes": [
            n("start", "trigger.manual", "Start", 0, 0, test_data='{"customer": "Jane", "refund": 250}'),
            n("ask", "flow.approval", "Approve refund?", 260, 0,
              message="Refund {{ $json.refund }} USD to {{ $json.customer }}?"),
            n("ok", "core.set", "Refund issued", 540, -80, fields=[{"name": "result", "value": "refunded"}]),
            n("no", "core.set", "Refund denied", 540, 80, fields=[{"name": "result", "value": "denied"}]),
        ],
        "edges": [e("start", "ask"), e("ask", "ok", "approved"), e("ask", "no", "rejected")],
    },
    "scheduled-digest": {
        "name": "Scheduled API Digest",
        "description": "Every morning: fetch data, transform with Python, merge and summarize.",
        "tags": ["schedule", "http", "code"],
        "nodes": [
            n("cron", "trigger.schedule", "Every day 9am", 0, 0, cron="0 9 * * *"),
            n("http", "core.http", "Fetch posts", 260, -80, url="https://jsonplaceholder.typicode.com/posts?_limit=5"),
            n("users", "core.http", "Fetch users", 260, 80, url="https://jsonplaceholder.typicode.com/users?_limit=5"),
            n("merge", "flow.merge", "Merge", 520, 0, mode="by_position"),
            n("code", "core.code", "Summarize", 760, 0, code=(
                "titles = [i.get('title') for i in items]\n"
                "return [{'count': len(items), 'titles': titles}]")),
        ],
        "edges": [e("cron", "http"), e("cron", "users"), e("http", "merge", "main", "input1"),
                  e("users", "merge", "main", "input2"), e("merge", "code")],
    },
}
