"""Alert subsystem: detect new high-signal events and send immediate email.

State is persisted in data/state/alert_state.json so that re-runs never
re-send an alert for the same event. This file is committed back to the repo
by the GitHub Actions workflow so state survives across runs.

The alert email is built around a human-readable TLDR ("Leo bought X, shorted Y")
derived from the latest parsed 13F model, followed by a deduplicated news section.
"""
from __future__ import annotations

import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from .config import Config
from . import events as evts
from .render import render_alert
from .utils import get_logger, read_json, utc_now_iso, write_json

log = get_logger("alert")

_STATE_FILE = "alert_state.json"
# Fix [H1]: include amendments — SC 13D/A is the highest-signal SEC event class;
# omitting it caused a conf=1.0 Core Scientific amendment to go unalerted.
_ALERTABLE_TYPES = {"13f_position", "ownership_13dg", "ownership_13dg_amendment", "public_statement"}


_CLEANUP_DAYS = 60  # remove alerted IDs older than this from state
_STORY_WINDOW_HOURS = 48  # same-ticker events within this window = same story


def _load_state(cfg: Config) -> dict:
    path = cfg.paths.state / _STATE_FILE
    return read_json(path) or {
        "alerted_event_ids": [],
        "alerted_event_ids_ts": {},  # event_id → ISO timestamp when alerted
        "last_sent_at": None,
        "last_run_at": None,
    }


