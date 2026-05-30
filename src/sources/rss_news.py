"""Curated free RSS sources for Leo Aschenbrenner monitoring.

These feeds require no API keys and are fetched on every discovery run.
They complement the user-configured GOOGLE_ALERT_FEEDS / BLOG_FEEDS env vars.

Sources included:
  - situational-awareness.ai/feed/ — Leo's primary publication (highest confidence)
  - Nitter RSS — @leopoldasch timeline via public Nitter instances (no API key)
  - ScrapeCreators API — @leopoldasch tweets via api.scrapecreators.com (free tier,
    requires SCRAPE_CREATORS_API_KEY env var; runs in parallel with Nitter so either
    source alone is sufficient)
  - Google News RSS (search by name + variants)
  - Reddit search RSS (r/MachineLearning, r/investing, r/agi, all)

All items are passed through the same DiscoveryItem → parse_public_statement
pipeline as every other source, so deduplication and confidence scoring
are applied automatically.
"""
from __future__ import annotations

import json
import os
import urllib.parse

from ..config import Config
from ..sources.discovery import DiscoveryItem, _parse_rss
from ..utils import HttpClient, get_logger

log = get_logger("sources.rss_news")

_X_HANDLE = "leopoldasch"
# twiiit.com auto-selects a live Nitter instance. nitter.net is the fallback
# for when twiiit.com redirects to an instance that returns 403.
_NITTER_URLS = [
    f"https://twiiit.com/{_X_HANDLE}/rss",
    f"https://nitter.net/{_X_HANDLE}/rss",
]

# Google News RSS — free, no API key, throttle-tolerant (30 s between calls is fine)
_GOOGLE_NEWS_QUERIES = [
    '"Leopold Aschenbrenner"',
    '"Leopold Aschenbrenner" invest',
    '"Situational Awareness LP"',
    '"Leopold Aschenbrenner" fund',
]
_GNEWS_BASE = "https://news.google.com/rss/search?hl=en-US&gl=US&ceid=US:en&q="

# Primary sources — Leo's own publications, no auth required
_PRIMARY_FEEDS = [
    "https://situational-awareness.ai/feed/",
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


def from_nitter_x(cfg: Config) -> list[DiscoveryItem]:
    """@leopoldasch timeline via Nitter RSS.

    Tries twiiit.com first (auto-selects a live instance), falls back to
    nitter.net directly if twiiit.com redirects to a broken instance.
    ScrapeCreators runs in parallel as an independent backup.
    """
    client = HttpClient(cfg.sec_user_agent, cfg.sec_request_delay)
    for url in _NITTER_URLS:
        try:
            text = client.get_text(url, timeout=20)
            if not text or "<rss" not in text.lower():
                log.debug("Nitter: no RSS from %s, trying next.", url)
                continue
            items = _parse_rss(text, "x")
            log.info("Nitter (%s): %d tweet(s) from @%s.", url, len(items), _X_HANDLE)
            return items
        except Exception as exc:  # noqa: BLE001
            log.debug("Nitter: %s failed: %s", url, exc)
    log.warning("Nitter: all URLs failed for @%s.", _X_HANDLE)
    return []


_SCRAPE_CREATORS_URL = "https://api.scrapecreators.com/v1/twitter/user-tweets"


def from_scrape_creators_x(cfg: Config) -> list[DiscoveryItem]:
    """@leopoldasch tweets via ScrapeCreators API (free tier).

    Runs in parallel with from_nitter_x — deduplication happens downstream via
    content_hash(). If SCRAPE_CREATORS_API_KEY is not set, this source is
    silently skipped so the pipeline stays key-free by default.
    """
    api_key = os.environ.get("SCRAPE_CREATORS_API_KEY", "")
    if not api_key:
        log.debug("ScrapeCreators: SCRAPE_CREATORS_API_KEY not set, skipping.")
        return []

    client = HttpClient(cfg.sec_user_agent, cfg.sec_request_delay)
    try:
        # Inject the API key header for this single request, then remove it.
        resp = client.session.get(
            f"{_SCRAPE_CREATORS_URL}?handle={_X_HANDLE}",
            headers={"x-api-key": api_key},
            timeout=20,
        )
        resp.raise_for_status()
        data = json.loads(resp.text)
    except Exception as exc:  # noqa: BLE001
        log.warning("ScrapeCreators X: request failed: %s", exc)
        return []

    # Response shape: {"tweets": [...]} where each tweet has id, text, created_at, url
    raw_tweets = data.get("tweets") if "tweets" in data else data.get("data")
    if raw_tweets is None:
        # Key missing entirely — API changed shape or returned an error payload.
        log.warning(
            "ScrapeCreators X: unexpected response shape (keys: %s). "
            "API may have changed or account may be rate-limited.",
            list(data.keys()),
        )
        return []
    if not isinstance(raw_tweets, list):
        log.warning("ScrapeCreators X: 'tweets' field is not a list (%s).", type(raw_tweets))
        return []
    tweets = raw_tweets
    if not tweets:
        log.info("ScrapeCreators X: 0 tweet(s) from @%s — account appears inactive.", _X_HANDLE)
        return []

    out: list[DiscoveryItem] = []
    for t in tweets:
        tweet_id = t.get("id") or t.get("rest_id") or ""
        text_body = t.get("text") or t.get("full_text") or ""
        created_at = t.get("created_at") or t.get("date") or ""
        url = t.get("url") or (
            f"https://x.com/{_X_HANDLE}/status/{tweet_id}" if tweet_id else ""
        )
        if not url or not text_body:
            continue
        out.append(DiscoveryItem(
            url=url,
            title=f"@{_X_HANDLE}: {text_body[:120]}",
            excerpt=text_body[:500],
            source_kind="x",
            published=created_at,
        ))

    log.info("ScrapeCreators X: %d tweet(s) from @%s.", len(out), _X_HANDLE)
    return out


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


def from_primary_sources(cfg: Config) -> list[DiscoveryItem]:
    """Leo's own publications (situational-awareness.ai) — highest confidence."""
    client = HttpClient(cfg.sec_user_agent, cfg.sec_request_delay)
    out: list[DiscoveryItem] = []
    for url in _PRIMARY_FEEDS:
        items = _fetch_feed(client, url, "blog")
        out.extend(items)
        log.debug("Primary source %s: %d items", url, len(items))
    if out:
        log.info("Primary sources: %d item(s).", len(out))
    return out


def from_all_curated(cfg: Config) -> list[DiscoveryItem]:
    """Fetch all curated free sources. Called from step_discover in pipeline."""
    items: list[DiscoveryItem] = []
    items.extend(from_edgar_rss(cfg))
    items.extend(from_edgar_form_d(cfg))
    items.extend(from_primary_sources(cfg))
    items.extend(from_nitter_x(cfg))
    items.extend(from_scrape_creators_x(cfg))  # parallel X source — dedup downstream
    items.extend(from_google_news(cfg))
    items.extend(from_reddit(cfg))
    log.info("Curated news sources total: %d items.", len(items))
    return items
