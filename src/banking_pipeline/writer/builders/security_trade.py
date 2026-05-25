"""Security buy / sell beancount builder.

Renders the family of single-cash-effect security advices: fund
subscriptions and redemptions, structured-product buys/sells, ETF
buys/sells, and direct equity buys/sells. Bond trades route through
:mod:`banking_pipeline.writer.builders.bond_trade` instead because they
carry a separate accrued-interest leg.
"""

from __future__ import annotations

from banking_pipeline.models import DocumentType, Transaction
from banking_pipeline.writer.format import (
    align,
    cash_account,
    escape,
    fee_segment,
    format_amount,
    gbp_rate_metadata,
    header_line,
    inline_open_directive,
    portfolio_segment,
    transaction_number_comment,
)

# Security-trade doctypes — buys list the asset leg first, sells list the
# cash leg first (so the account that *receives* the value is always the
# first posting).
SECURITY_BUY_TYPES: frozenset[DocumentType] = frozenset({
    DocumentType.TRADE_CONFIRMATION,
    DocumentType.SUBSCRIPTION_NOTICE,
    DocumentType.BUY_BONDS,
    DocumentType.BUY_STRUCTURED_PRODUCTS,
    DocumentType.BUY_ETF,
    DocumentType.BUY_SHARES,
    DocumentType.COMPRA,
    DocumentType.SUSCRIPCION,
    DocumentType.SWITCH_ENTRADA,
    DocumentType.VANGUARD_CONTRACT_NOTE_BUY,
})

SECURITY_SELL_TYPES: frozenset[DocumentType] = frozenset({
    DocumentType.FINAL_REDEMPTION,
    DocumentType.REDEMPTION_NOTICE,
    DocumentType.REEMBOLSO,
    DocumentType.REEMBOLSO_FINAL,
    DocumentType.SELL_BONDS,
    DocumentType.SELL_ETF,
    DocumentType.SELL_STRUCTURED_PRODUCTS,
    DocumentType.SWITCH_SALIDA,
    DocumentType.VENTA,
    DocumentType.VANGUARD_CONTRACT_NOTE_SELL,
})

SECURITY_TRADE_TYPES = SECURITY_BUY_TYPES | SECURITY_SELL_TYPES


