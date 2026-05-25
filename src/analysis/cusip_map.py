"""CUSIP -> ticker resolution.

13F reports CUSIPs, not tickers, so a mapping layer is mandatory before any
price lookup. This module reads the manual override table; unmapped CUSIPs are
reported with low confidence rather than guessed.
"""
from __future__ import annotations

import csv
from pathlib import Path

from ..config import Config
from ..utils import get_logger

log = get_logger("analysis.cusip")

OVERRIDES_FILE = "cusip_ticker_overrides.csv"


def load_overrides(cfg: Config) -> dict[str, dict[str, str]]:
    """Return {cusip: {ticker, yfinance_symbol, sector, source, confidence}}."""
    path: Path = cfg.paths.reference / OVERRIDES_FILE
    mapping: dict[str, dict[str, str]] = {}
    if not path.exists():
        log.warning("No CUSIP override file at %s", path)
        return mapping
    with path.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            cusip = (row.get("cusip") or "").strip()
            if cusip:
                mapping[cusip] = {k: (v or "").strip() for k, v in row.items()}
    log.info("Loaded %d CUSIP overrides", len(mapping))
    return mapping


def resolve(cusip: str, issuer: str, overrides: dict[str, dict[str, str]]) -> dict[str, str]:
    """Resolve one CUSIP to ticker info, defaulting to a low-confidence stub."""
    if cusip in overrides:
        row = overrides[cusip]
        return {
            "ticker": row.get("ticker", ""),
            "yfinance_symbol": row.get("yfinance_symbol") or row.get("ticker", ""),
            "sector": row.get("sector", ""),
            "mapping": row.get("source", "manual_override"),
            "confidence": row.get("confidence", "high"),
        }
    return {"ticker": "", "yfinance_symbol": "", "sector": "", "mapping": "unmapped", "confidence": "none"}
