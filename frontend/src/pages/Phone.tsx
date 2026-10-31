import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { confirmDialog, errText } from '../components/Dialogs'
import { Select } from '../components/Select'
import { useAuth } from '../auth'
import { Icon, toast } from '../lib'
import type { Credential, DispatchRule, SipTrunk, Telephony, Workflow } from '../types'
import '../pages.css'  // styles for pages loaded on demand

const DEFAULT_AGENT = 'debbuilder-voice'

/**
 * Telephony for voice agents: LiveKit SIP trunks (inbound / outbound), dispatch rules (which agent answers),
 * and a one-click Plivo setup. Everything here is also available as the LiveKit Telephony and Plivo nodes.
 */
export default function Phone() {
  const canWrite = useAuth().can('credential:write')
  const [creds, setCreds] = useState<Credential[]>([])
  const [credId, setCredId] = useState('')
  const [data, setData] = useState<Telephony | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [workflows, setWorkflows] = useState<Workflow[]>([])
  const [form, setForm] = useState<'inbound' | 'outbound' | 'rule' | null>(null)

  useEffect(() => {
    api.credentials('livekit,plivo').then((cs) => {
      setCreds(cs)
      const lk = cs.find((c) => c.type === 'livekit')
      if (lk) setCredId((cur) => cur || lk.id)
    })
    api.workflows().then((ws) => setWorkflows(ws.filter((w) => (w.trigger_types ?? []).includes('trigger.voice'))))
  }, [])
  const load = async () => {
    if (!credId) return
    setLoading(true); setError('')
    try { setData(await api.telephony(credId)) } catch (e) { setData(null); setError(errText(e)) } finally { setLoading(false) }
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load() }, [credId])

  const lkCreds = creds.filter((c) => c.type === 'livekit')
  const plivoCreds = creds.filter((c) => c.type === 'plivo')
  const wfName = (id?: string) => workflows.find((w) => w.id === id)?.name
  const remove = async (kind: 'trunk' | 'rule', id: string, label: string) => {
    if (!await confirmDialog({ title: `Delete ${label}?`, message: 'Calls using it stop working immediately.', confirmLabel: 'Delete', danger: true })) return
    try {
      await (kind === 'trunk' ? api.deleteTrunk(credId, id) : api.deleteDispatchRule(credId, id))
      toast('Deleted'); load()
    } catch (e) { toast(errText(e), 'error') }
  }

  return (
    <div className="page">
      <div className="page-inner">
        <div className="page-head">
          <h1>Phone</h1>
          {lkCreds.length > 0 && (
            <Select style={{ width: 260 }} value={credId} onChange={setCredId} title="LiveKit project"
              options={lkCreds.map((c) => ({ value: c.id, label: c.name, hint: 'LiveKit' }))} />
          )}
          <button className="btn" onClick={load} disabled={!credId || loading} title="Reload"><Icon name="refresh-cw" size={15} className={loading ? 'spin' : ''} /></button>
        </div>
        <p className="page-sub"><Icon name="phone" size={14} /> Phone numbers and SIP trunks for your voice agents. Calls arrive on an <b>inbound trunk</b>,
          a <b>dispatch rule</b> sends each caller to an agent, and agents call out through an <b>outbound trunk</b>.</p>

        {lkCreds.length === 0 ? (
          <div className="card empty">
            <div className="empty-art"><Icon name="phone" size={28} /></div>
            <h3>Add a LiveKit credential first</h3>
            <p>Voice agents run on LiveKit. Add your project URL, API key and secret (and its SIP URI for phone calls).</p>
            <Link className="btn primary" to="/credentials">Go to credentials</Link>
          </div>
        ) : (
          <>
            {error && <div className="error-box" style={{ marginBottom: 12 }}>{error}</div>}
            {data && !data.sip_uri && (
              <div className="card phone-note"><Icon name="info" size={15} /> Set the <b>SIP URI</b> on this LiveKit credential (LiveKit Cloud → Settings → SIP URI)
                so carriers like Plivo can send calls to your agents.</div>
            )}
            {data?.sip_uri && <p className="muted small">SIP URI: <code>{data.sip_uri}</code> (point your carrier's inbound/origination here)</p>}

            {canWrite && plivoCreds.length > 0 && data && (
              <PlivoWizard livekit={credId} plivoCreds={plivoCreds} workflows={workflows} onDone={load} />
            )}
            {canWrite && plivoCreds.length === 0 && (
              <div className="card phone-note"><Icon name="sparkles" size={15} /> Using <b>Plivo</b>? Add a Plivo credential to connect a Plivo number to an agent in one click.</div>
            )}

            <Block title="Inbound trunks" icon="phone-incoming" hint="Numbers that can receive calls."
              action={canWrite && <button className="btn sm" onClick={() => setForm(form === 'inbound' ? null : 'inbound')}><Icon name="plus" size={14} /> Inbound trunk</button>}>
              {form === 'inbound' && <InboundForm onSubmit={async (b) => { await api.addInboundTrunk(credId, b); toast('Inbound trunk created'); setForm(null); load() }} />}
              <Rows empty="No inbound trunks" items={data?.inbound ?? []} render={(t: SipTrunk) => (
                <TrunkRow key={t.sip_trunk_id} t={t} sub={(t.numbers ?? []).join(', ') || 'any number'} onDelete={canWrite ? () => remove('trunk', t.sip_trunk_id, t.name || t.sip_trunk_id) : undefined} />
              )} />
            </Block>

            <Block title="Dispatch rules" icon="route" hint="Which agent answers incoming calls (a new room per caller)."
              action={canWrite && <button className="btn sm" onClick={() => setForm(form === 'rule' ? null : 'rule')}><Icon name="plus" size={14} /> Dispatch rule</button>}>
              {form === 'rule' && <RuleForm trunks={data?.inbound ?? []} workflows={workflows}
                onSubmit={async (b) => { await api.addDispatchRule(credId, b); toast('Dispatch rule created'); setForm(null); load() }} />}
              <Rows empty="No dispatch rules: inbound calls won't reach an agent" items={data?.rules ?? []} render={(r: DispatchRule) => {
                const agents = r.room_config?.agents ?? []
                const kind = Object.keys(r.rule ?? {})[0]?.replace('dispatch_rule_', '') ?? '?'
                const wf = agents.map((a) => { try { return JSON.parse(a.metadata || '{}').workflow_id } catch { return undefined } }).find(Boolean)
                return (
                  <div key={r.sip_dispatch_rule_id} className="list-item static">
                    <div className="mini-icon" style={{ '--nc': '#ef4444' } as React.CSSProperties}><Icon name="route" size={15} /></div>
                    <div className="grow">
                      <div className="title">{r.name || r.sip_dispatch_rule_id}</div>
                      <div className="muted small">
                        agent <b>{agents.map((a) => a.agent_name || DEFAULT_AGENT).join(', ') || '— none —'}</b>{wf ? <> → {wfName(wf) ?? wf}</> : ' (routed by number)'}
                        {' · '}{kind} rooms{r.trunk_ids?.length ? ` · trunks ${r.trunk_ids.join(', ')}` : ' · all trunks'}{r.krisp_enabled ? ' · Krisp' : ''}
                      </div>
                    </div>
                    <code className="muted small hide-sm">{r.sip_dispatch_rule_id}</code>
                    {canWrite && <button className="btn ghost sm icon danger" title="Delete" onClick={() => remove('rule', r.sip_dispatch_rule_id, r.name || 'rule')}><Icon name="trash-2" size={15} /></button>}
                  </div>
                )
              }} />
            </Block>

            <Block title="Outbound trunks" icon="phone-outgoing" hint="How agents place calls (Make Phone Call node, outbound campaigns)."
              action={canWrite && <button className="btn sm" onClick={() => setForm(form === 'outbound' ? null : 'outbound')}><Icon name="plus" size={14} /> Outbound trunk</button>}>
              {form === 'outbound' && <OutboundForm onSubmit={async (b) => { await api.addOutboundTrunk(credId, b); toast('Outbound trunk created'); setForm(null); load() }} />}
              <Rows empty="No outbound trunks" items={data?.outbound ?? []} render={(t: SipTrunk) => (
                <TrunkRow key={t.sip_trunk_id} t={t} sub={`${t.address ?? ''}${t.numbers?.length ? ` · from ${t.numbers.join(', ')}` : ''}`}
                  onDelete={canWrite ? () => remove('trunk', t.sip_trunk_id, t.name || t.sip_trunk_id) : undefined} />
              )} />
            </Block>
            <p className="muted small">Run the voice worker with the agent name you use here: <code>AGENT_NAME=&lt;name&gt; uv run --extra voice python voice/worker.py start</code>
              {' '}(the default worker answers as <code>{DEFAULT_AGENT}</code>).</p>
          </>
        )}
      </div>
    </div>
  )
}

function Block({ title, icon, hint, action, children }: { title: string; icon: string; hint: string; action?: ReactNode; children: ReactNode }) {
  return (
    <section className="phone-block">
      <div className="section-title"><Icon name={icon} size={17} /> {title}<span className="muted small" style={{ fontWeight: 400 }}>{hint}</span><span className="spacer" />{action}</div>
      {children}
    </section>
  )
}

function Rows<T>({ items, render, empty }: { items: T[]; render: (t: T) => ReactNode; empty: string }) {
  return items.length ? <div className="list">{items.map(render)}</div> : <div className="muted small phone-empty">{empty}</div>
}

function TrunkRow({ t, sub, onDelete }: { t: SipTrunk; sub: string; onDelete?: () => void }) {
  return (
    <div className="list-item static">
      <div className="mini-icon" style={{ '--nc': '#ef4444' } as React.CSSProperties}><Icon name="phone" size={15} /></div>
      <div className="grow"><div className="title">{t.name || t.sip_trunk_id}</div><div className="muted small">{sub}{t.krisp_enabled ? ' · Krisp' : ''}</div></div>
      <code className="muted small hide-sm">{t.sip_trunk_id}</code>
      {onDelete && <button className="btn ghost sm icon danger" title="Delete" onClick={onDelete}><Icon name="trash-2" size={15} /></button>}
    </div>
  )
}

/** Small form helper: labelled inputs in a wrapping row, submit with errors shown inline. */
function FormCard({ onSubmit, children, submit }: { onSubmit: () => Promise<void>; children: ReactNode; submit: string }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const go = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true); setErr('')
    try { await onSubmit() } catch (x) { setErr(errText(x)) } finally { setBusy(false) }
  }
  return (
    <form className="card phone-form" onSubmit={go}>
      <div className="phone-grid">{children}</div>
      {err && <div className="error-box small">{err}</div>}
      <div className="row"><span className="spacer" /><button className="btn primary" disabled={busy}>{busy ? 'Working…' : submit}</button></div>
    </form>
  )
}

