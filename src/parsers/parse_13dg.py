"""Parse SC 13D / SC 13G beneficial-ownership filings.

Extracts the key fields from the EDGAR XML primary document:
  - issuer name, CUSIP, CIK
  - aggregate shares owned
  - percent of class (the headline number)
  - securities class (common stock, etc.)
  - date of event

Falls back to a best-effort text scan if no XML is found.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

# EDGAR 13D/G XML namespaces
_NS13D = "http://www.sec.gov/edgar/schedule13D"
_NS13G = "http://www.sec.gov/edgar/schedule13G"
_NSCOM = "http://www.sec.gov/edgar/common"


def _parse_xml(text: str) -> dict:
    """Parse the primary_doc.xml of a 13D or 13G filing."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {}

    ns = root.tag.split("}")[0].lstrip("{") if "}" in root.tag else _NS13D

    def find(path: str) -> str:
        parts = [f"{{{ns}}}{p}" for p in path.split("/")]
        el = root
        for part in parts:
            el = el.find(part) if el is not None else None  # type: ignore
        return (el.text or "").strip() if el is not None and el.text else ""

    # Look under formData/coverPageHeader for issuer info
    cover = root.find(f"{{{ns}}}formData/{{{ns}}}coverPageHeader")

    def cv(tag: str) -> str:
        if cover is None:
            return ""
        el = cover.find(f"{{{ns}}}{tag}")
        return (el.text or "").strip() if el is not None else ""

    issuer_el = cover.find(f"{{{ns}}}issuerInfo") if cover is not None else None

    def iss(tag: str) -> str:
        if issuer_el is None:
            return ""
        el = issuer_el.find(f"{{{ns}}}{tag}")
        return (el.text or "").strip() if el is not None else ""

    # CUSIP: older 13D schema uses issuerCUSIP directly; newer 13G schema nests
    # it under issuerCusips/issuerCusipNumber.
    issuer_cusip = iss("issuerCUSIP")
    if not issuer_cusip and issuer_el is not None:
        cusips_el = issuer_el.find(f"{{{ns}}}issuerCusips")
        if cusips_el is not None:
            first = cusips_el.find(f"{{{ns}}}issuerCusipNumber")
            if first is not None:
                issuer_cusip = (first.text or "").strip()

    # issuerCik: older schema = issuerCIK, newer 13G schema = issuerCik
    issuer_cik = iss("issuerCIK") or iss("issuerCik")

    # Aggregate ownership: older schema uses reportingPersons/reportingPersonInfo;
    # newer 13G schema uses coverPageHeaderReportingPersonDetails directly under formData.
    aggregate = ""
    pct = ""

    persons = root.findall(
        f"{{{ns}}}formData/{{{ns}}}reportingPersons/{{{ns}}}reportingPersonInfo"
    )
    if persons:
        p = persons[0]
        agg_el = p.find(f"{{{ns}}}aggregateAmountOwned")
        pct_el = p.find(f"{{{ns}}}percentOfClass")
        if agg_el is not None:
            aggregate = (agg_el.text or "").strip()
        if pct_el is not None:
            pct = (pct_el.text or "").strip()

    if not aggregate or not pct:
        # Newer 13G schema: first coverPageHeaderReportingPersonDetails block
        details = root.findall(
            f"{{{ns}}}formData/{{{ns}}}coverPageHeaderReportingPersonDetails"
        )
        if details:
            d = details[0]
            if not aggregate:
                agg_el = d.find(
                    f"{{{ns}}}reportingPersonBeneficiallyOwnedAggregateNumberOfShares"
                )
                if agg_el is not None:
                    aggregate = (agg_el.text or "").strip()
            if not pct:
                pct_el = d.find(f"{{{ns}}}classPercent")
                if pct_el is not None:
                    pct = (pct_el.text or "").strip()

    return {
        "issuer_name": iss("issuerName"),
        "issuer_cusip": issuer_cusip,
        "issuer_cik": issuer_cik,
        "securities_class": cv("securitiesClassTitle"),
        "date_of_event": cv("dateOfEvent") or cv("eventDateRequiresFilingThisStatement"),
        "aggregate_shares": aggregate,
        "percent_of_class": pct,
    }


def _parse_text_fallback(text: str) -> dict:
    """Best-effort regex extraction from HTML/SGML filings."""
    result: dict = {}

    # Subject company from SGML header
    m = re.search(r"SUBJECT COMPANY.*?COMPANY CONFORMED NAME\s*:\s*(.+)", text, re.S)
    if m:
        result["issuer_name"] = m.group(1).splitlines()[0].strip()

    # CUSIP
    m = re.search(r"CUSIP\s+(?:NO\.?\s*)?([0-9A-Z]{9})", text, re.I)
    if m:
        result["issuer_cusip"] = m.group(1)

    # Percent of class — look for "X.X%" or "X%" near "percent" or "row 11"
    m = re.search(r"(?:percent\s+of\s+class|row\s+11)[^0-9]*(\d+\.?\d*)\s*%", text, re.I)
    if m:
        result["percent_of_class"] = m.group(1)

    # Aggregate amount — near "row 9" or "aggregate amount"
    m = re.search(
        r"(?:aggregate\s+amount|row\s+9)[^0-9]*(\d[\d,]+)", text, re.I
    )
    if m:
        result["aggregate_shares"] = m.group(1).replace(",", "")

    return result


def summarize_filing(filing_dir: Path) -> dict:
    """Return a structured summary of a 13D/G filing directory.

    Tries XML first (clean, reliable), falls back to text regex.
    """
    result: dict = {
        "issuer_name": "", "issuer_cusip": "", "issuer_cik": "",
        "securities_class": "", "date_of_event": "",
        "aggregate_shares": "", "percent_of_class": "",
    }

    # 1. Try XML primary doc
    for xml_path in sorted(filing_dir.glob("primary_doc*.xml")):
        try:
            text = xml_path.read_text(encoding="utf-8", errors="ignore")
            parsed = _parse_xml(text)
            if parsed.get("issuer_name") or parsed.get("percent_of_class"):
                result.update({k: v for k, v in parsed.items() if v})
                return result
        except OSError:
            continue

    # 2. Fallback: scan all text/html documents
    for path in sorted(filing_dir.glob("*.txt")) + sorted(filing_dir.glob("*.htm*")):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        parsed = _parse_text_fallback(text)
        if parsed:
            result.update({k: v for k, v in parsed.items() if v})
            if result.get("issuer_name") and result.get("percent_of_class"):
                break

    return result
