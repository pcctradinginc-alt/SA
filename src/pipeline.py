"""Orchestrator + CLI for the Situational Awareness Tracker.

Subcommands (see ``python -m src.pipeline --help``):

  resolve-entities   Refresh data/reference/entity_map.json from EDGAR.
  fetch              Discover + download + parse new SEC filings; emit events.
  discover           Run news/primary-source discovery; emit statement events.
  analyze            Rebuild the position model and regenerate README.md.
  digest             Render the email and send it (or save a preview).
  alert              Check for new high-signal events; send alert email if any.
  run                Full pipeline (fetch -> discover -> verify -> analyze -> digest -> alert).
  map-cusips         Auto-map unmapped CUSIPs via OpenFIGI; update override CSV.
  backfill-tiers     One-shot: LLM-classify existing events that lack signal_tier.
"""
from __future__ import annotations

import argparse
import glob
import xml.etree.ElementTree as _ET

from .config import load_config, Config
from .utils import get_logger, read_json, utc_now_iso, write_json
from .sources import sec, entity_resolution, discovery
from .sources import rss_news
from .parsers import parse_13f, parse_13dg, parse_public_statement
from .analysis import positions, cusip_map, llm_13f, prices as prices_mod, map_cusips
from . import events
from . import alert as alert_mod
from .render import render_readme, render_email
from . import notify

log = get_logger("pipeline")


# ── helpers ─────────────────────────────────────────────────────────────────--
def load_recent_quarters(cfg: Config, n: int) -> list[dict]:
    """Load the most recent n parsed 13F quarters, sorted oldest -> newest.

    Deduplicates by report_date: if both a 13F-HR and a 13F-HR/A exist for the
    same quarter, keeps the one with the highest accession number (the amendment).
    """
    files = glob.glob(str(cfg.paths.parsed / "13f" / "*.json"))
    parsed = [read_json(f) for f in files]
    parsed = [p for p in parsed if p and p.get("holdings") is not None]
    # Keep only the latest filing per report_date (amendment wins over original)
    by_date: dict[str, dict] = {}
    for p in parsed:
        rd = p.get("report_date", "")
        acc = p.get("accession", "")
        if rd not in by_date or acc > by_date[rd].get("accession", ""):
            by_date[rd] = p
    deduped = sorted(by_date.values(), key=lambda p: p.get("report_date", ""))
    return deduped[-n:]


def collect_13dg_cusips(cfg: Config) -> frozenset[str]:
    """Return CUSIPs that appear in any recorded 13D/G ownership event."""
    cusips: set[str] = set()
    for evt in events.load_events(cfg):
        if evt.get("signal_type") != "ownership_13dg":
            continue
        for src in evt.get("sources", []):
            c = (src.get("issuer_cusip") or "").strip()
            if c:
                cusips.add(c)
    return frozenset(cusips)


def latest_tickers(cfg: Config, model: dict) -> set[str]:
    out: set[str] = set()
    for r in model.get("common_stock", []) + model.get("options", []):
        if r.get("ticker"):
            out.add(r["ticker"])
    return out


def exited_tickers(model: dict) -> set[str]:
    """Return tickers that were fully exited in the latest 13F quarter."""
    out: set[str] = set()
    for r in model.get("exits", []):
        if r.get("ticker"):
            out.add(r["ticker"])
    return out


