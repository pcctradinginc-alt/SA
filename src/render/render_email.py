"""Render the Apple-style HTML digest email from the position model.

The 13F overview block is ALWAYS included; ``signal`` is the optional new event
that triggered this send (None for a scheduled digest with no new signal).
"""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import Config
from ..utils import get_logger
from .format import FILTERS

log = get_logger("render.email")
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "email_templates"


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(default=True),
        trim_blocks=True, lstrip_blocks=True,
    )
    env.filters.update(FILTERS)
    return env


def render(cfg: Config, model: dict, signal: dict | None = None) -> str:
    meta = {
        "person": cfg.person,
        "manager": cfg.primary_name,
        "subject_prefix": cfg.raw["email"]["subject_prefix"],
    }
    return _env().get_template("digest.html.j2").render(model=model, signal=signal, meta=meta)


def subject(cfg: Config, model: dict, signal: dict | None) -> str:
    prefix = cfg.raw["email"]["subject_prefix"]
    if signal:
        return f"{prefix} · new signal · {model['summary']['latest_quarter']}"
    s = model["summary"]
    return f"{prefix} · {s['latest_quarter']} · {s['new_common_positions']} new, {s['exited_common_positions']} exits"
