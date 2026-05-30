"""Extract candidate position statements from discovery items.

Two extraction modes:
  1. Keyword-only  — fast, free, zero dependencies. Used as pre-filter and fallback.
  2. LLM-validated — Claude Haiku validates each keyword-matched candidate and
     classifies it into one of three signal tiers. Activated when
     ANTHROPIC_API_KEY is set and config.llm.validate_statements is true.

Signal tiers (stored as ``signal_tier`` in the returned dict):
  alpha_signal     — statement implies a NEW position NOT in the current 13F
  position_update  — statement contains NEW actionable info about a KNOWN position
                     (exit / add / reduce / strategy change)
  context          — discusses a KNOWN position but no new position info
                     (general commentary, thesis restatement) → not alerted
  unrelated        — not relevant to this fund

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
from pathlib import Path

from ..sources.discovery import DiscoveryItem

log = logging.getLogger("parsers.public_statement")

# ── In-process LLM classification cache ─────────────────────────────────────
# Keyed by content_hash. Loaded from disk on first use, written back on each
# new entry. Avoids re-calling the LLM for the same URL across multiple runs
# within a process (alert.yml fires hourly; items stay in RSS feeds for days).
_llm_cache: dict = {}
_llm_cache_path: Path | None = None
_llm_cache_loaded: bool = False


def _load_llm_cache(path: Path) -> None:
    global _llm_cache, _llm_cache_path, _llm_cache_loaded
    if _llm_cache_loaded and _llm_cache_path == path:
        return
    try:
        _llm_cache = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        _llm_cache = {}
    _llm_cache_path = path
    _llm_cache_loaded = True


def _save_llm_cache(path: Path) -> None:
    try:
        path.write_text(json.dumps(_llm_cache, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        log.warning("LLM cache write failed: %s", exc)

_LLM_SYSTEM_PROMPT_BASE = """\
You are a precise financial analyst monitoring the hedge fund "Situational Awareness LP" \
(manager: Leopold Aschenbrenner, former OpenAI) to detect investment activity early.

You receive a news headline + excerpt and the list of currently known 13F positions of the fund. \
Classify the signal into exactly one of the following tiers:

signal_tier:
  "alpha_signal"    — The statement indicates a NEW position NOT in the current 13F list. \
This is the highest information value.
  "position_update" — The statement contains NEW, actionable information about a KNOWN position \
(e.g. exit, increase, reduction, strategy change, options structure). \
Important: an exit or "I sold" on a known position is also a position_update.
  "context"         — The statement discusses a known position or sector but contains no new \
position information (general commentary, thesis repetition, explanation of an already known holding).
  "unrelated"       — Not relevant to this fund.

Respond ONLY with valid JSON:
{
  "is_relevant": true | false,
  "signal_tier": "alpha_signal" | "position_update" | "context" | "unrelated",
  "action": "buy" | "sell" | "highlight" | "announce" | "unrelated",
  "ticker": "<TICKER>" | null,
  "confidence": 0.0–1.0,
  "reason": "<one sentence explaining this tier classification>",
  "quote": "<the most salient quote or core statement from the article, max 120 chars>",
  "inference": "<what this statement implies about a possible new/changed position, \
or null if context/unrelated>",
  "action_hint": "<rough action direction, e.g. 'Possible entry into copper stocks before next 13F' \
— ONLY for alpha_signal or position_update with confidence >= 0.7, otherwise null. \
No specific trade suggestions, no price targets.>"
}

Rules:
- is_relevant = true for alpha_signal and position_update. false for context and unrelated.
- For "context": is_relevant = false (no alert needed), but still set signal_tier to "context".
- confidence: 0.9+ only for explicit position statements. 0.7–0.85 for strong implication. \
  Below 0.65 → is_relevant = false.
