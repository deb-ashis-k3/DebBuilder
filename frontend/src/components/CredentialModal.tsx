import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { Icon, toast } from '../lib'
import type { Credential, CredentialCatalog, CredentialField, TestResult } from '../types'
import { Select } from './Select'

let catalogCache: CredentialCatalog | null = null

export function useCredentialCatalog() {
  const [cat, setCat] = useState<CredentialCatalog | null>(catalogCache)
  useEffect(() => {
    if (!catalogCache) api.credentialTypes().then((c) => { catalogCache = c; setCat(c) })
  }, [])
  return cat
}

/** Monogram tile in the service's brand color (no third-party logos). */
export function TypeTile({ label, color, size = 36 }: { label: string; color: string; size?: number }) {
  const words = label.replace(/\(.*?\)/g, '').split(/[\s/.-]+/).filter(Boolean)
  // "OpenAI" → "OP", not "OO".
  const two = words.length > 1 && words[0][0].toLowerCase() !== words[1][0].toLowerCase()
  const text = (two ? words[0][0] + words[1][0] : words[0]?.slice(0, 2) ?? '?').toUpperCase()
  const hex = color.replace('#', '')
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16))
  const light = (r * 299 + g * 587 + b * 114) / 1000 > 165
  return (
    <div className="type-tile" style={{ width: size, height: size, background: color, color: light ? '#1a1a1a' : '#fff', fontSize: size * 0.36 }}>
      {text}
    </div>
  )
}

function TestBanner({ result }: { result: TestResult | 'testing' | null }) {
  if (!result) return null
  if (result === 'testing') return <div className="test-banner testing"><Icon name="loader-circle" size={16} className="spin" /> Testing connection…</div>
  return (
    <div className={`test-banner ${result.ok ? (result.untested ? 'info' : 'ok') : 'bad'}`}>
      <Icon name={result.ok ? (result.untested ? 'info' : 'circle-check') : 'circle-alert'} size={16} />
      <span>{result.message}</span>
    </div>
  )
}

function TypePicker({ catalog, allowed, onPick }: { catalog: CredentialCatalog; allowed?: string[]; onPick: (t: string) => void }) {
  const [q, setQ] = useState('')
  const [cat, setCat] = useState<string>('All')
  const ref = useRef<HTMLInputElement>(null)
  useEffect(() => ref.current?.focus(), [])
  const entries = Object.entries(catalog.types).filter(([k]) => !allowed?.length || allowed.includes(k))
  const needle = q.toLowerCase()
  const shown = entries.filter(([k, t]) => (cat === 'All' || t.category === cat) &&
    (!needle || `${t.label} ${k} ${t.category} ${t.description}`.toLowerCase().includes(needle)))
  const cats = ['All', ...catalog.categories.filter((c) => entries.some(([, t]) => t.category === c))]
  return (
    <div className="type-picker">
      <div className="search">
        <Icon name="search" size={15} />
        <input ref={ref} className="input" placeholder={`Search ${entries.length} services…`} value={q} onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && shown[0]) onPick(shown[0][0]) }} />
      </div>
      {cats.length > 2 && (
        <div className="chips">
          {cats.map((c) => <button key={c} className={`chip ${cat === c ? 'on' : ''}`} onClick={() => setCat(c)}>{c}</button>)}
        </div>
      )}
      <div className="type-grid">
        {shown.map(([k, t]) => (
          <button key={k} className="type-card" onClick={() => onPick(k)} title={t.description || t.label}>
            <TypeTile label={t.label} color={t.color} />
            <div className="grow">
              <div className="tc-title">{t.label}</div>
              <div className="tc-sub">{t.oauth2 ? 'OAuth2 · ' : ''}{t.category}</div>
            </div>
            {t.testable && <Icon name="badge-check" size={15} className="tc-check" />}
          </button>
        ))}
        {shown.length === 0 && <div className="empty small">No service matches “{q}”. Use <b>Header Auth</b>, <b>Bearer Token</b> or <b>OAuth2 API (generic)</b> for any other API.</div>}
      </div>
    </div>
  )
}

