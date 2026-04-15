from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage
from html import escape

from core.config import settings


def is_email_otp_available() -> bool:
    if not settings.EMAIL_OTP_ENABLED:
        return False
    from_address = (settings.EMAIL_FROM_ADDRESS or "").strip()
    if not from_address:
        return False
    return bool(_resolve_smtp_host())


def _build_sender_header() -> str:
    from_name = (settings.EMAIL_FROM_NAME or "").strip()
    from_address = (settings.EMAIL_FROM_ADDRESS or "").strip()
    if from_name:
        return f"{from_name} <{from_address}>"
    return from_address


def _smtp_mode_label() -> str:
    if settings.EMAIL_SMTP_USE_SSL:
        return "SSL"
    if settings.EMAIL_SMTP_STARTTLS:
        return "STARTTLS"
    return "PLAINTEXT"


def _extract_domain_from_email(email_address: str) -> str:
    local, sep, domain = (email_address or "").strip().lower().rpartition("@")
    if not sep or not local or not domain:
        return ""
    return domain


def _resolve_smtp_host() -> str:
    configured_host = (settings.EMAIL_SMTP_HOST or "").strip()
    if configured_host:
        return configured_host
    from_address = (settings.EMAIL_FROM_ADDRESS or "").strip()
    return _extract_domain_from_email(from_address)


def _build_login_email_html(*, otp_code: str, expires_minutes: int, recipient_name: str | None) -> str:
    safe_name = escape((recipient_name or "Gamer").strip() or "Gamer")
    safe_code = escape(otp_code)
    safe_minutes = max(1, int(expires_minutes))
    return f"""
<!doctype html>
<html lang=\"en\">
  <body style=\"margin:0;padding:0;background:#0f172a;font-family:'Segoe UI',Tahoma,sans-serif;\">
    <table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" style=\"background:#0f172a;padding:30px 12px;\">
      <tr>
        <td align=\"center\">
          <table role=\"presentation\" width=\"640\" cellspacing=\"0\" cellpadding=\"0\" style=\"max-width:640px;background:#111827;border-radius:18px;overflow:hidden;border:1px solid #1f2937;\">
            <tr>
              <td style=\"background:linear-gradient(135deg,#16a34a,#0ea5e9);padding:22px 24px;color:#ffffff;\">
                <div style=\"font-size:12px;letter-spacing:1.4px;text-transform:uppercase;opacity:.9;\">GamerzAdda Login Security</div>
                <div style=\"font-size:28px;font-weight:700;line-height:1.2;margin-top:6px;\">Your OTP Is Ready</div>
              </td>
            </tr>
            <tr>
              <td style=\"padding:26px 24px;color:#e5e7eb;\">
                <p style=\"margin:0 0 14px;font-size:15px;line-height:1.6;\">Hi {safe_name},</p>
                <p style=\"margin:0 0 16px;font-size:15px;line-height:1.6;\">Use this one-time password to complete your login:</p>
                <div style=\"display:inline-block;background:#0b1220;border:1px solid #334155;border-radius:12px;padding:14px 18px;\">
                  <span style=\"font-size:30px;letter-spacing:8px;font-weight:800;color:#f8fafc;\">{safe_code}</span>
                </div>
                <p style=\"margin:16px 0 0;font-size:14px;color:#cbd5e1;line-height:1.6;\">This OTP expires in <strong>{safe_minutes} minutes</strong>. Do not share it with anyone.</p>
              </td>
            </tr>
            <tr>
              <td style=\"padding:0 24px 24px;color:#94a3b8;\">
                <div style=\"border-top:1px solid #243042;padding-top:14px;font-size:12px;line-height:1.6;\">
                  If you did not request this login, please secure your account immediately.
                </div>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
""".strip()


def _build_login_email_text(*, otp_code: str, expires_minutes: int, recipient_name: str | None) -> str:
    safe_name = (recipient_name or "Gamer").strip() or "Gamer"
    safe_minutes = max(1, int(expires_minutes))
    return (
        f"Hi {safe_name},\n\n"
        f"Your GamerzAdda login OTP is: {otp_code}\n"
        f"This OTP expires in {safe_minutes} minutes.\n"
        "Do not share this OTP with anyone.\n\n"
        "- GamerzAdda Security"
    )


def _send_email_sync(*, to_email: str, subject: str, text_body: str, html_body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = _build_sender_header()
    msg["To"] = to_email
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    timeout = float(settings.EMAIL_SMTP_TIMEOUT_SECONDS)
    host = _resolve_smtp_host()
    if not host:
        raise RuntimeError("SMTP host is empty and could not be derived from EMAIL_FROM_ADDRESS")
    port = int(settings.EMAIL_SMTP_PORT)
    mode = _smtp_mode_label()
    stage = "connect"

    try:
        if settings.EMAIL_SMTP_USE_SSL:
            with smtplib.SMTP_SSL(host, port, timeout=timeout) as client:
                stage = "ehlo"
                client.ehlo()
                if settings.EMAIL_SMTP_USERNAME and settings.EMAIL_SMTP_PASSWORD:
                    stage = "auth"
                    client.login(settings.EMAIL_SMTP_USERNAME, settings.EMAIL_SMTP_PASSWORD)
                stage = "send"
                client.send_message(msg)
            return

        with smtplib.SMTP(host, port, timeout=timeout) as client:
            stage = "ehlo"
            client.ehlo()
            if settings.EMAIL_SMTP_STARTTLS:
                stage = "starttls"
                client.starttls()
                stage = "ehlo_after_starttls"
                client.ehlo()
            if settings.EMAIL_SMTP_USERNAME and settings.EMAIL_SMTP_PASSWORD:
                stage = "auth"
                client.login(settings.EMAIL_SMTP_USERNAME, settings.EMAIL_SMTP_PASSWORD)
            stage = "send"
            client.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        raise RuntimeError(
            f"SMTP auth failed during {stage} (host={host}, port={port}, mode={mode})"
        ) from exc
    except TimeoutError as exc:
        raise RuntimeError(
            f"SMTP timeout during {stage} (host={host}, port={port}, mode={mode}, timeout={timeout}s)"
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"SMTP network error during {stage} (host={host}, port={port}, mode={mode}): {exc}"
        ) from exc
    except smtplib.SMTPException as exc:
        raise RuntimeError(
            f"SMTP protocol error during {stage} (host={host}, port={port}, mode={mode}): {exc}"
        ) from exc


async def send_login_otp_email(*, to_email: str, otp_code: str, recipient_name: str | None = None) -> None:
    if not is_email_otp_available():
        raise RuntimeError("Email OTP is disabled or SMTP configuration is incomplete")

    email = (to_email or "").strip().lower()
    if not email:
        raise RuntimeError("Recipient email is missing")

    ttl_seconds = int(settings.EMAIL_OTP_TTL_SECONDS)
    expires_minutes = max(1, ttl_seconds // 60)
    subject = "GamerzAdda Login OTP"
    text_body = _build_login_email_text(
        otp_code=otp_code,
        expires_minutes=expires_minutes,
        recipient_name=recipient_name,
    )
    html_body = _build_login_email_html(
        otp_code=otp_code,
        expires_minutes=expires_minutes,
        recipient_name=recipient_name,
    )

    await asyncio.to_thread(
        _send_email_sync,
        to_email=email,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
    )
