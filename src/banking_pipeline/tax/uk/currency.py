"""GBP conversion for tax computation.

Every amount that lands on an SA106 / SA108 line must be in GBP. The
preferred source is the trade-date rate the extractor already stamped on
the transaction (:attr:`Transaction.gbp_rate`); a
:class:`~banking_pipeline.fx.gbp_rates.GbpRateSource` is the fallback for
transactions that predate that enrichment.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from banking_pipeline.fx.gbp_rates import GbpRateSource


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
