"""Bond purchase / sale beancount builder.

Distinct from :mod:`banking_pipeline.writer.builders.security_trade`
because Pictet's bond advices carry a dedicated accrued-interest line
in the ``CASH EFFECT`` block — accrued interest the buyer pays to the
seller for the period since the last coupon. We surface that as a
separate ``Income:<prefix>:<isin>:Interest`` leg so the bond's running
yield stays distinct from realised capital gain/loss on the principal.
"""

from __future__ import annotations

from banking_pipeline.models import DocumentType, Transaction
from banking_pipeline.writer.builders.security_trade import SECURITY_BUY_TYPES
from banking_pipeline.writer.format import (
    align,
    cash_account,
    escape,
    format_amount,
    inline_open_directive,
    portfolio_segment,
    transaction_number_comment,
)

# Doctypes routed through this builder. Used for both directions: on a
# buy the buyer pays accrued interest to the seller (income debited);
# on a sell the buyer's accrued payment hits the seller's cash (income
# credited). The renderer branches on :data:`SECURITY_BUY_TYPES` to
# pick the asset-leg cost form and the leg ordering.
BOND_TRADE_TYPES: frozenset[DocumentType] = frozenset({
    DocumentType.BUY_BONDS,
    DocumentType.SELL_BONDS,
})


