"""Alert subsystem: detect new high-signal events and send immediate email.

State is persisted in data/state/alert_state.json so that re-runs never
re-send an alert for the same event. This file is committed back to the repo
by the GitHub Actions workflow so state survives across runs.

The alert is independent of the full digest: it uses a compact template
(alert.html.j2) and fires as soon as new events clear the confidence threshold,
rather than waiting for the next scheduled digest window.
"""
from __future__ import annotations

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from .config import Config
from . import events as evts
from .render import render_alert
from .utils import get_logger, read_json, utc_now_iso, write_json

log = get_logger("alert")

_STATE_FILE = "alert_state.json"

# Signal types that can trigger an alert
_ALERTABLE_TYPES = {"13f_position", "ownership_13dg", "public_statement"}


def _load_state(cfg: Config) -> dict:
    path = cfg.paths.state / _STATE_FILE
    return read_json(path) or {"alerted_event_ids": [], "last_sent_at": None, "last_run_at": None}


def _save_state(cfg: Config, state: dict) -> None:
    write_json(cfg.paths.state / _STATE_FILE, state)


def _alert_cfg(cfg: Config) -> dict:
    return cfg.raw.get("alert", {})


def get_new_alertable_events(cfg: Config, state: dict) -> list[dict]:
    """Return events that have not yet been alerted and clear the threshold."""
    all_events = list(evts.load_events(cfg))
    alerted_ids = set(state.get("alerted_event_ids", []))
    min_conf: float = float(_alert_cfg(cfg).get("min_confidence", 0.5))
    on_types: list[str] = list(_alert_cfg(cfg).get("on_signal_types", list(_ALERTABLE_TYPES)))

    new: list[dict] = []
    for evt in all_events:
        if evt.get("event_id") in alerted_ids:
            continue
        if float(evt.get("confidence", 0.0)) < min_conf:
            continue
        if evt.get("signal_type") not in on_types:
            continue
        new.append(evt)
    return new


def _save_preview(cfg: Config, html: str) -> Path:
    out = cfg.paths.root / "examples" / "last_alert.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


def _send(cfg: Config, subject_line: str, html: str) -> bool:
    """Send the alert email; return True if sent, False if skipped."""
    _save_preview(cfg, html)

    if not _alert_cfg(cfg).get("enabled", False):
        log.info("Alerts disabled (alert.enabled = false); preview written only.")
        return False

    from .config import Config as _C  # avoid circular at module level
    s = _C.smtp_settings()
    if not all([s["host"], s["user"], s["password"], s["to"]]):
        log.warning("SMTP env vars incomplete; alert not sent. Preview written.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject_line
    msg["From"] = f"{cfg.raw['email']['from_name']} <{s['user']}>"
    msg["To"] = s["to"]
    msg.attach(MIMEText("This alert requires an HTML-capable mail client.", "plain"))
    msg.attach(MIMEText(html, "html"))

    recipients = [r.strip() for r in str(s["to"]).split(",") if r.strip()]
    with smtplib.SMTP(str(s["host"]), int(s["port"])) as server:
        server.starttls()
        server.login(str(s["user"]), str(s["password"]))
        server.sendmail(str(s["user"]), recipients, msg.as_string())
    log.info("Alert sent to %s (%d event(s)).", s["to"], 1)
    return True


def check_and_alert(cfg: Config) -> int:
    """Check for new alertable events; send email if any found.

    Returns the number of new events found (0 means no alert sent).
    State is always updated with the current run timestamp even when idle.
    """
    state = _load_state(cfg)
    state["last_run_at"] = utc_now_iso()

    new_events = get_new_alertable_events(cfg, state)
    if not new_events:
        log.info("No new alertable events.")
        _save_state(cfg, state)
        return 0

    log.info("%d new alertable event(s) found.", len(new_events))
    html = render_alert.render(cfg, new_events)
    subj = render_alert.subject(cfg, new_events)
    sent = _send(cfg, subj, html)

    if sent:
        sent_ids = {e["event_id"] for e in new_events}
        state["alerted_event_ids"] = list(
            set(state.get("alerted_event_ids", [])) | sent_ids
        )
        state["last_sent_at"] = utc_now_iso()
        log.info("Alert state updated (%d total alerted IDs).", len(state["alerted_event_ids"]))

    _save_state(cfg, state)
    return len(new_events)
