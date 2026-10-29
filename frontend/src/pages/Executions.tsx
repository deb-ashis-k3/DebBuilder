import { Background, BackgroundVariant, Controls, ReactFlow, ReactFlowProvider } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api } from '../api'
import { edgeTypes } from '../components/CanvasEdge'
import { Menu, confirmDialog, errText } from '../components/Dialogs'
import { ExecutionLog } from '../components/ExecutionLog'
import { nodeTypes } from '../components/FlowNode'
import { Select } from '../components/Select'
import { Icon, StatusBadge, fmtDate, fmtDuration, toast } from '../lib'
import type { Execution, ExecutionFilter, Workflow } from '../types'
import { edgeToRF, runStateFromExecution, toRF } from './Editor'
import { useAuth } from '../auth'

const PAGE = 50
const STATUSES = ['success', 'error', 'running', 'waiting', 'canceled']
const MODES = ['manual', 'trigger', 'webhook', 'schedule', 'chat', 'form', 'whatsapp', 'voice', 'subworkflow', 'error']

const STATUS_TEXT: Record<string, string> = {
  success: 'Succeeded in', error: 'Failed in', running: 'Running', waiting: 'Waiting', canceled: 'Canceled after',
}

export function statusLine(ex: Execution) {
  const d = ex.duration_ms != null ? fmtDuration(ex.duration_ms) : ''
  return `${STATUS_TEXT[ex.status] ?? ex.status}${d && ex.status !== 'running' && ex.status !== 'waiting' ? ` ${d}` : ''}`
}

/** datetime-local value <-> ISO string */
const toLocal = (iso?: string) => (iso ? new Date(iso).toLocaleString('sv-SE').slice(0, 16).replace(' ', 'T') : '')
const fromLocal = (v: string) => (v ? new Date(v).toISOString() : '')

// ===========================================================================
// Global execution list (checkboxes, batch actions, filters)
// ===========================================================================