def render(tx: Transaction, doc_type: DocumentType, prefix: str) -> str:
    """Render a Pictet bond purchase or sale as a beancount entry.

    Sell layout (``SELL_BONDS``)::

        <booking_date> * "Sell bonds" "<narration>"
          Assets:<prefix>:<portfolio>:<currency>     <net>     <ccy>  ; Net amount
          Expenses:<prefix>:Fees:<ccy>               <fees>    <ccy>  ; Commission/Fee
          Income:<prefix>:<ISIN>:Interest            <-int>    <ccy>  ; Accrued interest
          Assets:<prefix>:<portfolio>:<ISIN>         <-nominal> <ISIN> {} @ <unit_px> <ccy>
          Income:<prefix>:<ISIN>:Realized
          no: <transaction_number>

    Buy layout (``BUY_BONDS``)::

        <booking_date> * "Buy bonds" "<narration>"
          Assets:<prefix>:<portfolio>:<ISIN>         <+nominal> <ISIN> {<unit_px> <ccy>}
          Expenses:<prefix>:Fees:<ccy>               <fees>    <ccy>  ; Brokerage
          Income:<prefix>:<ISIN>:Interest            <+int>    <ccy>  ; Accrued interest
          Assets:<prefix>:<portfolio>:<currency>     <net>     <ccy>  ; Net amount
          no: <transaction_number>

    Four legs always (five for sells, counting the elastic
    ``:Realized``) that balance to zero:

      - Cash leg — signed-as-printed Net amount. Sells are positive
        (proceeds in); buys are negative (cash out). Posted to
        ``Assets:<prefix>:<portfolio>:<currency>``.
      - Fee leg ``+abs(fees)`` — Pictet's brokerage / commission,
        posted positive on ``Expenses:<prefix>:Fees:<ccy>`` regardless
        of direction. The inline comment branches on direction
        because the per-line cost label differs (``Brokerage`` on
        buys, ``Commission/Fee`` on sells).
      - Interest leg ``-accrued_interest`` — accrued interest paid
        alongside the principal, recognised on
        ``Income:<prefix>:<ISIN>:Interest``. Pictet prints the
        accrued amount with the cash sign (positive on sells, the
        buyer paid the seller; negative on buys, the buyer's cash
        out). Negating it gives the income-account sign: credit on
        sells (income recognised), debit on buys (income reversed
        because we paid for accrued interest belonging to the prior
        holder).
      - Asset leg — face-value units. Sells use ``{} @ <unit_px>`` so
        the lot leaves inventory at its cost basis and beancount
        attributes the cost-vs-proceeds difference to the elastic
        ``:Realized`` leg below. Buys use ``{<unit_px> <ccy>}`` to
        record the cost basis at acquisition; the realised-gain
        leg is omitted (buys don't realise anything).
      - Elastic ``Income:<prefix>:<ISIN>:Realized`` leg (sells only) —
        beancount fills in the diff between cost basis and
        ``nominal × unit_price``.

    Posting order is determined by direction: buys list the asset leg
    first (the account receiving value), sells list the cash leg
    first (same convention as
    :mod:`banking_pipeline.writer.builders.security_trade`).
    """

    isin = tx.isin or "Unknown"
    portfolio = portfolio_segment(tx.account_number)
    entry_date = tx.booking_date or tx.trade_date
    is_buy = doc_type in SECURITY_BUY_TYPES

    # No-op today (``OPEN_EMITTING_TYPES`` is empty — account opens
    # are centralised in ``portfolio.beancount``); kept as a hook so
    # a standalone-file workflow can opt back in.
    out = inline_open_directive(tx, doc_type, prefix)

    parts: list[str] = [str(entry_date), "*"]
    if tx.title:
        parts.append(f'"{escape(tx.title)}"')
    parts.append(f'"{escape(tx.narration)}"')
    lines: list[str] = [" ".join(parts)]

    # Asset leg. Buys carry a literal cost-basis brace
    # ``{<unit_px> <ccy>}``; sells use the empty-cost
    # ``{} @ <unit_px> <ccy>`` form so beancount reduces the position
    # at its existing inventory cost basis and the ``@`` records the
    # market price for capital-gains attribution.
    qty_str = format_amount(tx.quantity) if tx.quantity is not None else "0"
    if tx.price is not None:
        if is_buy:
            cost_basis = f" {{{format_amount(tx.price)} {tx.currency}}}"
        else:
            cost_basis = f" {{}} @ {format_amount(tx.price)} {tx.currency}"
    else:
        cost_basis = ""
    asset_line = align(
        f"Assets:{prefix}:{portfolio}:{isin}",
        qty_str,
        isin,
        extras=cost_basis,
    )

    # Fee leg — Pictet prints negative inside the CASH EFFECT block on
    # both directions; expense accounts hold positive amounts.
    fee_line: str | None = None
    if tx.fees is not None and tx.fees != 0:
        fees_ccy = tx.fees_currency or tx.currency
        fee_label = " ; Brokerage" if is_buy else " ; Commission/Fee"
        fee_line = align(
            f"Expenses:{prefix}:{portfolio}:Fees:{fees_ccy}",
            format_amount(abs(tx.fees)),
            fees_ccy,
            extras=fee_label,
        )

    # Interest leg — direction-agnostic; ``-accrued_interest`` flips
    # Pictet's cash-sign printing into the income-account sign.
    interest_line: str | None = None
    if tx.accrued_interest is not None and tx.accrued_interest != 0:
        interest_line = align(
            f"Income:{prefix}:{portfolio}:{isin}:Interest",
            format_amount(-tx.accrued_interest),
            tx.currency,
            extras=" ; Accrued interest",
        )

    # Cash leg — signed as Pictet printed Net amount: positive on
    # sells, negative on buys.
    cash_line = align(
        cash_account(prefix, tx.account_number, tx.currency),
        format_amount(tx.amount),
        tx.currency,
        extras=" ; Net amount",
    )

    if is_buy:
        # Asset-first ordering: the account receiving value leads.
        lines.append(asset_line)
        if fee_line is not None:
            lines.append(fee_line)
        if interest_line is not None:
            lines.append(interest_line)
        lines.append(cash_line)
    else:
        # Cash-first ordering, with the elastic realised-gain leg at
        # the end so beancount auto-balances against the cost-basis
        # diff pulled from inventory.
        lines.append(cash_line)
        if fee_line is not None:
            lines.append(fee_line)
        if interest_line is not None:
            lines.append(interest_line)
        lines.append(asset_line)
        if tx.isin:
            lines.append(f"  Income:{prefix}:{portfolio}:{isin}:Realized")

    trailer = transaction_number_comment(tx)
    if trailer:
        lines.append(trailer)

    return out + "\n".join(lines) + "\n"