def render(tx: Transaction, doc_type: DocumentType, prefix: str) -> str:
    """Render a security buy/sell as a multi-posting beancount entry.

    Layout (FX trade, with all fields populated)::

        <booking_date> * "<title>" "<narration>"
          Assets:<prefix>:<portfolio>:<ISIN>          <qty> <ISIN> {<price> <sec_ccy>}
          Expenses:<prefix>:Fees:<sec_ccy>  <fees>      <sec_ccy>
          Assets:<prefix>:<currency>      <amount>     <ccy> @@ <subtotal> <sec_ccy>
          no: <transaction_number>

    Non-FX trades omit the ``Expenses:...:Fees`` leg (Pictet rolls fees
    into the cash net amount on those advices) and the ``@@`` annotation.
    Sells reverse the asset/cash posting order so the value-receiving
    account is always listed first.

    Sells with a multi-item fee breakdown (e.g. stock-exchange sales
    that itemise ``Corretaje y/o spread`` + ``Tasa bursátil``) are
    routed to :func:`_render_sell_with_breakdown`, which uses a
    different posting order, broken-out fee legs, and an inline
    income-account open. The branch is intentionally narrow so existing
    sell-path goldens (``reembolso_final`` etc.) keep matching.

    Sign conventions
    ----------------
    The cash leg's ``amount`` is emitted as the extractor stored it (Pictet
    prints negative for cash-out / positive for cash-in, which matches
    beancount's convention exactly). ``fees`` is flipped to its absolute
    value because Pictet prints fees as negative cash-out lines while
    beancount expense legs are positive. ``subtotal_security`` likewise
    uses ``abs()`` because the ``@@ <total> <ccy>`` form takes the absolute
    total cost in the price currency.
    """

    # Sells with a multi-item fee breakdown render in a different shape
    # — see :func:`_render_sell_with_breakdown` for why. Single-item
    # or empty breakdowns continue through the simpler path below so
    # existing goldens (``reembolso_final``, ``suscripcion.fx``) stay
    # byte-stable.
    if doc_type not in SECURITY_BUY_TYPES and len(tx.fee_breakdown) > 1:
        return _render_sell_with_breakdown(tx, doc_type, prefix)

    sec_ccy = tx.security_currency or tx.currency

    # --- Optional inline open directive --------------------------------
    # No-op today (``OPEN_EMITTING_TYPES`` is empty — account opens
    # are centralised in ``portfolio.beancount``); kept as a hook so
    # a standalone-file workflow can opt back in. See
    # :func:`inline_open_directive` for the gating rule.
    out = inline_open_directive(tx, doc_type, prefix)

    lines: list[str] = [header_line(tx)]

    # --- Asset leg ------------------------------------------------------
    # Buys carry a literal cost basis ``{<price> <sec_ccy>}`` — the new
    # units enter inventory at that price. Sells use the empty-cost
    # ``{}`` + ``@ <price> <sec_ccy>`` form: ``{}`` reduces the position
    # at its existing inventory cost basis (per the per-account booking
    # method), and ``@ <price>`` records the per-unit market price for
    # capital-gains computation. Setting a literal cost basis on a sell
    # would tell beancount to treat the sale as creating a new lot,
    # which is semantically wrong; the elastic ``Income:...Realized``
    # leg below absorbs the gain/loss the empty-cost form produces.
    isin = tx.isin or "Unknown"
    qty_str = format_amount(tx.quantity) if tx.quantity is not None else "0"
    if tx.price is not None:
        if doc_type in SECURITY_BUY_TYPES:
            cost_basis = f" {{{format_amount(tx.price)} {sec_ccy}}}"
        else:
            cost_basis = f" {{}} @ {format_amount(tx.price)} {sec_ccy}"
    else:
        cost_basis = ""
    portfolio = portfolio_segment(tx.account_number)
    asset_line = align(
        f"Assets:{prefix}:{portfolio}:{isin}",
        qty_str,
        isin,
        extras=cost_basis,
    )
    # Trade-date GBP rate, attached to the security posting as metadata
    # (``None`` when the rate is absent or the trade is already GBP).
    gbp_meta = gbp_rate_metadata(tx)

    # --- Fees leg(s) ---------------------------------------------------
    # Emitted whenever the document carries non-zero fees, regardless of
    # FX status. Non-FX advices with ``Costs <ccy> 0.00`` (e.g.
    # ``compra.2022``) skip via the ``fees != 0`` guard; non-FX advices
    # with non-zero fees (e.g. ``buy_shares`` with its commission line)
    # need this leg for the entry to balance arithmetically — the cash
    # leg is gross + fees and the asset leg is gross-only.
    #
    # Multi-item fee breakdowns (e.g. an FX buy that splits ``Forex
    # spread`` from ``Commission/Fee``) emit one expense leg per item
    # with the item's description as an inline beancount comment so
    # the audit detail Pictet printed survives in the rendered entry.
    # Sells with multi-item breakdowns get this same treatment via the
    # dedicated :func:`_render_sell_with_breakdown` builder above
    # (which also reorders postings so the cash leg lands first); buys
    # stay in this builder because their asset-first order is shared
    # with the single-fee path.
    fees_lines: list[str] = []
    if len(tx.fee_breakdown) > 1:
        for item in tx.fee_breakdown:
            fees_lines.append(
                align(
                    f"Expenses:{prefix}:{portfolio}:{fee_segment(item.description)}:{item.currency}",
                    format_amount(abs(item.amount)),
                    item.currency,
                    extras=f" ; {item.description}",
                )
            )
    elif tx.fees is not None and tx.fees != 0:
        fees_ccy = tx.fees_currency or sec_ccy
        # Single in-block ``Costes`` aggregate has no per-line
        # description to categorise on, so it lands in the catch-all
        # ``Fees:<ccy>`` bucket. Per-line breakdowns above route to
        # the appropriate category (Spread / Brokerage / Tax /
        # Management) via :func:`fee_segment`.
        fees_lines.append(
            align(
                f"Expenses:{prefix}:{portfolio}:Fees:{fees_ccy}",
                format_amount(abs(tx.fees)),
                fees_ccy,
            )
        )

    # --- Cash leg -------------------------------------------------------
    cash_extras = ""
    if tx.is_fx and tx.subtotal_security is not None:
        cash_extras = (
            f" @@ {format_amount(abs(tx.subtotal_security))} {sec_ccy}"
        )
    cash_line = align(
        cash_account(prefix, tx.account_number, tx.currency),
        format_amount(tx.amount),
        tx.currency,
        extras=cash_extras,
    )

    # --- Posting order: asset-first for buys, cash-first for sells -----
    if doc_type in SECURITY_BUY_TYPES:
        lines.append(asset_line)
        if gbp_meta:
            lines.append(gbp_meta)
        lines.extend(fees_lines)
        lines.append(cash_line)
    else:
        lines.append(cash_line)
        lines.extend(fees_lines)
        lines.append(asset_line)
        if gbp_meta:
            lines.append(gbp_meta)
        # Elastic ``Income:<prefix>:<ISIN>:Realized`` posting on every
        # sell — beancount auto-balances it against the difference
        # between the cost basis pulled from inventory (via ``{}``) and
        # the cash proceeds, so the leg ends up carrying the realised
        # gain/loss for these units. Skipped when the ISIN is unknown
        # (the leg's account name would degrade to ``...:Unknown:Realized``,
        # which is uglier than just leaving the entry to balance via
        # whichever leg picks up the slack).
        if tx.isin:
            lines.append(f"  Income:{prefix}:{portfolio}:{tx.isin}:Realized")

    # --- Trailing reference comment ------------------------------------
    trailer = transaction_number_comment(tx)
    if trailer:
        lines.append(trailer)

    return out + "\n".join(lines) + "\n"


