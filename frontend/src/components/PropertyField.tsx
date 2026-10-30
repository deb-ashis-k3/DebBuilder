import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { Icon, isExpr, isVisible, toast } from '../lib'
import type { Credential, PropSpec, Workflow } from '../types'
import { CredentialModal } from './CredentialModal'
import { Select } from './Select'

// Small shared caches so every field doesn't refetch.
let credCache: Credential[] | null = null
let wfCache: Workflow[] | null = null
const credListeners = new Set<(c: Credential[]) => void>()
window.addEventListener('auth:changed', () => { credCache = null; wfCache = null; modelCache.clear() })

// Provider model lists per credential (the server caches too; this avoids refetching per node).
type ModelOption = { value: string; label: string; hint?: string }
const modelCache = new Map<string, Promise<ModelOption[]>>()
function loadModels(credId: string, refresh = false): Promise<ModelOption[]> {
  if (refresh || !modelCache.has(credId)) {
    const p = api.credentialModels(credId, refresh).then((r) => r.models)
    p.catch(() => modelCache.delete(credId))  // retry next time
    modelCache.set(credId, p)
  }
  return modelCache.get(credId)!
}

/** n8n-style model picker: "From list" asks the provider (via the node's credential), "By ID" is free text / expression. */
function ModelPicker({ spec, value, onChange, credentialId }: { spec: PropSpec; value: unknown; onChange: (v: unknown) => void; credentialId: string | null }) {
  const str = value == null ? '' : String(value)
  const [mode, setMode] = useState<'list' | 'id'>(isExpr(value) ? 'id' : 'list')
  // Tagged with its credential so switching credentials never shows the previous one's list.
  const [result, setResult] = useState<{ cred: string; models: ModelOption[] | null; err: string } | null>(null)
  const [loading, setLoading] = useState(false)
  const current = result?.cred === credentialId ? result : null
  const models = current?.models ?? null
  const err = current?.err ?? ''

  const load = useCallback(async (refresh = false) => {
    if (!credentialId) return
    setLoading(true)
    try { setResult({ cred: credentialId, models: await loadModels(credentialId, refresh), err: '' }) }
    catch (e) { setResult({ cred: credentialId, models: null, err: String(e instanceof Error ? e.message : e) }) }
    finally { setLoading(false) }
  }, [credentialId])
  useEffect(() => { if (mode === 'list') load() }, [load, mode])

  // Without a credential (or if loading failed) the node's suggestions still work offline.
  const base: ModelOption[] = models ?? (spec.options ?? []).map((o) => ({ value: String(o.value), label: o.label }))
  const options = str && !base.some((m) => m.value === str) ? [{ value: str, label: str, hint: 'current' }, ...base] : base
  return (
    <div className="model-picker">
      <div className="row" style={{ gap: 6 }}>
        <Select style={{ width: 112, flex: 'none' }} value={mode} onChange={(v) => setMode(v as 'list' | 'id')} title="How to choose the model"
          options={[{ value: 'list', label: 'From list' }, { value: 'id', label: 'By ID' }]} />
        {mode === 'list' ? (
          <>
            <Select style={{ flex: 1, minWidth: 0 }} value={str} onChange={(v) => onChange(v)} searchable
              placeholder={loading ? 'Loading models…' : 'Select a model…'}
              options={options.map((m) => ({ value: m.value, label: m.label, hint: m.hint !== m.label ? m.hint : undefined }))} />
            <button type="button" className="btn icon" title="Reload models from the provider" disabled={!credentialId || loading} onClick={() => load(true)}>
              <Icon name={loading ? 'loader-circle' : 'refresh-cw'} size={15} className={loading ? 'spin' : undefined} />
            </button>
          </>
        ) : (
          <input className={`input ${isExpr(value) ? 'expr' : ''}`} style={{ flex: 1, minWidth: 0 }} value={str}
            placeholder="e.g. gpt-5-mini or {{ $json.model }}" onChange={(e) => onChange(e.target.value)} />
        )}
      </div>
      {mode === 'list' && !credentialId && <div className="muted small" style={{ marginTop: 6 }}>Select a credential to load the models it can use.</div>}
      {mode === 'list' && err && <div className="test-inline bad">Couldn’t load models: {err}. Pick “By ID” to type a model name.</div>}
      {mode === 'list' && models && !err && <div className="muted small" style={{ marginTop: 6 }}>{models.length} model{models.length === 1 ? '' : 's'} available</div>}
    </div>
  )
}

