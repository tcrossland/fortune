#!/usr/bin/env python3
"""Populate ``data/fx/ecb-daily.csv`` from the ECB euro reference rates.

Source: the ECB's full-history euro foreign-exchange reference rates, a
daily (~16:00 CET, working days only) mid-market fixing::

    https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip

That file is EUR-based — each cell is *units of the currency per 1 EUR*.
The banking-pipeline rate file wants **GBP per 1 unit of the currency**, so
each rate is triangulated through the same day's EUR/GBP fixing::

    GBP-per-X = (GBP per EUR) / (X per EUR)

and EUR itself is stored as (GBP per EUR). GBP is omitted (the source returns
1 for it). Rows with no GBP fixing that day can't be triangulated and are
skipped.

Run it from the repo root (network required)::

    uv run python scripts/fetch_ecb_rates.py

These are ECB *reference* rates — the ECB flags them as for information, not
transaction, purposes — so they are a consistent **spot proxy** for UK CGT,
not a broker's dealt rate. Spot-check a few against ecb.europa.eu, and pick
one source and stick with it across the whole section 104 history.
"""

from __future__ import annotations

import csv
import io
import sys
import urllib.error
import urllib.request
import zipfile
from decimal import Decimal, getcontext
from pathlib import Path

# Foreign currencies referenced by the ledgers (GBP is home; EUR is the ECB
# base and is stored directly). Extend to match ``fetch_hmrc_rates.py``.
CURRENCIES = frozenset({"EUR", "USD", "JPY", "CHF", "DKK", "HKD", "SEK"})

OUTPUT = Path("data/fx/ecb-daily.csv")
URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"

getcontext().prec = 28
_QUANTUM = Decimal("1e-8")


def _fetch() -> str | None:
    """Download the history zip and return the CSV text inside, or None."""

    try:
        with urllib.request.urlopen(URL, timeout=60) as response:  # noqa: S310 — fixed HTTPS host
            blob = response.read()
    except urllib.error.URLError as exc:
        print(f"error: could not fetch {URL}: {exc}", file=sys.stderr)
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
            return zf.read(name).decode("utf-8")
    except (zipfile.BadZipFile, StopIteration) as exc:
        print(f"error: unexpected download format: {exc}", file=sys.stderr)
        return None


def _triangulate(text: str) -> list[tuple[str, str, Decimal]]:
    """Wide EUR-based ECB CSV → (date, currency, GBP-per-unit) rows."""

    reader = csv.reader(io.StringIO(text))
    header = [c.strip() for c in next(reader, [])]
    if not header or header[0].lower() != "date" or "GBP" not in header:
        return []
    col = {name: i for i, name in enumerate(header)}
    gbp_i = col["GBP"]

    rows: list[tuple[str, str, Decimal]] = []
    for raw in reader:
        if not raw or not raw[0].strip():
            continue
        day = raw[0].strip()
        gbp_raw = raw[gbp_i].strip() if gbp_i < len(raw) else ""
        if not gbp_raw or gbp_raw.upper() == "N/A":
            continue  # can't triangulate without the day's EUR/GBP fixing
        gbp_per_eur = Decimal(gbp_raw)
        rows.append((day, "EUR", gbp_per_eur.quantize(_QUANTUM)))
        for ccy in CURRENCIES - {"EUR"}:
            i = col.get(ccy)
            if i is None or i >= len(raw):
                continue
            cell = raw[i].strip()
            if not cell or cell.upper() == "N/A":
                continue
            per_eur = Decimal(cell)
            if per_eur > 0:
                rows.append((day, ccy, (gbp_per_eur / per_eur).quantize(_QUANTUM)))
    return rows


def main() -> int:
    text = _fetch()
    if text is None:
        return 1
    rows = _triangulate(text)
    # Sanity guard on distinct *days* (not rows — rows mix ~7 currencies, so a
    # row count can clear a low bar while covering only a few months). The
    # history spans 1999→present (~7k publication days), so a healthy pull is
    # in the thousands; a small day count means a truncated or changed feed.
    days = len({r[0] for r in rows})
    if days < 1000:
        print(
            f"error: only {days} day(s) parsed — refusing to overwrite "
            f"{OUTPUT} (likely a truncated or changed ECB feed)",
            file=sys.stderr,
        )
        return 1

    rows.sort(key=lambda r: (r[0], r[1]))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["date", "currency", "rate"])
        for day, ccy, rate in rows:
            writer.writerow([day, ccy, rate])
    tmp.replace(OUTPUT)  # atomic

    print(f"wrote {len(rows)} rows across {days} days to {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
