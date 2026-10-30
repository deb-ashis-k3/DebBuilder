import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Icon } from '../lib'

type Pt = { x: number; y: number }
export type MenuTarget =
  | { kind: 'nodes'; x: number; y: number; ids: string[] }
  | { kind: 'pane'; x: number; y: number; at: Pt }

export interface CanvasActions {
  open: (id: string) => void
  execute: (id: string) => void
  copy: () => void
  cut: () => void
  duplicate: () => void
  toggleDisabled: (ids: string[]) => void
  remove: () => void
  tidy: () => void
  selectAll: () => void
  paste: (at: Pt) => void
  addNode: (at: Pt) => void
  addSticky: (at: Pt) => void
  fitView: () => void
}

interface Item { label: string; icon: string; keys?: string; danger?: boolean; run: () => void; divider?: false }
type Entry = Item | { divider: true }

/** Right-click menu on nodes, a selection, or the empty canvas (n8n's canvas context menu). */
export function CanvasContextMenu({ target, actions, readOnly, onClose, nodeCount, disabledAll }: {
  target: MenuTarget; actions: CanvasActions; readOnly: boolean; onClose: () => void; nodeCount: number; disabledAll: boolean
}) {
  const ref = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState({ left: target.x, top: target.y })

  // Keep the menu on screen.
  useLayoutEffect(() => {
    const r = ref.current?.getBoundingClientRect()
    if (!r) return
    setPos({ left: Math.min(target.x, window.innerWidth - r.width - 8), top: Math.min(target.y, window.innerHeight - r.height - 8) })
  }, [target])
  useEffect(() => {
    const away = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) onClose() }
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('mousedown', away)
    document.addEventListener('keydown', esc)
    window.addEventListener('wheel', onClose, { passive: true })
    return () => {
      document.removeEventListener('mousedown', away)
      document.removeEventListener('keydown', esc)
      window.removeEventListener('wheel', onClose)
    }
  }, [onClose])

  const entries: Entry[] = []
  if (target.kind === 'nodes') {
    const one = target.ids.length === 1
    if (one) {
      entries.push({ label: 'Open', icon: 'square-arrow-out-up-right', keys: 'Enter', run: () => actions.open(target.ids[0]) })
      entries.push({ label: 'Execute step', icon: 'play', run: () => actions.execute(target.ids[0]) })
      entries.push({ divider: true })
    }
    entries.push({ label: 'Copy', icon: 'copy', keys: 'Ctrl+C', run: actions.copy })
    if (!readOnly) {
      entries.push({ label: 'Cut', icon: 'scissors', keys: 'Ctrl+X', run: actions.cut })
      entries.push({ label: 'Duplicate', icon: 'copy-plus', keys: 'Ctrl+D', run: actions.duplicate })
      entries.push({ label: disabledAll ? 'Activate' : 'Deactivate', icon: disabledAll ? 'power' : 'power-off', keys: 'D', run: () => actions.toggleDisabled(target.ids) })
      if (!one) entries.push({ label: 'Tidy up selection', icon: 'wand-sparkles', keys: 'Shift+Alt+T', run: actions.tidy })
    }
    entries.push({ label: 'Select all', icon: 'box-select', keys: 'Ctrl+A', run: actions.selectAll })
    if (!readOnly) entries.push({ divider: true }, { label: one ? 'Delete' : `Delete ${target.ids.length} nodes`, icon: 'trash-2', keys: 'Del', danger: true, run: actions.remove })
  } else {
    if (!readOnly) {
      entries.push({ label: 'Add node', icon: 'plus', keys: 'Tab', run: () => actions.addNode(target.at) })
      entries.push({ label: 'Add sticky note', icon: 'sticky-note', keys: 'Shift+S', run: () => actions.addSticky(target.at) })
      entries.push({ label: 'Paste', icon: 'clipboard-paste', keys: 'Ctrl+V', run: () => actions.paste(target.at) })
      entries.push({ divider: true })
    }
    entries.push({ label: 'Select all', icon: 'box-select', keys: 'Ctrl+A', run: actions.selectAll })
    if (!readOnly && nodeCount > 1) entries.push({ label: 'Tidy up workflow', icon: 'wand-sparkles', keys: 'Shift+Alt+T', run: actions.tidy })
    entries.push({ label: 'Fit view', icon: 'maximize', keys: '1', run: actions.fitView })
  }

  return (
    <div ref={ref} className="act-menu canvas-menu" role="menu" style={{ position: 'fixed', ...pos }} onContextMenu={(e) => e.preventDefault()}>
      {entries.map((it, i) => ('divider' in it && it.divider ? <div key={i} className="act-sep" /> : (
        <button key={i} role="menuitem" className={`act-item ${(it as Item).danger ? 'danger' : ''}`}
          onClick={() => { onClose(); (it as Item).run() }}>
          <Icon name={(it as Item).icon} size={14} />
          <span className="grow">{(it as Item).label}</span>
          {(it as Item).keys && <span className="kbd">{(it as Item).keys}</span>}
        </button>
      )))}
    </div>
  )
}

/** Floating bar while 2+ nodes are selected (n8n's selection toolbar). */
export function SelectionToolbar({ count, onCopy, onDuplicate, onTidy, onDisable, onDelete }: {
  count: number; onCopy: () => void; onDuplicate: () => void; onTidy: () => void; onDisable: () => void; onDelete: () => void
}) {
  if (count < 2) return null
  return (
    <div className="sel-toolbar" role="toolbar" aria-label="Selection">
      <b>{count} selected</b>
      <button className="btn ghost sm" onClick={onCopy} title="Copy (Ctrl+C)"><Icon name="copy" size={14} /> Copy</button>
      <button className="btn ghost sm" onClick={onDuplicate} title="Duplicate (Ctrl+D)"><Icon name="copy-plus" size={14} /> Duplicate</button>
      <button className="btn ghost sm" onClick={onTidy} title="Tidy up selection (Shift+Alt+T)"><Icon name="wand-sparkles" size={14} /> Tidy up</button>
      <button className="btn ghost sm" onClick={onDisable} title="Deactivate / activate (D)"><Icon name="power-off" size={14} /></button>
      <button className="btn ghost sm danger" onClick={onDelete} title="Delete (Del)"><Icon name="trash-2" size={14} /></button>
    </div>
  )
}
