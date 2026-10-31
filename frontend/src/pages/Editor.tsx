import {
  Background, BackgroundVariant, ControlButton, Controls, MiniMap, ReactFlow, ReactFlowProvider, SelectionMode, addEdge, useEdgesState, useNodesState,
  useReactFlow, useUpdateNodeInternals,
  type Connection, type Edge, type FinalConnectionState, type Node,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { ApiError, api, conflictOf, downloadJson, openSocket } from '../api'
import { EdgeActionsCtx, edgeTypes, type EdgeActions } from '../components/CanvasEdge'
import { ChatPanel } from '../components/ChatPanel'
import { Menu, MoveDialog, PromptDialog, confirmDialog, folderPath } from '../components/Dialogs'
import { ExecutionLog, type ExecMeta } from '../components/ExecutionLog'
import { subNodeStats, upsertSubRun } from '../components/logTree'
import { nodeTypes, type FlowNodeData } from '../components/FlowNode'
import { NodeDetails } from '../components/NodeDetails'
import { Palette } from '../components/Palette'
import { PropertyField } from '../components/PropertyField'
import { Icon, defaultParams, newId, toast, uniqueName, useCatalog, useDialog } from '../lib'
import type { ExecEvent, Execution, Folder, Item, NodeRun, NodeTypeSpec, RunState, StateKey, WFEdge, WFNode, Workflow, WorkflowVersion } from '../types'
import { WorkflowExecutionsView } from './Executions'
import { useAuth } from '../auth'
import { CanvasContextMenu, SelectionToolbar, type MenuTarget } from '../canvas/CanvasMenus'
import { tidyUp } from '../canvas/tidyUp'
import '../pages.css'  // styles for pages loaded on demand

// LiveKit client is large; load it only when the voice panel opens.
const AUTOSAVE_MS = 800        // after the last change (a drag only saves once you let go)
const AUTOSAVE_RETRY_MS = 5000
const VoicePanel = lazy(() => import('../components/VoicePanel').then((m) => ({ default: m.VoicePanel })))
const VersionCompare = lazy(() => import('../components/VersionCompare').then((m) => ({ default: m.VersionCompare })))

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

/** Group an execution's ordered node runs per node: latest run drives the canvas, all runs feed the NDV.
 *  Linear in the number of runs. With `prev` (the last result), a node whose runs didn't change keeps its
 *  RunState object, so canvas nodes and the open panel don't re-render for every event of a long loop. */
export function runStateFromRuns(runs: NodeRun[], prev?: Record<string, RunState>): Record<string, RunState> {
  const groups = new Map<string, NodeRun[]>()
  for (const r of runs) {
    const g = groups.get(r.node_id)
    if (g) g.push(r)
    else groups.set(r.node_id, [r])
  }
  const out: Record<string, RunState> = {}
  for (const [id, all] of groups) {
    const old = prev?.[id]
    if (old?.runs && old.runs.length === all.length && old.runs[all.length - 1] === all[all.length - 1] && old.runs.every((x, i) => x === all[i])) {
      out[id] = old
      continue
    }
    const r = all[all.length - 1]
    let duration = 0
    for (const x of all) duration += x.duration_ms ?? 0
    out[id] = {
      status: r.status, input: r.input, output: r.output, error: r.error, started_at: r.started_at, pinned: r.pinned,
      // Canvas shows total time across loop iterations, like n8n.
      duration_ms: duration,
      counts: r.item_counts ?? Object.fromEntries(Object.entries(r.output ?? {}).map(([k, v]) => [k, v.length])),
      runs: all,
    }
  }
  return out
}

/** Undo history is capped by size as well as steps (large workflows can be several MB per snapshot). */
const HISTORY_MAX_STEPS = 100
const HISTORY_MAX_CHARS = 16_000_000  // ~32 MB of UTF-16 across past + future

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
  // Autosave: every change bumps `changes`; a save only clears `dirty` if nothing changed while it ran.
  const changes = useRef(0)
  const savedVersion = useRef(0)  // `changes` value the server has
  const [saveState, setSaveState] = useState<'saved' | 'saving' | 'error'>('saved')
  // Optimistic concurrency: the server's revision of what this tab last loaded / saved. A save from another tab or
  // session bumps it, and ours is then refused (409) instead of silently overwriting theirs.
  const revision = useRef<number | undefined>(undefined)
  const [conflict, setConflict] = useState<Workflow | null>(null)
  const conflictRef = useRef<Workflow | null>(null)
  const autosave = useRef<{ timer?: number; inflight: boolean; again: boolean }>({ inflight: false, again: false })
  const flushRef = useRef<() => Promise<void>>(async () => {})
  const touched = () => {
    changes.current++
    setDirty(true)
    window.clearTimeout(autosave.current.timer)
    autosave.current.timer = window.setTimeout(() => { flushRef.current() }, AUTOSAVE_MS)  // after the edits settle
  }
  const [ctxMenu, setCtxMenu] = useState<MenuTarget | null>(null)
  const [panning, setPanning] = useState(false)
  const pointer = useRef<{ x: number; y: number } | null>(null)
  // ---- responsive canvas ------------------------------------------------------------------------------
  const canvasRef = useRef<HTMLDivElement>(null)
  const userMoved = useRef(false)  // the user panned / zoomed: leave their view alone on resize
  /** Fit the workflow so it stays readable on any screen: everything when that keeps nodes legible,
   *  otherwise the full width (at a readable zoom) starting from the top, where the triggers are. */
  const smartFit = useCallback((duration = 0) => {
    const el = canvasRef.current
    const ns = rf.getNodes()
    if (!el || !ns.length) return
    const READABLE = 0.55, PAD_X = 40, PAD_TOP = 84, PAD_BOTTOM = 96
    const W = el.clientWidth, H = el.clientHeight
    // Hidden (Executions tab) or not laid out yet: nothing to fit, and an animated move on a zero-size
    // canvas interpolates through NaN (SVG errors). The resize observer fits once it has a size again.
    if (W <= 0 || H <= 0) return
    const b = rf.getNodesBounds(ns)
    const fitAll = Math.min(1, (W - 2 * PAD_X) / Math.max(b.width, 1), (H - PAD_TOP - PAD_BOTTOM) / Math.max(b.height, 1))
    if (fitAll >= READABLE) {
      rf.fitView({ padding: 0.3, maxZoom: 1, duration })
      return
    }
    const z = Math.max(READABLE, Math.min(1, (W - 2 * PAD_X) / Math.max(b.width, 1)))
    const x = b.width * z <= W - 2 * PAD_X ? (W - b.width * z) / 2 - b.x * z : PAD_X - b.x * z
    rf.setViewport({ x, y: PAD_TOP - b.y * z, zoom: z }, { duration })
  }, [rf])
  const refit = useCallback((duration = 200) => { userMoved.current = false; smartFit(duration) }, [smartFit])
  useEffect(() => {
    const el = canvasRef.current
    if (!el) return
    let t: number | undefined
    const ro = new ResizeObserver(() => {
      window.clearTimeout(t)
      t = window.setTimeout(() => { if (!userMoved.current) smartFit(150) }, 120)
    })
    ro.observe(el)
    return () => { ro.disconnect(); window.clearTimeout(t) }
  }, [smartFit])
  const placeAt = useRef<{ x: number; y: number } | null>(null)  // "Add node" from the canvas menu: drop it where you right-clicked
  // Undo/redo: snapshots of the graph, committed once edits settle (a drag or a burst of typing = one step).
  const history = useRef<{ past: string[]; future: string[]; last: string | null; timer?: number }>({ past: [], future: [], last: null })
  const [palette, setPalette] = useState<{ pending?: Pending } | null>(null)
  const [openNode, setOpenNode] = useState<string | null>(null)
  const [execRuns, setExecRuns] = useState<NodeRun[]>([])
  // Live events arrive one websocket message at a time (thousands in a long loop): apply them once per frame.
  const runQueue = useRef<{ fns: ((rs: NodeRun[]) => NodeRun[])[]; raf?: number }>({ fns: [] })
  const updateRuns = useCallback((fn: (rs: NodeRun[]) => NodeRun[]) => {
    const q = runQueue.current
    q.fns.push(fn)
    if (q.raf !== undefined) return
    q.raf = requestAnimationFrame(() => {
      q.raf = undefined
      const fns = q.fns
      q.fns = []
      setExecRuns((rs) => fns.reduce((acc, f) => f(acc), rs))
    })
  }, [])
  useEffect(() => () => { if (runQueue.current.raf !== undefined) cancelAnimationFrame(runQueue.current.raf) }, [])
  const [execMeta, setExecMeta] = useState<ExecMeta | null>(null)
  const runDataRef = useRef<Record<string, RunState>>({})
  const runData = useMemo(() => (runDataRef.current = runStateFromRuns(execRuns, runDataRef.current)), [execRuns])
  const nodeNames = useMemo(() => Object.fromEntries(nodes.map((n) => [n.id, n.data.node.name])), [nodes])
  const [execId, setExecId] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const [waiting, setWaiting] = useState<Execution['waiting_for'] | null>(null)
  const [panel, setPanel] = useState<'chat' | 'voice' | null>(null)
  const [showLog, setShowLog] = useState(false)
  const [logLines, setLogLines] = useState<string[]>([])
  const [logFocus, setLogFocus] = useState<string | null>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  /** Compare view: [before, after] ('draft' or version ids), the versions to pick from and the editor graph then. */
  const [compare, setCompare] = useState<{ pair: [string, string]; versions: WorkflowVersion[]; draft: { nodes: WFNode[]; edges: WFEdge[] } } | null>(null)
  const [publishing, setPublishing] = useState(false)
  const listeners = useRef(new Set<(ev: ExecEvent) => void>())
  const execRef = useRef<string | null>(null)
  const panelRef = useRef<'chat' | 'voice' | null>(null)
  panelRef.current = panel

  // ---- load -------------------------------------------------------------
  useEffect(() => {
    if (!id) return
    let alive = true // ignore a superseded response (route change / StrictMode double effect)
    api.workflow(id).then((w) => {
      if (!alive) return
      setWf(w)
      revision.current = w.revision
      conflictRef.current = null
      setConflict(null)
      setNodes(w.nodes.map((n) => toRF(n)))
      setEdges(w.edges.map(edgeToRF))
      setDirty(false)
      savedVersion.current = changes.current
      setSaveState('saved')
      history.current = { past: [], future: [], last: null }
      scheduleCommit()
      requestAnimationFrame(() => {
        userMoved.current = false
        smartFit()
        // nodes are measured a moment after they mount: fit again with their real sizes
        window.setTimeout(() => { if (!userMoved.current) smartFit() }, 250)
        // Replacing nodes can drop React Flow's cached handle positions without a re-measure,
        // which hides every edge; force one once the nodes are on screen.
        requestAnimationFrame(() => updateInternals(w.nodes.map((n) => n.id)))
      })
    }).catch((e) => toast(String(e), 'error'))
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, rf, setEdges, setNodes, updateInternals])

  // ---- live execution events ---------------------------------------------
  useEffect(() => {
    if (!id) return
    return openSocket(`/ws/workflows/${id}`, (ev: ExecEvent) => {
      listeners.current.forEach((l) => l(ev))
      if (ev.type === 'execution_started') {
        // Like n8n, the canvas shows test runs: Execute workflow / step and test webhooks ("manual"), and chat or
        // voice runs while their test panel is open. Production runs (schedules, triggers, webhooks) only go to
        // the Executions tab, so a busy workflow doesn't flicker on the canvas while you edit it.
        const test = ev.mode === 'manual' || (panelRef.current !== null && (ev.mode === 'chat' || ev.mode === 'voice'))
        if (!test) return
        execRef.current = ev.execution_id
        setExecId(ev.execution_id)
        updateRuns(() => [])
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
        updateRuns((rs) => [...rs, {
          node_id: ev.node_id!, name: ev.name ?? n?.data.node.name ?? ev.node_id!, type: n?.data.node.type ?? '',
          status: 'running', started_at: ev.started_at ?? new Date().toISOString(), run_index: ev.run_index ?? 0,
        }])
      } else if (ev.type === 'node_finished' && ev.node_id) {
        updateRuns((rs) => {
          const i = rs.findLastIndex(sameRun)
          if (i < 0) return rs
          const next = [...rs]
          next[i] = { ...next[i], status: ev.status as NodeRun['status'], output: ev.output as Record<string, Item[]>,
            item_counts: ev.item_counts, error: ev.error, duration_ms: ev.duration_ms, finished_at: ev.finished_at, pinned: ev.pinned }
          return next
        })
      } else if (ev.type === 'sub_run' && ev.node_id && ev.sub_run) {
        // A model / tool / memory call inside an AI node: grows the log tree while the node runs.
        updateRuns((rs) => upsertSubRun(rs, ev.node_id!, ev.sub_run!))
      } else if (ev.type === 'node_waiting' && ev.node_id) {
        updateRuns((rs) => rs.map((r) => (r.node_id === ev.node_id && r.status === 'running' ? { ...r, status: 'waiting' } : r)))
      } else if (ev.type === 'log') {
        const n = rf.getNode(ev.node_id ?? '') as RFNode | undefined
        setLogLines((l) => [...l, `[${n?.data.node.name ?? ev.node_id}] ${ev.message}`])
      } else if (ev.type === 'execution_finished') {
        setRunning(false)
        setExecMeta((m) => (m ? { ...m, status: ev.status ?? 'success', error: ev.error } : m))
        if (ev.status === 'error') toast(`Execution failed: ${ev.error}`, 'error')
        if (ev.status === 'waiting') setWaiting(ev.waiting_for ?? null)
        api.execution(ev.execution_id).then((ex) => {
          updateRuns(() => ex.node_runs ?? [])
          setExecMeta((m) => ({ ...(m ?? { id: ex.id }), id: ex.id, status: ex.status, mode: ex.mode, started_at: ex.started_at, duration_ms: ex.duration_ms, error: ex.error }))
        }).catch(() => {})
      }
    })
  }, [id, updateRuns])

  const subscribe = useCallback((fn: (ev: ExecEvent) => void) => {
    listeners.current.add(fn)
    return () => { listeners.current.delete(fn) }
  }, [])

  // Push run data into node components & edge labels.
  // Sub-nodes (models, tools, memory) never run on their own: show what their AI node did with them.
  const subStats = useMemo(() => subNodeStats(execRuns), [execRuns])
  const viewNodes = useMemo(() => nodes.map((n) => (n.data.run === runData[n.id] && n.data.sub === subStats[n.id] ? n
    : { ...n, data: { ...n.data, run: runData[n.id], sub: subStats[n.id] } })), [nodes, runData, subStats])
  const viewEdges = useMemo(() => edges.map((e) => {
    const c = runData[e.source]?.counts?.[e.sourceHandle ?? 'main']
    const tgt = runData[e.target]?.status
    // Animate the edge while its target is processing the items it just received.
    const state = c && tgt === 'running' ? 'flowing' : c && tgt ? 'done' : ''
    const className = [e.targetHandle?.startsWith('ai_') ? 'ai' : '', state].filter(Boolean).join(' ') || undefined
    return c ? { ...e, className, label: `${c} item${c === 1 ? '' : 's'}` } : { ...e, className }
  }), [edges, runData])

  // ---- graph editing -----------------------------------------------------
  // Pinned data can be large: snapshots refer to it by a token instead of copying it into every step. Pin data is
  // replaced (never mutated) on edit, so object identity is enough, and pin edits stay undoable (the token changes).
  const pins = useRef({ ids: new WeakMap<object, string>(), byId: new Map<string, unknown>(), seq: 0 })
  const pinToken = (data: unknown) => {
    if (!data || typeof data !== 'object') return data
    const P = pins.current
    let k = P.ids.get(data)
    if (!k) { k = `p${++P.seq}`; P.ids.set(data, k); P.byId.set(k, data) }
    return { $pin: k }
  }
  const pinValue = (data: unknown) => {
    const k = data && typeof data === 'object' && !Array.isArray(data) ? (data as { $pin?: unknown }).$pin : undefined
    return typeof k === 'string' && pins.current.byId.has(k) ? pins.current.byId.get(k) : data
  }
  const tokenized = (ns: WFNode[]) => ns.map((n) => (n.pin_data == null ? n : { ...n, pin_data: pinToken(n.pin_data) as WFNode['pin_data'] }))
  const snapshot = () => JSON.stringify({ nodes: tokenized(fromRF(rf.getNodes() as RFNode[])), edges: edgesFromRF(rf.getEdges()) })
  /** Drop the oldest steps beyond the step / size budget, and pinned data no step refers to any more. */
  const trimHistory = () => {
    const h = history.current
    if (h.past.length > HISTORY_MAX_STEPS) h.past.splice(0, h.past.length - HISTORY_MAX_STEPS)
    let size = (h.last?.length ?? 0) + h.past.reduce((a, x) => a + x.length, 0) + h.future.reduce((a, x) => a + x.length, 0)
    while (size > HISTORY_MAX_CHARS && h.past.length) size -= h.past.shift()!.length
    const P = pins.current
    if (P.byId.size > 20) {
      const used = new Set<string>()
      for (const st of [h.last ?? '', ...h.past, ...h.future]) for (const m of st.matchAll(/"\$pin":"(p\d+)"/g)) used.add(m[1])
      for (const k of [...P.byId.keys()]) if (!used.has(k)) P.byId.delete(k)
    }
  }
  const commit = () => {
    const h = history.current
    window.clearTimeout(h.timer)
    h.timer = undefined
    const now = snapshot()
    if (h.last === null) { h.last = now; return }
    if (now === h.last) return
    h.past.push(h.last)
    h.future = []
    h.last = now
    trimHistory()
  }
  function scheduleCommit() {
    window.clearTimeout(history.current.timer)
    history.current.timer = window.setTimeout(commit, 350)
  }
  const markDirty = () => { touched(); scheduleCommit() }
  const restore = (state: string) => {
    const s = JSON.parse(state) as { nodes: WFNode[]; edges: WFEdge[] }
    setNodes(s.nodes.map((n) => toRF(n.pin_data == null ? n : { ...n, pin_data: pinValue(n.pin_data) as WFNode['pin_data'] })))
    setEdges(s.edges.map(edgeToRF))
    history.current.last = state
    touched()  // undo / redo are saved like any edit
  }
  const undo = () => {
    commit()  // an edit still settling becomes its own step first
    const h = history.current
    const prev = h.past.pop()
    if (prev === undefined || h.last === null) return toast('Nothing to undo')
    h.future.push(h.last)
    restore(prev)
  }
  const redo = () => {
    commit()
    const h = history.current
    const next = h.future.pop()
    if (next === undefined || h.last === null) return toast('Nothing to redo')
    h.past.push(h.last)
    restore(next)
  }
  const currentNodes = () => fromRF(rf.getNodes() as RFNode[])
  /** Edges whose both ends still exist (the server refuses a graph with dangling connections). */
  const liveEdges = () => {
    const ids = new Set(rf.getNodes().map((n) => n.id))
    return edgesFromRF(rf.getEdges().filter((e) => ids.has(e.source) && ids.has(e.target)))
  }

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
    let pos = position ?? pending?.position ?? placeAt.current ?? undefined
    placeAt.current = null
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
    remove: (edgeId) => { setEdges((es) => es.filter((e) => e.id !== edgeId)); touched() },
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
  /** Take a workflow the server returned (save, publish, move…): its server-owned fields and revision. */
  const adopt = useCallback((saved: Workflow) => {
    if (saved.revision != null) revision.current = saved.revision
    // Only server-owned fields: what was typed while the request ran (name, settings…) stays.
    setWf((w) => (w ? { ...w, ...serverFields(saved) } : saved))
  }, [])

  /** Save the editor's draft. `force` overwrites a newer server version ("Keep mine" after a conflict). */
  const save = useCallback(async (extra: Partial<Workflow> = {}, quiet = false, force = false) => {
    if (!wf) return
    if (conflictRef.current && !force) throw new Error('This workflow was changed in another tab or session. Choose which version to keep first.')
    const version = changes.current
    try {
      const saved = await api.updateWorkflow(wf.id, {
        name: wf.name, settings: wf.settings, description: wf.description,
        nodes: currentNodes(), edges: liveEdges(), ...extra,
        // Without a revision from the server (older backend) there's nothing to check: save as before.
        ...(revision.current != null ? { revision: revision.current } : {}),
      })
      adopt(saved)
      savedVersion.current = Math.max(savedVersion.current, version)
      if (changes.current === version) setDirty(false)
      return saved
    } catch (e) {
      const latest = conflictOf(e)
      if (latest) {
        // Someone else saved first: stop autosaving and let the user choose (banner below the top bar).
        window.clearTimeout(autosave.current.timer)
        conflictRef.current = latest
        setConflict(latest)
        throw e
      }
      if (!quiet) toast(String(e), 'error')
      throw e
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wf, rf, adopt])

  /** Write pending changes now. Serialised: a change made during a save is written right after it. */
  const flush = useCallback(async () => {
    const a = autosave.current
    window.clearTimeout(a.timer)
    if (readOnly || changes.current === savedVersion.current) return  // nothing new since the last save
    if (conflictRef.current) return  // paused until the user picks a version
    if (a.inflight) { a.again = true; return }
    a.inflight = true
    setSaveState('saving')
    const version = changes.current
    try {
      await save({}, true)
      setSaveState('saved')
    } catch (e) {
      if (conflictOf(e)) { setSaveState('saved'); return }  // the conflict banner takes over; no retry loop
      setSaveState('error')
      // The server refused the content (empty name, invalid graph…): retrying won't help, say why. The next edit saves again.
      if (e instanceof ApiError && e.status >= 400 && e.status < 500 && e.status !== 429) { toast(`Couldn't save: ${e.message}`, 'error'); return }
      a.timer = window.setTimeout(() => { flushRef.current() }, AUTOSAVE_RETRY_MS)  // e.g. network blip
    } finally {
      a.inflight = false
      if (a.again || changes.current !== version) { a.again = false; if (changes.current !== version && !conflictRef.current) flushRef.current() }
    }
  }, [save, readOnly])
  flushRef.current = flush
  /** Wait for an autosave that's already running, then write whatever is still pending. */
  const settle = useCallback(async () => {
    for (let i = 0; i < 300 && autosave.current.inflight; i++) await new Promise((r) => window.setTimeout(r, 50))
    await flush()
  }, [flush])
  const ensureSaved = useCallback(async () => { if (dirty) await flush() }, [dirty, flush])

  // Leaving the editor, switching tabs or closing the window: write what's pending right away.
  useEffect(() => {
    const onHide = () => { if (document.visibilityState === 'hidden') flushRef.current() }
    const onUnload = (e: BeforeUnloadEvent) => {
      if (!autosave.current.inflight && changes.current === savedVersion.current) return
      flushRef.current()
      e.preventDefault()  // the browser asks "leave site?" only while a save is still pending
    }
    document.addEventListener('visibilitychange', onHide)
    window.addEventListener('beforeunload', onUnload)
    return () => {
      document.removeEventListener('visibilitychange', onHide)
      window.removeEventListener('beforeunload', onUnload)
      flushRef.current()  // navigating away inside the app
    }
  }, [])

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
    try {
      await settle()
      const saved = await save({ active: !wf.active })
      if (saved) toast(saved.active ? `Workflow activated · running v${saved.published_version}` : 'Workflow deactivated')
    } catch { /* toast / conflict banner already shown */ }
  }

  /** Conflict: "Load latest" replaces the editor with the server's version (undo brings yours back). */
  const loadLatest = () => {
    const latest = conflictRef.current
    if (!latest) return
    commit()  // your unsaved edits become an undo step
    const h = history.current
    const state = JSON.stringify({ nodes: tokenized(latest.nodes ?? []), edges: latest.edges ?? [] })
    if (h.last !== null && h.last !== state) { h.past.push(h.last); h.future = []; trimHistory() }
    h.last = state
    window.clearTimeout(h.timer)
    window.clearTimeout(autosave.current.timer)
    conflictRef.current = null
    setConflict(null)
    revision.current = latest.revision
    setWf(latest)
    setNodes((latest.nodes ?? []).map((n) => toRF(n)))
    setEdges((latest.edges ?? []).map(edgeToRF))
    savedVersion.current = changes.current
    setDirty(false)
    setSaveState('saved')
    requestAnimationFrame(() => requestAnimationFrame(() => updateInternals((latest.nodes ?? []).map((n) => n.id))))
    toast('Loaded the latest version. Undo (Ctrl+Z) brings your changes back.')
  }
  /** Conflict: "Keep mine" saves the editor over the newer version. */
  const keepMine = async () => {
    const latest = conflictRef.current
    if (!latest) return
    revision.current = latest.revision
    conflictRef.current = null
    setConflict(null)
    setSaveState('saving')
    try {
      await save({}, false, true)
      setSaveState('saved')
      toast('Your version was saved')
    } catch (e) {
      setSaveState(conflictOf(e) ? 'saved' : 'error')
    }
  }

  /** n8n's Publish: the saved draft becomes a new version and what production runs; activates the workflow. */
  const publish = async () => {
    if (!wf || publishing) return
    setPublishing(true)
    try {
      if (conflictRef.current) throw new Error('This workflow was changed in another tab or session. Choose which version to keep, then publish.')
      await settle()
      if (conflictRef.current) return  // the save just hit a conflict: the banner asks what to keep
      if (changes.current !== savedVersion.current) throw new Error("Couldn't save your latest changes, so nothing was published. Try again.")
      const saved = await api.publishWorkflow(wf.id)
      adopt(saved)
      toast(`Published v${saved.published_version} · production runs it now`)
    } catch (e) {
      if (!conflictOf(e)) toast(String(e), 'error')
    } finally {
      setPublishing(false)
    }
  }

  /** Version history "Restore": load that version into the editor as a normal (undoable, autosaved) edit. */
  const restoreVersion = async (v: WorkflowVersion) => {
    if (!wf) return
    const full = await api.workflowVersion(wf.id, v.id)
    commit()
    const h = history.current
    if (h.last !== null) { h.past.push(h.last); h.future = [] }
    restore(JSON.stringify({ nodes: tokenized(full.nodes ?? []), edges: full.edges ?? [] }))
    requestAnimationFrame(() => { smartFit(); requestAnimationFrame(() => updateInternals((full.nodes ?? []).map((n) => n.id))) })
    toast(`v${v.number} restored to the editor · publish it to put it live`)
  }

  /** Add nodes/edges (pasted or imported) to the canvas with fresh ids, selected. `at` = top-left of the group
   *  in flow coordinates (paste at the mouse, like n8n); otherwise offset by 40 px from where they were. */
  const insertGraph = (data: { nodes?: WFNode[]; edges?: WFEdge[] }, at?: { x: number; y: number }) => {
    if (!Array.isArray(data?.nodes)) return 0
    const existing = currentNodes()
    const idMap: Record<string, string> = {}
    const known = data.nodes.filter((n) => byType[n.type])
    const minX = Math.min(...known.map((n) => n.position?.x ?? 0)), minY = Math.min(...known.map((n) => n.position?.y ?? 0))
    const dx = at ? Math.round((at.x - minX) / 20) * 20 : 40, dy = at ? Math.round((at.y - minY) / 20) * 20 : 40
    const pasted: WFNode[] = known.map((n) => {
      idMap[n.id] = newId()
      const node = { ...n, id: idMap[n.id], name: uniqueName(n.name, existing), position: { x: (n.position?.x ?? 0) + dx, y: (n.position?.y ?? 0) + dy } }
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

  // ---- selection, clipboard, layout ----------------------------------------
  const selectedNodes = () => (rf.getNodes() as RFNode[]).filter((n) => n.selected)
  /** Selected nodes + the connections between them: the clipboard format (also valid workflow JSON to import). */
  const selectionData = (sel = selectedNodes()) => {
    const ids = new Set(sel.map((n) => n.id))
    return { nodes: fromRF(sel), edges: edgesFromRF(rf.getEdges().filter((x) => ids.has(x.source) && ids.has(x.target))) }
  }
  const pointerFlow = () => {
    const el = document.querySelector('.canvas')?.getBoundingClientRect()
    const pt = pointer.current
    return el && pt && pt.x >= el.left && pt.x <= el.right && pt.y >= el.top && pt.y <= el.bottom ? rf.screenToFlowPosition(pt) : undefined
  }
  const deleteSelected = () => {
    const ids = new Set(selectedNodes().map((n) => n.id))
    if (!ids.size) return
    setNodes((ns) => ns.filter((n) => !ids.has(n.id)))
    setEdges((es) => es.filter((e) => !ids.has(e.source) && !ids.has(e.target)))
    markDirty()
  }
  const copySelection = async (cut = false) => {
    const sel = selectedNodes()
    if (!sel.length) return
    try {
      await navigator.clipboard.writeText(JSON.stringify(selectionData(sel), null, 2))
      toast(`${cut ? 'Cut' : 'Copied'} ${sel.length} node${sel.length === 1 ? '' : 's'}`)
      if (cut) deleteSelected()
    } catch { toast('The browser blocked clipboard access. Use Ctrl+C instead.', 'error') }
  }
  const pasteFromClipboard = async (at?: { x: number; y: number }) => {
    try {
      const data = JSON.parse(await navigator.clipboard.readText())
      const n = insertGraph(data, at)
      if (!n) toast('The clipboard has no DebBuilder nodes', 'error')
    } catch { toast('The clipboard has no DebBuilder nodes (or the browser blocked access: use Ctrl+V)', 'error') }
  }
  const duplicateSelected = () => {
    const sel = selectedNodes()
    if (sel.length) insertGraph(selectionData(sel))
  }
  const selectAll = () => setNodes((ns) => ns.map((n) => ({ ...n, selected: true })))
  const toggleDisabled = (ids?: string[]) => {
    const target = new Set(ids ?? selectedNodes().map((n) => n.id))
    if (!target.size) return
    const sel = (rf.getNodes() as RFNode[]).filter((n) => target.has(n.id) && n.type !== 'sticky')
    const disable = sel.some((n) => !n.data.node.disabled)  // n8n: any enabled → disable all
    setNodes((ns) => ns.map((n) => (target.has(n.id) && n.type !== 'sticky' ? { ...n, data: { ...n.data, node: { ...n.data.node, disabled: disable || undefined } } } : n)))
    markDirty()
  }
  /** n8n "Tidy up": the selection when 2+ nodes are selected, otherwise the whole workflow. */
  const tidy = () => {
    const all = rf.getNodes() as RFNode[]
    const sel = all.filter((n) => n.selected)
    const scope = sel.length > 1 ? sel : all
    if (scope.length < 2) return
    const ids = new Set(scope.map((n) => n.id))
    const moves = tidyUp(
      scope.map((n) => ({ id: n.id, x: n.position.x, y: n.position.y, width: n.measured?.width ?? 92, height: n.measured?.height ?? 92, sticky: n.type === 'sticky' })),
      rf.getEdges().filter((e) => ids.has(e.source) && ids.has(e.target)).map((e) => ({ source: e.source, target: e.target, ai: (e.targetHandle ?? '').startsWith('ai_') })),
    )
    setNodes((ns) => ns.map((n) => {
      const m = moves[n.id]
      if (!m) return n
      const node = m.width ? { ...n.data.node, width: m.width, height: m.height } : n.data.node
      return { ...n, position: { x: m.x, y: m.y }, data: { ...n.data, node }, ...(m.width ? { style: { ...n.style, width: m.width, height: m.height } } : {}) }
    }))
    markDirty()
    if (sel.length <= 1) requestAnimationFrame(() => rf.fitView({ padding: 0.2, duration: 300, maxZoom: 1 }))
  }
  const addSticky = () => { const s = byType['ui.sticky']; if (s) addNode(s, pointerFlow()) }

  // The listeners below are registered once per mode; they call the latest handlers through this ref.
  const act = useRef({ refit, flush, run, undo, redo, tidy, duplicateSelected, toggleDisabled, addSticky, selectAll, deleteSelected, insertGraph,
    pointerFlow, selectionData, selectedNodes, save })
  act.current = { refit, flush, run, undo, redo, tidy, duplicateSelected, toggleDisabled, addSticky, selectAll, deleteSelected, insertGraph,
    pointerFlow, selectionData, selectedNodes, save }
  // ---- keyboard (n8n's canvas keymap), clipboard events ----------------------
  useEffect(() => {
    const inField = (t: EventTarget | null) => t instanceof HTMLElement && (t.closest('input, textarea, select, [contenteditable], .modal, .palette, .ndv') !== null)
    const onKey = (e: KeyboardEvent) => {
      const mod = e.ctrlKey || e.metaKey
      const k = e.key.toLowerCase()
      if (mod && k === 's') { e.preventDefault(); act.current.flush(); return }  // autosaves anyway; this saves now
      if (inField(e.target) || tab !== 'editor') return
      if (e.key === ' ' && !e.repeat) setPanning(true)
      if (mod && k === 'enter') { e.preventDefault(); act.current.run(); return }
      if (mod && k === 'a') { e.preventDefault(); act.current.selectAll(); return }
      if (e.key === '1' && !mod) { act.current.refit(); return }
      if (e.key === '0' && !mod) { rf.zoomTo(1, { duration: 200 }); return }
      if (readOnly) return
      if (mod && k === 'z' && !e.shiftKey) { e.preventDefault(); act.current.undo(); return }
      if ((mod && k === 'z' && e.shiftKey) || (mod && k === 'y')) { e.preventDefault(); act.current.redo(); return }
      if (mod && k === 'd') { e.preventDefault(); act.current.duplicateSelected(); return }
      if (e.shiftKey && e.altKey && e.code === 'KeyT') { e.preventDefault(); act.current.tidy(); return }
      if (e.shiftKey && !mod && !e.altKey && k === 's') { e.preventDefault(); act.current.addSticky(); return }
      if (!mod && !e.shiftKey && !e.altKey && k === 'd') { act.current.toggleDisabled(); return }
      if (e.key === 'Tab') { e.preventDefault(); setPalette({}) }
    }
    const onKeyUp = (e: KeyboardEvent) => { if (e.key === ' ') setPanning(false) }
    const onCopy = (e: ClipboardEvent, cut = false) => {
      if (inField(e.target) || tab !== 'editor') return
      const sel = act.current.selectedNodes()
      if (!sel.length) return
      e.clipboardData?.setData('text/plain', JSON.stringify(act.current.selectionData(sel), null, 2))
      e.preventDefault()
      toast(`${cut ? 'Cut' : 'Copied'} ${sel.length} node${sel.length === 1 ? '' : 's'}`)
      if (cut && !readOnly) act.current.deleteSelected()
    }
    const onCut = (e: ClipboardEvent) => onCopy(e, true)
    const onPaste = (e: ClipboardEvent) => {
      if (inField(e.target) || readOnly || tab !== 'editor') return
      try {
        const data = JSON.parse(e.clipboardData?.getData('text/plain') ?? '')
        if (!Array.isArray(data?.nodes)) return
        e.preventDefault()
        act.current.insertGraph(data, act.current.pointerFlow())
      } catch { /* not ours */ }
    }
    const onBlur = () => setPanning(false)
    window.addEventListener('keydown', onKey)
    window.addEventListener('keyup', onKeyUp)
    window.addEventListener('blur', onBlur)
    window.addEventListener('copy', onCopy)
    window.addEventListener('cut', onCut)
    window.addEventListener('paste', onPaste)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('keyup', onKeyUp)
      window.removeEventListener('blur', onBlur)
      window.removeEventListener('copy', onCopy)
      window.removeEventListener('cut', onCut)
      window.removeEventListener('paste', onPaste)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rf, readOnly, tab])

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
    ...(!readOnly && (dirty || wf.has_unpublished_changes) ? [{ label: 'Publish', icon: 'upload', onClick: publish }] : []),
    { label: 'Version history', icon: 'history', onClick: () => setHistoryOpen(true) },
    { divider: true, label: '' },
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
        <input className="wf-name" value={wf.name} aria-label="Workflow name" title={wf.name} onChange={(e) => { setWf({ ...wf, name: e.target.value }); markDirty() }} />
        {!readOnly && (() => {
          const st = conflict || saveState === 'error' ? 'error' : dirty || saveState === 'saving' ? 'saving' : 'saved'
          const text = conflict ? 'Not saved · changed elsewhere' : st === 'error' ? "Couldn't save" : st === 'saving' ? 'Saving…' : 'All changes saved'
          return <span className={`save-state ${st}`} title={`${text} (changes save automatically)`} aria-live="polite"><span className="btn-text">{text}</span></span>
        })()}
        <span className="spacer ed-break" />
        <div className="tabs ed-tabs" role="tablist">
          <button role="tab" aria-selected={tab === 'editor'} className={`tab ${tab === 'editor' ? 'active' : ''}`} onClick={() => setTab('editor')}>Editor</button>
          <button role="tab" aria-selected={tab === 'executions'} className={`tab ${tab === 'executions' ? 'active' : ''}`} onClick={() => setTab('executions')}>Executions</button>
        </div>
        <span className="spacer" />
        {(() => {
          const changed = dirty || !!wf.has_unpublished_changes
          const label = wf.published_version ? `v${wf.published_version}` : 'Not published'
          return (
            <div className="group publish-group">
              <button className={`btn sm publish-state ${changed ? 'changed' : 'live'}`} onClick={() => setHistoryOpen(true)} aria-label={`Version history (${label})`}
                title={`${wf.published_version ? `Production runs v${wf.published_version}` : 'Never published'}${changed ? ' · the editor has unpublished changes' : ''}. Open version history`}>
                <Icon name="history" size={14} /> <span className="btn-text">{label}{changed && wf.published_version ? ' · edited' : ''}</span>
              </button>
              {!readOnly && (
                <button className="btn sm primary" onClick={publish} disabled={publishing || !changed} aria-label={publishing ? 'Publishing' : changed ? 'Publish' : 'Published'}
                  title={changed ? 'Publish these changes: production runs them from now on' : 'Everything is published'}>
                  <Icon name="upload" size={14} /> <span className="btn-text">{publishing ? 'Publishing…' : changed ? 'Publish' : 'Published'}</span>
                </button>
              )}
            </div>
          )
        })()}
        <label className={`active-pill ${wf.active ? 'on' : ''}`} title="Active workflows run the published version for webhooks, schedules and other triggers">
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
        <Menu items={menu} title="Workflow actions" className="btn icon ed-menu" />
        <input ref={importRef} type="file" accept=".json,application/json" hidden onChange={(e) => { if (e.target.files?.[0]) importFile(e.target.files[0]); e.target.value = '' }} />
      </div>
      {readOnly && <div className="readonly-banner"><Icon name="eye" size={14} /> View only: your role can't change or run this workflow.</div>}
      {conflict && (
        <div className="conflict-banner" role="alert">
          <Icon name="triangle-alert" size={16} />
          <div className="grow">
            <b>This workflow was changed in another tab or session.</b>
            <div className="small">Autosave is paused so nothing gets overwritten. Load the latest version, or keep yours and replace theirs.</div>
          </div>
          <button className="btn sm" onClick={loadLatest} title="Replace the editor with the saved version (Ctrl+Z brings your changes back)">
            <Icon name="download" size={14} /> Load latest
          </button>
          <button className="btn sm primary" onClick={keepMine} title="Save what's in this editor over the other version">
            <Icon name="save" size={14} /> Keep mine
          </button>
        </div>
      )}

      {tab === 'executions' && (
        <WorkflowExecutionsView workflowId={wf.id} execId={routeExecId} onDebug={debugExecution}
          onSelect={(eid) => navigate(eid ? `/workflow/${wf.id}/executions/${eid}` : `/workflow/${wf.id}/executions`, { replace: true })} />
      )}
      {/* The editor stays mounted on the Executions tab so unsaved changes and the canvas view survive. */}
      <div className={`ed-pane ${tab === 'editor' ? '' : 'ed-hidden'}`}>
      <div className="canvas-wrap">
        <div ref={canvasRef} className={`canvas ${panning ? 'panning' : ''}`} onDrop={onDrop} onDragOver={(e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'move' }}
          onMouseMove={(e) => { pointer.current = { x: e.clientX, y: e.clientY } }}>
          <EdgeActionsCtx.Provider value={edgeActions}>
          <ReactFlow
            nodes={viewNodes} edges={viewEdges} nodeTypes={nodeTypes} edgeTypes={edgeTypes}
            onNodesChange={(c) => { onNodesChange(c); if (c.some((x) => x.type !== 'select' && x.type !== 'dimensions')) markDirty() }}
            onEdgesChange={(c) => { onEdgesChange(c); if (c.some((x) => x.type !== 'select')) markDirty() }}
            onConnect={connect} onConnectEnd={onConnectEnd} isValidConnection={isValidConnection}
            onNodeDoubleClick={(_, n) => setOpenNode(n.id)}
            onNodeClick={(_, n) => setLogFocus(n.id)}
            deleteKeyCode={readOnly ? null : ['Delete', 'Backspace']} multiSelectionKeyCode={['Control', 'Meta', 'Shift']}
            // n8n: drag on empty canvas = selection box; middle mouse (or Space + drag) pans; wheel pans, Ctrl/Cmd + wheel zooms.
            selectionOnDrag={!panning} selectionMode={SelectionMode.Full} panOnDrag={panning ? [0, 1] : [1]} panOnScroll
            zoomActivationKeyCode={['Control', 'Meta']} panActivationKeyCode={null}
            onNodeContextMenu={(e, n) => {
              e.preventDefault()
              if (!n.selected) setNodes((ns) => ns.map((x) => ({ ...x, selected: x.id === n.id })))
              setCtxMenu({ kind: 'nodes', x: e.clientX, y: e.clientY, ids: n.selected ? selectedNodes().map((x) => x.id) : [n.id] })
            }}
            onSelectionContextMenu={(e, sel) => { e.preventDefault(); setCtxMenu({ kind: 'nodes', x: e.clientX, y: e.clientY, ids: sel.map((x) => x.id) }) }}
            onPaneContextMenu={(e) => { e.preventDefault(); setCtxMenu({ kind: 'pane', x: e.clientX, y: e.clientY, at: rf.screenToFlowPosition({ x: e.clientX, y: e.clientY }) }) }}
            onPaneClick={() => setCtxMenu(null)}
            onNodeDragStart={() => setCtxMenu(null)}
            defaultEdgeOptions={{ type: 'canvas' }} snapToGrid snapGrid={[20, 20]} minZoom={0.1} maxZoom={2}
            proOptions={{ hideAttribution: true }}
            onMoveEnd={(e) => { if (e) userMoved.current = true }}  // programmatic moves have no event
          >
            <Background variant={BackgroundVariant.Dots} gap={20} size={1.4} color="var(--canvas-dot)" />
            <Controls showInteractive={false} showFitView={false}>
              <ControlButton onClick={() => refit()} title="Fit to screen (1)"><Icon name="maximize" size={14} /></ControlButton>
              <ControlButton onClick={() => rf.zoomTo(1, { duration: 200 })} title="Reset zoom (0)"><Icon name="scan-search" size={14} /></ControlButton>
              {!readOnly && <>
                <ControlButton onClick={tidy} title="Tidy up (Shift+Alt+T)"><Icon name="wand-sparkles" size={14} /></ControlButton>
                <ControlButton onClick={undo} title="Undo (Ctrl+Z)"><Icon name="undo-2" size={14} /></ControlButton>
                <ControlButton onClick={redo} title="Redo (Ctrl+Shift+Z)"><Icon name="redo-2" size={14} /></ControlButton>
              </>}
            </Controls>
            <MiniMap pannable zoomable nodeColor={(n) => byType[(n.data as FlowNodeData).node.type]?.color ?? '#999'} />
          </ReactFlow>
          </EdgeActionsCtx.Provider>

          {!readOnly && (
            <SelectionToolbar count={nodes.filter((n) => n.selected).length} onCopy={() => copySelection()} onDuplicate={duplicateSelected}
              onTidy={tidy} onDisable={() => toggleDisabled()} onDelete={deleteSelected} />
          )}
          {ctxMenu && (
            <CanvasContextMenu target={ctxMenu} readOnly={readOnly} onClose={() => setCtxMenu(null)}
              nodeCount={nodes.length} disabledAll={ctxMenu.kind === 'nodes' && nodes.filter((n) => ctxMenu.ids.includes(n.id) && n.type !== 'sticky').every((n) => n.data.node.disabled)}
              actions={{
                open: (id) => setOpenNode(id), execute: (id) => run(id), copy: () => copySelection(), cut: () => copySelection(true),
                duplicate: duplicateSelected, toggleDisabled: (ids) => toggleDisabled(ids), remove: deleteSelected, tidy, selectAll,
                paste: (at) => pasteFromClipboard(at), addNode: (at) => { placeAt.current = at; setPalette({}) }, addSticky: (at) => { const s = byType['ui.sticky']; if (s) addNode(s, at) },
                fitView: () => refit(),
              }} />
          )}
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
        <NodeDetails key={opened.id} node={opened.data.node} run={runData[opened.id]} runsByNode={runData} workflowId={wf.id} nodeNames={nodeNames}
          onOpen={(id) => setOpenNode(id)}
          children={edges.filter((e) => e.source === opened.id && !(e.sourceHandle ?? '').startsWith('ai_')).map((e) => {
            const n = rf.getNode(e.target) as RFNode | undefined
            return { id: e.target, port: e.targetHandle ?? 'main', name: n?.data.node.name ?? e.target, type: n?.data.node.type }
          })}
          parents={edges.filter((e) => e.target === opened.id && !(e.targetHandle ?? '').startsWith('ai_')).map((e) => ({
            id: e.source, port: e.sourceHandle ?? 'main',
            name: (rf.getNode(e.source) as RFNode | undefined)?.data.node.name ?? e.source,
            type: (rf.getNode(e.source) as RFNode | undefined)?.data.node.type,
          }))}
          onChange={updateNode} onClose={() => setOpenNode(null)}
          onRun={run} />
      )}
      {opened && opened.type === 'sticky' && (
        <StickyModal node={opened.data.node} onChange={updateNode} onClose={() => setOpenNode(null)} />
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
        <MoveDialog title={`Move "${wf.name}"`} folders={folders.filter((f) => f.owner_id === wf.owner_id)} current={wf.folder_id ?? null} onClose={() => setDialog(null)}
          onPick={async (folderId) => {
            await settle()
            const saved = await save({ folder_id: folderId })
            if (!saved) return
            toast(`Moved to ${folderId ? folders.find((f) => f.id === folderId)?.name : 'Home'}`)
          }} />
      )}
      {historyOpen && <VersionHistory wf={wf} readOnly={readOnly} onClose={() => setHistoryOpen(false)}
        onPublished={adopt}
        onRestore={(v) => { setHistoryOpen(false); restoreVersion(v).catch((e) => toast(String(e), 'error')) }}
        onCompare={(pair, versions) => {
          setHistoryOpen(false)
          setCompare({ pair, versions, draft: { nodes: currentNodes(), edges: edgesFromRF(rf.getEdges()) } })
        }} />}
      {compare && (
        <Suspense fallback={null}>
          <VersionCompare wf={wf} versions={compare.versions} draft={compare.draft} initial={compare.pair} readOnly={readOnly}
            onClose={() => { setCompare(null); setHistoryOpen(true) }}
            onPublished={adopt}
            onRestore={(v) => { setCompare(null); restoreVersion(v).catch((e) => toast(String(e), 'error')) }} />
        </Suspense>
      )}
      {settingsOpen && <SettingsModal wf={wf} onClose={() => setSettingsOpen(false)} onChange={(w) => { setWf(w); markDirty() }} onDelete={async () => {
        if (!await confirmDialog({ title: `Delete "${wf.name}"?`, message: 'The workflow and all its executions are deleted. This cannot be undone.', confirmLabel: 'Delete', danger: true })) return
        try { await api.deleteWorkflow(wf.id); navigate(home) } catch (e) { toast(String(e), 'error') }
      }} />}
    </div>
  )
}

function StickyModal({ node, onChange, onClose }: { node: WFNode; onChange: (n: WFNode) => void; onClose: () => void }) {
  const ref = useDialog(onClose)
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={ref} className="modal" role="dialog" aria-modal="true" aria-label="Sticky note">
        <div className="modal-head"><h2>Sticky note</h2><button className="btn ghost icon" onClick={onClose} aria-label="Close" title="Close (Esc)"><Icon name="x" /></button></div>
        <div className="modal-body">
          <textarea className="textarea" rows={10} autoFocus value={String(node.parameters.content ?? '')}
            onChange={(e) => onChange({ ...node, parameters: { content: e.target.value } })} />
        </div>
      </div>
    </div>
  )
}

