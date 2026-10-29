"""Sign-in, first-run setup, the signed-in user's own account (password, MFA, sessions, API keys)."""

import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select

from ..config import get_settings
from ..credentials import decrypt, encrypt
from ..db import ApiKey, AuthSession, LoginChallenge, SessionLocal, User
from ..services import mailer
from ..security import (CSRF_COOKIE, LOCKOUT_AFTER, LOCKOUT_FOR, ROLES, SESSION_IDLE, _allowed_origin, audit,
                        check_password_policy, clear_session_cookies, client_ip, create_session, current,
                        hash_password, new_recovery_codes, new_totp_secret, now, rate_limit, resolve, revoke_sessions,
                        role_rank, set_session_cookies, sha256, sign, totp_uri, unsign, verify_password, verify_totp)

router = APIRouter(prefix="/api/auth", tags=["auth"])
log = logging.getLogger("debbuilder.auth")

CODE_TTL = timedelta(minutes=10)
CODE_ATTEMPTS = 5
RESEND_AFTER = timedelta(seconds=30)
MAX_SENDS = 5


def user_json(u: User, role: str | None = None) -> dict:
    return {"id": str(u.id), "email": u.email, "name": u.name, "role": role or u.role, "mfa_enabled": u.mfa_enabled,
            "must_change_password": u.must_change_password, "disabled": u.disabled,
            "last_login_at": u.last_login_at, "created_at": u.created_at,
            "recovery_codes_left": len(u.recovery_codes or [])}


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
    return {"setup_required": n == 0, "user": user_json(p.user, p.role) if p else None}


class SetupIn(BaseModel):
    email: str
    name: str = ""
    password: str


@router.post("/setup")
async def setup(body: SetupIn, request: Request):
    """Create the owner account. Only possible while no user exists."""
    _same_origin(request)
    rate_limit(f"setup:{client_ip(request)}", 5, 60)
    if not get_settings().allow_remote_setup and client_ip(request) not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(403, "Create the owner account from the server itself (http://localhost), "
                                 "or set ALLOW_REMOTE_SETUP=true in backend/.env for the first setup.")
    email = body.email.strip().lower()
    if "@" not in email or len(email) > 320:
        raise HTTPException(400, "Enter a valid email address")
    check_password_policy(body.password, email, body.name)
    async with SessionLocal() as s:
        # Serialise concurrent setups so two "first" owners can't be created.
        await s.execute(select(func.pg_advisory_xact_lock(4242)))
        if (await s.execute(select(func.count(User.id)))).scalar_one():
            raise HTTPException(409, "Setup is already complete. Sign in instead.")
        u = User(email=email, name=body.name.strip()[:200], password_hash=hash_password(body.password), role="owner")
        s.add(u)
        await s.commit()
        await s.refresh(u)
    await audit("auth.setup", request, user=u, status=200)
    return await _start_challenge(u, request)


class LoginIn(BaseModel):
    email: str
    password: str


