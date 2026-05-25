"""Render README.md from the position model (Jinja markdown template)."""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import Config
from ..utils import get_logger
from .format import FILTERS

log = get_logger("render.readme")
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "email_templates"


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(enabled_extensions=("html",)),
        trim_blocks=True, lstrip_blocks=True,
    )
    env.filters.update(FILTERS)
    return env


def render(cfg: Config, model: dict) -> str:
    meta = {"person": cfg.person, "manager": cfg.primary_name}
    html = _env().get_template("readme.md.j2").render(model=model, meta=meta)
    out = cfg.paths.root / "README.md"
    out.write_text(html, encoding="utf-8")
    log.info("Wrote %s", out)
    return html
