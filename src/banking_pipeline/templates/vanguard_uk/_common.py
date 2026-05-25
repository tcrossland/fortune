"""Shared parsing helpers for Vanguard UK ISA documents.

Vanguard's PDF-to-text output is plain English and GBP-only:

  - Amounts are ``£`` followed by comma-grouped digits and two decimals
    (``£1,000.00``); a debit row prints a leading minus *before* the
    pound sign (``-£485.30``).
  - Dates appear in two long forms — ``13 February 2025`` in letterhead
    prose and ``13 Feb 2025`` in the per-trade detail lines — plus the
    ``dd/mm/yyyy`` short form in the regular statement's activity table.
  - The platform account number is ``VG`` + 7 digits (``VG0000000``),
    printed under ``Account number:`` on every advice.

These helpers extract those primitives; per-doctype templates compose
them into :class:`~banking_pipeline.models.Transaction` objects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from banking_pipeline.models import RawDocument, Transaction

# ``VG`` + 7 digits, under the ``Account number:`` header.
_ACCOUNT_RE = re.compile(r"Account\s+number:\s*(VG\d{7})", re.I)

# ``£1,000.00`` / ``-£485.30`` — optional leading sign, pound sign,
# comma-grouped integer part, two-decimal fraction.
_MONEY_RE = re.compile(r"(-?)£\s*([\d,]+\.\d{2})")

# First three letters of every English month name, abbreviated or full.
_MONTHS: dict[str, int] = {
    m: i
    for i, m in enumerate(
        (
            "jan", "feb", "mar", "apr", "may", "jun",
            "jul", "aug", "sep", "oct", "nov", "dec",
        ),
        start=1,
    )
}

# ``13 Feb 2025`` / ``13 February 2025`` — day, month name, year.
_LONG_DATE_RE = re.compile(r"\b(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\b")
# ``13/05/2025`` — the regular statement's activity-table short form.
_SHORT_DATE_RE = re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b")


def parse_gbp(value: str) -> Decimal:
    """Parse a ``£`` amount (``£1,000.00`` / ``-£485.30``) into a Decimal.

    Raises :class:`ValueError` when the string carries no pound amount —
    callers extract the substring with :data:`_MONEY_RE` first, so this
    is a guard against a regex/template drift rather than a routine path.
    """

    m = _MONEY_RE.search(value)
    if not m:
        raise ValueError(f"Not a Vanguard GBP amount: {value!r}")
    sign, digits = m.groups()
    amount = Decimal(digits.replace(",", ""))
    return -amount if sign else amount


def parse_long_date(value: str) -> date:
    """Parse a ``13 Feb 2025`` / ``13 February 2025`` date string.

    Matches on the first three letters of the month name, so the
    abbreviated and full forms both resolve.
    """

    m = _LONG_DATE_RE.search(value)
    if not m:
        raise ValueError(f"Not a Vanguard long-form date: {value!r}")
    day_s, month_s, year_s = m.groups()
    month = _MONTHS.get(month_s[:3].lower())
    if month is None:
        raise ValueError(f"Unrecognised month in date: {value!r}")
    return date(int(year_s), month, int(day_s))


def parse_short_date(value: str) -> date:
    """Parse a ``dd/mm/yyyy`` activity-table date string."""

    m = _SHORT_DATE_RE.search(value)
    if not m:
        raise ValueError(f"Not a dd/mm/yyyy date: {value!r}")
    day, month, year = (int(g) for g in m.groups())
    return date(year, month, day)


def find_account_number(text: str) -> str | None:
    """Extract the ``VG#######`` platform account number, or ``None``."""

    m = _ACCOUNT_RE.search(text)
    return m.group(1) if m else None


# Fund name → ticker. The ticker is used as the beancount commodity, but
# Vanguard prints it inconsistently: buy contract notes carry it for
# every fund, sell notes sometimes omit it (printing only the ISIN). The
# fund *name* is present in every block, so it's the reliable key. Add a
# row here when a new fund is first traded — :func:`resolve_ticker` falls
# back to the in-document ticker / ISIN when a name is unmapped.
_TICKER_BY_NAME: dict[str, str] = {
    "FTSE 250 UCITS ETF - Accumulating": "VMIG",
    "U.K. Gilt UCITS ETF - Accumulating": "VGVA",
}


def resolve_ticker(
    name: str, parens_ticker: str | None = None, isin: str | None = None
) -> str | None:
    """Resolve a fund's beancount commodity (its ticker) from a trade block.

    Prefers the curated :data:`_TICKER_BY_NAME` entry (always present and
    consistent), then the ticker Vanguard printed in parentheses next to
    the name (present on buys, sometimes on sells), then the ISIN as a
    last resort. Returning the ISIN keeps extraction deterministic for an
    unmapped, ticker-less sell — but it won't match the buy's ticker-keyed
    lot, so ``bean-check`` will surface it rather than it passing silently.
    """

    mapped = _TICKER_BY_NAME.get(name)
    if mapped:
        return mapped
    if parens_ticker:
        return parens_ticker
    return isin


@dataclass
class NoOpTemplate:
    """Template that always extracts nothing.

    Registered for Vanguard's paper-trail-only doctypes (the ISA
    declaration, costs-and-charges illustration, direct-debit mandate
    confirmation, cash-holding notice). Returning ``[]`` here means the
    :class:`~banking_pipeline.fields.hybrid.HybridExtractor` takes the
    explicit ``NO_OUTPUT_DOCTYPES`` branch (logged at INFO) instead of
    falling through to the generic regex extractor, so these documents
    can never accidentally seed a junk transaction into the sidecar.
    """

    template_id: str

    def extract(self, doc: RawDocument) -> list[Transaction]:  # noqa: ARG002
        return []
