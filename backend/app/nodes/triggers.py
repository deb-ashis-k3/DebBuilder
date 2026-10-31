"""Trigger nodes. A trigger simply emits the items the execution started with;
the HTTP layer / scheduler / WhatsApp / voice worker supply those items."""

from .base import NodeContext, NodeType, prop, register


async def _passthrough(ctx: NodeContext):
    return {"main": ctx.items or [{}]}


async def _manual(ctx: NodeContext):
    # Optional test payload configured on the node (used for editor runs).
    if ctx.items and ctx.items != [{}]:
        return {"main": ctx.items}
    sample = ctx.raw_params.get("test_data")
    if isinstance(sample, str) and sample.strip():
        import json

        data = json.loads(sample)
        return {"main": data if isinstance(data, list) else [data]}
    return {"main": [{}]}


TRIGGER = dict(trigger=True, inputs=[], group="trigger", color="#f97316")

register(NodeType(
    type="trigger.manual", label="Manual Trigger", icon="mouse-pointer-click",
    description="Runs when you click 'Execute workflow' in the editor.",
    properties=[prop("test_data", "Test data (JSON)", "json", "", description="Items emitted on manual runs")],
    execute=_manual, **TRIGGER,
))

register(NodeType(
    type="trigger.webhook", label="Webhook", icon="webhook",
    description="Starts the workflow on an HTTP request to /webhook/<path>.",
    properties=[
        prop("path", "Path", "string", "my-hook", required=True, description="URL: {base}/webhook/<path>"),
        prop("method", "HTTP Method", "options", "POST", options=["GET", "POST", "PUT", "PATCH", "DELETE"]),
        prop("respond", "Respond", "options", "last_node", options=[
            {"label": "Immediately (202)", "value": "immediately"},
            {"label": "When last node finishes", "value": "last_node"},
            {"label": "Using 'Respond to Webhook' node", "value": "respond_node"},
        ]),
        prop("auth_header", "Required header (name:value)", "string", "", placeholder="X-Token: secret"),
    ],
    execute=_passthrough, **TRIGGER,
))

register(NodeType(
    type="trigger.schedule", label="Schedule", icon="clock",
    description="Runs on a schedule while the workflow is active: a cron expression or every N seconds.",
    properties=[
        prop("mode", "Trigger", "options", "cron", options=[
            {"label": "Cron expression", "value": "cron"}, {"label": "Every N seconds", "value": "interval"}]),
        prop("cron", "Cron expression", "string", "*/15 * * * *", show_if={"mode": ["cron"]},
             description="minute hour day month weekday (e.g. 0 9 * * 1-5)"),
        prop("seconds", "Every (seconds)", "number", 60, show_if={"mode": ["interval"]},
             description="1 or more. If a run takes longer than this, the next one is skipped rather than piling up."),
        prop("timezone", "Timezone", "string", "UTC", show_if={"mode": ["cron"]}),
    ],
    execute=_passthrough, **TRIGGER,
))

register(NodeType(
    type="trigger.chat", label="Chat Trigger", icon="message-circle",
    description="Starts on a chat message (editor chat panel or POST /api/chat/<workflow_id>). Emits {message, session_id}.",
    properties=[
        prop("welcome", "Welcome message", "string", "Hi! How can I help?"),
        prop("public", "Allow public chat endpoint", "boolean", False),
    ],
    execute=_passthrough, **TRIGGER,
))

register(NodeType(
    type="trigger.whatsapp", label="WhatsApp Trigger", icon="message-square",
    description="Starts on incoming WhatsApp Cloud API messages. Emits {from, name, text, type, message_id, ...}.",
    properties=[
        prop("credential", "WhatsApp credential", "credential", None, credential_types=["whatsapp"], required=True),
        prop("events", "Events", "options", "messages", options=[
            {"label": "Messages", "value": "messages"},
            {"label": "Messages + status updates", "value": "all"},
        ]),
    ],
    execute=_passthrough, **TRIGGER,
))

register(NodeType(
    type="trigger.subworkflow", label="When Called by Another Workflow", icon="corner-down-right",
    description="Entry point for 'Execute Workflow' nodes and agent workflow tools.",
    properties=[prop("input_schema", "Expected inputs (doc only)", "text", "", rows=3)],
    execute=_passthrough, **TRIGGER,
))

register(NodeType(
    type="trigger.error", label="Error Trigger", icon="alert-triangle",
    description="Runs when another workflow that names this one as its error workflow fails.",
    properties=[],
    execute=_passthrough, **TRIGGER,
))

register(NodeType(
    type="trigger.form", label="Form Trigger", icon="file-text",
    description="Serves a hosted form at /form/<workflow_id>; submissions start the workflow.",
    properties=[
        prop("title", "Form title", "string", "Contact us"),
        prop("description", "Description", "text", ""),
        prop("fields", "Fields", "collection", [{"name": "email", "label": "Email", "type": "email", "required": True}],
             fields=[
                 prop("name", "Field key"),
                 prop("label", "Label"),
                 prop("type", "Type", "options", "text", options=["text", "email", "number", "textarea", "date"]),
                 prop("required", "Required", "boolean", False),
             ]),
        prop("success_message", "Success message", "string", "Thanks! We received your submission."),
    ],
    execute=_passthrough, **TRIGGER,
))
