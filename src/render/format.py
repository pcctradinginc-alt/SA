"""Presentation helpers, registered as Jinja filters by both renderers.

All number formatting is centralised here so the README and the email stay
visually consistent and every displayed number is explicitly rounded.
"""
from __future__ import annotations

DASH = "—"


def usd(value: float | int | None) -> str:
    """Compact USD: 13_676_657_000 -> '$13.7B', 421_600_000 -> '$421.6M'."""
    if value is None:
        return DASH
    v = float(value)
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1_000_000_000:
        return f"{sign}${v / 1_000_000_000:.1f}B"
    if v >= 1_000_000:
        return f"{sign}${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{sign}${v / 1_000:.1f}K"
    return f"{sign}${v:.0f}"


def usd_signed(value: float | int | None) -> str:
    if value is None:
        return DASH
    s = usd(value)
    return s if s.startswith("-") else f"+{s}"


def shares(value: int | None) -> str:
    if value is None:
        return DASH
    v = float(value)
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.0f}k"
    return f"{v:.0f}"


def pct(value: float | None) -> str:
    if value is None:
        return DASH
    return f"{value * 100:+.1f}%"


def qoq(value: float | None) -> str:
    """QoQ change: None -> 'New', -1.0 -> 'Exit', else percentage."""
    if value is None:
        return "New"
    if value <= -0.999:
        return "Exit"
    return f"{value * 100:+.1f}%"


TREND = {
    "up_up_up": "↑↑↑", "down_down": "↓↓", "new_add": "New + Add",
    "flat": "Flat", "new": "New", "mixed": "Mixed",
}


def trend(value: str | None) -> str:
    return TREND.get(value or "", value or DASH)


def usd_compact(value: float | int | None) -> str:
    """Signed compact USD for delta displays: +$474M, -$12M."""
    if value is None:
        return DASH
    v = float(value)
    sign = "+" if v >= 0 else "-"
    v = abs(v)
    if v >= 1_000_000_000:
        return f"{sign}${v / 1_000_000_000:.1f}B"
    if v >= 1_000_000:
        return f"{sign}${v / 1_000_000:.0f}M"
    if v >= 1_000:
        return f"{sign}${v / 1_000:.0f}K"
    return f"{sign}${v:.0f}"


FILTERS = {"usd": usd, "usd_signed": usd_signed, "usd_compact": usd_compact,
           "shares": shares, "pct": pct, "qoq": qoq, "trend": trend}
