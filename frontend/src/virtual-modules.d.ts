declare module 'virtual:used-icons' {
  import type { ComponentType } from 'react'
  import type { LucideProps } from 'lucide-react'
  /** Lucide icons by PascalCase name: every icon named in the frontend or the backend's node catalog (see vite.config.ts). */
  const icons: Record<string, ComponentType<LucideProps>>
  export default icons
}