# ── steps ───────────────────────────────────────────────────────────────────--
def step_fetch(cfg: Config) -> None:
    new_filings = sec.collect_new_filings(cfg)
    processed: list[str] = []
    for f in new_filings:
        try:
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
                processed.append(f.accession)
            elif f.form.startswith("SC 13"):
                d = cfg.paths.raw / "sec" / f.cik / f.accession
                dg = parse_13dg.summarize_filing(d)
                issuer = dg.get("issuer_name") or "unknown issuer"
                pct = dg.get("percent_of_class", "")
                shares = dg.get("aggregate_shares", "")
                pct_str = f" ({pct}% of class)" if pct else ""
                shares_str = f", {int(float(shares.replace(',', ''))):,} shares" if shares else ""
                is_amendment = f.form.endswith("/A")
                amend_str = " [AMENDMENT]" if is_amendment else " [INITIAL]"
                summary = f"{f.form}: {issuer}{pct_str}{shares_str}{amend_str} — filed {f.filing_date}."
                sig_type = "ownership_13dg_amendment" if is_amendment else "ownership_13dg"
                events.append_event(cfg, events.Event(
                    event_id=f"evt_{f.filing_date}_13dg_{f.accession}",
                    timestamp=utc_now_iso(), person=cfg.person, entity=cfg.primary_name,
                    entity_cik=f.cik, signal_type=sig_type,
                    source_class=events.SEC_VERIFIED, verification_status=events.VERIFIED,
                    summary=summary,
                    ticker_guess=[dg["issuer_cusip"]] if dg.get("issuer_cusip") else [],
                    sources=[{
                        "kind": "sec_filing", "accession": f.accession,
                        "issuer_name": issuer, "issuer_cusip": dg.get("issuer_cusip", ""),
                        "percent_of_class": pct, "aggregate_shares": shares,
                        "is_amendment": is_amendment,
                    }],
                ))
                processed.append(f.accession)
            else:
                processed.append(f.accession)  # Form D etc. — no parsing needed
        except (_ET.ParseError, ValueError, KeyError, UnicodeDecodeError) as exc:
            # Permanent parse error — filing is malformed and will never succeed.
            # Mark as seen so we stop retrying, but log at ERROR for visibility.
            log.error("Permanent parse error for %s (skipping permanently): %s", f.accession, exc)
            processed.append(f.accession)
        except OSError as exc:
            # Transient I/O or network error — do NOT mark as seen so next run retries.
            log.warning("Transient error for %s (will retry next run): %s", f.accession, exc)
        except Exception as exc:
            # Unexpected error — log with full traceback, do not mark as seen.
            log.exception("Unexpected error for %s (will retry next run): %s", f.accession, exc)
    sec.mark_seen(cfg, processed)
    log.info("Fetch complete: %d new filings, %d processed.", len(new_filings), len(processed))


