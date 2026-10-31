import { createContext, useContext, useEffect, useState } from 'react'
import { api } from '../api'
import { Icon } from '../lib'
import type { Item } from '../types'

/** What an expression in the open node resolves against: its input items and earlier nodes' outputs (by name). */
export interface ExprData { workflowId: string; items: Item[]; nodeOutputs: Record<string, Item[]> }
export const ExprDataContext = createContext<ExprData | null>(null)

export type ExprResult = { ok: true; type: string; text: string; truncated?: boolean } | { ok: false; error: string }

/** n8n's inline result: resolve the value on the server (same engine as runs), debounced while typing. */
function usePreview(value: string): { res: ExprResult | null; data: ExprData | null } {
  const data = useContext(ExprDataContext)
  const [res, setRes] = useState<ExprResult | null>(null)
  useEffect(() => {
    if (!data) { setRes(null); return }
    let alive = true
    const t = window.setTimeout(() => {
      api.expressionPreview(data.workflowId, { value, items: data.items, node_outputs: data.nodeOutputs })
        .then((r) => { if (alive) setRes(r) })
        .catch((e) => { if (alive) setRes({ ok: false, error: String(e instanceof Error ? e.message : e) }) })
    }, 250)
    return () => { alive = false; window.clearTimeout(t) }
  }, [value, data])
  return { res, data }
}

/** The text with every {{ … }} block coloured: green when it resolves, red when it fails. */
function Highlighted({ text, state }: { text: string; state: 'ok' | 'bad' | 'pending' }) {
  const parts = text.split(/(\{\{[\s\S]*?\}\})/g)
  return (
    <>
      {parts.map((p, i) => (p.startsWith('{{') && p.endsWith('}}')
        ? <span key={i} className={`expr-seg ${state}`}>{p}</span>
        : <span key={i}>{p}</span>))}
      {/* keeps a trailing newline's height, like the textarea */}
      {'​'}
    </>
  )
}

/** Editor + coloured overlay stacked in one grid cell: the highlighted copy sizes the box, so it grows with the text. */
function ExprEditor({ value, onChange, state, big, placeholder, autoFocus }: {
  value: string; onChange: (v: string) => void; state: 'ok' | 'bad' | 'pending'; big?: boolean; placeholder?: string; autoFocus?: boolean
}) {
  return (
    <div className={`expr-edit ${big ? 'big' : ''}`}>
      <pre className="expr-hl" aria-hidden><Highlighted text={value} state={state} /></pre>
      <textarea className="expr-ta" value={value} spellCheck={false} placeholder={placeholder} autoFocus={autoFocus}
        onChange={(e) => onChange(e.target.value)} />
    </div>
  )
}

function ResultLine({ res, data, value }: { res: ExprResult | null; data: ExprData | null; value: string }) {
  if (!data) return null
  const noInput = data.items.length === 0
  if (!res) return <div className="expr-result pending">Resolving…</div>
  if (!res.ok) return <div className="expr-result bad" title={res.error}>{res.error}</div>
  const shown = res.text === '' ? '[empty]' : res.type === 'NoneType' ? '[null]' : res.text
  return (
    <div className="expr-result ok" title={res.text}>
      {shown}{res.truncated ? ' …' : ''}
      {noInput && value.includes('$json') && <span className="expr-note"> · run the previous nodes to preview with real input</span>}
    </div>
  )
}

/** n8n-style expression input: fx badge, coloured {{ }}, live result underneath, expand to a full editor. */
export function ExprField({ value, onChange, placeholder }: { value: string; onChange: (v: string) => void; placeholder?: string }) {
  const { res, data } = usePreview(value)
  const [open, setOpen] = useState(false)
  const state = !res ? 'pending' : res.ok ? 'ok' : 'bad'
  return (
    <>
      <div className={`expr-field ${state}`}>
        <span className="expr-fx" title="Expression">fx</span>
        <ExprEditor value={value} onChange={onChange} state={state} placeholder={placeholder ?? '{{ $json.field }}'} />
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
                <ExprEditor value={value} onChange={onChange} state={state} big autoFocus placeholder="{{ $json.field }}" />
                <div className="expr-help">
                  <code>{'{{ $json.name }}'}</code> current item · <code>{"{{ $('Node').json.x }}"}</code> another node ·{' '}
                  <code>{'{{ $input.all }}'}</code> · <code>{'{{ $now }}'}</code> · <code>{'{{ $vars.key }}'}</code> ·{' '}
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
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
