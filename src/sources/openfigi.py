"""OpenFIGI CUSIP-to-ticker batch mapping.

API: https://www.openfigi.com/api
- POST https://api.openfigi.com/v3/mapping
- Batch size: max 100 per request
- Rate limit: 25 req/min (no key), 250 req/min (X-OPENFIGI-APIKEY header)
"""
from __future__ import annotations

import os
import time

from ..utils import get_logger

log = get_logger("sources.openfigi")

_URL = "https://api.openfigi.com/v3/mapping"
# Without API key: max 10 items/batch, 25 req/min
# With API key:   max 100 items/batch, 250 req/min
_BATCH_SIZE_UNAUTH = 10
_BATCH_SIZE_AUTH = 100

# US exchange codes: NYSE, NASDAQ, NYSE American, ARCA, OTC Bulletin Board…
_US_EXCHANGES = {"US", "UQ", "UN", "UA", "UP", "UR", "UT", "UF"}
_WANTED_SECTOR = "Equity"
_TYPE_RANK = ["Common Stock", "ETF", "ADR", "Depositary Receipt", "Preference"]
_BOND_EXCHANGES = {"TRACE", "BVAL", "CORP", "MUN"}
_BOND_TYPES = {"US DOMESTIC", "Corp Bond", "Note", "Convertible", "Fixed Income"}


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    key = os.environ.get("OPENFIGI_API_KEY", "")
    if key:
        h["X-OPENFIGI-APIKEY"] = key
    return h


def _best_match(candidates: list[dict]) -> dict | None:
    """Pick the most relevant result from a list of FIGI candidates."""
    if not candidates:
        return None
    equities = [c for c in candidates if c.get("marketSector") == _WANTED_SECTOR] or candidates
    us = [c for c in equities if c.get("exchCode") in _US_EXCHANGES]
    pool = us if us else equities
    for sec_type in _TYPE_RANK:
        typed = [c for c in pool if c.get("securityType") == sec_type]
        if typed:
            return typed[0]
    return pool[0]


def lookup_batch(cusips: list[str]) -> dict[str, dict | None]:
    """Query OpenFIGI for a list of CUSIPs.

    Returns {cusip: result_dict} where result_dict has keys:
      ticker, name, exchCode, securityType, figi, confidence, candidate_count
    Missing / no-match CUSIPs map to {"error": reason}.
    """
    import requests

    results: dict[str, dict | None] = {}
    hdrs = _headers()
    batch_size = _BATCH_SIZE_AUTH if "X-OPENFIGI-APIKEY" in hdrs else _BATCH_SIZE_UNAUTH

    for i in range(0, len(cusips), batch_size):
        batch = cusips[i : i + batch_size]
        payload = [{"idType": "ID_CUSIP", "idValue": c} for c in batch]
        try:
            resp = requests.post(_URL, json=payload, headers=hdrs, timeout=30)
            if resp.status_code == 429:
                log.warning("OpenFIGI rate limit — sleeping 12 s")
                time.sleep(12)
                resp = requests.post(_URL, json=payload, headers=hdrs, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.error("OpenFIGI batch %d-%d failed: %s", i, i + BATCH_SIZE, exc)
            for c in batch:
                results[c] = {"error": str(exc)}
            continue

        for cusip, item in zip(batch, data):
            if "error" in item or not item.get("data"):
                results[cusip] = {"error": item.get("error", "no_data")}
                continue
            candidates = item["data"]
            best = _best_match(candidates)
            if not best:
                results[cusip] = {"error": "no_equity_match"}
                continue
            # Reject bond/fixed-income results
            if (best.get("exchCode") in _BOND_EXCHANGES
                    or best.get("securityType") in _BOND_TYPES
                    or best.get("marketSector") in ("Fixed Income", "Corporate")):
                results[cusip] = {"error": "bond_cusip"}
                continue
            n = len(candidates)
            # Single US-equity match → high; otherwise scale down
            if n == 1 and best.get("exchCode") in _US_EXCHANGES:
                confidence = "high"
            elif n <= 3:
                confidence = "medium"
            else:
                confidence = "low"
            results[cusip] = {
                "ticker": best.get("ticker", ""),
                "name": best.get("name", ""),
                "exchCode": best.get("exchCode", ""),
                "securityType": best.get("securityType", ""),
                "figi": best.get("figi", ""),
                "confidence": confidence,
                "candidate_count": n,
            }

        if i + batch_size < len(cusips):
            time.sleep(0.5)  # stay polite

    return results
