"""System email (sign-in codes, password reset, security notices) over the SMTP server configured in backend/.env."""

import asyncio
import logging
import re
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from html import escape

from ..config import get_settings

log = logging.getLogger("debbuilder.mail")

PRODUCT = "DebBuilder"
BRAND = "#2DD4BF"
ON_BRAND = "#042F2E"  # text on the brand colour
LINK = "#0F766E"  # brand-hued text on white (readable)
INK = "#1f2328"
MUTED = "#57606a"
LINE = "#e4e7eb"


class MailError(Exception):
    pass


def configured() -> bool:
    return bool(get_settings().smtp_host)


def _send(to: str, subject: str, text: str, html: str) -> None:
    st = get_settings()
    msg = EmailMessage()
    sender = st.mail_from or st.smtp_user or "debbuilder@localhost"
    msg["From"] = formataddr((PRODUCT, sender)) if "<" not in sender else sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1].strip("> ") or "localhost")
    # Mark as machine-generated so mail servers don't send out-of-office replies to it.
    msg["Auto-Submitted"] = "auto-generated"
    msg["X-Auto-Response-Suppress"] = "All"
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
                pw = st.smtp_password
                if st.smtp_host.lower().endswith("gmail.com"):
                    pw = pw.replace(" ", "")  # Google shows app passwords as "abcd efgh ijkl mnop"
                srv.login(st.smtp_user, pw)
            srv.send_message(msg)
    except (OSError, smtplib.SMTPException) as e:
        raise MailError(f"{type(e).__name__}: {e}") from e


async def send(to: str, subject: str, text: str, html: str) -> None:
    await asyncio.to_thread(_send, to, subject, text, html)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def describe_device(ua: str) -> str:
    """'Chrome on Windows' from a User-Agent string (best effort)."""
    ua = ua or ""
    browser = next((n for n, pat in (("Edge", r"Edg/"), ("Opera", r"OPR/"), ("Chrome", r"Chrome/"), ("Firefox", r"Firefox/"),
                                     ("Safari", r"Safari/")) if re.search(pat, ua)), "")
    os_ = next((n for n, pat in (("Windows", r"Windows"), ("Android", r"Android"), ("iPhone", r"iPhone|iPad"),
                                 ("macOS", r"Mac OS X"), ("Linux", r"Linux")) if re.search(pat, ua)), "")
    if browser and os_:
        return f"{browser} on {os_}"
    return browser or os_ or ("API client" if ua else "Unknown device")


def _when() -> str:
    t = datetime.now().astimezone()
    off = t.strftime("%z")  # +0530
    return f"{t.strftime('%d %b %Y, %H:%M')} (UTC{off[:3]}:{off[3:]})"


def _layout(*, preheader: str, heading: str, greeting: str, intro: str, code: str | None = None, after: str = "",
            details: list[tuple[str, str]] | None = None, notice: str = "", to: str, reason: str) -> str:
    base = get_settings().public_base_url.rstrip("/")
    p = f'style="margin:0 0 16px;font-size:15px;line-height:24px;color:{INK}"'
    code_html = ""
    if code:
        code_html = f"""
          <tr><td style="padding:4px 0 20px">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr><td align="center" style="background:#f4f6f8;border:1px solid {LINE};border-radius:10px;padding:20px 12px">
                <div style="font-family:Consolas,'SFMono-Regular',Menlo,Monaco,monospace;font-size:34px;line-height:40px;font-weight:700;letter-spacing:10px;color:{INK};padding-left:10px">{escape(code)}</div>
              </td></tr>
            </table>
          </td></tr>"""
    details_html = ""
    if details:
        rows = "".join(
            f'<tr><td style="padding:6px 0;font-size:13px;color:{MUTED};width:110px;vertical-align:top">{escape(k)}</td>'
            f'<td style="padding:6px 0;font-size:13px;color:{INK};vertical-align:top">{escape(v)}</td></tr>' for k, v in details)
        details_html = f"""
          <tr><td style="padding:4px 0 8px">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-top:1px solid {LINE};border-bottom:1px solid {LINE}">
              <tr><td style="padding:10px 0"><table role="presentation" cellpadding="0" cellspacing="0" border="0">{rows}</table></td></tr>
            </table>
          </td></tr>"""
    notice_html = ""
    if notice:
        notice_html = f"""
          <tr><td style="padding:16px 0 0">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr><td style="background:#fff8e6;border-left:3px solid #f59e0b;border-radius:4px;padding:12px 14px;font-size:13px;line-height:20px;color:{INK}">{notice}</td></tr>
            </table>
          </td></tr>"""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light"><title>{escape(heading)}</title></head>
