"""Starter workflows. Credentials are left empty; pick them in the editor."""


def n(id, type, name, x, y, **params):
    return {"id": id, "type": type, "name": name, "position": {"x": x, "y": y}, "parameters": params}


def e(src, tgt, sh="main", th="main"):
    return {"id": f"e_{src}_{sh}_{tgt}_{th}", "source": src, "sourceHandle": sh, "target": tgt, "targetHandle": th}


MODEL = dict(model="gpt-5-mini")

LOOP_NOTE = """## Agentic loop on LangGraph state
**State** (Settings → State) is a LangGraph channel per key, each with a reducer: `question` replace,
`draft` replace, `critiques` append, `iteration` add, `messages` messages. Read it anywhere as
`{{ $state.key }}`; it is checkpointed, so it survives the Human Approval pause.

1. **Writer Agent** keeps its conversation in `$state.messages` and saves its answer to `$state.draft`.
2. **Critic** scores the draft with structured output.
3. **Good enough?** approved, or `$state.iteration` reached `$vars.max_iterations` → human review;
   otherwise the critique is appended and the graph **loops back** to the writer.
4. A rejected draft loops back too, with the reviewer's feedback."""

def sticky(id, x, y, w, h, content):
    return {**n(id, "ui.sticky", id, x, y, content=content), "width": w, "height": h}


VOICE_OVERVIEW = """## Voice AI agent: complete example
**Setup (once)**
1. Credentials: **LiveKit** (URL, key, secret + SIP URI), **OpenAI** (agent, embeddings, summaries), **Deepgram** (speech-to-text), **Cartesia** (voice), **Slack** (alerts). Pick them in the nodes marked ⚠.
2. **Phone** page → *Connect a Plivo number* (or add trunks + a dispatch rule) with agent name **acme-support** and this workflow.
3. Install the voice packages once: `cd backend; uv sync --extra voice` (the worker then starts by itself on activate).
4. Run **Load FAQ** once, then **activate** the workflow. Test in the browser with **Voice** (it has a keypad).

**Lanes:** ① live call · ② call events (summary, alerts) · ③ outbound reminder campaign · ④ knowledge base."""

VOICE_CALL_NOTE = """### ① Live call
Every caller turn runs this lane; the output is spoken. **Keypad**: 0 → a person, 9 → hang up, other keys → menu.
**Speech** → *Ava*: memory per call, FAQ search, order lookup, clock, and *Call control* (transfer to billing / sales / a human, hang up).
The trigger shows every voice setting: Deepgram STT with key terms, Cartesia voice, LiveKit turn detector with dynamic endpointing,
adaptive barge-in, noise cancellation, office ambience + typing while thinking, filler phrases, silence check-ins, keypad input,
voicemail detection (outbound) and recording (turn on *Record calls* + pick an S3 credential and bucket)."""

VOICE_EVENTS_NOTE = """### ② Call events
Runs after/around calls, not during them. **call_ended** carries the transcript, duration, end reason and recording file →
an AI summary (structured) → Slack when a follow-up is needed. Voicemail (amd) and transfers are logged."""

VOICE_OUTBOUND_NOTE = """### ③ Outbound campaign
Weekdays 10:00 (UTC): calls each patient through your LiveKit outbound trunk (e.g. the Plivo one the Phone page creates).
The same agent answers with the reminder context; if voicemail picks up, the trigger's voicemail message is left.
Replace the Code node with Google Sheets / Postgres to read real contacts."""

VOICE_KB_NOTE = """### ④ Knowledge base
Run once (and after edits): embeds the FAQ into the **acme-faq** vector store that Ava's *knowledge_base* tool searches.
In-memory stores reset when the server restarts: switch both store nodes to PGVector / Qdrant / Pinecone for production."""

AVA_SYSTEM = """You are Ava, the phone assistant of Acme Dental. You are speaking on a phone call:
- Reply in one to three short, natural sentences. No lists, markdown, emojis or URLs.
- Answer questions about services, prices, opening hours and policies with the knowledge_base tool; never guess.
- Use order_lookup when the caller gives an order or invoice number.
- Use call_control to transfer to "billing" (payments, invoices), "sales" (new treatments, quotes) or "human" when asked
  or when you can't help; tell the caller before transferring. When the caller says goodbye, say goodbye and hang up with call_control.
- For outbound calls (direction "outbound"), you are calling with the purpose in the context: confirm the appointment, offer to reschedule.
- Use the think tool to plan multi-step requests."""

