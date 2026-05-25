"""Vanguard UK ISA cash-event builders.

Two render shapes that don't fit the Pictet builders:

  - **Regular-statement cash events** (``vanguard_regular_statement``):
    the cash ``Deposit for Investment Purchases`` contributions and the
    monthly ``Cash Account Interest`` credits. Both are single cash-leg
    events with a self-contained counter-leg — contributions against
    ``Equity:<prefix>:Contributions`` (money entering the wrapper from
    outside), interest against ``Income:<prefix>:<portfolio>:Interest``.
    The two are told apart by the narration the template sets.

  - **Account fee** (``vanguard_direct_debit_details``): the quarterly
    platform fee, collected by direct debit from the user's external
    bank. It never touches the ISA cash (the statement shows it charged
    then cleared, netting to zero), so it's booked as
    ``Expenses:<prefix>:Fees`` against the same contributions-equity
    account rather than against the ISA cash leg.

``<prefix>`` is ``Vgd:ISA`` (see
:data:`banking_pipeline.writer.profile.VANGUARD_PROFILE`), so every
account lands under the dedicated ``…:Vgd:ISA:…`` subtree.
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

VANGUARD_STATEMENT_TYPES: frozenset[DocumentType] = frozenset({
    DocumentType.VANGUARD_REGULAR_STATEMENT,
})

VANGUARD_FEE_TYPES: frozenset[DocumentType] = frozenset({
    DocumentType.VANGUARD_DIRECT_DEBIT_DETAILS,
})

# Equity account (under the bank prefix) that funds external cash flows
# into the ISA — contributions in, the account fee out. Keeps the ISA
# ledger self-contained: no untracked external bank account is needed.
_CONTRIBUTIONS_SEGMENT = "Contributions"


def render_statement(tx: Transaction, doc_type: DocumentType, prefix: str) -> str:
    """Render a regular-statement cash event (contribution or interest).

    Layout (interest — cash credited to the ISA)::

        <date> * "Regular statement" "Cash Account Interest"
          Assets:<prefix>:<portfolio>:<ccy>        0.19 <ccy>
          Income:<prefix>:<portfolio>:Interest:<ccy>  -0.19 <ccy>

    Layout (contribution — cash arriving from outside the wrapper)::

        <date> * "Regular statement" "Deposit for Investment Purchases"
          Assets:<prefix>:<portfolio>:<ccy>      1000.00 <ccy>
          Equity:<prefix>:Contributions         -1000.00 <ccy>

    The two are distinguished by the narration the regular-statement
    template assigns (the only two row kinds it emits).
    """

    portfolio = portfolio_segment(tx.account_number)
    cash = cash_account(prefix, tx.account_number, tx.currency)

    lines: list[str] = [header_line(tx)]
    # Cash leg first (signed as the statement printed it), then the
    # self-balancing counter leg.
    lines.append(align(cash, format_amount(tx.amount), tx.currency))
    if "Interest" in tx.narration:
        lines.append(
            align(
                f"Income:{prefix}:{portfolio}:Interest:{tx.currency}",
                format_amount(-tx.amount),
                tx.currency,
            )
        )
    else:
        lines.append(
            align(
                f"Equity:{prefix}:{_CONTRIBUTIONS_SEGMENT}",
                format_amount(-tx.amount),
                tx.currency,
            )
        )

    trailer = transaction_number_comment(tx)
    if trailer:
        lines.append(trailer)
    return "\n".join(lines) + "\n"


def render_account_fee(tx: Transaction, doc_type: DocumentType, prefix: str) -> str:
    """Render the quarterly platform account fee.

    Layout::

        <date> * "Account fee" "Platform account fee"
          Expenses:<prefix>:Fees:<ccy>     10.11 <ccy>
          Equity:<prefix>:Contributions   -10.11 <ccy>

    The fee is collected externally, so neither leg touches the ISA cash
    account — the expense is funded from the contributions-equity
    account, keeping the ISA cash balance untouched.
    """

    fee = abs(tx.amount)
    lines: list[str] = [
        header_line(tx),
        align(
            f"Expenses:{prefix}:Fees:{tx.currency}",
            format_amount(fee),
            tx.currency,
        ),
        align(
            f"Equity:{prefix}:{_CONTRIBUTIONS_SEGMENT}",
            format_amount(-fee),
            tx.currency,
        ),
    ]
    trailer = transaction_number_comment(tx)
    if trailer:
        lines.append(trailer)
    return "\n".join(lines) + "\n"
