import {
  Background, BackgroundVariant, Controls, MiniMap, ReactFlow, ReactFlowProvider, addEdge, useEdgesState, useNodesState, useReactFlow, useUpdateNodeInternals,
  type Connection, type Edge, type FinalConnectionState, type Node,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, downloadJson, openSocket } from '../api'
import { EdgeActionsCtx, edgeTypes, type EdgeActions } from '../components/CanvasEdge'
import { ChatPanel } from '../components/ChatPanel'
import { Menu, MoveDialog, PromptDialog, confirmDialog, folderPath } from '../components/Dialogs'
import { ExecutionLog, type ExecMeta } from '../components/ExecutionLog'
import { nodeTypes, type FlowNodeData } from '../components/FlowNode'
import { NodeDetails } from '../components/NodeDetails'
import { Palette } from '../components/Palette'
import { PropertyField } from '../components/PropertyField'
import { Icon, defaultParams, newId, toast, uniqueName, useCatalog } from '../lib'
import type { ExecEvent, Execution, Folder, Item, NodeRun, NodeTypeSpec, RunState, WFEdge, WFNode, Workflow } from '../types'
import { WorkflowExecutionsView } from './Executions'
import { useAuth } from '../auth'

// LiveKit client is large; load it only when the voice panel opens.
const VoicePanel = lazy(() => import('../components/VoicePanel').then((m) => ({ default: m.VoicePanel })))

type RFNode = Node<FlowNodeData>

export const toRF = (n: WFNode, run?: RunState): RFNode => ({
  id: n.id,
  type: n.type === 'ui.sticky' ? 'sticky' : 'flow',
  position: n.position ?? { x: 0, y: 0 },
  data: { node: n, run },
  ...(n.type === 'ui.sticky' ? { style: { width: n.width ?? 240, height: n.height ?? 140 }, zIndex: -1 } : {}),
})

export const edgeToRF = (e: WFEdge): Edge => ({
  id: e.id, type: 'canvas', source: e.source, target: e.target, sourceHandle: e.sourceHandle, targetHandle: e.targetHandle,
  className: e.targetHandle?.startsWith('ai_') ? 'ai' : undefined,
})

const fromRF = (nodes: RFNode[]): WFNode[] =>
  nodes.map((rn) => ({
    ...rn.data.node,
    position: { x: Math.round(rn.position.x), y: Math.round(rn.position.y) },
    ...(rn.type === 'sticky' ? { width: rn.measured?.width ?? rn.data.node.width, height: rn.measured?.height ?? rn.data.node.height } : {}),
  }))

const edgesFromRF = (edges: Edge[]): WFEdge[] =>
  edges.map((e) => ({ id: e.id, source: e.source, target: e.target, sourceHandle: e.sourceHandle ?? 'main', targetHandle: e.targetHandle ?? 'main' }))

/** Group an execution's ordered node runs per node: latest run drives the canvas, all runs feed the NDV. */
export function runStateFromRuns(runs: NodeRun[]): Record<string, RunState> {
  const out: Record<string, RunState> = {}
  for (const r of runs) {
    const prev = out[r.node_id]
    const all = [...(prev?.runs ?? []), r]
    out[r.node_id] = {
      status: r.status, input: r.input, output: r.output, error: r.error, started_at: r.started_at, pinned: r.pinned,
      // Canvas shows total time across loop iterations, like n8n.
      duration_ms: all.reduce((a, x) => a + (x.duration_ms ?? 0), 0),
      counts: r.item_counts ?? Object.fromEntries(Object.entries(r.output ?? {}).map(([k, v]) => [k, v.length])),
      runs: all,
    }
  }
  return out
}

export const runStateFromExecution = (ex: Execution) => runStateFromRuns(ex.node_runs ?? [])

interface Pending {
  nodeId: string; handleId: string; handleType: 'source' | 'target'; position: { x: number; y: number }
  /** Set when inserting into an existing connection: the new node is wired source → new → target. */
  insertEdge?: { id: string; target: string; targetHandle: string }
}

