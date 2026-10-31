"""Template: IoT incident response with multi-agent orchestration (Perception → Triage → Planning → Review → Response).

Kept in its own module because of its size; `templates.py` registers it. Every agent has a rule-based fallback on its
error output, so the workflow also completes (in a degraded mode) before any model credential is picked.
"""


def n(id, type, name, x, y, settings=None, **params):
    node = {"id": id, "type": type, "name": name, "position": {"x": x, "y": y}, "parameters": params}
    if settings:
        node["settings"] = settings
    return node


def e(src, tgt, sh="main", th="main"):
    return {"id": f"e_{src}_{sh}_{tgt}_{th}", "source": src, "sourceHandle": sh, "target": tgt, "targetHandle": th}


def sticky(id, x, y, w, h, content):
    return {**n(id, "ui.sticky", id, x, y, content=content), "width": w, "height": h}


# No preset model name: pick one from your provider's list in the editor.
MODEL = dict(model="")
# Agents retry transient model / tool failures, then hand over to their rule-based fallback.
AGENT_SETTINGS = {"retry_on_fail": True, "max_tries": 3, "wait_between_tries_ms": 1500, "on_error": "error_output"}

OVERVIEW = """## IoT incident response: multi-agent orchestration
One sensor alert (camera, asset tracker, SOS wearable or fire sensor) runs through five agents, each with one job:

| Agent | Role | Output (graph state) |
|---|---|---|
| **Perception** | Understands the raw signal: device lookup, sensor history, false-positive check | `$state.perception` |
| **Triage** | Severity P1–P4, risk to life, SLA, escalation | `$state.triage` |
| **Planning** | Step-by-step plan with teams, ETA and safety checks from the SOP | `$state.plan` |
| **Review** | Incident-commander checklist; approves or sends the plan back | `$state.review` |
| **Response** | Final response plan: actions, stakeholder messages, follow-ups | `$state.response` |

**Run it:** pick an OpenAI credential in the 5 model nodes, set `$vars.mock_alert` (Settings → Variables) to
`camera`, `asset`, `sos`, `fire` or `random`, then **Execute workflow**. Or POST a real alert to
`/webhook-test/iot-alert` (`/webhook/iot-alert` once published). Without a credential every agent falls back to its
rule-based twin, so the flow still completes: a good way to watch the state, loops and retries."""

RESILIENCE_NOTE = """### Retry, review and loop-backs
- **Retry:** every agent retries 3× (1.5 s apart); dispatch retries 3× on mock gateway errors.
- **Fallbacks:** after the last retry an agent's *error* output goes to a rule-based twin (dashed lane above).
- **Loop 1, perception:** confidence below `$vars.min_confidence` → feedback is appended to `$state.perception_feedback`
  and the Perception Agent tries again (max `$vars.max_perception_retries`), then an operator verifies the alert.
- **Loop 2, planning:** the Review Agent rejects → its required changes go to `$state.plan_feedback` and the planner
  revises (max `$vars.max_plan_revisions`), then a supervisor approves or sends it back again.
- **State** (Settings → State): one LangGraph channel per key, each with a reducer (`replace`, `append`, `add`);
  checkpointed, so it survives the human-approval pauses. `$state.audit` is the incident timeline."""

# ---- mock data ----------------------------------------------------------------------------------------------
INGEST_CODE = '''# Normalise the incoming alert. A POST to the webhook with a "device_id" is used as is;
# otherwise one of the mock alerts below is picked by $vars.mock_alert (camera | asset | sos | fire | random).
MOCK = {
    "camera": {"source": "camera", "device_id": "CAM-114", "site": "Warehouse B", "zone": "Dock 3 (restricted)",
               "event": "person_detected_restricted_zone",
               "payload": {"confidence": 0.91, "frames": 12, "objects": ["person", "forklift"], "time_of_day": "02:14",
                           "snapshot": "s3://cams/CAM-114/0214.jpg"}},
    "asset": {"source": "asset", "device_id": "TRK-2207", "site": "Yard North", "zone": "Geofence GF-12",
              "event": "geofence_exit",
              "payload": {"asset": "Diesel generator G-7 on trailer", "speed_kmh": 64, "battery_pct": 18,
                          "lat": 19.0896, "lng": 72.8656, "authorised_move": False}},
    "sos": {"source": "sos", "device_id": "WEAR-0381", "site": "Plant 2", "zone": "Boiler room",
            "event": "sos_button_pressed",
            "payload": {"worker": "R. Mehta", "heart_rate_bpm": 132, "fall_detected": True, "spo2_pct": 91,
                        "last_motion_s": 45}},
    "fire": {"source": "fire", "device_id": "FIR-0902", "site": "HQ Tower", "zone": "Server room L2",
             "event": "smoke_and_heat",
             "payload": {"smoke_ppm": 410, "temp_c": 78, "rate_of_rise_c_per_min": 9, "sprinkler": "armed",
                         "occupancy": 3}},
}
item = items[0] if items else {}
body = item.get("body") if isinstance(item.get("body"), dict) else {}
if body.get("device_id"):
    alert = dict(body)
    alert.setdefault("source", "unknown")
else:
    kind = str(vars.get("mock_alert") or "fire").lower()
    if kind == "random":
        import random
        kind = random.choice(list(MOCK))
    alert = json.loads(json.dumps(MOCK.get(kind, MOCK["fire"])))
alert.setdefault("alert_id", "ALR-" + str(execution_id)[:8].upper())
alert.setdefault("received_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
set_state("alert", alert)
set_state("audit", f"{alert['received_at']} INGEST {alert['source']} alert {alert['alert_id']} from {alert['device_id']} ({alert.get('event')})")
return [alert]
'''

