# DebBuilder

An n8n-style visual workflow builder whose execution engine is **LangGraph**, with **LangChain** AI nodes, **LiveKit** real-time voice agents (Vapi-style), and the **WhatsApp Business Cloud API** (feature parity with `wapi.go`).

```
frontend/  React + Vite + React Flow editor (n8n-like canvas, NDV, executions, credentials, chat & voice panels)
backend/   FastAPI + SQLAlchemy (Postgres) + LangGraph engine + node library
backend/voice/worker.py   LiveKit Agents worker that turns any workflow into a voice agent
```

## Quick start

Prereqs: Python 3.12 via [uv](https://docs.astral.sh/uv/), Node 20+, Postgres on `localhost:5432` (user `postgres`).

Install the `debbuilder` command once (editable: it runs this checkout, so code changes need no reinstall):

```powershell
cd backend
uv tool install --editable ".[voice]"
uv tool update-shell        # adds ~/.local/bin to PATH; open a new terminal afterwards
```

Then, from anywhere, in the terminal you're in (VS Code's included):

```powershell
debbuilder                  # = debbuilder start
```

(Don't type `start debbuilder` on Windows: `start` is the Windows command for "open in a new window".
Without installing: `uv run --directory backend python -m app` from the repo root.)

`debbuilder start` sets everything up and runs in the current terminal (Ctrl+C stops it):

1. creates `backend/.env` from `.env.example` on first run and generates `ENCRYPTION_KEY` / `INTERNAL_TOKEN` if empty;
2. creates the Postgres database in `DATABASE_URL` if it's missing (tables are created at startup);
3. runs `npm install` when the editor's dependencies changed and `npm run build` when its sources are newer than `frontend/dist`;
4. starts the API, which serves the editor on the same port, and opens the browser; if DebBuilder already runs, it just opens it.

