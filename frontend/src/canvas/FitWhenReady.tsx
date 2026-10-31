import { useReactFlow, useStore, type FitViewOptions, type ReactFlowState } from '@xyflow/react'
import { useEffect, useRef } from 'react'

/** Every node measured (size + handles). React Flow's own `nodesInitialized` only updates when the nodes prop
 *  changes, which a read-only canvas (no onNodesChange) never does after the first render. */
const allMeasured = (s: ReactFlowState) => {
  if (!s.nodeLookup.size) return false
  for (const n of s.nodeLookup.values()) {
    if (n.hidden) continue
    if (!n.measured?.width || !n.measured?.height || n.internals.handleBounds === undefined) return false
  }
  return true
}

/** Child of <ReactFlow>: fit the view once every node has been measured and the canvas has a size.
 *  React Flow's `fitView` prop can run before that (a hidden or still-laying-out canvas, unmeasured nodes),
 *  which leaves the graph cropped and puts NaN into the SVG. Re-fits when `fitKey` changes (another graph). */
export function FitWhenReady({ options = { padding: 0.2, maxZoom: 1 }, fitKey }: { options?: FitViewOptions; fitKey?: unknown }) {
  const rf = useReactFlow()
  const ready = useStore(allMeasured)
  const sized = useStore((s) => s.width > 0 && s.height > 0)
  const done = useRef<unknown>(Symbol('unfitted'))
  const opts = useRef(options)
  opts.current = options
  useEffect(() => {
    if (!ready || !sized || done.current === fitKey) return
    // One frame later: the edges and handles of the measured nodes are in place too.
    const raf = requestAnimationFrame(() => { done.current = fitKey; rf.fitView(opts.current) })
    return () => cancelAnimationFrame(raf)
  }, [ready, sized, fitKey, rf])
  return null
}
