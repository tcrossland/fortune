"""Security distribution / dividend beancount builder.

Renders security-distribution advices that pay income on a held position.
The shape is a two-leg entry (income-recognition leg + cash leg) keyed on
the underlying ISIN. ``DIVIDEND_NOTICE`` is the canonical case; future
``CAPITAL_GAINS_DISTRIBUTION``-style doctypes would route here too.
"""

from __future__ import annotations

from banking_pipeline.models import DocumentType, Transaction
from banking_pipeline.writer.format import (
    align,
    cash_account,
    format_amount,
    header_line,
    portfolio_segment,
    transaction_number_comment,
)

DIVIDEND_TYPES: frozenset[DocumentType] = frozenset({
    DocumentType.DIVIDEND_NOTICE,
})


def render(tx: Transaction, doc_type: DocumentType, prefix: str) -> str:
    """Render a Pictet dividend / distribution advice.

    Layout::

        <booking_date> * "<title>" "<narration>"
          Income:<prefix>:<ISIN>:Dividend         -<amount> <ccy>
          Assets:<prefix>:<currency>               <amount> <ccy>
          no: <transaction_number>

    Pictet prints the ``Net amount`` positive (cash arriving in the
    client's account); the cash leg flows through unchanged. The income
    leg is signed-negative because beancount records income as a credit
    on the income-side accounts. The ``Income:<prefix>:<ISIN>:Dividend``
    naming keys per-ISIN — earlier the legacy template used
    ``Income:Dividends:<ISIN>`` which didn't carry the bank prefix and
    wouldn't compose with the per-bank account hierarchy the rest of
    the writer now emits.

    No inline ``open`` directive: dividends recur on the same position
    over a holder's lifetime, and emitting an open on every quarterly
    distribution would be noise. Manage the
    ``Income:<prefix>:<ISIN>:Dividend`` opens via
    :func:`banking_pipeline.writer.dispatch.render_open_directives`
    (which collects them across an extraction batch) or your existing
    ledger-level conventions.
    """

    isin = tx.isin or "Unknown"
    portfolio = portfolio_segment(tx.account_number)
    lines: list[str] = [header_line(tx)]

    # Income leg — signed-negative (beancount income-account convention).
    lines.append(
        align(
            f"Income:{prefix}:{portfolio}:{isin}:Dividend",
            format_amount(-tx.amount),
            tx.currency,
        )
    )

    # Cash leg — signed as Pictet printed it (positive, cash in).
    lines.append(
        align(
            cash_account(prefix, tx.account_number, tx.currency),
            format_amount(tx.amount),
            tx.currency,
        )
    )

    trailer = transaction_number_comment(tx)
    if trailer:
        lines.append(trailer)

    return "\n".join(lines) + "\n"