def step_discover(cfg: Config) -> None:
    items = (
        discovery.from_google_alerts(cfg)
        + discovery.from_blog(cfg)
        + discovery.from_x(cfg)
        + rss_news.from_all_curated(cfg)  # free curated sources: Google News, HN, Reddit
    )

    # Load active 13F tickers to pass as whitelist context to the LLM classifier.
    # This enables the alpha_signal / position_update / context tier distinction.
    # Falls back to empty set if no position model exists yet (first run).
    try:
        _model = read_json(cfg.paths.derived / "position_table.json") or {}
        _active_tickers = latest_tickers(cfg, _model)
        _exited_tickers = exited_tickers(_model)
    except Exception:
        _active_tickers = set()
        _exited_tickers = set()
    if _active_tickers:
        log.info("Discover: passing %d active 13F tickers as whitelist context.", len(_active_tickers))
    if _exited_tickers:
        log.info("Discover: passing %d exited 13F tickers to suppress echo-alerts.", len(_exited_tickers))
    if not _active_tickers and not _exited_tickers:
        log.info("Discover: no position model found — all signals treated as potential alpha.")

    use_llm = cfg.llm_validate_statements
    if use_llm:
        log.info("LLM validation enabled (model=%s).", cfg.llm_model)
        extractor = (
            lambda item: parse_public_statement.extract_statement_with_llm(
                item, model=cfg.llm_model, active_tickers=_active_tickers,
                exited_tickers=_exited_tickers,
                cache_path=cfg.paths.state / "llm_classification_cache.json",
            )
        )
    else:
        extractor = parse_public_statement.extract_statement

    # Fix [H3]: skip EDGAR RSS/Form-D items from statement extraction.
    # These are SEC filing notifications — they belong in step_fetch (parse_13f /
    # parse_13dg). Running them through parse_public_statement produces spurious
    # public_statement events (conf 0.6–0.7) that duplicate or shadow the proper
    # 13f_position / ownership_13dg events created by step_fetch.
    _SKIP_KINDS = {"edgar_rss"}

    n = 0
    n_context = 0
    for item in items:
        if item.source_kind in _SKIP_KINDS:
            log.debug("Skipping edgar_rss item from statement pipeline (handled by fetch): %s", item.url)
            continue
        stmt = extractor(item)
        if not stmt:
            continue
        tier = stmt.get("signal_tier")
        if tier == "context":
            n_context += 1
        src_class = events.PRIMARY_SOURCE if item.source_kind in {"blog", "x"} else events.MEDIA_REPORTED
        events.append_event(cfg, events.Event(
            event_id=f"evt_stmt_{stmt['content_hash'][7:23]}",
            timestamp=utc_now_iso(), person=cfg.person, signal_type="public_statement",
            source_class=src_class, verification_status=events.OPEN,
            summary=stmt["title"][:200], confidence=stmt["confidence"],
            ticker_guess=stmt["ticker_guess"], needs_human_review=stmt["needs_human_review"],
            signal_tier=tier,
            sources=[{
                "kind": item.source_kind, "url": stmt["url"], "hash": stmt["content_hash"],
                "llm_quote": stmt.get("llm_quote", ""),
                "llm_inference": stmt.get("llm_inference", ""),
                "llm_action_hint": stmt.get("llm_action_hint", ""),
                "llm_reason": stmt.get("llm_reason", ""),
                "signal_category": stmt.get("signal_category", ""),
                "llm_validated": stmt.get("llm_validated", False),
            }],
        ))
        n += 1
    log.info(
        "Discovery complete: %d candidate statements (%d context-only, suppressed from alerts).",
        n, n_context,
    )


def step_analyze(cfg: Config) -> dict:
    prices_mod.init(cfg.paths)
    quarters = load_recent_quarters(cfg, cfg.quarters)
    dg_cusips = collect_13dg_cusips(cfg)
    model = positions.build(cfg, quarters, cusips_with_13dg=dg_cusips)
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


