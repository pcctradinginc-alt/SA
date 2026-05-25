"""Shared helpers: logging, hashing, JSON/JSONL IO, and a rate-limited HTTP client.

Keeping these in one place keeps the source modules small and consistent. None
of the helpers here know anything about the domain (SEC, 13F, ...); they are
pure plumbing.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

try:  # requests is required at runtime but optional for offline unit tests
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore


# ── logging ───────────────────────────────────────────────────────────────────
def get_logger(name: str) -> logging.Logger:
    """Return a configured module logger (idempotent)."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s  %(levelname)-7s %(name)s  %(message)s")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


# ── time ────────────────────────────────────────────────────────────────────--
def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string with a trailing Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── hashing ─────────────────────────────────────────────────────────────────--
def sha256_text(text: str) -> str:
    """Stable content hash, used to dedupe and to fingerprint sources."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── JSON / JSONL IO ────────────────────────────────────────────────────────────
def read_json(path: str | Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    with p.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: str | Path, data: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False, default=str)
        fh.write("\n")


def read_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def append_jsonl(path: str | Path, record: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def rewrite_jsonl(path: str | Path, records: Iterable[dict[str, Any]]) -> None:
    """Overwrite a JSONL file atomically-ish (write temp, then replace)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    tmp.replace(p)


# ── HTTP ────────────────────────────────────────────────────────────────────--
class HttpClient:
    """Thin, polite HTTP wrapper.

    Adds a mandatory User-Agent (SEC requires one) and a fixed inter-request
    delay so we stay well under EDGAR's ~10 req/s ceiling.
    """

    def __init__(self, user_agent: str, request_delay: float = 0.2) -> None:
        if requests is None:  # pragma: no cover
            raise RuntimeError("The 'requests' package is required for network access.")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self.request_delay = request_delay
        self._log = get_logger("http")

    def get(self, url: str, *, accept: str | None = None, timeout: int = 30):
        headers = {"Accept": accept} if accept else {}
        self._log.debug("GET %s", url)
        resp = self.session.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        time.sleep(self.request_delay)
        return resp

    def get_json(self, url: str, timeout: int = 30) -> Any:
        return self.get(url, accept="application/json", timeout=timeout).json()

    def get_text(self, url: str, timeout: int = 30) -> str:
        return self.get(url, timeout=timeout).text
