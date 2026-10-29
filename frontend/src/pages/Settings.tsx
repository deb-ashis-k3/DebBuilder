import { useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { NavLink, Navigate, useParams } from 'react-router-dom'
import { auth } from '../api'
import { ROLE_HELP, ROLE_LABEL, useAuth } from '../auth'
import { Menu, confirmDialog, errText } from '../components/Dialogs'
import { Select } from '../components/Select'
import { Icon, fmtDate, toast } from '../lib'
import type { ApiKey, AuditEntry, AuthSessionInfo, Role, User } from '../types'
import { PasswordRules } from './AuthScreens'

const ROLES: Role[] = ['viewer', 'editor', 'admin', 'owner']

function Section({ title, desc, children, action }: { title: string; desc?: ReactNode; children: ReactNode; action?: ReactNode }) {
  return (
    <section className="card set-section">
      <div className="set-head"><div><h2>{title}</h2>{desc && <p className="muted small">{desc}</p>}</div>{action}</div>
      {children}
    </section>
  )
}

/** A secret shown exactly once (temporary password, API key, recovery codes). */
function OneTimeSecret({ label, value, onDone }: { label: string; value: string; onDone: () => void }) {
  return (
    <div className="secret-box" role="alert">
      <div className="small"><Icon name="triangle-alert" size={14} /> <b>{label}</b> It won't be shown again.</div>
      <pre>{value}</pre>
      <div className="row" style={{ gap: 8 }}>
        <button className="btn sm" onClick={() => navigator.clipboard.writeText(value).then(() => toast('Copied'))}><Icon name="copy" size={14} /> Copy</button>
        <button className="btn sm primary" onClick={onDone}>I've saved it</button>
      </div>
    </div>
  )
}

// ===========================================================================
// Account
// ===========================================================================

function Profile() {
  const { user, setUser } = useAuth()
  const [name, setName] = useState(user?.name ?? '')
  return (
    <Section title="Profile" desc={<>Signed in as <b>{user?.email}</b> · role <b>{user && ROLE_LABEL[user.role]}</b></>}>
      <form className="row set-row" onSubmit={async (e) => { e.preventDefault(); try { setUser((await auth.updateMe(name)).user); toast('Saved') } catch (x) { toast(errText(x), 'error') } }}>
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="Your name" />
        <button className="btn">Save</button>
      </form>
    </Section>
  )
}

function ChangePassword() {
  const { user } = useAuth()
  const [cur, setCur] = useState('')
  const [pw, setPw] = useState('')
  const [pw2, setPw2] = useState('')
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (pw !== pw2) return toast("New passwords don't match", 'error')
    try { await auth.changePassword(cur, pw); setCur(''); setPw(''); setPw2(''); toast('Password changed. Other sessions were signed out.') } catch (x) { toast(errText(x), 'error') }
  }
  return (
    <Section title="Password" desc="Changing it signs you out everywhere else.">
      <form className="set-form" onSubmit={submit}>
        <input className="input" type="password" placeholder="Current password" value={cur} onChange={(e) => setCur(e.target.value)} autoComplete="current-password" required />
        <input className="input" type="password" placeholder="New password" value={pw} onChange={(e) => setPw(e.target.value)} autoComplete="new-password" required />
        <input className="input" type="password" placeholder="Confirm new password" value={pw2} onChange={(e) => setPw2(e.target.value)} autoComplete="new-password" required />
        {pw && <PasswordRules pw={pw} email={user?.email} />}
        <div><button className="btn primary">Change password</button></div>
      </form>
    </Section>
  )
}

