[SETUP.md](https://github.com/user-attachments/files/28212749/SETUP.md)
# Setup & operation guide

This is the permanent setup reference. The repository's `README.md` is a
**generated dashboard** and is overwritten on each run, so all durable
documentation lives here.

---

## 1. What this does

Archives and monitors the public activity of Leopold Aschenbrenner /
Situational Awareness LP:

- pulls SEC EDGAR filings (13F-HR, 13D/G, Form D) for the watched CIK(s),
- parses each 13F into an **instrument-separated** position model (common
  stock longs are never mixed with option notional),
- discovers primary-source / media statements and records them as `open`
  events that flip to `verified` once a filing confirms the ticker,
- regenerates `README.md` and, optionally, emails an Apple-style HTML digest.

It is an **archive, not a trading signal.** Not investment advice.

---

## 2. Local quick start

```bash
git clone <your-repo-url>
cd situational-awareness-tracker
python -m venv .venv && source .venv/bin/activate     # optional
pip install -r requirements.txt

# 1) set a real SEC contact (required by EDGAR) — see §3
# 2) run the pipeline
python -m src.pipeline fetch      # download + parse new filings
python -m src.pipeline analyze    # rebuild model + regenerate README.md
python -m src.pipeline digest     # render email -> examples/last_email.html
# or everything at once:
python -m src.pipeline run
```

Run the tests with `python tests/test_positions.py` (no network needed).

---

## 3. Configuration (`config.yaml`)

| Key | What to do |
| --- | --- |
| `sec.user_agent` | **Required.** Put a real contact, e.g. `sa-tracker (you@example.com)`. EDGAR rejects requests without a descriptive UA. |
| `entity.primary_cik` | `0002045724` (Situational Awareness LP). Verified. |
| `entity.extra_ciks` | Add more CIKs **only after** confirming them on EDGAR. |
| `analysis.quarters` | Trailing quarters in the table (default 3). |
| `analysis.hold_band_pct` / `trim_threshold_pct` | Traffic-light thresholds. |
| `prices.enabled` | `true` uses yfinance; set `false` to skip price columns. |
| `email.enabled` | `false` by default. Set `true` once SMTP secrets are configured (§5). |

Secrets are **never** stored in `config.yaml` — only in environment variables.

---

## 4. CUSIP → ticker mapping (important)

13F reports **CUSIPs, not tickers**, so prices/tickers need a mapping layer:
`data/reference/cusip_ticker_overrides.csv`.

After your first `fetch`, open the parsed file
`data/parsed/13f/<cik>_<date>.json`, copy each `cusip` + `name_of_issuer`, and
add a row:

```csv
cusip,issuer,ticker,yfinance_symbol,sector,source,confidence
093712107,Bloom Energy Corp,BE,BE,Power,manual,high
```

Unmapped CUSIPs render as `?` and get no price — they are never guessed.

---

## 5. Environment variables (secrets)

Set these locally (`export VAR=...`) or as GitHub **repository secrets** (§7).

| Variable | Purpose | Required |
| --- | --- | --- |
| `SMTP_HOST`, `SMTP_PORT` | Mail server (e.g. `smtp.gmail.com`, `587`) | for email |
| `SMTP_USER`, `SMTP_PASSWORD` | Login (use an app password, not your main one) | for email |
| `SMTP_TO` | Recipient(s), comma-separated | for email |
| `GOOGLE_ALERT_FEEDS` | Comma-separated Google Alerts RSS URLs | optional |
| `BLOG_FEEDS` | Comma-separated primary-source RSS URLs | optional |
| `X_BEARER_TOKEN` | X/Twitter API token (free tier can't read timelines) | optional |

The digest always writes a preview to `examples/last_email.html`, and the
alert writes to `examples/last_alert.html`, so you can verify both designs
before enabling real sending.

---

## 6. Email digest

Every email **always** contains the full 13F overview, in this order:
Latest 13F summary → Common stock longs (3 quarters + post-quarter price move)
→ New buys → Exits → Options (notional only, "direction unknown") → Methodology
+ disclaimer. A new `open` signal, if any, appears as a card at the top.

Design is deliberately Apple-style: San Francisco system font stack, light
background, rounded cards, status pills — built with inline styles + table
layout so it survives Gmail/Outlook/Apple Mail. Preview: open
`examples/sample_digest_email.html` in a browser.

---

## 7. Alert emails

Enable the alert system to receive an **immediate email** the moment a new
signal is detected, instead of waiting for the next scheduled digest.

**What triggers an alert:**
- A new SEC filing (13F, 13D/G) arrives — confidence 1.0, always triggered.
- A news / blog mention matches an investment, sell, announce, or highlight
  keyword — only if the item clears `alert.min_confidence` (default 0.5).

**Signal categories detected:**
| Category | Example matched phrases |
| --- | --- |
| `invest` | "invested in", "backed", "series A", "building a position" |
| `sell` | "sold stake", "exited", "trimmed position", "divested" |
| `announce` | "new fund", "raising capital", "first close", "press release" |
| `highlight` | "bullish on", "high conviction", "key player", "compelling opportunity" |

**To activate:**
1. In `config.yaml`, set `alert.enabled: true`.
2. Ensure the SMTP secrets are configured (same as the digest — §5).
3. Optionally tune `alert.min_confidence` (lower = more alerts, higher = fewer).

**Free curated news sources** (no API keys required, active by default):
- **Google News RSS** — four targeted search queries for "Leopold Aschenbrenner"
- **HackerNews RSS** via hnrss.org
- **Reddit RSS** — r/MachineLearning, r/investing, r/agi

These are supplemented by any optional `GOOGLE_ALERT_FEEDS` / `BLOG_FEEDS`
you set in §5.

Preview the alert design (without sending) by running:
```bash
python -m src.pipeline discover
python -m src.pipeline alert    # writes examples/last_alert.html
```

---

## 8. Scheduling on GitHub Actions

Four workflows are included (`.github/workflows/`):

| Workflow | Cadence | Command |
| --- | --- | --- |
| `alert` | **every hour** at :12 | discover → alert |
| `news-and-digest` | ~09:17 & 17:17 Europe/Berlin | discover → analyze → digest |
| `filings` | once daily | fetch → analyze |
| `entity-and-adv` | weekly (Mon) | resolve-entities |

Notes:
- The `alert` workflow runs every hour and sends an email only when new
  signals are found. State is committed back (`data/state/alert_state.json`)
  to prevent duplicate sends across runs.
- GitHub cron is **UTC** and not minute-exact. The news workflow fires at four
  UTC times and a **Berlin-local guard step** lets it proceed only at 09:00 or
  17:00 local, so DST (CET↔CEST) never double-runs it.
- Scheduled workflows are **auto-disabled after 60 days** of no repo activity.
  The auto-commit steps keep the repo active; if you pause, re-enable them in
  the **Actions** tab.
- Set secrets under **Settings → Secrets and variables → Actions**.
- Workflows commit updated data back to the repo (`contents: write`).

---

## 9. How to upload this folder to GitHub via the browser

You don't need git installed.

1. Go to <https://github.com/new>, create a repository (e.g.
   `situational-awareness-tracker`), **Private** is fine. Don't add a README.
2. On the empty repo page, click **uploading an existing file**
   (or **Add file → Upload files**).
3. **Unzip** the downloaded folder first, then drag the *contents* (not the
   outer folder) into the browser — including the hidden `.github/` folder.
   - On macOS Finder hides dotfiles: press **⌘⇧.** to show them, or drag the
     `.github` folder in as a separate step.
4. Scroll down, write a commit message, click **Commit changes**.
5. Open **Settings → Secrets and variables → Actions** and add the secrets
   from §5 you want to use.
6. Edit `config.yaml`: set a real `sec.user_agent` contact; flip
   `email.enabled: true` only after secrets are in place.
7. Open the **Actions** tab and enable workflows (GitHub asks once for
   confirmation on a new repo). Click **Run workflow** on `filings` to do the
   first fetch, then add CUSIP overrides (§4) and re-run.

That's it — from then on it runs on schedule and commits updates itself.