function FieldInput({ f, value, onChange }: { f: CredentialField; value: unknown; onChange: (v: unknown) => void }) {
  const [reveal, setReveal] = useState(false)
  if (f.type === 'options') {
    return (
      <Select value={String(value ?? f.default ?? '')} onChange={onChange} options={(f.options ?? []).map((o) => ({ value: o, label: o }))} />
    )
  }
  if (f.type === 'boolean') {
    return <label className="switch"><input type="checkbox" checked={!!value} onChange={(e) => onChange(e.target.checked)} /><span /></label>
  }
  if (f.type === 'textarea') {
    return <textarea className="textarea code" rows={5} placeholder={f.placeholder} value={String(value ?? '')} onChange={(e) => onChange(e.target.value)} />
  }
  return (
    <div className="input-wrap">
      <input className="input" autoComplete="off" spellCheck={false}
        type={f.secret && !reveal ? 'password' : f.type === 'number' ? 'number' : 'text'}
        placeholder={f.placeholder || (f.default != null ? String(f.default) : '')} value={String(value ?? '')}
        onChange={(e) => onChange(f.type === 'number' ? (e.target.value === '' ? null : Number(e.target.value)) : e.target.value)} />
      {f.secret && (
        <button type="button" className="btn ghost sm icon reveal" title={reveal ? 'Hide' : 'Show'} onClick={() => setReveal(!reveal)}>
          <Icon name={reveal ? 'eye-off' : 'eye'} size={15} />
        </button>
      )}
    </div>
  )
}

