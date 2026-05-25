"""Post-quarter price lookups.

Primary source is yfinance; if unavailable it degrades gracefully (prices are
returned as None and flagged), because price data is contextual, not core.
Only ever used for COMMON STOCK longs — never to imply option P&L.
"""
from __future__ import annotations

from datetime import date, timedelta

from ..utils import get_logger

log = get_logger("analysis.prices")

try:
    import yfinance as yf
except ImportError:  # pragma: no cover
    yf = None  # type: ignore


def _nearby_close(symbol: str, on: date) -> float | None:
    """Closing price on/around a date (handles weekends/holidays)."""
    if yf is None or not symbol:
        return None
    try:
        hist = yf.Ticker(symbol).history(
            start=(on - timedelta(days=6)).isoformat(),
            end=(on + timedelta(days=2)).isoformat(),
        )
        if hist.empty:
            return None
        return round(float(hist["Close"].iloc[-1]), 2)
    except Exception as exc:  # noqa: BLE001
        log.debug("price_on(%s,%s) failed: %s", symbol, on, exc)
        return None


def current_price(symbol: str) -> float | None:
    if yf is None or not symbol:
        return None
    try:
        hist = yf.Ticker(symbol).history(period="5d")
        if hist.empty:
            return None
        return round(float(hist["Close"].iloc[-1]), 2)
    except Exception as exc:  # noqa: BLE001
        log.debug("current_price(%s) failed: %s", symbol, exc)
        return None


def quarter_end_price(symbol: str, report_date: str) -> float | None:
    try:
        return _nearby_close(symbol, date.fromisoformat(report_date))
    except ValueError:
        return None
