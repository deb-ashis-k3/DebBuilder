import { BaseEdge, EdgeLabelRenderer, Position, getBezierPath, getSmoothStepPath, type EdgeProps } from '@xyflow/react'
import { createContext, useContext, useRef, useState } from 'react'
import { Icon } from '../lib'

export interface EdgeActions {
  insert: (edgeId: string, at: { x: number; y: number }) => void
  remove: (edgeId: string) => void
}
/** Provided by the editor; read-only canvases (execution preview) leave it null. */
export const EdgeActionsCtx = createContext<EdgeActions | null>(null)

const ARROW = 9 // arrowhead length; the path stops short of the handle so the tip lands on it
const LOOP_BELOW = 120 // how far a backward connection dips under the nodes (n8n style)

/**
 * n8n-like connection: bezier curve going forward; when the target sits behind the source the
 * line leaves to the right, drops below both nodes, runs back and enters the target from the left.
 */
function edgePath(p: EdgeProps): [string, number, number] {
  const { sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition } = p
  const horizontal = sourcePosition === Position.Right && targetPosition === Position.Left
  const tx = horizontal ? targetX - ARROW : targetX
  if (horizontal && targetX < sourceX) {
    const midX = (sourceX + tx) / 2
    const midY = Math.max(sourceY, targetY) + LOOP_BELOW
    const [a] = getSmoothStepPath({ sourceX, sourceY, sourcePosition, targetX: midX, targetY: midY, targetPosition: Position.Right, borderRadius: 14, offset: 24 })
    const [b] = getSmoothStepPath({ sourceX: midX, sourceY: midY, sourcePosition: Position.Left, targetX: tx, targetY, targetPosition, borderRadius: 14, offset: 24 })
    return [`${a} ${b}`, midX, midY]
  }
  const [d, lx, ly] = getBezierPath({ sourceX, sourceY, sourcePosition, targetX: tx, targetY, targetPosition, curvature: 0.35 })
  return [d, lx, ly]
}

export function CanvasEdge(props: EdgeProps) {
  const { id, targetX, targetY, targetPosition, sourcePosition, label, selected } = props
  const actions = useContext(EdgeActionsCtx)
  const [hover, setHover] = useState(false)
  const leave = useRef<number | undefined>(undefined)
  const [d, lx, ly] = edgePath(props)
  const ai = props.targetHandleId?.startsWith('ai_')
  const showArrow = !ai && sourcePosition === Position.Right && targetPosition === Position.Left

  const enter = () => { window.clearTimeout(leave.current); setHover(true) }
  const exit = () => { leave.current = window.setTimeout(() => setHover(false), 120) }
  const active = hover || selected

  return (
    <>
      <BaseEdge id={id} path={d} className={active ? 'edge-active' : undefined} interactionWidth={0} />
      {showArrow && <path className={`edge-arrow ${active ? 'edge-active' : ''}`} d={`M ${targetX - ARROW - 1} ${targetY - 5} L ${targetX} ${targetY} L ${targetX - ARROW - 1} ${targetY + 5} Z`} />}
      <path d={d} fill="none" stroke="transparent" strokeWidth={22} className="edge-hit" onMouseEnter={enter} onMouseLeave={exit} />
      <EdgeLabelRenderer>
        <div className="edge-label-wrap nodrag nopan" style={{ transform: `translate(-50%, -50%) translate(${lx}px, ${ly}px)` }}
          onMouseEnter={enter} onMouseLeave={exit}>
          {actions && active && (
            <div className="edge-toolbar">
              <button title="Add node here" onClick={() => actions.insert(id, { x: lx, y: ly })}><Icon name="plus" size={14} /></button>
              <button title="Delete connection" onClick={() => actions.remove(id)}><Icon name="trash-2" size={13} /></button>
            </div>
          )}
          {label != null && label !== '' && <span className={`edge-count ${actions && active ? 'below' : ''}`}>{label}</span>}
        </div>
      </EdgeLabelRenderer>
    </>
  )
}

export const edgeTypes = { canvas: CanvasEdge }
