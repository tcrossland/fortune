"""Render :class:`Transaction` objects as beancount plain-text entries.

We emit the text directly (no runtime dependency on the ``beancount`` package,
which is GPL-2.0). If you want to validate the output, shell out to the
``bean-check`` CLI as a separate process — that's a normal program invocation,
not library linking.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Iterable

from jinja2 import Environment

from banking_pipeline.models import (
    BankId,
    Classification,
    DocumentType,
    ExtractionResult,
    Transaction,
)

# Right-edge column for amount values in postings. Beancount tolerates any
# alignment; this constant is what the project's golden ``.beancount`` files
# use, and matching it makes diff-based testing meaningful.
_AMOUNT_COL = 59

# Short account-name prefix per bank — used in ``Assets:<prefix>:<portfolio>:<ISIN>``,
# ``Assets:<prefix>:<currency>``, ``Expenses:<prefix>:Fees:<ccy>``. Banks not
# in this map fall back to ``"Unknown"``; that keeps generic/bank-agnostic
# classifications producing valid (if ugly) beancount instead of crashing.
BANK_PREFIX: dict[BankId, str] = {
    BankId.PICTET: "Pic",
}


def _bank_prefix(classification: Classification | None) -> str:
    """Resolve a bank prefix from a ``Classification``.

    Falls back to ``"Unknown"`` when the classification carries no bank
    (generic rules) or when the bank isn't in :data:`BANK_PREFIX`. The
    fallback keeps the writer producing parseable beancount even on
    bank-agnostic test fixtures.
    """

    if classification is None or classification.bank is None:
        return "Unknown"
    return BANK_PREFIX.get(classification.bank.bank, "Unknown")

# NOTE: do NOT enable ``trim_blocks`` / ``lstrip_blocks`` here. With those
# defaults Jinja swallows the newline that follows a block tag (``{% endif %}``
# etc.), which historically collapsed multi-leg postings onto a single line
# and produced output bean-check rejected. None of the surviving Jinja
# templates use block tags today, but the safe-default stays as a guard
# against the next template that adds an inline conditional.
_ENV = Environment()

# Security-trade doctypes — buys list the asset leg first, sells list the
# cash leg first (so the account that *receives* the value is always the
# first posting).
_SECURITY_BUY_TYPES: frozenset[DocumentType] = frozenset({
    DocumentType.TRADE_CONFIRMATION,
    DocumentType.SUBSCRIPTION_NOTICE,
    DocumentType.BUY_BONDS,
    DocumentType.BUY_STRUCTURED_PRODUCTS,
    DocumentType.BUY_ETF,
    DocumentType.BUY_SHARES,
    DocumentType.COMPRA,
    DocumentType.SUSCRIPCION,
    DocumentType.SWITCH_ENTRADA,
})

_SECURITY_SELL_TYPES: frozenset[DocumentType] = frozenset({
    DocumentType.FINAL_REDEMPTION,
    DocumentType.REDEMPTION_NOTICE,
    DocumentType.REEMBOLSO,
    DocumentType.REEMBOLSO_FINAL,
    DocumentType.SELL_BONDS,
    DocumentType.SELL_ETF,
    DocumentType.SELL_STRUCTURED_PRODUCTS,
    DocumentType.SWITCH_SALIDA,
    DocumentType.VENTA,
})

# Doctypes routed through ``_render_bond_trade`` — a trade shape that
# differs from the regular security-trade renderer in carrying a
# dedicated ``Income:<prefix>:<isin>:Interest`` leg for the accrued
# interest paid alongside the principal. Used for both directions:
# on a buy the buyer pays accrued interest to the seller (income
# debited); on a sell the buyer's accrued payment hits the seller's
# cash (income credited). The renderer branches on
# ``_SECURITY_BUY_TYPES`` to pick the asset-leg cost form and the
# leg ordering.
_BOND_TRADE_TYPES: frozenset[DocumentType] = frozenset({
    DocumentType.BUY_BONDS,
    DocumentType.SELL_BONDS,
})

_SECURITY_TRADE_TYPES = _SECURITY_BUY_TYPES | _SECURITY_SELL_TYPES

# Switches are *also* members of the buy/sell sets above (entrada is a buy,
# salida is a sell at the security-leg level), so ``render_open_directives``
# still finds their ISINs and emits the right ``Assets:<prefix>:<portfolio>:<ISIN>``
# opens. The dispatcher in ``_render_transaction`` checks this set first so
# switch advices route to the switch builder rather than the regular trade
# builder — the cash-leg shape, the ``{} @`` cost form, the Switch holding
# account, the ``^<txn>`` link, and the elastic Unrealized leg are all
# distinctive enough to warrant their own builder.
_SWITCH_TYPES: frozenset[DocumentType] = frozenset({
    DocumentType.SWITCH_SALIDA,
    DocumentType.SWITCH_ENTRADA,
})

# Fee-advice doctypes routed through ``_render_fee_advice``. Both the
# ES ``Débito de gastos`` and EN ``Debit of fees`` advices have
# bank-prefixed multi-leg goldens; ``find_fee_breakdown`` handles
# their single-line and multi-line label layouts respectively.
# ``FACTURA`` is intentionally excluded — that doctype's template
# returns ``[]`` to avoid double-counting against the matching
# ``Débito de gastos`` advice (same economic event, two paper trails).
_FEE_ADVICE_TYPES: frozenset[DocumentType] = frozenset({
    DocumentType.DEBIT_OF_FEES,
    DocumentType.DEBITO_DE_GASTOS,
})

# Doctypes routed through ``_render_third_party_payment`` — simple
# cash-in/cash-out entries with an elastic ``Income:<prefix>:Other``
# (incoming) or ``Expenses:<prefix>:Other`` (outgoing) posting that
# beancount auto-balances. Direction is keyed on the cash-leg sign
# inside the renderer; the doctype membership just routes here.
#
# ``PAGO_INTERNA`` (self-to-self) stays on the legacy Jinja
# ``_CASH_IN_TEMPLATE`` for now — its real shape is asset→asset across
# the user's own external accounts, not income/expense, and that needs
# a separate builder once a golden lands.
_THIRD_PARTY_PAYMENT_TYPES: frozenset[DocumentType] = frozenset({
    DocumentType.INCOMING_PAYMENT,
    DocumentType.PAGO_ENTRANTE,
    DocumentType.PAYMENT,
})

# Doctypes routed through ``_render_internal_transfer`` — cross-currency
# book transfers between the user's own current accounts. The single
# entry holds the source-currency debit leg, the destination-currency
# credit leg with an ``@@ <abs_source> <src_ccy>`` annotation, and the
# trailing ``no:`` reference. ``SPOT`` keeps using the legacy
# ``_FX_LEG_TEMPLATE`` until it gets its own golden; ``FX_FORWARD``'s
# template returns ``[]`` (the opening has no cash impact; the matching
# ``SETTLE_FX_FORWARD`` advice books the cash exchange at maturity and
# is routed through ``_render_fx_settlement`` — see below).
_INTERNAL_TRANSFER_TYPES: frozenset[DocumentType] = frozenset({
    DocumentType.INTERNAL_TRANSFER,
})

# Doctypes routed through ``_render_fx_settlement`` — physical-settlement
# of an OTC FX forward at maturity. Same two-leg-cash shape as
# ``_render_internal_transfer`` but with a third ``Expenses:<prefix>:Fees:<ccy>``
# posting carrying the forward spread, and the @@ value derived from the
# pre-fee gross rather than the post-fee net.
_FX_SETTLEMENT_TYPES: frozenset[DocumentType] = frozenset({
    DocumentType.SETTLE_FX_FORWARD,
})

# Doctypes that produce no beancount output whatsoever — neither the
# ``;`` audit header nor any transaction lines. Used for documents
# whose information either duplicates a cash leg booked elsewhere or
# is purely a position snapshot rather than a movement.
#
# Two families:
#
#   - **Paired-advice openings**: ``FX_FORWARD`` is the canonical
#     case — the opening of the contract has zero cash effect, and
#     the matching ``SETTLE_FX_FORWARD`` advice at maturity is the
#     canonical paper trail for the cash exchange.
#
#   - **Periodic valuation statements**: monthly / quarterly / annual
#     reports across both locales. These describe portfolio
#     valuations at a point in time, not transactions. Their cash
#     events have already been booked by the per-trade and per-cash-
#     movement advices that fed them; emitting any postings from a
#     statement would double-count, and the regex-extractor fallback
#     was producing degraded ``Equity:Uncategorized`` postings on
#     them. ``ACCOUNT_STATEMENT`` is the generic non-bank-specific
#     equivalent.
#
# Per-template ``extract`` may already return ``[]`` for some of
# these doctypes; enforcing the rule at the writer level adds
# defence-in-depth so the regex-extractor fallback can't re-emit
# postings if a statement classifies but its template misses.
_NO_EMIT_TYPES: frozenset[DocumentType] = frozenset({
    DocumentType.FX_FORWARD,
    # Periodic-valuation statements (English / Pictet Luxembourg).
    DocumentType.MONTHLY_STATEMENT,
    DocumentType.QUARTERLY_STATEMENT,
    DocumentType.ANNUAL_STATEMENT,
    # Periodic-valuation statements (Spanish / Pictet Madrid).
    DocumentType.ESTADO_MENSUAL,
    DocumentType.ESTADO_TRIMESTRAL,
    DocumentType.ESTADO_ANUAL,
    # Generic non-bank-specific account statement.
    DocumentType.ACCOUNT_STATEMENT,
})

# Doctypes routed through ``_render_dividend`` — security-distribution
# advices that pay income on a held position. The shape is a two-leg
# entry (income-recognition leg + cash leg) keyed on the underlying
# ISIN. ``DIVIDEND_NOTICE`` is the canonical case; future
# ``CAPITAL_GAINS_DISTRIBUTION``-style doctypes would route here too.
_DIVIDEND_TYPES: frozenset[DocumentType] = frozenset({
    DocumentType.DIVIDEND_NOTICE,
})

# Doctypes routed through ``_render_interest`` — quarterly current-account
# interest postings. The shape is a two-leg entry: the cash leg flows as
# Pictet printed (negative on debit-balance interest charged to the user,
# positive on credit-balance interest paid to the user) and the
# counter-leg switches account family based on direction —
# ``Expenses:<prefix>:Interest:<ccy>`` for charges, ``Income:<prefix>:Interest:<ccy>``
# for earnings. ``INTEREST_SCALE`` is intentionally absent: the scale
# document is the per-day rate ledger that produced the same cash event
# the payment advice already books, and emitting both would
# double-count.
_INTEREST_TYPES: frozenset[DocumentType] = frozenset({
    DocumentType.INTEREST_PAYMENT,
})

# Doctypes that would emit an inline ``open Assets:<prefix>:<portfolio>:<ISIN> <ISIN>``
# directive at the top of their entry. Empty by design today — every
# account open is centralised in ``portfolio.beancount`` (via the
# ``banking-pipeline portfolio`` aggregate command), and emitting an
# inline open here would duplicate the central one and trip a
# duplicate-open error in ``bean-check``.
#
# The membership set is preserved (rather than removing the
# ``_inline_open_directive`` helper outright) so a future workflow
# that renders per-document beancount files independently of the
# central aggregate can opt back in by adding the relevant doctypes.
_OPEN_EMITTING_TYPES: frozenset[DocumentType] = frozenset()


def _portfolio_segment(account_number: str | None) -> str:
    """Sanitise a Pictet portfolio identifier for use as a beancount account
    segment.

    Pictet prints portfolio IDs as ``P-123456.789`` / ``K-123456.001`` —
    a dash and a period that are both invalid in beancount account
    segments (each segment is letters, digits, and hyphens only, and
    the dash position rules are tighter than what Pictet's format
    produces). We strip both punctuation marks so the same identity
    survives as ``P123456789`` / ``K123456001``: still
    portfolio-unique, still readable, but a valid beancount segment.

    Returns ``Unknown`` when the document didn't carry a portfolio
    identifier — rare for Pictet but kept as a fallback so the writer
    produces parseable output on malformed input.
    """

    if not account_number:
        return "Unknown"
    return account_number.replace("-", "").replace(".", "")


# Expose ``_portfolio_segment`` to Jinja so the fallback template can
# sanitise ``tx.account_number`` the same way the Python builders do.
_ENV.filters["portfolio_segment"] = _portfolio_segment


def _cash_account(prefix: str, account_number: str | None, currency: str) -> str:
    """Build a bank-prefixed cash-account path including the portfolio.

    Format: ``Assets:<prefix>:<portfolio>:<currency>`` — e.g.
    ``Assets:Pic:P999999999:GBP``. The portfolio segment lets users
    distinguish multiple Pictet accounts they hold within the same
    currency (e.g. ``P-…`` vs ``K-…`` portfolios that both have an EUR
    sub-account); without it beancount would treat them as the same
    bucket. The Pictet ID is sanitised through
    :func:`_portfolio_segment` to drop the dash and period so the
    segment is valid beancount syntax. Falls back to ``Unknown`` when
    the document doesn't carry a portfolio identifier — that's rare
    for Pictet (every advice we see has an ``Account no.`` /
    ``N° de cuenta`` header), but the fallback keeps the writer
    producing parseable output even on malformed input.
    """

    return f"Assets:{prefix}:{_portfolio_segment(account_number)}:{currency}"


def _inline_open_directive(
    tx: Transaction, doc_type: DocumentType, prefix: str
) -> str:
    """Return the inline ``open`` directive line for advices that need
    one, or an empty string when they don't.

    Returns ``""`` for every doctype today: ``_OPEN_EMITTING_TYPES``
    is the empty set because account opens are centralised in
    ``portfolio.beancount`` (via the ``banking-pipeline portfolio``
    aggregate command). Re-emitting an inline open here would
    duplicate the central declaration and trip a duplicate-open
    error when the per-year file is loaded alongside the aggregate.

    The function is preserved as a hook so a future workflow that
    renders standalone per-document beancount files (without the
    central aggregate) can opt back in by repopulating
    ``_OPEN_EMITTING_TYPES``. Output format when active:
    ``<date> open Assets:<prefix>:<portfolio>:<ISIN> <ISIN>\\n`` with
    a trailing newline so callers can prepend it directly to their
    entry text without juggling separators.
    """

    if doc_type not in _OPEN_EMITTING_TYPES:
        return ""
    if not tx.isin:
        return ""
    entry_date = tx.booking_date or tx.trade_date
    portfolio = _portfolio_segment(tx.account_number)
    return f"{entry_date} open Assets:{prefix}:{portfolio}:{tx.isin} {tx.isin}\n"


_CASH_IN_TEMPLATE = _ENV.from_string(
    """\
{{ tx.trade_date }} * "{{ narration }}"
  Assets:{{ prefix }}:{{ tx.account_number | portfolio_segment }}:{{ tx.currency }}  {{ tx.amount }} {{ tx.currency }}
  Equity:Uncategorized                           {{ -tx.amount }} {{ tx.currency }}