function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return <label className="field"><span className="phone-label">{label}</span>{children}{hint && <span className="hint">{hint}</span>}</label>
}

function useForm<T extends Record<string, unknown>>(init: T) {
  const [v, setV] = useState(init)
  const bind = (k: keyof T) => ({
    value: String(v[k] ?? ''),
    onChange: (e: React.ChangeEvent<HTMLInputElement>) => setV((s) => ({ ...s, [k]: e.target.type === 'checkbox' ? e.target.checked : e.target.value })),
  })
  const check = (k: keyof T) => ({ checked: !!v[k], onChange: (e: React.ChangeEvent<HTMLInputElement>) => setV((s) => ({ ...s, [k]: e.target.checked })) })
  return { v, setV, bind, check }
}

function InboundForm({ onSubmit }: { onSubmit: (b: Record<string, unknown>) => Promise<void> }) {
  const f = useForm({ name: 'Inbound', numbers: '', allowed_addresses: '', auth_username: '', auth_password: '', krisp: true })
  return (
    <FormCard submit="Create inbound trunk" onSubmit={() => onSubmit(f.v)}>
      <Field label="Name"><input className="input" required {...f.bind('name')} /></Field>
      <Field label="Phone numbers" hint="E.164, comma separated"><input className="input" placeholder="+14155550100" {...f.bind('numbers')} /></Field>
      <Field label="Allowed carrier IPs" hint="Optional: only accept calls from these"><input className="input" {...f.bind('allowed_addresses')} /></Field>
      <Field label="Auth username (optional)"><input className="input" {...f.bind('auth_username')} /></Field>
      <Field label="Auth password"><input className="input" type="password" {...f.bind('auth_password')} /></Field>
      <label className="row small"><input type="checkbox" {...f.check('krisp')} /> Krisp noise cancellation</label>
    </FormCard>
  )
}