function EditorInner() {
  const { id, '*': rest = '' } = useParams<{ id: string; '*': string }>()
  const navigate = useNavigate()
  // n8n-style tabs: /workflow/:id (editor) and /workflow/:id/executions[/:execId]
  const tab = rest.startsWith('executions') ? 'executions' : 'editor'
  const routeExecId = tab === 'executions' ? rest.split('/')[1] || null : null
  const [folders, setFolders] = useState<Folder[]>([])
  const [dialog, setDialog] = useState<'duplicate' | 'move' | null>(null)
  const importRef = useRef<HTMLInputElement>(null)
  useEffect(() => { api.folders().then(setFolders).catch(() => {}) }, [])
  const { byType } = useCatalog()
  const { can } = useAuth()
  const readOnly = !can('workflow:write')
  const rf = useReactFlow()
  const [wf, setWf] = useState<Workflow | null>(null)
  const [nodes, setNodes, onNodesChange] = useNodesState<RFNode>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])
  const updateInternals = useUpdateNodeInternals()
  const [dirty, setDirty] = useState(false)
  const [palette, setPalette] = useState<{ pending?: Pending } | null>(null)
  const [openNode, setOpenNode] = useState<string | null>(null)
  const [execRuns, setExecRuns] = useState<NodeRun[]>([])
  const [execMeta, setExecMeta] = useState<ExecMeta | null>(null)
  const runData = useMemo(() => runStateFromRuns(execRuns), [execRuns])
  const [execId, setExecId] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const [waiting, setWaiting] = useState<Execution['waiting_for'] | null>(null)
  const [panel, setPanel] = useState<'chat' | 'voice' | null>(null)
  const [showLog, setShowLog] = useState(false)
  const [logLines, setLogLines] = useState<string[]>([])
  const [logFocus, setLogFocus] = useState<string | null>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const listeners = useRef(new Set<(ev: ExecEvent) => void>())
  const execRef = useRef<string | null>(null)

  // ---- load -------------------------------------------------------------
  useEffect(() => {
    if (!id) return
    let alive = true // ignore a superseded response (route change / StrictMode double effect)
    api.workflow(id).then((w) => {
      if (!alive) return
      setWf(w)
      setNodes(w.nodes.map((n) => toRF(n)))
      setEdges(w.edges.map(edgeToRF))
      setDirty(false)
      requestAnimationFrame(() => {
        rf.fitView({ padding: 0.3, maxZoom: 1 })
        // Replacing nodes can drop React Flow's cached handle positions without a re-measure,
        // which hides every edge; force one once the nodes are on screen.
        requestAnimationFrame(() => updateInternals(w.nodes.map((n) => n.id)))
      })
    }).catch((e) => toast(String(e), 'error'))
    return () => { alive = false }
  }, [id, rf, setEdges, setNodes, updateInternals])

  // ---- live execution events ---------------------------------------------
  useEffect(() => {
    if (!id) return
    return openSocket(`/ws/workflows/${id}`, (ev: ExecEvent) => {
      listeners.current.forEach((l) => l(ev))
      if (ev.type === 'execution_started') {
        execRef.current = ev.execution_id
        setExecId(ev.execution_id)
        setExecRuns([])
        setExecMeta({ id: ev.execution_id, status: 'running', mode: ev.mode, started_at: new Date((ev.ts ?? Date.now() / 1000) * 1000).toISOString() })
        setWaiting(null)
        setRunning(true)
        setLogLines([])
        return
      }
      if (ev.execution_id !== execRef.current) return
      const sameRun = (r: NodeRun) => r.node_id === ev.node_id && (ev.run_index == null || r.run_index === ev.run_index)
      if (ev.type === 'node_started' && ev.node_id) {
        const n = rf.getNode(ev.node_id) as RFNode | undefined
        setExecRuns((rs) => [...rs, {
          node_id: ev.node_id!, name: ev.name ?? n?.data.node.name ?? ev.node_id!, type: n?.data.node.type ?? '',
          status: 'running', started_at: ev.started_at ?? new Date().toISOString(), run_index: ev.run_index ?? 0,
        }])
      } else if (ev.type === 'node_finished' && ev.node_id) {
        setExecRuns((rs) => {
          const i = rs.findLastIndex(sameRun)
          if (i < 0) return rs
          const next = [...rs]
          next[i] = { ...next[i], status: ev.status as NodeRun['status'], output: ev.output as Record<string, Item[]>,
            item_counts: ev.item_counts, error: ev.error, duration_ms: ev.duration_ms, finished_at: ev.finished_at, pinned: ev.pinned }
          return next
        })
      } else if (ev.type === 'node_waiting' && ev.node_id) {
        setExecRuns((rs) => rs.map((r) => (r.node_id === ev.node_id && r.status === 'running' ? { ...r, status: 'waiting' } : r)))
      } else if (ev.type === 'log') {
        const n = rf.getNode(ev.node_id ?? '') as RFNode | undefined
        setLogLines((l) => [...l, `[${n?.data.node.name ?? ev.node_id}] ${ev.message}`])
      } else if (ev.type === 'execution_finished') {
        setRunning(false)
        setExecMeta((m) => (m ? { ...m, status: ev.status ?? 'success', error: ev.error } : m))
        if (ev.status === 'error') toast(`Execution failed: ${ev.error}`, 'error')
        if (ev.status === 'waiting') setWaiting(ev.waiting_for ?? null)
        api.execution(ev.execution_id).then((ex) => {
          setExecRuns(ex.node_runs ?? [])
          setExecMeta((m) => ({ ...(m ?? { id: ex.id }), id: ex.id, status: ex.status, mode: ex.mode, started_at: ex.started_at, duration_ms: ex.duration_ms, error: ex.error }))
        }).catch(() => {})
      }
    })
  }, [id])

  const subscribe = useCallback((fn: (ev: ExecEvent) => void) => {
    listeners.current.add(fn)
    return () => { listeners.current.delete(fn) }
  }, [])

  // Push run data into node components & edge labels.
  const viewNodes = useMemo(() => nodes.map((n) => (n.data.run === runData[n.id] ? n : { ...n, data: { ...n.data, run: runData[n.id] } })), [nodes, runData])
  const viewEdges = useMemo(() => edges.map((e) => {
    const c = runData[e.source]?.counts?.[e.sourceHandle ?? 'main']
    const tgt = runData[e.target]?.status
    // Animate the edge while its target is processing the items it just received.
    const state = c && tgt === 'running' ? 'flowing' : c && tgt ? 'done' : ''
    const className = [e.targetHandle?.startsWith('ai_') ? 'ai' : '', state].filter(Boolean).join(' ') || undefined
    return c ? { ...e, className, label: `${c} item${c === 1 ? '' : 's'}` } : { ...e, className }
  }), [edges, runData])

  // ---- graph editing -----------------------------------------------------
  const markDirty = () => setDirty(true)
  const currentNodes = () => fromRF(rf.getNodes() as RFNode[])

  const isValidConnection = useCallback((c: Connection | Edge) => {
    const sh = c.sourceHandle ?? 'main'
    const th = c.targetHandle ?? 'main'
    if (c.source === c.target) return false
    if (sh.startsWith('ai_') || th.startsWith('ai_')) return sh === th
    return true
  }, [])

  const connect = useCallback((c: Connection) => {
    setEdges((eds) => {
      const th = c.targetHandle ?? 'main'
      let next = eds
      // Single-slot AI inputs (model / memory) replace their existing connection.
      const tgt = rf.getNode(c.target) as RFNode | undefined
      const slot = tgt && byType[tgt.data.node.type]?.ai_inputs.find((a) => a.name === th)
      if (slot?.max === 1) next = next.filter((e) => !(e.target === c.target && e.targetHandle === th))
      return addEdge({ ...c, id: `e_${newId()}`, type: 'canvas', className: th.startsWith('ai_') ? 'ai' : undefined }, next)
    })
    markDirty()
  }, [byType, rf, setEdges])

  const onConnectEnd = useCallback((event: MouseEvent | TouchEvent, state: FinalConnectionState) => {
    if (state.isValid || !state.fromNode || !state.fromHandle) return
    const pt = 'changedTouches' in event ? event.changedTouches[0] : event
    setPalette({
      pending: {
        nodeId: state.fromNode.id, handleId: state.fromHandle.id ?? 'main', handleType: state.fromHandle.type,
        position: rf.screenToFlowPosition({ x: pt.clientX, y: pt.clientY }),
      },
    })
  }, [rf])

  const addNode = useCallback((spec: NodeTypeSpec, position?: { x: number; y: number }, pending?: Pending) => {
    const existing = currentNodes()
    let pos = position ?? pending?.position
    const ins = pending?.insertEdge
    if (ins) {
      // n8n-style insert: place the node right after the source and push the target branch
      // to the right when there isn't enough room between them.
      const src = rf.getNode(pending!.nodeId)
      const tgt = rf.getNode(ins.target)
      if (src && tgt) {
        const GAP = 80
        const srcRight = src.position.x + (src.measured?.width ?? 92)
        // Same row as the target keeps sibling branches clear of the new node.
        pos = { x: srcRight + GAP + 44, y: tgt.position.y + (tgt.measured?.height ?? 88) / 2 }
        const need = srcRight + GAP + 88 + GAP - tgt.position.x
        if (need > 0) {
          const shift = new Set([tgt.id])
          const queue = [tgt.id]
          const all = rf.getEdges()
          while (queue.length) {
            const cur = queue.shift()!
            for (const e of all) {
              if (e.source === cur && e.target !== src.id && !shift.has(e.target)) { shift.add(e.target); queue.push(e.target) }
            }
          }
          setNodes((ns) => ns.map((n) => (shift.has(n.id) ? { ...n, position: { x: n.position.x + need, y: n.position.y } } : n)))
        }
      }
    }
    if (!pos) {
      const el = document.querySelector('.canvas')?.getBoundingClientRect()
      pos = rf.screenToFlowPosition({ x: (el?.left ?? 0) + (el?.width ?? 800) / 2, y: (el?.top ?? 0) + (el?.height ?? 600) / 2 })
    }
    const node: WFNode = {
      id: newId(), type: spec.type, name: uniqueName(spec.label, existing), position: { x: pos.x - 44, y: pos.y - 44 },
      parameters: defaultParams(spec),
    }
    setNodes((ns) => [...ns.map((n) => ({ ...n, selected: false })), { ...toRF(node), selected: true }])
    if (pending) {
      const h = pending.handleId
      let c: Connection | null = null
      if (pending.handleType === 'source' && h.startsWith('ai_')) c = { source: pending.nodeId, sourceHandle: h, target: node.id, targetHandle: h }
      else if (pending.handleType === 'source') c = { source: pending.nodeId, sourceHandle: h, target: node.id, targetHandle: spec.inputs[0] ?? 'main' }
      else if (h.startsWith('ai_')) c = { source: node.id, sourceHandle: h, target: pending.nodeId, targetHandle: h }
      else c = { source: node.id, sourceHandle: spec.outputs[0] ?? 'main', target: pending.nodeId, targetHandle: h }
      if (c && (spec.inputs.length || pending.handleType === 'target' || h.startsWith('ai_'))) connect(c)
      if (ins && spec.inputs.length && spec.outputs.length) {
        setEdges((es) => es.filter((e) => e.id !== ins.id))
        connect({ source: node.id, sourceHandle: spec.outputs[0], target: ins.target, targetHandle: ins.targetHandle })
      }
    }
    setPalette(null)
    markDirty()
    if (!spec.provides && spec.type !== 'ui.sticky') setOpenNode(node.id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connect, rf, setNodes, setEdges])

  // Hover toolbar on connections: insert a node in between, or delete the connection.
  const edgeActions = useMemo<EdgeActions>(() => ({
    insert: (edgeId, at) => {
      const e = rf.getEdge(edgeId)
      if (!e) return
      setPalette({ pending: { nodeId: e.source, handleId: e.sourceHandle ?? 'main', handleType: 'source', position: at,
        insertEdge: { id: e.id, target: e.target, targetHandle: e.targetHandle ?? 'main' } } })
    },
    remove: (edgeId) => { setEdges((es) => es.filter((e) => e.id !== edgeId)); setDirty(true) },
  }), [rf, setEdges])

  const paletteFilter = useMemo(() => {
    const p = palette?.pending
    if (!p) return undefined
    if (p.handleId.startsWith('ai_')) {
      return p.handleType === 'target'
        ? (t: NodeTypeSpec) => t.provides === p.handleId
        : (t: NodeTypeSpec) => t.ai_inputs.some((a) => a.name === p.handleId)
    }
    return p.handleType === 'source'
      ? (t: NodeTypeSpec) => !t.provides && !t.trigger && t.inputs.length > 0
      : (t: NodeTypeSpec) => !t.provides && t.outputs.length + (t.dynamic_outputs ? 1 : 0) > 0
  }, [palette])

  const updateNode = (n: WFNode) => {
    setNodes((ns) => ns.map((x) => (x.id === n.id ? { ...x, data: { ...x.data, node: n } } : x)))
    markDirty()
  }

  const onDrop = (e: React.DragEvent) => {
    const type = e.dataTransfer.getData('application/debbuilder-node')
    if (!type || !byType[type]) return
    e.preventDefault()
    addNode(byType[type], rf.screenToFlowPosition({ x: e.clientX, y: e.clientY }))
  }

  // ---- save / run --------------------------------------------------------
  const save = useCallback(async (extra: Partial<Workflow> = {}) => {
    if (!wf) return
    try {
      const saved = await api.updateWorkflow(wf.id, {
        name: wf.name, settings: wf.settings, description: wf.description,
        nodes: currentNodes(), edges: edgesFromRF(rf.getEdges()), ...extra,
      })
      setWf((w) => (w ? { ...w, ...saved } : saved))
      setDirty(false)
      return saved
    } catch (e) {
      toast(String(e), 'error')
      throw e
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wf, rf])

  const ensureSaved = useCallback(async () => { if (dirty) await save() }, [dirty, save])

  /** Run the editor's current graph. With `destination`, only up to that node (n8n "Execute step"). */
  const run = async (destination?: string) => {
    if (!wf) return
    try {
      setRunning(true)
      setShowLog(true)
      await api.runWorkflow(wf.id, {
        workflow: { nodes: currentNodes(), edges: edgesFromRF(rf.getEdges()), settings: wf.settings },
        destination_node_id: destination,
      })
    } catch (e) {
      setRunning(false)
      toast(String(e), 'error')
    }
  }

  const toggleActive = async () => {
    if (!wf) return
    const saved = await save({ active: !wf.active })
    if (saved) toast(saved.active ? 'Workflow activated' : 'Workflow deactivated')
  }

  /** Add nodes/edges (pasted or imported) to the canvas with fresh ids, offset and selected. */
  const insertGraph = (data: { nodes?: WFNode[]; edges?: WFEdge[] }) => {
    if (!Array.isArray(data?.nodes)) return 0
    const existing = currentNodes()
    const idMap: Record<string, string> = {}
    const pasted: WFNode[] = data.nodes.filter((n) => byType[n.type]).map((n) => {
      idMap[n.id] = newId()
      const node = { ...n, id: idMap[n.id], name: uniqueName(n.name, existing), position: { x: (n.position?.x ?? 0) + 40, y: (n.position?.y ?? 0) + 40 } }
      existing.push(node)
      return node
    })
    const pe: WFEdge[] = (data.edges ?? []).filter((x) => idMap[x.source] && idMap[x.target])
      .map((x) => ({ ...x, id: `e_${newId()}`, source: idMap[x.source], target: idMap[x.target] }))
    setNodes((ns) => [...ns.map((n) => ({ ...n, selected: false })), ...pasted.map((n) => ({ ...toRF(n), selected: true }))])
    setEdges((es) => [...es, ...pe.map(edgeToRF)])
    markDirty()
    return pasted.length
  }

  // ---- keyboard: save, copy/paste ------------------------------------------
  useEffect(() => {
    const inField = (t: EventTarget | null) => t instanceof HTMLElement && (t.closest('input, textarea, select, .modal, .palette') !== null)
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); save().then(() => toast('Saved')) }
      else if (e.key === 'Tab' && !inField(e.target)) { e.preventDefault(); setPalette({}) }
    }
    const onCopy = (e: ClipboardEvent) => {
      if (inField(e.target)) return
      const sel = (rf.getNodes() as RFNode[]).filter((n) => n.selected)
      if (!sel.length) return
      const ids = new Set(sel.map((n) => n.id))
      const data = { nodes: fromRF(sel), edges: edgesFromRF(rf.getEdges().filter((x) => ids.has(x.source) && ids.has(x.target))) }
      e.clipboardData?.setData('text/plain', JSON.stringify(data, null, 2))
      e.preventDefault()
      toast(`Copied ${sel.length} node(s)`)
    }
    const onPaste = (e: ClipboardEvent) => {
      if (inField(e.target)) return
      try {
        const data = JSON.parse(e.clipboardData?.getData('text/plain') ?? '')
        if (!Array.isArray(data?.nodes)) return
        e.preventDefault()
        insertGraph(data)
      } catch { /* not ours */ }
    }
    window.addEventListener('keydown', onKey)
    window.addEventListener('copy', onCopy)
    window.addEventListener('paste', onPaste)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('copy', onCopy)
      window.removeEventListener('paste', onPaste)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [save, rf, byType])

  if (!wf) return <div className="empty">Loading workflow…</div>

  const types = new Set(nodes.map((n) => n.data.node.type))
  const chatNode = nodes.find((n) => n.data.node.type === 'trigger.chat')
  const opened = nodes.find((n) => n.id === openNode)

  const download = () => downloadJson({ name: wf.name, description: wf.description, nodes: currentNodes(), edges: edgesFromRF(rf.getEdges()),
    settings: wf.settings, tags: wf.tags }, `${wf.name}.json`)
  const crumbs = folderPath(folders, wf.folder_id)
  const home = wf.folder_id ? `/folder/${wf.folder_id}` : '/'
  const setTab = (t: 'editor' | 'executions') => navigate(t === 'editor' ? `/workflow/${wf.id}` : `/workflow/${wf.id}/executions`)
  const importFile = async (f: File) => {
    try {
      const data = JSON.parse(await f.text())
      const graph = Array.isArray(data?.workflows) ? data.workflows[0] : data
      const n = insertGraph(graph)
      toast(n ? `Imported ${n} node${n === 1 ? '' : 's'} into this workflow` : 'No nodes found in that file', n ? 'info' : 'error')
    } catch {
      toast('Import failed: not a workflow JSON file', 'error')
    }
  }
  /** n8n "Debug in editor": pin the execution's trigger output so the next run replays it. */
  const debugExecution = (ex: Execution) => {
    const trig = ex.node_runs?.find((r) => r.node_id === ex.trigger_node) ?? ex.node_runs?.[0]
    const nodeId = trig?.node_id ?? ex.trigger_node
    const items = trig?.output?.main ?? ex.input ?? []
    if (!nodeId || !rf.getNode(nodeId)) return toast("This execution's trigger is no longer in the workflow", 'error')
    setNodes((ns) => ns.map((n) => (n.id === nodeId ? { ...n, data: { ...n.data, node: { ...n.data.node, pin_data: items } } } : n)))
    markDirty()
    setTab('editor')
    toast(`Pinned ${items.length} item${items.length === 1 ? '' : 's'} from that run on "${trig?.name ?? 'the trigger'}". Execute to debug.`)
  }
  const menu = [
    { label: 'Duplicate', icon: 'copy', onClick: () => setDialog('duplicate') },
    { label: 'Move to folder…', icon: 'folder-input', onClick: () => setDialog('move') },
    { divider: true, label: '' },
    { label: 'Import from file…', icon: 'upload', hint: 'adds nodes', onClick: () => importRef.current?.click() },
    { label: 'Download (export)', icon: 'download', onClick: download },
    { divider: true, label: '' },
    { label: 'Settings', icon: 'settings', onClick: () => setSettingsOpen(true) },
    { label: 'Delete workflow', icon: 'trash-2', danger: true, onClick: async () => {
      if (!await confirmDialog({ title: `Delete "${wf.name}"?`, message: 'The workflow and all its executions are deleted. This cannot be undone.', confirmLabel: 'Delete', danger: true })) return
      await api.deleteWorkflow(wf.id)
      navigate(home)
    } },
  ]

  return (
    <div className="editor">
      <div className="topbar">
        <Link to={home} className="btn ghost icon" title={crumbs.length ? `Back to ${crumbs[crumbs.length - 1].name}` : 'All workflows'}><Icon name="arrow-left" /></Link>
        {crumbs.length > 0 && (
          <Link to={home} className="ed-crumb hide-sm" title={crumbs.map((f) => f.name).join(' / ')}>
            <Icon name="folder" size={14} /> <span>{crumbs[crumbs.length - 1].name}</span>
          </Link>
        )}
        {crumbs.length > 0 && <span className="ed-crumb-sep hide-sm">/</span>}
        <input className="wf-name" value={wf.name} onChange={(e) => { setWf({ ...wf, name: e.target.value }); markDirty() }} />
        <span className={`save-state ${dirty ? 'dirty' : ''}`} title={dirty ? 'Unsaved changes' : 'All changes saved'}><span className="btn-text">{dirty ? 'Unsaved changes' : 'All changes saved'}</span></span>
        <span className="spacer" />
        <div className="tabs ed-tabs" role="tablist">
          <button role="tab" aria-selected={tab === 'editor'} className={`tab ${tab === 'editor' ? 'active' : ''}`} onClick={() => setTab('editor')}>Editor</button>
          <button role="tab" aria-selected={tab === 'executions'} className={`tab ${tab === 'executions' ? 'active' : ''}`} onClick={() => setTab('executions')}>Executions</button>
        </div>
        <span className="spacer" />
        <label className={`active-pill ${wf.active ? 'on' : ''}`} title="Active workflows listen for webhooks, schedules and WhatsApp">
          <span className="btn-text">{wf.active ? 'Active' : 'Inactive'}</span>
          <span className="switch"><input type="checkbox" checked={wf.active} onChange={toggleActive} disabled={readOnly} /><span /></span>
        </label>
        {(types.has('trigger.chat') || types.has('trigger.voice')) && (
          <div className="group">
            {types.has('trigger.chat') && <button className={`btn sm ${panel === 'chat' ? 'on' : ''}`} onClick={() => setPanel(panel === 'chat' ? null : 'chat')}><Icon name="message-circle" size={15} /> <span className="btn-text">Chat</span></button>}
            {types.has('trigger.voice') && <button className={`btn sm ${panel === 'voice' ? 'on' : ''}`} onClick={() => setPanel(panel === 'voice' ? null : 'voice')}><Icon name="mic" size={15} /> <span className="btn-text">Voice</span></button>}
          </div>
        )}
        <div className="group hide-sm">
          <button className={`btn sm icon ${showLog ? 'on' : ''}`} title="Execution logs" onClick={() => setShowLog(!showLog)}><Icon name="scroll-text" size={16} /></button>
          <button className="btn sm icon" title="Download JSON" onClick={download}><Icon name="download" size={16} /></button>
          <button className="btn sm icon" title="Workflow settings" onClick={() => setSettingsOpen(true)}><Icon name="settings" size={16} /></button>
        </div>
        <Menu items={menu} title="Workflow actions" className="btn icon" />
        <input ref={importRef} type="file" accept=".json,application/json" hidden onChange={(e) => { if (e.target.files?.[0]) importFile(e.target.files[0]); e.target.value = '' }} />
        {!readOnly && <button className={`btn ${dirty ? 'primary' : ''}`} onClick={() => save().then(() => toast('Saved'))} disabled={!dirty} title="Save (Ctrl+S)"><Icon name="save" size={15} /> <span className="btn-text">Save</span> <span className="kbd">Ctrl+S</span></button>}
      </div>
      {readOnly && <div className="readonly-banner"><Icon name="eye" size={14} /> View only: your role can't change or run this workflow.</div>}

      {tab === 'executions' && (
        <WorkflowExecutionsView workflowId={wf.id} execId={routeExecId} onDebug={debugExecution}
          onSelect={(eid) => navigate(eid ? `/workflow/${wf.id}/executions/${eid}` : `/workflow/${wf.id}/executions`, { replace: true })} />
      )}
      {/* The editor stays mounted on the Executions tab so unsaved changes and the canvas view survive. */}
      <div className={`ed-pane ${tab === 'editor' ? '' : 'ed-hidden'}`}>
      <div className="canvas-wrap">
        <div className="canvas" onDrop={onDrop} onDragOver={(e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'move' }}>
          <EdgeActionsCtx.Provider value={edgeActions}>
          <ReactFlow
            nodes={viewNodes} edges={viewEdges} nodeTypes={nodeTypes} edgeTypes={edgeTypes}
            onNodesChange={(c) => { onNodesChange(c); if (c.some((x) => x.type !== 'select' && x.type !== 'dimensions')) markDirty() }}
            onEdgesChange={(c) => { onEdgesChange(c); if (c.some((x) => x.type !== 'select')) markDirty() }}
            onConnect={connect} onConnectEnd={onConnectEnd} isValidConnection={isValidConnection}
            onNodeDoubleClick={(_, n) => setOpenNode(n.id)}
            onNodeClick={(_, n) => setLogFocus(n.id)}
            deleteKeyCode={['Delete', 'Backspace']} multiSelectionKeyCode={['Control', 'Meta', 'Shift']}
            defaultEdgeOptions={{ type: 'canvas' }} snapToGrid snapGrid={[20, 20]} minZoom={0.2} maxZoom={2}
            proOptions={{ hideAttribution: true }}
          >
            <Background variant={BackgroundVariant.Dots} gap={20} size={1.4} color="var(--canvas-dot)" />
            <Controls showInteractive={false} />
            <MiniMap pannable zoomable nodeColor={(n) => byType[(n.data as FlowNodeData).node.type]?.color ?? '#999'} />
          </ReactFlow>
          </EdgeActionsCtx.Provider>

          {nodes.length === 0 && (
            <div className="empty-canvas">
              <div>
                <div className="add-first" onClick={() => setPalette({})}><Icon name="plus" size={40} strokeWidth={1.6} /></div>
                <b>Add first step</b>
                <p className="muted small">Pick a trigger: chat, webhook, schedule, WhatsApp or voice.<br />Tip: press <span className="kbd">Tab</span> anytime to add a node.</p>
              </div>
            </div>
          )}
          {!readOnly && <div className="fab"><button className="btn primary" title="Add node (Tab)" onClick={() => setPalette({})}><Icon name="plus" /></button></div>}
          {nodes.length > 0 && can('workflow:execute') && (
            <div className="canvas-actions">
              <button className="btn primary" onClick={() => run()} disabled={running}>
                <Icon name={running ? 'loader-circle' : 'flask-conical'} className={running ? 'spin' : ''} /> {running ? 'Executing…' : 'Execute workflow'}
              </button>
              {running && execId && <button className="btn" onClick={() => api.cancel(execId)}><Icon name="square" size={14} /> Stop</button>}
              {chatNode && panel !== 'chat' && <button className="btn" onClick={() => setPanel('chat')}><Icon name="message-circle" /> Open chat</button>}
            </div>
          )}
          {waiting && execId && (
            <div className="banner">
              <span className="b-icon"><Icon name="user-check" size={16} /></span>
              <div className="small"><b>{waiting.node_name}</b> is waiting for approval<div className="muted">{waiting.message}</div></div>
              <button className="btn sm primary" onClick={() => api.resume(execId, true).then(() => setWaiting(null))}>Approve</button>
              <button className="btn sm danger" onClick={() => api.resume(execId, false).then(() => setWaiting(null))}>Reject</button>
            </div>
          )}
          {palette && <Palette filter={paletteFilter} onPick={(t) => addNode(t, undefined, palette.pending)} onClose={() => setPalette(null)} />}
        </div>
        {panel === 'chat' && <ChatPanel workflowId={wf.id} welcome={String(chatNode?.data.node.parameters.welcome ?? '')} ensureSaved={ensureSaved} subscribe={subscribe} onClose={() => setPanel(null)} />}
        {panel === 'voice' && (
          <Suspense fallback={<div className="side-panel empty">Loading…</div>}>
            <VoicePanel workflowId={wf.id} ensureSaved={ensureSaved} onClose={() => setPanel(null)} />
          </Suspense>
        )}
      </div>

      {showLog && (
        <ExecutionLog meta={execMeta} runs={execRuns} lines={logLines} focusNode={logFocus} onClose={() => setShowLog(false)}
          onFocusNode={(nid) => {
            const n = rf.getNode(nid)
            if (!n) return
            setNodes((ns) => ns.map((x) => ({ ...x, selected: x.id === nid })))
            rf.setCenter(n.position.x + 46, n.position.y + 46, { zoom: rf.getZoom(), duration: 300 })
          }} />
      )}
      </div>

      {opened && opened.type === 'flow' && (
        <NodeDetails node={opened.data.node} run={runData[opened.id]} runsByNode={runData} workflowId={wf.id}
          parents={edges.filter((e) => e.target === opened.id && !(e.targetHandle ?? '').startsWith('ai_')).map((e) => ({
            id: e.source, port: e.sourceHandle ?? 'main',
            name: (rf.getNode(e.source) as RFNode | undefined)?.data.node.name ?? e.source,
          }))}
          onChange={updateNode} onClose={() => setOpenNode(null)}
          onRun={run} />
      )}
      {opened && opened.type === 'sticky' && (
        <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && setOpenNode(null)}>
          <div className="modal">
            <div className="modal-head"><h2>Sticky note</h2><button className="btn ghost icon" onClick={() => setOpenNode(null)}><Icon name="x" /></button></div>
            <div className="modal-body">
              <textarea className="textarea" rows={10} value={String(opened.data.node.parameters.content ?? '')}
                onChange={(e) => updateNode({ ...opened.data.node, parameters: { content: e.target.value } })} />
            </div>
          </div>
        </div>
      )}
      {dialog === 'duplicate' && (
        <PromptDialog title="Duplicate workflow" label="Name" initial={`${wf.name} (copy)`} confirm="Duplicate" onClose={() => setDialog(null)}
          onSubmit={async (name) => {
            await ensureSaved()
            const copy = await api.duplicateWorkflow(wf.id, { name })
            toast('Duplicated. Opening the copy')
            navigate(`/workflow/${copy.id}`)
          }} />
      )}
      {dialog === 'move' && (
        <MoveDialog title={`Move "${wf.name}"`} folders={folders} current={wf.folder_id ?? null} onClose={() => setDialog(null)}
          onPick={async (folderId) => {
            const saved = await api.updateWorkflow(wf.id, { folder_id: folderId })
            setWf((w) => (w ? { ...w, folder_id: saved.folder_id } : w))
            toast(`Moved to ${folderId ? folders.find((f) => f.id === folderId)?.name : 'Home'}`)
          }} />
      )}
      {settingsOpen && <SettingsModal wf={wf} onClose={() => setSettingsOpen(false)} onChange={(w) => { setWf(w); markDirty() }} onDelete={async () => {
        if (!await confirmDialog({ title: `Delete "${wf.name}"?`, message: 'The workflow and all its executions are deleted. This cannot be undone.', confirmLabel: 'Delete', danger: true })) return
        try { await api.deleteWorkflow(wf.id); navigate(home) } catch (e) { toast(String(e), 'error') }
      }} />}
    </div>
  )
}

