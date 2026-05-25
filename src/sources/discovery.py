"""Discovery-layer sources (news / primary text).

These are intentionally thin and resilient: the discovery layer only needs to
surface candidate URLs and short excerpts. Full text of third-party media is
NOT stored — we keep url + hash + short excerpt only (copyright + repo size).

Google Alerts and a generic RSS reader are implemented; X is an optional stub
because the free X API tier cannot reliably read a user timeline.
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from ..config import Config
from ..utils import HttpClient, get_logger, sha256_text

log = get_logger("sources.discovery")

EXCERPT_CHARS = 280  # keep excerpts short — never store full third-party text


@dataclass
class DiscoveryItem:
    url: str
    title: str
    excerpt: str
    source_kind: str  # "google_alerts" | "rss" | "blog" | "x"
    published: str | None = None

    def content_hash(self) -> str:
        return sha256_text(self.url + self.title)


def _parse_rss(xml_text: str, source_kind: str) -> list[DiscoveryItem]:
    items: list[DiscoveryItem] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.warning("RSS parse error (%s): %s", source_kind, exc)
        return items

    # Handle both RSS <item> and Atom <entry>.
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    nodes = root.iter("item")
    found = list(nodes)
    if not found:
        found = list(root.iter("{http://www.w3.org/2005/Atom}entry"))

    for node in found:
        title = (node.findtext("title") or node.findtext("atom:title", default="", namespaces=ns) or "").strip()
        link = node.findtext("link") or ""
        if not link:  # Atom puts the URL in an attribute
            link_el = node.find("atom:link", ns)
            link = link_el.get("href", "") if link_el is not None else ""
        desc = (node.findtext("description") or node.findtext("atom:summary", default="", namespaces=ns) or "").strip()
        published = node.findtext("pubDate") or node.findtext("atom:updated", default=None, namespaces=ns)
        items.append(
            DiscoveryItem(
                url=link.strip(),
                title=title,
                excerpt=desc[:EXCERPT_CHARS],
                source_kind=source_kind,
                published=published,
            )
        )
    return items


def from_google_alerts(cfg: Config) -> list[DiscoveryItem]:
    """Read Google Alerts RSS feeds listed in the GOOGLE_ALERT_FEEDS env var
    (comma-separated). Coverage is partial and feeds are fragile — treat as a
    low-confidence discovery layer.
    """
    feeds = [u.strip() for u in os.environ.get("GOOGLE_ALERT_FEEDS", "").split(",") if u.strip()]
    if not feeds:
        log.info("No GOOGLE_ALERT_FEEDS configured; skipping Google Alerts.")
        return []
    client = HttpClient(cfg.sec_user_agent, cfg.sec_request_delay)
    out: list[DiscoveryItem] = []
    for feed in feeds:
        try:
            out.extend(_parse_rss(client.get_text(feed), "google_alerts"))
        except Exception as exc:  # noqa: BLE001
            log.warning("Google Alerts feed failed (%s): %s", feed, exc)
    return out


def from_blog(cfg: Config) -> list[DiscoveryItem]:
    """Read primary-source blog/website RSS feeds (BLOG_FEEDS env var)."""
    feeds = [u.strip() for u in os.environ.get("BLOG_FEEDS", "").split(",") if u.strip()]
    if not feeds:
        return []
    client = HttpClient(cfg.sec_user_agent, cfg.sec_request_delay)
    out: list[DiscoveryItem] = []
    for feed in feeds:
        try:
            out.extend(_parse_rss(client.get_text(feed), "blog"))
        except Exception as exc:  # noqa: BLE001
            log.warning("Blog feed failed (%s): %s", feed, exc)
    return out


def from_x(cfg: Config) -> list[DiscoveryItem]:
    """Optional X (Twitter) source.

    Deliberately a stub: the free X API tier cannot reliably read a user
    timeline, so this returns nothing unless X_BEARER_TOKEN is set AND the
    account has a paid tier. Implement against the official API only.
    """
    if not os.environ.get("X_BEARER_TOKEN"):
        log.info("X disabled (no X_BEARER_TOKEN). This is expected on the free tier.")
        return []
    log.info("X token present but reader not implemented — wire the official API here.")
    return []