TEMPLATES = {
    "voice-ai-agent-complete": {
        "name": "Voice AI Agent – Complete (phone, IVR, RAG, events, outbound)",
        "description": "Every voice feature in one workflow: fully configured LiveKit voice agent, keypad IVR, AI agent with memory, "
                       "knowledge base, API tool and call control (transfer / hang up), post-call AI summary + Slack alert, "
                       "voicemail handling and an outbound reminder campaign.",
        "tags": ["voice", "livekit", "ai", "multi-agent"],
        "nodes": [
            sticky("Overview", -80, -700, 620, 330, VOICE_OVERVIEW),
            sticky("Live call", 600, -700, 820, 230, VOICE_CALL_NOTE),

            # ① Live call ------------------------------------------------------------------------------------
            n("voice", "trigger.voice", "Phone line ⚠", 0, 0,
              agent_name="acme-support", phone_numbers="",
              greeting="Thanks for calling Acme Dental. I'm Ava, your virtual assistant. You can just talk to me, "
                       "press 0 to reach a person, or 9 to end the call.",
              greeting_mode="agent", greeting_interruptible=True,
              stt_provider="deepgram", stt_model="nova-3", language="en-US", detect_language=False,
              keyterms="Acme, Ava, invoice, refund, appointment, root canal, whitening",
              stt_smart_format=True, stt_profanity_filter=False,
              tts_provider="cartesia", tts_model="sonic-3", tts_voice="", tts_speed=1.0, tts_emotion="Content",
              filter_markdown=True,
              turn_detection="model", endpointing="dynamic", min_endpointing_delay=0.4, max_endpointing_delay=2.5,
              vad_activation_threshold=0.55, vad_min_silence=0.5, vad_min_speech=0.05,
              allow_interruptions=True, interruption_mode="adaptive", min_interruption_duration=0.5, min_interruption_words=1,
              resume_false_interruption=True, false_interruption_timeout=2.0,
              noise_cancellation="auto", ambient_sound="office", ambient_volume=0.15, thinking_sound="keyboard", thinking_volume=0.35,
              stream_tokens=True, filler_after=2.5, filler_phrases="One moment please.|Let me check that for you.|Just a second.",
              user_away_timeout=12, away_prompt="Are you still there?", away_hangup_after=2,
              goodbye_message="Thanks for calling Acme Dental. Goodbye!", max_call_minutes=20,
              dtmf_input="forward", dtmf_terminator="#", dtmf_timeout=2,
              amd="message", amd_message="Hi, this is Ava from Acme Dental calling about your appointment. "
                                         "Please call us back at your convenience. Thank you!",
              record_call=False, recording_bucket="", recording_path="calls/{time}-{room}.ogg", recording_dual_channel=True,
              recording_consent="This call may be recorded for quality and training purposes."),
            n("route", "flow.switch", "Keypad or speech?", 300, 0, fallback=True, all_matches=False, rules=[
                {"name": "0 → person", "left": "{{ $json.dtmf }}", "operator": "equals", "right": "0"},
                {"name": "9 → end call", "left": "{{ $json.dtmf }}", "operator": "equals", "right": "9"},
                {"name": "other keys → menu", "left": "{{ $json.input_type }}", "operator": "equals", "right": "dtmf"},
            ]),
            n("to_human", "voice.action", "Transfer to a person", 620, -300, action="transfer", transfer_to="+15555550100",
              play_dialtone=False, message="Sure, connecting you to a member of our team. Please hold."),
            n("hangup", "voice.action", "End call", 620, -170, action="hangup", message="Thanks for calling Acme Dental. Goodbye!"),
            n("menu", "voice.action", "Say the menu", 620, -40, action="say", interruptible=True,
              message="Press 0 to talk to a person, or 9 to end the call. Or just tell me what you need."),
            n("ava", "ai.agent", "Ava (support agent)", 640, 140, system_message=AVA_SYSTEM, max_iterations=6,
              prompt="Call direction: {{ $json.direction or 'inbound' }}. Caller: {{ $json.phone or 'web visitor' }}. "
                     "Context: {{ $json.context or 'none' }}.\nCaller said: {{ $json.text }}"),
            n("ava_model", "model.openai", "Agent model ⚠", 380, 420, **MODEL),
            n("ava_memory", "memory.buffer", "Call memory", 520, 420, session_key="{{ $json.session_id }}", window=10),
            n("clock", "tool.datetime", "Clock", 660, 420),
            n("control", "tool.voice_control", "Call control", 800, 420, tool_name="call_control", allow_any_number=False,
              transfer_targets=[{"name": "billing", "number": "+15555550101"}, {"name": "sales", "number": "+15555550102"},
                                {"name": "human", "number": "+15555550100"}]),
            n("kb_tool", "tool.vector_store", "Knowledge base", 940, 420, tool_name="knowledge_base", top_k=4,
              tool_description="Search Acme Dental's FAQ: services, prices, opening hours, insurance and policies."),
            n("kb_store", "vs.memory", "FAQ store", 940, 600, memory_key="acme-faq"),
            n("kb_embed", "embeddings.openai", "FAQ embeddings ⚠", 940, 760, model="text-embedding-3-small"),
            n("orders", "tool.http", "Order lookup", 1080, 420, tool_name="order_lookup", method="GET",
              url="https://api.example.com/orders/{order_id}",
              tool_description="Look up an order or invoice by its number: status, amount, due date.",
              parameters=[{"name": "order_id", "type": "string", "description": "Order or invoice number", "required": True,
                           "location": "path"}]),
            n("think", "tool.think", "Think", 1220, 420),

            # ② Call events ------------------------------------------------------------------------------------
            sticky("Call events", -80, 920, 760, 150, VOICE_EVENTS_NOTE),
            n("events", "trigger.voice_event", "Call events", 0, 1120, events="call_started,call_ended,amd,dtmf,transfer,error"),
            n("ev_route", "flow.switch", "Which event?", 300, 1120, fallback=True, all_matches=False, rules=[
                {"name": "call_started", "left": "{{ $json.event }}", "operator": "equals", "right": "call_started"},
                {"name": "call_ended", "left": "{{ $json.event }}", "operator": "equals", "right": "call_ended"},
                {"name": "voicemail", "left": "{{ $json.event }}", "operator": "equals", "right": "amd"},
                {"name": "transfer", "left": "{{ $json.event }}", "operator": "equals", "right": "transfer"},
            ]),
            n("started", "core.set", "Log call start", 620, 980, include_other_fields=True, fields=[
                {"name": "logged_at", "value": "{{ $now.isoformat() }}", "type": "string"},
                {"name": "caller", "value": "{{ $json.phone or $json.participant }}", "type": "string"}]),
            n("summary", "ai.llm", "Summarise call", 620, 1120, structured=True,
              system_message="You review phone calls of a dental clinic. Be factual and brief.",
              prompt="Call with {{ $json.phone or 'a web visitor' }} ({{ $json.direction }}), {{ $json.duration_s }} s, "
                     "ended because: {{ $json.reason }}.\nTranscript (JSON): {{ dumps($json.transcript) }}",
              output_fields=[
                  {"name": "summary", "type": "string", "description": "Two-sentence summary of the call", "required": True},
                  {"name": "sentiment", "type": "string", "description": "positive, neutral or negative", "required": True},
                  {"name": "follow_up", "type": "boolean", "description": "Does staff need to act?", "required": True},
                  {"name": "follow_up_reason", "type": "string", "description": "What staff should do, if anything", "required": False},
              ]),
            n("summary_model", "model.openai", "Summary model ⚠", 620, 1320, **MODEL),
            n("needs", "flow.if", "Needs follow-up?", 900, 1120,
              conditions=[{"left": "{{ $json.output.follow_up }}", "operator": "is_true", "right": ""}]),
            n("slack", "app.slack_api", "Alert the team ⚠", 1160, 1060, operation="message.post", channel="#front-desk",
              text="📞 Follow-up needed: call from {{ $('Call events').json.phone or 'web' }} "
                   "({{ $('Call events').json.duration_s }} s, {{ $json.output.sentiment }})\n"
                   "{{ $json.output.summary }}\n➡️ {{ $json.output.follow_up_reason }}"),
            n("no_follow", "core.noop", "Nothing to do", 1160, 1200),
            n("amd_log", "core.set", "Log voicemail", 620, 1460, include_other_fields=True, fields=[
                {"name": "voicemail", "value": "{{ $json.category }}", "type": "string"}]),
            n("xfer_log", "core.set", "Log transfer", 620, 1580, include_other_fields=True, fields=[
                {"name": "transferred_to", "value": "{{ $json.to }}", "type": "string"}]),
            n("other_ev", "core.noop", "Keypad / errors", 620, 1700),

            # ③ Outbound campaign --------------------------------------------------------------------------------
            sticky("Outbound", -80, 1880, 760, 150, VOICE_OUTBOUND_NOTE),
            n("schedule", "trigger.schedule", "Weekdays 10:00", 0, 2080, cron="0 10 * * 1-5", timezone="UTC"),
            n("patients", "core.code", "Patients to remind", 300, 2080, language="python", mode="all",
              code="# Replace with Google Sheets / Postgres. One item per call.\n"
                   "return [\n"
                   "    {'phone': '+15555550123', 'name': 'Jordan', 'appointment': 'tomorrow at 9:30'},\n"
                   "    {'phone': '+15555550124', 'name': 'Sam', 'appointment': 'Friday at 14:00'},\n"
                   "]\n"),
            n("dial", "voice.call", "Call patient ⚠", 600, 2080, phone="{{ $json.phone }}", agent_name="acme-support",
              context='{"purpose": "appointment reminder", "name": "{{ $json.name }}", "appointment": "{{ $json.appointment }}"}',
              ringing_timeout=30, max_call_duration=600, krisp=True, play_dialtone=False, wait_until_answered=False),

            # ④ Knowledge base --------------------------------------------------------------------------------
            sticky("Knowledge base", -80, 2300, 760, 130, VOICE_KB_NOTE),
            n("kb_run", "trigger.manual", "Load FAQ (run once)", 0, 2480),
            n("faq", "core.code", "FAQ documents", 300, 2480, language="python", mode="all",
              code="return [\n"
                   "    {'topic': 'hours', 'text': 'Acme Dental is open Monday to Friday 8:00-18:00 and Saturday 9:00-13:00. Closed on Sundays.'},\n"
                   "    {'topic': 'prices', 'text': 'A check-up with cleaning costs 89 dollars. Whitening costs 249 dollars. A root canal starts at 650 dollars.'},\n"
                   "    {'topic': 'insurance', 'text': 'We accept Delta Dental, Cigna, Aetna and MetLife. Bring your insurance card to the first visit.'},\n"
                   "    {'topic': 'cancellations', 'text': 'Appointments can be moved or cancelled free of charge up to 24 hours before. Later cancellations cost 30 dollars.'},\n"
                   "    {'topic': 'emergencies', 'text': 'For dental emergencies outside opening hours call +1 555 555 0199. For severe swelling or bleeding go to the ER.'},\n"
                   "]\n"),
            n("kb_insert", "rag.insert", "Store FAQ", 600, 2480, batch_size=200),
            n("kb_loader", "loader.default", "FAQ loader", 520, 2680, mode="expression", text="{{ $json.text }}",
              metadata=[{"name": "topic", "value": "{{ $json.topic }}"}]),
            n("kb_store2", "vs.memory", "FAQ store (write)", 700, 2680, memory_key="acme-faq"),
            n("kb_embed2", "embeddings.openai", "FAQ embeddings (write) ⚠", 700, 2840, model="text-embedding-3-small"),
        ],
        "edges": [
            e("voice", "route"),
            e("route", "to_human", "out0"), e("route", "hangup", "out1"), e("route", "menu", "out2"), e("route", "ava", "fallback"),
            e("ava_model", "ava", "ai_model", "ai_model"), e("ava_memory", "ava", "ai_memory", "ai_memory"),
            e("clock", "ava", "ai_tool", "ai_tool"), e("control", "ava", "ai_tool", "ai_tool"), e("kb_tool", "ava", "ai_tool", "ai_tool"),
            e("orders", "ava", "ai_tool", "ai_tool"), e("think", "ava", "ai_tool", "ai_tool"),
            e("kb_store", "kb_tool", "ai_vector_store", "ai_vector_store"), e("kb_embed", "kb_store", "ai_embedding", "ai_embedding"),

            e("events", "ev_route"),
            e("ev_route", "started", "out0"), e("ev_route", "summary", "out1"), e("ev_route", "amd_log", "out2"),
            e("ev_route", "xfer_log", "out3"), e("ev_route", "other_ev", "fallback"),
            e("summary_model", "summary", "ai_model", "ai_model"),
            e("summary", "needs"), e("needs", "slack", "true"), e("needs", "no_follow", "false"),

            e("schedule", "patients"), e("patients", "dial"),

            e("kb_run", "faq"), e("faq", "kb_insert"),
            e("kb_loader", "kb_insert", "ai_document", "ai_document"), e("kb_store2", "kb_insert", "ai_vector_store", "ai_vector_store"),
            e("kb_embed2", "kb_store2", "ai_embedding", "ai_embedding"),
        ],
    },
    "agentic-research-loop": {
        "name": "Agentic Research Loop (LangGraph state)",
        "description": "Writer agent with tools → critic → loop back until approved, with graph state, reducers, "
                       "a max-iterations guard and human approval (checkpoint + resume).",
        "tags": ["ai", "chat", "multi-agent", "hitl"],
        "settings": {
            "state": [
                {"key": "question", "reducer": "replace", "default": '""'},
                {"key": "draft", "reducer": "replace", "default": '""'},
                {"key": "critiques", "reducer": "append", "default": "[]"},
                {"key": "iteration", "reducer": "add", "default": "0"},
                {"key": "messages", "reducer": "messages", "default": "[]"},
            ],
            "variables": [{"key": "max_iterations", "value": "3"}],
        },
        "nodes": [
            n("note", "ui.sticky", "How it works", -40, -380, content=LOOP_NOTE),
            n("chat", "trigger.chat", "When chat message received", 0, 0),
            n("init", "flow.state", "Init State", 260, 0,
              updates=[{"key": "question", "value": "{{ $json.chatInput }}", "reducer": "", "type": "string"}]),
            n("writer", "ai.agent", "Writer Agent", 520, 0,
              prompt="Question: {{ $state.question }}\n\n"
                     "{{ 'Reviewer feedback to address:\\n- ' ~ ($state.critiques | join('\\n- ')) if $state.critiques else 'Write the first draft.' }}",
              system_message="You are a careful research writer. Check facts with your tools before answering. "
                             "Reply with the complete improved answer only, under 200 words.",
              state_messages="messages", output_state_key="draft", max_iterations=6),
            n("writer_model", "model.openai", "Writer Model", 400, 220, **MODEL),
            n("wiki", "tool.wikipedia", "Wikipedia", 540, 220),
            n("calc", "tool.calculator", "Calculator", 660, 220),
            n("clock", "tool.datetime", "Clock", 780, 220),
            n("count", "flow.state", "Count Iteration", 820, 0,
              updates=[{"key": "iteration", "value": "1", "reducer": "", "type": "number"}]),
            n("critic", "ai.llm", "Critic", 1080, 0,
              prompt="Question: {{ $state.question }}\n\nDraft (iteration {{ $state.iteration }}):\n{{ $state.draft }}",
              system_message="You are a strict reviewer. Set approved=true only if the draft answers the question "
                             "correctly, completely and clearly. Otherwise give one short, actionable piece of feedback.",
              structured=True, output_fields=[
                  {"name": "approved", "type": "boolean", "description": "Is the draft ready?", "required": True},
                  {"name": "score", "type": "number", "description": "Quality from 1 to 10", "required": True},
                  {"name": "feedback", "type": "string", "description": "What to improve next", "required": True},
              ]),
            n("critic_model", "model.openai", "Critic Model", 1080, 220, **MODEL),
            n("check", "flow.if", "Good enough?", 1340, 0, combine="or", conditions=[
                {"left": "{{ $json.output.approved }}", "operator": "is_true", "right": ""},
                {"left": "{{ $state.iteration }}", "operator": "gte", "right": "{{ $vars.max_iterations }}"},
            ]),
            n("save_critique", "flow.state", "Save Critique", 1340, 260,
              updates=[{"key": "critiques", "value": "{{ $json.output.feedback }}", "reducer": "", "type": "string"}]),
            n("review", "flow.approval", "Human Review", 1600, -80,
              message="Draft after {{ $state.iteration }} iteration(s), critic score {{ $json.output.score }}:\n\n{{ $state.draft }}",
              fields=[{"name": "feedback", "label": "Feedback for the writer (if rejecting)"}]),
            n("save_human", "flow.state", "Save Human Feedback", 1860, 120,
              updates=[{"key": "critiques", "value": "Human reviewer: {{ $json.approval.feedback or 'Not good enough yet, improve it.' }}",
                        "reducer": "", "type": "string"}]),
            n("final", "core.set", "Final Answer", 1860, -120, include_other_fields=False, fields=[
                {"name": "output", "value": "{{ $state.draft }}", "type": "string"},
                {"name": "iterations", "value": "{{ $state.iteration }}", "type": "number"},
                {"name": "critiques", "value": "{{ $state.critiques }}", "type": "auto"},
            ]),
        ],
        "edges": [
            e("chat", "init"), e("init", "writer"), e("writer", "count"), e("count", "critic"), e("critic", "check"),
            e("check", "review", "true"), e("check", "save_critique", "false"),
            e("save_critique", "writer"),  # loop back
            e("review", "final", "approved"), e("review", "save_human", "rejected"),
            e("save_human", "writer"),  # loop back after a human rejection
            e("writer_model", "writer", "ai_model", "ai_model"),
            e("wiki", "writer", "ai_tool", "ai_tool"), e("calc", "writer", "ai_tool", "ai_tool"),
            e("clock", "writer", "ai_tool", "ai_tool"),
            e("critic_model", "critic", "ai_model", "ai_model"),
        ],
    },
    "ai-chat-agent": {
        "name": "AI Chat Agent with Memory & Tools",
        "description": "Chat trigger → LangGraph tool-calling agent with Postgres memory, calculator, clock and Wikipedia.",
        "tags": ["ai", "chat"],
        "nodes": [
            n("chat", "trigger.chat", "When chat message received", 0, 0),
            n("agent", "ai.agent", "AI Agent", 320, 0, system_message="You are a helpful assistant. Use tools when useful."),
            n("model", "model.openai", "OpenAI Chat Model", 200, 220, **MODEL),
            n("mem", "memory.buffer", "Simple Memory", 360, 220),
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
            n("mem", "memory.buffer", "Memory (per phone)", 600, 280, session_key="{{ $json['from'] }}"),
            n("reply", "whatsapp.action", "Send reply", 820, 60, operation="text",
              to="{{ $('WhatsApp Trigger').json['from'] }}", text="{{ $json.output }}"),
        ],
        "edges": [
            e("wa", "only_text"), e("only_text", "read"), e("only_text", "agent"), e("agent", "reply"),
            e("model", "agent", "ai_model", "ai_model"), e("mem", "agent", "ai_memory", "ai_memory"),
        ],
    },
    "voice-receptionist": {
        "name": "Phone Receptionist (LiveKit + SIP)",
        "description": "A phone voice agent: noise cancellation, turn detection, an AI receptionist with memory that can transfer "
                       "or hang up (Call Control tool), keypad 0 → a human, and an AI summary of every finished call.",
        "tags": ["voice", "livekit", "ai"],
        "nodes": [
            n("voice", "trigger.voice", "Phone line", 0, 0, agent_name="debbuilder-voice",
              greeting="Hi, thanks for calling Acme Dental. How can I help?", noise_cancellation="auto", ambient_sound="office",
              thinking_sound="keyboard", user_away_timeout=12, away_prompt="Are you still there?", dtmf_input="forward"),
            n("pressed0", "flow.if", "Pressed 0?", 260, 0, conditions=[{"left": "{{ $json.dtmf }}", "operator": "equals", "right": "0"}]),
            n("human", "voice.action", "Transfer to front desk", 520, -140, action="transfer", transfer_to="+15555550100",
              message="Connecting you to our front desk now."),
            n("agent", "ai.agent", "Receptionist", 520, 60,
              system_message="You are a concise, warm phone receptionist for Acme Dental. Speak in short sentences; no markdown or lists. "
                             "Use call_control to transfer callers who ask for billing, sales or a human, and to hang up once they "
                             "say goodbye.",
              prompt="{{ $json.text }}"),
            n("model", "model.openai", "OpenAI Chat Model", 400, 280, **MODEL),
            n("mem", "memory.buffer", "Call memory", 540, 280, session_key="{{ $json.session_id }}"),
            n("clock", "tool.datetime", "Clock", 680, 280),
            n("control", "tool.voice_control", "Call control", 820, 280, transfer_targets=[
                {"name": "billing", "number": "+15555550101"}, {"name": "sales", "number": "+15555550102"},
                {"name": "human", "number": "+15555550100"}]),
            n("ended", "trigger.voice_event", "Call ended", 0, 520, events="call_ended"),
            n("summary", "ai.llm", "Summarize call", 260, 520,
              prompt="Summarise this phone call in 3 bullet points (reason, outcome, follow-up). Caller {{ $json.phone }}, "
                     "{{ $json.duration_s }} s. Transcript (JSON): {{ dumps($json.transcript) }}"),
            n("summary_model", "model.openai", "Summary model", 260, 720, **MODEL),
        ],
        "edges": [
            e("voice", "pressed0"), e("pressed0", "human", "true"), e("pressed0", "agent", "false"),
            e("model", "agent", "ai_model", "ai_model"), e("mem", "agent", "ai_memory", "ai_memory"),
            e("clock", "agent", "ai_tool", "ai_tool"), e("control", "agent", "ai_tool", "ai_tool"),
            e("ended", "summary"), e("summary_model", "summary", "ai_model", "ai_model"),
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
