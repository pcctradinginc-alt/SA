"""Automated CUSIP → ticker mapping via OpenFIGI.

Collects every CUSIP seen across all parsed 13F files, queries OpenFIGI in
batches for any that are unmapped (or previously returned no result), and
writes back to cusip_ticker_overrides.csv.

Rules:
- Existing 'manual' entries are NEVER overwritten.
- Previous 'openfigi' entries with confidence=none/low are re-queried.
- No-match CUSIPs are recorded as source=openfigi_no_match so they are
  skipped on subsequent runs unless the entry is deleted.
"""
from __future__ import annotations

import csv
import glob
from pathlib import Path

from ..config import Config
from ..utils import get_logger, read_json
from ..sources import openfigi

# Suffixes stripped before yfinance name search to improve match rate
_CORP_SUFFIXES = (
    " INC", " CORP", " LTD", " LLC", " LP", " PLC",
    " HOLDINGS", " GROUP", " CO", " AG", " NV", " SA",
)

log = get_logger("analysis.map_cusips")

_FIELDS = ["cusip", "issuer", "ticker", "yfinance_symbol", "sector", "source", "confidence"]


def _load_csv(path: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    if not path.exists():
        return rows
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            c = (row.get("cusip") or "").strip()
            if c:
                rows[c] = {k: (v or "").strip() for k, v in row.items()}
    return rows


def _write_csv(path: Path, rows: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=_FIELDS, extrasaction="ignore")
        w.writeheader()
        for row in rows.values():
            w.writerow({f: row.get(f, "") for f in _FIELDS})
    log.info("Wrote %d rows to %s", len(rows), path)


def _yfinance_lookup(issuer_name: str) -> str | None:
    """Try to resolve a ticker from a company name via yfinance Search.

    Returns the best-match ticker symbol, or None if nothing useful is found.
    Only used as a last-resort fallback after OpenFIGI returns no result.
    """
    try:
        import yfinance as yf  # optional dependency
    except ImportError:
        return None

    # Strip legal suffixes for a cleaner search query
    query = issuer_name.upper()
    for suffix in _CORP_SUFFIXES:
        if query.endswith(suffix):
            query = query[: -len(suffix)].strip()
            break

    if not query:
        return None

    # Use the first word as the search term — full cleaned names often return
    # zero results (e.g. "SEAGATE TECHNOLOGY HLDNGS PL" → no hits, but
    # "SEAGATE" → STX immediately).
    root = query.split()[0] if query else query
    try:
        results = yf.Search(root, max_results=5).quotes
    except Exception as exc:
        log.debug("yfinance Search failed for %r: %s", root, exc)
        return None

    if not results:
        return None

    # Pick the first result whose shortName contains the root word.
    for hit in results:
        symbol = (hit.get("symbol") or "").strip()
        short = (hit.get("shortname") or hit.get("longname") or "").lower()
        if not symbol or len(symbol) > 6:  # skip indexes / fund codes
            continue
        # Whole-word match to avoid "bloom" → "bloomberg" false positives
        import re as _re
        if _re.search(r"\b" + _re.escape(root.lower()) + r"\b", short):
            log.info("  yfinance fallback: %r → %s (%s)", issuer_name, symbol, short[:40])
            return symbol

    return None


def _needs_query(row: dict) -> bool:
    """Return True if the row should be re-queried from OpenFIGI.

    Only unprocessed rows (no source or blank confidence) are queried.
    Existing openfigi results — regardless of confidence — are stable and
    won't change; delete the row to force a re-query.
    """
    source = row.get("source", "")
    if source in ("manual", "openfigi", "openfigi_bond", "openfigi_no_match"):
        return False
    return True


def run(cfg: Config) -> None:
    """Collect unmapped CUSIPs, query OpenFIGI, update the override CSV."""
    csv_path = cfg.paths.reference / "cusip_ticker_overrides.csv"
    existing = _load_csv(csv_path)

    # Gather all CUSIPs seen across every parsed 13F
    seen: dict[str, str] = {}  # cusip → issuer name
    for f in sorted(glob.glob(str(cfg.paths.parsed / "13f" / "*.json"))):
        data = read_json(f)
        if not data:
            continue
        for h in data.get("holdings", []):
            c = (h.get("cusip") or "").strip()
            if c and c not in seen:
                seen[c] = h.get("name_of_issuer", "")

    log.info("Unique CUSIPs across all 13F files: %d", len(seen))

    to_query = [
        c for c in seen
        if c not in existing or _needs_query(existing[c])
    ]

    if not to_query:
        log.info("All CUSIPs already mapped — nothing to do.")
        _print_summary(existing)
        return

    log.info("Querying OpenFIGI for %d CUSIPs...", len(to_query))
    figi_results = openfigi.lookup_batch(to_query)

    added = updated = errors = 0
    for cusip in to_query:
        issuer = seen[cusip]
        res = figi_results.get(cusip)

        if not res or "error" in res:
            err = (res or {}).get("error", "no_response")
            if err == "bond_cusip":
                log.info("  %-12s → bond/convertible, skipped", cusip)
                source = "openfigi_bond"
                ticker = ""
            else:
                log.debug("No match: %s (%s) — %s", cusip, issuer[:40], err)
                # Fallback: try yfinance name search for equity CUSIPs
                ticker = _yfinance_lookup(issuer) or ""
                source = "yfinance_name" if ticker else "openfigi_no_match"
            existing.setdefault(cusip, {
                "cusip": cusip, "issuer": issuer,
                "ticker": "", "yfinance_symbol": "",
                "sector": "", "source": source, "confidence": "none",
            })
            existing[cusip]["source"] = source
            existing[cusip]["ticker"] = ticker
            existing[cusip]["yfinance_symbol"] = ticker
            existing[cusip]["confidence"] = "medium" if ticker else "none"
            if not ticker:
                errors += 1
            continue

        ticker = res["ticker"]
        confidence = res["confidence"]
        name = res.get("name") or issuer
        log.info(
            "  %-12s → %-6s  %-40s  exch=%-3s  type=%-15s  conf=%s  n=%d",
            cusip, ticker, name[:40],
            res.get("exchCode", ""), res.get("securityType", ""),
            confidence, res.get("candidate_count", 0),
        )

        row = {
            "cusip": cusip,
            "issuer": name,
            "ticker": ticker,
            "yfinance_symbol": ticker,
            "sector": "",
            "source": "openfigi",
            "confidence": confidence,
        }
        if cusip in existing:
            existing[cusip].update(row)
            updated += 1
        else:
            existing[cusip] = row
            added += 1

    _write_csv(csv_path, existing)
    log.info(
        "Done: %d added, %d updated, %d no-match. CSV now has %d rows.",
        added, updated, errors, len(existing),
    )
    _print_summary(existing)


def _print_summary(rows: dict[str, dict]) -> None:
    manual = sum(1 for r in rows.values() if r.get("source") == "manual")
    figi_high = sum(1 for r in rows.values() if r.get("source") == "openfigi" and r.get("confidence") == "high")
    figi_med = sum(1 for r in rows.values() if r.get("source") == "openfigi" and r.get("confidence") == "medium")
    figi_low = sum(1 for r in rows.values() if r.get("source") == "openfigi" and r.get("confidence") == "low")
    no_match = sum(1 for r in rows.values() if "no_match" in r.get("source", "") or r.get("confidence") == "none")
    with_ticker = sum(1 for r in rows.values() if r.get("ticker"))
    log.info(
        "Coverage: %d with ticker / %d total | manual=%d openfigi_high=%d med=%d low=%d no_match=%d",
        with_ticker, len(rows), manual, figi_high, figi_med, figi_low, no_match,
    )