function TwoFactor() {
  const { user, refresh } = useAuth()
  const [setup, setSetup] = useState<{ secret: string; qr: string; pending_token: string } | null>(null)
  const [code, setCode] = useState('')
  const [codes, setCodes] = useState<string[] | null>(null)
  const [confirm, setConfirm] = useState<'disable' | 'regen' | null>(null)
  const [pw, setPw] = useState('')
  const enabled = user?.mfa_enabled

  if (codes) {
    return (
      <Section title="Two-factor authentication" desc="Store these recovery codes somewhere safe, like a password manager. Each one signs you in once if you lose your phone.">
        <OneTimeSecret label="Recovery codes." value={codes.join('\n')} onDone={() => { setCodes(null); refresh() }} />
      </Section>
    )
  }
  return (
    <Section title="Two-factor authentication"
      desc={enabled ? <>On. <b>{user?.recovery_codes_left}</b> recovery codes left.</> : 'Require a code from an authenticator app (Google Authenticator, 1Password, Authy…) at sign-in.'}
      action={enabled ? <span className="badge success">On</span> : <span className="badge">Off</span>}>
      {!enabled && !setup && <button className="btn primary" onClick={async () => setSetup(await auth.mfaSetup())}><Icon name="shield-check" size={15} /> Set up two-factor authentication</button>}
      {!enabled && setup && (
        <form className="mfa-setup" onSubmit={async (e) => {
          e.preventDefault()
          try { setCodes((await auth.mfaEnable(setup.pending_token, code)).recovery_codes); setSetup(null); setCode('') } catch (x) { toast(errText(x), 'error') }
        }}>
          <img src={setup.qr} alt="QR code for your authenticator app" width={180} height={180} />
          <div className="set-form">
            <p className="small">1. Scan the QR code with your authenticator app.<br />Can't scan? Enter this key: <code className="mfa-secret">{setup.secret}</code></p>
            <p className="small">2. Enter the 6-digit code it shows:</p>
            <input className="input otp" value={code} onChange={(e) => setCode(e.target.value)} inputMode="numeric" maxLength={6} placeholder="123456" autoFocus required />
            <div className="row" style={{ gap: 8 }}><button className="btn primary">Turn on</button><button type="button" className="btn" onClick={() => setSetup(null)}>Cancel</button></div>
          </div>
        </form>
      )}
      {enabled && !confirm && (
        <div className="row" style={{ gap: 8 }}>
          <button className="btn" onClick={() => setConfirm('regen')}><Icon name="refresh-cw" size={14} /> New recovery codes</button>
          <button className="btn danger" onClick={() => setConfirm('disable')}>Turn off</button>
        </div>
      )}
      {enabled && confirm && (
        <form className="set-form" onSubmit={async (e) => {
          e.preventDefault()
          try {
            if (confirm === 'disable') { await auth.mfaDisable(pw, code); toast('Two-factor authentication is off'); await refresh() }
            else setCodes((await auth.recoveryCodes(pw, code)).recovery_codes)
            setConfirm(null); setPw(''); setCode('')
          } catch (x) { toast(errText(x), 'error') }
        }}>
          <p className="small">Confirm it's you:</p>
          <input className="input" type="password" placeholder="Password" value={pw} onChange={(e) => setPw(e.target.value)} required />
          <input className="input otp" placeholder="Authenticator code" value={code} onChange={(e) => setCode(e.target.value)} inputMode="numeric" maxLength={6} required />
          <div className="row" style={{ gap: 8 }}>
            <button className={`btn ${confirm === 'disable' ? 'danger solid' : 'primary'}`}>{confirm === 'disable' ? 'Turn off two-factor' : 'Generate new codes'}</button>
            <button type="button" className="btn" onClick={() => setConfirm(null)}>Cancel</button>
          </div>
        </form>
      )}
    </Section>
  )
}

function shortUa(ua: string) {
  const b = /Edg\//.test(ua) ? 'Edge' : /Chrome\//.test(ua) ? 'Chrome' : /Firefox\//.test(ua) ? 'Firefox' : /Safari\//.test(ua) ? 'Safari' : ua.split(' ')[0] || 'Unknown'
  const os = /Windows/.test(ua) ? 'Windows' : /Mac OS/.test(ua) ? 'macOS' : /Android/.test(ua) ? 'Android' : /iPhone|iPad/.test(ua) ? 'iOS' : /Linux/.test(ua) ? 'Linux' : ''
  return `${b}${os ? ` on ${os}` : ''}`
}

function Sessions() {
  const [list, setList] = useState<AuthSessionInfo[]>([])
  const load = () => auth.sessions().then(setList)
  useEffect(() => { load() }, [])
  return (
    <Section title="Active sessions" desc="Browsers signed in to your account. Sessions end after 12 hours without activity, or 7 days at most."
      action={list.length > 1 ? <button className="btn sm" onClick={async () => { await auth.revokeOtherSessions(); toast('Signed out of other sessions'); load() }}>Sign out all others</button> : null}>
      <div className="set-list">
        {list.map((s) => (
          <div key={s.id} className="set-item">
            <Icon name="monitor" size={18} />
            <div className="grow">
              <b>{shortUa(s.user_agent)}</b>{s.current && <span className="badge success" style={{ marginLeft: 8 }}>This browser</span>}
              <div className="muted small">{s.ip || 'unknown IP'} · signed in {fmtDate(s.created_at)} · last active {fmtDate(s.last_seen_at)}</div>
            </div>
            {!s.current && <button className="btn sm" onClick={async () => { await auth.revokeSession(s.id); load() }}>Sign out</button>}
          </div>
        ))}
      </div>
    </Section>
  )
}

