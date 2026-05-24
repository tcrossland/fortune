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
import re
from collections.abc import Callable, Iterable, Iterator
from decimal import Decimal

from banking_pipeline.models import (
    NO_OUTPUT_DOCTYPES as NO_EMIT_TYPES,
)
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
    render_transfer_in,
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
from banking_pipeline.writer.builders.transfer_in import TRANSFER_IN_TYPES
from banking_pipeline.writer.format import (
    bank_prefix,
    portfolio_segment,
    withholding_account,
)

# NO_EMIT_TYPES is the writer's local alias for
# :data:`banking_pipeline.models.NO_OUTPUT_DOCTYPES`. The same set
# also gates the extractor's "template returned [] is expected here"
# branch in :class:`banking_pipeline.fields.hybrid.HybridExtractor`,
# so the "this doctype produces no transactions / no beancount
# output" rule has a single source of truth.


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


def render_all(results: Iterable[ExtractionResult], *, close_zeroed: bool = True) -> str:
    """Render a batch of results, prepending a single ``open`` directive block.

    When ``close_zeroed`` is true (the default), append a ``close`` directive
    for every ISIN-keyed asset account whose final units balance across the
    batch is exactly zero. The close date is the day after the last posting
    that touched the account so beancount's exclusive-end-date semantics
    align with "no postings starting tomorrow." Positions that go to zero
    and later re-open within the same batch are deliberately *not* closed —
    that would invalidate the later buy. See :func:`render_close_directives`.

    Set ``close_zeroed=False`` to keep the legacy output (open + entries
    only) — useful when consuming downstream tools that don't tolerate
    extra ``close`` directives, or when the batch is a partial slice of
    history and a position that's zero now might re-open in a later run.
    """

    results = list(results)
    chunks = [render_open_directives(results)]
    # Filter out empty renderings (no-emit doctypes like ``FX_FORWARD``)
    # so the joined output doesn't carry stray blank-line gaps where a
    # paper-trail document was suppressed.
    chunks.extend(c for c in (render(r) for r in results) if c)
    rendered = "\n".join(chunks)
    if close_zeroed:
        closes = render_close_directives(rendered)
        if closes:
            rendered = f"{rendered}\n\n{closes}"
    return rendered


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
    # Foreign withholding-tax accounts are keyed only by their resolved
    # account string (``Expenses:Tax:Withholding:<country>``): they carry
    # no commodity constraint and aren't per-ISIN, so a set suffices.
    wht_accounts: set[str] = set()

    for result in results:
        prefix = bank_prefix(result.classification)
        doc_type = result.classification.document_type
        # No-emit doctypes contribute no postings to the ledger, so any
        # ISINs they happen to carry shouldn't drag account opens with
        # them either.
        if doc_type in NO_EMIT_TYPES:
            continue
        for tx in result.transactions:
            # WHT accounts attach to the income event, not to an ISIN
            # asset account, so collect them before the ISIN guard.
            if tx.withholding_tax is not None and tx.withholding_country:
                wht_accounts.add(
                    withholding_account(prefix, tx.withholding_country)
                )
            if not tx.isin:
                continue
            isin = tx.isin
            commodity = isin  # beancount commodity == ISIN
            portfolio = portfolio_segment(tx.account_number)
            if doc_type in SECURITY_TRADE_TYPES:
                asset_accounts[(prefix, portfolio, isin)] = commodity
            elif doc_type in TRANSFER_IN_TYPES:
                # Transfer-in advices open a new ISIN-keyed asset
                # account just like a security buy — the position
                # arrives with a cost basis and lives in the same
                # ``Assets:<prefix>:<portfolio>:<ISIN>`` slot a
                # subsequent buy of the same ISIN would land in.
                asset_accounts[(prefix, portfolio, isin)] = commodity
            elif doc_type in DIVIDEND_TYPES:
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
    for account in sorted(wht_accounts):
        lines.append(f"{date_str} open {account}")

    return "\n".join(lines)


# ISO 6166: 2 letters + 9 alphanumerics + 1 digit. We don't run the full
# checksum here because the writer already validated ISINs upstream — this
# regex's only job is to distinguish ISIN-keyed asset accounts (whose
# commodity == ISIN) from cash/fee/income accounts (commodity == ISO-4217
# fiat code). Any 12-char [A-Z0-9] commodity is treated as an ISIN; the
# narrower fiat codes (3 letters) won't match.
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")

# A line that opens a directive: ``YYYY-MM-DD`` followed by anything
# (transaction flag, ``open``, ``close``, ``price`` …). We only need the
# date; postings that follow inherit it as their entry date.
_DIRECTIVE_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\b")