@router.post("/login")
async def login(body: LoginIn, request: Request):
    _same_origin(request)
    ip = client_ip(request)
    rate_limit(f"login:ip:{ip}", get_settings().login_rate_limit, 60)
    email = body.email.strip().lower()
    rate_limit(f"login:user:{email}", 20, 300)
    async with SessionLocal() as s:
        u = (await s.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if u and u.locked_until and u.locked_until > now():
            await audit("auth.login_locked", request, user=u, status=423)
            raise HTTPException(423, "Too many failed attempts. The account is locked for a few minutes.")
        # verify_password runs the hash even for unknown emails, so timing doesn't reveal accounts.
        ok = verify_password(u.password_hash if u else None, body.password)
        if not ok or not u or u.disabled:
            if u:
                u.failed_logins += 1
                if u.failed_logins >= LOCKOUT_AFTER:
                    u.locked_until, u.failed_logins = now() + LOCKOUT_FOR, 0
                await s.commit()
            await audit("auth.login_failed", request, user=u, status=401, email=email, disabled=bool(u and u.disabled))
            raise HTTPException(401, "Wrong email or password")
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


async def _deliver(u: User, code: str, request: Request) -> str:
    """Email the code. Returns how it was delivered: "email" or "console" (dev fallback)."""
    subject, text, html = mailer.code_email(code, client_ip(request), int(CODE_TTL.total_seconds() // 60))
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
    log.warning("\n%s\n  EMAIL NOT CONFIGURED (set SMTP_HOST in backend/.env).\n  Sign-in code for %s: %s  (valid %d min)\n%s",
                "=" * 64, u.email, code, CODE_TTL.total_seconds() // 60, "=" * 64)
    return "console"


async def _start_challenge(u: User, request: Request) -> dict:
    code = f"{secrets.randbelow(1_000_000):06d}"
    async with SessionLocal() as s:
        ch = LoginChallenge(user_id=u.id, code_hash="", expires_at=now() + CODE_TTL, ip=client_ip(request))
        s.add(ch)
        await s.flush()
        ch.code_hash = _code_hash(ch.id, code)
        await s.commit()
    delivery = await _deliver(u, code, request)
    await audit("auth.code_sent", request, user=u, status=200, via=delivery)
    methods = ["email"] + (["totp"] if u.mfa_enabled else []) + (["recovery"] if u.recovery_codes else [])
    return {"verification_required": True, "challenge": sign({"cid": str(ch.id), "uid": str(u.id)}, ttl=int(CODE_TTL.total_seconds())),
            "sent_to": _mask(u.email), "delivery": delivery, "methods": methods,
            "resend_in": int(RESEND_AFTER.total_seconds())}


async def _load_challenge(token: str, s) -> tuple[LoginChallenge, User]:
    data = unsign(token)
    if not data:
        raise HTTPException(401, "This sign-in attempt expired. Start again.")
    ch = await s.get(LoginChallenge, uuid.UUID(data["cid"]))
    u = await s.get(User, uuid.UUID(data["uid"]))
    if not ch or not u or ch.user_id != u.id or ch.consumed or ch.expires_at < now() or u.disabled:
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
            await s.commit()
            await audit("auth.code_failed", request, user=u, status=401, method=body.method, attempts=ch.attempts)
            if ch.consumed:
                raise HTTPException(401, "Too many wrong codes. Sign in again to get a new one.")
            raise HTTPException(401, f"Wrong code. {CODE_ATTEMPTS - ch.attempts} attempt{'s' if CODE_ATTEMPTS - ch.attempts != 1 else ''} left.")
        ch.consumed = True
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
    delivery = await _deliver(u, code, request)
    await audit("auth.code_resent", request, user=u, status=200, via=delivery)
    return {"ok": True, "delivery": delivery, "resend_in": int(RESEND_AFTER.total_seconds())}


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
    name: str


@router.put("/me")
async def update_me(body: ProfileIn, request: Request):
    p = current(request)
    async with SessionLocal() as s:
        u = await s.get(User, p.user.id)
        u.name = body.name.strip()[:200]
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
        if not verify_password(u.password_hash, body.current_password):
            await audit("auth.password_change_failed", request, p, status=401)
            raise HTTPException(401, "Current password is wrong")
        if verify_password(u.password_hash, body.new_password):
            raise HTTPException(400, "Choose a password different from the current one")
        check_password_policy(body.new_password, u.email, u.name)
        u.password_hash, u.must_change_password, u.password_changed_at = hash_password(body.new_password), False, now()
        await s.commit()
    await revoke_sessions(p.user.id, except_id=p.session_id)  # sign out everywhere else
    await audit("auth.password_changed", request, p, status=200)
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
        if not verify_password(u.password_hash, body.password):
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
        x = await s.get(AuthSession, uuid.UUID(sid))
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
        k = await s.get(ApiKey, uuid.UUID(kid))
        if not k or k.user_id != p.user.id:
            raise HTTPException(404, "Key not found")
        k.revoked = True
        await s.commit()
    await audit("auth.api_key_revoked", request, p, status=200, key=k.prefix)
    return {"ok": True}


__all__ = ["router", "user_json", "CSRF_COOKIE"]
