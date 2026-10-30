import { useEffect, useMemo, useRef, useState } from 'react'
import { Icon, fmtDuration, useCatalog } from '../lib'
import type { Item, LlmMessage, NodeRun, SubRun } from '../types'
import { DataView } from './DataView'
import { allEntries, buildLogTree, entryNodeId, entryStatus, entryType, fmtTokens, visibleRows, type LogEntry } from './logTree'

export interface ExecMeta {
  id: string
  status: string
  mode?: string
  started_at?: string
  duration_ms?: number | null
  error?: string | null
}

const t = (s?: string) => (s ? new Date(s).getTime() : NaN)
const SUB_ICON: Record<SubRun['type'], string> = { llm: 'brain', tool: 'wrench', memory: 'database', retriever: 'search', vector_store: 'database-zap' }

function lsGet(k: string) { try { return localStorage.getItem(k) } catch { return null } }
function lsSet(k: string, v: string) { try { localStorage.setItem(k, v) } catch { /* private mode */ } }

/**
 * n8n-style execution log. Left: every node run as a tree; AI nodes expand into each model, tool and
 * memory call they made (sub-agents nest under their tool call). Right: the selected entry's input
 * and output side by side, with timing and token usage.
 */
export function ExecutionLog({ meta, runs, lines, onClose, onFocusNode, focusNode, height: initialHeight = 320 }: {
  meta: ExecMeta | null
  runs: NodeRun[]
  lines?: string[]
  onClose?: () => void
  onFocusNode?: (nodeId: string) => void
  /** Select this node's latest entry (e.g. when clicked on the canvas). */
  focusNode?: string | null
  height?: number
}) {
  const { byType } = useCatalog()
  const [selKey, setSelKey] = useState<string | null>(null)
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const [view, setView] = useState<'overview' | 'details'>(() => (lsGet('log-view') === 'overview' ? 'overview' : 'details'))
  const [panes, setPanes] = useState<Panes>({ input: true, output: true, console: false, state: false })
  const [height, setHeight] = useState(initialHeight)
  const [now, setNow] = useState(Date.now())
  const drag = useRef<number | null>(null)
  const listRef = useRef<HTMLDivElement>(null)

  const running = meta?.status === 'running'
  useEffect(() => {
    if (!running) return
    const id = window.setInterval(() => setNow(Date.now()), 200)
    return () => window.clearInterval(id)
  }, [running])

  const tree = useMemo(() => buildLogTree(runs), [runs])
  const rows = useMemo(() => visibleRows(tree, collapsed), [tree, collapsed])
  const totalTokens = useMemo(() => tree.reduce((a, e) => a + e.tokens.total, 0), [tree])

  // New execution: start fresh. Canvas click: select that node's latest entry (node run or sub-call).
  useEffect(() => { setSelKey(null); setCollapsed(new Set()) }, [meta?.id])
  useEffect(() => {
    if (!focusNode) return
    const hit = [...allEntries(tree)].filter((e) => entryNodeId(e) === focusNode).at(-1)
    if (hit) {
      setSelKey(hit.key)
      setCollapsed((c) => { const n = new Set(c); for (let p = hit.parent; p; p = p.parent) n.delete(p.key); return n })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusNode])

  const flat = useMemo(() => [...allEntries(tree)], [tree])
  const selected = (selKey && flat.find((e) => e.key === selKey))
    || tree.filter((e) => e.run.status !== 'running').at(-1) || tree.at(-1) || null

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

  const select = (e: LogEntry) => {
    setSelKey(e.key)
    const nid = entryNodeId(e)
    if (nid) onFocusNode?.(nid)
  }
  const toggle = (e: LogEntry, open?: boolean) => setCollapsed((c) => {
    const n = new Set(c)
    if (open ?? n.has(e.key)) n.delete(e.key); else n.add(e.key)
    return n
  })

  // Keyboard: ↑/↓ move, → expand (or step in), ← collapse (or step out) — like n8n's log tree.
  const onKey = (ev: React.KeyboardEvent) => {
    if (!selected) return
    const i = rows.findIndex((r) => r.key === selected.key)
    const go = (e?: LogEntry) => { if (e) { ev.preventDefault(); select(e); requestAnimationFrame(() => listRef.current?.querySelector('.exec-row.on')?.scrollIntoView({ block: 'nearest' })) } }
    if (ev.key === 'ArrowDown') go(rows[i + 1])
    else if (ev.key === 'ArrowUp') go(rows[i - 1])
    else if (ev.key === 'ArrowRight' && selected.children.length) {
      ev.preventDefault()
      if (collapsed.has(selected.key)) toggle(selected, true); else go(selected.children[0])
    } else if (ev.key === 'ArrowLeft') {
      if (selected.children.length && !collapsed.has(selected.key)) { ev.preventDefault(); toggle(selected, false) } else go(selected.parent)
    }
  }

  const counts = (r: NodeRun) => r.item_counts ?? Object.fromEntries(Object.entries(r.output ?? {}).map(([k, v]) => [k, v.length]))
  const outCount = (r: NodeRun) => Object.values(counts(r)).reduce((a, b) => a + b, 0)

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
              {totalTokens > 0 ? ` · ${fmtTokens(totalTokens)} tokens` : ''}
              {meta.started_at ? ` · ${new Date(meta.started_at).toLocaleTimeString()}` : ''}
            </span>
          </>
        )}
        <span className="spacer" />
        {runs.length > 0 && (
          <div className="tabs log-view" role="tablist" aria-label="Log view">
            {(['overview', 'details'] as const).map((v) => (
              <button key={v} role="tab" aria-selected={view === v} className={`tab ${view === v ? 'active' : ''}`}
                onClick={() => { setView(v); lsSet('log-view', v) }}>{v === 'overview' ? 'Overview' : 'Details'}</button>
            ))}
          </div>
        )}
        {onClose && <button className="btn ghost sm icon" onClick={onClose} title="Close logs"><Icon name="x" size={15} /></button>}
      </div>
      {!meta && runs.length === 0 ? (
        <div className="dv-empty">Execute the workflow to see each node's runs, timing and data here.</div>
      ) : (
        <div className="exec-body">
          <div className={`exec-runs ${view}`} ref={listRef} role="tree" tabIndex={0} onKeyDown={onKey} aria-label="Execution log">
            {meta?.error && <div className="error-box small" style={{ margin: 8 }}>{meta.error}</div>}
            {rows.map((e) => {
              const r = e.run
              const status = entryStatus(e)
              const nt = byType[entryType(e) ?? '']
              const startAt = t(e.sub?.started_at ?? r.started_at)
              const dur = e.sub ? e.sub.duration_ms ?? (status === 'running' ? now - startAt : 0) : r.duration_ms ?? (status === 'running' ? now - startAt : 0)
              const left = ((startAt - t0) / span) * 100
              const width = Math.max((dur / span) * 100, 0.8)
              const open = !collapsed.has(e.key)
              const sameNodeRuns = !e.sub ? runs.filter((x) => x.node_id === r.node_id).length : 1
              return (
                <div key={e.key} role="treeitem" aria-expanded={e.children.length ? open : undefined} aria-selected={e.key === selected?.key}
                  className={`exec-row ${e.key === selected?.key ? 'on' : ''} ${status === 'error' ? 'err' : ''}`} onClick={() => select(e)}>
                  <span className="exec-tree" style={{ width: e.depth * 16 + 18 }}>
                    {Array.from({ length: e.depth }, (_, k) => <i key={k} style={{ left: k * 16 + 8 }} />)}
                    {e.children.length > 0 && (
                      <button className="exec-caret" style={{ left: e.depth * 16 }} aria-label={open ? 'Collapse' : 'Expand'}
                        onClick={(ev) => { ev.stopPropagation(); toggle(e) }}>
                        <Icon name={open ? 'chevron-down' : 'chevron-right'} size={13} />
                      </button>
                    )}
                  </span>
                  <span className={`st-dot ${status}`}>
                    {status === 'running' ? <Icon name="loader-circle" size={12} className="spin" /> :
                      <Icon name={status === 'success' ? 'check' : status === 'error' ? 'x' : 'pause'} size={11} strokeWidth={3} />}
                  </span>
                  <span className="exec-ico" style={{ '--nc': nt?.color ?? (e.sub ? '#8b5cf6' : '#888') } as React.CSSProperties}>
                    <Icon name={nt?.icon ?? (e.sub ? SUB_ICON[e.sub.type] : 'box')} size={13} />
                  </span>
                  <span className="exec-name" title={e.sub ? `${e.sub.name}${e.sub.model ? ` · ${e.sub.model}` : ''}` : r.name}>
                    {e.sub ? e.sub.name : r.name}
                    {!e.sub && sameNodeRuns > 1 && <span className="muted"> #{(r.run_index ?? 0) + 1}</span>}
                    {e.sub?.type === 'memory' && e.sub.action && <span className="muted"> · {e.sub.action}</span>}
                    {r.pinned && !e.sub && <Icon name="pin" size={11} className="pin-ico" />}
                  </span>
                  {view === 'details' && (
                    <span className="exec-items" title={e.sub ? 'tokens used' : 'items in → out'}>
                      {e.sub ? (e.tokens.total ? `${fmtTokens(e.tokens.total)} tok` : '') : `${r.input_count ?? r.input?.length ?? 0} → ${r.status === 'running' ? '…' : outCount(r)}`}
                    </span>
                  )}
                  {view === 'details' && <span className="exec-bar"><i className={status} style={{ left: `${left}%`, width: `${Math.min(width, 100 - left)}%` }} /></span>}
                  <span className="exec-dur">{fmtDuration(dur)}</span>
                </div>
              )
            })}
          </div>
          <div className="exec-detail">
            {selected ? (
              <EntryDetails e={selected} panes={panes} setPanes={setPanes} lines={lines} icon={byType[entryType(selected) ?? '']} />
            ) : <div className="dv-empty">Select a node run</div>}
          </div>
        </div>
      )}
    </div>
  )
}

