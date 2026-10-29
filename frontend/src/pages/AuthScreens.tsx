import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { auth } from '../api'
import { useAuth } from '../auth'
import { Icon, toast } from '../lib'
import type { LoginChallenge } from '../types'

const errText = (e: unknown) => String(e instanceof Error ? e.message : e).replace(/^Error: /, '')

function Card({ title, subtitle, children }: { title: string; subtitle?: ReactNode; children: ReactNode }) {
  return (
    <div className="auth-wrap">
      <div className="auth-card">
        <div className="auth-brand">
          <div className="brand-mark"><Icon name="waypoints" size={19} strokeWidth={2.4} /></div>
          <b>DebBuilder</b>
        </div>
        <h1>{title}</h1>
        {subtitle && <p className="auth-sub">{subtitle}</p>}
        {children}
      </div>
    </div>
  )
}

function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: ReactNode }) {
  return <label className="field"><span className="auth-label">{label}</span>{children}{hint && <span className="muted small">{hint}</span>}</label>
}

/** Live checklist that mirrors the server policy (the server is the authority). */
export function PasswordRules({ pw, email = '' }: { pw: string; email?: string }) {
  const local = email.split('@')[0].toLowerCase()
  const rules = [
    { ok: pw.length >= 12, text: 'At least 12 characters' },
    { ok: new Set(pw).size >= 5, text: 'Not just repeated characters' },
    { ok: !(local.length >= 4 && pw.toLowerCase().includes(local)), text: "Doesn't contain your email name" },
  ]
  return (
    <ul className="pw-rules">
      {rules.map((r) => <li key={r.text} className={r.ok ? 'ok' : ''}><Icon name={r.ok ? 'circle-check' : 'circle'} size={13} /> {r.text}</li>)}
    </ul>
  )
}

function PasswordInput({ value, onChange, autoComplete, autoFocus }: { value: string; onChange: (v: string) => void; autoComplete: string; autoFocus?: boolean }) {
  const [show, setShow] = useState(false)
  return (
    <div className="input-wrap">
      <input className="input" type={show ? 'text' : 'password'} value={value} onChange={(e) => onChange(e.target.value)}
        autoComplete={autoComplete} autoFocus={autoFocus} required />
      <button type="button" className="btn ghost sm icon reveal" onClick={() => setShow(!show)} title={show ? 'Hide' : 'Show'} tabIndex={-1}>
        <Icon name={show ? 'eye-off' : 'eye'} size={15} />
      </button>
    </div>
  )
}

