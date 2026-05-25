"""Generate examples/sample_digest_email.html from illustrative data.

Issuer names reflect real, public Q1-2026 13F holdings (e.g. Bloom Energy long,
NVDA puts); share counts, prices and the headline statement are ILLUSTRATIVE
placeholders only. The statement is explicitly fictional — no real quote is
attributed to any person. Run: python examples/generate_example.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.analysis import positions
from src.render import render_email
from src import notify

# Point CUSIP overrides at the fixture so tickers resolve in the example.
import src.analysis.cusip_map as cm
cm.OVERRIDES_FILE = "../../tests/fixtures/example_overrides.csv"

cfg = load_config()


def holding(name, cusip, shares, value, put_call=""):
    itype = "COMMON_STOCK" if not put_call else ("OPTION_PUT" if put_call == "Put" else "OPTION_CALL")
    return {"name_of_issuer": name, "title_of_class": "COM", "cusip": cusip,
            "value_usd": value, "amount": shares, "amount_type": "SH",
            "put_call": put_call, "instrument_type": itype}

# Three illustrative quarters (oldest -> newest).
q3 = {"quarter": "Q3 2025", "report_date": "2025-09-30", "holding_count": 4, "holdings": [
    holding("Bloom Energy Corp", "093712107", 5_000_000, 600_000_000),
    holding("CoreWeave Inc", "21873S108", 1_000_000, 120_000_000),
    holding("IREN Ltd", "Q49887115", 8_000_000, 90_000_000),
    holding("Nvidia Corp", "67066G104", 0, 1_200_000_000, "Put"),
]}
q4 = {"quarter": "Q4 2025", "report_date": "2025-12-31", "holding_count": 4, "holdings": [
    holding("Bloom Energy Corp", "093712107", 6_000_000, 780_000_000),
    holding("CoreWeave Inc", "21873S108", 1_400_000, 175_000_000),
    holding("IREN Ltd", "Q49887115", 6_000_000, 70_000_000),
    holding("Nvidia Corp", "67066G104", 0, 1_600_000_000, "Put"),
]}
q1 = {"quarter": "Q1 2026", "report_date": "2026-03-31", "holding_count": 5, "holdings": [
    holding("Bloom Energy Corp", "093712107", 6_500_000, 879_000_000),
    holding("CoreWeave Inc", "21873S108", 2_100_000, 295_000_000),
    holding("IREN Ltd", "Q49887115", 0, 0),  # exited
    holding("Sandisk Corp", "80004C101", 1_200_000, 96_000_000),  # new buy
    holding("Nvidia Corp", "67066G104", 0, 1_600_000_000, "Put"),
]}

cfg.raw["prices"]["enabled"] = False  # offline example: no network price calls
model = positions.build(cfg, [q3, q4, q1])

signal = {
    "source_class": "MEDIA_REPORTED", "verification_status": "open", "confidence": 0.50,
    "summary": "[Illustrative placeholder — fictional] Report: building a position in [Company X].",
    "ticker_guess": ["X"], "reason_not_verifiable": None,
    "sources": [{"url": "https://example.com/illustrative"}],
}

html = render_email.render(cfg, model, signal)
out = notify.save_preview(cfg, html, "sample_digest_email.html")
print("Wrote", out)