def step_backfill_tiers(cfg: Config) -> None:
    """Retroactively classify public_statement events that lack a signal_tier.

    Events created before the 3-tier LLM system was in place have
    ``signal_tier=None`` and ``llm_validated=False``.  Because ``append_event``
    is idempotent by event_id, re-discovering the same articles never updates
    them.  This command does a one-shot backfill:

      1. Loads all events from events.jsonl.
      2. For each ``public_statement`` event whose sources[0] does NOT contain
         ``llm_validated=True``, calls Claude to classify the headline.
      3. Writes updated fields (signal_tier, llm_validated, llm_quote,
         llm_inference, llm_action_hint, llm_reason) back into the event's
         sources dict and the top-level ``signal_tier`` field.
      4. Rewrites events.jsonl atomically.

    Safe to re-run: already-validated events are skipped.
    """
    import os
    import json as _json
    import re as _re

    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning("ANTHROPIC_API_KEY not set — cannot backfill tiers.")
        return

    try:
        import anthropic
    except ImportError:
        log.warning("anthropic package not installed — cannot backfill tiers.")
        return

    # Load active + exited 13F tickers for whitelist context (same as step_discover).
    try:
        _model = read_json(cfg.paths.derived / "position_table.json") or {}
        _active_tickers = latest_tickers(cfg, _model)
        _exited_tickers_bf = exited_tickers(_model)
    except Exception:
        _active_tickers = set()
        _exited_tickers_bf = set()

    from .parsers.parse_public_statement import _build_system_prompt

    system_prompt = _build_system_prompt(_active_tickers, _exited_tickers_bf)
    client = anthropic.Anthropic()

    all_events = events.load_events(cfg)
    updated = 0
    skipped = 0

    for evt in all_events:
        if evt.get("signal_type") != "public_statement":
            continue
        src = evt.get("sources", [{}])[0] if evt.get("sources") else {}
        if src.get("llm_validated") is True:
            skipped += 1
            continue  # already classified

        title = evt.get("summary", "")
        source_kind = src.get("kind", "unknown")

        try:
            response = client.messages.create(
                model=cfg.llm_model,
                max_tokens=384,
                system=[{
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{
                    "role": "user",
                    "content": (
                        f"Title: {title}\n"
                        f"Excerpt: (no excerpt stored)\n"
                        f"Source: {source_kind}"
                    ),
                }],
            )
            raw = response.content[0].text.strip()
            if raw.startswith("```"):
                raw = _re.sub(r'^```(?:json)?\s*', '', raw)
                raw = _re.sub(r'\s*```\s*$', '', raw.strip())
            m = _re.search(r'\{.*\}', raw, _re.DOTALL)
            if m:
                raw = m.group()
            result = _json.loads(raw)
        except Exception as exc:
            log.warning("Backfill LLM call failed for event %s: %s", evt.get("event_id"), exc)
            continue

        signal_tier = result.get("signal_tier", "unrelated")
        evt["signal_tier"] = signal_tier

        # Update the sources entry with LLM fields.
        if evt.get("sources"):
            evt["sources"][0]["llm_validated"] = True
            evt["sources"][0]["llm_quote"] = result.get("quote") or ""
            evt["sources"][0]["llm_inference"] = result.get("inference") or ""
            evt["sources"][0]["llm_action_hint"] = result.get("action_hint") or ""
            evt["sources"][0]["llm_reason"] = result.get("reason") or ""
            evt["sources"][0]["signal_category"] = result.get("action", "")

        # Rebuild confidence from LLM result if available.
        llm_conf = result.get("confidence")
        if llm_conf is not None:
            evt["confidence"] = round(float(llm_conf), 2)
            evt["needs_human_review"] = evt["confidence"] < 0.80

        updated += 1
        log.debug(
            "Backfilled event %s → tier=%s conf=%.2f",
            evt.get("event_id"), signal_tier, evt.get("confidence", 0),
        )

    from .utils import rewrite_jsonl
    rewrite_jsonl(cfg.paths.parsed / events.EVENTS_FILE, all_events)
    log.info(
        "Backfill complete: %d events updated, %d already validated, %d non-statement.",
        updated, skipped, len(all_events) - updated - skipped,
    )


# ── CLI ─────────────────────────────────────────────────────────────────────--
def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Situational Awareness Tracker")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("resolve-entities", "fetch", "discover", "analyze", "digest", "alert", "run",
                 "map-cusips", "backfill-tiers"):
        sub.add_parser(name)
    args = parser.parse_args(argv)
    cfg = load_config(args.config)

    if args.command == "resolve-entities":
        entity_resolution.resolve(cfg)
    elif args.command == "map-cusips":
        map_cusips.run(cfg)
    elif args.command == "fetch":
        step_fetch(cfg)
    elif args.command == "discover":
        step_discover(cfg)
    elif args.command == "analyze":
        step_analyze(cfg)
    elif args.command == "digest":
        # Fix [Bug1]: load pre-built model from disk instead of re-running step_analyze.
        # update_news.yml runs `analyze` then `digest` — the old code caused step_analyze
        # to run twice (once explicitly, once inside the digest CLI command).
        model = read_json(cfg.paths.derived / "position_table.json") or {}
        if not model.get("available"):
            log.warning("No position_table.json found; run `analyze` first.")
        step_digest(cfg, model)
    elif args.command == "alert":
        step_alert(cfg)
    elif args.command == "run":
        step_run(cfg)
    elif args.command == "backfill-tiers":
        step_backfill_tiers(cfg)


if __name__ == "__main__":
    main()
