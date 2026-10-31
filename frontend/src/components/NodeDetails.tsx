import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { Icon, fmtDuration, isVisible, outputPorts, toast, useCatalog } from '../lib'
import type { CodeRuntime, Item, ListenerStatus, NodeRun, NodeSettings, RunState, VoiceWorkerStatus, WFNode } from '../types'
import { AiLogs } from './AiLogs'
import { ExprDataContext, type ExprData } from './ExprField'
import { DataView } from './DataView'
import { PropertyField } from './PropertyField'
import { Select } from './Select'

export interface ParentRef { id: string; name: string; port: string; type?: string }

function pinnedPorts(pin: WFNode['pin_data']): Record<string, Item[]> | null {
  if (pin == null) return null
  return Array.isArray(pin) ? { main: pin } : pin
}

export function NodeDetails({ node, run, runsByNode, nodeNames, parents, children, workflowId, onChange, onClose, onRun, onOpen, readOnly = false }: {
  node: WFNode
  /** Node id → name, so expressions like $('Name') can be previewed with earlier nodes' output. */
  nodeNames: Record<string, string>
  run?: RunState
  runsByNode: Record<string, RunState>
  parents: ParentRef[]
  /** Nodes this one feeds (main outputs), shown on the right edge to jump to, like n8n. */
  children: ParentRef[]
  /** Open another node's panel (the previous / next node buttons). */
  onOpen: (id: string) => void
  workflowId: string
  onChange: (n: WFNode) => void
  onClose: () => void
  /** Run the workflow; with a node id, stop after that node ("Execute step"). */
  onRun: (destination?: string) => void
  /** Opened from an execution (n8n): that run's data, nothing editable, no Execute / pin buttons. */
  readOnly?: boolean
}) {
  const { byType } = useCatalog()
  const spec = byType[node.type]
  const [tab, setTab] = useState<'params' | 'settings'>('params')
  const [pane, setPane] = useState<'input' | 'params' | 'output'>('params') // narrow screens show one column at a time
  const ports = outputPorts(spec, node)
  const [port, setPort] = useState<string>(ports[0]?.id ?? 'main')
  const [outTab, setOutTab] = useState<'data' | 'ai'>('data')
  const [source, setSource] = useState<string>('actual')
  const [editing, setEditing] = useState<string | null>(null)
  const runs: NodeRun[] = run?.runs ?? []
  const [runIdx, setRunIdx] = useState(Math.max(runs.length - 1, 0))
  useEffect(() => { setRunIdx(Math.max((run?.runs?.length ?? 1) - 1, 0)) }, [run?.runs?.length])
  // Fill unset params with defaults so show_if conditions and controls reflect what the engine uses.
  const params = { ...Object.fromEntries(spec.properties.map((p) => [p.name, p.default])), ...(node.parameters ?? {}) }
  const settings = node.settings ?? {}
  const setParam = (k: string, v: unknown) => onChange({ ...node, parameters: { ...params, [k]: v } })
  const setSetting = (k: keyof NodeSettings, v: unknown) => onChange({ ...node, settings: { ...settings, [k]: v } })
  const isSub = !!spec.provides
  const origin = location.origin.replace(':5173', ':8000')

  const r = runs[runIdx]
  const output = r?.output ?? run?.output
  const pinned = pinnedPorts(node.pin_data)
  const shownOutput = pinned ?? output
  const outItems = shownOutput?.[port] ?? (port === ports[0]?.id ? shownOutput?.main : undefined)
  const outCount = (p: string) => (pinned ? pinned[p]?.length : r?.item_counts?.[p] ?? run?.counts?.[p])

  // Input: the run's actual input, or the latest output of any parent (n8n's input selector).
  const parent = parents.find((p) => p.id === source)
  const parentRun = parent ? runsByNode[parent.id] : undefined
  const parentNode = parent && byType ? parent : undefined
  const inItems = parentNode ? parentRun?.output?.[parent!.port] ?? parentRun?.output?.main : (r?.input ?? run?.input)
  const inBase = parentNode ? `$('${parentNode.name.replace(/'/g, "\\'")}').json` : '$json'
  // Live expression results resolve against this node's input and every node's latest output (capped for size).
  const exprData = useMemo<ExprData>(() => ({
    workflowId,
    items: (inItems ?? []).slice(0, 50),
    nodeNames: Object.values(nodeNames),
    nodeOutputs: Object.fromEntries(Object.entries(runsByNode)
      .filter(([id, rs]) => rs.output && nodeNames[id])
      .map(([id, rs]) => [nodeNames[id], (rs.output!.main ?? Object.values(rs.output!)[0] ?? []).slice(0, 50)])),
  }), [workflowId, inItems, runsByNode, nodeNames])
  const fed = r?.sources?.map((s) => parents.find((p) => p.id === s.node_id)?.name ?? s.node_id).join(', ')

  const pin = () => {
    if (!output) return
    const data = Object.keys(output).length === 1 && output.main ? output.main : output
    onChange({ ...node, pin_data: data })
    toast('Output pinned: editor runs will reuse it instead of executing this node')
  }
  const unpin = () => {
    const { pin_data: _, ...rest } = node
    onChange(rest)
  }
  const saveEdit = () => {
    try {
      const parsed = JSON.parse(editing ?? '[]')
      const data = Array.isArray(parsed) ? parsed : (parsed && typeof parsed === 'object' && Object.values(parsed).every(Array.isArray)) ? parsed : [parsed]
      onChange({ ...node, pin_data: data })
      setEditing(null)
    } catch (e) {
      toast(`Invalid JSON: ${e}`, 'error')
    }
  }
  const copy = () => { navigator.clipboard.writeText(JSON.stringify(outItems ?? [], null, 2)); toast('Copied output JSON') }
  const download = () => {
    const a = document.createElement('a')
    a.href = URL.createObjectURL(new Blob([JSON.stringify(outItems ?? [], null, 2)], { type: 'application/json' }))
    a.download = `${node.name.replace(/[^\w-]+/g, '_')}-output.json`
    a.click()
  }

  const runPicker = runs.length > 1 && (
    <Select className="run-select" value={String(runIdx)} onChange={(v) => setRunIdx(Number(v))}
      options={runs.map((_, i) => ({ value: String(i), label: `Run ${i + 1} of ${runs.length}` }))} />
  )

  const layout = usePanelLayout()
  const uniq = (refs: ParentRef[]) => refs.filter((r, i) => refs.findIndex((x) => x.id === r.id) === i)

  return (
    <ExprDataContext.Provider value={exprData}>
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="ndv-shell">
      <NodeNav side="prev" refs={uniq(parents)} onOpen={onOpen} />
      <NodeNav side="next" refs={uniq(children)} onOpen={onOpen} />
      <div className="modal wide">
        <div className="modal-head">
          <div className="mini-icon" style={{ '--nc': spec.color } as React.CSSProperties}><Icon name={spec.icon} size={15} /></div>
          <input className="wf-name" value={node.name} readOnly={readOnly} onChange={(e) => onChange({ ...node, name: e.target.value })} />
          {readOnly && <span className="badge plain" title="Opened from an execution: this run's data, read-only"><Icon name="lock" size={11} /> Execution · read-only</span>}
          <span className="muted small">{spec.label}</span>
          <span className="spacer" />
          {run && (
            <span className={`badge ${run.status}`}>
              {run.status}{run.duration_ms != null ? ` · ${fmtDuration(run.duration_ms)}` : ''}{runs.length > 1 ? ` · ${runs.length} runs` : ''}
            </span>
          )}
          {!isSub && !readOnly && (
            <>
              <button className="btn sm" onClick={() => onRun()} title="Run the whole workflow"><Icon name="play" size={14} /> <span className="btn-text">Execute workflow</span></button>
              <button className="btn sm primary" onClick={() => onRun(node.id)} title="Run only the nodes needed to reach this one">
                <Icon name="step-forward" size={14} /> <span className="btn-text">Execute step</span>
              </button>
            </>
          )}
          <button className="btn ghost icon" onClick={onClose} title="Close (Esc)"><Icon name="x" /></button>
        </div>
        <div className="ndv-switch tabs">
          {(['input', 'params', 'output'] as const).map((p) => (
            <button key={p} className={`tab ${pane === p ? 'active' : ''}`} onClick={() => setPane(p)}>{p === 'params' ? 'Parameters' : p[0].toUpperCase() + p.slice(1)}</button>
          ))}
        </div>
        <div ref={layout.ref} className={`ndv pane-${pane}`} style={{ flex: 1, minHeight: 0, ...layout.style }}>
          <div className="ndv-col ndv-data ndv-in">
            <div className="ndv-col-head">
              <Icon name="log-in" size={14} /> Input
              {!spec.trigger && !isSub && parents.length > 0 && (
                <Select className="run-select" value={source} onChange={setSource} title="Which data to show"
                  options={[{ value: 'actual', label: r ? 'Received input' : 'Input' },
                    ...parents.map((p) => ({ value: p.id, label: `${p.name}${p.port !== 'main' ? ` (${p.port})` : ''}` }))]} />
              )}
              {source === 'actual' && runPicker}
            </div>
            <div className="ndv-col-body">
              {spec.listener ? (
                <ListenerHelp nodeId={node.id} workflowId={workflowId} description={spec.description} />
              ) : spec.trigger ? (
                <TriggerHelp type={node.type} nodeId={node.id} params={params} workflowId={workflowId} origin={origin} />
              ) : isSub ? (
                <div className="muted small">Sub-nodes are configured here and used by the AI node they connect to. Expressions resolve against that node's input item.</div>
              ) : (
                <DataView items={inItems} base={inBase}
                  meta={source === 'actual' && fed ? <span className="muted">from {fed}</span> : parentNode ? <span className="muted">latest output of {parentNode.name}</span> : null}
                  empty={
                    <div className="dv-cta">
                      <Icon name="database-zap" size={28} />
                      <b>No input data yet</b>
                      <span>Run the previous nodes to see data you can drag into parameters.</span>
                      {parents[0] && !readOnly && (
                        <button className="btn sm primary" onClick={() => onRun(parents[0].id)}>
                          <Icon name="step-forward" size={14} /> Execute previous nodes
                        </button>
                      )}
                    </div>
                  } />
              )}
            </div>
          </div>

          <div className="ndv-col ndv-params">
            {layout.active && (
              <>
                <div className="ndv-resize l" title="Drag to resize" onPointerDown={layout.start('left')} />
                <div className="ndv-resize r" title="Drag to resize" onPointerDown={layout.start('right')} />
                <div className="ndv-grip" title="Drag to move · double-click to reset" onPointerDown={layout.start('move')} onDoubleClick={layout.reset}>
                  <Icon name="grip-horizontal" size={14} />
                </div>
              </>
            )}
            <div className="ndv-col-head">
              <div className="tabs">
                <button className={`tab ${tab === 'params' ? 'active' : ''}`} onClick={() => setTab('params')}>Parameters</button>
                <button className={`tab ${tab === 'settings' ? 'active' : ''}`} onClick={() => setTab('settings')}>Settings</button>
              </div>
            </div>
            <div className="ndv-col-body">
              <fieldset className="ndv-fieldset" disabled={readOnly}>
              {tab === 'params' ? (
                <>
                  <p className="muted small" style={{ marginTop: 0 }}>{spec.description}</p>
                  {spec.properties.length === 0 && <div className="muted">This node has no parameters.</div>}
                  {spec.properties.filter((p) => isVisible(p, params)).map((p) => (
                    <Fragment key={p.name}>
                      <PropertyField spec={p} value={params[p.name] ?? p.default} onChange={(v) => setParam(p.name, v)} params={params} />
                      {node.type === 'core.code' && p.name === 'language' && <CodeRuntimeHint language={String(params.language ?? 'python')} />}
                    </Fragment>
                  ))}
                  <div className="hint small muted" style={{ marginTop: 16 }}>
                    Expressions: <code>{'{{ $json.field }}'}</code>, <code>{"{{ $('Node name').json.x }}"}</code>, <code>{'{{ $now }}'}</code>, <code>{'{{ $vars.key }}'}</code>, <code>{'{{ $state.key }}'}</code> (Jinja/Python syntax).
                  </div>
                </>
              ) : (
                <>
                  <div className="field row">
                    <label className="switch"><input type="checkbox" checked={!!node.disabled} onChange={(e) => onChange({ ...node, disabled: e.target.checked })} /><span /></label>
                    <span>Disable node (items pass through)</span>
                  </div>
                  {!isSub && !spec.trigger && (
                    <>
                      <div className="field">
                        <label>On error</label>
                        <Select value={String(settings.on_error ?? 'stop')} onChange={(v) => setSetting('on_error', v)} options={[
                          { value: 'stop', label: 'Stop workflow' },
                          { value: 'continue', label: 'Continue (add error to items)' },
                          { value: 'error_output', label: 'Continue using error output' },
                        ]} />
                      </div>
                      <div className="field row">
                        <label className="switch"><input type="checkbox" checked={!!settings.retry_on_fail} onChange={(e) => setSetting('retry_on_fail', e.target.checked)} /><span /></label>
                        <span>Retry on fail</span>
                      </div>
                      {settings.retry_on_fail && (
                        <div className="row">
                          <div className="field grow"><label>Max tries</label>
                            <input className="input" type="number" value={settings.max_tries ?? 3} onChange={(e) => setSetting('max_tries', Number(e.target.value))} /></div>
                          <div className="field grow"><label>Wait between (ms)</label>
                            <input className="input" type="number" value={settings.wait_between_tries_ms ?? 1000} onChange={(e) => setSetting('wait_between_tries_ms', Number(e.target.value))} /></div>
                        </div>
                      )}
                      <div className="field row">
                        <label className="switch"><input type="checkbox" checked={!!settings.always_output_data} onChange={(e) => setSetting('always_output_data', e.target.checked)} /><span /></label>
                        <span>Always output data (emit empty item if none)</span>
                      </div>
                    </>
                  )}
                  <div className="field">
                    <label>Notes</label>
                    <textarea className="textarea" value={settings.notes ?? ''} onChange={(e) => setSetting('notes', e.target.value)} />
                  </div>
                  <div className="muted small">Node ID: <code>{node.id}</code> · Type: <code>{node.type}</code></div>
                </>
              )}
              </fieldset>
            </div>
          </div>

          <div className="ndv-col ndv-data ndv-out">
            <div className="ndv-col-head">
              <Icon name="log-out" size={14} /> Output
              {!!r?.sub_runs?.length && (
                <div className="tabs">
                  <button className={`tab ${outTab === 'data' ? 'active' : ''}`} onClick={() => setOutTab('data')}>Data</button>
                  <button className={`tab ${outTab === 'ai' ? 'active' : ''}`} onClick={() => setOutTab('ai')}>AI logs ({r.sub_runs.length})</button>
                </div>
              )}
              {runPicker}
            </div>
            {ports.length > 1 && outTab === 'data' && (
              <div className="port-tabs">
                {ports.map((p) => (
                  <button key={p.id} className={`port-tab ${port === p.id ? 'on' : ''}`} onClick={() => setPort(p.id)}>
                    {p.label || p.id}<span>{outCount(p.id) ?? 0}</span>
                  </button>
                ))}
              </div>
            )}
            <div className="ndv-col-body">
              {r?.error && !pinned && <div className="error-box" style={{ marginBottom: 12 }}>{r.error}</div>}
              {isSub ? <div className="muted small">Sub-node output is consumed by its parent AI node. See the parent's <b>AI logs</b>.</div>
                : outTab === 'ai' && r?.sub_runs ? <AiLogs runs={r.sub_runs} />
                : editing != null ? (
                  <div className="pin-editor">
                    <div className="pin-banner"><Icon name="pencil" size={14} /> Edit output (JSON array of items). Saving pins it.</div>
                    <textarea className="textarea code" rows={18} value={editing} onChange={(e) => setEditing(e.target.value)} spellCheck={false} />
                    <div className="row" style={{ justifyContent: 'flex-end', marginTop: 8 }}>
                      <button className="btn sm" onClick={() => setEditing(null)}>Cancel</button>
                      <button className="btn sm primary" onClick={saveEdit}><Icon name="pin" size={13} /> Save &amp; pin</button>
                    </div>
                  </div>
                ) : (
                  <>
                    {pinned && (
                      <div className="pin-banner">
                        <Icon name="pin" size={14} /> This data is pinned. Editor runs use it instead of executing the node.
                        <button className="btn sm ghost" onClick={unpin}>Unpin</button>
                      </div>
                    )}
                    <DataView items={outItems}
                      meta={!pinned && r ? (
                        <span className={`exec-meta ${r.status}`}>
                          <Icon name={r.status === 'success' ? 'circle-check' : r.status === 'error' ? 'circle-alert' : 'loader-circle'} size={13} />
                          {r.status === 'running' ? 'Running…' : `Executed in ${fmtDuration(r.duration_ms)} at ${new Date(r.started_at).toLocaleTimeString()}`}
                          {r.pinned ? ' (pinned data)' : ''}
                        </span>
                      ) : null}
                      actions={
                        <div className="row" style={{ gap: 2 }}>
                          {!readOnly && (pinned ? <button className="btn ghost sm icon on-pin" title="Unpin" onClick={unpin}><Icon name="pin-off" size={15} /></button>
                            : <button className="btn ghost sm icon" title="Pin this output (reuse it in editor runs)" disabled={!output} onClick={pin}><Icon name="pin" size={15} /></button>)}
                          {!readOnly && <button className="btn ghost sm icon" title="Edit output" onClick={() => setEditing(JSON.stringify(pinned ? (Array.isArray(node.pin_data) ? node.pin_data : pinned) : (outItems ?? [{}]), null, 2))}>
                            <Icon name="pencil" size={15} />
                          </button>}
                          <button className="btn ghost sm icon" title="Copy JSON" disabled={!outItems?.length} onClick={copy}><Icon name="copy" size={15} /></button>
                          <button className="btn ghost sm icon" title="Download JSON" disabled={!outItems?.length} onClick={download}><Icon name="download" size={15} /></button>
                        </div>
                      }
                      empty={
                        <div className="dv-cta">
                          <Icon name="play-circle" size={28} />
                          <b>{run ? 'No items on this output' : 'No output data'}</b>
                          {!run && <span>Execute this step, or set mock data with ✎ to pin it.</span>}
                          {!run && !spec.trigger && !readOnly && <button className="btn sm primary" onClick={() => onRun(node.id)}><Icon name="step-forward" size={14} /> Execute step</button>}
                        </div>
                      } />
                  </>
                )}
            </div>
          </div>
        </div>
      </div>
      </div>
    </div>
    </ExprDataContext.Provider>
  )
}

