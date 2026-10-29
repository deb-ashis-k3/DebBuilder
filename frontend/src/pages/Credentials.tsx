import { useEffect, useState } from 'react'
import { api } from '../api'
import { CredentialModal, TypeTile, useCredentialCatalog } from '../components/CredentialModal'
import { Icon, fmtDate, toast } from '../lib'
import type { Credential, TestResult } from '../types'
import { confirmDialog, errText } from '../components/Dialogs'
import { useAuth } from '../auth'

export default function Credentials() {
  const canWrite = useAuth().can('credential:write')
  const catalog = useCredentialCatalog()
  const [list, setList] = useState<Credential[] | null>(null)
  const [edit, setEdit] = useState<Credential | 'new' | null>(null)
  const [q, setQ] = useState('')
  const [results, setResults] = useState<Record<string, TestResult | 'testing'>>({})
  const load = () => api.credentials().then(setList)
  useEffect(() => { load() }, [])

  const test = async (c: Credential) => {
    setResults((r) => ({ ...r, [c.id]: 'testing' }))
    try {
      const res = await api.testSavedCredential(c.id)
      setResults((r) => ({ ...r, [c.id]: res }))
    } catch (e) {
      setResults((r) => ({ ...r, [c.id]: { ok: false, message: String(e) } }))
    }
  }
  const testAll = async () => {
    const targets = (list ?? []).filter((c) => c.testable)
    await Promise.all(targets.map(test))
    toast(`Tested ${targets.length} credential(s)`)
  }

  const shown = (list ?? []).filter((c) => `${c.name} ${c.type_label} ${c.category}`.toLowerCase().includes(q.toLowerCase()))
  const total = catalog ? Object.keys(catalog.types).length : 0

  return (
    <div className="page">
      <div className="page-inner">
        <div className="page-head">
          <h1>Credentials</h1>
          {list && list.length > 0 && <button className="btn" onClick={testAll}><Icon name="plug-zap" size={15} /> Test all</button>}
          {canWrite && <button className="btn primary" onClick={() => setEdit('new')}><Icon name="plus" size={16} /> Add credential</button>}
        </div>
        <p className="page-sub"><Icon name="shield-check" size={14} /> {total} services supported: LLMs, voice, messaging, email, databases, CRMs, payments, cloud and any OAuth2 / HTTP API. Encrypted at rest.</p>

        {list && list.length > 0 && (
          <div className="search" style={{ maxWidth: 340, marginBottom: 14 }}>
            <Icon name="search" size={15} />
            <input className="input" placeholder="Search your credentials…" value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
        )}

        {list && list.length === 0 && (
          <div className="card empty">
            <div className="empty-art"><Icon name="key-round" size={28} /></div>
            <h3>No credentials yet</h3>
            <p>Connect OpenAI, Anthropic, WhatsApp, LiveKit, Google, Slack and {total - 6}+ more.</p>
            <button className="btn primary" onClick={() => setEdit('new')}><Icon name="plus" size={16} /> Add credential</button>
          </div>
        )}

        <div className="list">
          {shown.map((c, i) => {
            const r = results[c.id]
            return (
              <div key={c.id} className="list-item" style={{ animationDelay: `${i * 30}ms` }} onClick={() => canWrite && setEdit(c)}>
                <TypeTile label={c.type_label} color={c.color ?? '#6366f1'} />
                <div className="grow">
                  <div className="title">{c.name}</div>
                  <div className="muted small">{c.type_label} · {c.category} · updated {fmtDate(c.updated_at)}</div>
                  {r && r !== 'testing' && <div className={`test-inline ${r.ok ? 'ok' : 'bad'}`}>{r.message}</div>}
                </div>
                {c.oauth2 && (c.oauth_connected ? <span className="badge success">Connected</span> : <span className="badge waiting">Not connected</span>)}
                {r === 'testing' && <span className="badge running">Testing</span>}
                {r && r !== 'testing' && <span className={`badge ${r.ok ? 'success' : 'error'}`}>{r.ok ? 'Working' : 'Failed'}</span>}
                <div className="actions">
                  {c.testable && (
                    <button className="btn ghost sm" title="Test connection" onClick={(e) => { e.stopPropagation(); test(c) }}>
                      <Icon name="plug-zap" size={15} /> Test
                    </button>
                  )}
                  <button className="btn ghost sm icon danger" title="Delete" onClick={async (e) => {
                    e.stopPropagation()
                    if (!await confirmDialog({ title: `Delete credential "${c.name}"?`, message: 'Workflows that use it will fail until you pick another credential.', confirmLabel: 'Delete', danger: true })) return
                    try { await api.deleteCredential(c.id); toast('Credential deleted'); load() } catch (err) { toast(errText(err), 'error') }
                  }}><Icon name="trash-2" size={15} /></button>
                </div>
              </div>
            )
          })}
        </div>
      </div>
      {edit && (
        <CredentialModal credential={edit === 'new' ? undefined : edit} onClose={() => { setEdit(null); load() }}
          onSaved={(c) => { setEdit(null); load(); setResults((r) => { const n = { ...r }; delete n[c.id]; return n }) }} />
      )}
    </div>
  )
}
