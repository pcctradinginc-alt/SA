"""Offline unit tests for the position model (no network required).

Run: python -m pytest tests/  (or simply: python tests/test_positions.py)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.analysis import positions
import src.analysis.cusip_map as cm

cm.OVERRIDES_FILE = "../../tests/fixtures/example_overrides.csv"


def _h(name, cusip, shares, value, put_call=""):
    itype = "COMMON_STOCK" if not put_call else ("OPTION_PUT" if put_call == "Put" else "OPTION_CALL")
    return {"name_of_issuer": name, "title_of_class": "COM", "cusip": cusip,
            "value_usd": value, "amount": shares, "amount_type": "SH",
            "put_call": put_call, "instrument_type": itype}


def build_model():
    cfg = load_config()
    cfg.raw["prices"]["enabled"] = False
    q1 = {"quarter": "Q3 2025", "report_date": "2025-09-30", "holding_count": 2,
          "holdings": [_h("Bloom Energy Corp", "093712107", 5_000_000, 600_000_000),
                       _h("Nvidia Corp", "67066G104", 0, 1_200_000_000, "Put")]}
    q2 = {"quarter": "Q4 2025", "report_date": "2025-12-31", "holding_count": 2,
          "holdings": [_h("Bloom Energy Corp", "093712107", 6_000_000, 780_000_000),
                       _h("Nvidia Corp", "67066G104", 0, 1_600_000_000, "Put")]}
    q3 = {"quarter": "Q1 2026", "report_date": "2026-03-31", "holding_count": 2,
          "holdings": [_h("Bloom Energy Corp", "093712107", 6_500_000, 879_000_000),
                       _h("Nvidia Corp", "67066G104", 0, 1_600_000_000, "Put")]}
    return positions.build(cfg, [q1, q2, q3])


def test_instrument_separation():
    m = build_model()
    # Common stock exposure must exclude option notional entirely.
    assert m["summary"]["common_stock_long_exposure_usd"] == 879_000_000
    assert m["summary"]["options_notional_exposure_usd"] == 1_600_000_000
    # No option appears in the common-stock table.
    assert all(r["instrument_type"] == "COMMON_STOCK" for r in m["common_stock"])
    assert len(m["options"]) == 1


def test_status_traffic_light():
    m = build_model()
    be = next(r for r in m["common_stock"] if r["ticker"] == "BE")
    # 5.0M -> 6.0M -> 6.5M is a steady build.
    assert be["three_quarter_trend"] == "up_up_up"
    assert be["status"] in {"strong_add"}


if __name__ == "__main__":
    test_instrument_separation()
    test_status_traffic_light()
    print("All tests passed.")