"""
)

# Single cash leg of an FX trade or internal transfer. Two of these per
# document balance each other when viewed together; each is valid alone.
_FX_LEG_TEMPLATE = _ENV.from_string(
    """\
{{ tx.trade_date }} * "{{ narration }}"
  Assets:{{ prefix }}:{{ tx.account_number | portfolio_segment }}:{{ tx.currency }}  {{ tx.amount }} {{ tx.currency }}
  Equity:Uncategorized                           {{ -tx.amount }} {{ tx.currency }}
"""
)

_TEMPLATES = {
    # --- Security trades ---
    # Routed through ``_render_security_trade`` (the Python builder), not
    # through Jinja: the FX-vs-non-FX split + bank-prefixed accounts +
    # column-aligned amounts are awkward to express as a static template,
    # and the builder avoids the trim_blocks/whitespace fragility we hit
    # when the legacy Jinja templates carried inline conditionals.

    # --- Dividends ---
    # ``DIVIDEND_NOTICE`` routes through ``_render_dividend`` (the Python
    # builder) for the new bank-prefixed two-leg shape; see
    # ``_DIVIDEND_TYPES``. The legacy Jinja form here is gone.

    # --- Interest ---
    # ``INTEREST_PAYMENT`` routes through ``_render_interest`` (the Python
    # builder) for the new bank-prefixed two-leg shape; see
    # ``_INTEREST_TYPES``. ``INTEREST_SCALE`` is the companion ledger
    # document — informational only, the template returns ``[]`` and no
    # beancount entry is emitted (the matching ``INTEREST_PAYMENT`` advice
    # carries the cash leg). ``INTEREST_NOTICE`` is gone — it was a
    # speculative third doctype that never landed a fixture and would
    # have duplicated ``INTEREST_PAYMENT``'s shape if it had.
    # --- Fees ---
    # ``DEBITO_DE_GASTOS`` and ``DEBIT_OF_FEES`` route through
    # ``_render_fee_advice`` (the Python builder) for the new
    # bank-prefixed multi-leg shape with the per-line breakdown.
    # ``FACTURA``'s template returns ``[]`` so the writer never sees a
    # Transaction for it (it's the tax-invoice paper trail of a
    # ``Débito de gastos`` event we already book elsewhere — emitting
    # both would double-count). ``FEE_NOTICE`` was a third speculative
    # doctype that never landed a fixture and has been removed from
    # the model entirely.
    # --- Cash movements ---
    # ``INCOMING_PAYMENT`` and ``PAYMENT`` route through
    # ``_render_third_party_payment`` (the Python builder); see
    # ``_THIRD_PARTY_PAYMENT_TYPES``. ``PAGO_INTERNA`` (self-to-self)
    # stays on the legacy ``_CASH_IN_TEMPLATE`` for now — its real
    # shape is asset→asset across the user's own external accounts,
    # not income/expense, which needs a separate builder.
    DocumentType.PAGO_INTERNA: _CASH_IN_TEMPLATE,
    # --- FX advices ---
    # ``INTERNAL_TRANSFER`` routes through ``_render_internal_transfer``
    # (a Python builder) — see ``_INTERNAL_TRANSFER_TYPES``.
    # ``SETTLE_FX_FORWARD`` routes through ``_render_fx_settlement`` —
    # see ``_FX_SETTLEMENT_TYPES``. ``FX_FORWARD``'s template returns
    # ``[]`` (the contract opening has no cash impact; the matching
    # ``SETTLE_FX_FORWARD`` advice books the cash exchange at maturity),
    # so the writer never sees a Transaction for it. ``SPOT`` keeps
    # the legacy two-leg-per-document Jinja path for now until it
    # gets its own golden.
    DocumentType.SPOT: _FX_LEG_TEMPLATE,
    # --- Non-cash events ---
    DocumentType.LIMIT_EXTENSION: _ENV.from_string(
        """\
{{ tx.trade_date }} * "{{ narration }}"
  ; non-cash event — credit line adjustment, no postings
