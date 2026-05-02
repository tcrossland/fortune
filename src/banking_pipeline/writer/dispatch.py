"""Top-level rendering dispatcher.

The entry points :func:`render`, :func:`render_entry`, :func:`render_all`,
and :func:`render_open_directives` route :class:`~banking_pipeline.models.Transaction`
objects to the per-shape builder under
:mod:`banking_pipeline.writer.builders` indicated by the document's
:class:`~banking_pipeline.models.DocumentType`. Doctypes the dispatcher
doesn't recognise fall through to
:func:`banking_pipeline.writer.builders.fallback.render`, which emits a
parseable but ``Equity:Uncategorized``-balanced entry with a ``TODO
review`` audit comment.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable

from banking_pipeline.models import (
    Classification,
    DocumentType,
    ExtractionResult,
    Transaction,
)
from banking_pipeline.writer.builders import (
    render_bond_trade,
    render_dividend,
    render_fallback,
    render_fee_advice,
    render_fx_settlement,
    render_interest,
    render_internal_transfer,
    render_limit_extension,
    render_security_trade,
    render_switch_trade,
    render_third_party_payment,
)
from banking_pipeline.writer.builders.bond_trade import BOND_TRADE_TYPES
from banking_pipeline.writer.builders.dividend import DIVIDEND_TYPES
from banking_pipeline.writer.builders.fee_advice import FEE_ADVICE_TYPES
from banking_pipeline.writer.builders.fx_settlement import FX_SETTLEMENT_TYPES
from banking_pipeline.writer.builders.interest import INTEREST_TYPES
from banking_pipeline.writer.builders.internal_transfer import (
    INTERNAL_TRANSFER_TYPES,
)
from banking_pipeline.writer.builders.limit_extension import (
    LIMIT_EXTENSION_TYPES,
)
from banking_pipeline.writer.builders.payment import THIRD_PARTY_PAYMENT_TYPES
from banking_pipeline.writer.builders.security_trade import (
    SECURITY_TRADE_TYPES,
)
from banking_pipeline.writer.builders.switch_trade import SWITCH_TYPES
from banking_pipeline.writer.format import bank_prefix, portfolio_segment

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
NO_EMIT_TYPES: frozenset[DocumentType] = frozenset({
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


def render(result: ExtractionResult) -> str:
    """Render all transactions in ``result`` as beancount entries.

    Returns the empty string for doctypes in :data:`NO_EMIT_TYPES`
    (currently ``FX_FORWARD`` plus all monthly/quarterly/annual
    statements) — those documents are paper-trail only and have their
    cash leg booked elsewhere, so no header, no audit comments, and
    no transaction lines are emitted.
    """

    if result.classification.document_type in NO_EMIT_TYPES:
        return ""

    prefix = bank_prefix(result.classification)
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

    Returns the empty string for doctypes in :data:`NO_EMIT_TYPES` —
    paper-trail documents whose cash leg is booked by a paired advice
    (``SETTLE_FX_FORWARD`` at maturity) and which therefore must not
    contribute any beancount output.
    """

    if classification.document_type in NO_EMIT_TYPES:
        return ""
    return _render_transaction(
        tx, classification.document_type, bank_prefix(classification)
    )


def render_all(results: Iterable[ExtractionResult]) -> str:
    """Render a batch of results, prepending a single ``open`` directive block."""

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
        prefix = bank_prefix(result.classification)
        doc_type = result.classification.document_type
        # No-emit doctypes contribute no postings to the ledger, so any
        # ISINs they happen to carry shouldn't drag account opens with
        # them either.
        if doc_type in NO_EMIT_TYPES:
            continue
        for tx in result.transactions:
            if not tx.isin:
                continue
            isin = tx.isin
            commodity = isin  # beancount commodity == ISIN
            portfolio = portfolio_segment(tx.account_number)
            if doc_type in SECURITY_TRADE_TYPES:
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
    """Pick a builder and render ``tx``.

    Dispatch order: switches first (their shape is distinct enough to
    need their own builder, even though they're also in the buy/sell
    sets so :func:`render_open_directives` finds their ISINs), then
    bond trades (extra accrued-interest leg), then fee advices
    (multi-leg per-component breakdown), then payments / internal
    transfers / FX settlements, then dividends / interest, then
    regular security trades, then non-cash limit-extension advices.
    Everything else falls through to
    :func:`banking_pipeline.writer.builders.fallback.render`, which
    emits a parseable ``Equity:Uncategorized``-balanced entry with a
    ``TODO review`` audit comment.
    """

    if doc_type in SWITCH_TYPES:
        return render_switch_trade(tx, doc_type, prefix)
    if doc_type in BOND_TRADE_TYPES:
        return render_bond_trade(tx, doc_type, prefix)
    if doc_type in FEE_ADVICE_TYPES:
        return render_fee_advice(tx, doc_type, prefix)
    if doc_type in THIRD_PARTY_PAYMENT_TYPES:
        return render_third_party_payment(tx, doc_type, prefix)
    if doc_type in INTERNAL_TRANSFER_TYPES:
        return render_internal_transfer(tx, doc_type, prefix)
    if doc_type in FX_SETTLEMENT_TYPES:
        return render_fx_settlement(tx, doc_type, prefix)
    if doc_type in DIVIDEND_TYPES:
        return render_dividend(tx, doc_type, prefix)
    if doc_type in INTEREST_TYPES:
        return render_interest(tx, doc_type, prefix)
    if doc_type in SECURITY_TRADE_TYPES:
        return render_security_trade(tx, doc_type, prefix)
    if doc_type in LIMIT_EXTENSION_TYPES:
        return render_limit_extension(tx, doc_type, prefix)
    return render_fallback(tx, doc_type, prefix)
