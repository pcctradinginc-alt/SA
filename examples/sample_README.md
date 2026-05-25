# Situational Awareness Tracker

Automated, EDGAR-first archive and monitor for the public activity of
**Leopold Aschenbrenner** / **Situational Awareness LP**. Not a trading signal — a
reproducible archive with instrument-separated 13F analysis.

_Last updated: 2026-05-25 · prices via yfinance · Not investment advice._

## Latest 13F summary

| Metric | Value |
| --- | --- |
| Latest quarter | Q1 2026 (period ended 2026-03-31) |
| Reported holdings | 5 |
| Reported 13F value | $2.9B |
| **Common stock long exposure** | **$1.3B** |
| **Options notional exposure** | **$1.6B** _(direction unknown)_ |
| New / Increased / Reduced / Exited (common) | 1 / 1 / 0 / 1 |

> Common stock long exposure and options notional are shown separately on purpose — combining them would let the options book dominate the picture.

## Common stock longs — last 3 quarters

| Ticker | Issuer | Q3 2025 | Q4 2025 | Q1 2026 | QoQ | 3Q | Wt | Px since Q-end | Est. value | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BE | Bloom Energy Corp | 5.0M | 6.0M | 6.5M | +8.3% | ↑↑↑ | 69.2% | — | — | 🟢 Strong Add |
| CRWV | CoreWeave Inc | 1.0M | 1.4M | 2.1M | +50.0% | ↑↑↑ | 23.2% | — | — | 🟢 Strong Add |
| SNDK | Sandisk Corp | 0 | 0 | 1.2M | New | New | 7.6% | — | — | 🟡 New Buy |

## New common stock positions in latest 13F

| Ticker | Issuer | Sector | Q1 2026 shares | Value | Wt | Px since Q-end |
| --- | --- | --- | --- | --- | --- | --- |
| SNDK | Sandisk Corp | Memory | 1.2M | $96.0M | 7.6% | — |

## Exited common stock positions

| Ticker | Issuer | Q3 2025 | Q4 2025 | Q1 2026 | Last seen value |
| --- | --- | --- | --- | --- | --- |
| IREN | IREN Ltd | 8.0M | 6.0M | 0 | $0 |

## Options / puts / calls — notional only

| Underlying | Type | Q3 2025 | Q4 2025 | Q1 2026 | QoQ | Underlying px move | Interpretation risk |
| --- | --- | --- | --- | --- | --- | --- | --- |
| NVDA | PUT | $1.2B | $1.6B | $1.6B | +0.0% | — | Notional, not premium; long/short direction unknown |

> Options are shown as reported notional exposure. Direction, premium, strike, expiry and true economic exposure are unknown.

## Data quality notes

- 13F reports CUSIPs, not tickers; tickers come from `data/reference/cusip_ticker_overrides.csv`. Unmapped lines show `?`.
- Prices are best-effort via yfinance; missing prices render as `—`.
- Estimated value is a mark-to-current of the last reported share count, not realised P&L.

## Methodology

This repository ingests SEC EDGAR (13F, 13D/G, Form D) as the verified layer,
plus primary-source and media discovery feeds. Each signal is recorded in
`data/parsed/events.jsonl` with a `source_class`, a `verification_status`
(`verified` / `open` / `not_verifiable_via_13f`) and a deterministic
`confidence`. See README §How it works.