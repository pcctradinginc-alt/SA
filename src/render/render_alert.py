"""Render a compact alert email for newly detected high-signal events."""
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


def render(cfg: Config, new_events: list[dict]) -> str:
    meta = {
        "person": cfg.person,
        "manager": cfg.primary_name,
        "subject_prefix": cfg.raw.get("alert", {}).get("subject_prefix", "SA Alert"),
    }
    return _env().get_template("alert.html.j2").render(events=new_events, meta=meta)


def subject(cfg: Config, new_events: list[dict]) -> str:
    prefix = cfg.raw.get("alert", {}).get("subject_prefix", "SA Alert")
    if not new_events:
        return f"{prefix} · no new signals"
    first = new_events[0]
    sig = first.get("signal_type", "signal")
    cat = first.get("signal_category", "")
    label = cat if cat else sig.replace("_", " ")
    summary = first.get("summary", "")[:60]
    count = len(new_events)
    tail = f" (+{count - 1} more)" if count > 1 else ""
    return f"{prefix} · {label} · {summary}{tail}"
