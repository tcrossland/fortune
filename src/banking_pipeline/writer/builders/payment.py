"""Third-party / self-to-self payment beancount builder.

Renders four shapes, picked on what the extractor populated:

  - ``counter_account`` + ``gross_amount`` → outgoing self-to-self
    three-leg form (PAYMENT → Revolut etc.).
  - ``counter_account`` + positive ``amount`` → incoming
    self-to-self two-leg form (PAGO_INTERNA: Revolut → Pictet).
  - no ``counter_account``, positive ``amount`` → genuine
    third-party incoming, elastic ``Income:<prefix>:<portfolio>:Other``.
  - no ``counter_account``, negative ``amount`` → genuine
    third-party outgoing, elastic ``Expenses:<prefix>:<portfolio>:Other``.
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

THIRD_PARTY_PAYMENT_TYPES: frozenset[DocumentType] = frozenset({
    DocumentType.INCOMING_PAYMENT,
    DocumentType.PAGO,
    DocumentType.PAGO_ENTRANTE,
    DocumentType.PAGO_INTERNA,
    DocumentType.PAYMENT,
})


def render(tx: Transaction, doc_type: DocumentType, prefix: str) -> str:
    """Render a third-party / self-to-self payment advice as a beancount entry.

    Four render shapes, keyed on what the extractor populated:

    **Outgoing self-to-self payment** (``tx.counter_account`` set,
    ``tx.gross_amount`` set, ``tx.amount < 0`` — user wired to one of
    their own external accounts, e.g. Pictet → Revolut)::

        <booking_date> * "<title>" "<narration>"
          Assets:<counter_account>:<currency>     <gross_amount> <ccy> ; Gross amount
          Assets:<prefix>:<portfolio>:<currency>  <amount>      <ccy> ; Net amount
          Expenses:<prefix>:<portfolio>:Fees:<ccy>  <abs_fees>  <ccy> ; Payment fees
          no: <transaction_number>

    Three legs that balance arithmetically: the user receives
    ``gross_amount`` in their external account, the Pictet portfolio's
    cash account decreases by ``amount`` (which is gross + fees, signed
    negative), and the wire fee posts to
    ``Expenses:<prefix>:<portfolio>:Fees:<ccy>``.

    **Incoming self-to-self payment** (``tx.counter_account`` set,
    ``tx.amount > 0`` — user-owned external account credited the
    Pictet portfolio, e.g. Revolut → Pictet)::

        <booking_date> * "<title>" "<narration>"
          Assets:<prefix>:<portfolio>:<currency>  <amount>  <ccy>
          Assets:<counter_account>:<currency>     -<amount> <ccy>
          no: <transaction_number>

    Two legs that balance: Pictet portfolio credited with the cash
    in, the source external account debited with the same amount.
    No fee leg in the typical case (Pictet's incoming Pago Interna
    advice doesn't carry a Pictet-side fee).

    **Incoming third-party payment** (no ``counter_account``,
    ``tx.amount > 0`` — external counterparty paid the user)::

        <booking_date> * "<title>" "<narration>"
          Assets:<prefix>:<portfolio>:<currency>  <amount> <ccy>
          Income:<prefix>:<portfolio>:Other
          no: <transaction_number>

    **Outgoing third-party payment** (no ``counter_account``,
    ``tx.amount < 0`` — user paid an external counterparty)::

        <booking_date> * "<title>" "<narration>"
          Assets:<prefix>:<portfolio>:<currency>  <amount> <ccy>
          Expenses:<prefix>:<portfolio>:Other
          no: <transaction_number>

    The elastic counter-leg ``Income:<prefix>:<portfolio>:Other`` /
    ``Expenses:<prefix>:<portfolio>:Other`` carries no amount;
    beancount auto-balances against the cash leg. ``Other`` is a
    placeholder the user can rewire to payer/payee-specific accounts.
    """

    lines: list[str] = [header_line(tx)]
    portfolio = portfolio_segment(tx.account_number)
    trailer = transaction_number_comment(tx)

    # --- Outgoing self-to-self three-leg shape -------------------------
    if tx.counter_account is not None and tx.gross_amount is not None:
        # Destination leg — user's external account credited with the
        # principal sent. Positive amount, no portfolio (the external
        # bank's account naming is its own concern).
        lines.append(
            align(
                f"Assets:{tx.counter_account}:{tx.currency}",
                format_amount(tx.gross_amount),
                tx.currency,
                extras=" ; Gross amount",
            )
        )
        # Source leg — Pictet portfolio cash account debited with the
        # net (gross + fees, signed negative).
        lines.append(
            align(
                cash_account(prefix, tx.account_number, tx.currency),
                format_amount(tx.amount),
                tx.currency,
                extras=" ; Net amount",
            )
        )
        # Wire fee leg — Pictet's payment-fee charge as an expense.
        if tx.fees is not None and tx.fees != 0:
            fees_ccy = tx.fees_currency or tx.currency
            lines.append(
                align(
                    f"Expenses:{prefix}:{portfolio}:Fees:{fees_ccy}",
                    format_amount(abs(tx.fees)),
                    fees_ccy,
                    extras=" ; Payment fees",
                )
            )
        if trailer:
            lines.append(trailer)
        return "\n".join(lines) + "\n"

    # --- Incoming self-to-self two-leg shape ---------------------------
    # Source-bank resolution succeeded (counter_account set) and the
    # cash leg is positive (Pictet receiving the wire). Mirror of the
    # outgoing three-leg shape with the legs sign-flipped and no fee
    # leg — incoming Pago Interna advices don't carry a Pictet-side
    # fee.
    if tx.counter_account is not None and tx.amount >= 0:
        # Destination leg — Pictet portfolio credited with the cash
        # in, signed as Pictet printed it.
        lines.append(
            align(
                cash_account(prefix, tx.account_number, tx.currency),
                format_amount(tx.amount),
                tx.currency,
            )
        )
        # Source leg — user's external account debited with the same
        # amount, sign-flipped to balance.
        lines.append(
            align(
                f"Assets:{tx.counter_account}:{tx.currency}",
                format_amount(-tx.amount),
                tx.currency,
            )
        )
        if trailer:
            lines.append(trailer)
        return "\n".join(lines) + "\n"

    # --- Two-leg-elastic shape (genuine third-party in either direction) -
    lines.append(
        align(
            cash_account(prefix, tx.account_number, tx.currency),
            format_amount(tx.amount),
            tx.currency,
        )
    )
    # Elastic counter-leg, keyed on direction. When the extractor
    # resolved the counterparty name via
    # ``settings.counterparty_account_map`` (e.g. ``Beneficiary``
    # ``ACME EMPLOYER`` → ``External:Earnout:Acme``), use the mapped
    # segment in place of the catch-all ``:Other`` placeholder. The
    # ``Income:`` / ``Expenses:`` family is picked from the cash-leg
    # sign, so a single map entry covers a counterparty that flows in
    # either direction.
    if tx.counterparty_account is not None:
        counter_segment = tx.counterparty_account
    else:
        counter_segment = f"{prefix}:{portfolio}:Other"
    if tx.amount >= 0:
        lines.append(f"  Income:{counter_segment}")
    else:
        lines.append(f"  Expenses:{counter_segment}")

    if trailer:
        lines.append(trailer)

    return "\n".join(lines) + "\n"
