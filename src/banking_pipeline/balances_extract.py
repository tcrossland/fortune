"""Balance-assertion extractor for Pictet monthly statements.

Pictet's ``Portfolio valuation`` page lists every position's quantity
and every cash sub-account's balance as of the statement's ``As at``
date. Wiring those into beancount ``balance`` directives gives
``bean-check`` an enforceable inventory checkpoint at month-end —
the moment the running ledger drifts from Pictet's records (a missed
ingest, an extraction bug, a writer regression), the next load
fails with the source statement date in the error message.

Assertion date convention
-------------------------
Beancount evaluates ``balance`` directives at the *beginning* of the
asserted date. Pictet's ``As at <day> <Month> <year>`` is the *end*
of that day. We add one day to convert: an "as at 31 December 2025"
statement emits assertions dated 2026-01-01.

Output shape
------------
For each holding row::

    2026-01-01 balance Assets:Pic:K123456001:LU2601001147  2248.13866 LU2601001147

For each cash row::

    2026-01-01 balance Assets:Pic:K123456001:GBP  57909.10 GBP

The portfolio segment comes from the statement header's ``Account
no.:`` / ``N° de cuenta:`` line, sanitised via the same dash-and-
period strip the writer uses on trade advices (so ``K-123456.001``
→ ``K123456001``).

Locale handling
---------------
The ``As at`` anchor accepts both English (``As at 31 December 2025``)
and Spanish (``al 31 Enero 2026``) date strings; the parser returns
``[]`` on the fully-anonymised ``99 Enero 9999`` form. The Spanish
``ESTADO FINANCIERO`` (Madrid account) statement differs in two further
ways the parser handles: the portfolio number prints **bare** on its own
line (``K-NNNNNN.NNN``) rather than behind an ``Account no.`` /
``N° de cuenta`` label, and its cash row leads with the currency
(``<CCY> <bal> <name> <bal> <%>``) instead of the English
``<bal> <name> <CCY> <bal>``. Security rows (quantity-led + ``ISIN:``
next) are layout-agnostic and need no locale handling.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from banking_pipeline.commodities_metadata import normalise_security_name
from banking_pipeline.prices_extract import _parse_statement_date
from banking_pipeline.vanguard_statement import parse_isa_valuation
from banking_pipeline.writer.format import portfolio_segment
from banking_pipeline.writer.profile import VANGUARD_PROFILE

_AS_AT_RE = re.compile(r"\b(?:As\s+at|al)\s+(\d{1,2}\s+\w+\s+\d{4})", re.I)
_ACCOUNT_NO_RE = re.compile(
    r"^(?:Account\s+no\.|N°\s*de\s+cuenta)\s*:\s*([A-Z]-\d{6}\.\d{3})",
    re.M,
)
# Fallback for the Spanish statement, which prints the portfolio number on
# its own line (``K-NNNNNN.NNN``) rather than behind an ``Account no.:``
# label. Anchored to a whole line so the sub-account forms in the
# current-account table (``K-NNNNNN.NNN.00.EUR``) can't match.
_ACCOUNT_BARE_RE = re.compile(r"^([A-Z]-\d{6}\.\d{3})\s*$", re.M)
_ISIN_LINE_RE = re.compile(
    r"\bISIN(?:/Internal\s+ref\.)?\s*:\s*"
    r"([A-Z]{2}[A-Z0-9]{8}(?:[A-Z0-9]{2}|\s[A-Z0-9]))"
)
# Quantity-led row preceding an ISIN block: ``<quantity> <description>``.
# Matched permissively — the ISIN-line scan a few lines later confirms
# this was an actual security row.
_QUANTITY_ROW_RE = re.compile(
    r"^([-\d'.]+)\s+(\S.*)$"
)
# Cash-currency row: ``<balance> <Currency Name> <CCY> <balance>``,
# where the two balance amounts repeat. The currency name is one or
# more capitalised words. The character class spans accented Latin
# letters (``À-ÿ``) as well as ASCII because the Spanish statement
# localises the names (``Dólar USA``, ``Yen Japón``) — an ASCII-only
# class silently failed to match those rows and dropped that currency's
# cash from the valuation entirely.
_CASH_ROW_RE = re.compile(
    r"^([-\d'.]+)\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\s]+?)\s+([A-Z]{3})\s+([-\d'.]+)\s*$"
)
# Spanish cash row: ``<CCY> <balance> <Currency Name> <balance> <%>`` — the
# currency leads, the two balances repeat, and a trailing weight column the
# English layout doesn't have. Requires a digit in each balance so a
# dash-only zero row (``USD - Dólar USA - -``) can't match.
_ES_CASH_ROW_RE = re.compile(
    r"^([A-Z]{3})\s+(-?[\d'][\d'.]*)\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\s]+?)\s+"
    r"(-?[\d'][\d'.]*)\s+[-\d'.]+\s*$"
)
# --- Pictet P mandate "Financial Statement" by-name layout ---------------
# The P (leveraged Lombard) mandate's valuation page prints holdings *by
# name with NO ``ISIN:`` marker* and cash rows with the reference-currency
# (GBP) conversion column + a weight the K layout collapses away. Both the
# K patterns above and the two below run per line; they're mutually
# exclusive by shape (K cash ends after one balance; the by-name cash has a
# second ``<CCY> <balance>`` group + ``%``; K security data is multi-line
# while the by-name security row is single-line with three ``<CCY>``
# tokens), so no P-vs-K sniffing is needed.
#
# Trailing weight column. Normally ``<signed-number>%`` (``113.55%``,
# ``-13.68%``), but in the early months a leveraged base pushed weights
# off-scale and Pictet prints a clamped ``> 999.99%`` / ``< -999.99%`` with
# a comparison operator and a space — so allow an optional leading ``<``/``>``
# and sign. A missing operator here silently dropped every row on the
# opening statement (the coverage guard caught it as an ``empty-statement``).
_WEIGHT = r"[<>]?\s*[-+]?[\d'.]+%\s*$"

# Cash row: ``<qty> <Currency Name> <CCY> <val> <REF-CCY> <val-ref> <%>``
# — exactly TWO currency tokens. The name is letters + spaces only (no
# punctuation/digits), which alone rejects punctuated security names; the
# ``qty ≈ val`` guard in the loop confirms it's a genuine cash row (the
# quantity repeats as the origin-currency valuation) and rejects the
# ``C/A Limit`` credit-limit row (``qty 2'400'000 ≠ val 0.00``).
_FS_CASH_ROW_RE = re.compile(
    r"^(-?[\d'][\d'.]*)\s+"
    r"([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\s]+?)\s+"
    r"([A-Z]{3})\s+(-?[\d'][\d'.]*)\s+"
    r"([A-Z]{3})\s+(-?[\d'][\d'.]*)\s+"
    + _WEIGHT
)
# Security row: ``<qty> <Name> <CCY> <price> <CCY> <val> <REF-CCY> <val-ref>
# <%>`` — exactly THREE currency tokens (price + orig valuation + GBP
# conversion). The name is broad (ETF names carry ``-.&/`` and digits),
# anchored by the trailing three ``<CCY> <number>`` groups + ``%``. Only qty
# and name are captured; the name resolves to a ledger ISIN via
# :func:`commodities_metadata.build_statement_name_index` (holdings carry no
# ISIN here). The 2-token ``C/A Limit`` row can't match (needs 3 tokens).
_FS_SECURITY_ROW_RE = re.compile(
    r"^(-?[\d'][\d'.]*)\s+"
    r"(.+?)\s+"
    r"[A-Z]{3}\s+-?[\d'][\d'.]*\s+"
    r"[A-Z]{3}\s+-?[\d'][\d'.]*\s+"
    r"[A-Z]{3}\s+-?[\d'][\d'.]*\s+"
    + _WEIGHT
)


def _sanitise_portfolio(account_no: str) -> str:
    """Match the writer's ``_portfolio_segment``: strip the dash and
    period from ``K-123456.001`` to get a beancount-compatible
    segment ``K123456001``."""

    return account_no.replace("-", "").replace(".", "")


def _normalise_amount(s: str) -> str:
    return s.replace("'", "")


def extract_balances_from_statement(
    text: str,
    name_to_isin: dict[str, str] | None = None,
) -> list[tuple[str, str, str, str]]:
    """Parse a statement text dump for per-holding / per-currency balances.

    Returns ``(date, account, quantity, commodity)`` 4-tuples ready to
    format as ``<date> balance <account> <quantity> <commodity>``
    directives. The date is one day after the statement's valuation
    anchor (beancount's beginning-of-day evaluation convention).

    Dispatches by bank: the Pictet monthly-statement parser and the
    Vanguard ISA regular-statement parser each run and no-op on the
    other issuer's text (Pictet keys on its ``As at`` / ``K-NNNNNN.NNN``
    markers; Vanguard on its ``Your ISA investments at`` table), so the
    union is safe to return without sniffing the issuer first.

    ``name_to_isin`` (from
    :func:`commodities_metadata.build_statement_name_index`) resolves the
    Pictet P mandate's by-name holding rows to a ledger ISIN; without it,
    those rows are cash-only (securities carry no ISIN on that layout and
    can't be asserted). It has no effect on the K / Vanguard paths.

    Returns ``[]`` when neither parser recognises the document (e.g. the
    fixture's anonymised ``99 Enero 9999`` form, or a drained Vanguard
    statement with no valuation table).
    """

    return _pictet_balances(text, name_to_isin) + _vanguard_balances(text)


def _vanguard_balances(text: str) -> list[tuple[str, str, str, str]]:
    """Vanguard ISA balance assertions from the valuation snapshot.

    Emits a cash-balance assertion (when the statement prints a ``Cash
    account`` row) and one assertion per **non-zero** holding. Wound-down
    positions (movement-pair rows netting to zero) are skipped — asserting
    a 0-unit balance is noise, and the ticker accounts are never closed
    (the writer only closes ISIN-shaped commodities), so there's nothing
    to confirm. Accounts mirror the writer's ``Assets:Vgd:ISA:<acct>:…``
    layout exactly so the assertions line up with the ingested postings.
    """

    valuation = parse_isa_valuation(text)
    if valuation is None:
        return []

    assertion_date = (valuation.statement_date + timedelta(days=1)).isoformat()
    prefix = VANGUARD_PROFILE.account_prefix
    portfolio = portfolio_segment(valuation.account_number)
    rows: list[tuple[str, str, str, str]] = []

    if valuation.cash_balance is not None:
        rows.append(
            (
                assertion_date,
                f"Assets:{prefix}:{portfolio}:GBP",
                str(valuation.cash_balance),
                "GBP",
            )
        )
    for holding in valuation.holdings:
        if holding.quantity == 0:
            continue
        rows.append(
            (
                assertion_date,
                f"Assets:{prefix}:{portfolio}:{holding.ticker}",
                str(holding.quantity),
                holding.ticker,
            )
        )
    return rows


def _pictet_balances(
    text: str, name_to_isin: dict[str, str] | None = None
) -> list[tuple[str, str, str, str]]:
    """Per-holding / per-currency balances from a Pictet monthly statement.

    Handles both the K layout (ISIN-led, multi-line security blocks) and
    the P mandate's by-name "Financial Statement" layout (holdings named,
    no ISIN). ``name_to_isin`` resolves the by-name holdings to a ledger
    ISIN; when it's ``None`` or a name doesn't resolve, that holding emits
    no assertion (the coverage guard reports it instead of guessing).

    Returns ``[]`` when the statement's date or account header can't be
    parsed (e.g. the fixture's anonymised ``99 Enero 9999`` /
    ``K-999999.999`` forms, or a non-Pictet document).
    """

    date_match = _AS_AT_RE.search(text)
    if date_match is None:
        return []
    statement_date = _parse_statement_date(date_match.group(1))
    if statement_date is None:
        return []
    assertion_date = (statement_date + timedelta(days=1)).isoformat()

    acct_match = _ACCOUNT_NO_RE.search(text) or _ACCOUNT_BARE_RE.search(text)
    if acct_match is None:
        return []
    portfolio = _sanitise_portfolio(acct_match.group(1))

    rows: list[tuple[str, str, str, str]] = []
    seen_isins: set[str] = set()
    seen_currencies: set[str] = set()
    lines = text.splitlines()

    for i, line in enumerate(lines):
        stripped = line.strip()

        # --- P mandate by-name layout ("Financial Statement") ---
        # Runs before the K patterns; both no-op on the other's rows (see
        # the pattern comments). A by-name *cash* row is a leading balance,
        # a letters-only currency name, then two ``<CCY> <value>`` groups
        # (origin valuation + GBP conversion) and a weight.
        m_fs_cash = _FS_CASH_ROW_RE.match(stripped)
        if m_fs_cash is not None:
            qty, _name, ccy, _val, _ref_ccy, _val_ref = m_fs_cash.groups()
            # Assert the leading *quantity* — the booked account balance,
            # which is what the ledger holds. The ``Valuation (Orig.)``
            # column adds accrued-but-unpaid interest (the two diverge
            # mid-quarter, e.g. -269'090.40 booked vs -269'977.91 valued),
            # and that accrual is never a ledger posting. The letters-only
            # name + two-currency-token shape already rejects the
            # ``C/A Limit`` credit-limit row (punctuated name), subtotals
            # (word-led), and security rows (three currency tokens), so no
            # symmetry guard is needed. (Substituting the more-precise
            # valuation column, as the K cash path does for its doubled
            # balance, is wrong here: the columns are *different* numbers, not
            # two prints of one — and a whole-unit-rounded quantity already
            # gets ``render``'s ``~ 0.5`` fiat tolerance.) Skip zero balances
            # (the ledger never opens that sub-account) and seen currencies.
            nq = _normalise_amount(qty)
            if ccy not in seen_currencies and Decimal(nq) != 0:
                seen_currencies.add(ccy)
                rows.append(
                    (assertion_date, f"Assets:Pic:{portfolio}:{ccy}", nq, ccy)
                )
            continue
        # A by-name *security* row carries three currency tokens and no
        # ISIN; resolve its name → a ledger ISIN. An unresolved name emits
        # nothing (the coverage guard flags it) rather than guessing. Defer
        # to the ISIN-anchored path below when an ISIN marker is on this line
        # or the next few: the by-name layout has none, so a row that does
        # have one is the K (ISIN-led) layout and must be keyed by ISIN, not
        # by a name-resolved guess.
        m_fs_sec = _FS_SECURITY_ROW_RE.match(stripped)
        if m_fs_sec is not None and not any(
            _ISIN_LINE_RE.search(lines[j])
            for j in range(i, min(i + 4, len(lines)))
        ):
            qty, name = m_fs_sec.group(1), m_fs_sec.group(2)
            isin = (name_to_isin or {}).get(normalise_security_name(name))
            if isin is not None and isin not in seen_isins:
                seen_isins.add(isin)
                rows.append(
                    (
                        assertion_date,
                        f"Assets:Pic:{portfolio}:{isin}",
                        _normalise_amount(qty),
                        isin,
                    )
                )
            continue

        # --- Cash row ---
        # EN ``<bal> <Currency-Name> <CCY> <bal>`` or ES ``<CCY> <bal>
        # <Currency-Name> <bal> <%>`` — both repeat the balance (symmetry
        # guard against layout drift). Checked *before* the security row
        # because an EN cash row also begins with a number and would
        # otherwise be caught by the quantity-led pattern.
        cash: tuple[str, str, str] | None = None  # (ccy, bal1, bal2)
        m_cash = _CASH_ROW_RE.match(stripped)
        if m_cash is not None:
            bal1, _name, ccy, bal2 = m_cash.groups()
            cash = (ccy, bal1, bal2)
        else:
            m_es_cash = _ES_CASH_ROW_RE.match(stripped)
            if m_es_cash is not None:
                ccy, bal1, _name, bal2 = m_es_cash.groups()
                cash = (ccy, bal1, bal2)
        if cash is not None:
            ccy, bal1, bal2 = cash
            n1, n2 = _normalise_amount(bal1), _normalise_amount(bal2)
            # The two balance columns should agree — a symmetry guard
            # against layout drift catching a non-cash row. But the Spanish
            # ``ESTADO FINANCIERO`` rounds the portfolio-currency *display*
            # column to whole units (``31'673``) while the balance column
            # keeps cents (``31'673.01``), so an exact-equality guard
            # silently dropped that currency's cash whenever the display
            # column rounded. Compare numerically with a half-unit
            # tolerance instead (the max round-to-nearest-integer error),
            # which still rejects a genuinely mismatched cross-currency row.
            if abs(Decimal(n1) - Decimal(n2)) <= Decimal("0.5"):
                if ccy not in seen_currencies:
                    seen_currencies.add(ccy)
                    # Keep the more precise column (the one carrying cents)
                    # so the assertion matches the ledger to the cent.
                    balance = n2 if "." in n2 else n1
                    # Skip zero-balance currencies. A statement lists a
                    # residual ``0.00`` line for currencies the account
                    # briefly held; the ledger never opens that
                    # sub-account, so asserting ``0`` against it trips
                    # bean-check's "Invalid reference to inactive
                    # account". A zero assertion is low-value anyway.
                    if Decimal(balance) != 0:
                        rows.append(
                            (
                                assertion_date,
                                f"Assets:Pic:{portfolio}:{ccy}",
                                balance,
                                ccy,
                            )
                        )
                continue

        # --- Security row ---
        # A quantity-led row whose next line carries an ``ISIN:``
        # marker is a per-position balance entry. We scan up to ~3
        # lines forward to tolerate the occasional blank between the
        # quantity row and the ISIN line.
        m_qty = _QUANTITY_ROW_RE.match(stripped)
        if m_qty is not None:
            # The newer statement layout concatenates the quantity row and
            # the ISIN marker onto a single line, joined by a stray control
            # character (``1'743.00 Eleva… <0xFFFE> ISIN: LU…``). The
            # forward scan below only looks at *following* lines, so check
            # this line first — otherwise the holding is silently dropped.
            m_isin_same = _ISIN_LINE_RE.search(stripped)
            if m_isin_same is not None:
                isin = m_isin_same.group(1).replace(" ", "")
                if isin not in seen_isins:
                    seen_isins.add(isin)
                    rows.append(
                        (
                            assertion_date,
                            f"Assets:Pic:{portfolio}:{isin}",
                            _normalise_amount(m_qty.group(1)),
                            isin,
                        )
                    )
                continue
            for j in range(i + 1, min(i + 4, len(lines))):
                # A later quantity-led row means a *new* holding has
                # started before we found an ISIN — so the ISIN that
                # follows belongs to it, not to this row. Stop scanning
                # so a non-holding numeric line that merely precedes a
                # real holding (e.g. a ``2'400'000.00 C/A Limit Gbp …``
                # lombard credit-limit row on the P portfolio's
                # valuation page) can't annex the next holding's ISIN
                # and assert a bogus quantity against it.
                if _QUANTITY_ROW_RE.match(lines[j].strip()):
                    break
                m_isin = _ISIN_LINE_RE.search(lines[j])
                if m_isin is None:
                    continue
                isin = m_isin.group(1).replace(" ", "")
                if isin in seen_isins:
                    break
                seen_isins.add(isin)
                quantity = _normalise_amount(m_qty.group(1))
                rows.append(
                    (
                        assertion_date,
                        f"Assets:Pic:{portfolio}:{isin}",
                        quantity,
                        isin,
                    )
                )
                break

    return rows


# --- Coverage guard -------------------------------------------------------
# A deliberately permissive *re-detector* run alongside the production
# parser to catch rows it silently drops. The two cash bugs this guards
# against — a whole-unit-rounded display column and an accented currency
# name (``Dólar USA``) — both made a real cash row fail the strict parser
# and vanish from the valuation, understating net cash with no error. The
# guard re-finds cash rows and ISIN markers with looser patterns (Unicode
# name class, balance-equality tolerance) and reports any the real parser
# missed, so a future tightening of the parser surfaces as a coverage gap
# rather than a silent shortfall. It is FX-free (a pure presence check, no
# valuation), so unlike a value reconciliation it can't be fooled by the
# HMRC-monthly-vs-statement-spot FX drift that legitimately moves a
# multi-currency book's GBP total by ~10%.

# Permissive cash-row re-detectors: the name is any run of non-digits
# (so accents pass), and the row must end right after the second balance
# (``$``-anchored) so a security price row — which carries further
# columns — can't match.
_LOOSE_EN_CASH_RE = re.compile(r"^([-\d'.]+)\s+\D+?\s+([A-Z]{3})\s+([-\d'.]+)\s*$")
_LOOSE_ES_CASH_RE = re.compile(
    r"^([A-Z]{3})\s+(-?[\d'][\d'.]*)\s+\D+?\s+(-?[\d'][\d'.]*)\s+[-\d'.]+\s*$"
)
# Permissive P by-name cash re-detector: leading balance, any non-digit
# name (accents pass), then two ``<CCY> <value>`` groups + a weight. Groups
# the leading balance and the currency; the two value columns diverge by
# accrued interest, so — unlike the K/ES detectors — there is no doubled-
# balance symmetry to check. Presence-only, so a dropped P cash row (e.g.
# the accrued-interest divergence that used to fail the parser's guard)
# surfaces as a gap instead of a silent shortfall.
_LOOSE_FS_CASH_RE = re.compile(
    r"^(-?[\d'][\d'.]*)\s+\D+?\s+([A-Z]{3})\s+-?[\d'][\d'.]*\s+"
    r"[A-Z]{3}\s+-?[\d'][\d'.]*\s+" + _WEIGHT
)


@dataclass(frozen=True)
class CoverageGap:
    """A holding or cash row present in the statement text that the
    production parser did not extract.

    Kinds:

    * ``cash`` / ``security`` — a cash currency / ISIN-marked holding the
      loose re-detector found but the strict parser dropped.
    * ``unresolved-holding`` — a P by-name security row whose display name
      didn't resolve to a ledger ISIN (needs a ``statement_names`` alias in
      ``commodities.toml``). ``detail`` is the unresolved display name.
    * ``empty-statement`` — a document recognised as a valuation statement
      that nonetheless extracted zero rows (a whole-statement hole, which
      the P by-name layout produced before it had a parser path).
    * ``unreadable`` — the file couldn't be loaded (``detail`` is the error).
    """

    kind: str
    detail: str

    @property
    def message(self) -> str:
        """Human-readable one-line description for the CLI."""

        match self.kind:
            case "cash":
                return f"cash {self.detail} present in statement but not extracted"
            case "security":
                return (
                    f"holding {self.detail} present in statement but not extracted"
                )
            case "unresolved-holding":
                return (
                    f"by-name holding {self.detail!r} did not resolve to a ledger "
                    "ISIN — add a statement_names alias in commodities.toml"
                )
            case "empty-statement":
                return "recognised valuation extracted zero rows (whole-statement drop)"
            case "unreadable":
                return f"could not read file: {self.detail}"
            case _:
                return f"{self.kind} {self.detail}"


# The portfolio-total line, EN ``Total portfolio … <CCY> <amount> <%>`` /
# ES ``Total de la cartera <CCY> <amount>``. Used to tell a genuine
# whole-statement drop (non-zero total, zero rows extracted) from a
# legitimately-empty statement (an opening or drained account whose total
# is zero).
_TOTAL_LINE_RE = re.compile(r"Total portfolio|Total de la cartera", re.I)
# Strip the trailing weight (``100.00%`` / ``> 999.99%``) off a total line —
# same shape as the row ``_WEIGHT``, plus the leading space.
_TRAILING_PCT_RE = re.compile(r"\s*" + _WEIGHT)
_NUMBER_RE = re.compile(r"-?[\d'][\d'.]*")


def _pictet_header_ok(text: str) -> bool:
    """True when ``text`` has a parseable Pictet valuation header (an
    ``As at`` / ``al`` date **and** a ``K-NNNNNN.NNN`` account). Excludes
    the anonymised ``99 Enero 9999`` fixture (date doesn't parse) and any
    non-Pictet document."""

    date_match = _AS_AT_RE.search(text)
    return (
        date_match is not None
        and _parse_statement_date(date_match.group(1)) is not None
        and (
            _ACCOUNT_NO_RE.search(text) is not None
            or _ACCOUNT_BARE_RE.search(text) is not None
        )
    )


def _pictet_total_nonzero(text: str) -> bool:
    """True when any ``Total portfolio`` / ``Total de la cartera`` line
    carries a non-zero amount.

    The discriminator for the ``empty-statement`` guard: a whole-statement
    drop (the P by-name bug) shows a non-zero portfolio total but extracts
    zero rows, whereas a legitimately-empty statement (a freshly-opened or
    drained account) reports a zero total. The trailing weight column is
    stripped so the value — not the ``100.00%`` weight — is read.
    """

    for line in text.splitlines():
        if not _TOTAL_LINE_RE.search(line):
            continue
        body = _TRAILING_PCT_RE.sub("", line.strip())
        nums = _NUMBER_RE.findall(body)
        if not nums:
            continue
        try:
            if Decimal(_normalise_amount(nums[-1])) != 0:
                return True
        except (ArithmeticError, ValueError):
            continue
    return False


def statement_coverage_gaps(
    text: str, name_to_isin: dict[str, str] | None = None
) -> list[CoverageGap]:
    """Re-scan a statement and report rows the production parser dropped.

    For a document the parser doesn't recognise at all it returns ``[]``
    (nothing to reconcile against) — *unless* it's a Pictet valuation with a
    non-zero portfolio total that extracted nothing, which is a
    whole-statement coverage hole (``empty-statement``). A zero-total
    statement (a freshly-opened or drained account) is legitimately empty,
    as is a Vanguard statement (its own parser handles emptiness). Otherwise
    it compares the cash currencies, ISINs, and by-name holdings visible in
    the text against what :func:`extract_balances_from_statement` captured,
    and returns one gap per missed row.
    """

    extracted = extract_balances_from_statement(text, name_to_isin)
    if not extracted:
        if _pictet_header_ok(text) and _pictet_total_nonzero(text):
            return [CoverageGap("empty-statement", "no rows extracted")]
        return []
    def _is_ccy(code: str) -> bool:
        return len(code) == 3 and code.isalpha()

    extracted_cash = {c for _d, _a, _q, c in extracted if _is_ccy(c)}
    extracted_isins = {c for _d, _a, _q, c in extracted if not _is_ccy(c)}

    gaps: list[CoverageGap] = []

    seen_cash: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        detected: tuple[str, str, str] | None = None  # (ccy, bal1, bal2)
        m_en = _LOOSE_EN_CASH_RE.match(stripped)
        if m_en is not None:
            bal1, ccy, bal2 = m_en.groups()
            detected = (ccy, bal1, bal2)
        else:
            m_es = _LOOSE_ES_CASH_RE.match(stripped)
            if m_es is not None:
                ccy, bal1, bal2 = m_es.groups()
                detected = (ccy, bal1, bal2)
        if detected is None:
            continue
        ccy, bal1, bal2 = detected
        if ccy in seen_cash:
            continue
        a, b = Decimal(_normalise_amount(bal1)), Decimal(_normalise_amount(bal2))
        # The doubled balances should agree (a whole-unit-rounded display
        # column differs by < 1); a line where they diverge isn't a cash
        # row, so it doesn't count as one the parser ought to have caught.
        if abs(a - b) > max(Decimal("0.5"), abs(a) / 100):
            continue
        seen_cash.add(ccy)
        if a != 0 and ccy not in extracted_cash:
            gaps.append(CoverageGap("cash", ccy))

    # P by-name cash rows (two value columns + weight) — a separate loose
    # pass because they carry no doubled-balance symmetry the K/ES detectors
    # above key on. Reports a non-zero currency the parser didn't capture.
    for line in text.splitlines():
        m_fs = _LOOSE_FS_CASH_RE.match(line.strip())
        if m_fs is None:
            continue
        bal, ccy = m_fs.group(1), m_fs.group(2)
        if ccy in seen_cash:
            continue
        seen_cash.add(ccy)
        if Decimal(_normalise_amount(bal)) != 0 and ccy not in extracted_cash:
            gaps.append(CoverageGap("cash", ccy))

    seen_isins: set[str] = set()
    for m in _ISIN_LINE_RE.finditer(text):
        isin = m.group(1).replace(" ", "")
        if isin in seen_isins:
            continue
        seen_isins.add(isin)
        if isin not in extracted_isins:
            gaps.append(CoverageGap("security", isin))

    # By-name (P mandate) security rows carry no ISIN; a row whose display
    # name doesn't resolve to a ledger commodity emits no assertion, so
    # report it — the missing ``statement_names`` alias is then visible
    # rather than silently understating the holdings. Skip rows with an ISIN
    # marker nearby (the K layout, keyed by ISIN not name), mirroring the
    # parser's deferral so an ISIN-anchored holding isn't mis-flagged.
    index = name_to_isin or {}
    guard_lines = text.splitlines()
    seen_names: set[str] = set()
    for i, line in enumerate(guard_lines):
        m_sec = _FS_SECURITY_ROW_RE.match(line.strip())
        if m_sec is None or any(
            _ISIN_LINE_RE.search(guard_lines[j])
            for j in range(i, min(i + 4, len(guard_lines)))
        ):
            continue
        name = m_sec.group(2).strip()
        key = normalise_security_name(name)
        if key in seen_names:
            continue
        seen_names.add(key)
        if key not in index:
            gaps.append(CoverageGap("unresolved-holding", name))

    return gaps


def coverage_report(
    statement_files: Iterable[Path],
    name_to_isin: dict[str, str] | None = None,
) -> list[tuple[Path, list[CoverageGap]]]:
    """Run :func:`statement_coverage_gaps` over each statement file and
    return one ``(path, gaps)`` entry per file that has at least one gap.

    The reconciliation guard behind ``balances --strict``: a holding or
    cash row visible in a statement that the parser failed to extract
    would otherwise vanish from the valuation with no error (the two cash
    bugs and the concatenated-ISIN bug all did exactly that). An
    unreadable file is reported as a gap rather than crashing the run.
    """

    out: list[tuple[Path, list[CoverageGap]]] = []
    for path in statement_files:
        try:
            if path.suffix.lower() == ".txt":
                text = path.read_text(encoding="utf-8")
            else:
                from banking_pipeline.extractors import load_pdf

                text = load_pdf(path).text
        except Exception as exc:  # noqa: BLE001 — report, don't abort the batch
            out.append((path, [CoverageGap("unreadable", str(exc))]))
            continue
        gaps = statement_coverage_gaps(text, name_to_isin)
        if gaps:
            out.append((path, gaps))
    return out


def merge_balances(
    *balance_lists: Iterable[tuple[str, str, str, str]],
) -> list[tuple[str, str, str, str]]:
    """Merge multiple balance-tuple lists into a deduplicated, sorted
    sequence. Last-occurrence-wins on duplicate ``(date, account)``
    pairs."""

    seen: dict[tuple[str, str], tuple[str, str, str, str]] = {}
    for balance_list in balance_lists:
        for row in balance_list:
            seen[(row[0], row[1])] = row
    return sorted(seen.values())


_DEFAULT_HEADER = (
    ";; Balance assertions extracted from Pictet monthly statements.\n"
    ";;\n"
    ";; One ``<date> balance <account> <quantity> <commodity>``\n"
    ";; directive per holding and per cash sub-account, sourced from\n"
    ";; the ``Portfolio valuation`` page on each statement. Assertion\n"
    ";; dates are one day after the statement's ``As at`` anchor —\n"
    ";; beancount evaluates balance directives at the beginning of\n"
    ";; the asserted date, so end-of-2025 maps to 2026-01-01.\n"
    ";;\n"
    ";; Regenerate via ``banking-pipeline balances data/`` after\n"
    ";; adding new statements. ``portfolio.beancount`` should\n"
    ";; ``include`` this file so ``bean-check`` runs the assertions\n"
    ";; on every load.\n"
)


def render(
    rows: Iterable[tuple[str, str, str, str]],
    header: str = _DEFAULT_HEADER,
) -> str:
    """Format the extracted balance tuples as a beancount file body."""

    lines = [header.rstrip("\n"), ""]
    for assertion_date, account, quantity, commodity in rows:
        # A statement that rounds cash to whole units (the Spanish locale
        # prints ``EUR 10'080``) can't be asserted to the cent against the
        # precise ledger under the tight ``EUR:0.005`` default tolerance, so
        # a whole-number *fiat* balance carries an explicit ``~ 0.5``
        # rounding tolerance. Security quantities (ISIN / ticker commodities)
        # and cent-precise cash assert exactly. The padding between account
        # and quantity is cosmetic — beancount ignores the spacing.
        tol = (
            " ~ 0.5"
            if len(commodity) == 3 and commodity.isalpha() and "." not in quantity
            else ""
        )
        lines.append(
            f"{assertion_date} balance {account}  {quantity}{tol} {commodity}"
        )
    lines.append("")
    return "\n".join(lines)


def generate(
    data_dir: Path,
    statement_files: Iterable[Path],
    output: Path | None = None,
    name_to_isin: dict[str, str] | None = None,
) -> tuple[Path, int]:
    """Parse the supplied statement files and write a beancount
    balance-assertions file under ``data_dir``. Returns
    ``(output_path, row_count)``.

    The output file is overwritten on every run; aggregating across
    multiple invocations means caller passes the full statement set
    each time. Statements that don't parse (anonymised dates etc.)
    contribute zero rows silently. ``name_to_isin`` resolves the P
    mandate's by-name holdings (see
    :func:`extract_balances_from_statement`).
    """

    if output is None:
        output = data_dir / "balances.beancount"

    rows: list[tuple[str, str, str, str]] = []
    for path in statement_files:
        if path.suffix.lower() == ".txt":
            text = path.read_text(encoding="utf-8")
        else:
            from banking_pipeline.extractors import load_pdf
            text = load_pdf(path).text
        rows.extend(extract_balances_from_statement(text, name_to_isin))

    rows = merge_balances(rows)
    output.write_text(render(rows), encoding="utf-8")
    return output, len(rows)
