"""Curated free RSS sources for Leo Aschenbrenner monitoring.

These feeds require no API keys and are fetched on every discovery run.
They complement the user-configured GOOGLE_ALERT_FEEDS / BLOG_FEEDS env vars.

Sources included:
  - Nitter RSS — @leopoldasch timeline via public Nitter instances (no API key)
  - Google News RSS (search by name + variants)
  - HackerNews RSS via hnrss.org
  - Reddit search RSS (r/MachineLearning, r/investing, r/agi, all)

All items are passed through the same DiscoveryItem → parse_public_statement
pipeline as every other source, so deduplication and confidence scoring
are applied automatically.
"""
from __future__ import annotations

import json
import urllib.parse

from ..config import Config
from ..sources.discovery import DiscoveryItem, _parse_rss
from ..utils import HttpClient, get_logger, read_json, write_json, utc_now_iso

log = get_logger("sources.rss_news")

# Nitter instances — tried in order; last successful instance is persisted to
# data/derived/nitter_state.json and moved to front of list on the next run.
# Update this list when instances go down (community list: github.com/zedeus/nitter/wiki/Instances)
_X_HANDLE = "leopoldasch"
_NITTER_INSTANCES = [
    "https://nitter.poast.org",
    "https://nitter.net",
    "https://nitter.privacydev.net",
    "https://nitter.cz",
    "https://bird.trom.tf",
    "https://nitter.1d4.us",
    "https://nitter.mint.lgbt",
    "https://nitter.unixfox.eu",
    "https://nitter.moomoo.me",
    "https://lightbrd.com",
]
_NITTER_STATE_KEY = "nitter_last_working"

# Google News RSS — free, no API key, throttle-tolerant (30 s between calls is fine)
_GOOGLE_NEWS_QUERIES = [
    '"Leopold Aschenbrenner"',
    '"Leopold Aschenbrenner" invest',
    '"Situational Awareness LP"',
    '"Leopold Aschenbrenner" fund',
]
_GNEWS_BASE = "https://news.google.com/rss/search?hl=en-US&gl=US&ceid=US:en&q="

# HackerNews via hnrss.org — free, no auth required
_HN_FEEDS = [
    "https://hnrss.org/newest?q=Aschenbrenner",
    "https://hnrss.org/newest?q=%22Situational+Awareness%22+invest",
]

# Reddit search RSS — free, no auth required
_REDDIT_FEEDS = [
    "https://www.reddit.com/search.rss?q=%22Leopold+Aschenbrenner%22&sort=new&t=week",
    "https://www.reddit.com/r/MachineLearning/search.rss?q=Aschenbrenner&sort=new&t=week&restrict_sr=1",
    "https://www.reddit.com/r/investing/search.rss?q=Aschenbrenner&sort=new&t=week&restrict_sr=1",
    "https://www.reddit.com/r/agi/search.rss?q=Aschenbrenner&sort=new&t=week&restrict_sr=1",
]


def _fetch_feed(client: HttpClient, url: str, source_kind: str) -> list[DiscoveryItem]:
    try:
        return _parse_rss(client.get_text(url, timeout=20), source_kind)
    except Exception as exc:  # noqa: BLE001
        log.warning("Feed failed (%s): %s", url, exc)
        return []


def _nitter_state_path(cfg: Config):
    return cfg.paths.derived / "nitter_state.json"


def from_nitter_x(cfg: Config) -> list[DiscoveryItem]:
    """@leopoldasch timeline via public Nitter RSS — no API key required.

    Tries each Nitter instance in order and returns items from the first that
    responds successfully. The last successful instance is persisted so it is
    tried first on the next run, cutting latency when an instance is stable.
    """
    # Load last known-good instance and promote it to front
    state = read_json(str(_nitter_state_path(cfg))) or {}
    last_working = state.get(_NITTER_STATE_KEY, "")
    instances = list(_NITTER_INSTANCES)
    if last_working and last_working in instances:
        instances.remove(last_working)
        instances.insert(0, last_working)

    client = HttpClient(cfg.sec_user_agent, cfg.sec_request_delay)
    for instance in instances:
        url = f"{instance}/{_X_HANDLE}/rss"
        try:
            text = client.get_text(url, timeout=15)
            if not text or "<rss" not in text.lower():
                log.debug("Nitter %s returned no RSS content", instance)
                continue
            items = _parse_rss(text, "x")
            if items:
                log.info("Nitter (%s): %d tweet(s) from @%s.", instance, len(items), _X_HANDLE)
                # Persist this instance so it's tried first next run
                if instance != last_working:
                    write_json(str(_nitter_state_path(cfg)),
                               {_NITTER_STATE_KEY: instance, "updated": utc_now_iso()})
                return items
            log.debug("Nitter %s: empty feed", instance)
        except Exception as exc:  # noqa: BLE001
            log.debug("Nitter %s failed: %s", instance, exc)
    log.warning("Nitter: all instances failed for @%s — X source unavailable.", _X_HANDLE)
    return []


