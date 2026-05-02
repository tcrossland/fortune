"""Cross-currency internal-transfer beancount builder.

Renders cross-currency book transfers between two of the user's own
current accounts. The single entry holds the source-currency debit
leg, the destination-currency credit leg with an
``@@ <abs_source> <src_ccy>`` annotation, and the trailing ``no:``
reference. Both ``INTERNAL_TRANSFER`` and ``SPOT`` produce the same
shape: source and destination are both
``Assets:Pic:<portfolio>:<ccy>`` accounts at the same Pictet portfolio,
with FX inside the document. Earlier ``SPOT`` rendered through the
legacy ``_FX_LEG_TEMPLATE`` which emitted two entries balanced against
``Equity:Uncategorized``; routing it here gives a single
self-balancing entry.

``FX_FORWARD``'s template returns ``[]`` (the opening has no cash
impact; the matching ``SETTLE_FX_FORWARD`` advice books the cash
exchange at maturity and is routed through
:mod:`banking_pipeline.writer.builders.fx_settlement`).
"""

from __future__ import annotations

from banking_pipeline.models import DocumentType, Transaction
from banking_pipeline.writer.builders.fallback import render as render_fallback
from banking_pipeline.writer.format import (
    align,
    cash_account,
    format_amount,
    header_line,
    transaction_number_comment,
)

INTERNAL_TRANSFER_TYPES: frozenset[DocumentType] = frozenset({
    DocumentType.INTERNAL_TRANSFER,
    # Spanish-locale cross-currency book transfer between two of the
    # client's own accounts — same source/destination ``Assets:Pic:...``
    # shape as the EN sibling, just emitted under
    # ``TRANSFERENCIA INTERNA DE EFECTIVO`` rather than
    # ``Internal money transfer``.
    DocumentType.TRANSFERENCIA_INTERNA,
    DocumentType.SPOT,
    # Spanish-locale spot FX trade ("MERCADO DE DIVISAS / Cambio de
    # divisas al contado") — same two-cash-leg shape as ``SPOT``, just
    # in the Pictet Madrid template family.
    DocumentType.CAMBIO_DE_DIVISAS,
})


def render(tx: Transaction, doc_type: DocumentType, prefix: str) -> str:
    """Render a Pictet cross-currency internal-money-transfer advice.

    Layout::

        <booking_date> * "<title>" "<narration>"
          Assets:<prefix>:<currency>             <amount> <ccy>
          Assets:<prefix>:<counter_currency>     <counter_amount> <counter_ccy> @@ <abs_amount> <ccy>
          no: <transaction_number>

    Both legs are positive-or-negative as Pictet stored them: the source
    leg is signed-negative (cash out) and the destination leg is
    signed-positive (cash in). The destination leg's ``@@ <abs_source>
    <src_ccy>`` annotation tells beancount the conversion total — this
    is what lets it cross-reconcile the two cash currencies on a
    single entry rather than splitting into two ``Equity:Uncategorized``-
    balanced entries.

    Falls back to the catch-all
    :mod:`banking_pipeline.writer.builders.fallback` builder if
    ``counter_currency`` / ``counter_amount`` aren't populated
    (legacy callers that built ``Transaction`` objects without the
    cross-leg fields). In practice every current extractor populates
    both fields, so the fallback is defensive only.
    """

    if tx.counter_currency is None or tx.counter_amount is None:
        return render_fallback(tx, doc_type, prefix)

    lines: list[str] = [header_line(tx)]

    # Source (debit) leg — signed negative as printed.
    lines.append(
        align(
            cash_account(prefix, tx.account_number, tx.currency),
            format_amount(tx.amount),
            tx.currency,
        )
    )

    # Destination (credit) leg with ``@@`` total-cost annotation. The
    # absolute value of the source leg's amount goes in the source
    # currency on the right of the ``@@`` — beancount uses that to
    # reconcile the two cash currencies without needing the explicit
    # rate field.
    lines.append(
        align(
            cash_account(prefix, tx.account_number, tx.counter_currency),
            format_amount(tx.counter_amount),
            tx.counter_currency,
            extras=f" @@ {format_amount(abs(tx.amount))} {tx.currency}",
        )
    )

    trailer = transaction_number_comment(tx)
    if trailer:
        lines.append(trailer)

    return "\n".join(lines) + "\n"