function ApiKeys() {
  const { user } = useAuth()
  const [list, setList] = useState<ApiKey[]>([])
  const [name, setName] = useState('')
  const [role, setRole] = useState<string>(user?.role ?? 'viewer')
  const [days, setDays] = useState('90')
  const [created, setCreated] = useState<string | null>(null)
  const load = () => auth.apiKeys().then(setList)
  useEffect(() => { load() }, [])
  const allowed = ROLES.slice(0, ROLES.indexOf(user?.role ?? 'viewer') + 1)
  return (
    <Section title="API keys" desc={<>For scripts and other systems. Send as <code>X-API-Key: dbk_…</code> or <code>Authorization: Bearer dbk_…</code>. A key can't have more access than you.</>}>
      {created && <OneTimeSecret label="Your new API key." value={created} onDone={() => setCreated(null)} />}
      <form className="row set-row" style={{ flexWrap: 'wrap' }} onSubmit={async (e) => {
        e.preventDefault()
        try { setCreated((await auth.createApiKey({ name, role, expires_days: days === 'never' ? null : Number(days) })).key); setName(''); load() } catch (x) { toast(errText(x), 'error') }
      }}>
        <input className="input" style={{ flex: '1 1 180px' }} placeholder="Key name, e.g. CI pipeline" value={name} onChange={(e) => setName(e.target.value)} required />
        <Select style={{ width: 130 }} value={role} onChange={setRole} options={allowed.map((r) => ({ value: r, label: ROLE_LABEL[r] }))} title="Role" />
        <Select style={{ width: 150 }} value={days} onChange={setDays} title="Expires"
          options={[{ value: '30', label: 'Expires in 30 days' }, { value: '90', label: 'Expires in 90 days' }, { value: '365', label: 'Expires in 1 year' }, { value: 'never', label: 'Never expires' }]} />
        <button className="btn primary"><Icon name="key-round" size={14} /> Create key</button>
      </form>
      <div className="set-list">
        {list.map((k) => (
          <div key={k.id} className="set-item">
            <Icon name="key-round" size={18} />
            <div className="grow">
              <b>{k.name}</b> <code className="muted small">dbk_{k.prefix}_…</code> <span className="badge">{ROLE_LABEL[k.role]}</span>
              <div className="muted small">created {fmtDate(k.created_at)} · {k.expires_at ? `expires ${fmtDate(k.expires_at)}` : 'never expires'} · {k.last_used_at ? `last used ${fmtDate(k.last_used_at)}` : 'never used'}</div>
            </div>
            <button className="btn sm danger" onClick={async () => {
              if (await confirmDialog({ title: `Revoke "${k.name}"?`, message: 'Anything using this key stops working immediately.', confirmLabel: 'Revoke', danger: true })) { await auth.revokeApiKey(k.id); load() }
            }}>Revoke</button>
          </div>
        ))}
        {list.length === 0 && <div className="muted small">No API keys.</div>}
      </div>
    </Section>
  )
}

// ===========================================================================
// Users (admin)
// ===========================================================================

function Users() {
  const { user: me, can } = useAuth()
  const [list, setList] = useState<User[]>([])
  const [adding, setAdding] = useState(false)
  const [email, setEmail] = useState('')
  const [name, setName] = useState('')
  const [role, setRole] = useState('editor')
  const [secret, setSecret] = useState<{ label: string; value: string } | null>(null)
  const load = () => auth.users().then(setList)
  useEffect(() => { load() }, [])
  const isOwner = can('owner')
  const roleOptions = (isOwner ? ROLES : ROLES.filter((r) => r !== 'owner')).map((r) => ({ value: r, label: ROLE_LABEL[r], hint: ROLE_HELP[r] }))
  const act = async (fn: () => Promise<unknown>, msg?: string) => { try { await fn(); if (msg) toast(msg); load() } catch (x) { toast(errText(x), 'error') } }

  return (
    <Section title="Users" desc="People who can sign in. New users get a temporary password and must choose their own at first sign-in."
      action={<button className="btn primary" onClick={() => setAdding(!adding)}><Icon name="user-plus" size={15} /> Add user</button>}>
      {secret && <OneTimeSecret label={secret.label} value={secret.value} onDone={() => setSecret(null)} />}
      {adding && (
        <form className="row set-row card-inset" style={{ flexWrap: 'wrap' }} onSubmit={async (e) => {
          e.preventDefault()
          await act(async () => {
            const r = await auth.createUser({ email, name, role })
            if (r.temporary_password) setSecret({ label: `Temporary password for ${email}. Share it securely.`, value: r.temporary_password })
            setEmail(''); setName(''); setAdding(false)
          }, 'User added')
        }}>
          <input className="input" style={{ flex: '1 1 200px' }} type="email" placeholder="email@company.com" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus />
          <input className="input" style={{ flex: '1 1 140px' }} placeholder="Name" value={name} onChange={(e) => setName(e.target.value)} />
          <Select style={{ width: 140 }} value={role} onChange={setRole} options={roleOptions} title="Role" />
          <button className="btn primary">Add</button>
        </form>
      )}
      <div className="table-wrap"><table className="table">
        <thead><tr><th>User</th><th>Role</th><th className="hide-sm">2FA</th><th className="hide-md">Last sign-in</th><th>Status</th><th /></tr></thead>
        <tbody>
          {list.map((u) => {
            const self = u.id === me?.id
            const locked = u.role === 'owner' && !isOwner
            return (
              <tr key={u.id}>
                <td><b>{u.name || u.email.split('@')[0]}</b>{self && <span className="muted small"> (you)</span>}<div className="muted small">{u.email}</div></td>
                <td style={{ minWidth: 130 }}>
                  {self || locked ? <span className="badge">{ROLE_LABEL[u.role]}</span> : (
                    <Select value={u.role} options={roleOptions} onChange={(r) => act(() => auth.updateUser(u.id, { role: r }), `${u.email} is now ${ROLE_LABEL[r as Role]}`)} />
                  )}
                </td>
                <td className="hide-sm">{u.mfa_enabled ? <span className="badge success">On</span> : <span className="badge">Off</span>}</td>
                <td className="muted small hide-md">{u.last_login_at ? fmtDate(u.last_login_at) : 'never'}</td>
                <td>{u.disabled ? <span className="badge error">Disabled</span> : u.must_change_password ? <span className="badge waiting">Pending</span> : <span className="badge success">Active</span>}</td>
                <td className="col-act">
                  {!self && !locked && (
                    <Menu items={[
                      { label: u.disabled ? 'Enable' : 'Disable', icon: u.disabled ? 'user-check' : 'user-x', onClick: () => act(() => auth.updateUser(u.id, { disabled: !u.disabled }), u.disabled ? 'Enabled' : 'Disabled and signed out') },
                      { label: 'Reset password', icon: 'key-round', onClick: async () => {
                        if (await confirmDialog({ title: `Reset ${u.email}'s password?`, message: 'They are signed out and must set a new password with the temporary one.', confirmLabel: 'Reset' })) {
                          await act(async () => setSecret({ label: `Temporary password for ${u.email}.`, value: (await auth.resetPassword(u.id)).temporary_password }))
                        }
                      } },
                      ...(u.mfa_enabled ? [{ label: 'Reset two-factor', icon: 'shield-off', onClick: async () => {
                        if (await confirmDialog({ title: `Turn off two-factor for ${u.email}?`, message: 'Use this if they lost their phone and recovery codes.', confirmLabel: 'Reset 2FA', danger: true })) await act(() => auth.resetMfa(u.id), 'Two-factor reset')
                      } }] : []),
                      { label: 'Sign out everywhere', icon: 'log-out', onClick: () => act(() => auth.revokeUserSessions(u.id), 'Signed out') },
                      { divider: true, label: '' },
                      { label: 'Delete user', icon: 'trash-2', danger: true, onClick: async () => {
                        if (await confirmDialog({ title: `Delete ${u.email}?`, message: 'Their sessions and API keys are removed. Workflows stay.', confirmLabel: 'Delete', danger: true })) await act(() => auth.deleteUser(u.id), 'User deleted')
                      } },
                    ]} />
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table></div>
    </Section>
  )
}

// ===========================================================================
// Audit log (admin)
// ===========================================================================

function Audit() {
  const [data, setData] = useState<{ total: number; items: AuditEntry[] } | null>(null)
  const [userQ, setUserQ] = useState('')
  const [action, setAction] = useState('')
  const [page, setPage] = useState(0)
  useEffect(() => {
    const t = window.setTimeout(() => auth.audit({ user: userQ, action, limit: 50, offset: page * 50 }).then(setData), 250)
    return () => window.clearTimeout(t)
  }, [userQ, action, page])
  return (
    <Section title="Audit log" desc="Sign-ins, security changes and every change made through the API. Request bodies (and so secrets) are never recorded.">
      <div className="row" style={{ gap: 8, flexWrap: 'wrap', marginBottom: 10 }}>
        <input className="input" style={{ width: 220 }} placeholder="Filter by user email" value={userQ} onChange={(e) => { setUserQ(e.target.value); setPage(0) }} />
        <Select style={{ width: 200 }} value={action} onChange={(v) => { setAction(v); setPage(0) }} title="Action"
          options={[{ value: '', label: 'All events' }, { value: 'auth.', label: 'Sign-in & security' }, { value: 'auth.login_failed', label: 'Failed sign-ins' },
            { value: 'user.', label: 'User management' }, { value: 'access.denied', label: 'Denied requests' }, { value: 'api.', label: 'Data changes' }]} />
      </div>
      <div className="table-wrap"><table className="table audit-table">
        <thead><tr><th>When</th><th>User</th><th>Event</th><th className="hide-md">Request</th><th className="hide-sm">IP</th></tr></thead>
        <tbody>
          {data?.items.map((a) => (
            <tr key={a.id} className={a.action.includes('failed') || a.action.includes('denied') || a.action.includes('locked') ? 'audit-bad' : ''}>
              <td className="muted small nowrap">{fmtDate(a.at)}</td>
              <td className="small">{a.user_email || '—'}</td>
              <td><code className="small">{a.action}</code>{Object.keys(a.detail || {}).length > 0 && <div className="muted small">{Object.entries(a.detail).map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`).join(' · ')}</div>}</td>
              <td className="muted small hide-md">{a.method} {a.path} {a.status ? `→ ${a.status}` : ''}</td>
              <td className="muted small hide-sm">{a.ip}</td>
            </tr>
          ))}
          {data && data.items.length === 0 && <tr><td colSpan={5} className="empty">No events</td></tr>}
        </tbody>
      </table></div>
      {data && data.total > 50 && (
        <div className="row" style={{ marginTop: 10, justifyContent: 'flex-end', gap: 8 }}>
          <button className="btn sm" disabled={page === 0} onClick={() => setPage(page - 1)}>Prev</button>
          <span className="muted small">{page * 50 + 1}–{Math.min((page + 1) * 50, data.total)} of {data.total}</span>
          <button className="btn sm" disabled={(page + 1) * 50 >= data.total} onClick={() => setPage(page + 1)}>Next</button>
        </div>
      )}
    </Section>
  )
}

export default function Settings() {
  const { tab = 'account' } = useParams<{ tab: string }>()
  const { can } = useAuth()
  if ((tab === 'users' && !can('user:manage')) || (tab === 'audit' && !can('audit:read'))) return <Navigate to="/settings/account" replace />
  return (
    <div className="page">
      <div className="page-inner settings">
        <div className="page-head"><h1>Settings</h1></div>
        <div className="tabs set-tabs">
          <NavLink to="/settings/account" className={({ isActive }) => `tab ${isActive ? 'active' : ''}`}>Account &amp; security</NavLink>
          {can('user:manage') && <NavLink to="/settings/users" className={({ isActive }) => `tab ${isActive ? 'active' : ''}`}>Users</NavLink>}
          {can('audit:read') && <NavLink to="/settings/audit" className={({ isActive }) => `tab ${isActive ? 'active' : ''}`}>Audit log</NavLink>}
        </div>
        {tab === 'account' && <><Profile /><ChangePassword /><TwoFactor /><Sessions /><ApiKeys /></>}
        {tab === 'users' && <Users />}
        {tab === 'audit' && <Audit />}
      </div>
    </div>
  )
}