# A posting line: indented, an account beginning with one of beancount's
# five root types, then a signed number and its commodity. ``format.align``
# emits ``  <account><pad><amount> <currency><extras>`` with no thousands
# separators (see ``format.format_amount``), so the number is a bare
# ``-?\d+(\.\d+)?``. Posting *metadata* lines (e.g. ``    gbp-rate: "…"``)
# never start with a root type, so they don't match — exactly what we want,
# since metadata carries no units.
_POSTING_RE = re.compile(
    r"^\s+"
    r"(?P<account>(?:Assets|Liabilities|Equity|Income|Expenses)(?::[A-Za-z0-9][A-Za-z0-9-]*)+)"
    r"\s+(?P<number>-?\d+(?:\.\d+)?)"
    r"\s+(?P<commodity>[A-Z][A-Z0-9]*)"
)


def render_close_directives(rendered: str) -> str:
    """Return ``close`` directives for ISIN asset accounts that net to zero.

    Scans ``rendered`` line by line for units-bearing postings — the
    project's own writer is the only producer of this text, so its format
    is fixed and a regex parse is sufficient and keeps us clear of the
    ``beancount`` (GPL-2.0) import the rest of the pipeline avoids (see
    ``bean_check.py`` for the rationale). If an account holds zero units of
    its ISIN commodity once every posting is summed, we close it. Anything
    we can't parse is simply not counted — better to under-close than to
    emit a directive that would break ``bean-check``.

    The close date is the day after the last posting that touched the
    account. Beancount's ``close`` is exclusive of the asserted day, so
    ``last_date + 1`` reads as "no postings on or after the day after the
    final sell" — which is exactly the situation when the position has
    been fully wound down on the previous day.

    No assumption is made about ``open`` directives being present in
    ``rendered``. ISIN accounts are identified by scanning **posting
    units** for ISIN-shaped commodities, so this works equally well on
    self-contained :func:`render_all` output (has opens) and on partial
    ingest output (no opens).

    Returns the empty string when no qualifying accounts exist (e.g. a
    cash-only batch, or every ISIN position still has open units).
    """

    return _close_directives_from_postings(_parse_postings(rendered))


def _parse_postings(
    rendered: str,
) -> Iterator[tuple[datetime.date, str, Decimal, str]]:
    """Yield ``(date, account, units, commodity)`` for each posting in text.

    A directive-opening line (``YYYY-MM-DD …``) sets the date that the
    indented posting lines beneath it inherit; ``open`` / ``close`` /
    ``price`` directives set a date but carry no posting lines, so they
    contribute nothing. See :data:`_DIRECTIVE_DATE_RE` / :data:`_POSTING_RE`.
    """

    current_date: datetime.date | None = None
    for line in rendered.splitlines():
        date_match = _DIRECTIVE_DATE_RE.match(line)
        if date_match:
            current_date = datetime.date(
                int(date_match[1]), int(date_match[2]), int(date_match[3])
            )
            continue
        if current_date is None:
            continue
        posting_match = _POSTING_RE.match(line)
        if posting_match:
            yield (
                current_date,
                posting_match["account"],
                Decimal(posting_match["number"]),
                posting_match["commodity"],
            )


def _close_directives_from_postings(
    postings: Iterable[tuple[datetime.date, str, Decimal | None, str]],
) -> str:
    """Pure-Python core of :func:`render_close_directives`.

    Takes ``(date, account, units, currency)`` tuples; sums units per
    account for ISIN-shaped currencies; emits close directives for
    accounts whose final balance is exactly zero.

    Cash postings, fee postings, dividend income — all use 3-letter
    fiat codes and are skipped automatically by the ISIN regex filter.
    """

    balances: dict[str, Decimal] = {}
    last_dates: dict[str, datetime.date] = {}
    for entry_date, account, units, currency in postings:
        if units is None or not _ISIN_RE.match(currency):
            continue
        balances[account] = balances.get(account, Decimal(0)) + units
        prev = last_dates.get(account)
        if prev is None or entry_date > prev:
            last_dates[account] = entry_date

    closes: list[tuple[datetime.date, str]] = []
    for account, bal in balances.items():
        if bal != 0:
            continue
        last = last_dates.get(account)
        if last is None:  # Defensive — bal != 0 was the only branch out.
            continue
        closes.append((last + datetime.timedelta(days=1), account))

    closes.sort()
    return "\n".join(f"{d.isoformat()} close {a}" for d, a in closes)


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


# Builder signature: ``(tx, doc_type, prefix) -> str``. Every per-shape
# builder in :mod:`banking_pipeline.writer.builders` matches this shape,
# which lets the dispatch table treat them uniformly.
Builder = Callable[[Transaction, DocumentType, str], str]


# ``SECURITY_TRADE_TYPES`` is a deliberate *tag set* — it carries the
# union of every doctype whose ``Transaction`` may be ISIN-bearing,
# which :func:`render_open_directives` reads to emit account ``open``
# directives. That union legitimately overlaps with ``SWITCH_TYPES``
# (switches are buys/sells at the security-leg level) and
# ``BOND_TRADE_TYPES`` (bonds are too).
#
# For *dispatch*, we want a partition — exactly one builder per
# doctype — so the duplicate-doctype assertion below can be exact and
# the if/elif ordering doesn't have to encode "switches/bonds first".
# This is the security-trade builder's domain after subtracting the
# overlapping families that have their own dedicated builders.
_DISPATCH_SECURITY_TRADE_TYPES: frozenset[DocumentType] = (
    SECURITY_TRADE_TYPES - SWITCH_TYPES - BOND_TRADE_TYPES
)