/** Second step for every sign-in: the code emailed to the user (or authenticator / recovery code). */
function CodeStep({ ch, onBack }: { ch: LoginChallenge; onBack: () => void }) {
  const { setUser, refresh } = useAuth()
  const [method, setMethod] = useState<'email' | 'totp' | 'recovery'>('email')
  const [digits, setDigits] = useState(['', '', '', '', '', ''])
  const [text, setText] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [wait, setWait] = useState(ch.resend_in)
  const [delivery, setDelivery] = useState(ch.delivery)
  const boxes = useRef<(HTMLInputElement | null)[]>([])

  useEffect(() => {
    if (wait <= 0) return
    const t = window.setTimeout(() => setWait((w) => w - 1), 1000)
    return () => window.clearTimeout(t)
  }, [wait])

  const verify = async (code: string) => {
    setBusy(true); setErr('')
    try {
      setUser((await auth.verify(ch.challenge, code, method)).user)
      await refresh()
    } catch (x) {
      const m = errText(x)
      setErr(m)
      if (/expired|Sign in again/i.test(m)) window.setTimeout(onBack, 1800)
      setDigits(['', '', '', '', '', '']); setText('')
      boxes.current[0]?.focus()
    } finally { setBusy(false) }
  }
  const setDigit = (i: number, v: string) => {
    const only = v.replace(/\D/g, '')
    if (only.length > 1) {  // pasted (or autofilled) the whole code
      const next = only.slice(0, 6).split('').concat(Array(6).fill('')).slice(0, 6)
      setDigits(next)
      boxes.current[Math.min(only.length, 5)]?.focus()
      if (only.length >= 6) verify(only.slice(0, 6))
      return
    }
    const next = [...digits]; next[i] = only
    setDigits(next)
    if (only && i < 5) boxes.current[i + 1]?.focus()
    if (next.every((d) => d)) verify(next.join(''))
  }
  const resend = async () => {
    setErr('')
    try {
      const r = await auth.resend(ch.challenge)
      setWait(r.resend_in); setDelivery(r.delivery as 'email' | 'console'); toast('A new code is on its way')
    } catch (x) { setErr(errText(x)) }
  }

  const subtitle = method === 'email'
    ? <>We sent a 6-digit code to <b>{ch.sent_to}</b>. It expires in 10 minutes.</>
    : method === 'totp' ? 'Enter the 6-digit code from your authenticator app.' : 'Enter one of your recovery codes. Each works once.'
  return (
    <Card title={method === 'email' ? 'Check your email' : 'Verify it’s you'} subtitle={subtitle}>
      <form className="auth-form" onSubmit={(e) => { e.preventDefault(); verify(method === 'email' ? digits.join('') : text) }}>
        {method === 'email' && delivery === 'console' && (
          <div className="auth-note"><Icon name="info" size={14} /><span>Email isn’t set up on this server yet, so the code was written to the <b>backend console</b>. Set SMTP in <code>backend/.env</code> to email it.</span></div>
        )}
        {method === 'email' ? (
          <div className="code-boxes" aria-label="Verification code">
            {digits.map((d, i) => (
              <input key={i} ref={(el) => { boxes.current[i] = el }} className="input code-box" value={d} inputMode="numeric"
                autoComplete={i === 0 ? 'one-time-code' : 'off'} autoFocus={i === 0} maxLength={i === 0 ? 6 : 1} aria-label={`Digit ${i + 1}`}
                onChange={(e) => setDigit(i, e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Backspace' && !d && i > 0) boxes.current[i - 1]?.focus() }} />
            ))}
          </div>
        ) : (
          <input className="input otp" value={text} onChange={(e) => setText(e.target.value)} autoFocus required
            inputMode={method === 'totp' ? 'numeric' : 'text'} autoComplete="one-time-code"
            placeholder={method === 'totp' ? '123456' : 'xxxxxx-xxxxxx'} maxLength={method === 'totp' ? 6 : 13} />
        )}
        {err && <div className="auth-err" role="alert">{err}</div>}
        <button className="btn primary lg" disabled={busy || (method === 'email' ? digits.some((x) => !x) : !text)}>{busy ? 'Verifying…' : 'Verify'}</button>
        <div className="auth-links">
          {method === 'email'
            ? <button type="button" className="link-btn" disabled={wait > 0} onClick={resend}>{wait > 0 ? `Resend code in ${wait}s` : 'Resend code'}</button>
            : <button type="button" className="link-btn" onClick={() => { setMethod('email'); setErr('') }}>Use the emailed code</button>}
          <button type="button" className="link-btn" onClick={onBack}>Back</button>
        </div>
        {(ch.methods.includes('totp') || ch.methods.includes('recovery')) && (
          <div className="auth-alt">
            {ch.methods.includes('totp') && method !== 'totp' && <button type="button" className="link-btn" onClick={() => { setMethod('totp'); setErr('') }}>Use authenticator app</button>}
            {ch.methods.includes('recovery') && method !== 'recovery' && <button type="button" className="link-btn" onClick={() => { setMethod('recovery'); setErr('') }}>Use a recovery code</button>}
          </div>
        )}
      </form>
    </Card>
  )
}

export function SetupScreen() {
  const [email, setEmail] = useState('')
  const [name, setName] = useState('')
  const [pw, setPw] = useState('')
  const [pw2, setPw2] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [ch, setCh] = useState<LoginChallenge | null>(null)
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (pw !== pw2) return setErr("Passwords don't match")
    setBusy(true); setErr('')
    try { setCh(await auth.setup({ email, name, password: pw })) } catch (x) { setErr(errText(x)) } finally { setBusy(false) }
  }
  // The owner exists now; entering the emailed code (which also proves the address works) signs in.
  if (ch) return <CodeStep ch={ch} onBack={() => window.location.reload()} />
  return (
    <Card title="Create the owner account" subtitle="This is a new installation. The first account becomes the owner, with full control over users and settings.">
      <form onSubmit={submit} className="auth-form">
        <Field label="Your name"><input className="input" value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" autoFocus /></Field>
        <Field label="Email" hint="Sign-in codes are sent here."><input className="input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" required /></Field>
        <Field label="Password"><PasswordInput value={pw} onChange={setPw} autoComplete="new-password" /></Field>
        <PasswordRules pw={pw} email={email} />
        <Field label="Confirm password"><PasswordInput value={pw2} onChange={setPw2} autoComplete="new-password" /></Field>
        {err && <div className="auth-err" role="alert">{err}</div>}
        <button className="btn primary lg" disabled={busy}>{busy ? 'Creating…' : 'Create owner account'}</button>
      </form>
    </Card>
  )
}

export function LoginScreen() {
  const [email, setEmail] = useState('')
  const [pw, setPw] = useState('')
  const [ch, setCh] = useState<LoginChallenge | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true); setErr('')
    try { setCh(await auth.login(email, pw)); setPw('') } catch (x) { setErr(errText(x)) } finally { setBusy(false) }
  }

  if (ch) return <CodeStep ch={ch} onBack={() => setCh(null)} />
  return (
    <Card title="Sign in">
      <form onSubmit={submit} className="auth-form">
        <Field label="Email"><input className="input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="username" autoFocus required /></Field>
        <Field label="Password"><PasswordInput value={pw} onChange={setPw} autoComplete="current-password" /></Field>
        {err && <div className="auth-err" role="alert">{err}</div>}
        <button className="btn primary lg" disabled={busy}>{busy ? 'Checking…' : 'Continue'}</button>
        <p className="muted small" style={{ textAlign: 'center', margin: 0 }}>We’ll email you a code to finish signing in. Forgot your password? Ask an admin to reset it.</p>
      </form>
    </Card>
  )
}