export function ExecutionList() {
  const canDelete = useAuth().can('execution:write')
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const filter: ExecutionFilter = {
    workflow_id: params.get('workflow_id') ?? undefined,
    status: params.get('status') ?? undefined,
    mode: params.get('mode') ?? undefined,
    started_after: params.get('from') ?? undefined,
    started_before: params.get('to') ?? undefined,
  }
  const filterKey = params.toString()
  const [data, setData] = useState<{ total: number; items: Execution[] } | null>(null)
  const [workflows, setWorkflows] = useState<Workflow[]>([])
  const [page, setPage] = useState(0)
  const [auto, setAuto] = useState(true)
  const [sel, setSel] = useState<Set<string>>(new Set())
  const [allMatching, setAllMatching] = useState(false)
  const [tick, setTick] = useState(0)

  useEffect(() => { api.workflows().then(setWorkflows) }, [])
  useEffect(() => { setSel(new Set()); setAllMatching(false); setPage(0) }, [filterKey])
  useEffect(() => {
    let alive = true
    const load = () => api.executions({ ...filter, limit: PAGE, offset: page * PAGE }).then((d) => alive && setData(d))
    load()
    const t = auto ? window.setInterval(load, 4000) : undefined
    return () => { alive = false; window.clearInterval(t) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filterKey, page, auto, tick])

  const setParam = (k: string, v: string) => {
    if (v) params.set(k, v); else params.delete(k)
    setParams(params, { replace: true })
  }
  const anyFilter = ['workflow_id', 'status', 'mode', 'from', 'to'].some((k) => params.get(k))
  const items = data?.items ?? []
  const pageAll = items.length > 0 && items.every((x) => sel.has(x.id))
  const count = allMatching ? data?.total ?? 0 : sel.size
  const selection = () => (allMatching ? { filter } : { ids: [...sel] })
  const toggle = (id: string) => { setAllMatching(false); setSel((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n }) }
  const clearSel = () => { setSel(new Set()); setAllMatching(false) }

  const bulkDelete = async () => {
    if (!await confirmDialog({
      title: `Delete ${count} execution${count === 1 ? '' : 's'}?`,
      message: 'Running executions in the selection are stopped first. This cannot be undone.', confirmLabel: 'Delete', danger: true,
    })) return
    try {
      const r = await api.deleteExecutions(selection())
      toast(`Deleted ${r.deleted} execution${r.deleted === 1 ? '' : 's'}`)
      clearSel(); setTick((t) => t + 1)
    } catch (e) { toast(`Delete failed: ${errText(e)}`, 'error') }
  }
  const bulkStop = async () => {
    try {
      const r = await api.stopExecutions(selection())
      toast(r.stopped ? `Stopped ${r.stopped} execution${r.stopped === 1 ? '' : 's'}` : 'Nothing running in the selection')
      clearSel(); setTick((t) => t + 1)
    } catch (e) { toast(`Stop failed: ${errText(e)}`, 'error') }
  }
  const open = (ex: Execution) => navigate(`/workflow/${ex.workflow_id}/executions/${ex.id}`)

  return (
    <div className="page">
      <div className="page-inner">
        <div className="page-head">
          <h1>Executions</h1>
          <label className="exv-auto inline"><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> Auto refresh</label>
        </div>

        <div className="ex-filters">
          <Select className="ex-filter wide" value={filter.workflow_id ?? ''} onChange={(v) => setParam('workflow_id', v)} title="Workflow" searchable
            options={[{ value: '', label: 'All workflows' }, ...workflows.map((w) => ({ value: w.id, label: w.name }))]} />
          <Select className="ex-filter" value={filter.status ?? ''} onChange={(v) => setParam('status', v)} title="Status"
            options={[{ value: '', label: 'Any status' }, ...STATUSES.map((s) => ({ value: s, label: s[0].toUpperCase() + s.slice(1) }))]} />
          <Select className="ex-filter" value={filter.mode ?? ''} onChange={(v) => setParam('mode', v)} title="Mode"
            options={[{ value: '', label: 'Any mode' }, ...MODES.map((m) => ({ value: m, label: m[0].toUpperCase() + m.slice(1) }))]} />
          <label className="ex-date"><span>From</span>
            <input className="input" type="datetime-local" value={toLocal(filter.started_after)} onChange={(e) => setParam('from', fromLocal(e.target.value))} /></label>
          <label className="ex-date"><span>To</span>
            <input className="input" type="datetime-local" value={toLocal(filter.started_before)} onChange={(e) => setParam('to', fromLocal(e.target.value))} /></label>
          {anyFilter && <button className="btn ghost sm" onClick={() => setParams({}, { replace: true })}><Icon name="x" size={14} /> Clear filters</button>}
        </div>

        {count > 0 && canDelete && (
          <div className="bulk-bar">
            <b>{count} selected</b>
            <button className="btn sm" onClick={bulkStop}><Icon name="square" size={13} /> Stop</button>
            <button className="btn sm danger" onClick={bulkDelete}><Icon name="trash-2" size={14} /> Delete</button>
            <span className="spacer" />
            <button className="btn ghost sm" onClick={clearSel}>Clear selection</button>
          </div>
        )}
        {pageAll && !allMatching && data && data.total > items.length && (
          <div className="select-all-banner">
            All {items.length} executions on this page are selected.
            <button className="link-btn" onClick={() => setAllMatching(true)}>Select all {data.total} matching executions</button>
          </div>
        )}
        {allMatching && data && (
          <div className="select-all-banner">
            All {data.total} executions matching the filters are selected. <button className="link-btn" onClick={clearSel}>Clear selection</button>
          </div>
        )}

        <div className="table-wrap"><table className="table exec-table">
          <thead><tr>
            <th className="col-check"><input type="checkbox" checked={pageAll || allMatching} aria-label="Select page"
              onChange={() => { setAllMatching(false); setSel(pageAll ? new Set() : new Set(items.map((x) => x.id))) }} /></th>
            <th>Workflow</th><th>Status</th><th>Started</th><th className="hide-sm">Run time</th><th className="hide-md">Mode</th><th className="hide-md">ID</th><th />
          </tr></thead>
          <tbody>
            {items.map((ex) => (
              <tr key={ex.id} className={`clickable ${sel.has(ex.id) || allMatching ? 'selected' : ''}`} onClick={() => open(ex)}>
                <td className="col-check" onClick={(e) => e.stopPropagation()}>
                  <input type="checkbox" checked={allMatching || sel.has(ex.id)} onChange={() => toggle(ex.id)} aria-label="Select execution" />
                </td>
                <td><Link to={`/workflow/${ex.workflow_id}`} onClick={(e) => e.stopPropagation()} className="ex-wf">{ex.workflow_name}</Link></td>
                <td><StatusBadge status={ex.status} /></td>
                <td className="muted">{fmtDate(ex.started_at)}</td>
                <td className="muted hide-sm">{ex.status === 'running' ? 'running…' : fmtDuration(ex.duration_ms)}</td>
                <td className="hide-md"><span className="badge">{ex.mode}</span></td>
                <td className="muted small hide-md"><code>{ex.id.slice(0, 8)}</code></td>
                <td className="col-act" onClick={(e) => e.stopPropagation()}>
                  <Menu items={[
                    { label: 'Open', icon: 'square-arrow-out-up-right', onClick: () => open(ex) },
                    { label: 'Retry', icon: 'rotate-ccw', onClick: async () => { const r = await api.retry(ex.id); navigate(`/workflow/${ex.workflow_id}/executions/${r.execution_id}`) } },
                    ...(ex.status === 'running' || ex.status === 'waiting' ? [{ label: 'Stop', icon: 'square', onClick: async () => { await api.cancel(ex.id); setTick((t) => t + 1) } }] : []),
                    { divider: true, label: '' },
                    { label: 'Delete', icon: 'trash-2', danger: true, onClick: async () => { await api.deleteExecution(ex.id); setTick((t) => t + 1) } },
                  ]} />
                </td>
              </tr>
            ))}
            {data && items.length === 0 && <tr><td colSpan={8} className="empty">{anyFilter ? 'No executions match these filters' : 'No executions yet'}</td></tr>}
          </tbody>
        </table></div>
        {data && data.total > PAGE && (
          <div className="row" style={{ marginTop: 12, justifyContent: 'flex-end' }}>
            <button className="btn sm" disabled={page === 0} onClick={() => setPage(page - 1)}>Prev</button>
            <span className="muted small">{page * PAGE + 1}–{Math.min((page + 1) * PAGE, data.total)} of {data.total}</span>
            <button className="btn sm" disabled={(page + 1) * PAGE >= data.total} onClick={() => setPage(page + 1)}>Next</button>
          </div>
        )}
      </div>
    </div>
  )
}

// ===========================================================================
// Workflow executions tab: list + preview (n8n's "Executions" tab)
// ===========================================================================

function ExecutionSidebar({ workflowId, current, onSelect, reloadKey }: {
  workflowId: string; current: string | null; onSelect: (id: string) => void; reloadKey: number
}) {
  const [items, setItems] = useState<Execution[] | null>(null)
  const [auto, setAuto] = useState(true)
  const [status, setStatus] = useState('')
  const [sel, setSel] = useState<Set<string>>(new Set())
  const [tick, setTick] = useState(0)

  useEffect(() => {
    let alive = true
    const load = () => api.executions({ workflow_id: workflowId, status, limit: 100, offset: 0 }).then((d) => alive && setItems(d.items))
    load()
    const t = auto ? window.setInterval(load, 4000) : undefined
    return () => { alive = false; window.clearInterval(t) }
  }, [workflowId, status, auto, reloadKey, tick])

  // Select the newest execution when none is chosen (n8n opens the latest run).
  useEffect(() => { if (!current && items?.length) onSelect(items[0].id) }, [current, items, onSelect])

  const del = async () => {
    if (!await confirmDialog({ title: `Delete ${sel.size} execution${sel.size === 1 ? '' : 's'}?`, confirmLabel: 'Delete', danger: true })) return
    try {
      await api.deleteExecutions({ ids: [...sel] })
      setSel(new Set()); setTick((t) => t + 1)
    } catch (e) { toast(`Delete failed: ${errText(e)}`, 'error') }
  }
  return (
    <aside className="exv-list">
      <div className="exv-list-head">
        <b>Executions</b>
        <span className="spacer" />
        <Select className="exv-filter" value={status} onChange={setStatus} title="Filter by status"
          options={['', ...STATUSES].map((s) => ({ value: s, label: s ? s[0].toUpperCase() + s.slice(1) : 'All' }))} />
      </div>
      <div className="exv-tools">
        <label className="exv-auto"><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> Auto refresh</label>
        {sel.size > 0 && <button className="btn sm danger" onClick={del}><Icon name="trash-2" size={13} /> Delete {sel.size}</button>}
      </div>
      <div className="exv-items">
        {items?.map((x) => (
          <div key={x.id} className={`exv-item st-${x.status} ${x.id === current ? 'on' : ''}`} onClick={() => onSelect(x.id)} role="button" tabIndex={0}
            onKeyDown={(e) => e.key === 'Enter' && onSelect(x.id)}>
            <input type="checkbox" className="exv-check" checked={sel.has(x.id)} onClick={(e) => e.stopPropagation()} aria-label="Select execution"
              onChange={() => setSel((s) => { const n = new Set(s); if (n.has(x.id)) n.delete(x.id); else n.add(x.id); return n })} />
            <div className="exv-item-text">
              <b>{fmtDate(x.started_at)}</b>
              <span>{statusLine(x)}{x.mode === 'manual' ? ' · manual' : ''}</span>
            </div>
          </div>
        ))}
        {items && items.length === 0 && (
          <div className="dv-empty">No executions{status ? ` with status "${status}"` : ' yet'}.<br />Run the workflow or activate it.</div>
        )}
      </div>
    </aside>
  )
}

function ExecutionPreview({ execId, onChanged, onDebug, onSelect }: {
  execId: string; onChanged: () => void; onDebug?: (ex: Execution) => void; onSelect: (id: string | null) => void
}) {
  const [ex, setEx] = useState<Execution | null>(null)
  const [missing, setMissing] = useState(false)
  const [sel, setSel] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    setMissing(false)
    const load = () => api.execution(execId).then((d) => alive && setEx(d)).catch(() => alive && setMissing(true))
    load()
    const t = window.setInterval(() => { if (ex?.status === 'running' || ex?.status === 'waiting') load() }, 1500)
    return () => { alive = false; window.clearInterval(t) }
  }, [execId, ex?.status])
  useEffect(() => { setSel(null) }, [execId])

  const runs = useMemo(() => (ex ? runStateFromExecution(ex) : {}), [ex])
  const nodes = useMemo(() => (ex?.workflow?.nodes ?? []).map((n) => toRF(n, runs[n.id])), [ex, runs])
  const edges = useMemo(() => (ex?.workflow?.edges ?? []).map((e) => {
    const c = runs[e.source]?.counts?.[e.sourceHandle ?? 'main']
    return { ...edgeToRF(e), type: 'canvas', ...(c ? { label: `${c} item${c === 1 ? '' : 's'}` } : {}) }
  }), [ex, runs])

  if (missing) return <section className="exv-main"><div className="dv-empty">This execution no longer exists.</div></section>
  if (!ex || ex.id !== execId) return <section className="exv-main"><div className="dv-empty">Loading…</div></section>

  const act = async (fn: () => Promise<unknown>, msg: string) => {
    try { await fn(); toast(msg); setEx(await api.execution(ex.id)); onChanged() } catch (e) { toast(String(e), 'error') }
  }

  return (
    <section className="exv-main">
      <div className="exv-head">
        <div className="exv-title">
          <b>{fmtDate(ex.started_at)}</b>
          <span className="small">
            <span className={`exv-st st-${ex.status}`}>{statusLine(ex)}</span>
            <span className="muted"> | {ex.mode} | ID#{ex.id.slice(0, 8)}</span>
          </span>
        </div>
        <div className="exv-actions">
          {ex.status === 'waiting' && (
            <>
              <span className="small exv-wait"><b>{ex.waiting_for?.node_name}:</b> {ex.waiting_for?.message}</span>
              <button className="btn sm primary" onClick={() => act(() => api.resume(ex.id, true), 'Approved')}>Approve</button>
              <button className="btn sm danger" onClick={() => act(() => api.resume(ex.id, false), 'Rejected')}>Reject</button>
            </>
          )}
          {(ex.status === 'running' || ex.status === 'waiting') && <button className="btn sm" onClick={() => act(() => api.cancel(ex.id), 'Stopped')}><Icon name="square" size={13} /> Stop</button>}
          {onDebug && (
            <button className="btn sm" title="Load this run's trigger data into the editor (pinned) to debug it"
              onClick={() => onDebug(ex)}><Icon name="bug-play" size={14} /> <span className="btn-text">Debug in editor</span></button>
          )}
          <button className="btn sm" title="Retry" onClick={async () => { const r = await api.retry(ex.id); onChanged(); onSelect(r.execution_id) }}>
            <Icon name="rotate-ccw" size={14} /> <span className="btn-text">Retry</span></button>
          <button className="btn sm icon" title="Delete execution" onClick={async () => { await api.deleteExecution(ex.id); onChanged(); onSelect(null) }}>
            <Icon name="trash-2" size={14} /></button>
        </div>
      </div>
      <div className="exv-canvas canvas">
        <ReactFlowProvider>
          <ReactFlow key={ex.id} nodes={nodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes} fitView fitViewOptions={{ padding: 0.2, maxZoom: 1 }}
            nodesDraggable={false} nodesConnectable={false} onNodeClick={(_, n) => setSel(n.id)} minZoom={0.1} proOptions={{ hideAttribution: true }}>
            <Background variant={BackgroundVariant.Dots} gap={20} size={1.4} color="var(--canvas-dot)" />
            <Controls showInteractive={false} orientation="horizontal" />
          </ReactFlow>
        </ReactFlowProvider>
      </div>
      <ExecutionLog height={340} focusNode={sel} runs={ex.node_runs ?? []}
        meta={{ id: ex.id, status: ex.status, mode: ex.mode, started_at: ex.started_at, duration_ms: ex.duration_ms, error: ex.error }} />
    </section>
  )
}

