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
from datetime import date, timedelta
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


def _parse_ecb_rates(text: str) -> dict[tuple[str, str], Decimal]:
    """Parse the ECB daily CSV into a ``(YYYY-MM-DD, ccy) -> rate`` map.

    Columns: ``date`` (``YYYY-MM-DD``), ``currency`` (ISO-4217), ``rate``
    (GBP per 1 unit of ``currency``, already triangulated from the ECB
    EUR-reference set by ``scripts/fetch_ecb_rates.py``).
    """

    rates: dict[tuple[str, str], Decimal] = {}
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        day = (row.get("date") or "").strip()
        currency = (row.get("currency") or "").strip().upper()
        raw_rate = (row.get("rate") or "").strip()
        if not day or not currency or not raw_rate:
            continue
        rates[(day, currency)] = Decimal(raw_rate)
    return rates


class EcbDailyRateSource:
    """GBP rates from the ECB daily euro foreign-exchange reference rates.

    Backed by a user-maintained CSV (default ``data/fx/ecb-daily.csv``) with
    ``date`` (``YYYY-MM-DD``), ``currency`` and ``rate`` columns — the rate is
    GBP per 1 unit of the currency, triangulated from ECB's EUR-reference set
    by the fetcher (``GBP-per-X = (GBP per EUR) / (X per EUR)``, both from the
    same publication day). ``GBP`` resolves to ``1``.

    The ECB publishes one fixing per working day (~16:00 CET), so a trade on a
    weekend or a TARGET holiday has no rate of its own; the date is resolved to
    the **latest publication on or before it** via a bounded day-by-day
    walk-back. A gap beyond the walk-back yields ``None`` — a genuine hole (a
    stale CSV at the leading edge) surfaces as a :class:`RateGap` rather than
    silently marking at an old rate.

    These are ECB *reference* rates — a mid-market fixing the ECB itself
    flags as "for information, not transaction, purposes" — so this is a
    consistent **spot proxy** for UK CGT, not a broker's dealt rate. It will
    not equal a custodian's booked GBP (which carries a dealer spread); use a
    per-transaction ``gbp_rate`` for that.
    """

    # ECB skips only weekends + TARGET holidays; the longest run (an Easter or
    # Christmas cluster) is ~4 days, so 7 covers every real gap while a
    # genuinely stale leading edge still surfaces as no rate.
    _MAX_LOOKBACK_DAYS = 7

    def __init__(self, rates: dict[tuple[str, str], Decimal]) -> None:
        self._rates = rates

    @classmethod
    def from_path(cls, path: Path) -> Self:
        return cls(_parse_ecb_rates(path.read_text(encoding="utf-8")))

    @classmethod
    def from_text(cls, text: str) -> Self:
        return cls(_parse_ecb_rates(text))

    def get_rate(self, on_date: date, currency: str) -> Decimal | None:
        ccy = currency.upper()
        if ccy == "GBP":
            return Decimal(1)
        day = on_date
        for _ in range(self._MAX_LOOKBACK_DAYS + 1):
            rate = self._rates.get((day.isoformat(), ccy))
            if rate is not None:
                return rate
            day -= timedelta(days=1)
        return None


class NullSource:
    """A source that has no rates — always returns ``None``."""

    def get_rate(self, on_date: date, currency: str) -> Decimal | None:
        return None


class ForwardFillRateSource:
    """Wrap a rate source so a missing month falls back to the most recent
    earlier month that has a rate (bounded month-by-month look-back).

    For *mark-to-market valuation* only. A month-end statement is dated to
    the following day (a 30 June snapshot carries ``on_date`` 1 July), so it
    asks for a leading-edge month the local CSV may not carry yet — HMRC
    publishes each month's rate in advance (near the end of the prior month),
    but the CSV is refreshed manually (``scripts/fetch_hmrc_rates.py``) and
    can lag the newest statement. Rather than drop every non-GBP holding as a
    :class:`RateGap` and collapse the snapshot, valuation reports mark to the
    latest *known* rate, matching the balance sheet's "latest rate on or
    before the as-of date" behaviour.

    NOT for tax: UK CGT requires the exact trade-month HMRC average, so the
    tax pipeline keeps the un-wrapped source. The look-back is bounded so a
    genuine multi-month hole still surfaces as a gap rather than silently
    valuing at a stale rate.
    """

    # A leading-edge gap is a single month the CSV hasn't been refreshed to
    # yet; the cap only guards against a genuinely absent stretch (which then
    # still flags a gap).
    _MAX_LOOKBACK_MONTHS = 12

    def __init__(self, base: GbpRateSource) -> None:
        self._base = base

    def get_rate(self, on_date: date, currency: str) -> Decimal | None:
        month = on_date
        for _ in range(self._MAX_LOOKBACK_MONTHS + 1):
            rate = self._base.get_rate(month, currency)
            if rate is not None:
                return rate
            # Step to the last day of the previous month (get_rate snaps to
            # ``%Y-%m``, so any day within the target month resolves it).
            month = month.replace(day=1) - timedelta(days=1)
        return None


_DEFAULT_HMRC_PATH = Path("data/fx/hmrc-monthly-average.csv")
_DEFAULT_ECB_PATH = Path("data/fx/ecb-daily.csv")


def build_rate_source(settings: Settings) -> GbpRateSource:
    """Construct the configured GBP rate source.

    ``gbp_rate_source == "hmrc-monthly"`` loads the CSV at
    ``settings.hmrc_rate_path``; ``"ecb-daily"`` loads
    ``settings.ecb_rate_path`` (each with its default path). A missing file
    degrades to :class:`NullSource` rather than failing, in keeping with the
    "never fail extraction on a missing rate" contract. Anything else (incl.
    ``"null"``) returns :class:`NullSource`.
    """

    if settings.gbp_rate_source == "hmrc-monthly":
        path = settings.hmrc_rate_path or _DEFAULT_HMRC_PATH
        if path.is_file():
            return HmrcMonthlyAverageSource.from_path(path)
        return NullSource()
    if settings.gbp_rate_source == "ecb-daily":
        path = settings.ecb_rate_path or _DEFAULT_ECB_PATH
        if path.is_file():
            return EcbDailyRateSource.from_path(path)
        return NullSource()
    return NullSource()