// Owner of the workflow open in the editor. A workflow can only use its owner's credentials and
// call its owner's workflows (enforced by the server), so pickers only offer those. Admins see
// everyone's lists; without this they'd be offered items the run would reject.
let pickerOwner: string | null | undefined
let pickerMine = true  // new credentials belong to the caller, so only offer "create" in your own workflow
export function setPickerOwner(ownerId: string | null | undefined, mine = true) { pickerOwner = ownerId; pickerMine = mine }
const ofOwner = <T extends { owner_id?: string | null }>(items: T[]) =>
  pickerOwner === undefined ? items : items.filter((x) => x.owner_id === pickerOwner)

async function refreshCreds() {
  credCache = await api.credentials()
  credListeners.forEach((l) => l(credCache!))
}

function useCredentials() {
  const [creds, setCreds] = useState<Credential[]>(credCache ?? [])
  useEffect(() => {
    credListeners.add(setCreds)
    if (!credCache) refreshCreds()
    return () => { credListeners.delete(setCreds) }
  }, [])
  return creds
}

function useWorkflows() {
  const [wfs, setWfs] = useState<Workflow[]>(wfCache ?? [])
  useEffect(() => {
    api.workflows().then((w) => { wfCache = w; setWfs(w) })
  }, [])
  return wfs
}

function CodeArea({ value, onChange, rows, className }: { value: string; onChange: (v: string) => void; rows?: number; className?: string }) {
  return (
    <textarea className={`textarea code ${className ?? ''}`} rows={rows ?? 10} value={value} spellCheck={false}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === 'Tab') {
          e.preventDefault()
          const t = e.currentTarget
          const s = t.selectionStart
          const next = value.slice(0, s) + '    ' + value.slice(t.selectionEnd)
          onChange(next)
          requestAnimationFrame(() => { t.selectionStart = t.selectionEnd = s + 4 })
        }
      }} />
  )
}

function CredentialSelect({ spec, value, onChange }: { spec: PropSpec; value: unknown; onChange: (v: unknown) => void }) {
  const creds = useCredentials()
  const [modal, setModal] = useState(false)
  const allowed = spec.credential_types ?? []
  const options = ofOwner(creds).filter((c) => !allowed.length || allowed.includes(c.type))
  return (
    <div className="row">
      <Select value={String(value ?? '')} onChange={(v) => onChange(v || null)} placeholder={options.length ? 'Select credential…' : 'No credentials yet'}
        options={options.map((c) => ({ value: c.id, label: c.name, hint: c.type_label }))} />
      {!!value && (
        <button className="btn icon" title="Test this credential" onClick={async () => {
          const r = await api.testSavedCredential(String(value)).catch((e) => ({ ok: false, message: String(e) }))
          toast(r.message, r.ok ? 'info' : 'error')
        }}><Icon name="plug-zap" size={16} /></button>
      )}
      {pickerMine && <button className="btn icon" title="Create credential" onClick={() => setModal(true)}><Icon name="plus" /></button>}
      {modal && (
        <CredentialModal allowedTypes={allowed} onClose={() => setModal(false)}
          onSaved={async (c) => { setModal(false); await refreshCreds(); onChange(c.id) }} />
      )}
    </div>
  )
}

function Collection({ spec, value, onChange }: { spec: PropSpec; value: unknown; onChange: (v: unknown) => void }) {
  const list = Array.isArray(value) ? (value as Record<string, unknown>[]) : []
  const fields = spec.fields ?? []
  const blank = () => Object.fromEntries(fields.map((f) => [f.name, structuredClone(f.default ?? '')]))
  return (
    <div>
      {list.map((entry, i) => (
        <div className="collection-item" key={i}>
          <button className="btn ghost sm icon remove" title="Remove" onClick={() => onChange(list.filter((_, j) => j !== i))}>
            <Icon name="trash-2" size={14} />
          </button>
          <div className="collection-grid">
            {fields.filter((f) => isVisible(f, entry)).map((f) => (
              <PropertyField key={f.name} spec={f} value={entry[f.name]}
                onChange={(v) => onChange(list.map((e, j) => (j === i ? { ...e, [f.name]: v } : e)))} />
            ))}
          </div>
        </div>
      ))}
      <button className="btn sm" onClick={() => onChange([...list, blank()])}><Icon name="plus" size={14} /> Add {spec.label.toLowerCase().replace(/\s*\(.*\)/, '')}</button>
    </div>
  )
}

