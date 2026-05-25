"""Email delivery over SMTP.

Credentials come from environment variables only (SMTP_HOST, SMTP_PORT,
SMTP_USER, SMTP_PASSWORD, SMTP_TO). If email is disabled or unconfigured, the
rendered HTML is still written to examples/last_email.html for inspection.
"""
from __future__ import annotations

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from .config import Config
from .utils import get_logger

log = get_logger("notify")


def save_preview(cfg: Config, html: str, name: str = "last_email.html") -> Path:
    out = cfg.paths.root / "examples" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


def send_email(cfg: Config, subject: str, html: str) -> bool:
    """Send the digest. Returns True if actually sent, False if skipped."""
    save_preview(cfg, html)

    if not cfg.email_enabled:
        log.info("Email disabled in config; preview saved only.")
        return False

    s = Config.smtp_settings()
    if not all([s["host"], s["user"], s["password"], s["to"]]):
        log.warning("SMTP env vars incomplete; not sending. Preview saved.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{cfg.raw['email']['from_name']} <{s['user']}>"
    msg["To"] = s["to"]
    msg.attach(MIMEText("This digest requires an HTML-capable mail client.", "plain"))
    msg.attach(MIMEText(html, "html"))

    recipients = [r.strip() for r in str(s["to"]).split(",") if r.strip()]
    with smtplib.SMTP(str(s["host"]), int(s["port"])) as server:
        server.starttls()
        server.login(str(s["user"]), str(s["password"]))
        server.sendmail(str(s["user"]), recipients, msg.as_string())
    log.info("Digest sent to %s", s["to"])
    return True
