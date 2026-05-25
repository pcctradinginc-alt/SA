"""Extract candidate position statements from discovery items.

Two extraction modes:
  1. Keyword-only  — fast, free, zero dependencies. Used as pre-filter and fallback.
  2. LLM-validated — Claude Haiku validates each keyword-matched candidate for real
     semantic relevance and extracts structured fields. Activated when
     ANTHROPIC_API_KEY is set and config.llm.validate_statements is true.

Signal categories (stored as ``signal_category`` in the returned dict):
  invest    — buying / entering a position
  sell      — selling / exiting / trimming
  announce  — fund launch, new vehicle, press release
  highlight — thesis, sector conviction, company endorsement
"""
from __future__ import annotations

import json
import logging
import os
import re

from ..sources.discovery import DiscoveryItem

log = logging.getLogger("parsers.public_statement")

_LLM_SYSTEM_PROMPT = """\
You are a financial signal classifier for a hedge fund monitoring system.

The fund you monitor is "Situational Awareness LP", managed by Leopold Aschenbrenner (ex-OpenAI researcher). It holds long equity positions (CoreWeave, Core Scientific, Bloom Energy, SandDisk, Intel, etc.) and large put options on Nvidia and other AI chip companies.

You receive a news headline + excerpt. Classify whether it contains actionable investment signal about THIS fund's positions or moves.

Return ONLY valid JSON with these fields:
{
  "is_relevant": true | false,
  "action": "buy" | "sell" | "highlight" | "announce" | "unrelated",
  "ticker": "<TICKER>" | null,
  "confidence": 0.0–1.0,
  "reason": "<one short sentence>"
}

Rules:
- is_relevant = true ONLY if the article specifically reports on this fund's investment moves (new stake, exit, increase, decrease, short position, fund-level announcement).
- is_relevant = false for: general AI news, opinion pieces that merely mention Aschenbrenner, interviews not about positions, articles about other funds.
- confidence reflects how clearly the headline/excerpt supports your classification (not how important the news is).
- ticker: the primary stock ticker mentioned in the context of the fund's position, or null.
- Return ONLY the JSON object — no markdown, no explanation outside the JSON."""

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
    """Keyword-only extraction. Fast, free, used as pre-filter and fallback."""
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
        "llm_validated": False,
    }


def extract_statement_with_llm(item: DiscoveryItem, model: str = "claude-haiku-4-5-20251001") -> dict | None:
    """Keyword pre-filter → Claude Haiku semantic validation.

    Falls back silently to keyword-only if ANTHROPIC_API_KEY is not set or the
    API call fails. The returned dict is compatible with extract_statement().
    """
    candidate = extract_statement(item)
    if candidate is None:
        return None

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return candidate

    try:
        import anthropic
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=model,
            max_tokens=256,
            system=[{
                "type": "text",
                "text": _LLM_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{
                "role": "user",
                "content": (
                    f"Title: {item.title}\n"
                    f"Excerpt: {item.excerpt}\n"
                    f"Source: {item.source_kind}"
                ),
            }],
        )
        result = json.loads(response.content[0].text)
    except Exception as exc:
        log.warning("LLM validation failed (%s); using keyword result. Error: %s", item.url, exc)
        return candidate

    if not result.get("is_relevant"):
        log.debug("LLM rejected item (action=%s, reason=%s): %s", result.get("action"), result.get("reason"), item.title[:80])
        return None

    # Merge LLM assessment into candidate
    action = result.get("action", candidate["signal_category"])
    if action in ("buy",):
        action = "invest"
    candidate["signal_category"] = action
    candidate["confidence"] = round(float(result.get("confidence", candidate["confidence"])), 2)
    candidate["needs_human_review"] = candidate["confidence"] < 0.80
    candidate["llm_validated"] = True
    candidate["llm_reason"] = result.get("reason", "")

    llm_ticker = result.get("ticker")
    if llm_ticker:
        existing = set(candidate.get("ticker_guess") or [])
        candidate["ticker_guess"] = sorted(existing | {llm_ticker})

    return candidate