export function PropertyField({ spec, value, onChange, params }: {
  spec: PropSpec; value: unknown; onChange: (v: unknown) => void
  /** The node's other parameters (e.g. its credential, for loading model lists). */
  params?: Record<string, unknown>
}) {
  const workflows = useWorkflows()
  const exprCapable = ['number', 'boolean', 'options'].includes(spec.type)
  const [exprMode, setExprMode] = useState(exprCapable && isExpr(value))
  const str = value == null ? '' : typeof value === 'string' ? value : JSON.stringify(value, null, 2)
  const exprCls = isExpr(value) ? 'expr' : ''
  if (spec.type === 'section') return <div className="prop-section">{spec.label}</div>

  let control: React.ReactNode
  if (exprMode) {
    control = <input className="input expr" value={str} placeholder="{{ $json.field }}" onChange={(e) => onChange(e.target.value)} />
  } else {
    switch (spec.type) {
      case 'text':
        control = <textarea className={`textarea ${exprCls}`} rows={spec.rows ?? 3} value={str} placeholder={spec.placeholder} onChange={(e) => onChange(e.target.value)} />
        break
      case 'code':
        control = <CodeArea value={str} rows={spec.rows} onChange={onChange} />
        break
      case 'json':
        control = <CodeArea value={str} rows={spec.rows ?? 5} className={exprCls} onChange={onChange} />
        break
      case 'number':
        control = <input className="input" type="number" value={value == null ? '' : String(value)}
          onChange={(e) => onChange(e.target.value === '' ? null : Number(e.target.value))} />
        break
      case 'boolean':
        control = (
          <label className="switch">
            <input type="checkbox" checked={!!value} onChange={(e) => onChange(e.target.checked)} /><span />
          </label>
        )
        break
      case 'options': {
        const opts = spec.options ?? []
        const idx = opts.findIndex((o) => o.value === value)
        control = (
          <Select value={idx < 0 ? '' : String(idx)} onChange={(v) => onChange(opts[Number(v)]?.value)} placeholder={String(value ?? 'Select…')}
            options={opts.map((o, i) => ({ value: String(i), label: o.label }))} />
        )
        break
      }
      case 'combo': {
        if (spec.load_options === 'models') {
          const cred = params?.credential
          control = <ModelPicker spec={spec} value={value} onChange={onChange} credentialId={typeof cred === 'string' && cred && !isExpr(cred) ? cred : null} />
          break
        }
        const id = `dl-${spec.name}`
        control = (
          <>
            <input className={`input ${exprCls}`} list={id} value={str} onChange={(e) => onChange(e.target.value)} />
            <datalist id={id}>{spec.options?.map((o) => <option key={String(o.value)} value={String(o.value)} />)}</datalist>
          </>
        )
        break
      }
      case 'credential':
        control = <CredentialSelect spec={spec} value={value} onChange={onChange} />
        break
      case 'workflow':
        control = (
          <Select value={String(value ?? '')} onChange={(v) => onChange(v || null)} placeholder="Select workflow…"
            options={ofOwner(workflows).map((w) => ({ value: w.id, label: w.name }))} />
        )
        break
      case 'collection':
        control = <Collection spec={spec} value={value} onChange={onChange} />
        break
      default:
        control = <input className={`input ${exprCls}`} value={str} placeholder={spec.placeholder} onChange={(e) => onChange(e.target.value)} />
    }
  }

  return (
    <div className="field">
      <label>
        {spec.label}{spec.required && <span className="req">*</span>}
        <span className="spacer" />
        {exprCapable && (
          <button className={`btn ghost sm ${exprMode ? 'primary' : ''}`} style={{ height: 22, padding: '0 6px', fontSize: 11 }}
            title="Toggle expression" onClick={() => setExprMode(!exprMode)}>fx</button>
        )}
      </label>
      {control}
      {spec.description && <div className="hint">{spec.description}</div>}
    </div>
  )
}
