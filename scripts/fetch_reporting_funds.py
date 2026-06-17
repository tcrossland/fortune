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

Safety: the rewrite is order-independent within each ``[[commodity]]``
block (it doesn't assume ``isin`` precedes ``reporting_status``), the new
content is TOML-validated *and* sanity-checked against an implausibly
small ISIN count before anything is written, the original is backed up to
``commodities.toml.bak``, and the write is atomic (temp file + replace).
A network / format failure exits non-zero with a clear message and never
touches the metadata. ``apply_reporting_status`` is pure and unit-tested.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import tomllib
import urllib.error
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

# The HMRC list carries ~100k ISINs. A result far below this means a
# truncated / wrong download — abort rather than wipe statuses.
MIN_EXPECTED_ISINS = 10_000

_ISIN_TOKEN = re.compile(r"(?<![A-Z0-9])[A-Z]{2}[A-Z0-9]{9}[0-9](?![A-Z0-9])")
# A [[commodity]] table header on its own line (keeps the file splittable
# into per-holding blocks so the isin↔status rewrite is order-independent).
_BLOCK_SPLIT = re.compile(r"(?m)^(\[\[commodity\]\][ \t]*\n)")
_ISIN_LINE = re.compile(r'(?m)^\s*isin\s*=\s*"([^"]+)"')
_UNKNOWN_STATUS = re.compile(r'(?m)^(?P<indent>\s*)reporting_status\s*=\s*"unknown"\s*$')


class FetchError(RuntimeError):
    """A recoverable failure (network / format) — reported, not a traceback."""


def _attachment_url() -> str:
    try:
        with urllib.request.urlopen(CONTENT_API, timeout=60) as response:
            data = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"could not reach the gov.uk content API: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise FetchError(f"gov.uk content API returned non-JSON: {exc}") from exc
    for attachment in data.get("details", {}).get("attachments", []):
        url = attachment.get("url", "")
        if url.endswith(".ods"):
            return url
    raise FetchError(
        "no .ods attachment found on the gov.uk page (the publication may "
        "have changed format)"
    )


def _reporting_isins() -> set[str]:
    """Download the HMRC ODS and return its set of valid ISINs.

    Raises :class:`FetchError` on any network / archive / format problem so
    the caller can abort cleanly without touching the metadata.
    """

    url = _attachment_url()
    print(f"downloading {url}", file=sys.stderr)
    try:
        with urllib.request.urlopen(url, timeout=300) as response:
            raw = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"could not download the ODS: {exc}") from exc
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            content = archive.read("content.xml").decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise FetchError(
            f"downloaded file isn't a readable ODS (content.xml missing?): {exc}"
        ) from exc
    candidates = set(_ISIN_TOKEN.findall(content))
    return {c for c in candidates if isinmod.is_valid(c)}


def apply_reporting_status(text: str, reporting: set[str]) -> tuple[str, int]:
    """Upgrade ``reporting_status = "unknown"`` → ``"reporting"`` for any
    ``[[commodity]]`` whose ISIN is in ``reporting``.

    Pure and order-independent: the file is split into per-holding blocks
    and each block's status line is matched regardless of whether it sits
    before or after the ``isin`` line, so a reordered block can't be
    mis-associated. Comments, formatting and every other field are left
    byte-for-byte intact; only a listed-and-unknown status line changes.
    Returns ``(new_text, n_updated)``.
    """

    parts = _BLOCK_SPLIT.split(text)
    out = [parts[0]]  # preamble before the first [[commodity]]
    updated = 0
    for i in range(1, len(parts), 2):
        header = parts[i]
        body = parts[i + 1] if i + 1 < len(parts) else ""
        m_isin = _ISIN_LINE.search(body)
        if m_isin is not None and m_isin.group(1) in reporting:
            body, n = _UNKNOWN_STATUS.subn(
                lambda m: f'{m.group("indent")}reporting_status = "reporting"',
                body,
            )
            updated += n
        out.append(header)
        out.append(body)
    return "".join(out), updated


def _needs_review(parsed: dict[str, object]) -> list[str]:
    commodities = parsed.get("commodity", [])
    if not isinstance(commodities, list):
        return []
    return sorted(
        str(c["isin"])
        for c in commodities
        if isinstance(c, dict) and c.get("reporting_status") == "unknown"
    )


def main() -> int:
    if not METADATA.is_file():
        print(f"{METADATA} not found", file=sys.stderr)
        return 1

    try:
        reporting = _reporting_isins()
    except FetchError as exc:
        print(f"aborted: {exc}", file=sys.stderr)
        return 1

    print(f"{len(reporting)} reporting-fund ISINs on the HMRC list", file=sys.stderr)
    if len(reporting) < MIN_EXPECTED_ISINS:
        print(
            f"aborted: only {len(reporting)} ISINs parsed (expected "
            f"~100k) — the download looks truncated/wrong; metadata "
            "left untouched.",
            file=sys.stderr,
        )
        return 1

    original = METADATA.read_text(encoding="utf-8")
    new_text, updated = apply_reporting_status(original, reporting)

    try:
        parsed = tomllib.loads(new_text)  # validate before overwriting
    except tomllib.TOMLDecodeError as exc:
        print(f"aborted: rewrite produced invalid TOML ({exc}); not written.",
              file=sys.stderr)
        return 1

    if new_text != original:
        backup = METADATA.with_name(METADATA.name + ".bak")
        backup.write_text(original, encoding="utf-8")
        tmp = METADATA.with_name(METADATA.name + ".tmp")
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, METADATA)  # atomic
        print(f"Set {updated} holding(s) to reporting in {METADATA} "
              f"(backup: {backup.name}).", file=sys.stderr)
    else:
        print("No changes — every listed holding was already classified.",
              file=sys.stderr)

    needs_review = _needs_review(parsed)
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
