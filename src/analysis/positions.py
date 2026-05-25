"""Build the 3-quarter position table — instrument-separated.

The single most important analytical rule of this project: common-stock longs
and option notional are NEVER combined. Scores ("Reported Position Change")
apply to common stock only; options are shown as reported notional with an
explicit "direction unknown" caveat.

Output is a plain dict, ready for both the README and the email renderers, and
is also persisted to data/derived/position_table.json.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config
from ..utils import get_logger, write_json, today_iso
from . import cusip_map, prices
from ..parsers.parse_13f import COMMON_STOCK, OPTION_PUT, OPTION_CALL

log = get_logger("analysis.positions")

STATUS_STRONG_ADD = "strong_add"
STATUS_NEW_ADD = "new_add"
STATUS_NEW_BUY = "new_buy"
STATUS_HOLD = "hold"
STATUS_TRIM = "trim"
STATUS_EXIT = "exit"

STATUS_ICON = {
    STATUS_STRONG_ADD: "🟢", STATUS_NEW_ADD: "🟢", STATUS_NEW_BUY: "🟡",
    STATUS_HOLD: "🟡", STATUS_TRIM: "🔴", STATUS_EXIT: "⚫",
}
STATUS_LABEL = {
    STATUS_STRONG_ADD: "Strong Add", STATUS_NEW_ADD: "New + Add", STATUS_NEW_BUY: "New Buy",
    STATUS_HOLD: "Hold", STATUS_TRIM: "Trim", STATUS_EXIT: "Exit",
}

_SIGNAL_STATUSES = {STATUS_STRONG_ADD, STATUS_NEW_ADD, STATUS_NEW_BUY}
_SCORE_THRESHOLD = 5.0
_TOP_SIGNALS_N = 5


def _conviction_score(row: dict) -> float:
    """Weighted 0–10 conviction score for copy-trading decisions.

    Components (max):
      Portfolio weight   2.5  — primary conviction indicator
      QoQ $ delta        2.5  — dollar add beats raw percentage (avoids 1→200k noise)
      3-quarter trend    2.0  — consistency across quarters
      Absolute value     1.5  — avoids micro-positions dominating
      Price opportunity  1.0  — penalise if >50% already moved since filing
      13D/G bonus       +1.5  — ownership filing = stronger/faster signal
      (total capped at 10.0)
    """
    w = row["portfolio_weight_common_stock"]
    dollar_delta = row.get("qoq_dollar_delta", 0)
    is_new = row.get("value_prev_quarter_usd", -1) == 0
    trend = row.get("three_quarter_trend", "")
    val = row.get("value_latest_usd", 0)
    pm = row.get("price_change_since_quarter_end_pct")
    has_13dg = row.get("has_13dg", False)

    if w >= 0.15:       w_score = 2.5
    elif w >= 0.08:     w_score = 2.0
    elif w >= 0.03:     w_score = 1.5
    elif w >= 0.01:     w_score = 1.0
    else:               w_score = 0.5

    # Dollar delta: new buys scored on full position size
    if is_new:
        q_score = 2.0 if val >= 50_000_000 else 1.5
    elif dollar_delta >= 200_000_000:  q_score = 2.5
    elif dollar_delta >= 100_000_000:  q_score = 2.0
    elif dollar_delta >= 50_000_000:   q_score = 1.5
    elif dollar_delta >= 20_000_000:   q_score = 1.0
    elif dollar_delta >= 5_000_000:    q_score = 0.5
    else:                              q_score = 0.2

    t_score = {"up_up_up": 2.0, "new_add": 1.8, "new": 1.4,
               "mixed": 0.8, "flat": 0.4, "down_down": 0.0}.get(trend, 0.6)

    if val >= 400_000_000:    v_score = 1.5
    elif val >= 200_000_000:  v_score = 1.1
    elif val >= 100_000_000:  v_score = 0.8
    elif val >= 25_000_000:   v_score = 0.5
    else:                     v_score = 0.2

    if pm is None:      p_score = 0.8   # unknown, neutral
    elif pm > 0.5:      p_score = 0.0   # ship has sailed (+50%+)
    elif pm > 0.2:      p_score = 0.4   # partially sailed
    else:               p_score = 1.0   # still opportunity (or dip)

    dg_bonus = 1.5 if has_13dg else 0.0

    return min(10.0, round(w_score + q_score + t_score + v_score + p_score + dg_bonus, 1))


def _build_top_signals(common_rows: list[dict]) -> list[dict]:
    """Return the top copy-trade signals ranked by conviction score."""
    signals = []
    for r in common_rows:
        if r.get("status") not in _SIGNAL_STATUSES:
            continue
        if r.get("value_latest_usd", 0) < 25_000_000:
            continue
        # Require meaningful dollar add (or new buy)
        if r.get("value_prev_quarter_usd", -1) != 0 and r.get("qoq_dollar_delta", 0) < 5_000_000:
            continue
        score = _conviction_score(r)
        if score < _SCORE_THRESHOLD:
            continue
        if score >= 8.0:      rec = "Sofort nachkaufen"
        elif score >= 6.5:    rec = "Stark nachkaufen"
        else:                 rec = "Aufbauen"
        signals.append({
            "ticker": r["ticker"],
            "issuer": r["issuer"],
            "status_label": r["status_label"],
            "has_13dg": r.get("has_13dg", False),
            "portfolio_weight_common_stock": r["portfolio_weight_common_stock"],
            "value_latest_usd": r["value_latest_usd"],
            "qoq_dollar_delta": r.get("qoq_dollar_delta", 0),
            "qoq_share_change_pct": r.get("qoq_share_change_pct"),
            "three_quarter_trend": r["three_quarter_trend"],
            "price_change_since_quarter_end_pct": r.get("price_change_since_quarter_end_pct"),
            "conviction_score": score,
            "recommendation": rec,
        })
    signals.sort(key=lambda x: x["conviction_score"], reverse=True)
    return signals[:_TOP_SIGNALS_N]


@dataclass
class Series:
    """A single instrument tracked across the trailing quarters (by CUSIP)."""

    cusip: str
    issuer: str
    instrument_type: str
    shares: list[int] = field(default_factory=list)   # per quarter, oldest->newest
    values: list[int] = field(default_factory=list)   # per quarter, USD


def _collect(parsed_quarters: list[dict], instrument_types: set[str]) -> dict[str, Series]:
    """Group holdings by CUSIP across quarters for the given instrument types."""
    n = len(parsed_quarters)
    series: dict[str, Series] = {}
    for qi, parsed in enumerate(parsed_quarters):
        for h in parsed.get("holdings", []):
            if h["instrument_type"] not in instrument_types:
                continue
            key = h["cusip"] + "|" + h.get("put_call", "")
            s = series.get(key)
            if s is None:
                s = Series(cusip=h["cusip"], issuer=h["name_of_issuer"],
                           instrument_type=h["instrument_type"],
                           shares=[0] * n, values=[0] * n)
                series[key] = s
            s.shares[qi] += int(h["amount"])
            s.values[qi] += int(h["value_usd"])
    return series


def _qoq(prev: float, latest: float) -> float | None:
    if prev == 0 and latest > 0:
        return None          # "new" — no percentage
    if prev > 0 and latest == 0:
        return -1.0          # full exit
    if prev == 0 and latest == 0:
        return 0.0
    return (latest - prev) / prev


def _trend(shares: list[int]) -> str:
    a, b, c = (shares + [0, 0, 0])[:3] if len(shares) < 3 else shares[-3:]
    if a == 0 and b == 0 and c > 0:
        return "new"
    if a == 0 and b > 0 and c > b:
        return "new_add"
    if a < b < c:
        return "up_up_up"
    if a > b > c:
        return "down_down"
    if a == b == c:
        return "flat"
    return "mixed"


def _status(shares: list[int], weight: float, cfg: Config) -> str:
    a, b, c = (shares[-3:] + [0, 0, 0])[:3]
    if c == 0:
        return STATUS_EXIT
    if a == 0 and b == 0 and c > 0:
        return STATUS_NEW_BUY
    if a == 0 and b > 0 and c > b:
        return STATUS_NEW_ADD
    qoq = _qoq(b, c)
    if qoq is None:
        return STATUS_NEW_BUY
    if a < b < c and weight >= 0.01:
        return STATUS_STRONG_ADD
    if qoq < cfg.trim_threshold:
        return STATUS_TRIM
    if abs(qoq) <= cfg.hold_band:
        return STATUS_HOLD
    return STATUS_STRONG_ADD if qoq > 0 else STATUS_TRIM


def build(cfg: Config, parsed_quarters: list[dict],
          cusips_with_13dg: frozenset[str] = frozenset()) -> dict:
    """Build the full instrument-separated position model from N parsed quarters.

    ``parsed_quarters`` must be sorted oldest -> newest.
    ``cusips_with_13dg`` is a set of CUSIPs that have associated 13D/G filings,
    used to boost the conviction score for those positions.
    """
    if not parsed_quarters:
        return {"available": False}

    overrides = cusip_map.load_overrides(cfg)
    latest = parsed_quarters[-1]
    quarter_labels = [p["quarter"] for p in parsed_quarters]
    report_date = latest["report_date"]

    # ── common stock ─────────────────────────────────────────────────────────
    common = _collect(parsed_quarters, {COMMON_STOCK})
    total_common_value = sum(s.values[-1] for s in common.values()) or 1

    common_rows: list[dict] = []
    new_buys: list[dict] = []
    exits: list[dict] = []

    for s in common.values():
        info = cusip_map.resolve(s.cusip, s.issuer, overrides)
        weight = s.values[-1] / total_common_value
        shares_latest = s.shares[-1]

        q_end_price = current = None
        price_move = est_value = est_move = None
        if cfg.prices_enabled and info["yfinance_symbol"]:
            q_end_price = prices.quarter_end_price(info["yfinance_symbol"], report_date)
            current = prices.current_price(info["yfinance_symbol"])
            if q_end_price and current:
                price_move = round((current - q_end_price) / q_end_price, 4)
                est_value = round(shares_latest * current)
                est_move = round(shares_latest * (current - q_end_price))

        value_prev = s.values[-2] if len(s.values) > 1 else 0
        status = _status(s.shares, weight, cfg)
        row = {
            "cusip": s.cusip,
            "ticker": info["ticker"],
            "issuer": s.issuer,
            "sector": info["sector"],
            "instrument_type": COMMON_STOCK,
            "shares_by_quarter": s.shares,
            "shares_latest": shares_latest,
            "value_latest_usd": s.values[-1],
            "value_prev_quarter_usd": value_prev,
            "qoq_dollar_delta": s.values[-1] - value_prev,
            "portfolio_weight_common_stock": round(weight, 4),
            "qoq_share_change_pct": _qoq(s.shares[-2] if len(s.shares) > 1 else 0, shares_latest),
            "three_quarter_trend": _trend(s.shares),
            "has_13dg": s.cusip in cusips_with_13dg,
            "price_at_quarter_end": q_end_price,
            "current_price": current,
            "price_change_since_quarter_end_pct": price_move,
            "estimated_current_value": est_value,
            "estimated_value_change_since_quarter_end": est_move,
            "status": status,
            "status_icon": STATUS_ICON[status],
            "status_label": STATUS_LABEL[status],
            "data_quality": {"cusip_ticker_mapping": info["mapping"],
                             "price_source": cfg.raw["prices"]["source"] if current else "unavailable"},
        }
        if status == STATUS_EXIT:
            exits.append(row)
        else:
            common_rows.append(row)
            prev = s.shares[-2] if len(s.shares) > 1 else 0
            if prev == 0 and shares_latest > 0:
                new_buys.append(row)

    common_rows.sort(key=lambda r: r["value_latest_usd"], reverse=True)
    top_signals = _build_top_signals(common_rows)

    # ── options ──────────────────────────────────────────────────────────────
    options = _collect(parsed_quarters, {OPTION_PUT, OPTION_CALL})
    total_option_notional = sum(s.values[-1] for s in options.values())
    option_rows: list[dict] = []
    for s in options.values():
        info = cusip_map.resolve(s.cusip, s.issuer, overrides)
        underlying_move = None
        if cfg.prices_enabled and info["yfinance_symbol"]:
            qe = prices.quarter_end_price(info["yfinance_symbol"], report_date)
            cur = prices.current_price(info["yfinance_symbol"])
            if qe and cur:
                underlying_move = round((cur - qe) / qe, 4)
        option_rows.append({
            "underlying": s.issuer,
            "ticker": info["ticker"],
            "instrument": "PUT" if s.instrument_type == OPTION_PUT else "CALL",
            "notional_by_quarter": s.values,
            "notional_latest_usd": s.values[-1],
            "qoq_notional_change_pct": _qoq(s.values[-2] if len(s.values) > 1 else 0, s.values[-1]),
            "underlying_price_move": underlying_move,
            "interpretation_risk": "Notional, not premium; long/short direction unknown",
        })
    option_rows.sort(key=lambda r: r["notional_latest_usd"], reverse=True)

    # ── summary ────────────────────────────────────────────────────────────────
    increased = [r for r in common_rows if isinstance(r["qoq_share_change_pct"], float) and r["qoq_share_change_pct"] > cfg.hold_band]
    reduced = [r for r in common_rows if isinstance(r["qoq_share_change_pct"], float) and r["qoq_share_change_pct"] < -cfg.hold_band]
    priced = [r for r in common_rows
              if r["price_change_since_quarter_end_pct"] is not None
              and r["portfolio_weight_common_stock"] >= 0.005]
    best = max(priced, key=lambda r: r["price_change_since_quarter_end_pct"], default=None)
    worst = min(priced, key=lambda r: r["price_change_since_quarter_end_pct"], default=None)
    # Use dollar value as ranking basis, not percentage — avoids micro-positions
    # (e.g. 1→202k shares looks like +20M% but is only 0.2% of portfolio) dominating.
    _meaningful = [r for r in common_rows if r["portfolio_weight_common_stock"] >= 0.005]
    largest_add = max(
        [r for r in _meaningful if isinstance(r.get("qoq_share_change_pct"), float) and r["qoq_share_change_pct"] > 0],
        key=lambda r: r["value_latest_usd"], default=None,
    )
    largest_trim = min(
        [r for r in _meaningful if isinstance(r.get("qoq_share_change_pct"), float) and r["qoq_share_change_pct"] < 0],
        key=lambda r: r["qoq_share_change_pct"], default=None,
    )

    summary = {
        "latest_quarter": latest["quarter"],
        "report_date": report_date,
        "reported_holdings": latest["holding_count"],
        "reported_13f_value_usd": sum(h["value_usd"] for h in latest["holdings"]),
        "common_stock_long_exposure_usd": total_common_value if common else 0,
        "options_notional_exposure_usd": total_option_notional,
        "new_common_positions": len(new_buys),
        "increased_common_positions": len(increased),
        "reduced_common_positions": len(reduced),
        "exited_common_positions": len(exits),
        "largest_add": {"ticker": largest_add["ticker"], "issuer": largest_add["issuer"]} if largest_add else None,
        "largest_trim": {"ticker": largest_trim["ticker"], "issuer": largest_trim["issuer"]} if largest_trim else None,
        "best_performer": {"ticker": best["ticker"], "move": best["price_change_since_quarter_end_pct"]} if best else None,
        "worst_performer": {"ticker": worst["ticker"], "move": worst["price_change_since_quarter_end_pct"]} if worst else None,
    }

    model = {
        "available": True,
        "generated": today_iso(),
        "manager": cfg.primary_name,
        "quarter_labels": quarter_labels,
        "price_source": cfg.raw["prices"]["source"],
        "summary": summary,
        "common_stock": common_rows,
        "new_buys": new_buys,
        "exits": exits,
        "options": option_rows,
        "top_signals": top_signals,
    }
    write_json(cfg.paths.derived / "position_table.json", model)
    return model
