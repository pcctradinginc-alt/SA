"""Parse a 13F-HR information table into structured holdings.

Critical design choice (see project review): holdings are tagged by
instrument type so that downstream analysis never mixes common-stock share
counts with option *notional* values.

13F facts encoded here:
  * ``value`` is reported in whole US dollars (filings since the 2023 EDGAR
    amendment; all watched filings post-date it).
  * the ``putCall`` element marks an option row; for options the reported value
    is the notional value of the underlying, NOT the premium paid.
  * ``sshPrnamtType`` is "SH" (shares) or "PRN" (principal amount).
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

from ..config import Config
from ..utils import get_logger, write_json

log = get_logger("parsers.13f")

COMMON_STOCK = "COMMON_STOCK"
OPTION_PUT = "OPTION_PUT"
OPTION_CALL = "OPTION_CALL"
OTHER = "OTHER"


def _localname(tag: str) -> str:
    """Strip an XML namespace: '{ns}value' -> 'value'."""
    return tag.rsplit("}", 1)[-1]


def quarter_label(report_date: str) -> str:
    """'2026-03-31' -> 'Q1 2026'."""
    try:
        d = date.fromisoformat(report_date)
    except ValueError:
        return report_date or "unknown"
    return f"Q{(d.month - 1) // 3 + 1} {d.year}"


def _classify(put_call: str, amount_type: str) -> str:
    pc = (put_call or "").strip().lower()
    if pc == "put":
        return OPTION_PUT
    if pc == "call":
        return OPTION_CALL
    if not pc and (amount_type or "").strip().upper() == "SH":
        return COMMON_STOCK
    return OTHER


def parse_info_table(xml_text: str) -> list[dict]:
    """Parse an information-table XML string into a list of holding dicts."""
    root = ET.fromstring(xml_text)
    holdings: list[dict] = []

    for info in root.iter():
        if _localname(info.tag) != "infoTable":
            continue
        fields: dict[str, str] = {}
        amount = ""
        amount_type = ""
        for child in info.iter():
            name = _localname(child.tag)
            if name == "sshPrnamt":
                amount = (child.text or "").strip()
            elif name == "sshPrnamtType":
                amount_type = (child.text or "").strip()
            elif child.text and child.text.strip():
                fields[name] = child.text.strip()

        instrument = _classify(fields.get("putCall", ""), amount_type)
        holdings.append(
            {
                "name_of_issuer": fields.get("nameOfIssuer", ""),
                "title_of_class": fields.get("titleOfClass", ""),
                "cusip": fields.get("cusip", ""),
                "value_usd": int(fields.get("value", "0") or 0),
                "amount": int(amount or 0),
                "amount_type": amount_type,
                "put_call": fields.get("putCall", ""),
                "instrument_type": instrument,
            }
        )
    return holdings


def find_info_table_file(filing_dir: Path) -> Path | None:
    """Locate the information-table XML inside a downloaded filing directory."""
    candidates = sorted(filing_dir.glob("*.xml"))
    for path in candidates:
        try:
            head = path.read_text(encoding="utf-8", errors="ignore")[:4000]
        except OSError:
            continue
        if "informationTable" in head or "<infoTable" in head:
            return path
    return None


def parse_filing(cfg: Config, filing_dir: Path, cik: str, report_date: str, accession: str) -> dict | None:
    """Parse one downloaded 13F filing directory; write parsed/13f/<key>.json."""
    info_file = find_info_table_file(filing_dir)
    if info_file is None:
        log.warning("No information table found in %s", filing_dir)
        return None

    holdings = parse_info_table(info_file.read_text(encoding="utf-8", errors="ignore"))
    quarter = quarter_label(report_date)
    parsed = {
        "cik": cik,
        "accession": accession,
        "report_date": report_date,
        "quarter": quarter,
        "holding_count": len(holdings),
        "holdings": holdings,
    }
    out_path = cfg.paths.parsed / "13f" / f"{cik}_{report_date}.json"
    write_json(out_path, parsed)
    log.info("Parsed %s: %d holdings", quarter, len(holdings))
    return parsed
