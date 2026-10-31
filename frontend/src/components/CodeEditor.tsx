import { linter, lintGutter, type Diagnostic } from '@codemirror/lint'
import { Compartment, EditorState } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Icon } from '../lib'
import { editorBasics, loadLanguage } from './codemirror'

export interface CodeProblem { line: number; column?: number; message: string }

/** VS Code-style code field (CodeMirror 6): syntax highlighting, line numbers, folding, multi-cursor, Ctrl+F,
 *  Ctrl+/ to comment, the server's syntax error underlined in place, and a maximise button for long scripts. */
export default function CodeEditor({ value, onChange, language, rows = 14, problem }: {
  value: string
  onChange: (v: string) => void
  language?: string
  rows?: number
  problem?: CodeProblem | null
}) {
  const slot = useRef<HTMLDivElement>(null)     // where the editor sits in the panel
  const maxBody = useRef<HTMLDivElement>(null)  // where it sits while maximised (portal on <body>)
  // CodeMirror's own element, outside React's tree, so it can move between the two without losing its state.
  const mount = useRef<HTMLDivElement | null>(null)
  if (!mount.current) { mount.current = document.createElement('div'); mount.current.style.height = '100%' }
  const view = useRef<EditorView | null>(null)
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange
  const problemRef = useRef(problem)
  problemRef.current = problem
  const comp = useRef({ lang: new Compartment(), ro: new Compartment(), lint: new Compartment() })
  const [max, setMax] = useState(false)
  const [readOnly, setReadOnly] = useState(false)

  useEffect(() => {
    const el = mount.current!
    // Opened from an execution, the parameters sit in a disabled <fieldset>: CodeMirror must be read-only too.
    const ro = !!slot.current?.closest('fieldset')?.disabled
    setReadOnly(ro)
    const c = comp.current
    const v = new EditorView({
      parent: el,
      state: EditorState.create({
        doc: value,
        extensions: [
          editorBasics(), c.lang.of([]), lintGutter(),
          c.lint.of([]),
          c.ro.of([EditorState.readOnly.of(ro), EditorView.editable.of(!ro)]),
          EditorView.updateListener.of((u) => { if (u.docChanged) onChangeRef.current(u.state.doc.toString()) }),
          EditorView.contentAttributes.of({ 'aria-label': `${language ?? 'Code'} editor`, spellcheck: 'false' }),
        ],
      }),
    })
    view.current = v
    return () => { v.destroy(); view.current = null; el.remove() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    let alive = true
    loadLanguage(language).then((ext) => { if (alive) view.current?.dispatch({ effects: comp.current.lang.reconfigure(ext) }) })
    return () => { alive = false }
  }, [language])

  // Value changed from outside (undo on the canvas, restore a version…): replace the document, keep the cursor if possible.
  useEffect(() => {
    const v = view.current
    if (!v) return
    const cur = v.state.doc.toString()
    if (cur === value) return
    const head = Math.min(v.state.selection.main.head, value.length)
    v.dispatch({ changes: { from: 0, to: cur.length, insert: value }, selection: { anchor: head } })
  }, [value])

  useEffect(() => {
    const v = view.current
    if (!v) return
    v.dispatch({ effects: comp.current.lint.reconfigure(problem ? linter((ev) => {
      const p = problemRef.current
      if (!p) return []
      const doc = ev.state.doc
      const line = doc.line(Math.min(Math.max(p.line, 1), doc.lines))
      const from = Math.min(line.from + Math.max((p.column ?? 1) - 1, 0), line.to)
      const d: Diagnostic = { from, to: from < line.to ? line.to : from, severity: 'error', message: p.message }
      return [d]
    }, { delay: 0 }) : []) })
  }, [problem])

  // Maximised, the editor lives in a fixed layer on <body>: inside the node panel the panel's sticky toolbars
  // and its stacking context would draw over it.
  useLayoutEffect(() => {
    const target = max ? maxBody.current : slot.current
    if (target && mount.current && mount.current.parentElement !== target) target.appendChild(mount.current)
    view.current?.requestMeasure()
  }, [max])

  useEffect(() => {
    if (!max) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.stopPropagation(); setMax(false) } }
    window.addEventListener('keydown', onKey, true)
    requestAnimationFrame(() => view.current?.focus())
    return () => window.removeEventListener('keydown', onKey, true)
  }, [max])

  const height = { '--cm-h': `${Math.max(rows, 4) * 21 + 14}px` } as React.CSSProperties
  return (
    <>
      <div className={`cm-host code-editor ${readOnly ? 'readonly' : ''}`} style={height}>
        {!max && (
          <div className="code-editor-tools">
            <button type="button" className="btn ghost sm icon" title="Maximise editor" onClick={() => setMax(true)}>
              <Icon name="maximize-2" size={14} />
            </button>
          </div>
        )}
        <div ref={slot} style={{ height: '100%' }}>{max && <div className="code-editor-placeholder">Editing in the full-screen editor…</div>}</div>
      </div>
      {max && createPortal(
        <>
          <div className="code-editor-backdrop" onMouseDown={() => setMax(false)} />
          <div className="cm-host code-editor-max" role="dialog" aria-label="Code editor">
            <div className="code-editor-max-head">
              <Icon name="code" size={14} /> <span>{language ?? 'Code'}</span>
              <span className="spacer" />
              <span><kbd>Esc</kbd> to restore</span>
              <button type="button" className="btn ghost sm icon" title="Restore (Esc)" onClick={() => setMax(false)}><Icon name="minimize-2" size={14} /></button>
            </div>
            <div className="code-editor-max-body" ref={maxBody} />
          </div>
        </>,
        document.body,
      )}
    </>
  )
}
