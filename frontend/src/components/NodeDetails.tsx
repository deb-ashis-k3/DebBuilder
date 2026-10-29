import { Fragment, useEffect, useState } from 'react'
import { api } from '../api'
import { Icon, fmtDuration, isVisible, outputPorts, toast, useCatalog } from '../lib'
import type { CodeRuntime, Item, ListenerStatus, NodeRun, NodeSettings, RunState, WFNode } from '../types'
import { AiLogs } from './AiLogs'
import { DataView } from './DataView'
import { PropertyField } from './PropertyField'
import { Select } from './Select'

export interface ParentRef { id: string; name: string; port: string }

function pinnedPorts(pin: WFNode['pin_data']): Record<string, Item[]> | null {
  if (pin == null) return null
  return Array.isArray(pin) ? { main: pin } : pin
}

export function NodeDetails({ node, run, runsByNode, parents, workflowId, onChange, onClose, onRun }: {
  node: WFNode
  run?: RunState
  runsByNode: Record<string, RunState>
  parents: ParentRef[]
  workflowId: string
  onChange: (n: WFNode) => void
  onClose: () => void
  /** Run the workflow; with a node id, stop after that node ("Execute step"). */
  onRun: (destination?: string) => void
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

  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal wide">
        <div className="modal-head">
          <div className="mini-icon" style={{ '--nc': spec.color } as React.CSSProperties}><Icon name={spec.icon} size={15} /></div>
          <input className="wf-name" value={node.name} onChange={(e) => onChange({ ...node, name: e.target.value })} />
          <span className="muted small">{spec.label}</span>
          <span className="spacer" />
          {run && (
            <span className={`badge ${run.status}`}>
              {run.status}{run.duration_ms != null ? ` · ${fmtDuration(run.duration_ms)}` : ''}{runs.length > 1 ? ` · ${runs.length} runs` : ''}
            </span>
          )}
          {!isSub && (
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
        <div className={`ndv pane-${pane}`} style={{ flex: 1, minHeight: 0 }}>
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
                <TriggerHelp type={node.type} params={params} workflowId={workflowId} origin={origin} />
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
                      {parents[0] && (
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
            <div className="ndv-col-head">
              <div className="tabs">
                <button className={`tab ${tab === 'params' ? 'active' : ''}`} onClick={() => setTab('params')}>Parameters</button>
                <button className={`tab ${tab === 'settings' ? 'active' : ''}`} onClick={() => setTab('settings')}>Settings</button>
              </div>
            </div>
            <div className="ndv-col-body">
              {tab === 'params' ? (
                <>
                  <p className="muted small" style={{ marginTop: 0 }}>{spec.description}</p>
                  {spec.properties.length === 0 && <div className="muted">This node has no parameters.</div>}
                  {spec.properties.filter((p) => isVisible(p, params)).map((p) => (
                    <Fragment key={p.name}>
                      <PropertyField spec={p} value={params[p.name] ?? p.default} onChange={(v) => setParam(p.name, v)} />
                      {node.type === 'core.code' && p.name === 'language' && <CodeRuntimeHint language={String(params.language ?? 'python')} />}
                    </Fragment>
                  ))}
                  <div className="hint small muted" style={{ marginTop: 16 }}>
                    Expressions: <code>{'{{ $json.field }}'}</code>, <code>{"{{ $('Node name').json.x }}"}</code>, <code>{'{{ $now }}'}</code>, <code>{'{{ $vars.key }}'}</code> (Jinja/Python syntax).
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
                          {pinned ? <button className="btn ghost sm icon on-pin" title="Unpin" onClick={unpin}><Icon name="pin-off" size={15} /></button>
                            : <button className="btn ghost sm icon" title="Pin this output (reuse it in editor runs)" disabled={!output} onClick={pin}><Icon name="pin" size={15} /></button>}
                          <button className="btn ghost sm icon" title="Edit output" onClick={() => setEditing(JSON.stringify(pinned ? (Array.isArray(node.pin_data) ? node.pin_data : pinned) : (outItems ?? [{}]), null, 2))}>
                            <Icon name="pencil" size={15} />
                          </button>
                          <button className="btn ghost sm icon" title="Copy JSON" disabled={!outItems?.length} onClick={copy}><Icon name="copy" size={15} /></button>
                          <button className="btn ghost sm icon" title="Download JSON" disabled={!outItems?.length} onClick={download}><Icon name="download" size={15} /></button>
                        </div>
                      }
                      empty={
                        <div className="dv-cta">
                          <Icon name="play-circle" size={28} />
                          <b>{run ? 'No items on this output' : 'No output data'}</b>
                          {!run && <span>Execute this step, or set mock data with ✎ to pin it.</span>}
                          {!run && !spec.trigger && <button className="btn sm primary" onClick={() => onRun(node.id)}><Icon name="step-forward" size={14} /> Execute step</button>}
                        </div>
                      } />
                  </>
                )}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

function TriggerHelp({ type, params, workflowId, origin }: { type: string; params: Record<string, unknown>; workflowId: string; origin: string }) {
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
          <p>Click <b>Voice</b> in the editor to talk to this workflow in the browser. Requires the voice worker:</p>
          {code('cd backend\nuv run --extra voice python voice/worker.py dev')}
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
