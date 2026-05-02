"""Shared formatting primitives for the beancount writer.

These are the lowest-level helpers — amount formatting, posting alignment,
narration escaping, and the bank-prefix / portfolio-segment / cash-account
account-name builders. Per-shape builders compose these; nothing in here
depends on a specific render shape.
"""

from __future__ import annotations

from decimal import Decimal

from banking_pipeline.models import Classification, DocumentType, Transaction
from banking_pipeline.writer.profile import resolve_profile

# Right-edge column for amount values in postings. Beancount tolerates any
# alignment; this constant is what the project's golden ``.beancount`` files
# use, and matching it makes diff-based testing meaningful.
AMOUNT_COL = 59


# Doctypes that would emit an inline ``open Assets:<prefix>:<portfolio>:<ISIN> <ISIN>``
# directive at the top of their entry. Empty by design today — every
# account open is centralised in ``portfolio.beancount`` (via the
# ``banking-pipeline portfolio`` aggregate command), and emitting an
# inline open here would duplicate the central one and trip a
# duplicate-open error in ``bean-check``.
#
# The membership set is preserved (rather than removing the
# :func:`inline_open_directive` helper outright) so a future workflow
# that renders per-document beancount files independently of the
# central aggregate can opt back in by adding the relevant doctypes.
OPEN_EMITTING_TYPES: frozenset[DocumentType] = frozenset()


def bank_prefix(classification: Classification | None) -> str:
    """Resolve a bank prefix from a ``Classification``.

    Falls back to ``"Unknown"`` when the classification carries no bank
    (generic rules) or when the bank isn't in any registered profile.
    The fallback keeps the writer producing parseable beancount even on
    bank-agnostic test fixtures.
    """

    bank_id = (
        classification.bank.bank
        if classification is not None and classification.bank is not None
        else None
    )
    return resolve_profile(bank_id).account_prefix


def portfolio_segment(account_number: str | None) -> str:
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


def cash_account(prefix: str, account_number: str | None, currency: str) -> str:
    """Build a bank-prefixed cash-account path including the portfolio.

    Format: ``Assets:<prefix>:<portfolio>:<currency>`` — e.g.
    ``Assets:Pic:P999999999:GBP``. The portfolio segment lets users
    distinguish multiple Pictet accounts they hold within the same
    currency (e.g. ``P-…`` vs ``K-…`` portfolios that both have an EUR
    sub-account); without it beancount would treat them as the same
    bucket. The Pictet ID is sanitised through :func:`portfolio_segment`
    to drop the dash and period so the segment is valid beancount syntax.
    Falls back to ``Unknown`` when the document doesn't carry a portfolio
    identifier — that's rare for Pictet (every advice we see has an
    ``Account no.`` / ``N° de cuenta`` header), but the fallback keeps
    the writer producing parseable output even on malformed input.
    """

    return f"Assets:{prefix}:{portfolio_segment(account_number)}:{currency}"


def inline_open_directive(
    tx: Transaction, doc_type: DocumentType, prefix: str
) -> str:
    """Return the inline ``open`` directive line for advices that need
    one, or an empty string when they don't.

    Returns ``""`` for every doctype today: :data:`OPEN_EMITTING_TYPES`
    is the empty set because account opens are centralised in
    ``portfolio.beancount`` (via the ``banking-pipeline portfolio``
    aggregate command). Re-emitting an inline open here would
    duplicate the central declaration and trip a duplicate-open
    error when the per-year file is loaded alongside the aggregate.

    The function is preserved as a hook so a future workflow that
    renders standalone per-document beancount files (without the
    central aggregate) can opt back in by repopulating
    :data:`OPEN_EMITTING_TYPES`. Output format when active:
    ``<date> open Assets:<prefix>:<portfolio>:<ISIN> <ISIN>\\n`` with
    a trailing newline so callers can prepend it directly to their
    entry text without juggling separators.
    """

    if doc_type not in OPEN_EMITTING_TYPES:
        return ""
    if not tx.isin:
        return ""
    entry_date = tx.booking_date or tx.trade_date
    portfolio = portfolio_segment(tx.account_number)
    return f"{entry_date} open Assets:{prefix}:{portfolio}:{tx.isin} {tx.isin}\n"


def format_amount(value: Decimal) -> str:
    """Format a ``Decimal`` for emission inside a beancount posting.

    Returns the canonical decimal string — no thousands separators, sign as
    stored. Beancount accepts arbitrary precision, so we don't normalise the
    number of decimals; the extractor preserved whatever the source PDF
    printed and we honour that.
    """

    return str(value)


def align(account: str, amount: str, currency: str, extras: str = "") -> str:
    """Build a posting line with the amount right-aligned at :data:`AMOUNT_COL`.

    Produces ``  <account><pad><amount> <currency><extras>`` where ``pad`` is
    the number of spaces that places the rightmost digit of ``amount`` at
    column :data:`AMOUNT_COL` (zero-indexed end position). When the account
    name is so long that even one space of padding would push the amount
    past the column, we fall back to a single space — the entry stays valid
    beancount, it just doesn't line up.
    """

    prefix = f"  {account}"
    pad = AMOUNT_COL - len(prefix) - len(amount)
    if pad < 1:
        pad = 1
    return f"{prefix}{' ' * pad}{amount} {currency}{extras}"


def escape(narration: str) -> str:
    """Escape a narration string for inclusion inside beancount double quotes."""

    return narration.replace("\\", "\\\\").replace('"', '\\"')


def header_line(tx: Transaction, *, link: str | None = None) -> str:
    """Return the ``<date> * "<title>" "<narration>"`` opening line.

    When ``tx.title`` is set the entry uses beancount's two-string
    payee+narration form; otherwise it carries a single narration string.
    An optional trailing ``^<link>`` is appended when ``link`` is non-empty.
    """

    entry_date = tx.booking_date or tx.trade_date
    parts: list[str] = [str(entry_date), "*"]
    if tx.title:
        parts.append(f'"{escape(tx.title)}"')
    parts.append(f'"{escape(tx.narration)}"')
    if link:
        parts.append(f"^{link}")
    return " ".join(parts)


def transaction_number_comment(tx: Transaction) -> str | None:
    """The ``  no: <number>`` trailing comment, or ``None`` when absent."""

    if not tx.transaction_number:
        return None
    return f"  no: {tx.transaction_number}"
