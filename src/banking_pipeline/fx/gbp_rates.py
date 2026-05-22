"""GBP spot-rate sourcing for UK capital-gains cost basis.

UK CGT requires every security acquisition and disposal to be recorded
at its GBP equivalent at the trade-date spot rate, and the section 104
pool is maintained in GBP. Pictet trade confirmations are denominated in
EUR/USD/etc.; this module supplies the GBP-per-unit-of-local-currency
rate the extractor stamps onto each :class:`~banking_pipeline.models.Transaction`.

The :class:`GbpRateSource` protocol keeps the lookup pluggable. The two
concrete sources today are :class:`HmrcMonthlyAverageSource` (a
user-maintained CSV of HMRC monthly average rates) and
:class:`NullSource` (always ``None`` — the default, so the rest of the
pipeline behaves exactly as before unless GBP sourcing is opted into).
"""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Protocol, Self, runtime_checkable

from banking_pipeline.config import Settings


@runtime_checkable
class GbpRateSource(Protocol):
    """Resolves a GBP-per-1-unit-of-``currency`` rate for a given date.

    Implementations must never raise on a missing rate — they return
    ``None`` so the extractor can leave ``Transaction.gbp_rate`` unset
    and downstream builders fall back to their non-GBP behaviour.
    """

    def get_rate(self, on_date: date, currency: str) -> Decimal | None: ...


def _parse_rates(text: str) -> dict[tuple[str, str], Decimal]:
    """Parse the HMRC monthly-average CSV into a ``(month, ccy) -> rate`` map.

    Columns: ``month`` (``YYYY-MM``), ``currency`` (ISO-4217), ``rate``
    (GBP per 1 unit of ``currency``). Currency keys are upper-cased; the
    month is kept as the raw ``YYYY-MM`` string.
    """

    rates: dict[tuple[str, str], Decimal] = {}
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        month = (row.get("month") or "").strip()
        currency = (row.get("currency") or "").strip().upper()
        raw_rate = (row.get("rate") or "").strip()
        if not month or not currency or not raw_rate:
            continue
        rates[(month, currency)] = Decimal(raw_rate)
    return rates


class HmrcMonthlyAverageSource:
    """GBP rates from HMRC's published monthly average exchange rates.

    Backed by a user-maintained CSV (default
    ``data/fx/hmrc-monthly-average.csv``) with ``month`` (``YYYY-MM``),
    ``currency`` and ``rate`` columns. A trade date is resolved by
    snapping it to its calendar month; an absent month or currency
    yields ``None``.
    """

    def __init__(self, rates: dict[tuple[str, str], Decimal]) -> None:
        self._rates = rates

    @classmethod
    def from_path(cls, path: Path) -> Self:
        return cls(_parse_rates(path.read_text(encoding="utf-8")))

    @classmethod
    def from_text(cls, text: str) -> Self:
        return cls(_parse_rates(text))

    def get_rate(self, on_date: date, currency: str) -> Decimal | None:
        return self._rates.get((f"{on_date:%Y-%m}", currency.upper()))


class NullSource:
    """A source that has no rates — always returns ``None``."""

    def get_rate(self, on_date: date, currency: str) -> Decimal | None:
        return None


_DEFAULT_HMRC_PATH = Path("data/fx/hmrc-monthly-average.csv")


def build_rate_source(settings: Settings) -> GbpRateSource:
    """Construct the configured GBP rate source.

    ``gbp_rate_source == "hmrc-monthly"`` loads the CSV at
    ``settings.hmrc_rate_path`` (or the default path); a missing file
    degrades to :class:`NullSource` rather than failing, in keeping with
    the "never fail extraction on a missing rate" contract. Anything
    else returns :class:`NullSource`.
    """

    if settings.gbp_rate_source == "hmrc-monthly":
        path = settings.hmrc_rate_path or _DEFAULT_HMRC_PATH
        if path.is_file():
            return HmrcMonthlyAverageSource.from_path(path)
        return NullSource()
    return NullSource()
