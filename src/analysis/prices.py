"""Post-quarter price lookups with a disk cache.

Primary source is yfinance; results are cached to
data/derived/prices_cache.json so a single flaky yfinance response doesn't
blank an entire email run:

  - Quarter-end prices: cached indefinitely (historical prices never change).
  - Current prices: cached for 4 hours (TTL_CURRENT_SECONDS).

Only ever used for COMMON STOCK longs — never to imply option P&L.
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path

from ..utils import get_logger

log = get_logger("analysis.prices")

TTL_CURRENT_SECONDS = 4 * 3600  # 4 hours
_CACHE_FILE = "prices_cache.json"
_cache: dict = {}            # in-process memory cache (warm across same run)
_cache_path: Path | None = None
_cache_dirty = False


def _init_cache(cfg_paths) -> None:
    global _cache, _cache_path
    if _cache_path is not None:
        return
    _cache_path = cfg_paths.derived / _CACHE_FILE
    if _cache_path.exists():
        try:
            _cache = json.loads(_cache_path.read_text(encoding="utf-8"))
        except Exception:
            _cache = {}


def _flush_cache() -> None:
    global _cache_dirty
    if _cache_path and _cache_dirty:
        try:
            _cache_path.write_text(json.dumps(_cache, indent=2), encoding="utf-8")
            _cache_dirty = False
        except Exception as exc:
            log.warning("Could not write price cache: %s", exc)


def _cache_get_qe(symbol: str, report_date: str) -> float | None:
    return _cache.get("qe", {}).get(f"{symbol}:{report_date}")


def _cache_set_qe(symbol: str, report_date: str, price: float) -> None:
    global _cache_dirty
    _cache.setdefault("qe", {})[f"{symbol}:{report_date}"] = price
    _cache_dirty = True


def _cache_get_current(symbol: str) -> float | None:
    entry = _cache.get("current", {}).get(symbol)
    if not entry:
        return None
    if time.time() - entry.get("ts", 0) > TTL_CURRENT_SECONDS:
        return None
    return entry.get("price")


def _cache_set_current(symbol: str, price: float) -> None:
    global _cache_dirty
    _cache.setdefault("current", {})[symbol] = {"price": price, "ts": time.time()}
    _cache_dirty = True


try:
    import yfinance as yf
except ImportError:
    yf = None  # type: ignore


def _nearby_close(symbol: str, on: date) -> float | None:
    """Return the closing price on or just before `on`, never after it."""
    if yf is None or not symbol:
        return None
    try:
        hist = yf.Ticker(symbol).history(
            start=(on - timedelta(days=6)).isoformat(),
            end=(on + timedelta(days=2)).isoformat(),
        )
        if hist.empty:
            return None
        # Strict: only use prices whose date is <= the requested date.
        hist = hist[hist.index.normalize().date <= on] if hasattr(hist.index, "normalize") \
            else hist[hist.index.map(lambda x: x.date()) <= on]
        if hist.empty:
            return None
        px = hist["Close"].dropna()
        return round(float(px.iloc[-1]), 2) if not px.empty else None
    except Exception as exc:
        log.debug("price_on(%s,%s) failed: %s", symbol, on, exc)
        return None


def init(cfg_paths) -> None:
    """Call once per pipeline run to enable the disk cache."""
    _init_cache(cfg_paths)


def flush() -> None:
    """Persist in-memory cache to disk. Call at end of pipeline run."""
    _flush_cache()


def current_price(symbol: str) -> float | None:
    cached = _cache_get_current(symbol)
    if cached is not None:
        return cached
    if yf is None or not symbol:
        return None
    try:
        hist = yf.Ticker(symbol).history(period="5d")
        if hist.empty:
            return None
        price = round(float(hist["Close"].iloc[-1]), 2)
        _cache_set_current(symbol, price)
        return price
    except Exception as exc:
        log.debug("current_price(%s) failed: %s", symbol, exc)
        return None


def quarter_end_price(symbol: str, report_date: str) -> float | None:
    cached = _cache_get_qe(symbol, report_date)
    if cached is not None:
        return cached
    try:
        price = _nearby_close(symbol, date.fromisoformat(report_date))
    except ValueError:
        return None
    if price is not None:
        _cache_set_qe(symbol, report_date, price)
    return price
