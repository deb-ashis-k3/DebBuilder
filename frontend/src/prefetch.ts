/**
 * The workflow editor (React Flow, node panel, executions) is its own chunk. It is fetched when the user
 * shows intent to open one (pointer over / focus on a workflow, "Create workflow", a template), not
 * during the first page load, so the first visit downloads only what it shows.
 */
export const loadEditor = () => import('./pages/Editor')

let started = false
export function prefetchEditor() {
  if (started) return
  started = true
  loadEditor().catch(() => { started = false })  // retry on the next intent if the network failed
}
