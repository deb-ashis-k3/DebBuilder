import dagre from '@dagrejs/dagre'

/**
 * "Tidy up" auto-layout, ported from n8n (editor-ui/features/workflows/canvas/composables/useCanvasLayout.ts):
 *
 * 1. Split the graph into connected components.
 * 2. In each component, every AI node and its sub-nodes (model, memory, tools, and their own sub-nodes)
 *    are laid out top-to-bottom first; that block then counts as one box in the component.
 * 3. Each component is laid out left-to-right with dagre.
 * 4. Components are stacked top-to-bottom (kept in their original vertical order).
 * 5. The result is anchored at the original top-left, and nodes snap to the grid by their
 *    connection handle so a row of nodes stays on one line.
 * Sticky notes that covered nodes are resized/moved to keep covering them.
 */

export const GRID = 20
const NODE_X_SPACING = GRID * 8
const NODE_Y_SPACING = GRID * 6
const SUBGRAPH_SPACING = GRID * 8
const AI_X_SPACING = GRID * 3
const AI_Y_SPACING = GRID * 8

export interface LayoutNode { id: string; x: number; y: number; width: number; height: number; sticky?: boolean }
export interface LayoutEdge { source: string; target: string; ai: boolean }
export interface Box { x: number; y: number; width: number; height: number }
export type LayoutResult = Record<string, { x: number; y: number; width?: number; height?: number }>

const snap = (v: number) => Math.round(v / GRID) * GRID
const LAYOUT_OPTS = { disableOptimalOrderHeuristic: true } as dagre.configUnion

function bbox(boxes: Box[]): Box {
  const minX = Math.min(...boxes.map((b) => b.x)), minY = Math.min(...boxes.map((b) => b.y))
  const maxX = Math.max(...boxes.map((b) => b.x + b.width)), maxY = Math.max(...boxes.map((b) => b.y + b.height))
  return { x: minX, y: minY, width: maxX - minX, height: maxY - minY }
}

function graphBox(g: dagre.graphlib.Graph): Box {
  return bbox(g.nodes().map((id) => { const n = g.node(id); return { x: n.x - n.width / 2, y: n.y - n.height / 2, width: n.width, height: n.height } }))
}

const covers = (outer: Box, inner: Box) =>
  inner.x >= outer.x && inner.y >= outer.y && inner.x + inner.width <= outer.x + outer.width && inner.y + inner.height <= outer.y + outer.height

const intersects = (a: Box, b: Box, margin = 0) =>
  a.x < b.x + b.width + margin && a.x + a.width + margin > b.x && a.y < b.y + b.height + margin && a.y + a.height + margin > b.y