const STATE_REDUCERS = [
  { label: 'Replace: last write wins', value: 'replace' },
  { label: 'Append: add to a list', value: 'append' },
  { label: 'Add: sum numbers (counters)', value: 'add' },
  { label: 'Merge: combine objects', value: 'merge' },
  { label: 'Messages: chat history', value: 'messages' },
]

/** Fields the server owns: merged into the editor's copy without touching what is being typed. */
function serverFields(w: Workflow): Partial<Workflow> {
  return { updated_at: w.updated_at, active: w.active, folder_id: w.folder_id, published_version: w.published_version,
           published_version_id: w.published_version_id, has_unpublished_changes: w.has_unpublished_changes,
           ...(w.revision != null ? { revision: w.revision } : {}) }
}

/** n8n's workflow history: every published version, which one production runs, rollback and restore. */
function VersionHistory({ wf, readOnly, onClose, onPublished, onRestore, onCompare }: {
  wf: Workflow; readOnly: boolean; onClose: () => void; onPublished: (w: Workflow) => void; onRestore: (v: WorkflowVersion) => void
  onCompare: (pair: [string, string], versions: WorkflowVersion[]) => void
}) {
  const [versions, setVersions] = useState<WorkflowVersion[] | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const ref = useDialog(onClose)
  const load = useCallback(() => { api.workflowVersions(wf.id).then(setVersions).catch((e) => toast(String(e), 'error')) }, [wf.id])
  useEffect(load, [load])
  const goLive = async (v: WorkflowVersion) => {
    if (!await confirmDialog({ title: `Publish v${v.number}?`, confirmLabel: `Publish v${v.number}`,
      message: `Production (webhooks, schedules, listeners${wf.active ? '' : ', once activated'}) runs v${v.number} instead of v${wf.published_version}. The editor's draft is not changed.` })) return
    setBusy(v.id)
    try {
      const saved = await api.publishVersion(wf.id, v.id)
      onPublished(saved)
      toast(`v${v.number} is live`)
      load()
    } catch (e) {
      toast(String(e), 'error')
    } finally {
      setBusy(null)
    }
  }
  /** Before / after for comparing a row: older on the left. The live row compares with the draft (or the version before it). */
  const compareWith = (v: WorkflowVersion) => {
    const all = versions ?? []
    const live = all.find((x) => x.published)
    if (!live || v.id === live.id) {
      const prev = all.find((x) => x.number < v.number)
      return onCompare(wf.has_unpublished_changes || !prev ? [v.id, 'draft'] : [prev.id, v.id], all)
    }
    onCompare(v.number < live.number ? [v.id, live.id] : [live.id, v.id], all)
  }
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={ref} className="modal version-modal" role="dialog" aria-modal="true" aria-label="Version history">
        <div className="modal-head"><Icon name="history" /><h2>Version history</h2><button className="btn ghost icon" onClick={onClose} aria-label="Close" title="Close (Esc)"><Icon name="x" /></button></div>
        <div className="modal-body">
          <p className="hint">The editor works on a draft that saves automatically. <b>Publish</b> makes it a new version that production runs. Publish an older version to roll back, or restore it into the editor to keep working from it.</p>
          {wf.has_unpublished_changes && (
            <div className="version-draft">
              <Icon name="pencil" size={14} /> <span>Draft: unpublished changes in the editor</span>
              {versions?.some((v) => v.published) && (
                <button className="btn sm" onClick={() => onCompare([versions.find((v) => v.published)!.id, 'draft'], versions)}
                  title="See what the draft changes compared with what production runs"><Icon name="git-compare" size={14} /> Compare with live</button>
              )}
            </div>
          )}
          {versions === null ? <div className="hint">Loading…</div> : versions.length === 0 ? (
            <div className="hint">Nothing published yet. Use <b>Publish</b> to put this workflow live.</div>
          ) : (
            <ul className="version-list">
              {versions.map((v) => (
                <li key={v.id} className={v.published ? 'live' : ''}>
                  <div className="version-main">
                    <div className="version-title">
                      <b>v{v.number}</b>
                      {v.published && <span className={`badge ${wf.active ? 'success' : 'plain'}`}>{wf.active ? 'Live' : 'Published · inactive'}</span>}
                    </div>
                    <div className="version-meta">
                      {new Date(v.created_at).toLocaleString()}{v.author ? ` · ${v.author}` : ''} · {v.node_count} node{v.node_count === 1 ? '' : 's'}
                    </div>
                    {v.note && <div className="version-note">{v.note}</div>}
                  </div>
                  <div className="version-actions">
                    <button className="btn sm" onClick={() => compareWith(v)} title={v.published ? 'Compare the live version with the draft' : 'Compare with the live version'}>
                      <Icon name="git-compare" size={14} /> Compare
                    </button>
                  {!readOnly && (
                    <>
                      <button className="btn sm" onClick={() => onRestore(v)} title="Load this version into the editor (doesn't change production)">
                        <Icon name="rotate-ccw" size={14} /> Restore
                      </button>
                      {!v.published && (
                        <button className="btn sm primary" disabled={busy !== null} onClick={() => goLive(v)} title="Production runs this version">
                          <Icon name="upload" size={14} /> {busy === v.id ? 'Publishing…' : 'Publish'}
                        </button>
                      )}
                    </>
                  )}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  )
}

function SettingsModal({ wf, onChange, onClose, onDelete }: { wf: Workflow; onChange: (w: Workflow) => void; onClose: () => void; onDelete: () => void }) {
  const ref = useDialog(onClose)
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={ref} className="modal" role="dialog" aria-modal="true" aria-label="Workflow settings">
        <div className="modal-head"><Icon name="settings" /><h2>Workflow settings</h2><button className="btn ghost icon" onClick={onClose} aria-label="Close" title="Close (Esc)"><Icon name="x" /></button></div>
        <div className="modal-body">
          <div className="field">
            <label>Description</label>
            <textarea className="textarea" value={wf.description} onChange={(e) => onChange({ ...wf, description: e.target.value })} />
          </div>
          <PropertyField spec={{ name: 'error_workflow_id', label: 'Error workflow (runs its Error Trigger on failure)', type: 'workflow', default: null }}
            value={wf.settings.error_workflow_id} onChange={(v) => onChange({ ...wf, settings: { ...wf.settings, error_workflow_id: (v as string) || undefined } })} />
          <div className="prop-section">Execution history</div>
          {([
            ['save_success', 'Save successful production executions', [['all', 'Save'], ['none', 'Do not save']]],
            ['save_error', 'Save failed production executions', [['all', 'Save'], ['none', 'Do not save']]],
            ['save_manual', 'Save manual (editor) executions', [['all', 'Save'], ['none', 'Do not save']]],
            ['save_data', 'Execution data to keep', [['full', 'Everything (every node\'s items)'], ['summary', 'Summary only (status, timings, item counts)']]],
          ] as const).map(([key, label, opts]) => (
            <PropertyField key={key} spec={{ name: key, label, type: 'options', default: 'default',
              options: [{ label: 'Server default', value: 'default' }, ...opts.map(([value, l]) => ({ label: l, value }))] }}
              value={wf.settings[key] ?? 'default'}
              onChange={(v) => onChange({ ...wf, settings: { ...wf.settings, [key]: v === 'default' ? undefined : v } })} />
          ))}
          <div className="hint" style={{ marginTop: -8, marginBottom: 16 }}>For high-traffic workflows, save failed runs only or a summary: the history stays small and fast. Counts on the dashboard include unsaved runs. Paused runs are always kept.</div>
          <PropertyField spec={{ name: 'variables', label: 'Variables ($vars.key)', type: 'collection', default: [], fields: [
            { name: 'key', label: 'Key', type: 'string', default: '' }, { name: 'value', label: 'Value', type: 'string', default: '' }] }}
            value={wf.settings.variables ?? []} onChange={(v) => onChange({ ...wf, settings: { ...wf.settings, variables: v as { key: string; value: string }[] } })} />
          <PropertyField spec={{ name: 'state', label: 'State ($state.key)', type: 'collection', default: [],
            description: 'LangGraph graph state: shared by every node, kept across loops and Human Approval pauses. '
              + 'Each key has a reducer that decides how writes combine. Write it with the Update State node, set_state() in Code, '
              + 'or an AI Agent\'s "Conversation in state key".',
            fields: [
              { name: 'key', label: 'Key', type: 'string', default: '' },
              { name: 'reducer', label: 'Reducer', type: 'options', default: 'replace', options: STATE_REDUCERS },
              { name: 'default', label: 'Initial value (JSON)', type: 'string', default: '', placeholder: '[] · 0 · "" · {}' },
            ] }}
            value={wf.settings.state ?? []} onChange={(v) => onChange({ ...wf, settings: { ...wf.settings, state: v as StateKey[] } })} />
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