function SettingsModal({ wf, onChange, onClose, onDelete }: { wf: Workflow; onChange: (w: Workflow) => void; onClose: () => void; onDelete: () => void }) {
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal">
        <div className="modal-head"><Icon name="settings" /><h2>Workflow settings</h2><button className="btn ghost icon" onClick={onClose}><Icon name="x" /></button></div>
        <div className="modal-body">
          <div className="field">
            <label>Description</label>
            <textarea className="textarea" value={wf.description} onChange={(e) => onChange({ ...wf, description: e.target.value })} />
          </div>
          <PropertyField spec={{ name: 'error_workflow_id', label: 'Error workflow (runs its Error Trigger on failure)', type: 'workflow', default: null }}
            value={wf.settings.error_workflow_id} onChange={(v) => onChange({ ...wf, settings: { ...wf.settings, error_workflow_id: (v as string) || undefined } })} />
          <PropertyField spec={{ name: 'variables', label: 'Variables ($vars.key)', type: 'collection', default: [], fields: [
            { name: 'key', label: 'Key', type: 'string', default: '' }, { name: 'value', label: 'Value', type: 'string', default: '' }] }}
            value={wf.settings.variables ?? []} onChange={(v) => onChange({ ...wf, settings: { ...wf.settings, variables: v as { key: string; value: string }[] } })} />
          <p className="muted small">Workflow ID: <code>{wf.id}</code></p>
        </div>
        <div className="modal-foot">
          <button className="btn danger" onClick={onDelete}><Icon name="trash-2" size={14} /> Delete workflow</button>
          <span className="spacer" />
          <button className="btn primary" onClick={onClose}>Done</button>
        </div>
      </div>
    </div>
  )
}

export default function Editor() {
  return <ReactFlowProvider><EditorInner /></ReactFlowProvider>
}
