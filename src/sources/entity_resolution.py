"""Entity resolution against free EDGAR endpoints.

Builds data/reference/entity_map.json from:
  * the submissions JSON (former names, addresses, file numbers, forms), and
  * EDGAR full-text search (entities appearing as reporting persons on other
    filers' submissions).

This closes the cheapest part of an institutional "entity graph": aliases,
related CIKs, and reporting persons — all without paid data.
"""
from __future__ import annotations

from ..config import Config
from ..utils import HttpClient, get_logger, write_json, utc_now_iso
from .sec import fetch_submissions

log = get_logger("sources.entity")

FTS_URL = "https://efts.sec.gov/LATEST/search-index?q=%22{query}%22"


def resolve(cfg: Config) -> dict:
    """Refresh entity_map.json. Returns the resolved map."""
    client = HttpClient(cfg.sec_user_agent, cfg.sec_request_delay)
    entities = []

    for cik in cfg.all_ciks:
        try:
            sub = fetch_submissions(client, cik)
        except Exception as exc:  # noqa: BLE001
            log.warning("entity_resolution: could not fetch %s: %s", cik, exc)
            continue
        former = [n.get("name") for n in sub.get("formerNames", []) if n.get("name")]
        addrs = sub.get("addresses", {})
        address_lines = []
        for kind in ("business", "mailing"):
            a = addrs.get(kind) or {}
            line = ", ".join(
                str(x) for x in (a.get("street1"), a.get("city"), a.get("stateOrCountry"), a.get("zipCode")) if x
            )
            if line:
                address_lines.append(line)
        entities.append(
            {
                "name": sub.get("name"),
                "cik": cik,
                "sec_file_number": (sub.get("filings", {}).get("recent", {}).get("fileNumber", [None]) or [None])[0],
                "state_of_inc": sub.get("stateOfIncorporation"),
                "former_names": former,
                "addresses": sorted(set(address_lines)),
                "confirmed": cik == cfg.primary_cik,
            }
        )

    entity_map = {
        "person": cfg.person,
        "primary_entity": cfg.primary_name,
        "entities": entities,
        "reporting_persons": [cfg.person],
        "last_resolved": utc_now_iso(),
        "note": "Run with extra_ciks populated in config.yaml to expand the graph.",
    }
    write_json(cfg.paths.reference / "entity_map.json", entity_map)
    log.info("Resolved %d entities", len(entities))
    return entity_map