/** List of a workflow's executions with the selected run's canvas + logs (used by the editor's Executions tab). */
export function WorkflowExecutionsView({ workflowId, execId, onSelect, onDebug }: {
  workflowId: string; execId: string | null; onSelect: (id: string | null) => void; onDebug?: (ex: Execution) => void
}) {
  const [reloadKey, setReloadKey] = useState(0)
  return (
    <div className="exv">
      <ExecutionSidebar workflowId={workflowId} current={execId} onSelect={onSelect} reloadKey={reloadKey} />
      {execId
        ? <ExecutionPreview execId={execId} onSelect={onSelect} onDebug={onDebug} onChanged={() => setReloadKey((k) => k + 1)} />
        : <section className="exv-main"><div className="dv-empty">Select an execution to see its data.</div></section>}
    </div>
  )
}

/** Old /executions/:id links open the execution inside its workflow's Executions tab. */
export function ExecutionDetail() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const [err, setErr] = useState(false)
  useEffect(() => {
    if (id) api.execution(id).then((ex) => navigate(`/workflow/${ex.workflow_id}/executions/${ex.id}`, { replace: true })).catch(() => setErr(true))
  }, [id, navigate])
  return <div className="empty">{err ? <>Execution not found. <Link to="/executions">All executions</Link></> : 'Opening execution…'}</div>
}
