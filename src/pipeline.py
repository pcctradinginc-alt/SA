"""Orchestrator + CLI for the Situational Awareness Tracker.

Subcommands (see ``python -m src.pipeline --help``):

  resolve-entities   Refresh data/reference/entity_map.json from EDGAR.
  fetch              Discover + download + parse new SEC filings; emit events.
  discover           Run news/primary-source discovery; emit statement events.
  analyze            Rebuild the position model and regenerate README.md.
  digest             Render the email and send it (or save a preview).
  alert              Check for new high-signal events; send alert email if any.
  run                Full pipeline (fetch -> discover -> verify -> analyze -> digest -> alert).
"""
from __future__ import annotations

import argparse
import glob

from .config import load_config, Config
from .utils import get_logger, read_json, utc_now_iso, write_json
from .sources import sec, entity_resolution, discovery
from .sources import rss_news
from .parsers import parse_13f, parse_public_statement
from .analysis import positions, cusip_map, llm_13f, prices as prices_mod
from . import events
from . import alert as alert_mod
from .render import render_readme, render_email
from . import notify

log = get_logger("pipeline")


# ── helpers ─────────────────────────────────────────────────────────────────--
def load_recent_quarters(cfg: Config, n: int) -> list[dict]:
    """Load the most recent n parsed 13F quarters, sorted oldest -> newest."""
    files = glob.glob(str(cfg.paths.parsed / "13f" / "*.json"))
    parsed = [read_json(f) for f in files]
    parsed = [p for p in parsed if p and p.get("holdings") is not None]
    parsed.sort(key=lambda p: p.get("report_date", ""))
    return parsed[-n:]


def latest_tickers(cfg: Config, model: dict) -> set[str]:
    out: set[str] = set()
    for r in model.get("common_stock", []) + model.get("options", []):
        if r.get("ticker"):
            out.add(r["ticker"])
    return out


# ── steps ───────────────────────────────────────────────────────────────────--
def step_fetch(cfg: Config) -> None:
    new_filings = sec.collect_new_filings(cfg)
    for f in new_filings:
        if f.form.startswith("13F"):
            d = cfg.paths.raw / "sec" / f.cik / f.accession
            parsed = parse_13f.parse_filing(cfg, d, f.cik, f.report_date, f.accession)
            if parsed:
                events.append_event(cfg, events.Event(
                    event_id=f"evt_{f.report_date}_13f_{f.accession}",
                    timestamp=utc_now_iso(), as_of=f.report_date, person=cfg.person,
                    entity=cfg.primary_name, entity_cik=f.cik, signal_type="13f_position",
                    source_class=events.SEC_VERIFIED, verification_status=events.VERIFIED,
                    summary=f"{parsed['quarter']} 13F filed: {parsed['holding_count']} reported holdings.",
                    sources=[{"kind": "sec_filing", "accession": f.accession}],
                ))
        elif f.form.startswith("SC 13"):
            d = cfg.paths.raw / "sec" / f.cik / f.accession
            dg = parse_13dg.summarize_filing(d)
            issuer = dg.get("issuer_name") or "unknown issuer"
            pct = dg.get("percent_of_class", "")
            shares = dg.get("aggregate_shares", "")
            pct_str = f" ({pct}% of class)" if pct else ""
            shares_str = f", {int(float(shares.replace(',', ''))):,} shares" if shares else ""
            summary = f"{f.form}: {issuer}{pct_str}{shares_str} — filed {f.filing_date}."
            events.append_event(cfg, events.Event(
                event_id=f"evt_{f.filing_date}_13dg_{f.accession}",
                timestamp=utc_now_iso(), person=cfg.person, entity=cfg.primary_name,
                entity_cik=f.cik, signal_type="ownership_13dg",
                source_class=events.SEC_VERIFIED, verification_status=events.VERIFIED,
                summary=summary,
                ticker_guess=[dg["issuer_cusip"]] if dg.get("issuer_cusip") else [],
                sources=[{
                    "kind": "sec_filing", "accession": f.accession,
                    "issuer_name": issuer, "issuer_cusip": dg.get("issuer_cusip", ""),
                    "percent_of_class": pct, "aggregate_shares": shares,
                }],
            ))
    log.info("Fetch complete: %d new filings.", len(new_filings))


