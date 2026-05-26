"""GBP conversion for tax computation.

Every amount that lands on an SA106 / SA108 line must be in GBP. The
preferred source is the trade-date rate the extractor already stamped on
the transaction (:attr:`Transaction.gbp_rate`); a
:class:`~banking_pipeline.fx.gbp_rates.GbpRateSource` is the fallback for
transactions that predate that enrichment.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from banking_pipeline.fx.gbp_rates import GbpRateSource


@dataclass(frozen=True)
class RateGap:
    """A GBP-rate that couldn't be resolved for a tax amount.

    Records the security plus the ``currency`` and ``month`` (``YYYY-MM``)
    the conversion needed, so a coverage warning can name the exact HMRC
    monthly-average CSV row to add — rather than just flagging the ISIN.
    """

    isin: str
    currency: str
    month: str

    @classmethod
    def at(cls, isin: str, currency: str, on_date: date) -> RateGap:
        return cls(isin=isin, currency=currency.upper(), month=f"{on_date:%Y-%m}")


def to_gbp(
    amount: Decimal,
    *,
    currency: str,
    on_date: date,
    gbp_rate: Decimal | None = None,
    source: GbpRateSource | None = None,
) -> Decimal | None:
    """Convert ``amount`` (in ``currency``) to GBP, or ``None`` if no rate.

    Resolution order: GBP is 1:1; otherwise the supplied per-transaction
    ``gbp_rate`` (GBP per 1 unit of ``currency``); otherwise a lookup
    against ``source`` at ``on_date``. Returns ``None`` when no rate is
    available so the caller can flag the gap rather than silently
    emitting a wrong figure.
    """

    if currency.upper() == "GBP":
        return amount
    if gbp_rate is not None:
        return amount * gbp_rate
    if source is not None:
        rate = source.get_rate(on_date, currency)
        if rate is not None:
            return amount * rate
    return None


def to_gbp_all(
    amounts: Iterable[Decimal],
    *,
    currency: str,
    on_date: date,
    gbp_rate: Decimal | None = None,
    source: GbpRateSource | None = None,
) -> list[Decimal] | None:
    """Convert several amounts that share one rate context, or ``None``.

    All amounts are in the same ``currency`` at the same ``on_date`` (the
    gross / withholding / net trio on a dividend, say), so they resolve a
    GBP rate the same way — see :func:`to_gbp`. Returns the converted list,
    or ``None`` if *any* amount can't be converted, so a caller can record a
    single rate gap and skip the whole row rather than converting some legs
    and not others.
    """

    out: list[Decimal] = []
    for amount in amounts:
        value = to_gbp(
            amount, currency=currency, on_date=on_date,
            gbp_rate=gbp_rate, source=source,
        )
        if value is None:
            return None
        out.append(value)
    return out
