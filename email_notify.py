"""Send alert emails when the webshop session needs manual restore."""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from typing import List, Optional, Sequence

from config_loader import alert_notify_emails, load_config
from logging_setup import get_logger

logger = get_logger()


def alert_recipients(config=None) -> List[str]:
    """Parse [alerts] notify_emails (comma/semicolon separated)."""
    return alert_notify_emails(config or load_config())


def _smtp_settings(config) -> dict:
    host = config.get("alerts", "smtp_host", fallback="smtp.gmail.com").strip()
    port = config.getint("alerts", "smtp_port", fallback=587)
    use_tls = config.getboolean("alerts", "smtp_use_tls", fallback=True)
    user = config.get(
        "alerts",
        "smtp_user",
        fallback=config.get("gmail", "username", fallback=""),
    ).strip()
    password = config.get(
        "alerts",
        "smtp_password",
        fallback=config.get("gmail", "password", fallback=""),
    ).strip()
    from_email = config.get("alerts", "from_email", fallback=user).strip() or user
    return {
        "host": host,
        "port": port,
        "use_tls": use_tls,
        "user": user,
        "password": password,
        "from_email": from_email,
    }


def send_email(
    subject: str,
    body: str,
    recipients: Optional[Sequence[str]] = None,
    config=None,
) -> bool:
    """
    Send a plain-text email via SMTP.
    Returns True on success. Logs and returns False on failure / missing config.
    """
    config = config or load_config()
    to_list = list(recipients) if recipients is not None else alert_recipients(config)
    if not to_list:
        logger.warning(
            "No alert recipients configured ([alerts] notify_emails). "
            "Skipping email: %s",
            subject,
        )
        return False

    smtp = _smtp_settings(config)
    if not smtp["user"] or not smtp["password"]:
        logger.error(
            "Cannot send alert email — set [alerts] smtp_user/smtp_password "
            "or [gmail] username/password."
        )
        return False
    if not smtp["from_email"]:
        logger.error("Cannot send alert email — from_email is empty.")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp["from_email"]
    msg["To"] = ", ".join(to_list)
    msg.set_content(body)

    try:
        if smtp["use_tls"]:
            context = ssl.create_default_context()
            with smtplib.SMTP(smtp["host"], smtp["port"], timeout=30) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(smtp["user"], smtp["password"])
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp["host"], smtp["port"], timeout=30) as server:
                server.login(smtp["user"], smtp["password"])
                server.send_message(msg)
        logger.info("Alert email sent to %s — %s", to_list, subject)
        return True
    except Exception as exc:
        logger.error("Failed to send alert email to %s: %s", to_list, exc)
        return False


def notify_session_inactive(config=None, *, detail: str = "") -> bool:
    """
    Notify configured recipients that the webshop session must be restored.
    """
    config = config or load_config()
    bot_name = config.get("bot", "name", fallback="Webshop Order Robot")
    subject = f"[ACTION REQUIRED] {bot_name}: webshop session inactive"
    body = (
        f"{bot_name} cannot continue — the Hiab webshop session is not active.\n"
        "\n"
        "Additional action required to restore the session:\n"
        "  1. On the robot machine run:\n"
        "       python main.py --login\n"
        "  2. Complete sign-in / passkey / MFA in the Hiab Bot Chrome window.\n"
        "  3. Leave that Chrome window open.\n"
        "  4. Restart unattended mode:\n"
        "       python main.py --unattended\n"
        "     or:\n"
        "       python unattended_main.py\n"
        "\n"
    )
    if detail:
        body += f"Details:\n{detail.strip()}\n\n"
    body += "This message was sent automatically by the Webshop Order Robot.\n"
    return send_email(subject, body, config=config)
