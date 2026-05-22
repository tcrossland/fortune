#!/usr/bin/env python3
"""Mark holdings as UK reporting funds from HMRC's published list.

Downloads HMRC's *Approved offshore reporting funds* spreadsheet (an ODS
resolved via the gov.uk content API), extracts every ISIN on it, and
sets ``reporting_status = "reporting"`` in ``data/commodities.toml`` for
any holding that appears on the list.

Only entries currently ``"unknown"`` are touched, and only ever upgraded
to ``"reporting"`` — a deliberate ``non-reporting`` / ``uk-domestic``
you've set is never overwritten. Holdings *absent* from the list are
left ``"unknown"`` and reported for manual review, because absence isn't
the same as ``non-reporting``: direct shares, bonds and structured
products aren't offshore funds at all (they're CGT regardless), while a
genuine offshore fund missing from the list is non-reporting.

    uv run python scripts/fetch_reporting_funds.py

Network required; the list is large (~4.5 MB ODS, ~100k ISINs).
"""

from __future__ import annotations

import io
import json
import re
import sys
import tomllib
import urllib.request
import zipfile
from pathlib import Path

from stdnum import isin as isinmod

DATA_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data")
METADATA = DATA_DIR / "commodities.toml"

CONTENT_API = (
    "https://www.gov.uk/api/content/government/publications/"
    "approved-offshore-reporting-funds"
)

_ISIN_TOKEN = re.compile(r"(?<![A-Z0-9])[A-Z]{2}[A-Z0-9]{9}[0-9](?![A-Z0-9])")
_ISIN_LINE = re.compile(r'^\s*isin\s*=\s*"([^"]+)"')
_UNKNOWN_STATUS = re.compile(r'^(?P<indent>\s*)reporting_status\s*=\s*"unknown"\s*$')


def _attachment_url() -> str:
    with urllib.request.urlopen(CONTENT_API, timeout=60) as response:
        data = json.load(response)
    for attachment in data.get("details", {}).get("attachments", []):
        url = attachment.get("url", "")
        if url.endswith(".ods"):
            return url
    raise RuntimeError("no .ods attachment found on the gov.uk page")


def _reporting_isins() -> set[str]:
    url = _attachment_url()
    print(f"downloading {url}", file=sys.stderr)
    with urllib.request.urlopen(url, timeout=300) as response:
        raw = response.read()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        content = archive.read("content.xml").decode("utf-8", errors="replace")
    candidates = set(_ISIN_TOKEN.findall(content))
    return {c for c in candidates if isinmod.is_valid(c)}


def main() -> int:
    if not METADATA.is_file():
        print(f"{METADATA} not found", file=sys.stderr)
        return 1

    reporting = _reporting_isins()
    print(f"{len(reporting)} reporting-fund ISINs on the HMRC list", file=sys.stderr)

    out: list[str] = []
    current_isin: str | None = None
    updated = 0
    for line in METADATA.read_text(encoding="utf-8").splitlines():
        m_isin = _ISIN_LINE.match(line)
        if m_isin:
            current_isin = m_isin.group(1)
        m_status = _UNKNOWN_STATUS.match(line)
        if m_status and current_isin in reporting:
            out.append(f'{m_status.group("indent")}reporting_status = "reporting"')
            updated += 1
            continue
        out.append(line)

    new_text = "\n".join(out) + "\n"
    tomllib.loads(new_text)  # validate before overwriting
    METADATA.write_text(new_text, encoding="utf-8")
    print(f"Set {updated} holding(s) to reporting in {METADATA}.", file=sys.stderr)

    parsed = tomllib.loads(new_text)
    needs_review = sorted(
        c["isin"]
        for c in parsed.get("commodity", [])
        if c.get("reporting_status") == "unknown"
    )
    if needs_review:
        print(
            f"\n{len(needs_review)} holding(s) not on the list — set manually "
            "(direct shares/bonds/structured products = CGT; offshore funds "
            "absent from the list = non-reporting):",
            file=sys.stderr,
        )
        for code in needs_review:
            print(f"  {code}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
