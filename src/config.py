"""Central configuration loader.

Loads ``config.yaml`` once and exposes a typed ``Config`` object plus the set
of project paths. Secrets are read from environment variables, never from the
config file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Paths:
    """Resolved absolute paths for every data directory."""

    root: Path
    reference: Path
    raw: Path
    parsed: Path
    derived: Path
    state: Path

    @classmethod
    def from_config(cls, raw_cfg: dict[str, Any]) -> "Paths":
        p = raw_cfg.get("paths", {})
        return cls(
            root=PROJECT_ROOT,
            reference=PROJECT_ROOT / p.get("reference", "data/reference"),
            raw=PROJECT_ROOT / p.get("raw", "data/raw"),
            parsed=PROJECT_ROOT / p.get("parsed", "data/parsed"),
            derived=PROJECT_ROOT / p.get("derived", "data/derived"),
            state=PROJECT_ROOT / p.get("state", "data/state"),
        )

    def ensure(self) -> None:
        """Create all data directories if they do not yet exist."""
        for path in (self.reference, self.raw, self.parsed, self.derived, self.state):
            path.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Config:
    """Typed view over config.yaml plus environment-derived secrets."""

    raw: dict[str, Any]
    paths: Paths = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "paths", Paths.from_config(self.raw))

    # ── convenience accessors ────────────────────────────────────────────────
    @property
    def person(self) -> str:
        return self.raw["entity"]["person"]

    @property
    def primary_name(self) -> str:
        return self.raw["entity"]["primary_name"]

    @property
    def primary_cik(self) -> str:
        return str(self.raw["entity"]["primary_cik"]).zfill(10)

    @property
    def all_ciks(self) -> list[str]:
        extra = [str(c).zfill(10) for c in self.raw["entity"].get("extra_ciks", [])]
        return [self.primary_cik, *extra]

    @property
    def sec_user_agent(self) -> str:
        return self.raw["sec"]["user_agent"]

    @property
    def sec_request_delay(self) -> float:
        return float(self.raw["sec"]["request_delay_seconds"])

    @property
    def filing_types(self) -> list[str]:
        return list(self.raw["sec"]["filing_types"])

    @property
    def quarters(self) -> int:
        return int(self.raw["analysis"]["quarters"])

    @property
    def hold_band(self) -> float:
        return float(self.raw["analysis"]["hold_band_pct"])

    @property
    def trim_threshold(self) -> float:
        return float(self.raw["analysis"]["trim_threshold_pct"])

    @property
    def prices_enabled(self) -> bool:
        return bool(self.raw["prices"]["enabled"])

    @property
    def email_enabled(self) -> bool:
        return bool(self.raw["email"]["enabled"])

    # ── secrets (env only) ────────────────────────────────────────────────────
    @staticmethod
    def smtp_settings() -> dict[str, str | int | None]:
        return {
            "host": os.environ.get("SMTP_HOST"),
            "port": int(os.environ.get("SMTP_PORT", "587")),
            "user": os.environ.get("SMTP_USER"),
            "password": os.environ.get("SMTP_PASSWORD"),
            "to": os.environ.get("SMTP_TO"),
        }


@lru_cache(maxsize=1)
def load_config(path: str | Path | None = None) -> Config:
    """Load and cache the project configuration."""
    cfg_path = Path(path) if path else PROJECT_ROOT / "config.yaml"
    with cfg_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    cfg = Config(raw=raw)
    cfg.paths.ensure()
    return cfg