function OutboundForm({ onSubmit }: { onSubmit: (b: Record<string, unknown>) => Promise<void> }) {
  const f = useForm({ name: 'Outbound', address: '', numbers: '', auth_username: '', auth_password: '', transport: 'auto' })
  return (
    <FormCard submit="Create outbound trunk" onSubmit={() => onSubmit(f.v)}>
      <Field label="Name"><input className="input" required {...f.bind('name')} /></Field>
      <Field label="Carrier SIP address" hint="e.g. 1234.zt.plivo.com, example.pstn.twilio.com"><input className="input" required {...f.bind('address')} /></Field>
      <Field label="Caller ID numbers" hint="Numbers you own at the carrier"><input className="input" {...f.bind('numbers')} /></Field>
      <Field label="Auth username"><input className="input" {...f.bind('auth_username')} /></Field>
      <Field label="Auth password"><input className="input" type="password" {...f.bind('auth_password')} /></Field>
      <Field label="Transport">
        <Select value={f.v.transport} onChange={(t) => f.setV((s) => ({ ...s, transport: t }))}
          options={['auto', 'udp', 'tcp', 'tls'].map((x) => ({ value: x, label: x.toUpperCase() }))} />
      </Field>
    </FormCard>
  )
}

function RuleForm({ trunks, workflows, onSubmit }: { trunks: SipTrunk[]; workflows: Workflow[]; onSubmit: (b: Record<string, unknown>) => Promise<void> }) {
  const f = useForm({ name: 'Inbound calls', agent_name: DEFAULT_AGENT, workflow_id: '', trunk_ids: '', room_prefix: 'call-', pin: '', krisp: true, hide_phone_number: false })
  return (
    <FormCard submit="Create dispatch rule" onSubmit={() => onSubmit({ ...f.v, workflow_id: f.v.workflow_id || null })}>
      <Field label="Name"><input className="input" required {...f.bind('name')} /></Field>
      <Field label="Agent name" hint="Must match the Voice Agent trigger and a running worker"><input className="input" required {...f.bind('agent_name')} /></Field>
      <Field label="Voice workflow" hint="Empty = pick by agent name + dialled number">
        <Select value={f.v.workflow_id} onChange={(id) => f.setV((s) => ({ ...s, workflow_id: id }))} placeholder="Route by number"
          options={[{ value: '', label: 'Route by agent name + number' }, ...workflows.map((w) => ({ value: w.id, label: w.name }))]} />
      </Field>
      <Field label="Only for trunks">
        <Select value={f.v.trunk_ids} onChange={(id) => f.setV((s) => ({ ...s, trunk_ids: id }))}
          options={[{ value: '', label: 'All inbound trunks' }, ...trunks.map((t) => ({ value: t.sip_trunk_id, label: t.name || t.sip_trunk_id }))]} />
      </Field>
      <Field label="Room prefix"><input className="input" {...f.bind('room_prefix')} /></Field>
      <Field label="PIN (optional)"><input className="input" {...f.bind('pin')} /></Field>
      <label className="row small"><input type="checkbox" {...f.check('krisp')} /> Krisp noise cancellation</label>
      <label className="row small"><input type="checkbox" {...f.check('hide_phone_number')} /> Hide caller number</label>
    </FormCard>
  )
}

