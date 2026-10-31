"""Sign-in, first-run setup, the signed-in user's own account (password, MFA, sessions, API keys)."""

import asyncio
import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import timedelta
from types import SimpleNamespace

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, select

from ..config import get_settings
from ..credentials import decrypt, encrypt
from ..db import ApiKey, AuthSession, LoginChallenge, SessionLocal, User
from ..services import mailer
from ..security import (CSRF_COOKIE, LOCKOUT_AFTER, LOCKOUT_FOR, ROLES, SESSION_IDLE, _allowed_origin, audit,
                        check_password_policy, clean_username, clear_session_cookies, client_ip, create_session, current,
                        hash_password_async, new_recovery_codes, new_totp_secret, now, rate_limit, resolve, revoke_sessions,
                        role_rank, set_session_cookies, sha256, sign, totp_uri, unsign, verify_password_async, verify_totp)

router = APIRouter(prefix="/api/auth", tags=["auth"])
log = logging.getLogger("debbuilder.auth")

CODE_TTL = timedelta(minutes=10)
CODE_ATTEMPTS = 5
RESEND_AFTER = timedelta(seconds=30)
MAX_SENDS = 5


def user_json(u: User, role: str | None = None) -> dict:
    return {"id": str(u.id), "email": u.email, "username": u.username, "name": u.name, "role": role or u.role, "mfa_enabled": u.mfa_enabled,
            "must_change_password": u.must_change_password, "disabled": u.disabled,
            "last_login_at": u.last_login_at, "created_at": u.created_at,
            "recovery_codes_left": len(u.recovery_codes or [])}


async def find_user(s, ident: str) -> User | None:
    """Look an account up by email or username (usernames never contain @)."""
    ident = ident.strip().lower()
    col = User.email if "@" in ident else User.username
    return (await s.execute(select(User).where(col == ident))).scalar_one_or_none()