def _render_sell_with_breakdown(
    tx: Transaction, doc_type: DocumentType, prefix: str
) -> str:
    """Render a security sell that carries a multi-item fee breakdown.

    Used for stock-exchange sales where Pictet prints a per-line ``Costes``
    block (``Corretaje y/o spread`` + ``Tasa bursátil`` etc.). The shape
    differs from the simpler sell path on two points:

      - Asset leg first (mirroring the switch_salida convention),
        followed by the fee legs, the FX-aware cash leg, and the
        elastic income leg.
      - One ``Expenses:<prefix>:Fees:<ccy>`` posting per breakdown item
        with the item's description as an inline ``; <description>``
        comment — preserves the audit detail Pictet prints rather than
        collapsing to a single aggregate fees leg.

    Income leg uses the same ``Income:<prefix>:<portfolio>:<ISIN>:Realized``
    shape as :func:`render` for unbroken consistency across all sell
    paths. Earlier this builder emitted a bare-ISIN
    ``Income:<prefix>:<portfolio>:<ISIN>`` form, a vestige of when the
    builder also emitted an inline ``open Income:…`` directive (now
    suppressed — opens are centralised in ``portfolio.beancount``).

    The simple :func:`render` sell path stays in place for sells without
    a breakdown (e.g. ``reembolso_final`` with its ``Costes EUR 0.00``
    non-FX layout); switching shapes based on breakdown presence avoids
    disturbing those goldens.

    No inline ``open`` directive is emitted: account opens are
    centralised in ``portfolio.beancount``.
    """

    sec_ccy = tx.security_currency or tx.currency
    isin = tx.isin or "Unknown"
    entry_date = tx.booking_date or tx.trade_date

    # Header (two-string narration when title is set).
    narration = escape(tx.narration)
    if tx.title:
        header = f'{entry_date} * "{escape(tx.title)}" "{narration}"'
    else:
        header = f'{entry_date} * "{narration}"'
    lines: list[str] = [header]

    # Asset leg — sell-from-inventory with empty cost-braces and
    # ``@ <price>`` market-price annotation.
    qty_str = format_amount(tx.quantity) if tx.quantity is not None else "0"
    cost_basis = (
        f" {{}} @ {format_amount(tx.price)} {sec_ccy}"
        if tx.price is not None
        else ""
    )
    portfolio = portfolio_segment(tx.account_number)
    lines.append(
        align(
            f"Assets:{prefix}:{portfolio}:{isin}",
            qty_str,
            isin,
            extras=cost_basis,
        )
    )
    gbp_meta = gbp_rate_metadata(tx)
    if gbp_meta:
        lines.append(gbp_meta)

    # Per-item expense legs. Each fee item becomes its own posting
    # with the item's description as an inline beancount comment AND
    # a category-specific account segment via :func:`fee_segment`
    # (e.g. ``Corretaje y/o spread`` → ``Spread:<ccy>``, ``Tasa
    # bursátil`` → ``Tax:<ccy>``) so cost analysis splits cleanly by
    # category rather than collapsing every component into ``Fees``.
    for item in tx.fee_breakdown:
        lines.append(
            align(
                f"Expenses:{prefix}:{portfolio}:{fee_segment(item.description)}:{item.currency}",
                format_amount(abs(item.amount)),
                item.currency,
                extras=f" ; {item.description}",
            )
        )

    # Cash leg — FX-aware ``@@ <subtotal> <sec_ccy>`` annotation when
    # the security and cash-account currencies differ.
    cash_extras = ""
    if tx.is_fx and tx.subtotal_security is not None:
        cash_extras = (
            f" @@ {format_amount(abs(tx.subtotal_security))} {sec_ccy}"
        )
    lines.append(
        align(
            cash_account(prefix, tx.account_number, tx.currency),
            format_amount(tx.amount),
            tx.currency,
            extras=cash_extras,
        )
    )

    # Elastic ``Income:<prefix>:<portfolio>:<ISIN>:Realized`` posting —
    # beancount auto-balances against the cost basis pulled from
    # inventory and the cash proceeds.
    if tx.isin:
        lines.append(f"  Income:{prefix}:{portfolio}:{isin}:Realized")

    # Trailing reference comment.
    trailer = transaction_number_comment(tx)
    if trailer:
        lines.append(trailer)

    return "\n".join(lines) + "\n"