type Panes = { input: boolean; output: boolean; console: boolean; state: boolean }
type PaneKey = keyof Panes
const PANE_TITLE: Record<PaneKey, string> = { input: 'Input', output: 'Output', console: 'Console', state: 'State' }

function EntryDetails({ e, panes, setPanes, lines, icon }: {
  e: LogEntry
  panes: Panes
  setPanes: (p: Panes) => void
  lines?: string[]
  icon?: { icon: string; color: string }
}) {
  const r = e.run
  const s = e.sub
  const status = entryStatus(e)
  const started = s?.started_at ?? r.started_at
  const dur = s ? s.duration_ms : r.duration_ms
  const error = s?.error ?? (s ? undefined : r.error)
  // Graph state after this node (sub-calls show their AI node's): LangGraph Studio's state view.
  const hasState = !!r.state && Object.keys(r.state).length > 0
  const available = (k: PaneKey) => (k === 'console' ? !!lines?.length : k === 'state' ? hasState : true)
  // At least one visible pane stays open (n8n's Input / Output toggles).
  const flip = (k: PaneKey) => {
    const next = { ...panes, [k]: !panes[k] }
    if ((Object.keys(next) as PaneKey[]).some((x) => next[x] && available(x))) setPanes(next)
  }
  const shown = (['input', 'output', 'state', 'console'] as const).filter((k) => panes[k] && available(k))
  return (
    <>
      <div className="exec-detail-head">
        <span className="exec-ico" style={{ '--nc': icon?.color ?? '#8b5cf6' } as React.CSSProperties}>
          <Icon name={icon?.icon ?? (s ? SUB_ICON[s.type] : 'box')} size={13} />
        </span>
        <b>{s ? s.name : r.name}</b>
        <span className={`muted small exec-detail-meta ${status === 'error' ? 'err' : ''}`}>
          {status === 'running' ? 'Running…' : `${status === 'error' ? 'Error' : status === 'waiting' ? 'Waiting' : 'Success'} in ${fmtDuration(dur)}`}
          {e.tokens.total > 0 && <span title={`Prompt ${fmtTokens(e.tokens.input)} · Completion ${fmtTokens(e.tokens.output)}`}> · {fmtTokens(e.tokens.total)} tokens</span>}
          {s?.model && <> · {s.model}</>}
          {started && <> · {new Date(started).toLocaleTimeString()}</>}
          {!s && r.pinned ? ' · pinned data' : ''}
        </span>
        <span className="spacer" />
        <div className="pane-toggles">
          <button className={`btn sm ${panes.input ? 'on' : ''}`} aria-pressed={panes.input} onClick={() => flip('input')}>Input</button>
          <button className={`btn sm ${panes.output ? 'on' : ''}`} aria-pressed={panes.output} onClick={() => flip('output')}>Output</button>
          {hasState && <button className={`btn sm ${panes.state ? 'on' : ''}`} aria-pressed={panes.state} onClick={() => flip('state')}
            title="Graph state after this step">State</button>}
          {!!lines?.length && <button className={`btn sm ${panes.console ? 'on' : ''}`} aria-pressed={panes.console} onClick={() => flip('console')}>Console</button>}
        </div>
      </div>
      {error && <div className="error-box" style={{ margin: '10px 14px 0' }}>{error}</div>}
      <div className="exec-panes" style={{ gridTemplateColumns: `repeat(${Math.max(shown.length, 1)}, minmax(0, 1fr))` }}>
        {shown.map((k) => (
          <div key={k} className="exec-pane">
            <div className="exec-pane-title">{PANE_TITLE[k]}</div>
            <div className="exec-pane-body">
              {k === 'console' ? <div className="log-lines">{lines?.map((l, i) => <div key={i}>{l}</div>)}</div>
                : k === 'state' ? <StateView state={r.state!} changed={s ? [] : r.state_updates ?? []} />
                : s ? <SubRunData s={s} which={k} />
                  : k === 'input' ? <DataView items={r.input} empty="No input data (trigger or not yet loaded)" />
                    : <NodeOutput r={r} />}
            </div>
          </div>
        ))}
      </div>
    </>
  )
}

