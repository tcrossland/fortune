"""Multi-component fee-advice beancount builder.

Both the ES ``Débito de gastos`` and EN ``Debit of fees`` advices have
bank-prefixed multi-leg goldens; ``find_fee_breakdown`` handles their
single-line and multi-line label layouts respectively. ``FACTURA`` is
intentionally excluded — that doctype's template returns ``[]`` to
avoid double-counting against the matching ``Débito de gastos`` advice
(same economic event, two paper trails).
"""

from __future__ import annotations

from banking_pipeline.models import DocumentType, Transaction
from banking_pipeline.writer.format import (
    align,
    cash_account,
    fee_segment,
    format_amount,
    header_line,
    portfolio_segment,
    transaction_number_comment,
)

FEE_ADVICE_TYPES: frozenset[DocumentType] = frozenset({
    DocumentType.DEBIT_OF_FEES,
    DocumentType.DEBITO_DE_GASTOS,
})


def render(tx: Transaction, doc_type: DocumentType, prefix: str) -> str:
    """Render a multi-component fee advice as a beancount entry.

    Layout::

        <booking_date> * "<title>" "<narration>"
          [Expenses:<prefix>:Fees:<ccy>  <abs_amount> <ccy> ; <description>]
          ... (one per fee_breakdown item)
          Assets:<prefix>:<currency>     <signed_amount> <ccy>
          no: <transaction_number>

    When ``fee_breakdown`` is empty the function falls back to a single
    aggregate expense leg using ``abs(tx.amount)`` so advices that don't
    carry a per-line breakdown (or where the breakdown helper hasn't
    been extended to parse them yet) still render correctly.

    Sign conventions match the rest of the writer: the cash leg's
    ``amount`` flows through unchanged (Pictet prints negative for
    cost-out, which matches beancount), and each fee item's ``amount``
    is run through ``abs()`` because beancount expense legs are positive.
    """

    lines: list[str] = [header_line(tx)]

    portfolio = portfolio_segment(tx.account_number)

    # --- Expense legs ---------------------------------------------------
    # Per-item legs route to a category account picked by
    # :func:`fee_segment` (e.g. ``Honorarios de gestión`` →
    # ``Management:<ccy>``, ``IVA extranjero`` → ``Tax:<ccy>``) so a
    # year-by-year breakdown of management fees vs taxes is a single
    # account-prefix match. Pictet descriptions outside the curated
    # map fall back to ``Fees:<ccy>`` (the catch-all). Bare aggregate
    # advices that don't carry a per-line breakdown still post to
    # ``Fees:<ccy>`` because there's no per-line description to
    # categorise on.
    if tx.fee_breakdown:
        for item in tx.fee_breakdown:
            lines.append(
                align(
                    f"Expenses:{prefix}:{portfolio}:{fee_segment(item.description)}:{item.currency}",
                    format_amount(abs(item.amount)),
                    item.currency,
                    extras=f" ; {item.description}",
                )
            )
    else:
        # No per-line breakdown — fall back to a single aggregate leg.
        lines.append(
            align(
                f"Expenses:{prefix}:{portfolio}:Fees:{tx.currency}",
                format_amount(abs(tx.amount)),
                tx.currency,
            )
        )

    # --- Cash leg -------------------------------------------------------
    lines.append(
        align(
            cash_account(prefix, tx.account_number, tx.currency),
            format_amount(tx.amount),
            tx.currency,
        )
    )

    # --- Trailing reference comment ------------------------------------
    trailer = transaction_number_comment(tx)
    if trailer:
        lines.append(trailer)

    return "\n".join(lines) + "\n"
