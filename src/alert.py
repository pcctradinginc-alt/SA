"""Alert subsystem: detect new high-signal events and send immediate email.

State is persisted in data/state/alert_state.json so that re-runs never
re-send an alert for the same event. This file is committed back to the repo
by the GitHub Actions workflow so state survives across runs.

The alert email is built around a human-readable TLDR ("Leo bought X, shorted Y")
derived from the latest parsed 13F model, followed by a deduplicated news section.
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
_ALERTABLE_TYPES = {"13f_position", "ownership_13dg", "public_statement"}


def _load_state(cfg: Config) -> dict:
    path = cfg.paths.state / _STATE_FILE
    return read_json(path) or {"alerted_event_ids": [], "last_sent_at": None, "last_run_at": None}


def _save_state(cfg: Config, state: dict) -> None:
    write_json(cfg.paths.state / _STATE_FILE, state)


def _alert_cfg(cfg: Config) -> dict:
    return cfg.raw.get("alert", {})


def _deduplicate_news(events: list[dict]) -> list[dict]:
    """Remove near-duplicate news items — keep the highest-confidence version.

    Two news events are considered duplicates if they share the same ticker
    AND the first 50 characters of their summary are identical.
    """
    seen: dict[str, dict] = {}
    out: list[dict] = []
    for evt in events:
        if evt.get("signal_type") != "public_statement":
            out.append(evt)
            continue
        tickers = tuple(sorted(evt.get("ticker_guess") or []))
        key = tickers + (evt.get("summary", "")[:50],)
        existing = seen.get(str(key))
        if existing is None:
            seen[str(key)] = evt
            out.append(evt)
        elif float(evt.get("confidence", 0)) > float(existing.get("confidence", 0)):
            # Replace with higher-confidence version
            out[out.index(existing)] = evt
            seen[str(key)] = evt
    return out


def _load_position_model(cfg: Config) -> dict:
    """Load the pre-built position model from disk (built by step_analyze)."""
    path = cfg.paths.derived / "position_table.json"
    model = read_json(path) or {}
    if model and not model.get("llm_13f_analysis"):
        cached = read_json(cfg.paths.derived / "13f_analysis.json") or {}
        model["llm_13f_analysis"] = cached.get("analysis", "")
    return model


def _build_tldr(model: dict) -> dict:
    """Extract a human-readable action summary from the position model."""
    if not model.get("available"):
        return {}

    new_buys = [r["ticker"] or r["issuer"] for r in model.get("new_buys", []) if r.get("ticker") or r.get("issuer")]

    # Deduplicate exits by name (same company can appear twice if its CUSIP
    # changed across quarters), then sort largest-exited position first.
    # Exclude issuers that still have an active common-stock position — this
    # happens when a CUSIP changed (e.g. corporate action) across quarters.
    _active_names = {
        (r.get("ticker") or r.get("issuer") or "").strip()
        for r in model.get("common_stock", [])
    }
    _exit_by_name: dict[str, int] = {}
    for r in model.get("exits", []):
        name = (r.get("ticker") or r.get("issuer") or "").strip()
        if not name or name in _active_names:
            continue
        peak = max(r.get("shares_by_quarter") or [0])
        _exit_by_name[name] = max(_exit_by_name.get(name, 0), peak)
    exits_sorted = sorted(_exit_by_name, key=lambda n: _exit_by_name[n], reverse=True)

    # Fix: parentheses required — without them `or r.get("issuer")` makes every
    # position with an issuer pass the filter regardless of status.
    strong_adds = [
        r["ticker"] or r["issuer"]
        for r in model.get("common_stock", [])
        if r.get("status") in ("strong_add", "new_add")
        and (r.get("ticker") or r.get("issuer"))
        and r.get("status") not in ("new_buy",)
    ]
    puts = [
        r["ticker"] or r["underlying"]
        for r in model.get("options", [])
        if r.get("instrument") == "PUT" and (r.get("ticker") or r.get("underlying"))
    ]

    return {
        "quarter": model.get("summary", {}).get("latest_quarter", ""),
        "new_buys": new_buys[:10],
        "strong_adds": strong_adds[:10],
        "exits": exits_sorted[:10],
        "puts_shorts": puts[:10],
        "total_value": model.get("summary", {}).get("common_stock_long_exposure_usd"),
        "top_positions": model.get("common_stock", [])[:5],
    }


def get_new_alertable_events(cfg: Config, state: dict) -> list[dict]:
    """Return events not yet alerted that clear the confidence threshold."""
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

    return _deduplicate_news(new)


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

    s = Config.smtp_settings()
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
    log.info("Alert sent to %s (%d event(s)).", s["to"], len(recipients))
    return True


def check_and_alert(cfg: Config, model: dict | None = None) -> int:
    """Check for new alertable events; send email if any found.

    Pass ``model`` (from step_analyze) to include position data in the email.
    Returns the number of new events found (0 means no alert sent).
    """
    state = _load_state(cfg)
    state["last_run_at"] = utc_now_iso()

    new_events = get_new_alertable_events(cfg, state)
    if not new_events:
        log.info("No new alertable events.")
        _save_state(cfg, state)
        return 0

    log.info("%d new alertable event(s) found.", len(new_events))

    # Load the position model (from disk if not passed in)
    if model is None:
        model = _load_position_model(cfg)
    tldr = _build_tldr(model)

    html = render_alert.render(cfg, new_events, model=model, tldr=tldr)
    subj = render_alert.subject(cfg, new_events, tldr=tldr)
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
