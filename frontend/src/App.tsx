import { Suspense, lazy, useEffect, useRef, useState } from 'react'
import { BrowserRouter, NavLink, Route, Routes, useLocation } from 'react-router-dom'
import { AuthProvider, ROLE_LABEL, useAuth } from './auth'
import { ConfirmHost } from './components/Dialogs'
import { CatalogProvider, Icon, Toaster } from './lib'
import { ForcePasswordChange, LoginScreen } from './pages/AuthScreens'
import Workflows from './pages/Workflows'
import { loadEditor } from './prefetch'


const Editor = lazy(loadEditor)
const Credentials = lazy(() => import('./pages/Credentials'))
const Phone = lazy(() => import('./pages/Phone'))
const Settings = lazy(() => import('./pages/Settings'))
const ExecutionList = lazy(() => import('./pages/Executions').then((m) => ({ default: m.ExecutionList })))
const ExecutionDetail = lazy(() => import('./pages/Executions').then((m) => ({ default: m.ExecutionDetail })))

// Themes in click order. '' follows the system's light / dark setting (Slate palette).
const THEMES = [
  { id: '', label: 'System', icon: 'monitor' },
  { id: 'dark', label: 'Dark', icon: 'moon' },
  { id: 'light', label: 'Light', icon: 'sun' },
  { id: 'aqua', label: 'Aqua', icon: 'droplets' },
  { id: 'rose', label: 'Soft Rose', icon: 'flower-2' },
] as const
export const THEME_KEY = 'debbuilder-ui-theme'  // index.html applies it before the first paint (no flash)

function ThemeToggle({ collapsed }: { collapsed: boolean }) {
  const [theme, setTheme] = useState<string>(() => localStorageGet(THEME_KEY) ?? '')
  useEffect(() => {
    if (theme) document.documentElement.dataset.theme = theme
    else delete document.documentElement.dataset.theme
    localStorageSet(THEME_KEY, theme)
  }, [theme])
  const i = Math.max(0, THEMES.findIndex((t) => t.id === theme))
  const cur = THEMES[i], next = THEMES[(i + 1) % THEMES.length]
  const label = `Theme: ${cur.label}`
  return (
    <button className="nav-link theme-toggle" onClick={() => setTheme(next.id)}
      title={collapsed ? `${label} (click: ${next.label})` : `Switch theme (next: ${next.label})`}>
      <Icon name={cur.icon} /> <span className="label">{label}</span>
    </button>
  )
}

/** Bump together with API_VERSION in backend/app/main.py when the editor needs new endpoints. */
const REQUIRED_API_VERSION = 4

/** An API server started before an update silently 404s/405s new endpoints; say so instead. */
function ApiVersionBanner() {
  const [stale, setStale] = useState<number | null>(null)
  useEffect(() => {
    fetch('/api/health').then((r) => r.json()).then((h) => {
      const v = Number(h?.api_version ?? 0)
      if (v < REQUIRED_API_VERSION) setStale(v)
    }).catch(() => {})
  }, [])
  if (stale === null) return null
  return (
    <div className="api-banner" role="alert">
      <Icon name="triangle-alert" size={16} />
      <span>The API server is running an older version{stale ? ` (v${stale})` : ''}, so some actions (folders, batch delete, …) will fail.
        Restart it: <code>cd backend</code> then <code>uv run python -m app</code>.</span>
      <button className="btn ghost sm icon" onClick={() => setStale(null)} title="Dismiss"><Icon name="x" size={14} /></button>
    </div>
  )
}

function localStorageGet(k: string) {
  try { return localStorage.getItem(k) } catch { return null }
}
function localStorageSet(k: string, v: string) {
  try { localStorage.setItem(k, v) } catch { }
}

