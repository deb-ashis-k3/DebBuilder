import { icons, type LucideProps } from 'lucide-react'
import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { api } from './api'
import type { NodeTypeSpec, PropSpec, WFNode } from './types'

const toPascal = (s: string) => s.replace(/(^|-)([a-z0-9])/g, (_, __, c: string) => c.toUpperCase())

// Names renamed in lucide v1; node types from the API use the classic names.
const ALIASES: Record<string, string> = {
  history: 'rotate-ccw-clock', 'trash-2': 'trash', filter: 'funnel', 'alert-triangle': 'triangle-alert', smile: 'activity',
}

export function Icon({ name, ...props }: { name: string } & LucideProps) {
  const C = (icons as Record<string, React.ComponentType<LucideProps>>)[toPascal(ALIASES[name] ?? name)] ?? icons.Box
  return <C size={18} {...props} />
}

// ---------------------------------------------------------------------------
// Node type catalog
// ---------------------------------------------------------------------------

interface Catalog {
  list: NodeTypeSpec[]
  byType: Record<string, NodeTypeSpec>
}
const CatalogCtx = createContext<Catalog>({ list: [], byType: {} })

export function CatalogProvider({ children }: { children: ReactNode }) {
  const [cat, setCat] = useState<Catalog | null>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => {
    api.nodeTypes()
      .then((list) => setCat({ list, byType: Object.fromEntries(list.map((t) => [t.type, t])) }))
      .catch((e) => setErr(String(e)))
  }, [])
  if (err) return <div className="empty"><h3>Cannot reach the DebBuilder API</h3><p>{err}</p><p>Start it with <code>uv run python -m app</code> in <code>backend/</code>.</p></div>
  if (!cat) return <div className="empty">Loading…</div>
  return <CatalogCtx.Provider value={cat}>{children}</CatalogCtx.Provider>
}

export const useCatalog = () => useContext(CatalogCtx)

export const GROUPS: { id: string; label: string }[] = [
  { id: 'trigger', label: 'Triggers' },
  { id: 'ai', label: 'AI (LangChain + LangGraph)' },
  { id: 'ai_model', label: 'AI · Chat Models' },
  { id: 'ai_memory', label: 'AI · Memory' },
  { id: 'ai_tool', label: 'AI · Tools' },
  { id: 'ai_vector_store', label: 'AI · Vector Stores' },
  { id: 'ai_embedding', label: 'AI · Embeddings' },
  { id: 'ai_document', label: 'AI · Document Loaders' },
  { id: 'ai_text_splitter', label: 'AI · Text Splitters' },
  { id: 'ai_retriever', label: 'AI · Retrievers' },
  { id: 'ai_reranker', label: 'AI · Rerankers' },
  { id: 'voice', label: 'Voice (LiveKit)' },
  { id: 'communication', label: 'Communication' },
  { id: 'productivity', label: 'Productivity & Docs' },
  { id: 'crm', label: 'CRM & Support' },
  { id: 'commerce', label: 'Payments & E-commerce' },
  { id: 'marketing', label: 'Marketing, Email & Social' },
  { id: 'devops', label: 'Developer & DevOps' },
  { id: 'storage', label: 'Files & Storage' },
  { id: 'ai_services', label: 'AI Services, Search & Scraping' },
  { id: 'core', label: 'Core' },
  { id: 'flow', label: 'Flow' },
  { id: 'data', label: 'Data transformation' },
]

// ---------------------------------------------------------------------------
// Node helpers
// ---------------------------------------------------------------------------

export function defaultParams(spec: NodeTypeSpec): Record<string, unknown> {
  return Object.fromEntries(spec.properties.map((p) => [p.name, structuredClone(p.default ?? null)]))
}

export function isVisible(p: PropSpec, params: Record<string, unknown>): boolean {
  if (!p.show_if) return true
  return Object.entries(p.show_if).every(([k, vals]) => vals.includes(params[k] as never))
}

export interface Port { id: string; label: string }

export function outputPorts(spec: NodeTypeSpec, node: WFNode): Port[] {
  let ports: Port[] = spec.outputs.map((o) => ({ id: o, label: o === 'main' ? '' : o }))
  if (spec.dynamic_outputs) {
    const entries = (node.parameters?.[spec.dynamic_outputs] as { name?: string }[] | undefined) ?? []
    ports = entries.map((e, i) => ({ id: `out${i}`, label: e?.name || `Output ${i + 1}` }))
    const fallbackOn = node.parameters?.fallback ?? true
    if (fallbackOn) ports.push({ id: 'fallback', label: spec.type === 'ai.classifier' ? 'other' : 'fallback' })
  }
  if (node.settings?.on_error === 'error_output') ports.push({ id: 'error', label: 'error' })
  if (ports.length === 1 && ports[0].id === 'main') ports[0].label = ''
  return ports
}

export function inputPorts(spec: NodeTypeSpec): Port[] {
  return spec.inputs.map((i) => ({ id: i, label: i === 'main' ? '' : i.replace('input', 'Input ') }))
}

export function uniqueName(base: string, nodes: WFNode[]): string {
  const names = new Set(nodes.map((n) => n.name))
  if (!names.has(base)) return base
  let i = 1
  while (names.has(`${base}${i}`)) i++
  return `${base}${i}`
}

export const newId = () => 'n_' + Math.random().toString(36).slice(2, 12)

export const isExpr = (v: unknown) => typeof v === 'string' && v.includes('{{')

export function fmtDate(s?: string) {
  if (!s) return ''
  const d = new Date(s)
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

export function fmtDuration(ms?: number | null) {
  if (ms == null) return ''
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`
}

// ---------------------------------------------------------------------------
// Toasts
// ---------------------------------------------------------------------------

type ToastFn = (msg: string, kind?: 'info' | 'error') => void
let pushToast: ToastFn = () => {}
export const toast: ToastFn = (m, k) => pushToast(m, k)

export function Toaster() {
  const [t, setT] = useState<{ msg: string; kind: string; id: number } | null>(null)
  useEffect(() => {
    pushToast = (msg, kind = 'info') => {
      const id = Date.now()
      setT({ msg, kind, id })
      window.setTimeout(() => setT((cur) => (cur?.id === id ? null : cur)), kind === 'error' ? 6000 : 2500)
    }
  }, [])
  return t ? (
    <div key={t.id} className={`toast ${t.kind}`} onClick={() => setT(null)}>
      <span className="t-icon"><Icon name={t.kind === 'error' ? 'circle-alert' : 'check'} size={16} strokeWidth={2.5} /></span>
      <span>{t.msg}</span>
    </div>
  ) : null
}

export function StatusBadge({ status }: { status: string }) {
  return <span className={`badge ${status}`}>{status}</span>
}