DEVICE_TOOL = '''REGISTRY = {
    "CAM-114": {"type": "PTZ camera (AI analytics)", "site": "Warehouse B", "zone": "Dock 3 (restricted)", "criticality": "high",
                "owner_team": "Site security", "nearby": ["CAM-115", "DOOR-31"], "false_positive_rate_30d": 0.04},
    "TRK-2207": {"type": "GPS asset tracker", "site": "Yard North", "asset": "Diesel generator G-7", "asset_value_usd": 48000,
                 "criticality": "medium", "owner_team": "Fleet & assets", "false_positive_rate_30d": 0.10},
    "WEAR-0381": {"type": "Worker safety wearable", "site": "Plant 2", "assigned_to": "R. Mehta (boiler technician)",
                  "criticality": "critical", "owner_team": "EHS", "medical_notes": "none on file", "false_positive_rate_30d": 0.02},
    "FIR-0902": {"type": "Multi-sensor smoke/heat detector", "site": "HQ Tower", "zone": "Server room L2", "criticality": "critical",
                 "owner_team": "Facilities", "suppression": "FM-200 gas + sprinklers", "false_positive_rate_30d": 0.01},
}
return REGISTRY.get(str(device_id).strip().upper(), {"error": f"Unknown device {device_id}"})
'''

HISTORY_TOOL = '''HISTORY = {
    "CAM-114": ["01:50 no motion", "02:05 vehicle at gate (authorised)", "02:12 motion Dock 3", "02:14 person + forklift"],
    "TRK-2207": ["21:00 parked GF-12", "01:40 ignition on", "01:52 moving 22 km/h inside GF-12", "02:01 exit GF-12 at 64 km/h"],
    "WEAR-0381": ["02:00 HR 88", "02:08 HR 95", "02:10 fall detected", "02:10 SOS pressed", "02:11 no motion 45 s"],
    "FIR-0902": ["01:55 temp 24C smoke 3ppm", "02:05 temp 31C smoke 40ppm", "02:10 temp 58C smoke 220ppm", "02:12 temp 78C smoke 410ppm"],
}
return HISTORY.get(str(device_id).strip().upper(), [])
'''

SOP_TOOL = '''SOP = {
    "fire": "SOP-FIRE-01: 1) Confirm with a second sensor or camera 2) Trigger building alarm and evacuate the zone "
            "3) Call fire brigade (101) 4) Isolate power to affected racks 5) Let FM-200 discharge; no re-entry until cleared "
            "6) Notify Facilities head and IT on-call. SLA: response start within 5 min.",
    "sos": "SOP-SOS-02: 1) Call the worker via radio 2) Dispatch nearest first-aid responder 3) Call ambulance (108) if no "
           "response or vitals abnormal 4) Make the area safe (boiler shutdown if needed) 5) Notify EHS and the shift lead. SLA: 3 min.",
    "camera": "SOP-SEC-03: 1) Verify on live feed and neighbouring cameras 2) Dispatch security patrol 3) Lock down doors "
              "4) Call police (100) if intrusion confirmed 5) Preserve footage. SLA: 10 min.",
    "asset": "SOP-AST-04: 1) Check for an authorised work order 2) Call the assigned driver 3) Remote immobilise if "
             "unauthorised and safe 4) Alert local police with live GPS 5) Notify Fleet manager and insurer. SLA: 15 min.",
}
key = str(alert_type).strip().lower()
for k in SOP:
    if k in key:
        return SOP[k]
return "No SOP found: escalate to the duty manager."
'''

