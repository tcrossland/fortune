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

from banking_pipeline.models import DocumentType, ExtractionResult, Transaction

# NOTE: do NOT enable ``trim_blocks`` / ``lstrip_blocks`` here. The buy/sell
# security templates carry an inline ``{% if tx.price %}...{% endif %}`` at
# end of the asset-posting line; with ``trim_blocks=True`` Jinja swallows the
# newline that follows ``{% endif %}``, collapsing the cash leg onto the
# asset-posting line and producing output that bean-check rejects. None of
# the other templates have block tags, so leaving these defaults off is a
# strict improvement.
_ENV = Environment()

# One template per document type keeps postings clean. Adjust the account
# strings in examples/accounts.beancount to match your own chart of accounts.
_BUY_SECURITY_TEMPLATE = _ENV.from_string(
    """\
{{ tx.trade_date }} * "{{ narration }}"
  Assets:Investments:{{ tx.isin or 'Unknown' }}  {{ tx.quantity or '' }} {{ tx.isin or '' }} {% if tx.price %}{ {{ tx.price }} {{ tx.currency }} }{% endif %}
  Assets:Broker:Cash                             {{ -tx.amount }} {{ tx.currency }}
"""
)

_SELL_SECURITY_TEMPLATE = _ENV.from_string(
    """\
{{ tx.trade_date }} * "{{ narration }}"
  Assets:Broker:Cash                             {{ tx.amount }} {{ tx.currency }}
  Assets:Investments:{{ tx.isin or 'Unknown' }}  {{ tx.quantity or '' }} {{ tx.isin or '' }} {% if tx.price %}{ {{ tx.price }} {{ tx.currency }} }{% endif %}
"""
)

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
    DocumentType.TRADE_CONFIRMATION: _BUY_SECURITY_TEMPLATE,
    DocumentType.SUBSCRIPTION_NOTICE: _BUY_SECURITY_TEMPLATE,
    DocumentType.BUY_STRUCTURED_PRODUCTS: _BUY_SECURITY_TEMPLATE,
    DocumentType.BUY_ETF: _BUY_SECURITY_TEMPLATE,
    DocumentType.COMPRA: _BUY_SECURITY_TEMPLATE,
    DocumentType.SUSCRIPCION: _BUY_SECURITY_TEMPLATE,
    DocumentType.SWITCH_ENTRADA: _BUY_SECURITY_TEMPLATE,
    DocumentType.FINAL_REDEMPTION: _SELL_SECURITY_TEMPLATE,
    DocumentType.REDEMPTION_NOTICE: _SELL_SECURITY_TEMPLATE,
    DocumentType.REEMBOLSO: _SELL_SECURITY_TEMPLATE,
    DocumentType.SWITCH_SALIDA: _SELL_SECURITY_TEMPLATE,
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


def render(result: ExtractionResult) -> str:
    """Render all transactions in ``result`` as beancount entries."""

    chunks = [_render_header(result)]
    for tx in result.transactions:
        chunks.append(_render_transaction(tx, result.classification.document_type))
    return "\n".join(chunks)


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


def _render_transaction(tx: Transaction, doc_type: DocumentType) -> str:
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

    Call this once across all results so the generated ledger is self-contained.
    Accounts for ISINs that appear in multiple results are deduplicated.
    """
    if open_date is None:
        open_date = datetime.date(2020, 1, 1)
    date_str = open_date.isoformat()

    asset_accounts: dict[str, str] = {}   # isin -> commodity
    income_accounts: dict[str, str] = {}  # isin -> commodity

    for result in results:
        doc_type = result.classification.document_type
        for tx in result.transactions:
            if not tx.isin:
                continue
            isin = tx.isin
            commodity = isin  # beancount commodity == ISIN
            if doc_type in (
                DocumentType.TRADE_CONFIRMATION,
                DocumentType.SUBSCRIPTION_NOTICE,
                DocumentType.BUY_STRUCTURED_PRODUCTS,
                DocumentType.BUY_ETF,
                DocumentType.COMPRA,
                DocumentType.SUSCRIPCION,
                DocumentType.SWITCH_ENTRADA,
                DocumentType.FINAL_REDEMPTION,
                DocumentType.REDEMPTION_NOTICE,
                DocumentType.REEMBOLSO,
                DocumentType.SWITCH_SALIDA,
            ):
                asset_accounts[isin] = commodity
            elif doc_type == DocumentType.DIVIDEND_NOTICE:
                income_accounts[isin] = commodity

    lines: list[str] = []
    for isin in sorted(asset_accounts):
        lines.append(f"{date_str} open Assets:Investments:{isin}  {isin}")
    for isin in sorted(income_accounts):
        lines.append(f"{date_str} open Income:Dividends:{isin}")

    return "\n".join(lines)


# Re-exported for convenience in callers that want the zero-amount shortcut.
ZERO = Decimal("0")
