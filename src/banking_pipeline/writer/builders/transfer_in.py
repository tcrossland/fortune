"""Free-of-payment securities transfer-in beancount builder.

Renders Pictet's ``LIQUIDACIÓN / RECEPCIÓN DE VALORES (GRATUITA)``
advice — securities arriving from an external custodian without a
cash payment. The position lands in the portfolio's ``Assets:<prefix>:
<portfolio>:<ISIN>`` account at a cost basis derived from the
``Estimacion de transferencia`` line on the advice; the offsetting
posting goes to ``Equity:<prefix>:<portfolio>:Transfers`` since
there's no cash leg to balance against.

Layout::

    <booking_date> * "<title>" "<narration>"
      Assets:<prefix>:<portfolio>:<ISIN>     <quantity> <ISIN> {{<abs_amount> <ccy>, <lot_date>}}
      Equity:<prefix>:<portfolio>:Transfers  <amount>   <ccy>
      no: <transaction_number>

The asset leg uses beancount's *total-cost* form (double-brace
``{{ }}`` rather than per-unit ``{ }``) because Pictet prints the
total transfer value rather than a per-unit price — letting
beancount divide preserves precision and avoids rounding-induced
imbalance on the entry. The lot date is the actual transfer date
from the document's ``Transferencia / Fecha`` line, which may
differ from the ``booking_date`` used in the entry header (Pictet
books arrivals one calendar day after the position physically
moves).

Sign convention
---------------
The extractor stores ``amount`` as a signed-negative cost-basis
total (cash-equivalent leaving the equity bucket and landing as
an asset). The asset leg's total-cost annotation uses
``abs(amount)`` because beancount's ``{{<total>}}`` form expects a
positive total; the equity leg is rendered with the signed
``amount`` directly so the entry balances.
"""

from __future__ import annotations

from banking_pipeline.models import DocumentType, Transaction
from banking_pipeline.writer.builders.fallback import render as render_fallback
from banking_pipeline.writer.format import (
    align,
    format_amount,
    header_line,
    portfolio_segment,
    transaction_number_comment,
)

TRANSFER_IN_TYPES: frozenset[DocumentType] = frozenset({
    DocumentType.LIQUIDACION_RECEPCION_DE_VALORES,
})


def render(tx: Transaction, doc_type: DocumentType, prefix: str) -> str:
    """Render a free-of-payment securities transfer-in advice."""

    if tx.isin is None or tx.quantity is None:
        # Fallback keeps the writer producing parseable beancount on
        # malformed input — the regex extractor would land here on a
        # document whose ENTRADA block we couldn't parse.
        return render_fallback(tx, doc_type, prefix)

    portfolio = portfolio_segment(tx.account_number)
    # Lot date for the cost basis comes from the extractor's
    # ``trade_date`` (the ``Transferencia / Fecha`` line on the
    # source advice), which may differ from the entry-level
    # ``booking_date`` used in the header.
    lot_date = tx.trade_date

    lines: list[str] = [header_line(tx)]

    # Asset leg with total-cost ``{{<total> <ccy>, <date>}}``
    # annotation. Using the total-cost form (rather than per-unit
    # ``{<price>}``) preserves Pictet's printed transfer value
    # exactly and lets beancount derive per-unit cost without
    # rounding drift.
    lines.append(
        align(
            f"Assets:{prefix}:{portfolio}:{tx.isin}",
            format_amount(tx.quantity),
            tx.isin,
            extras=(
                f" {{{{{format_amount(abs(tx.amount))} {tx.currency}, "
                f"{lot_date}}}}}"
            ),
        )
    )

    # Equity offset leg — the cost-basis cash-equivalent flowing
    # from the user's equity bucket into the asset position. Sign
    # is the negative of the asset leg's value (the extractor
    # stored it that way, so we render verbatim).
    lines.append(
        align(
            f"Equity:{prefix}:{portfolio}:Transfers",
            format_amount(tx.amount),
            tx.currency,
        )
    )

    trailer = transaction_number_comment(tx)
    if trailer:
        lines.append(trailer)

    return "\n".join(lines) + "\n"
