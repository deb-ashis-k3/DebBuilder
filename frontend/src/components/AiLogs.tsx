import { useMemo, useState } from 'react'
import { Icon, fmtDuration, useCatalog } from '../lib'
import type { NodeRun, SubRun } from '../types'
import { SubRunData } from './ExecutionLog'
import { buildLogTree, fmtTokens, type LogEntry } from './logTree'

const SUB_ICON: Record<SubRun['type'], string> = { llm: 'brain', tool: 'wrench', memory: 'database', retriever: 'search', vector_store: 'database-zap' }

function Row({ e, first }: { e: LogEntry; first: boolean }) {
  const { byType } = useCatalog()
  const [open, setOpen] = useState(first)
  const s = e.sub!
  const nt = byType[s.node_type ?? '']
  return (
    <>
      <div className={`ai-run ${s.status}`} style={{ marginLeft: (e.depth - 1) * 18 }}>
        <div className="ai-run-head" onClick={() => setOpen(!open)}>
          <span className="json-toggle">{open ? '▾' : '▸'}</span>
          <span className="exec-ico" style={{ '--nc': nt?.color ?? '#8b5cf6' } as React.CSSProperties}><Icon name={nt?.icon ?? SUB_ICON[s.type]} size={13} /></span>
          <b className="grow">{s.name}{s.type === 'memory' && s.action ? <span className="muted"> · {s.action}</span> : null}</b>
          {e.tokens.total > 0 && <span className="badge plain" title={`Prompt ${fmtTokens(e.tokens.input)} · Completion ${fmtTokens(e.tokens.output)}`}>{fmtTokens(e.tokens.total)} tok</span>}
          <span className="muted small">{fmtDuration(s.duration_ms)}</span>
          <Icon name={s.status === 'success' ? 'circle-check' : s.status === 'error' ? 'circle-alert' : 'loader-circle'} size={14}
            className={s.status === 'running' ? 'spin' : ''} color={s.status === 'error' ? 'var(--danger)' : 'var(--success)'} />
        </div>
        {open && (
          <div className="ai-run-body">
            {s.error && <div className="error-box" style={{ marginBottom: 8 }}>{s.error}</div>}
            <div className="ai-sec">Input</div>
            <SubRunData s={s} which="input" />
            <div className="ai-sec">Output</div>
            <SubRunData s={s} which="output" />
          </div>
        )}
      </div>
      {e.children.map((c) => <Row key={c.key} e={c} first={false} />)}
    </>
  )
}

/** The model / tool / memory calls of one AI node run, as a tree (same data as the execution log). */
export function AiLogs({ runs }: { runs: SubRun[] }) {
  const root = useMemo(() => buildLogTree([{ node_id: '', name: '', type: '', status: 'success', started_at: '', sub_runs: runs } as NodeRun])[0], [runs])
  const llm = runs.filter((r) => r.type === 'llm').length
  const tools = runs.filter((r) => r.type === 'tool').length
  return (
    <div>
      <div className="ai-summary">
        <span><Icon name="brain" size={13} /> {llm} LLM call{llm === 1 ? '' : 's'}</span>
        <span><Icon name="wrench" size={13} /> {tools} tool call{tools === 1 ? '' : 's'}</span>
        {root.tokens.total > 0 && <span><Icon name="coins" size={13} /> {fmtTokens(root.tokens.total)} tokens</span>}
      </div>
      {root.children.map((e, i) => <Row key={e.key} e={e} first={i === 0} />)}
    </div>
  )
}
