"""FX-forward settlement beancount builder.

Same two-leg-cash shape as
:mod:`banking_pipeline.writer.builders.internal_transfer` but with a
third ``Expenses:<prefix>:Fees:<ccy>`` posting carrying the forward
spread, and the ``@@`` value derived from the pre-fee gross rather
than the post-fee net.
"""

from __future__ import annotations

from banking_pipeline.models import DocumentType, Transaction
from banking_pipeline.writer.builders.internal_transfer import (
    render as render_internal_transfer,
)
from banking_pipeline.writer.format import (
    align,
    cash_account,
    format_amount,
    header_line,
    portfolio_segment,
    transaction_number_comment,
)

FX_SETTLEMENT_TYPES: frozenset[DocumentType] = frozenset({
    DocumentType.SETTLE_FX_FORWARD,
})


def render(tx: Transaction, doc_type: DocumentType, prefix: str) -> str:
    """Render a Pictet ``Settle FX forward`` advice.

    Layout::

        <booking_date> * "<title>" "<narration>"
          Assets:<prefix>:<currency>             <amount> <ccy>
          Expenses:<prefix>:Fees:<ccy>           <abs_fees> <ccy> ; Forward spread
          Assets:<prefix>:<counter_currency>     <counter_amount> <counter_ccy> @@ <abs_gross> <ccy>
          no: <transaction_number>

    Both cash legs are signed as Pictet stored them: the fee-bearing leg
    on ``currency``/``amount`` may be either signed-positive (cash-in
    when buying the counter currency) or signed-negative (cash-out when
    selling it). The fee leg is always positive — beancount expense
    accounts are positive — and is set to ``abs(fees)`` since Pictet
    writes the spread as a negative number.

    The ``@@ <abs_gross> <ccy>`` annotation on the counter leg uses the
    pre-fee gross of the fee-bearing leg: ``gross = amount - fees`` in
    signed arithmetic, then ``abs(gross)``. That value reflects the
    cash exchange before the spread is taken out, which is what
    beancount needs to cross-reconcile the two cash currencies.

    Falls back to the internal-transfer renderer when the fee fields
    aren't populated — covers any future fee-less Settle FX forward
    variant Pictet might emit (none in the current fixture set).
    """

    if (
        tx.counter_currency is None
        or tx.counter_amount is None
        or tx.fees is None
        or tx.fees_currency is None
    ):
        return render_internal_transfer(tx, doc_type, prefix)

    lines: list[str] = [header_line(tx)]
    portfolio = portfolio_segment(tx.account_number)

    # Fee-bearing cash leg, signed as printed.
    lines.append(
        align(
            cash_account(prefix, tx.account_number, tx.currency),
            format_amount(tx.amount),
            tx.currency,
        )
    )

    # Forward-spread expense leg. Pictet writes the spread negative
    # (cash-out from the user's perspective); flip to positive for
    # the expense account.
    lines.append(
        align(
            f"Expenses:{prefix}:{portfolio}:Fees:{tx.fees_currency}",
            format_amount(abs(tx.fees)),
            tx.fees_currency,
            extras=" ; Forward spread",
        )
    )

    # Counter cash leg with ``@@ <abs_gross> <ccy>`` annotation.
    # ``gross = amount - fees`` in signed arithmetic — see the
    # docstring for the worked-through cases.
    gross = tx.amount - tx.fees
    lines.append(
        align(
            cash_account(prefix, tx.account_number, tx.counter_currency),
            format_amount(tx.counter_amount),
            tx.counter_currency,
            extras=f" @@ {format_amount(abs(gross))} {tx.currency}",
        )
    )

    trailer = transaction_number_comment(tx)
    if trailer:
        lines.append(trailer)

    return "\n".join(lines) + "\n"
