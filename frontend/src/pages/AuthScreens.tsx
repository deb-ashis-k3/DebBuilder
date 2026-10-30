import { useEffect, useRef, useState, type FormEvent, type MutableRefObject, type ReactNode } from 'react'
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

const EMPTY = ['', '', '', '', '', '']

function useCountdown(initial: number) {
  const [wait, setWait] = useState(initial)
  useEffect(() => {
    if (wait <= 0) return
    const t = window.setTimeout(() => setWait((w) => w - 1), 1000)
    return () => window.clearTimeout(t)
  }, [wait])
  return [wait, setWait] as const
}

/** Six one-digit boxes. Pasting or autofilling the whole code fills them all; the 6th digit calls onComplete. */
function CodeBoxes({ digits, onChange, onComplete, boxes }: {
  digits: string[]; onChange: (d: string[]) => void; onComplete: (code: string) => void
  boxes: MutableRefObject<(HTMLInputElement | null)[]>
}) {
  const setDigit = (i: number, v: string) => {
    const only = v.replace(/\D/g, '')
    if (only.length > 1) {  // pasted (or autofilled) the whole code
      const next = only.slice(0, 6).split('').concat(EMPTY).slice(0, 6)
      onChange(next)
      boxes.current[Math.min(only.length, 5)]?.focus()
      if (only.length >= 6) onComplete(only.slice(0, 6))
      return
    }
    const next = [...digits]; next[i] = only
    onChange(next)
    if (only && i < 5) boxes.current[i + 1]?.focus()
    if (next.every((d) => d)) onComplete(next.join(''))
  }
  return (
    <div className="code-boxes" aria-label="Verification code">
      {digits.map((d, i) => (
        <input key={i} ref={(el) => { boxes.current[i] = el }} className="input code-box" value={d} inputMode="numeric"
          autoComplete={i === 0 ? 'one-time-code' : 'off'} autoFocus={i === 0} maxLength={i === 0 ? 6 : 1} aria-label={`Digit ${i + 1}`}
          onChange={(e) => setDigit(i, e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Backspace' && !d && i > 0) boxes.current[i - 1]?.focus() }} />
      ))}
    </div>
  )
}

const ConsoleNote = () => (
  <div className="auth-note"><Icon name="info" size={14} /><span>Email isn’t set up on this server yet, so the code was written to the <b>backend console</b>. Set SMTP in <code>backend/.env</code> to email it.</span></div>
)

/** Second step for every sign-in: the code emailed to the user (or authenticator / recovery code). */
function CodeStep({ ch, onBack }: { ch: LoginChallenge; onBack: () => void }) {
  const { setUser, refresh } = useAuth()
  const [method, setMethod] = useState<'email' | 'totp' | 'recovery'>('email')
  const [digits, setDigits] = useState(EMPTY)
  const [text, setText] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [wait, setWait] = useCountdown(ch.resend_in)
  const [delivery, setDelivery] = useState(ch.delivery)
  const boxes = useRef<(HTMLInputElement | null)[]>([])

  const verify = async (code: string) => {
    setBusy(true); setErr('')
    try {
      setUser((await auth.verify(ch.challenge, code, method)).user)
      await refresh()
    } catch (x) {
      const m = errText(x)
      setErr(m)
      if (/expired|Sign in again/i.test(m)) window.setTimeout(onBack, 1800)
      setDigits(EMPTY); setText('')
      boxes.current[0]?.focus()
    } finally { setBusy(false) }
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
        {method === 'email' && delivery === 'console' && <ConsoleNote />}
        {method === 'email' ? (
          <CodeBoxes digits={digits} onChange={setDigits} onComplete={verify} boxes={boxes} />
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

/** Forgotten password: email or username -> emailed code -> new password. */
function ForgotPassword({ initial, onBack, onDone }: { initial: string; onBack: () => void; onDone: () => void }) {
  const [step, setStep] = useState<'ask' | 'code' | 'password'>('ask')
  const [login, setLogin] = useState(initial)
  const [challenge, setChallenge] = useState('')
  const [delivery, setDelivery] = useState<'email' | 'console'>('email')
  const [token, setToken] = useState('')
  const [digits, setDigits] = useState(EMPTY)
  const [pw, setPw] = useState('')
  const [pw2, setPw2] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [wait, setWait] = useCountdown(0)
  const boxes = useRef<(HTMLInputElement | null)[]>([])

  const send = async () => {
    setBusy(true); setErr('')
    try {
      const r = await auth.forgot(login)
      setChallenge(r.challenge); setDelivery(r.delivery); setWait(r.resend_in); setDigits(EMPTY)
      if (step === 'code') { toast('A new code is on its way'); boxes.current[0]?.focus() }
      setStep('code')
    } catch (x) { setErr(errText(x)) } finally { setBusy(false) }
  }
  const verify = async (code: string) => {
    setBusy(true); setErr('')
    try {
      setToken((await auth.resetVerify(challenge, code)).reset_token)
      setStep('password')
    } catch (x) {
      const m = errText(x)
      setErr(m)
      setDigits(EMPTY); boxes.current[0]?.focus()
      if (/expired|Start again/i.test(m)) window.setTimeout(() => { setStep('ask'); setErr('') }, 2200)
    } finally { setBusy(false) }
  }
  const save = async (e: FormEvent) => {
    e.preventDefault()
    if (pw !== pw2) return setErr("Passwords don't match")
    setBusy(true); setErr('')
    try { await auth.finishReset(token, pw); onDone() } catch (x) { setErr(errText(x)) } finally { setBusy(false) }
  }

  if (step === 'code') return (
    <Card title="Check your email" subtitle={<>If an account matches <b>{login}</b>, we’ve sent a 6-digit reset code to its email address. It expires in 10 minutes.</>}>
      <form className="auth-form" onSubmit={(e) => { e.preventDefault(); verify(digits.join('')) }}>
        {delivery === 'console' && <ConsoleNote />}
        <CodeBoxes digits={digits} onChange={setDigits} onComplete={verify} boxes={boxes} />
        {err && <div className="auth-err" role="alert">{err}</div>}
        <button className="btn primary lg" disabled={busy || digits.some((x) => !x)}>{busy ? 'Checking…' : 'Continue'}</button>
        <div className="auth-links">
          <button type="button" className="link-btn" disabled={wait > 0 || busy} onClick={send}>{wait > 0 ? `Resend code in ${wait}s` : 'Resend code'}</button>
          <button type="button" className="link-btn" onClick={onBack}>Back to sign in</button>
        </div>
        <p className="muted small" style={{ textAlign: 'center', margin: 0 }}>No email? Check spam, or ask an admin to reset your password.</p>
      </form>
    </Card>
  )
  if (step === 'password') return (
    <Card title="Choose a new password" subtitle="You’ll be signed out on all devices, then you can sign in with the new password.">
      <form onSubmit={save} className="auth-form">
        <Field label="New password"><PasswordInput value={pw} onChange={setPw} autoComplete="new-password" autoFocus /></Field>
        <PasswordRules pw={pw} email={login.includes('@') ? login : ''} />
        <Field label="Confirm new password"><PasswordInput value={pw2} onChange={setPw2} autoComplete="new-password" /></Field>
        {err && <div className="auth-err" role="alert">{err}</div>}
        <button className="btn primary lg" disabled={busy}>{busy ? 'Saving…' : 'Reset password'}</button>
      </form>
    </Card>
  )
  return (
    <Card title="Forgot your password?" subtitle="Enter your email or username and we’ll email you a code to reset it.">
      <form className="auth-form" onSubmit={(e) => { e.preventDefault(); send() }}>
        <Field label="Email or username"><input className="input" value={login} onChange={(e) => setLogin(e.target.value)} autoComplete="username" autoFocus required /></Field>
        {err && <div className="auth-err" role="alert">{err}</div>}
        <button className="btn primary lg" disabled={busy || !login.trim()}>{busy ? 'Sending…' : 'Send reset code'}</button>
        <button type="button" className="link-btn" onClick={onBack}>Back to sign in</button>
      </form>
    </Card>
  )
}

/** Sign up: on a new installation this creates the owner, afterwards an editor with an empty workspace. */
function SignupScreen({ onBack }: { onBack: () => void }) {
  const { setupRequired: first } = useAuth()
  const [email, setEmail] = useState('')
  const [name, setName] = useState('')
  const [username, setUsername] = useState('')
  const [pw, setPw] = useState('')
  const [pw2, setPw2] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [ch, setCh] = useState<LoginChallenge | null>(null)
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (pw !== pw2) return setErr("Passwords don't match")
    setBusy(true); setErr('')
    try { setCh(await auth.signup({ email, name, username, password: pw })) } catch (x) { setErr(errText(x)) } finally { setBusy(false) }
  }
  // Entering the emailed code (which also proves the address works) creates the account and signs in.
  if (ch) return <CodeStep ch={ch} onBack={() => setCh(null)} />
  return (
    <Card title={first ? 'Create the owner account' : 'Create your account'}
      subtitle={first ? 'This is a new installation. The first account becomes the owner, with full control over users and settings.'
        : 'You get your own workspace for workflows and credentials. We’ll email you a code to confirm your address.'}>
      <form onSubmit={submit} className="auth-form">
        <Field label="Your name"><input className="input" value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" autoFocus /></Field>
        <Field label="Email" hint="Sign-in codes are sent here."><input className="input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" required /></Field>
        <Field label="Username (optional)" hint="Sign in with this or your email. Letters, digits, . _ -">
          <input className="input" value={username} onChange={(e) => setUsername(e.target.value.toLowerCase())} autoComplete="username" maxLength={32} />
        </Field>
        <Field label="Password"><PasswordInput value={pw} onChange={setPw} autoComplete="new-password" /></Field>
        <PasswordRules pw={pw} email={email} />
        <Field label="Confirm password"><PasswordInput value={pw2} onChange={setPw2} autoComplete="new-password" /></Field>
        {err && <div className="auth-err" role="alert">{err}</div>}
        <button className="btn primary lg" disabled={busy}>{busy ? 'Creating…' : first ? 'Create owner account' : 'Create account'}</button>
        <p className="muted small" style={{ textAlign: 'center', margin: 0 }}>
          Already have an account? <button type="button" className="link-btn" onClick={onBack}>Sign in</button>
        </p>
      </form>
    </Card>
  )
}

export function LoginScreen() {
  const { setupRequired, signupEnabled } = useAuth()
  const [signup, setSignup] = useState(false)
  const [login, setLogin] = useState('')
  const [pw, setPw] = useState('')
  const [ch, setCh] = useState<LoginChallenge | null>(null)
  const [forgot, setForgot] = useState(false)
  const [notice, setNotice] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true); setErr(''); setNotice('')
    try { setCh(await auth.login(login, pw)); setPw('') } catch (x) { setErr(errText(x)) } finally { setBusy(false) }
  }

  if (ch) return <CodeStep ch={ch} onBack={() => setCh(null)} />
  if (signup) return <SignupScreen onBack={() => { setSignup(false); setErr('') }} />
  if (forgot) return <ForgotPassword initial={login} onBack={() => setForgot(false)}
    onDone={() => { setForgot(false); setPw(''); setErr(''); setNotice('Your password was reset. Sign in with the new password.') }} />
  return (
    <Card title="Sign in">
      <form onSubmit={submit} className="auth-form">
        {notice && <div className="auth-ok" role="status"><Icon name="circle-check" size={14} /> {notice}</div>}
        {setupRequired && <div className="auth-ok" role="status"><Icon name="info" size={14} /> No accounts yet. Sign up to create the owner account.</div>}
        <Field label="Email or username"><input className="input" value={login} onChange={(e) => setLogin(e.target.value)} autoComplete="username" autoFocus required /></Field>
        <div className="field">
          <div className="auth-label-row">
            <span className="auth-label">Password</span>
            <button type="button" className="link-btn" onClick={() => { setForgot(true); setErr('') }}>Forgot password?</button>
          </div>
          <PasswordInput value={pw} onChange={setPw} autoComplete="current-password" />
        </div>
        {err && <div className="auth-err" role="alert">{err}</div>}
        <button className="btn primary lg" disabled={busy}>{busy ? 'Checking…' : 'Continue'}</button>
        <p className="muted small" style={{ textAlign: 'center', margin: 0 }}>We’ll email you a code to finish signing in.</p>
        {signupEnabled && (
          <p className="small" style={{ textAlign: 'center', margin: 0 }}>
            Don’t have an account? <button type="button" className="link-btn" onClick={() => { setSignup(true); setErr('') }}>Sign up</button>
          </p>
        )}
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
