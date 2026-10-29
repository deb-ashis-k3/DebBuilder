import { useState } from 'react'
import { Icon, fmtDuration } from '../lib'
import type { SubRun } from '../types'

function SubRunRow({ s, i }: { s: SubRun; i: number }) {
  const [open, setOpen] = useState(i === 0)
  const msgs = Array.isArray(s.input) ? s.input : null
  const out = typeof s.output === 'object' && s.output ? s.output : null
  return (
    <div className={`ai-run ${s.status}`} style={{ marginLeft: s.depth * 18 }}>
      <div className="ai-run-head" onClick={() => setOpen(!open)}>
        <span className="json-toggle">{open ? '▾' : '▸'}</span>
        <span className={`ai-kind ${s.type}`}><Icon name={s.type === 'llm' ? 'brain' : 'wrench'} size={13} /></span>
        <b className="grow">{s.type === 'llm' ? s.name : `Tool · ${s.name}`}</b>
        {s.tokens?.total != null && <span className="badge plain" title={`in ${s.tokens.input ?? '?'} · out ${s.tokens.output ?? '?'}`}>{s.tokens.total} tok</span>}
        <span className="muted small">{fmtDuration(s.duration_ms)}</span>
        <Icon name={s.status === 'success' ? 'circle-check' : s.status === 'error' ? 'circle-alert' : 'loader-circle'} size={14}
          className={s.status === 'running' ? 'spin' : ''} color={s.status === 'error' ? 'var(--danger)' : 'var(--success)'} />
      </div>
      {open && (
        <div className="ai-run-body">
          <div className="ai-sec">Input</div>
          {msgs ? msgs.map((m, k) => (
            <div key={k} className="ai-msg"><span className={`role ${m.role}`}>{m.role}</span><pre>{m.content}</pre></div>
          )) : <pre className="ai-pre">{String(s.input ?? '')}</pre>}
          <div className="ai-sec">Output</div>
          {s.error ? <div className="error-box">{s.error}</div> : out ? (
            <>
              {out.text && <pre className="ai-pre">{out.text}</pre>}
              {out.tool_calls?.map((t, k) => (
                <div key={k} className="ai-msg"><span className="role tool">calls {t.name}</span><pre>{JSON.stringify(t.args, null, 2)}</pre></div>
              ))}
              {!out.text && !out.tool_calls?.length && <span className="muted small">(empty)</span>}
            </>
          ) : <pre className="ai-pre">{String(s.output ?? '')}</pre>}
        </div>
      )}
    </div>
  )
}

export function AiLogs({ runs }: { runs: SubRun[] }) {
  const llm = runs.filter((r) => r.type === 'llm')
  const tokens = llm.reduce((a, r) => a + (r.tokens?.total ?? 0), 0)
  return (
    <div>
      <div className="ai-summary">
        <span><Icon name="brain" size={13} /> {llm.length} LLM call{llm.length === 1 ? '' : 's'}</span>
        <span><Icon name="wrench" size={13} /> {runs.length - llm.length} tool call{runs.length - llm.length === 1 ? '' : 's'}</span>
        {tokens > 0 && <span><Icon name="coins" size={13} /> {tokens.toLocaleString()} tokens</span>}
      </div>
      {runs.map((s, i) => <SubRunRow key={i} s={s} i={i} />)}
    </div>
  )
}