def step_discover(cfg: Config) -> None:
    items = (
        discovery.from_google_alerts(cfg)
        + discovery.from_blog(cfg)
        + discovery.from_x(cfg)
        + rss_news.from_all_curated(cfg)  # free curated sources: Google News, HN, Reddit
    )
    use_llm = cfg.llm_validate_statements
    extractor = (
        lambda item: parse_public_statement.extract_statement_with_llm(item, model=cfg.llm_model)
        if use_llm
        else parse_public_statement.extract_statement
    )
    if use_llm:
        log.info("LLM validation enabled (model=%s).", cfg.llm_model)

    n = 0
    for item in items:
        stmt = extractor(item)
        if not stmt:
            continue
        src_class = events.PRIMARY_SOURCE if item.source_kind in {"blog", "x"} else events.MEDIA_REPORTED
        events.append_event(cfg, events.Event(
            event_id=f"evt_stmt_{stmt['content_hash'][7:23]}",
            timestamp=utc_now_iso(), person=cfg.person, signal_type="public_statement",
            source_class=src_class, verification_status=events.OPEN,
            summary=stmt["title"][:200], confidence=stmt["confidence"],
            ticker_guess=stmt["ticker_guess"], needs_human_review=stmt["needs_human_review"],
            sources=[{"kind": item.source_kind, "url": stmt["url"], "hash": stmt["content_hash"]}],
        ))
        n += 1
    log.info("Discovery complete: %d candidate statements.", n)


def step_analyze(cfg: Config) -> dict:
    prices_mod.init(cfg.paths)
    quarters = load_recent_quarters(cfg, cfg.quarters)
    model = positions.build(cfg, quarters)
    if model.get("available"):
        verified = events.verify_open_statements(cfg, latest_tickers(cfg, model))
        if verified:
            log.info("Verified %d previously open statements.", verified)
        render_readme.render(cfg, model)
        model["llm_13f_analysis"] = llm_13f.generate_analysis(cfg, model)
    else:
        log.warning("No parsed 13F data yet; run `fetch` first.")
    prices_mod.flush()
    return model


def step_digest(cfg: Config, model: dict, signal: dict | None = None) -> None:
    if not model.get("available"):
        log.warning("No model available; skipping digest.")
        return
    html = render_email.render(cfg, model, signal)
    subject = render_email.subject(cfg, model, signal)
    notify.send_email(cfg, subject, html)


def step_alert(cfg: Config, model: dict | None = None) -> int:
    """Send an immediate alert if new high-signal events exist. Returns event count."""
    found = alert_mod.check_and_alert(cfg, model=model)
    if found:
        log.info("Alert step: %d new event(s) triggered an alert.", found)
    else:
        log.info("Alert step: nothing new to alert on.")
    return found


def step_run(cfg: Config) -> None:
    step_fetch(cfg)
    step_discover(cfg)
    model = step_analyze(cfg)
    # The newest unverified statement (if any) becomes the email's headline signal.
    open_stmts = [e for e in events.load_events(cfg) if e.get("verification_status") == events.OPEN]
    signal = open_stmts[-1] if open_stmts else None
    step_digest(cfg, model, signal)
    step_alert(cfg, model=model)


# ── CLI ─────────────────────────────────────────────────────────────────────--
def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Situational Awareness Tracker")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("resolve-entities", "fetch", "discover", "analyze", "digest", "alert", "run"):
        sub.add_parser(name)
    args = parser.parse_args(argv)
    cfg = load_config(args.config)

    if args.command == "resolve-entities":
        entity_resolution.resolve(cfg)
    elif args.command == "fetch":
        step_fetch(cfg)
    elif args.command == "discover":
        step_discover(cfg)
    elif args.command == "analyze":
        step_analyze(cfg)
    elif args.command == "digest":
        model = step_analyze(cfg)
        step_digest(cfg, model)
    elif args.command == "alert":
        step_alert(cfg)
    elif args.command == "run":
        step_run(cfg)


if __name__ == "__main__":
    main()
