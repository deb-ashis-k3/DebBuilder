import { useEffect, useMemo, useRef, useState } from 'react'
import { Icon, fmtDuration, useCatalog } from '../lib'
import type { NodeRun } from '../types'
import { AiLogs } from './AiLogs'
import { DataView } from './DataView'

export interface ExecMeta {
  id: string
  status: string
  mode?: string
  started_at?: string
  duration_ms?: number | null
  error?: string | null
}

const t = (s?: string) => (s ? new Date(s).getTime() : NaN)

/** n8n-style execution log: node runs in order with a timing waterfall + input/output of the selected run. */
export function ExecutionLog({ meta, runs, lines, onClose, onFocusNode, focusNode, height: initialHeight = 320 }: {
  meta: ExecMeta | null
  runs: NodeRun[]
  lines?: string[]
  onClose?: () => void
  onFocusNode?: (nodeId: string) => void
  /** Select this node's latest run (e.g. when clicked on the canvas). */
  focusNode?: string | null
  height?: number
}) {
  const { byType } = useCatalog()
  const [sel, setSel] = useState<number | null>(null)
  const [tab, setTab] = useState<'output' | 'input' | 'ai' | 'console'>('output')
  const [height, setHeight] = useState(initialHeight)
  const [now, setNow] = useState(Date.now())
  const drag = useRef<number | null>(null)

  const running = meta?.status === 'running'
  useEffect(() => {
    if (!running) return
    const id = window.setInterval(() => setNow(Date.now()), 200)
    return () => window.clearInterval(id)
  }, [running])

  // Follow the latest run while executing; reset selection on new executions.
  useEffect(() => { setSel(null) }, [meta?.id])
  useEffect(() => {
    if (!focusNode) return
    const i = runs.findLastIndex((r) => r.node_id === focusNode)
    if (i >= 0) setSel(i)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusNode])
  const selected = sel != null ? runs[sel] : runs.filter((r) => r.status !== 'running').at(-1) ?? runs.at(-1)
  const selIndex = selected ? runs.indexOf(selected) : -1

  const { t0, span } = useMemo(() => {
    const starts = runs.map((r) => t(r.started_at)).filter((x) => !isNaN(x))
    const ends = runs.map((r) => (r.finished_at ? t(r.finished_at) : t(r.started_at) + (r.duration_ms ?? (running ? now - t(r.started_at) : 0))))
      .filter((x) => !isNaN(x))
    const start = starts.length ? Math.min(...starts) : 0
    const end = Math.max(...ends, start + 1)
    return { t0: start, span: Math.max(end - start, 1) }
  }, [runs, now, running])

  const onResize = (e: React.MouseEvent) => {
    drag.current = e.clientY
    const startH = height
    const move = (ev: MouseEvent) => setHeight(Math.min(Math.max(startH + (drag.current! - ev.clientY), 160), window.innerHeight - 160))
    const up = () => { window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up) }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
  }

  const counts = (r: NodeRun) => r.item_counts ?? Object.fromEntries(Object.entries(r.output ?? {}).map(([k, v]) => [k, v.length]))
  const outCount = (r: NodeRun) => Object.values(counts(r)).reduce((a, b) => a + b, 0)
  const ports = selected ? Object.entries(selected.output ?? {}) : []
  const hasAi = !!selected?.sub_runs?.length
  const effTab = tab === 'ai' && !hasAi ? 'output' : tab

  return (
    <div className="exec-log" style={{ height }}>
      <div className="exec-resize" onMouseDown={onResize} />
      <div className="exec-head">
        <Icon name="activity" size={15} />
        <b>Logs</b>
        {meta && (
          <>
            <span className={`badge ${meta.status}`}>{meta.status}</span>
            {meta.mode && <span className="badge plain exec-mode">{meta.mode}</span>}
            <span className="muted small exec-summary">
              {runs.length} node run{runs.length === 1 ? '' : 's'}
              {meta.duration_ms != null ? ` · ${fmtDuration(meta.duration_ms)}` : running && meta.started_at ? ` · ${fmtDuration(now - t(meta.started_at))}` : ''}
              {meta.started_at ? ` · ${new Date(meta.started_at).toLocaleTimeString()}` : ''}
            </span>
          </>
        )}
        <span className="spacer" />
        {onClose && <button className="btn ghost sm icon" onClick={onClose} title="Close logs"><Icon name="x" size={15} /></button>}
      </div>
      {!meta && runs.length === 0 ? (
        <div className="dv-empty">Execute the workflow to see each node's runs, timing and data here.</div>
      ) : (
        <div className="exec-body">
          <div className="exec-runs">
            {meta?.error && <div className="error-box small" style={{ margin: 8 }}>{meta.error}</div>}
            {runs.map((r, i) => {
              const nt = byType[r.type]
              const start = t(r.started_at)
              const dur = r.duration_ms ?? (r.status === 'running' ? now - start : 0)
              const left = ((start - t0) / span) * 100
              const width = Math.max((dur / span) * 100, 0.8)
              const sameNodeRuns = runs.filter((x) => x.node_id === r.node_id).length
              return (
                <div key={`${r.node_id}-${r.run_index}-${i}`} className={`exec-row ${i === selIndex ? 'on' : ''}`}
                  onClick={() => { setSel(i); onFocusNode?.(r.node_id) }}>
                  <span className={`st-dot ${r.status}`}>
                    {r.status === 'running' ? <Icon name="loader-circle" size={12} className="spin" /> :
                      <Icon name={r.status === 'success' ? 'check' : r.status === 'error' ? 'x' : 'pause'} size={11} strokeWidth={3} />}
                  </span>
                  <span className="exec-ico" style={{ '--nc': nt?.color ?? '#888' } as React.CSSProperties}><Icon name={nt?.icon ?? 'box'} size={13} /></span>
                  <span className="exec-name" title={r.name}>
                    {r.name}{sameNodeRuns > 1 && <span className="muted"> #{r.run_index + 1}</span>}
                    {r.pinned && <Icon name="pin" size={11} className="pin-ico" />}
                  </span>
                  <span className="exec-items" title="items in → out">{r.input_count ?? r.input?.length ?? 0} → {r.status === 'running' ? '…' : outCount(r)}</span>
                  <span className="exec-bar"><i className={r.status} style={{ left: `${left}%`, width: `${Math.min(width, 100 - left)}%` }} /></span>
                  <span className="exec-dur">{r.status === 'running' ? fmtDuration(dur) : fmtDuration(r.duration_ms)}</span>
                </div>
              )
            })}
          </div>
          <div className="exec-detail">
            {selected ? (
              <>
                <div className="exec-detail-head">
                  <b>{selected.name}</b>
                  <span className="muted small exec-detail-meta">
                    {selected.status === 'running' ? 'running…' : `${fmtDuration(selected.duration_ms)} · ${new Date(selected.started_at).toLocaleTimeString()}`}
                    {selected.pinned ? ' · pinned data' : ''}
                  </span>
                  <span className="spacer" />
                  <div className="tabs">
                    <button className={`tab ${effTab === 'input' ? 'active' : ''}`} onClick={() => setTab('input')}>Input</button>
                    <button className={`tab ${effTab === 'output' ? 'active' : ''}`} onClick={() => setTab('output')}>Output</button>
                    {hasAi && <button className={`tab ${effTab === 'ai' ? 'active' : ''}`} onClick={() => setTab('ai')}>AI logs</button>}
                    {!!lines?.length && <button className={`tab ${effTab === 'console' ? 'active' : ''}`} onClick={() => setTab('console')}>Console</button>}
                  </div>
                </div>
                <div className="exec-detail-body">
                  {selected.error && <div className="error-box" style={{ marginBottom: 10 }}>{selected.error}</div>}
                  {effTab === 'input' && <DataView items={selected.input} empty="No input data (trigger or not yet loaded)" />}
                  {effTab === 'output' && (ports.length <= 1
                    ? <DataView items={ports[0]?.[1]} empty={selected.status === 'running' ? 'Running…' : 'No output items'} />
                    : ports.map(([p, items]) => (
                      <div key={p} className="port-block"><div className="port-title">Output · {p} <span className="muted">({items.length})</span></div><DataView items={items} empty="No items" /></div>
                    )))}
                  {effTab === 'ai' && hasAi && <AiLogs runs={selected.sub_runs!} />}
                  {effTab === 'console' && <div className="log-lines">{lines?.map((l, i) => <div key={i}>{l}</div>)}</div>}
                </div>
              </>
            ) : <div className="dv-empty">Select a node run</div>}
          </div>
        </div>
      )}
    </div>
  )
}
