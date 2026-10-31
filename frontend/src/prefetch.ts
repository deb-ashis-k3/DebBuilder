export const loadEditor = () => import('./pages/Editor')

let started = false
export function prefetchEditor() {
  if (started) return
  started = true
  loadEditor().catch(() => { started = false })  
}