/** n8n's floating buttons on the panel's edges: the nodes before (left) and after (right) this one. */
function NodeNav({ side, refs, onOpen }: { side: 'prev' | 'next'; refs: ParentRef[]; onOpen: (id: string) => void }) {
  const { byType } = useCatalog()
  if (!refs.length) return null
  return (
    <div className={`ndv-nav ${side}`}>
      {refs.map((r) => {
        const s = r.type ? byType[r.type] : undefined
        return (
          <button key={r.id} type="button" className="ndv-nav-btn" style={{ '--nc': s?.color ?? 'var(--text-3)' } as React.CSSProperties}
            onClick={() => onOpen(r.id)} aria-label={`${side === 'prev' ? 'Previous' : 'Next'} node: ${r.name}`}>
            <Icon name={s?.icon ?? 'box'} size={18} />
            <span className="ndv-nav-tip">{r.name}</span>
          </button>
        )
      })}
    </div>
  )
}

const LAYOUT_KEY = 'ndv-layout'
const DEFAULT_LAYOUT = { left: 0.31, width: 0.38 }  // fractions of the panel width: input | parameters | output
const MIN_SIDE = 200
const MIN_MAIN = 380

/** The parameters column can be moved (top grip) and resized (its edges); input / output take the rest. Remembered per browser. */
function usePanelLayout() {
  const ref = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(0)
  const [frac, setFrac] = useState<{ left: number; width: number }>(() => {
    try {
      const v = JSON.parse(localStorage.getItem(LAYOUT_KEY) ?? 'null')
      if (v && typeof v.left === 'number' && typeof v.width === 'number') return v
    } catch { /* private mode / bad value */ }
    return DEFAULT_LAYOUT
  })
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const ro = new ResizeObserver(([e]) => setWidth(e.contentRect.width))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  // Narrow screens keep the stacked / tabbed layout from the stylesheet.
  const active = width >= 1100
  const clamp = useCallback((left: number, w: number) => {
    const W = width || 1
    const wpx = Math.min(Math.max(w * W, MIN_MAIN), W - 2 * MIN_SIDE)
    const lpx = Math.min(Math.max(left * W, MIN_SIDE), W - wpx - MIN_SIDE)
    return { left: lpx, width: wpx }
  }, [width])
  const px = clamp(frac.left, frac.width)
  const save = (f: { left: number; width: number }) => { try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(f)) } catch { /* ignore */ } }

  const start = (mode: 'left' | 'right' | 'move') => (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return
    e.preventDefault()
    const el = e.currentTarget
    el.setPointerCapture(e.pointerId)
    const x0 = e.clientX
    const { left: l0, width: w0 } = px
    const W = width
    let last = frac
    const move = (ev: PointerEvent) => {
      const dx = ev.clientX - x0
      let l = l0, w = w0
      if (mode === 'move') l = l0 + dx
      else if (mode === 'right') w = w0 + dx
      else { l = Math.min(l0 + dx, l0 + w0 - MIN_MAIN); w = w0 - (l - l0) }
      const c = clamp(l / W, w / W)
      last = { left: c.left / W, width: c.width / W }
      setFrac(last)
    }
    const up = () => {
      el.removeEventListener('pointermove', move)
      el.removeEventListener('pointerup', up)
      el.removeEventListener('pointercancel', up)
      document.body.classList.remove('ndv-dragging')
      save(last)
    }
    document.body.classList.add('ndv-dragging')
    el.addEventListener('pointermove', move)
    el.addEventListener('pointerup', up)
    el.addEventListener('pointercancel', up)
  }
  const reset = () => { setFrac(DEFAULT_LAYOUT); save(DEFAULT_LAYOUT) }
  const style: React.CSSProperties = active ? { gridTemplateColumns: `${px.left}px ${px.width}px minmax(0, 1fr)` } : {}
  return { ref, active, style, start, reset }
}