RESOURCES_TOOL = '''TEAMS = {
    "HQ Tower": [{"team": "Facilities engineer", "eta_min": 4}, {"team": "IT on-call", "eta_min": 8},
                 {"team": "Fire brigade (101)", "eta_min": 12}, {"team": "Floor wardens", "eta_min": 2}],
    "Plant 2": [{"team": "First-aid responder", "eta_min": 2}, {"team": "Shift lead", "eta_min": 3},
                {"team": "Ambulance (108)", "eta_min": 14}, {"team": "Boiler operator", "eta_min": 3}],
    "Warehouse B": [{"team": "Security patrol", "eta_min": 5}, {"team": "Police (100)", "eta_min": 18},
                    {"team": "Warehouse supervisor", "eta_min": 10}],
    "Yard North": [{"team": "Fleet manager", "eta_min": 20}, {"team": "Security patrol", "eta_min": 9},
                   {"team": "Police (100)", "eta_min": 15}, {"team": "Telematics (remote immobilise)", "eta_min": 1}],
}
for name, teams in TEAMS.items():
    if name.lower() in str(site).lower():
        return {"site": name, "available": teams}
return {"site": site, "available": [{"team": "Duty manager", "eta_min": 15}]}
'''

# ---- rule-based fallbacks (used when an agent fails after its retries) ---------------------------------------
FALLBACK_PERCEPTION = '''a = state.get("alert") or {}
p = a.get("payload") or {}
summary = {"fire": f"Smoke {p.get('smoke_ppm')} ppm and {p.get('temp_c')} C rising {p.get('rate_of_rise_c_per_min')} C/min",
           "sos": f"SOS from {p.get('worker')}; fall detected={p.get('fall_detected')}, HR {p.get('heart_rate_bpm')}",
           "camera": f"Camera saw {', '.join(p.get('objects', []))} in a restricted zone at {p.get('time_of_day')}",
           "asset": f"{p.get('asset')} left geofence at {p.get('speed_kmh')} km/h, authorised={p.get('authorised_move')}"}
perception = {"alert_type": a.get("source", "unknown"), "event": a.get("event", "unknown"),
              "location": f"{a.get('site')} / {a.get('zone')}", "confidence": 0.65,
              "false_positive_likely": False, "key_facts": summary.get(a.get("source"), str(p)),
              "method": "rule-based fallback"}
set_state("perception", perception)
set_state("audit", "PERCEPTION fallback (agent unavailable): " + perception["key_facts"])
return [{"output": perception}]
'''

FALLBACK_TRIAGE = '''a, per = state.get("alert") or {}, state.get("perception") or {}
kind = per.get("alert_type") or a.get("source")
rules = {"fire": ("P1", True, 5), "sos": ("P1", True, 3), "camera": ("P2", False, 10), "asset": ("P3", False, 15)}
sev, life, sla = rules.get(kind, ("P2", False, 15))
triage = {"severity": sev, "category": kind, "risk_to_life": life, "people_at_risk": "see perception",
          "assets_at_risk": a.get("zone", ""), "escalation_required": sev == "P1", "response_sla_minutes": sla,
          "rationale": f"Rule table for {kind} alerts (agent unavailable)"}
set_state("triage", triage)
set_state("audit", f"TRIAGE fallback: {sev}, SLA {sla} min")
return [{"output": triage}]
'''

FALLBACK_PLAN = '''a, tri = state.get("alert") or {}, state.get("triage") or {}
plan = {"plan_summary": f"Follow the {tri.get('category', 'standard')} SOP", "sop_reference": "per SOP library",
        "steps": "1) Verify the alert 2) Dispatch nearest responders 3) Make the area safe 4) Notify stakeholders 5) Monitor until closed",
        "resources": "Nearest on-site team + emergency services when risk_to_life",
        "eta_minutes": min(int(tri.get("response_sla_minutes") or 15), 10),
        "notifications": "Site lead, owner team, duty manager", "safety_checks": "Do not enter unsafe areas; wait for clearance"}
set_state("plan", plan)
set_state("audit", "PLANNING fallback: generic SOP plan")
return [{"output": plan}]
'''

FALLBACK_REVIEW = '''tri, plan = state.get("triage") or {}, state.get("plan") or {}
issues = []
if not plan.get("steps"):
    issues.append("plan has no steps")
sla = float(tri.get("response_sla_minutes") or 999)
if float(plan.get("eta_minutes") or 999) > sla:
    issues.append(f"ETA {plan.get('eta_minutes')} min exceeds the SLA of {sla:g} min")
if tri.get("risk_to_life") and "emergency" not in (str(plan.get("resources")) + str(plan.get("steps"))).lower() \\
        and not any(w in str(plan).lower() for w in ("ambulance", "fire brigade", "police", "first-aid", "evacuat")):
    issues.append("life-safety risk but no emergency service or evacuation step")
review = {"approved": not issues, "score": 8 if not issues else 4, "issues": "; ".join(issues) or "none",
          "required_changes": "; ".join(issues), "method": "rule-based fallback"}
set_state("review", review)
return [{"output": review}]
'''

