"""Quarterly current-account interest-payment beancount builder.

The shape is a two-leg entry: the cash leg flows as Pictet printed
(negative on debit-balance interest charged to the user, positive on
credit-balance interest paid to the user) and the counter-leg switches
account family based on direction —
``Expenses:<prefix>:Interest:<ccy>`` for charges,
``Income:<prefix>:Interest:<ccy>`` for earnings. ``INTEREST_SCALE`` is
intentionally absent: the scale document is the per-day rate ledger
that produced the same cash event the payment advice already books, and
emitting both would double-count.
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

INTEREST_TYPES: frozenset[DocumentType] = frozenset({
    DocumentType.INTEREST_PAYMENT,
})


def render(tx: Transaction, doc_type: DocumentType, prefix: str) -> str:
    """Render a Pictet quarterly current-account interest payment.

    Layout (debit-balance interest — user is charged for an overdraft)::

        <booking_date> * "<title>" "<narration>"
          Expenses:<prefix>:Interest:<ccy>        <abs_amount> <ccy>
          Assets:<prefix>:<currency>              <amount> <ccy>
          no: <transaction_number>

    Layout (credit-balance interest — Pictet pays interest on the
    cash balance)::

        <booking_date> * "<title>" "<narration>"
          Income:<prefix>:Interest:<ccy>          -<amount> <ccy>
          Assets:<prefix>:<currency>              <amount> <ccy>
          no: <transaction_number>

    The counter-leg account-family switch is keyed on the cash leg's
    sign: when ``tx.amount`` is negative (Pictet's convention for cash
    out — the user is paying interest on their overdraft) the entry
    posts to ``Expenses:...:Interest:<ccy>``; when positive (cash in,
    Pictet paid the user interest on a credit balance) it posts to
    ``Income:...:Interest:<ccy>``. Beancount's sign convention then
    flips: expenses are positive, income is negative.

    Currency-suffixed account names (``Interest:GBP``, ``Interest:EUR``)
    let the user track interest separately per current account currency
    without an extra hierarchy level — same convention the writer
    already uses for ``Expenses:<prefix>:Fees:<ccy>``.
    """

    lines: list[str] = [header_line(tx)]
    portfolio = portfolio_segment(tx.account_number)

    # Counter-leg: Expenses for negative cash (interest charged),
    # Income for positive cash (interest earned).
    if tx.amount < 0:
        lines.append(
            align(
                f"Expenses:{prefix}:{portfolio}:Interest:{tx.currency}",
                format_amount(abs(tx.amount)),
                tx.currency,
            )
        )
    else:
        lines.append(
            align(
                f"Income:{prefix}:{portfolio}:Interest:{tx.currency}",
                format_amount(-tx.amount),
                tx.currency,
            )
        )

    # Cash leg — signed as Pictet printed it.
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
