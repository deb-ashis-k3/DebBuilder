import { useMemo, useState, type ReactNode } from 'react'
import { Icon } from '../lib'
import type { Item } from '../types'

const IDENT = /^[A-Za-z_][A-Za-z0-9_]*$/
type Mode = 'schema' | 'table' | 'json'

function joinPath(base: string, key: string | number) {
  if (typeof key === 'number') return `${base}[${key}]`
  return IDENT.test(key) ? `${base}.${key}` : `${base}['${key.replace(/'/g, "\\'")}']`
}

function dragExpr(e: React.DragEvent, path: string) {
  e.dataTransfer.setData('text/plain', `{{ ${path} }}`)
  e.dataTransfer.effectAllowed = 'copy'
}

// ---------------------------------------------------------------------------
// JSON tree
// ---------------------------------------------------------------------------

function JsonNode({ value, path, name, depth }: { value: unknown; path: string; name?: string | number; depth: number }) {
  const [open, setOpen] = useState(depth < 2)
  const keyEl = name !== undefined && (
    <span className="json-key" draggable onDragStart={(e) => dragExpr(e, path)} title={`Drag into a field: {{ ${path} }}`}>
      {typeof name === 'number' ? name : `"${name}"`}
    </span>
  )
  if (value !== null && typeof value === 'object') {
    const entries = Array.isArray(value) ? value.map((v, i) => [i, v] as const) : Object.entries(value as object)
    const [o, c] = Array.isArray(value) ? ['[', ']'] : ['{', '}']
    return (
      <div style={{ paddingLeft: depth ? 16 : 0 }}>
        <span className="json-toggle" onClick={() => setOpen(!open)}>{open ? '▾' : '▸'}</span>
        {keyEl}{keyEl && ': '}{o}
        {!open && <span className="muted"> {entries.length} {Array.isArray(value) ? 'items' : 'keys'} </span>}
        {open && entries.map(([k, v]) => <JsonNode key={String(k)} value={v} name={k} path={joinPath(path, k)} depth={depth + 1} />)}
        {open ? <div style={{ paddingLeft: 14 }}>{c}</div> : c}
      </div>
    )
  }
  const cls = value === null ? 'json-null' : typeof value === 'string' ? 'json-str' : typeof value === 'number' ? 'json-num' : 'json-bool'
  const text = typeof value === 'string' ? JSON.stringify(value.length > 2000 ? value.slice(0, 2000) + '…' : value) : String(value)
  return (
    <div style={{ paddingLeft: depth ? 16 : 0 }}>
      <span className="json-toggle" />{keyEl}{keyEl && ': '}<span className={cls}>{text}</span>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Schema view (n8n's default): field tree with types + sample values
// ---------------------------------------------------------------------------

interface SchemaNode { key: string | number; type: string; sample?: unknown; children?: SchemaNode[]; path: string }

function typeOf(v: unknown) {
  if (v === null || v === undefined) return 'null'
  if (Array.isArray(v)) return 'array'
  return typeof v
}

function buildSchema(values: unknown[], path: string, depth = 0): SchemaNode[] {
  const objs = values.filter((v) => v && typeof v === 'object' && !Array.isArray(v)) as Record<string, unknown>[]
  const keys = Array.from(new Set(objs.flatMap((o) => Object.keys(o)))).slice(0, 80)
  return keys.map((k) => {
    const vals = objs.map((o) => o[k]).filter((v) => v !== undefined)
    const first = vals.find((v) => v !== null) ?? vals[0]
    const t = typeOf(first)
    const p = joinPath(path, k)
    const node: SchemaNode = { key: k, type: t, path: p }
    if (depth > 5) return node
    if (t === 'object') node.children = buildSchema(vals, p, depth + 1)
    else if (t === 'array') {
      const inner = (vals as unknown[][]).flat()
      const it = typeOf(inner[0])
      node.type = `array<${it === 'object' ? 'object' : it}>`
      if (it === 'object') node.children = buildSchema(inner, `${p}[0]`, depth + 1)
      else node.sample = inner.slice(0, 3)
    } else node.sample = first
    return node
  })
}

const TYPE_ICON: Record<string, string> = { string: 'type', number: 'hash', boolean: 'toggle-left', object: 'braces', null: 'circle-slash' }

function SchemaRow({ n, depth }: { n: SchemaNode; depth: number }) {
  const [open, setOpen] = useState(depth < 2)
  const icon = n.type.startsWith('array') ? 'brackets' : TYPE_ICON[n.type] ?? 'circle'
  const sample = n.sample === undefined ? '' : typeof n.sample === 'string' ? n.sample : JSON.stringify(n.sample)
  return (
    <>
      <div className="schema-row" style={{ paddingLeft: 8 + depth * 18 }}>
        {n.children ? <span className="json-toggle" onClick={() => setOpen(!open)}>{open ? '▾' : '▸'}</span> : <span className="json-toggle" />}
        <span className="schema-pill" draggable onDragStart={(e) => dragExpr(e, n.path)} title={`Drag into a field: {{ ${n.path} }}`}>
          <Icon name={icon} size={12} /> {String(n.key)}
        </span>
        {sample !== '' && <span className="schema-sample" title={sample}>{sample}</span>}
        {n.children && <span className="schema-type">{n.type}</span>}
      </div>
      {open && n.children?.map((c) => <SchemaRow key={String(c.key)} n={c} depth={depth + 1} />)}
    </>
  )
}

// ---------------------------------------------------------------------------
// Table
// ---------------------------------------------------------------------------

function Table({ items, base }: { items: Item[]; base: string }) {
  const cols = Array.from(new Set(items.flatMap((i) => Object.keys(i)))).slice(0, 40)
  return (
    <div style={{ overflowX: 'auto' }}>
      <table className="data-table">
        <thead>
          <tr><th className="rownum">#</th>{cols.map((c) => <th key={c} draggable onDragStart={(e) => dragExpr(e, joinPath(base, c))}>{c}</th>)}</tr>
        </thead>
        <tbody>
          {items.slice(0, 200).map((it, r) => (
            <tr key={r}>
              <td className="rownum">{r}</td>
              {cols.map((c) => {
                const v = it[c]
                const s = v === undefined ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v)
                return <td key={c} title={s} className={v === null || v === undefined ? 'muted' : ''}>{v === null ? 'null' : s}</td>
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ---------------------------------------------------------------------------
// DataView
// ---------------------------------------------------------------------------

export function DataView({ items, base = '$json', empty, actions, defaultMode = 'schema', meta }: {
  items?: Item[]
  base?: string
  empty?: ReactNode
  actions?: ReactNode
  defaultMode?: Mode
  meta?: ReactNode
}) {
  const [mode, setMode] = useState<Mode>(() => (localStorageGet('dv-mode') as Mode) || defaultMode)
  const [q, setQ] = useState('')
  const filtered = useMemo(() => {
    if (!items || !q) return items ?? []
    const needle = q.toLowerCase()
    return items.filter((it) => JSON.stringify(it).toLowerCase().includes(needle))
  }, [items, q])
  const schema = useMemo(() => (mode === 'schema' ? buildSchema(filtered.slice(0, 50), base) : []), [filtered, base, mode])
  const pick = (m: Mode) => { setMode(m); localStorageSet('dv-mode', m) }

  return (
    <div className="dataview">
      <div className="dv-bar">
        <div className="tabs">
          {(['schema', 'table', 'json'] as Mode[]).map((m) => (
            <button key={m} className={`tab ${mode === m ? 'active' : ''}`} onClick={() => pick(m)}>{m === 'json' ? 'JSON' : m[0].toUpperCase() + m.slice(1)}</button>
          ))}
        </div>
        {!!items?.length && (
          <div className="search dv-search">
            <Icon name="search" size={13} />
            <input className="input" placeholder="Search" value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
        )}
        <span className="spacer" />
        {actions}
      </div>
      {(meta || !!items?.length) && (
        <div className="dv-meta">
          {!!items?.length && <span className="dv-count">{q ? `${filtered.length} of ` : ''}{items.length} item{items.length === 1 ? '' : 's'}</span>}
          {meta}
        </div>
      )}
      {!items || items.length === 0 ? (
        <div className="dv-empty">{empty ?? 'No data'}</div>
      ) : mode === 'table' ? <Table items={filtered} base={base} />
        : mode === 'schema' ? <div className="schema">{schema.map((n) => <SchemaRow key={String(n.key)} n={n} depth={0} />)}</div>
        : filtered.slice(0, 100).map((it, i) => <div key={i} className="json-item json"><JsonNode value={it} path={base} depth={0} /></div>)}
      {mode === 'json' && filtered.length > 100 && <div className="muted small">Showing first 100 items</div>}
    </div>
  )
}

function localStorageGet(k: string) {
  try { return localStorage.getItem(k) } catch { return null }
}
function localStorageSet(k: string, v: string) {
  try { localStorage.setItem(k, v) } catch { /* private mode */ }
}