export function tidyUp(allNodes: LayoutNode[], allEdges: LayoutEdge[]): LayoutResult {
  const nodes = allNodes.filter((n) => !n.sticky)
  if (!nodes.length) return {}
  const ids = new Set(nodes.map((n) => n.id))
  const byId = new Map(nodes.map((n) => [n.id, n]))
  const edges = allEdges.filter((e) => ids.has(e.source) && ids.has(e.target))
  const before = bbox(nodes)

  // Parent graph: nodes in reading order so dagre keeps the existing vertical order of branches.
  const parent = new dagre.graphlib.Graph()
  parent.setDefaultEdgeLabel(() => ({}))
  for (const n of [...nodes].sort((a, b) => a.y - b.y || a.x - b.x)) parent.setNode(n.id, { width: n.width, height: n.height })
  for (const e of [...edges].sort((a, b) => (byId.get(a.target)!.y - byId.get(b.target)!.y))) parent.setEdge(e.source, e.target)

  // AI sub-nodes: sources of ai_* edges, recursively (embedding → vector store → tool → agent).
  const aiInto = new Map<string, string[]>()
  for (const e of edges.filter((x) => x.ai)) aiInto.set(e.target, [...(aiInto.get(e.target) ?? []), e.source])
  const isSubNode = new Set(edges.filter((e) => e.ai).map((e) => e.source))
  const configOf = (root: string) => {
    const out: string[] = []
    const stack = [...(aiInto.get(root) ?? [])]
    while (stack.length) {
      const id = stack.pop()!
      if (out.includes(id)) continue
      out.push(id)
      stack.push(...(aiInto.get(id) ?? []))
    }
    return out
  }

  const components = (dagre.graphlib.alg.components(parent) as string[][])
    .map((compIds) => compIds.sort())
    .sort((a, b) => Math.min(...a.map((id) => byId.get(id)!.y)) - Math.min(...b.map((id) => byId.get(id)!.y)))

  const laidOut = components.map((compIds) => {
    const set = new Set(compIds)
    const sub = new dagre.graphlib.Graph()
    sub.setGraph({ rankdir: 'LR', edgesep: NODE_Y_SPACING, nodesep: NODE_Y_SPACING, ranksep: NODE_X_SPACING })
    sub.setDefaultEdgeLabel(() => ({}))
    for (const id of parent.nodes().filter((x) => set.has(x))) sub.setNode(id, { ...parent.node(id) })
    for (const e of parent.edges().filter((x) => set.has(x.v) && set.has(x.w))) sub.setEdge(e.v, e.w)

    const aiRoots = compIds.filter((id) => aiInto.has(id) && !isSubNode.has(id))
    const aiGraphs = aiRoots.map((root) => {
      const configs = configOf(root)
      const all = new Set([root, ...configs])
      const g = new dagre.graphlib.Graph()
      g.setGraph({ rankdir: 'TB', edgesep: AI_X_SPACING, nodesep: AI_X_SPACING, ranksep: AI_Y_SPACING })
      g.setDefaultEdgeLabel(() => ({}))
      for (const id of [root, ...configs.sort((a, b) => byId.get(a)!.x - byId.get(b)!.x)]) g.setNode(id, { ...parent.node(id) })
      for (const e of edges.filter((x) => x.ai && all.has(x.source) && all.has(x.target))) g.setEdge(e.target, e.source) // parent above its sub-nodes
      const rootFlowEdges = sub.edges().filter((x) => x.v === root || x.w === root)
      configs.forEach((id) => { if (sub.hasNode(id)) sub.removeNode(id) })
      dagre.layout(g, LAYOUT_OPTS)
      const box = graphBox(g)
      sub.setNode(root, { width: box.width, height: box.height })
      rootFlowEdges.forEach((x) => { if (sub.hasNode(x.v) && sub.hasNode(x.w)) sub.setEdge(x.v, x.w) })
      return { root, graph: g, box }
    })
    dagre.layout(sub, LAYOUT_OPTS)
    return { graph: sub, aiGraphs, box: graphBox(sub) }
  })

  // Stack the components vertically.
  const stack = new dagre.graphlib.Graph()
  stack.setGraph({ rankdir: 'TB', align: 'UL', edgesep: SUBGRAPH_SPACING, nodesep: SUBGRAPH_SPACING, ranksep: SUBGRAPH_SPACING })
  stack.setDefaultEdgeLabel(() => ({}))
  laidOut.forEach(({ box }, i) => stack.setNode(String(i), { width: box.width, height: box.height }))
  laidOut.forEach((_, i) => { if (i + 1 < laidOut.length) stack.setEdge(String(i), String(i + 1)) })
  dagre.layout(stack, LAYOUT_OPTS)

  const boxes: Record<string, Box> = {}
  laidOut.forEach(({ graph, aiGraphs, box }, i) => {
    const s = stack.node(String(i))
    const off = { x: -box.x, y: s.y - s.height / 2 - box.y }
    for (const id of graph.nodes()) {
      const n = graph.node(id)
      const placed = { x: n.x - n.width / 2 + off.x, y: n.y - n.height / 2 + off.y, width: n.width, height: n.height }
      const ai = aiGraphs.find((a) => a.root === id)
      if (!ai) { boxes[id] = placed; continue }
      for (const aid of ai.graph.nodes()) {
        const an = ai.graph.node(aid)
        boxes[aid] = { x: an.x - an.width / 2 - ai.box.x + placed.x, y: an.y - an.height / 2 - ai.box.y + placed.y, width: an.width, height: an.height }
      }
    }
  })

  // An AI block is centred on its row; move it down so the AI node itself sits on the row, unless that would overlap.
  for (const { root, graph } of laidOut.flatMap((c) => c.aiGraphs)) {
    const members = graph.nodes()
    const block = bbox(members.map((id) => boxes[id]))
    const shift = block.height / 2 - byId.get(root)!.height / 2
    const moved = { ...block, y: block.y + shift }
    const clash = Object.entries(boxes).some(([id, b]) => !graph.hasNode(id) && intersects(moved, b, NODE_Y_SPACING))
    if (!clash) members.forEach((id) => { boxes[id] = { ...boxes[id], y: boxes[id].y + shift } })
  }

  // Keep the selection where it was (top-left anchor) and snap each node by its handle (vertical middle).
  const after = bbox(Object.values(boxes))
  const anchor = { x: after.x - before.x, y: after.y - before.y }
  const result: LayoutResult = {}
  for (const [id, b] of Object.entries(boxes)) {
    result[id] = { x: snap(b.x - anchor.x + b.width / 2) - b.width / 2, y: snap(b.y - anchor.y + b.height / 2) - b.height / 2 }
  }

  // Stickies that covered nodes keep covering the same nodes (with the same padding).
  for (const s of allNodes.filter((n) => n.sticky)) {
    const covered = nodes.filter((n) => covers(s, n))
    if (!covered.length) continue
    const was = bbox(covered)
    const now = bbox(covered.map((n) => ({ ...result[n.id], width: n.width, height: n.height })))
    const pad = { l: was.x - s.x, t: was.y - s.y, r: s.x + s.width - (was.x + was.width), b: s.y + s.height - (was.y + was.height) }
    result[s.id] = { x: snap(now.x - pad.l), y: snap(now.y - pad.t), width: snap(now.width + pad.l + pad.r), height: snap(now.height + pad.t + pad.b) }
  }
  return result
}
