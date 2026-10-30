"""Authentication and authorization.

- Passwords: Argon2id, length + blocklist policy, account lockout and per-IP throttling.
- Sessions: random 256-bit token in an HttpOnly + SameSite=Strict cookie; only its SHA-256 is
  stored. Idle + absolute expiry, server-side revocation.
- CSRF: double-submit token (readable `db_csrf` cookie echoed in `X-CSRF-Token`) plus an
  Origin check on every state-changing request made with a session cookie.
- API keys: `dbk_<prefix>_<secret>` in `X-API-Key` or `Authorization: Bearer`; hashed, expiring,
  role capped at the creator's.
- MFA: TOTP (RFC 6238) with replay protection and one-time recovery codes.
- Authorization: roles owner > admin > editor > viewer mapped to permissions, enforced by
  one route policy table. Anything not listed is denied (default deny).
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
import struct
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request
from sqlalchemy import select, update
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .config import get_settings
from .db import ApiKey, AuditLog, AuthSession, SessionLocal, User

SESSION_COOKIE = "db_session"
CSRF_COOKIE = "db_csrf"
SESSION_IDLE = timedelta(hours=12)
SESSION_ABSOLUTE = timedelta(days=7)
LOCKOUT_AFTER = 5
LOCKOUT_FOR = timedelta(minutes=15)

_ph = PasswordHasher()  # argon2id, library defaults (time_cost=3, memory 64 MiB)
_DUMMY_HASH = _ph.hash(secrets.token_urlsafe(16))


def now() -> datetime:
    return datetime.now(timezone.utc)


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

_COMMON = {
    "password", "password1", "password123", "123456789012", "qwertyuiop", "letmein", "welcome", "iloveyou",
    "admin", "administrator", "changeme", "passw0rd", "p@ssw0rd", "qwerty123", "1q2w3e4r5t", "abc123456789",
    "trustno1", "football", "baseball", "dragon", "monkey", "sunshine", "princess", "debbuilder",
}


def password_problems(pw: str, email: str = "", name: str = "") -> list[str]:
    """NIST 800-63B style: length and blocklist, no arbitrary composition rules."""
    out = []
    if len(pw) < 12:
        out.append("at least 12 characters")
    if len(pw) > 128:
        out.append("at most 128 characters")
    low = pw.lower()
    if low in _COMMON or any(low.startswith(c) and len(low) - len(c) < 4 for c in _COMMON):
        out.append("not a common password")
    local = email.split("@")[0].lower()
    if len(local) >= 4 and local in low:
        out.append("must not contain your email name")
    if name and len(name) >= 4 and name.lower().replace(" ", "") in low.replace(" ", ""):
        out.append("must not contain your name")
    if len(set(pw)) < 5:
        out.append("too many repeated characters")
    return out


_USERNAME = re.compile(r"[a-z][a-z0-9._-]{2,31}")


def clean_username(raw: str | None) -> str | None:
    """Optional sign-in name: 3–32 chars, starts with a letter, a-z 0-9 . _ - (never contains @, so it can't look like an email)."""
    u = (raw or "").strip().lower()
    if not u:
        return None
    if not _USERNAME.fullmatch(u):
        raise HTTPException(400, "Username must be 3–32 characters: letters, digits, dot, dash or underscore, starting with a letter")
    return u


def check_password_policy(pw: str, email: str = "", name: str = "") -> None:
    problems = password_problems(pw, email, name)
    if problems:
        raise HTTPException(400, "Password must be " + ", ".join(problems))


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(stored: str | None, pw: str) -> bool:
    try:
        return _ph.verify(stored or _DUMMY_HASH, pw) and stored is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def temp_password() -> str:
    return secrets.token_urlsafe(15)  # ~20 chars, ~120 bits


# ---------------------------------------------------------------------------
# Signed short-lived tokens (MFA step, OAuth state …) — HMAC over JSON
# ---------------------------------------------------------------------------

def _signing_key() -> bytes:
    return hashlib.sha256(b"debbuilder-auth-v1:" + get_settings().encryption_key.encode()).digest()


def sign(payload: dict, ttl: int) -> str:
    body = base64.urlsafe_b64encode(json.dumps({**payload, "exp": int(time.time()) + ttl}).encode()).decode().rstrip("=")
    sig = base64.urlsafe_b64encode(hmac.new(_signing_key(), body.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    return f"{body}.{sig}"


def unsign(token: str) -> dict | None:
    try:
        body, sig = token.split(".")
        good = base64.urlsafe_b64encode(hmac.new(_signing_key(), body.encode(), hashlib.sha256).digest()).decode().rstrip("=")
        if not hmac.compare_digest(sig, good):
            return None
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return data if data.get("exp", 0) >= time.time() else None
    except (ValueError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# TOTP (RFC 6238, SHA-1, 6 digits, 30 s) + recovery codes
# ---------------------------------------------------------------------------

def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def verify_totp(secret: str, code: str, last_step: int, window: int = 1) -> int | None:
    """Return the matched time step (must be > last_step: a code works once), else None."""
    code = re.sub(r"\s", "", code or "")
    if not re.fullmatch(r"\d{6}", code):
        return None
    step = int(time.time()) // 30
    for s in range(step - window, step + window + 1):
        if s > last_step and hmac.compare_digest(_hotp(secret, s), code):
            return s
    return None


def totp_uri(secret: str, email: str) -> str:
    from urllib.parse import quote
    return f"otpauth://totp/DebBuilder:{quote(email)}?secret={secret}&issuer=DebBuilder&algorithm=SHA1&digits=6&period=30"


def new_recovery_codes(n: int = 10) -> tuple[list[str], list[str]]:
    codes = [f"{secrets.token_hex(3)}-{secrets.token_hex(3)}" for _ in range(n)]
    return codes, [sha256(c) for c in codes]


# ---------------------------------------------------------------------------
# Rate limiting (in-memory sliding window; per process)
# ---------------------------------------------------------------------------

_hits: dict[str, deque] = defaultdict(deque)


def rate_limit(key: str, limit: int, per: float) -> None:
    q = _hits[key]
    t = time.monotonic()
    while q and q[0] < t - per:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, "Too many attempts. Wait a minute and try again.", headers={"Retry-After": str(int(per))})
    q.append(t)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


# ---------------------------------------------------------------------------
# Roles & permissions
# ---------------------------------------------------------------------------

ROLES = ["viewer", "editor", "admin", "owner"]
_VIEW = {"workflow:read", "execution:read", "credential:read"}
_EDIT = _VIEW | {"workflow:write", "workflow:execute", "execution:write", "credential:write"}
_ADMIN = _EDIT | {"user:manage", "audit:read"}
PERMISSIONS = {"viewer": _VIEW, "editor": _EDIT, "admin": _ADMIN, "owner": _ADMIN | {"owner"}}


def role_rank(role: str) -> int:
    return ROLES.index(role) if role in ROLES else -1


def has_perm(role: str, perm: str) -> bool:
    return perm in PERMISSIONS.get(role, set())


# Route policy. First match wins; "public" = no authentication, "optional" = authenticate if
# credentials are present but let the handler decide, "self" = any signed-in user.
R = "GET|HEAD"
W = "POST|PUT|PATCH|DELETE"
POLICY: list[tuple[str, str, str]] = [
    # public
    (R, r"/api/health", "public"),
    (R, r"/api/auth/status", "public"),
    ("POST", r"/api/auth/(setup|signup|login|login/verify|login/resend|logout|password/forgot|password/reset/verify|password/reset)", "public"),
    ("*", r"/webhook/.+", "public"),                      # production webhooks (active workflows only)
    ("GET|POST", r"/form/[^/]+", "public"),               # hosted forms are public by design
    ("GET|POST", r"/api/whatsapp/webhook", "public"),     # verified by Meta signature / verify token
    (R, r"/api/oauth2/callback", "public"),               # verified by the signed state
    ("GET|POST", r"/api/voice/(config/[^/]+|turn|resolve|event)", "public"),  # verified by INTERNAL_TOKEN
    ("POST", r"/api/chat/[^/]+", "optional"),             # public only if the Chat Trigger allows it
    # signed-in user, own account
    ("*", r"/api/auth/.*", "self"),
    # admin
    ("*", r"/api/users(/.*)?", "user:manage"),
    (R, r"/api/audit", "audit:read"),
    # workflows, folders, templates
    (R, r"/api/(node-types|code-runtimes|templates)", "workflow:read"),
    (R, r"/api/(workflows|folders)(/.*)?", "workflow:read"),
    ("POST", r"/api/workflows/[^/]+/run", "workflow:execute"),
    ("POST", r"/api/voice/session", "workflow:execute"),
    (R, r"/api/voice/telephony/[^/]+", "credential:read"),
    (W, r"/api/voice/telephony(/.*)?", "credential:write"),
    ("*", r"/webhook-test/.+", "workflow:execute"),
    (W, r"/api/(workflows|folders|templates)(/.*)?", "workflow:write"),
    # executions
    (R, r"/api/executions(/.*)?", "execution:read"),
    (R, r"/api/binary/[0-9a-f]{32}", "execution:read"),
    ("POST", r"/api/executions/(stop|[^/]+/(resume|cancel|retry))", "workflow:execute"),
    (W, r"/api/executions(/.*)?", "execution:write"),
    # credentials
    (R, r"/api/credentials(/.*)?", "credential:read"),
    (W, r"/api/credentials(/.*)?", "credential:write"),
    (R, r"/api/oauth2/auth-url", "credential:write"),
    # live events
    (R, r"/ws/.+", "workflow:read"),
    # API docs
    (R, r"/(docs|redoc|openapi\.json)(/.*)?", "self"),
]
_COMPILED = [(set(m.split("|")) if m != "*" else None, re.compile(rf"^{p}$"), need) for m, p, need in POLICY]


def required(method: str, path: str) -> str | None:
    """Permission needed for a request; None = not an API route (static frontend)."""
    for methods, rx, need in _COMPILED:
        if (methods is None or method in methods) and rx.match(path):
            return need
    if path.startswith(("/api/", "/ws/", "/webhook")):
        return "deny"  # default deny for anything not in the policy
    return None


# ---------------------------------------------------------------------------
# Resolving the caller
# ---------------------------------------------------------------------------

class Principal:
    def __init__(self, user: User, role: str, via: str, session_id: uuid.UUID | None = None, key_id: uuid.UUID | None = None):
        self.user, self.role, self.via, self.session_id, self.key_id = user, role, via, session_id, key_id

    def can(self, perm: str) -> bool:
        return perm == "self" or has_perm(self.role, perm)


async def resolve(request) -> Principal | None:
    """From an API key header or the session cookie (HTTP request or WebSocket)."""
    headers = request.headers
    key = headers.get("x-api-key") or ""
    auth = headers.get("authorization") or ""
    if not key and auth.lower().startswith("bearer dbk_"):
        key = auth[7:].strip()
    async with SessionLocal() as s:
        if key:
            # dbk_<hex prefix>_<urlsafe secret>; the secret itself may contain "_".
            parts = key.split("_", 2)
            if len(parts) != 3 or parts[0] != "dbk" or not parts[1]:
                return None
            ak = (await s.execute(select(ApiKey).where(ApiKey.prefix == parts[1]))).scalar_one_or_none()
            if not ak or ak.revoked or (ak.expires_at and ak.expires_at < now()) or not hmac.compare_digest(ak.key_hash, sha256(key)):
                return None
            user = await s.get(User, ak.user_id)
            if not user or user.disabled:
                return None
            if not ak.last_used_at or now() - ak.last_used_at > timedelta(minutes=1):
                ak.last_used_at = now()
                await s.commit()
            # The key can't exceed the owner's *current* role either.
            role = ak.role if role_rank(ak.role) <= role_rank(user.role) else user.role
            return Principal(user, role, "api_key", key_id=ak.id)
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        sess = (await s.execute(select(AuthSession).where(AuthSession.token_hash == sha256(token)))).scalar_one_or_none()
        t = now()
        if not sess or sess.revoked or sess.expires_at < t or sess.last_seen_at + SESSION_IDLE < t:
            return None
        user = await s.get(User, sess.user_id)
        if not user or user.disabled:
            return None
        if t - sess.last_seen_at > timedelta(minutes=1):
            sess.last_seen_at = t
            await s.commit()
        return Principal(user, user.role, "session", session_id=sess.id)


async def create_session(user: User, request: Request) -> str:
    token = secrets.token_urlsafe(32)
    async with SessionLocal() as s:
        s.add(AuthSession(token_hash=sha256(token), user_id=user.id, expires_at=now() + SESSION_ABSOLUTE,
                          ip=client_ip(request), user_agent=(request.headers.get("user-agent") or "")[:300]))
        await s.commit()
    return token


def set_session_cookies(resp, token: str) -> None:
    secure = get_settings().public_base_url.startswith("https")
    max_age = int(SESSION_ABSOLUTE.total_seconds())
    resp.set_cookie(SESSION_COOKIE, token, max_age=max_age, httponly=True, samesite="strict", secure=secure, path="/")
    resp.set_cookie(CSRF_COOKIE, secrets.token_urlsafe(24), max_age=max_age, httponly=False, samesite="strict", secure=secure, path="/")


def clear_session_cookies(resp) -> None:
    resp.delete_cookie(SESSION_COOKIE, path="/")
    resp.delete_cookie(CSRF_COOKIE, path="/")


async def revoke_sessions(user_id, except_id=None) -> None:
    async with SessionLocal() as s:
        q = update(AuthSession).where(AuthSession.user_id == user_id, AuthSession.revoked.is_(False))
        if except_id:
            q = q.where(AuthSession.id != except_id)
        await s.execute(q.values(revoked=True))
        await s.commit()


async def audit(action: str, request=None, principal: Principal | None = None, *, user: User | None = None,
                status: int = 0, **detail) -> None:
    u = principal.user if principal else user
    try:
        async with SessionLocal() as s:
            s.add(AuditLog(user_id=u.id if u else None, user_email=u.email if u else str(detail.pop("email", "")),
                           action=action, method=request.method if request is not None else "",
                           path=str(request.url.path)[:500] if request is not None else "", status=status,
                           ip=client_ip(request) if request is not None else "", detail=detail))
            await s.commit()
    except Exception:  # noqa: BLE001 — auditing must never break the request
        pass


def current(request: Request) -> Principal:
    p = getattr(request.state, "principal", None)
    if p is None:
        raise HTTPException(401, "Sign in required")
    return p


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
# Account and user endpoints write their own specific audit events; chat/WhatsApp/voice are
# high-volume conversation traffic, not configuration changes.
_MUTATING_AUDIT_SKIP = re.compile(r"^/api/(auth/|users|chat/|whatsapp/|voice/turn)")


def _allowed_origin(request: Request, origin: str) -> bool:
    if not origin:
        return True
    allowed = {o.strip().rstrip("/") for o in get_settings().cors_origins.split(",") if o.strip()}
    allowed.add(get_settings().public_base_url.rstrip("/"))
    host = request.headers.get("host", "")
    return origin.rstrip("/") in allowed or origin.split("://", 1)[-1] == host


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path, method = request.url.path, request.method
        if method == "OPTIONS":
            return await call_next(request)
        need = required(method, path)
        request.state.principal = None
        if need is None or need == "public":
            return _secure_headers(await call_next(request), path)

        principal = await resolve(request)
        request.state.principal = principal
        if principal is None and need != "optional":
            return JSONResponse({"detail": "Sign in required"}, status_code=401)
        if principal is not None:
            if principal.via == "session" and method in _UNSAFE:
                # CSRF: same-origin + double-submit token.
                if not _allowed_origin(request, request.headers.get("origin") or ""):
                    return JSONResponse({"detail": "Cross-origin request blocked"}, status_code=403)
                sent, cookie = request.headers.get("x-csrf-token") or "", request.cookies.get(CSRF_COOKIE) or ""
                if not cookie or not hmac.compare_digest(sent, cookie):
                    return JSONResponse({"detail": "Missing or invalid CSRF token. Reload the page."}, status_code=403)
            if principal.user.must_change_password and not path.startswith("/api/auth/"):
                return JSONResponse({"detail": "password_change_required"}, status_code=403)
            if need == "deny" or (need not in ("self", "optional") and not principal.can(need)):
                await audit("access.denied", request, principal, status=403, need=need)
                return JSONResponse({"detail": f"Your role ({principal.role}) doesn't allow this"}, status_code=403)

        response = await call_next(request)
        if principal is not None and method in _UNSAFE and not _MUTATING_AUDIT_SKIP.match(path):
            await audit(f"api.{method.lower()}", request, principal, status=response.status_code, via=principal.via)
        return _secure_headers(response, path)


_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; img-src 'self' data: blob: https:; "
        "connect-src 'self' ws: wss: https:; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


def _secure_headers(resp, path: str):
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy", "camera=(), geolocation=(), microphone=(self)")
    h.setdefault("X-Frame-Options", "DENY")
    if get_settings().public_base_url.startswith("https"):
        h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if not path.startswith(("/api/", "/webhook", "/ws/", "/form/")) and h.get("content-type", "").startswith("text/html"):
        h.setdefault("Content-Security-Policy", _CSP)
    if path.startswith("/api/"):
        h.setdefault("Cache-Control", "no-store")
    return resp
