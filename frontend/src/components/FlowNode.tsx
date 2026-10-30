import { Handle, NodeResizer, Position, useUpdateNodeInternals, type NodeProps } from '@xyflow/react'
import { memo, useEffect } from 'react'
import { Icon, fmtDuration, inputPorts, outputPorts, useCatalog } from '../lib'
import type { RunState, WFNode } from '../types'

export interface FlowNodeData extends Record<string, unknown> {
  node: WFNode
  run?: RunState
  /** Sub-nodes: calls their AI node made through them in the last execution. */
  sub?: { status: 'running' | 'success' | 'error'; count: number; duration_ms: number }
}

const pct = (i: number, n: number) => `${((i + 1) / (n + 1)) * 100}%`

function FlowNodeImpl({ id, data, selected }: NodeProps & { data: FlowNodeData }) {
  const { byType } = useCatalog()
  const node = data.node
  const spec = byType[node.type]
  // Handles appear once the catalog loads (and change with dynamic outputs). React Flow caches
  // handle positions at first measure, so without this edges can stay hidden until a resize.
  const updateInternals = useUpdateNodeInternals()
  const handleKey = spec ? `${inputPorts(spec).map((p) => p.id)}|${outputPorts(spec, node).map((p) => p.id)}|${spec.ai_inputs.length}` : ''
  useEffect(() => { updateInternals(id) }, [id, handleKey, updateInternals])
  if (!spec) return <div className="fnode-box">?</div>

  const isSub = !!spec.provides
  const wide = spec.ai_inputs.length > 0
  const ins = inputPorts(spec)
  const outs = outputPorts(spec, node)
  const st = data.run?.status ?? data.sub?.status
  const cls = ['fnode', spec.trigger && 'trigger', isSub && 'sub', wide && 'wide', selected && 'selected',
    node.disabled && 'disabled', st && `st-${st}`].filter(Boolean).join(' ')
  const counts = data.run?.counts

  return (
    <div className={cls} title={spec.description} style={{ '--nc': spec.color } as React.CSSProperties}>
      <div className="fnode-box">
        {spec.trigger && <Icon name="zap" size={16} className="fnode-bolt" fill="currentColor" />}
        <div className="icon-tile"><Icon name={spec.icon} size={wide ? 26 : 26} strokeWidth={2} /></div>
        {wide && <div className="inner-label">{node.name}<div className="fnode-sub">{spec.label}</div></div>}

        {ins.map((p, i) => (
          <Handle key={p.id} id={p.id} type="target" position={Position.Left} style={{ top: pct(i, ins.length) }}>
            {p.label && <span className="port-label in" style={{ top: '50%' }}>{p.label}</span>}
          </Handle>
        ))}
        {outs.map((p, i) => (
          <Handle key={p.id} id={p.id} type="source" position={Position.Right} style={{ top: pct(i, outs.length) }}>
            {p.label && (
              <span className="port-label out" style={{ top: '50%' }}>
                {p.label}{counts?.[p.id] ? ` (${counts[p.id]})` : ''}
              </span>
            )}
          </Handle>
        ))}
        {spec.ai_inputs.map((a, i) => (
          <Handle key={a.name} id={a.name} type="target" position={Position.Bottom}
            className={`ai ${a.required ? 'required' : ''}`} style={{ left: pct(i, spec.ai_inputs.length) }} />
        ))}
        {/* Labels live outside the (rotated) diamond handles so they stay upright. */}
        {spec.ai_inputs.map((a, i) => (
          <span key={`l-${a.name}`} className="port-label ai" style={{ left: pct(i, spec.ai_inputs.length) }}>
            {a.label}{a.required ? '*' : ''}
          </span>
        ))}
        {isSub && <Handle id={spec.provides!} type="source" position={Position.Top} className="ai ai-top" />}

        {node.pin_data != null && <div className="fnode-pin" title="Output is pinned"><Icon name="pin" size={11} strokeWidth={2.6} /></div>}
        {st === 'success' && <div className={`fnode-status success ${data.sub && data.sub.count > 1 ? 'count' : ''}`}>
          <Icon name="check" size={13} strokeWidth={3} />{data.sub && data.sub.count > 1 && <span>{data.sub.count}</span>}
        </div>}
        {st === 'error' && <div className="fnode-status error"><Icon name="x" size={13} strokeWidth={3} /></div>}
        {st === 'waiting' && <div className="fnode-status waiting"><Icon name="pause" size={12} strokeWidth={3} /></div>}
      </div>
      {!wide && <div className="fnode-label">{node.name}</div>}
      {node.disabled && <div className="fnode-sub">(disabled)</div>}
      {data.run && data.run.status !== 'running' && (
        <div className={`fnode-time ${data.run.status}`}>
          <Icon name="timer" size={11} />{fmtDuration(data.run.duration_ms)}
          {data.run.runs.length > 1 && <span> · {data.run.runs.length} runs</span>}
        </div>
      )}
    </div>
  )
}

function StickyImpl({ data, selected }: NodeProps & { data: FlowNodeData }) {
  return (
    <>
      <NodeResizer isVisible={!!selected} minWidth={160} minHeight={80} />
      <div className="sticky">{String(data.node.parameters?.content ?? '')}</div>
    </>
  )
}

export const FlowNode = memo(FlowNodeImpl)
export const StickyNode = memo(StickyImpl)
export const nodeTypes = { flow: FlowNode, sticky: StickyNode }
