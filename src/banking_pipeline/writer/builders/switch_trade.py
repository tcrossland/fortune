"""Pictet fund-switch beancount builder.

Switches are *also* members of the buy/sell sets in
:mod:`banking_pipeline.writer.builders.security_trade` (entrada is a
buy, salida is a sell at the security-leg level), so
``render_open_directives`` still finds their ISINs and emits the right
``Assets:<prefix>:<portfolio>:<ISIN>`` opens. The dispatcher checks
:data:`SWITCH_TYPES` first so switch advices route here rather than to
the regular trade builder — the cash-leg shape, the ``{} @`` cost form,
the Switch holding account, the ``^<txn>`` link, and the elastic
Unrealized leg are all distinctive enough to warrant their own builder.
"""

from __future__ import annotations

from banking_pipeline.models import DocumentType, Transaction
from banking_pipeline.writer.format import (
    align,
    escape,
    fee_segment,
    format_amount,
    inline_open_directive,
    portfolio_segment,
    transaction_number_comment,
)

SWITCH_TYPES: frozenset[DocumentType] = frozenset({
    DocumentType.SWITCH_SALIDA,
    DocumentType.SWITCH_ENTRADA,
})


def render(tx: Transaction, doc_type: DocumentType, prefix: str) -> str:
    """Render a Pictet switch leg as a beancount entry.

    Switch advices have **no external cash effect**: the proceeds
    (salida) or cost (entrada) land in an intermediate
    ``Assets:<prefix>:Switch:<ccy>`` holding account that the paired
    leg later debits or credits, so the pair nets to zero across the
    two-document switch. Pictet may still levy a small spread / fee
    on the trade, charged against the holding-account proceeds; when
    present it surfaces as a separate ``Expenses:<prefix>:<portfolio>:Fees:<ccy>``
    leg between the asset and switch-holding postings.

    Salida (sale) layout::

        <booking_date> * "<title>" "<narration>" ^<txn_no>
          Assets:<prefix>:<portfolio>:<ISIN>          <quantity> <ISIN> {} @ <price> <ccy>
          [Expenses:<prefix>:<portfolio>:Fees:<ccy>   <abs_fees> <ccy>]
          Assets:<prefix>:Switch:<ccy>    <amount>   <ccy>
          Income:<prefix>:<ISIN>:Unrealized
          no: <txn_no>

    The empty ``{}`` cost-braces tell beancount to reduce the position
    at its existing inventory cost basis (FIFO/etc., per the per-account
    booking method). The single-``@`` form records the per-unit market
    price for capital-gains computation, distinct from the ``@@`` total
    form the FX cash leg uses. The ``Income:...:Unrealized`` posting
    has no amount: it's an *elastic* leg, and beancount fills in the
    balance — which equals the realised gain/loss on the units. The
    user labels it ``Unrealized`` because economically a switch rotates
    the position into a different fund rather than truly liquidating it.

    Entrada (buy) layout omits the Unrealized leg and uses the standard
    ``{<price> <ccy>}`` cost-basis braces — new units enter the
    inventory at the purchase price.

    Fees leg
    --------
    Emitted whenever the document carries a non-zero ``Costes`` line,
    regardless of FX status. The shape mirrors
    :mod:`banking_pipeline.writer.builders.security_trade`: a single
    aggregate ``Expenses:<prefix>:<portfolio>:Fees:<ccy>`` posting at
    ``abs(tx.fees)`` when ``len(fee_breakdown) <= 1``, or one posting
    per breakdown item with the item description as an inline ``;
    <description>`` comment when the document itemises multiple fee
    components. ``Costes 0.00`` advices skip the leg entirely (the
    ``tx.fees != 0`` guard), which keeps the older non-zero-spread
    goldens (``switch_entrada.2021`` / ``switch_entrada.2023``)
    byte-stable.

    Header link
    -----------
    The ``^<txn_no>`` after the narrations is a beancount link (not a
    tag — those use ``#``). Switches receive a link in addition to the
    ``no:`` comment so cross-reference queries in ``bean-query`` can
    find the entry without parsing comments.
    """

    sec_ccy = tx.security_currency or tx.currency
    isin = tx.isin or "Unknown"
    entry_date = tx.booking_date or tx.trade_date

    # --- Optional inline open directive --------------------------------
    # No-op today (``OPEN_EMITTING_TYPES`` is empty — account opens
    # are centralised in ``portfolio.beancount``); kept as a hook so
    # a standalone-file workflow can opt back in.
    out = inline_open_directive(tx, doc_type, prefix)

    lines: list[str] = []

    # --- Header ---------------------------------------------------------
    # Link precedence: ``link_id`` wins (set by a future pairing layer
    # that can resolve the salida↔entrada cross-reference); otherwise
    # fall back to ``transaction_number`` so a switch leg processed in
    # isolation still carries a discoverable link.
    parts: list[str] = [str(entry_date), "*"]
    if tx.title:
        parts.append(f'"{escape(tx.title)}"')
    parts.append(f'"{escape(tx.narration)}"')
    link = tx.link_id or tx.transaction_number
    if link:
        parts.append(f"^{link}")
    lines.append(" ".join(parts))

    # --- Asset leg ------------------------------------------------------
    # Salida uses ``{} @ <price>`` (reduce-from-inventory at market price);
    # entrada uses ``{<price> <ccy>}`` (new units enter at purchase cost).
    qty_str = format_amount(tx.quantity) if tx.quantity is not None else "0"
    if tx.price is not None:
        if doc_type == DocumentType.SWITCH_SALIDA:
            cost_extras = f" {{}} @ {format_amount(tx.price)} {sec_ccy}"
        else:
            cost_extras = f" {{{format_amount(tx.price)} {sec_ccy}}}"
    else:
        cost_extras = ""
    portfolio = portfolio_segment(tx.account_number)
    lines.append(
        align(
            f"Assets:{prefix}:{portfolio}:{isin}",
            qty_str,
            isin,
            extras=cost_extras,
        )
    )

    # --- Fees leg(s) ---------------------------------------------------
    # Per-line items get one posting each with the item description as
    # an inline ``; <description>`` comment AND a category-specific
    # account segment via :func:`fee_segment` (e.g. ``Spread`` →
    # ``Spread:<ccy>``, future ``Tasa bursátil`` → ``Tax:<ccy>``).
    # This shares the same taxonomy as every other builder so a
    # year-by-year spread-cost query lands a single
    # account-prefix match across switches, FX settlements, and
    # security trades. When only the in-block aggregate ``tx.fees``
    # is set, fall back to the generic ``Fees:<ccy>`` segment. The
    # ``tx.fees != 0`` guard skips the leg entirely on zero-fee
    # advices, keeping ``switch_entrada.2021`` / ``switch_entrada.2023``
    # / ``switch_salida.2021`` byte-stable.
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
    elif tx.fees is not None and tx.fees != 0:
        fees_ccy = tx.fees_currency or sec_ccy
        lines.append(
            align(
                f"Expenses:{prefix}:{portfolio}:Fees:{fees_ccy}",
                format_amount(abs(tx.fees)),
                fees_ccy,
            )
        )

    # --- Switch holding leg --------------------------------------------
    # Sign is as printed by Pictet's ``Importe neto``: positive on salida
    # (proceeds into the holding), negative on entrada (cost leaving the
    # holding to fund the buy). When the underlying is in a different
    # currency than the Switch holding (FX entrada / FX salida), append
    # ``@@ <subtotal> <sec_ccy>`` so beancount sees the conversion.
    cash_extras = ""
    if tx.is_fx and tx.subtotal_security is not None:
        cash_extras = (
            f" @@ {format_amount(abs(tx.subtotal_security))} {sec_ccy}"
        )
    lines.append(
        align(
            f"Assets:{prefix}:{portfolio}:Switch:{tx.currency}",
            format_amount(tx.amount),
            tx.currency,
            extras=cash_extras,
        )
    )

    # --- Unrealized gain/loss (salida only) ----------------------------
    if doc_type == DocumentType.SWITCH_SALIDA:
        # Elastic posting — no amount, beancount fills in the balance.
        lines.append(f"  Income:{prefix}:{portfolio}:{isin}:Unrealized")

    # --- Trailing reference comment ------------------------------------
    # The ``no:`` comment carries the document's own transaction number,
    # which differs from the link on entrada when pairing is wired up
    # (link = salida's txn, no: = entrada's own txn).
    trailer = transaction_number_comment(tx)
    if trailer:
        lines.append(trailer)

    return out + "\n".join(lines) + "\n"