<body style="margin:0;padding:0;background:#f0f2f5;-webkit-text-size-adjust:100%">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent">{escape(preheader)}&#8199;&#65279;&#847;&#8199;&#65279;&#847;&#8199;&#65279;&#847;</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f0f2f5">
  <tr><td align="center" style="padding:32px 16px">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif">
      <tr><td style="padding:0 4px 18px">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
          <td style="background:{BRAND};border-radius:8px;width:32px;height:32px;text-align:center;vertical-align:middle;font-size:17px;font-weight:700;color:{ON_BRAND}">D</td>
          <td style="padding-left:10px;font-size:18px;font-weight:700;color:{INK};letter-spacing:-0.2px">{PRODUCT}</td>
        </tr></table>
      </td></tr>
      <tr><td style="background:#ffffff;border:1px solid {LINE};border-radius:12px;border-top:4px solid {BRAND}">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr><td style="padding:32px 36px 28px">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr><td><h1 style="margin:0 0 20px;font-size:22px;line-height:30px;font-weight:700;color:{INK}">{escape(heading)}</h1></td></tr>
              <tr><td><p {p}>{greeting}</p><p {p}>{intro}</p></td></tr>{code_html}
              <tr><td>{f'<p {p}>{after}</p>' if after else ''}</td></tr>{details_html}{notice_html}
              <tr><td style="padding-top:24px"><p style="margin:0;font-size:15px;line-height:24px;color:{INK}">Thanks,<br>The {PRODUCT} team</p></td></tr>
            </table>
          </td></tr>
        </table>
      </td></tr>
      <tr><td style="padding:20px 8px 0;font-size:12px;line-height:18px;color:{MUTED};text-align:center">
        This email was sent to <span style="color:{INK}">{escape(to)}</span> {escape(reason)}.<br>
        It's an automated message, so replies aren't read.{f' Never share this code with anyone, including {PRODUCT} staff.' if code else ''}<br>
        <a href="{escape(base)}" style="color:{LINK};text-decoration:none">{escape(base.split('://')[-1])}</a> &nbsp;·&nbsp; &copy; {datetime.now().year} {PRODUCT}
      </td></tr>
    </table>
  </td></tr>
