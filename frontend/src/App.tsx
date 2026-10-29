import { useEffect, useRef, useState } from 'react'
import { BrowserRouter, NavLink, Route, Routes, useLocation } from 'react-router-dom'
import { AuthProvider, ROLE_LABEL, useAuth } from './auth'
import { ConfirmHost } from './components/Dialogs'
import { CatalogProvider, Icon, Toaster } from './lib'
import Credentials from './pages/Credentials'
import Editor from './pages/Editor'
import { ForcePasswordChange, LoginScreen, SetupScreen } from './pages/AuthScreens'
import { ExecutionDetail, ExecutionList } from './pages/Executions'
import Settings from './pages/Settings'
import Workflows from './pages/Workflows'

function ThemeToggle({ collapsed }: { collapsed: boolean }) {
  const [theme, setTheme] = useState(() => localStorageGet('theme') ?? '')
  useEffect(() => {
    if (theme) document.documentElement.dataset.theme = theme
    else delete document.documentElement.dataset.theme
    localStorageSet('theme', theme)
  }, [theme])
  const next = theme === '' ? 'dark' : theme === 'dark' ? 'light' : ''
  const label = `Theme: ${theme || 'system'}`
  return (
    <button className="nav-link theme-toggle" onClick={() => setTheme(next)}
      title={collapsed ? `${label} (click: ${next || 'system'})` : 'Cycle theme: system → dark → light'}>
      <Icon name={theme === 'dark' ? 'moon' : theme === 'light' ? 'sun' : 'monitor'} /> <span className="label">{label}</span>
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
  try { localStorage.setItem(k, v) } catch { /* private mode */ }
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
  // Collapses automatically: the workflow editor (compact) and narrow screens get an icon rail
  // that opens as an overlay while hovered (or when the logo is tapped on touch screens).
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
    // Short delay so just crossing the rail on the way to the canvas doesn't open it.
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
  const { loading, setupRequired, user } = useAuth()
  if (loading) return <div className="auth-wrap"><div className="spin"><Icon name="loader-circle" size={26} /></div></div>
  if (setupRequired) return <SetupScreen />
  if (!user) return <LoginScreen />
  if (user.must_change_password) return <ForcePasswordChange />
  return (
    <CatalogProvider>
      <Routes>
        <Route path="/workflow/:id/*" element={<Shell compact><Editor /></Shell>} />
          <Route path="/folder/:folderId" element={<Shell><Workflows /></Shell>} />
          <Route path="/executions/:id" element={<Shell><ExecutionDetail /></Shell>} />
          <Route path="/executions" element={<Shell><ExecutionList /></Shell>} />
          <Route path="/credentials" element={<Shell><Credentials /></Shell>} />
          <Route path="/settings/:tab?" element={<Shell><Settings /></Shell>} />
          <Route path="*" element={<Shell><Workflows /></Shell>} />
      </Routes>
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