FALLBACK_RESPONSE = '''a, tri, plan = state.get("alert") or {}, state.get("triage") or {}, state.get("plan") or {}
resp = {"incident_title": f"{tri.get('severity', 'P?')} {a.get('event', 'alert')} at {a.get('site')}",
        "actions": plan.get("steps", ""), "stakeholder_messages": f"{a.get('site')}: {a.get('event')} - response under way, ETA {plan.get('eta_minutes')} min.",
        "follow_ups": "Post-incident review within 24 h", "close_criteria": "Area declared safe by the responsible team"}
set_state("response", resp)
set_state("audit", "RESPONSE fallback: plan converted to actions")
return [{"output": resp}]
'''

PAGE_CODE = '''a, tri = state.get("alert") or {}, state.get("triage") or {}
page = {"channel": "pager + SMS (mock)", "to": ["duty-manager", "site-lead", "owner-team"],
        "message": f"[{tri.get('severity')}] {a.get('event')} at {a.get('site')}/{a.get('zone')} - planning response now",
        "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
set_state("audit", f"PAGE sent immediately for {tri.get('severity')}: {page['message']}")
return [{"page": page}]
'''

DISPATCH_CODE = '''# Mock executor: "sends" each notification / dispatch. Fails randomly at $vars.dispatch_failure_rate to show
# node retries (Settings → Retry on fail); after the last try the error output escalates to a human.
import random
rate = float(vars.get("dispatch_failure_rate") or 0)
if random.random() < rate:
    raise RuntimeError("Mock gateway timeout while dispatching (will be retried)")
resp, a = state.get("response") or {}, state.get("alert") or {}
actions = [s.strip(" .") for s in str(resp.get("actions", "")).replace("\\n", " ").split(")") if s.strip(" .")]
now = datetime.now(timezone.utc).isoformat(timespec="seconds")
receipts = [{"action": act[:-2].strip() if act[-2:-1].isdigit() else act, "status": "dispatched", "at": now} for act in actions[:10]]
set_state("audit", f"DISPATCH {len(receipts)} action(s) and stakeholder messages sent")
return [{"dispatch_status": "sent", "receipts": receipts, "message": resp.get("stakeholder_messages")}]
'''

PERCEPTION_SYSTEM = """You are the Perception Agent of an IoT security operations centre. Your only job is to understand the raw
sensor signal. Use device_registry and sensor_history before concluding. Identify the alert type (camera, asset, sos,
fire), the concrete event, the exact location, and how confident you are that it is real (0-1), using the device's
false-positive rate and the trend in its history. Do not triage or plan."""

TRIAGE_SYSTEM = """You are the Triage Agent. From the perception, decide severity: P1 = risk to life or major loss now,
P2 = serious, needs response within ~10 min, P3 = needs attention within the hour, P4 = informational. Look up the SOP
with sop_lookup to get the response SLA. Be decisive and explain briefly. Do not plan the response."""

PLANNING_SYSTEM = """You are the Planning Agent. Build a concrete, ordered response plan that follows the SOP (sop_lookup) and only
uses teams that resource_availability says are available at the site. Life safety first, then containment, then
property. The ETA must meet the triage SLA. If reviewer feedback is given, fix every point."""

REVIEW_SYSTEM = """You are the Review Agent (incident commander). Check the plan against this checklist: 1) every SOP step is
covered, 2) life-safety actions come first when risk_to_life is true, 3) only available teams are used, 4) ETA is within
the SLA, 5) the right stakeholders are notified, 6) safety checks for responders are present. Approve only if all pass;
otherwise list the precise required changes."""

RESPONSE_SYSTEM = """You are the Response Agent. Turn the approved plan into the final response plan that operators execute:
numbered actions with owners, short stakeholder messages (SMS-length), follow-ups and the criteria to close the incident."""

