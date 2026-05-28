"""Generate and cache a Claude Haiku narrative analysis of the latest 13F.

The analysis is produced once per quarter and written to
data/derived/13f_analysis.json.  Every subsequent email reuses the cached
text so we never re-call the API for the same filing.

Falls back silently (returns empty string) if ANTHROPIC_API_KEY is not set
or the API call fails.
"""
from __future__ import annotations

import os

from ..config import Config
from ..utils import get_logger, read_json, today_iso, write_json

log = get_logger("analysis.llm_13f")

_CACHE_FILE = "13f_analysis.json"

_SYSTEM_PROMPT = """\
Du bist ein präziser Finanz-Analyst. Du analysierst die 13F-Quartalsmeldung des \
Hedgefonds "Situational Awareness LP" (Manager: Leopold Aschenbrenner, ex-OpenAI).

Du bekommst eine strukturierte Zusammenfassung der aktuellen Positionen und der \
Veränderungen zu den letzten zwei Quartalen.

Schreibe eine kompakte Analyse auf Deutsch (4–6 Sätze) die folgendes beantwortet:
1. Was ist die erkennbare Kernthese des Quartals?
2. Was ist die überraschendste oder bedeutsamste Einzelbewegung?
3. Welche Sektoren werden auf- bzw. abgebaut?

Stil: direkt, klar, keine Füllwörter. Keine Aufzählungspunkte — Fließtext.

STRIKTE REGELN — diese haben Vorrang vor allem anderen:

1. Schreibe ausschließlich was direkt aus den Filing-Daten ableitbar ist.
   Keine Absichten, keine Motive, keine Strategieinferenz.

2. Optionspositionen (PUT/CALL): Nenne nur Nominalwert und Anzahl Kontrakte.
   Verbotene Formulierungen solange Richtung, Strike und Expiry unbekannt sind:
   "hedged", "short", "long via Optionen", "Volatilitätsspread", "gesichert",
   "taktisch", "defensiv", "spekulativ", "Absicherung", "Wette auf".
   Erlaubt: "PUT-Exposition von $X auf [Underlying] — Richtung unbekannt."

3. Formulierungen wie "komplett hedged", "netto short", "neutral positioniert"
   sind nur zulässig wenn sie wörtlich aus einem SC 13D/G oder einer
   verifizierten öffentlichen Aussage des Managers stammen. Andernfalls: weglassen.

4. Wenn Daten fehlen oder mehrdeutig sind: schreibe "nicht ableitbar" statt
   eine plausible Erklärung zu erfinden.\
"""


def _build_prompt(model: dict) -> str:
    summary = model.get("summary", {})
    q = summary.get("latest_quarter", "?")
    total = summary.get("common_stock_long_exposure_usd", 0)
    labels = model.get("quarter_labels", [])

    lines = [f"Quartal: {q}  |  Portfolio-Wert (Long Common): ${total/1e9:.2f}B"]
    lines.append(f"Vergleichsquartale: {', '.join(labels)}\n")

    # New buys
    new_buys = [r for r in model.get("common_stock", []) if r.get("status") == "new_buy"]
    if new_buys:
        lines.append("NEUE POSITIONEN:")
        for r in sorted(new_buys, key=lambda r: r.get("value_latest_usd", 0), reverse=True):
            lines.append(f"  + {r['issuer']}  ${r.get('value_latest_usd',0)/1e6:.0f}M")

    # Strong adds / new_add (>+20% or new continuation)
    adds = [r for r in model.get("common_stock", [])
            if r.get("status") in ("strong_add", "new_add")
            and isinstance(r.get("qoq_share_change_pct"), float)]
    if adds:
        lines.append("\nSIGNIFIKANT AUFGESTOCKT:")
        for r in sorted(adds, key=lambda r: r.get("qoq_share_change_pct", 0), reverse=True)[:8]:
            pct = r["qoq_share_change_pct"] * 100
            lines.append(f"  ↑ {r['issuer']}  {pct:+.0f}%  ${r.get('value_latest_usd',0)/1e6:.0f}M")

    # Trims
    trims = [r for r in model.get("common_stock", []) if r.get("status") == "trim"]
    if trims:
        lines.append("\nREDUZIERT:")
        for r in sorted(trims, key=lambda r: r.get("qoq_share_change_pct", 0)):
            pct = r["qoq_share_change_pct"] * 100
            lines.append(f"  ↓ {r['issuer']}  {pct:+.0f}%  ${r.get('value_latest_usd',0)/1e6:.0f}M")

    # Exits
    exits = model.get("exits", [])
    if exits:
        lines.append("\nVERKAUFT (vollständig):")
        for r in sorted(exits, key=lambda r: max(r.get("shares_by_quarter") or [0]), reverse=True)[:6]:
            lines.append(f"  ✗ {r['issuer']}")

    # Options book — direction (long/short), strike and expiry are unknown
    puts = [r for r in model.get("options", []) if r.get("instrument") == "PUT"]
    calls = [r for r in model.get("options", []) if r.get("instrument") == "CALL"]
    if puts:
        lines.append("\nPUT-OPTIONEN (Richtung/Strike/Expiry unbekannt, nur Nominalwert):")
        for r in sorted(puts, key=lambda r: r.get("notional_latest_usd", 0), reverse=True)[:8]:
            lines.append(f"  PUT {r['underlying']}  Notional ${r.get('notional_latest_usd',0)/1e9:.2f}B  —  Richtung unbekannt")
    if calls:
        lines.append("\nCALL-OPTIONEN (Richtung/Strike/Expiry unbekannt, nur Nominalwert):")
        for r in sorted(calls, key=lambda r: r.get("notional_latest_usd", 0), reverse=True)[:8]:
            lines.append(f"  CALL {r['underlying']}  Notional ${r.get('notional_latest_usd',0)/1e9:.2f}B  —  Richtung unbekannt")

    return "\n".join(lines)


def generate_analysis(cfg: Config, model: dict) -> str:
    """Return a cached (or freshly generated) Haiku analysis of the 13F."""
    if not model.get("available"):
        return ""

    quarter = model.get("summary", {}).get("latest_quarter", "")
    cache_path = cfg.paths.derived / _CACHE_FILE
    cached = read_json(cache_path) or {}

    if cached.get("quarter") == quarter and cached.get("analysis"):
        log.info("13F analysis: reusing cached analysis for %s.", quarter)
        return cached["analysis"]

    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.info("13F analysis: no ANTHROPIC_API_KEY, skipping.")
        return ""

    prompt = _build_prompt(model)
    log.info("13F analysis: generating for %s via Claude Haiku.", quarter)

    try:
        import anthropic
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=cfg.llm_model,
            max_tokens=512,
            system=[{"type": "text", "text": _SYSTEM_PROMPT,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        analysis = response.content[0].text.strip()
    except Exception as exc:
        log.warning("13F analysis: API call failed: %s", exc)
        return ""

    write_json(cache_path, {
        "quarter": quarter,
        "analysis": analysis,
        "generated": today_iso(),
    })
    log.info("13F analysis: cached for %s.", quarter)
    return analysis
