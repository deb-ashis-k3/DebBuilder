import type { NodeRun, SubRun } from '../types'

/** One row of the execution log tree (n8n's LogEntry): a node run, or a model / tool / memory call inside it. */
export interface LogEntry {
  key: string
  run: NodeRun
  sub?: SubRun
  children: LogEntry[]
  depth: number
  parent?: LogEntry
  /** LLM tokens used by this entry and everything under it. */
  tokens: { input: number; output: number; total: number }
}

const zero = () => ({ input: 0, output: 0, total: 0 })

export const runKey = (r: NodeRun, i: number) => `${r.node_id}#${r.run_index ?? 0}#${i}`

/** Sub-runs recorded before ids existed only have `depth`: rebuild parents from the call order. */
function withIds(subs: SubRun[], prefix: string): SubRun[] {
  if (subs.every((s) => s.id)) return subs
  const stack: SubRun[] = []
  return subs.map((s, k) => {
    const out = { ...s, id: s.id ?? `${prefix}-${k}` }
    while (stack.length > s.depth) stack.pop()
    out.parent = s.depth > 0 ? stack[s.depth - 1]?.id ?? null : null
    if (s.type === 'tool') stack[s.depth] = out
    return out
  })
}

export function buildLogTree(runs: NodeRun[]): LogEntry[] {
  return runs.map((run, i) => {
    const root: LogEntry = { key: runKey(run, i), run, children: [], depth: 0, tokens: zero() }
    const byId = new Map<string, LogEntry>()
    for (const sub of withIds(run.sub_runs ?? [], root.key)) {
      const parent = (sub.parent && byId.get(sub.parent)) || root
      const e: LogEntry = { key: sub.id!, run, sub, children: [], depth: parent.depth + 1, parent, tokens: zero() }
      byId.set(sub.id!, e)
      parent.children.push(e)
    }
    sumTokens(root)
    return root
  })
}

function sumTokens(e: LogEntry): LogEntry['tokens'] {
  const t = e.tokens
  if (e.sub?.tokens) {
    t.input += e.sub.tokens.input ?? 0
    t.output += e.sub.tokens.output ?? 0
    t.total += e.sub.tokens.total ?? (e.sub.tokens.input ?? 0) + (e.sub.tokens.output ?? 0)
  }
  for (const c of e.children) {
    const ct = sumTokens(c)
    t.input += ct.input; t.output += ct.output; t.total += ct.total
  }
  return t
}

/** Rows to show, depth first, skipping children of collapsed entries. */
export function visibleRows(tree: LogEntry[], collapsed: Set<string>): LogEntry[] {
  const out: LogEntry[] = []
  const walk = (e: LogEntry) => {
    out.push(e)
    if (!collapsed.has(e.key)) e.children.forEach(walk)
  }
  tree.forEach(walk)
  return out
}

export function* allEntries(tree: LogEntry[]): Generator<LogEntry> {
  for (const e of tree) {
    yield e
    yield* allEntries(e.children)
  }
}

export const entryStatus = (e: LogEntry) => e.sub?.status ?? e.run.status
export const entryNodeId = (e: LogEntry) => e.sub?.node_id ?? (e.sub ? undefined : e.run.node_id)
export const entryType = (e: LogEntry) => e.sub?.node_type ?? (e.sub ? undefined : e.run.type)

/** Sub-node activity per canvas node (status + call count), for the ✓ and count badges on sub-nodes. */
export function subNodeStats(runs: NodeRun[]): Record<string, { status: SubRun['status']; count: number; duration_ms: number }> {
  const out: Record<string, { status: SubRun['status']; count: number; duration_ms: number }> = {}
  for (const r of runs) {
    for (const s of r.sub_runs ?? []) {
      if (!s.node_id || (s.type === 'memory' && s.action === 'save')) continue
      const prev = out[s.node_id] ?? { status: 'success' as const, count: 0, duration_ms: 0 }
      const status = prev.status === 'error' || s.status === 'error' ? 'error' : prev.status === 'running' || s.status === 'running' ? 'running' : 'success'
      out[s.node_id] = { status, count: prev.count + 1, duration_ms: prev.duration_ms + (s.duration_ms ?? 0) }
    }
  }
  return out
}

/** Live update: add or replace a sub-run (by id) on the node's running record. */
export function upsertSubRun(runs: NodeRun[], nodeId: string, sub: SubRun): NodeRun[] {
  const i = runs.findLastIndex((r) => r.node_id === nodeId)
  if (i < 0) return runs
  const r = runs[i]
  const subs = r.sub_runs ?? []
  const k = subs.findIndex((s) => s.id === sub.id)
  const next = k < 0 ? [...subs, sub] : subs.map((s, j) => (j === k ? sub : s))
  return runs.map((x, j) => (j === i ? { ...r, sub_runs: next } : x))
}

export function fmtTokens(n: number): string {
  return n.toLocaleString()
}
