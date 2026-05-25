"""Extract candidate position statements from discovery items.

This is intentionally conservative keyword matching with a confidence floor.
It NEVER fabricates a quote: it only flags an item and records the matched
phrase + a short excerpt of the source. High-precision NER / LLM extraction
can be layered on later behind the same interface.
"""
from __future__ import annotations

import re

from ..sources.discovery import DiscoveryItem

# Phrases that suggest a position-relevant statement.
POSITION_PHRASES = [
    "large position", "major position", "opening a position", "building a position",
    "accumulating", "we are buying", "bought", "stake", "major stake", "short",
    "long ", "betting against", "investment in",
]

# Naive ticker pattern: 1-5 uppercase letters in parentheses or after a $.
TICKER_RE = re.compile(r"\$([A-Z]{1,5})\b|\(([A-Z]{1,5})\)")


def extract_statement(item: DiscoveryItem) -> dict | None:
    """Return a statement record if the item looks position-relevant, else None."""
    text = f"{item.title} {item.excerpt}".lower()
    matched = [p for p in POSITION_PHRASES if p in text]
    if not matched:
        return None

    tickers = sorted({m[0] or m[1] for m in TICKER_RE.findall(f"{item.title} {item.excerpt}")})
    # Confidence floor by source kind; media is lower than primary source.
    base = {"blog": 0.8, "x": 0.7, "google_alerts": 0.5, "rss": 0.5}.get(item.source_kind, 0.5)

    return {
        "matched_phrases": matched,
        "ticker_guess": tickers,
        "confidence": base,
        "needs_human_review": base < 0.8,
        "title": item.title,
        "excerpt": item.excerpt,
        "url": item.url,
        "source_kind": item.source_kind,
        "content_hash": item.content_hash(),
    }
