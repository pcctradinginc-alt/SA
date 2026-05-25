"""Minimal 13D/13G locator.

A full 13D/G parser is filing-specific and noisy; for verification purposes we
only need to know that a beneficial-ownership filing exists for the entity and
which issuer it concerns. Issuer detection from the document text is best-effort.
"""
from __future__ import annotations

from pathlib import Path


def summarize_filing(filing_dir: Path) -> dict:
    """Return a light summary: form present + any detected subject company."""
    subject = ""
    for path in sorted(filing_dir.glob("*.txt")) + sorted(filing_dir.glob("*.htm*")):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # SUBJECT COMPANY block in the SGML header names the issuer.
        marker = "SUBJECT COMPANY"
        if marker in text:
            after = text.split(marker, 1)[1]
            for line in after.splitlines():
                if "COMPANY CONFORMED NAME" in line:
                    subject = line.split(":", 1)[-1].strip()
                    break
        if subject:
            break
    return {"subject_company": subject}