# Doctype-set → builder, as a tuple to preserve declaration order. With
# the partition above, no doctype appears in two entries and dispatch
# order doesn't matter — the table is iterated linearly and the first
# (only) match wins. The ordering is kept stable for readability and
# so error messages list builders in the same order each run.
_DISPATCH_TABLE: tuple[tuple[frozenset[DocumentType], Builder], ...] = (
    (SWITCH_TYPES, render_switch_trade),
    (BOND_TRADE_TYPES, render_bond_trade),
    (FEE_ADVICE_TYPES, render_fee_advice),
    (THIRD_PARTY_PAYMENT_TYPES, render_third_party_payment),
    (INTERNAL_TRANSFER_TYPES, render_internal_transfer),
    (FX_SETTLEMENT_TYPES, render_fx_settlement),
    (TRANSFER_IN_TYPES, render_transfer_in),
    (DIVIDEND_TYPES, render_dividend),
    (INTEREST_TYPES, render_interest),
    (_DISPATCH_SECURITY_TRADE_TYPES, render_security_trade),
    (LIMIT_EXTENSION_TYPES, render_limit_extension),
)


def _validate_dispatch_table() -> dict[DocumentType, Builder]:
    """Build the doctype → builder index, asserting partition invariants.

    Two checks at module-import time:

    1. **No doctype is dispatched by two builders.** This catches the
       ``_INTEREST_TYPES`` / copy-paste class of bug that lived in the
       old if/elif chain unnoticed (the second branch was unreachable
       and silently masked typos). The error message names both
       offending builders.
    2. **No dispatched doctype is also in :data:`NO_EMIT_TYPES`.**
       ``NO_EMIT_TYPES`` short-circuits at the top of :func:`render`
       and :func:`render_entry` before dispatch fires; a doctype in
       both would mean the dispatch entry is dead code, almost always
       a typo.

    Both errors raise :class:`AssertionError` at import time so the
    test suite catches them on the first run, not on the next
    document of that type.
    """

    def _qualify(builder: Builder) -> str:
        # Each per-shape builder module exports its public function as
        # ``render`` (aliased to ``render_<shape>`` only at the
        # ``writer.builders`` package boundary), so ``__name__`` alone
        # would print "render and render" and tell the maintainer
        # nothing. Qualifying with ``__module__`` gives
        # ``banking_pipeline.writer.builders.dividend.render`` vs
        # ``banking_pipeline.writer.builders.interest.render`` —
        # immediately actionable.
        return f"{builder.__module__}.{builder.__name__}"

    index: dict[DocumentType, Builder] = {}
    for types, builder in _DISPATCH_TABLE:
        for doctype in types:
            existing = index.get(doctype)
            if existing is not None:
                raise AssertionError(
                    f"Document type {doctype.value!r} is dispatched by "
                    f"both {_qualify(existing)} and {_qualify(builder)}. "
                    "Each doctype must route to exactly one builder. "
                    "If the overlap is legitimate (e.g. a doctype that "
                    "belongs to a tag set like SECURITY_TRADE_TYPES "
                    "but also has a more specific builder), define a "
                    "dispatch-only subset and use that in _DISPATCH_TABLE."
                )
            index[doctype] = builder
    overlap = NO_EMIT_TYPES & index.keys()
    if overlap:
        raise AssertionError(
            f"Document type(s) {sorted(d.value for d in overlap)!r} "
            "appear in both NO_EMIT_TYPES and a dispatch-table entry. "
            "NO_EMIT short-circuits before dispatch — the dispatch "
            "entry would be dead code. Remove from one of them."
        )
    return index


# Module-import-time validation. A duplicate or NO_EMIT collision
# fails fast here rather than waiting for a doctype-specific test or
# (worse) a silent rendering bug at runtime.
_DOCTYPE_TO_BUILDER: dict[DocumentType, Builder] = _validate_dispatch_table()


def _render_transaction(
    tx: Transaction, doc_type: DocumentType, prefix: str
) -> str:
    """Pick a builder for ``doc_type`` and render ``tx``.

    Looks up :data:`_DOCTYPE_TO_BUILDER` (built at import time from
    :data:`_DISPATCH_TABLE` and validated for partition / NO_EMIT
    invariants). Doctypes not in any dispatch set fall through to
    :func:`banking_pipeline.writer.builders.fallback.render`, which
    emits a parseable ``Equity:Uncategorized``-balanced entry with a
    ``TODO review`` audit comment so the user notices the unmapped
    doctype on next ``bean-check`` / Fava load.
    """

    builder = _DOCTYPE_TO_BUILDER.get(doc_type, render_fallback)
    return builder(tx, doc_type, prefix)
