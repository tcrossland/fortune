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

# Short account-name prefix per bank — used in ``Assets:<prefix>:<ISIN>``,
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
    DocumentType.BUY_STRUCTURED_PRODUCTS,
    DocumentType.BUY_ETF,
    DocumentType.COMPRA,
    DocumentType.SUSCRIPCION,
    DocumentType.SWITCH_ENTRADA,
})

_SECURITY_SELL_TYPES: frozenset[DocumentType] = frozenset({
    DocumentType.FINAL_REDEMPTION,
    DocumentType.REDEMPTION_NOTICE,
    DocumentType.REEMBOLSO,
    DocumentType.SWITCH_SALIDA,
})

_SECURITY_TRADE_TYPES = _SECURITY_BUY_TYPES | _SECURITY_SELL_TYPES


_FEE_TEMPLATE = _ENV.from_string(
    """\
{{ tx.trade_date }} * "{{ narration }}"
  Expenses:Banking:Fees                          {{ tx.amount }} {{ tx.currency }}
  Assets:Bank:{{ tx.account_number or 'Unknown' }}  {{ -tx.amount }} {{ tx.currency }}
"""
)

_INTEREST_TEMPLATE = _ENV.from_string(
    """\
{{ tx.trade_date }} * "{{ narration }}"
  Income:Interest                                {{ -tx.amount }} {{ tx.currency }}
  Assets:Bank:{{ tx.account_number or 'Unknown' }}  {{ tx.amount }} {{ tx.currency }}
"""
)

_CASH_IN_TEMPLATE = _ENV.from_string(
    """\
{{ tx.trade_date }} * "{{ narration }}"
  Assets:Broker:Cash                             {{ tx.amount }} {{ tx.currency }}
  Equity:Uncategorized                           {{ -tx.amount }} {{ tx.currency }}
"""
)

_CASH_OUT_TEMPLATE = _ENV.from_string(
    """\
{{ tx.trade_date }} * "{{ narration }}"
  Equity:Uncategorized                           {{ tx.amount }} {{ tx.currency }}
  Assets:Broker:Cash                             {{ -tx.amount }} {{ tx.currency }}
"""
)