function TriggerHelp({ type, nodeId, params, workflowId, origin }: { type: string; nodeId: string; params: Record<string, unknown>; workflowId: string; origin: string }) {
  const code = (s: string) => <pre className="json-item" style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>{s}</pre>
  switch (type) {
    case 'trigger.webhook':
      return (
        <div className="small">
          <p><b>Production URL</b> (workflow must be active):</p>{code(`${String(params.method ?? 'POST')} ${origin}/webhook/${String(params.path ?? '')}`)}
          <p><b>Test URL</b> (works while inactive):</p>{code(`${origin}/webhook-test/${String(params.path ?? '')}`)}
          <p className="muted">Item: <code>{'{ body, query, headers, method, path }'}</code></p>
        </div>
      )
    case 'trigger.form':
      return <div className="small"><p><b>Form URL</b>:</p>{code(`${origin}/form/${workflowId}`)}<p className="muted">Each field becomes a key on the item.</p></div>
    case 'trigger.chat':
      return (
        <div className="small">
          <p>Use the <b>Chat</b> button in the editor. API (enable "public" first):</p>
          {code(`POST ${origin}/api/chat/${workflowId}\n{"message": "hi", "session_id": "user-1", "wait": true}`)}
          <p className="muted">Item: <code>{'{ message, chatInput, session_id }'}</code></p>
        </div>
      )
    case 'trigger.whatsapp':
      return (
        <div className="small">
          <p>Set this as the callback URL in your Meta app (public HTTPS, e.g. tunnel):</p>{code(`${origin}/api/whatsapp/webhook`)}
          <p className="muted">Verify token = the credential's "Webhook Verify Token". Activate the workflow to receive messages.</p>
          <p className="muted">Item: <code>{'{ from, name, text, type, message_id, session_id, raw }'}</code></p>
        </div>
      )
    case 'trigger.voice':
      return (
        <div className="small">
          <VoiceWorkerHelp nodeId={nodeId} workflowId={workflowId} agentName={String(params.agent_name || 'debbuilder-voice')} />
          <p>Click <b>Voice</b> in the editor to talk to this workflow in the browser, or call a number routed to this agent (Phone page).</p>
          <p className="muted">Each user turn emits <code>{'{ text, session_id, room, phone? }'}</code>; the final node's <code>output</code> is spoken.</p>
        </div>
      )
    case 'trigger.schedule':
      return <div className="small muted">Runs on the cron schedule while the workflow is active. "Execute workflow" runs it once now.</div>
    case 'trigger.subworkflow':
      return <div className="small muted">Receives the items passed by an "Execute Workflow" node or the AI's arguments when used as a "Call Workflow Tool".</div>
    case 'trigger.error':
      return <div className="small muted">Set this workflow as another workflow's "Error workflow" (workflow settings). Item: <code>{'{ execution, workflow }'}</code></div>
    default:
      return <div className="small muted">Runs when you click "Execute workflow".</div>
  }
}