/** Shown after signing in with a temporary password set by an admin. */
export function ForcePasswordChange() {
  const { user, refresh, logout } = useAuth()
  const [cur, setCur] = useState('')
  const [pw, setPw] = useState('')
  const [pw2, setPw2] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (pw !== pw2) return setErr("Passwords don't match")
    setBusy(true); setErr('')
    try { await auth.changePassword(cur, pw); await refresh() } catch (x) { setErr(errText(x)) } finally { setBusy(false) }
  }
  return (
    <Card title="Choose a new password" subtitle={<>You signed in with a temporary password. Set your own to continue, <b>{user?.email}</b>.</>}>
      <form onSubmit={submit} className="auth-form">
        <Field label="Temporary password"><PasswordInput value={cur} onChange={setCur} autoComplete="current-password" autoFocus /></Field>
        <Field label="New password"><PasswordInput value={pw} onChange={setPw} autoComplete="new-password" /></Field>
        <PasswordRules pw={pw} email={user?.email} />
        <Field label="Confirm new password"><PasswordInput value={pw2} onChange={setPw2} autoComplete="new-password" /></Field>
        {err && <div className="auth-err" role="alert">{err}</div>}
        <button className="btn primary lg" disabled={busy}>{busy ? 'Saving…' : 'Set password'}</button>
        <button type="button" className="link-btn" onClick={logout}>Sign out</button>
      </form>
    </Card>
  )
}