def from_google_news(cfg: Config) -> list[DiscoveryItem]:
    """Google News RSS — one request per search query, no key needed."""
    import urllib.parse
    client = HttpClient(cfg.sec_user_agent, cfg.sec_request_delay)
    out: list[DiscoveryItem] = []
    for q in _GOOGLE_NEWS_QUERIES:
        url = _GNEWS_BASE + urllib.parse.quote(q)
        items = _fetch_feed(client, url, "google_news")
        out.extend(items)
        log.debug("google_news: %d items for %r", len(items), q)
    log.info("Google News: %d total items across %d queries.", len(out), len(_GOOGLE_NEWS_QUERIES))
    return out


def from_hackernews(cfg: Config) -> list[DiscoveryItem]:
    """HackerNews RSS via hnrss.org."""
    client = HttpClient(cfg.sec_user_agent, cfg.sec_request_delay)
    out: list[DiscoveryItem] = []
    for url in _HN_FEEDS:
        items = _fetch_feed(client, url, "hackernews")
        out.extend(items)
    log.info("HackerNews: %d items.", len(out))
    return out


def from_reddit(cfg: Config) -> list[DiscoveryItem]:
    """Reddit search RSS — free, no credentials required."""
    client = HttpClient(cfg.sec_user_agent, cfg.sec_request_delay)
    out: list[DiscoveryItem] = []
    for url in _REDDIT_FEEDS:
        items = _fetch_feed(client, url, "reddit")
        out.extend(items)
    log.info("Reddit: %d items.", len(out))
    return out


def from_edgar_rss(cfg: Config) -> list[DiscoveryItem]:
    """EDGAR Atom feed for every filing by the watched CIKs.

    This is the fastest free signal for new SC 13D/G filings — EDGAR publishes
    the feed within minutes of acceptance, well before the JSON submissions
    endpoint updates. Items that are already handled by step_fetch (via
    collect_new_filings) are still surfaced here so discovery sees them too.
    """
    client = HttpClient(cfg.sec_user_agent, cfg.sec_request_delay)
    out: list[DiscoveryItem] = []
    for cik in cfg.all_ciks:
        cik_int = str(int(cik))
        url = (
            "https://www.sec.gov/cgi-bin/browse-edgar"
            f"?action=getcompany&CIK={cik_int}&type=&dateb=&owner=include"
            "&count=20&search_text=&output=atom"
        )
        items = _fetch_feed(client, url, "edgar_rss")
        out.extend(items)
    log.info("EDGAR RSS: %d items across %d CIK(s).", len(out), len(cfg.all_ciks))
    return out


def from_edgar_form_d(cfg: Config) -> list[DiscoveryItem]:
    """Search EDGAR full-text for Form D filings mentioning Situational Awareness LP.

    Form D is filed by the COMPANY raising private capital. When SA LP invests
    in a pre-IPO company (e.g. CoreWeave), that company files Form D and may
    list SA LP as a related person / investor. This surfaces those filings.

    Uses EDGAR EFTS (Electronic Full-Text Search) — no API key required.
    """
    client = HttpClient(cfg.sec_user_agent, cfg.sec_request_delay)
    out: list[DiscoveryItem] = []
    seen_accessions: set[str] = set()

    queries = ['"Situational Awareness LP"', '"Situational Awareness Fund"']
    for q in queries:
        url = (
            "https://efts.sec.gov/LATEST/search-index"
            f"?q={urllib.parse.quote(q)}&forms=D&dateRange=custom&startdt=2024-01-01"
        )
        try:
            text = client.get_text(url, timeout=20)
            data = json.loads(text)
            hits = data.get("hits", {}).get("hits", [])
            for h in hits:
                src = h.get("_source", {})
                accno = src.get("accession_no", "")
                if not accno or accno in seen_accessions:
                    continue
                seen_accessions.add(accno)
                entity = src.get("entity_name", "Unknown Company")
                file_date = src.get("file_date", "")
                edgar_url = (
                    "https://www.sec.gov/cgi-bin/browse-edgar"
                    f"?action=getcompany&filenum=&State=0&SIC=&dateb=&owner=include"
                    f"&count=1&search_text=&accession={urllib.parse.quote(accno)}"
                )
                out.append(DiscoveryItem(
                    title=f"Form D: {entity} — SA LP mentioned",
                    excerpt=(
                        f"Private placement: {entity} filed Form D on {file_date}. "
                        f"Situational Awareness LP appears as investor/related person."
                    ),
                    url=edgar_url,
                    source_kind="edgar_rss",
                ))
        except Exception as exc:  # noqa: BLE001
            log.warning("EDGAR Form D search failed for %r: %s", q, exc)

    log.info("EDGAR Form D: %d filing(s) mentioning SA LP.", len(out))
    return out


def from_all_curated(cfg: Config) -> list[DiscoveryItem]:
    """Fetch all curated free sources. Called from step_discover in pipeline."""
    items: list[DiscoveryItem] = []
    items.extend(from_edgar_rss(cfg))
    items.extend(from_edgar_form_d(cfg))
    items.extend(from_nitter_x(cfg))
    items.extend(from_google_news(cfg))
    items.extend(from_hackernews(cfg))
    items.extend(from_reddit(cfg))
    log.info("Curated news sources total: %d items.", len(items))
    return items