def _cleanup_state(state: dict) -> None:
    """Remove alerted_event_ids entries older than _CLEANUP_DAYS."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=_CLEANUP_DAYS)
    ts_map: dict[str, str] = state.get("alerted_event_ids_ts", {})

    # Purge timestamp map
    stale = [eid for eid, ts in ts_map.items()
             if datetime.fromisoformat(ts.replace("Z", "+00:00")) < cutoff]
    for eid in stale:
        del ts_map[eid]

    # Keep only IDs that still have a timestamp (i.e. not yet stale).
    # IDs without a timestamp (legacy, before this change) are kept indefinitely
    # until they age out naturally — we don't have their timestamp, so we can't
    # prune them here without risk of re-alerting.
    current_ids = set(state.get("alerted_event_ids", []))
    stale_set = set(stale)
    state["alerted_event_ids"] = [eid for eid in current_ids if eid not in stale_set]
    state["alerted_event_ids_ts"] = ts_map


def _save_state(cfg: Config, state: dict) -> None:
    _cleanup_state(state)
    write_json(cfg.paths.state / _STATE_FILE, state)


def _alert_cfg(cfg: Config) -> dict:
    return cfg.raw.get("alert", {})


def _deduplicate_news(events: list[dict]) -> list[dict]:
    """Remove near-duplicate news items — keep the highest-confidence version.

    Two public_statement events are considered the same story if they share at
    least one ticker AND were discovered within _STORY_WINDOW_HOURS of each
    other. When tickers are unknown, fall back to the first 50 chars of the
    summary (original behaviour).
    """
    # SEC filings pass through unchanged
    out_sec = [e for e in events if e.get("signal_type") != "public_statement"]
    news = [e for e in events if e.get("signal_type") == "public_statement"]

    # Sort by confidence desc so we keep the best version of each story
    news.sort(key=lambda e: float(e.get("confidence", 0)), reverse=True)

    def _discovered_dt(evt: dict):
        raw = evt.get("discovered_at") or evt.get("date") or ""
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None

    kept: list[dict] = []
    for evt in news:
        tickers = set(evt.get("ticker_guess") or [])
        dt = _discovered_dt(evt)
        is_dup = False
        for existing in kept:
            existing_tickers = set(existing.get("ticker_guess") or [])
            existing_dt = _discovered_dt(existing)

            if tickers and existing_tickers and tickers & existing_tickers:
                # Same ticker — check time window
                if dt is None or existing_dt is None:
                    is_dup = True  # no timestamp → conservative: treat as dup
                elif abs((dt - existing_dt).total_seconds()) <= _STORY_WINDOW_HOURS * 3600:
                    is_dup = True
            elif not tickers or not existing_tickers:
                # No ticker available — fall back to summary prefix match
                if evt.get("summary", "")[:50] == existing.get("summary", "")[:50]:
                    is_dup = True

            if is_dup:
                break

        if not is_dup:
            kept.append(evt)

    return out_sec + kept


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
    # Build the set from BOTH ticker and issuer so that a bond-CUSIP exit whose
    # row has no ticker (name falls back to issuer) is still matched against the
    # active equity position (which uses the ticker as its primary key).
    _active_names: set[str] = set()
    for r in model.get("common_stock", []):
        if r.get("ticker"):
            _active_names.add(r["ticker"].strip())
        if r.get("issuer"):
            _active_names.add(r["issuer"].strip())
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
    # Only include puts that are still active in the latest quarter (notional > 0).
    # The options list may contain expired/closed puts from prior quarters.
    puts = [
        r["ticker"] or r["underlying"]
        for r in model.get("options", [])
        if r.get("instrument") == "PUT"
        and r.get("notional_latest_usd", 0) > 0
        and (r.get("ticker") or r.get("underlying"))
    ]

    # Strategy flips: a Call >$50M closed this quarter while an active Put exists
    # for the same ticker — indicates a deliberate bearish restructuring (e.g. INTC).
    opt_ctx = model.get("options_context", {})
    strategy_changes = [
        tk for tk, ctx in opt_ctx.items()
        if ctx.get("call_went_zero") and ctx.get("has_active_put")
    ]

    return {
        "quarter": model.get("summary", {}).get("latest_quarter", ""),
        "new_buys": new_buys[:10],
        "strong_adds": strong_adds[:10],
        "exits": exits_sorted[:10],
        "puts_shorts": puts,
        "strategy_changes": strategy_changes,
        "total_value": model.get("summary", {}).get("common_stock_long_exposure_usd"),
        "top_positions": model.get("common_stock", [])[:5],
    }


_PENDING_REVIEW_FILE = "pending_review.jsonl"


def _queue_for_review(cfg: Config, evt: dict) -> None:
    """Write a below-threshold news event to pending_review.jsonl for manual inspection."""
    from .utils import append_jsonl, read_jsonl
    path = cfg.paths.derived / _PENDING_REVIEW_FILE
    existing_ids = {e.get("event_id") for e in read_jsonl(path)}
    if evt.get("event_id") not in existing_ids:
        append_jsonl(path, {**evt, "queued_at": utc_now_iso()})


def get_new_alertable_events(cfg: Config, state: dict) -> list[dict]:
    """Return events not yet alerted that clear the confidence threshold.

    For public_statement events the signal_tier is checked:
      - alpha_signal / position_update / None (keyword-only) → alert if conf >= min_confidence
      - confidence in [min_confidence_queue, min_confidence) → pending_review.jsonl
      - confidence < min_confidence_queue or tier == context → silently dropped
    SEC-verified events (13f_position, ownership_13dg*) are never queued — always alert.
    """
    all_events = list(evts.load_events(cfg))
    alerted_ids = set(state.get("alerted_event_ids", []))
    alert_cfg = _alert_cfg(cfg)
    min_conf: float = float(alert_cfg.get("min_confidence", 0.85))
    min_conf_queue: float = float(alert_cfg.get("min_confidence_queue", 0.5))
    on_types: list[str] = list(alert_cfg.get("on_signal_types", list(_ALERTABLE_TYPES)))

    new: list[dict] = []
    for evt in all_events:
        if evt.get("event_id") in alerted_ids:
            continue
        if evt.get("signal_type") not in on_types:
            continue

        is_news = evt.get("signal_type") == "public_statement"
        conf = float(evt.get("confidence", 0.0))

        if is_news:
            tier = evt.get("signal_tier")
            if tier == "context":
                continue  # context-only: never alert, never queue
            if conf < min_conf_queue:
                continue  # too weak even for review queue
            if conf < min_conf:
                # Below immediate-alert threshold → queue for manual review
                _queue_for_review(cfg, evt)
                log.debug("Queued for review (conf=%.2f): %s", conf, evt.get("event_id"))
                continue
        else:
            # SEC-verified filings: apply standard threshold (always 1.0, effectively unfiltered)
            if conf < min_conf_queue:
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


def get_review_queue_events(cfg: Config, state: dict) -> list[dict]:
    """Return pending_review.jsonl entries not yet included in an alert."""
    from .utils import read_jsonl
    path = cfg.paths.derived / _PENDING_REVIEW_FILE
    alerted_ids = set(state.get("alerted_event_ids", []))
    return [e for e in read_jsonl(path) if e.get("event_id") not in alerted_ids]


def check_and_alert(cfg: Config, model: dict | None = None) -> int:
    """Check for new alertable events; send email if any found.

    Pass ``model`` (from step_analyze) to include position data in the email.
    Returns the number of new events found (0 means no alert sent).
    """
    state = _load_state(cfg)
    state["last_run_at"] = utc_now_iso()

    new_events = get_new_alertable_events(cfg, state)
    review_events = get_review_queue_events(cfg, state)

    if not new_events and not review_events:
        log.info("No new alertable events.")
        _save_state(cfg, state)
        return 0

    log.info("%d alertable + %d review-queue event(s) found.", len(new_events), len(review_events))

    # Load the position model (from disk if not passed in)
    if model is None:
        model = _load_position_model(cfg)
    tldr = _build_tldr(model)

    html = render_alert.render(cfg, new_events, model=model, tldr=tldr, review_events=review_events)
    subj = render_alert.subject(cfg, new_events, tldr=tldr)
    sent = _send(cfg, subj, html)

    if sent:
        now_ts = utc_now_iso()
        sent_ids = {e["event_id"] for e in new_events} | {e["event_id"] for e in review_events}
        state["alerted_event_ids"] = list(
            set(state.get("alerted_event_ids", [])) | sent_ids
        )
        ts_map: dict = state.setdefault("alerted_event_ids_ts", {})
        for eid in sent_ids:
            ts_map.setdefault(eid, now_ts)
        state["last_sent_at"] = now_ts
        log.info("Alert state updated (%d total alerted IDs).", len(state["alerted_event_ids"]))

    _save_state(cfg, state)
    return len(new_events) + len(review_events)
