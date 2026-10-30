"""Account recovery from the server's shell (needs access to the machine and backend/.env).

  uv run python -m app.cli list-users
  uv run python -m app.cli reset-password owner@example.com   # prints a temporary password
  uv run python -m app.cli disable-mfa owner@example.com
  uv run python -m app.cli test-email you@example.com         # checks the SMTP settings
"""

import asyncio
import sys

from sqlalchemy import select


async def main(argv: list[str]) -> int:
    from .db import User, init_db, SessionLocal
    from .security import hash_password, revoke_sessions, temp_password

    if argv[:1] == ["test-email"] and len(argv) > 1:
        from .config import get_settings
        from .services import mailer
        st = get_settings()
        if not mailer.configured():
            print("SMTP_HOST isn't set in backend/.env")
            return 1
        print(f"Sending via {st.smtp_host}:{st.smtp_port} ({st.smtp_security}) as {st.smtp_user or '(no login)'} ...")
        try:
            await mailer.send(argv[1], *mailer.sign_in_email("123456", to=argv[1], name="", ip="127.0.0.1",
                                                            user_agent="DebBuilder CLI test", minutes=10))
        except mailer.MailError as e:
            print(f"Failed: {e}")
            if "535" in str(e) or "Username and Password" in str(e):
                print("Gmail rejected the login: SMTP_USER must be the full Gmail address and SMTP_PASSWORD the app password "
                      "(not your normal password), with 2-Step Verification on.")
            return 1
        print(f"Sent. Check the inbox (and spam) of {argv[1]}.")
        return 0
    await init_db()
    if not argv or argv[0] not in ("list-users", "reset-password", "disable-mfa") or (argv[0] != "list-users" and len(argv) < 2):
        print(__doc__)
        return 2
    async with SessionLocal() as s:
        if argv[0] == "list-users":
            for u in (await s.execute(select(User).order_by(User.created_at))).scalars():
                print(f"{u.email:40} {u.role:7} mfa={'on' if u.mfa_enabled else 'off':3} {'DISABLED' if u.disabled else ''}")
            return 0
        u = (await s.execute(select(User).where(User.email == argv[1].strip().lower()))).scalar_one_or_none()
        if not u:
            print(f"No user {argv[1]}")
            return 1
        if argv[0] == "reset-password":
            pw = temp_password()
            u.password_hash, u.must_change_password, u.failed_logins, u.locked_until, u.disabled = hash_password(pw), True, 0, None, False
            await s.commit()
            await revoke_sessions(u.id)
            print(f"Temporary password for {u.email}: {pw}\n(They must choose a new one at sign-in.)")
        else:
            u.mfa_enabled, u.mfa_secret, u.recovery_codes = False, None, []
            await s.commit()
            await revoke_sessions(u.id)
            print(f"Two-factor authentication turned off for {u.email}.")
    from .security import audit
    await audit(f"cli.{argv[0]}", user=u, status=200)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:]), loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None))
