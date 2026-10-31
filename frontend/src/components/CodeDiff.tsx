import { MergeView } from '@codemirror/merge'
import { EditorState } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { useEffect, useRef } from 'react'
import { loadLanguage, viewerBasics } from './codemirror'

/** Side-by-side diff like VS Code's: before on the left, after on the right, line numbers, changed lines
 *  and the changed characters inside them highlighted, long unchanged stretches folded. */
export function CodeDiff({ before, after, language }: { before: string; after: string; language?: string }) {
  const host = useRef<HTMLDivElement>(null)
  useEffect(() => {
    let alive = true
    let mv: MergeView | null = null
    loadLanguage(language).then((lang) => {
      if (!alive || !host.current) return
      const ext = [viewerBasics(), lang, EditorState.readOnly.of(true), EditorView.lineWrapping]
      mv = new MergeView({
        a: { doc: before, extensions: ext }, b: { doc: after, extensions: ext }, parent: host.current,
        highlightChanges: true, gutter: true, collapseUnchanged: { margin: 3, minSize: 6 },
      })
    })
    return () => { alive = false; mv?.destroy() }
  }, [before, after, language])
  return <div className="cm-host diff" ref={host} />
}
