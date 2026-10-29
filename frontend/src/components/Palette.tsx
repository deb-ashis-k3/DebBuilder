import { useEffect, useMemo, useRef, useState } from 'react'
import { GROUPS, Icon, useCatalog } from '../lib'
import type { NodeTypeSpec } from '../types'

export function Palette({ filter, onPick, onClose }: {
  /** Restrict to a group (e.g. "ai_tool" when dragging from an AI handle). */
  filter?: (t: NodeTypeSpec) => boolean
  onPick: (t: NodeTypeSpec) => void
  onClose: () => void
}) {
  const { list } = useCatalog()
  const [q, setQ] = useState('')
  const ref = useRef<HTMLInputElement>(null)
  useEffect(() => ref.current?.focus(), [])

  const groups = useMemo(() => {
    const needle = q.toLowerCase()
    const visible = list.filter((t) => (!filter || filter(t)) &&
      (!needle || `${t.label} ${t.description} ${t.type}`.toLowerCase().includes(needle)))
    return GROUPS.map((g) => ({ ...g, items: visible.filter((t) => t.group === g.id) })).filter((g) => g.items.length)
  }, [list, q, filter])

  return (
    <div className="palette" onKeyDown={(e) => e.key === 'Escape' && onClose()}>
      <div className="palette-head">
        <div className="row">
          <b className="grow">Add a node</b>
          <button className="btn ghost sm icon" onClick={onClose}><Icon name="x" size={16} /></button>
        </div>
        <div className="search">
          <Icon name="search" size={15} />
          <input ref={ref} className="input" placeholder={`Search ${list.length} nodes…  (Enter adds the first)`} value={q} onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && groups[0]?.items[0]) onPick(groups[0].items[0]) }} />
        </div>
      </div>
      <div className="palette-list">
        {groups.map((g) => (
          <div key={g.id}>
            <div className="palette-group">{g.label}</div>
            {g.items.map((t) => (
              <div key={t.type} className="palette-item" draggable
                onDragStart={(e) => { e.dataTransfer.setData('application/debbuilder-node', t.type); e.dataTransfer.effectAllowed = 'move' }}
                onClick={() => onPick(t)}>
                <div className="p-icon" style={{ '--nc': t.color } as React.CSSProperties}><Icon name={t.icon} size={17} /></div>
                <div>
                  <div className="p-title">{t.label}</div>
                  <div className="p-desc">{t.description}</div>
                </div>
              </div>
            ))}
          </div>
        ))}
        {groups.length === 0 && <div className="empty small">No nodes match</div>}
      </div>
    </div>
  )
}
