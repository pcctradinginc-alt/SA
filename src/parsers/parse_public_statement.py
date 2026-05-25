"""Extract candidate position statements from discovery items.

This is intentionally conservative keyword matching with a confidence floor.
It NEVER fabricates a quote: it only flags an item and records the matched
phrase + a short excerpt of the source. High-precision NER / LLM extraction
can be layered on later behind the same interface.

Signal categories (also stored as ``signal_category`` in the returned dict):
  invest    — buying / entering a position
  sell      — selling / exiting / trimming
  announce  — fund launch, new vehicle, press release
  highlight — thesis, sector conviction, company endorsement
"""
from __future__ import annotations

import re

from ..sources.discovery import DiscoveryItem

# ── Phrase tables by signal category ────────────────────────────────────────
INVEST_PHRASES = [
    "invested in", "investing in", "investment in", "backed",
    "seed round", "series a", "series b", "series c", "venture",
    "stake in", "position in", "building a position", "opening a position",
    "accumulating", "we are buying", "bought", "large position", "major position",
    "major stake", "long position", "took a stake", "lead investor",
    "co-invested", "co-invest", "portfolio company",
]

SELL_PHRASES = [
    "sold", "selling stake", "sold stake", "exited", "divested",
    "liquidating", "trimmed", "trimmed position", "reduced position",
    "closed position", "sold out", "exit position", "sold shares",
    "reducing exposure", "pared back",
]

ANNOUNCE_PHRASES = [
    "announced", "launching", "new fund", "raising capital", "raising a fund",
    "press release", "launched a fund", "first close", "final close",
    "raising $", "raising €", "spac", "ipo backed", "blank-check",
]

HIGHLIGHT_PHRASES = [
    "bullish on", "excited about", "strong conviction", "high conviction",
    "believe in", "thesis on", "sector thesis", "key player", "leading company",
    "compelling opportunity", "betting on", "bet on", "critical infrastructure",
    "important company", "very bullish", "incredible company", "huge opportunity",
    "stands out", "worth watching",
]

# All phrases in one flat list for quick membership check
_ALL_PHRASES = INVEST_PHRASES + SELL_PHRASES + ANNOUNCE_PHRASES + HIGHLIGHT_PHRASES

# Naive ticker pattern: 1-5 uppercase letters in parentheses or after a $.
TICKER_RE = re.compile(r"\$([A-Z]{1,5})\b|\(([A-Z]{1,5})\)")

# Source-kind → base confidence
_SOURCE_CONFIDENCE = {
    "blog": 0.85, "x": 0.75, "google_news": 0.60,
    "google_alerts": 0.55, "hackernews": 0.55, "reddit": 0.45, "rss": 0.50,
}

# Category → confidence boost on top of source base
_CATEGORY_BOOST = {"invest": 0.10, "sell": 0.10, "announce": 0.05, "highlight": 0.0}


def _categorize(matched: list[str]) -> str:
    """Return the dominant signal category for the matched phrases."""
    counts = {"invest": 0, "sell": 0, "announce": 0, "highlight": 0}
    for p in matched:
        if p in INVEST_PHRASES:
            counts["invest"] += 1
        elif p in SELL_PHRASES:
            counts["sell"] += 1
        elif p in ANNOUNCE_PHRASES:
            counts["announce"] += 1
        elif p in HIGHLIGHT_PHRASES:
            counts["highlight"] += 1
    return max(counts, key=lambda k: counts[k])


def extract_statement(item: DiscoveryItem) -> dict | None:
    """Return a statement record if the item looks position-relevant, else None."""
    text = f"{item.title} {item.excerpt}".lower()
    matched = [p for p in _ALL_PHRASES if p in text]
    if not matched:
        return None

    tickers = sorted({m[0] or m[1] for m in TICKER_RE.findall(f"{item.title} {item.excerpt}")})
    base = _SOURCE_CONFIDENCE.get(item.source_kind, 0.50)
    category = _categorize(matched)
    confidence = min(1.0, round(base + _CATEGORY_BOOST[category], 2))

    return {
        "matched_phrases": matched,
        "signal_category": category,
        "ticker_guess": tickers,
        "confidence": confidence,
        "needs_human_review": confidence < 0.80,
        "title": item.title,
        "excerpt": item.excerpt,
        "url": item.url,
        "source_kind": item.source_kind,
        "content_hash": item.content_hash(),
    }