# Single cash leg of an FX trade or internal transfer. Two of these per
# document balance each other when viewed together; each is valid alone.
_FX_LEG_TEMPLATE = _ENV.from_string(
    """\
{{ tx.trade_date }} * "{{ narration }}"
  Assets:Broker:Cash                             {{ tx.amount }} {{ tx.currency }}
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
    DocumentType.DIVIDEND_NOTICE: _ENV.from_string(
        """\
{{ tx.trade_date }} * "{{ narration }}"
  Income:Dividends:{{ tx.isin or 'Unknown' }}    {{ -tx.amount }} {{ tx.currency }}
  Assets:Broker:Cash                             {{ tx.amount }} {{ tx.currency }}
"""
    ),
    # --- Interest ---
    DocumentType.INTEREST_NOTICE: _INTEREST_TEMPLATE,
    DocumentType.INTEREST_PAYMENT: _INTEREST_TEMPLATE,
    DocumentType.INTEREST_SCALE: _INTEREST_TEMPLATE,
    # --- Fees ---
    DocumentType.FEE_NOTICE: _FEE_TEMPLATE,
    DocumentType.DEBIT_OF_FEES: _FEE_TEMPLATE,
    DocumentType.DEBITO_DE_GASTOS: _FEE_TEMPLATE,
    # FACTURA stores amount as -gross_total (already negative), so flip signs
    # vs the standard fee template to get Expenses positive, Cash negative.
    DocumentType.FACTURA: _ENV.from_string(
        """\
{{ tx.trade_date }} * "{{ narration }}"
  Expenses:Banking:Fees                          {{ -tx.amount }} {{ tx.currency }}
  Assets:Bank:{{ tx.account_number or 'Unknown' }}  {{ tx.amount }} {{ tx.currency }}
"""
    ),
    # --- Cash movements ---
    DocumentType.INCOMING_PAYMENT: _CASH_IN_TEMPLATE,
    DocumentType.PAGO_INTERNA: _CASH_IN_TEMPLATE,
    DocumentType.PAYMENT: _CASH_OUT_TEMPLATE,
    # --- FX and internal transfers (one Transaction per leg) ---
    DocumentType.SPOT: _FX_LEG_TEMPLATE,
    DocumentType.SETTLE_FX_FORWARD: _FX_LEG_TEMPLATE,
    DocumentType.INTERNAL_TRANSFER: _FX_LEG_TEMPLATE,
    # FX forward opening: zero-amount legs record the contract event.
    DocumentType.FX_FORWARD: _FX_LEG_TEMPLATE,
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
  Assets:Bank:{{ tx.account_number or 'Unknown' }}  {{ tx.amount }} {{ tx.currency }}
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


def _render_security_trade(
    tx: Transaction, doc_type: DocumentType, prefix: str
) -> str:
    """Render a security buy/sell as a multi-posting beancount entry.

    Layout (FX trade, with all fields populated)::

        <booking_date> * "<title>" "<narration>"
          Assets:<prefix>:<ISIN>          <qty> <ISIN> {<price> <sec_ccy>}
          Expenses:<prefix>:Fees:<sec_ccy>  <fees>      <sec_ccy>
          Assets:<prefix>:<currency>      <amount>     <ccy> @@ <subtotal> <sec_ccy>
          no: <transaction_number>

    Non-FX trades omit the ``Expenses:...:Fees`` leg (Pictet rolls fees
    into the cash net amount on those advices) and the ``@@`` annotation.
    Sells reverse the asset/cash posting order so the value-receiving
    account is always listed first.

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

    sec_ccy = tx.security_currency or tx.currency

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
    isin = tx.isin or "Unknown"
    qty_str = _format_amount(tx.quantity) if tx.quantity is not None else "0"
    cost_basis = (
        f" {{{_format_amount(tx.price)} {sec_ccy}}}"
        if tx.price is not None
        else ""
    )
    asset_line = _align(
        f"Assets:{prefix}:{isin}", qty_str, isin, extras=cost_basis
    )

    # --- Fees leg (FX only) --------------------------------------------
    # Non-FX advices carry a ``Costes EUR 0.00`` line that we extract as
    # ``tx.fees == 0`` — skip emission in that case to avoid noise legs.
    fees_line: str | None = None
    if tx.is_fx and tx.fees is not None and tx.fees != 0:
        fees_ccy = tx.fees_currency or sec_ccy
        fees_line = _align(
            f"Expenses:{prefix}:Fees:{fees_ccy}",
            _format_amount(abs(tx.fees)),
            fees_ccy,
        )

    # --- Cash leg -------------------------------------------------------
    cash_extras = ""
    if tx.is_fx and tx.subtotal_security is not None:
        cash_extras = (
            f" @@ {_format_amount(abs(tx.subtotal_security))} {sec_ccy}"
        )
    cash_line = _align(
        f"Assets:{prefix}:{tx.currency}",
        _format_amount(tx.amount),
        tx.currency,
        extras=cash_extras,
    )

    # --- Posting order: asset-first for buys, cash-first for sells -----
    if doc_type in _SECURITY_BUY_TYPES:
        lines.append(asset_line)
        if fees_line is not None:
            lines.append(fees_line)
        lines.append(cash_line)
    else:
        lines.append(cash_line)
        if fees_line is not None:
            lines.append(fees_line)
        lines.append(asset_line)

    # --- Trailing reference comment ------------------------------------
    if tx.transaction_number:
        lines.append(f"  no: {tx.transaction_number}")

    return "\n".join(lines) + "\n"


def render(result: ExtractionResult) -> str:
    """Render all transactions in ``result`` as beancount entries."""

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
    """

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
    # Security trades go through the Python builder — see
    # ``_render_security_trade`` for why. Everything else continues to use
    # Jinja templates from ``_TEMPLATES``.
    if doc_type in _SECURITY_TRADE_TYPES:
        return _render_security_trade(tx, doc_type, prefix)
    template = _TEMPLATES.get(doc_type, _DEFAULT_TEMPLATE)
    return template.render(tx=tx, doc_type=doc_type, narration=_escape(tx.narration))


def _escape(narration: str) -> str:
    return narration.replace("\\", "\\\\").replace('"', '\\"')


def render_all(results: Iterable[ExtractionResult]) -> str:
    results = list(results)
    chunks = [render_open_directives(results)]
    chunks.extend(render(r) for r in results)
    return "\n".join(chunks)


def render_open_directives(
    results: Iterable[ExtractionResult],
    open_date: datetime.date | None = None,
) -> str:
    """Return beancount ``open`` directives for every ISIN-based account seen.

    Call this once across all results so the generated ledger is
    self-contained. Accounts are keyed on (bank prefix, ISIN) — accounts
    for the same ISIN held at different banks are tracked separately
    because each bank's holdings live under its own prefix
    (``Assets:Pic:LU…`` vs e.g. ``Assets:Cs:LU…``).
    """
    if open_date is None:
        open_date = datetime.date(2020, 1, 1)
    date_str = open_date.isoformat()

    # Keys are ``(prefix, isin)`` so the same ISIN held at two different
    # banks generates two distinct opens.
    asset_accounts: dict[tuple[str, str], str] = {}
    income_accounts: dict[tuple[str, str], str] = {}

    for result in results:
        prefix = _bank_prefix(result.classification)
        doc_type = result.classification.document_type
        for tx in result.transactions:
            if not tx.isin:
                continue
            isin = tx.isin
            commodity = isin  # beancount commodity == ISIN
            if doc_type in _SECURITY_TRADE_TYPES:
                asset_accounts[(prefix, isin)] = commodity
            elif doc_type == DocumentType.DIVIDEND_NOTICE:
                income_accounts[(prefix, isin)] = commodity

    lines: list[str] = []
    for prefix, isin in sorted(asset_accounts):
        lines.append(f"{date_str} open Assets:{prefix}:{isin}  {isin}")
    for prefix, isin in sorted(income_accounts):
        lines.append(f"{date_str} open Income:{prefix}:Dividends:{isin}")

    return "\n".join(lines)


# Re-exported for convenience in callers that want the zero-amount shortcut.
ZERO = Decimal("0")