function ago(ts: number) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts))
  return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : `${Math.round(s / 3600)}h ago`
}

/** Live state of a Kafka / Redis / MQTT … listener, polled while the panel is open. */
function ListenerHelp({ nodeId, workflowId, description }: { nodeId: string; workflowId: string; description: string }) {
  const [st, setSt] = useState<ListenerStatus | null | undefined>(undefined)
  useEffect(() => {
    let alive = true
    const load = () => api.listenerStatus(workflowId).then((r) => alive && setSt(r[nodeId] ?? null)).catch(() => alive && setSt(null))
    load()
    const t = window.setInterval(load, 3000)
    return () => { alive = false; window.clearInterval(t) }
  }, [workflowId, nodeId])

  const state = st?.state
  return (
    <div className="small listener-help">
      <div className={`listener-state ${state ?? 'off'}`}>
        <span className="listener-dot" />
        {st === undefined ? 'Checking…'
          : !st ? 'Not listening: activate the workflow to start'
          : state === 'listening' ? 'Listening for events'
          : state === 'error' ? 'Connection error, retrying'
          : state === 'starting' ? 'Connecting…' : 'Stopped'}
      </div>
      {st?.error && <div className="error-box small" style={{ marginTop: 8 }}>{st.error}</div>}
      {st && (
        <div className="listener-stats">
          <span><b>{st.events}</b> event{st.events === 1 ? '' : 's'}</span>
          <span>Last event: <b>{st.last_event_at ? ago(st.last_event_at) : 'none yet'}</b></span>
          <span>Up since: <b>{ago(st.since)}</b></span>
        </div>
      )}
      <p>{description}</p>
      <p className="muted">
        While the workflow is <b>active</b>, every event starts an execution (see Executions). Changes apply after you <b>save</b>.
      </p>
      <p className="muted">
        <b>Execute step</b> waits for the next live event. Test runs don't acknowledge messages or join consumer groups, so production traffic is untouched.
      </p>
    </div>
  )
}