export function CredentialModal({ credential, allowedTypes, onClose, onSaved }: {
  credential?: Credential
  allowedTypes?: string[]
  onClose: () => void
  onSaved: (c: Credential) => void
}) {
  const catalog = useCredentialCatalog()
  const allowed = allowedTypes?.length ? allowedTypes : undefined
  const [type, setType] = useState(credential?.type ?? (allowed?.length === 1 ? allowed[0] : ''))
  const [saved, setSaved] = useState<Credential | undefined>(credential)
  const [name, setName] = useState(credential?.name ?? '')
  const [data, setData] = useState<Record<string, unknown>>({})
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<TestResult | 'testing' | null>(null)
  const [connected, setConnected] = useState(!!credential?.oauth_connected)

  useEffect(() => {
    if (credential) api.credential(credential.id).then((c) => { setData(c.data ?? {}); setConnected(!!c.data?.oauth_connected) })
  }, [credential])

  const spec = catalog && type ? catalog.types[type] : undefined
  useEffect(() => {
    if (!credential && spec) {
      setData(Object.fromEntries(spec.fields.filter((f) => f.default != null).map((f) => [f.name, f.default])))
      setName((n) => n || spec.label)
      setResult(null)
    }
  }, [spec, credential])

  const visible = useMemo(() => (spec?.fields ?? []).filter((f) =>
    !f.show_if || Object.entries(f.show_if).every(([k, vals]) => vals.includes((data[k] ?? spec?.fields.find((x) => x.name === k)?.default) as never))),
  [spec, data])

  // OAuth popup reports back via postMessage.
  useEffect(() => {
    const onMsg = (e: MessageEvent) => {
      if (e.data?.type !== 'debbuilder-oauth2') return
      setConnected(!!e.data.ok)
      if (e.data.ok && saved) { toast('Account connected'); runTest(saved.id) }
    }
    window.addEventListener('message', onMsg)
    return () => window.removeEventListener('message', onMsg)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [saved])

  if (!catalog) return null

  const runTest = async (id = saved?.id) => {
    if (!spec) return
    setResult('testing')
    try {
      setResult(await api.testCredential(type, data, id))
    } catch (e) {
      setResult({ ok: false, message: String(e) })
    }
  }

  const persist = async (): Promise<Credential | null> => {
    if (!spec) return null
    const missing = visible.filter((f) => f.required && f.type !== 'boolean' && (data[f.name] === undefined || data[f.name] === '' || data[f.name] === null))
    if (missing.length && !saved) {
      toast(`Missing: ${missing.map((m) => m.label).join(', ')}`, 'error')
      return null
    }
    const c = saved
      ? await api.updateCredential(saved.id, { name, data })
      : await api.createCredential({ name: name || spec.label, type, data })
    setSaved(c)
    return c
  }

  const save = async () => {
    setBusy(true)
    try {
      const c = await persist()
      if (!c) return
      if (spec?.testable && !(spec.oauth2 && !connected && data.grant_type !== 'client_credentials')) {
        setResult('testing')
        const r = await api.testSavedCredential(c.id)
        setResult(r)
        if (!r.ok) { toast('Saved, but the connection test failed', 'error'); return }
        toast(r.untested ? 'Credential saved' : 'Saved · connection tested successfully')
      } else {
        toast(spec?.oauth2 && !connected ? 'Saved. Now click “Connect my account”' : 'Credential saved')
        if (spec?.oauth2 && !connected) return
      }
      onSaved(c)
    } catch (e) {
      toast(String(e), 'error')
    } finally {
      setBusy(false)
    }
  }

  const connect = async () => {
    try {
      const c = await persist()  // the callback needs a stored credential
      if (!c) return
      const { url } = await api.oauthUrl(c.id)
      window.open(url, 'debbuilder-oauth', 'width=600,height=720')
    } catch (e) {
      toast(String(e), 'error')
    }
  }

  const head = spec ? (
    <>
      <TypeTile label={spec.label} color={spec.color} size={32} />
      <div className="grow">
        <h2 style={{ margin: 0 }}>{credential ? 'Edit' : 'New'} {spec.label} credential</h2>
        <div className="muted small">{spec.category}{spec.description ? ` · ${spec.description}` : ''}</div>
      </div>
    </>
  ) : <><Icon name="key-round" /><h2>Add credential</h2></>

  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal ${spec ? 'cred-form' : 'cred-pick'}`}>
        <div className="modal-head">
          {spec && !credential && <button className="btn ghost sm icon" title="Choose another service" onClick={() => { setType(''); setName('') }}><Icon name="arrow-left" size={16} /></button>}
          {head}
          <button className="btn ghost icon" onClick={onClose}><Icon name="x" /></button>
        </div>
        <div className="modal-body">
          {!spec ? (
            <TypePicker catalog={catalog} allowed={allowed} onPick={(t) => setType(t)} />
          ) : (
            <>
              <TestBanner result={result} />
              <div className="field">
                <label>Credential name</label>
                <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
              </div>
              {spec.oauth2 && (
                <div className="oauth-box">
                  <div className="field" style={{ marginBottom: 8 }}>
                    <label>OAuth redirect URL <span className="muted small">(add this in the provider's app settings)</span></label>
                    <div className="input-wrap">
                      <input className="input" readOnly value={catalog.oauth_redirect_uri} />
                      <button type="button" className="btn ghost sm icon reveal" title="Copy" onClick={() => { navigator.clipboard.writeText(catalog.oauth_redirect_uri); toast('Copied') }}><Icon name="copy" size={15} /></button>
                    </div>
                  </div>
                </div>
              )}
              {visible.map((f) => (
                <div className="field" key={f.name}>
                  <label>{f.label}{f.required && f.type !== 'boolean' && <span className="req">*</span>}</label>
                  <FieldInput f={f} value={data[f.name]} onChange={(v) => { setData({ ...data, [f.name]: v }); setResult(null) }} />
                  {f.help && <div className="hint">{f.help}</div>}
                </div>
              ))}
              {spec.oauth2 && data.grant_type !== 'client_credentials' && (
                <div className={`oauth-status ${connected ? 'on' : ''}`}>
                  <Icon name={connected ? 'link' : 'unlink'} size={16} />
                  <span className="grow">{connected ? 'Account connected' : 'Not connected yet'}</span>
                  <button className="btn sm primary" onClick={connect}><Icon name="log-in" size={14} /> {connected ? 'Reconnect' : 'Connect my account'}</button>
                </div>
              )}
              <div className="cred-foot-note">
                <span><Icon name="lock" size={12} /> Encrypted at rest</span>
                {spec.docs && <a href={spec.docs} target="_blank" rel="noreferrer"><Icon name="external-link" size={12} /> Where do I find this?</a>}
              </div>
            </>
          )}
        </div>
        {spec && (
          <div className="modal-foot">
            {spec.testable && (
              <button className="btn" disabled={result === 'testing'} onClick={() => runTest()}>
                <Icon name="plug-zap" size={15} /> Test connection
              </button>
            )}
            <span className="spacer" />
            <button className="btn" onClick={onClose}>Cancel</button>
            <button className="btn primary" disabled={busy} onClick={save}>{busy ? 'Saving…' : spec.testable ? 'Save & test' : 'Save'}</button>
          </div>
        )}
      </div>
    </div>
  )
}