</table>
</body></html>"""


def _text(*, heading: str, greeting: str, intro: str, code: str | None = None, after: str = "",
          details: list[tuple[str, str]] | None = None, notice: str = "", to: str, reason: str) -> str:
    strip = lambda s: re.sub(r"<[^>]+>", "", s).replace("&nbsp;", " ")  # noqa: E731
    out = [heading, "=" * len(heading), "", strip(greeting), "", strip(intro), ""]
    if code:
        out += [f"    {code}", ""]
    if after:
        out += [strip(after), ""]
    if details:
        out += [f"{k + ':':<10} {v}" for k, v in details] + [""]
    if notice:
        out += [strip(notice), ""]
    out += ["Thanks,", f"The {PRODUCT} team", "", "--",
            f"This email was sent to {to} {reason}. It's an automated message, so replies aren't read.",
            get_settings().public_base_url]
    return "\n".join(out)


def _render(subject: str, **kw) -> tuple[str, str, str]:
    return subject, _text(**{k: v for k, v in kw.items() if k != "preheader"}), _layout(**kw)


def _hi(name: str) -> str:
    return f"Hi {escape(name.split()[0])}," if name.strip() else "Hello,"


def sign_in_email(code: str, *, to: str, name: str, ip: str, user_agent: str, minutes: int, first: bool = False,
                  owner: bool = True) -> tuple[str, str, str]:
    if first:  # owner setup or sign-up: the same code also confirms the address works
        return _render(
            f"{code} is your {PRODUCT} verification code",
            preheader=f"Confirm your email to finish setting up {PRODUCT}. The code expires in {minutes} minutes.",
            heading="Confirm your email address", greeting=_hi(name),
            intro=f"Welcome to {PRODUCT}. Enter this code to confirm your email address and finish creating "
                  f"{'the owner account' if owner else 'your account'}:",
            code=code, after=f"The code expires in <b>{minutes} minutes</b> and can be used once.",
            details=[("Requested", _when()), ("Device", describe_device(user_agent)), ("IP address", ip or "unknown")],
            notice=f"Didn't {'set up' if owner else 'sign up for'} {PRODUCT}? You can ignore this email; nothing happens without the code.",
            to=to, reason=f"because it was used to {'set up' if owner else 'sign up for'} {PRODUCT}")
    return _render(
        f"{code} is your {PRODUCT} sign-in code",
        preheader=f"Use this code to finish signing in. It expires in {minutes} minutes.",
        heading="Your sign-in code", greeting=_hi(name),
        intro=f"Someone entered your password to sign in to {PRODUCT}. Enter this code to finish signing in:",
        code=code, after=f"The code expires in <b>{minutes} minutes</b> and can be used once.",
        details=[("Requested", _when()), ("Device", describe_device(user_agent)), ("IP address", ip or "unknown")],
        notice="<b>Wasn't you?</b> Someone may know your password. Don't share this code, and change your password "
               "in Settings &rarr; Account, or use <i>Forgot password</i> on the sign-in page.",
        to=to, reason=f"because of a sign-in to your {PRODUCT} account")


def reset_email(code: str, *, to: str, name: str, ip: str, user_agent: str, minutes: int) -> tuple[str, str, str]:
    return _render(
        f"Reset your {PRODUCT} password",
        preheader=f"Your password reset code is {code}. It expires in {minutes} minutes.",
        heading="Reset your password", greeting=_hi(name),
        intro=f"We received a request to reset the password for your {PRODUCT} account. Enter this code on the reset page:",
        code=code, after=f"The code expires in <b>{minutes} minutes</b> and can be used once. "
                         "After the reset you'll be signed out everywhere and can sign in with the new password.",
        details=[("Requested", _when()), ("Device", describe_device(user_agent)), ("IP address", ip or "unknown")],
        notice="<b>Didn't ask for this?</b> You can ignore this email: your password stays the same unless the code is entered.",
        to=to, reason=f"because a password reset was requested for your {PRODUCT} account")


def password_changed_email(*, to: str, name: str, ip: str, user_agent: str, how: str) -> tuple[str, str, str]:
    return _render(
        f"Your {PRODUCT} password was changed",
        preheader=f"The password for your {PRODUCT} account was just changed.",
        heading="Your password was changed", greeting=_hi(name),
        intro=f"The password for your {PRODUCT} account <b>{escape(to)}</b> was {escape(how)}. "
              "For your security, other signed-in sessions were signed out.",
        details=[("When", _when()), ("Device", describe_device(user_agent)), ("IP address", ip or "unknown")],
        notice="<b>Wasn't you?</b> Reset your password right away with <i>Forgot password</i> on the sign-in page, "
               "and tell your DebBuilder administrator.",
        to=to, reason=f"to let you know about a security change on your {PRODUCT} account")
