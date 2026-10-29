"""System email (sign-in codes) over the SMTP server configured in backend/.env."""

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from ..config import get_settings

log = logging.getLogger("debbuilder.mail")


class MailError(Exception):
    pass


def configured() -> bool:
    return bool(get_settings().smtp_host)


def _send(to: str, subject: str, text: str, html: str) -> None:
    st = get_settings()
    msg = EmailMessage()
    sender = st.mail_from or st.smtp_user or "debbuilder@localhost"
    msg["From"] = formataddr(("DebBuilder", sender)) if "<" not in sender else sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1].strip("> ") or "localhost")
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    ctx = ssl.create_default_context()
    try:
        if st.smtp_security == "ssl":
            srv = smtplib.SMTP_SSL(st.smtp_host, st.smtp_port or 465, timeout=20, context=ctx)
        else:
            srv = smtplib.SMTP(st.smtp_host, st.smtp_port or 587, timeout=20)
            if st.smtp_security == "starttls":
                srv.starttls(context=ctx)
        with srv:
            if st.smtp_user:
                srv.login(st.smtp_user, st.smtp_password)
            srv.send_message(msg)
    except (OSError, smtplib.SMTPException) as e:
        raise MailError(f"{type(e).__name__}: {e}") from e


async def send(to: str, subject: str, text: str, html: str) -> None:
    await asyncio.to_thread(_send, to, subject, text, html)


def code_email(code: str, ip: str, minutes: int, purpose: str = "sign in") -> tuple[str, str, str]:
    subject = f"{code} is your DebBuilder verification code"
    text = (f"Your DebBuilder code to {purpose} is: {code}\n\n"
            f"It expires in {minutes} minutes and works once.\n"
            f"Request from IP {ip or 'unknown'}.\n\n"
            "If you didn't try to sign in, someone may know your password: change it now.")
    html = f"""<div style="font-family:Segoe UI,Arial,sans-serif;max-width:480px;margin:auto;padding:24px;color:#1f1f1f">
  <h2 style="margin:0 0 12px">Your verification code</h2>
  <p style="margin:0 0 16px">Use this code to {purpose} to DebBuilder:</p>
  <div style="font-size:32px;font-weight:700;letter-spacing:8px;background:#f2f2f2;border-radius:8px;padding:14px;text-align:center">{code}</div>
  <p style="color:#555;font-size:13px;margin:16px 0 0">It expires in {minutes} minutes and works once. Request from IP {ip or 'unknown'}.</p>
  <p style="color:#555;font-size:13px">If you didn't try to sign in, someone may know your password: change it now.</p>
</div>"""
    return subject, text, html