/** The voice worker the backend runs while the workflow is active (polled while the panel is open). */
function VoiceWorkerHelp({ nodeId, workflowId, agentName }: { nodeId: string; workflowId: string; agentName: string }) {
  const [st, setSt] = useState<VoiceWorkerStatus | null | undefined>(undefined)
  const [showLogs, setShowLogs] = useState(false)
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    let alive = true
    const load = () => api.voiceWorkerStatus(workflowId).then((r) => alive && setSt(r[nodeId] ?? null)).catch(() => alive && setSt(null))
    load()
    const t = window.setInterval(load, 3000)
    return () => { alive = false; window.clearInterval(t) }
  }, [workflowId, nodeId])

  const restart = () => {
    setBusy(true)
    api.restartVoiceWorker(workflowId, nodeId).then(setSt).catch((e) => toast(String(e.message ?? e), 'error')).finally(() => setBusy(false))
  }
  const state = st?.state
  const cls = state === 'running' ? 'listening' : state === 'error' ? 'error' : state === 'starting' ? 'starting' : 'off'
  return (
    <div className="listener-help voice-worker">
      <div className={`listener-state ${cls}`}>
        <span className="listener-dot" />
        {st === undefined ? 'Checking…'
          : !st ? 'Voice worker off: activate the workflow to start it'
          : state === 'running' ? `Voice worker running · agent "${st.agent_name}"`
          : state === 'starting' ? 'Starting the voice worker…'
          : state === 'error' ? 'Voice worker problem'
          : state === 'external' ? 'Workers are run separately (VOICE_WORKERS=off)' : 'Voice worker stopped'}
        {st && state !== 'external' && (
          <button className="btn ghost sm" style={{ marginLeft: 'auto' }} disabled={busy} onClick={restart} title="Restart the worker process">
            <Icon name="rotate-ccw" size={13} /> Restart
          </button>
        )}
      </div>
      {st?.error && <div className="error-box small" style={{ marginTop: 8 }}>{st.error}</div>}
      {st && state !== 'external' && (
        <div className="listener-stats">
          {st.pid ? <span>PID <b>{st.pid}</b></span> : null}
          <span>Up since: <b>{ago(st.since)}</b></span>
          {st.workflows > 1 && <span>Shared by <b>{st.workflows}</b> workflows</span>}
          {!!st.restarts && <span>Restarts: <b>{st.restarts}</b></span>}
          {st.logs.length > 0 && <button className="link-btn" onClick={() => setShowLogs((v) => !v)}>{showLogs ? 'Hide' : 'Show'} log</button>}
        </div>
      )}
      {showLogs && st && <pre className="worker-log">{st.logs.join('\n')}</pre>}
      {state === 'external' ? (
        <pre className="json-item" style={{ whiteSpace: 'pre-wrap' }}>{`cd backend\nAGENT_NAME=${agentName} uv run --extra voice python voice/worker.py start`}</pre>
      ) : (
        <p className="muted">
          The backend starts a LiveKit worker for agent <b>{agentName}</b> when you <b>activate</b> the workflow, restarts it if it
          stops, and stops it when no active workflow uses that agent. Saving a change restarts it.
        </p>
      )}
    </div>
  )
}

let runtimesCache: Promise<CodeRuntime[]> | null = null

/** Under the Code node's language picker: is that runtime installed on the server? */
function CodeRuntimeHint({ language }: { language: string }) {
  const [rts, setRts] = useState<CodeRuntime[] | null>(null)
  useEffect(() => {
    runtimesCache ??= api.codeRuntimes().catch(() => { runtimesCache = null; return [] })
    runtimesCache.then(setRts)
  }, [])
  if (language === 'custom') {
    return <div className="rt-hint muted small">Runs any installed interpreter or compiler. Input JSON is in the file at <code>DEBBUILDER_INPUT</code>; write the result to <code>DEBBUILDER_OUTPUT</code> or print JSON.</div>
  }
  const rt = rts?.find((r) => r.key === language)
  if (!rt) return null
  return rt.available ? (
    <div className="rt-hint ok small" title={rt.path ?? ''}><Icon name="circle-check" size={13} /> {rt.label}{rt.version ? ` · ${rt.version}` : ''}{rt.builtin ? ' (built in)' : ''}</div>
  ) : (
    <div className="rt-hint bad small"><Icon name="triangle-alert" size={13} /> {rt.label} isn't installed on the server. {rt.install_hint}</div>
  )
}
