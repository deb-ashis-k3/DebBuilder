import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'
import { auth } from './api'
import type { Role, User } from './types'

/** Mirror of backend/app/security.py PERMISSIONS (the server enforces; this only shapes the UI). */
const VIEW = ['workflow:read', 'execution:read', 'credential:read']
const EDIT = [...VIEW, 'workflow:write', 'workflow:execute', 'execution:write', 'credential:write']
const ADMIN = [...EDIT, 'user:manage', 'audit:read']
const PERMS: Record<Role, string[]> = { viewer: VIEW, editor: EDIT, admin: ADMIN, owner: [...ADMIN, 'owner'] }

export type Perm = 'workflow:read' | 'workflow:write' | 'workflow:execute' | 'execution:read' | 'execution:write'
  | 'credential:read' | 'credential:write' | 'user:manage' | 'audit:read' | 'owner'

interface AuthState {
  loading: boolean
  setupRequired: boolean
  user: User | null
  refresh: () => Promise<void>
  setUser: (u: User | null) => void
  logout: () => Promise<void>
  can: (p: Perm) => boolean
}

const Ctx = createContext<AuthState>({
  loading: true, setupRequired: false, user: null, refresh: async () => {}, setUser: () => {}, logout: async () => {}, can: () => false,
})

export function AuthProvider({ children }: { children: ReactNode }) {
  const [loading, setLoading] = useState(true)
  const [setupRequired, setSetupRequired] = useState(false)
  const [user, setUser] = useState<User | null>(null)

  const refresh = useCallback(async () => {
    try {
      const s = await auth.status()
      setSetupRequired(s.setup_required)
      setUser(s.user)
    } catch {
      setUser(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refresh()
    // Any API call that comes back 401 (session expired / revoked) re-checks and shows sign-in.
    const onExpired = () => { refresh() }
    window.addEventListener('auth:expired', onExpired)
    return () => window.removeEventListener('auth:expired', onExpired)
  }, [refresh])

  const logout = useCallback(async () => {
    try { await auth.logout() } finally { setUser(null) }
  }, [])

  const can = useCallback((p: Perm) => !!user && (PERMS[user.role] ?? []).includes(p), [user])

  return <Ctx.Provider value={{ loading, setupRequired, user, refresh, setUser, logout, can }}>{children}</Ctx.Provider>
}

export const useAuth = () => useContext(Ctx)

export const ROLE_LABEL: Record<Role, string> = { owner: 'Owner', admin: 'Admin', editor: 'Editor', viewer: 'Viewer' }
export const ROLE_HELP: Record<Role, string> = {
  owner: 'Everything, including managing admins and other owners',
  admin: 'Manage users and see the audit log, plus everything an editor can do',
  editor: 'Create, edit and run workflows; manage credentials',
  viewer: 'Read-only access to workflows and executions',
}
