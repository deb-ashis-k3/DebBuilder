import { useEffect, useState } from 'react'
import { api } from '../api'
import { Icon, isExpr, isVisible, toast } from '../lib'
import type { Credential, PropSpec, Workflow } from '../types'
import { CredentialModal } from './CredentialModal'
import { Select } from './Select'

// Small shared caches so every field doesn't refetch.
let credCache: Credential[] | null = null
let wfCache: Workflow[] | null = null
const credListeners = new Set<(c: Credential[]) => void>()

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
  const options = creds.filter((c) => !allowed.length || allowed.includes(c.type))
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
      <button className="btn icon" title="Create credential" onClick={() => setModal(true)}><Icon name="plus" /></button>
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

export function PropertyField({ spec, value, onChange }: { spec: PropSpec; value: unknown; onChange: (v: unknown) => void }) {
  const workflows = useWorkflows()
  const exprCapable = ['number', 'boolean', 'options'].includes(spec.type)
  const [exprMode, setExprMode] = useState(exprCapable && isExpr(value))
  const str = value == null ? '' : typeof value === 'string' ? value : JSON.stringify(value, null, 2)
  const exprCls = isExpr(value) ? 'expr' : ''

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
            options={workflows.map((w) => ({ value: w.id, label: w.name }))} />
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