function PlivoWizard({ livekit, plivoCreds, workflows, onDone }: { livekit: string; plivoCreds: Credential[]; workflows: Workflow[]; onDone: () => void }) {
  const f = useForm({ plivo_credential: plivoCreds[0]?.id ?? '', phone_number: '', agent_name: DEFAULT_AGENT, workflow_id: workflows[0]?.id ?? '', outbound: true, krisp: true })
  const [result, setResult] = useState<Record<string, unknown> | null>(null)
  const steps = useMemo(() => result && Object.entries(result).filter(([k]) => k !== 'ok'), [result])
  return (
    <section className="phone-block">
      <div className="section-title"><Icon name="wand-sparkles" size={17} /> Connect a Plivo number<span className="muted small" style={{ fontWeight: 400 }}>
        Creates the Plivo SIP trunks and the LiveKit trunks + dispatch rule so the number answers with your agent.</span></div>
      <FormCard submit="Connect number" onSubmit={async () => {
        setResult(null)
        try {
          setResult(await api.plivoConnect({ ...f.v, livekit_credential: livekit, workflow_id: f.v.workflow_id || null }))
          toast('Number connected'); onDone()
        } catch (e) { throw new Error(errText(e)) }
      }}>
        <Field label="Plivo account">
          <Select value={f.v.plivo_credential} onChange={(id) => f.setV((s) => ({ ...s, plivo_credential: id }))} options={plivoCreds.map((c) => ({ value: c.id, label: c.name }))} />
        </Field>
        <Field label="Plivo phone number" hint="A number you own in Plivo"><input className="input" required placeholder="+14155550100" {...f.bind('phone_number')} /></Field>
        <Field label="Agent name"><input className="input" required {...f.bind('agent_name')} /></Field>
        <Field label="Voice workflow">
          <Select value={f.v.workflow_id} onChange={(id) => f.setV((s) => ({ ...s, workflow_id: id }))}
            options={[{ value: '', label: 'Route by agent name + number' }, ...workflows.map((w) => ({ value: w.id, label: w.name }))]} />
        </Field>
        <label className="row small"><input type="checkbox" {...f.check('outbound')} /> Also let agents call out through this number</label>
        <label className="row small"><input type="checkbox" {...f.check('krisp')} /> Krisp noise cancellation</label>
      </FormCard>
      {steps && <div className="card phone-result"><b><Icon name="circle-check" size={15} /> Connected</b>
        {steps.map(([k, v]) => <div key={k} className="small"><span className="muted">{k.replace(/_/g, ' ')}:</span> <code>{String(v)}</code></div>)}</div>}
    </section>
  )
}