IOT_INCIDENT_TEMPLATE = {
    "name": "IoT Incident Response (multi-agent)",
    "description": "Camera, asset, SOS or fire sensor alert → Perception → Triage → Planning → Review → Response agents, "
                   "with LangGraph state, retries, rule-based fallbacks, review loop-backs and human approval. Mock data included.",
    "tags": ["ai", "multi-agent", "iot", "hitl"],
    "settings": {
        "state": [
            {"key": "alert", "reducer": "replace", "default": "{}"},
            {"key": "perception", "reducer": "replace", "default": "{}"},
            {"key": "triage", "reducer": "replace", "default": "{}"},
            {"key": "plan", "reducer": "replace", "default": "{}"},
            {"key": "review", "reducer": "replace", "default": "{}"},
            {"key": "response", "reducer": "replace", "default": "{}"},
            {"key": "perception_attempts", "reducer": "add", "default": "0"},
            {"key": "plan_attempts", "reducer": "add", "default": "0"},
            {"key": "perception_feedback", "reducer": "append", "default": "[]"},
            {"key": "plan_feedback", "reducer": "append", "default": "[]"},
            {"key": "audit", "reducer": "append", "default": "[]"},
        ],
        "variables": [
            {"key": "mock_alert", "value": "fire"},
            {"key": "min_confidence", "value": "0.6"},
            {"key": "max_perception_retries", "value": "2"},
            {"key": "max_plan_revisions", "value": "3"},
            {"key": "dispatch_failure_rate", "value": "0.3"},
        ],
    },
    "nodes": [
        sticky("Overview", -620, -760, 560, 470, OVERVIEW),
        sticky("Retry, review and loop-backs", 2300, -760, 600, 330, RESILIENCE_NOTE),

        # ---- ingest
        n("manual", "trigger.manual", "Run with mock alert", -560, 0),
        n("hook", "trigger.webhook", "IoT alert webhook", -560, 220, path="iot-alert", method="POST", respond="last_node"),
        n("ingest", "core.code", "Ingest alert (mock data)", -280, 100, language="python", mode="all", code=INGEST_CODE),

        # ---- 1. perception
        n("perceive", "ai.agent", "Perception Agent", 0, 100, settings=AGENT_SETTINGS,
          prompt="Raw IoT alert:\n{{ $state.alert | tojson }}\n\n"
                 "{{ 'Your previous reading was rejected: ' ~ ($state.perception_feedback | last) ~ '. Look again, using the tools.' "
                 "if $state.perception_feedback else 'Analyse this alert.' }}",
          system_message=PERCEPTION_SYSTEM, max_iterations=6, structured=True, output_state_key="perception",
          output_fields=[
              {"name": "alert_type", "type": "string", "description": "camera | asset | sos | fire", "required": True},
              {"name": "event", "type": "string", "description": "What is happening, in a few words", "required": True},
              {"name": "location", "type": "string", "description": "Site / zone", "required": True},
              {"name": "confidence", "type": "number", "description": "0-1: how sure the alert is real", "required": True},
              {"name": "false_positive_likely", "type": "boolean", "description": "Probably a false alarm?", "required": True},
              {"name": "key_facts", "type": "string", "description": "Readings and evidence that matter", "required": True},
          ]),
        n("m_perceive", "model.openai", "Perception model", -120, 330, **MODEL),
        n("t_device", "tool.code", "device_registry", 20, 330, tool_description="Look up a device: type, site, zone, owner team, criticality, false-positive rate.",
          parameters=[{"name": "device_id", "type": "string", "description": "Device id, e.g. FIR-0902", "required": True}], code=DEVICE_TOOL),
        n("t_history", "tool.code", "sensor_history", 160, 330, tool_description="Recent readings of a device (oldest first).",
          parameters=[{"name": "device_id", "type": "string", "description": "Device id", "required": True}], code=HISTORY_TOOL),
        n("fb_perceive", "core.code", "Rule-based perception (fallback)", 0, -200, language="python", mode="all", code=FALLBACK_PERCEPTION),
        n("count_p", "flow.state", "Count perception attempt", 280, 100, updates=[
            {"key": "perception_attempts", "value": "1", "reducer": "", "type": "number"},
            {"key": "audit", "value": "PERCEPTION #{{ $state.perception_attempts + 1 }}: {{ $state.perception.event }} at {{ $state.perception.location }} "
                                      "(confidence {{ $state.perception.confidence }})", "reducer": "", "type": "string"},
        ]),
        n("check_p", "flow.if", "Perception confident?", 540, 100, combine="and", conditions=[
            {"left": "{{ $state.perception.confidence }}", "operator": "gte", "right": "{{ $vars.min_confidence }}"},
            {"left": "{{ $state.perception.event }}", "operator": "is_not_empty", "right": ""},
        ]),
        n("retry_p", "flow.if", "Retry perception?", 540, 380, conditions=[
            {"left": "{{ $state.perception_attempts }}", "operator": "lt", "right": "{{ $vars.max_perception_retries }}"},
        ]),
        n("save_pfb", "flow.state", "Save perception feedback", 280, 520, updates=[
            {"key": "perception_feedback", "value": "confidence {{ $state.perception.confidence }} is below {{ $vars.min_confidence }}; "
                                                    "check device history and false-positive rate", "reducer": "", "type": "string"},
        ]),
        n("verify", "flow.approval", "Operator: verify alert", 800, 380,
          message="Perception is unsure (confidence {{ $state.perception.confidence }} after {{ $state.perception_attempts }} attempt(s)).\n"
                  "{{ $state.perception.key_facts }}\n\nApprove if the alert is real; reject to close it as a false alarm."),
        n("false_alarm", "core.set", "Closed: false alarm", 1060, 520, include_other_fields=False, fields=[
            {"name": "status", "value": "closed_false_alarm", "type": "string"},
            {"name": "alert", "value": "{{ $state.alert }}", "type": "auto"},
            {"name": "audit", "value": "{{ $state.audit }}", "type": "auto"},
        ]),

        # ---- 2. triage
        n("triage", "ai.agent", "Triage Agent", 800, 100, settings=AGENT_SETTINGS,
          prompt="Alert: {{ $state.alert | tojson }}\n\nPerception: {{ $state.perception | tojson }}",
          system_message=TRIAGE_SYSTEM, max_iterations=4, structured=True, output_state_key="triage",
          output_fields=[
              {"name": "severity", "type": "string", "description": "P1 | P2 | P3 | P4", "required": True},
              {"name": "category", "type": "string", "description": "fire | medical/sos | intrusion | asset theft | other", "required": True},
              {"name": "risk_to_life", "type": "boolean", "description": "Anyone in danger?", "required": True},
              {"name": "people_at_risk", "type": "string", "description": "Who, how many", "required": True},
              {"name": "assets_at_risk", "type": "string", "description": "What property / systems", "required": True},
              {"name": "escalation_required", "type": "boolean", "description": "Page management now?", "required": True},
              {"name": "response_sla_minutes", "type": "number", "description": "From the SOP", "required": True},
              {"name": "rationale", "type": "string", "description": "One or two sentences", "required": True},
          ]),
        n("m_triage", "model.openai", "Triage model", 700, 330, **MODEL),
        n("t_sop1", "tool.code", "sop_lookup", 840, 330, tool_description="Standard operating procedure (steps and SLA) for an alert type: fire, sos, camera or asset.",
          parameters=[{"name": "alert_type", "type": "string", "description": "fire | sos | camera | asset", "required": True}], code=SOP_TOOL),
        n("fb_triage", "core.code", "Rule-based triage (fallback)", 800, -200, language="python", mode="all", code=FALLBACK_TRIAGE),
        n("log_t", "flow.state", "Log triage", 1060, 100, updates=[
            {"key": "audit", "value": "TRIAGE {{ $state.triage.severity }} ({{ $state.triage.category }}), risk to life: {{ $state.triage.risk_to_life }}, "
                                      "SLA {{ $state.triage.response_sla_minutes }} min", "reducer": "", "type": "string"},
        ]),
        n("route", "flow.switch", "Severity router", 1300, 100, rules=[
            {"name": "P1 critical", "left": "{{ $state.triage.severity }}", "operator": "equals", "right": "P1"},
        ], fallback=True),
        n("page", "core.code", "Page on-call now (P1)", 1540, -60, language="python", mode="all", code=PAGE_CODE),

        # ---- 3. planning
        n("plan", "ai.agent", "Planning Agent", 1800, 100, settings=AGENT_SETTINGS,
          prompt="Alert: {{ $state.alert | tojson }}\nPerception: {{ $state.perception | tojson }}\nTriage: {{ $state.triage | tojson }}\n\n"
                 "{{ 'Reviewer feedback to fix (revision ' ~ ($state.plan_attempts + 1) ~ '):\\n- ' ~ ($state.plan_feedback | join('\\n- ')) "
                 "if $state.plan_feedback else 'Write the first plan.' }}",
          system_message=PLANNING_SYSTEM, max_iterations=6, structured=True, output_state_key="plan",
          output_fields=[
              {"name": "plan_summary", "type": "string", "description": "One sentence", "required": True},
              {"name": "sop_reference", "type": "string", "description": "SOP id followed", "required": True},
              {"name": "steps", "type": "string", "description": "Numbered steps: 1) ... 2) ... with owner team", "required": True},
              {"name": "resources", "type": "string", "description": "Teams dispatched with their ETA", "required": True},
              {"name": "eta_minutes", "type": "number", "description": "When the first responder is on scene", "required": True},
              {"name": "notifications", "type": "string", "description": "Who is informed, how", "required": True},
              {"name": "safety_checks", "type": "string", "description": "Responder safety", "required": True},
          ]),
        n("m_plan", "model.openai", "Planning model", 1680, 330, **MODEL),
        n("t_sop2", "tool.code", "sop_lookup ", 1820, 330, tool_name="sop_lookup",
          tool_description="Standard operating procedure (steps and SLA) for an alert type: fire, sos, camera or asset.",
          parameters=[{"name": "alert_type", "type": "string", "description": "fire | sos | camera | asset", "required": True}], code=SOP_TOOL),
        n("t_res", "tool.code", "resource_availability", 1960, 330, tool_description="Teams available at a site right now, with ETA in minutes.",
          parameters=[{"name": "site", "type": "string", "description": "Site name, e.g. HQ Tower", "required": True}], code=RESOURCES_TOOL),
        n("fb_plan", "core.code", "Rule-based plan (fallback)", 1800, -200, language="python", mode="all", code=FALLBACK_PLAN),
        n("count_plan", "flow.state", "Count plan revision", 2060, 100, updates=[
            {"key": "plan_attempts", "value": "1", "reducer": "", "type": "number"},
            {"key": "audit", "value": "PLAN v{{ $state.plan_attempts + 1 }}: {{ $state.plan.plan_summary }} (ETA {{ $state.plan.eta_minutes }} min)",
             "reducer": "", "type": "string"},
        ]),

        # ---- 4. review
        n("review", "ai.llm", "Review Agent", 2320, 100, settings=AGENT_SETTINGS,
          prompt="Triage: {{ $state.triage | tojson }}\n\nPlan (revision {{ $state.plan_attempts }}): {{ $state.plan | tojson }}",
          system_message=REVIEW_SYSTEM, structured=True, output_fields=[
              {"name": "approved", "type": "boolean", "description": "All checklist items pass", "required": True},
              {"name": "score", "type": "number", "description": "1-10", "required": True},
              {"name": "issues", "type": "string", "description": "Failed checklist items, or 'none'", "required": True},
              {"name": "required_changes", "type": "string", "description": "Exact changes the planner must make", "required": True},
          ]),
        n("m_review", "model.openai", "Review model", 2320, 330, **MODEL),
        n("fb_review", "core.code", "Rule-based review (fallback)", 2320, -200, language="python", mode="all", code=FALLBACK_REVIEW),
        n("check_r", "flow.if", "Plan approved?", 2580, 100, conditions=[
            {"left": "{{ $json.output.approved }}", "operator": "is_true", "right": ""},
        ]),
        n("retry_r", "flow.if", "Revise again?", 2580, 380, conditions=[
            {"left": "{{ $state.plan_attempts }}", "operator": "lt", "right": "{{ $vars.max_plan_revisions }}"},
        ]),
        n("save_rfb", "flow.state", "Save review feedback", 2320, 560, updates=[
            {"key": "plan_feedback", "value": "{{ $json.output.required_changes or $json.output.issues }}", "reducer": "", "type": "string"},
            {"key": "audit", "value": "REVIEW rejected plan v{{ $state.plan_attempts }} (score {{ $json.output.score }}): {{ $json.output.issues }}",
             "reducer": "", "type": "string"},
        ]),
        n("supervisor", "flow.approval", "Supervisor: approve plan", 2840, 380,
          message="The plan was revised {{ $state.plan_attempts }} times and still has open review points:\n{{ $json.output.issues }}\n\n"
                  "Plan:\n{{ $state.plan.steps }}\n\nApprove to execute it, or reject with instructions for the planner.",
          fields=[{"name": "instructions", "label": "Instructions for the planner (if rejecting)"}]),
        n("save_sup", "flow.state", "Save supervisor feedback", 2840, 620, updates=[
            {"key": "plan_feedback", "value": "Supervisor: {{ $json.approval.instructions or 'Improve the plan.' }}", "reducer": "", "type": "string"},
        ]),

        # ---- 5. response
        n("respond", "ai.agent", "Response Agent", 3100, 100, settings=AGENT_SETTINGS,
          prompt="Alert: {{ $state.alert | tojson }}\nTriage: {{ $state.triage | tojson }}\nApproved plan: {{ $state.plan | tojson }}",
          system_message=RESPONSE_SYSTEM, max_iterations=3, structured=True, output_state_key="response",
          output_fields=[
              {"name": "incident_title", "type": "string", "description": "Short title with severity", "required": True},
              {"name": "actions", "type": "string", "description": "Numbered actions 1) ... with owner", "required": True},
              {"name": "stakeholder_messages", "type": "string", "description": "SMS-length messages per audience", "required": True},
              {"name": "follow_ups", "type": "string", "description": "After the incident", "required": True},
              {"name": "close_criteria", "type": "string", "description": "When to close", "required": True},
          ]),
        n("m_respond", "model.openai", "Response model", 3100, 330, **MODEL),
        n("fb_respond", "core.code", "Rule-based response (fallback)", 3100, -200, language="python", mode="all", code=FALLBACK_RESPONSE),
        n("log_resp", "flow.state", "Log response", 3360, 100, updates=[
            {"key": "audit", "value": "RESPONSE ready: {{ $state.response.incident_title }}", "reducer": "", "type": "string"},
        ]),
        n("dispatch", "core.code", "Dispatch actions (mock)", 3620, 100, language="python", mode="all", code=DISPATCH_CODE,
          settings={"retry_on_fail": True, "max_tries": 3, "wait_between_tries_ms": 1000, "on_error": "error_output"}),
        n("dispatch_failed", "core.set", "Dispatch failed: manual escalation", 3620, 360, include_other_fields=False, fields=[
            {"name": "dispatch_status", "value": "failed_escalated_to_operator", "type": "string"},
            {"name": "error", "value": "{{ $json.error }}", "type": "string"},
        ]),
        n("report", "core.set", "Incident report", 3880, 100, include_other_fields=False, fields=[
            {"name": "incident_id", "value": "{{ $state.alert.alert_id }}", "type": "string"},
            {"name": "title", "value": "{{ $state.response.incident_title }}", "type": "string"},
            {"name": "severity", "value": "{{ $state.triage.severity }}", "type": "string"},
            {"name": "dispatch_status", "value": "{{ $json.dispatch_status }}", "type": "string"},
            {"name": "perception", "value": "{{ $state.perception }}", "type": "auto"},
            {"name": "triage", "value": "{{ $state.triage }}", "type": "auto"},
            {"name": "plan", "value": "{{ $state.plan }}", "type": "auto"},
            {"name": "response", "value": "{{ $state.response }}", "type": "auto"},
            {"name": "perception_attempts", "value": "{{ $state.perception_attempts }}", "type": "number"},
            {"name": "plan_revisions", "value": "{{ $state.plan_attempts }}", "type": "number"},
            {"name": "review_feedback", "value": "{{ $state.plan_feedback }}", "type": "auto"},
            {"name": "timeline", "value": "{{ $state.audit }}", "type": "auto"},
        ]),
    ],
    "edges": [
        e("manual", "ingest"), e("hook", "ingest"), e("ingest", "perceive"),
        # perception (+ retry loop, fallback, human verification)
        e("perceive", "count_p"), e("perceive", "fb_perceive", "error"), e("fb_perceive", "count_p"),
        e("count_p", "check_p"), e("check_p", "triage", "true"), e("check_p", "retry_p", "false"),
        e("retry_p", "save_pfb", "true"), e("save_pfb", "perceive"),  # loop back
        e("retry_p", "verify", "false"), e("verify", "triage", "approved"), e("verify", "false_alarm", "rejected"),
        e("m_perceive", "perceive", "ai_model", "ai_model"),
        e("t_device", "perceive", "ai_tool", "ai_tool"), e("t_history", "perceive", "ai_tool", "ai_tool"),
        # triage
        e("triage", "log_t"), e("triage", "fb_triage", "error"), e("fb_triage", "log_t"), e("log_t", "route"),
        e("route", "page", "out0"), e("route", "plan", "fallback"), e("page", "plan"),
        e("m_triage", "triage", "ai_model", "ai_model"), e("t_sop1", "triage", "ai_tool", "ai_tool"),
        # planning + review loop
        e("plan", "count_plan"), e("plan", "fb_plan", "error"), e("fb_plan", "count_plan"), e("count_plan", "review"),
        e("review", "check_r"), e("review", "fb_review", "error"), e("fb_review", "check_r"),
        e("check_r", "respond", "true"), e("check_r", "retry_r", "false"),
        e("retry_r", "save_rfb", "true"), e("save_rfb", "plan"),  # loop back
        e("retry_r", "supervisor", "false"), e("supervisor", "respond", "approved"), e("supervisor", "save_sup", "rejected"),
        e("save_sup", "plan"),  # loop back after a supervisor rejection
        e("m_plan", "plan", "ai_model", "ai_model"), e("t_sop2", "plan", "ai_tool", "ai_tool"), e("t_res", "plan", "ai_tool", "ai_tool"),
        e("m_review", "review", "ai_model", "ai_model"),
        # response + dispatch
        e("respond", "log_resp"), e("respond", "fb_respond", "error"), e("fb_respond", "log_resp"),
        e("log_resp", "dispatch"), e("dispatch", "report"), e("dispatch", "dispatch_failed", "error"), e("dispatch_failed", "report"),
        e("m_respond", "respond", "ai_model", "ai_model"),
    ],
}
