#!/usr/bin/env python3
"""Populate ``data/fx/hmrc-monthly-average.csv`` from HMRC's published rates.

Source: HMRC's monthly exchange-rate CSVs served by the trade-tariff
service, e.g.::

    https://www.trade-tariff.service.gov.uk/api/v2/exchange_rates/files/monthly_csv_2024-3.csv

Those publish ``Currency Units per £1`` (foreign units per pound). The
banking-pipeline rate file wants the **inverse** — GBP per 1 unit of the
foreign currency — because the extractor does ``gbp_value = amount *
rate``. So each value written here is ``1 / (units per £1)``.

Run it from the repo root (network required)::

    uv run python scripts/fetch_hmrc_rates.py

Months that aren't published yet are skipped with a note. These figures
feed UK tax computations — spot-check a few against gov.uk before
relying on the output for a return.
"""

from __future__ import annotations

import csv
import io
import sys
import urllib.error
import urllib.request
from decimal import Decimal, getcontext
from pathlib import Path

# Foreign currencies referenced by the ledgers (GBP is the home currency
# and needs no rate). Extend this set if new currencies appear.
CURRENCIES = frozenset({"EUR", "USD", "JPY", "CHF", "DKK", "HKD", "SEK"})

OUTPUT = Path("data/fx/hmrc-monthly-average.csv")
URL_TEMPLATE = (
    "https://www.trade-tariff.service.gov.uk/api/v2/exchange_rates/"
    "files/monthly_csv_{year}-{month}.csv"
)

# Inclusive month range. Start covers the earliest ledger activity (2021);
# the end is generous — unpublished future months are skipped.
START = (2021, 1)
END = (2026, 6)

# Plenty of precision for the reciprocal; the model parses these as Decimal.
getcontext().prec = 28
_QUANTUM = Decimal("1e-10")


def _months(start: tuple[int, int], end: tuple[int, int]):
    year, month = start
    while (year, month) <= end:
        yield year, month
        month += 1
        if month > 12:
            month = 1
            year += 1


def _fetch(year: int, month: int) -> str | None:
    url = URL_TEMPLATE.format(year=year, month=month)
    try:
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 — fixed HTTPS host
            return response.read().decode("utf-8")
    except urllib.error.URLError:
        return None


def _parse(text: str) -> dict[str, Decimal]:
    reader = csv.DictReader(io.StringIO(text))
    fields = reader.fieldnames or []
    code_col = next(
        (f for f in fields if f.strip().lower() == "currency code"), None
    )
    rate_col = next((f for f in fields if "units per" in f.lower()), None)
    if code_col is None or rate_col is None:
        return {}

    rates: dict[str, Decimal] = {}
    for row in reader:
        code = (row.get(code_col) or "").strip().upper()
        if code not in CURRENCIES:
            continue
        raw = (row.get(rate_col) or "").strip()
        if not raw:
            continue
        units = Decimal(raw)
        if units > 0:
            # Invert: file stores GBP per 1 unit of the foreign currency.
            rates[code] = (Decimal(1) / units).quantize(_QUANTUM)
    return rates


def main() -> int:
    rows: list[tuple[str, str, Decimal]] = []
    months_written = 0
    for year, month in _months(START, END):
        text = _fetch(year, month)
        if text is None:
            print(f"skip {year}-{month:02d} (not available)", file=sys.stderr)
            continue
        rates = _parse(text)
        if not rates:
            print(
                f"warn {year}-{month:02d}: no target currencies in CSV",
                file=sys.stderr,
            )
            continue
        for code in sorted(rates):
            rows.append((f"{year}-{month:02d}", code, rates[code]))
        months_written += 1

    if not rows:
        print("error: fetched no rates (network down?)", file=sys.stderr)
        return 1

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["month", "currency", "rate"])
        for month_label, code, rate in rows:
            writer.writerow([month_label, code, rate])

    print(
        f"wrote {len(rows)} rows across {months_written} month(s) -> {OUTPUT}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