/** Graph state keys and values; keys this step wrote are marked. */
function StateView({ state, changed }: { state: Record<string, unknown>; changed: string[] }) {
  return (
    <div className="state-view">
      {Object.entries(state).map(([k, v]) => (
        <div key={k} className={`state-row ${changed.includes(k) ? 'changed' : ''}`}>
          <div className="state-key"><code>$state.{k}</code>{changed.includes(k) && <span className="badge plain">updated</span>}</div>
          {isMessages(v) ? <Messages msgs={v} /> : <Payload v={v} />}
        </div>
      ))}
    </div>
  )
}

function NodeOutput({ r }: { r: NodeRun }) {
  const ports = Object.entries(r.output ?? {})
  if (ports.length <= 1) return <DataView items={ports[0]?.[1]} empty={r.status === 'running' ? 'Running…' : 'No output items'} />
  return <>{ports.map(([p, items]) => (
    <div key={p} className="port-block"><div className="port-title">Output · {p} <span className="muted">({items.length})</span></div><DataView items={items} empty="No items" /></div>
  ))}</>
}

const isMessages = (v: unknown): v is LlmMessage[] => Array.isArray(v) && v.every((m) => m && typeof m === 'object' && 'role' in m && 'content' in m)
const ROLE: Record<string, string> = { system: 'System', human: 'Human', ai: 'AI', tool: 'Tool', user: 'Human', assistant: 'AI' }