function useMedia(query: string) {
  const [match, setMatch] = useState(() => window.matchMedia(query).matches)
  useEffect(() => {
    const mq = window.matchMedia(query)
    const on = () => setMatch(mq.matches)
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [query])
  return match
}

function Shell({ children, compact = false }: { children: React.ReactNode; compact?: boolean }) {
  
  
  const narrow = useMedia('(max-width: 1100px)')
  const rail = narrow || compact
  const { pathname } = useLocation()
  const [peek, setPeek] = useState<'hover' | 'tap' | null>(null)
  const hoverTimer = useRef<number | undefined>(undefined)
  useEffect(() => { setPeek(null) }, [pathname])
  useEffect(() => () => window.clearTimeout(hoverTimer.current), [])
  const collapsed = rail && !peek
  const onEnter = () => {
    if (!rail || peek) return
    
    hoverTimer.current = window.setTimeout(() => setPeek('hover'), 220)
  }
  const onLeave = () => {
    window.clearTimeout(hoverTimer.current)
    setPeek((p) => (p === 'hover' ? null : p))
  }
  const link = (to: string, icon: string, label: string, match?: (p: string) => boolean) => (
    <NavLink to={to} end={!match} className={({ isActive }) => `nav-link ${isActive || match?.(pathname) ? 'active' : ''}`}
      title={collapsed ? label : undefined} onClick={() => setPeek(null)}>
      <Icon name={icon} /> <span className="label">{label}</span>
    </NavLink>
  )
  return (
    <div className={`app ${collapsed ? 'side-collapsed' : ''} ${rail && peek ? 'side-peek' : ''}`}>
      <nav className="sidebar" onMouseEnter={onEnter} onMouseLeave={onLeave}>
        <div className="brand" onClick={() => rail && setPeek(peek ? null : 'tap')} style={rail ? { cursor: 'pointer' } : undefined}>
          <div className="brand-mark"><Icon name="waypoints" size={19} strokeWidth={2.4} /></div>
          <div className="brand-text"><b>DebBuilder</b><small>AI workflow studio</small></div>
        </div>
        <ThemeToggle collapsed={collapsed} />
        <div className="nav-section">Build</div>
        {link('/', 'workflow', 'Workflows', (p) => p === '/' || p.startsWith('/folder/') || p.startsWith('/workflow/'))}
        {link('/executions', 'history', 'Executions', (p) => p.startsWith('/executions'))}
        <div className="nav-section">Configure</div>
        {link('/credentials', 'key-round', 'Credentials')}
        {link('/phone', 'phone', 'Phone')}
        {link('/settings/account', 'settings', 'Settings', (p) => p.startsWith('/settings'))}
        <UserChip collapsed={collapsed} />
      </nav>
      {rail && peek === 'tap' && <div className="side-scrim" onClick={() => setPeek(null)} />}
      <main className="main">{children}</main>
    </div>
  )
}

function UserChip({ collapsed }: { collapsed: boolean }) {
  const { user, logout } = useAuth()
  if (!user) return null
  const initials = (user.name || user.email).split(/[\s@.]+/).filter(Boolean).slice(0, 2).map((w) => w[0]?.toUpperCase()).join('')
  return (
    <div className="user-chip" style={{ marginTop: 'auto' }} title={collapsed ? `${user.email} · ${ROLE_LABEL[user.role]}` : undefined}>
      <div className="avatar">{initials}</div>
      <div className="label user-meta"><b>{user.name || user.email.split('@')[0]}</b><small>{ROLE_LABEL[user.role]}</small></div>
      <button className="nav-link signout" onClick={logout} title="Sign out"><Icon name="log-out" size={16} /></button>
    </div>
  )
}

/** Nothing but the sign-in screens renders until the server confirms a session. */
function AuthGate() {
  const { loading, user } = useAuth()
  if (loading) return <div className="auth-wrap"><div className="spin"><Icon name="loader-circle" size={26} /></div></div>
  if (!user) return <LoginScreen />
  if (user.must_change_password) return <ForcePasswordChange />
  return (
    <CatalogProvider>
      <Suspense fallback={<div className="page-loading" aria-busy="true" />}>
      <Routes>
        <Route path="/workflow/:id/*" element={<Shell compact><Editor /></Shell>} />
          <Route path="/folder/:folderId" element={<Shell><Workflows /></Shell>} />
          <Route path="/executions/:id" element={<Shell><ExecutionDetail /></Shell>} />
          <Route path="/executions" element={<Shell><ExecutionList /></Shell>} />
          <Route path="/credentials" element={<Shell><Credentials /></Shell>} />
          <Route path="/phone" element={<Shell><Phone /></Shell>} />
          <Route path="/settings/:tab?" element={<Shell><Settings /></Shell>} />
          <Route path="*" element={<Shell><Workflows /></Shell>} />
      </Routes>
      </Suspense>
    </CatalogProvider>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <AuthGate />
      </AuthProvider>
      <Toaster />
      <ConfirmHost />
      <ApiVersionBanner />
    </BrowserRouter>
  )
}
