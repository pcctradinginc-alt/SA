"""Render the actionable alert email."""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import Config
from ..utils import get_logger
from .format import FILTERS

log = get_logger("render.alert")
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "email_templates"


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(default=True),
        trim_blocks=True, lstrip_blocks=True,
    )
    env.filters.update(FILTERS)
    return env


def render(cfg: Config, new_events: list[dict], model: dict | None = None, tldr: dict | None = None) -> str:
    meta = {
        "person": cfg.person,
        "manager": cfg.primary_name,
        "subject_prefix": cfg.raw.get("alert", {}).get("subject_prefix", "SA Alert"),
    }
    sec_events = [e for e in new_events if e.get("signal_type") in ("13f_position", "ownership_13dg")]
    news_events = [e for e in new_events if e.get("signal_type") == "public_statement"]
    # Sort news by confidence descending
    news_events.sort(key=lambda e: float(e.get("confidence", 0)), reverse=True)

    return _env().get_template("alert.html.j2").render(
        sec_events=sec_events,
        news_events=news_events,
        model=model or {},
        tldr=tldr or {},
        analysis_13f=(model or {}).get("llm_13f_analysis", ""),
        top_signals=(model or {}).get("top_signals", []),
        meta=meta,
    )


def subject(cfg: Config, new_events: list[dict], tldr: dict | None = None) -> str:
    prefix = cfg.raw.get("alert", {}).get("subject_prefix", "SA Alert")
    tldr = tldr or {}
    quarter = tldr.get("quarter", "")
    new_buys = tldr.get("new_buys", [])
    puts = tldr.get("puts_shorts", [])

    has_13f = any(e.get("signal_type") == "13f_position" for e in new_events)

    if has_13f and new_buys:
        bought = ", ".join(new_buys[:3])
        short_str = f" | Short: {', '.join(puts[:2])}" if puts else ""
        return f"{prefix} · {quarter} · Neu: {bought}{short_str}"
    if has_13f:
        return f"{prefix} · Neues 13F-Filing · {quarter}"

    # News-only alert
    tickers = sorted({t for e in new_events for t in (e.get("ticker_guess") or [])})
    if tickers:
        return f"{prefix} · News: {', '.join(tickers[:4])}"
    first_summary = new_events[0].get("summary", "")[:55] if new_events else ""
    return f"{prefix} · {first_summary}"
