# Situational Awareness Tracker

An automated, **EDGAR-first archive and monitor** for the public activity of
**Leopold Aschenbrenner** / **Situational Awareness LP** — SEC filings and
public statements, linked together, with an instrument-separated 13F position
analysis. It runs on a schedule, keeps everything in Git, and can email an
Apple-style digest.

> **This is an archive, not a trading signal. Not investment advice.**
> 13F data is delayed up to 45 days; short, private and intra-quarter positions
> never appear. Common stock and options are always shown separately.

---

## What it does

- **Verified layer** — pulls SEC EDGAR 13F-HR, 13D/G and Form D for the watched
  CIK (`0002045724`) and parses each 13F into a structured, instrument-separated
  model (common-stock longs are never mixed with option notional).
- **Discovery layer** — optional RSS/primary-source feeds surface candidate
  statements, stored as `open` events with a deterministic confidence.
- **Linking** — an `open` statement flips to `verified` once a filing confirms
  its ticker. Statements that a 13F can never confirm (shorts, private deals)
  are marked `not_verifiable_via_13f`.
- **Output** — regenerates a README dashboard and, optionally, emails a clean
  digest that always carries the full 13F overview.

## Quick start

```bash
pip install -r requirements.txt
# set a real contact in config.yaml -> sec.user_agent first!
python -m src.pipeline run
```

See **[`docs/SETUP.md`](docs/SETUP.md)** for full configuration, the
CUSIP→ticker mapping step, email/SMTP setup, GitHub Actions scheduling, and a
step-by-step **browser upload guide**.

## Preview

- `examples/sample_digest_email.html` — the Apple-style email (open in a browser).
- `examples/sample_README.md` — what the generated dashboard looks like.

> The example data uses real, public Q1-2026 holding *names* with **illustrative**
> share counts and prices, and a clearly **fictional** placeholder statement.

## Project layout

```
config.yaml              # all non-secret settings
src/
  config.py utils.py     # config loader + plumbing (logging, IO, HTTP)
  sources/               # sec.py, entity_resolution.py, discovery.py
  parsers/               # parse_13f.py (instrument split), statements, 13d/g
  analysis/              # cusip_map.py, prices.py, positions.py (the model)
  events.py              # unified event log + confidence/verification
  render/                # README + Apple-style email renderers
  email_templates/       # digest.html.j2, readme.md.j2
  notify.py              # SMTP delivery
  pipeline.py            # orchestrator + CLI
data/                    # reference / raw / parsed / derived / state
.github/workflows/       # filings (daily), news+digest (2×/day), entity (weekly)
tests/                   # offline unit tests
```

## License

MIT. SEC filings are public domain; third-party media is referenced by
URL + hash + short excerpt only, never reproduced in full.