async def ensure_username_free(s, username: str | None, except_id: uuid.UUID | None = None) -> None:
    if not username:
        return
    other = (await s.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if other and other.id != except_id:
        raise HTTPException(409, "That username is taken")


_tasks: set[asyncio.Task] = set()


def _background(coro) -> None:
    t = asyncio.create_task(coro)
    _tasks.add(t)  # keep a reference until done
    t.add_done_callback(_tasks.discard)


async def notify_password_changed(u: User, request: Request, how: str) -> None:
    """Security notice to the account's address. Best effort: never fails the request."""
    if not mailer.configured():
        return
    msg = mailer.password_changed_email(to=u.email, name=u.name, ip=client_ip(request),
                                        user_agent=request.headers.get("user-agent", ""), how=how)

    async def go():
        try:
            await mailer.send(u.email, *msg)
        except mailer.MailError as e:
            log.error("Password-changed notice to %s failed: %s", u.email, e)
    _background(go())


def _uuid_or_none(v: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(v)
    except ValueError:
        return None


def _same_origin(request: Request) -> None:
    if not _allowed_origin(request, request.headers.get("origin") or ""):
        raise HTTPException(403, "Cross-origin request blocked")


async def _login_response(user: User, request: Request, method: str = "email") -> JSONResponse:
    token = await create_session(user, request)
    async with SessionLocal() as s:
        u = await s.get(User, user.id)
        u.failed_logins, u.locked_until, u.last_login_at = 0, None, now()
        await s.commit()
        await s.refresh(u)
    await audit("auth.login", request, user=u, status=200, method=method)
    resp = JSONResponse(jsonable_encoder({"user": user_json(u)}))
    set_session_cookies(resp, token)
    return resp


# ---------------------------------------------------------------------------
# Setup / status / login / logout
# ---------------------------------------------------------------------------

@router.get("/status")
async def status(request: Request):
    async with SessionLocal() as s:
        n = (await s.execute(select(func.count(User.id)))).scalar_one()
    p = await resolve(request) if n else None
    return {"setup_required": n == 0, "signup_enabled": n == 0 or get_settings().allow_signup,
            "user": user_json(p.user, p.role) if p else None}


class SetupIn(BaseModel):
    email: str
    name: str = ""
    username: str = ""
    password: str


@router.post("/setup")
async def setup(body: SetupIn, request: Request):
    """Create the owner account. Only possible while no user exists (older clients; see signup)."""
    async with SessionLocal() as s:
        if (await s.execute(select(func.count(User.id)))).scalar_one():
            raise HTTPException(409, "Setup is already complete. Sign in instead.")
    return await signup(body, request)


@router.post("/signup")
async def signup(body: SetupIn, request: Request):
    """Create an account from the sign-in page. The first one is the owner; later ones (if ALLOW_SIGNUP)
    are editors with their own empty workspace."""
    _same_origin(request)
    rate_limit(f"signup:{client_ip(request)}", 5, 60)
    async with SessionLocal() as s:
        first = not (await s.execute(select(func.count(User.id)))).scalar_one()
    purpose = "setup" if first else "signup"
    if first and not get_settings().allow_remote_setup and client_ip(request) not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(403, "Create the owner account from the server itself (http://localhost), "
                                 "or set ALLOW_REMOTE_SETUP=true in backend/.env for the first setup.")
    if not first and not get_settings().allow_signup:
        raise HTTPException(403, "Sign-up is turned off. Ask an admin to add you.")
    email = body.email.strip().lower()
    if "@" not in email or len(email) > 320:
        raise HTTPException(400, "Enter a valid email address")
    username = clean_username(body.username)
    check_password_policy(body.password, email, body.name)
    # No account is saved yet: the details wait, encrypted, in a short-lived challenge and the account
    # is created only when the emailed code is entered (see login_verify). An email that already has an
    # account isn't revealed here: its owner gets the code, and only entering it says so.
    code = f"{secrets.randbelow(1_000_000):06d}"
    async with SessionLocal() as s:
        if first and (await s.execute(select(func.count(User.id)))).scalar_one():
            raise HTTPException(409, "Setup is already complete. Sign in instead.")
        await ensure_username_free(s, username)
        await s.execute(delete(LoginChallenge).where(LoginChallenge.purpose == purpose, LoginChallenge.expires_at < now()))
        ch = LoginChallenge(purpose=purpose, user_id=None, code_hash="", expires_at=now() + CODE_TTL, ip=client_ip(request),
                            payload=encrypt({"email": email, "username": username, "name": body.name.strip()[:200],
                                             "password_hash": await hash_password_async(body.password)}))
        s.add(ch)
        await s.flush()
        ch.code_hash = _code_hash(ch.id, code)
        await s.commit()
    pending = _pending(ch.payload)
    try:
        delivery = await _deliver(pending, code, request, purpose)
    except HTTPException:
        async with SessionLocal() as s:  # nothing to keep if the code couldn't be emailed
            await s.execute(delete(LoginChallenge).where(LoginChallenge.id == ch.id))
            await s.commit()
        raise
    await audit(f"auth.{purpose}_started", request, status=200, email=email, via=delivery)
    return _challenge_response(ch, pending, purpose, delivery)


class LoginIn(BaseModel):
    login: str = ""  # email or username
    email: str = ""  # older clients
    password: str


@router.post("/login")
async def login(body: LoginIn, request: Request):
    _same_origin(request)
    ip = client_ip(request)
    rate_limit(f"login:ip:{ip}", get_settings().login_rate_limit, 60)
    ident = (body.login or body.email).strip().lower()[:320]
    rate_limit(f"login:user:{ident}", 20, 300)
    async with SessionLocal() as s:
        u = await find_user(s, ident) if ident else None
        if u and u.locked_until and u.locked_until > now():
            await audit("auth.login_locked", request, user=u, status=423)
            raise HTTPException(423, "Too many failed attempts. The account is locked for a few minutes.")
        # verify_password runs the hash even for unknown emails, so timing doesn't reveal accounts.
        ok = await verify_password_async(u.password_hash if u else None, body.password)
        if not ok or not u or u.disabled:
            if u:
                u.failed_logins += 1
                if u.failed_logins >= LOCKOUT_AFTER:
                    u.locked_until, u.failed_logins = now() + LOCKOUT_FOR, 0
                await s.commit()
            await audit("auth.login_failed", request, user=u, status=401, login=ident, disabled=bool(u and u.disabled))
            raise HTTPException(401, "Wrong email, username or password")
    return await _start_challenge(u, request)


# ---------------------------------------------------------------------------
# Second step: one-time code sent by email (or authenticator app / recovery code)
# ---------------------------------------------------------------------------

def _code_hash(cid: uuid.UUID, code: str) -> str:
    # Keyed + bound to the challenge, so a leaked row can't be checked offline or reused elsewhere.
    from ..security import _signing_key
    return hmac.new(_signing_key(), f"{cid}:{code}".encode(), hashlib.sha256).hexdigest()


def _mask(email: str) -> str:
    local, _, domain = email.partition("@")
    shown = local[0] + "***" + (local[-1] if len(local) > 2 else "")
    return f"{shown}@{domain}"


async def _deliver(u: User, code: str, request: Request, kind: str = "login") -> str:
    """Email the code (kind: login | setup | signup | reset). Returns how it was delivered: "email" or "console" (dev fallback)."""
    minutes = int(CODE_TTL.total_seconds() // 60)
    who = dict(to=u.email, name=u.name, ip=client_ip(request), user_agent=request.headers.get("user-agent", ""), minutes=minutes)
    subject, text, html = (mailer.reset_email(code, **who) if kind == "reset"
                           else mailer.sign_in_email(code, first=kind in ("setup", "signup"), owner=kind == "setup", **who))
    fallback = get_settings().email_code_fallback == "log"
    if mailer.configured():
        try:
            await mailer.send(u.email, subject, text, html)
            return "email"
        except mailer.MailError as e:
            await audit("auth.code_email_failed", request, user=u, status=503, error=str(e)[:200])
            if not fallback:
                raise HTTPException(503, "We couldn't send the verification email. Try again later or contact an admin.") from e
            log.error("Sending the sign-in email failed (%s); using the console fallback", e)
    elif not fallback:
        raise HTTPException(503, "Email isn't configured on this server, so sign-in codes can't be sent. Ask an admin.")
    log.warning("\n%s\n  EMAIL NOT CONFIGURED (set SMTP_HOST in backend/.env).\n  %s code for %s: %s  (valid %d min)\n%s",
                "=" * 64, "Password reset" if kind == "reset" else "Sign-in", u.email, code, minutes, "=" * 64)
    return "console"


def _pending(payload: str) -> SimpleNamespace:
    """An account (owner setup or sign-up) waiting for its email code, shaped like a User for delivery and auditing."""
    return SimpleNamespace(id=None, mfa_enabled=False, recovery_codes=[], disabled=False, **decrypt(payload))


def _challenge_response(ch: LoginChallenge, u, purpose: str, delivery: str) -> dict:
    methods = ["email"] + (["totp"] if u.mfa_enabled else []) + (["recovery"] if u.recovery_codes else [])
    return {"verification_required": True,
            "challenge": sign({"cid": str(ch.id), "uid": str(u.id or ""), "p": purpose}, ttl=int(CODE_TTL.total_seconds())),
            "sent_to": _mask(u.email), "delivery": delivery, "methods": methods,
            "resend_in": int(RESEND_AFTER.total_seconds())}


async def _start_challenge(u: User, request: Request) -> dict:
    code = f"{secrets.randbelow(1_000_000):06d}"
    async with SessionLocal() as s:
        ch = LoginChallenge(user_id=u.id, code_hash="", expires_at=now() + CODE_TTL, ip=client_ip(request))
        s.add(ch)
        await s.flush()
        ch.code_hash = _code_hash(ch.id, code)
        await s.commit()
    delivery = await _deliver(u, code, request, "login")
    await audit("auth.code_sent", request, user=u, status=200, via=delivery)
    return _challenge_response(ch, u, "login", delivery)


async def _load_challenge(token: str, s) -> tuple[LoginChallenge, User | SimpleNamespace]:
    """A sign-in challenge (with its user) or a pending setup / sign-up (with the account still to be created)."""
    data = unsign(token)
    purpose = data.get("p", "login") if data else None
    if purpose not in ("login", "setup", "signup"):
        raise HTTPException(401, "This sign-in attempt expired. Start again.")
    ch = await s.get(LoginChallenge, uuid.UUID(data["cid"]))
    if not ch or ch.purpose != purpose or ch.consumed or ch.expires_at < now():
        raise HTTPException(401, "This sign-in attempt expired. Start again.")
    if purpose in ("setup", "signup"):
        return ch, _pending(ch.payload)
    u = await s.get(User, uuid.UUID(data["uid"]))
    if not u or ch.user_id != u.id or u.disabled:
        raise HTTPException(401, "This sign-in attempt expired. Start again.")
    return ch, u


class VerifyIn(BaseModel):
    challenge: str
    code: str
    method: str = "email"  # email | totp | recovery


@router.post("/login/verify")
async def login_verify(body: VerifyIn, request: Request):
    _same_origin(request)
    rate_limit(f"verify:ip:{client_ip(request)}", 2 * get_settings().login_rate_limit, 60)
    code = body.code.strip().replace(" ", "")
    async with SessionLocal() as s:
        ch, u = await _load_challenge(body.challenge, s)
        ok = False
        if body.method == "email":
            ok = len(code) == 6 and code.isdigit() and hmac.compare_digest(ch.code_hash, _code_hash(ch.id, code))
        elif body.method == "totp" and u.mfa_enabled:
            step = verify_totp(decrypt(u.mfa_secret)["s"], code, u.mfa_last_step)
            if step is not None:
                u.mfa_last_step, ok = step, True
        elif body.method == "recovery" and sha256(code.lower()) in (u.recovery_codes or []):
            u.recovery_codes, ok = [h for h in u.recovery_codes if h != sha256(code.lower())], True
        if not ok:
            ch.attempts += 1
            if ch.attempts >= CODE_ATTEMPTS:
                ch.consumed = True  # too many guesses: this challenge is dead, sign in again
                ch.payload = None  # a dead setup keeps nothing
            await s.commit()
            await audit("auth.code_failed", request, user=u, status=401, method=body.method, attempts=ch.attempts)
            if ch.consumed:
                raise HTTPException(401, "Too many wrong codes. Sign in again to get a new one.")
            raise HTTPException(401, f"Wrong code. {CODE_ATTEMPTS - ch.attempts} attempt{'s' if CODE_ATTEMPTS - ch.attempts != 1 else ''} left.")
        ch.consumed = True
        if ch.purpose == "setup":
            # Code verified: only now is the owner account created.
            ch.payload = None
            await s.execute(select(func.pg_advisory_xact_lock(4242)))  # two pending setups can't both become owner
            if (await s.execute(select(func.count(User.id)))).scalar_one():
                await s.commit()
                raise HTTPException(409, "Setup is already complete. Sign in instead.")
            u = User(email=u.email, username=u.username, name=u.name, password_hash=u.password_hash, role="owner")
            s.add(u)
            # Any other setup attempts (other tabs, wrong addresses) are void now.
            await s.execute(delete(LoginChallenge).where(LoginChallenge.purpose == "setup", LoginChallenge.id != ch.id))
            await s.commit()
            await s.refresh(u)
            await audit("auth.setup", request, user=u, status=200)
        elif ch.purpose == "signup":
            # Code verified: the address works, so create the account (an editor with an empty workspace).
            ch.payload = None
            await s.execute(select(func.pg_advisory_xact_lock(4243)))  # same address in two tabs: one account
            if (await s.execute(select(User.id).where(User.email == u.email))).first():
                await s.commit()
                raise HTTPException(409, "An account with this email already exists. Sign in, or use Forgot password.")
            if u.username and (await s.execute(select(User.id).where(User.username == u.username))).first():
                await s.commit()
                raise HTTPException(409, "That username was just taken. Sign up again with another one.")
            u = User(email=u.email, username=u.username, name=u.name, password_hash=u.password_hash, role="editor")
            s.add(u)
            await s.commit()
            await s.refresh(u)
            await audit("auth.signup", request, user=u, status=200)
        else:
            await s.commit()
    if body.method == "recovery":
        await audit("auth.recovery_code_used", request, user=u, status=200, left=len(u.recovery_codes))
    return await _login_response(u, request, method=body.method)


class ResendIn(BaseModel):
    challenge: str


@router.post("/login/resend")
async def login_resend(body: ResendIn, request: Request):
    _same_origin(request)
    rate_limit(f"resend:ip:{client_ip(request)}", 10, 60)
    code = f"{secrets.randbelow(1_000_000):06d}"
    async with SessionLocal() as s:
        ch, u = await _load_challenge(body.challenge, s)
        wait = (ch.last_sent_at + RESEND_AFTER - now()).total_seconds()
        if wait > 0:
            raise HTTPException(429, f"Wait {int(wait) + 1} seconds before requesting another code.")
        if ch.sends >= MAX_SENDS:
            raise HTTPException(429, "Too many codes requested. Sign in again.")
        # A new code replaces the old one and resets the guess counter.
        ch.code_hash, ch.sends, ch.attempts, ch.last_sent_at = _code_hash(ch.id, code), ch.sends + 1, 0, now()
        ch.expires_at = now() + CODE_TTL
        await s.commit()
    delivery = await _deliver(u, code, request, ch.purpose)
    await audit("auth.code_resent", request, user=u, status=200, via=delivery)
    return {"ok": True, "delivery": delivery, "resend_in": int(RESEND_AFTER.total_seconds())}


# ---------------------------------------------------------------------------
# Forgotten password: emailed code -> new password
# The answers never reveal whether an account exists.
# ---------------------------------------------------------------------------

_fake_attempts: dict[str, int] = {}  # guesses against reset attempts for unknown accounts


class ForgotIn(BaseModel):
    login: str  # email or username


@router.post("/password/forgot")
async def forgot_password(body: ForgotIn, request: Request):
    _same_origin(request)
    ip = client_ip(request)
    ident = body.login.strip().lower()[:320]
    if not ident:
        raise HTTPException(400, "Enter your email or username")
    rate_limit(f"forgot:ip:{ip}", 10, 60)
    rate_limit(f"forgot:id:{ident}", 1, 30)
    rate_limit(f"forgot:id15:{ident}", 5, 900)
    if not mailer.configured() and get_settings().email_code_fallback != "log":
        raise HTTPException(503, "Email isn't configured on this server, so reset codes can't be sent. Ask an admin.")
    cid = uuid.uuid4()
    async with SessionLocal() as s:
        u = await find_user(s, ident)
        if u and u.disabled:
            u = None
        if u:
            code = f"{secrets.randbelow(1_000_000):06d}"
            s.add(LoginChallenge(id=cid, user_id=u.id, purpose="reset", code_hash=_code_hash(cid, code),
                                 expires_at=now() + CODE_TTL, ip=ip))
            await s.commit()

            # Sent in the background so the response time is the same for known and unknown accounts.
            async def go(u=u, code=code):
                try:
                    await _deliver(u, code, request, "reset")
                except HTTPException:
                    pass  # already audited by _deliver
            _background(go())
    await audit("auth.password_reset_requested", request, user=u, status=200, login=ident)
    return {"challenge": sign({"cid": str(cid), "uid": str(u.id if u else uuid.uuid4()), "p": "reset"}, ttl=int(CODE_TTL.total_seconds())),
            "delivery": "email" if mailer.configured() else "console", "resend_in": int(RESEND_AFTER.total_seconds())}


async def _reset_challenge(token: str, purpose: str, s) -> tuple[LoginChallenge | None, User | None, str]:
    data = unsign(token)
    if not data or data.get("p") != purpose:
        raise HTTPException(401, "This password reset expired. Start again.")
    ch = await s.get(LoginChallenge, uuid.UUID(data["cid"]))
    u = await s.get(User, uuid.UUID(data["uid"]))
    if ch and (not u or ch.purpose != "reset" or ch.user_id != u.id or ch.consumed or ch.expires_at < now() or u.disabled):
        raise HTTPException(401, "This password reset expired. Start again.")
    return ch, u, data["cid"]


class ResetVerifyIn(BaseModel):
    challenge: str
    code: str


@router.post("/password/reset/verify")
async def reset_verify(body: ResetVerifyIn, request: Request):
    """Check the emailed code; returns a short-lived token for choosing the new password."""
    _same_origin(request)
    rate_limit(f"reset:ip:{client_ip(request)}", 2 * get_settings().login_rate_limit, 60)
    code = body.code.strip().replace(" ", "")
    async with SessionLocal() as s:
        ch, u, cid = await _reset_challenge(body.challenge, "reset", s)
        if ch and len(code) == 6 and code.isdigit() and hmac.compare_digest(ch.code_hash, _code_hash(ch.id, code)):
            return {"reset_token": sign({"cid": cid, "uid": str(u.id), "p": "reset-ok"}, ttl=int(CODE_TTL.total_seconds()))}
        if ch:
            ch.attempts += 1
            ch.consumed = ch.attempts >= CODE_ATTEMPTS
            attempts = ch.attempts
            await s.commit()
            await audit("auth.reset_code_failed", request, user=u, status=401, attempts=attempts)
        else:  # unknown account: count guesses the same way
            if len(_fake_attempts) > 10_000:
                _fake_attempts.clear()
            attempts = _fake_attempts[cid] = _fake_attempts.get(cid, 0) + 1
    if attempts >= CODE_ATTEMPTS:
        raise HTTPException(401, "Too many wrong codes. Start again to get a new one.")
    left = CODE_ATTEMPTS - attempts
    raise HTTPException(401, f"Wrong code. {left} attempt{'s' if left != 1 else ''} left.")


class ResetIn(BaseModel):
    reset_token: str
    password: str


@router.post("/password/reset")
async def reset_password(body: ResetIn, request: Request):
    _same_origin(request)
    rate_limit(f"reset:ip:{client_ip(request)}", 2 * get_settings().login_rate_limit, 60)
    async with SessionLocal() as s:
        ch, u, _ = await _reset_challenge(body.reset_token, "reset-ok", s)
        if not ch:
            raise HTTPException(401, "This password reset expired. Start again.")
        check_password_policy(body.password, u.email, u.name)
        u.password_hash, u.must_change_password, u.password_changed_at = await hash_password_async(body.password), False, now()
        u.failed_logins, u.locked_until = 0, None
        # This and every other outstanding code for the account stop working.
        for other in (await s.execute(select(LoginChallenge).where(LoginChallenge.user_id == u.id,
                                                                     LoginChallenge.consumed.is_(False)))).scalars():
            other.consumed = True
        await s.commit()
    await revoke_sessions(u.id)  # sign out everywhere
    await audit("auth.password_reset", request, user=u, status=200)
    await notify_password_changed(u, request, "reset using a code sent to this address")
    return {"ok": True}


@router.post("/logout")
async def logout(request: Request):
    p = await resolve(request)
    if p and p.session_id:
        async with SessionLocal() as s:
            sess = await s.get(AuthSession, p.session_id)
            if sess:
                sess.revoked = True
                await s.commit()
        await audit("auth.logout", request, p, status=200)
    resp = JSONResponse({"ok": True})
    clear_session_cookies(resp)
    return resp


# ---------------------------------------------------------------------------
# Own account
# ---------------------------------------------------------------------------

@router.get("/me")
async def me(request: Request):
    p = current(request)
    return {"user": user_json(p.user, p.role), "via": p.via}


class ProfileIn(BaseModel):
    name: str | None = None
    username: str | None = None  # "" removes it


@router.put("/me")
async def update_me(body: ProfileIn, request: Request):
    p = current(request)
    async with SessionLocal() as s:
        u = await s.get(User, p.user.id)
        if body.name is not None:
            u.name = body.name.strip()[:200]
        if body.username is not None:
            username = clean_username(body.username)
            await ensure_username_free(s, username, u.id)  # before assigning, or autoflush hits the unique index
            u.username = username
        await s.commit()
        await s.refresh(u)
    return {"user": user_json(u)}


class PasswordIn(BaseModel):
    current_password: str
    new_password: str


@router.post("/password")
async def change_password(body: PasswordIn, request: Request):
    p = current(request)
    rate_limit(f"pw:{p.user.id}", 5, 300)
    async with SessionLocal() as s:
        u = await s.get(User, p.user.id)
        if not await verify_password_async(u.password_hash, body.current_password):
            await audit("auth.password_change_failed", request, p, status=401)
            raise HTTPException(401, "Current password is wrong")
        if await verify_password_async(u.password_hash, body.new_password):
            raise HTTPException(400, "Choose a password different from the current one")
        check_password_policy(body.new_password, u.email, u.name)
        u.password_hash, u.must_change_password, u.password_changed_at = await hash_password_async(body.new_password), False, now()
        await s.commit()
    await revoke_sessions(p.user.id, except_id=p.session_id)  # sign out everywhere else
    await audit("auth.password_changed", request, p, status=200)
    await notify_password_changed(u, request, "changed from your account settings")
    return {"ok": True}


@router.post("/mfa/setup")
async def mfa_setup(request: Request):
    """Start enrolment: a new secret (not active until confirmed with a code)."""
    import segno

    p = current(request)
    secret = new_totp_secret()
    uri = totp_uri(secret, p.user.email)
    qr = segno.make(uri, error="m").svg_data_uri(scale=5, border=2)
    return {"secret": secret, "otpauth_uri": uri, "qr": qr, "pending_token": sign({"uid": str(p.user.id), "s": secret}, ttl=900)}


class MfaEnableIn(BaseModel):
    pending_token: str
    code: str


@router.post("/mfa/enable")
async def mfa_enable(body: MfaEnableIn, request: Request):
    p = current(request)
    rate_limit(f"mfa-enable:{p.user.id}", 10, 300)
    data = unsign(body.pending_token)
    if not data or data.get("uid") != str(p.user.id):
        raise HTTPException(400, "Setup expired. Start again.")
    step = verify_totp(data["s"], body.code, 0)
    if step is None:
        raise HTTPException(400, "That code doesn't match. Check your authenticator app's time and try again.")
    codes, hashes = new_recovery_codes()
    async with SessionLocal() as s:
        u = await s.get(User, p.user.id)
        u.mfa_secret, u.mfa_enabled, u.mfa_last_step, u.recovery_codes = encrypt({"s": data["s"]}), True, step, hashes
        await s.commit()
    await audit("auth.mfa_enabled", request, p, status=200)
    return {"recovery_codes": codes}


class ConfirmIn(BaseModel):
    password: str
    code: str = ""


async def _confirm_identity(p, body: ConfirmIn, request: Request) -> User:
    async with SessionLocal() as s:
        u = await s.get(User, p.user.id)
        if not await verify_password_async(u.password_hash, body.password):
            await audit("auth.reauth_failed", request, p, status=401)
            raise HTTPException(401, "Password is wrong")
        if u.mfa_enabled:
            step = verify_totp(decrypt(u.mfa_secret)["s"], body.code, u.mfa_last_step)
            if step is None:
                raise HTTPException(401, "Authenticator code is wrong")
            u.mfa_last_step = step
            await s.commit()
        return u


@router.post("/mfa/disable")
async def mfa_disable(body: ConfirmIn, request: Request):
    p = current(request)
    rate_limit(f"reauth:{p.user.id}", 5, 300)
    await _confirm_identity(p, body, request)
    async with SessionLocal() as s:
        u = await s.get(User, p.user.id)
        u.mfa_enabled, u.mfa_secret, u.recovery_codes = False, None, []
        await s.commit()
    await audit("auth.mfa_disabled", request, p, status=200)
    return {"ok": True}


@router.post("/mfa/recovery-codes")
async def regenerate_recovery(body: ConfirmIn, request: Request):
    p = current(request)
    rate_limit(f"reauth:{p.user.id}", 5, 300)
    u = await _confirm_identity(p, body, request)
    if not u.mfa_enabled:
        raise HTTPException(400, "Turn on two-factor authentication first")
    codes, hashes = new_recovery_codes()
    async with SessionLocal() as s:
        u = await s.get(User, p.user.id)
        u.recovery_codes = hashes
        await s.commit()
    await audit("auth.recovery_codes_regenerated", request, p, status=200)
    return {"recovery_codes": codes}


@router.get("/sessions")
async def my_sessions(request: Request):
    p = current(request)
    t = now()
    async with SessionLocal() as s:
        rows = (await s.execute(select(AuthSession).where(
            AuthSession.user_id == p.user.id, AuthSession.revoked.is_(False), AuthSession.expires_at > t,
            AuthSession.last_seen_at > t - SESSION_IDLE).order_by(AuthSession.last_seen_at.desc()))).scalars().all()
    return [{"id": str(x.id), "created_at": x.created_at, "last_seen_at": x.last_seen_at, "expires_at": x.expires_at,
             "ip": x.ip, "user_agent": x.user_agent, "current": x.id == p.session_id} for x in rows]


@router.delete("/sessions/{sid}")
async def revoke_session(sid: str, request: Request):
    p = current(request)
    async with SessionLocal() as s:
        x = await s.get(AuthSession, _uuid_or_none(sid))
        if not x or x.user_id != p.user.id:
            raise HTTPException(404, "Session not found")
        x.revoked = True
        await s.commit()
    await audit("auth.session_revoked", request, p, status=200, session=sid)
    return {"ok": True}


@router.post("/sessions/revoke-others")
async def revoke_others(request: Request):
    p = current(request)
    await revoke_sessions(p.user.id, except_id=p.session_id)
    await audit("auth.sessions_revoked", request, p, status=200)
    return {"ok": True}


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

def _key_json(k: ApiKey) -> dict:
    return {"id": str(k.id), "name": k.name, "prefix": k.prefix, "role": k.role, "created_at": k.created_at,
            "expires_at": k.expires_at, "last_used_at": k.last_used_at}


@router.get("/api-keys")
async def list_keys(request: Request):
    p = current(request)
    async with SessionLocal() as s:
        rows = (await s.execute(select(ApiKey).where(ApiKey.user_id == p.user.id, ApiKey.revoked.is_(False))
                                .order_by(ApiKey.created_at.desc()))).scalars().all()
    return [_key_json(k) for k in rows]


class KeyIn(BaseModel):
    name: str
    role: str | None = None
    expires_days: int | None = 90


@router.post("/api-keys")
async def create_key(body: KeyIn, request: Request):
    p = current(request)
    if p.via != "session":
        raise HTTPException(403, "API keys can only be created from a signed-in browser session")
    role = body.role or p.user.role
    if role not in ROLES or role_rank(role) > role_rank(p.user.role):
        raise HTTPException(400, "A key can't have a higher role than yours")
    if not body.name.strip():
        raise HTTPException(400, "Name the key")
    prefix, secret = secrets.token_hex(6), secrets.token_urlsafe(32)
    full = f"dbk_{prefix}_{secret}"
    async with SessionLocal() as s:
        k = ApiKey(user_id=p.user.id, name=body.name.strip()[:200], prefix=prefix, key_hash=sha256(full), role=role,
                   expires_at=now() + timedelta(days=body.expires_days) if body.expires_days else None)
        s.add(k)
        await s.commit()
        await s.refresh(k)
    await audit("auth.api_key_created", request, p, status=200, key=prefix, role=role)
    return {**_key_json(k), "key": full}


@router.delete("/api-keys/{kid}")
async def revoke_key(kid: str, request: Request):
    p = current(request)
    async with SessionLocal() as s:
        k = await s.get(ApiKey, _uuid_or_none(kid))
        if not k or k.user_id != p.user.id:
            raise HTTPException(404, "Key not found")
        k.revoked = True
        await s.commit()
    await audit("auth.api_key_revoked", request, p, status=200, key=k.prefix)
    return {"ok": True}


__all__ = ["router", "user_json", "CSRF_COOKIE"]