function Messages({ msgs }: { msgs: LlmMessage[] }) {
  if (!msgs.length) return <span className="muted small">No messages</span>
  return (
    <div className="llm-msgs">
      {msgs.map((m, i) => (
        <div key={i} className={`llm-msg ${m.role}`}>
          <div className="llm-role">{ROLE[m.role] ?? m.role}{m.name ? ` · ${m.name}` : ''}</div>
          {m.content && <pre className="ai-pre">{m.content}</pre>}
          {m.tool_calls?.map((c, k) => <ToolCall key={k} name={c.name} args={c.args} />)}
        </div>
      ))}
    </div>
  )
}

function ToolCall({ name, args }: { name: string; args: unknown }) {
  return (
    <div className="llm-call"><Icon name="wrench" size={12} /> <b>{name}</b><pre className="ai-pre">{JSON.stringify(args ?? {}, null, 2)}</pre></div>
  )
}

/** Tool / memory payloads: JSON objects as a data table, JSON text parsed when possible, else plain text. */
function Payload({ v }: { v: unknown }) {
  let val = v
  if (typeof v === 'string') {
    const s = v.trim()
    if ((s.startsWith('{') && s.endsWith('}')) || (s.startsWith('[') && s.endsWith(']'))) {
      try { val = JSON.parse(s) } catch { /* not JSON */ }
    }
  }
  if (val == null || val === '') return <span className="muted small">(empty)</span>
  if (typeof val !== 'object') return <pre className="ai-pre">{String(val)}</pre>
  const items = (Array.isArray(val) ? val : [val]) as unknown[]
  if (items.every((x) => x && typeof x === 'object' && !Array.isArray(x))) return <DataView items={items as Item[]} empty="(empty)" />
  return <pre className="ai-pre">{JSON.stringify(val, null, 2)}</pre>
}

export function SubRunData({ s, which }: { s: SubRun; which: 'input' | 'output' }) {
  if (which === 'input') {
    if (s.type === 'llm' && isMessages(s.input)) return <Messages msgs={s.input} />
    return <Payload v={s.input} />
  }
  if (s.status === 'running') return <span className="muted small">Running…</span>
  if (s.error) return <span className="muted small">No output (failed)</span>
  if (s.type === 'llm' && s.output && typeof s.output === 'object' && !Array.isArray(s.output)) {
    const o = s.output as { text?: string; tool_calls?: { name: string; args: unknown }[] }
    return (
      <div className="llm-msgs">
        <div className="llm-msg ai">
          <div className="llm-role">AI</div>
          {o.text && <pre className="ai-pre">{o.text}</pre>}
          {o.tool_calls?.map((c, k) => <ToolCall key={k} name={c.name} args={c.args} />)}
          {!o.text && !o.tool_calls?.length && <span className="muted small">(empty response)</span>}
        </div>
      </div>
    )
  }
  const out = s.output as Record<string, unknown> | undefined
  if (s.type === 'memory' && out && isMessages(out.chat_history)) return <Messages msgs={out.chat_history} />
  return <Payload v={s.output} />
}
