"""The unified event log (data/parsed/events.jsonl) and its confidence model.

Every signal — a filing, a primary-source post, a media report — becomes one
event record with two orthogonal dimensions:

  * ``source_class``        where it came from
  * ``verification_status`` whether SEC filings confirm it

``confidence`` is derived deterministically from those two, so the README and
emails stay explainable (no opaque scores).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

from .config import Config
from .utils import append_jsonl, read_jsonl, rewrite_jsonl, utc_now_iso

# source classes
SEC_VERIFIED = "SEC_VERIFIED"
PRIMARY_SOURCE = "PRIMARY_SOURCE"
MEDIA_REPORTED = "MEDIA_REPORTED"
MARKET_INFERRED = "MARKET_INFERRED"

# verification statuses
VERIFIED = "verified"
OPEN = "open"
NOT_VERIFIABLE = "not_verifiable_via_13f"

_BASE_CONFIDENCE = {SEC_VERIFIED: 1.0, PRIMARY_SOURCE: 0.8, MEDIA_REPORTED: 0.5, MARKET_INFERRED: 0.3}

EVENTS_FILE = "events.jsonl"


def confidence_for(source_class: str, verification_status: str, base_override: float | None = None) -> float:
    """Deterministic confidence: verified events are always 1.0, else the
    source-class base (optionally overridden by an extractor's own estimate)."""
    if verification_status == VERIFIED:
        return 1.0
    base = base_override if base_override is not None else _BASE_CONFIDENCE.get(source_class, 0.3)
    return round(base, 2)


@dataclass
class Event:
    event_id: str
    timestamp: str
    person: str
    signal_type: str            # 13f_position | ownership_13dg | public_statement | sector_thesis | ...
    source_class: str
    verification_status: str
    summary: str
    entity: str | None = None
    entity_cik: str | None = None
    as_of: str | None = None
    confidence: float = 0.0
    ticker_guess: list[str] = field(default_factory=list)
    sector_watch: list[str] = field(default_factory=list)
    instrument: dict[str, Any] | None = None
    needs_human_review: bool = False
    reason_not_verifiable: str | None = None
    # Signal tier for public_statement events (set by LLM classifier):
    #   alpha_signal     — new position not in current 13F → alert
    #   position_update  — known position, new info (exit/add/reduce) → alert
    #   context          — known position, no new info → suppress alert
    #   None             — unknown (keyword-only, no LLM) → alert conservatively
    signal_tier: str | None = None
    sources: list[dict[str, Any]] = field(default_factory=list)
    links_to: list[str] = field(default_factory=list)

    def finalize(self) -> "Event":
        self.confidence = confidence_for(self.source_class, self.verification_status,
                                         self.confidence or None)
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def append_event(cfg: Config, event: Event) -> None:
    """Append an event if its event_id is not already present (idempotent)."""
    path = cfg.paths.parsed / EVENTS_FILE
    existing_ids = {e.get("event_id") for e in read_jsonl(path)}
    if event.event_id in existing_ids:
        return
    append_jsonl(path, event.finalize().to_dict())


def load_events(cfg: Config) -> list[dict[str, Any]]:
    return list(read_jsonl(cfg.paths.parsed / EVENTS_FILE))


def verify_open_statements(cfg: Config, latest_tickers: set[str]) -> int:
    """Flip OPEN statements to VERIFIED when their ticker now appears in a filing.

    Returns the number of statements newly verified. Conservative: only matches
    on an exact ticker guess, and leaves NOT_VERIFIABLE events untouched.
    """
    path = cfg.paths.parsed / EVENTS_FILE
    events = load_events(cfg)
    changed = 0
    for e in events:
        if e.get("verification_status") != OPEN:
            continue
        if e.get("signal_type") not in {"public_statement", "sector_thesis"}:
            continue
        guesses = set(e.get("ticker_guess") or [])
        if guesses & latest_tickers:
            e["verification_status"] = VERIFIED
            e["confidence"] = 1.0
            e["verified_at"] = utc_now_iso()
            changed += 1
    if changed:
        rewrite_jsonl(path, events)
    return changed
