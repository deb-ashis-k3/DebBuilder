import { createContext, useContext, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { Icon } from '../lib'
import type { Item } from '../types'

/** What an expression in the open node resolves against: its input items and earlier nodes' outputs (by name). */
export interface ExprData { workflowId: string; items: Item[]; nodeOutputs: Record<string, Item[]>; nodeNames: string[] }
export const ExprDataContext = createContext<ExprData | null>(null)

/** One {{ … }} block, checked on its own like n8n's inline expression editor. */
export interface ExprSegment { from: number; to: number; state: 'valid' | 'pending' | 'invalid'; value?: string; error?: string; warnings: string[] }
export type ExprResult =
  | { ok: true; type: string; text: string; truncated?: boolean; segments?: ExprSegment[] }
  | { ok: false; error: string; pending?: boolean; segments?: ExprSegment[] }

/** n8n's inline result: resolve the value on the server (same engine as runs), debounced while typing. */
function usePreview(value: string): { res: ExprResult | null; data: ExprData | null } {
  const data = useContext(ExprDataContext)
  const [res, setRes] = useState<ExprResult | null>(null)
  useEffect(() => {
    if (!data) { setRes(null); return }
    let alive = true
    const t = window.setTimeout(() => {
      api.expressionPreview(data.workflowId, { value, items: data.items, node_outputs: data.nodeOutputs, node_names: data.nodeNames })
        .then((r) => { if (alive) setRes(r) })
        .catch((e) => { if (alive) setRes({ ok: false, error: String(e instanceof Error ? e.message : e) }) })
    }, 250)
    return () => { alive = false; window.clearTimeout(t) }
  }, [value, data])
  return { res, data }
}

type SegClass = 'ok' | 'bad' | 'pending' | 'warn'
const segClass = (s?: ExprSegment): SegClass =>
  !s ? 'pending' : s.state === 'invalid' ? 'bad' : s.state === 'pending' ? 'pending' : s.warnings.length ? 'warn' : 'ok'

/** Every {{ … }} coloured by its own state: green resolves, amber resolves but names a missing field,
 *  grey needs a node that hasn't run, red fails. */
function Highlighted({ text, segments }: { text: string; segments?: ExprSegment[] }) {
  const parts = text.split(/(\{\{[\s\S]*?\}\})/g)
  let block = 0
  return (
    <>
      {parts.map((p, i) => (p.startsWith('{{') && p.endsWith('}}')
        ? <span key={i} className={`expr-seg ${segClass(segments?.[block++])}`}>{p}</span>
        : <span key={i}>{p}</span>))}
      {/* keeps a trailing newline's height, like the textarea */}
      {'​'}
    </>
  )
}

// ---------------------------------------------------------------------------
// autocomplete (n8n: $ variables, $json fields from the input item, node names and their fields)
// ---------------------------------------------------------------------------

interface Suggestion { label: string; insert: string; detail?: string }
interface Completion { items: Suggestion[]; from: number; to: number }

const ROOTS: Suggestion[] = [
  { label: '$json', insert: '$json', detail: 'current input item' },
  { label: "$('…')", insert: "$('", detail: 'output of another node' },
  { label: '$input', insert: '$input', detail: 'all / first / last input items' },
  { label: '$now', insert: '$now', detail: 'current date and time' },
  { label: '$today', insert: '$today', detail: "today's date" },
  { label: '$vars', insert: '$vars', detail: 'workflow variables' },
  { label: '$state', insert: '$state', detail: 'LangGraph graph state' },
  { label: '$execution', insert: '$execution', detail: 'execution id / mode' },
  { label: '$workflow', insert: '$workflow', detail: 'workflow id / name' },
  { label: '$binary', insert: '$binary', detail: 'files on the current item' },
  { label: '$env', insert: '$env', detail: 'server settings (secrets hidden)' },
]
const IDENT = /^[A-Za-z_]\w*$/
const typeOf = (v: unknown) => (Array.isArray(v) ? `array[${v.length}]` : v === null ? 'null' : typeof v === 'object' ? 'object' : typeof v)

function walk(obj: unknown, path: string): unknown {
  let cur = obj
  for (const m of path.matchAll(/\.([A-Za-z_]\w*)|\[(\d+)\]|\[['"]([^'"]+)['"]\]/g)) {
    const key = m[1] ?? m[3] ?? Number(m[2])
    if (cur == null || typeof cur !== 'object') return undefined
    cur = (cur as Record<string | number, unknown>)[key]
  }
  return cur
}

function fieldSuggestions(obj: unknown, partial: string, dotPos: number, caret: number): Completion | null {
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return null
  const items = Object.entries(obj as Record<string, unknown>)
    .filter(([k]) => k.toLowerCase().startsWith(partial.toLowerCase()))
    .map(([k, v]) => ({ label: k, detail: typeOf(v), insert: IDENT.test(k) ? `.${k}` : `['${k.replace(/'/g, "\\'")}']` }))
  return items.length ? { items, from: dotPos, to: caret } : null
}

function complete(text: string, caret: number, data: ExprData | null): Completion | null {
  const before = text.slice(0, caret)
  const open = before.lastIndexOf('{{')
  if (open < 0 || before.lastIndexOf('}}') > open) return null  // only inside an open {{ … }}
  const item = data?.items[0]
  let m: RegExpExecArray | null
  if ((m = /\$\(\s*(['"])([^'"]*)$/.exec(before))) {
    const names = (data?.nodeNames ?? []).filter((n) => n.toLowerCase().includes(m![2].toLowerCase()))
    return names.length ? {
      items: names.map((n) => ({ label: n, insert: `${n}${m![1]})`, detail: data?.nodeOutputs[n] ? 'has run' : 'not run yet' })),
      from: caret - m[2].length, to: caret,
    } : null
  }
  if ((m = /\$\(\s*['"]([^'"]+)['"]\s*\)\.(?:first\(\)\.|last\(\)\.|item\.)?json((?:\.[A-Za-z_]\w*|\[\d+\]|\[['"][^'"]+['"]\])*)\.([A-Za-z_]\w*)?$/.exec(before))) {
    const out = data?.nodeOutputs[m[1]]?.[0]
    const partial = m[3] ?? ''
    return fieldSuggestions(walk(out, m[2]), partial, caret - partial.length - 1, caret)
  }
  if ((m = /\$\(\s*['"][^'"]+['"]\s*\)\.(\w*)$/.exec(before))) {
    const opts = [['first().json', 'first output item'], ['last().json', 'last output item'], ['all()', 'every output item'], ['item.json', 'paired item']]
      .filter(([o]) => o.startsWith(m![1])).map(([o, d]) => ({ label: o, insert: o, detail: d }))
    return opts.length ? { items: opts, from: caret - m[1].length, to: caret } : null
  }
  if ((m = /\$json((?:\.[A-Za-z_]\w*|\[\d+\]|\[['"][^'"]+['"]\])*)\.([A-Za-z_]\w*)?$/.exec(before))) {
    const partial = m[2] ?? ''
    return fieldSuggestions(walk(item, m[1]), partial, caret - partial.length - 1, caret)
  }
  if ((m = /\$input\.(\w*)$/.exec(before))) {
    const opts = [['first().json', 'first input item'], ['last().json', 'last input item'], ['all()', 'every input item'], ['item.json', 'current item']]
      .filter(([o]) => o.startsWith(m![1])).map(([o, d]) => ({ label: o, insert: o, detail: d }))
    return opts.length ? { items: opts, from: caret - m[1].length, to: caret } : null
  }
  if ((m = /\$(\w*)$/.exec(before))) {
    const items = ROOTS.filter((r) => r.label.slice(1).startsWith(m![1]))
    return items.length ? { items, from: caret - m[1].length - 1, to: caret } : null
  }
  return null
}

/** Editor + coloured overlay stacked in one grid cell: the highlighted copy sizes the box, so it grows with the text. */
function ExprEditor({ value, onChange, segments, big, placeholder, autoFocus }: {
  value: string; onChange: (v: string) => void; segments?: ExprSegment[]; big?: boolean; placeholder?: string; autoFocus?: boolean
}) {
  const data = useContext(ExprDataContext)
  const ta = useRef<HTMLTextAreaElement>(null)
  const [comp, setComp] = useState<(Completion & { active: number }) | null>(null)
  const refresh = (text: string, caret: number) => {
    const c = complete(text, caret, data)
    setComp(c ? { ...c, active: 0 } : null)
  }
  const accept = (s: Suggestion) => {
    if (!comp) return
    const next = value.slice(0, comp.from) + s.insert + value.slice(comp.to)
    const caret = comp.from + s.insert.length
    onChange(next)
    setComp(null)
    requestAnimationFrame(() => {
      ta.current?.focus()
      ta.current?.setSelectionRange(caret, caret)
      refresh(next, caret)  // e.g. after "$('Node')" offer .first().json, after "$json" offer fields
    })
  }
  return (
    <div className={`expr-edit ${big ? 'big' : ''}`}>
      <pre className="expr-hl" aria-hidden><Highlighted text={value} segments={segments} /></pre>
      <textarea ref={ta} className="expr-ta" value={value} spellCheck={false} placeholder={placeholder} autoFocus={autoFocus}
        onChange={(e) => { onChange(e.target.value); refresh(e.target.value, e.target.selectionStart) }}
        onClick={(e) => refresh(value, e.currentTarget.selectionStart)}
        onBlur={() => window.setTimeout(() => setComp(null), 120)}
        onKeyDown={(e) => {
          if (!comp) {
            if (e.key === ' ' && e.ctrlKey) { e.preventDefault(); refresh(value, e.currentTarget.selectionStart) }
            return
          }
          if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            e.preventDefault()
            const n = comp.items.length
            setComp({ ...comp, active: (comp.active + (e.key === 'ArrowDown' ? 1 : n - 1)) % n })
          } else if (e.key === 'Enter' || e.key === 'Tab') {
            e.preventDefault()
            accept(comp.items[comp.active])
          } else if (e.key === 'Escape') {
            e.preventDefault()
            e.stopPropagation()
            setComp(null)
          }
        }} />
      {comp && (
        <div className="expr-complete" role="listbox">
          {comp.items.slice(0, 12).map((s, i) => (
            <div key={s.label + i} role="option" aria-selected={i === comp.active} className={`expr-opt ${i === comp.active ? 'on' : ''}`}
              onMouseDown={(e) => { e.preventDefault(); accept(s) }}>
              <code>{s.label}</code>{s.detail && <span className="muted">{s.detail}</span>}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/** Under the field: the value (or why there isn't one) and field warnings, one line each. */
function ResultLine({ res, data, value }: { res: ExprResult | null; data: ExprData | null; value: string }) {
  if (!data) return null
  if (!res) return <div className="expr-result pending">Resolving…</div>
  const warnings = [...new Set((res.segments ?? []).flatMap((s) => s.warnings))]
  const warn = warnings.length > 0 && (
    <div className="expr-warn">{warnings.map((w) => <div key={w}><Icon name="triangle-alert" size={12} /> {w}</div>)}</div>
  )
  if (!res.ok) {
    return (
      <>
        <div className={`expr-result ${res.pending ? 'pending' : 'bad'}`} title={res.error}>{res.error}</div>
        {warn}
      </>
    )
  }
  const noInput = data.items.length === 0
  const shown = res.text === '' ? '[empty]' : res.type === 'NoneType' ? '[null]' : res.text
  return (
    <>
      <div className="expr-result ok" title={res.text}>
        {shown}{res.truncated ? ' …' : ''}
        {noInput && value.includes('$json') && <span className="expr-note"> · run the previous nodes to preview with real input</span>}
      </div>
      {warn}
    </>
  )
}

/** n8n-style expression input: fx badge, each {{ }} checked and coloured, autocomplete, live result, expand to a full editor. */
export function ExprField({ value, onChange, placeholder }: { value: string; onChange: (v: string) => void; placeholder?: string }) {
  const { res, data } = usePreview(value)
  const [open, setOpen] = useState(false)
  const segments = res?.segments
  const state = useMemo(() => {
    if (!res) return 'pending'
    if (!res.ok) return res.pending ? 'pending' : 'bad'
    return (segments ?? []).some((s) => s.warnings.length) ? 'warn' : 'ok'
  }, [res, segments])
  return (
    <>
      <div className={`expr-field ${state}`}>
        <span className="expr-fx" title="Expression">fx</span>
        <ExprEditor value={value} onChange={onChange} segments={segments} placeholder={placeholder ?? '{{ $json.field }}'} />
        <button type="button" className="expr-expand" title="Open the expression editor" onClick={() => setOpen(true)}>
          <Icon name="maximize-2" size={13} />
        </button>
      </div>
      <ResultLine res={res} data={data} value={value} />
      {open && (
        <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && setOpen(false)}>
          <div className="modal expr-modal">
            <div className="modal-head">
              <span className="expr-fx">fx</span>
              <h2 className="grow" style={{ margin: 0 }}>Edit expression</h2>
              <button className="btn ghost icon" onClick={() => setOpen(false)}><Icon name="x" /></button>
            </div>
            <div className="expr-modal-body">
              <div className="expr-modal-col">
                <div className="expr-modal-label">Expression</div>
                <ExprEditor value={value} onChange={onChange} segments={segments} big autoFocus placeholder="{{ $json.field }}" />
                <div className="expr-help">
                  Type <code>$</code> for suggestions (<kbd>Ctrl</kbd>+<kbd>Space</kbd> anywhere inside braces):
                  {' '}<code>{'{{ $json.name }}'}</code> current item · <code>{"{{ $('Node').first().json.x }}"}</code> another node ·{' '}
                  <code>{'{{ $input.all() }}'}</code> · <code>{'{{ $now }}'}</code> · <code>{'{{ $vars.key }}'}</code> ·{' '}
                  <code>{'{{ $state.key }}'}</code> · <code>{'{{ dumps(x) }}'}</code> · Python/Jinja syntax inside braces.
                  Drag fields from the input panel into the editor.
                </div>
              </div>
              <div className="expr-modal-col">
                <div className="expr-modal-label">Result {data && data.items.length > 0 && <span className="muted">(item 1 of {data.items.length})</span>}</div>
                <pre className={`expr-modal-result ${state}`}>
                  {!data ? 'Open this node from a saved workflow to preview.'
                    : !res ? 'Resolving…'
                    : !res.ok ? res.error
                    : res.text === '' ? '[empty]' : res.type === 'NoneType' ? '[null]' : res.text}
                </pre>
                <ResultLine res={res} data={data} value={value} />
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  )
}

/** Code node: syntax errors while typing (compiled on the server, never run), like n8n's code editor lint. */
export function CodeCheck({ code, language }: { code: string; language: string }) {
  const data = useContext(ExprDataContext)
  const [res, setRes] = useState<{ ok: boolean; checked?: boolean; message?: string } | null>(null)
  useEffect(() => {
    if (!data || language !== 'python') { setRes(null); return }
    let alive = true
    const t = window.setTimeout(() => {
      api.codeCheck(data.workflowId, { language, code }).then((r) => { if (alive) setRes(r) }).catch(() => { if (alive) setRes(null) })
    }, 400)
    return () => { alive = false; window.clearTimeout(t) }
  }, [code, language, data])
  if (!res?.checked) return null
  return res.ok
    ? <div className="code-check ok"><Icon name="circle-check" size={12} /> No syntax errors</div>
    : <div className="code-check bad"><Icon name="circle-alert" size={12} /> {res.message}</div>
}
