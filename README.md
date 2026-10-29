# DebBuilder

An n8n-style visual workflow builder whose execution engine is **LangGraph**, with **LangChain** AI nodes, **LiveKit** real-time voice agents (Vapi-style), and the **WhatsApp Business Cloud API** (feature parity with `wapi.go`).

```
frontend/  React + Vite + React Flow editor (n8n-like canvas, NDV, executions, credentials, chat & voice panels)
backend/   FastAPI + SQLAlchemy (Postgres) + LangGraph engine + node library
backend/voice/worker.py   LiveKit Agents worker that turns any workflow into a voice agent
```

## Quick start

Prereqs: Python 3.12 via [uv](https://docs.astral.sh/uv/), Node 20+, Postgres on `localhost:5432` (user `postgres`). The database `debbuilder` and all tables are created automatically on first start.

```powershell
# 1. API (http://localhost:8000)
cd backend
uv sync
uv run python -m app            # add --reload while developing

# 2. Editor (http://localhost:5173)
cd frontend
npm install
npm run dev

# 3. (optional) Voice worker
cd backend
uv sync --extra voice
$env:LIVEKIT_URL="wss://<project>.livekit.cloud"; $env:LIVEKIT_API_KEY="..."; $env:LIVEKIT_API_SECRET="..."
uv run --extra voice python voice/worker.py dev
```

Or run `./start.ps1` to launch the API and the editor together.

Production: `npm run build` in `frontend/`; the API then serves the built UI at http://localhost:8000.

`backend/.env` holds the DB URL, the Fernet key that encrypts credentials, and the internal token the voice worker uses. **Back up `ENCRYPTION_KEY`**: losing it makes stored credentials unreadable.

## How it works

Each workflow is stored as JSON (`nodes`, `edges`) and compiled into a `langgraph.StateGraph` per execution (`backend/app/engine/runtime.py`):

| n8n concept | DebBuilder / LangGraph implementation |
|---|---|
| Items flowing between nodes | Each node returns `{port: [items]}`; routers dispatch `Send(target, items)` for every port that produced items |
| Branching (IF / Switch / Classifier) | Multiple output ports; only non-empty ports fire |
| Merge (wait for both inputs) | Node added with `defer=True`, reads every incoming edge from graph state |
| Loops | Graph cycles (`Loop Over Items` keeps the batch cursor); `recursion_limit` guards runaway loops |
| Wait for human / approval | `langgraph.types.interrupt()` + `AsyncPostgresSaver` checkpoints, resumed with `Command(resume=…)` |
| Cluster nodes (Agent + Model/Memory/Tools) | `ai_*` edges are not flow edges; the root node instantiates attached sub-nodes (`ctx.supply`) |
| Expressions `{{ $json.x }}` | Sandboxed Jinja with n8n-style names: `$json`, `$('Node').json`, `$node['Node']`, `$now`, `$vars`, `$execution` |
| Retry / continue on fail / error output | Per-node settings, error workflow per workflow (`Error Trigger`) |
| Live execution view | Events on an in-process bus → WebSocket `/ws/workflows/{id}` (node status, item counts, LLM tokens) |

### Node library

- **Triggers**: Manual, Webhook (`/webhook/<path>`, `/webhook-test/<path>`), Schedule (cron), Chat, Form (hosted at `/form/<id>`), WhatsApp, Voice Agent (LiveKit), Called by another workflow, Error Trigger.
- **Listener triggers** (run while the workflow is active, see below): Kafka, Redis Pub/Sub, Redis Streams, Redis List queue, RabbitMQ, MQTT, NATS, AWS SQS, Postgres (LISTEN/NOTIFY or table row changes), Email (IMAP), RSS/Atom, Server-Sent Events, Local File, Interval, Workflow Event.
- **AI (LangChain + LangGraph)**: AI Agent (`create_agent` tool-calling loop, optional structured output), Basic LLM Chain, Text Classifier (one output per category), Information Extractor, Sentiment.
  - Chat models: OpenAI and any OpenAI-compatible endpoint (Groq, LM Studio, vLLM…), Anthropic, Google Gemini, Ollama.
  - Memory: Postgres chat memory (per session key).
  - Tools: HTTP Request tool, Python Code tool, Call Workflow tool, **AI Agent tool (multi-agent / supervisor patterns)**, MCP client (remote MCP servers), Web search (Tavily), Wikipedia, Calculator, Date & Time.
- **Voice (LiveKit)**: Voice Agent trigger (STT: OpenAI/Deepgram; TTS: OpenAI/Cartesia/ElevenLabs/Deepgram; Silero VAD; barge-in; token streaming to TTS), Make Phone Call (outbound SIP, like Vapi outbound), Create Voice Session (room + token for any LiveKit client).
- **WhatsApp**: send text, media, reply buttons, list menus, templates, location, reactions; mark read + typing indicator; get media URL; list/create templates; phone numbers; business profile. Webhook verification and `X-Hub-Signature-256` checking.
- **Core / Flow / Data**: HTTP Request, Code (Python in-process; JavaScript, TypeScript, Bash, PowerShell, Perl, Java, Ruby, PHP, Go or any language via a custom command as a subprocess; see `backend/app/nodes/code_runtimes.py`), Edit Fields (Set), If, Filter, Switch, Merge, Loop Over Items, Wait, Human Approval, Execute Workflow, Respond to Webhook, Stop and Error, No-op, Split Out, Aggregate, Limit, Sort, Remove Duplicates, Date & Time, Postgres (parameterised SQL), Slack, Telegram, Email (SMTP), Sticky Note.

### Adding a node

Create a function and register it (any file imported in `app/nodes/__init__.py`):

```python
async def _shout(ctx: NodeContext):
    return {"main": [{**item, "text": str(ctx.param("text", i)).upper()} for i, item in enumerate(ctx.items)]}

register(NodeType(type="custom.shout", label="Shout", group="data", icon="megaphone",
                  description="Uppercase text", properties=[prop("text", "Text", "string", "{{ $json.text }}")],
                  execute=_shout))
```

The editor picks it up automatically (forms are generated from `properties`). To add a sub-node, set `provides="ai_tool"` (or `ai_model` / `ai_memory`) and implement `supply` instead of `execute`.

## Voice agents (LiveKit)

1. Create a **LiveKit** credential (Cloud project or self-hosted `livekit-server --dev`, whose dev keys are `devkey`/`secret`) and an **OpenAI** (or Deepgram/Cartesia/ElevenLabs) credential.
2. Use the *Voice Receptionist* template, or add a **Voice Agent (LiveKit)** trigger. It emits `{text, session_id, room, phone?}` each user turn, and the workflow's final `output` is spoken.
3. Start the worker. It must use the **same LiveKit project** as the credential.
4. Click **Voice** in the editor to talk to the workflow in your browser.
5. Phone calls: configure a SIP trunk in LiveKit. **Outbound**: the *Make Phone Call* node. **Inbound**: create a SIP dispatch rule for agent `debbuilder-voice` with metadata `{"workflow_id": "<id>"}`.

## WhatsApp

1. Create a Meta app with WhatsApp, then add a **WhatsApp** credential (access token, phone number ID, WABA ID, verify token, app secret).
2. Expose the API publicly (for example `cloudflared tunnel --url http://localhost:8000`) and set the Meta webhook to `https://<public>/api/whatsapp/webhook` with your verify token. Subscribe to `messages`.
3. Use the *WhatsApp AI Assistant* template and **activate** the workflow.

## Listener triggers

`backend/app/listeners.py` keeps one supervised task per listener trigger of every **active** workflow (started on activation and at server start, restarted when the node is saved with new settings).

- **Reconnects** with exponential backoff (1 s to 60 s). The node's Input panel shows the live state, event count and last error.
- **Backpressure**: at most *Max parallel executions* run at once per trigger; further events wait, so a burst doesn't pile up unbounded work.
- **Delivery**: Kafka, Redis Streams, RabbitMQ and SQS can acknowledge *after the workflow succeeds* (at-least-once: failures are redelivered) or *immediately* (at-most-once).
- **Editor test runs** ("Execute step") wait for the next live event without affecting production. Kafka reads without a consumer group, Redis Streams uses plain XREAD, RabbitMQ/SQS/Redis lists only peek, NATS skips the queue group, and MQTT uses a separate client id.
- **Postgres table mode** creates a notify trigger and function on the table while listening and drops them on deactivate. Rows over the 8 KB NOTIFY limit are sent as `{operation, table, truncated: true}` so your writes never fail.
- **One API process at a time**: each listener runs inside the API process. With several API instances, Pub/Sub, MQTT, RSS, file and interval triggers would fire once per instance.

Credentials for the brokers are under **Queues & Streaming** (Kafka with SASL/SSL, RabbitMQ, MQTT, NATS); Redis, Postgres, IMAP and AWS reuse the existing types.

To point the editor at an API on another port: `BACKEND_URL=http://localhost:8001 npm run dev`.

## Security

Every request is checked by `backend/app/security.py` against one route policy table; routes not listed are **denied**.

- **Accounts:** the first visit (from the server itself) creates the **owner**. Admins add users from *Settings → Users*. New users get a temporary password and must set their own at first sign-in.
- **Passwords:** Argon2id. At least 12 characters, common passwords blocked. 5 failed sign-ins lock the account for 15 minutes; sign-in is also throttled per IP (`LOGIN_RATE_LIMIT`, default 10/min).
- **Email code at every sign-in:** after the password, a 6-digit code is emailed to the user and must be entered to finish. The first owner setup works the same way, which also proves the address. The code:
  - is random, stored only as a keyed hash, and expires in 10 minutes;
  - works once and allows 5 wrong guesses;
  - can be resent at most every 30 s, and a resend replaces the previous code.

  Configure SMTP in `backend/.env` (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_SECURITY`, `MAIL_FROM`). Without it, codes go to the backend console (`EMAIL_CODE_FALLBACK=log`, development only); set `off` in production.
- **Authenticator app (TOTP), optional:** can be used instead of the emailed code. *Settings → Account*, with QR code and 10 one-time recovery codes. Codes can't be replayed. Admins can reset a user's 2FA.
- **Sessions:**
  - stored as hashes only;
  - HttpOnly + SameSite=Strict cookie (`Secure` when `PUBLIC_BASE_URL` is https);
  - 12 h idle and 7 day maximum lifetime;
  - you can see and revoke your sessions;
  - changing your password, or an admin changing your role or disabling you, signs out your other sessions.
- **CSRF:** state-changing browser requests need the double-submit `X-CSRF-Token` and an allowed `Origin`.
- **API keys** (*Settings → Account*): `X-API-Key: dbk_…` or `Authorization: Bearer dbk_…`. Each key is shown once, stored hashed, expires, and is capped at its creator's role.
- **Roles:**

  | Role | Can |
  |---|---|
  | viewer | read workflows, executions, credential names |
  | editor | + create/edit/run workflows, manage credentials, delete executions |
  | admin | + manage users (not owners), read the audit log |
  | owner | + manage admins and owners (at least one owner always remains) |

- **Public routes:**
  - `/webhook/*` (active workflows only);
  - `/form/*`;
  - `/api/chat/*` only when the Chat Trigger is marked public;
  - WhatsApp (verified by Meta signature/token), OAuth callback (signed state), voice worker (`INTERNAL_TOKEN`).

  `/webhook-test/*`, WebSockets and the API docs require sign-in.
- **Audit log** (*Settings → Audit log*): sign-ins, failures, lockouts, 2FA, keys, user changes, denied requests and every data change. Request bodies are never stored.
- **Headers:** `nosniff`, `X-Frame-Options: DENY`, CSP on the UI, HSTS on https, `Cache-Control: no-store` on the API.

**Locked out?** From a shell on the server (in `backend/`): `uv run python -m app.cli list-users`, `uv run python -m app.cli reset-password you@example.com` (prints a temporary password) or `uv run python -m app.cli disable-mfa you@example.com`. These need access to the machine, are audited, and sign the user out everywhere.

## API cheatsheet

| Endpoint | Purpose |
|---|---|
| `GET /api/node-types` | Node catalogue (drives the editor) |
| `CRUD /api/workflows`, `POST /api/workflows/{id}/run` | Workflows; run (optionally with an unsaved graph) |
| `GET /api/executions`, `/{id}`, `POST /{id}/resume`, `/{id}/cancel`, `/{id}/retry` | Execution history and control |
| `POST /api/chat/{workflow_id}` `{message, session_id, wait}` | Chat with a workflow (enable *public* on the Chat Trigger for external use) |
| `ANY /webhook/{path}` | Production webhooks (active workflows) |
| `GET/POST /form/{workflow_id}` | Hosted forms |
| `GET/POST /api/whatsapp/webhook` | Meta webhook |
| `POST /api/voice/session` | Browser voice session (room + token + agent dispatch) |
| `WS /ws/workflows/{id}`, `WS /ws/executions/{id}` | Live events |

## Not implemented yet (vs. full n8n)

- Per-workflow sharing and projects: access is by role across the whole instance (see Security).
- The several hundred SaaS integration nodes. Use HTTP Request, the MCP tool or a custom node.
- Vector-store / RAG nodes. The local Postgres has no `pgvector`; install it to add PGVector nodes.
- Binary data (files). The Code node runs in-process Python, as n8n's does, so treat workflow editors as trusted.
- Queue mode / horizontal workers. Executions run in the API process, and the live event bus is in-memory.