| Command | Use |
|---|---|
| `debbuilder start` | local use (options `--no-build`, `--no-browser`, `--reload`) |
| `debbuilder dev` | development: API + Vite dev server (http://localhost:5173), both live-reloading |
| `debbuilder serve` | **production**: settings only from the environment, never writes `.env` or builds; proxy headers, graceful shutdown |
| `debbuilder build` | build the editor into `frontend/dist` (CI) |
| `debbuilder check` | check settings + database connection, exit code 0/1 (CI, init containers) |
| `debbuilder voice-worker --agent=<name>` | one LiveKit voice worker, to scale voice separately |

`backend/.env` holds the DB URL, the Fernet key that encrypts credentials, and the internal token the voice worker uses. **Back up `ENCRYPTION_KEY`**: losing it makes stored credentials unreadable.

## Production (Linux, Docker, Kubernetes)

The [Dockerfile](Dockerfile) builds the editor at image build time and runs `debbuilder serve` as an unprivileged
user (uid 10001). Voice models are baked in; the root filesystem can be read-only (only `/app/backend/data` and `/tmp` are written).

- **One machine (VM / server):** [docker-compose.yml](docker-compose.yml) runs DebBuilder + PostgreSQL. Put `ENCRYPTION_KEY`,
  `INTERNAL_TOKEN`, `POSTGRES_PASSWORD` and `PUBLIC_BASE_URL` in a `.env` next to it, then `docker compose up -d --build`
  and put a TLS reverse proxy (Caddy, Traefik, nginx) in front of port 8000.
- **Kubernetes:** [deploy/kubernetes/debbuilder.yaml](deploy/kubernetes/debbuilder.yaml): Deployment (2 replicas, rolling updates,
  startup / liveness / readiness probes, PodDisruptionBudget, non-root + read-only), Service, Ingress (TLS, WebSockets,
  sticky sessions), Secret + ConfigMap, a `check` init container, and an optional separate voice-worker Deployment.
  Use a managed PostgreSQL (or an operator) for `DATABASE_URL`.
- **Plain Linux host:** `uv tool install ".[voice]"` (or `uv sync --extra voice`), `debbuilder build`, then run `debbuilder serve`
  under systemd with the settings in an `EnvironmentFile`.

**Configuration** is environment variables only (see [backend/.env.example](backend/.env.example)); keep `ENCRYPTION_KEY`,
`INTERNAL_TOKEN`, `DATABASE_URL` and SMTP passwords in your secret store. Back up `ENCRYPTION_KEY` with the database:
credentials can't be decrypted without it. Set `PUBLIC_BASE_URL` to the public https URL and `FORWARDED_ALLOW_IPS`
to your proxy (`*` inside Kubernetes).

**Probes:** `GET /api/health` (liveness: the process answers) and `GET /api/ready` (readiness: database reachable; also
reports this replica's id and whether it is the leader).

**High traffic** (thousands of executions per second across workflows), see [engine/store.py](backend/app/engine/store.py):
- **Batched history writes.** Executions are written by one background writer per process in a single multi-row upsert
  every 200 ms (or every 500 rows), not one INSERT + one UPDATE each. A short run is written once, already finished.
  Measured: 20,000 executions → about 120 commits (0.006 per execution instead of 2). Rows are written in primary-key
  order, so replicas writing at the same time can't deadlock (two replicas under concurrent webhook load: 0 deadlocks).
  The API reads a run that isn't written yet from the writer's buffer, and paused / editor runs are written at once.
- **Backpressure:** past `EXECUTIONS_MAX_PENDING` buffered rows, new executions wait for the database instead of filling memory.
- **Workflow snapshots** are stored once per version (`workflow_snapshots`), not copied into every execution.
- **What to keep** (like n8n): successful / failed / manual executions, and full data or a summary (status, timings,
  counts). Defaults in `EXECUTIONS_SAVE_*`, overridable per workflow in *Workflow settings → Execution history*.
  For a 200-requests-per-second webhook, "save failed only" or "summary" keeps the table small and fast.
- **Stats** come from a per-day rollup (`execution_stats`) the writer increments; the execution list stops counting at 10,000.
- **Retention** (leader): history older than `EXECUTIONS_MAX_AGE_DAYS` (14) is deleted in batches of 5,000; runs left
  "running" by a crashed server are marked failed. Only running / waiting rows are in the status index.
- **No per-execution overhead elsewhere:** live events are buffered only for runs someone watches (editor runs,
  workflows open in an editor); credentials are cached for 30 s per process (cleared on every replica when edited);
  the webhook route table is rebuilt single-flight after a change (a burst can't stampede the database).
- **Sizing:** throughput scales with replicas (each is one process; the engine is CPU-bound). On Linux the image uses
  uvloop + httptools. Budget connections: replicas × (`DB_POOL_SIZE` + `DB_MAX_OVERFLOW` + 13) < Postgres
  `max_connections`, or put PgBouncer (transaction mode) in front. Webhooks that don't need the workflow's result
  should respond immediately, so callers don't hold connections open while it runs.

**Several replicas** share one PostgreSQL:
- every replica serves the editor, API and webhooks and runs executions;
- schedules, listener triggers (Kafka, IMAP, …) and voice workers run on **one elected replica** (a Postgres advisory lock);
  when it stops, another takes over within seconds, so nothing fires twice and nothing stops firing;
- saving or (de)activating a workflow on any replica reaches the others through Postgres `NOTIFY`;
- `BACKGROUND_TASKS=off` keeps a replica out of the election (pure web pods next to a worker pod);
- the live execution view streams from the replica running the execution: use sticky sessions (the manifests do);
- `data/` (execution files, the Read/Write Files sandbox) must be shared storage (ReadWriteMany) when replicas > 1.

## How it works

Each workflow is stored as JSON (`nodes`, `edges`) and compiled into a `langgraph.StateGraph` per execution (`backend/app/engine/runtime.py`):

| n8n concept | DebBuilder / LangGraph implementation |
|---|---|
| Items flowing between nodes | Each node returns `{port: [items]}`; routers dispatch `Send(target, items)` for every port that produced items |
| Branching (IF / Switch / Classifier) | Multiple output ports; only non-empty ports fire |
| Merge (wait for both inputs) | Node added with `defer=True`, reads every incoming edge from graph state |
| Loops | Graph cycles (`Loop Over Items` keeps the batch cursor); `recursion_limit` guards runaway loops |
| Shared graph state (`$state`) | One `state` channel with per-key reducers (replace, append, add, merge, messages) from *Workflow settings → State*; checkpointed, combined across parallel branches (`backend/app/engine/state.py`) |
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

### RAG (embeddings, vector stores, retrievers)

Sub-nodes plug together like models and tools:

```
Vector Store: Insert ─┬─ Vector store (In-Memory, PGVector, Qdrant, Pinecone, Supabase) ── Embeddings
                      └─ Default Data Loader (text, item JSON, or files: PDF/Word/HTML/CSV/Excel) ── Text splitter
Q&A Chain ── Retriever (Vector Store / MultiQuery / Contextual Compression) [+ Cohere reranker]
AI Agent ── Vector Store Tool ── Vector store ── Embeddings
```

- **Embeddings:** OpenAI (and compatible servers), Azure OpenAI, Gemini, Ollama, Mistral, Cohere.
- **Text splitters:** Recursive Character, Character, Token.
- **Flow nodes:** Vector Store: Search (one item per document, with its score), Summarization Chain (map-reduce or stuff).
- **Tools:** Think.
- **Memories:** Simple (in-process), Redis.
- **More chat models:** Azure OpenAI, Groq, Mistral, DeepSeek, OpenRouter, xAI, Together; model lists load from the provider.

Retrievals are logged in the execution tree under the retriever node. In-Memory stores are separate per workspace. PGVector needs the [pgvector](https://github.com/pgvector/pgvector) extension on that Postgres server.

### Files (binary data)

Items can carry files, like n8n: `item.binary.data = {id, fileName, mimeType, fileSize}`, readable in expressions as `$binary.data.fileName`. The bytes are stored on disk under `DATA_DIR/binary/<workspace>` (default `backend/data`, max `MAX_BINARY_MB`, default 100). The data view's **Binary** tab previews and downloads them; a workspace can only download its own.

- **Files:** Extract From File (CSV, Excel, PDF, JSON, HTML, Word, text, Base64), Convert To File (CSV, Excel, JSON, HTML table, text, Base64), Compression (zip/gzip), Edit Image, Read/Write Files from Disk.
  - Read/Write Files is confined to `FILES_ROOT`, default `backend/data/files`.
- **Data:** Compare Datasets, Summarize, Rename Keys, Crypto, JWT, TOTP, XML, HTML (CSS-selector extraction), Markdown, iCalendar.
- **System:** GraphQL, SSH/SFTP, FTP, Execute Command.
  - Execute Command is **off** unless `ENABLE_EXECUTE_COMMAND=true`, because it runs shell commands with the server's permissions.
- HTTP Request can return the response as a file (`Response format: file`).

### Integrations (declarative nodes)

79 services with about 460 operations, plus a **Custom API call** on each (any endpoint, using the service's credential). They include GitHub, GitLab, Slack, Notion, Airtable, Jira, HubSpot, Stripe, Shopify, Google Sheets/Drive/Calendar/Gmail, Outlook/OneDrive/Teams, Zendesk, Twilio, SendGrid, Mailchimp, Dropbox, Linear, Salesforce, PayPal and more.

Each service is a spec in `backend/app/nodes/services/*.py`, run by one engine in `backend/app/nodes/declarative.py`. The engine handles:

- **Auth:** from the credential catalog.
- **Requests:** path, query, JSON, form and multipart bodies, plus GraphQL.
- **Pagination ("Return all"):** Link headers, cursors, pages, offsets, next-URLs and Stripe-style `starting_after`.
- **Results and errors:** splitting responses into items, file upload/download, and readable API errors.

To add a service, write a `service(...)` with `op(...)` entries; nothing else is needed.

### Graph state and agentic loops

Like a LangGraph `State`, a workflow can declare shared state in *Workflow settings → State*: each key has a reducer and an initial value.

| Reducer | A write… | Typical use |
|---|---|---|
| replace | overwrites | `question`, `draft` |
| append | adds to a list | `critiques`, collected results |
| add | adds a number | `iteration` counters |
| merge | merges into an object | accumulated fields |
| messages | appends chat messages (same `id` replaces) | agent conversation (`MessagesState`) |

- **Read** anywhere: `{{ $state.key }}`; in the Code node, `state["key"]`.
- **Write** with the **Update State** node, `set_state("key", value)` in Code, or the AI Agent's *Save output to state key* / *Conversation in state key*.
- **Loop back** by wiring a node back to an earlier one; guard it with an If on `{{ $state.iteration }}` (and `recursion_limit` stops runaway loops).
- State is part of the LangGraph checkpoint: it survives Human Approval and Wait pauses. Parallel branches writing the same key are combined by its reducer. A node sees the state as of when its input was sent.
- The execution log's **State** pane shows the state after each step, with the keys that step wrote marked.

Start from the **Agentic Research Loop** template: writer agent with tools → critic with structured output → loop back with the critique until approved or `$vars.max_iterations` → human review (reject = loop back with feedback).

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

## Voice agents (LiveKit, SIP, Plivo)

A workflow with a **Voice Agent** trigger is a phone/web voice agent:

- **The worker** (`backend/voice/worker.py`) handles the audio: noise cancellation, VAD, STT, turn detection, interruptions, TTS, background audio, DTMF, voicemail detection and recording.
- **The workflow** handles the business logic. Every caller turn runs it with `{text, session_id, phone, dialed_number, direction, input_type, dtmf?}`, and its output is spoken. Use an AI Agent node there (LangChain model + tools, LangGraph runtime) for the conversation.

**Voice Agent trigger settings** (grouped in the node):

| Group | Settings |
|---|---|
| Agent | agent name, inbound phone numbers routed here, greeting, who speaks first |
| Speech-to-text | OpenAI / Deepgram Nova / Deepgram Flux, model, language + auto-detect, key terms, noise reduction, smart format |
| Text-to-speech | OpenAI / Cartesia / ElevenLabs / Deepgram, model, voice, speed, style (OpenAI), emotion (Cartesia), stability/similarity/style (ElevenLabs), strip markdown |
| Turn-taking | LiveKit turn-detector model / VAD / STT endpointing, fixed or dynamic endpointing delays, VAD threshold and timings |
| Interruptions | barge-in on/off, adaptive (ignores "mm-hm") or VAD, min duration/words, resume after false interruptions |
| Audio | enhanced noise cancellation (auto: BVC-Telephony for phone, BVC for web; needs LiveKit Cloud), ambience (office/city/forest/crowd), thinking sound |
| Behaviour | stream tokens, filler phrase when the workflow is slow, check in on silence then hang up, goodbye, max call length |
| Phone | keypad (DTMF) input to the workflow, voicemail/IVR detection for outbound calls (hang up / leave a message / report) |
| Recording | room recording to S3 (dual-channel caller/agent), consent announcement |

Preemptive generation is always off: each reply runs the workflow, and a speculative run could have side effects.

**Inside the workflow:**

- **Voice Call Action** node and **Call Control** tool (for an AI Agent): hang up, transfer to a number or SIP URI (cold transfer), send DTMF, say something, hold music. These run after the turn's reply has been spoken.
- **Voice Call Events** trigger: `call_started`, `call_ended` (transcript, duration, reason, usage, recording file), `amd`, `dtmf`, `transfer`, `error`. Put post-call logic here.
- **LiveKit Telephony** node: inbound/outbound trunks, dispatch rules, transfer, hang up, start/stop recording, rooms.
- **Plivo** node: numbers (search, buy, route), calls (make, live speak/play/record/DTMF, transfer, hang up), applications, SIP endpoints, recordings, and Zentrunk trunks, credentials, URIs and IP access lists.
- **Make Phone Call** node: outbound calls with caller ID, ringing timeout, max duration, Krisp, SIP headers and context for the agent.

**Phone page** (sidebar):

- See and create inbound trunks, outbound trunks and dispatch rules (a dispatch rule sends each caller to an agent, with a new room per caller).
- **Connect a Plivo number**, which sets up the following in one click:
  1. A Plivo origination URI pointing at your LiveKit SIP URI.
  2. A Zentrunk inbound trunk, with your number routed to it.
  3. A LiveKit inbound trunk plus a dispatch rule to your agent.
  4. Optionally, a Zentrunk credential + outbound trunk, and a LiveKit outbound trunk to its termination domain.

**Setup:**

1. Add a **LiveKit** credential: URL, key, secret, and the project's **SIP URI** (LiveKit Cloud → Settings) for phone calls.
2. Add STT/TTS credentials (OpenAI, Deepgram, Cartesia or ElevenLabs) and, for phone numbers, a **Plivo** credential (Auth ID + token).
3. Start from the **Phone Receptionist** template, or add a Voice Agent trigger. Talk to it with **Voice** in the editor; the panel has a DTMF keypad for testing IVR flows.
4. Install the voice packages once (`cd backend; uv sync --extra voice`) and **activate** the workflow: the backend
   starts a LiveKit worker for the trigger's agent name with its LiveKit credential, one per (credential, agent name),
   restarts it if it crashes, restarts it when you save the workflow or edit the credential, and stops it when no
   active workflow uses it. Its state, log and a **Restart** button are on the Voice Agent trigger.
   To run workers elsewhere (other machines, Docker, more capacity), set `VOICE_WORKERS=off` and run
   `AGENT_NAME=<agent> uv run --extra voice python voice/worker.py start` yourself.
5. For a phone number, use **Phone → Connect a Plivo number**, or create trunks and a dispatch rule there for any SIP carrier. **Activate** the workflow so routing by agent name + number finds it.

Calls are routed by the workflow id in the dispatch metadata (dispatch rules, outbound calls and web sessions created by DebBuilder). Otherwise they go to the active workflow whose trigger has the worker's agent name and lists the dialled number, falling back to one that lists no numbers.

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

- **Accounts:** the sign-in page always comes first, with a **Sign up** link.
  - On a new installation, the first sign-up (from the server itself) creates the **owner**.
  - After that, sign-up creates an **editor** with their own empty workspace. Set `ALLOW_SIGNUP=false` in `backend/.env` to make accounts admin-invite only.
  - Either way the account is created only after the emailed code is entered. Signing up with an address that already has an account looks the same as a new one; only the mailbox owner (who gets the code) is told.
  - Admins can also add users from *Settings → Users*. Those users get a temporary password and must set their own at first sign-in.
- **Sign in with email or username:** a username is optional (3–32 characters: letters, digits, `.` `_` `-`, starting with a letter; case-insensitive and unique). Users set it in *Settings → Account*; admins can set it for anyone.
- **Passwords:** Argon2id. At least 12 characters, common passwords blocked. 5 failed sign-ins lock the account for 15 minutes; sign-in is also throttled per IP (`LOGIN_RATE_LIMIT`, default 10/min).
- **Email code at every sign-in:** after the password, a 6-digit code is emailed to the user and must be entered to finish. The first owner setup works the same way, which also proves the address: nothing is saved as an account until the right code is entered (the pending details are kept encrypted for 10 minutes and wiped after). The code:
  - is random, stored only as a keyed hash, and expires in 10 minutes;
  - works once and allows 5 wrong guesses;
  - can be resent at most every 30 s, and a resend replaces the previous code.

  Configure SMTP in `backend/.env` (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_SECURITY`, `MAIL_FROM`), then check it with `uv run python -m app.cli test-email you@example.com`. Without SMTP, codes go to the backend console (`EMAIL_CODE_FALLBACK=log`, development only); set `off` in production.
- **Forgot password:** *Forgot password?* on the sign-in page emails a 6-digit reset code (same rules as above). After the code, the user picks a new password; every session is signed out and a "password changed" notice is emailed. Changing the password in Settings sends the same notice.
  - The answer is identical whether or not the account exists (same message, same timing, wrong codes count down the same way), so it can't be used to find out who has an account.
  - Limits: one request per account every 30 s, 5 per 15 minutes, 10 per IP per minute. Disabled accounts get no email.
  - A reset doesn't sign the user in: they still sign in with the new password and an emailed code.
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
  | viewer | sign in and view their own (empty) workspace; can't create or change anything |
  | editor | create/edit/run **their own** workflows, folders and credentials; see only their own executions |
  | admin | the same in their own workspace + manage users (not owners) and read the audit log |
  | owner | + manage admins and owners (at least one owner always remains) |

- **Per-user workspaces:** workflows, folders, credentials and executions belong to the user who created them (`owner_id`, see `backend/app/access.py`).
  - **Every role**, including admin and owner, sees and changes only their own; anyone else's answers 404. Roles control what you may do, never whose data you see.
  - At run time a workflow can only use **its owner's** credentials and call its owner's workflows (Execute Workflow, Call Workflow tool, error workflow), so pasting someone else's credential id doesn't work.
  - Deleting a user transfers their workspace to the admin who deletes them; active workflows keep running.
  - Upgrading an existing database assigns all existing workflows, folders and credentials to the first owner.

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