"""
    ),
}

_DEFAULT_TEMPLATE = _ENV.from_string(
    """\
{{ tx.trade_date }} * "{{ narration }}"
  ; TODO review — document type: {{ doc_type }}
  Assets:{{ prefix }}:{{ tx.account_number | portfolio_segment }}:{{ tx.currency }}  {{ tx.amount }} {{ tx.currency }}
  Equity:Uncategorized                           {{ -tx.amount }} {{ tx.currency }}
"""
)


def _format_amount(value: Decimal) -> str:
    """Format a ``Decimal`` for emission inside a beancount posting.

    Returns the canonical decimal string — no thousands separators, sign as
    stored. Beancount accepts arbitrary precision, so we don't normalise the
    number of decimals; the extractor preserved whatever the source PDF
    printed and we honour that.
    """

    return str(value)


def _align(account: str, amount: str, currency: str, extras: str = "") -> str:
    """Build a posting line with the amount right-aligned at ``_AMOUNT_COL``.

    Produces ``  <account><pad><amount> <currency><extras>`` where ``pad`` is
    the number of spaces that places the rightmost digit of ``amount`` at
    column ``_AMOUNT_COL`` (zero-indexed end position). When the account
    name is so long that even one space of padding would push the amount
    past the column, we fall back to a single space — the entry stays valid
    beancount, it just doesn't line up.
    """

    prefix = f"  {account}"
    pad = _AMOUNT_COL - len(prefix) - len(amount)
    if pad < 1:
        pad = 1
    return f"{prefix}{' ' * pad}{amount} {currency}{extras}"


def _render_security_sell_with_breakdown(
    tx: Transaction, doc_type: DocumentType, prefix: str
) -> str:
    """Render a security sell that carries a multi-item fee breakdown.

    Used for stock-exchange sales where Pictet prints a per-line ``Costes``
    block (``Corretaje y/o spread`` + ``Tasa bursátil`` etc.). The shape
    differs from the simpler sell path on three points:

      - Asset leg first (mirroring the switch_salida convention),
        followed by the fee legs, the FX-aware cash leg, and the
        elastic income leg.
      - One ``Expenses:<prefix>:Fees:<ccy>`` posting per breakdown item
        with the item's description as an inline ``; <description>``
        comment — preserves the audit detail Pictet prints rather than
        collapsing to a single aggregate fees leg.
      - Income leg uses ``Income:<prefix>:<ISIN>`` (no ``:Realized``
        suffix) — matches the convention the user's golden file
        establishes for this shape.

    The existing :func:`_render_security_trade` sell path stays in
    place for sells without a breakdown (e.g. ``reembolso_final`` with
    its ``Costes EUR 0.00`` non-FX layout); switching shapes based on
    breakdown presence avoids disturbing those goldens.

    No inline ``open`` directive is emitted: account opens are
    centralised in ``portfolio.beancount`` and an inline
    ``Income:<prefix>:<ISIN>`` open here would duplicate the central
    one and fail ``bean-check``.
    """

    sec_ccy = tx.security_currency or tx.currency
    isin = tx.isin or "Unknown"
    entry_date = tx.booking_date or tx.trade_date

    # Header (two-string narration when title is set).
    narration = _escape(tx.narration)
    if tx.title:
        header = f'{entry_date} * "{_escape(tx.title)}" "{narration}"'
    else:
        header = f'{entry_date} * "{narration}"'
    lines: list[str] = [header]

    # Asset leg — sell-from-inventory with empty cost-braces and
    # ``@ <price>`` market-price annotation.
    qty_str = _format_amount(tx.quantity) if tx.quantity is not None else "0"
    cost_basis = (
        f" {{}} @ {_format_amount(tx.price)} {sec_ccy}"
        if tx.price is not None
        else ""
    )
    portfolio = _portfolio_segment(tx.account_number)
    lines.append(
        _align(
            f"Assets:{prefix}:{portfolio}:{isin}",
            qty_str,
            isin,
            extras=cost_basis,
        )
    )

    # Per-item expense legs. Each fee item becomes its own posting with
    # the item's description as an inline beancount comment, so the
    # rendered entry preserves the audit detail Pictet printed.
    for item in tx.fee_breakdown:
        lines.append(
            _align(
                f"Expenses:{prefix}:{portfolio}:Fees:{item.currency}",
                _format_amount(abs(item.amount)),
                item.currency,
                extras=f" ; {item.description}",
            )
        )

    # Cash leg — FX-aware ``@@ <subtotal> <sec_ccy>`` annotation when
    # the security and cash-account currencies differ.
    cash_extras = ""
    if tx.is_fx and tx.subtotal_security is not None:
        cash_extras = (
            f" @@ {_format_amount(abs(tx.subtotal_security))} {sec_ccy}"
        )
    lines.append(
        _align(
            _cash_account(prefix, tx.account_number, tx.currency),
            _format_amount(tx.amount),
            tx.currency,
            extras=cash_extras,
        )
    )

    # Elastic income leg — beancount auto-balances against the cost
    # basis pulled from inventory and the proceeds.
    if tx.isin:
        lines.append(f"  Income:{prefix}:{portfolio}:{isin}")

    # Trailing reference comment.
    if tx.transaction_number:
        lines.append(f"  no: {tx.transaction_number}")

    return "\n".join(lines) + "\n"


def _render_security_trade(
    tx: Transaction, doc_type: DocumentType, prefix: str
) -> str:
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
    routed to :func:`_render_security_sell_with_breakdown`, which uses
    a different posting order, broken-out fee legs, and an inline
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
    # — see ``_render_security_sell_with_breakdown`` for why. Single-item
    # or empty breakdowns continue through the simpler path below so
    # existing goldens (``reembolso_final``, ``suscripcion.fx``) stay
    # byte-stable.
    if doc_type not in _SECURITY_BUY_TYPES and len(tx.fee_breakdown) > 1:
        return _render_security_sell_with_breakdown(tx, doc_type, prefix)

    sec_ccy = tx.security_currency or tx.currency

    # --- Optional inline open directive --------------------------------
    # No-op today (``_OPEN_EMITTING_TYPES`` is empty — account opens
    # are centralised in ``portfolio.beancount``); kept as a hook so
    # a standalone-file workflow can opt back in. See
    # ``_inline_open_directive`` for the gating rule.
    out = _inline_open_directive(tx, doc_type, prefix)

    # --- Header ---------------------------------------------------------
    # Booking date (when the cash actually moved) is preferred over
    # ``trade_date`` for the entry date when the document carries it.
    entry_date = tx.booking_date or tx.trade_date
    narration = _escape(tx.narration)
    if tx.title:
        title = _escape(tx.title)
        header = f'{entry_date} * "{title}" "{narration}"'
    else:
        header = f'{entry_date} * "{narration}"'

    lines: list[str] = [header]

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
    qty_str = _format_amount(tx.quantity) if tx.quantity is not None else "0"
    if tx.price is not None:
        if doc_type in _SECURITY_BUY_TYPES:
            cost_basis = f" {{{_format_amount(tx.price)} {sec_ccy}}}"
        else:
            cost_basis = f" {{}} @ {_format_amount(tx.price)} {sec_ccy}"
    else:
        cost_basis = ""
    portfolio = _portfolio_segment(tx.account_number)
    asset_line = _align(
        f"Assets:{prefix}:{portfolio}:{isin}",
        qty_str,
        isin,
        extras=cost_basis,
    )

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
    # dedicated ``_render_security_sell_with_breakdown`` builder above
    # (which also reorders postings so the cash leg lands first); buys
    # stay in this builder because their asset-first order is shared
    # with the single-fee path.
    fees_lines: list[str] = []
    if len(tx.fee_breakdown) > 1:
        for item in tx.fee_breakdown:
            fees_lines.append(
                _align(
                    f"Expenses:{prefix}:{portfolio}:Fees:{item.currency}",
                    _format_amount(abs(item.amount)),
                    item.currency,
                    extras=f" ; {item.description}",
                )
            )
    elif tx.fees is not None and tx.fees != 0:
        fees_ccy = tx.fees_currency or sec_ccy
        fees_lines.append(
            _align(
                f"Expenses:{prefix}:{portfolio}:Fees:{fees_ccy}",
                _format_amount(abs(tx.fees)),
                fees_ccy,
            )
        )

    # --- Cash leg -------------------------------------------------------
    cash_extras = ""
    if tx.is_fx and tx.subtotal_security is not None:
        cash_extras = (
            f" @@ {_format_amount(abs(tx.subtotal_security))} {sec_ccy}"
        )
    cash_line = _align(
        _cash_account(prefix, tx.account_number, tx.currency),
        _format_amount(tx.amount),
        tx.currency,
        extras=cash_extras,
    )

    # --- Posting order: asset-first for buys, cash-first for sells -----
    if doc_type in _SECURITY_BUY_TYPES:
        lines.append(asset_line)
        lines.extend(fees_lines)
        lines.append(cash_line)
    else:
        lines.append(cash_line)
        lines.extend(fees_lines)
        lines.append(asset_line)
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
    if tx.transaction_number:
        lines.append(f"  no: {tx.transaction_number}")

    return out + "\n".join(lines) + "\n"


def _render_bond_trade(
    tx: Transaction, doc_type: DocumentType, prefix: str
) -> str:
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
    first (same convention as ``_render_security_trade``).
    """

    isin = tx.isin or "Unknown"
    portfolio = _portfolio_segment(tx.account_number)
    entry_date = tx.booking_date or tx.trade_date
    is_buy = doc_type in _SECURITY_BUY_TYPES

    # No-op today (``_OPEN_EMITTING_TYPES`` is empty — account opens
    # are centralised in ``portfolio.beancount``); kept as a hook so
    # a standalone-file workflow can opt back in.
    out = _inline_open_directive(tx, doc_type, prefix)

    parts: list[str] = [str(entry_date), "*"]
    if tx.title:
        parts.append(f'"{_escape(tx.title)}"')
    parts.append(f'"{_escape(tx.narration)}"')
    lines: list[str] = [" ".join(parts)]

    # Asset leg. Buys carry a literal cost-basis brace
    # ``{<unit_px> <ccy>}``; sells use the empty-cost
    # ``{} @ <unit_px> <ccy>`` form so beancount reduces the position
    # at its existing inventory cost basis and the ``@`` records the
    # market price for capital-gains attribution.
    qty_str = _format_amount(tx.quantity) if tx.quantity is not None else "0"
    if tx.price is not None:
        if is_buy:
            cost_basis = f" {{{_format_amount(tx.price)} {tx.currency}}}"
        else:
            cost_basis = f" {{}} @ {_format_amount(tx.price)} {tx.currency}"
    else:
        cost_basis = ""
    asset_line = _align(
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
        fee_line = _align(
            f"Expenses:{prefix}:{portfolio}:Fees:{fees_ccy}",
            _format_amount(abs(tx.fees)),
            fees_ccy,
            extras=fee_label,
        )

    # Interest leg — direction-agnostic; ``-accrued_interest`` flips
    # Pictet's cash-sign printing into the income-account sign.
    interest_line: str | None = None
    if tx.accrued_interest is not None and tx.accrued_interest != 0:
        interest_line = _align(
            f"Income:{prefix}:{portfolio}:{isin}:Interest",
            _format_amount(-tx.accrued_interest),
            tx.currency,
            extras=" ; Accrued interest",
        )

    # Cash leg — signed as Pictet printed Net amount: positive on
    # sells, negative on buys.
    cash_line = _align(
        _cash_account(prefix, tx.account_number, tx.currency),
        _format_amount(tx.amount),
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

    if tx.transaction_number:
        lines.append(f"  no: {tx.transaction_number}")

    return out + "\n".join(lines) + "\n"


def _render_switch_trade(
    tx: Transaction, doc_type: DocumentType, prefix: str
) -> str:
    """Render a Pictet switch leg as a beancount entry.

    Switch advices have **no cash effect**: the proceeds (salida) or
    cost (entrada) land in an intermediate ``Assets:<prefix>:Switch:<ccy>``
    holding account that the paired leg later debits or credits, so the
    pair nets to zero across the two-document switch.

    Salida (sale) layout::

        <booking_date> * "<title>" "<narration>" ^<txn_no>
          Assets:<prefix>:<portfolio>:<ISIN>          <quantity> <ISIN> {} @ <price> <ccy>
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
    # No-op today (``_OPEN_EMITTING_TYPES`` is empty — account opens
    # are centralised in ``portfolio.beancount``); kept as a hook so
    # a standalone-file workflow can opt back in.
    out = _inline_open_directive(tx, doc_type, prefix)

    lines: list[str] = []

    # --- Header ---------------------------------------------------------
    # Link precedence: ``link_id`` wins (set by a future pairing layer
    # that can resolve the salida↔entrada cross-reference); otherwise
    # fall back to ``transaction_number`` so a switch leg processed in
    # isolation still carries a discoverable link.
    parts: list[str] = [str(entry_date), "*"]
    if tx.title:
        parts.append(f'"{_escape(tx.title)}"')
    parts.append(f'"{_escape(tx.narration)}"')
    link = tx.link_id or tx.transaction_number
    if link:
        parts.append(f"^{link}")
    lines.append(" ".join(parts))

    # --- Asset leg ------------------------------------------------------
    # Salida uses ``{} @ <price>`` (reduce-from-inventory at market price);
    # entrada uses ``{<price> <ccy>}`` (new units enter at purchase cost).
    qty_str = _format_amount(tx.quantity) if tx.quantity is not None else "0"
    if tx.price is not None:
        if doc_type == DocumentType.SWITCH_SALIDA:
            cost_extras = f" {{}} @ {_format_amount(tx.price)} {sec_ccy}"
        else:
            cost_extras = f" {{{_format_amount(tx.price)} {sec_ccy}}}"
    else:
        cost_extras = ""
    portfolio = _portfolio_segment(tx.account_number)
    lines.append(
        _align(
            f"Assets:{prefix}:{portfolio}:{isin}",
            qty_str,
            isin,
            extras=cost_extras,
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
            f" @@ {_format_amount(abs(tx.subtotal_security))} {sec_ccy}"
        )
    lines.append(
        _align(
            f"Assets:{prefix}:{portfolio}:Switch:{tx.currency}",
            _format_amount(tx.amount),
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
    if tx.transaction_number:
        lines.append(f"  no: {tx.transaction_number}")

    return out + "\n".join(lines) + "\n"


def _render_fee_advice(
    tx: Transaction, doc_type: DocumentType, prefix: str
) -> str:
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

    entry_date = tx.booking_date or tx.trade_date
    parts: list[str] = [str(entry_date), "*"]
    if tx.title:
        parts.append(f'"{_escape(tx.title)}"')
    parts.append(f'"{_escape(tx.narration)}"')
    lines: list[str] = [" ".join(parts)]

    portfolio = _portfolio_segment(tx.account_number)

    # --- Expense legs ---------------------------------------------------
    if tx.fee_breakdown:
        for item in tx.fee_breakdown:
            lines.append(
                _align(
                    f"Expenses:{prefix}:{portfolio}:Fees:{item.currency}",
                    _format_amount(abs(item.amount)),
                    item.currency,
                    extras=f" ; {item.description}",
                )
            )
    else:
        # No per-line breakdown — fall back to a single aggregate leg.
        lines.append(
            _align(
                f"Expenses:{prefix}:{portfolio}:Fees:{tx.currency}",
                _format_amount(abs(tx.amount)),
                tx.currency,
            )
        )

    # --- Cash leg -------------------------------------------------------
    lines.append(
        _align(
            _cash_account(prefix, tx.account_number, tx.currency),
            _format_amount(tx.amount),
            tx.currency,
        )
    )

    # --- Trailing reference comment ------------------------------------
    if tx.transaction_number:
        lines.append(f"  no: {tx.transaction_number}")

    return "\n".join(lines) + "\n"


def _render_third_party_payment(
    tx: Transaction, doc_type: DocumentType, prefix: str
) -> str:
    """Render a third-party / self-to-self payment advice as a beancount entry.

    Three render shapes, keyed on what the extractor populated:

    **Self-to-self payment** (``tx.counter_account`` set — outgoing
    payment to one of the user's own external accounts, e.g. Revolut)::

        <booking_date> * "<title>" "<narration>"
          Assets:<counter_account>:<currency>     <gross_amount> <ccy> ; Gross amount
          Assets:<prefix>:<portfolio>:<currency>  <amount>      <ccy> ; Net amount
          Expenses:<prefix>:Fees:<ccy>            <abs_fees>    <ccy> ; Payment fees
          no: <transaction_number>

    Three legs that balance arithmetically: the user receives
    ``gross_amount`` in their external account, the Pictet portfolio's
    cash account decreases by ``amount`` (which is gross + fees, signed
    negative), and the wire fee posts to ``Expenses:<prefix>:Fees:<ccy>``.

    **Incoming third-party payment** (``tx.amount > 0`` — third party
    paid the user)::

        <booking_date> * "<title>" "<narration>"
          Assets:<prefix>:<portfolio>:<currency>  <amount> <ccy>
          Income:<prefix>:Other
          no: <transaction_number>

    **Outgoing third-party payment** (``tx.amount < 0`` — user paid a
    third party who isn't them)::

        <booking_date> * "<title>" "<narration>"
          Assets:<prefix>:<portfolio>:<currency>  <amount> <ccy>
          Expenses:<prefix>:Other
          no: <transaction_number>

    The elastic counter-leg ``Income:<prefix>:Other`` /
    ``Expenses:<prefix>:Other`` carries no amount; beancount
    auto-balances against the cash leg. ``Other`` is a placeholder
    that the user can rewire to payer/payee-specific accounts.
    """

    entry_date = tx.booking_date or tx.trade_date
    parts: list[str] = [str(entry_date), "*"]
    if tx.title:
        parts.append(f'"{_escape(tx.title)}"')
    parts.append(f'"{_escape(tx.narration)}"')
    lines: list[str] = [" ".join(parts)]

    portfolio = _portfolio_segment(tx.account_number)

    # --- Self-to-self three-leg shape ---------------------------------
    if tx.counter_account is not None and tx.gross_amount is not None:
        # Destination leg — user's external account credited with the
        # principal sent. Positive amount, no portfolio (the external
        # bank's account naming is its own concern).
        lines.append(
            _align(
                f"Assets:{tx.counter_account}:{tx.currency}",
                _format_amount(tx.gross_amount),
                tx.currency,
                extras=" ; Gross amount",
            )
        )
        # Source leg — Pictet portfolio cash account debited with the
        # net (gross + fees, signed negative).
        lines.append(
            _align(
                _cash_account(prefix, tx.account_number, tx.currency),
                _format_amount(tx.amount),
                tx.currency,
                extras=" ; Net amount",
            )
        )
        # Wire fee leg — Pictet's payment-fee charge as an expense.
        if tx.fees is not None and tx.fees != 0:
            fees_ccy = tx.fees_currency or tx.currency
            lines.append(
                _align(
                    f"Expenses:{prefix}:{portfolio}:Fees:{fees_ccy}",
                    _format_amount(abs(tx.fees)),
                    fees_ccy,
                    extras=" ; Payment fees",
                )
            )
        if tx.transaction_number:
            lines.append(f"  no: {tx.transaction_number}")
        return "\n".join(lines) + "\n"

    # --- Two-leg-elastic shape (incoming / non-self-to-self outgoing) -
    lines.append(
        _align(
            _cash_account(prefix, tx.account_number, tx.currency),
            _format_amount(tx.amount),
            tx.currency,
        )
    )
    # Elastic counter-leg, keyed on direction.
    if tx.amount >= 0:
        lines.append(f"  Income:{prefix}:{portfolio}:Other")
    else:
        lines.append(f"  Expenses:{prefix}:{portfolio}:Other")

    if tx.transaction_number:
        lines.append(f"  no: {tx.transaction_number}")

    return "\n".join(lines) + "\n"


def _render_interest(
    tx: Transaction, doc_type: DocumentType, prefix: str
) -> str:
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

    entry_date = tx.booking_date or tx.trade_date
    parts: list[str] = [str(entry_date), "*"]
    if tx.title:
        parts.append(f'"{_escape(tx.title)}"')
    parts.append(f'"{_escape(tx.narration)}"')
    lines: list[str] = [" ".join(parts)]

    portfolio = _portfolio_segment(tx.account_number)

    # Counter-leg: Expenses for negative cash (interest charged),
    # Income for positive cash (interest earned).
    if tx.amount < 0:
        lines.append(
            _align(
                f"Expenses:{prefix}:{portfolio}:Interest:{tx.currency}",
                _format_amount(abs(tx.amount)),
                tx.currency,
            )
        )
    else:
        lines.append(
            _align(
                f"Income:{prefix}:{portfolio}:Interest:{tx.currency}",
                _format_amount(-tx.amount),
                tx.currency,
            )
        )

    # Cash leg — signed as Pictet printed it.
    lines.append(
        _align(
            _cash_account(prefix, tx.account_number, tx.currency),
            _format_amount(tx.amount),
            tx.currency,
        )
    )

    if tx.transaction_number:
        lines.append(f"  no: {tx.transaction_number}")

    return "\n".join(lines) + "\n"


def _render_dividend(
    tx: Transaction, doc_type: DocumentType, prefix: str
) -> str:
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
    :func:`render_open_directives` (which collects them across an
    extraction batch) or your existing ledger-level conventions.
    """

    isin = tx.isin or "Unknown"
    portfolio = _portfolio_segment(tx.account_number)
    entry_date = tx.booking_date or tx.trade_date
    parts: list[str] = [str(entry_date), "*"]
    if tx.title:
        parts.append(f'"{_escape(tx.title)}"')
    parts.append(f'"{_escape(tx.narration)}"')
    lines: list[str] = [" ".join(parts)]

    # Income leg — signed-negative (beancount income-account convention).
    lines.append(
        _align(
            f"Income:{prefix}:{portfolio}:{isin}:Dividend",
            _format_amount(-tx.amount),
            tx.currency,
        )
    )

    # Cash leg — signed as Pictet printed it (positive, cash in).
    lines.append(
        _align(
            _cash_account(prefix, tx.account_number, tx.currency),
            _format_amount(tx.amount),
            tx.currency,
        )
    )

    if tx.transaction_number:
        lines.append(f"  no: {tx.transaction_number}")

    return "\n".join(lines) + "\n"


def _render_internal_transfer(
    tx: Transaction, doc_type: DocumentType, prefix: str
) -> str:
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

    Skips its job if ``counter_currency`` / ``counter_amount`` aren't
    populated (legacy callers that built ``Transaction`` objects without
    the cross-leg fields), falling back to a single-leg render via
    ``_FX_LEG_TEMPLATE``-style shape — but in practice every fresh
    extraction populates both fields.
    """

    if tx.counter_currency is None or tx.counter_amount is None:
        # Defensive fallback: legacy/incomplete Transaction. Render the
        # single leg with the legacy account naming so the entry at
        # least balances against ``Equity:Uncategorized``.
        return _TEMPLATES[DocumentType.INTERNAL_TRANSFER].render(
            tx=tx, doc_type=doc_type, narration=_escape(tx.narration)
        ) if doc_type in _TEMPLATES else _DEFAULT_TEMPLATE.render(
            tx=tx, doc_type=doc_type, narration=_escape(tx.narration)
        )

    entry_date = tx.booking_date or tx.trade_date
    parts: list[str] = [str(entry_date), "*"]
    if tx.title:
        parts.append(f'"{_escape(tx.title)}"')
    parts.append(f'"{_escape(tx.narration)}"')
    lines: list[str] = [" ".join(parts)]

    # Source (debit) leg — signed negative as printed.
    lines.append(
        _align(
            _cash_account(prefix, tx.account_number, tx.currency),
            _format_amount(tx.amount),
            tx.currency,
        )
    )

    # Destination (credit) leg with ``@@`` total-cost annotation. The
    # absolute value of the source leg's amount goes in the source
    # currency on the right of the ``@@`` — beancount uses that to
    # reconcile the two cash currencies without needing the explicit
    # rate field.
    lines.append(
        _align(
            _cash_account(prefix, tx.account_number, tx.counter_currency),
            _format_amount(tx.counter_amount),
            tx.counter_currency,
            extras=f" @@ {_format_amount(abs(tx.amount))} {tx.currency}",
        )
    )

    if tx.transaction_number:
        lines.append(f"  no: {tx.transaction_number}")

    return "\n".join(lines) + "\n"


def _render_fx_settlement(
    tx: Transaction, doc_type: DocumentType, prefix: str
) -> str:
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

    Falls back to ``_render_internal_transfer`` when the fee fields
    aren't populated — covers any future fee-less Settle FX forward
    variant Pictet might emit (none in the current fixture set).
    """

    if (
        tx.counter_currency is None
        or tx.counter_amount is None
        or tx.fees is None
        or tx.fees_currency is None
    ):
        return _render_internal_transfer(tx, doc_type, prefix)

    entry_date = tx.booking_date or tx.trade_date
    parts: list[str] = [str(entry_date), "*"]
    if tx.title:
        parts.append(f'"{_escape(tx.title)}"')
    parts.append(f'"{_escape(tx.narration)}"')
    lines: list[str] = [" ".join(parts)]

    portfolio = _portfolio_segment(tx.account_number)

    # Fee-bearing cash leg, signed as printed.
    lines.append(
        _align(
            _cash_account(prefix, tx.account_number, tx.currency),
            _format_amount(tx.amount),
            tx.currency,
        )
    )

    # Forward-spread expense leg. Pictet writes the spread negative
    # (cash-out from the user's perspective); flip to positive for
    # the expense account.
    lines.append(
        _align(
            f"Expenses:{prefix}:{portfolio}:Fees:{tx.fees_currency}",
            _format_amount(abs(tx.fees)),
            tx.fees_currency,
            extras=" ; Forward spread",
        )
    )

    # Counter cash leg with ``@@ <abs_gross> <ccy>`` annotation.
    # ``gross = amount - fees`` in signed arithmetic — see the
    # docstring for the worked-through cases.
    gross = tx.amount - tx.fees
    lines.append(
        _align(
            _cash_account(prefix, tx.account_number, tx.counter_currency),
            _format_amount(tx.counter_amount),
            tx.counter_currency,
            extras=f" @@ {_format_amount(abs(gross))} {tx.currency}",
        )
    )

    if tx.transaction_number:
        lines.append(f"  no: {tx.transaction_number}")

    return "\n".join(lines) + "\n"


def render(result: ExtractionResult) -> str:
    """Render all transactions in ``result`` as beancount entries.

    Returns the empty string for doctypes in :data:`_NO_EMIT_TYPES`
    (currently ``FX_FORWARD``) — those documents are paper-trail only
    and have their cash leg booked elsewhere, so no header, no audit
    comments, and no transaction lines are emitted.
    """

    if result.classification.document_type in _NO_EMIT_TYPES:
        return ""

    prefix = _bank_prefix(result.classification)
    chunks = [_render_header(result)]
    for tx in result.transactions:
        chunks.append(
            _render_transaction(
                tx, result.classification.document_type, prefix
            )
        )
    return "\n".join(chunks)


def render_entry(tx: Transaction, classification: Classification) -> str:
    """Render a single ``Transaction`` as the corresponding beancount entry.

    Skips the file-level header comments (``; source:``, ``; classification:``,
    ``; bank:``) that :func:`render` prepends. Useful for golden-file tests
    that want to compare entry text directly without slicing past a header
    of variable length.

    Returns the empty string for doctypes in :data:`_NO_EMIT_TYPES`
    (currently ``FX_FORWARD``) — paper-trail documents whose cash leg
    is booked by a paired advice (``SETTLE_FX_FORWARD`` at maturity)
    and which therefore must not contribute any beancount output.
    """

    if classification.document_type in _NO_EMIT_TYPES:
        return ""
    return _render_transaction(
        tx, classification.document_type, _bank_prefix(classification)
    )


def _render_header(result: ExtractionResult) -> str:
    c = result.classification
    lines = [
        f"; source: {result.source_path}",
        f"; classification: {c.document_type} "
        f"(confidence={c.confidence:.2f}, source={c.source}, template={c.template_id})",
    ]
    if c.bank is not None:
        lines.append(
            f"; bank: {c.bank.bank} "
            f"(confidence={c.bank.confidence:.2f}, source={c.bank.source})"
        )
    for w in result.warnings:
        lines.append(f"; warning: {w}")
    return "\n".join(lines)


def _render_transaction(
    tx: Transaction, doc_type: DocumentType, prefix: str
) -> str:
    # Dispatch order: switches first (their shape is distinct enough to
    # need their own builder, even though they're also in the buy/sell
    # sets so ``render_open_directives`` finds their ISINs), then fee
    # advices (multi-leg per-component breakdown), then regular security
    # trades. Everything else falls through to the Jinja templates in
    # ``_TEMPLATES``.
    if doc_type in _SWITCH_TYPES:
        return _render_switch_trade(tx, doc_type, prefix)
    if doc_type in _BOND_TRADE_TYPES:
        return _render_bond_trade(tx, doc_type, prefix)
    if doc_type in _FEE_ADVICE_TYPES:
        return _render_fee_advice(tx, doc_type, prefix)
    if doc_type in _THIRD_PARTY_PAYMENT_TYPES:
        return _render_third_party_payment(tx, doc_type, prefix)
    if doc_type in _INTERNAL_TRANSFER_TYPES:
        return _render_internal_transfer(tx, doc_type, prefix)
    if doc_type in _FX_SETTLEMENT_TYPES:
        return _render_fx_settlement(tx, doc_type, prefix)
    if doc_type in _DIVIDEND_TYPES:
        return _render_dividend(tx, doc_type, prefix)
    if doc_type in _INTEREST_TYPES:
        return _render_interest(tx, doc_type, prefix)
    if doc_type in _INTEREST_TYPES:
        return _render_interest(tx, doc_type, prefix)
    if doc_type in _SECURITY_TRADE_TYPES:
        return _render_security_trade(tx, doc_type, prefix)
    template = _TEMPLATES.get(doc_type, _DEFAULT_TEMPLATE)
    return template.render(
        tx=tx,
        doc_type=doc_type,
        prefix=prefix,
        narration=_escape(tx.narration),
    )


def _escape(narration: str) -> str:
    return narration.replace("\\", "\\\\").replace('"', '\\"')


def render_all(results: Iterable[ExtractionResult]) -> str:
    results = list(results)
    chunks = [render_open_directives(results)]
    # Filter out empty renderings (no-emit doctypes like ``FX_FORWARD``)
    # so the joined output doesn't carry stray blank-line gaps where a
    # paper-trail document was suppressed.
    chunks.extend(c for c in (render(r) for r in results) if c)
    return "\n".join(chunks)


def render_open_directives(
    results: Iterable[ExtractionResult],
    open_date: datetime.date | None = None,
) -> str:
    """Return beancount ``open`` directives for every ISIN-based account seen.

    Call this once across all results so the generated ledger is
    self-contained. Both asset and income (dividend) account keys are
    ``(bank prefix, portfolio segment, ISIN)`` so the same ISIN held
    in two different portfolios at the same bank (e.g. ``P-…`` vs
    ``K-…`` Pictet portfolios) generates two distinct opens — the
    same dimensionality the per-trade postings carry.
    """
    if open_date is None:
        open_date = datetime.date(2020, 1, 1)
    date_str = open_date.isoformat()

    asset_accounts: dict[tuple[str, str, str], str] = {}
    income_accounts: dict[tuple[str, str, str], str] = {}

    for result in results:
        prefix = _bank_prefix(result.classification)
        doc_type = result.classification.document_type
        # No-emit doctypes contribute no postings to the ledger, so any
        # ISINs they happen to carry shouldn't drag account opens with
        # them either.
        if doc_type in _NO_EMIT_TYPES:
            continue
        for tx in result.transactions:
            if not tx.isin:
                continue
            isin = tx.isin
            commodity = isin  # beancount commodity == ISIN
            portfolio = _portfolio_segment(tx.account_number)
            if doc_type in _SECURITY_TRADE_TYPES:
                asset_accounts[(prefix, portfolio, isin)] = commodity
            elif doc_type == DocumentType.DIVIDEND_NOTICE:
                income_accounts[(prefix, portfolio, isin)] = commodity

    lines: list[str] = []
    for prefix, portfolio, isin in sorted(asset_accounts):
        lines.append(
            f"{date_str} open Assets:{prefix}:{portfolio}:{isin}  {isin}"
        )
    for prefix, portfolio, isin in sorted(income_accounts):
        lines.append(
            f"{date_str} open Income:{prefix}:{portfolio}:{isin}:Dividend"
        )

    return "\n".join(lines)


# Re-exported for convenience in callers that want the zero-amount shortcut.
ZERO = Decimal("0")
