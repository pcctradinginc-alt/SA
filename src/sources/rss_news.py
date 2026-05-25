"""Curated free RSS sources for Leo Aschenbrenner monitoring.

These feeds require no API keys and are fetched on every discovery run.
They complement the user-configured GOOGLE_ALERT_FEEDS / BLOG_FEEDS env vars.

Sources included:
  - Google News RSS (search by name + variants)
  - HackerNews RSS via hnrss.org
  - Reddit search RSS (r/MachineLearning, r/investing, r/agi, all)

All items are passed through the same DiscoveryItem → parse_public_statement
pipeline as every other source, so deduplication and confidence scoring
are applied automatically.
"""
from __future__ import annotations

from ..config import Config
from ..sources.discovery import DiscoveryItem, _parse_rss
from ..utils import HttpClient, get_logger

log = get_logger("sources.rss_news")

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


def from_all_curated(cfg: Config) -> list[DiscoveryItem]:
    """Fetch all curated free sources. Called from step_discover in pipeline."""
    items: list[DiscoveryItem] = []
    items.extend(from_edgar_rss(cfg))
    items.extend(from_google_news(cfg))
    items.extend(from_hackernews(cfg))
    items.extend(from_reddit(cfg))
    log.info("Curated news sources total: %d items.", len(items))
    return items
