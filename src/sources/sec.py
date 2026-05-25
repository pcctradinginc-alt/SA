"""SEC EDGAR ingestion.

Uses only free, public EDGAR endpoints:

* ``https://data.sec.gov/submissions/CIK##########.json`` — filing history
* ``https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/index.json`` — file list

Responsibilities (only):
  1. discover new filings for the watched CIKs (idempotent via state file),
  2. download their documents into ``data/raw/sec/{cik}/{accession}/``.

Interpretation (13F information tables etc.) is the parsers' job.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path

from ..config import Config
from ..utils import HttpClient, get_logger, read_json, write_json, utc_now_iso

log = get_logger("sources.sec")

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE_INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/index.json"
ARCHIVE_FILE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{name}"

SEEN_FILE = "seen_accessions.json"

# EDGAR occasionally returns full form names (e.g. "SCHEDULE 13D") in older
# filings or certain search endpoints; normalize them to the canonical short
# codes that config.yaml and pipeline.py use.
_FORM_ALIASES: dict[str, str] = {
    "SCHEDULE 13D": "SC 13D",
    "SCHEDULE 13D/A": "SC 13D/A",
    "SCHEDULE 13G": "SC 13G",
    "SCHEDULE 13G/A": "SC 13G/A",
}


@dataclass
class Filing:
    """One discovered filing (metadata only)."""

    cik: str
    accession: str
    form: str
    filing_date: str
    report_date: str
    primary_document: str

    @property
    def acc_nodash(self) -> str:
        return self.accession.replace("-", "")


def _seen_accessions(cfg: Config) -> set[str]:
    data = read_json(cfg.paths.state / SEEN_FILE, default={"accessions": []})
    return set(data.get("accessions", []))


def _save_seen(cfg: Config, seen: set[str]) -> None:
    write_json(cfg.paths.state / SEEN_FILE, {"accessions": sorted(seen), "updated": utc_now_iso()})


def fetch_submissions(client: HttpClient, cik: str) -> dict:
    """Return the EDGAR submissions JSON for a zero-padded 10-digit CIK."""
    return client.get_json(SUBMISSIONS_URL.format(cik=cik))


def iter_filings(submissions: dict, cik: str, allowed_forms: list[str]) -> list[Filing]:
    """Flatten the ``filings.recent`` columnar arrays into Filing objects."""
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    allowed = set(allowed_forms)
    out: list[Filing] = []
    for i, raw_form in enumerate(forms):
        form = _FORM_ALIASES.get((raw_form or "").strip().upper(), (raw_form or "").strip())
        if form not in allowed:
            continue
        out.append(
            Filing(
                cik=cik,
                accession=recent["accessionNumber"][i],
                form=form,
                filing_date=recent["filingDate"][i],
                report_date=recent.get("reportDate", [""] * len(forms))[i],
                primary_document=recent.get("primaryDocument", [""] * len(forms))[i],
            )
        )
    return out


def download_filing(client: HttpClient, cfg: Config, filing: Filing) -> Path:
    """Download all documents of a filing into data/raw/sec/{cik}/{accession}/.

    Returns the local directory. Skips files that already exist on disk.
    """
    cik_int = str(int(filing.cik))
    dest = cfg.paths.raw / "sec" / filing.cik / filing.accession
    dest.mkdir(parents=True, exist_ok=True)

    index_url = ARCHIVE_INDEX_URL.format(cik_int=cik_int, acc_nodash=filing.acc_nodash)
    index = client.get_json(index_url)
    write_json(dest / "_index.json", index)

    for item in index.get("directory", {}).get("item", []):
        name = item.get("name", "")
        if not name or name.endswith("/"):
            continue
        target = dest / name
        if target.exists():
            continue
        url = ARCHIVE_FILE_URL.format(cik_int=cik_int, acc_nodash=filing.acc_nodash, name=name)
        try:
            text = client.get_text(url)
            target.write_text(text, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 — keep ingest resilient
            log.warning("Could not download %s: %s", url, exc)
    return dest


def mark_seen(cfg: Config, accessions: list[str]) -> None:
    """Persist a list of accessions to the seen set.

    Call after successful processing so a crashed/partial run can retry on the
    next invocation rather than silently losing the filing.
    """
    seen = _seen_accessions(cfg)
    seen.update(accessions)
    _save_seen(cfg, seen)


def collect_new_filings(cfg: Config) -> list[Filing]:
    """Discover and download all not-yet-seen filings for every watched CIK.

    Downloads are idempotent (existing files are skipped). Accessions are NOT
    marked as seen here — the caller must call mark_seen() after successfully
    processing each filing so that a parse error does not permanently lose it.
    """
    client = HttpClient(cfg.sec_user_agent, cfg.sec_request_delay)
    seen = _seen_accessions(cfg)
    discovered: list[Filing] = []

    for cik in cfg.all_ciks:
        log.info("Checking EDGAR submissions for CIK %s", cik)
        try:
            submissions = fetch_submissions(client, cik)
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to fetch submissions for %s: %s", cik, exc)
            continue

        for filing in iter_filings(submissions, cik, cfg.filing_types):
            if filing.accession in seen:
                continue
            log.info("New filing %s %s (%s)", filing.form, filing.accession, filing.report_date)
            try:
                download_filing(client, cfg, filing)
            except Exception as exc:  # noqa: BLE001
                log.error("Download failed for %s; will retry next run: %s", filing.accession, exc)
                continue
            discovered.append(filing)
            seen.add(filing.accession)  # in-memory only — prevents re-download in same run

    _write_index(cfg, discovered)
    return discovered


def _write_index(cfg: Config, filings: list[Filing]) -> None:
    """Append newly discovered filings to the parsed filings index."""
    index_path = cfg.paths.parsed / "filings_index.json"
    existing = read_json(index_path, default={"filings": []})
    existing["filings"].extend(asdict(f) for f in filings)
    existing["updated"] = utc_now_iso()
    write_json(index_path, existing)
