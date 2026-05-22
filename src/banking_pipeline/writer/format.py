"""Shared formatting primitives for the beancount writer.

These are the lowest-level helpers — amount formatting, posting alignment,
narration escaping, and the bank-prefix / portfolio-segment / cash-account
account-name builders. Per-shape builders compose these; nothing in here
depends on a specific render shape.
"""

from __future__ import annotations

from decimal import Decimal

from banking_pipeline.models import Classification, DocumentType, Transaction
from banking_pipeline.writer.profile import profile_for_prefix, resolve_profile

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


def withholding_account(prefix: str, country: str) -> str:
    """Account for foreign withholding tax levied by ``country``.

    Resolves the ``withholding_tax_account_template`` from the profile
    that owns ``prefix`` and formats it with the upper-cased ISO 3166-1
    code — e.g. ``Expenses:Tax:Withholding:US``. The default template is
    bank-agnostic; the ``prefix`` lookup exists only so a bank can
    override the root.
    """

    template = profile_for_prefix(prefix).withholding_tax_account_template
    return template.format(country=country.upper())


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


def gbp_rate_metadata(tx: Transaction) -> str | None:
    """The ``    gbp-rate: "<rate>"`` posting-metadata line, or ``None``.

    Returned for the security posting of a non-GBP trade carrying a
    trade-date GBP rate (``tx.gbp_rate``, populated by the extractor —
    see :mod:`banking_pipeline.fx.gbp_rates`). The value is GBP per 1
    unit of ``tx.currency`` — the currency of the cash consideration,
    which is what UK CGT converts to GBP — quoted as a string so the
    ``Decimal`` precision survives a beancount round-trip.

    The cost basis itself stays in the trade's native currency: the
    ledger is kept in EUR/USD/etc. and the GBP / section-104
    computation happens downstream at tax-report time off this rate.
    Indented one level deeper than the posting so beancount attaches it
    to that posting.

    Returns ``None`` (no line emitted) when no rate is available or the
    consideration is already GBP — a GBP trade needs no conversion, and
    suppressing it keeps every existing ``gbp_rate``-free entry
    byte-identical to today.
    """

    if tx.gbp_rate is None or tx.currency.upper() == "GBP":
        return None
    return f'    gbp-rate: "{tx.gbp_rate}"'


# Map Pictet's printed cost-line descriptions to a canonical beancount
# account segment. Pictet uses "Costes" / "Costs" as a deliberately
# broad term that covers three economically distinct things:
#
#   - **Spread** — bid-ask transaction cost; a market-microstructure
#     cost that's conceptually part of the trade basis, not a service
#     fee. Includes Pictet's ``Spread``, ``Forward spread``, ``Forex
#     spread``, and the combined ``Corretaje y/o spread`` line that
#     Pictet uses on Spanish stock-exchange trades when they don't
#     break broker commission out separately.
#   - **Brokerage** — explicit broker commission for executing a
#     trade. Pictet's bond advices print this as ``Brokerage`` (buy)
#     or ``Commission/Fee`` (sell).
#   - **Tax** — statutory levies (stock-exchange tax, foreign VAT,
#     transaction taxes). Distinct from fees because they're mandated,
#     and because some jurisdictions allow different deductibility
#     treatment.
#   - **Management** — service fees the bank levies for managing or
#     custodying the account. Pictet's quarterly ``Débito de gastos``
#     advice itemises ``Honorarios de gestión``, ``Administration
#     flat fee``, and ``Account maintenance fees`` here.
#   - **Wire** — payment / transfer service fees on outgoing wires.
#
# Anything not in the map falls back to ``Fees`` — generic catch-all
# for descriptions we haven't categorised yet (extending the map is
# preferable to letting the catch-all grow). Lookup is exact-match,
# case-sensitive: Pictet's labels are stable enough that a fuzzy
# match would more often hide a typo than help.
FEE_CATEGORIES: dict[str, str] = {
    # --- Spread / transaction cost ---
    "Spread": "Spread",
    "Forward spread": "Spread",
    "Forex spread": "Spread",
    "Corretaje y/o spread": "Spread",
    # --- Broker commission ---
    "Brokerage": "Brokerage",
    "Commission/Fee": "Brokerage",
    # --- Statutory levies ---
    "Tasa bursátil": "Tax",
    "IVA extranjero": "Tax",
    "Transaction taxes": "Tax",
    # --- Service fees (bank-side) ---
    "Honorarios de gestión": "Management",
    "Administration flat fee (subject to VAT)": "Management",
    "Account maintenance fees": "Management",
    # --- Cash-movement fees ---
    "Payment fees": "Wire",
}


def fee_segment(description: str | None) -> str:
    """Map a Pictet cost-line description to its canonical account segment.

    Returns the matching :data:`FEE_CATEGORIES` value when ``description``
    is a known label; otherwise returns ``"Fees"`` so unknown / missing
    descriptions still land in a parseable account. ``None`` and the
    empty string both fall back to ``"Fees"`` — the in-block aggregate
    ``Costes`` line on a trade advice has no description, and lumping
    those into the catch-all is the right default since we genuinely
    don't know what kind of cost they are.

    Adding support for a new description is data-only: extend
    :data:`FEE_CATEGORIES`. Per-bank category schemes (e.g. an IBKR
    profile that uses a different vocabulary) can override by promoting
    this map onto :class:`~banking_pipeline.writer.profile.BankWriterProfile`
    when the second bank lands.
    """

    if not description:
        return "Fees"
    return FEE_CATEGORIES.get(description, "Fees")