- Return only the JSON object — no markdown, no explanation outside it.\
"""


def _build_system_prompt(active_tickers: set[str] | None = None) -> str:
    """Build the LLM system prompt, injecting current 13F positions as context."""
    prompt = _LLM_SYSTEM_PROMPT_BASE
    if active_tickers:
        tickers_str = ", ".join(sorted(active_tickers))
        prompt += f"\n\nCurrent 13F positions of the fund (known whitelist): {tickers_str}"
    else:
        prompt += (
            "\n\nHinweis: Keine 13F-Positionsliste verfügbar. Klassifiziere konservativ — "
            "im Zweifel 'context' statt 'alpha_signal'."
        )
    return prompt


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

_ALL_PHRASES = INVEST_PHRASES + SELL_PHRASES + ANNOUNCE_PHRASES + HIGHLIGHT_PHRASES

TICKER_RE = re.compile(r"\$([A-Z]{1,5})\b|\(([A-Z]{1,5})\)")

_SOURCE_CONFIDENCE = {
    "blog": 0.85, "x": 0.75, "google_news": 0.60,
    "google_alerts": 0.55, "hackernews": 0.55, "reddit": 0.45, "rss": 0.50,
    "edgar_rss": 0.95,
}

_CATEGORY_BOOST = {"invest": 0.10, "sell": 0.10, "announce": 0.05, "highlight": 0.0}


def _categorize(matched: list[str]) -> str:
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
        "signal_tier": None,        # unknown without LLM — alert system treats as alertable
        "ticker_guess": tickers,
        "confidence": confidence,
        "needs_human_review": confidence < 0.80,
        "title": item.title,
        "excerpt": item.excerpt,
        "url": item.url,
        "source_kind": item.source_kind,
        "content_hash": item.content_hash(),
        "llm_validated": False,
        "llm_quote": "",
        "llm_inference": "",
        "llm_action_hint": "",
    }


def extract_statement_with_llm(
    item: DiscoveryItem,
    model: str = "claude-haiku-4-5-20251001",
    active_tickers: set[str] | None = None,
    cache_path: Path | None = None,
) -> dict | None:
    """Keyword pre-filter → Claude Haiku semantic validation with 3-tier classification.

    active_tickers: set of ticker symbols currently in the 13F (passed from step_discover).
    Used to distinguish alpha_signal (new) from position_update (known, new info)
    from context (known, no new info).

    cache_path: if given, LLM results are persisted by content_hash so the same
    URL is never re-classified within or across runs (RSS items stay in feeds for days).

    Falls back silently to keyword-only if ANTHROPIC_API_KEY is not set or the
    API call fails. The returned dict is compatible with extract_statement().
    """
    candidate = extract_statement(item)
    if candidate is None:
        return None

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return candidate

    content_hash = candidate["content_hash"]

    # ── Cache lookup ─────────────────────────────────────────────────────────
    if cache_path is not None:
        _load_llm_cache(cache_path)
        cached = _llm_cache.get(content_hash)
        if cached is not None:
            if cached.get("rejected"):
                log.debug("LLM cache hit (rejected): %s", item.title[:80])
                return None
            log.debug("LLM cache hit (tier=%s): %s", cached.get("signal_tier"), item.title[:80])
            candidate["signal_category"] = cached["signal_category"]
            candidate["signal_tier"] = cached["signal_tier"]
            candidate["confidence"] = cached["confidence"]
            candidate["needs_human_review"] = cached["confidence"] < 0.80
            candidate["llm_validated"] = True
            candidate["llm_reason"] = cached.get("llm_reason", "")
            candidate["llm_quote"] = cached.get("llm_quote", "")
            candidate["llm_inference"] = cached.get("llm_inference", "")
            candidate["llm_action_hint"] = cached.get("llm_action_hint", "")
            candidate["llm_analysis"] = candidate["llm_inference"]
            candidate["llm_trade_signal"] = candidate["llm_action_hint"]
            if cached.get("ticker_extra"):
                existing = set(candidate.get("ticker_guess") or [])
                candidate["ticker_guess"] = sorted(existing | set(cached["ticker_extra"]))
            return candidate

    try:
        import anthropic
        client = anthropic.Anthropic()
        system_prompt = _build_system_prompt(active_tickers)
        response = client.messages.create(
            model=model,
            max_tokens=384,
            system=[{
                "type": "text",
                "text": system_prompt,
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
        raw = response.content[0].text.strip()
        # Models sometimes wrap JSON in markdown code blocks (```json ... ```)
        # or prepend/append prose. Extract the first {...} object robustly.
        if raw.startswith("```"):
            # Strip opening fence (```json or ```)
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            # Strip closing fence
            raw = re.sub(r'\s*```\s*$', '', raw.strip())
        # Final fallback: find the outermost {...} in case there is still noise
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            raw = m.group()
        result = json.loads(raw)
    except Exception as exc:
        log.warning("LLM validation failed (%s); using keyword result. Error: %s", item.url, exc)
        return candidate

    signal_tier = result.get("signal_tier", "unrelated")

    # Discard unrelated and context signals entirely (no event needed)
    if not result.get("is_relevant") or signal_tier in ("unrelated",):
        log.debug(
            "LLM rejected item (tier=%s, reason=%s): %s",
            signal_tier, result.get("reason"), item.title[:80],
        )
        if cache_path is not None:
            _llm_cache[content_hash] = {"rejected": True}
            _save_llm_cache(cache_path)
        return None

    # context: store as event but mark tier so alert system suppresses it
    # alpha_signal / position_update: fully alertable

    action = result.get("action", candidate["signal_category"])
    if action == "buy":
        action = "invest"
    candidate["signal_category"] = action
    candidate["signal_tier"] = signal_tier
    candidate["confidence"] = round(float(result.get("confidence", candidate["confidence"])), 2)
    candidate["needs_human_review"] = candidate["confidence"] < 0.80
    candidate["llm_validated"] = True
    candidate["llm_reason"] = result.get("reason", "")
    candidate["llm_quote"] = result.get("quote") or ""
    candidate["llm_inference"] = result.get("inference") or ""
    candidate["llm_action_hint"] = result.get("action_hint") or ""

    # Keep backward compat fields
    candidate["llm_analysis"] = candidate["llm_inference"]
    candidate["llm_trade_signal"] = candidate["llm_action_hint"]

    llm_ticker = result.get("ticker")
    ticker_extra: list[str] = []
    if llm_ticker:
        existing = set(candidate.get("ticker_guess") or [])
        ticker_extra = [llm_ticker] if llm_ticker not in existing else []
        candidate["ticker_guess"] = sorted(existing | {llm_ticker})

    log.debug(
        "LLM classified (tier=%s, conf=%.2f): %s",
        signal_tier, candidate["confidence"], item.title[:80],
    )

    if cache_path is not None:
        _llm_cache[content_hash] = {
            "signal_tier": candidate["signal_tier"],
            "signal_category": candidate["signal_category"],
            "confidence": candidate["confidence"],
            "ticker_extra": ticker_extra,
            "llm_reason": candidate.get("llm_reason", ""),
            "llm_quote": candidate.get("llm_quote", ""),
            "llm_inference": candidate.get("llm_inference", ""),
            "llm_action_hint": candidate.get("llm_action_hint", ""),
        }
        _save_llm_cache(cache_path)

    return candidate
